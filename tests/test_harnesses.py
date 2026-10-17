import json

import pytest
from conftest import ROOT, calls

from sf.agents import load_agent, parse_agent
from sf.dashboard import list_runs, run_detail
from sf.run import Run, RunState, Usage
from sf.runner import StageFailed, run_agent


@pytest.fixture
def run(tmp_path):
    result = Run(tmp_path, RunState(run_id="r1", target=str(tmp_path), trunk="main", branch="sf/r1"))
    result.state.stage = "plan"
    return result


def agent(harness, name="planner"):
    a = load_agent(ROOT / "agents", name)
    return parse_agent(f"---\n{json.dumps(a.model_dump(exclude={'body'}) | {'harness': harness, 'model': 'native-model'})}\n---\n{a.body}")


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_native_success_and_observability(run, fake_harness, tmp_path, harness):
    script = fake_harness({"planner": [{"text": '{"plan":"ok"}', "bad_line": True}]})
    a = agent(harness)
    a.body = "role instructions with process-cleanup-pattern"
    assert run_agent(run, a, "task containing process-cleanup-pattern", tmp_path) == {"plan": "ok"}
    inv = run.state.invocations[0]
    assert inv.ok and inv.harness == harness and inv.session_id
    assert (inv.usage.input, inv.usage.output, inv.usage.cache_read) == (10, 5, 2)
    assert all("process-cleanup-pattern" not in arg for arg in calls(script)[0]["argv"])
    assert "process-cleanup-pattern" in calls(script)[0]["message"]
    if harness == "claude-code":
        assert inv.usage.cost == .01 and inv.usage.estimated and not inv.usage.unavailable
    else:
        assert inv.usage.unavailable == ["cache_write", "cost"]
        assert list_runs(tmp_path / "runs")[0]["cost"] is None
    detail = run_detail(tmp_path / "runs", "r1")
    assert detail["agents"]["planner"]["harness"] == harness
    assert len(detail["tools"]) == 1
    assert any(e["type"] == "agent_bad_line" for e in detail["events"])
    assert all(p[3] is None for p in detail["timeline"])
    assert f"{harness}-plan-1.stderr" in detail["artefacts"]
    assert "fake native stderr" in (run.dir / f"{harness}-plan-1.stderr").read_text()
    assert (run.dir / f"{harness}-plan-1.jsonl").read_text().startswith("bad JSON")


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_native_contract_retries_same_session_and_no_double_count(run, fake_harness, tmp_path, harness):
    script = fake_harness({"planner": [{"text": "no json"}, {"text": '{"plan":3}'}, {"text": '{"plan":"ok"}'}]})
    a = agent(harness)

    class Display:
        def agent_event(self, inv, event):
            a.model = "changed-while-running"
            a.harness = "pi"

    assert run_agent(run, a, "task", tmp_path, Display()) == {"plan": "ok"}
    recorded = calls(script)
    assert len(recorded) == 3 and len({c["session"] for c in recorded}) == 1
    assert "plan: 3 is not of type 'string'" in recorded[2]["message"]
    assert all(c["argv"][c["argv"].index("--model") + 1] == "native-model" for c in recorded)
    inv = run.state.invocations[0]
    assert inv.usage.input == 30 and inv.usage.output == 15
    assert inv.usage.cost == (.03 if harness == "claude-code" else 0)
    assert inv.model == "native-model" and inv.harness == harness


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_native_retry_cap(run, fake_harness, tmp_path, harness):
    script = fake_harness({"planner": [{"text": "no json"}]})
    with pytest.raises(StageFailed, match="2 re-prompts"):
        run_agent(run, agent(harness), "task", tmp_path)
    assert len(calls(script)) == 3


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("step", [{"events": []}, {"exit": 1, "text": '{"plan":"valid"}'}])
def test_native_incomplete_and_exit_failures(run, fake_harness, tmp_path, harness, step):
    fake_harness({"planner": [step]})
    with pytest.raises(StageFailed):
        run_agent(run, agent(harness), "task", tmp_path)
    assert not run.state.invocations[0].ok
    assert any(e.get("failure") for e in run_detail(tmp_path / "runs", "r1")["events"])


