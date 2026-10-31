"""Stage job queue on Postgres: runner registration, claim with lease, completion, infra requeue.

All lease times use the database clock (`now()`), so runners with skewed clocks agree. Functions that
write take the caller's connection and do not open their own transaction unless noted, so callers can
combine them (e.g. record a result and enqueue the next stage atomically).
"""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from sf import store
from sf.run import now

CHANNEL = "sf_jobs"
MAX_INFRA_RETRIES = 2
STALE_AFTER_SECONDS = 120  # several missed heartbeats (runners beat every 15 s by default)


def register_runner(conn: psycopg.Connection, runner_id: str, host: str, caps: list[str],
                    concurrency: int = 1) -> None:
    conn.execute(
        "INSERT INTO runners (id, host, caps, concurrency, last_heartbeat) VALUES (%s, %s, %s, %s, now())"
        " ON CONFLICT (id) DO UPDATE SET host = EXCLUDED.host, caps = EXCLUDED.caps,"
        " concurrency = EXCLUDED.concurrency, last_heartbeat = now()",
        (runner_id, host, caps, concurrency),
    )


def heartbeat(conn: psycopg.Connection, runner_id: str, lease_seconds: float = 60) -> None:
    """Record liveness and extend the lease on every job this runner holds."""
    with conn.transaction():
        cur = conn.execute("UPDATE runners SET last_heartbeat = now() WHERE id = %s", (runner_id,))
        if cur.rowcount != 1:
            raise LookupError(f"runner {runner_id} is not registered")
        conn.execute(
            "UPDATE stage_jobs SET lease_until = now() + make_interval(secs => %s)"
            " WHERE claimed_by = %s AND status = 'claimed'",
            (lease_seconds, runner_id),
        )


def list_runners(conn: psycopg.Connection, stale_after: float = STALE_AFTER_SECONDS) -> list[dict[str, Any]]:
    """Every registered runner with `heartbeat_age` (seconds, database clock), `stale` and `held` jobs.

    A runner is stale when its last heartbeat is older than `stale_after`; stale runners stay listed.
    """
    return conn.execute(
        "SELECT r.id, r.host, r.caps, r.concurrency, r.last_heartbeat,"
        " extract(epoch FROM now() - r.last_heartbeat)::float AS heartbeat_age,"
        " (r.last_heartbeat IS NULL OR r.last_heartbeat < now() - make_interval(secs => %s)) AS stale,"
        " (SELECT count(*) FROM stage_jobs j WHERE j.claimed_by = r.id AND j.status = 'claimed')::int AS held"
        " FROM runners r ORDER BY r.id",
        (stale_after,),
    ).fetchall()


def queue_depth(conn: psycopg.Connection) -> int:
    """Number of jobs waiting to be claimed."""
    return conn.execute("SELECT count(*) AS n FROM stage_jobs WHERE status = 'queued'").fetchone()["n"]


def enqueue(conn: psycopg.Connection, run_id: str, stage: str, attempt: int = 1,
            inputs: dict[str, Any] | None = None, required_caps: list[str] | None = None) -> int:
    """Insert a queued job and notify waiters (the NOTIFY is delivered only if the caller commits)."""
    row = conn.execute(
        "INSERT INTO stage_jobs (run_id, stage, attempt, inputs, required_caps) VALUES (%s, %s, %s, %s, %s)"
        " RETURNING id",
        (run_id, stage, attempt, Jsonb(inputs or {}), required_caps or []),
    ).fetchone()
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(row["id"])))
    return row["id"]


def claim(conn: psycopg.Connection, runner_id: str, caps: list[str],
          lease_seconds: float = 60) -> dict[str, Any] | None:
    """Claim the oldest queued job this runner can run, within its concurrency limit; None if none."""
    with conn.transaction():
        # Lock the runner row so this runner's concurrent claims count its held jobs consistently.
        runner = conn.execute("SELECT concurrency FROM runners WHERE id = %s FOR UPDATE", (runner_id,)).fetchone()
        if runner is None:
            raise LookupError(f"runner {runner_id} is not registered")
        held = conn.execute(
            "SELECT count(*) AS n FROM stage_jobs WHERE claimed_by = %s AND status = 'claimed'", (runner_id,)
        ).fetchone()["n"]
        if held >= runner["concurrency"]:
            return None
        return conn.execute(
            "UPDATE stage_jobs SET status = 'claimed', claimed_by = %s,"
            " lease_until = now() + make_interval(secs => %s)"
            " WHERE id = (SELECT id FROM stage_jobs WHERE status = 'queued' AND required_caps <@ %s::text[]"
            "             ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)"
            " RETURNING *",
            (runner_id, lease_seconds, caps),
        ).fetchone()


