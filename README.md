# sf — software factory

Drives the [Pi](https://pi.dev) coding agent through a fixed workflow
(prompt → plan → implement ⇄ review → document → merge → improve). Spec: [docs/spec.md](docs/spec.md).

## Usage

Run from the root of this repo (the factory repo):

```sh
uv run sf run --target ../my-app "Add a /health endpoint"   # or --prompt-file req.md
uv run sf run --target ../my-app --approve-plan "..."        # human approves plan.md first
uv run sf resume <run-id>                                    # continue after a failure
uv run sf improve <run-id>                                   # re-enter the improve phase
uv run sf dashboard [--port 8765]                            # web dashboard at http://127.0.0.1:8765
```

The dashboard is read-only and refreshes every 2 s: every run's status, the stage pipeline, review/validation
results per loop, token usage, artefacts (plan, reviews, validation logs, stderr), orchestrator events and tool calls.
Runs are read-only; the **config** page (`/config`) sets each agent's model and thinking level by rewriting those two
frontmatter lines in `agents/<name>.md` (model suggestions come from `pi --list-models`).

Exit codes: `0` done, `1` stage failed (resumable), `2` aborted (plan rejected, loop cap abort, merge conflict).

## Layout

| Path | Purpose |
|---|---|
| `agents/<name>.md` | Agent definitions: YAML frontmatter (model, thinking, tools, JSON Schema contract) + system prompt |
| `templates/` | Starter `docs/coding-standards.md` and `docs/architecture.md` for targets that lack them (`AGENTS.md` and `docs/VALIDATION.md` are downloaded from [agents-default](https://github.com/yandaq/agents-default)) |
| `runs/<run-id>/` | `events.jsonl`, `run-state.json`, `sessions/`, `pi-<stage>-<n>.stderr`, `prompt.md`, `plan.md`, `review-<n>.md`, `validation-<n>.log` |
| `improvements.jsonl` | Every improve-phase decision; rejected titles are never re-proposed |
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
uv run pytest -q        # uses tests/fake_pi.py via SF_PI; no API calls
uv run ruff check src tests
```

`SF_PI` overrides the Pi binary path.
