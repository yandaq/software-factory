# sf — software factory

Drives [Pi](https://pi.dev), Claude Code, or Codex through a fixed workflow
(prompt → plan → implement ⇄ review → document → merge → improve). Spec: [docs/spec.md](docs/spec.md).

## Usage

Run from the root of this repo (the factory repo):

```sh
uv run sf run --target ../my-app "Add a /health endpoint"   # or --prompt-file req.md
uv run sf run --target ../my-app --approve-plan "..."        # human approves plan.md first
uv run sf watch --target ../my-app ./prompts                    # run every .md dropped into ./prompts/todo
uv run sf resume <run-id>                                    # continue after a failure
uv run sf improve <run-id>                                   # re-enter the improve phase
uv run sf dashboard [--port 8765]                            # web dashboard at http://127.0.0.1:8765
```

`sf watch` turns a folder into a prompt queue. It creates `todo/`, `doing/` and `done/` inside the folder
it is given and then monitors `todo/` until you press Ctrl+C (exit 0). Each Markdown file you drop into
`todo/` becomes one run whose prompt is that file's content: the file moves to `doing/` while the run
executes and to `done/` when the run succeeds. Runs never overlap and the oldest file goes first
(oldest modification time, ties broken by name); files that are not Markdown, subdirectories and files
still being written are left alone. A failed or aborted run leaves its file in `doing/` for inspection
and `sf resume`, and the watch reports it and moves on to the next file instead of retrying it. The run
lock is taken per run and released in between, so `sf resume` still fits; while another `sf` process
holds it, prompts wait in `todo/`. Per-run failures never end the watch or the process.

A prompt is any regular `*.md` file (any case) sitting directly in `todo/`. `todo/` is re-read about every
2 s and a file is claimed only once it has gone about 1 s without modification, so a file still being
written is never started. A name already used in `doing/` or `done/` is suffixed (`fix.md` → `fix-1.md`),
so re-dropping a prompt never overwrites the earlier copy. Before watching, `sf watch` checks that the
current directory is the factory repo and that the target is a git repository with a usable trunk: an
unusable target exits 1, a `FOLDER` argument that is an existing file is a usage error, and neither
creates the queue folders.

The dashboard provides editors, run retros and explicit improvement application. It refreshes every 2 s:
every run's status, the stage pipeline, review/validation results per loop, token usage, artefacts (plan, reviews,
validation logs, stderr), orchestrator events and tool calls.
Each run in the sidebar has a **retro** button. It re-enters the improve phase for that run, exactly as
`sf improve <run-id>` does, so the improvement agent can be pointed at a historical run again: it studies
that run's logs and its merged code and records suggestions in `improvements.jsonl`, and it applies
nothing. The retro runs as a child process of the dashboard, so the run's own page shows the improve stage
live and the child's console output is downloadable as `retro.log`. A run that has not merged has the
button disabled, and a retro that cannot start (another `sf` run holds the run lock) is reported in the
browser and in `retro.log`. A retro analyses the evidence from scratch, so it can record an idea that run
already produced.

The **config** page (`/config`) sets each agent's harness, model and thinking level by updating only those
frontmatter fields in `agents/<name>.md`. Changes apply to the next invocation; contract-correction retries
keep the original settings. Pi remains the default when `harness` is absent, including for existing definitions.
Model dropdowns are populated from each installed harness: `pi --list-models`, Claude Code
initialization, and Codex app-server `model/list`. Existing unlisted models are preserved; discovery
failures show a diagnostic and allow manual entry for native harnesses. Switching harness clears the model field to avoid accidentally passing a Pi model ID to another CLI.

For example, a Codex agent's frontmatter can include:

```yaml
harness: codex                  # pi | claude-code | codex
model: gpt-6.1-sol              # a native model ID available to your account
thinking: high
```

Install and authenticate each selected CLI separately; the factory reuses its existing authentication.
The new harnesses offer `low`, `medium`, `high`, and `xhigh` effort; the selected model must support the
chosen level. Unsupported models/settings fail the stage with native stderr available in the dashboard.
The adapters have been smoke-tested with Claude Code 2.1.296 and Codex CLI 0.162.1.

All harnesses use the same workflow, JSON contracts and two same-session correction retries. Claude Code
maps the existing agent tool lists to native tools. Codex uses a read-only sandbox for planner/reviewer/improver
and workspace-write for editing roles; its shell is broader than Pi's exact tool list. Existing file-scope checks
remain in place. Permission prompts are disabled for
new harnesses, and no sandbox/permission bypass is enabled. Operations blocked by native permissions
fail visibly rather than waiting for an unattended prompt.

