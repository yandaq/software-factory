"""Local web dashboard over `runs/*/` (stdlib HTTP server, polled JSON API).

Run artifacts include retro output and explicit improvement executions. Project Markdown and agent
configuration are editable; suggestions can be applied by the implementer or deleted. A run's retro
action starts the read-only improve phase again in a child process.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, get_args
from urllib.parse import parse_qs, urlsplit

import psycopg
import yaml
from jsonschema.exceptions import SchemaError

from sf import artifacts, queue, store
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
from sf.apply import (
    ApplyRefused,
    recover_applications,
    recover_queued_applications,
    start_application,
    start_queued_application,
)
from sf.improve import delete_decision, load_decisions
from sf.models import list_native_models, list_pi_models
from sf.run import RunLock
from sf.target import TrunkError, discover_documentation
from sf.workflow import resume_queued, submit_remote_run

STATIC = Path(__file__).parent / "static"
# Console output of a dashboard retro, written by the `sf improve` child process and served as an artefact.
RETRO_LOG = "retro.log"
ARTEFACT_PREFIXES = ("prompt.md", "plan.md", "review-", "portability-", "validation-", "pi-", "claude-code-",
                     "codex-", RETRO_LOG)
EVENT_TAIL = 200
# Streaming token deltas: tens of thousands per agent, too dense to be useful as timeline dots.
TIMELINE_SKIP = {"message_update"}


def _summary(s: dict[str, Any]) -> dict[str, Any]:
    usage = s.get("tokens", {}).get("run", {})
    unavailable = "cost" in usage.get("unavailable", [])
    known = "cost" in usage.get("known", ["cost"])
    return (
        {k: s.get(k) for k in ("run_id", "status", "stage", "loop", "target", "cap_reached")}
        | {"can_retro": "merge" in s.get("completed", []),
           "cost": usage.get("cost", 0) if not unavailable or known else None,
           "cost_partial": unavailable and known, "cost_estimated": usage.get("estimated", False)}
    )


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for state_file in sorted(runs_dir.glob("*/run-state.json"), reverse=True):
        try:
            s = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            runs.append({"run_id": state_file.parent.name, "status": "unreadable", "can_retro": False,
                         "error": str(e)})
            continue
        runs.append(_summary(s))
    return runs


def _event_views(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The detail fields derived from a run's event records: agents, events, tools, timeline."""
    events = [e for e in records if e.get("source") == "sf"][-EVENT_TAIL:]
    # Historical runs may contain both Pi and normalized events. Prefer normalized per invocation.
    normalized = {(e.get("stage"), e.get("agent"), e.get("invocation"))
                  for e in records if e.get("source") == "agent"}
    visible = [e for e in records if e.get("source") != "pi"
               or (e.get("stage"), e.get("agent"), e.get("invocation")) not in normalized]
    tools = [e for e in visible if (e.get("event") or {}).get("type") == "tool_execution_start"][-EVENT_TAIL:]
    timeline = [p for p in (_timeline_point(json.dumps(e)) for e in visible) if p]
    return {"agents": _agent_models(records), "events": events, "tools": tools, "timeline": timeline}


def _documents(target: Any) -> tuple[list[str], str | None]:
    try:
        return discover_documentation(Path(target)), None
    except (g.GitError, OSError, ValueError, KeyError, TypeError) as e:
        return [], f"Could not load project documents: {e}"


