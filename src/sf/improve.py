"""Improve phase: improver proposes ideas, a human decides, approved ideas are applied in scope."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sf import git as g
from sf.run import now
from sf.target import STANDARDS_FILES

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


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def improve(wf: Workflow) -> str:
    run, state, factory, target = wf.run, wf.state, wf.factory, wf.run.target
    wf.git(target, "checkout", state.trunk)
    decisions = load_decisions(factory)
    rejected = {_norm(d["title"]) for d in decisions if d["decision"] == "rejected"}

    out = wf._agent(
        "improver",
        (
            f"Analyse run {state.run_id} of the software factory and propose improvements to the "
            "agent definitions and the target's standards files.\n\n"
            f"- Generated code: target repository at {target} (merged into {state.trunk}); "
            f"see `git log {state.trunk}` and the run branch {state.branch}.\n"
            f"- Run logs: {run.dir} (events.jsonl, review-*.md, validation-*.log, run-state.json).\n"
            f"- Agent definitions: {factory / 'agents'}.\n"
            f"- Review loop cap reached without approval: {'yes' if state.cap_reached else 'no'}.\n\n"
            "# Past decisions (never re-propose a rejected idea)\n\n"
            + (
                "\n".join(json.dumps({k: d.get(k) for k in ("title", "decision", "note")}) for d in decisions)
                or "None."
            )
        ),
        cwd=factory,
    )

    ideas = []
    for idea in out["ideas"]:
        if _norm(idea["title"]) in rejected:
            run.log("idea_filtered", title=idea["title"], reason="previously rejected")
        else:
            ideas.append(idea)

    counts = {"approved": 0, "skipped": 0, "rejected": 0}
    for idea in ideas:
        decision, note = wf.human.review_idea(idea)
        counts[decision] += 1
        record_decision(
            factory,
            {
                "ts": now(),
                "run_id": state.run_id,
                **{k: idea[k] for k in ("title", "rationale", "evidence", "proposed_change")},
                "decision": decision,
                "note": note,
            },
        )
        run.log("idea_decision", title=idea["title"], decision=decision, note=note)
        if decision == "approved":
            _apply(wf, idea, note)
    wf.commit(factory, f"sf {state.run_id}: improvement decisions", (IMPROVEMENTS,))
    return ", ".join(f"{v} {k}" for k, v in counts.items())


def _apply(wf: Workflow, idea: dict[str, Any], note: str) -> None:
    run, state, factory, target = wf.run, wf.state, wf.factory, wf.run.target
    allowed_target = ", ".join(str(target / f) for f in STANDARDS_FILES)
    wf._agent(
        "improvement-implementer",
        (
            f"Apply this approved improvement.\n\n# {idea['title']}\n\n{idea['proposed_change']}\n\n"
            f"Rationale: {idea['rationale']}\n"
            + (f"Human note: {note}\n" if note else "")
            + f"\nYou may change ONLY: {factory / 'agents'}/*.md and {allowed_target}. "
            "Do not change orchestrator code or anything else."
        ),
        cwd=factory,
    )

    def factory_ok(p: str) -> bool:
        return (
            p.startswith("runs/")
            or p == IMPROVEMENTS
            or (p.startswith("agents/") and p.endswith(".md") and "/" not in p[len("agents/") :])
        )

    for repo, ok in ((factory, factory_ok), (target, lambda p: p in STANDARDS_FILES)):
        bad = [p for _, p in g.changed_paths(repo) if not ok(p)]
        if bad:
            wf.revert(repo, bad)
            run.log("scope_violation", agent="improvement-implementer", repo=str(repo), paths=bad)
            wf.display.note(f"[red]reverted out-of-scope changes in {repo}: {bad}[/]")

    msg = f"sf {state.run_id}: improvement — {idea['title']}"
    if wf.commit(factory, msg, ("agents",)):
        run.log("improvement_committed", repo="factory", title=idea["title"])
    if wf.commit(target, msg, STANDARDS_FILES):
        run.log("improvement_committed", repo="target", title=idea["title"])
