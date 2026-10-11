Apply this approved improvement.

# Require deterministic end-state assertions (reduced-motion emulation) when verifying CSS transitions/animations headlessly

In the target's docs/VALIDATION.md, 'End-to-End Validation' section (browser-based applications list), add a bullet: 'When asserting the end state of a CSS transition or animation in a headless browser, emulate prefers-reduced-motion (or otherwise disable animations) and assert the computed end state, rather than sampling after a wall-clock delay; headless browsers may throttle frame updates while other animations run, making timing-based checks flaky. Verify animated states separately via the reduced-motion code path or an explicit animation-disable override, and record that as the method used.'

Rationale: The implementer spent three probe cycles (~2 minutes and several tool calls) chasing a flaky focus-ring assertion because headless Chromium throttles transition ticks while infinite background animations run; computed styles were stuck mid-interpolation. It only got stable results after emulating reducedMotion, and had to report the animated focus screenshot as 'Not checked'. A one-line technique note in VALIDATION.md eliminates this entire debugging detour for any visually animated target.

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