def run_detail(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    run_dir = _run_dir(runs_dir, run_id)
    if run_dir is None:
        return None
    state = json.loads((run_dir / "run-state.json").read_text())
    events_path = run_dir / "events.jsonl"
    lines = events_path.read_text().splitlines() if events_path.exists() else []
    records = [json.loads(line) for line in lines]
    artefacts = sorted(p.name for p in run_dir.iterdir() if p.is_file() and p.name.startswith(ARTEFACT_PREFIXES))
    updated = events_path.stat().st_mtime if events_path.exists() else None
    documents, documents_error = _documents(state.get("target"))
    return {
        "documents": documents,
        "documents_error": documents_error,
        "state": state,
        **_event_views(records),
        "artefacts": artefacts,
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
    try:
        state = json.loads((run_dir / "run-state.json").read_text())
    except json.JSONDecodeError:
        state = {}  # unreadable historical runs remain deletable
    if state.get("kind") == "improvement" and state.get("status") == "running":
        raise ApplyRefused("Cannot delete an improvement execution while it is running.")
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


def _project_document(runs: Any, run_id: str, name: str) -> Path:
    """Resolve only existing Markdown in this run's target inventory, never arbitrary paths."""
    target = runs.target(run_id)
    if target is None:
        raise FileNotFoundError("Run not found")
    if name not in discover_documentation(target):
        raise FileNotFoundError("Project document not found")
    return target / name


class FileRuns:
    """Runs and improvement records from `runs/*/` and `improvements.jsonl` (no SF_DATABASE_URL)."""

    queued = False

    def __init__(self, runs_dir: Path, factory: Path):
        self.runs_dir, self.factory = runs_dir, factory

    def list_runs(self) -> list[dict[str, Any]]:
        return list_runs(self.runs_dir)

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        return run_detail(self.runs_dir, run_id)

    def artefact(self, run_id: str, name: str) -> str | None:
        return artefact(self.runs_dir, run_id, name)

    def delete_run(self, run_id: str) -> bool:
        return delete_run(self.runs_dir, run_id)

    def target(self, run_id: str) -> Path | None:
        run_dir = _run_dir(self.runs_dir, run_id)
        return None if run_dir is None else Path(json.loads((run_dir / "run-state.json").read_text())["target"])

    def improvements(self) -> list[dict[str, Any]]:
        return load_decisions(self.factory)

    def delete_improvement(self, identity: tuple[str, str, str]) -> bool:
        return delete_decision(self.factory, *identity)

    def apply_improvement(self, identity: tuple[str, str, str]) -> str:
        return start_application(self.factory, identity)


class DbRuns:
    """Runs, jobs, runners and improvement records from the Postgres store (SF_DATABASE_URL set).

    One connection per call: the server is threaded and a psycopg connection is not shared across threads.
    Artifacts are linked with presigned artifact-store URLs; run documents are served from the store.
    """

    queued = True

    def __init__(self, db_url: str, factory: Path):
        self.db_url, self.factory = db_url, factory

    def _conn(self) -> Any:
        return store.connect(self.db_url)

    def list_runs(self, target: str | None = None) -> list[dict[str, Any]]:
        """Run summaries, newest first, each with its `target_key`; only that target's runs when `target` is set."""
        with self._conn() as conn:
            rows = conn.execute("SELECT r.state, c.target_config->>'remote' AS remote FROM runs r"
                                " LEFT JOIN run_config c ON c.run_id = r.id ORDER BY r.created_at DESC, r.id DESC")
            runs = [_summary(r["state"]) | {"target_key": _target_key(r["remote"])} for r in rows]
        return [r for r in runs if target is None or r["target_key"] == target]

    def targets(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return store.list_targets(conn)

    def add_target(self, url: str) -> dict[str, Any]:
        """Configure a target after `git ls-remote` reaches it; ValueError for a duplicate, GitError if unreachable."""
        url = g.expand_target_url(url)
        if not url:
            raise ValueError("url must not be empty")
        if g.target_key(url) is None:  # a local path (one machine, or tests): stored resolved, keyed by itself
            url = str(Path(url).expanduser().resolve())
        g.ls_remote(url)
        with self._conn() as conn:
            return store.add_target(conn, url, _target_key(url))

    def delete_target(self, target_id: int) -> bool:
        with self._conn() as conn:
            return store.delete_target(conn, target_id)

    def submit(self, target: str, prompt: str) -> str:
        """Queue a run against the configured target with this key; LookupError when there is none."""
        with self._conn() as conn:
            row = store.get_target(conn, target)
            if row is None:
                raise LookupError(f"no configured target {target}")
            return submit_remote_run(conn, self.factory, row["url"], prompt).run_id

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT state FROM runs WHERE id = %s", (run_id,)).fetchone()
            if row is None:
                return None
            records = store.events(conn, run_id)
            names = sorted(store.documents(conn, run_id))
            jobs = conn.execute(
                "SELECT j.id, j.stage, j.attempt, j.status, j.claimed_by, j.infra_retries, j.created_at,"
                " r.outcome, r.branch_sha, r.artifact_prefix FROM stage_jobs j"
                " LEFT JOIN stage_results r ON r.job_id = j.id WHERE j.run_id = %s ORDER BY j.id",
                (run_id,),
            ).fetchall()
        artifacts_list, artifacts_error = [], None
        try:
            for job in jobs:
                if prefix := job["artifact_prefix"]:
                    artifacts_list += [{"job_id": job["id"], "stage": job["stage"], "name": key[len(prefix):],
                                        "key": key, "url": artifacts.presigned_url(key)}
                                       for key in artifacts.list_keys(prefix)]
        except g.InfraError as e:
            artifacts_error = str(e)
        state = row["state"]
        documents, documents_error = _documents(state.get("target"))
        updated = datetime.fromisoformat(records[-1]["ts"]).timestamp() if records else None
        return {
            "documents": documents,
            "documents_error": documents_error,
            "state": state,
            **_event_views(records),
            "artefacts": [n for n in names if n.startswith(ARTEFACT_PREFIXES)],
            "artifacts": artifacts_list,
            "artifacts_error": artifacts_error,
            "jobs": [{k: (str(v) if k == "created_at" else v) for k, v in job.items()} for job in jobs],
            "can_resume": state.get("status") == "parked" and not state.get("cap_reached"),
            "events_updated": updated,
        }

    def artefact(self, run_id: str, name: str) -> str | None:
        if "/" in name or not name.startswith(ARTEFACT_PREFIXES):
            return None
        with self._conn() as conn:
            try:
                return store.get_document(conn, run_id, name)
            except LookupError:
                return None

    def delete_run(self, run_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM runs WHERE id = %s", (run_id,)).rowcount == 1

    def target(self, run_id: str) -> Path | None:
        with self._conn() as conn:
            row = conn.execute("SELECT target FROM runs WHERE id = %s", (run_id,)).fetchone()
        return None if row is None else Path(row["target"])

    def resume(self, run_id: str) -> dict[str, Any] | None:
        """`workflow.resume_queued`; None for an unknown run, ValueError when the run cannot resume."""
        with self._conn() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = %s", (run_id,)).fetchone() is None:
                return None
            return resume_queued(conn, run_id)

    def runners(self) -> dict[str, Any]:
        with self._conn() as conn:
            runners = [r | {"last_heartbeat": str(r["last_heartbeat"]) if r["last_heartbeat"] else None}
                       for r in queue.list_runners(conn)]
            return {"runners": runners, "queued": queue.queue_depth(conn)}

    def improvements(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return store.improvements(conn)

    def delete_improvement(self, identity: tuple[str, str, str]) -> bool:
        with self._conn() as conn:
            return store.delete_improvement(conn, identity)

    def apply_improvement(self, identity: tuple[str, str, str]) -> str:
        return start_queued_application(self.db_url, self.factory, identity)


def _target_key(remote: str | None) -> str | None:
    """A run's or target's filter key: git.target_key of its remote URL, else (a local path) the path itself."""
    return remote and (g.target_key(remote) or remote)


def agent_config(agents_dir: Path) -> dict[str, Any]:
    agents = []
    for name in AGENT_NAMES:
        a = load_agent(agents_dir, name)
        agents.append({"name": name, "purpose": a.purpose, "harness": a.harness, "model": a.model,
                       "thinking": a.thinking})
    with ThreadPoolExecutor(max_workers=3) as pool:
        pi_models = pool.submit(list_pi_models)
        native = {h: pool.submit(list_native_models, h) for h in ("claude-code", "codex")}
        models, error = pi_models.result()
        catalogs = {"pi": (models, error), **{h: f.result() for h, f in native.items()}}
    harnesses = [{"id": id_, "label": label, "thinking_levels": THINKING_LEVELS[id_],
                  "models": catalogs[id_][0], "models_error": catalogs[id_][1]}
                 for id_, label in (("pi", "Pi"), ("claude-code", "Claude Code"), ("codex", "Codex"))]
    return {"agents": agents, "thinking_levels": list(get_args(Thinking)), "models": models,
            "models_error": error, "harnesses": harnesses}


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


def make_handler(runs: FileRuns | DbRuns, agents_dir: Path) -> type[BaseHTTPRequestHandler]:
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
                    return self._json(list(reversed(runs.improvements())))
                except (OSError, ValueError, psycopg.Error) as e:
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
            if parts == ["api", "runners"]:  # file mode has no runners: null tells the page to hide the panel
                return self._json(runs.runners() if runs.queued else {"runners": None, "queued": None})
            if parts == ["api", "mode"]:
                return self._json({"queued": runs.queued})
            if parts == ["api", "targets"]:
                return self._json(runs.targets()) if runs.queued else self._needs_db()
            if parts == ["api", "runs"]:
                target = parse_qs(urlsplit(self.path).query).get("target", [None])[0]
                if target is not None and not runs.queued:
                    return self._needs_db()
                return self._json(runs.list_runs(target) if target is not None else runs.list_runs())
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._json(runs.run_detail(parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "document":
                try:
                    name = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
                    path = _project_document(runs, parts[2], name)
                    return self._json({"content": path.read_text(),
                                       "deletable": not path.resolve().is_relative_to(agents_dir.resolve())})
                except (g.GitError, OSError, ValueError, KeyError, TypeError) as e:
                    return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.NOT_FOUND)
            if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                text = runs.artefact(parts[2], parts[3])
                if text is not None:
                    return self._send(text.encode(), "text/plain; charset=utf-8")
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["api", "improvements", "apply"]:
                return self._apply_improvement()
            if parts == ["api", "targets"]:
                return self._add_target() if runs.queued else self._needs_db()
            if parts == ["api", "runs"]:
                return self._submit() if runs.queued else self._needs_db()
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "retro":
                return self._start_retro(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "resume" and runs.queued:
                return self._resume(parts[2])
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["api", "improvements"]:
                return self._delete_improvement()
            if len(parts) == 3 and parts[:2] == ["api", "targets"]:
                if not runs.queued:
                    return self._needs_db()
                if parts[2].isdigit() and runs.delete_target(int(parts[2])):
                    return self._json({"error": None})
                return self._send(json.dumps({"error": "No such target"}).encode(), "application/json",
                                  HTTPStatus.NOT_FOUND)
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "document":
                return self._delete_project_document(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                try:
                    if runs.delete_run(parts[2]):
                        return self._send(b"{}", "application/json")
                except ApplyRefused as e:
                    return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.CONFLICT)
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

        def _needs_db(self) -> None:
            self._send(json.dumps({"error": "Targets and job submission require SF_DATABASE_URL"}).encode(),
                       "application/json", HTTPStatus.CONFLICT)

        def _body(self, *keys: str) -> dict[str, Any]:
            """The JSON object body, with each of `keys` a non-empty string; ValueError otherwise."""
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if not isinstance(body, dict) or any(not isinstance(body.get(k), str) or not body[k].strip() for k in keys):
                raise ValueError(f"{' and '.join(keys)} must be non-empty strings")
            return body

        def _add_target(self) -> None:
            try:
                body = self._body("url")
            except ValueError as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.BAD_REQUEST)
            try:
                target = runs.add_target(body["url"])
            except ValueError as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.CONFLICT)
            except g.GitError as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.BAD_REQUEST)
            self._send(json.dumps({"error": None, "target": target}).encode(), "application/json", HTTPStatus.CREATED)

        def _submit(self) -> None:
            try:
                body = self._body("target", "prompt")
            except ValueError as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.BAD_REQUEST)
            try:
                run_id = runs.submit(body["target"], body["prompt"])
            except LookupError as e:
                status, error = HTTPStatus.NOT_FOUND, str(e)
            except g.GitError as e:  # the target could not be cloned
                status, error = HTTPStatus.BAD_GATEWAY, str(e)
            except (TrunkError, OSError, ValueError) as e:  # its .sf.toml or trunk is unusable
                status, error = HTTPStatus.UNPROCESSABLE_ENTITY, str(e)
            else:
                return self._send(json.dumps({"error": None, "run_id": run_id}).encode(), "application/json",
                                  HTTPStatus.CREATED)
            self._send(json.dumps({"error": error}).encode(), "application/json", status)

        def _resume(self, run_id: str) -> None:
            try:
                job = runs.resume(run_id)
            except ValueError as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.CONFLICT)
            if job is None:
                return self._send(json.dumps({"error": "Run not found"}).encode(), "application/json",
                                  HTTPStatus.NOT_FOUND)
            self._json({"error": None, "job": job})

        def _start_retro(self, run_id: str) -> None:
            try:
                if runs.queued:
                    raise RetroRefused("retro is not available for queued runs in v1")
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

        def _apply_improvement(self) -> None:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or any(not isinstance(body.get(k), str)
                                                     for k in ("ts", "run_id", "title")):
                    raise ValueError("ts, run_id and title must be strings")
            except (ValueError, TypeError) as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.BAD_REQUEST)
            try:
                run_id = runs.apply_improvement((body["ts"], body["run_id"], body["title"]))
            except FileNotFoundError as e:
                status, error = HTTPStatus.NOT_FOUND, str(e)
            except ApplyRefused as e:
                status, error = HTTPStatus.CONFLICT, str(e)
            except (OSError, ValueError, TypeError, RuntimeError, g.GitError, psycopg.Error) as e:
                status, error = HTTPStatus.INTERNAL_SERVER_ERROR, str(e)
            else:
                return self._send(json.dumps({"error": None, "run_id": run_id}).encode(),
                                  "application/json", HTTPStatus.ACCEPTED)
            self._send(json.dumps({"error": error}).encode(), "application/json", status)

        def _delete_improvement(self) -> None:
            status = HTTPStatus.BAD_REQUEST
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or any(not isinstance(body.get(k), str)
                                                     for k in ("ts", "run_id", "title")):
                    raise ValueError("ts, run_id and title must be strings")
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                deleted = runs.delete_improvement((body["ts"], body["run_id"], body["title"]))
            except RuntimeError as e:
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", HTTPStatus.CONFLICT)
            except (OSError, ValueError, psycopg.Error) as e:  # unreadable log, or a body that is not a JSON object
                return self._send(json.dumps({"error": str(e)}).encode(), "application/json", status)
            if not deleted:
                return self._send(json.dumps({"error": "That improvement is no longer in the log."}).encode(),
                                  "application/json", HTTPStatus.NOT_FOUND)
            self._json({"error": None})

        def _delete_project_document(self, run_id: str) -> None:
            status = HTTPStatus.BAD_REQUEST
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if not isinstance(body, dict) or not isinstance(body.get("original"), str):
                    raise TypeError("original must be a string")
                status = HTTPStatus.NOT_FOUND
                name = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
                path = _project_document(runs, run_id, name)
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
                path = _project_document(runs, run_id, name)
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


def serve(runs_dir: Path, host: str, port: int, agents_dir: Path | None = None,
          db_url: str | None = None) -> ThreadingHTTPServer:
    """Serve the dashboard over `runs_dir` files, or over the Postgres store when `db_url` (or SF_DATABASE_URL) is set."""
    agents_dir = agents_dir or runs_dir.parent / "agents"
    factory = agents_dir.parent
    db_url = db_url or os.environ.get("SF_DATABASE_URL")
    runs: FileRuns | DbRuns
    if db_url:
        runs = DbRuns(db_url, factory)
        with store.connect(db_url) as conn:
            store.migrate(conn)
            try:
                recover_queued_applications(conn)
            except (psycopg.Error, KeyError, TypeError):
                logging.getLogger(__name__).exception("Could not recover interrupted improvement executions")
    else:
        runs = FileRuns(runs_dir, factory)
        try:
            recover_applications(factory)
        except (OSError, ValueError, KeyError, TypeError):
            logging.getLogger(__name__).exception("Could not recover interrupted improvement executions")
    return ThreadingHTTPServer((host, port), make_handler(runs, agents_dir))
