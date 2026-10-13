Analyse run {{run_id}} of the software factory and propose improvements to the agent definitions, role prompts, task templates and existing target documentation.

- Generated code: target repository at {{target}} (merged into {{trunk}}); see `git log {{trunk}}` and the run branch {{branch}}.
- Run logs: {{run_dir}} (events.jsonl, review-*.md, validation-*.log, run-state.json).
- Agent definitions and task templates: {{agents_dir}}.
- Review loop cap reached without approval: {{cap_reached}}.

# Past decisions (never re-propose a rejected idea)

{{past_decisions}}

# Available target documentation

{{documentation}}

Inspect this inventory and read documents relevant to this task, including applicable component-local guidance and relevant document references. Distinguish governing requirements, descriptive documentation and historical records; apply local instructions only within their intended scope.
