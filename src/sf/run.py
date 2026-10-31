"""Run state and event stream, persisted to `runs/<id>/` files or, given a connection, to the Postgres store."""

import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field

STAGES = ("branch", "prompt", "plan", "implement", "document", "merge", "improve")


def now() -> str:
    return datetime.now(UTC).isoformat()


class Usage(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    unavailable: list[str] = Field(default_factory=list)
    known: list[str] = Field(default_factory=lambda: ["input", "output", "cache_read", "cache_write", "cost"])
    estimated: bool = False

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cost=self.cost + other.cost,
            unavailable=sorted(set(self.unavailable) | set(other.unavailable)),
            known=sorted(set(self.known) | set(other.known)),
            estimated=self.estimated or other.estimated,
        )

    @classmethod
    def from_pi(cls, u: dict[str, Any]) -> Usage:
        return cls(
            input=u.get("input", 0),
            output=u.get("output", 0),
            cache_read=u.get("cacheRead", 0),
            cache_write=u.get("cacheWrite", 0),
            cost=(u.get("cost") or {}).get("total", 0.0),
            estimated=True,
        )


class Invocation(BaseModel):
    agent: str
    stage: str
    loop: int
    n: int
    harness: str = "pi"
    model: str | None = None
    thinking: str | None = None
    session_id: str | None = None
    ok: bool = False
    usage: Usage = Field(default_factory=Usage)


class RunState(BaseModel):
    kind: Literal["workflow", "improvement"] = "workflow"
    run_id: str
    target: str
    trunk: str
    branch: str
    approve_plan: bool = False
    # `parked`: a queued run stopped on an agent failure or loop cap; resumable, visible in the dashboard.
    status: Literal["running", "done", "failed", "aborted", "parked"] = "running"
    stage: str = "branch"
    loop: int = 0
    max_loops: int = 5
    cap_reached: bool = False
    merge_conflicts: int = 0
    pending: dict[str, Any] | None = None  # the next step `{stage, inputs}` the local driver resumes from
    completed: list[str] = Field(default_factory=list)
    results: dict[str, str] = Field(default_factory=dict)
    invocations: list[Invocation] = Field(default_factory=list)

    def totals(self) -> dict[str, Any]:
        by_stage: dict[str, Usage] = defaultdict(lambda: Usage(known=[]))
        by_loop: dict[int, Usage] = defaultdict(lambda: Usage(known=[]))
        total = Usage(known=[])
        for inv in self.invocations:
            by_stage[inv.stage] += inv.usage
            if inv.stage == "implement":
                by_loop[inv.loop] += inv.usage
            total += inv.usage
        return {
            "run": total.model_dump(),
            "by_stage": {k: v.model_dump() for k, v in by_stage.items()},
            "by_loop": {str(k): v.model_dump() for k, v in by_loop.items()},
        }


class Run:
    """Owns `runs/<id>/` and the run's state persistence and tagged event stream.

    With `db` (a `sf.store.connect()` connection) state and events go to Postgres instead of
    `run-state.json` / `events.jsonl`; the run must already exist there (`store.create_run`).
    `run_dir` replaces `runs/<id>/` as the home of harness outputs (a queued job's temp dir).
    """

    def __init__(self, factory: Path, state: RunState, db: Any = None, run_dir: Path | None = None):
        self.factory = factory
        self.state = state
        self.db = db
        self.dir = run_dir or factory / "runs" / state.run_id  # queued jobs pass a per-job temp dir
        (self.dir / "sessions").mkdir(parents=True, exist_ok=True)
        self._invocation_counts: dict[str, int] = defaultdict(int)

    @classmethod
    def load(cls, factory: Path, run_id: str, db: Any = None) -> Run:
        if db is not None:
            from sf import store

            state = store.load_state(db, run_id)
        else:
            path = factory / "runs" / run_id / "run-state.json"
            if not path.exists():
                raise FileNotFoundError(f"no run-state.json for run {run_id} at {path}")
            state = RunState.model_validate_json(path.read_text())
        run = cls(factory, state, db)
        for inv in state.invocations:
            run._invocation_counts[inv.stage] = max(run._invocation_counts[inv.stage], inv.n)
        return run

    @property
    def target(self) -> Path:
        return Path(self.state.target)

    def save(self) -> None:
        if self.db is not None:
            from sf import store

            store.save_state(self.db, self.state)
            return
        data = self.state.model_dump()
        data["tokens"] = self.state.totals()
        (self.dir / "run-state.json").write_text(json.dumps(data, indent=2) + "\n")

    def emit(self, source: str, event: dict[str, Any]) -> None:
        record = {
            "ts": now(),
            "run_id": self.state.run_id,
            "stage": self.state.stage,
            "loop": self.state.loop,
            "source": source,
            **event,
        }
        if self.db is not None:
            from sf import store

            store.append_event(self.db, record)
            return
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    def log(self, type_: str, **fields: Any) -> None:
        self.emit("sf", {"type": type_, **fields})

    def next_invocation(self, agent: str) -> Invocation:
        stage = self.state.stage
        self._invocation_counts[stage] += 1
        inv = Invocation(agent=agent, stage=stage, loop=self.state.loop, n=self._invocation_counts[stage])
        self.state.invocations.append(inv)
        return inv


class RunLock:
    """Serialises runs: one `runs/.lock` holding the owning pid."""

    def __init__(self, factory: Path):
        self.path = factory / "runs" / ".lock"

    def holder(self) -> int | None:
        """The pid of the live process holding the lock, or None when nothing holds it."""
        try:
            pid = int(self.path.read_text().strip() or 0)
        except FileNotFoundError:
            return None
        return pid if pid and _alive(pid) else None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if (pid := self.holder()) is not None:
            raise RuntimeError(f"another sf run is active (pid {pid}, lock {self.path})")
        self.path.unlink(missing_ok=True)  # a lock left by a dead process is ours to take
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
