# AGENTS.md

Behavioural and engineering guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Trade-off:** These guidelines favour caution, clarity and verifiable progress over speed. Use judgement for trivial tasks.

> `AGENTS.md` defines **how agents engineer changes**.  
> `VALIDATION.md` defines **how agents prove those changes are correct**.

## 1. Think Before Coding

**Do not assume. Do not hide confusion. Surface trade-offs.**

Before implementing:
- Inspect the relevant code, tests and repository conventions.
- Before installing any tool or dependency, check whether the required capability is already provided by the execution environment or the repository.
- State material assumptions explicitly.
- If multiple interpretations exist, identify them rather than choosing silently.
- Prefer the simplest interpretation consistent with the request.
- Record minor assumptions in `PLAN.md` when working autonomously.
- If a requirement is materially unclear or contradictory, stop and record the exact blocker rather than guessing.
- If a simpler approach exists, say so and push back when warranted.

## 2. Simplicity First

**Write the minimum code that solves the defined problem. Nothing speculative.**

- Do not add features beyond what was requested.
- Do not add speculative abstractions, extensibility, configuration or flexibility.
- Do not create interfaces, factories, wrappers or layers without a concrete need.
- Introduce an abstraction only when it removes meaningful duplication, isolates a volatile dependency, clarifies a responsibility or creates a necessary testing boundary.
- Do not add error handling for impossible scenarios.
- Prefer explicit control flow and simple data flow over hidden behaviour or excessive indirection.
- If a solution is substantially larger than necessary, simplify it.

Ask: "Would a senior engineer consider this overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what is required. Clean up only what your change makes obsolete.**

When editing existing code:
- Do not improve adjacent code, comments or formatting without need.
- Do not refactor unrelated code.
- Match the existing style and architecture unless the task explicitly changes them.
- Mention unrelated issues rather than fixing them.
- Keep architectural changes within the scope of the current plan step.

When your changes create orphans:
- Remove imports, variables, functions and files made unused by your change.
- Do not remove pre-existing dead code unless asked.

Every changed line should trace directly to the request or to work required to keep the change correct, coherent and maintainable.

## 4. Outcome-Driven Execution

**Understand the required outcome before deciding how to implement it.**

- Translate the request into a clear behavioural outcome.
- Identify constraints, invariants and affected public contracts.
- Distinguish the requested outcome from a suggested implementation approach.
- Prefer the smallest implementation that achieves the outcome.
- For multi-step work, create a brief `PLAN.md` and work one step at a time unless explicitly instructed otherwise.
- Keep each plan step independently understandable and small enough to reason about.
- Do not treat implementation activity as evidence of success; completion is governed by `VALIDATION.md`.

For multi-step work, a plan may use this shape:

```text
1. [Step] → outcome: [expected result]
2. [Step] → outcome: [expected result]
3. [Step] → outcome: [expected result]
```

## 5. Preserve Existing Behaviour

**Change the requested behaviour without accidentally changing unrelated behaviour.**

- Treat existing public behaviour as a constraint unless the task explicitly changes it.
- Preserve API contracts, data formats, configuration semantics and user-visible behaviour outside the requested scope.
- Avoid broad rewrites when a local change is sufficient.
- Consider downstream callers and consumers before changing shared code.
- Prefer backwards-compatible changes when compatibility is part of the existing contract.
- Do not silently change defaults, error semantics or side effects.
- If an intentional breaking change is required, make it explicit in the plan and implementation.

Ask: "What existing behaviour could this change accidentally disturb?"

## 6. Architecture and Design

**Prefer high cohesion, loose coupling and clear module boundaries.**

