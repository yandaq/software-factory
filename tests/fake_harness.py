#!/usr/bin/env python3
"""Scripted native CLI fake, preserving actual stdin/argv and session-resume boundaries."""

import json
import os
import re
import sys
import uuid
from pathlib import Path

argv = sys.argv[1:]
claude = "--agents" in argv
if claude:
    agent = next(iter(json.loads(Path(argv[argv.index("--agents") + 1]).read_text())))
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
else:
    instructions = json.loads(next(a.split("=", 1)[1] for a in argv if a.startswith("developer_instructions=")))
    path = re.search(r" in (.+)\.$", instructions)[1]
    agent = Path(path).name.removesuffix("-codex.md")
    session = argv[-2] if "resume" in argv else str(uuid.uuid4())
script_path = Path(os.environ["FAKE_HARNESS_SCRIPT"])
script = json.loads(script_path.read_text())
calls = script_path.with_suffix(".calls.jsonl")
prior = [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []
n = sum(c["agent"] == agent for c in prior)
message = sys.stdin.read()
with calls.open("a") as f:
    f.write(json.dumps({"agent": agent, "argv": argv, "message": message, "cwd": os.getcwd(), "session": session}) + "\n")
step = script[agent][min(n, len(script[agent]) - 1)]
for path, content in step.get("write", {}).items():
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
text = step.get("text", "")
if claude:
    events = [
        {"type": "system", "subtype": "init", "session_id": session},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "README.md"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": text,
         "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2,
                   "cache_creation_input_tokens": 1}, "total_cost_usd": (n + 1) * .01},
    ]
else:
    events = [
        {"type": "thread.started", "thread_id": session}, {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "t1", "type": "command_execution", "command": "cat README.md"}},
        {"type": "item.completed", "item": {"id": "t1", "type": "command_execution", "exit_code": 0,
                                              "status": "completed", "command": "cat README.md"}},
        {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 5}},
    ]
if "events" in step:
    events = step["events"]
if step.get("bad_line"):
    print("bad JSON")
    print("[]")
for e in events:
    print(json.dumps(e), flush=True)
print("fake native stderr", file=sys.stderr)
sys.exit(step.get("exit", 0))
