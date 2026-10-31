"""The fixed workflow: branch → prompt → plan → implement ⇄ review → document → merge → improve.

The stage graph is `advance()`, a pure function from (run state, finished stage, its result) to the
next step. Each stage is a handler (`Workflow.stage_<name>(inputs) -> StageResult`) that runs on its own
given the run state and the job inputs, so a queued run's stages can run on different machines. Two
drivers walk the same graph: `Workflow.execute` in-process for `sf run`, and `run_job` +
`complete_stage` for stage jobs claimed from the Postgres queue.
"""

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from sf import artifacts, queue, store
from sf import git as g
from sf.agents import load_agent, parse_agent, render_task, render_task_text
from sf.run import STAGES, Run, RunState, Usage, now
from sf.runner import Display, StageFailed, run_agent
from sf.target import (
    STANDARDS_FILES,
    StandardsError,
    TrunkError,
    discover_documentation,
    install_missing_standards,
    load_config,
    resolve_trunk,
    run_validation,
    validation_commands,
)


class Aborted(Exception):
    pass


class Human(Protocol):
    def approve_plan(self, plan: str) -> bool: ...
    def cap_reached(self, loops: int) -> Any: ...


class Notifier(Display, Protocol):
    def note(self, text: str) -> None: ...


def _run_id(factory: Path) -> str:
    """The current second, suffixed `-N` until no `runs/<id>` directory uses it.

    Two runs started in the same second, which the watch monitor makes easy, must not share a run
    directory, state file or event stream.
    """
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id, n = base, 1
    while (factory / "runs" / run_id).exists():
        run_id = f"{base}-{n}"
        n += 1
    return run_id


def new_run(factory: Path, target: Path, prompt: str, approve_plan: bool) -> Run:
    target = target.resolve()
    g.git(target, "rev-parse", "--git-dir")
    run_id = _run_id(factory)
    trunk = resolve_trunk(target, load_config(target))
    state = RunState(
        run_id=run_id,
        target=str(target),
        trunk=trunk,
        branch=f"sf/{run_id}",
        approve_plan=approve_plan,
    )
    run = Run(factory, state)
    (run.dir / "prompt.md").write_text(prompt.rstrip() + "\n")
    run.log("run_created", target=str(target), trunk=trunk)
    run.save()
    return run


@dataclass(frozen=True)
class StageResult:
    """What a handler reports: an outcome tag for `advance`, a summary, and data for the next stage."""

    outcome: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Next:
    """Run `stage` with `inputs`; `completes` = (phase, summary) to record as finished first."""

    stage: str
    inputs: dict[str, Any] = field(default_factory=dict)
    completes: tuple[str, str] | None = None


@dataclass(frozen=True)
class CapReached:
    """The loop cap was reached without approval; `more` is the implement step if more loops are allowed."""

    loops: int
    more: Next


@dataclass(frozen=True)
class Finished:
    status: Literal["done", "aborted", "failed"]
    reason: str
    completes: tuple[str, str] | None = None


Step = Next | CapReached | Finished

# Job stages; `review` belongs to the `implement` phase recorded in RunState.stage/completed/usage.
PHASE = {"review": "implement"}
STAGE_AGENT = {"plan": "planner", "implement": "implementer", "review": "reviewer", "document": "documenter",
               "improve": "improver"}
HARNESS_CAP = {"pi": "harness:pi", "claude-code": "harness:claude", "codex": "harness:codex"}
MAX_MERGE_CONFLICTS = 1  # merge conflicts / merge-validation failures each earn one more implement⇄review pass


