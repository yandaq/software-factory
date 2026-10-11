"""Local web dashboard over `runs/*/` (stdlib HTTP server, polled JSON API).

Runs are read-only; the config page edits only the `model` and `thinking` frontmatter of `agents/*.md`.
"""

import json
import re
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, get_args

import yaml

from sf.agents import AGENT_NAMES, Thinking, load_agent, parse_agent
from sf.pi import pi_bin

STATIC = Path(__file__).parent / "static"
ARTEFACT_PREFIXES = ("prompt.md", "plan.md", "review-", "validation-", "pi-")
EVENT_TAIL = 200
# Streaming token deltas: tens of thousands per agent, too dense to be useful as timeline dots.
TIMELINE_SKIP = {"message_update"}


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for state_file in sorted(runs_dir.glob("*/run-state.json"), reverse=True):
        try:
            s = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            runs.append({"run_id": state_file.parent.name, "status": "unreadable", "error": str(e)})
            continue
        runs.append(
            {k: s.get(k) for k in ("run_id", "status", "stage", "loop", "target", "cap_reached")}
            | {"cost": s.get("tokens", {}).get("run", {}).get("cost", 0)}
        )
    return runs


def run_detail(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    run_dir = _run_dir(runs_dir, run_id)
    if run_dir is None:
        return None
    state = json.loads((run_dir / "run-state.json").read_text())
    events_path = run_dir / "events.jsonl"
    lines = events_path.read_text().splitlines() if events_path.exists() else []
    events = [json.loads(line) for line in lines if '"source": "sf"' in line][-EVENT_TAIL:]
    tools = [json.loads(line) for line in lines if '"tool_execution_start"' in line][-EVENT_TAIL:]
    timeline = [p for p in map(_timeline_point, lines) if p]
    artefacts = sorted(p.name for p in run_dir.iterdir() if p.is_file() and p.name.startswith(ARTEFACT_PREFIXES))
    updated = events_path.stat().st_mtime if events_path.exists() else None
    return {
        "state": state,
        "events": events,
        "tools": tools,
        "artefacts": artefacts,
        "timeline": timeline,
        "events_updated": updated,
    }


def _timeline_point(line: str) -> list[Any] | None:
    """[ts, lane, type, context, invocation] for one event; the lane is the agent when there is one, else "sf" (the orchestrator).

    context is the agent's context size in tokens after an assistant turn (pi's usage.totalTokens), else None;
    invocation numbers the agent's pi process (each starts with a fresh context), None for orchestrator events.
    """
    e = json.loads(line)
    inner = e.get("event")
    kind = inner.get("type") if isinstance(inner, dict) else e.get("type")
    if kind in TIMELINE_SKIP:
        return None
    context = None
    if kind == "message_end" and inner["message"].get("role") == "assistant":
        context = (inner["message"].get("usage") or {}).get("totalTokens")
    return [e["ts"], e.get("agent") or "sf", kind, context, e.get("invocation")]


def artefact(runs_dir: Path, run_id: str, name: str) -> str | None:
    run_dir = _run_dir(runs_dir, run_id)
    if run_dir is None or "/" in name or not name.startswith(ARTEFACT_PREFIXES):
        return None
    path = run_dir / name
    return path.read_text(errors="replace") if path.is_file() else None


def _run_dir(runs_dir: Path, run_id: str) -> Path | None:
    run_dir = runs_dir / run_id
    if "/" in run_id or run_id.startswith(".") or not (run_dir / "run-state.json").is_file():
        return None
    return run_dir


def agent_config(agents_dir: Path) -> dict[str, Any]:
    agents = []
    for name in AGENT_NAMES:
        a = load_agent(agents_dir, name)
        agents.append({"name": name, "purpose": a.purpose, "model": a.model, "thinking": a.thinking})
    models, error = list_models()
    return {"agents": agents, "thinking_levels": list(get_args(Thinking)), "models": models, "models_error": error}


def list_models() -> tuple[list[str], str | None]:
    """`provider/model` ids from `pi --list-models`, for the model picker's suggestions."""
    try:
        out = subprocess.run([pi_bin(), "--list-models"], capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"pi --list-models failed: {e}"
    rows = [line.split() for line in out.stdout.splitlines()[1:]]
    return [f"{r[0]}/{r[1]}" for r in rows if len(r) >= 2], None


def set_agent_config(agents_dir: Path, name: str, model: str, thinking: str) -> str | None:
    """Rewrite one agent's `model`/`thinking` frontmatter lines in place; return an error message or None."""
    if name not in AGENT_NAMES:
        return f"unknown agent {name!r}"
    model = model.strip()
    if not model or "\n" in model:
        return "model must be a single non-empty line"
    if thinking not in get_args(Thinking):
        return f"thinking must be one of {', '.join(get_args(Thinking))}"
    path = agents_dir / f"{name}.md"
    _, front, body = path.read_text().split("---", 2)
    for key, value in (("model", model), ("thinking", thinking)):
        front, n = re.subn(rf"^{key}:.*$", f"{key}: {value}", front, count=1, flags=re.MULTILINE)
        if n != 1:
            return f"{path}: no '{key}:' line in frontmatter"
    text = f"---{front}---{body}"
    try:
        parsed = parse_agent(text)  # never write a definition the factory could not load
    except (ValueError, yaml.YAMLError) as e:
        return f"invalid definition: {e}"
    if (parsed.model, parsed.thinking) != (model, thinking):
        return f"model {model!r} does not survive YAML parsing unquoted"
    path.write_text(text)
    return None


def make_handler(runs_dir: Path, agents_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if not parts:
                return self._send((STATIC / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if parts == ["config"]:
                return self._send((STATIC / "config.html").read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "config"]:
                return self._json(agent_config(agents_dir))
            if parts == ["api", "runs"]:
                return self._json(list_runs(runs_dir))
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._json(run_detail(runs_dir, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                text = artefact(runs_dir, parts[2], parts[3])
                if text is not None:
                    return self._send(text.encode(), "text/plain; charset=utf-8")
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_PUT(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if len(parts) != 3 or parts[:2] != ["api", "config"]:
                return self.send_error(HTTPStatus.NOT_FOUND)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                error = set_agent_config(agents_dir, parts[2], str(body["model"]), str(body["thinking"]))
            except (ValueError, KeyError, TypeError) as e:
                error = f"bad request: {e}"
            self._send(
                json.dumps({"error": error}).encode(),
                "application/json",
                HTTPStatus.BAD_REQUEST if error else HTTPStatus.OK,
            )

        def _json(self, data: Any) -> None:
            if data is None:
                return self.send_error(HTTPStatus.NOT_FOUND)
            self._send(json.dumps(data).encode(), "application/json")

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError, ConnectionResetError:
                pass  # the browser navigated away or reloaded mid-response; nothing to deliver

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep the terminal quiet; errors still surface via send_error responses

    return Handler


def serve(runs_dir: Path, host: str, port: int, agents_dir: Path | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(runs_dir, agents_dir or runs_dir.parent / "agents"))
