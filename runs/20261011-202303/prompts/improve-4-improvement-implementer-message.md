Apply this approved improvement.

# Require reviewer remediation advice to preserve higher-priority contracts

Update agents/reviewer.md to rank the request and explicit acceptance criteria above incidental naming concerns, and to cross-check every proposed remediation against the full plan before suggesting it. Require changes_requested for any actionable contract or protected-document inaccuracy; reserve non-blocking minor findings for genuinely advisory issues that need no code or documentation change.

Rationale: A reviewer finding should not offer a fix that conflicts with an explicit acceptance criterion. Reviews should also not approve while leaving a concrete, readily fixable inconsistency in a protected architecture document.

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