def advance(state: RunState, stage: str | None, result: StageResult | None, *, human: bool = False) -> Step:
    """The stage graph: the step after `stage` finished with `result` (`stage=None` starts the run).

    Pure: reads `state` (loop, max_loops, merge_conflicts, approve_plan) and never mutates it.
    `human` says whether a person can answer plan approval; queued runs cannot in v1.
    """
    if stage is None:
        if state.approve_plan and not human:
            return Finished("failed", "plan approval (--approve-plan) is not supported for queued runs in v1")
        return Next("branch")
    assert result is not None
    done = (stage, result.summary)
    match stage, result.outcome:
        case "branch", _:
            return Next("prompt", completes=done)
        case "prompt", _:
            return Next("plan", completes=done)
        case "plan", _:
            return Next("implement", {"loop": state.loop + 1, "feedback": ""}, completes=done)
        case "implement", "validation_failed":
            return _another_loop(state, result.data["feedback"])
        case "implement", _:
            return Next("review", {"loop": state.loop, "validation_report": result.data["validation_report"]})
        case "review", "approve":
            return Next("document", completes=("implement", f"approved in loop {state.loop}"))
        case "review", _:
            return _another_loop(state, result.data["feedback"])
        case "document", _:
            return Next("merge", completes=done)
        case "merge", "merged":
            return Next("improve", completes=done)
        case "merge", "conflict" | "validation_failed":
            if state.merge_conflicts > MAX_MERGE_CONFLICTS:
                return Finished("aborted", result.summary)
            return Next("implement", {"loop": state.loop + 1, "feedback": result.data["feedback"]})
        case "improve", _:
            return Finished("done", "run done", completes=done)
    raise ValueError(f"no transition from stage {stage!r} with outcome {result.outcome!r}")


def _another_loop(state: RunState, feedback: str) -> Step:
    more = Next("implement", {"loop": state.loop + 1, "feedback": feedback})
    if state.loop >= state.max_loops + min(state.merge_conflicts, MAX_MERGE_CONFLICTS):
        return CapReached(state.loop, more)
    return more


def record_step(state: RunState, step: Step) -> None:
    """Apply a step's bookkeeping to the run state (completed phases, results, pending, terminal status)."""
    if not isinstance(step, CapReached) and step.completes:
        phase, summary = step.completes
        state.results[phase] = summary
        if phase not in state.completed:
            state.completed.append(phase)
    if isinstance(step, Next):
        state.pending = {"stage": step.stage, "inputs": step.inputs}
    elif isinstance(step, Finished):
        state.pending = None
        state.status = step.status


class _Quiet:
    """Display for unattended stage jobs."""

    def note(self, text: str) -> None:
        pass

    def agent_event(self, inv: Any, event: dict[str, Any]) -> None:
        pass