Dashboard status, stages, review/validation results, activity, tool calls and diagnostics remain available
across harnesses, including mixed-harness runs. Token and cost fields are shown only when reported:
unavailable metrics display **N/A**, mixed/incomplete totals are labelled **partial**, and estimated costs
are labelled **est.** Codex's exec stream does not report dollar cost, cache-write tokens, or Pi-equivalent
per-message context size; Claude Code context size is also left unavailable. Consumption totals are not
plotted as context size. Historical Pi runs remain readable. Native stdout JSONL and stderr are downloadable
from the dashboard; Claude Code and Codex keep resumable sessions in their own native storage.

Exit codes: `0` done, `1` stage failed (resumable), `2` aborted (plan rejected, loop cap abort, merge conflict).

## Layout

| Path | Purpose |
|---|---|
| `agents/<name>.user.md` | Editable task templates with fixed context placeholders; see [agents/README.md](agents/README.md) |
| `agents/<name>.md` | Agent definitions: YAML frontmatter (harness, model, thinking, tools, JSON Schema contract) + role prompt |
| `templates/` | Starter `docs/coding-standards.md`, `docs/architecture.md` and `.gitignore` for targets that lack them (`AGENTS.md` and `docs/VALIDATION.md` are downloaded from [agents-default](https://github.com/yandaq/agents-default)) |
| `runs/<run-id>/` | `events.jsonl`, `run-state.json`, `sessions/`, `<harness>-<stage>-<n>.stderr` / `.jsonl`, `prompt.md`, `plan.md`, `review-<n>.md`, `validation-<n>.log`, `retro.log` |
| `improvements.jsonl` | Improvement suggestions and historical decisions; previously rejected titles remain excluded. View newest-first history below the agent widget on `/config`, including rationale, evidence and proposed changes, and delete any one entry from that list. Play invokes the configured implementer against software-factory, leaving changes uncommitted. Suggestions retain applied/failed status and execution diagnostics; failures can be retried. |
| `src/sf/` | Orchestrator (see [ARCHITECTURE.md](ARCHITECTURE.md)) |

## Target configuration

Optional `<target>/.sf.toml`:

```toml
trunk = "main"                      # default: main if it exists, else master
validation = ["uv run pytest -q"]   # overrides the commands in docs/VALIDATION.md
```

Without an override, validation runs every line of the ```` ```sh ```` blocks in the target's `docs/VALIDATION.md`. The downloaded default has none, so set `validation` here to get checks.

## Development

```sh
uv run pytest -q        # uses fake Pi/Claude Code/Codex executables; no API calls
uv run ruff check src tests
```

`SF_PI`, `SF_CLAUDE`, and `SF_CODEX` override the corresponding CLI binary paths.

Agent tuning covers both persistent role instructions and task wording. The dashboard
provides separate Markdown links for each. The improver proposes evidence-backed changes to either layer
and records them in `improvements.jsonl`. On config, play beside delete runs the configured implementer
against software-factory. Existing edits are allowed and must be preserved; related factory files may be
changed and changes remain uncommitted. The named Markdown file must exist in software-factory.
The row shows progress, a result and execution diagnostics; applied suggestions cannot be replayed, while
failures can be retried after inspecting partial changes. Only one factory run or application executes
at a time. Config edits can also still be made manually. Missing or invalid task templates fail visibly,
and the dashboard rejects an edit that breaks rendering or the JSON contract.

Every workflow agent receives an automatically discovered target-documentation inventory,
including files such as DESIGN.md and component-local AGENTS.md. Discovery includes existing
tracked and non-ignored untracked Markdown anywhere in the repository, with case-insensitive
extensions; deleted files and symlinks are excluded. Agents read the applicable documents,
while rendered task artifacts preserve the supplied paths. Improvement suggestions may name any existing
discovered document. Validation commands still come from
.sf.toml overrides or docs/VALIDATION.md.

Ideas about shared factory prompts must generalise beyond the current target, because they are applied
to `agents/*.md` for every future target; project-specific guidance belongs in the target's own
documentation. Portability is judged by the person applying the suggestion in the dashboard. Improvement
selection requires no human input; new suggestions are recorded as `decision: automatic`, and historical
decisions remain readable.

The run dashboard's **Project documents** panel lists current Markdown files from the selected
run's target, below the activity chart. Click a document button to review and edit it in the popup
file editor. Save writes the project file without committing; Cancel leaves it unchanged. If the
file changed since opening, the editor keeps your draft and asks you to reopen before saving.
Dashboard refreshes preserve open drafts. Unavailable targets report an error in the panel.
The document popup also offers **Delete file**, with confirmation and stale-file checks. Deletion
removes the project file without committing. Agent config popups have no delete action, and the
factory agents directory is protected even when a run targets the factory itself.
