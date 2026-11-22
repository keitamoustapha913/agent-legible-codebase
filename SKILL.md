---
name: agent-legible-codebase
description: Improve source-level codebase discoverability, change locality, deep-module interfaces, and AI coding-agent context efficiency. Audit, redesign, safely refactor, or empirically benchmark real code changes. Includes Codex CLI JSONL trace collection, independent Git-worktree task trials, correctness checks, and evidence-backed navigation failure analysis. Use when agents search too much, must read unrelated code, repeatedly miss feature owners, or need large context for routine changes.
---

# Agent-Legible Codebase — v4 (Codex-ready)

**Goal:** Maximize **correct, maintainable, verified code changes per unit of agent context**. Organize the **source code itself** so an unfamiliar Codex agent can discover the owner, inspect a small interface, trace behavior, make a local change, and find tests without reading unrelated parts of the repository. Do **not** optimize for shortest files, fewer lines, or a bigger `AGENTS.md`.

## Pick the workflow

| User intent | Mode | What to deliver |
|---|---|---|
| Find what's making the repo hard to navigate | **audit** | Real path-specific friction and ranked refactor candidates; no source edits |
| Plan a clearer module design | **design** | Explicit ownership, interface, wiring, test seam, risks; no edits unless requested |
| Improve actual source architecture | **refactor** | Small reversible source changes, verified behavior, new guardrails where useful |
| Gather concrete Codex navigation failures | **trace** | Parsed JSONL with attributable source bytes, independent provider usage and event evidence |
| Compare repository design on real edits | **benchmark** | Counterbalanced paired edit trials with independent checks, usage and evidence |
| Compare edits when tests need a separate runtime | **benchmark (isolated verifier)** | Restricted sandboxed editing and independent checks in offline disposable VMs |
| Compare how quickly agents locate and explain code | **benchmark (discovery)** | Read-only paired trials with offline owner, symbol and dependency scoring |

Use **audit** if requested to review; **refactor** if the user explicitly requests source changes. **Trace/benchmark** do not automatically mean refactor. Running large or costly autonomous benchmarks requires explicit user authorization. Do not claim measured savings without real paired runs.

## Core design vocabulary and rules

- **Module:** a cohesive owner of one behavior and its invariants, whether function, package, class, or feature slice.
- **Interface:** everything a caller must know (inputs, results, errors, side effects, ordering, performance), not merely exported signatures.
- **Depth:** meaningful behavior and decisions behind a small, stable interface, with unnecessary detail hidden.
- **Seam:** a useful point of variation, justified by real alternatives or testing requirements.
- **Locality:** how few unrelated concepts/files a change needs to touch or understand.
- **Information scent:** whether source names and paths point an unfamiliar agent to the correct owner.
- **Context footprint:** source and tool content a specific task actually exposes, not repository size or source file count.

Borrow the deep-module/seam principles from Matt Pocock's public *codebase-design* and *improve-codebase-architecture* work, but combine them with searchability, source-first navigation, and empirical Codex evaluation. This skill is an independent workflow, not an official copy. For trade-offs, open `references/design-principles.md` only when designing a module.

## Workflow A — audit / design / refactor

1. **Select a narrow, real slice.** Respect the user's target area; otherwise inspect recent commits, tests, actual reported friction and high-churn modules. Preserve user modifications. Note baseline errors, runtime behavior, and public contracts.
2. **Simulate cold-start discovery.** Begin with targeted path/symbol searches. Read interface and local tests first; read narrow implementation ranges next; follow dependencies only as required. Record actual searches, reads, false leads, and unknowns. Do not eagerly dump the entire repo.
3. **Identify source-level friction:** unclear rule owner; duplicated domain concepts; generic `utils`/`helpers` bins; shallow forwarding layers; hidden runtime registration; cross-feature import cycles; leaked internal workflow; scattered tests; implicit global state; changes spanning unrelated modules.
4. **Propose a deep, discoverable owner.** Match feature vocabulary between directory, exported symbol, runtime entry point, error behavior, and tests. Keep a small public surface and explicit composition root. Put coupled decisions/invariants together; do not create speculative adapters or microfiles merely to hide lines.
5. **Prefer the smallest safe structural intervention.** Use the deletion test on wrappers. Choose a seam that removes complexity from callers. Preserve observable behavior, external contracts, failure modes, data migrations, startup order, concurrency, and security controls.
6. **Enforce the best constraints.** Add only low-maintenance checks that catch real regressions: import direction, dependency cycles, single rule ownership, public API compatibility, relevant test coverage. Do not force a specific folder taxonomy.
7. **Verify and report.** Run focused and regression checks. Show paths changed and why a future agent would need less irrelevant context. Separate hypothetical gains from measured changes. If meaningful improvements are absent, stop rather than creating cosmetic churn.

For practical patterns and anti-patterns open `references/context-locality.md`; for enforceable checks open `references/architecture-guardrails.md`.

In **audit**, stop after 2–5 candidate opportunities with concrete affected paths and a prioritized recommendation. In **design**, present alternatives for irreversible interfaces. In **refactor**, make actual scoped source edits and tests; never silently turn an audit into an edit.

## Workflow B — empirical Codex measurement

**Benchmark only when users approve model usage.** Start with the [simple benchmark command](README.md#run-a-benchmark).

1. Choose equivalent tasks and pin the original and refactored Git commits. An edit task needs an ID and a prompt. Discovery tasks use `--mode discovery` with a question and offline expected answers.
2. Dry-run the plan outside the target repository. With approval, use `--approve-manifest`. The runner uses fresh `codex exec --json` sessions and disposable worktrees for both revisions. Edit sessions stay restricted and the prompt tells Codex to skip pytest. Discovery sessions have read-only navigation tools.
3. Use `--allow-host-tests` only after the user approves the reviewed commands and trusts the candidate code they can execute on the host. The default skips host commands. [Isolated verification](references/isolated-verifier.md) is available when a separate offline VM is needed.
4. Read `report.md` for independent correctness, provider input, navigation and change scope. Use one pair to check instrumentation and at least three verified pairs per task for a preliminary trend. Keep inference separate from observation when checks or telemetry are missing.

To analyze one saved trace offline:

```sh
python3 scripts/codex_trace.py /path/to/codex-exec.jsonl \
  --repo-root /original/worktree --owner src/checkout/discounts.ts \
  --output /tmp/discount-trace.json
```

`references/measurement-model.md` defines source bytes and regex units, provider token usage, first owner mention and their coverage limits. A reading with no visible output or unclear source attribution is unknown. A path mention is only an observable reference. Model input covers all provider context. `references/failure-taxonomy.md` maps observed navigation patterns to hypotheses after environmental and instrumentation problems are accounted for.

## Source-first acceptance check

A cold-start agent should be able to find an authoritative owner via domain vocabulary; understand the interface without reading all internals; trace explicit dependency/side-effect wiring; change one cohesive behavior; identify its focused tests; and pass checks without learning the whole system. A human maintainer should find the resulting interface simpler, not just differently organized.

**Deliver:** changed source or ranked candidates; test and compatibility status; recorded navigation failures when available; paired correctness counts; measured costs with uncertainty and instrumentation caveats; any remaining friction.

**Stop rule:** if the source already has clear ownership and localized behavior, and additional restructuring would only make a benchmark look smaller, leave it alone.
