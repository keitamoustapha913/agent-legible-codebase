# What v4 measures

Model usage and source exposure have different units. Neither can be converted into the other.

| Metric | Meaning |
|---|---|
| Provider input, cached input, output | Counts reported by Codex's `turn.completed.usage`. Input includes instructions, prompt, tool content and other context. |
| Derived uncached input | Provider input less cached input. Both counters must be valid. Applicable pricing and billing assumptions would be required to estimate money. |
| Discovery | Command or tool counts, search fingerprints, paths mentioned, confirmed reads, first expected owner mention and offline discovery score. A mention can come from a listing and says nothing about comprehension. |
| Source exposure | UTF-8 bytes and optional word-or-punctuation regex units in attributable tool output. Search records include their filename and line prefix. The counts measure captured output and have no exact model-source-token equivalent. |
| Implementation | Git changed paths and changed application-source parent directories before verification, plus independent success and regression outcomes. Parent directories are observable change spread, and may not match architectural module boundaries. |
| Environment | Detected test commands, repeated identical commands, diagnostics, timeout exit codes, host setup and verifier outcomes. Counts describe observed events, including recognizable operations inside mixed commands. |

## What qualifies for source attribution

Supply `--repo-root` matching the checkout recorded in the trace. For a standalone trace, supply `--unknown-cwd` if unqualified paths did not reliably start from that root. Recorded command working directories take precedence. The benchmark runner treats relative shell paths as unscoped when a command working directory is absent, since a Codex tool can override the session checkout directory. For replay of a legacy batch the default is unknown relative cwd. Use `--assume-root-cwd` only with independent evidence of the working directory.

The shell parser uses lexical inspection and never runs a captured command. A fixed invocation such as `cat src/rule.py` or `sed -n '20,40p' src/rule.py` can qualify. A search can qualify when each observed line has a source filename and line number, and the search flags preserve contents. The dedicated discovery tools report successful reads and searches through their own JSONL events. Instructions, project docs, listings, test execution and diagnostics have separate categories. Git commands that can display source, unfamiliar tools, ambiguous paths and unsupported output stay unattributed.

Pipelines, compound commands, redirects, expansions and opaque scripts can contain several output producers. v4 retains detected operations and captured bytes for inspection and leaves the aggregate source count unknown. A fixed external-skill loop qualifies as instruction-only when its sole operation reads skill documents. Paths are treated as mentions until the parser can attribute displayed contents. Source detection uses conservative filename and directory conventions. For offline traces, a removed worktree cannot supply historical symlink details.

## Coverage and compatibility

- `complete_for_reported_model_usage` means the captured completed turns supplied valid input, cached and output counts without a known turn gap. `partial` and `missing` qualify provider telemetry independently of source coverage.
- Source coverage is `complete` when the supported observed tool events are all attributable, `partial` when some source was observed alongside gaps, and `insufficient` when gaps leave no attributable source evidence.
- `observed_source_output_bytes` and `observed_source_output_proxy_units` preserve usable observations even when the full-run totals are unknown. Reports use complete source totals for paired comparisons only.
- Tool output may be missing, empty or truncated by the client. Streaming fallback bytes and mixed output remain visible as evidence without becoming a source total. A listing with no displayed source can have a genuine zero. An unobserved read cannot.
- v4 sets legacy `source_tokens_read` and `source_tokens_to_owner` to null. Historical v3 `trace_complete_for_source_proxy` never certifies v4 attribution. Reclassify the raw JSONL to obtain v4 coverage. Source traces, command text, agent answers and verifier transcripts are excluded from default results.
