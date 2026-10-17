# Software Factory — Specification

A Python orchestrator that drives the Pi CLI coding agent through a fixed workflow:
**prompt → plan → implement ⇄ review → document → merge → improve**.

## 1. Repositories

- **Factory repo** (this repo): orchestrator code, agent definitions, starter templates, run logs.
  - Trunk is `main`.
  - Its architecture record is `ARCHITECTURE.md`; `docs/architecture.md` points there, including when the
    factory is used as its own target.
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
sf watch --target <path> <folder> [--approve-plan]
sf resume <run-id>
sf improve <run-id>        # re-enter the improve phase
```

- **Retro.** The same phase can be re-entered from the dashboard: each merged run has a **retro** button
  that starts `sf improve <run-id>` as a child process of the dashboard server, so the improvement agent
  can be pointed at a historical run again. The CLI rule applies unchanged (an unmerged run is refused)
  and a retro that cannot start because another run holds the lockfile is reported in the browser.

- Runs are serial and guarded by a lockfile.
- **Watch folder.** `<folder>` replaces the prompt argument: the command creates `todo/`, `doing/` and
  `done/` inside it and monitors `todo/` until Ctrl+C, which exits cleanly. Each regular Markdown file
  (case-insensitive `.md`) directly in `todo/` becomes one run's prompt, oldest modification time first
  with ties broken by name; other files, subdirectories and files still being written wait in `todo/`.
  A claimed file moves to `doing/` and reaches `done/` only when its run succeeds: a failed or aborted
  run leaves it in `doing/` (no automatic retry) and the watch continues with the next file. The lock is
  taken per run and released in between, so `sf resume` fits between prompts and a busy lock leaves
  prompts in `todo/`. `sf run`, `sf resume`, `sf improve` and `sf dashboard` are unchanged.
  `<folder>` is created when missing; an existing file in its place is a usage error. The target is
  checked once before watching — a git repository with a resolvable trunk — and an unusable one exits 1
  without creating the queue. Polling (~2 s) and the settle window (~1 s) are module constants with no
  CLI flag. A name already used in `doing/` or `done/` is suffixed (`fix.md` → `fix-1.md`).

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

Agents: `planner`, `implementer`, `reviewer`, `documenter`, `improver`.

Each agent also has a required `agents/<name>.user.md` task template. All task templates include
`{{documentation}}`: the target repository path and a JSON inventory of repository-relative
Markdown paths. Discovery runs before each new invocation using Git's tracked and non-ignored
untracked files throughout the target, including DESIGN.md and component-local guidance.
Only existing files with case-insensitive .md extensions are included; symlinks and deleted
files are excluded. Agents read applicable docs and references and respect local instruction
scope; historical records are not automatically requirements. Correction retries retain the
original invocation context, and rendered task artifacts preserve the supplied inventory.

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
3. **Plan.** The planner produces `plan.md`. Acceptance criteria and out-of-scope lists scope the implementation; documentation left for the document stage is recorded as guidance, not excluded by the plan, unless the request itself forbids touching documentation. With `--approve-plan`, a human approves the plan before the run continues. The approved plan is then committed to the target branch as `docs/plans/<run-id>.md` before implementation starts.
4. **Implement ⇄ Review**, for up to **5 loops**:
   - **Implementer.** Starts a fresh session each loop, with the plan, the latest review findings and the latest validation output. It follows **no** coding standards and simply delivers the planned work. Commit.
   - **Validation.** The orchestrator runs the commands listed in `docs/VALIDATION.md`. If there are none, validation is skipped and the skip is logged. On failure, control returns to the implementer without running the reviewer, and this counts as a loop.
   - **Reviewer.** Reviews the work against `docs/coding-standards.md`. It returns `{verdict: approve|changes_requested, findings[]}`, which is written to `review-<n>.md`.
   - **Cap reached.** If the reviewer has not approved after 5 loops, a human chooses: merge anyway, abort, or allow N more loops. This outcome is flagged to the improve phase.
5. **Document.** The documenter diffs `trunk...branch`. It reduces drift between implemented code and docs, updating existing documentation including `README.md`, `AGENTS.md`, `docs/VALIDATION.md`, `docs/coding-standards.md` and `docs/architecture.md` when factual guidance changes. Silence counts as drift: where the diff makes information necessary and the document that naturally holds it — usually `README.md` — omits it, the documenter adds it. It preserves normative requirements and reports conflicts rather than weakening standards. New docs are restricted to `docs/`; code and runtime configuration are outside its scope. The orchestrator commits the documentation changes.
6. **Merge.** Local `git merge --no-ff` into the trunk. If there is a conflict, the run aborts.
7. **Improve** (suggestions only):
   - The improver analyses the generated code, all run logs, and the past decisions recorded in `improvements.jsonl`. Ideas that were previously rejected must not be proposed again.
   - Every suggestion that survives historical rejection filtering is recorded in `improvements.jsonl` with `decision: automatic` and an empty note, and only that file is committed. Selection records what was proposed, not what was applied. Historical decisions remain readable. There is no terminal approve/skip/reject prompt or human note.
   - Each idea names exactly one existing Markdown file in its required `file` field: `agents/<name>.md` or `agents/<name>.user.md` relative to the factory, or a path copied from the target-documentation inventory. The JSON contract rejects a missing path, an absolute path, a `..` segment and a non-Markdown path, so the dashboard can offer the idea as one edit to one file.
   - No agent applies a suggestion. Shared factory instructions must work on unrelated targets, so project-specific rules belong in target documentation; applying an idea means editing the prompt, template or target document it names in the dashboard.
   - A re-run (the CLI or a dashboard retro) analyses the same evidence again and only previously **rejected** titles are filtered out, so it can record an idea that was already recorded. Recording what was proposed stays the improver's judgement; nothing is applied either way.

Run state is saved to `runs/<id>/run-state.json` as each stage completes. Run ids are
`YYYYMMDD-HHMMSS`, suffixed `-N` when `runs/<id>` already exists: two runs started inside one second —
which the watch monitor makes easy — must not share a run directory, state file or event stream.

## 7. Observability

`runs/<run-id>/` is git-ignored (never committed) and contains:

- `events.jsonl`: one stream holding every Pi event and every orchestrator event, in real time. Each line is tagged with `run_id`, `stage`, `loop` and `ts`.
- `sessions/`: Pi session transcripts.
- `pi-<stage>-<n>.stderr`
- `prompt.md`, `plan.md`, `review-<n>.md`, `validation-<n>.log`
- `retro.log`: the console output of the latest dashboard retro of this run, downloadable as an artefact.
- `run-state.json`: the current stage, the loop number, the result of each stage, and token totals.

Watched prompts also leave `sf`-source events in the run they produced: `mailbox_claimed` (file name,
watch root), `mailbox_completed` (file name, `done/` folder) and `mailbox_failed` (file name, outcome
and `sf resume` hint). The dashboard's orchestrator lane already lists every `sf` event, so it shows
which dropped file produced the run and where that file went.

**Token accounting:**

- Input, output, cache-read, cache-write and cost (where Pi reports them) are captured from Pi usage events.
- They are recorded per Pi invocation and aggregated per stage, per loop and per run.
- They are shown live in the Rich console.

## 8. Resume

The per-stage commits plus `run-state.json` act as checkpoints. `sf resume <id>` continues from the next incomplete stage.
