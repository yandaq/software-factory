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

Exit codes: `0` done, `1` stage failed (resumable), `2` aborted (plan rejected, loop cap abort, merge conflict).

## Layout

| Path | Purpose |
|---|---|
| `agents/<name>.md` | Agent definitions: YAML frontmatter (model, thinking, tools, JSON Schema contract) + system prompt |
| `templates/` | Starter `coding-standards.md`, `architecture.md`, `validation.md`, `agents.md` copied into targets that lack them |
| `runs/<run-id>/` | `events.jsonl`, `run-state.json`, `sessions/`, `pi-<stage>-<n>.stderr`, `prompt.md`, `plan.md`, `review-<n>.md`, `validation-<n>.log` |
| `improvements.jsonl` | Every improve-phase decision; rejected titles are never re-proposed |
| `src/sf/` | Orchestrator (see [ARCHITECTURE.md](ARCHITECTURE.md)) |

## Target configuration

Optional `<target>/.sf.toml`:

```toml
trunk = "main"                      # default
validation = ["uv run pytest -q"]   # overrides the commands in validation.md
```

Without an override, validation runs every line of the ```` ```sh ```` blocks in the target's `validation.md`.

## Development

```sh
uv run pytest -q        # uses tests/fake_pi.py via SF_PI; no API calls
uv run ruff check src tests
```

`SF_PI` overrides the Pi binary path.
