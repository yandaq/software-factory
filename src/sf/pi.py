"""Pi subprocess invocation: JSONL streaming, success detection, contract enforcement."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from sf.agents import AgentDefinition
from sf.run import Invocation, Run, Usage

MAX_CONTRACT_RETRIES = 2


class StageFailed(Exception):
    pass


class Display(Protocol):
    def pi_event(self, inv: Invocation, event: dict[str, Any]) -> None: ...


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


def _invoke(
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
    cmd += ["--", message]
    run.log("pi_start", agent=agent.name, invocation=inv.n, session=session, cmd=cmd[:-1])

    result = PiResult()
    stderr_path = run.dir / f"pi-{inv.stage}-{inv.n}.stderr"
    with stderr_path.open("a") as stderr:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=stderr)
        assert proc.stdout is not None
        for raw in proc.stdout:  # binary stream: split on LF only
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                run.log("pi_bad_line", line=line.decode(errors="replace"))
                continue
            run.emit("pi", {"agent": agent.name, "invocation": inv.n, "event": event})
            _track(result, inv, event)
            if display:
                display.pi_event(inv, event)
        result.returncode = proc.wait()
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


def extract_json(text: str) -> Any:
    """Return the last JSON object in `text` (bare or fenced)."""
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            found, end = decoder.raw_decode(text, i)
            i = text.find("{", end)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
    if found is None:
        raise ValueError("no JSON object found in final assistant message")
    return found


def contract_errors(contract: dict[str, Any], text: str) -> list[str]:
    try:
        data = extract_json(text)
    except ValueError as e:
        return [str(e)]
    validator = Draft202012Validator(contract)
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(data)]


def run_agent(
    run: Run,
    agent: AgentDefinition,
    task: str,
    cwd: Path,
    display: Display | None = None,
) -> dict[str, Any]:
    """Run `agent` on `task`; return its contract-valid JSON output or raise StageFailed."""
    inv = run.next_invocation(agent.name)
    instructions = (
        f"{task}\n\nWhen finished, end your final message with a single JSON object that "
        f"validates against this JSON Schema:\n```json\n{json.dumps(agent.contract, indent=2)}\n```"
    )
    result = _invoke(run, agent, inv, instructions, cwd, None, display)
    for attempt in range(MAX_CONTRACT_RETRIES + 1):
        if result.failure:
            raise StageFailed(f"{agent.name}: {result.failure}")
        errors = contract_errors(agent.contract, result.final_text)
        if not errors:
            inv.ok = True
            run.save()
            return extract_json(result.final_text)
        run.log("contract_invalid", agent=agent.name, attempt=attempt, errors=errors)
        if attempt == MAX_CONTRACT_RETRIES:
            break
        retry = (
            "Your final response did not satisfy the required JSON contract:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\nReply with only the corrected JSON object."
        )
        result = _invoke(run, agent, inv, retry, cwd, result.session_id, display)
    raise StageFailed(f"{agent.name}: contract still invalid after {MAX_CONTRACT_RETRIES} re-prompts")
