# Architecture

| Module | Responsibility |
|---|---|
| `cli.py` | Typer commands; lockfile; watch wiring (per-run `RunLock`, `mailbox_*` events, up-front target check); maps outcomes to exit codes |
| `mailbox.py` | Watch-folder queue only: `todo/`/`doing/`/`done/` layout, oldest-first candidates, settle guard, collision-safe claim/complete and the monitor loop; the caller supplies the per-file executor and the busy reason, poll and settle intervals default to module constants |
| `workflow.py` | Stage driver, run-id allocation (`YYYYMMDD-HHMMSS`, `-N` suffixed when `runs/<id>` exists), per-stage checkpoints (`runs/<id>/run-state.json`; `runs/` is git-ignored), stages branch → merge |
| `improve.py` | Improve phase: read-only improver suggestions, decision history in `improvements.jsonl`; suggestions are never applied, applying one is a human edit in the dashboard |
| `runner.py` | Shared invocation lifecycle, streamed subprocess boundary, JSON contract validation + re-prompts |
| `pi.py`, `claude.py`, `codex.py` | Native CLI invocation, session handling, success detection and event/usage normalization; `pi.py` retains original runner imports for compatibility |
| `run.py` | `RunState` (Pydantic), `events.jsonl` emitter, token aggregation, `RunLock` |
| `agents.py` | Agent definitions plus fixed-vocabulary task-template validation and literal rendering |
| `target.py` | `.sf.toml`, starter standards, Git-based Markdown discovery, validation commands |
| `git.py` | git wrapper |
| `dashboard.py` + `static/dashboard.html`, `static/config.html` | Stdlib HTTP server and polling page over `runs/*/`; settings controls edit frontmatter; Markdown editor edits role definitions and task templates; a run's retro starts the improve phase again in a child process |
| `console.py` | Rich live display and terminal prompts (`TerminalHuman`) |

`Workflow` depends on a `Human` and a display protocol, so tests substitute scripted fakes.

## Compromises

- **The watch queue is polled, not watched.** `mailbox.py` re-reads `todo/` on a short interval instead of
  taking a filesystem-watcher dependency: a dropped prompt starts within one poll, and a file is only
  claimed once it has been unmodified for the settle window, so a half-written drop is never a prompt.
  A poll is a directory sweep plus a `stat` per file - no model call, no new dependency.
- **A failed prompt is parked, not retried.** Its file stays in `doing/` with its run resumable, and the
  watch moves on. There is no fourth folder and no automatic retry: three folders were specified, and a
  permanently failing prompt would otherwise loop forever. Retrying one means dropping the file into
  `todo/` again as a fresh run; `doing/` then doubles as the queue's visible failure list.
- **The run lock is taken per run, not per watch.** `sf watch` checks the lock before each claim and holds
  it only while a run executes, so `sf resume` still fits between prompts and a busy lock leaves prompts
  in `todo/`. The check is `RunLock.holder()`, a read-only query that reports the pid of a live holder and
  ignores a lock left by a dead process - the same rule `RunLock` itself applies when taking over. Two
  concurrent `sf watch` processes are not coordinated beyond that check: the check and the acquisition are
  not atomic, so a prompt that loses that race is reported as a failed file and left in `doing/` with no
  run created for it.
- **The queue knows nothing about runs.** `mailbox.py` handles folders only; the CLI supplies the per-file
  executor, the sleep and the busy reason, and owns the run, its `mailbox_*` events and its terminal
  notes. The trade-off is that the watch's behaviour is proven by two layers of tests - queue-level and
  end-to-end through `cli._watch_loop` - rather than one.
- **Scope rules are enforced after the fact.** Native permissions do not express every role's allowed paths, so the
  orchestrator inspects `git status` after the documenter. Violations are reverted and fail the stage.
- **Native permissions differ.** Pi and Claude Code use agent tool lists; Codex uses role-based
  read-only/workspace-write sandboxes with broader shell access. New harnesses deny interactive approval
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

The improver is read-only: it proposes evidence-backed changes and the improve stage records every
suggestion that survives historical rejection filtering. No agent edits prompt files; a human applies a
suggestion with the dashboard Markdown editor, which validates the file it saves. Whether a shared-prompt
idea stays portable across unrelated targets is the judgement of the person applying it.

## Documenter responsibility

The document stage reduces drift between implemented code and documentation. Existing
Markdown guidance, including AGENTS.md, validation, coding standards and architecture,
is eligible for factual updates. New documents remain restricted to docs/. The agent
must preserve normative requirements and report implementation conflicts; policy tuning
belongs in the improvement suggestions. Code and runtime configuration remain outside
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

The improve stage supplies the inventory to the improver, so suggestions can name existing target docs;
applying them is a human edit. Contract-correction retries reuse the current invocation context. Starter
standards and validation-command sources do not change; discovery never executes doc content.

## Improvement recording

The improve stage checks the target back out to the trunk, runs the read-only improver over the merged
code and the run logs, drops ideas whose title was previously rejected (`idea_filtered`) and appends the
rest to `improvements.jsonl` with `decision: automatic` and an empty note, then commits only that file.
Nothing is applied: applying a suggestion means editing the prompt or document it names in the dashboard,
so the history records what was proposed, not what was done.

Each recorded idea carries `file`, the one Markdown file it changes. The improver's contract requires a
repository-relative `.md` path with no absolute or `..` segment, which is the guarantee the dashboard's
apply action relies on; resolving that path to a real file and writing it stays the dashboard's
responsibility, so the stage itself still touches neither repository.

Improvement selection never calls the Human protocol. That protocol now covers only optional plan
approval and loop-cap choices. Historical human decisions remain readable and no decision history is
migrated. `portability-*` artifacts recorded by earlier runs stay downloadable from the dashboard, but no
run produces them.

Re-running the phase over a historical run adds no improvement logic of its own. `sf improve <run-id>` and
the dashboard's retro button reach the same `stage_improve`: the button starts that command as a child of
the dashboard server, using the interpreter that serves it, in the factory directory, with its console in
`runs/<id>/retro.log`. The child owns the run: it takes the run lock, re-enters only the improve stage and
writes into the run's own state file and event stream, which is how the polled page shows a retro in
progress and its result afterwards. The server only guards the door: an unknown run is a 404 and a run that
has not merged, or one whose factory run lock is held by a live process, is a 409 with the reason.

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
