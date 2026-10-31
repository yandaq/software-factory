"""`sf submit`, `sf runner` (worker.serve) and queued `sf resume`."""

import os
import threading
import time

import pytest
from conftest import sh
from test_queued_workflow import ALL_CAPS, claim, jobs
from test_workflow import script
from typer.testing import CliRunner

from sf import queue, store, worker, workflow
from sf.cli import app
from sf.workflow import Finished, StageResult, complete_stage, submit_run


@pytest.fixture
def env(monkeypatch, db, db_url, factory):
    monkeypatch.setenv("SF_DATABASE_URL", db_url)
    monkeypatch.chdir(factory)
    return db


def invoke(*args, input=None):
    return CliRunner().invoke(app, list(args), input=input)


def test_submit_creates_run_prompt_and_first_job(env, target, tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("build an app")
    result = invoke("submit", str(target), str(prompt))
    assert result.exit_code == 0, result.output
    run_id = result.output.strip()
    assert store.get_document(env, run_id, "prompt.md") == "build an app\n"
    assert [(j["stage"], j["status"]) for j in jobs(env, run_id)] == [("branch", "queued")]
    state = store.load_state(env, run_id)
    assert state.status == "running" and state.trunk == "main" and state.branch == f"sf/{run_id}"
    assert store.load_config(env, run_id)["agents"]
    assert [e["type"] for e in store.events(env, run_id)] == ["run_created", "job_enqueued"]


def test_submit_reads_stdin(env, target):
    result = invoke("submit", str(target), "-", input="from stdin\n")
    assert result.exit_code == 0, result.output
    assert store.get_document(env, result.output.strip(), "prompt.md") == "from stdin\n"


def test_submit_without_database_url_fails_clearly(monkeypatch, factory, target):
    monkeypatch.delenv("SF_DATABASE_URL", raising=False)
    monkeypatch.chdir(factory)
    result = invoke("submit", str(target), "-", input="x")
    assert result.exit_code == 1 and "SF_DATABASE_URL is not set" in result.output


def test_submit_rejects_approve_plan(env, target):
    result = invoke("submit", str(target), "-", "--approve-plan", input="x")
    assert result.exit_code == 2 and "not supported for queued runs" in result.output
    assert env.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_submit_rejects_unusable_target_without_creating_a_run(env, tmp_path):
    not_git = tmp_path / "plain"
    not_git.mkdir()
    result = invoke("submit", str(not_git), "-", input="x")
    assert result.exit_code == 1 and "error:" in result.output
    assert env.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_runner_fails_fast_without_env(monkeypatch, factory):
    monkeypatch.delenv("SF_DATABASE_URL", raising=False)
    monkeypatch.delenv("SF_S3_BUCKET")
    monkeypatch.chdir(factory)
    result = invoke("runner")
    assert result.exit_code == 1
    assert "SF_DATABASE_URL" in result.output and "SF_S3_BUCKET" in result.output


def test_detect_caps_uses_harness_env(monkeypatch, fake_pi, fake_harness):
    assert set(worker.detect_caps()) >= set(ALL_CAPS)


def test_detect_caps_advertises_only_listed_pi_models(monkeypatch, fake_pi):
    monkeypatch.setenv("FAKE_PI_MODELS", "halogen/q3,anthropic/claude-x")
    caps = worker.detect_caps(log=lambda **r: pytest.fail(f"unexpected log {r}"))
    assert {"harness:pi", "model:pi:halogen/q3", "model:pi:anthropic/claude-x"} <= set(caps)
    assert not any(c.startswith("model:pi:openai/") for c in caps)


def test_detect_caps_logs_failed_pi_listing_and_adds_no_model_caps(monkeypatch, fake_pi):
    monkeypatch.setenv("FAKE_PI_LIST_EXIT", "1")
    logs = []
    caps = worker.detect_caps(log=lambda **r: logs.append(r))
    assert "harness:pi" in caps and not any(c.startswith("model:") for c in caps)
    assert logs[0]["event"] == "pi_models_failed" and "list-models" in logs[0]["error"]


def test_runner_without_the_agents_pi_model_cannot_claim(db, factory, target):
    config = store.snapshot_config(factory, target)
    state = submit_run(db, factory, target, "x")
    caps = workflow.required_caps(config, "plan")
    assert caps == ["harness:pi", "model:pi:fake/model"]
    queue.enqueue(db, state.run_id, "plan", required_caps=caps)
    other = ["harness:pi", "model:pi:openai/other"]
    queue.register_runner(db, "no-auth", "host", other)
    assert all(j["stage"] != "plan" for j in iter(lambda: queue.claim(db, "no-auth", other), None))
    queue.register_runner(db, "ok", "host", ALL_CAPS)
    assert queue.claim(db, "ok", ALL_CAPS)["stage"] == "plan"


class Serve:
    """worker.serve in a thread, logging into a list."""

    def __init__(self, factory, db_url, **kw):
        self.stop, self.logs = threading.Event(), []
        kw = {"lease_seconds": 30, "heartbeat_seconds": 0.2, "poll_seconds": 0.2} | kw
        self.thread = threading.Thread(target=worker.serve, args=(
            factory, "r1", "host", ALL_CAPS, kw.pop("concurrency", 1), self.stop),
            kwargs={"db_url": db_url, "log": lambda **r: self.logs.append(r)} | kw)
        self.thread.start()

    def events(self, name):
        return [r for r in self.logs if r["event"] == name]

    def shutdown(self, timeout=30):
        self.stop.set()
        self.thread.join(timeout)
        assert not self.thread.is_alive()


def wait_until(check, timeout=120):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_runner_finishes_two_submitted_runs(db, db_url, factory, target, remote, fake_pi):
    fake_pi(script(
        implementer=[{"text": '{"summary": "a"}', "write": {"a.py": "a\n"}},
                     {"text": '{"summary": "b"}', "write": {"b.py": "b\n"}}],
        documenter=[{"text": '{"summary": "d"}', "write": {"docs/a.md": "a\n"}},
                    {"text": '{"summary": "d"}', "write": {"docs/b.md": "b\n"}}]))
    runs = [submit_run(db, factory, target, f"build {n}").run_id for n in (1, 2)]
    server = Serve(factory, db_url, concurrency=2)
    try:
        wait_until(lambda: all(store.load_state(db, r).status != "running" for r in runs))
    finally:
        server.shutdown()
    assert [store.load_state(db, r).status for r in runs] == ["done", "done"]
    files = sh(remote, "git", "ls-tree", "-r", "--name-only", "main").split()
    assert {"a.py", "b.py", "docs/a.md", "docs/b.md"} <= set(files)
    assert len(server.events("job_claimed")) == len(server.events("job_completed")) == 16  # 8 stages x 2 runs
    assert not server.events("job_error") and not server.events("worker_crashed")


def test_heartbeat_keeps_a_long_jobs_lease(db, db_url, factory, target, monkeypatch):
    run_id = submit_run(db, factory, target, "x").run_id

    def long_job(conn, factory, job, display=None):
        other = store.connect(db_url)
        try:
            for _ in range(6):  # 3s with a 1s lease: only the heartbeat keeps it from being reaped
                time.sleep(0.5)
                assert queue.reap_expired(other) == []
        finally:
            other.close()
        queue.complete(conn, job["id"], "ok")
        return Finished("done", "ok")

    monkeypatch.setattr(workflow, "run_job", long_job)
    server = Serve(factory, db_url, lease_seconds=1, heartbeat_seconds=0.2)
    try:
        wait_until(lambda: server.events("job_completed") or server.events("job_error"), timeout=30)
    finally:
        server.shutdown()
    assert not server.events("job_error")
    assert [(j["status"]) for j in jobs(db, run_id)] == ["done"]
    assert db.execute("SELECT infra_retries FROM stage_jobs").fetchone()["infra_retries"] == 0


def test_shutdown_lets_the_in_flight_job_finish_and_claims_no_more(db, db_url, factory, target, monkeypatch):
    first, second = (submit_run(db, factory, target, "x").run_id for _ in range(2))
    started, release = threading.Event(), threading.Event()

    def blocking_job(conn, factory, job, display=None):
        started.set()
        release.wait(30)
        queue.complete(conn, job["id"], "ok")
        return Finished("done", "ok")

    monkeypatch.setattr(workflow, "run_job", blocking_job)
    server = Serve(factory, db_url)
    assert started.wait(30)
    server.stop.set()
    time.sleep(0.5)
    assert server.thread.is_alive()  # waiting for the in-flight job
    release.set()
    server.shutdown()
    assert [j["status"] for j in jobs(db, first)] == ["done"]
    assert [j["status"] for j in jobs(db, second)] == ["queued"]
    assert server.events("runner_stopped")


def test_resume_requeues_a_parked_queued_run(env, factory, target):
    state = submit_run(env, factory, target, "x")
    complete_stage(env, claim(env), state, StageResult("failed", "failed: boom"))
    assert store.load_state(env, state.run_id).status == "parked"
    result = invoke("resume", state.run_id)
    assert result.exit_code == 0, result.output
    assert "queued branch" in result.output
    assert [(j["stage"], j["status"]) for j in jobs(env, state.run_id)] == [("branch", "failed"), ("branch", "queued")]
    row = env.execute("SELECT status, state->>'status' AS s FROM runs").fetchone()
    assert row == {"status": "running", "s": "running"}
    assert store.events(env, state.run_id)[-1]["type"] == "run_resumed"


def test_resume_rejects_a_running_queued_run(env, factory, target):
    state = submit_run(env, factory, target, "x")
    result = invoke("resume", state.run_id)
    assert result.exit_code == 1 and "not parked" in result.output
    assert len(jobs(env, state.run_id)) == 1


def test_dotenv_fills_unset_variables_without_overriding(monkeypatch, factory):
    monkeypatch.chdir(factory)
    (factory / ".env").write_text("# comment\nexport SF_TEST_A='from-file'\nSF_TEST_B=from-file\n")
    monkeypatch.delenv("SF_TEST_A", raising=False)
    monkeypatch.setenv("SF_TEST_B", "from-shell")
    invoke("submit", "--help")
    assert os.environ["SF_TEST_A"] == "from-file"
    assert os.environ["SF_TEST_B"] == "from-shell"
    monkeypatch.delenv("SF_TEST_A")
