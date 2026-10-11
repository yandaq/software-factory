"""Read-only local web dashboard over `runs/*/` (stdlib HTTP server, polled JSON API)."""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STATIC = Path(__file__).parent / "static"
ARTEFACT_PREFIXES = ("prompt.md", "plan.md", "review-", "validation-", "pi-")
EVENT_TAIL = 200


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
    artefacts = sorted(p.name for p in run_dir.iterdir() if p.is_file() and p.name.startswith(ARTEFACT_PREFIXES))
    return {"state": state, "events": events, "tools": tools, "artefacts": artefacts}


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


def make_handler(runs_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if not parts:
                return self._send((STATIC / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "runs"]:
                return self._json(list_runs(runs_dir))
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._json(run_detail(runs_dir, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                text = artefact(runs_dir, parts[2], parts[3])
                if text is not None:
                    return self._send(text.encode(), "text/plain; charset=utf-8")
            self.send_error(HTTPStatus.NOT_FOUND)

        def _json(self, data: Any) -> None:
            if data is None:
                return self.send_error(HTTPStatus.NOT_FOUND)
            self._send(json.dumps(data).encode(), "application/json")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep the terminal quiet; errors still surface via send_error responses

    return Handler


def serve(runs_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(runs_dir))
