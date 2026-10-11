Produce an implementation plan for the request below. Read AGENTS.md and docs/ (including docs/architecture.md) in the repository first.

# Request

build a hello world web app. Make the text 'hello world' editable. Text edits should persist across sessions. Make it visually stunning.


When finished, end your final message with a single JSON object that validates against this JSON Schema:
```json
{
  "type": "object",
  "required": [
    "plan"
  ],
  "properties": {
    "plan": {
      "type": "string",
      "description": "The full plan as markdown"
    }
  }
}
```