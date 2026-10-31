"""Postgres store: run state, per-run config snapshot, stage jobs/results, event log and runners.

Connections come from `SF_DATABASE_URL`. `migrate()` applies the numbered `MIGRATIONS` that the
database has not yet recorded in `schema_version`, so it is safe to call on every start.
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sf import git as g
from sf.run import RunState
from sf.target import load_config as load_target_config

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE runs (
        id text PRIMARY KEY,
        kind text NOT NULL,
        target text NOT NULL,
        status text NOT NULL,
        current_stage text NOT NULL,
        loop_count integer NOT NULL,
        state jsonb NOT NULL,  -- the full RunState (trunk, branch, completed, results, invocations, ...)
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE run_config (
        run_id text PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        agents jsonb NOT NULL,
        templates jsonb NOT NULL,
        target_config jsonb NOT NULL,
        factory_sha text
    );
    CREATE TABLE stage_jobs (
        id bigserial PRIMARY KEY,
        run_id text NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        stage text NOT NULL,
        attempt integer NOT NULL DEFAULT 1,
        inputs jsonb NOT NULL DEFAULT '{}',
        required_caps text[] NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'queued',
        claimed_by text,
        lease_until timestamptz,
        infra_retries integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX stage_jobs_status ON stage_jobs (status, created_at);
    CREATE TABLE stage_results (
        job_id bigint PRIMARY KEY REFERENCES stage_jobs(id) ON DELETE CASCADE,
        outcome text NOT NULL,
        contract jsonb,
        usage jsonb,
        branch_sha text,
        artifact_prefix text
    );
    CREATE TABLE events (
        id bigserial PRIMARY KEY,
        run_id text NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        job_id bigint REFERENCES stage_jobs(id) ON DELETE SET NULL,
        ts timestamptz NOT NULL,
        type text,
        data jsonb NOT NULL  -- the full event record, as `events.jsonl` held it
    );
    CREATE INDEX events_run ON events (run_id, id);
    CREATE TABLE runners (
        id text PRIMARY KEY,
        host text NOT NULL,
        caps text[] NOT NULL DEFAULT '{}',
        concurrency integer NOT NULL DEFAULT 1,
        last_heartbeat timestamptz
    );
    """,
    # Small text documents later stages read (prompt.md, plan.md, review-N.md), so no stage
    # depends on files a previous stage left on another machine.
    """
    CREATE TABLE run_documents (
        run_id text NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        name text NOT NULL,
        body text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, name)
    );
    """,
    # The improvement log of queued runs (`improvements.jsonl` for local runs): each `record` is one
    # log line's object, in the format of docs/improvements-log.md; `id` keeps the log's order.
    # `factory_remote`: where improve jobs and applications clone the factory repository.
    """
    CREATE TABLE improvements (
        id bigserial PRIMARY KEY,
        record jsonb NOT NULL
    );
    ALTER TABLE run_config ADD COLUMN factory_remote text;
    """,
)


def connect(url: str | None = None) -> psycopg.Connection:
    url = url or os.environ.get("SF_DATABASE_URL")
    if not url:
        raise RuntimeError("SF_DATABASE_URL is not set; it must name the factory's Postgres database")
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def migrate(conn: psycopg.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    with conn.transaction():
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY)")
        conn.execute("LOCK TABLE schema_version IN EXCLUSIVE MODE")  # concurrent starters migrate once
        row = conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_version").fetchone()
        version = row["v"]
        for n, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.execute(sql)
            conn.execute("INSERT INTO schema_version VALUES (%s)", (n,))
    return len(MIGRATIONS)


def new_run_id(conn: psycopg.Connection) -> str:
    """The current second, suffixed `-N` until no run uses it (same format as file-based runs)."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    taken = {r["id"] for r in conn.execute("SELECT id FROM runs WHERE id LIKE %s", (base + "%",))}
    run_id, n = base, 1
    while run_id in taken:
        run_id = f"{base}-{n}"
        n += 1
    return run_id


def snapshot_config(factory: Path, target: Path) -> dict[str, Any]:
    """Agent definitions, task templates, target `.sf.toml` + remote URL and factory commit, frozen for one run.

    When the factory checkout has an `origin`, agents and templates come from a fresh clone of its default
    branch, so a run uses the latest pushed factory state (including applied improvements) rather than the
    submitter's possibly stale or uncommitted copy. Without an `origin` (one machine) the checkout is read.
    """
    remote = g.git(factory, "remote", "get-url", "origin", check=False).strip() or None
    with tempfile.TemporaryDirectory(prefix="sf-factory-") as tmp:
        source = factory
        if remote:
            source = Path(tmp) / "factory"
            g.clone_latest(remote, source)
        return {
            "agents": {p.name: p.read_text() for p in sorted((source / "agents").glob("*.md"))},
            "templates": {p.name: p.read_text() for p in sorted((source / "templates").iterdir()) if p.is_file()},
            # `remote`: where runners on any machine fetch and push this target (see git.target_remote).
            "target_config": load_target_config(target).model_dump() | {"remote": g.target_remote(target)},
            "factory_sha": g.git(source, "rev-parse", "HEAD").strip(),
            "factory_remote": remote,  # None: no origin; runners then use their own factory checkout
        }


def create_run(conn: psycopg.Connection, state: RunState, config: dict[str, Any]) -> None:
    """Insert a run and its config snapshot atomically."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO runs (id, kind, target, status, current_stage, loop_count, state)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (state.run_id, state.kind, state.target, state.status, state.stage, state.loop, _state_json(state)),
        )
        conn.execute(
            "INSERT INTO run_config (run_id, agents, templates, target_config, factory_sha, factory_remote)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (state.run_id, Jsonb(config["agents"]), Jsonb(config["templates"]),
             Jsonb(config["target_config"]), config["factory_sha"], config.get("factory_remote")),
        )


