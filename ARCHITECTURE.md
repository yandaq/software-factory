# Architecture

| Module | Responsibility |
|---|---|
| `cli.py` | Typer commands; lockfile; maps outcomes to exit codes |
| `workflow.py` | Stage driver, per-stage checkpoints (factory commit of `runs/<id>`), stages branch → merge |
| `improve.py` | Improve phase: improver ideas, human decisions, `improvements.jsonl`, scope enforcement |
| `pi.py` | Pi subprocess, JSONL parsing, success detection, contract validation + re-prompts |
| `run.py` | `RunState` (Pydantic), `events.jsonl` emitter, token aggregation, `RunLock` |
| `agents.py` | Agent definition parsing/validation |
| `target.py` | `.sf.toml`, starter standards, validation commands |
| `git.py` | git wrapper |
| `dashboard.py` + `static/dashboard.html` | Read-only stdlib HTTP server and polling page over `runs/*/`; no state of its own |
| `console.py` | Rich live display and terminal prompts (`TerminalHuman`) |

`Workflow` depends on a `Human` and a display protocol, so tests substitute scripted fakes.

## Compromises

- **Scope rules are enforced after the fact.** Pi has no per-path write restriction, so the
  orchestrator inspects `git status` after the documenter and improvement-implementer. Documenter
  violations are reverted and fail the stage; improvement-implementer violations are reverted and
  logged (`scope_violation`) while in-scope edits are kept.
- **Resume granularity is a stage, or a loop inside implement.** An interrupted loop counts as used
  and the next loop starts fresh; an interrupted stage is re-run from its start.
- **Agents return documents inside their JSON contract** (e.g. `{"plan": ...}`) and the orchestrator
  writes `plan.md` / `review-<n>.md`, so read-only agents need no write tools.