class Workflow:
    """Stage handlers for one run, plus the in-process driver used by `sf run` / `sf resume`.

    `workspace` is the git working tree the handlers act on; it defaults to the target checkout. Queued
    jobs pass a fresh clone of the target `remote` (`git.checkout_job`), and the merge then lands on that
    remote under a per-target advisory lock instead of merging in the checkout.
    """

    def __init__(self, run: Run, human: Human | None, display: Notifier | None, workspace: Path | None = None,
                 remote: str | None = None):
        self.run = run
        self.state = run.state
        self.human = human
        self.display = display or _Quiet()
        self.factory = run.factory
        self.workspace = workspace or run.target
        self.remote = remote

    # -- in-process driver ---------------------------------------------------
    def execute(self) -> None:
        self.state.status = "running"
        step = self._first_step()
        while True:
            record_step(self.state, step)
            self.run.save()
            if isinstance(step, Finished):
                break
            if isinstance(step, CapReached):
                step = self._cap_reached(step)
                continue
            result = self._stage(step.stage, step.inputs)
            step = advance(self.state, step.stage, result, human=self.human is not None)
        if step.status != "done":
            self.state.results[self.state.stage] = f"{step.status}: {step.reason}"
            self.run.log(f"run_{step.status}", reason=step.reason)
            self._checkpoint(step.reason)
            raise Aborted(step.reason) if step.status == "aborted" else StageFailed(step.reason)
        self.run.log("run_done")
        self._checkpoint("done")

    def _first_step(self) -> Step:
        """Resume from the pending step, else the first uncompleted phase (e.g. `sf improve`)."""
        if self.state.pending:
            return Next(self.state.pending["stage"], self.state.pending["inputs"])
        if not self.state.completed:
            return advance(self.state, None, None, human=self.human is not None)
        for stage in STAGES:
            if stage not in self.state.completed:
                inputs = {"loop": self.state.loop + 1, "feedback": ""} if stage == "implement" else {}
                return Next(stage, inputs)
        return Finished("done", "run done")

    def _cap_reached(self, cap: CapReached) -> Step:
        self.state.cap_reached = True
        self.run.log("loop_cap_reached", loops=cap.loops)
        self.run.save()
        choice = self.human.cap_reached(cap.loops)
        self.run.log("cap_decision", choice=choice)
        if choice == "merge":
            return Next("document", completes=("implement", f"merged without approval after {cap.loops} loops"))
        if choice == "abort":
            self.state.results["implement"] = f"aborted: no approval after {cap.loops} loops"
            self.state.status = "aborted"
            self.run.log("stage_end", result=self.state.results["implement"])
            self._checkpoint("aborted")
            raise Aborted(f"no approval after {cap.loops} loops")
        self.state.max_loops += int(choice)
        return cap.more

    def _stage(self, stage: str, inputs: dict[str, Any] | None = None) -> StageResult:
        """Run one handler with stage events and failure status; failures propagate."""
        self.state.stage = PHASE.get(stage, stage)
        self.run.log("stage_start")
        self.display.note(f"stage [bold]{stage}[/]")
        self.run.save()
        try:
            result = self.run_stage(stage, inputs or {})
        except Aborted as e:
            self._finish_stage(f"aborted: {e}", status="aborted")
            raise
        except (StageFailed, g.GitError) as e:
            self._finish_stage(f"failed: {e}", status="failed")
            raise
        self.run.log("stage_end", result=result.summary)
        self._checkpoint(result.summary)
        return result

    def run_stage(self, stage: str, inputs: dict[str, Any]) -> StageResult:
        """Execute stage `stage`'s handler on this run's state and the job `inputs`."""
        return getattr(self, f"stage_{stage}")(inputs)

    def _finish_stage(self, result: str, status: str | None = None) -> None:
        self.state.results[self.state.stage] = result
        if status:
            self.state.status = status  # type: ignore[assignment]
        self.run.log("stage_end", result=result)
        self._checkpoint(result)

    def _checkpoint(self, what: str) -> None:
        """Persist run state; `runs/` is git-ignored, so checkpoints are not committed."""
        self.run.save()

    def git(self, repo: Path, *args: str, check: bool = True) -> str:
        """A state-changing git command, logged as a `git_<subcommand>` event for the dashboard."""
        self.run.log(f"git_{args[0]}", repo=self._repo_name(repo), args=list(args[1:]))
        return g.git(repo, *args, check=check)

    def commit(self, repo: Path, message: str, paths: tuple[str, ...] = (".",)) -> bool:
        """`g.commit_all`, logged as a `git_commit` event when there is something to commit.

        Logged before committing so a checkpoint's own event lands in that checkpoint's commit.
        """
        if not g.git(repo, "status", "--porcelain", "--", *paths).strip():
            return False
        self.run.log("git_commit", repo=self._repo_name(repo), message=message)
        return g.commit_all(repo, message, paths)

    def revert(self, repo: Path, paths: list[str]) -> None:
        """`g.revert_paths`, logged as a `git_revert` event."""
        self.run.log("git_revert", repo=self._repo_name(repo), paths=paths)
        g.revert_paths(repo, paths)

    def _repo_name(self, repo: Path) -> str:
        return "factory" if repo == self.factory else "target"

    def _agent(self, name: str, context: dict[str, str], cwd: Path | None = None) -> dict[str, Any]:
        try:
            documents = discover_documentation(self.workspace)
        except (g.GitError, OSError, ValueError) as e:
            self.run.log("documentation_discovery_failed", agent=name, target=str(self.workspace), error=str(e))
            raise StageFailed(f"documentation discovery for {self.workspace}: {e}") from e
        documentation = (f"Target repository: {json.dumps(str(self.workspace))}\n"
                         f"Repository-relative Markdown paths:\n{json.dumps(documents, ensure_ascii=False, indent=2)}")
        values = context | {"documentation": documentation}
        try:
            if self.run.db is None:
                agent = load_agent(self.factory / "agents", name)
                task = render_task(self.factory / "agents", name, values)
            else:  # queued: the run's config snapshot, so every stage uses identical prompts
                agent, task = self._snapshot_agent(name, values)
        except ValueError as e:
            self.run.log("prompt_invalid", agent=name, error=str(e))
            raise StageFailed(str(e)) from e
        return run_agent(self.run, agent, task, cwd or self.workspace, self.display)

    def _snapshot_agent(self, name: str, values: dict[str, str]) -> tuple[Any, str]:
        agents = store.load_config(self.run.db, self.state.run_id)["agents"]
        source = f"run_config snapshot of {self.state.run_id}"
        try:
            agent = parse_agent(agents[f"{name}.md"])
            template = agents[f"{name}.user.md"]
        except Exception as e:
            raise ValueError(f"invalid agent definition {name}.md in {source}: {e!r}") from e
        if agent.name != name:
            raise ValueError(f"{name}.md in {source}: name '{agent.name}' does not match file name")
        return agent, render_task_text(name, template, values)

    def _templates(self) -> tempfile.TemporaryDirectory[str] | None:
        """Queued runs: the snapshot's standards templates in a temp dir (None: use factory/templates)."""
        if self.run.db is None:
            return None
        tmp = tempfile.TemporaryDirectory(prefix="sf-templates-")
        for name, text in store.load_config(self.run.db, self.state.run_id)["templates"].items():
            (Path(tmp.name) / name).write_text(text)
        return tmp

    def read_doc(self, name: str) -> str:
        """A run document (prompt.md, plan.md, review-N.md): from the store on queued runs, else runs/<id>/."""
        if self.run.db is None:
            return (self.run.dir / name).read_text()
        return store.get_document(self.run.db, self.state.run_id, name)

    def write_doc(self, name: str, text: str) -> None:
        """Write a run document to the run dir (uploaded with the job's artifacts) and, queued, the store."""
        (self.run.dir / name).write_text(text)
        if self.run.db is not None:
            store.put_document(self.run.db, self.state.run_id, name, text)

    def _commit_target(self, what: str) -> bool:
        return self.commit(self.workspace, f"sf {self.state.run_id}: {what}")

    def _on_branch(self) -> None:
        if g.git(self.workspace, "branch", "--show-current").strip() != self.state.branch:
            self.git(self.workspace, "checkout", self.state.branch)

    # -- stage handlers: each runs alone given the run state and its job inputs -------------
    def stage_branch(self, inputs: dict[str, Any]) -> StageResult:
        target = self.workspace
        if g.git(target, "status", "--porcelain").strip():
            raise StageFailed(f"target {target} has uncommitted changes")
        try:  # re-resolved so a resumed run picks up a fixed trunk or .sf.toml
            self.state.trunk = resolve_trunk(target, load_config(target))
        except TrunkError as e:
            raise StageFailed(str(e)) from e
        if g.git(target, "branch", "--list", self.state.branch).strip():
            self.git(target, "checkout", self.state.branch)
        else:
            self.git(target, "checkout", "-b", self.state.branch, self.state.trunk)
        snapshot = self._templates()
        try:
            copied = install_missing_standards(target, Path(snapshot.name) if snapshot else self.factory / "templates")
        except StandardsError as e:
            raise StageFailed(str(e)) from e
        finally:
            if snapshot:
                snapshot.cleanup()
        if copied:
            self.run.log("standards_installed", files=copied)
            self._commit_target(f"add starter standards ({', '.join(copied)})")
        return StageResult("ok", f"on {self.state.branch}" + (f"; installed {', '.join(copied)}" if copied else ""))

    def stage_prompt(self, inputs: dict[str, Any]) -> StageResult:
        return StageResult("ok", "prompt saved")

    def stage_plan(self, inputs: dict[str, Any]) -> StageResult:
        self._on_branch()
        prompt = self.read_doc("prompt.md")
        out = self._agent(
            "planner",
            {"request": prompt},
        )
        plan = out["plan"]
        self.write_doc("plan.md", plan.rstrip() + "\n")
        self._checkpoint("plan written")
        if self.state.approve_plan and not self.human.approve_plan(plan):
            raise Aborted("plan rejected by human")
        plan_path = f"docs/plans/{self.state.run_id}.md"
        (self.workspace / plan_path).parent.mkdir(parents=True, exist_ok=True)
        (self.workspace / plan_path).write_text(plan.rstrip() + "\n")
        self.commit(self.workspace, f"sf {self.state.run_id}: plan", (plan_path,))
        return StageResult("ok", "plan approved" if self.state.approve_plan else "plan written")

    def stage_implement(self, inputs: dict[str, Any]) -> StageResult:
        """Implementer pass `inputs["loop"]` with `inputs["feedback"]`, commit, then the validation gate."""
        n = self.state.loop = inputs["loop"]
        self._on_branch()
        commands = validation_commands(self.workspace, load_config(self.workspace))
        self.run.log("loop_start")
        plan = self.read_doc("plan.md")
        out = self._agent("implementer", {"plan": plan, "feedback": inputs.get("feedback", "")})
        self._commit_target(f"implement (loop {n})")
        self.run.log("implemented", summary=out.get("summary"))

        if not commands:
            self.run.log("validation_skipped", reason="no commands in docs/VALIDATION.md")
            report = f"Validation gate (loop {n}): SKIPPED — no validation commands configured."
            return StageResult("validation_skipped", f"loop {n} validation skipped", {"validation_report": report})
        log = self.run.dir / f"validation-{n}.log"
        ok = run_validation(self.workspace, commands, log)
        self.state.results[f"validation-{n}"] = "passed" if ok else "failed"
        self.run.log("validation", passed=ok, log=log.name)
        if not ok:
            feedback = f"# Latest validation output (failed)\n\n```\n{log.read_text()}\n```"
            return StageResult("validation_failed", f"loop {n} validation failed", {"feedback": feedback})
        diagnostics = log.read_text()
        if len(diagnostics) > 12000:
            diagnostics = "[diagnostic output truncated; last 12000 characters]\n" + diagnostics[-12000:]
        report = (
            f"Validation gate (loop {n}): PASS — all executed commands exited 0.\n\n"
            "Executed commands:\n" + "\n".join(f"$ {command}" for command in commands)
            + "\n\nRunner diagnostic output:\n" + diagnostics
        )
        return StageResult("validated", f"loop {n} validation passed", {"validation_report": report})

    def stage_review(self, inputs: dict[str, Any]) -> StageResult:
        """Review loop `inputs["loop"]` with that loop's runner-generated validation gate as evidence."""
        n = inputs["loop"]
        self._on_branch()
        plan = self.read_doc("plan.md")
        out = self._agent(
            "reviewer",
            {"plan": plan, "trunk": self.state.trunk, "validation_report": inputs["validation_report"]},
        )
        review = render_review(n, out)
        self.write_doc(f"review-{n}.md", review)
        self.state.results[f"review-{n}"] = out["verdict"]
        return StageResult(out["verdict"], f"loop {n} review {out['verdict']}",
                           {"feedback": f"# Latest review findings\n\n{review}"})

    def stage_document(self, inputs: dict[str, Any]) -> StageResult:
        self._on_branch()
        trunk, branch = self.state.trunk, self.state.branch
        out = self._agent(
            "documenter",
            {"trunk": trunk, "branch": branch, "standards_files": ", ".join(STANDARDS_FILES)},
        )
        violations = [p for status, p in g.changed_paths(self.workspace) if not _doc_change_allowed(status, p)]
        if violations:
            self.revert(self.workspace, violations)
            self.run.log("scope_violation", agent="documenter", paths=violations)
            raise StageFailed(f"documenter changed files outside its scope: {violations}")
        self._commit_target("document")
        return StageResult("ok", out.get("summary", "documented"))

    def stage_merge(self, inputs: dict[str, Any]) -> StageResult:
        """Merge the branch into the trunk; a conflict is a `conflict` result, not a failure."""
        if self.remote is not None:
            return self._merge_remote()
        target = self.workspace
        self.git(target, "checkout", self.state.trunk)
        try:
            self.git(
                target,
                "merge",
                "--no-ff",
                self.state.branch,
                "-m",
                f"sf {self.state.run_id}: merge {self.state.branch}",
            )
        except g.GitError as e:
            self.git(target, "merge", "--abort", check=False)
            self.state.merge_conflicts += 1
            self.run.log("merge_conflict", count=self.state.merge_conflicts)
            feedback = (f"# Merge conflict\n\nMerging {self.state.branch} into {self.state.trunk} failed; "
                        f"resolve it on the branch.\n\n```\n{e}\n```")
            return StageResult("conflict", f"merge conflict: {e}", {"feedback": feedback})
        return StageResult("merged", f"merged {self.state.branch} into {self.state.trunk}")

    def _merge_remote(self) -> StageResult:
        """Rebase onto the remote's latest trunk, revalidate, fast-forward and push it, one run per target at a time.

        A rebase conflict (`conflict`) or failed revalidation (`validation_failed`) earns another
        implement⇄review pass via `advance`; a fetch or push failure is an `InfraError` (job requeued).
        """
        repo, trunk, branch = self.workspace, self.state.trunk, self.state.branch
        db = self.run.db
        db.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (self.remote,))
        self.run.log("merge_lock_acquired", remote=self.remote)
        try:
            g.remote_git(repo, "fetch", "--quiet", "origin")
            self.git(repo, "checkout", branch)
            try:
                self.git(repo, "rebase", f"origin/{trunk}")
            except g.GitError as e:
                files = g.git(repo, "diff", "--name-only", "--diff-filter=U").split()
                self.git(repo, "rebase", "--abort", check=False)
                self.state.merge_conflicts += 1
                self.run.log("merge_conflict", count=self.state.merge_conflicts, files=files)
                feedback = (f"# Merge conflict\n\nRebasing {branch} onto the latest {trunk} conflicts in: "
                            f"{', '.join(files) or '(unknown files)'}. Resolve it on the branch.\n\n```\n{e}\n```")
                return StageResult("conflict", f"merge conflict in {', '.join(files)}", {"feedback": feedback})
            commands = validation_commands(repo, load_config(repo))
            log = self.run.dir / "merge-validation.log"
            ok = run_validation(repo, commands, log) if commands else True
            self.run.log("merge_validation", passed=ok, commands=len(commands), log=log.name)
            if not ok:
                self.state.merge_conflicts += 1
                g.remote_git(repo, "push", "--quiet", "--force", "origin", f"{branch}:{branch}")
                feedback = (f"# Validation failed after rebasing onto the latest {trunk}\n\n"
                            f"```\n{log.read_text()}\n```")
                return StageResult("validation_failed", "merge revalidation failed", {"feedback": feedback})
            self.git(repo, "checkout", trunk)
            self.git(repo, "reset", "--hard", f"origin/{trunk}")
            self.git(repo, "merge", "--ff-only", branch)
            g.remote_git(repo, "push", "--quiet", "--atomic", "origin", f"{trunk}:{trunk}", f"+{branch}:{branch}")
        finally:
            db.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (self.remote,))
        return StageResult("merged", f"rebased {branch} onto {trunk} and fast-forwarded it")

    def stage_improve(self, inputs: dict[str, Any]) -> StageResult:
        from sf.improve import improve

        return StageResult("ok", improve(self))


