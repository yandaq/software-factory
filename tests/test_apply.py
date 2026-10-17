"""Apply suggestions through the dashboard using real invocation artifacts and fake harnesses."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import calls, sh
from typer.testing import CliRunner

from sf import git as g
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
    assert sh(factory, "git", "rev-parse", "HEAD") != before
    assert not sh(factory, "git", "status", "--porcelain")
    committed = json.loads(sh(factory, "git", "show", "HEAD:improvements.jsonl").splitlines()[0])
    assert committed == load_decisions(factory)[0]
    state = Run.load(factory, application["run_id"]).state
    assert state.kind == "improvement" and state.status == "done"
    assert state.invocations[0].harness == "pi" and state.invocations[0].ok
    assert state.completed == ["implement"]
    assert state.results["commit"] == sh(factory, "git", "rev-parse", "HEAD").strip()
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
    before = sh(factory, "git", "rev-parse", "HEAD")
    assert request(apply_server)[0] == 202
    failure = finished(factory)
    assert failure["status"] == "failed" and "exited with code 1" in failure["error"]
    assert Run.load(factory, failure["run_id"]).state.status == "failed"
    assert sh(factory, "git", "rev-parse", "HEAD") == before
    assert request(apply_server)[0] == 202
    success = finished(factory)
    assert success["status"] == "applied" and success["run_id"] != failure["run_id"]
    assert len(calls(script)) == 2


def test_validation_failure_keeps_partial_changes(apply_server, factory, fake_pi):
    (factory / ".sf.toml").write_text('validation = ["exit 7"]\n')
    fake_pi({"implementer": [{"text": '{"summary":"Implemented"}', "write": {"partial.py": "partial"}}]})
    before = sh(factory, "git", "rev-parse", "HEAD")
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "failed" and "validation failed" in result["error"]
    assert (factory / "partial.py").read_text() == "partial"
    log = factory / "runs" / result["run_id"] / "validation-1.log"
    assert "[exit 7]" in log.read_text()
    assert sh(factory, "git", "rev-parse", "HEAD") == before
    assert "commit" not in Run.load(factory, result["run_id"]).state.results


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


@pytest.mark.parametrize("name", ["../outside.md", "/tmp/outside.md", "missing.md", "code.py",
                                  "src/other/runner.py", "tests/test_runner.py", "src/sf/missing.py",
                                  "src/sf/../outside.py"])
def test_refuse_ineligible_improvement_file(apply_server, factory, name):
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


@pytest.mark.parametrize("name", ["src/sf/workflow.py", "src/sf/nested/runner.py"])
def test_apply_runner_suggestion(apply_server, factory, fake_pi, name):
    source = factory / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Original runner\n")
    (factory / "improvements.jsonl").write_text(json.dumps(IDEA | {"file": name}) + "\n")
    script = fake_pi({"implementer": [{"text": '{"summary":"Runner fixed"}',
                                      "write": {name: "# Fixed runner\n"}}]})
    before = sh(factory, "git", "rev-parse", "HEAD")
    assert request(apply_server)[0] == 202
    assert finished(factory)["status"] == "applied"
    assert source.read_text() == "# Fixed runner\n"
    assert calls(script)[0]["cwd"] == str(factory)
    assert name in calls(script)[0]["message"]
    assert sh(factory, "git", "rev-parse", "HEAD") != before
    assert not sh(factory, "git", "status", "--porcelain")


@pytest.mark.parametrize("kind", ["symlink", "parent-symlink", "ignored"])
def test_apply_refuses_unsafe_or_ignored_runner_source(apply_server, factory, tmp_path, kind):
    source = factory / "src/sf/runner.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "runner.py").write_text("# Outside runner\n")
    if kind == "symlink":
        source.symlink_to(outside / "runner.py")
    elif kind == "parent-symlink":
        source.parent.rmdir()
        source.parent.symlink_to(outside, target_is_directory=True)
    else:
        source.write_text("# Ignored runner\n")
        with (factory / ".gitignore").open("a") as ignore:
            ignore.write("src/sf/runner.py\n")
    (factory / "improvements.jsonl").write_text(json.dumps(IDEA | {"file": "src/sf/runner.py"}) + "\n")
    assert request(apply_server)[0] == 409
    assert (outside / "runner.py").read_text() == "# Outside runner\n"
    assert not (factory / "runs/.lock").exists()


def test_success_commits_staged_untracked_and_deleted_files(apply_server, factory, fake_pi):
    (factory / "staged.txt").write_text("Already staged\n")
    sh(factory, "git", "add", "staged.txt")
    (factory / "untracked.txt").write_text("Existing untracked\n")
    (factory / "README.md").unlink()
    fake_pi({"implementer": [{"text": '{"summary":"Done"}'}]})
    before = sh(factory, "git", "rev-parse", "HEAD").strip()
    branch = sh(factory, "git", "branch", "--show-current")
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "applied"
    assert sh(factory, "git", "rev-parse", "HEAD^").strip() == before
    assert sh(factory, "git", "show", "HEAD:staged.txt") == "Already staged\n"
    assert sh(factory, "git", "show", "HEAD:untracked.txt") == "Existing untracked\n"
    assert "README.md" not in sh(factory, "git", "ls-tree", "--name-only", "HEAD").splitlines()
    assert sh(factory, "git", "branch", "--show-current") == branch
    assert not sh(factory, "git", "status", "--porcelain")


def test_commit_failure_is_retryable_without_success_signal(apply_server, factory, fake_pi):
    hook = factory / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho 'commit rejected' >&2\nexit 1\n")
    hook.chmod(0o755)
    fake_pi({"implementer": [{"text": '{"summary":"Done"}', "write": {"partial.py": "partial"}}]})
    before = sh(factory, "git", "rev-parse", "HEAD")
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "failed" and "commit rejected" in result["error"]
    assert sh(factory, "git", "rev-parse", "HEAD") == before
    assert (factory / "partial.py").read_text() == "partial"
    run = Run.load(factory, result["run_id"])
    assert run.state.status == "failed"
    assert "commit" not in run.state.results
    assert "application_applied" not in (run.dir / "events.jsonl").read_text()
    hook.unlink()
    assert request(apply_server)[0] == 202
    assert finished(factory)["status"] == "applied"
    assert not sh(factory, "git", "status", "--porcelain")


def test_ignored_history_and_no_changes_do_not_create_empty_commit(apply_server, factory, fake_pi):
    with (factory / ".gitignore").open("a") as ignore:
        ignore.write("improvements.jsonl\n")
    sh(factory, "git", "add", ".gitignore")
    sh(factory, "git", "commit", "-qm", "Ignore local history")
    before = sh(factory, "git", "rev-parse", "HEAD")
    fake_pi({"implementer": [{"text": '{"summary":"Already satisfied"}'}]})
    assert request(apply_server)[0] == 202
    result = finished(factory)
    assert result["status"] == "applied"
    assert sh(factory, "git", "rev-parse", "HEAD") == before
    assert Run.load(factory, result["run_id"]).state.results["commit"] == "No changes to commit"



def test_success_is_not_visible_until_commit_succeeds(apply_server, factory, fake_pi, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked_commit(*args):
        entered.set()
        assert release.wait(5)
        raise g.GitError("Commit rejected after waiting")

    monkeypatch.setattr("sf.improve.g.commit_all", blocked_commit)
    fake_pi({"implementer": [{"text": '{"summary":"Done"}'}]})
    assert request(apply_server)[0] == 202
    try:
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            reader = pool.submit(load_decisions, factory)
            try:
                with pytest.raises(TimeoutError):
                    reader.result(timeout=.1)
            finally:
                release.set()
            observed = reader.result(timeout=5)[0]["application"]["status"]
            assert observed in ("running", "failed")
    finally:
        release.set()
    assert finished(factory)["status"] == "failed"
