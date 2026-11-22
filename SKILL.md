---
name: agent-legible-codebase
description: Audit source architecture for unclear ownership, expensive navigation, scattered changes, and leaky interfaces. Prioritize deep-module improvements, then implement and verify the changes the user selects. Analyze traces or benchmark only on explicit request.
---

# Agent-Legible Codebase

Help an unfamiliar coding agent find the owner of a behavior, understand its public contract, make a local change, and find its tests. Improve the source architecture so the same work is easier for human maintainers.

## Audit by default

“Improve my codebase” starts a read-only architecture audit in the current Codex session and working tree. Use the tools already available in the session. A request for design can go deeper on a proposed interface or seam while keeping the audit read-only. A request to implement a specific source change can proceed within that scope.

1. Start with the user's area or a representative change in an important or frequently changed workflow. Use repository guidance, a shallow path listing, targeted searches, and relevant history as needed to choose where to look.
2. Trace the path a new maintainer would take. Find the runtime entry point, read the caller-facing interface and nearby tests, then follow only the implementation and dependencies needed to explain the behavior. Record concrete friction such as competing owners, false leads, rules split across callers, hidden wiring, or tests that are hard to locate. State the scope and any gaps in the evidence.
3. Look for a cohesive owner with a small interface that hides meaningful decisions and invariants. An interface includes the rules callers must know about errors, ordering, state, and side effects. Use domain terms consistently in paths, symbols, and tests so the owner is discoverable. Keep dependency direction and side-effect wiring explicit. Use the deletion test on thin wrappers and add seams where real variation or testing warrants them. Keep independently changing responsibilities separate. Favor local changes and lower coupling over cosmetic moves or file-size targets.
4. Return a short, numbered list ordered by expected impact, effort, and risk. For each item, cite the actual paths and symbols or lines, show the concrete evidence, propose the source change, and explain the expected benefit, effort, and risks. Note dependencies between items where they affect selection. Treat benefits as estimates unless measured. If the evidence supports leaving a slice alone, say so.
5. Stop after the recommendations. The user chooses which items to implement.

Subagents are optional when available and permitted. Use them only if independent investigations would materially help. A single session is sufficient for the full workflow.

## Implement selected improvements

“Fix item 1,” “Fix the first three,” and “Apply all” authorize the corresponding items from the latest recommendations. Work in the current repository, preserving existing user edits. Group selected items into manageable batches and keep related changes together. Verify focused behavior after each batch and run relevant regression checks. Preserve caller contracts, failure behavior, and other observable behavior unless the user requests a change. Report what shipped, the checks and results, and any gaps or blockers.

## Read references as needed

- For a consequential ownership or interface choice, read [Design principles](references/design-principles.md) for the deep-module, seam, and deletion-test guidance drawn from Matt Pocock’s architectural work.
- For navigation and change-locality patterns, read [Context locality](references/context-locality.md). For a recurring architectural constraint worth enforcing, read [Architecture guardrails](references/architecture-guardrails.md).
- Only for an explicit request to analyze saved traces, see [Trace analysis and replay](README.md#migrate-v3-traces-and-results). For requested formal measurement, see [Paired benchmark protocol](references/codex-benchmark.md) and [benchmark commands](README.md#run-a-benchmark) for edit, discovery, and isolated verification. Ordinary audits and selected improvements run with the current session and checkout, without benchmark setup.