@pytest.mark.parametrize("harness,events", [
    ("claude-code", [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": '{"plan":"valid"}', "errors": ["failure"]}]),
    ("claude-code", [{"type": "result", "subtype": "success", "result": '{"plan":"valid"}',
                      "permission_denials": [{"tool_name": "Write"}]}]),
    ("codex", [{"type": "turn.failed", "error": {"message": "failure"}}]),
])
def test_native_semantic_failures_despite_exit_zero(run, fake_harness, tmp_path, harness, events):
    fake_harness({"planner": [{"events": events}]})
    with pytest.raises(StageFailed, match="failure|permission denied"):
        run_agent(run, agent(harness), "task", tmp_path)


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_missing_binary_is_observable(run, tmp_path, monkeypatch, harness):
    monkeypatch.setenv("SF_CLAUDE" if harness == "claude-code" else "SF_CODEX", str(tmp_path / "absent"))
    with pytest.raises(StageFailed, match="No such file"):
        run_agent(run, agent(harness), "task", tmp_path)
    assert run_detail(tmp_path / "runs", "r1")["events"][-1]["type"] == "agent_end"


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("name", ["planner", "implementer"])
def test_native_permission_and_resume_commands(run, fake_harness, tmp_path, harness, name):
    script = fake_harness({name: [{"text": "no json"}, {"text": '{"plan":"ok","summary":"ok"}'}]})
    run_agent(run, agent(harness, name), "task", tmp_path)
    for call in calls(script):
        argv = call["argv"]
        assert not any("bypass" in arg or "skip-permissions" in arg for arg in argv)
        if harness == "codex":
            sandbox = "read-only" if name == "planner" else "workspace-write"
            assert f'sandbox_mode="{sandbox}"' in argv and 'approval_policy="never"' in argv
        else:
            tools = argv[argv.index("--tools") + 1].split(",")
            assert ("Bash" in tools) == (name == "implementer")
            assert ("Write" in tools) == (name != "planner")
            assert argv[argv.index("--permission-prompts") + 1] == "none"


def test_mixed_totals_distinguish_unknown_from_zero(run):
    pi = run.next_invocation("planner")
    pi.usage = Usage(cost=0)
    codex = run.next_invocation("reviewer")
    codex.usage = Usage(input=10, known=["input"], unavailable=["output", "cache_read", "cache_write", "cost"])
    total = run.state.totals()["run"]
    assert "cost" in total["unavailable"] and "cost" in total["known"]
    assert total["input"] == 10 and total["cost"] == 0


def test_normalized_pi_events_not_duplicated_in_dashboard(run, fake_pi, tmp_path):
    fake_pi({"planner": [{"text": '{"plan":"ok"}'}]})
    run_agent(run, load_agent(ROOT / "agents", "planner").model_copy(update={"harness": "pi"}), "task", tmp_path)
    detail = run_detail(tmp_path / "runs", "r1")
    assert len(detail["tools"]) == 1
    context = [p[3] for p in detail["timeline"] if p[3] is not None]
    assert context == [18]
    assert detail["agents"]["planner"]["harness"] == "pi"


def test_duplicate_codex_completion_not_double_counted(run, fake_harness, tmp_path):
    completed = {"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 5}}
    fake_harness({"planner": [{"events": [
        {"type": "thread.started", "thread_id": "s"},
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": '{"plan":"ok"}'}},
        completed, completed,
    ]}]})
    run_agent(run, agent("codex"), "task", tmp_path)
    assert run.state.invocations[0].usage.input == 10


def test_duplicate_claude_result_not_double_counted(run, fake_harness, tmp_path):
    result = {"type": "result", "subtype": "success", "result": '{"plan":"ok"}', "session_id": "s",
              "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": .01}
    fake_harness({"planner": [{"events": [result, result]}]})
    run_agent(run, agent("claude-code"), "task", tmp_path)
    assert run.state.invocations[0].usage.input == 10
    assert run.state.invocations[0].usage.unavailable == ["cache_read", "cache_write"]
