"""Codex exec adapter: role-based sandbox, JSONL activity and per-turn token totals."""

import json
import os
from pathlib import Path
from typing import Any

from sf.agents import AgentDefinition
from sf.run import Invocation, Run, Usage
from sf.runner import AgentResult, Display, stream_process


class CodexResult(AgentResult):
    def __init__(self, inv: Invocation):
        super().__init__("codex")
        self.inv = inv
        self.seen_tools: set[str] = set()

    def track(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "thread.started":
            self.session_id = event["thread_id"]
        elif kind in ("turn.failed", "error"):
            self.error = str(event.get("error") or event.get("message") or "Codex execution failed")
            return [{"type": "agent_error", "detail": self.error}]
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item") or {}
            type_ = item.get("type")
            if type_ == "agent_message" and kind == "item.completed":
                self.final_text = item.get("text", "")
                return [{"type": "message_end", "message": {"role": "assistant", "usage": {}}}]
            if type_ in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                id_ = item["id"]
                activity = []
                if id_ not in self.seen_tools:
                    self.seen_tools.add(id_)
                    activity.append({"type": "tool_execution_start", "toolCallId": id_, "toolName": type_,
                                     "args": {k: v for k, v in item.items()
                                              if k not in ("id", "type", "aggregated_output")}})
                if kind == "item.completed":
                    activity.append({"type": "tool_execution_end", "toolCallId": id_,
                                     "status": item.get("status"), "exitCode": item.get("exit_code"),
                                     "error": item.get("error")})
                return activity
        elif kind == "turn.completed" and not self.completed:
            native = event.get("usage") or {}
            fields = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cached_input_tokens"}
            usage = Usage(**{field: native[key] for field, key in fields.items() if key in native},
                          known=[field for field, key in fields.items() if key in native],
                          unavailable=["cache_write", "cost"] + [f for f, k in fields.items() if k not in native])
            # Codex input includes cached tokens; Pi/Claude input counts uncached tokens.
            if "input_tokens" in native and "cached_input_tokens" in native:
                usage.input = max(0, usage.input - usage.cache_read)
            elif "input" not in usage.unavailable:
                usage.unavailable.append("input")
                usage.known.remove("input")
            first = not self.inv.usage.known
            self.inv.usage += usage
            if first:
                self.inv.usage.unavailable = usage.unavailable
            self.completed = True
            return [{"type": "agent_settled", "aborted": False}]
        return []


def invoke(run: Run, agent: AgentDefinition, inv: Invocation, message: str, cwd: Path,
           session: str | None, display: Display | None) -> CodexResult:
    prompts = run.dir / "prompts"
    prompts.mkdir(exist_ok=True)
    # Store role instructions separately; only a file path appears in the process arguments.
    body = prompts / f"{agent.name}-codex.md"
    body.write_text(agent.body)
    task = prompts / f"{inv.stage}-{inv.n}-{agent.name}-message.md"
    task.write_text(f"Read and follow the agent role instructions in {body}.\n\n{message}")
    writable = bool({"edit", "write"} & set(agent.tools))
    sandbox = "workspace-write" if writable else "read-only"
    cmd = [os.environ.get("SF_CODEX", "codex"), "exec"]
    if session:
        cmd += ["resume"]
    cmd += ["--json", "--model", agent.model, "-c", f'model_reasoning_effort={json.dumps(agent.thinking)}',
            "-c", f'sandbox_mode={json.dumps(sandbox)}', "-c", 'approval_policy="never"',
            "-c", f'developer_instructions={json.dumps(f"Read and follow the agent role instructions in {body}.")}']
    if agent.name == "improvement-implementer":
        # Resume's CLI has no --add-dir flag; the config override works for both forms.
        cmd += ["-c", f"sandbox_workspace_write.writable_roots={json.dumps([str(run.target)])}"]
    if session:
        cmd += [session]
    cmd += ["-"]
    result = CodexResult(inv)
    result.session_id = session
    run.log("agent_command", agent=agent.name, invocation=inv.n, harness=agent.harness, cmd=cmd)
    result.returncode = stream_process(run, inv, cmd, cwd, task, result.track, display)
    return result
