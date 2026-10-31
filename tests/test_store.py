"""The Postgres store: schema migration, run creation with config snapshot, state and events."""

from pathlib import Path

import pytest

from sf import store
from sf.run import Invocation, Run, RunState, Usage


def _state(run_id: str, target: Path) -> RunState:
    return RunState(run_id=run_id, target=str(target), trunk="main", branch=f"sf/{run_id}")


def test_migrate_is_idempotent(db):
    assert store.migrate(db) == len(store.MIGRATIONS)
    tables = {r["table_name"] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert {"runs", "run_config", "stage_jobs", "stage_results", "events", "runners"} <= tables


def test_connect_requires_a_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SF_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SF_DATABASE_URL"):
        store.connect()


def test_run_round_trips_state_config_and_events(db, factory: Path, target: Path):
    run_id = store.new_run_id(db)
    config = store.snapshot_config(factory, target)
    assert "implementer.md" in config["agents"] and "gitignore" in config["templates"]
    store.create_run(db, _state(run_id, target), config)
    assert store.load_config(db, run_id) == config

    run = Run(factory, store.load_state(db, run_id), db)
    run.state.stage, run.state.loop, run.state.completed = "implement", 2, ["branch", "prompt", "plan"]
    inv = run.next_invocation("implementer")
    inv.usage = Usage(input=10, output=5)
    run.save()
    run.log("stage_started", detail="x")

    again = Run.load(factory, run_id, db)
    assert again.state == run.state
    assert isinstance(again.state.invocations[0], Invocation)
    assert again.next_invocation("implementer").n == 2
    row = db.execute("SELECT status, current_stage, loop_count, state FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert (row["status"], row["current_stage"], row["loop_count"]) == ("running", "implement", 2)
    assert row["state"]["tokens"]["run"]["input"] == 10
    [event] = store.events(db, run_id)
    assert event["type"] == "stage_started" and event["stage"] == "implement" and event["source"] == "sf"
    assert not (run.dir / "events.jsonl").exists() and not (run.dir / "run-state.json").exists()


def test_snapshot_uses_submitter_working_tree_not_origin(tmp_path, factory: Path, target: Path):
    """Uncommitted dashboard config edits reach the run even when origin's default branch differs."""
    from conftest import sh

    from sf.dashboard import set_agent_config

    bare = tmp_path / "factory.git"
    sh(tmp_path, "git", "clone", "-q", "--bare", str(factory), str(bare))
    sh(factory, "git", "remote", "add", "origin", str(bare))
    sh(factory, "git", "checkout", "-qb", "feature")
    assert set_agent_config(factory / "agents", "planner", "halogen/local-model", "high") is None
    config = store.snapshot_config(factory, target)
    assert "model: halogen/local-model" in config["agents"]["planner.md"]
    assert config["factory_sha"] == sh(factory, "git", "rev-parse", "HEAD").strip()
    assert config["factory_remote"] == str(bare)


def test_new_run_id_suffixes_a_taken_second(db, target: Path, monkeypatch: pytest.MonkeyPatch):
    class Fixed:
        @staticmethod
        def now():
            from datetime import datetime

            return datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(store, "datetime", Fixed)
    cfg = {"agents": {}, "templates": {}, "target_config": {}, "factory_sha": None}
    store.create_run(db, _state(store.new_run_id(db), target), cfg)
    assert store.new_run_id(db) == "20260102-030405-1"


def test_missing_run_is_reported(db, target: Path):
    with pytest.raises(LookupError, match="no run nope"):
        store.load_state(db, "nope")
    with pytest.raises(LookupError, match="create_run"):
        store.save_state(db, _state("nope", target))
