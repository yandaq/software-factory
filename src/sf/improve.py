"""Improve phase: record evidence-backed suggestions; a human applies them from the dashboard."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sf.run import now

if TYPE_CHECKING:
    from sf.workflow import Workflow

IMPROVEMENTS = "improvements.jsonl"


def load_decisions(factory: Path) -> list[dict[str, Any]]:
    path = factory / IMPROVEMENTS
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def record_decision(factory: Path, decision: dict[str, Any]) -> None:
    with (factory / IMPROVEMENTS).open("a") as f:
        f.write(json.dumps(decision, ensure_ascii=False) + "\n")


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
            deleted = True
            continue
        keep.append(line)
    if deleted:
        path.write_text("".join(f"{line}\n" for line in keep))
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
