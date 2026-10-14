# PLAN — implement docs/spec.md (all steps done)

## GitHub push without run artifacts — 2026-10-13

Verified runs/ is ignored and untracked; added pytest/Ruff cache exclusions. Retained the small
improvements.jsonl needed by the history widget. Audited outgoing commits and staged files: no run
artifacts or raw logs; largest staged file below 28 KB. Old large run blobs already exist in origin's
history; this push does not rewrite history or delete local runs.
Validation: full Ruff and diff whitespace checks PASS; pytest **202 passed, 5 failed**, matching the
previously documented saved-planner configuration and case-insensitive filename baseline failures.

## Improvement history widget — 2026-10-13

1. Assess the four improvements from hello1 run 20261013-191343 against artifacts and actual edits.
2. Reuse improvements.jsonl as the historical log; expose a read-only API and expandable config widget below agents.
3. Validate history ordering, empty/error handling, safe rendering and existing dashboard behavior.

Assumption: historical decisions describe intent, not confirmed application; preserve that distinction in the UI.
Assessment: all four address observed wasted review loops. Preserve pre-existing edits when interpreting
the new cleanup instruction; treat the target's Python path as an environment example rather than a portable contract.

Validation: dashboard HTTP suite **83 passed**; scoped Ruff and git diff --check **PASS**;
executed config JavaScript with DOM/API stubs: history rendering, HTML escaping and empty state **PASS**.
Browser visual/E2E check **BLOCKED**: Browser skill runtime initialization failed with
`Cannot redefine property: process`, before an agent/browser connection existed. No browser packages installed.
Acceptance: existing JSONL history is exposed newest-first, expanded entries show decision/rationale/evidence/change,
and the widget follows agent config. Empty and malformed logs have observable states; agent tests remain green.

## Configurable harnesses (implemented; validation limitations recorded)

1. Add backwards-compatible harness settings and shared execution/contracts → Pi remains the default; native adapters preserve retries.
2. Normalize telemetry and extend config/dashboard → harness selection, supported metrics, raw diagnostics, historical run compatibility.
3. Validate adapters, mixed workflows, UI and real CLI smoke runs → capture evidence and blockers.

Assumptions: native model IDs are entered explicitly; new harnesses offer low/medium/high/xhigh effort;
Codex uses role-based read-only/workspace-write permissions, not Pi's exact tool allowlist. Missing metrics
are N/A and aggregates are partial. Native permission prompts are disabled; no sandbox bypass is enabled.
Existing edits to agents/reviewer.md are preserved. CLI sessions use existing user authentication/storage.

### Validation — 2026-10-13

- Focused adapter, contract, agent-definition and mixed-workflow checks: **50 passed**.
- Full pytest suite: **87 passed, 2 failed**. Both failures reproduced with unchanged HEAD source/tests
  in a temporary baseline checkout retaining the pre-existing reviewer settings:
  `test_config_update_rewrites_only_frontmatter_lines` assumes reviewer thinking was not already `low`;
  `test_install_missing_standards_keeps_existing` assumes `AGENTS.md` and `agents.md` are distinct on
  this case-insensitive filesystem. These unrelated tests and the reviewer settings were not changed.
- Ruff, `git diff --check`, and the installed `sf --help` entry point: **PASS**.
- Browser validation with the existing Chrome installation: **PASS** — all three harness choices,
  model reset, native effort options, save/reload persistence, return to Pi with `off` effort,
  mobile layout, dashboard harness labels/activity, N/A and partial metrics; no console/page errors.
- Real authenticated Pi, Claude Code and Codex invocations and same-session resume: **PASS** —
  disposable repositories, observable tool calls, contract-valid results and usage. Final Pi check
  also passed after moving subprocess handling into the shared runner.
- Real Claude Code and Codex improvement-implementer writes: **PASS** — both factory agent files
  and target standards files were writable under the configured native permissions.
- No dependency installation, test suppression, or changes to global harness settings were required.

Acceptance: per-agent harness settings persist; omitted harness defaults to Pi; correction retries pin
settings and resume the same session; mixed-harness validation/review/approval/resume and scope checks
work; native errors and artifacts remain observable; unsupported metrics are N/A and incomplete totals
are partial; historical Pi events remain readable without duplicate tool/context points.

Validation limitation: the full suite remains non-green because of the two confirmed baseline test
assumptions above. Feature checks, native integration checks and browser checks passed.

