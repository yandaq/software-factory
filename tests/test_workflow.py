import json

import pytest
from conftest import calls, sh

from sf.improve import load_decisions
from sf.pi import StageFailed
from sf.run import Run
from sf.workflow import Aborted, Workflow, new_run

APPROVE = {"text": '{"verdict": "approve", "findings": []}'}
CHANGES = {
    "text": '{"verdict": "changes_requested", "findings": '
    '[{"severity": "major", "location": "app.py:1", "description": "bad name"}]}'
}
IDEA = {"title": "Tighten reviewer", "rationale": "r", "evidence": "e", "proposed_change": "c"}


def script(**over):
    s = {
        "planner": [{"text": '{"plan": "# Plan\\n1. write app.py"}'}],
        "implementer": [{"text": '{"summary": "s"}', "write": {"app.py": "print(1)\n"}}],
        "reviewer": [APPROVE],
        "documenter": [{"text": '{"summary": "docs"}', "write": {"docs/app.md": "# App\n"}}],
        "improver": [{"text": '{"ideas": []}'}],
        "improvement-implementer": [{"text": '{"summary": "s"}'}],
    }
    s.update(over)
    return s


class Human:
    def __init__(self, plan=True, cap="merge", ideas=()):
        self.plan, self.cap, self.ideas = plan, cap, list(ideas)
        self.caps = 0

    def approve_plan(self, plan):
        return self.plan

    def cap_reached(self, loops):
        self.caps += 1
        return self.cap.pop(0) if isinstance(self.cap, list) else self.cap

    def review_idea(self, idea):
        return self.ideas.pop(0)


class Display:
    def note(self, text):
        pass

    def pi_event(self, inv, event):
        pass


def start(factory, target, human=None, approve_plan=False):
    run = new_run(factory, target, "build an app", approve_plan)
    return run, Workflow(run, human or Human(), Display())


def test_happy_path(factory, target, fake_pi):
    s = fake_pi(script())
    run, wf = start(factory, target)
    wf.execute()
    st = run.state
    assert st.status == "done" and st.completed == [
        "branch",
        "prompt",
        "plan",
        "implement",
        "document",
        "merge",
        "improve",
    ]
    assert (run.dir / "plan.md").read_text().startswith("# Plan")
    assert "**Verdict:** approve" in (run.dir / "review-1.md").read_text()
    # target: standards installed, merged with --no-ff, docs written
    assert sh(target, "git", "branch", "--show-current").strip() == "main"
    assert (target / "app.py").exists() and (target / "docs/app.md").exists()
    assert (target / "coding-standards.md").exists()
    assert len(sh(target, "git", "log", "-1", "--format=%P").split()) == 2
    # factory: run artefacts committed, one commit per stage at least
    log = sh(factory, "git", "log", "--format=%s")
    for stage in ("branch", "prompt", "plan", "implement", "document", "merge", "improve"):
        assert f": {stage} —" in log
    assert not sh(factory, "git", "status", "--porcelain", "runs")
    # validation skipped (template has no commands) and logged
    events = [json.loads(l) for l in (run.dir / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "validation_skipped" for e in events)
    assert [c["agent"] for c in calls(s)] == ["planner", "implementer", "reviewer", "documenter", "improver"]
    assert json.loads((run.dir / "run-state.json").read_text())["tokens"]["by_stage"]["implement"]["input"] == 20


def test_validation_failure_skips_reviewer_and_feeds_back(factory, target, fake_pi):
    (target / ".sf.toml").write_text('validation = ["test -f ok"]\n')
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "cfg")
    s = fake_pi(
        script(
            implementer=[
                {"text": '{"summary": "s"}', "write": {"app.py": "x"}},
                {"text": '{"summary": "s"}', "write": {"ok": "1"}},
            ]
        )
    )
    run, wf = start(factory, target)
    wf.execute()
    agents = [c["agent"] for c in calls(s)]
    assert agents[:4] == ["planner", "implementer", "implementer", "reviewer"]
    second_impl = calls(s)[2]["argv"][-1]
    assert "Latest validation output (failed)" in second_impl and "test -f ok" in second_impl
    assert run.state.results["validation-1"] == "failed" and run.state.results["implement"] == "approved in loop 2"


def test_review_findings_fed_to_next_loop(factory, target, fake_pi):
    s = fake_pi(script(reviewer=[CHANGES, APPROVE]))
    start(factory, target)[1].execute()
    impl2 = [c for c in calls(s) if c["agent"] == "implementer"][1]["argv"][-1]
    assert "bad name" in impl2 and "app.py:1" in impl2