def complete(conn: psycopg.Connection, job_id: int, outcome: str, *, failed: bool = False,
             contract: dict[str, Any] | None = None, usage: dict[str, Any] | None = None,
             branch_sha: str | None = None, artifact_prefix: str | None = None) -> None:
    """Record the stage result and mark the job done (or failed), in the caller's transaction."""
    cur = conn.execute(
        "UPDATE stage_jobs SET status = %s, lease_until = NULL WHERE id = %s AND status = 'claimed'",
        ("failed" if failed else "done", job_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"job {job_id} is not claimed; cannot complete it")
    conn.execute(
        "INSERT INTO stage_results (job_id, outcome, contract, usage, branch_sha, artifact_prefix)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (job_id, outcome, Jsonb(contract) if contract is not None else None,
         Jsonb(usage) if usage is not None else None, branch_sha, artifact_prefix),
    )


def park_run(conn: psycopg.Connection, run_id: str) -> None:
    """Mark a run parked in both the `status` column and its RunState JSON, so readers agree."""
    conn.execute(
        "UPDATE runs SET status = 'parked', state = jsonb_set(state, '{status}', '\"parked\"'),"
        " updated_at = now() WHERE id = %s",
        (run_id,),
    )


def report_infra_failure(conn: psycopg.Connection, job_id: int, reason: str,
                         max_infra_retries: int = MAX_INFRA_RETRIES) -> str:
    """A runner hit an infra error (push/fetch, ...): requeue or fail+park. Returns the new status."""
    with conn.transaction():
        job = conn.execute("SELECT * FROM stage_jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if job is None:
            raise LookupError(f"no job {job_id}")
        return _retry_or_fail(conn, job, reason, max_infra_retries)


def reap_expired(conn: psycopg.Connection, max_infra_retries: int = MAX_INFRA_RETRIES) -> list[dict[str, Any]]:
    """Requeue or fail+park every claimed job whose lease has expired; returns [{job_id, status}]."""
    with conn.transaction():
        jobs = conn.execute(
            "SELECT * FROM stage_jobs WHERE status = 'claimed' AND lease_until < now()"
            " ORDER BY id FOR UPDATE SKIP LOCKED"
        ).fetchall()
        return [{"job_id": j["id"], "status": _retry_or_fail(conn, j, "lease expired", max_infra_retries)}
                for j in jobs]


def _retry_or_fail(conn: psycopg.Connection, job: dict[str, Any], reason: str, max_infra_retries: int) -> str:
    if job["infra_retries"] < max_infra_retries:
        conn.execute(
            "UPDATE stage_jobs SET status = 'queued', infra_retries = infra_retries + 1,"
            " claimed_by = NULL, lease_until = NULL WHERE id = %s",
            (job["id"],),
        )
        conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(job["id"])))
        status, event_type = "queued", "job_requeued"
    else:
        conn.execute("UPDATE stage_jobs SET status = 'failed', lease_until = NULL WHERE id = %s", (job["id"],))
        park_run(conn, job["run_id"])
        status, event_type = "failed", "job_failed_infra"
    store.append_event(conn, {
        "ts": now(), "run_id": job["run_id"], "stage": job["stage"], "source": "queue", "type": event_type,
        "job_id": job["id"], "reason": reason, "claimed_by": job["claimed_by"],
        "infra_retries": job["infra_retries"] + (status == "queued"),
    }, job_id=job["id"])
    return status


def listen(conn: psycopg.Connection) -> None:
    """Subscribe this (autocommit) connection to job notifications; call before the first claim."""
    conn.execute(f"LISTEN {CHANNEL}")


def wait_for_jobs(conn: psycopg.Connection, timeout: float) -> bool:
    """Block until a job notification arrives or `timeout` elapses; True if notified.

    Subscribes on first use; for no lost wake-ups, call `listen` before the claim that found nothing.
    """
    listen(conn)
    return any(True for _ in conn.notifies(timeout=timeout, stop_after=1))
