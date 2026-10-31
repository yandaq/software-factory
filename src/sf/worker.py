"""`sf runner`: register with the queue, heartbeat its leases, and claim and run stage jobs.

Each worker thread has its own Postgres connection (LISTEN and advisory locks are per session). A
separate heartbeat thread renews every held lease. Setting `stop` stops claiming; jobs already running
finish and are completed before `serve` returns. Progress is one JSON object per line via `log`.
"""

import json
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sf import artifacts, queue, store, workflow
from sf.run import now

HARNESS_BINARIES = {"pi": "SF_PI", "claude": "SF_CLAUDE", "codex": "SF_CODEX"}


def detect_caps() -> list[str]:
    """`harness:<x>` for each harness CLI configured via SF_<X> or found on PATH."""
    return [f"harness:{name}" for name, env in HARNESS_BINARIES.items() if os.environ.get(env) or shutil.which(name)]


def missing_env() -> list[str]:
    """Settings a runner needs that are unset: the database and the artifact store."""
    return [name for name in ("SF_DATABASE_URL", *artifacts.ENV) if not os.environ.get(name)]


def json_log(**record: Any) -> None:
    print(json.dumps({"ts": now(), **record}, default=str), flush=True)


def serve(factory: Path, runner_id: str, host: str, caps: list[str], concurrency: int, stop: threading.Event, *,
          db_url: str | None = None, lease_seconds: float = 60, heartbeat_seconds: float = 15,
          poll_seconds: float = 5, log: Callable[..., None] = json_log) -> None:
    """Run `concurrency` workers until `stop` is set and every in-flight job has completed."""
    conn = store.connect(db_url)
    try:
        store.migrate(conn)
        queue.register_runner(conn, runner_id, host, caps, concurrency)
        log(event="runner_started", runner=runner_id, caps=caps, concurrency=concurrency)
        workers_done = threading.Event()
        beat = threading.Thread(target=_heartbeat, args=(conn, runner_id, lease_seconds, heartbeat_seconds,
                                                         workers_done, log), daemon=True)
        beat.start()
        args = (factory, runner_id, caps, stop, db_url, lease_seconds, poll_seconds, log)
        threads = [threading.Thread(target=_work, args=args, name=f"{runner_id}-{n}", daemon=True)
                   for n in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.2)  # short joins keep the main thread responsive to signals
        workers_done.set()
        beat.join()
        log(event="runner_stopped", runner=runner_id)
    finally:
        conn.close()


def _heartbeat(conn: Any, runner_id: str, lease_seconds: float, interval: float, done: threading.Event,
               log: Callable[..., None]) -> None:
    while not done.wait(interval):
        try:
            queue.heartbeat(conn, runner_id, lease_seconds)
        except Exception as e:  # noqa: BLE001 - keep beating; leases expire if the database stays away
            log(event="heartbeat_failed", runner=runner_id, error=str(e))


def _work(factory: Path, runner_id: str, caps: list[str], stop: threading.Event, db_url: str | None,
          lease_seconds: float, poll_seconds: float, log: Callable[..., None]) -> None:
    conn = store.connect(db_url)
    try:
        while not stop.is_set():
            queue.listen(conn)  # before the claim, so a job enqueued in between still wakes us
            for reaped in queue.reap_expired(conn):
                log(event="job_reaped", runner=runner_id, **reaped)
            job = queue.claim(conn, runner_id, caps, lease_seconds)
            if job is None:
                queue.wait_for_jobs(conn, poll_seconds)
                continue
            _run(conn, factory, runner_id, job, log)
    except Exception as e:
        log(event="worker_crashed", runner=runner_id, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        conn.close()


def _run(conn: Any, factory: Path, runner_id: str, job: dict[str, Any], log: Callable[..., None]) -> None:
    ids = {"runner": runner_id, "job_id": job["id"], "run_id": job["run_id"], "stage": job["stage"]}
    log(event="job_claimed", **ids)
    try:
        step = workflow.run_job(conn, factory, job)
    except Exception as e:  # noqa: BLE001 - an unexpected error must not leave the job leased forever
        error = f"{type(e).__name__}: {e}"
        log(event="job_error", **ids, error=error)
        queue.report_infra_failure(conn, job["id"], error)
        return
    if step is None:
        log(event="job_requeued", **ids)
    else:
        log(event="job_completed", **ids, next=type(step).__name__,
            next_stage=getattr(step, "stage", None), status=getattr(step, "status", None))
