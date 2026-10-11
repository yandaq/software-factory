Apply this approved improvement.

# Add a mandatory process-cleanup command preflight

Strengthen agents/implementer.md with a mandatory check before any cleanup command: only use a PID captured when starting the process or `fuser -k <known-port>/tcp`; reject commands containing pkill, killall, or process-pattern pipelines before execution. If ownership cannot be established, leave the process running and report it rather than guessing.

Rationale: The existing prohibition was ignored, and a pattern-based kill terminated the agent itself. A concrete preflight checklist is warranted because this failure wasted an invocation and could endanger the factory process.

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