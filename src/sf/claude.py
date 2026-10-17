"""Claude Code CLI adapter: native tool allowlist, streamed activity and turn usage."""

import json
import os
from pathlib import Path
from typing import Any

from sf.agents import AgentDefinition
from sf.run import Invocation, Run, Usage
from sf.runner import AgentResult, Display, stream_process

TOOLS = {"read": "Read", "grep": "Grep", "find": "Glob", "ls": "Glob",
         "bash": "Bash", "edit": "Edit", "write": "Write"}
TOKEN_FIELDS = {"input": "input_tokens", "output": "output_tokens",
                "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}


class ClaudeResult(AgentResult):
    def __init__(self, inv: Invocation):
        super().__init__("claude-code")
        self.inv = inv
        self.seen_results: set[str] = set()
        self.seen_tools: set[str] = set()

    def track(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if event.get("session_id"):
            self.session_id = event["session_id"]
        if kind == "assistant":
            msg = event.get("message") or {}
            tools = []
            for block in msg.get("content", []):
                if block.get("type") == "tool_use" and block["id"] not in self.seen_tools:
                    self.seen_tools.add(block["id"])
                    tools.append({"type": "tool_execution_start", "toolCallId": block["id"],
                                  "toolName": block["name"], "args": block.get("input", {})})
            return tools
        if kind == "stream_event":
            return [{"type": "message_update"}]
        if kind == "user":
            return [{"type": "tool_execution_end", "toolCallId": block["tool_use_id"],
                     "isError": block.get("is_error", False), "result": block.get("content")}
                    for block in (event.get("message") or {}).get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_result"]
        if kind == "system" and event.get("subtype") == "permission_denied":
            self.error = f"permission denied: {event.get('tool_name') or event}"
            return [{"type": "permission_denied", "detail": event}]
        if kind != "result":
            return []
        key = event.get("uuid") or "result"
        if key in self.seen_results:
            return []
        self.seen_results.add(key)
        self.completed = event.get("subtype") == "success" and not event.get("is_error")
        if not self.completed:
            self.error = str(event.get("errors") or event.get("result") or event.get("subtype"))
        if event.get("permission_denials"):
            self.error = f"permission denied: {event['permission_denials']}"
        self.final_text = event.get("result") or ""
        if event.get("structured_output") is not None:
            self.final_text = json.dumps(event["structured_output"])
        native = event.get("usage") or {}
        usage = Usage(**{field: native[key] for field, key in TOKEN_FIELDS.items() if key in native},
                      known=[field for field, key in TOKEN_FIELDS.items() if key in native],
                      unavailable=[field for field, key in TOKEN_FIELDS.items() if key not in native])
        # Result usage covers this turn; cost is a cumulative estimate for the resumed session.
        if event.get("total_cost_usd") is None:
            usage.unavailable.append("cost")
        else:
            usage.known.append("cost")
        first = not self.inv.usage.known
        self.inv.usage += usage
        if first:
            self.inv.usage.unavailable = usage.unavailable
        if event.get("total_cost_usd") is not None:
            self.inv.usage.cost = event["total_cost_usd"]
            self.inv.usage.estimated = True
        return [{"type": "message_end", "message": {"role": "assistant", "usage": {}}},
                {"type": "agent_settled", "aborted": not self.completed, "error": self.error}]


def invoke(run: Run, agent: AgentDefinition, inv: Invocation, message: str, cwd: Path,
           session: str | None, display: Display | None) -> ClaudeResult:
    prompts = run.dir / "prompts"
    prompts.mkdir(exist_ok=True)
    definitions = prompts / f"{agent.name}-claude.json"
    tools = sorted({TOOLS[t] for t in agent.tools})
    definitions.write_text(json.dumps({agent.name: {"description": agent.purpose, "prompt": agent.body,
                                                   "tools": tools}}))
    task = prompts / f"{inv.stage}-{inv.n}-{agent.name}-message.md"
    task.write_text(message)
    cmd = [os.environ.get("SF_CLAUDE", "claude"), "--print", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--agents", str(definitions), "--agent", agent.name,
           "--model", agent.model, "--effort", agent.thinking, "--tools", ",".join(tools),
           "--allowedTools", ",".join(tools), "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands"]
    if session:
        cmd += ["--resume", session]
    result = ClaudeResult(inv)
    result.session_id = session
    run.log("agent_command", agent=agent.name, invocation=inv.n, harness=agent.harness, cmd=cmd)
    result.returncode = stream_process(run, inv, cmd, cwd, task, result.track, display)
    return result
