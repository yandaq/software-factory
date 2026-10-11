Apply this approved improvement.

# Document the real browser-automation fallback and server-readiness polling in VALIDATION.md's E2E section

In the target's docs/VALIDATION.md, 'End-to-End Validation' section, after the existing 'Use Pi's built-in…' bullet, add: (a) 'If no built-in browser automation tool is in your current toolset, script the environment's installed Playwright library. Before launching, check which browser builds exist (ls ~/.cache/ms-playwright) and prefer launching with an installed build or the system browser executable (e.g. /usr/bin/chromium via executablePath); do not download browsers.' (b) 'When validating against a locally started server, poll for readiness (e.g. curl --silent --retry with a short interval, or a loop until HTTP 200) before navigating; do not rely on a fixed sleep.'

Rationale: VALIDATION.md tells agents to use Pi's built-in browser automation 'when available', but the implementer is invoked without any browser tool, so it falls back to ad-hoc Playwright scripts and must rediscover the working configuration every run. This run wasted one invocation on a missing default browser binary and two more on connection-refused races against a just-started static server. Concrete environment guidance turns three failed attempts into zero.

You may change ONLY: /home/ian/sf/agents/*.md and /home/ian/hellow-world2/AGENTS.md, /home/ian/hellow-world2/docs/VALIDATION.md, /home/ian/hellow-world2/docs/coding-standards.md, /home/ian/hellow-world2/docs/architecture.md. Do not change orchestrator code or anything else.

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