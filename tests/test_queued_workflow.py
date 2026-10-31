"""The queued driver: `complete_stage` / `run_job` over the Postgres queue with the shared stage graph."""

import threading
import time

import pytest
from conftest import calls, sh
from test_harness_workflow import configure
from test_workflow import APPROVE, CHANGES, script

from sf import queue, store
from sf.run import RunState
from sf.workflow import StageResult, complete_stage, run_job, start_run

ALL_CAPS = ["harness:pi", "harness:claude", "harness:codex", "model:pi:fake/model"]


def create(db, factory, target, **over) -> RunState:
    run_id = store.new_run_id(db)
    state = RunState(run_id=run_id, target=str(target), trunk="main", branch=f"sf/{run_id}", **over)
    store.create_run(db, state, store.snapshot_config(factory, target))
    store.put_document(db, run_id, "prompt.md", "build an app\n")
    return state


def jobs(db, run_id):
    return db.execute("SELECT id, stage, status, inputs, required_caps FROM stage_jobs WHERE run_id = %s"
                      " ORDER BY id", (run_id,)).fetchall()


def claim(db):
    queue.register_runner(db, "w", "host", ALL_CAPS)
    return queue.claim(db, "w", ALL_CAPS)


def test_complete_stage_enqueues_next_job_with_harness_caps(db, factory, target, fake_pi, fake_harness):
    configure(factory, fake_pi, fake_harness, script())  # implementer on claude-code, reviewer on codex
    state = create(db, factory, target)
    start_run(db, state)
    assert [(j["stage"], j["required_caps"]) for j in jobs(db, state.run_id)] == [("branch", [])]
    complete_stage(db, claim(db), state, StageResult("ok", "on branch"))
    complete_stage(db, claim(db), state, StageResult("ok", "prompt saved"))
    assert jobs(db, state.run_id)[-1]["required_caps"] == ["harness:pi", "model:pi:fake/model"]  # planner
    complete_stage(db, claim(db), state, StageResult("ok", "plan written"))
    last = jobs(db, state.run_id)[-1]
    assert (last["stage"], last["inputs"], last["required_caps"]) == (
        "implement", {"loop": 1, "feedback": ""}, ["harness:claude"])
    saved = store.load_state(db, state.run_id)
    assert saved.completed == ["branch", "prompt", "plan"] and saved.pending["stage"] == "implement"
    result = db.execute("SELECT outcome FROM stage_results ORDER BY job_id DESC LIMIT 1").fetchone()
    assert result["outcome"] == "ok"


def test_complete_stage_is_atomic(db, factory, target, monkeypatch):
    state = create(db, factory, target)
    start_run(db, state)
    job = claim(db)

    def broken(*args, **kwargs):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(queue, "enqueue", broken)
    with pytest.raises(RuntimeError):
        complete_stage(db, job, state, StageResult("ok", "on branch"))
    assert [j["status"] for j in jobs(db, state.run_id)] == ["claimed"]
    assert db.execute("SELECT count(*) AS n FROM stage_results").fetchone()["n"] == 0
    assert store.load_state(db, state.run_id).completed == []


def test_agent_failure_parks_the_run(db, factory, target):
    state = create(db, factory, target)
    start_run(db, state)
    job = claim(db)
    complete_stage(db, job, state, StageResult("failed", "failed: planner: exited 1"))
    assert [j["status"] for j in jobs(db, state.run_id)] == ["failed"]
    row = db.execute("SELECT status, state->>'status' AS json_status FROM runs WHERE id = %s",
                     (state.run_id,)).fetchone()
    assert row == {"status": "parked", "json_status": "parked"}
    assert store.events(db, state.run_id)[-1]["type"] == "run_parked"


def test_park_run_keeps_column_and_state_json_consistent(db, factory, target):
    state = create(db, factory, target)
    queue.park_run(db, state.run_id)
    assert store.load_state(db, state.run_id).status == "parked"
    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "parked"


def test_plan_approval_fails_queued_run_clearly(db, factory, target):
    state = create(db, factory, target, approve_plan=True)
    start_run(db, state)
    assert jobs(db, state.run_id) == []
    saved = store.load_state(db, state.run_id)
    assert saved.status == "failed"
    assert "not supported for queued runs" in store.events(db, state.run_id)[-1]["reason"]


