# Software Factory — Specification

A Python orchestrator that drives the Pi, Claude Code or Codex CLI coding agent through a fixed workflow:
**prompt → plan → implement ⇄ review → document → merge → improve**.

## 1. Repositories

- **Factory repo** (this repo): orchestrator code, agent definitions, starter templates, run logs.
  - Trunk is `main`.
  - Its architecture record is `ARCHITECTURE.md`; `docs/architecture.md` points there, including when the
    factory is used as its own target.
- **Target repo**: the software being built. It is passed with `--target <path>`.
  - Optional `<target>/.sf.toml` holds per-target settings: `trunk` (default `main`) and validation overrides.
  - It contains `AGENTS.md` at the root; every other doc lives in `docs/`, including `docs/coding-standards.md`, `docs/architecture.md` and `docs/VALIDATION.md`.
- If any standards file is missing from the target, the run adds it on the run branch: `AGENTS.md` and `docs/VALIDATION.md` are downloaded from https://github.com/yandaq/agents-default; `docs/coding-standards.md`, `docs/architecture.md` and `.gitignore` are copied from the factory's starter templates. The starter `.gitignore` keeps build output from agents' verification runs out of the working tree, which the scope and commit steps treat as uncommitted changes; a target that already has a `.gitignore` keeps its own, and agent and validation subprocesses additionally run with `PYTHONDONTWRITEBYTECODE=1` and `PYTEST_ADDOPTS=-p no:cacheprovider` so Python never drops caches there.

## 2. Stack

- Python 3.14 (`requires-python >= 3.14`), `uv` project
- Typer CLI
- Pydantic (definitions, contracts, state)
- PyYAML (frontmatter)
- jsonschema (agent output contracts, draft 2020-12)
- Rich (live console)
- pytest
- Each harness CLI is invoked through `subprocess`, and its stdout is read line by line as JSONL. The harness
  is chosen per agent; the workflow, contracts and correction retries are harness-neutral (`src/sf/runner.py`).

## 3. CLI

```
sf run --target <path> "<prompt>" | --prompt-file <file> [--approve-plan]
sf watch --target <path> <folder> [--approve-plan]
sf resume <run-id>
sf improve <run-id>        # re-enter the improve phase
sf dashboard [--host 127.0.0.1] [--port 8765]
sf submit <target> [<prompt-file>|-]        # queue a run (needs SF_DATABASE_URL); see §9
sf runner [--caps TAG]... [--concurrency N] [--id ID]   # execute queued stage jobs; see §9
```

Exit codes: `0` done, `1` stage failed (resumable), `2` aborted (plan rejected, loop-cap abort, merge
conflict). `sf resume` refuses an improvement execution run; those are retried from the dashboard.
The dashboard's HTTP API is documented in [dashboard-api.md](dashboard-api.md).
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
harness: pi               # pi | claude-code | codex; absent means pi
model: anthropic/claude-sonnet-...
thinking: high            # pi: off|minimal|low|medium|high|xhigh|max
                          # claude-code and codex: low|medium|high|xhigh
tools: [read, grep, find, ls]
contract:                 # JSON Schema for the final response
  type: object
  required: [verdict, findings]
  properties: {...}
---
<system prompt>
```

A thinking level the selected harness does not offer, or a model that is not a single non-empty line, makes
the definition invalid and fails the stage. Each harness receives the tool list its own CLI can honour: Pi and
Claude Code get the exact tools (Claude Code through a fixed mapping to native tool names), Codex gets a
read-only sandbox for read-only roles and workspace-write for roles whose tools include `edit` or `write`.

Agents: `planner`, `implementer`, `reviewer`, `documenter`, `improver`.

Each agent also has a required `agents/<name>.user.md` task template. All task templates include
`{{documentation}}`: the target repository path and a JSON inventory of repository-relative
Markdown paths. Discovery runs before each new invocation using Git's tracked and non-ignored
untracked files throughout the target, including DESIGN.md and component-local guidance.
Only existing files with case-insensitive .md extensions are included; symlinks and deleted
files are excluded. Agents read applicable docs and references and respect local instruction
scope; historical records are not automatically requirements. Correction retries retain the
original invocation context, and rendered task artifacts preserve the supplied inventory.

## 5. Invocation

One invocation is `harness -> result`, selected by the agent's `harness` field. The shared runner owns streaming,
contract validation, correction retries, the invocation record and usage accounting; the adapter only builds a
command, reads native events and reports what happened.

```
pi --mode json --model <model> --thinking <thinking> --tools <tools> \
   --append-system-prompt <body-file> --session-dir runs/<id>/sessions/ [ --session <id> ] -- @<task-file>

claude --print --output-format stream-json --verbose --include-partial-messages \
       --agents <definitions-file> --agent <name> --model <model> --effort <thinking> \
       --tools <tools> --allowedTools <tools> --permission-mode dontAsk --permission-prompts none \
       --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands [ --resume <id> ]

