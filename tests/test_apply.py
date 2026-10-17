"""Apply suggestions through the dashboard using real invocation artifacts and fake harnesses."""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import calls, sh
from typer.testing import CliRunner

from sf.apply import recover_applications, start_application
from sf.cli import app
from sf.dashboard import serve, set_agent_config
from sf.improve import decision_identity, load_decisions, record_decision
from sf.run import Run

IDEA = {"ts": "2026-01-01", "run_id": "source-run", "title": "Improve factory",
        "file": "agents/implementer.md", "rationale": "Make it work", "evidence": "Observed failure",
        "proposed_change": "Document the fix", "decision": "automatic", "note": ""}


@pytest.fixture
def apply_server(factory):
    record_decision(factory, IDEA)
    server = serve(factory / "runs", "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, body=IDEA, method="POST", path="/api/improvements/apply"):
    req = urllib.request.Request(server + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def finished(factory):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        application = load_decisions(factory)[0].get("application", {})
        if application.get("status") in ("applied", "failed") and not (factory / "runs" / ".lock").exists():
            return application
        time.sleep(.01)
    pytest.fail("Implementation did not finish within 10 seconds")


def test_apply_runs_only_configured_implementer_in_dirty_factory(apply_server, factory, fake_pi):
    script = fake_pi({"implementer": [{"text": '{"summary":"Delivered"}', "write": {"fix.py": "fixed\n"}}]})
    (factory / "README.md").write_text("existing edits\n")
    before = sh(factory, "git", "rev-parse", "HEAD")
    status, response = request(apply_server)
    assert status == 202
    application = finished(factory)
    assert application["status"] == "applied"
    assert application["summary"] == "Delivered"
    assert response["run_id"] == application["run_id"]
    assert len(calls(script)) == 1
    invocation = calls(script)[0]
    assert invocation["agent"] == "implementer" and invocation["cwd"] == str(factory)
    for text in (IDEA["proposed_change"], IDEA["evidence"], "preserve all existing", "uncommitted", "README.md"):
        assert text in invocation["message"]
    assert (factory / "README.md").read_text() == "existing edits\n"
    assert (factory / "fix.py").read_text() == "fixed\n"
    assert sh(factory, "git", "rev-parse", "HEAD") == before
    state = Run.load(factory, application["run_id"]).state
    assert state.kind == "improvement" and state.status == "done"
    assert state.invocations[0].harness == "pi" and state.invocations[0].ok
    assert state.completed == ["implement"]
    assert {k: v for k, v in load_decisions(factory)[0].items() if k != "application"} == IDEA
    assert request(apply_server)[0] == 409
    assert request(apply_server, {**IDEA, "title": "Gone"})[0] == 404


@pytest.mark.parametrize("body", [{}, [], {"ts": 1, "run_id": "r", "title": "t"}])
def test_apply_bad_identity(apply_server, body):
    assert request(apply_server, body)[0] == 400


def test_apply_busy_and_delete_running(apply_server, factory, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def agent(*args):
        entered.set()
        assert release.wait(5)
        return {"summary": "Done"}

    monkeypatch.setattr("sf.apply.run_agent", agent)
    try:
        status, started = request(apply_server)
        assert status == 202
        assert entered.wait(2)
        assert request(apply_server, method="DELETE", path="/api/runs/" + started["run_id"])[0] == 409
        assert request(apply_server)[0] == 409
        status, response = request(apply_server, method="DELETE", path="/api/improvements")
        assert status == 409 and "running" in response["error"]
        assert len(load_decisions(factory)) == 1
    finally:
        release.set()
    assert finished(factory)["status"] == "applied"


def test_agent_failure_is_retryable(apply_server, factory, fake_pi):
    script = fake_pi({"implementer": [{"exit": 1}, {"text": '{"summary":"Retry succeeded"}'}]})
    assert request(apply_server)[0] == 202
    failure = finished(factory)
    assert failure["status"] == "failed" and "exited with code 1" in failure["error"]
    assert Run.load(factory, failure["run_id"]).state.status == "failed"
    assert request(apply_server)[0] == 202
    success = finished(factory)
    assert success["status"] == "applied" and success["run_id"] != failure["run_id"]
    assert len(calls(script)) == 2


def test_validation_failure_keeps_partial_changes(apply_server, factory, fake_pi):
    (factory / ".sf.toml").write_text('validation = ["exit 7"]\n')
    fake_pi({"implementer": [{"text": '{"summary":"Implemented"}', "write": {"partial.py": "partial"}}]})
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "failed" and "validation failed" in result["error"]
    assert (factory / "partial.py").read_text() == "partial"
    log = factory / "runs" / result["run_id"] / "validation-1.log"
    assert "[exit 7]" in log.read_text()


def test_startup_failure_records_failure_and_unlocks(apply_server, factory, monkeypatch):
    def fail(self):
        raise RuntimeError("Cannot start worker")

    # Call the application service directly so the HTTP server's request threads can still start.
    monkeypatch.setattr("sf.apply.threading.Thread.start", fail)
    with pytest.raises(RuntimeError, match="Cannot start worker"):
        start_application(factory, decision_identity(IDEA))
    assert finished(factory)["status"] == "failed"


def test_recover_interrupted_execution(apply_server, factory, monkeypatch):
    def no_start(self):
        pass

    monkeypatch.setattr("sf.apply.threading.Thread.start", no_start)
    run_id = start_application(factory, decision_identity(IDEA))
    assert load_decisions(factory)[0]["application"]["pid"] == os.getpid()
    # Simulate a server process dying; its original lock is stale as well.
    monkeypatch.setattr("sf.apply._alive", lambda pid: False)
    (factory / "runs" / ".lock").unlink()
    recover_applications(factory)
    assert finished(factory)["status"] == "failed"
    assert Run.load(factory, run_id).state.status == "failed"
    assert "interrupted" in finished(factory)["error"]


@pytest.mark.parametrize("name", ["../outside.md", "/tmp/outside.md", "missing.md", "code.py"])
def test_refuse_non_factory_markdown(apply_server, factory, name):
    (factory / "improvements.jsonl").write_text(json.dumps(IDEA | {"file": name}) + "\n")
    assert request(apply_server)[0] == 409
    assert not (factory / "runs" / ".lock").exists()


def test_resume_refuses_improvement_execution(apply_server, factory, fake_pi, monkeypatch):
    fake_pi({"implementer": [{"text": '{"summary":"Done"}'}]})
    assert request(apply_server)[0] == 202
    result = finished(factory)
    monkeypatch.chdir(factory)
    cli_result = CliRunner().invoke(app, ["resume", result["run_id"]])
    assert cli_result.exit_code != 0
    assert "Retry improvement executions" in cli_result.output


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_apply_uses_native_agent_settings(apply_server, factory, fake_harness, harness):
    assert set_agent_config(factory / "agents", "implementer", "native-test", "high", harness) is None
    fake_harness({"implementer": [{"text": '{"summary":"Native done"}'}]})
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "applied"
    invocation = Run.load(factory, result["run_id"]).state.invocations[0]
    assert (invocation.harness, invocation.model, invocation.thinking) == (harness, "native-test", "high")


def test_validation_success_has_evidence(apply_server, factory, fake_pi):
    (factory / ".sf.toml").write_text('validation = ["echo validated"]\n')
    fake_pi({"implementer": [{"text": '{"summary":"Done"}'}]})
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "applied"
    run = Run.load(factory, result["run_id"])
    assert run.state.results["validation-1"] == "passed"
    assert "validated\n[exit 0]" in (run.dir / "validation-1.log").read_text()


def test_bad_configuration_and_unreadable_history_release_lock(apply_server, factory):
    path = factory / "agents" / "implementer.md"
    path.write_text("invalid")
    assert request(apply_server)[0] == 500
    assert "application" not in load_decisions(factory)[0]
    assert not (factory / "runs" / ".lock").exists()
    history = factory / "improvements.jsonl"
    history.write_text("{broken\n")
    assert request(apply_server)[0] == 500
    assert history.read_text() == "{broken\n"
    assert not (factory / "runs" / ".lock").exists()


def test_log_updates_preserve_concurrent_records(factory):
    from sf.improve import update_application

    record_decision(factory, IDEA)
    other = IDEA | {"title": "Another"}
    record_decision(factory, other)
    threads = [threading.Thread(target=update_application,
                               args=(factory, decision_identity(idea), {"status": "applied"}))
               for idea in (IDEA, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [i["application"]["status"] for i in load_decisions(factory)] == ["applied", "applied"]
