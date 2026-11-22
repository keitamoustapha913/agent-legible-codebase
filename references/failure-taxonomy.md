# Codex navigation failures → architecture hypotheses

**These are diagnostic hypotheses, not automatic refactor instructions.** An agent's weak query, missing search tool, flaky test, stale index, or poor task wording can produce the same symptoms as a codebase design flaw. Inspect actual trace evidence and test a small counterfactual change.

| Observable trace | Plausible source problem | Cheapest source-level experiment | Counterevidence to check |
|---|---|---|---|
| 7+ searches, competing names | Business term absent from paths, scattered handlers, duplicated aliases | Align domain vocabulary in owning path/export/test; establish one rule owner | Search syntax bad; task used an alien term |
| Owner path first seen after many steps | Ambiguous entry point; generic file names; indirect registration | Make feature owner/search surface unambiguous; make wiring directly traceable | Agent ignored obvious result; path first seen != understood |
| Repeated search fingerprints | Agent loops through the same failed retrieval | Concentrate behavior; remove misleading synonyms | Search command output truncated or permission denied |
| Repeated cross-module reads for a local rule | Callers orchestrate internals; shallow modules | Deepen the true behavior owner; narrow public contract | Task inherently integrates many systems |
| 5+ changed paths on a small behavior task | Change amplification; cross-feature ownership leakage | Move the invariant to one authoritative module, update callers | Migration, platform files or tests legitimately changed |
| Lots of very small forwarding layers | Indirection without meaningful information hiding | Apply the deletion test; absorb unnecessary wrappers | Seam has real alternative implementation/test requirements |
| Large file read for one behavior | Mixed responsibilities and poor progressive disclosure | Extract *cohesive* behavior with simple interface | A split could introduce more indirection and new hops |
| Runtime registration requires indirect search | Hidden composition or reflection, magic strings | Name explicit composition root and registration | Framework conventions require discovery; don't break them blindly |
| Missing JSON output / MCP read | Telemetry gap rather than architecture | Improve instrumentation or report only real model tokens | Do not report a fabricated source-token reduction |
| Lower tokens but failed acceptance checks | Benchmark gaming or reliability regression | Revert or repair; verify again before optimizing | Test harness itself broken or nondeterministic |

## Investigative loop

1. Inspect the flagged `events` in the sanitized `results.json`; use raw logs only if you explicitly retained them and have appropriate access controls.
2. Form a hypothesis using *real affected paths* and the observed task, including a likely non-architectural explanation.
3. Check whether a source change would improve the module's interface/locality for human engineers, not only a particular search query.
4. Change the smallest cohesive slice and preserve contract tests and compatibility.
5. Run the paired task suite and untouched holdouts. Keep the improvement only if correctness and maintenance quality hold.

Do not invent dependency-hop counts, mental "recognition" moments, or exact input tokens from shell output alone. Report metrics omitted by instrumentation as unavailable, never zero.
