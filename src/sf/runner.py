"""Harness-neutral invocation lifecycle, subprocess streaming and JSON contracts."""

import json
import os
import subprocess
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from sf.agents import AgentDefinition
from sf.run import Invocation, Run
from sf.target import NO_REPO_ARTIFACTS

MAX_CONTRACT_RETRIES = 2


class StageFailed(Exception):
    pass


class Display(Protocol):
    def agent_event(self, inv: Invocation, event: dict[str, Any]) -> None: ...


def emit_event(run: Run, inv: Invocation, event: dict[str, Any], display: Display | None) -> None:
    run.emit("agent", {"agent": inv.agent, "invocation": inv.n, "harness": inv.harness, "event": event})
    if display:
        callback = getattr(display, "agent_event", None) or getattr(display, "pi_event", None)
        if callback is None:
            raise TypeError("display must implement agent_event or pi_event")
        callback(inv, event)


def stream_process(run: Run, inv: Invocation, cmd: list[str], cwd: Path, message_file: Path | None,
                   track: Callable[[dict[str, Any]], list[dict[str, Any]]], display: Display | None) -> int:
    """Preserve native stdout verbatim; decode only JSON objects, with malformed output observable."""
    prefix = f"{inv.harness}-{inv.stage}-{inv.n}"
    with (
        message_file.open("rb") if message_file else nullcontext(None) as stdin,
        (run.dir / f"{prefix}.stderr").open("a") as stderr,
        (run.dir / f"{prefix}.jsonl").open("ab") as native,
    ):
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr,
                                env={**os.environ, **NO_REPO_ARTIFACTS})
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                native.write(raw)
                native.flush()
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise TypeError("event is not a JSON object")
                except (ValueError, TypeError, UnicodeDecodeError) as e:
                    run.log("pi_bad_line" if inv.harness == "pi" else "agent_bad_line",
                            agent=inv.agent, harness=inv.harness,
                            invocation=inv.n, error=str(e), line=raw.decode(errors="replace"))
                    continue
                for normalized in track(event):
                    emit_event(run, inv, normalized, display)
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
            proc.wait()
            raise
        proc.wait()
    return proc.returncode


def _invoke(run: Run, agent: AgentDefinition, inv: Invocation, message: str, cwd: Path,
            session: str | None, display: Display | None):
    from sf import claude, codex, pi

    adapter = {"pi": pi, "claude-code": claude, "codex": codex}[agent.harness]
    run.log("agent_start", agent=agent.name, invocation=inv.n, harness=agent.harness,
            model=agent.model, thinking=agent.thinking, session=session)
    try:
        result = adapter.invoke(run, agent, inv, message, cwd, session, display)
    except (OSError, ValueError, KeyError, TypeError) as e:
        failure = f"{agent.harness}: {e}"
        run.log("agent_end", agent=agent.name, invocation=inv.n, harness=agent.harness, failure=failure)
        run.save()
        raise StageFailed(f"{agent.name}: {failure}") from e
    inv.session_id = result.session_id
    run.log("agent_end", agent=agent.name, invocation=inv.n, harness=agent.harness,
            session=result.session_id, returncode=result.returncode, failure=result.failure)
    run.save()
    return result


class AgentResult:
    def __init__(self, harness: str):
        self.harness = harness
        self.session_id: str | None = None
        self.final_text = ""
        self.returncode: int | None = None
        self.completed = False
        self.error: str | None = None

    @property
    def failure(self) -> str | None:
        if self.returncode != 0:
            return f"{self.harness} exited with code {self.returncode}: {self.error or 'see stderr'}"
        if self.error:
            return self.error
        if not self.completed:
            return f"{self.harness} produced no successful completion event"
        if not self.final_text.strip():
            return "no final assistant message"
        return None


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
    agent = agent.model_copy(deep=True)  # pin settings through contract corrections
    inv = run.next_invocation(agent.name)
    inv.harness, inv.model, inv.thinking = agent.harness, agent.model, agent.thinking
    if agent.harness != "pi":
        inv.usage.unavailable = ["input", "output", "cache_read", "cache_write", "cost"]
        inv.usage.known = []
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
        if not result.session_id:
            raise StageFailed(f"{agent.name}: cannot correct contract without a session ID")
        result = _invoke(run, agent, inv, retry, cwd, result.session_id, display)
    raise StageFailed(f"{agent.name}: contract still invalid after {MAX_CONTRACT_RETRIES} re-prompts")
