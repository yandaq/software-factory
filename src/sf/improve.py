"""Improve phase: record evidence-backed suggestions; a human applies them from the dashboard."""

import fcntl
import json
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sf import artifacts, store
from sf import git as g
from sf.run import now

if TYPE_CHECKING:
    from sf.workflow import Workflow

IMPROVEMENTS = "improvements.jsonl"


def _serialized(fn):
    @wraps(fn)
    def locked(factory, *args, **kwargs):
        # A separate lock file survives atomic replacement of the log and covers CLI writers too.
        runs = factory / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        with (runs / ".improvements.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return fn(factory, *args, **kwargs)
    return locked


def decision_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    return record.get("ts"), record.get("run_id"), record.get("title")


def _replace_log(path: Path, lines: list[str]) -> None:
    temporary = path.with_suffix(".jsonl.tmp")
    try:
        temporary.write_text("".join(f"{line}\n" for line in lines))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@_serialized
def update_application(factory: Path, identity: tuple[str, str, str], application: dict[str, Any],
                       *, commit_message: str | None = None) -> str | None:
    """Persist a result, optionally committing all changes before readers can observe success.

    A failed commit restores the previous result under the same log lock; the caller then records
    the execution failure. The SHA belongs in ignored run diagnostics, not in its own committed log.
    """
    path = factory / IMPROVEMENTS
    lines = path.read_text().splitlines()
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        record = json.loads(line)
        if decision_identity(record) == identity:
            record["application"] = application
            lines[n] = json.dumps(record, ensure_ascii=False)
            _replace_log(path, lines)
            if commit_message is not None:
                try:
                    if g.commit_all(factory, commit_message):
                        return g.git(factory, "rev-parse", "HEAD").strip()
                except g.GitError:
                    lines[n] = line
                    _replace_log(path, lines)
                    raise
            return None
    raise FileNotFoundError("That improvement is no longer in the log.")


@_serialized
def load_decisions(factory: Path) -> list[dict[str, Any]]:
    path = factory / IMPROVEMENTS
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@_serialized
def record_decision(factory: Path, decision: dict[str, Any]) -> None:
    with (factory / IMPROVEMENTS).open("a") as f:
        f.write(json.dumps(decision, ensure_ascii=False) + "\n")


@_serialized
def delete_decision(factory: Path, ts: str, run_id: str, title: str) -> bool:
    """Drop the first recorded suggestion matching ts, run_id and title, rewriting the log without it.

    A record is named by what it says rather than by its position, so a page that is out of date cannot
    delete someone else's entry. The surviving lines are copied verbatim: deleting one suggestion never
    reformats the others.
    """
    path = factory / IMPROVEMENTS
    if not path.exists():
        return False
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    keep: list[str] = []
    deleted = False
    for line in lines:
        record = json.loads(line)  # a log the factory cannot parse must fail visibly, not be rewritten
        fields = (record.get("ts"), record.get("run_id"), record.get("title")) if isinstance(record, dict) else None
        if not deleted and fields == (ts, run_id, title):
            if (record.get("application") or {}).get("status") == "running":
                raise RuntimeError("Cannot delete an improvement while it is running.")
            deleted = True
            continue
        keep.append(line)
    if deleted:
        _replace_log(path, keep)
    return deleted


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def improve(wf: Workflow) -> str:
    """Record the improver's ideas: in `improvements.jsonl` (committed) locally, in the store when queued.

    The target is inspected in the stage's workspace (the target checkout locally, the job's clone when
    queued). A queued run's evidence and factory come from `_queued_inputs`, never from local `runs/`.
    """
    run, state, factory = wf.run, wf.state, wf.factory
    wf.git(wf.workspace, "checkout", state.trunk)
    if run.db is None:
        decisions = load_decisions(factory)
        run_dir, factory_tree = run.dir, factory
    else:
        decisions = store.improvements(run.db)
        run_dir, factory_tree = _queued_inputs(wf)
    rejected = {_norm(d["title"]) for d in decisions if d["decision"] == "rejected"}

    out = wf._agent(
        "improver",
        {"run_id": state.run_id, "target": str(wf.workspace), "trunk": state.trunk,
         "branch": state.branch, "run_dir": str(run_dir), "agents_dir": str(factory_tree / "agents"),
         "cap_reached": "yes" if state.cap_reached else "no",
         "past_decisions": "\n".join(
             json.dumps({k: d.get(k) for k in ("title", "decision", "note")}) for d in decisions
         ) or "None."},
        cwd=factory_tree,
    )

    recorded = 0
    for idea in out["ideas"]:
        if _norm(idea["title"]) in rejected:
            run.log("idea_filtered", title=idea["title"], reason="previously rejected")
            continue
        record = {
            "ts": now(),
            "run_id": state.run_id,
            **{k: idea[k] for k in ("title", "file", "rationale", "evidence", "proposed_change")},
            "decision": "automatic",
            "note": "",
        }
        if run.db is None:
            record_decision(factory, record)
        else:
            store.record_improvement(run.db, record)
        run.log("idea_decision", title=idea["title"], decision="automatic", note="")
        wf.display.note(f"Improvement recorded: {idea['title']}")
        recorded += 1
    if run.db is None:
        wf.commit(factory, f"sf {state.run_id}: improvement decisions", (IMPROVEMENTS,))
    return f"{recorded} improvements recorded"


def _queued_inputs(wf: Workflow) -> tuple[Path, Path]:
    """Build a queued improve job's inputs beside its workspace: (evidence dir, factory clone).

    The evidence dir holds what `runs/<id>/` holds locally - run documents, `events.jsonl` and
    `run-state.json` from the store, plus every earlier job's uploaded artifacts under
    `artifacts/<stage>-<job id>/`. The factory is a fresh clone of the factory remote (latest
    `src/sf/`) with the run's snapshot agents written over its `agents/`, so the improver reviews the
    prompts this run actually used. Clone/download failures are infra errors (the job is requeued).
    """
    db, run_id, root = wf.run.db, wf.state.run_id, wf.workspace.parent
    config = store.load_config(db, run_id)
    factory_tree = root / "factory"
    g.clone_latest(config.get("factory_remote") or g.target_remote(wf.factory), factory_tree)
    (factory_tree / "agents").mkdir(exist_ok=True)
    for name, text in config["agents"].items():
        (factory_tree / "agents" / name).write_text(text)
    evidence = root / "evidence"
    evidence.mkdir()
    for name, body in store.documents(db, run_id).items():
        (evidence / name).write_text(body)
    (evidence / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in store.events(db, run_id)))
    state = wf.state.model_dump() | {"tokens": wf.state.totals()}
    (evidence / "run-state.json").write_text(json.dumps(state, indent=2) + "\n")
    for job in store.artifact_prefixes(db, run_id):
        artifacts.download_dir(job["artifact_prefix"], evidence / "artifacts" / f"{job['stage']}-{job['job_id']}")
    wf.run.log("improve_inputs", evidence=str(evidence), factory=str(factory_tree))
    return evidence, factory_tree
