Apply this approved improvement.

# Make plans concise, internally consistent, and validation-proportional

Update agents/planner.md to require a final consistency audit across fixed decisions, file formats, contracts, tests, and acceptance criteria. Instruct it to distinguish normative requirements from illustrative implementation suggestions, avoid hard-coding incidental environment versions or excessive test mechanics, and choose the smallest proportional validation set that proves the user-visible outcome.

Rationale: The plan over-specified incidental implementation details and exhaustive visual instrumentation while containing a contradictory persistence contract. This increased implementation cost and made a later reviewer suggestion easy to misapply.

You may change ONLY: /home/ian/sf/agents/*.md and /home/ian/hello-world/AGENTS.md, /home/ian/hello-world/docs/VALIDATION.md, /home/ian/hello-world/docs/coding-standards.md, /home/ian/hello-world/docs/architecture.md. Do not change orchestrator code or anything else.

When finished, end your final message with a single JSON object that validates against this JSON Schema:
```json
{
  "type": "object",
  "required": [
    "summary"
  ],
  "properties": {
    "summary": {
      "type": "string"
    }
  }
}
```