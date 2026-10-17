"""Explicit, background application of a recorded suggestion to the factory working tree."""

import json
import logging
import os
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from sf import git as g
from sf.agents import AgentDefinition, load_agent, render_task
from sf.improve import decision_identity, load_decisions, update_application
from sf.run import Run, RunLock, RunState, _alive, now
from sf.runner import StageFailed, run_agent
from sf.target import discover_documentation, load_config, run_validation, validation_commands
from sf.workflow import _run_id


class ApplyRefused(Exception):
    """The selected suggestion cannot be applied right now."""


def recover_applications(factory: Path) -> None:
    """A server killed during execution leaves a visible, retryable failure on restart."""
    for idea in load_decisions(factory):
        application = idea.get("application") or {}
        if application.get("status") == "running" and not _alive(application["pid"]):
            error = "Implementation interrupted; inspect partial changes before retrying."
            update_application(factory, decision_identity(idea), application | {
                "status": "failed", "error": error, "finished_at": now(),
            })
            run = Run.load(factory, application["run_id"])
            run.state.status = "failed"
            run.state.results["implement"] = error
            run.log("application_failed", error=error)
            run.save()


def start_application(factory: Path, identity: tuple[str, str, str]) -> str:
    """Reserve the factory before returning; the worker releases the same lock in finally."""
    lock = RunLock(factory)
    try:
        lock.__enter__()
    except (RuntimeError, FileExistsError) as e:
        raise ApplyRefused(str(e)) from e
    run = None
    application = None
    try:
        recover_applications(factory)
        idea = next((i for i in load_decisions(factory) if decision_identity(i) == identity), None)
        if idea is None:
            raise FileNotFoundError("That improvement is no longer in the log.")
        if (idea.get("application") or {}).get("status") in ("running", "applied"):
            raise ApplyRefused("That improvement is already running or applied.")
        name = idea.get("file", "")
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts
                or path.suffix.lower() != ".md" or name not in discover_documentation(factory)):
            raise ApplyRefused("The suggestion must name an existing Markdown file in software-factory.")
        # Validate configuration before accepting a job; each retry reads the latest settings.
        agent = load_agent(factory / "agents", "implementer")
        plan = (
            "Apply this one recorded improvement to software-factory, the current repository.\n"
            "Inspect and preserve all existing uncommitted edits. Leave your changes uncommitted.\n"
            "Change only what is needed for this suggestion, including related code, tests and docs.\n"
            "Follow repository validation guidance and report the checks and their results.\n"
            "Do not modify improvements.jsonl or execution state; the dashboard owns those files.\n\n"
            + json.dumps(idea, ensure_ascii=False, indent=2)
        )
        task = render_task(factory / "agents", "implementer", {
            "plan": plan, "feedback": "",
            "documentation": f"Target repository: {factory}\nRepository-relative Markdown paths:\n"
                             + json.dumps(discover_documentation(factory), ensure_ascii=False, indent=2),
        })
        branch = g.git(factory, "branch", "--show-current").strip()
        run = Run(factory, RunState(run_id=_run_id(factory), target=str(factory), trunk=branch,
                                   branch=branch, stage="implement", kind="improvement"))
        (run.dir / "prompt.md").write_text(plan + "\n")
        (run.dir / "plan.md").write_text(plan + "\n")
        run.log("application_started", suggestion=list(identity))
        run.save()
        application = {"run_id": run.state.run_id, "status": "running", "pid": os.getpid(), "started_at": now()}
        update_application(factory, identity, application)
        worker = threading.Thread(target=_apply, args=(run, identity, application, agent, task, lock), daemon=True)
        worker.start()
        return run.state.run_id
    except Exception as e:
        try:
            if run is not None and application is not None:
                _finish(run, identity, application, error=str(e))
        finally:
            lock.__exit__()
        raise


def _finish(run: Run, identity: tuple[str, str, str], application: dict[str, Any],
            summary: str = "", error: str | None = None) -> None:
    run.state.status = "failed" if error else "done"
    run.state.results["implement"] = error or summary
    if not error:
        run.state.completed = ["implement"]
    run.log("application_failed" if error else "application_applied", summary=summary, error=error)
    run.save()
    update_application(run.factory, identity, application | {
        "status": "failed" if error else "applied", "summary": summary,
        "error": error, "finished_at": now(),
    })


def _apply(run: Run, identity: tuple[str, str, str], application: dict[str, Any],
           agent: AgentDefinition, task: str, lock: RunLock) -> None:
    try:
        out = run_agent(run, agent, task, run.factory)
        commands = validation_commands(run.factory, load_config(run.factory))
        if commands:
            ok = run_validation(run.factory, commands, run.dir / "validation-1.log")
            run.state.results["validation-1"] = "passed" if ok else "failed"
            run.log("validation", passed=ok, log="validation-1.log")
            if not ok:
                raise StageFailed("Factory validation failed; see validation-1.log. Partial changes remain for inspection.")
        else:
            run.log("validation_skipped", reason="no configured validation commands; see implementer evidence")
        _finish(run, identity, application, summary=out["summary"])
    except Exception as e:
        logging.getLogger(__name__).exception("Improvement implementation failed: %s", run.state.run_id)
        _finish(run, identity, application, error=str(e))
    finally:
        lock.__exit__()
