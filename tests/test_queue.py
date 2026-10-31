import threading
import time

import pytest

from sf import queue, store


@pytest.fixture
def run_id(db) -> str:
    db.execute("INSERT INTO runs (id, kind, target, status, current_stage, loop_count, state)"
               " VALUES ('r1', 'requirement', '/t', 'running', 'plan', 0, '{}')")
    return "r1"


def _expire(db, job_id: int) -> None:
    db.execute("UPDATE stage_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))


def test_claim_filters_on_capabilities(db, run_id):
    queue.register_runner(db, "a", "host-a", ["harness:pi"])
    job = queue.enqueue(db, run_id, "plan", required_caps=["harness:claude"])
    assert queue.claim(db, "a", ["harness:pi"]) is None
    queue.register_runner(db, "b", "host-b", ["harness:claude", "toolchain:python"])
    claimed = queue.claim(db, "b", ["harness:claude", "toolchain:python"])
    assert claimed["id"] == job and claimed["claimed_by"] == "b" and claimed["status"] == "claimed"


def test_claim_takes_oldest_first(db, run_id):
    queue.register_runner(db, "a", "h", [], concurrency=2)
    first = queue.enqueue(db, run_id, "plan")
    queue.enqueue(db, run_id, "implement")
    assert queue.claim(db, "a", [])["id"] == first


def test_concurrent_claims_never_share_a_job(db, db_url, run_id):
    jobs = {queue.enqueue(db, run_id, "plan") for _ in range(20)}
    got: dict[str, list[int]] = {}

    def worker(name: str) -> None:
        conn = store.connect(db_url)
        queue.register_runner(conn, name, "h", [], concurrency=100)
        mine = got.setdefault(name, [])
        while (job := queue.claim(conn, name, [])) is not None:
            mine.append(job["id"])
        conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = got["x"] + got["y"]
    assert sorted(claimed) == sorted(jobs)  # every job once, none twice


def test_claim_respects_concurrency_limit(db, run_id):
    queue.register_runner(db, "a", "h", [], concurrency=1)
    first = queue.enqueue(db, run_id, "plan")
    queue.enqueue(db, run_id, "plan")
    assert queue.claim(db, "a", [])["id"] == first
    assert queue.claim(db, "a", []) is None
    queue.complete(db, first, "ok")
    assert queue.claim(db, "a", []) is not None


def test_complete_records_result(db, run_id):
    queue.register_runner(db, "a", "h", [])
    job = queue.enqueue(db, run_id, "plan")
    queue.claim(db, "a", [])
    queue.complete(db, job, "agent_error", failed=True, contract={"k": 1}, branch_sha="abc")
    assert db.execute("SELECT status FROM stage_jobs WHERE id = %s", (job,)).fetchone()["status"] == "failed"
    row = db.execute("SELECT * FROM stage_results WHERE job_id = %s", (job,)).fetchone()
    assert row["outcome"] == "agent_error" and row["contract"] == {"k": 1} and row["branch_sha"] == "abc"
    with pytest.raises(LookupError):
        queue.complete(db, job, "again")


def test_lease_expiry_requeues_twice_then_fails_and_parks(db, run_id):
    queue.register_runner(db, "a", "h", [])
    job = queue.enqueue(db, run_id, "plan")
    for retry in (1, 2):
        assert queue.claim(db, "a", [])["id"] == job
        _expire(db, job)
        assert queue.reap_expired(db) == [{"job_id": job, "status": "queued"}]
        row = db.execute("SELECT * FROM stage_jobs WHERE id = %s", (job,)).fetchone()
        assert (row["status"], row["infra_retries"], row["claimed_by"]) == ("queued", retry, None)
    queue.claim(db, "a", [])
    _expire(db, job)
    assert queue.reap_expired(db) == [{"job_id": job, "status": "failed"}]
    assert db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()["status"] == "parked"
    types = [e["type"] for e in store.events(db, run_id)]
    assert types == ["job_requeued", "job_requeued", "job_failed_infra"]


def test_unexpired_lease_is_not_reaped(db, run_id):
    queue.register_runner(db, "a", "h", [])
    queue.enqueue(db, run_id, "plan")
    queue.claim(db, "a", [])
    assert queue.reap_expired(db) == []


def test_reported_infra_failure_uses_same_retry_rule(db, run_id):
    queue.register_runner(db, "a", "h", [])
    job = queue.enqueue(db, run_id, "plan")
    queue.claim(db, "a", [])
    assert queue.report_infra_failure(db, job, "push failed", max_infra_retries=0) == "failed"
    assert db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()["status"] == "parked"
    assert store.events(db, run_id)[0]["reason"] == "push failed"


def test_heartbeat_extends_lease(db, run_id):
    queue.register_runner(db, "a", "h", [])
    job = queue.enqueue(db, run_id, "plan")
    queue.claim(db, "a", [], lease_seconds=1)
    queue.heartbeat(db, "a", lease_seconds=3600)
    row = db.execute("SELECT lease_until > now() + interval '1 hour' - interval '1 minute' AS ok"
                     " FROM stage_jobs WHERE id = %s", (job,)).fetchone()
    assert row["ok"]
    with pytest.raises(LookupError):
        queue.heartbeat(db, "nobody")


def test_reregistering_runner_requeues_jobs_its_previous_process_held(db, run_id):
    queue.register_runner(db, "a", "h", [])
    job = queue.enqueue(db, run_id, "plan")
    queue.claim(db, "a", [], lease_seconds=3600)
    assert queue.register_runner(db, "a", "h", []) == [{"job_id": job, "status": "queued"}]
    row = db.execute("SELECT status, claimed_by, infra_retries FROM stage_jobs WHERE id = %s", (job,)).fetchone()
    assert row == {"status": "queued", "claimed_by": None, "infra_retries": 1}
    assert queue.register_runner(db, "a", "h", []) == []


def test_notify_wakes_waiter(db, db_url, run_id):
    waiter = store.connect(db_url)
    queue.listen(waiter)
    assert queue.wait_for_jobs(waiter, timeout=0.2) is False
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(queue.wait_for_jobs(waiter, timeout=10)))
    t.start()
    time.sleep(0.2)
    start = time.monotonic()
    queue.enqueue(db, run_id, "plan")
    t.join()
    waiter.close()
    assert result == [True] and time.monotonic() - start < 5


def test_enqueue_rolls_back_with_callers_transaction(db, run_id):
    class Abort(Exception):
        pass

    with pytest.raises(Abort), db.transaction():
        queue.enqueue(db, run_id, "plan")
        raise Abort
    assert db.execute("SELECT count(*) AS n FROM stage_jobs").fetchone()["n"] == 0
