Apply this approved improvement.

# Require the implementer to self-review against all repository contracts

Update agents/implementer.md to remove the instruction to ignore coding standards. Require a pre-handoff self-review against the plan, docs/architecture.md, docs/coding-standards.md, and docs/VALIDATION.md, including explicit checks of failure boundaries, documented contracts, and overlapping asynchronous events. Require every review fix to preserve all existing acceptance criteria and to include a focused regression test or deterministic reproduction.

Rationale: The instruction to ignore coding standards delegates basic correctness entirely to later review, contributing to three corrective loops and substantial token use. The implementer should check the plan, architecture, standards, and validation requirements before declaring completion, especially error paths and asynchronous state transitions.

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