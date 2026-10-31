"""Queued improve jobs, improvement records in Postgres, applying to the factory remote, and the DB dashboard."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import calls, sh
from test_artifacts import drain_without_local_state
from test_queued_workflow import ALL_CAPS, create
from test_workflow import IDEA, script

from sf import improve, queue, store
from sf.apply import start_queued_application
from sf.dashboard import serve
from sf.workflow import StageResult, complete_stage, start_run, submit_run

RECORD = {"ts": "2026-01-01", "run_id": "source-run", "title": "Improve factory", "file": "agents/implementer.md",
          "rationale": "Make it work", "evidence": "Observed failure", "proposed_change": "Document the fix",
          "decision": "automatic", "note": ""}


@pytest.fixture
def factory_remote(tmp_path, factory):
    """A bare repository acting as the factory's `origin`."""
    bare = tmp_path / "factory.git"
    sh(tmp_path, "git", "clone", "-q", "--bare", str(factory), str(bare))
    sh(factory, "git", "remote", "add", "origin", str(bare))
    return bare


def test_improve_job_runs_without_local_state_and_records_in_postgres(db, factory, target, remote, fake_pi,
                                                                     tmp_path, monkeypatch):
    native = fake_pi(script(improver=[{"text": json.dumps({"ideas": [IDEA]})}]))
    state = create(db, factory, target)
    start_run(db, state)
    seen = {}
    original = improve._queued_inputs

    def spy(wf):
        evidence, factory_tree = original(wf)
        seen["evidence"] = sorted(p.relative_to(evidence).as_posix() for p in evidence.rglob("*") if p.is_file())
        seen["workspace"], seen["factory_tree"] = wf.workspace, factory_tree
        return evidence, factory_tree

    monkeypatch.setattr(improve, "_queued_inputs", spy)
    head, branch = sh(target, "git", "rev-parse", "HEAD"), sh(target, "git", "branch", "--show-current")
    factory = drain_without_local_state(db, factory, tmp_path)
    final = store.load_state(db, state.run_id)
    assert final.status == "done" and final.results["improve"] == "1 improvements recorded"
    # The submitter's target checkout is never touched; the improver inspected the job clone.
    assert (sh(target, "git", "rev-parse", "HEAD"), sh(target, "git", "branch", "--show-current")) == (head, branch)
    improver = next(c for c in calls(native) if c["agent"] == "improver")
    assert str(seen["workspace"]) in improver["message"] and str(target) not in improver["message"]
    assert improver["cwd"] == str(seen["factory_tree"])
    for name in ("prompt.md", "plan.md", "review-1.md", "events.jsonl", "run-state.json"):
        assert name in seen["evidence"]
    assert any(p.startswith("artifacts/plan-") and p.endswith("pi-plan-1.jsonl") for p in seen["evidence"])
    records = store.improvements(db)
    assert [(r["title"], r["run_id"], r["decision"], r["note"]) for r in records] == [
        (IDEA["title"], state.run_id, "automatic", "")]
    assert set(records[0]) == {"ts", "run_id", "title", "file", "rationale", "evidence", "proposed_change",
                               "decision", "note"}
    assert not (factory / "improvements.jsonl").exists()


