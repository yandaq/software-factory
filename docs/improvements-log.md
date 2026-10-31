# `improvements.jsonl`

The factory's improvement history: one JSON object per line, committed at the factory root. It records what the
improve phase *proposed*, and — separately — what a human later chose to *apply*. Nothing in a run applies a
suggestion; application is an explicit dashboard action.

Writers are `src/sf/improve.py` and `src/sf/apply.py`, reached from the improve stage and from the dashboard
routes in [dashboard-api.md](dashboard-api.md).

## Queued (DB) mode storage

With `SF_DATABASE_URL` set, queued runs keep the same records in the Postgres `improvements` table instead of
this file: one row per record, `record` (jsonb) holding exactly the object described below and `id` (bigserial)
preserving log order. Reads, deletes and application updates go through `src/sf/store.py`; a row is locked
(`SELECT … FOR UPDATE`) while an application is reserved, so the file lock and atomic rewrite below do not
apply there. The improve job inserts rows and commits nothing to the factory repository.

## Concurrency and durability

Every read and write of the log takes an advisory exclusive lock on `runs/.improvements.lock`, which is a
separate file so it survives replacement of the log itself and also coordinates the CLI's improve writers with
dashboard threads. Rewrites (delete, application metadata) write a temporary file and replace the log atomically,
so a reader never sees a half-written log; the surviving lines are copied verbatim, so touching one record never
reformats another. A log line that cannot be parsed fails the operation visibly instead of being skipped or
rewritten.

## Identity

A record is named by the triple **`ts`, `run_id`, `title`** (`decision_identity`), never by its position. Apply,
delete and metadata update all take that triple, so a dashboard page that is out of date cannot act on somebody
else's entry: an identity that is no longer in the log is a 404 and nothing is rewritten.

## Suggestion record

Written by the improve stage, one line per idea that survives the rejection filter, in the improver's own order:

| Field | Meaning |
|---|---|
| `ts` | UTC ISO-8601 timestamp of the recording. |
| `run_id` | The run whose evidence produced the idea. Doubles as part of the identity. |
| `title` | One-line summary. Compared case- and whitespace-insensitively against earlier decisions. |
| `file` | The single primary change target: `agents/<name>.md`, `agents/<name>.user.md`, a Python module under `src/sf/`, or a path copied from that run's target-documentation inventory. Repository-relative; the improver's JSON contract rejects an absolute path, a `..` segment and any other extension. |
| `rationale` | Why the change is needed. |
| `evidence` | Concrete pointers into the run logs, code or docs. |
| `proposed_change` | The primary edit. For a `src/sf/` module it must also state the expected behaviour and the focused regression test to add. |
| `decision` | `automatic` for every record the current factory writes: the idea survived filtering and was recorded, nothing more. Records from earlier versions of the factory carry the human decision values they recorded; only `rejected` still has an effect — a previously rejected title is never proposed again. Any other historical value is inert and remains readable. |
| `note` | Empty for records written by the improve stage; kept for historical human notes. |

The improver is shown the `title`, `decision` and `note` of every existing record, so it can honour the
no-re-proposal rule. Recording an idea is not a promise that it was applied, and a retro (a re-run of the improve
phase over a historical run) may legitimately record a title that is already in the log, because only previously
**rejected** titles are filtered.

`file` is the *named* target, not a sandbox: applying an idea may require related code, tests and docs. Applying
additionally requires the named file to exist — a Markdown file in the factory's discovered documentation
inventory, or a tracked, non-ignored, non-symlink `.py` file under `src/sf/` that resolves inside the factory.
Target application Python files stay out of scope.

## `application` metadata

Present only once a human applies the suggestion from `/config`. Each attempt replaces the whole object, so the
log holds the latest attempt, not a history:

| Field | Meaning |
|---|---|
| `run_id` | The `kind: improvement` execution run created for the attempt — its directory holds the prompt, native output, validation log, usage and events. `sf resume` refuses these runs; retry from the dashboard. |
| `status` | `running`, `applied` or `failed`. |
| `pid` | The dashboard process owning the attempt. When that pid is gone at startup, the attempt is marked `failed` with "Implementation interrupted; inspect partial changes before retrying." |
| `started_at`, `finished_at` | UTC ISO-8601 timestamps. |
| `summary` | The implementer's contract summary on success. |
| `error` | Why it failed. A failure leaves partial working-tree changes in place for inspection and stays retryable. |

An attempt is `applied` only when the agent returned a contract-valid summary, the factory's configured
validation commands passed (or none are configured, in which case the agent's own validation evidence is what was
recorded) **and** the commit succeeded or there was nothing to commit. The metadata and the commit share the log
lock, so a reader cannot observe `applied` before the commit exists; a failed commit restores the previous
metadata and the attempt is recorded as failed.

## Commits

- The improve stage commits only this file, as `sf <run-id>: improvement decisions`.
- A successful application commits all factory working-tree changes — including pre-existing edits — on the
  current branch as `sf <execution run-id>: apply improvement — <title>`. It is never pushed, and no empty commit
  is created. The resulting SHA lives in the execution run's `results.commit` and a `git_commit` event, inside the
  git-ignored run directory, rather than in this committed log.

## Deletion

`DELETE /api/improvements` removes the first record matching the triple and creates no commit. A record whose
execution is still `running` cannot be deleted, and neither can its execution run directory.

## Queued applications

In DB mode an application runs in a dashboard thread against a fresh clone of the factory remote, never the
dashboard's checkout, serialised per remote by a Postgres advisory lock. Its `application` object has the fields
above plus:

| Field | Meaning |
|---|---|
| `host` | Host name of the dashboard that owns the attempt. Interruption recovery only fails `running` attempts from the same host whose `pid` is gone ("Implementation interrupted; nothing was pushed. Retry the suggestion."). |
| `commit` | SHA of the commit pushed to the factory remote's current branch, or null when nothing changed or the attempt failed. |
| `artifact_prefix` | Where the execution's run directory (prompt, native output, validation log, events) was uploaded in the artifact store: `improvements/<execution run-id>/`. |
| `artifact_error` | Present only when that upload failed; the outcome is still recorded. |

Unlike the local path, a successful queued application **is pushed**; a failed push (for example non-fast-forward)
fails the attempt, which stays retryable. Deleting a row creates no commit.
