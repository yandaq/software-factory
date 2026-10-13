"""Improve phase: automatically apply suggestions in scope, with a shared-prompt portability gate."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sf import git as g
from sf.agents import load_agent, load_task_template, render_task_text, validate_agents
from sf.run import now
from sf.runner import StageFailed, run_agent
from sf.target import discover_documentation

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
        {"run_id": state.run_id, "target": str(target), "trunk": state.trunk,
         "branch": state.branch, "run_dir": str(run.dir), "agents_dir": str(factory / "agents"),
         "cap_reached": "yes" if state.cap_reached else "no",
         "past_decisions": "\n".join(
             json.dumps({k: d.get(k) for k in ("title", "decision", "note")}) for d in decisions
         ) or "None."},
        cwd=factory,
    )

    ideas = []
    for idea in out["ideas"]:
        if _norm(idea["title"]) in rejected:
            run.log("idea_filtered", title=idea["title"], reason="previously rejected")
        else:
            ideas.append(idea)

    for idea in ideas:
        record_decision(
            factory,
            {
                "ts": now(),
                "run_id": state.run_id,
                **{k: idea[k] for k in ("title", "rationale", "evidence", "proposed_change")},
                "decision": "automatic",
                "note": "",
            },
        )
        run.log("idea_decision", title=idea["title"], decision="automatic", note="")
        wf.display.note(f"Applying improvement: {idea['title']}")
        _apply(wf, idea)
    wf.commit(factory, f"sf {state.run_id}: improvement decisions", (IMPROVEMENTS,))
    return f"{len(ideas)} improvements applied"


def _apply(wf: Workflow, idea: dict[str, Any]) -> None:
    run, state, factory, target = wf.run, wf.state, wf.factory, wf.run.target
    try:
        documents = discover_documentation(target)
    except (g.GitError, OSError, ValueError) as e:
        run.log("documentation_discovery_failed", agent="improvement-implementer", target=str(target), error=str(e))
        raise StageFailed(f"documentation discovery for {target}: {e}") from e
    allowed_target = json.dumps([str(target / path) for path in documents], ensure_ascii=False)
    def factory_ok(p: str) -> bool:
        return (
            p.startswith("runs/")
            or p == IMPROVEMENTS
            or (p.startswith("agents/") and p.endswith(".md") and "/" not in p[len("agents/") :])
        )

    scopes = ((factory, factory_ok), (target, lambda p: p in documents))
    # Scope enforcement must undo this invocation's edits, not unrelated local work.
    local = {
        repo: {p: (repo / p).read_bytes() if (repo / p).is_file() else None
               for _, p in g.changed_paths(repo) if not ok(p)}
        for repo, ok in scopes
    }
    agents_dir = factory / "agents"
    before = {p: p.read_bytes() for p in agents_dir.glob("*.md")}
    try:
        reviewer = load_agent(agents_dir, "improvement-reviewer")
        review_template = load_task_template(agents_dir, reviewer.name)
    except ValueError as e:
        run.log("prompt_invalid", agent="improvement-reviewer", error=str(e))
        raise StageFailed(str(e)) from e
    # The portability gate never needs write-capable tools, even if configured with them.
    reviewer = reviewer.model_copy(update={"tools": ["read", "grep", "find", "ls"]}, deep=True)
    wf._agent(
        "improvement-implementer",
        {"title": idea["title"], "proposed_change": idea["proposed_change"],
         "rationale": idea["rationale"],
         "allowed_files": f"{agents_dir}/*.md and {allowed_target}"},
        cwd=factory, documentation_paths=documents,
    )

    for repo, ok in scopes:
        bad = []
        fresh = []
        paths = dict.fromkeys([p for _, p in g.changed_paths(repo)] + list(local[repo]))
        for p in paths:
            if ok(p):
                continue
            path = repo / p
            if p in local[repo]:
                content = local[repo][p]
                current = path.read_bytes() if path.is_file() else None
                if current == content:
                    continue
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            else:
                fresh.append(p)
            bad.append(p)
        if fresh:
            wf.revert(repo, fresh)
        if bad:
            run.log("scope_violation", agent="improvement-implementer", repo=str(repo), paths=bad)
            wf.display.note(f"[red]reverted out-of-scope changes in {repo}: {bad}[/]")

    try:
        validate_agents(agents_dir)
    except ValueError as e:
        _restore_prompts(agents_dir, before)
        run.log("improvement_invalid", title=idea["title"], error=str(e))
        raise StageFailed(f"invalid prompt improvement: {e}") from e

    after = {p: p.read_bytes() for p in agents_dir.glob("*.md")}
    changed = [path for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]
    changes = []
    if changed:
        prefix = f"portability-{len(state.invocations) + 1}"
        try:
            changes = [
                {"path": str(path.relative_to(factory)),
                 "before": before[path].decode() if path in before else None,
                 "after": after[path].decode() if path in after else None}
                for path in changed
            ]
            inputs = {"idea": idea, "changes": changes,
                      "reviewer": reviewer.model_dump(), "review_template": review_template}
            (run.dir / f"{prefix}-input.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2) + "\n")
            task = render_task_text(reviewer.name, review_template, {
                "idea": json.dumps(idea, ensure_ascii=False, indent=2),
                "changes": json.dumps(changes, ensure_ascii=False, indent=2),
                "documentation": (f"Target repository: {json.dumps(str(target))}\n"
                                  f"Repository-relative Markdown paths:\n{json.dumps(documents)}"),
            })
            run.log("portability_review_start", title=idea["title"], artifact=prefix,
                    paths=[change["path"] for change in changes])
            result = run_agent(run, reviewer, task, factory, wf.display)
            (run.dir / f"{prefix}-result.json").write_text(json.dumps(result, indent=2) + "\n")
            run.log("portability_review_result", title=idea["title"], artifact=prefix, result=result)
            if result.get("verdict") != "approve" or result.get("findings") != []:
                reasons = "; ".join(f"{f['location']}: {f['description']}" for f in result.get("findings", []))
                raise StageFailed(f"portability review rejected improvement: {reasons or 'no explicit approval'}")
        except (StageFailed, ValueError, OSError) as e:
            _restore_prompts(agents_dir, before)
            run.log("portability_review_failed", title=idea["title"], artifact=prefix, error=str(e))
            wf.display.note(f"Portability review failed; shared prompts restored: {e}")
            raise StageFailed(f"portability gate: {e}") from e
    else:
        run.log("portability_review_skipped", title=idea["title"], reason="no shared prompt changes")

    msg = f"sf {state.run_id}: improvement — {idea['title']}"
    if changes and wf.commit(factory, msg, tuple(change["path"] for change in changes)):
        run.log("improvement_committed", repo="factory", title=idea["title"])
    if documents and wf.commit(target, msg, tuple(documents)):
        run.log("improvement_committed", repo="target", title=idea["title"])


def _restore_prompts(agents_dir: Path, before: dict[Path, bytes]) -> None:
    for path in agents_dir.glob("*.md"):
        if path not in before:
            path.unlink()
    for path, content in before.items():
        path.write_bytes(content)
