# Dashboard HTTP API

`sf dashboard [--host 127.0.0.1] [--port 8765]` serves a polled JSON API over `runs/*/` plus the pages
`/` (run dashboard) and `/config` (agent configuration and improvement history).
The implementation is `src/sf/dashboard.py`; the module table and behavioural notes are in
[ARCHITECTURE.md](../ARCHITECTURE.md).

## Modes

The server runs in one of two modes, chosen at start-up:

- **File mode** (no `SF_DATABASE_URL`): everything below reads `runs/*/` and `improvements.jsonl`.
- **DB mode** (`SF_DATABASE_URL` set): runs, jobs, runners and improvements come from Postgres and run
  artefacts from the S3 artifact store (`SF_S3_*`). The differences are listed in *DB mode* below; routes
  not mentioned there behave as in file mode (config edits still change the dashboard's local `agents/`).

## Access model

The server is a stdlib `ThreadingHTTPServer` with **no authentication, no CSRF protection and no TLS**, and
every request is treated as a local, trusted operator. That matters because the API can write files in the
factory and in any run's target, change agent harness/model/effort settings, delete project documents and
start runs and improvement executions that commit. Keep the default loopback bind address; `--host` exists for
deliberate exposure on a trusted network only. Responses always send `Cache-Control: no-store`, the request log
is silenced, and a client that disappears mid-response is ignored rather than logged.

Responses are JSON unless stated otherwise. Errors are `{"error": "<message>"}`; on success most mutating
routes answer `{"error": null}` plus their payload fields. Unknown routes answer 404.

## Runs

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/runs` | Newest-first summary per `runs/*/run-state.json`: `run_id`, `status`, `stage`, `loop`, `target`, `cap_reached`, plus `can_retro` (true once `merge` completed), `cost`, `cost_partial` and `cost_estimated`. A run whose state file cannot be read appears as `status: unreadable` with the error, and stays listable and deletable. |
| `GET` | `/api/runs/<id>` | Run detail, or 404 when the run has no `run-state.json`. Fields: `state` (the whole `run-state.json`, including `tokens`), `agents` (latest harness/model/thinking per agent, reconstructed from `pi_start` argv for historical Pi runs), `events` (the last 200 `sf`-source events), `tools` (the last 200 `tool_execution_start` events), `timeline` (`[ts, lane, type, context, invocation]` points, excluding token-stream `message_update` events), `artefacts`, `documents`, `documents_error`, `events_updated`. A target that cannot be read leaves `documents` empty and fills `documents_error` instead of failing the request. |
| `GET` | `/api/runs/<id>/<artefact>` | The artefact as `text/plain`. The name must contain no `/` and start with one of `prompt.md`, `plan.md`, `review-`, `portability-`, `validation-`, `pi-`, `claude-code-`, `codex-`, `retro.log`; anything else is 404. |
| `DELETE` | `/api/runs/<id>` | `{}` and removes the run directory. 409 while it is an improvement execution that is still `running`. |

## Run project documents

The document editor reaches the Markdown files inventoried through Git in *that run's* target — never an
arbitrary path. `path` is a repository-relative name taken from the run detail's `documents` list and is
supplied as a query parameter.

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/api/runs/<id>/document?path=<name>` | — | `{"content", "deletable"}`; `deletable` is false for a file inside the factory's `agents/` directory. 404 when the name is not in the current inventory. |
| `PUT` | `/api/runs/<id>/document?path=<name>` | `{"content", "original"}` | Writes the working-tree file without committing. 409 with "File changed since opening. Reopen it before saving." when `original` no longer matches the file; nothing is written. |
| `DELETE` | `/api/runs/<id>/document?path=<name>` | `{"original"}` | Deletes the file, no commit. 403 for a path inside `agents/`, 409 when stale, 404 when it left the inventory. |

## Agent configuration

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/api/config` | — | `agents` (`name`, `purpose`, `harness`, `model`, `thinking`), `thinking_levels` (the full Pi vocabulary), `models`/`models_error` for Pi, and `harnesses` — one entry per harness with its allowed `thinking_levels` and its discovered `models`/`models_error`. Catalog discovery runs concurrently and never fails the request: a CLI that will not answer yields an empty list plus a diagnostic so the page can offer manual entry. |
| `PUT` | `/api/config/<agent>` | `{"model", "thinking", "harness"?}` | Rewrites only those frontmatter fields. Omitting `harness` keeps the agent's current selection; a `harness` line is added only when the target harness is not Pi or the file already has one. 400 for an unknown agent, a model that is not one non-empty line, an effort the harness does not offer, a definition that would not parse, or a value that would not survive YAML quoting. |
| `GET` | `/api/config/<agent>/file` | — | `{"content"}` — the agent definition. 404 for an unknown agent or unreadable file. |
| `GET` | `/api/config/<agent>/user-file` | — | `{"content", "placeholders"}` — the task template plus its required placeholder list. |
| `PUT` | `/api/config/<agent>/file` | `{"content", "original"}` | Parses the submitted definition and requires its `name` to match the file before writing. Returns the parsed `name`, `purpose`, `harness`, `model`, `thinking`. |
| `PUT` | `/api/config/<agent>/user-file` | `{"content", "original"}` | Validates the template's placeholders against the fixed vocabulary before writing; returns `{"error": null, "name"}`. |

Both `PUT` file routes answer 409 when `original` no longer matches the file on disk, so a stale editor tab
cannot overwrite a newer one. Settings apply to the next agent invocation; contract-correction retries keep the
settings of the invocation they correct.

## Retro

`POST /api/runs/<id>/retro` re-enters the improve phase by starting `sf improve <id>` as a child of the
dashboard server, using the interpreter that serves the page. The child owns the run — same run lock, same
`run-state.json` and `events.jsonl` — so the page shows it live, and its console is captured in
`runs/<id>/retro.log`. It records suggestions only; it never applies them.

`{"error": null}` on success. 404 unknown run; 409 when the run has not merged or a live `sf` process holds the
run lock; 500 when the log could not be written or the child could not start.

## Improvements

Records are identified by the triple `ts`, `run_id` and `title`; see
[improvements-log.md](improvements-log.md) for the record shape.

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/api/improvements` | — | Newest-first list of every recorded suggestion. 500 when the log cannot be read. |
| `POST` | `/api/improvements/apply` | `{"ts", "run_id", "title"}` | 202 `{"error": null, "run_id": "<execution run>"}` once the factory run lock is taken and the execution is reserved. 400 malformed body, 404 record no longer present, 409 when the lock is held or the suggestion is already running or applied or names a file that is not an existing factory Markdown document or `src/sf/` Python module, 500 otherwise. The worker runs in the dashboard process. |
| `DELETE` | `/api/improvements` | `{"ts", "run_id", "title"}` | Removes that one record and rewrites the log verbatim without it, creating no commit. 400 malformed body, 404 identity no longer in the log, 409 while its execution is `running`, 500 when the log cannot be parsed. |

## Polling

`/` re-fetches the run list and the selected run every 2 s. `/config` loads `/api/config` on demand and polls
`/api/improvements` every 1.5 s while an execution is running, retrying after 3 s when a poll fails. Mutation
requests invalidate older in-flight polls so a late response cannot restore a deleted entry or a stale status.
Python changes applied by an execution need a restart of any long-running `sf watch` or `sf dashboard` process;
prompt changes are picked up at the next agent invocation.

## DB mode

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/runners` | `{"runners": [...], "queued": <n>}` — every registered runner (`id`, `host`, `caps`, `concurrency`, `last_heartbeat`, `heartbeat_age` in seconds, `stale` — no heartbeat for 120 s — and `held` claimed jobs) plus the count of queued jobs. In file mode both are `null`, which tells the page to hide the runners panel. |
| `GET` | `/api/runs` | Newest-first from the `runs` table; same summary fields. |
| `GET` | `/api/runs/<id>` | As in file mode, plus `jobs` (each stage job: `id`, `stage`, `attempt`, `status`, `claimed_by`, `infra_retries`, `created_at`, `outcome`, `branch_sha`, `artifact_prefix`), `artifacts` (`job_id`, `stage`, `name`, `key`, `url` — a presigned artifact-store URL valid for an hour), `artifacts_error` (set instead of failing when the store cannot be listed) and `can_resume` (parked and not at the loop cap). `artefacts` lists the run documents stored in Postgres (prompt, plan, reviews). |
| `GET` | `/api/runs/<id>/<artefact>` | Served from the `run_documents` table, same name rules. |
| `POST` | `/api/runs/<id>/resume` | Re-enqueues the parked run's latest failed stage job: `{"error": null, "job": {...}}`. 404 unknown run; 409 when the run is not parked or is parked at the loop cap. Only routed in DB mode. |
| `POST` | `/api/runs/<id>/retro` | Always 409: retro is not available for queued runs in v1. |
| `DELETE` | `/api/runs/<id>` | Deletes the run's rows; its artifact-store objects are kept. |
| `GET`/`DELETE` | `/api/improvements` | Read and delete rows of the `improvements` table. |
| `POST` | `/api/improvements/apply` | Same responses; the worker thread clones the factory remote, applies, validates, commits and pushes under a per-remote advisory lock. See [improvements-log.md](improvements-log.md#queued-applications). |