codex exec [resume] --json --model <model> -c model_reasoning_effort=<thinking> \
       -c sandbox_mode=<read-only|workspace-write> -c approval_policy="never" \
       -c developer_instructions=<pointer to the role file> [ <session-id> ] -
```

- **The task never travels in argv.** Pi receives it as `-- @<file>`; Claude Code and Codex read it on stdin.
  The rendered task is written to `runs/<run-id>/prompts/`. A Pi agent that runs `pkill -f <pattern>` would
  otherwise match, and kill, its own `pi` process whenever the task text contained the pattern.
- **Native output is retained verbatim.** Stdout goes to `<harness>-<stage>-<n>.jsonl` and stderr to
  `<harness>-<stage>-<n>.stderr` before anything is interpreted. Only JSON objects are decoded; any other line
  is recorded as `pi_bad_line` or `agent_bad_line` with the raw text rather than dropped.
- **Completion:** a CLI can exit 0 on a failed agent run, so success is decided from the native events; a nonzero
  exit is also a failure, as is an empty final assistant message.
  - **Pi:** failure when there is no `agent_settled` event, when `agent_settled.aborted` is set, or when the final
    `stopReason` is `error`, `aborted` or `length`.
  - **Claude Code:** the run completes only on a `result` event with `subtype: success` and no `is_error`; a
    `permission_denied` system event or any `permission_denials` entry fails it.
  - **Codex:** `turn.completed` completes the run; `turn.failed` or `error` fails it.
- **Sessions:** a new invocation always starts fresh. Pi keeps its transcript in `runs/<run-id>/sessions/`;
  Claude Code and Codex keep resumable sessions in their own native storage and are resumed by ID.
- **Contract:** the final assistant message must contain JSON that validates against the agent's contract. If it
  is invalid, the orchestrator resumes the same session with the validation errors, at most **2 times**, with the
  model and settings pinned, and then fails the stage. A correction without a session ID fails immediately.
- **Permissions:** interactive approval is disabled for Claude Code and Codex, and no sandbox or permission bypass
  is enabled, so a blocked operation fails visibly instead of waiting for an unattended prompt.
- **Limits:** there are **no timeouts and no budget limits** on an agent invocation, and configured validation
  commands run to completion. The only bounded calls are model-catalog discovery and the standards download,
  both 30 s.
- `SF_PI`, `SF_CLAUDE` and `SF_CODEX` override the CLI paths, which is what the test suite uses.

## 6. Workflow

1. **Branch.** Create a new branch `sf/<run-id>` in the target, off the trunk. The target must have a clean working tree: uncommitted changes fail this stage before anything is written. The trunk is re-resolved here, so a resumed run picks up a corrected `.sf.toml`.
2. **Prompt.** Save the prompt to `runs/<id>/prompt.md`.
3. **Plan.** The planner produces `plan.md`. Acceptance criteria and out-of-scope lists scope the implementation; documentation left for the document stage is recorded as guidance, not excluded by the plan, unless the request itself forbids touching documentation. With `--approve-plan`, a human approves the plan before the run continues; rejecting it aborts the run. The plan is then committed to the target branch as `docs/plans/<run-id>.md` before implementation starts, whether or not approval was requested.
4. **Implement ⇄ Review**, for up to **5 loops**:
   - **Implementer.** Starts a fresh session each loop, with the plan, the latest review findings or failed validation output, and the documentation inventory. It delivers the planned work and self-reviews against the plan, `docs/architecture.md`, `docs/coding-standards.md` and `docs/VALIDATION.md` before handing off, citing only validation output it actually inspected. Commit.
   - **Validation.** The orchestrator runs the commands listed in `docs/VALIDATION.md`. If there are none, validation is skipped and the skip is logged. On failure, control returns to the implementer without running the reviewer, and this counts as a loop.
   - **Reviewer.** Reviews the full plan, the applicable repository contracts and the affected documentation — not only `docs/coding-standards.md` — and checks the statements of the documents it reads against the implementation. It also receives this loop's validation gate as runner-generated evidence: PASS with the executed commands and their diagnostics, or SKIPPED when no commands are configured. It attributes that report to the runner, never to itself, and it does not replace independent review. It returns `{verdict: approve|changes_requested, findings[]}`, which is written to `review-<n>.md`.
   - **Cap reached.** If the reviewer has not approved after 5 loops, a human chooses: merge anyway, abort, or allow N more loops. This outcome is flagged to the improve phase.
5. **Document.** The documenter diffs `trunk...branch`. It reduces drift between implemented code and docs, updating existing documentation including `README.md`, `AGENTS.md`, `docs/VALIDATION.md`, `docs/coding-standards.md` and `docs/architecture.md` when factual guidance changes. Silence counts as drift: where the diff makes information necessary and the document that naturally holds it — usually `README.md` — omits it, the documenter adds it. It preserves normative requirements and reports conflicts rather than weakening standards. New docs are restricted to `docs/`; code and runtime configuration are outside its scope. The orchestrator commits the documentation changes.
6. **Merge.** Local `git merge --no-ff` into the trunk. If there is a conflict, the run aborts.
7. **Improve** (suggestions only):
   - The improver analyses the generated code, all run logs, and the past decisions recorded in `improvements.jsonl`. Ideas that were previously rejected must not be proposed again.
   - Every suggestion that survives historical rejection filtering is recorded in `improvements.jsonl` with `decision: automatic` and an empty note, and only that file is committed. Selection records what was proposed, not what was applied. Historical decisions remain readable. There is no terminal approve/skip/reject prompt or human note.
   - Each idea names exactly one existing file in its required `file` field: `agents/<name>.md` or `agents/<name>.user.md` relative to the factory, a Python module under `src/sf/`, or a path copied from the target-documentation inventory. The JSON contract rejects a missing path, an absolute path, a `..` segment and any other extension, so the dashboard can offer the idea as one edit to one file.
   - No run applies a suggestion. The improve stage checks the target back to the trunk, writes neither repository's files except the suggestions log, and project-specific rules belong in target documentation. A human then applies a suggestion explicitly from the dashboard, which runs the configured implementer against the factory itself, runs the factory's own validation commands and commits all working-tree changes on success; see the *Explicit improvement application* section of [../ARCHITECTURE.md](../ARCHITECTURE.md) and the log format in [improvements-log.md](improvements-log.md).
   - A re-run (the CLI or a dashboard retro) analyses the same evidence again and only previously **rejected** titles are filtered out, so it can record an idea that was already recorded. Recording what was proposed stays the improver's judgement; nothing is applied either way.

Run state is saved to `runs/<id>/run-state.json` as each stage completes. Run ids are
`YYYYMMDD-HHMMSS`, suffixed `-N` when `runs/<id>` already exists: two runs started inside one second —
which the watch monitor makes easy — must not share a run directory, state file or event stream.

## 7. Observability

This section is the single contract for what a run leaves on disk; the README points here rather than
repeating the list.

`runs/<run-id>/` is git-ignored (never committed) and contains:

- `events.jsonl`: one stream holding every orchestrator event and every normalized agent event, in real time.
  Each line is tagged with `run_id`, `stage`, `loop`, `source` and `ts`. A native event is written once as
  `source: agent` carrying `agent`, `invocation`, `harness` and the native `event`. Runs recorded before event
  normalization also contain `source: pi` lines; the dashboard shows those only when no normalized event exists
  for the same invocation.
- `<harness>-<stage>-<n>.jsonl`, `<harness>-<stage>-<n>.stderr`: one invocation's verbatim native stdout and
  stderr (`harness` is `pi`, `claude-code` or `codex`).
- `prompts/`: the role prompt and the rendered task actually sent to each invocation.
- `sessions/`: Pi session transcripts. Claude Code and Codex keep their sessions in their own native storage.
- `prompt.md`, `plan.md`, `review-<n>.md`, `validation-<n>.log`
- `retro.log`: the console output of the latest dashboard retro of this run, downloadable as an artefact.
- `run-state.json`: the current stage, the loop number, the result of each stage, one record per invocation
  (harness, model, thinking, session, usage) and the token totals.
- `portability-*`: recorded by earlier versions of the factory. The dashboard still serves them; no current run
  produces them.

Watched prompts also leave `sf`-source events in the run they produced: `mailbox_claimed` (file name,
watch root), `mailbox_completed` (file name, `done/` folder) and `mailbox_failed` (file name, outcome
and `sf resume` hint). The dashboard's orchestrator lane already lists every `sf` event, so it shows
which dropped file produced the run and where that file went.

**Token accounting:**

- Input, output, cache-read, cache-write and cost are captured from each harness's own usage events, recorded
  per invocation and aggregated per stage, per loop and per run.
- A field the harness does not report stays **unavailable** instead of being recorded as a measured zero: Codex
  reports no cache-write and no dollar cost, and Claude Code reports no cost when the CLI omits it. Cost is
  treated as an estimate where the harness estimates it (Pi and Claude Code).
- Codex input tokens exclude cached tokens so they match Pi's and Claude Code's convention.
- They are shown live in the Rich console: unavailable fields read **N/A**, mixed totals are labelled
  **partial**, and estimated costs **est.**

## 8. Resume

The per-stage commits plus `run-state.json` act as checkpoints. `sf resume <id>` continues from the next incomplete stage.

A parked **queued** run (§9) is resumed with the same command when `SF_DATABASE_URL` is set and the store
knows the run id: its latest failed stage job is re-enqueued and the run marked running.

## 9. Distributed (queued) mode

With `SF_DATABASE_URL` set, runs can be queued instead of driven by one local process. Local `sf run`,
`sf watch` and the file-mode dashboard are unchanged and need no database.

**Coordination.** Postgres holds run state, the `run_config` snapshot, events, run documents (prompt, plan,
reviews), stage jobs and results, runners and the improvement log; there is no separate orchestrator
process. Schema migrations are applied by `store.migrate` whenever a command connects.

**Submission.** `sf submit <target> [<prompt-file>|-]` (stdin by default) checks the target, creates the run
and snapshots into `run_config` the agent definitions, task templates and target config — including the
target's `origin` remote (its local path when it has none) — plus the factory remote. When the factory has an origin, agents and templates
are read from a fresh clone of it, so only pushed factory changes reach new runs. Every stage of a run uses
that snapshot. `--approve-plan` is refused in v1. It prints the run id.

**Stage jobs.** Each stage of a run (branch → prompt → plan → implement ⇄ review → document → merge → improve)
is one `stage_jobs` row carrying `run_id`, `stage`, `attempt`, small `inputs` (e.g. review findings) and
`required_caps` (`harness:<pi|claude|codex>` from the stage's agent). The stage graph is
`workflow.advance()`, the same function the local driver uses; the runner calls it inside the completion
transaction that records the result, saves the state and inserts the next job (or ends or parks the run).
Contract-correction retries stay inside the job, on the same runner, because harness sessions are
machine-local.

**Runners.** `sf runner` registers in `runners` (id default `<host>-<pid>`, `--id` to override), with
capability tags auto-detected from the harness CLIs found (`SF_PI`/`SF_CLAUDE`/`SF_CODEX` or `PATH`) plus any
`--caps`, and runs `--concurrency` worker threads (default 1). A worker claims the oldest queued job whose
required caps it has with `SELECT … FOR UPDATE SKIP LOCKED`, holds it under a lease renewed by a heartbeat
thread, and waits on `LISTEN/NOTIFY` between polls. SIGINT/SIGTERM stops claiming and lets running jobs
finish; a second signal exits at once and the abandoned leases expire. Progress is one JSON object per
stdout line.

**Working tree handoff.** A job runs in a fresh clone of the target remote made with `git clone --reference`
from a per-remote clone cache under `SF_CACHE_DIR` (default `~/.cache/sf`), checks out `sf/<run-id>`, and on
success pushes that branch back, so the next stage can run on any runner. The job's run directory (harness
output, prompts, sessions, validation logs) is uploaded to the artifact store under
`runs/<run>/<stage>/<job-id>/`, failed stages included; nothing is kept under `factory/runs/`.

**Failure semantics.**
- *Infra failures* (clone, fetch, push, artifact upload, expired lease) requeue the job on a fresh clone up to
  2 times (`queue.MAX_INFRA_RETRIES`); after that the job fails and the run is parked.
- *Agent failures* (a stage fails or a contract cannot be satisfied) park the run immediately.
- The loop cap parks the run with `cap_reached`; in v1 a queued run cannot continue past it.
- A parked run is resumed with `sf resume <id>` or the dashboard's resume button, which re-enqueues its
  latest failed stage job.

**Merge.** Serialised per target remote by a Postgres advisory lock: fetch, rebase `sf/<run-id>` onto the
remote trunk, re-run validation, fast-forward the trunk and push trunk and branch atomically. No merge commit
is made on this path. A rebase conflict or failed revalidation earns one more implement ⇄ review pass with the
conflict or validation output as feedback.

**Improve.** Runs as the final job, with the run's evidence (`evidence/`) and a fresh factory clone (`factory/`)
as inputs; suggestions are recorded in the `improvements` table instead of `improvements.jsonl`. Applying one
from the dashboard is described in [dashboard-api.md](dashboard-api.md) and
[improvements-log.md](improvements-log.md). Retro and plan approval are not available for queued runs in v1.

**Environment.**

| Variable | Used by | Meaning |
|---|---|---|
| `SF_DATABASE_URL` | submit, runner, resume, dashboard | Postgres connection string (libpq URL) |
| `SF_S3_ENDPOINT`, `SF_S3_REGION`, `SF_S3_BUCKET`, `SF_S3_ACCESS_KEY`, `SF_S3_SECRET_KEY` | runner, dashboard | S3-compatible artifact store (production: Garage) |
| `SF_CACHE_DIR` | runner, dashboard apply | Clone cache directory (default `~/.cache/sf`) |
| `SF_PI`, `SF_CLAUDE`, `SF_CODEX` | runner | Harness CLI paths; also drive capability detection |

`sf runner` exits 1 naming any of `SF_DATABASE_URL` and `SF_S3_*` that are unset.
