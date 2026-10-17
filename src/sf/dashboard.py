"""Local web dashboard over `runs/*/` (stdlib HTTP server, polled JSON API).

Run artifacts are read-only apart from whole-run deletion and the log a retro writes; project Markdown and
agent configuration are editable. A run's retro action starts the improve phase again in a child process.
"""

import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, get_args
from urllib.parse import parse_qs, urlsplit

import yaml
from jsonschema.exceptions import SchemaError

from sf import git as g
from sf.agents import (
    AGENT_NAMES,
    TASK_VARIABLES,
    THINKING_LEVELS,
    Harness,
    Thinking,
    load_agent,
    parse_agent,
    validate_task_template,
)
from sf.improve import load_decisions
from sf.models import list_native_models
from sf.pi import pi_bin
from sf.run import RunLock
from sf.target import discover_documentation

STATIC = Path(__file__).parent / "static"
# Console output of a dashboard retro, written by the `sf improve` child process and served as an artefact.
RETRO_LOG = "retro.log"
ARTEFACT_PREFIXES = ("prompt.md", "plan.md", "review-", "portability-", "validation-", "pi-", "claude-code-",
                     "codex-", RETRO_LOG)
EVENT_TAIL = 200
# Streaming token deltas: tens of thousands per agent, too dense to be useful as timeline dots.
TIMELINE_SKIP = {"message_update"}


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for state_file in sorted(runs_dir.glob("*/run-state.json"), reverse=True):
        try:
            s = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            runs.append({"run_id": state_file.parent.name, "status": "unreadable", "can_retro": False,
                         "error": str(e)})
            continue
        usage = s.get("tokens", {}).get("run", {})
        unavailable = "cost" in usage.get("unavailable", [])
        known = "cost" in usage.get("known", ["cost"])
        runs.append(
            {k: s.get(k) for k in ("run_id", "status", "stage", "loop", "target", "cap_reached")}
            | {"can_retro": "merge" in s.get("completed", []),
               "cost": usage.get("cost", 0) if not unavailable or known else None,
               "cost_partial": unavailable and known, "cost_estimated": usage.get("estimated", False)}
        )
    return runs


