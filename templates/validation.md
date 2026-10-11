# Validation

The factory runs every command in the `sh` blocks below from the repository root after each
implementation loop. Any nonzero exit fails validation. With no commands, validation is skipped.

```sh
# add commands, e.g.:
# uv run pytest -q
```
