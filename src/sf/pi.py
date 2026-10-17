"""Pi subprocess invocation: JSONL streaming, success detection, contract enforcement."""

import os
from pathlib import Path
from typing import Any

from sf.agents import AgentDefinition
from sf.run import Invocation, Run, Usage

# Preserve the original Python imports for callers while execution moves to the shared runner.
from sf.runner import (  # noqa: F401
    MAX_CONTRACT_RETRIES,
    Display,
    StageFailed,
    contract_errors,
    extract_json,
    run_agent,
    stream_process,
)


class PiResult:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.final_text: str = ""
        self.stop_reason: str | None = None
        self.error_message: str | None = None
        self.aborted: bool | None = None
        self.returncode: int | None = None

    @property
    def failure(self) -> str | None:
        if self.returncode != 0:
            return f"pi exited with code {self.returncode}"
        if self.aborted is None:
            return "pi produced no agent_settled event"
        if self.aborted:
            return "agent run aborted"
        if self.stop_reason in ("error", "aborted", "length"):
            return f"final stopReason={self.stop_reason}: {self.error_message or ''}".strip()
        if self.stop_reason is None:
            return "no final assistant message"
        return None


def pi_bin() -> str:
    return os.environ.get("SF_PI", "pi")


def invoke(
    run: Run,
    agent: AgentDefinition,
    inv: Invocation,
    message: str,
    cwd: Path,
    session: str | None,
    display: Display | None,
) -> PiResult:
    body_file = run.dir / "prompts" / f"{agent.name}.md"
    body_file.parent.mkdir(exist_ok=True)
    body_file.write_text(agent.body)
    cmd = [
        pi_bin(),
        "--mode",
        "json",
        "--model",
        agent.model,
        "--thinking",
        agent.thinking,
        "--tools",
        ",".join(agent.tools),
        "--append-system-prompt",
        str(body_file),
        "--session-dir",
        str(run.dir / "sessions"),
    ]
    if session:
        cmd += ["--session", session]
    # Pass the task as an @file, never as argv: agents that run `pkill -f <pattern>` would otherwise
    # match (and kill) their own pi process whenever the task text contains the pattern.
    message_file = run.dir / "prompts" / f"{inv.stage}-{inv.n}-{agent.name}-message.md"
    message_file.write_text(message)
    cmd += ["--", f"@{message_file}"]
    run.log("pi_start", agent=agent.name, invocation=inv.n, session=session, cmd=cmd)

    result = PiResult()

    def track(event: dict[str, Any]) -> list[dict[str, Any]]:
        _track(result, inv, event)
        return [event]

    result.returncode = stream_process(run, inv, cmd, cwd, None, track, display)
    run.log(
        "pi_end",
        agent=agent.name,
        invocation=inv.n,
        returncode=result.returncode,
        stop_reason=result.stop_reason,
        aborted=result.aborted,
        failure=result.failure,
    )
    run.save()
    return result


def _track(result: PiResult, inv: Invocation, event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "session":
        result.session_id = event.get("id")
    elif kind == "message_end":
        msg = event.get("message") or {}
        if msg.get("role") == "assistant":
            inv.usage += Usage.from_pi(msg.get("usage") or {})
            result.stop_reason = msg.get("stopReason")
            result.error_message = msg.get("errorMessage")
            text = "".join(c.get("text", "") for c in msg.get("content", []) if c.get("type") == "text")
            if text.strip():
                result.final_text = text
    elif kind == "agent_settled":
        result.aborted = bool(event.get("aborted"))