def _doc_change_allowed(status: str, path: str) -> bool:
    if path.startswith("docs/"):
        return True
    return status == "M" and path.endswith(".md")


def render_review(n: int, out: dict[str, Any]) -> str:
    lines = [f"# Review {n}", "", f"**Verdict:** {out['verdict']}", "", "## Findings", ""]
    if not out["findings"]:
        lines.append("None.")
    for f in out["findings"]:
        where = f" ({f['location']})" if f.get("location") else ""
        lines.append(f"- **[{f.get('severity', 'info')}]**{where} {f['description']}")
    return "\n".join(lines) + "\n"


# -- queued driver: stage jobs on the Postgres queue -----------------------------------------
def required_caps(config: dict[str, Any], stage: str) -> list[str]:
    """Capabilities a runner needs for `stage`: the harness of its agent in the run's config snapshot and,
    for Pi, that agent's model (runners advertise only models their Pi has auth for; see worker.detect_caps)."""
    agent = STAGE_AGENT.get(stage)
    if agent is None:
        return []
    definition = parse_agent(config["agents"][f"{agent}.md"])
    caps = [HARNESS_CAP[definition.harness]]
    if definition.harness == "pi":
        caps.append(pi_model_cap(definition.model))
    return caps


def pi_model_cap(model: str) -> str:
    return f"model:pi:{model}"