1. Scaffold uv project (Typer, Pydantic, PyYAML, Rich, pytest) → `uv run sf --help` works.
2. Agent definitions loader + 6 agent files in `agents/` → definitions parse and validate.
3. Pi runner: JSONL stream, events.jsonl, success detection, contract validation + 2 re-prompts, usage capture → unit tests with a fake `pi`.
4. Run state, lockfile, git helpers, target config/templates → unit tests.
5. Workflow stages (branch … merge) + resume → end-to-end test with fake `pi` against a temp target repo.
6. Improve phase with decision log and change-scope enforcement → tests.
7. Docs (README, ARCHITECTURE.md) → ruff + pytest green.

## Assumptions
- The factory repo is the current working directory when `sf` runs (`runs/`, `agents/`, `templates/`, `improvements.jsonl` live there).
- `jsonschema` is added: the spec requires JSON Schema contracts and Pydantic cannot validate arbitrary JSON Schema.
- Pi binary can be overridden with `SF_PI` (used by tests' fake Pi).
- Validation commands = each non-empty line inside ```sh / ```bash fenced blocks in the target's `validation.md`; `.sf.toml` key `validation = [...]` overrides them.
- Agents return their output in the contract JSON (e.g. planner returns `{plan}`); the orchestrator writes `plan.md`, `review-<n>.md` itself.
- Run IDs are `YYYYMMDD-HHMMSS`.
- Scope rules for the documenter and improvement-implementer are enforced by the orchestrator by inspecting `git status`; violations fail the stage (documenter) or are reverted and logged (improvement).
- "Each stage committed": run artefacts are committed to the factory repo after every stage; code changes are committed on the target branch.

## Harness model discovery

1. Query native Codex app-server and Claude Code initialization catalogs without inference.
2. Populate harness dropdowns; preserve unlisted current values and expose discovery failures with manual fallback.
3. Validate fake protocol boundaries, config API, real installed catalogs and browser behavior.

Assumptions: harness catalogs define suggestions, not a validation allowlist; existing native IDs remain valid.
Use existing SF_CODEX/SF_CLAUDE overrides. Preserve previous workspace edits and Pi API fields.

Validation: native protocol tests reproduce the missing module before implementation, then pass
for discovery, pagination, notifications, timeout, missing executable, malformed/empty catalogs,
protocol errors and stderr diagnostics. Real installed Codex and Claude catalogs both returned
non-empty lists with no errors; no inference turn was submitted. Browser flow using the existing
headless Chrome script and fake catalogs passed all three dropdowns, harness reset, selection,
save/reload and console/page error checks. Browser plugin setup was blocked twice by
`Cannot redefine property: process`; used the pre-existing disposable Chrome validation setup.
Ruff and diff whitespace checks pass. Full suite retains the two previously confirmed baseline
failures documented above; no unrelated tests or reviewer settings were modified.
Final regression result: **101 passed, 2 known baseline failures**. Final Chrome check after
preserving Pi's empty-catalog behavior: **PASS**. Ruff and `git diff --check`: **PASS**.

## Agent Markdown editor

1. Add validated file read/write API → full Markdown edits persist only for known agents; invalid or stale edits leave files unchanged.
2. Add filename links and editor dialog → open, edit, save or cancel; refresh the agent row after saving.
3. Validate API, browser journey and regression checks.

Assumption: editing an agent file includes its frontmatter and prompt; the filename/name identity must remain unchanged.

Validation — agent Markdown editor:
- File API integration checks: 12 passed (all six agents, exact text roundtrip, invalid definitions/schema/name/type, stale edits and path safety).
- Chrome user journey: PASS — links under descriptions, full Markdown loaded, cancel leaves file unchanged, invalid save stays open with error, save/reload persistence, row refresh, mobile dialog fit; no console/page errors.
- Ruff and git diff --check: PASS.
- Full suite: 113 passed, the same two baseline failures documented above (reviewer already uses low thinking; case-insensitive AGENTS.md/agents.md).
Acceptance: all agents expose [name.md] links and an editable popup with Save file/Cancel; saved definitions apply to subsequent invocations. Existing settings controls and unrelated workspace changes preserved.

## Editable task prompts (implemented; baseline validation limitations recorded)

1. Add six validated task templates and render workflow context → both prompt layers are tunable.
2. Validate approved improvements and extend the dashboard editor → invalid prompts cannot be saved or committed.
3. Test rendering, workflow, improvement rollback and dashboard behaviour → record completion evidence.

Defaults: retain role filenames; one required user template per agent; fixed {{identifier}} placeholders;
Python retains workflow, permissions and contract corrections. Preserve existing workspace edits.


Validation — editable task prompts:
- Full pytest: **148 passed, 2 known baseline failures**. The failures remain
  `test_config_update_rewrites_only_frontmatter_lines` (reviewer thinking is already low)
  and `test_install_missing_standards_keeps_existing` (case-insensitive filesystem).
  Their failure evidence matches the baseline failures previously recorded above.
- New coverage passes for all six templates, one-pass literal substitution, missing/empty/
  malformed/unknown placeholders, missing context and visible workflow failure; dashboard
  roundtrips, stale saves and path safety; approved task tuning used by the next invocation;
  invalid role/task/deletion rollback including pre-existing prompt edits and newly added files;
  validation-over-review feedback precedence; preservation of unrelated local files during scope
  enforcement, including modified and deleted files.
- Existing fake Pi, Claude Code and Codex workflow/contract checks pass. No native adapter
  interface or retry changes were needed; no new live model calls were required for this change.
- Browser user journey: **PASS** — 12 role/task links, placeholder guidance, invalid save,
  save/reopen/reload persistence, cancel, role save refresh, preservation of unsaved model controls,
  and mobile task/role dialog fit. No console or page errors. Browser plugin setup failed with
  `Cannot redefine property: process`; used the existing disposable local Chrome validation setup.
  Final screenshot: `/var/folders/5q/_h7bskgj1g34tm3js29dw16c0000gn/T/sf-harness-ui-mvlnsr_9/task-editor-mobile.png`.
- Ruff and `git diff --check`: **PASS**.

Acceptance: both prompt layers are editable and available to evidence-backed improvement;
invalid templates cannot be saved or committed; failed improvements restore prompt snapshots;
workflow control, permissions, contracts and correction retries stay in Python. No dependencies
were added and pre-existing workspace changes were retained. Target standards edits from an
invalid prompt improvement remain uncommitted for inspection, as documented in ARCHITECTURE.md.

## Documenter drift prevention (implemented; baseline validation limitations recorded)

1. Permit updates to existing guidance docs → architecture, validation, coding standards and AGENTS.md stay accurate.
2. Align role/task prompts and documented scope → changes describe implemented behaviour; policy tuning remains in improve.
3. Validate allowed documentation updates and forbidden code/root-file changes → regression evidence.

Assumption: all existing Markdown guidance can need factual updates. The documenter preserves normative
requirements, never weakens checks to excuse failures, and reports policy changes needed rather than applying them.
The orchestrator continues to commit. New documents remain restricted to docs/.


Validation — documenter drift prevention:
- Reproduced the old failure before implementation: a complete workflow rejected updates to
  AGENTS.md and all three docs guidance files with `documenter changed files outside its scope`.
- Focused workflow, mixed-harness and agent checks: **56 passed** after the fix. This includes
  successful guidance/README/new-doc updates and orchestrator commits, plus rejection and
  reversion of code, runtime configuration and newly created root Markdown files.
- Ruff and `git diff --check`: **PASS**.
- Prompt and scope contracts now agree: factual updates to guidance are permitted, normative
  requirements are preserved, discrepancies are reported, and the orchestrator owns commits.
- Browser validation is not applicable: no dashboard or browser code changed.

- Full regression suite: **152 passed, 2 known baseline failures** (unchanged reviewer-low
  assumption and case-insensitive AGENTS.md/agents.md assumption documented above).
- Acceptance: the documenter can update all four guidance files to prevent factual drift;
  code/config/root-creation restrictions and orchestrator commits remain verified. No new blockers.

## Automatic documentation discovery (implemented; baseline validation limitations recorded)

1. Discover tracked/non-ignored Markdown throughout the target → every agent receives a fresh path inventory.
2. Freeze discovered paths for approved improvements → existing docs are editable, new files and code remain outside scope.
3. Validate discovery, context, improvement permissions and regressions → record evidence.

Defaults: paths only, case-insensitive .md suffix, no symlinks/deleted files; Git excludes ignored untracked
files. Required {{documentation}} placeholder in all six templates. Existing validation sources and
starter standards stay unchanged. Preserve local prompt customisations and workspace edits.


Validation — automatic documentation discovery:
- Focused discovery, workflow, mixed-harness and template checks: **68 passed**, with the
  previously documented case-insensitive-filesystem baseline failure in target standards setup.
- Coverage includes nested/root/component docs, case-insensitive extensions, ignored and tracked
  files, deleted files, symlinks, sorted unique inventories, empty inventories, unusual filenames,
  fresh next-invocation context, all six agents receiving target context rather than factory docs,
  visible discovery failures, pinned correction prompts, and frozen improvement permissions.
- An approved DESIGN.md update was committed; code and newly created target docs were reverted.
  Existing rollback and local-edit preservation checks pass with the expanded document scope.
- Ruff and `git diff --check`: **PASS**.
- No dashboard flow changed; existing API checks validate the dynamically supplied placeholder
  registry. No new dependency or live model invocation is needed for deterministic Git discovery.

- Full regression suite: **159 passed, 2 known baseline failures**. Failure evidence remains
  the reviewer-already-low expectation and the case-insensitive AGENTS.md/agents.md expectation.
- Acceptance: all six agents receive automatically discovered target Markdown paths; relevant
  document reading is instructed beyond hard-coded filenames; approved improvements use one
  frozen inventory for context, allowed paths and commits; new files and code remain out of scope.
  Rendered prompts retain the inventory and discovery failures remain observable. No new blockers.

## Shared-prompt portability gate (implemented; baseline validation limitations)

1. Define portable improvement guidance and a read-only improvement reviewer → project-specific rules stay in target docs.
2. Review actual shared edits before commits using pinned pre-improvement instructions → rejection or review failure restores prompts and stops the stage.
3. Verify approval, rollback, target-only bypass, pinned self-review and harness/dashboard integration → record completion evidence.

Defaults: Codex gpt-6.1-sol / medium; shared prompts only; no automatic repair; existing human decision history unchanged.
Preserve existing workspace changes. Target documentation remains uncommitted on gate failure.

Validation — shared-prompt portability gate:
- Focused workflow, mixed-harness, Pi/native runner and dashboard checks: **122 passed, 1 deselected**. The deselected pre-existing configuration-update test passed in the full-suite run.
- Shipped definitions and literal-context template rendering: **14 passed** across all seven agents.
- Full suite: **167 passed, 5 pre-existing failures**. Four tests in test_agents.py assume the saved planner has no harness and high thinking; the actual saved definition has Claude Code and low thinking. These four failed before implementation. test_install_missing_standards_keeps_existing assumes AGENTS.md and agents.md differ on this case-insensitive filesystem; this baseline issue is recorded above. No assertions were weakened.
- Ruff and git diff --check: **PASS**.
- Initial baseline/full attempts exposed Pi tests dispatching to the real saved Claude harness. Those runs were interrupted. Workflow fixtures and explicitly Pi-specific runner tests now select Pi independently of saved production configuration, while mixed-harness tests explicitly configure and exercise fake native executables. Production models/settings were preserved.

Acceptance:
- Portable shared edits require explicit approval with zero findings before improvement commits: **PASS** — workflow approval and mixed-harness coverage.
- Rejection, invalid output and invocation failure restore original prompts, including local edits and added/deleted files, and fail without either improvement commit: **PASS**.
- Target-only improvements skip review, commit target docs and leave unrelated local prompts uncommitted: **PASS**.
- Reviewer role, template and configuration are pinned before implementation; edits to its own instructions are reviewed using the prior version; read-only tools are enforced even if write tools are configured: **PASS**.
- Input/result artifacts retain the reviewed changes and pinned reviewer, events retain failures, and dashboard configuration/artifact APIs expose the new reviewer: **PASS**.

Limitations: model judgement accuracy was not evaluated with a live portability reviewer; fake-agent tests prove enforcement, contracts, rollback and observable state. Full-suite green validation remains blocked by the five baseline failures above. No dependencies were added, no implementation repair loop or history migration was introduced, and existing workspace changes were preserved.

## Automatic improvements (implemented; baseline validation limitations)

1. Apply every non-rejected improver suggestion without a terminal decision → improve no longer waits for human input.
2. Remove improvement-only human prompts and note placeholders → interfaces and documentation reflect automatic selection.
3. Verify automatic selection, historical rejection filtering, portability rollback and remaining human controls → record evidence.

Defaults: retain historical decisions without migration; record new selections with decision `automatic`; preserve the portability gate, structural validation, file scope and stop-on-failure behaviour. Plan approval and loop-cap decisions remain unchanged.

Validation — automatic improvements:
- Reproduced the prior human dependency with test_improvements_apply_without_a_human_interface: **FAIL before implementation**, with AttributeError at wf.human.review_idea; **PASS after implementation**.
- Focused workflow and mixed-harness checks: **42 passed**. Coverage verifies automatic application of multiple suggestions, historical rejection filtering, decision history, unchanged plan/cap controls, structural validation, scope enforcement and portability rejection/rollback.
- Full suite: **168 passed, 5 unchanged baseline failures** — the four saved-planner configuration assumptions in test_agents.py and the case-insensitive AGENTS.md/agents.md assertion in test_target.py, as documented above.
- All seven agent definitions/task templates validate; dashboard template tests cover the removal of human_note. Ruff and git diff --check: **PASS**.

Acceptance: improve no longer depends on a human callback and every non-rejected suggestion is selected automatically; no improvement terminal prompt or human-note field remains. New selections use decision `automatic` without rewriting historical records. Portability gating, rollback, scope and stop-on-failure remain enforced, while optional plan approval and loop-cap decisions remain unchanged. Production configuration and unrelated workspace changes were preserved. Full-suite green remains blocked by the five baseline failures above; no live model call was required for this change.

## Dashboard project documents (implemented; baseline validation limitations)

1. Expose the selected run's current target Markdown inventory and safe read/save endpoints → existing project docs can be reviewed and edited.
2. Add document buttons below Activity and above Stage results/Loops → open a modal matching the existing file editor.
3. Verify API scope/conflicts, UI save/cancel/polling, mobile fit and browser diagnostics → record evidence.

Defaults: use discover_documentation for tracked/non-ignored existing Markdown; edit current working-tree files without committing; reject stale saves with HTTP 409 and paths outside the inventory. Keep the modal outside the polled main content so refreshes preserve drafts. Missing/unavailable projects show an observable panel message rather than breaking run detail.

Validation — dashboard project documents:
- Dashboard API tests: **64 passed**. New cases cover current inventory refresh, nested/special filenames, save/read-back without commits, invalid payloads, stale originals, deleted/symlink-replaced docs, unavailable targets, ignored docs, code and path traversal rejection.
- Full suite: **183 passed, 5 unchanged baseline failures** — the four saved-planner configuration assumptions and the case-insensitive AGENTS.md/agents.md assertion recorded above.
- Browser user journey: **PASS** — exact panel placement, root/nested document buttons, editor load, save/reload persistence, Cancel leaving files unchanged, HTTP 409 retaining drafts and concurrent changes, polling preserving open drafts, special filenames, switching projects, mobile editor and 120-character filename button fit. No console or page errors.
- Browser skill bootstrap failed with Cannot redefine property: process and exposed no browser binding. Used the pre-existing disposable local Chrome setup and native debugging protocol, without installing dependencies or altering the user's browser profile. Early disposable-check failures were corrected (test expression escaping; immediate close/reopen request handling). Final screenshots were inspected: /tmp/sf-docs-ui-_mu7uhpv/documents-panel.png and documents-editor-mobile.png.
- A pre-existing mobile run-header issue surfaced with the long macOS temporary project path: its unbroken absolute path can overflow the viewport. Kept that unrelated header behaviour unchanged; verified the new panel, long filename buttons and modal separately and ran the final full-page mobile check with a shorter disposable project path.
- Ruff and git diff --check: **PASS**.

Acceptance: the selected run's current Markdown documents appear as buttons immediately below the chart and above Stage results/Loops; clicking opens the matching popup editor and saves to the correct project file. Safe scope and stale-edit handling are enforced at the API, drafts survive polling, and inaccessible projects have observable panel errors. Existing workspace changes were preserved. Full-suite green remains blocked only by the five baseline failures above.

Live dashboard: restarted the existing local server at 127.0.0.1:8765 with the same settings to load the document endpoints; the live inventory and read endpoint passed without editing project files.

## Delete project documents (implemented; baseline validation limitations)

1. Add inventory-scoped document deletion with stale-file detection → remove unwanted project Markdown without committing.
2. Add a confirmed Delete file action only to the project-document modal → close and refresh the inventory on success; keep errors/drafts visible on failure.
3. Verify deletion, cancellation, conflicts, scope and agent-configuration exclusion through API and browser checks → record evidence.

Defaults: delete only the opened existing project document after explicit UI confirmation; require its original content to match; protect the configured factory agents directory even if it is listed through a run targeting the factory. Leave the agent-config popup unchanged; do not start or restart the main dashboard service.

Validation — delete project documents:
- Focused deletion/API checks: **20 passed**. Coverage includes tracked/untracked and special-name docs, inventory refresh, no commits, invalid bodies, stale originals, traversal/code/ignored/symlink rejection, and both role/task configuration files being protected through config and factory-targeted project routes.
- Full suite: **201 passed, 5 unchanged baseline failures** — the four saved-planner configuration assumptions and case-insensitive AGENTS.md/agents.md assertion recorded above. All 82 dashboard tests passed.
- Browser journey: **PASS** — document Delete action, native confirmation with filename, declined confirmation preserving draft/file, stale deletion preserving concurrent changes and editable draft, three-button mobile fit, confirmed deletion and inventory/reload persistence, and absence of Delete in the agent-config modal. No console or page errors. Screenshot inspected: /tmp/sf-docs-ui-026ysnkf/delete-editor-mobile.png.
- Browser skill bootstrap again failed with Cannot redefine property: process; used the existing disposable local Chrome setup without installation or the user's browser profile.
- Ruff and git diff --check: **PASS**.

Acceptance: Delete file is available only for deletable project documents, requires confirmation, closes/refreshes after success and preserves drafts on errors. The server enforces current-inventory scope, stale-content checks and protection of the configured factory agents directory, including when the factory is the target project. No real project file was deleted during validation. Existing workspace edits were preserved. The main dashboard was not started, stopped or restarted for this change; an independently restarted process was observed on port 8765 and left untouched. Restarting that process is needed to load the new endpoint. Full-suite green remains blocked by the five baseline failures above.

## Watch-folder prompt source (implemented; baseline validation limitations)

1. Give every run a unique id → two runs started inside one second keep separate run directories, state and event streams.
2. Add `mailbox.py` (todo/doing/done layout, oldest-first candidates, settle guard, collision-safe claim/complete, monitor loop) → the queue is provable without sleeping, running forever or calling a model.
3. Wire `sf watch --target <repo> <folder>` with per-run `mailbox_*` events → each dropped Markdown prompt becomes exactly one run and reaches `done/` only when that run succeeds.

Defaults: "oldest" is modification time with ties broken by name; a prompt is any regular `*.md` directly in `todo/` (case-insensitive), no recursion; ~2 s poll and ~1 s settle are module constants (no CLI flag); a failed or aborted prompt stays in `doing/` for `sf resume` and is never retried automatically; the run lock is taken per run and released between runs so `sf resume` still fits; monitor messages go to the terminal plus each run's `events.jsonl`, because the factory has no factory-wide log file. Empty or whitespace-only files are valid prompts. `sf run`, `sf resume`, `sf improve` and `sf dashboard` are unchanged, and `sf run`'s exit-code mapping was moved into `_execute` around a shared `_drive` rather than duplicated.

Validation — watch-folder prompt source:
- Focused `uv run pytest -q tests/test_mailbox.py tests/test_workflow.py`: **62 passed** (23 mailbox, 39 workflow). Each new behaviour was first seen failing for its expected reason: the same-second test asserted two identical run ids (`20261013-203515 == 20261013-203515`) before `new_run` changed, and the mailbox file did not import before the module existed.
- 18 targeted mutations of the finished code were each caught by a failing test: settle window, oldest-mtime ordering, case-insensitive `.md`, regular-file check, collision suffixes, unhandled execution errors, unhandled Ctrl+C, claiming while the queue is busy, unique run ids, each `mailbox_*` event, per-run lock acquisition, the watch's busy-lock wiring, up-front target check, queue layout creation, queue path output, stage-failure reporting, `RunLock.holder()` and stale-lock takeover. The up-front-target-check mutation first revealed that its test could hang rather than fail; that test now stubs the watch loop and bounds its polling.
- End-to-end with the scripted fake Pi (no live harness): two dropped prompts produced two distinct runs whose `runs/<id>/prompt.md` equalled each file's content, both files landed unchanged in `done/`, `doing/` ended empty and both run branches were merged to the target trunk. A failing first run left its file in `doing/` unchanged, printed `resume with: sf resume <id>`, recorded `mailbox_failed` with the outcome and resume command, and the next prompt still ran. A lock held by a live pid kept a prompt in `todo/` and it ran after release; `RunLock` holder reporting and stale-lock takeover are covered by the new `tests/test_run.py`.
- Real CLI session (`sf watch --target <disposable repo> <folder>` from a copy of the factory with the fake-Pi harness, then `kill -INT`): the three absolute queue paths printed on start; `notes.txt` and `nested/` were never claimed while `.MD` was; a file appended to across ~8 polls was claimed only after the writes stopped; the failed prompt stayed in `doing/` with its run `failed` at stage `document`; SIGINT (Ctrl+C) printed the stop line and exited **0**. `sf run` exit codes measured unchanged after the `_drive`/`_execute` split: **0** success, **1** stage failure, **2** rejected plan.
- Observability without touching the dashboard: `/api/runs/<run-id>` served over that session showed `mailbox_claimed`/`mailbox_completed`/`mailbox_failed` in the `source: "sf"` lane with file name, watch root, outcome and resume hint, and `prompt.md` remained listed as an artefact.
- `uv run sf --help` lists run/watch/resume/improve/dashboard; `uv run sf watch --help` shows the required `--target`, the `folder` argument and `--approve-plan` (default off). Ruff and `git diff --check`: **PASS**.
- Type checking is not configured for this repo (`mypy`/`pyright` are not project dependencies and `uvx --offline mypy` has no cached package). The environment's pre-installed `pyright 1.1.411` was run anyway: **0 errors** in `mailbox.py`, `cli.py`, `run.py`, `workflow.py` and both new test files. Its 10 remaining `src` errors (in `dashboard.py` and `models.py`) and 21 test errors are all in untouched code.
- Full suite: **227 passed, 5 failures**, the same set as the pre-change baseline (measured 202 passed, 5 failed on this tree before any edit). Those five are the saved-agent-configuration assumptions (three in `test_agents.py`, one in `test_dashboard.py`'s improvement-reviewer expectations) and the case-insensitive `AGENTS.md`/`agents.md` assertion in `test_target.py`; the recorded baseline wording names four in `test_agents.py`, but this tree's saved planner has Pi configuration, so the count is now three plus the dashboard one. Nothing was fixed or weakened here. One additional flake appeared in a single full-suite run (`test_model_discovery.py::test_native_discovery_failure[error-claude-code]`, which allows the fake CLI only 0.2 s): reproduced independently by running that test under CPU load with unchanged code, and it passed in every later run.

Acceptance:
- `sf watch --target <repo> <folder>` creates `todo`/`doing`/`done` when missing and takes no prompt argument: **PASS** — CLI-command tests through Typer plus the real CLI session.
- A dropped Markdown file starts a run whose `prompt.md` is that file's content, sits in `doing/` during the run and in `done/` unchanged afterwards: **PASS** — fake-Pi end-to-end and the real CLI session.
- Oldest `todo/` file first, never overlapping runs, lock held during each run and released between them: **PASS** — ordering tests, monitor claim assertions, lock-holder probe per run, busy-lock waiting test.
- The monitor polls until Ctrl+C (exit 0); a failed or aborted run leaves its file in `doing/`, is reported on the terminal with the resume hint and in that run's events, and the watch continues: **PASS** — interrupt tests, failure end-to-end, real SIGINT session.
- Non-Markdown files, subdirectories, directories named `*.md` and files still being written are not claimed: **PASS** — candidate tests plus a live file appended across polls.
- `sf run`, `sf resume`, `sf improve` and `sf dashboard` unchanged: **PASS** — unchanged sources for the three commands plus measured exit codes, and no dashboard change while its sf lane still shows every `sf` event.

Limitations: the monitor's per-file guard catches any `Exception` (justified `# noqa: BLE001`) so one bad prompt cannot end the watch; a permanently failing prompt accumulates in `doing/` until someone clears it; polling costs one `stat` sweep per interval; no live Pi/Claude/Codex call was made, so model behaviour is out of scope here, as it is for the fake-agent tests it relies on.
