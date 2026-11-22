---
name: agent-legible-codebase
description: Improve source-level codebase discoverability, change locality, deep-module interfaces, and AI coding-agent context efficiency. Audit, redesign, safely refactor, or empirically benchmark real code changes. Includes Codex CLI JSONL trace collection, independent Git-worktree task trials, correctness checks, and evidence-backed navigation failure analysis. Use when agents search too much, must read unrelated code, repeatedly miss feature owners, or need large context for routine changes.
---

# Agent-Legible Codebase — v3 (Codex-ready)

**Goal:** Maximize **correct, maintainable, verified code changes per unit of agent context**. Organize the **source code itself** so an unfamiliar Codex agent can discover the owner, inspect a small interface, trace behavior, make a local change, and find tests without reading unrelated parts of the repository. Do **not** optimize for shortest files, fewer lines, or a bigger `AGENTS.md`.

## Pick the workflow

| User intent | Mode | What to deliver |
|---|---|---|
| Find what's making the repo hard to navigate | **audit** | Real path-specific friction and ranked refactor candidates; no source edits |
| Plan a clearer module design | **design** | Explicit ownership, interface, wiring, test seam, risks; no edits unless requested |
| Improve actual source architecture | **refactor** | Small reversible source changes, verified behavior, new guardrails where useful |
| Gather concrete Codex navigation failures | **trace** | Parsed JSONL with source-output proxy, actual Codex model usage when logged, evidence |
| Test before vs after repository design | **benchmark** | Independent paired task runs, verified outcomes, cost medians, architectural hypotheses |

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

**Benchmark only when users approve the cost.** The harness can run many real Codex sessions, each consuming model usage. Do not launch it just to demonstrate this skill. Before a before/after experiment, read `references/codex-benchmark.md` and `references/task-design.md`.

1. **Freeze 2–10 authentic task prompts before refactoring**, phrased like user requests without file hints. Supply independent behavior oracles that are *red* on both untouched snapshots and regression checks that are *green* on both. Use comparable dependency installations and equivalent observable behavior.
2. **Pin two immutable Git revisions** (`--before` and `--after`), the same Codex CLI/model/config, tools, timeouts, and evaluation commands. Use at least three independent task trials per version where feasible; keep the same trial IDs across versions.
3. **Dry-run the manifest** using `scripts/benchmark_codex.py --dry-run`. Keep the output directory outside the repo. Review every manifest command: benchmark verifiers run on the host, and Codex operates on disposable Git worktrees.
4. **Run paired Codex tasks** with `codex exec --json --ephemeral --sandbox workspace-write`, one fresh thread per run. Capture completed shell commands, searches, visible file paths, edits, and `turn.completed.usage` where available. Never reuse a successful agent's learned context.
5. **Generate the report** (the runner creates `report.md` automatically; rerun `scripts/report_codex.py` when needed); optionally run the v2-compatible `scripts/compare_traces.py` on `before.json` and `after.json`. Inspect evidence for false leads, name collisions, repeated searches, dependency chasing, excessive edits and tool telemetry gaps.
6. **Accept a refactor only if correctness holds.** Compare all pass/fail trials first; compare token costs only on matched both-pass trials, without hiding failures. Reduce architectural complexity only when the evidence and maintainability review support it. Test any follow-up changes on untouched holdout tasks.

Typical commands (from the installed skill folder):

```sh
python scripts/benchmark_codex.py --repo /path/to/project --manifest /path/to/tasks.json \
  --before baseline-revision --after refactored-revision \
  --out /tmp/codex-codebase-benchmark --trials 3 --model YOUR_SUPPORTED_CODEX_MODEL --dry-run

# Remove --dry-run only after reviewing tasks, trusted local verifier commands,
# test preflight, model costs, and disposable worktree behavior.
python scripts/report_codex.py /tmp/codex-codebase-benchmark/results.json \
  --out /tmp/codex-codebase-benchmark/report.md
```

For a **single existing Codex JSONL trace** (no new model run), use:

```sh
python scripts/codex_trace.py /path/to/codex-exec.jsonl \
  --owner src/checkout/discounts.ts --output /tmp/discount-trace.json
```

### What counts as a real measurement?

- **Codex `turn.completed.usage.input_tokens`:** provider-reported input tokens, if the event contains them; includes prompt and other context, **not just source code**. Cache tokens are reported separately.
- **Navigation output estimate:** an explicit regex word/punctuation proxy over visible `rg`/`grep`/`cat`/`sed`/similar shell command output. It is **not** an exact model tokenizer, complete source exposure, or billing cost. Missing/empty successful navigation output or uninstrumented MCP calls suppress these numbers rather than silently counting zero.
- **Owner first seen:** the expected owning path first appears in a command or result. This is an observable **path mention**, not proof that the agent knew it was the right owner. Owner paths are used only by the offline evaluator, never leaked into task prompts.
- **Correct + verified:** independently defined success checks and baseline regression checks pass after Codex; not inferred from Codex's final summary. If checks don't prove the requested behavior, label the result provisional.

See `references/failure-taxonomy.md` when mapping repeated trace patterns to candidate source refactors. **Agent errors, tool bugs, poor task wording, and unavailable dependencies can mimic architecture failures; investigate before changing the source.**

## Source-first acceptance check

A cold-start agent should be able to find an authoritative owner via domain vocabulary; understand the interface without reading all internals; trace explicit dependency/side-effect wiring; change one cohesive behavior; identify its focused tests; and pass checks without learning the whole system. A human maintainer should find the resulting interface simpler, not just differently organized.

**Deliver:** changed source or ranked candidates; test and compatibility status; recorded navigation failures when available; paired correctness counts; measured costs with uncertainty and instrumentation caveats; any remaining friction.

**Stop rule:** if the source already has clear ownership and localized behavior, and additional restructuring would only make a benchmark look smaller, leave it alone.
