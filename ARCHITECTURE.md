# Architecture

| Module | Responsibility |
|---|---|
| `cli.py` | Typer commands; lockfile; maps outcomes to exit codes |
| `workflow.py` | Stage driver, per-stage checkpoints (`runs/<id>/run-state.json`; `runs/` is git-ignored), stages branch → merge |
| `improve.py` | Improve phase: automatic improver suggestions, decision history, `improvements.jsonl`, scope enforcement |
| `runner.py` | Shared invocation lifecycle, streamed subprocess boundary, JSON contract validation + re-prompts |
| `pi.py`, `claude.py`, `codex.py` | Native CLI invocation, session handling, success detection and event/usage normalization; `pi.py` retains original runner imports for compatibility |
| `run.py` | `RunState` (Pydantic), `events.jsonl` emitter, token aggregation, `RunLock` |
| `agents.py` | Agent definitions plus fixed-vocabulary task-template validation and literal rendering |
| `target.py` | `.sf.toml`, starter standards, Git-based Markdown discovery, validation commands |
| `git.py` | git wrapper |
| `dashboard.py` + `static/dashboard.html`, `static/config.html` | Stdlib HTTP server and polling page over `runs/*/`; settings controls edit frontmatter; Markdown editor edits role definitions and task templates |
| `console.py` | Rich live display and terminal prompts (`TerminalHuman`) |

`Workflow` depends on a `Human` and a display protocol, so tests substitute scripted fakes.

## Compromises

- **Scope rules are enforced after the fact.** Native permissions do not express every role's allowed paths, so the
  orchestrator inspects `git status` after the documenter and improvement-implementer. Documenter
  violations are reverted and fail the stage; improvement-implementer violations are reverted and
  logged (`scope_violation`) while in-scope edits are kept.
- **Native permissions differ.** Pi and Claude Code use agent tool lists; Codex uses role-based
  read-only/workspace-write sandboxes with broader shell access. The improvement-implementer receives
  target write access in addition to the factory workspace. New harnesses deny interactive approval
  requests and surface permission failures without bypassing protections.
- **Telemetry is normalized, not invented.** `source: agent` events share the dashboard's activity shape
  across harnesses; raw stdout/stderr are retained separately. Legacy `source: pi` events remain readable,
  but are excluded from dashboard activity when normalized events exist for that invocation. Invocation
  state records harness/model/effort/session; usage records known/unavailable fields and estimated cost.
  Aggregates retain availability so missing cost/context is never shown as a measured zero. Codex input
  tokens exclude cached tokens when normalized, matching Pi/Claude's uncached-input convention.
- **Session storage stays native.** Pi sessions live under the run; Claude Code and Codex persist sessions
  in their own user storage. A new invocation starts fresh; contract corrections resume its session with
  pinned settings. Native model support is checked by the CLI, not inferred from Pi's model catalogue.
- **Resume granularity is a stage, or a loop inside implement.** An interrupted loop counts as used
  and the next loop starts fresh; an interrupted stage is re-run from its start.
- **Agents return documents inside their JSON contract** (e.g. `{"plan": ...}`) and the orchestrator
  writes `plan.md` / `review-<n>.md`, so read-only agents need no write tools.

## Prompt tuning boundary

`agents/<name>.md` retains configuration and role instructions; `<name>.user.md`
holds task wording with required `{{identifier}}` context placeholders. Workflow and
improvement modules provide context, and `agents.py` substitutes it once. Inserted
requests and logs remain literal. Harness adapters and contract corrections retain
their existing interfaces. Templates are loaded for each new invocation.

Automatically selected improvements validate all seven definition/template pairs before committing.
Scope enforcement preserves pre-existing out-of-scope local edits.
Invalid prompt edits restore the pre-invocation Markdown snapshot and fail the stage;
target standards edits remain uncommitted for inspection. Structural validation does
not measure tuning quality; the improver selects evidence-backed changes and the portability
reviewer judges shared-prompt portability.

