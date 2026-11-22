# Paired Codex benchmarks

Use matched before/after trials to test whether a source design affects independently verified edit tasks or factual code discovery. Freeze task wording, grading, setup and commits before running either side. Review [task design](task-design.md), [preflight and sandbox behavior](benchmark-environment.md), and [the measurement model](measurement-model.md) before spending usage.

1. Choose `--mode edit` (the default) for implementation tasks with independent success and regression checks. Choose `--mode discovery` for locating owners, symbols and dependencies without executing application code. Keep results in each mode separate because the tool sets and tasks differ.
2. Pin a single model and CLI for both sides. Keep settings, permissions, timeouts and checks constant. Each run uses a fresh ephemeral session and disposable checkout. The seed sets the initial orientation for each task and shuffles whole pairs. The runner alternates before-first and after-first orientation and records the plan, actual sequence and UTC start time. For odd counts one orientation necessarily runs one more time.
3. Use a one-pair-per-task instrumentation pilot to inspect capture and environment. Use at least three pairs per task for preliminary trends. Three pairs are still a small sample. Mark untouched tasks with `"holdout": true` and document which other tasks shaped the refactor.
4. Review a dry-run, the frozen manifest and any oracle sidecar. Keep every output and worktree outside the examined repository. Start live execution only after explicit usage approval.

```sh
python3 scripts/benchmark_codex.py --repo /path/to/project \
  --manifest /path/to/tasks.json --before BASELINE_COMMIT --after REFACTOR_COMMIT \
  --out /tmp/agent-legible-v4 --model YOUR_SUPPORTED_CODEX_MODEL \
  --trials 3 --dry-run --show-commands
```

5. With approval, replace `--dry-run --show-commands` with `--approve-manifest`. Count smoke sessions separately when using `--sandbox-smoke`. The runner stops on invalid preflight or evidence of a blocking or confounding environment issue and writes a report even if no task sessions ran.
6. Read `report.md` in order. Check all correctness and regression outcomes first, then matched both-success pairs and exclusions. Compare each task individually before the aggregate. Use the report's event references to investigate possible source design issues, environment problems and telemetry gaps. An invalid or inconclusive run supplies diagnostic evidence without supporting an architecture-efficiency decision.
7. Treat `PROMISING` as preliminary evidence and confirm on untouched holdouts. A favorable source-output median alone does not qualify. The preliminary trend gate requires complete coverage on the analyzed runs, pinned conditions, balanced and complete pairs, and lower paired provider input in at least one task without a task-level total or uncached input increase.

```sh
python3 scripts/report_codex.py /tmp/agent-legible-v4/results.json --out /tmp/agent-legible-v4/report.md
python3 scripts/compare_traces.py /tmp/agent-legible-v4/before.json /tmp/agent-legible-v4/after.json
```

The comparator remains available for saved traces with matched IDs. It labels legacy source metrics and does not validate experiment setup. Use the report for validity decisions.
