Run `git diff master...sf/20261011-202303` and update the documentation to match. Update existing docs; create new docs only under docs/. Do not touch code or the standards files (AGENTS.md, docs/VALIDATION.md, docs/coding-standards.md, docs/architecture.md).

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