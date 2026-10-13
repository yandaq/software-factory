import json

import pytest
from conftest import calls, sh
from test_workflow import APPROVE, CHANGES, IDEA, Display, Human, script, start

from sf.dashboard import set_agent_config
from sf.improve import load_decisions
from sf.run import Run
from sf.runner import StageFailed
from sf.workflow import Workflow


def configure(factory, fake_pi, fake_harness, steps):
    fake_pi(steps)
    native = fake_harness(steps)
    for name, harness in (("implementer", "claude-code"), ("reviewer", "codex"),
                          ("documenter", "claude-code"), ("improver", "codex"),
                          ("improvement-implementer", "codex"), ("improvement-reviewer", "codex")):
        assert set_agent_config(factory / "agents", name, "native-model", "medium", harness) is None
    sh(factory, "git", "add", "agents")
    sh(factory, "git", "commit", "-qm", "harness config")
    return native


def test_mixed_harness_workflow_preserves_approval_validation_and_review(factory, target, fake_pi, fake_harness):
    (target / ".sf.toml").write_text('validation = ["test -f ok"]\n')
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "validation config")
    steps = script(implementer=[
        {"text": '{"summary":"first"}', "write": {"app.py": "x"}},
        {"text": '{"summary":"second"}', "write": {"ok": "ok"}},
        {"text": '{"summary":"third"}', "write": {"app.py": "fixed"}},
    ], reviewer=[CHANGES, APPROVE])
    native = configure(factory, fake_pi, fake_harness, steps)
    run, wf = start(factory, target, Human(plan=True), approve_plan=True)
    wf.execute()
    assert run.state.status == "done" and run.state.loop == 3
    assert run.state.results["validation-1"] == "failed"
    assert (run.dir / "review-3.md").exists() and (target / "docs/app.md").exists()
    implementers = [c for c in calls(native) if c["agent"] == "implementer"]
    assert "Latest validation output (failed)" in implementers[1]["message"]
    assert "bad name" in implementers[2]["message"]
    assert {i.harness for i in run.state.invocations} == {"pi", "claude-code", "codex"}
    assert "cost" in run.state.totals()["run"]["unavailable"]
    assert "cost" in run.state.totals()["run"]["known"]
    assert sh(target, "git", "branch", "--show-current").strip() == "main"


def test_mixed_harness_resume_failed_stage(factory, target, fake_pi, fake_harness):
    configure(factory, fake_pi, fake_harness, script(documenter=[{"exit": 1}]))
    run, wf = start(factory, target)
    with pytest.raises(StageFailed):
        wf.execute()
    assert run.state.status == "failed" and run.state.stage == "document"
    fake_harness(script())
    resumed = Run.load(factory, run.state.run_id)
    Workflow(resumed, Human(), Display()).execute()
    assert resumed.state.status == "done"
    assert len([i for i in resumed.state.invocations if i.agent == "planner"]) == 1
    assert len([i for i in resumed.state.invocations if i.agent == "documenter"]) == 2


def test_native_documenter_scope_enforced(factory, target, fake_pi, fake_harness):
    configure(factory, fake_pi, fake_harness, script(documenter=[
        {"text": '{"summary":"wrong"}', "write": {"app.py": "violation"}},
    ]))
    run, wf = start(factory, target)
    with pytest.raises(StageFailed, match="scope"):
        wf.execute()
    assert (target / "app.py").read_text() == "print(1)\n"
    assert run.state.status == "failed"


def test_native_improvement_decisions_and_scope(factory, target, fake_pi, fake_harness):
    steps = script(improver=[{"text": json.dumps({"ideas": [IDEA]})}],
                   **{"improvement-implementer": [{"text": '{"summary":"improved"}', "write": {
                       "agents/implementer.md": (factory / "agents/implementer.md").read_text() + "\nExtra rule\n",
                       "unrelated.py": "violation",
                       str(target / "docs/coding-standards.md"): "# Improved standards\n",
                   }}]})
    native = configure(factory, fake_pi, fake_harness, steps)
    run, wf = start(factory, target, Human())
    wf.execute()
    assert run.state.status == "done" and load_decisions(factory)[0]["decision"] == "automatic"
    assert not (factory / "unrelated.py").exists()
    assert (target / "docs/coding-standards.md").read_text() == "# Improved standards\n"
    implementer = next(c for c in calls(native) if c["agent"] == "improvement-implementer")
    assert "Apply this automatically selected improvement." in implementer["message"]
    assert 'sandbox_workspace_write.writable_roots=' in " ".join(implementer["argv"])
    review = calls(native)[-1]
    assert review["agent"] == "improvement-reviewer"
    assert 'sandbox_mode="read-only"' in review["argv"]
    assert 'agents/implementer.md' in review["message"]