def start_run(conn: Any, state: RunState) -> Step:
    """Enqueue a stored run's first stage job (or fail it, e.g. plan approval in v1), in one transaction."""
    with conn.transaction():
        step = advance(state, None, None)
        _apply_queued(conn, state, step, None)
    return step


def run_job(conn: Any, factory: Path, job: dict[str, Any], display: Notifier | None = None) -> Step | None:
    """Execute one claimed stage job in a fresh clone of the target remote and complete it.

    Harness outputs go to a per-job temp run dir, uploaded to the artifact store at job end (failed
    stages too); the stage's branch commits are then pushed and their sha, the artifact prefix and the
    job's token usage recorded. Nothing is read from `factory/runs/`. Agent failures park the run;
    clone/fetch/push/upload failures are infra failures: the job is requeued (or, after
    `queue.MAX_INFRA_RETRIES`, failed and parked) and None is returned.
    """
    state = store.load_state(conn, job["run_id"])
    workspace = g.job_dir(state.run_id)
    run = Run(factory, state, conn, run_dir=workspace.parent / "run")
    run.state.status = "running"
    seen = len(run.state.invocations)
    remote = store.load_config(conn, run.state.run_id)["target_config"]["remote"]
    prefix = artifacts.job_prefix(run.state.run_id, job["stage"], job["id"])
    try:
        start = g.checkout_job(remote, run.state.branch, run.state.trunk, workspace)
        wf = Workflow(run, None, display, workspace, remote=remote)
        try:
            result = wf._stage(job["stage"], job["inputs"])
        except Aborted as e:
            result = StageResult("aborted", f"aborted: {e}")
        except (StageFailed, g.GitError) as e:
            result = StageResult("failed", f"failed: {e}")
        keys = artifacts.upload_dir(run.dir, prefix)
        run.log("artifacts_uploaded", job_id=job["id"], prefix=prefix, files=len(keys))
        sha = None if result.outcome in ("failed", "aborted") else g.publish_branch(workspace, run.state.branch, start)
    except g.InfraError as e:
        run.log("infra_failure", job_id=job["id"], error=str(e))
        queue.report_infra_failure(conn, job["id"], str(e))
        return None
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)
    usage = sum((inv.usage for inv in run.state.invocations[seen:]), Usage(known=[]))
    return complete_stage(conn, job, run.state, result, branch_sha=sha, usage=usage.model_dump(),
                          artifact_prefix=prefix)


