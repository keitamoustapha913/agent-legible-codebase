# Sales pilot reanalysis

**Outcome: INCONCLUSIVE.** Four saved Codex JSONL traces were reclassified offline. The original checks recorded both tasks as successful and regression-clean on each revision. Each task has one pair, both pairs ran after-first, and historical preflight did not establish the intended assertion failure under the v4 contract. The original runs supply no comparable application-source totals.

## Source attribution before and after replay

| Task | Revision | v3 whole-run regex proxy | v4 mixed events | Captured mixed bytes | v4 source total |
|---|---|---:|---:|---:|---|
| Date fallback | Before | 15,008 | 18 | 134,313 | Unknown |
| Date fallback | After | 23,936 | 16 | 90,198 | Unknown |
| Blank currency | Before | 80,463 | 20 | 356,874 | Unknown |
| Blank currency | After | 37,711 | 34 | 130,195 | Unknown |

- Both baseline `item_1` events combined listing, Git or environment actions, and external skill reads. v3 treated them as listings and gave them zero source proxy units. v4 records the detected operations and marks the combined 40,295 bytes for date and 39,828 bytes for currency as mixed.
- Both after `item_1` events were fixed external-skill read loops. v3 included the instruction text in its source proxy. v4 attributes 39,548 captured bytes in each to agent instructions and zero source bytes for those individual events.
- No event in these traces qualified as an unmixed, scoped application-source read or search under the conservative replay. Both the default unknown-cwd replay and a root-cwd sensitivity check produced unknown source totals. Synthetic tests demonstrate that a known in-repo `cat` or `sed` with usable output still produces a measurement. The mixed-byte counts include capture and terminal formatting and cannot be used as a source-volume ranking.
- Three traces had exit-124 events. Date before had four (items 15, 19, 27 and 28). Currency before had two (items 15 and 22) and currency after had four (items 20, 21, 24 and 31). Date after had no observed exit-124 event. These events warrant environment investigation. The logs do not establish what caused the stalls.

## Provider usage stayed observable

| Task | Revision | Total input | Cached input | Uncached input | Output |
|---|---|---:|---:|---:|---:|
| Date fallback | Before | 1,792,229 | 1,732,608 | 59,621 | 6,222 |
| Date fallback | After | 1,165,006 | 1,116,032 | 48,974 | 4,019 |
| Blank currency | Before | 1,935,215 | 1,864,704 | 70,511 | 6,020 |
| Blank currency | After | 3,154,190 | 3,082,880 | 71,310 | 7,871 |

Date total input fell 35.0% and its uncached input fell 17.9% in one pair. Currency total input rose 63.0% and its uncached input rose 1.1% in one pair. These are recorded provider counts. Source exposure, billing and attribution to an architecture change remain unknown.

## Reproduce the replay and review the next run

The saved traces were read in place. Use the following command from the skill repository without launching a session:

```sh
python3 scripts/report_codex.py /tmp/qworks-sales-pilot.kxRdTx/results/results.json \
  --reanalyze-dir /tmp/qworks-sales-pilot.kxRdTx/results \
  --legacy-manifest /tmp/qworks-sales-benchmark.W7QcTc/tasks.json
```

The next edit study stays on the frozen revisions and manifest. The example [oracle sidecar](../examples/sales-pilot.oracle.json) binds assertion patterns to the original manifest digest without changing the manifest or verifiers. These patterns were derived from the frozen verifier's assertions and still need validation against actual host preflight output on both revisions.

```sh
python3 scripts/benchmark_codex.py \
  --repo /home/theyeq-admin-lap/master-workspace-2/Qworks-erp-native-register-json-recovery \
  --manifest /tmp/qworks-sales-benchmark.W7QcTc/tasks.json \
  --oracle-contract examples/sales-pilot.oracle.json \
  --before d682cc8884773063b361e40354b544c0950a228f \
  --after a3ece821fa3112858175fd7c8081eec1765eeeb9 \
  --out /tmp/agent-legible-v4-sales-study \
  --model gpt-6.2-sol --trials 3 --dry-run --show-commands
```

Review the plan, manifest and sidecar. A three-pair study on these two tasks schedules 12 sessions. For the first approved run, use `--trials 1 --sandbox-smoke` on a fresh output path to check host and sandbox behavior and instrumentation. That plan authorizes up to four smoke sessions and four task sessions. Replace `--dry-run --show-commands` with `--approve-manifest` only after approval for those sessions. If host preflight or sandbox smoke fails, resolve the reported environment issue before the study. The frozen tasks were used in the pilot. Include independently selected holdouts before drawing broader conclusions.