def wait_applied(db, title, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = next(r for r in store.improvements(db) if r["title"] == title)
        if (record.get("application") or {}).get("status") in ("applied", "failed"):
            return record["application"]
        time.sleep(0.05)
    pytest.fail(f"application of {title!r} did not finish")


def test_apply_pushes_to_factory_remote_and_reaches_runs_after_pull(db, db_url, factory, factory_remote, target,
                                                                    fake_pi, s3):
    template = "APPLIED {{request}} {{documentation}}\n"
    fake_pi({"implementer": [{"text": '{"summary":"Delivered"}', "write": {"agents/planner.user.md": template}}]})
    store.record_improvement(db, RECORD)
    local_head = sh(factory, "git", "rev-parse", "HEAD")
    run_id = start_queued_application(db_url, factory, (RECORD["ts"], RECORD["run_id"], RECORD["title"]))
    application = wait_applied(db, RECORD["title"])
    assert application["status"] == "applied", application
    assert application["run_id"] == run_id and application["summary"] == "Delivered"
    assert application["commit"] == sh(factory_remote, "git", "rev-parse", "main").strip()
    assert sh(factory_remote, "git", "show", "main:agents/planner.user.md") == template
    # The dashboard's own checkout is untouched.
    assert sh(factory, "git", "rev-parse", "HEAD") == local_head and not sh(factory, "git", "status", "--porcelain")
    client, bucket = s3
    keys = [o["Key"] for o in client.list_objects_v2(Bucket=bucket, Prefix=application["artifact_prefix"])["Contents"]]
    assert application["artifact_prefix"] + "prompt.md" in keys
    # Runs snapshot the submitter's checkout, so the application reaches them once it is pulled.
    assert store.load_config(db, submit_run(db, factory, target, "before pull").run_id)["agents"][
        "planner.user.md"] != template
    sh(factory, "git", "pull", "-q", "origin", "main")
    config = store.load_config(db, submit_run(db, factory, target, "next run").run_id)
    assert config["agents"]["planner.user.md"] == template
    assert config["factory_sha"] == application["commit"] and config["factory_remote"] == str(factory_remote)


def test_concurrent_applies_serialise_on_the_factory_remote(db, db_url, factory, factory_remote, monkeypatch):
    spans = []

    def agent(run, definition, task, cwd, *args):
        start = time.monotonic()
        time.sleep(0.3)
        (cwd / f"{run.state.run_id}.md").write_text("change\n")
        spans.append((start, time.monotonic()))
        return {"summary": "Done"}

    monkeypatch.setattr("sf.apply.run_agent", agent)
    titles = ["First", "Second"]
    for title in titles:
        store.record_improvement(db, RECORD | {"title": title})
    for title in titles:
        start_queued_application(db_url, factory, (RECORD["ts"], RECORD["run_id"], title))
    results = [wait_applied(db, title) for title in titles]
    assert [r["status"] for r in results] == ["applied", "applied"], results
    (_, first_end), (second_start, _) = sorted(spans)
    assert first_end <= second_start  # the second implementer only started after the first pushed
    log = sh(factory_remote, "git", "log", "--format=%H", "-3", "main").split()
    assert {r["commit"] for r in results} == set(log[:2])


def test_apply_refuses_running_or_missing_records(db, db_url, factory, factory_remote, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr("sf.apply.run_agent", lambda *a: release.wait(5) and {"summary": "ok"})
    store.record_improvement(db, RECORD)
    identity = (RECORD["ts"], RECORD["run_id"], RECORD["title"])
    start_queued_application(db_url, factory, identity)
    from sf.apply import ApplyRefused

    with pytest.raises(ApplyRefused):
        start_queued_application(db_url, factory, identity)
    with pytest.raises(RuntimeError):
        store.delete_improvement(db, identity)
    with pytest.raises(FileNotFoundError):
        start_queued_application(db_url, factory, (RECORD["ts"], RECORD["run_id"], "Gone"))
    release.set()
    assert wait_applied(db, RECORD["title"])["status"] == "applied"
    assert store.delete_improvement(db, identity) and store.improvements(db) == []


# -- dashboard over Postgres ------------------------------------------------------------------
@pytest.fixture
def db_server(db_url, db, factory):
    srv = serve(factory / "runs", "127.0.0.1", 0, factory / "agents", db_url=db_url)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, method="GET", body=None):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, json.loads(body) if e.headers.get_content_type() == "application/json" else body


def test_dashboard_reads_runs_events_runners_and_artifacts_from_postgres(db_server, db, factory, target, s3):
    state = create(db, factory, target)
    start_run(db, state)
    queue.register_runner(db, "live", "host-a", ALL_CAPS)
    queue.register_runner(db, "old", "host-b", ["harness:pi"])
    db.execute("UPDATE runners SET last_heartbeat = now() - interval '10 minutes' WHERE id = 'old'")
    job = queue.claim(db, "live", ALL_CAPS)
    client, bucket = s3
    prefix = f"runs/{state.run_id}/branch/{job['id']}/"
    client.put_object(Bucket=bucket, Key=prefix + "pi-x.jsonl", Body=b"artifact body")
    store.put_document(db, state.run_id, "plan.md", "# Plan\n")
    with db.transaction():
        queue.complete(db, job["id"], "ok", artifact_prefix=prefix)
    status, runs = call(db_server + "/api/runs")
    assert status == 200 and [r["run_id"] for r in runs] == [state.run_id]
    assert set(runs[0]) >= {"run_id", "status", "stage", "loop", "target", "cap_reached", "can_retro", "cost"}
    status, detail = call(db_server + f"/api/runs/{state.run_id}")
    assert status == 200 and detail["state"]["run_id"] == state.run_id
    assert [e["type"] for e in detail["events"]][:1] == ["job_enqueued"]
    assert "plan.md" in detail["artefacts"] and "prompt.md" in detail["artefacts"]
    assert [j["stage"] for j in detail["jobs"]] == ["branch"]
    (artifact,) = detail["artifacts"]
    assert artifact["name"] == "pi-x.jsonl" and artifact["stage"] == "branch"
    with urllib.request.urlopen(artifact["url"]) as r:
        assert r.read() == b"artifact body"
    with urllib.request.urlopen(db_server + f"/api/runs/{state.run_id}/plan.md") as r:
        assert r.read() == b"# Plan\n"
    status, runners = call(db_server + "/api/runners")
    assert status == 200 and runners["queued"] == 0
    by_id = {r["id"]: r for r in runners["runners"]}
    assert by_id["live"]["stale"] is False and by_id["old"]["stale"] is True
    assert by_id["old"]["heartbeat_age"] >= 600 and by_id["live"]["held"] == 0
    store.record_improvement(db, RECORD)
    assert call(db_server + "/api/improvements") == (200, [RECORD])
    assert call(db_server + "/api/runs/nope")[0] == 404


def test_dashboard_resumes_parked_queued_run(db_server, db, factory, target):
    state = create(db, factory, target)
    start_run(db, state)
    queue.register_runner(db, "w", "host", ALL_CAPS)
    complete_stage(db, queue.claim(db, "w", ALL_CAPS), state, StageResult("failed", "failed: boom"))
    status, detail = call(db_server + f"/api/runs/{state.run_id}")
    assert detail["state"]["status"] == "parked" and detail["can_resume"]
    status, body = call(db_server + f"/api/runs/{state.run_id}/resume", "POST")
    assert status == 200 and body["job"]["stage"] == "branch"
    assert store.load_state(db, state.run_id).status == "running"
    assert call(db_server + f"/api/runs/{state.run_id}/resume", "POST")[0] == 409
    assert call(db_server + "/api/runs/nope/resume", "POST")[0] == 404
    assert call(db_server + f"/api/runs/{state.run_id}/retro", "POST")[0] == 409


def test_file_mode_dashboard_reports_no_runners(factory, monkeypatch):
    monkeypatch.delenv("SF_DATABASE_URL", raising=False)
    srv = serve(factory / "runs", "127.0.0.1", 0, factory / "agents")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert call(url + "/api/runners") == (200, {"runners": None, "queued": None})
        assert call(url + "/api/runs") == (200, [])
        assert call(url + "/api/runs/x/resume", "POST")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