def complete_stage(conn: Any, job: dict[str, Any], state: RunState, result: StageResult,
                   branch_sha: str | None = None, usage: dict[str, Any] | None = None,
                   artifact_prefix: str | None = None) -> Step:
    """In one transaction: record the job result, save the state, and enqueue the next job or end/park the run."""
    with conn.transaction():
        failed = result.outcome in ("failed", "aborted")
        queue.complete(conn, job["id"], result.outcome, failed=failed, contract=result.data or None,
                       branch_sha=branch_sha, usage=usage, artifact_prefix=artifact_prefix)
        if result.outcome == "failed":  # agent failure: park, resumable (today's semantics)
            step: Step = Finished("failed", result.summary)
        elif result.outcome == "aborted":
            step = Finished("aborted", result.summary)
        else:
            step = advance(state, job["stage"], result)
        _apply_queued(conn, state, step, job["id"])
    return step


def _apply_queued(conn: Any, state: RunState, step: Step, job_id: int | None) -> None:
    record_step(state, step)
    event: dict[str, Any] = {"ts": now(), "run_id": state.run_id, "stage": state.stage, "loop": state.loop,
                             "source": "sf"}
    if isinstance(step, Next):
        caps = required_caps(store.load_config(conn, state.run_id), step.stage)
        queued = queue.enqueue(conn, state.run_id, step.stage, inputs=step.inputs, required_caps=caps)
        event |= {"type": "job_enqueued", "next_stage": step.stage, "next_job": queued, "required_caps": caps}
    elif isinstance(step, CapReached):
        state.cap_reached = True
        state.status = "parked"
        event |= {"type": "loop_cap_reached", "loops": step.loops, "parked": True}
    elif step.status == "failed" and job_id is not None:
        state.status = "parked"
        event |= {"type": "run_parked", "reason": step.reason}
    else:
        event |= {"type": f"run_{step.status}", "reason": step.reason}
    store.save_state(conn, state)
    store.append_event(conn, event, job_id=job_id)


