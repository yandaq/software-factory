#!/usr/bin/env python3
"""Fake `pi --mode json` driven by a JSON script at $FAKE_PI_SCRIPT.

Script: {"<agent>": [step, ...]}; each call consumes the agent's next step (the last repeats).
step: {"text": str, "write": {path: content}, "stop": str, "aborted": bool, "exit": int}
Every call's argv, and the task message read from its trailing @file, is appended to
$FAKE_PI_SCRIPT.calls.jsonl.
"""

import json
import os
import sys
import uuid
from pathlib import Path

argv = sys.argv[1:]
agent = Path(argv[argv.index("--append-system-prompt") + 1]).stem
script_path = Path(os.environ["FAKE_PI_SCRIPT"])
script = json.loads(script_path.read_text())
calls = script_path.with_suffix(".calls.jsonl")
prior = [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []
n = sum(1 for c in prior if c["agent"] == agent)
with calls.open("a") as f:
    message = Path(argv[-1].removeprefix("@")).read_text()
    f.write(json.dumps({"agent": agent, "argv": argv, "message": message, "cwd": os.getcwd()}) + "\n")
steps = script[agent]
step = steps[min(n, len(steps) - 1)]
for path, content in step.get("write", {}).items():
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)

session = argv[argv.index("--session") + 1] if "--session" in argv else str(uuid.uuid4())
usage = {
    "input": 10,
    "output": 5,
    "cacheRead": 2,
    "cacheWrite": 1,
    "totalTokens": 18,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0.01},
}
events = [
    {"type": "session", "version": 3, "id": session, "cwd": os.getcwd()},
    {"type": "agent_start"},
    {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "args": {"path": "x"}},
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": step.get("stop", "stop"),
            "usage": usage,
            "content": [{"type": "text", "text": step.get("text", "")}],
        },
    },
    {"type": "agent_end", "messages": [], "willRetry": False},
    {"type": "agent_settled", "aborted": step.get("aborted", False)},
]
for e in events:
    print(json.dumps(e), flush=True)
print("fake pi stderr", file=sys.stderr)
sys.exit(step.get("exit", 0))
