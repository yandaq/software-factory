"""The fixed workflow: branch → prompt → plan → implement ⇄ review → document → merge → improve."""

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sf import git as g
from sf.agents import load_agent
from sf.pi import Display, StageFailed, run_agent
from sf.run import STAGES, Run, RunState
from sf.target import (
    STANDARDS_FILES,
    install_missing_standards,
    load_config,
    run_validation,
    validation_commands,
)


class Aborted(Exception):
    pass


class Human(Protocol):
    def approve_plan(self, plan: str) -> bool: ...
    def cap_reached(self, loops: int) -> Any: ...
    def review_idea(self, idea: dict[str, Any]) -> tuple[str, str]: ...


class Notifier(Display, Protocol):
    def note(self, text: str) -> None: ...


def new_run(factory: Path, target: Path, prompt: str, approve_plan: bool) -> Run:
    target = target.resolve()
    g.git(target, "rev-parse", "--git-dir")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    config = load_config(target)
    state = RunState(
        run_id=run_id,
        target=str(target),
        trunk=config.trunk,
        branch=f"sf/{run_id}",
        approve_plan=approve_plan,
    )
    run = Run(factory, state)
    (run.dir / "prompt.md").write_text(prompt.rstrip() + "\n")
    run.log("run_created", target=str(target), trunk=config.trunk)
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
        self.run.save()
        g.commit_all(
            self.factory,
            f"sf {self.state.run_id}: {self.state.stage} — {what}",
            (f"runs/{self.state.run_id}",),
        )

    def _agent(self, name: str, task: str, cwd: Path | None = None) -> dict[str, Any]:
        agent = load_agent(self.factory / "agents", name)
        return run_agent(self.run, agent, task, cwd or self.run.target, self.display)

    def _commit_target(self, what: str) -> bool:
        return g.commit_all(self.run.target, f"sf {self.state.run_id}: {what}")

    def _on_branch(self) -> None:
        if g.git(self.run.target, "branch", "--show-current").strip() != self.state.branch:
            g.git(self.run.target, "checkout", self.state.branch)

    # -- stages ------------------------------------------------------------
    def stage_branch(self) -> str:
        target = self.run.target
        if g.git(target, "status", "--porcelain").strip():
            raise StageFailed(f"target {target} has uncommitted changes")
        if g.git(target, "branch", "--list", self.state.branch).strip():
            g.git(target, "checkout", self.state.branch)
        else:
            g.git(target, "checkout", "-b", self.state.branch, self.state.trunk)
        copied = install_missing_standards(target, self.factory / "templates")
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
            (
                "Produce an implementation plan for the request below. Read architecture.md, "
                f"agents.md and docs/ in the repository first.\n\n# Request\n\n{prompt}"
            ),
        )
        plan = out["plan"]
        (self.run.dir / "plan.md").write_text(plan.rstrip() + "\n")
        self._checkpoint("plan written")
        if self.state.approve_plan and not self.human.approve_plan(plan):
            raise Aborted("plan rejected by human")
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
        task = f"Implement this plan in the repository.\n\n# Plan\n\n{plan}"
        prev = n - 1
        review = self.run.dir / f"review-{prev}.md"
        validation = self.run.dir / f"validation-{prev}.log"
        if validation.exists() and self.state.results.get(f"validation-{prev}") == "failed":
            task += f"\n\n# Latest validation output (failed)\n\n```\n{validation.read_text()}\n```"
        elif review.exists():
            task += f"\n\n# Latest review findings\n\n{review.read_text()}"
        out = self._agent("implementer", task)
        self._commit_target(f"implement (loop {n})")
        self.run.log("implemented", summary=out.get("summary"))

        if not commands:
            self.run.log("validation_skipped", reason="no commands in validation.md")
        else:
            ok = run_validation(self.run.target, commands, self.run.dir / f"validation-{n}.log")
            self.state.results[f"validation-{n}"] = "passed" if ok else "failed"
            self.run.log("validation", passed=ok, log=f"validation-{n}.log")
            if not ok:
                self._checkpoint(f"loop {n} validation failed")
                return False

        out = self._agent(
            "reviewer",
            (
                "Review the work on this branch against coding-standards.md. "
                f"Inspect `git diff {self.state.trunk}...HEAD`.\n\n# Plan\n\n{plan}"
            ),
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
            (
                f"Run `git diff {trunk}...{branch}` and update the documentation to match. "
                "Update existing docs; create new docs only under docs/. Do not touch code or "
                f"the standards files ({', '.join(STANDARDS_FILES)})."
            ),
        )
        violations = [p for status, p in g.changed_paths(self.run.target) if not _doc_change_allowed(status, p)]
        if violations:
            g.revert_paths(self.run.target, violations)
            self.run.log("scope_violation", agent="documenter", paths=violations)
            raise StageFailed(f"documenter changed files outside its scope: {violations}")
        self._commit_target("document")
        return out.get("summary", "documented")

    def stage_merge(self) -> str:
        target = self.run.target
        g.git(target, "checkout", self.state.trunk)
        try:
            g.git(
                target,
                "merge",
                "--no-ff",
                self.state.branch,
                "-m",
                f"sf {self.state.run_id}: merge {self.state.branch}",
            )
        except g.GitError as e:
            g.git(target, "merge", "--abort", check=False)
            raise Aborted(f"merge conflict: {e}") from e
        return f"merged {self.state.branch} into {self.state.trunk}"

    def stage_improve(self) -> str:
        from sf.improve import improve

        return improve(self)


def _doc_change_allowed(status: str, path: str) -> bool:
    if path in STANDARDS_FILES:
        return False
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