def submit_run(conn: Any, factory: Path, target: Path, prompt: str, remote: str | None = None) -> RunState:
    """Create a queued run (state, config snapshot, prompt.md) and enqueue its first job, atomically.

    `remote` (when `target` is a throwaway clone) is recorded as the run's target and its runners' remote.
    """
    target = target.resolve()
    config = store.snapshot_config(factory, target, remote)
    with conn.transaction():
        run_id = store.new_run_id(conn)
        trunk = resolve_trunk(target, load_config(target))
        state = RunState(run_id=run_id, target=remote or str(target), trunk=trunk, branch=f"sf/{run_id}")
        store.create_run(conn, state, config)
        store.put_document(conn, run_id, "prompt.md", prompt.rstrip() + "\n")
        store.append_event(conn, {"ts": now(), "run_id": run_id, "stage": state.stage, "source": "sf",
                                  "type": "run_created", "target": state.target, "trunk": trunk})
        start_run(conn, state)
    return state


def submit_remote_run(conn: Any, factory: Path, url: str, prompt: str) -> RunState:
    """`submit_run` against a target known only by its clone URL, read from a throwaway clone of it."""
    root = g.cache_dir() / "submit"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sf-submit-", dir=root) as tmp:
        clone = Path(tmp) / "target"
        try:
            g.clone_latest(url, clone)
        except g.InfraError as e:
            raise g.GitError(f"cannot clone {url}: {e}") from e
        return submit_run(conn, factory, clone, prompt, remote=url)