## Documenter responsibility

The document stage reduces drift between implemented code and documentation. Existing
Markdown guidance, including AGENTS.md, validation, coding standards and architecture,
is eligible for factual updates. New documents remain restricted to docs/. The agent
must preserve normative requirements and report implementation conflicts; policy tuning
belongs in the automatic improvement loop. Code and runtime configuration remain outside
its stated scope, and the orchestrator owns commits. File checks enforce paths; the
semantic distinction between factual updates and policy changes is instructed in prompts.

## Documentation discovery

Before each new invocation, the workflow inventories target Markdown through Git's tracked
and non-ignored untracked files, anywhere in the repository. Existing regular files with
case-insensitive .md extensions are sorted and deduplicated; symlinks and deleted files are
excluded. The required documentation template variable supplies the target path and JSON
repository-relative paths. Inserted names stay literal, and rendered prompt artifacts retain
the inventory. Discovery failures emit documentation_discovery_failed and fail the stage.

All agent roles use the inventory to find relevant docs, references and scoped local guidance.
An inventory provides visibility, not proof that an agent read or understood every document;
old plans and descriptive docs are not automatically governing requirements.

Each automatically selected improvement freezes one inventory for context, scope enforcement and target
commit paths. Only existing discovered target docs are editable; new target docs remain with
the documenter. Contract-correction retries reuse the current invocation context. Starter
standards and validation-command sources do not change; discovery never executes doc content.

## Shared-prompt portability gate

The improve module owns a read-only semantic review between structural prompt validation and
improvement commits. It pins the improvement-reviewer definition and task template before the
implementer runs, then supplies exact shared-file before/after contents relative to that invocation's
snapshot. Pre-existing local edits are not themselves review subjects. The gate forces read-only
tools and uses the existing harness-neutral runner, contract correction and invocation artifacts.
Target-only edits skip review; target documentation is outside the semantic review scope.
Factory improvement commits are restricted to the reviewed shared paths, so unrelated local prompt
edits are not swept into a target-only improvement.

Explicit approval with no findings is required. Rejection, invalid output or invocation failure
restores shared Markdown exactly, removes newly added files and fails the improve stage before
either repository's improvement commit. Target documentation stays uncommitted for inspection,
matching existing invalid-prompt behaviour. Structured events and portability input/result artifacts
retain the applied idea, reviewed changes and reasons; new automatic selections and historical human decisions remain recorded independently.

The trade-off is one additional model invocation per improvement that changes shared prompts.
The gate enforces review and rollback deterministically, while portability judgement remains model
based. No automatic implementation repair or decision-history migration is introduced.

Improvement selection never calls the Human protocol. That protocol now covers only optional plan
approval and loop-cap choices. Every suggestion that survives historical rejection filtering is
recorded with `decision: automatic` before application, then applied sequentially without a human
note. This history records selection; commit events and stage results establish application success.
Portability or implementation failure still stops the stage. No history migration is performed.

## Dashboard project documents

Run detail includes a fresh target-documentation inventory and a separate discovery error, so an
unavailable target does not hide the rest of a run. The document GET/PUT/DELETE endpoint resolves paths
only through the selected run's current inventory; code, new files, ignored untracked files and
symlinks remain outside the editor. The save request supplies content and original text; stale
originals return HTTP 409 without writing. Saves update working-tree files without committing.
The modal follows the config editor's interaction pattern and lives outside the polled main node,
so refreshing run results cannot replace a draft. Requests are tied to the selected run and file.

Document deletion requires the original opened text, rejects stale requests with HTTP 409 and
rechecks the current inventory. It also rejects any path within the configured agents directory
with HTTP 403, preventing a factory-targeted run from bypassing agent-config protection. GET marks
those documents as not deletable so the project modal hides its delete action. No DELETE route or
button is added to agent configuration. Confirmed deletion closes the modal and refreshes the
project inventory; cancellation and errors preserve the draft. No commit is created.
