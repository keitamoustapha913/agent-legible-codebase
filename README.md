# Agent-Legible Codebase v4

[Open the v4 delivery handoff](V4-DELIVERY.md) for the changed files, test results, pilot analysis, limitations, and next benchmark.

A Codex skill for making source easier to navigate and change. It emphasizes deep modules, clear ownership, explicit wiring, local tests and evidence from independently verified tasks.

The design guidance is an independent synthesis inspired by Matt Pocock's [codebase-design](https://github.com/mattpocock/skills) and [improve-codebase-architecture](https://github.com/mattpocock/skills) skills.

| Mode | Use it to |
|---|---|
| Audit | Find concrete navigation problems and prioritize refactor candidates. |
| Design | Plan a clearer owner, interface and test seam. |
| Refactor | Make and verify small source-architecture changes. |
| Trace | Analyze saved Codex JSONL offline. |
| Benchmark edit | Compare independently verified task edits on two Git revisions. |
| Benchmark discovery | Compare how agents locate owners, symbols and dependencies through read-only navigation. |

## Install

Copy this directory to `<your-repo>/.agents/skills/agent-legible-codebase/` for one project, or to `~/.agents/skills/agent-legible-codebase/` for user-level access. Start a fresh Codex session and ask it to use `$agent-legible-codebase`. For example:

> Audit the checkout domain for unclear ownership, shallow modules and hard-to-find tests. Prioritize opportunities with concrete source paths.

## Plan a benchmark

Use Python 3.10+ and Git. The scripts use the standard library. A live benchmark also needs an authenticated Codex CLI, a suitable model and explicit usage approval. Discovery requires CLI support for read-only permission profiles and the scoped tool settings described in [the environment reference](references/benchmark-environment.md).

1. Freeze both Git commits and the task manifest. Start with the [edit example](examples/tasks.example.json) or [discovery example](examples/tasks.discovery.example.json). Keep the manifest and independent verifier files outside the tested repo. For edit tasks, supply a behavior-specific failure contract for each success check. For discovery, put revision-specific paths and symbols in the private `expected` block.
2. Print the plan and review the host commands. The dry-run creates no worktrees and starts no Codex, setup or verifier processes.

```sh
python3 scripts/benchmark_codex.py --repo /path/to/project \
  --manifest /path/to/tasks.json --before BASELINE_COMMIT --after REFACTOR_COMMIT \
  --out /tmp/agent-legible-v4 --trials 3 --model YOUR_SUPPORTED_CODEX_MODEL \
  --dry-run --show-commands
```

3. To plan discovery, pass `--mode discovery` with a discovery manifest and fresh output path. For either mode, replace the two dry-run flags with `--approve-manifest` only after approval for the planned usage and review of the host commands. `--trials 1` is an instrumentation pilot. `--sandbox-smoke` in edit mode authorizes one extra Codex session per task and revision.
4. Read `report.md`. The runner also writes `results.json`, `before.json` and `after.json`. Use `scripts/report_codex.py RESULTS_JSON --out REPORT_MD` to render it again. See [trial design and interpretation](references/codex-benchmark.md) and the [Sales pilot replay and next run](references/sales-pilot-reanalysis.md).

## Migrate v3 traces and results

v3 measured the aggregate output of some shell commands as a proxy. v4 requires a source path and attributable content. It counts visible source bytes and optional regex units. A mixed command leaves the full source total unknown while preserving any separately observed reads. Provider input, cached input, derived uncached input and output are independent metrics. The v4 schema leaves the old source-token fields null.

A single saved trace can be replayed without a Codex session:

```sh
python3 scripts/codex_trace.py /path/to/codex-exec.jsonl \
  --repo-root /original/worktree --owner src/checkout/discounts.ts \
  --output /tmp/codex-trace-summary.json
```

Add `--unknown-cwd` if the working directories of relative commands are uncertain. To reclassify an entire v3 result set, use the saved JSONL files named `TASK-TRIAL-VARIANT.jsonl`:

```sh
python3 scripts/report_codex.py /path/to/v3/results.json \
  --reanalyze-dir /path/to/raw-traces --worktree-root /original/worktree-parent \
  --legacy-manifest /path/to/frozen/tasks.json \
  --reanalysis-json /tmp/replayed-results.json --out /tmp/replayed-report.md
```

`--legacy-manifest` supplies optional offline owner labels. Omit `--worktree-root` if the worktrees were originally siblings of the traces. With historical JSON only, run the report without replay flags. The report labels historical source coverage unknown. See [the measurement model](references/measurement-model.md) for all coverage states.

Raw JSONL is private and opt-in with `--keep-raw`. Use `--keep-worktrees` only for manual inspection. Verifier results retain output hashes and diagnostic labels. Edit checks are trusted host processes. Codex edit sessions keep the workspace-write sandbox, and discovery uses a checked read-only profile.

## Verify the skill offline

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests use synthetic JSONL, temporary Git repositories, independent test commands and fake Codex executables. They use no model sessions.

## Package map

- `SKILL.md` defines the workflows and source-design rules. Architecture references cover depth, ownership, test seams and enforceable guardrails.
- `scripts/benchmark_codex.py` plans trials and coordinates approvals, preflight, execution and cleanup. `environment_probe.py` owns subprocess limits, diagnostics and fingerprints.
- `scripts/codex_trace.py` normalizes JSONL and independent usage and source metrics. `trace_attribution.py` classifies supported shell commands.
- `scripts/discovery_navigation.py` serves scoped listing, search and read operations. `discovery_oracle.py` validates and grades the answers offline.
- `scripts/report_codex.py` reports validity and per-task measurements and can replay old JSONL. `compare_traces.py` supports saved before/after files.