def resume_queued(conn: Any, run_id: str) -> dict[str, Any]:
    """Re-enqueue a parked queued run's failed stage job and mark it running, in one transaction.

    The stage re-run is the run's latest failed job (agent or infra failure). A run parked at the loop
    cap has no failed job and cannot be resumed through the queue in v1. Returns the new job row.
    """
    with conn.transaction():
        conn.execute("SELECT id FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        state = store.load_state(conn, run_id)
        if state.status != "parked":
            raise ValueError(f"run {run_id} is {state.status}, not parked; only parked runs can be resumed")
        failed = conn.execute(
            "SELECT stage, inputs FROM stage_jobs WHERE run_id = %s AND status = 'failed' ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if failed is None or state.cap_reached:
            raise ValueError(f"run {run_id} is parked at the loop cap; queued runs cannot continue past it in v1")
        caps = required_caps(store.load_config(conn, run_id), failed["stage"])
        job_id = queue.enqueue(conn, run_id, failed["stage"], inputs=failed["inputs"], required_caps=caps)
        state.status = "running"
        state.pending = {"stage": failed["stage"], "inputs": failed["inputs"]}
        store.save_state(conn, state)
        store.append_event(conn, {"ts": now(), "run_id": run_id, "stage": state.stage, "loop": state.loop,
                                  "source": "sf", "type": "run_resumed", "next_stage": failed["stage"],
                                  "next_job": job_id, "required_caps": caps}, job_id=job_id)
    return {"id": job_id, "stage": failed["stage"], "inputs": failed["inputs"]}
