"""Read native CLI model catalogs without starting an inference turn."""

import json
import os
import selectors
import subprocess
import tempfile
import time
from typing import Any


class _CatalogProcess:
    """Bounded JSONL control exchange; stderr is retained for failure diagnostics."""

    def __init__(self, process: subprocess.Popen, timeout: float):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.buffer = b""
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)

    def send(self, message: dict[str, Any]) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("model discovery timed out")
        while b"\n" not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("model discovery timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError("CLI exited before returning a model catalog")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        if not isinstance(message, dict):
            raise TypeError("expected a JSON control message")
        return message

    def rpc(self, id_: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"id": id_, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") != id_:
                continue
            if "error" in message:
                raise ValueError(str(message["error"]))
            return message["result"]


def _codex_models(channel: _CatalogProcess) -> list[str]:
    channel.rpc(1, "initialize", {"clientInfo": {"name": "sf", "version": "0.1.0"}})
    channel.send({"method": "initialized"})
    models, cursor, seen = [], None, set()
    id_ = 2
    while True:
        page = channel.rpc(id_, "model/list", {"limit": 100, "cursor": cursor, "includeHidden": False})
        models.extend(row["model"] for row in page["data"] if not row.get("hidden"))
        cursor = page.get("nextCursor")
        if not cursor:
            return models
        if cursor in seen:
            raise ValueError("model catalog repeated a pagination cursor")
        seen.add(cursor)
        id_ += 1


def _claude_models(channel: _CatalogProcess) -> list[str]:
    channel.send({"type": "control_request", "request_id": "sf-models", "request": {"subtype": "initialize"}})
    while True:
        message = channel.receive()
        if message.get("type") != "control_response":
            continue
        response = message["response"]
        if response.get("request_id") != "sf-models":
            continue
        if response.get("subtype") != "success":
            raise ValueError(str(response.get("error", "initialization failed")))
        return [row["value"] for row in response["response"]["models"]]


def list_native_models(harness: str, timeout: float = 30) -> tuple[list[str], str | None]:
    if harness == "codex":
        cmd = [os.environ.get("SF_CODEX", "codex"), "app-server"]
        discover = _codex_models
    else:
        cmd = [os.environ.get("SF_CLAUDE", "claude"), "--print", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--strict-mcp-config", "--mcp-config",
               '{"mcpServers":{}}', "--disable-slash-commands"]
        discover = _claude_models
    with tempfile.TemporaryFile() as stderr:
        try:
            with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr) as process:
                channel = _CatalogProcess(process, timeout)
                try:
                    models = list(dict.fromkeys(discover(channel)))
                    if not models or any(not isinstance(m, str) or not m for m in models):
                        raise ValueError("CLI returned an empty or invalid model catalog")
                    return models, None
                finally:
                    channel.selector.close()
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
            stderr.seek(0)
            detail = stderr.read().decode(errors="replace").strip()[-2000:]
            return [], f"{harness} model discovery failed: {e}" + (f"; {detail}" if detail else "")
