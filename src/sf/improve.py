"""Improve phase: record evidence-backed suggestions; a human applies them from the dashboard."""

import fcntl
import json
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
def update_application(factory: Path, identity: tuple[str, str, str], application: dict[str, Any]) -> None:
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
            return
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
    run, state, factory, target = wf.run, wf.state, wf.factory, wf.run.target
    wf.git(target, "checkout", state.trunk)
    decisions = load_decisions(factory)
    rejected = {_norm(d["title"]) for d in decisions if d["decision"] == "rejected"}

    out = wf._agent(
        "improver",
        {"run_id": state.run_id, "target": str(target), "trunk": state.trunk,
         "branch": state.branch, "run_dir": str(run.dir), "agents_dir": str(factory / "agents"),
         "cap_reached": "yes" if state.cap_reached else "no",
         "past_decisions": "\n".join(
             json.dumps({k: d.get(k) for k in ("title", "decision", "note")}) for d in decisions
         ) or "None."},
        cwd=factory,
    )

    recorded = 0
    for idea in out["ideas"]:
        if _norm(idea["title"]) in rejected:
            run.log("idea_filtered", title=idea["title"], reason="previously rejected")
            continue
        record_decision(
            factory,
            {
                "ts": now(),
                "run_id": state.run_id,
                **{k: idea[k] for k in ("title", "file", "rationale", "evidence", "proposed_change")},
                "decision": "automatic",
                "note": "",
            },
        )
        run.log("idea_decision", title=idea["title"], decision="automatic", note="")
        wf.display.note(f"Improvement recorded: {idea['title']}")
        recorded += 1
    wf.commit(factory, f"sf {state.run_id}: improvement decisions", (IMPROVEMENTS,))
    return f"{recorded} improvements recorded"
