"""Artifact store and the queued path's independence from local run state (S3 via moto)."""

import shutil
import urllib.request

from conftest import calls, sh
from test_queued_workflow import ALL_CAPS, create, jobs
from test_workflow import script

from sf import artifacts, queue, store
from sf.workflow import run_job, start_run


def test_upload_layout_list_download_and_presign(tmp_path, s3):
    src = tmp_path / "run"
    (src / "sessions").mkdir(parents=True)
    (src / "pi-plan-1.jsonl").write_text("{}\n")
    (src / "sessions" / "s.jsonl").write_text("session\n")
    prefix = artifacts.job_prefix("20261031-120000", "plan", 7)
    assert prefix == "runs/20261031-120000/plan/7/"
    keys = artifacts.upload_dir(src, prefix)
    assert keys == [prefix + "pi-plan-1.jsonl", prefix + "sessions/s.jsonl"]
    assert sorted(artifacts.list_keys(prefix)) == keys
    out = tmp_path / "out"
    artifacts.download_dir(prefix, out)
    assert (out / "sessions" / "s.jsonl").read_text() == "session\n"
    with urllib.request.urlopen(artifacts.presigned_url(keys[0])) as response:
        assert response.read() == b"{}\n"


def test_upload_failure_requeues_job_as_infra_failure(db, factory, target, remote, fake_pi, monkeypatch):
    fake_pi(script())
    state = create(db, factory, target)
    start_run(db, state)
    queue.register_runner(db, "w", "host", ALL_CAPS)
    for _ in ("branch", "prompt"):
        run_job(db, factory, queue.claim(db, "w", ALL_CAPS))
    before = sh(remote, "git", "rev-parse", state.branch)
    monkeypatch.setenv("SF_S3_BUCKET", "no-such-bucket")
    assert run_job(db, factory, queue.claim(db, "w", ALL_CAPS)) is None
    job = db.execute("SELECT stage, status, infra_retries FROM stage_jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert job == {"stage": "plan", "status": "queued", "infra_retries": 1}
    assert "no-such-bucket" in store.events(db, state.run_id)[-1]["reason"]
    assert sh(remote, "git", "rev-parse", state.branch) == before  # the plan commit is not pushed


def drain_without_local_state(db, factory, tmp_path):
    """Run every job, deleting runs/ and moving the factory checkout between stages."""
    n = 0
    queue.register_runner(db, "w", "host", ALL_CAPS)
    while (job := queue.claim(db, "w", ALL_CAPS)) is not None:
        shutil.rmtree(factory / "runs", ignore_errors=True)
        moved = tmp_path / f"factory-{n}"
        shutil.move(factory, moved)
        factory, n = moved, n + 1
        run_job(db, factory, job)
    return factory


def test_queued_run_needs_no_local_state_between_stages(db, factory, target, remote, fake_pi, s3, tmp_path):
    native = fake_pi(script())
    state = create(db, factory, target)
    start_run(db, state)
    factory = drain_without_local_state(db, factory, tmp_path)
    final = store.load_state(db, state.run_id)
    assert final.status == "done", final.results
    assert not (factory / "runs" / state.run_id).exists()  # outputs went to per-job temp dirs
    assert store.get_document(db, state.run_id, "plan.md").startswith("# Plan")
    assert store.get_document(db, state.run_id, "review-1.md").startswith("# Review 1")
    implementer = next(c for c in calls(native) if c["agent"] == "implementer")
    assert "write app.py" in implementer["message"]  # plan read from the store
    rows = db.execute("SELECT j.id, j.stage, r.artifact_prefix, r.usage FROM stage_results r"
                      " JOIN stage_jobs j ON j.id = r.job_id ORDER BY j.id").fetchall()
    assert all(r["artifact_prefix"] == f"runs/{state.run_id}/{r['stage']}/{r['id']}/" for r in rows)
    plan = next(r for r in rows if r["stage"] == "plan")
    assert plan["usage"]["input"] > 0
    client, bucket = s3
    keys = [o["Key"] for o in client.list_objects_v2(Bucket=bucket, Prefix=plan["artifact_prefix"])["Contents"]]
    names = {k.removeprefix(plan["artifact_prefix"]) for k in keys}
    assert {"plan.md", "pi-plan-1.jsonl", "pi-plan-1.stderr"} <= names
    assert any(n.startswith("prompts/") for n in names)


def test_failed_stage_artifacts_are_uploaded(db, factory, target, remote, fake_pi, s3):
    fake_pi(script(planner=[{"exit": 1}]))
    state = create(db, factory, target)
    start_run(db, state)
    queue.register_runner(db, "w", "host", ALL_CAPS)
    while (job := queue.claim(db, "w", ALL_CAPS)) is not None:
        run_job(db, factory, job)
    assert store.load_state(db, state.run_id).status == "parked"
    row = db.execute("SELECT r.artifact_prefix FROM stage_results r JOIN stage_jobs j ON j.id = r.job_id"
                     " WHERE j.stage = 'plan'").fetchone()
    client, bucket = s3
    keys = [o["Key"] for o in client.list_objects_v2(Bucket=bucket, Prefix=row["artifact_prefix"])["Contents"]]
    assert row["artifact_prefix"] + "pi-plan-1.stderr" in keys


def test_snapshot_prompts_used_even_if_agents_change_mid_run(db, factory, target, remote, fake_pi):
    native = fake_pi(script())
    state = create(db, factory, target)
    start_run(db, state)
    template = factory / "agents" / "planner.user.md"
    template.write_text("CHANGED MID-RUN {{request}} {{documentation}}\n")
    (factory / "agents" / "implementer.md").write_text("not an agent definition\n")
    queue.register_runner(db, "w", "host", ALL_CAPS)
    while (job := queue.claim(db, "w", ALL_CAPS)) is not None:
        run_job(db, factory, job)
    assert store.load_state(db, state.run_id).status == "done"
    assert not any("CHANGED MID-RUN" in c["message"] for c in calls(native))
    assert [j["status"] for j in jobs(db, state.run_id)] == ["done"] * len(jobs(db, state.run_id))
