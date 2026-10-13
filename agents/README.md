# Agent prompts

Each `<name>.md` contains YAML configuration and persistent role instructions. Each
`<name>.user.md` contains its editable task prompt. Both prompt layers can be tuned
by the improvement agents, subject to evidence, structural validation and portability reviewer approval.

Use exact `{{identifier}}` placeholders. The `documentation` placeholder is required for every agent, in addition to the agent-specific
placeholders below. All placeholders are required;
they can be reordered or repeated. Unknown or malformed placeholders are rejected.
Single braces are literal. Inserted values are never interpreted as templates.

| Task template | Required placeholders |
|---|---|
| planner | request |
| implementer | plan, feedback |
| reviewer | plan, trunk |
| documenter | trunk, branch, standards_files |
| improver | run_id, target, trunk, branch, run_dir, agents_dir, cap_reached, past_decisions |
| improvement-implementer | title, proposed_change, rationale, allowed_files |
| improvement-reviewer | idea, changes |

`feedback` includes the latest failed validation output, otherwise the latest review
findings, otherwise an empty string. Other values are provided by the orchestrator for the current task.
Missing or empty task templates fail visibly; there is no embedded fallback.

The dashboard exposes role and task files separately and lists required placeholders.
Changes apply to subsequent invocations. Workflow control, permissions, file scope,
JSON contracts and correction retries remain enforced by Python.

`documentation` contains the target repository path and a JSON list of repository-relative
Markdown paths. The inventory is refreshed for each new invocation, using tracked and
non-ignored untracked files throughout the target. Deleted files and symlinks are excluded;
extensions are case-insensitive. Read applicable docs and follow relevant references rather
than treating every historical document as a governing requirement.

Automatically selected improvements may update existing discovered target docs. Their path allowlist is
frozen before invocation; new target docs remain the documenter's responsibility under docs/.
Starter guidance files and the source of validation commands remain unchanged.

Shared instructions must remain portable across unrelated target repositories. Target-specific
rules belong in existing target docs; evidence and illustrative examples may mention target code.
After structural validation, shared edits require approval from the read-only improvement-reviewer
(default: Codex gpt-6.1-sol, medium). Its definition and template are pinned before implementation,
including when it reviews edits to itself. Its tools are restricted to read-only tools by the gate.
Target-only improvements skip this review. Rejection or review failure restores the pre-improvement
prompt snapshot and fails the stage; target documentation stays uncommitted for inspection.

The improve stage applies every suggested improvement automatically except previously rejected
titles. New selections are recorded as `decision: automatic` in improvements.jsonl before application;
that records selection, not successful completion. Historical human decisions remain readable.
The improvement-implementer no longer receives a human_note placeholder. Plan approval and loop-cap
prompts are independent of improvement selection.
