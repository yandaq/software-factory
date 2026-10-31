"""Explicit, background application of a recorded suggestion to the factory working tree."""

import json
import logging
import os
import shutil
import socket
import threading
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sf import artifacts, store
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


NAMED_FILE_ERROR = "The suggestion must name an existing factory Markdown file or Python module under src/sf/."


def _named_file_ok(factory: Path, name: str) -> bool:
    """The suggestion's file is an existing factory Markdown document or tracked `src/sf/` Python module."""
    path = PurePosixPath(name)
    source = factory / name
    runner_source = (
        path.is_relative_to("src/sf") and path.suffix == ".py"
        and not source.is_symlink() and source.is_file()
        and source.resolve().is_relative_to(factory.resolve())
        and name in g.git(factory, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    )
    return bool(name) and not path.is_absolute() and ".." not in path.parts and (
        runner_source or name in discover_documentation(factory))


def _task(factory: Path, plan: str) -> str:
    return render_task(factory / "agents", "implementer", {
        "plan": plan, "feedback": "",
        "documentation": f"Target repository: {factory}\nRepository-relative Markdown paths:\n"
                         + json.dumps(discover_documentation(factory), ensure_ascii=False, indent=2),
    })


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
        if not _named_file_ok(factory, idea.get("file", "")):
            raise ApplyRefused(NAMED_FILE_ERROR)
        # Validate configuration before accepting a job; each retry reads the latest settings.
        agent = load_agent(factory / "agents", "implementer")
        plan = (
            "Apply this one recorded improvement to software-factory, the current repository.\n"
            "Inspect and preserve all existing uncommitted edits. Do not commit; after validation, the dashboard\n"
            "will commit all factory working-tree changes, including existing edits, on the current branch.\n"
            "Change only what is needed for this suggestion, including related code, tests and docs.\n"
            "Follow repository validation guidance and report the checks and their results.\n"
            "Do not modify improvements.jsonl or execution state; the dashboard owns those files.\n\n"
            + json.dumps(idea, ensure_ascii=False, indent=2)
        )
        task = _task(factory, plan)
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
    message = f"sf {run.state.run_id}: apply improvement — {identity[2]}"
    commit = update_application(run.factory, identity, application | {
        "status": "failed" if error else "applied", "summary": summary,
        "error": error, "finished_at": now(),
    }, commit_message=message if not error else None)
    run.state.status = "failed" if error else "done"
    run.state.results["implement"] = error or summary
    if not error:
        run.state.completed = ["implement"]
        run.state.results["commit"] = commit or "No changes to commit"
        run.log("git_commit" if commit else "git_commit_skipped", repo="factory", commit=commit, message=message)
    run.log("application_failed" if error else "application_applied", summary=summary, error=error)
    run.save()


def _apply(run: Run, identity: tuple[str, str, str], application: dict[str, Any],
           agent: AgentDefinition, task: str, lock: RunLock) -> None:
    try:
        out = run_agent(run, agent, task, run.factory)
        _validate(run, "Partial changes remain for inspection.")
        _finish(run, identity, application, summary=out["summary"])
    except Exception as e:
        logging.getLogger(__name__).exception("Improvement implementation failed: %s", run.state.run_id)
        _finish(run, identity, application, error=str(e))
    finally:
        lock.__exit__()


def _validate(run: Run, on_failure: str) -> None:
    """Run the factory's configured validation in `run.factory`; StageFailed when it fails."""
    commands = validation_commands(run.factory, load_config(run.factory))
    if commands:
        ok = run_validation(run.factory, commands, run.dir / "validation-1.log")
        run.state.results["validation-1"] = "passed" if ok else "failed"
        run.log("validation", passed=ok, log="validation-1.log")
        if not ok:
            raise StageFailed(f"Factory validation failed; see validation-1.log. {on_failure}")
    else:
        run.log("validation_skipped", reason="no configured validation commands; see implementer evidence")


# -- queued mode (SF_DATABASE_URL): records in Postgres, changes pushed to the factory remote ----------
QUEUED_PLAN = (
    "Apply this one recorded improvement to software-factory, the current repository (a fresh clone).\n"
    "Do not commit; after validation, the factory commits all working-tree changes and pushes them.\n"
    "Change only what is needed for this suggestion, including related code, tests and docs.\n"
    "Follow repository validation guidance and report the checks and their results.\n"
    "Do not modify improvements.jsonl; the factory owns the improvement log.\n\n"
)


def recover_queued_applications(conn: Any) -> None:
    """Fail this host's `running` applications whose dashboard process is gone (visible, retryable)."""
    host = socket.gethostname()
    for idea in store.improvements(conn):
        application = idea.get("application") or {}
        if (application.get("status") == "running" and application.get("host") == host
                and not _alive(application["pid"])):
            store.update_improvement_application(conn, decision_identity(idea), application | {
                "status": "failed", "finished_at": now(),
                "error": "Implementation interrupted; nothing was pushed. Retry the suggestion.",
            })


def start_queued_application(db_url: str | None, factory: Path, identity: tuple[str, str, str]) -> str:
    """Reserve a stored suggestion (status `running`) and apply it in a background thread; returns the execution id.

    The worker clones the factory remote (`git.target_remote(factory)`), so the dashboard's own checkout
    is never modified. Applications to one factory remote are serialised by a Postgres advisory lock.
    """
    conn = store.connect(db_url)
    try:
        recover_queued_applications(conn)
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-apply-{uuid.uuid4().hex[:6]}"
        application = {"run_id": run_id, "status": "running", "pid": os.getpid(), "host": socket.gethostname(),
                       "started_at": now()}
        with conn.transaction():
            row = store.lock_improvement(conn, identity)
            if row is None:
                raise FileNotFoundError("That improvement is no longer in the log.")
            idea = row["record"]
            if (idea.get("application") or {}).get("status") in ("running", "applied"):
                raise ApplyRefused("That improvement is already running or applied.")
            if not _named_file_ok(factory, idea.get("file", "")):
                raise ApplyRefused(NAMED_FILE_ERROR)
            store.update_improvement_application(conn, identity, application)
    finally:
        conn.close()
    worker = threading.Thread(target=_apply_queued, daemon=True,
                              args=(db_url, g.target_remote(factory), idea, application))
    worker.start()
    return run_id


def _apply_queued(db_url: str | None, remote: str, idea: dict[str, Any], application: dict[str, Any]) -> None:
    """Clone, implement, validate, commit and push under the factory-remote lock; record the outcome.

    The execution's run directory (prompt, harness output, validation log, events) is uploaded to the
    artifact store under `improvements/<execution id>/`, whose prefix the application records.
    """
    identity, run_id = decision_identity(idea), application["run_id"]
    root = g.job_dir(run_id).parent
    prefix = f"improvements/{run_id}/"
    conn = store.connect(db_url)
    run = None
    result: dict[str, Any] = {"summary": "", "error": None, "commit": None}
    try:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (remote,))
        try:
            clone = root / "factory"
            g.clone_latest(remote, clone)
            branch = g.git(clone, "branch", "--show-current").strip()
            run = Run(clone, RunState(run_id=run_id, target=remote, trunk=branch, branch=branch,
                                      stage="implement", kind="improvement"), run_dir=root / "run")
            plan = QUEUED_PLAN + json.dumps(idea, ensure_ascii=False, indent=2)
            (run.dir / "prompt.md").write_text(plan + "\n")
            run.log("application_started", suggestion=list(identity), remote=remote)
            if not _named_file_ok(clone, idea.get("file", "")):
                raise ApplyRefused(NAMED_FILE_ERROR + " (in the latest factory remote)")
            out = run_agent(run, load_agent(clone / "agents", "implementer"), _task(clone, plan), clone)
            _validate(run, "Nothing was pushed.")
            message = f"sf {run_id}: apply improvement — {identity[2]}"
            if g.commit_all(clone, message):
                result["commit"] = g.git(clone, "rev-parse", "HEAD").strip()
                g.remote_git(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
            run.log("git_commit" if result["commit"] else "git_commit_skipped", repo="factory",
                    commit=result["commit"], message=message, pushed=bool(result["commit"]))
            result["summary"] = out["summary"]
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (remote,))
    except Exception as e:
        logging.getLogger(__name__).exception("Improvement implementation failed: %s", run_id)
        result |= {"error": str(e), "commit": None}
    try:
        if run is not None:
            run.state.status = "failed" if result["error"] else "done"
            run.state.results["implement"] = result["error"] or result["summary"]
            run.log("application_failed" if result["error"] else "application_applied", **result)
            run.save()
            artifacts.upload_dir(run.dir, prefix)
            result["artifact_prefix"] = prefix
    except Exception as e:  # the outcome must still be recorded
        logging.getLogger(__name__).exception("Could not upload improvement execution %s", run_id)
        result["artifact_error"] = str(e)
    finally:
        try:
            store.update_improvement_application(conn, identity, application | result | {
                "status": "failed" if result["error"] else "applied", "finished_at": now()})
        finally:
            conn.close()
            shutil.rmtree(root, ignore_errors=True)
