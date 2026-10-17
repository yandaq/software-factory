import json

import pytest
from conftest import ROOT, calls

from sf.agents import load_agent
from sf.dashboard import run_detail
from sf.pi import StageFailed, extract_json, run_agent
from sf.run import Run, RunState


@pytest.fixture
def run(tmp_path):
    r = Run(tmp_path, RunState(run_id="r1", target=str(tmp_path), trunk="main", branch="sf/r1"))
    r.state.stage = "plan"
    return r


def planner():
    return load_agent(ROOT / "agents", "planner").model_copy(update={"harness": "pi"})


def test_success_returns_contract_and_tracks_usage(run, fake_pi, tmp_path):
    fake_pi({"planner": [{"text": 'Done.\n```json\n{"plan": "# P"}\n```'}]})
    assert run_agent(run, planner(), "task", tmp_path) == {"plan": "# P"}
    inv = run.state.invocations[0]
    assert inv.ok and inv.usage.input == 10 and inv.usage.cost == 0.01
    assert (run.dir / "pi-plan-1.stderr").read_text().strip() == "fake pi stderr"
    events = [json.loads(l) for l in (run.dir / "events.jsonl").read_text().splitlines()]
    assert all({"ts", "run_id", "stage", "loop"} <= e.keys() for e in events)
    assert not any(e["source"] == "pi" for e in events)
    assert any(e["source"] == "agent" and e["event"]["type"] == "agent_settled" for e in events)
    state = json.loads((run.dir / "run-state.json").read_text())
    assert state["tokens"]["run"]["output"] == 5


@pytest.mark.parametrize(
    "step,msg",
    [
        ({"exit": 1, "text": '{"plan": "x"}'}, "exited with code 1"),
        ({"aborted": True, "text": '{"plan": "x"}'}, "aborted"),
        ({"stop": "error", "text": '{"plan": "x"}'}, "stopReason=error"),
    ],
)
def test_failures_detected_despite_exit_zero(run, fake_pi, tmp_path, step, msg):
    fake_pi({"planner": [step]})
    with pytest.raises(StageFailed, match=msg):
        run_agent(run, planner(), "task", tmp_path)
    assert not run.state.invocations[0].ok


def test_invalid_contract_reprompts_in_same_session(run, fake_pi, tmp_path):
    script = fake_pi({"planner": [{"text": "no json"}, {"text": '{"plan": 3}'}, {"text": '{"plan": "ok"}'}]})
    assert run_agent(run, planner(), "task", tmp_path) == {"plan": "ok"}
    c = calls(script)
    assert len(c) == 3
    session = c[1]["argv"][c[1]["argv"].index("--session") + 1]
    assert c[2]["argv"][c[2]["argv"].index("--session") + 1] == session
    assert "plan: 3 is not of type 'string'" in c[2]["message"]
    assert run.state.invocations[0].usage.input == 30


def test_contract_fails_after_two_reprompts(run, fake_pi, tmp_path):
    script = fake_pi({"planner": [{"text": "nope"}]})
    with pytest.raises(StageFailed, match="2 re-prompts"):
        run_agent(run, planner(), "task", tmp_path)
    assert len(calls(script)) == 3


def test_pi_events_persist_once_and_remain_visible(run, fake_pi, target):
    text = '{"plan": "ok"}'
    fake_pi({"planner": [{"text": text}]})
    run.state.target = str(target)
    assert run_agent(run, planner(), "task", target) == {"plan": "ok"}
    inv = run.state.invocations[0]
    usage = {
        "input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1, "totalTokens": 18,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0.01},
    }
    native_events = [
        {"type": "session", "version": 3, "id": inv.session_id, "cwd": str(target)},
        {"type": "agent_start"},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "args": {"path": "x"}},
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "usage": usage,
            "content": [{"type": "text", "text": text}],
        }},
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "agent_settled", "aborted": False},
    ]
    expected_stdout = "".join(json.dumps(e) + "\n" for e in native_events).encode()
    assert (run.dir / "pi-plan-1.jsonl").read_bytes() == expected_stdout
    records = [json.loads(line) for line in (run.dir / "events.jsonl").read_text().splitlines()]
    shared = [e for e in records if e["source"] in {"pi", "agent"}]
    assert [e["event"] for e in shared] == native_events
    assert all(e["source"] == "agent" and e["harness"] == "pi" for e in shared)
    assert [e["type"] for e in records if e["source"] == "sf"] == [
        "agent_start", "pi_start", "pi_end", "agent_end",
    ]
    assert inv.ok and inv.session_id
    assert (inv.usage.input, inv.usage.output, inv.usage.cache_read, inv.usage.cache_write,
            inv.usage.cost) == (10, 5, 2, 1, 0.01)

    detail = run_detail(run.dir.parent, run.state.run_id)
    assert detail["documents_error"] is None
    assert detail["tools"] == [shared[2]]
    expected_timeline = [
        [e["ts"], "planner", e.get("event", e).get("type"),
         18 if e.get("event", {}).get("type") == "message_end" else None, 1]
        for e in records
    ]
    assert detail["timeline"] == expected_timeline
    assert "pi-plan-1.jsonl" in detail["artefacts"]

    # Older runs containing both sources must still show each activity only once.
    for event in native_events:
        run.emit("pi", {"agent": "planner", "invocation": inv.n, "event": event})
    historical = run_detail(run.dir.parent, run.state.run_id)
    assert historical["tools"] == detail["tools"]
    assert historical["timeline"] == detail["timeline"]


def test_extract_json_takes_last_object():
    assert extract_json('a {"x": {"y": 1}} b {"z": 2}') == {"z": 2}


def test_task_passed_as_file_not_argv(run, fake_pi, tmp_path):
    # An agent running `pkill -f "node main.js"` must not match its own pi process via the task text.
    script = fake_pi({"planner": [{"text": '{"plan": "ok"}'}]})
    run_agent(run, planner(), "start it with node main.js", tmp_path)
    (c,) = calls(script)
    assert c["message"].startswith("start it with node main.js")
    assert not any("node main.js" in a for a in c["argv"])