def test_cap_reached_merge_anyway_is_flagged(factory, target, fake_pi):
    s = fake_pi(script(reviewer=[CHANGES]))
    human = Human(cap=[2, "merge"])
    run, wf = start(factory, target, human)
    wf.execute()
    assert human.caps == 2 and run.state.loop == 7 and run.state.cap_reached
    improver_task = next(c for c in calls(s) if c["agent"] == "improver")["argv"][-1]
    assert "cap reached without approval: yes" in improver_task


def test_cap_abort(factory, target, fake_pi):
    fake_pi(script(reviewer=[CHANGES]))
    run, wf = start(factory, target, Human(cap="abort"))
    with pytest.raises(Aborted):
        wf.execute()
    assert run.state.status == "aborted" and "merge" not in run.state.completed


def test_plan_rejected_aborts(factory, target, fake_pi):
    s = fake_pi(script())
    _run, wf = start(factory, target, Human(plan=False), approve_plan=True)
    with pytest.raises(Aborted):
        wf.execute()
    assert [c["agent"] for c in calls(s)] == ["planner"]


def test_documenter_scope_enforced(factory, target, fake_pi):
    fake_pi(
        script(documenter=[{"text": '{"summary": "d"}', "write": {"app.py": "hacked", "coding-standards.md": "x"}}])
    )
    run, wf = start(factory, target)
    with pytest.raises(StageFailed, match="outside its scope"):
        wf.execute()
    assert (target / "app.py").read_text() == "print(1)\n"
    assert run.state.status == "failed"


def test_merge_conflict_aborts(factory, target, fake_pi):
    fake_pi(script())
    _run, wf = start(factory, target)
    real_document = wf.stage_document

    def document_then_conflict():
        result = real_document()
        sh(target, "git", "checkout", "-q", "main")
        (target / "app.py").write_text("conflict\n")
        sh(target, "git", "add", "-A")
        sh(target, "git", "commit", "-qm", "c")
        return result

    wf.stage_document = document_then_conflict
    with pytest.raises(Aborted, match="merge conflict"):
        wf.execute()
    assert not sh(target, "git", "status", "--porcelain")


def test_resume_continues_from_failed_stage(factory, target, fake_pi):
    s = fake_pi(script(documenter=[{"exit": 1}]))
    run, wf = start(factory, target)
    with pytest.raises(StageFailed):
        wf.execute()
    assert run.state.stage == "document" and run.state.status == "failed"
    fake_pi(script())
    s.with_suffix(".calls.jsonl").unlink()
    loaded = Run.load(factory, run.state.run_id)
    Workflow(loaded, Human(), Display()).execute()
    assert loaded.state.status == "done"
    assert [c["agent"] for c in calls(s)] == ["documenter", "improver"]


def test_improve_decisions_and_scope(factory, target, fake_pi):
    ideas = [IDEA, {**IDEA, "title": "Old bad idea"}, {**IDEA, "title": "Skip me"}]
    (factory / "improvements.jsonl").write_text(json.dumps({"title": "old  BAD idea", "decision": "rejected"}) + "\n")
    agents_file = factory / "agents" / "reviewer.md"
    fake_pi(
        script(
            improver=[{"text": json.dumps({"ideas": ideas})}],
            **{
                "improvement-implementer": [
                    {
                        "text": '{"summary": "s"}',
                        "write": {
                            str(agents_file): agents_file.read_text() + "\nBe strict.\n",
                            str(target / "coding-standards.md"): "# stricter\n",
                            str(target / "app.py"): "forbidden\n",
                            str(factory / "src.py"): "forbidden\n",
                        },
                    }
                ]
            },
        )
    )
    run, wf = start(factory, target, Human(ideas=[("approved", "go"), ("skipped", "")]))
    wf.execute()
    decisions = load_decisions(factory)
    assert [(d["title"], d["decision"]) for d in decisions[1:]] == [
        ("Tighten reviewer", "approved"),
        ("Skip me", "skipped"),
    ]
    assert decisions[1]["note"] == "go" and decisions[1]["run_id"] == run.state.run_id
    assert "Be strict." in agents_file.read_text()
    assert (target / "coding-standards.md").read_text() == "# stricter\n"
    assert (target / "app.py").read_text() == "print(1)\n" and not (factory / "src.py").exists()
    assert f"sf {run.state.run_id}: improvement — Tighten reviewer" in sh(factory, "git", "log", "--format=%s")
    assert "improvement — Tighten reviewer" in sh(target, "git", "log", "-1", "main", "--format=%s")
    assert not sh(factory, "git", "status", "--porcelain")
