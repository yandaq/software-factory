# PLAN — implement docs/spec.md (all steps done)

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
