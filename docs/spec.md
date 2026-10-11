# Software Factory — Specification

A Python orchestrator that drives the Pi CLI coding agent through a fixed workflow:
**prompt → plan → implement ⇄ review → document → merge → improve**.

## 1. Repositories

- **Factory repo** (this repo): orchestrator code, agent definitions, starter templates, run logs.
  - Trunk is `main`.
- **Target repo**: the software being built. It is passed with `--target <path>`.
  - Optional `<target>/.sf.toml` holds per-target settings: `trunk` (default `main`) and validation overrides.
  - It contains `AGENTS.md` at the root; every other doc lives in `docs/`, including `docs/coding-standards.md`, `docs/architecture.md` and `docs/VALIDATION.md`.
- If any standards file is missing from the target, the run adds it on the run branch: `AGENTS.md` and `docs/VALIDATION.md` are downloaded from https://github.com/yandaq/agents-default; `docs/coding-standards.md` and `docs/architecture.md` are copied from the factory's starter templates.

## 2. Stack

- `uv` project
- Typer CLI
- Pydantic (definitions, contracts, state)
- PyYAML (frontmatter)
- Rich (live console)
- pytest
- Pi is invoked through `subprocess`, and its stdout is read line by line as JSONL.

## 3. CLI

```
sf run --target <path> "<prompt>" | --prompt-file <file> [--approve-plan]
sf resume <run-id>
sf improve <run-id>        # re-enter the improve phase
```

- Runs are serial and guarded by a lockfile.

## 4. Agent definitions

Each agent is defined in `agents/<name>.md`: YAML frontmatter followed by a markdown body that is the system prompt.

```yaml
---
name: reviewer
purpose: Review implementation against coding standards
model: anthropic/claude-sonnet-...
thinking: high            # off|minimal|low|medium|high|xhigh|max
tools: [read, grep, find, ls]
contract:                 # JSON Schema for the final response
  type: object
  required: [verdict, findings]
  properties: {...}
---
<system prompt>
```

Agents: `planner`, `implementer`, `reviewer`, `documenter`, `improver`, `improvement-implementer`.

## 5. Pi invocation

```
pi --mode json --model <model> --thinking <thinking> --tools <tools> \
   --append-system-prompt <body-file> --session-dir runs/<id>/sessions/ "<task>"
```

- **Completion:** Pi exits 0 in JSON mode even when the agent run fails. Success is therefore decided from the final events: `stopReason` and `agent_settled.aborted`. A nonzero exit is also treated as failure.
- **Contract:** the final assistant message must contain JSON that validates against the agent's contract. If it is invalid, the orchestrator re-prompts in the same session (`--session`) with the validation errors, at most **2 times**, and then fails the stage.
- **Limits:** there are **no timeouts and no budget limits**.

## 6. Workflow

1. **Branch.** Create a new branch `sf/<run-id>` in the target, off the trunk.
2. **Prompt.** Save the prompt to `runs/<id>/prompt.md`.
3. **Plan.** The planner produces `plan.md`. With `--approve-plan`, a human approves the plan before the run continues.
4. **Implement ⇄ Review**, for up to **5 loops**:
   - **Implementer.** Starts a fresh session each loop, with the plan, the latest review findings and the latest validation output. It follows **no** coding standards and simply delivers the planned work. Commit.
   - **Validation.** The orchestrator runs the commands listed in `docs/VALIDATION.md`. If there are none, validation is skipped and the skip is logged. On failure, control returns to the implementer without running the reviewer, and this counts as a loop.
   - **Reviewer.** Reviews the work against `docs/coding-standards.md`. It returns `{verdict: approve|changes_requested, findings[]}`, which is written to `review-<n>.md`.
   - **Cap reached.** If the reviewer has not approved after 5 loops, a human chooses: merge anyway, abort, or allow N more loops. This outcome is flagged to the improve phase.
5. **Document.** The documenter diffs `trunk...branch`. It updates existing docs and may create new docs only under `docs/`. It must not touch code or the standards files. Commit.
6. **Merge.** Local `git merge --no-ff` into the trunk. If there is a conflict, the run aborts.
7. **Improve** (human in the loop):
   - The improver analyses the generated code, all run logs, and the past decisions recorded in `improvements.jsonl`. Ideas that were previously rejected must not be proposed again.
   - Each idea is presented in the terminal with its title, rationale, evidence and proposed change. The human chooses **[a]pprove / [s]kip / [r]eject**, with an optional note. Every decision is recorded in `improvements.jsonl`.
   - Each approved idea is implemented by a new `improvement-implementer` Pi run.
     - **Allowed to change:** `agents/*.md` (committed in the factory repo, with the run-id in the commit message) and the target's `AGENTS.md`, `docs/VALIDATION.md`, `docs/coding-standards.md` and `docs/architecture.md` (a follow-up commit on the target trunk).
     - **Not allowed to change:** the orchestrator code.

Run state is saved to `runs/<id>/run-state.json` as each stage completes.

## 7. Observability

`runs/<run-id>/` is git-ignored (never committed) and contains:

- `events.jsonl`: one stream holding every Pi event and every orchestrator event, in real time. Each line is tagged with `run_id`, `stage`, `loop` and `ts`.
- `sessions/`: Pi session transcripts.
- `pi-<stage>-<n>.stderr`
- `prompt.md`, `plan.md`, `review-<n>.md`, `validation-<n>.log`
- `run-state.json`: the current stage, the loop number, the result of each stage, and token totals.

**Token accounting:**

- Input, output, cache-read, cache-write and cost (where Pi reports them) are captured from Pi usage events.
- They are recorded per Pi invocation and aggregated per stage, per loop and per run.
- They are shown live in the Rich console.

## 8. Resume

The per-stage commits plus `run-state.json` act as checkpoints. `sf resume <id>` continues from the next incomplete stage.
