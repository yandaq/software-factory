# VALIDATION.md

Validation rules for coding agents. This file defines how an implemented change is proved correct and how validation failures are fed back into the coding loop.

> `AGENTS.md` defines **how agents engineer changes**.  
> `VALIDATION.md` defines **how agents prove those changes are correct**.

## Core Rule

**Implementation is not evidence. A change is complete only when the applicable validation demonstrates that the requested outcome was achieved without unacceptable regressions.**

Validation should be proportional to the change:
- Use the smallest useful checks while iterating.
- Expand validation as confidence grows.
- Run all applicable completion checks before declaring success.
- Not every validation category applies to every change.
- Higher-risk, broader or more user-visible changes require stronger evidence.

## Validation Feedback Loop

Use this loop for every non-trivial change:

```text
Define outcome
    ↓
Implement smallest change
    ↓
Run relevant validation
    ↓
Capture evidence
    ↓
Pass? ── yes ──→ Expand / final validation ──→ Complete
  │
  no
  ↓
Diagnose from failure evidence
    ↓
Feed precise failure back into coding
    ↓
Fix the cause
    ↓
Re-run the failed check
    └───────────────────────────────────────────↺
```

### 1. Define the expected outcome

Before validating, identify:
- The requested user-visible or system-visible outcome.
- The behaviour that must remain unchanged.
- Any explicit acceptance criteria.
- Relevant boundaries, failure cases and invariants.
- Which validation categories below are applicable.

Do not validate merely that code exists. Validate the behaviour the change was intended to produce.

### 2. Start with the narrowest useful check

During implementation:
- Run the smallest relevant test, command or observable check first.
- Prefer fast feedback over repeatedly running the entire suite.
- Where practical, create or update a test that expresses the changed behaviour.
- For bug fixes, reproduce the bug with a failing test or equivalent deterministic check before fixing it.
- Confirm new tests fail for the expected reason before relying on them as evidence.

### 3. Capture failure evidence

When validation fails, preserve the useful diagnostic information:
- Check or command that failed.
- Exit code or failure status.
- Relevant assertion, exception or compiler output.
- Browser console or page errors where applicable.
- Relevant logs or structured diagnostics.
- Expected behaviour versus observed behaviour.

Do not reduce a failure to "it didn't work" when more precise evidence is available.

### 4. Feed the failure back into the coding loop

Use validation output as the next coding input.

A useful failure report contains:

```text
Check:      [what was run]
Expected:   [what should have happened]
Observed:   [what actually happened]
Evidence:   [error/assertion/log/console output]
Cause:      [current diagnosis, if known]
Next step:  [smallest change likely to address the cause]
```

- Diagnose before editing.
- Fix the cause, not merely the symptom presented by the check.
- Keep the corrective change as small as possible.
- Do not introduce unrelated refactoring while fixing validation failures.
- If the failure exposes a wrong assumption, update the plan or acceptance criteria before continuing.

### 5. Re-run after every fix

After correcting a failure:
1. Re-run the failed check.
2. Run closely related checks that could have been affected.
3. Once local validation passes, expand to the broader applicable validation set.
4. Continue until all required checks pass or a genuine blocker is identified.

Never assume a fix worked because the code now looks plausible.

## Validation Categories

These categories form the validation toolbox. Select the ones applicable to the change and repository.

### 1. Build / Runtime

**Does the software actually compile, package, start and run?**

Validate as applicable:
- Compilation or build succeeds.
- Packaging succeeds.
- Application or service starts successfully.
- Changed commands or entry points execute.
- Migrations, generated artefacts or startup steps complete when relevant.
- Runtime does not immediately fail on the changed path.

Use the repository's existing build and run commands.

### 2. Observability

**Do runtime signals show the system behaving correctly and expose failures clearly?**

Inspect as applicable:
- Process output.
- Exceptions and stack traces.
- Structured logs.
- Browser console messages.
- API responses.
- Health or status endpoints.
- Background-task or integration diagnostics.

Treat unexpected errors as failures unless there is a specific, documented reason they are expected.

Observability is both a validation input and a design requirement: `AGENTS.md` requires code to expose enough useful state for validation to be possible.

### 3. Unit Tests

**Do individual functions, classes or modules exhibit the required behaviour in isolation?**

- Add or update meaningful coverage for changed behaviour.
- Test observable behaviour rather than implementation details.
- Include relevant happy paths, boundaries and failure paths.
- Keep tests deterministic where practical.
- Do not weaken, delete, skip or rewrite legitimate tests merely to make the implementation pass.
- During development, run the smallest relevant test set first.

### 4. Integration Tests

**Do connected components work correctly together?**

Validate interactions such as:
- Module-to-module contracts.
- Database access.
- File-system behaviour.
- Network or API boundaries.
- Queues, events or background workers.
- Framework integration.
- External-service adapters.

Prefer existing test fixtures, fakes, containers and repository conventions over inventing parallel infrastructure.

### 5. End-to-End Validation

**Does the complete user or system journey work through the real interface?**

For browser-based applications:
- Use Pi's built-in Playwright-compatible browser automation tools when available.
- Do not install Playwright, Chromium or other browser-testing packages solely for validation when the execution environment already provides browser automation.
- Exercise the changed behaviour through the user-facing interface.
- Capture browser console messages during validation.
- Treat unexpected browser console errors and unhandled page errors as failures.
- Do not ignore console errors without documenting a specific, justified exception.
- Do not add Playwright configuration, dependencies or committed test files unless the task explicitly requires them.
- If the repository already has a browser test suite, use its documented commands without changing dependencies unless a missing dependency is confirmed.

