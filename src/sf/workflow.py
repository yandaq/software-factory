"""The fixed workflow: branch → prompt → plan → implement ⇄ review → document → merge → improve."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sf import git as g
from sf.agents import load_agent, render_task
from sf.run import STAGES, Run, RunState
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


class Workflow:
    def __init__(self, run: Run, human: Human, display: Notifier):
        self.run = run
        self.state = run.state
        self.human = human
        self.display = display
        self.factory = run.factory

    # -- driver ------------------------------------------------------------
    def execute(self) -> None:
        self.state.status = "running"
        for stage in STAGES:
            if stage in self.state.completed:
                continue
            self._stage(stage)
        self.state.status = "done"
        self.run.log("run_done")
        self._checkpoint("done")

    def _stage(self, stage: str) -> None:
        self.state.stage = stage
        self.run.log("stage_start")
        self.display.note(f"stage [bold]{stage}[/]")
        self.run.save()
        try:
            result = getattr(self, f"stage_{stage}")()
        except Aborted as e:
            self._finish_stage(f"aborted: {e}", status="aborted")
            raise
        except (StageFailed, g.GitError) as e:
            self._finish_stage(f"failed: {e}", status="failed")
            raise
        self.state.completed.append(stage)
        self._finish_stage(result)

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
            documents = discover_documentation(self.run.target)
        except (g.GitError, OSError, ValueError) as e:
            self.run.log("documentation_discovery_failed", agent=name, target=str(self.run.target), error=str(e))
            raise StageFailed(f"documentation discovery for {self.run.target}: {e}") from e
        documentation = (f"Target repository: {json.dumps(str(self.run.target))}\n"
                         f"Repository-relative Markdown paths:\n{json.dumps(documents, ensure_ascii=False, indent=2)}")
        try:
            agent = load_agent(self.factory / "agents", name)
            task = render_task(self.factory / "agents", name, context | {"documentation": documentation})
        except ValueError as e:
            self.run.log("prompt_invalid", agent=name, error=str(e))
            raise StageFailed(str(e)) from e
        return run_agent(self.run, agent, task, cwd or self.run.target, self.display)

    def _commit_target(self, what: str) -> bool:
        return self.commit(self.run.target, f"sf {self.state.run_id}: {what}")

    def _on_branch(self) -> None:
        if g.git(self.run.target, "branch", "--show-current").strip() != self.state.branch:
            self.git(self.run.target, "checkout", self.state.branch)

    # -- stages ------------------------------------------------------------
    def stage_branch(self) -> str:
        target = self.run.target
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
        try:
            copied = install_missing_standards(target, self.factory / "templates")
        except StandardsError as e:
            raise StageFailed(str(e)) from e
        if copied:
            self.run.log("standards_installed", files=copied)
            self._commit_target(f"add starter standards ({', '.join(copied)})")
        return f"on {self.state.branch}" + (f"; installed {', '.join(copied)}" if copied else "")

    def stage_prompt(self) -> str:
        return "prompt saved"

    def stage_plan(self) -> str:
        self._on_branch()
        prompt = (self.run.dir / "prompt.md").read_text()
        out = self._agent(
            "planner",
            {"request": prompt},
        )
        plan = out["plan"]
        (self.run.dir / "plan.md").write_text(plan.rstrip() + "\n")
        self._checkpoint("plan written")
        if self.state.approve_plan and not self.human.approve_plan(plan):
            raise Aborted("plan rejected by human")
        plan_path = f"docs/plans/{self.state.run_id}.md"
        (self.run.target / plan_path).parent.mkdir(parents=True, exist_ok=True)
        (self.run.target / plan_path).write_text(plan.rstrip() + "\n")
        self.commit(self.run.target, f"sf {self.state.run_id}: plan", (plan_path,))
        return "plan approved" if self.state.approve_plan else "plan written"

    def stage_implement(self) -> str:
        self._on_branch()
        commands = validation_commands(self.run.target, load_config(self.run.target))
        while True:
            while self.state.loop < self.state.max_loops:
                self.state.loop += 1
                if self._loop(self.state.loop, commands):
                    return f"approved in loop {self.state.loop}"
            self.state.cap_reached = True
            self.run.log("loop_cap_reached", loops=self.state.loop)
            self.run.save()
            choice = self.human.cap_reached(self.state.loop)
            self.run.log("cap_decision", choice=choice)
            if choice == "merge":
                return f"merged without approval after {self.state.loop} loops"
            if choice == "abort":
                raise Aborted(f"no approval after {self.state.loop} loops")
            self.state.max_loops += int(choice)

    def _loop(self, n: int, commands: list[str]) -> bool:
        """One implement → validate → review loop. Returns True when the reviewer approves."""
        self.run.log("loop_start")
        plan = (self.run.dir / "plan.md").read_text()
        feedback = ""
        prev = n - 1
        review = self.run.dir / f"review-{prev}.md"
        validation = self.run.dir / f"validation-{prev}.log"
        if validation.exists() and self.state.results.get(f"validation-{prev}") == "failed":
            feedback = f"# Latest validation output (failed)\n\n```\n{validation.read_text()}\n```"
        elif review.exists():
            feedback = f"# Latest review findings\n\n{review.read_text()}"
        out = self._agent("implementer", {"plan": plan, "feedback": feedback})
        self._commit_target(f"implement (loop {n})")
        self.run.log("implemented", summary=out.get("summary"))

        if not commands:
            self.run.log("validation_skipped", reason="no commands in docs/VALIDATION.md")
        else:
            ok = run_validation(self.run.target, commands, self.run.dir / f"validation-{n}.log")
            self.state.results[f"validation-{n}"] = "passed" if ok else "failed"
            self.run.log("validation", passed=ok, log=f"validation-{n}.log")
            if not ok:
                self._checkpoint(f"loop {n} validation failed")
                return False

        out = self._agent(
            "reviewer",
            {"plan": plan, "trunk": self.state.trunk},
        )
        (self.run.dir / f"review-{n}.md").write_text(render_review(n, out))
        self.state.results[f"review-{n}"] = out["verdict"]
        self._checkpoint(f"loop {n} review {out['verdict']}")
        return out["verdict"] == "approve"

    def stage_document(self) -> str:
        self._on_branch()
        trunk, branch = self.state.trunk, self.state.branch
        out = self._agent(
            "documenter",
            {"trunk": trunk, "branch": branch, "standards_files": ", ".join(STANDARDS_FILES)},
        )
        violations = [p for status, p in g.changed_paths(self.run.target) if not _doc_change_allowed(status, p)]
        if violations:
            self.revert(self.run.target, violations)
            self.run.log("scope_violation", agent="documenter", paths=violations)
            raise StageFailed(f"documenter changed files outside its scope: {violations}")
        self._commit_target("document")
        return out.get("summary", "documented")

    def stage_merge(self) -> str:
        target = self.run.target
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
            raise Aborted(f"merge conflict: {e}") from e
        return f"merged {self.state.branch} into {self.state.trunk}"

    def stage_improve(self) -> str:
        from sf.improve import improve

        return improve(self)


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