def run_detail(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    run_dir = _run_dir(runs_dir, run_id)
    if run_dir is None:
        return None
    state = json.loads((run_dir / "run-state.json").read_text())
    events_path = run_dir / "events.jsonl"
    lines = events_path.read_text().splitlines() if events_path.exists() else []
    records = [json.loads(line) for line in lines]
    events = [e for e in records if e.get("source") == "sf"][-EVENT_TAIL:]
    # New runs retain Pi's native events as well as normalized events. Prefer normalized per invocation.
    normalized = {(e.get("stage"), e.get("agent"), e.get("invocation"))
                  for e in records if e.get("source") == "agent"}
    visible = [e for e in records if e.get("source") != "pi"
               or (e.get("stage"), e.get("agent"), e.get("invocation")) not in normalized]
    tools = [e for e in visible if (e.get("event") or {}).get("type") == "tool_execution_start"][-EVENT_TAIL:]
    timeline = [p for p in (_timeline_point(json.dumps(e)) for e in visible) if p]
    artefacts = sorted(p.name for p in run_dir.iterdir() if p.is_file() and p.name.startswith(ARTEFACT_PREFIXES))
    updated = events_path.stat().st_mtime if events_path.exists() else None
    documents, documents_error = [], None
    try:
        documents = discover_documentation(Path(state["target"]))
    except (g.GitError, OSError, ValueError, KeyError, TypeError) as e:
        documents_error = f"Could not load project documents: {e}"
    return {
        "documents": documents,
        "documents_error": documents_error,
        "state": state,
        "agents": _agent_models(records),
        "events": events,
        "tools": tools,
        "artefacts": artefacts,
        "timeline": timeline,
        "events_updated": updated,
    }


def _agent_models(events: list[dict[str, Any]]) -> dict[str, dict[str, str | None]]:
    """Latest invocation metadata, with command-line fallback for historical Pi runs."""
    models = {}
    for e in events:
        if e.get("type") == "agent_start":
            models[e["agent"]] = {k: e.get(k) for k in ("harness", "model", "thinking")}
        elif e.get("type") == "pi_start" and "harness" not in models.get(e["agent"], {}):
            cmd = e.get("cmd") or []
            models[e["agent"]] = {"model": _flag(cmd, "--model"), "thinking": _flag(cmd, "--thinking")}
    return models


def _flag(cmd: list[str], name: str) -> str | None:
    return cmd[cmd.index(name) + 1] if name in cmd[:-1] else None


class RetroRefused(Exception):
    """Why a retro cannot start for this run right now."""


def start_retro(factory: Path, run_id: str) -> None:
    """Re-enter the improve phase of a finished run in a child `sf improve` process: suggestions only, never applied.

    The child owns the run the way the CLI does - same run lock, same stage, same `run-state.json` and
    `events.jsonl` the dashboard already polls - so the retro is live on the dashboard with no new state.
    Its console output is captured in `retro.log`, served as a run artefact.
    """
    run_dir = _run_dir(factory / "runs", run_id)
    if run_dir is None:
        raise FileNotFoundError("Run not found")
    state = json.loads((run_dir / "run-state.json").read_text())
    if "merge" not in state.get("completed", []):
        raise RetroRefused(f"run {run_id} has not merged; the improver studies merged code")
    if (pid := RunLock(factory).holder()) is not None:
        raise RetroRefused(f"another sf run is active (pid {pid})")
    with (run_dir / RETRO_LOG).open("wb") as log:
        subprocess.Popen([sys.executable, "-m", "sf.cli", "improve", run_id], cwd=factory,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def delete_run(runs_dir: Path, run_id: str) -> bool:
    run_dir = _run_dir(runs_dir, run_id)
    if run_dir is None:
        return False
    shutil.rmtree(run_dir)
    return True


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


def _project_document(runs_dir: Path, run_id: str, name: str) -> Path:
    """Resolve only existing Markdown in this run's target inventory, never arbitrary paths."""
    run_dir = _run_dir(runs_dir, run_id)
    if run_dir is None:
        raise FileNotFoundError("Run not found")
    state = json.loads((run_dir / "run-state.json").read_text())
    target = Path(state["target"])
    if name not in discover_documentation(target):
        raise FileNotFoundError("Project document not found")
    return target / name


def agent_config(agents_dir: Path) -> dict[str, Any]:
    agents = []
    for name in AGENT_NAMES:
        a = load_agent(agents_dir, name)
        agents.append({"name": name, "purpose": a.purpose, "harness": a.harness, "model": a.model,
                       "thinking": a.thinking})
    with ThreadPoolExecutor(max_workers=3) as pool:
        pi_models = pool.submit(list_models)
        native = {h: pool.submit(list_native_models, h) for h in ("claude-code", "codex")}
        models, error = pi_models.result()
        catalogs = {"pi": (models, error), **{h: f.result() for h, f in native.items()}}
    harnesses = [{"id": id_, "label": label, "thinking_levels": THINKING_LEVELS[id_],
                  "models": catalogs[id_][0], "models_error": catalogs[id_][1]}
                 for id_, label in (("pi", "Pi"), ("claude-code", "Claude Code"), ("codex", "Codex"))]
    return {"agents": agents, "thinking_levels": list(get_args(Thinking)), "models": models,
            "models_error": error, "harnesses": harnesses}


def list_models() -> tuple[list[str], str | None]:
    """`provider/model` ids from `pi --list-models`, for the model picker's suggestions."""
    try:
        out = subprocess.run([pi_bin(), "--list-models"], capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"pi --list-models failed: {e}"
    rows = [line.split() for line in out.stdout.splitlines()[1:]]
    return [f"{r[0]}/{r[1]}" for r in rows if len(r) >= 2], None


def set_agent_config(agents_dir: Path, name: str, model: str, thinking: str,
                     harness: str | None = None) -> str | None:
    """Rewrite only configuration fields; omitted harness preserves the existing selection."""
    if name not in AGENT_NAMES:
        return f"unknown agent {name!r}"
    model = model.strip()
    if not model or "\n" in model or "\r" in model:
        return "model must be a single non-empty line"
    if thinking not in get_args(Thinking):
        return f"thinking must be one of {', '.join(get_args(Thinking))}"
    path = agents_dir / f"{name}.md"
    before = path.read_text()
    harness = harness if harness is not None else parse_agent(before).harness
    if harness not in get_args(Harness):
        return f"harness must be one of {', '.join(get_args(Harness))}"
    if thinking not in THINKING_LEVELS[harness]:
        return f"thinking must be one of {', '.join(THINKING_LEVELS[harness])} for {harness}"
    _, front, body = before.split("---", 2)
    fields = [("model", model), ("thinking", thinking)]
    if re.search(r"^harness:", front, re.MULTILINE) or harness != "pi":
        if not re.search(r"^harness:", front, re.MULTILINE):
            front += f"harness: {harness}\n"
        else:
            fields.append(("harness", harness))
    for key, value in fields:
        encoded = json.dumps(value) if key == "thinking" and value == "off" else value
        replacement = f"{key}: {encoded}"
        front, n = re.subn(rf"^{key}:.*$", lambda _, replacement=replacement: replacement,
                          front, count=1, flags=re.MULTILINE)
        if n != 1:
            return f"{path}: no '{key}:' line in frontmatter"
    text = f"---{front}---{body}"
    try:
        parsed = parse_agent(text)  # never write a definition the factory could not load
    except (ValueError, yaml.YAMLError) as e:
        return f"invalid definition: {e}"
    if (parsed.model, parsed.thinking, parsed.harness) != (model, thinking, harness):
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
            if parts == ["api", "improvements"]:
                try:
                    return self._json(list(reversed(load_decisions(agents_dir.parent))))
                except (OSError, ValueError) as e:
                    return self._send(json.dumps({"error": f"Cannot read improvement log: {e}"}).encode(),
                                      "application/json", HTTPStatus.INTERNAL_SERVER_ERROR)
            if len(parts) == 4 and parts[:2] == ["api", "config"] and parts[3] in ("file", "user-file"):
                if parts[2] in AGENT_NAMES:
                    suffix = ".user.md" if parts[3] == "user-file" else ".md"
                    try:
                        result = {"content": (agents_dir / f"{parts[2]}{suffix}").read_text()}
                    except OSError as e:
                        return self._send(json.dumps({"error": str(e)}).encode(), "application/json",
                                          HTTPStatus.NOT_FOUND)
                    if parts[3] == "user-file":
                        result["placeholders"] = list(TASK_VARIABLES[parts[2]])
                    return self._json(result)
                return self.send_error(HTTPStatus.NOT_FOUND)
            if parts == ["api", "runs"]:
                return self._json(list_runs(runs_dir))
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._json(run_detail(runs_dir, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "document":
                try:
                    name = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
                    path = _project_document(runs_dir, parts[2], name)
                    return self._json({"content": path.read_text(),
                                       "deletable": not path.resolve().is_relative_to(agents_dir.resolve())})
                except (g.GitError, OSError, ValueError, KeyError, TypeError) as e:
                    return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.NOT_FOUND)
            if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                text = artefact(runs_dir, parts[2], parts[3])
                if text is not None:
                    return self._send(text.encode(), "text/plain; charset=utf-8")
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "retro":
                return self._start_retro(parts[2])
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "document":
                return self._delete_project_document(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "runs"] and delete_run(runs_dir, parts[2]):
                return self._send(b"{}", "application/json")
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_PUT(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "document":
                return self._save_project_document(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "config"] and parts[3] in ("file", "user-file"):
                return self._save_agent_file(parts[2], parts[3] == "user-file")
            if len(parts) != 3 or parts[:2] != ["api", "config"]:
                return self.send_error(HTTPStatus.NOT_FOUND)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or any(not isinstance(body[k], str) for k in ("model", "thinking")):
                    raise ValueError("model and thinking must be strings")
                if "harness" in body and not isinstance(body["harness"], str):
                    raise ValueError("harness must be a string")
                error = set_agent_config(agents_dir, parts[2], body["model"], body["thinking"], body.get("harness"))
            except (ValueError, KeyError, TypeError) as e:
                error = f"bad request: {e}"
            self._send(
                json.dumps({"error": error}).encode(),
                "application/json",
                HTTPStatus.BAD_REQUEST if error else HTTPStatus.OK,
            )

        def _start_retro(self, run_id: str) -> None:
            try:
                start_retro(agents_dir.parent, run_id)
            except FileNotFoundError as e:
                status, error = HTTPStatus.NOT_FOUND, str(e)
            except RetroRefused as e:
                status, error = HTTPStatus.CONFLICT, str(e)
            except (OSError, ValueError) as e:  # the log could not be written, or the child could not start
                status, error = HTTPStatus.INTERNAL_SERVER_ERROR, str(e)
            else:
                return self._json({"error": None})
            self._send(json.dumps({"error": error}).encode(), "application/json", status)

        def _delete_project_document(self, run_id: str) -> None:
            status = HTTPStatus.BAD_REQUEST
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or not isinstance(body.get("original"), str):
                    raise TypeError("original must be a string")
                status = HTTPStatus.NOT_FOUND
                name = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
                path = _project_document(runs_dir, run_id, name)
                status = HTTPStatus.FORBIDDEN
                if path.resolve().is_relative_to(agents_dir.resolve()):
                    raise ValueError("Agent configuration files cannot be deleted")
                status = HTTPStatus.CONFLICT
                if path.read_text() != body["original"]:
                    raise ValueError("File changed since opening. Reopen it before deleting.")
                status = HTTPStatus.BAD_REQUEST
                path.unlink()
            except (g.GitError, OSError, ValueError, KeyError, TypeError) as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", status)
            self._json({"error": None})

        def _save_project_document(self, run_id: str) -> None:
            status = HTTPStatus.BAD_REQUEST
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or any(not isinstance(body.get(k), str)
                                                     for k in ("content", "original")):
                    raise ValueError("content and original must be strings")
                status = HTTPStatus.NOT_FOUND
                name = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
                path = _project_document(runs_dir, run_id, name)
                status = HTTPStatus.CONFLICT
                if path.read_text() != body["original"]:
                    raise ValueError("File changed since opening. Reopen it before saving.")
                status = HTTPStatus.BAD_REQUEST
                path.write_text(body["content"])
            except (g.GitError, OSError, ValueError, KeyError, TypeError) as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", status)
            self._json({"error": None})

        def _save_agent_file(self, name: str, user_file: bool = False) -> None:
            if name not in AGENT_NAMES:
                return self.send_error(HTTPStatus.NOT_FOUND)
            status = HTTPStatus.BAD_REQUEST
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or any(not isinstance(body.get(k), str)
                                                     for k in ("content", "original")):
                    raise ValueError("content and original must be strings")
                if user_file:
                    validate_task_template(name, body["content"])
                else:
                    agent = parse_agent(body["content"])
                    if agent.name != name:
                        raise ValueError("agent name must match the file name")
                suffix = ".user.md" if user_file else ".md"
                path = agents_dir / f"{name}{suffix}"
                if path.read_text() != body["original"]:
                    status = HTTPStatus.CONFLICT
                    raise ValueError("File changed since opening. Reopen it before saving.")
                path.write_text(body["content"])
            except (ValueError, TypeError, yaml.YAMLError, SchemaError, OSError) as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", status)
            if user_file:
                return self._json({"error": None, "name": name})
            self._json({"error": None, "name": agent.name, "purpose": agent.purpose,
                        "harness": agent.harness, "model": agent.model, "thinking": agent.thinking})

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