- Keep behaviour, data and tests that change for the same reason close together.
- Give each module a clear responsibility and a small, explicit public interface.
- Depend on contracts rather than implementation details where that improves replaceability or testability.
- Preserve encapsulation; internal changes should not force unrelated modules to change.
- Minimise knowledge between modules. A component should depend only on what it genuinely needs.
- Prefer composition over inheritance.
- Avoid circular dependencies.
- Avoid global mutable state and hidden dependencies.
- Use dependency injection or explicit parameters when they improve clarity and testability.
- Isolate external systems, network calls, databases, file systems, clocks and other non-deterministic dependencies behind clear boundaries.
- Keep business logic separate from frameworks, user interfaces, persistence and external services where practical.
- Extend an existing coherent abstraction rather than creating a parallel one.
- Remove duplication when it represents the same concept or rule; do not combine code that merely looks similar but changes for different reasons.
- Choose the simplest design that satisfies current requirements and remains straightforward to change.

Before implementing a change:
1. Identify the module that owns the behaviour.
2. Identify the public contract affected.
3. Identify external or non-deterministic dependencies that require isolation.
4. Identify where the change naturally belongs.
5. Prefer changing the owning module over spreading conditional logic across callers.

Document any necessary architectural compromise and its trade-off in `ARCHITECTURE.md`.

## 7. Context-Friendly Modularity

**Keep files focused. Split only at clear responsibility boundaries.**

- Keep each file focused on one clear purpose.
- Split large files when there is a natural responsibility boundary.
- Do not split code merely to reduce file length.
- Avoid tiny fragments that force readers or agents to chase context across many files.
- Prefer modules that are understandable in isolation.
- Keep related implementation, contracts and diagnostics easy to locate.
- Avoid structures that require loading large amounts of unrelated context to make a small change.

Ask: "Does this split reduce cognitive load, or merely move complexity around?" If it only moves complexity, keep it together.

## 8. Design for Observability

**Build systems so that their behaviour, failures and state can be inspected by both humans and coding agents.**

- Do not silently swallow errors or exceptions.
- Surface failures through an appropriate observable channel: process output, browser console, structured logging, files, API responses, test results or equivalent.
- Include enough diagnostic context to identify where and why a failure occurred.
- Prefer machine-readable diagnostic output where practical.
- Ensure important asynchronous, background and integration failures remain discoverable after they occur.
- Expose useful state or health information where behaviour cannot be inspected directly.
- Avoid requiring manual visual inspection as the only means of determining whether something worked.
- When adding a feature, consider how a coding agent will determine that it is working correctly and diagnose it when it is not.
- Make observable signals available to automated checks and CI/CD wherever practical.
- Do not add excessive logging or instrumentation without a concrete diagnostic or operational purpose.

Ask: "If this fails when no human is watching, can an agent determine what failed and why?"

## 9. Follow Existing Conventions

**Prefer the repository's established patterns over inventing new ones.**

- Reuse existing libraries, utilities, abstractions and project conventions where they are fit for purpose.
- Match existing naming, structure, formatting and error-handling patterns.
- Use the project's documented commands and workflows before introducing alternatives.
- Do not add a new dependency when the repository or execution environment already provides the capability.
- Do not introduce a second way of solving a problem unless there is a concrete reason.
- When conventions conflict with the requested change, make the conflict explicit rather than silently bypassing them.
- If a new pattern is genuinely required, keep it small, document why it exists and apply it consistently within the affected scope.

Ask: "Am I extending the system that is already here, or accidentally creating a parallel one?"

## 10. Validate Before Completion

**A change is not complete until the applicable validation defined in `VALIDATION.md` has passed.**

- Read and follow `VALIDATION.md` for the validation process, evidence requirements and feedback loop.
- Select validation checks according to the change, repository and risk; do not assume every check applies to every task.
- Do not declare success based on code inspection or plausibility alone.
- Do not weaken tests, suppress errors or bypass checks merely to make validation pass.
- When validation fails, use the failure evidence to diagnose and correct the implementation, then run the relevant checks again.
- If required validation cannot be run or cannot pass, record the precise blocker instead of claiming completion.

---

**These guidelines are working when:** diffs remain focused, designs stay simple, repository conventions are respected, systems are observable, architectural boundaries remain clear, unrelated behaviour is preserved, and completion claims are backed by the evidence required in `VALIDATION.md`.
