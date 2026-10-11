"""Run directory: persistent state (`run-state.json`), event stream (`events.jsonl`), lockfile."""

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

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cost=self.cost + other.cost,
        )

    @classmethod
    def from_pi(cls, u: dict[str, Any]) -> Usage:
        return cls(
            input=u.get("input", 0),
            output=u.get("output", 0),
            cache_read=u.get("cacheRead", 0),
            cache_write=u.get("cacheWrite", 0),
            cost=(u.get("cost") or {}).get("total", 0.0),
        )


class Invocation(BaseModel):
    agent: str
    stage: str
    loop: int
    n: int
    ok: bool = False
    usage: Usage = Field(default_factory=Usage)


class RunState(BaseModel):
    run_id: str
    target: str
    trunk: str
    branch: str
    approve_plan: bool = False
    status: Literal["running", "done", "failed", "aborted"] = "running"
    stage: str = "branch"
    loop: int = 0
    max_loops: int = 5
    cap_reached: bool = False
    completed: list[str] = Field(default_factory=list)
    results: dict[str, str] = Field(default_factory=dict)
    invocations: list[Invocation] = Field(default_factory=list)

    def totals(self) -> dict[str, Any]:
        by_stage: dict[str, Usage] = defaultdict(Usage)
        by_loop: dict[int, Usage] = defaultdict(Usage)
        total = Usage()
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
    """Owns `runs/<id>/`: state persistence and the tagged event stream."""

    def __init__(self, factory: Path, state: RunState):
        self.factory = factory
        self.state = state
        self.dir = factory / "runs" / state.run_id
        (self.dir / "sessions").mkdir(parents=True, exist_ok=True)
        self._pi_counts: dict[str, int] = defaultdict(int)

    @classmethod
    def load(cls, factory: Path, run_id: str) -> Run:
        path = factory / "runs" / run_id / "run-state.json"
        if not path.exists():
            raise FileNotFoundError(f"no run-state.json for run {run_id} at {path}")
        state = RunState.model_validate_json(path.read_text())
        run = cls(factory, state)
        for inv in state.invocations:
            run._pi_counts[inv.stage] = max(run._pi_counts[inv.stage], inv.n)
        return run

    @property
    def target(self) -> Path:
        return Path(self.state.target)

    def save(self) -> None:
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
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    def log(self, type_: str, **fields: Any) -> None:
        self.emit("sf", {"type": type_, **fields})

    def next_invocation(self, agent: str) -> Invocation:
        stage = self.state.stage
        self._pi_counts[stage] += 1
        inv = Invocation(agent=agent, stage=stage, loop=self.state.loop, n=self._pi_counts[stage])
        self.state.invocations.append(inv)
        return inv


class RunLock:
    """Serialises runs: one `runs/.lock` holding the owning pid."""

    def __init__(self, factory: Path):
        self.path = factory / "runs" / ".lock"

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            pid = int(self.path.read_text().strip() or 0)
            if pid and _alive(pid):
                raise RuntimeError(f"another sf run is active (pid {pid}, lock {self.path})")
            self.path.unlink()
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