def save_state(conn: psycopg.Connection, state: RunState) -> None:
    cur = conn.execute(
        "UPDATE runs SET status = %s, current_stage = %s, loop_count = %s, state = %s, updated_at = now()"
        " WHERE id = %s",
        (state.status, state.stage, state.loop, _state_json(state), state.run_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"run {state.run_id} is not in the store; create_run it first")


def load_state(conn: psycopg.Connection, run_id: str) -> RunState:
    row = conn.execute("SELECT state FROM runs WHERE id = %s", (run_id,)).fetchone()
    if row is None:
        raise LookupError(f"no run {run_id} in the store")
    return RunState.model_validate(row["state"])


def load_config(conn: psycopg.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT agents, templates, target_config, factory_sha, factory_remote FROM run_config WHERE run_id = %s", (run_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no run_config for run {run_id} in the store")
    return dict(row)


def put_document(conn: psycopg.Connection, run_id: str, name: str, body: str) -> None:
    conn.execute(
        "INSERT INTO run_documents (run_id, name, body) VALUES (%s, %s, %s)"
        " ON CONFLICT (run_id, name) DO UPDATE SET body = EXCLUDED.body, updated_at = now()",
        (run_id, name, body),
    )


def get_document(conn: psycopg.Connection, run_id: str, name: str) -> str:
    row = conn.execute("SELECT body FROM run_documents WHERE run_id = %s AND name = %s", (run_id, name)).fetchone()
    if row is None:
        raise LookupError(f"run {run_id} has no document {name!r} in the store")
    return row["body"]


def append_event(conn: psycopg.Connection, record: dict[str, Any], job_id: int | None = None) -> None:
    """Store one event record (as `Run.emit` builds it: needs `run_id` and `ts`)."""
    conn.execute(
        "INSERT INTO events (run_id, job_id, ts, type, data) VALUES (%s, %s, %s, %s, %s)",
        (record["run_id"], job_id, record["ts"], record.get("type"), Jsonb(record)),
    )


def events(conn: psycopg.Connection, run_id: str) -> list[dict[str, Any]]:
    """A run's event records in emission order."""
    return [r["data"] for r in conn.execute("SELECT data FROM events WHERE run_id = %s ORDER BY id", (run_id,))]


def documents(conn: psycopg.Connection, run_id: str) -> dict[str, str]:
    """Every run document by name."""
    rows = conn.execute("SELECT name, body FROM run_documents WHERE run_id = %s ORDER BY name", (run_id,))
    return {r["name"]: r["body"] for r in rows}


def artifact_prefixes(conn: psycopg.Connection, run_id: str) -> list[dict[str, Any]]:
    """`{job_id, stage, artifact_prefix}` of every finished job of the run that uploaded artifacts, in job order."""
    return conn.execute(
        "SELECT j.id AS job_id, j.stage, r.artifact_prefix FROM stage_results r JOIN stage_jobs j ON j.id = r.job_id"
        " WHERE j.run_id = %s AND r.artifact_prefix IS NOT NULL ORDER BY j.id",
        (run_id,),
    ).fetchall()


# -- improvement log (queued runs); record shape and identity as in docs/improvements-log.md ----
def improvements(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every improvement record, oldest first (the order of `improvements.jsonl`)."""
    return [r["record"] for r in conn.execute("SELECT record FROM improvements ORDER BY id")]


def record_improvement(conn: psycopg.Connection, record: dict[str, Any]) -> None:
    conn.execute("INSERT INTO improvements (record) VALUES (%s)", (Jsonb(record),))


def lock_improvement(conn: psycopg.Connection, identity: tuple[str, str, str]) -> dict[str, Any] | None:
    """The first record with this (ts, run_id, title) identity, row-locked; call inside a transaction."""
    ts, run_id, title = identity
    return conn.execute(
        "SELECT id, record FROM improvements WHERE record->>'ts' = %s AND record->>'run_id' = %s"
        " AND record->>'title' = %s ORDER BY id LIMIT 1 FOR UPDATE",
        (ts, run_id, title),
    ).fetchone()


def update_improvement_application(conn: psycopg.Connection, identity: tuple[str, str, str],
                                   application: dict[str, Any]) -> None:
    """Replace one record's `application` object; LookupError when the identity is gone."""
    with conn.transaction():
        row = lock_improvement(conn, identity)
        if row is None:
            raise LookupError("That improvement is no longer in the log.")
        conn.execute("UPDATE improvements SET record = %s WHERE id = %s",
                     (Jsonb(row["record"] | {"application": application}), row["id"]))


def delete_improvement(conn: psycopg.Connection, identity: tuple[str, str, str]) -> bool:
    """Delete the first record with this identity; RuntimeError while its application is running."""
    with conn.transaction():
        row = lock_improvement(conn, identity)
        if row is None:
            return False
        if (row["record"].get("application") or {}).get("status") == "running":
            raise RuntimeError("Cannot delete an improvement while it is running.")
        conn.execute("DELETE FROM improvements WHERE id = %s", (row["id"],))
    return True


def _state_json(state: RunState) -> Jsonb:
    data = state.model_dump()
    data["tokens"] = state.totals()  # same shape as run-state.json, for readers like the dashboard
    return Jsonb(data)