def drain(db, factory) -> list[str]:
    """One in-process worker: claim and run jobs until the queue is empty."""
    stages = []
    while (job := claim(db)) is not None:
        stages.append(job["stage"])
        run_job(db, factory, job)
    return stages


def worker(db_url, factory, runner_id, run_id, errors):
    """A worker loop on its own connection: claim and run jobs until the run stops running."""
    conn = store.connect(db_url)
    try:
        queue.register_runner(conn, runner_id, "host", ALL_CAPS)
        while store.load_state(conn, run_id).status == "running":
            if (job := queue.claim(conn, runner_id, ALL_CAPS)) is None:
                time.sleep(0.05)
                continue
            run_job(conn, factory, job)
    except Exception as e:  # noqa: BLE001 - a thread must hand its failure to the test
        errors.append(e)
    finally:
        conn.close()


def test_whole_run_through_the_queue(db, db_url, factory, target, remote, fake_pi, fake_harness):
    """Two worker threads, each job in its own clone of a bare remote; the merge lands on the remote."""
    (target / ".sf.toml").write_text('validation = ["test -f ok"]\n')
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "validation config")
    sh(target, "git", "push", "-q", "origin", "main")
    steps = script(implementer=[
        {"text": '{"summary":"first"}', "write": {"app.py": "x"}},
        {"text": '{"summary":"second"}', "write": {"ok": "ok"}},
        {"text": '{"summary":"third"}', "write": {"app.py": "fixed"}},
    ], reviewer=[CHANGES, APPROVE])
    native = configure(factory, fake_pi, fake_harness, steps)
    state = create(db, factory, target)
    start_run(db, state)
    errors: list[Exception] = []
    threads = [threading.Thread(target=worker, args=(db_url, factory, f"w{i}", state.run_id, errors)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors
    assert [j["stage"] for j in jobs(db, state.run_id)] == [
        "branch", "prompt", "plan", "implement", "implement", "review", "implement", "review", "document",
        "merge", "improve"]
    claimed_by = {r["claimed_by"] for r in db.execute("SELECT claimed_by FROM stage_jobs").fetchall()}
    assert claimed_by <= {"w1", "w2"}
    final = store.load_state(db, state.run_id)
    assert final.status == "done" and final.loop == 3 and final.pending is None
    assert final.completed == ["branch", "prompt", "plan", "implement", "document", "merge", "improve"]
    assert final.results["implement"] == "approved in loop 3" and final.results["validation-1"] == "failed"
    assert {i.harness for i in final.invocations} == {"pi", "claude-code", "codex"}
    implementers = [c for c in calls(native) if c["agent"] == "implementer"]
    assert "Latest validation output (failed)" in implementers[1]["message"]
    assert "bad name" in implementers[2]["message"]
    assert sh(remote, "git", "show", "main:app.py") == "fixed"
    assert "docs/app.md" in sh(remote, "git", "ls-tree", "-r", "--name-only", "main")
    assert sh(remote, "git", "rev-parse", "main") == sh(remote, "git", "rev-parse", state.branch)
    shas = db.execute("SELECT j.stage, r.branch_sha FROM stage_results r JOIN stage_jobs j ON j.id = r.job_id"
                      " ORDER BY j.id").fetchall()
    assert all(r["branch_sha"] for r in shas)  # every stage, even branch, leaves sf/<run-id> on the remote
    assert {j["status"] for j in jobs(db, state.run_id)} == {"done"}
    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "done"
    assert store.events(db, state.run_id)[-1]["type"] == "run_done"


def test_queued_agent_failure_parks_mid_run(db, factory, target, fake_pi):
    fake_pi(script(documenter=[{"exit": 1}]))
    state = create(db, factory, target)
    start_run(db, state)
    assert drain(db, factory)[-1] == "document"
    final = store.load_state(db, state.run_id)
    assert final.status == "parked" and final.stage == "document"
    assert final.results["document"].startswith("failed:")
