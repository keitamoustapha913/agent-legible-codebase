# Keeping an agent-legible codebase maintainable

Use after a concrete architecture problem has been demonstrated. A guardrail is valuable only if it preserves an actual design invariant and can be maintained by the team's existing tooling.

## Guardrail targets

1. **Dependency direction.** Feature/domain code should not import a concrete app composition root or an unrelated feature's private implementation. Enforce package public surfaces only where doing so matches actual ownership.
2. **No accidental cycles.** Detect import cycles introduced by the refactor; distinguish harmless type-only cycles where the language makes that meaningful from runtime cycles.
3. **Stable behavior ownership.** If one business invariant must remain centralized, ensure boundary tests exercise it from multiple consumers. Avoid a new unmaintained duplicate implementation.
4. **Visible composition.** Confirm the application actually selects the intended route/handler/adapter via integration tests. Protect wiring decisions that would be invisible to search otherwise.
5. **Boundary contracts.** Test representative success, failure and edge cases, including data/serialization, authorization, persistence, ordering and concurrency when relevant. Tests should survive private function renaming.
6. **No unnecessary public surface.** Prefer explicit exports where practical, without making them a blanket rule. Avoid accidental exports of internal helpers as stable contracts.
7. **Build, type and runtime preservation.** Run the existing linter, tests, type checker, build and other project-specific checks. Treat untested performance or security drift as risk, not a token optimization.

## How to apply

- First see which rules already exist in tooling. Reuse the repository's dependency or lint configuration, package visibility conventions, language module system and CI checks.
- Make a narrow rule that captures the observed regression: e.g., “payment domain code cannot import checkout UI internals” rather than “all modules must follow a new five-layer architecture.”
- Use the simplest ecosystem-appropriate mechanism; no generic regex-based import checker for languages where it will produce false positives.
- Add a focused test or lint check for a real previous violation, prove it fails when the rule is broken, then restore compliance.
- Exceptions must be intentional, traceable, and constrained. Do not weaken lint or broaden allowlists merely to make the new code pass.
- Where the project cannot reasonably enforce a rule automatically, report the invariant and the manual check instead of pretending enforcement exists.

## Change safety matrix

| What is touched | What must be checked |
| --- | --- |
| File paths, exports, modules | Internal imports, public import consumers, tooling/config paths, package interfaces, dynamic load points |
| Orchestration and dependency injection | Execution order, lifecycle, cancellation, retry behavior, transactional boundaries, runtime integration tests |
| Data and public types | Serialization, schema compatibility, migrations, downstream consumers, type-level and runtime tests |
| Error and exception handling | Error identity, propagation, logging, monitoring, resilience, security responses |
| I/O or caching | Concurrency, idempotency, performance, resource lifecycle, timeout handling |
| Generated sources | Edit generator/config or source of truth, then regenerate; don't hand-edit generated artifacts |

## Maintenance stress test

Before declaring success, propose three likely follow-up edits to the refactored module: a new variation, a new failure condition, and an integration change. For each, predict where the agent will go and what it needs to read and modify. If the new structure makes common future changes broadly scattered, reconsider the design.

**Definition of done:** source ownership is apparent, public behavior is stable, tests prove the right outcomes, dependency rules prevent recurrence where feasible, and maintenance cost did not rise to chase a lower token count.