For non-browser systems, use the closest equivalent full journey through the public interface.

### 6. Linting

**Does the changed code conform to the project's static quality and style rules?**

- Run the repository's configured linter on the changed scope during iteration.
- Run the required project-level lint command before completion where applicable.
- Treat newly introduced lint failures as defects.
- Do not disable or suppress rules merely to make a check pass without justification.

Examples may include `ruff`, `eslint` or repository-specific equivalents.

### 7. Type Checking

**Are type contracts internally consistent before runtime?**

Run the repository's configured type checker where applicable.

Examples may include:
- `mypy`
- `pyright`
- `tsc`
- language-native compiler checks

Do not introduce broad casts, `Any`, suppression comments or equivalent escape hatches solely to silence a legitimate type error.

### 8. Complexity

**Has the change introduced code that is unnecessarily difficult to reason about or maintain?**

Use the repository's configured complexity tooling when present.

Where complexity checking is part of the validation harness:
- Flag functions or methods that exceed the agreed threshold.
- Refactor the local source of complexity rather than suppressing the warning.
- Prefer simpler control flow and smaller coherent responsibilities.
- Do not split code mechanically if doing so merely moves complexity elsewhere.

`lizard` is one possible tool where the repository has chosen it.

### 9. Duplication

**Has the change introduced meaningful copy-and-paste duplication?**

Use configured duplication checks when present.

- Distinguish duplicated syntax from duplicated knowledge or business rules.
- Remove duplication when multiple copies represent the same concept and should change together.
- Do not force unrelated code into a shared abstraction merely because it looks similar.

`jscpd` is one possible cross-language tool where the repository has chosen it.

### 10. Security

**Has the change introduced a vulnerability, unsafe dependency, exposed secret or dangerous execution path?**

Validate according to the affected surface:
- Dependency vulnerability checks.
- Secret scanning.
- Static security analysis.
- Authentication and authorisation behaviour.
- Input handling and injection risks.
- Unsafe file, command or network operations.
- Sensitive data exposure.
- Repository-specific security gates.

Treat security validation proportionally to risk. Do not claim a security property that has not actually been checked.

### 11. Architectural Conformance

**Does the change preserve the intended module boundaries, dependency rules and design constraints?**

Check as applicable:
- No forbidden dependency directions.
- No new circular dependencies.
- Public contracts remain coherent.
- Business logic remains in the intended layer.
- External systems remain behind defined boundaries.
- New code extends existing abstractions rather than creating accidental parallel architectures.
- Repository-specific architectural fitness rules still pass.

Automate these checks where the repository provides tooling; otherwise inspect the affected dependency and module boundaries explicitly.

### 12. Acceptance Criteria

**Does the final behaviour explicitly satisfy what was requested?**

This is the final outcome check.

For each acceptance criterion:
- State the criterion.
- Identify the evidence that demonstrates it.
- Confirm whether it passed.
- Do not substitute passing technical checks for the requested user outcome.

A build can pass while the feature is still wrong. Acceptance validation closes that gap.

## Validation Order

Use a fast-to-broad sequence where practical:

```text
Focused check
→ Unit
→ Static checks
→ Integration
→ Build / runtime
→ E2E
→ Architecture / security / quality gates
→ Acceptance criteria
```

The exact order depends on the repository and change. Fail fast with cheap checks before running expensive ones.

## Do Not Game Validation

Never:
- Delete or weaken a legitimate test because the implementation fails it.
- Mark a test skipped solely to obtain a green result.
- Suppress compiler, type, lint, security or runtime errors without a justified reason.
- Change thresholds merely because the new code exceeds them.
- Ignore browser console or unhandled runtime errors without documenting why they are acceptable.
- Replace a meaningful assertion with a weaker one that no longer proves the behaviour.
- Mock away the behaviour that the validation is supposed to prove.
- Claim completion when required validation was not run.

If a validation rule itself is wrong, treat changing that rule as a separate, explicit engineering decision.

## Existing Failures

If validation reveals a failure that predates the current change:
- Confirm that it is genuinely pre-existing where possible.
- Do not silently fix unrelated failures.
- Record the failure and the evidence that it is unrelated.
- Ensure the current change has not made it worse.
- Continue only if the unrelated failure does not prevent meaningful validation of the requested change.

## Blocked Validation

If a required check cannot be run:
- State exactly which check is blocked.
- State why it cannot run.
- Record any error or missing dependency.
- Do not install tools or change the environment until confirming the capability is not already available.
- Run the strongest remaining validation that is possible.
- Do not describe the task as fully validated.

Use a precise blocker rather than a vague caveat.

## Completion Evidence

Before declaring work complete, summarise the evidence concisely:

```text
Validation
- [check]: PASS
- [check]: PASS
- [check]: PASS

Acceptance criteria
- [criterion]: PASS — [evidence]

Not run
- [check]: [reason, if applicable]

Blockers
- None
```

Only include checks that were actually run or explicitly assessed.

## Definition of Done

A coding change is complete when:
- The requested outcome is implemented.
- Applicable validation checks pass.
- Relevant regressions have been checked.
- Acceptance criteria are explicitly demonstrated.
- Unexpected runtime, browser or diagnostic errors have been resolved or justified.
- No validation mechanism was weakened merely to obtain a pass.
- Any validation that could not be performed is clearly disclosed.
- There is no unresolved blocker being hidden behind a completion claim.

---

**This validation process is working when:** failures generate actionable evidence, that evidence drives the next coding step, fixes are re-tested rather than assumed, validation expands with confidence, and agents only declare success when the requested outcome is supported by passing evidence.
