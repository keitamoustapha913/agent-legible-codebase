# Agent-Legible Codebase v4

A Codex skill for making source easier to navigate and change. It emphasizes deep modules, clear ownership, explicit wiring, local tests and evidence from independently verified tasks.

The design guidance is an independent synthesis inspired by Matt Pocock's [codebase-design](https://github.com/mattpocock/skills) and [improve-codebase-architecture](https://github.com/mattpocock/skills) skills.

| Mode | Use it to |
|---|---|
| Audit | Find concrete navigation problems and prioritize refactor candidates. |
| Design | Plan a clearer owner, interface and test seam. |
| Refactor | Make and verify small source-architecture changes. |
| Trace | Analyze saved Codex JSONL offline. |
| Benchmark edit | Compare the same Codex editing tasks on two Git revisions. Host checks are optional. |
| Benchmark discovery | Compare how agents locate owners, symbols and dependencies through read-only navigation. |
| Benchmark isolated verifier | Keep agent edits sandboxed and run independent tests inside offline QEMU guests. |

## Install

Copy this directory to `<your-repo>/.agents/skills/agent-legible-codebase/` for one project, or to `~/.agents/skills/agent-legible-codebase/` for user-level access. Start a fresh Codex session and ask it to use `$agent-legible-codebase`. For example:

> Audit the checkout domain for unclear ownership, shallow modules and hard-to-find tests. Prioritize opportunities with concrete source paths.

## Run a benchmark

Choose a Git repository, its original and refactored revisions, and a [task manifest](examples/tasks.example.json). An edit task only needs an ID and a prompt. Choose an output directory outside the repository.

```sh
python3 scripts/benchmark_codex.py --repo /path/to/project \
  --manifest /path/to/tasks.json --before BASELINE_COMMIT --after REFACTOR_COMMIT \
  --out /tmp/agent-legible-v4 --model YOUR_SUPPORTED_CODEX_MODEL \
  --trials 1 --dry-run
```

After approving the planned model usage, replace `--dry-run` with `--approve-manifest`. Each task runs once per revision per trial in a fresh disposable worktree using `codex exec --json`. Open `report.md` for the outcome, correctness, tokens, searches, file visits and edit scope. Use three or more pairs per task for a preliminary trend.

- **Edit is the default.** Codex can inspect and edit in its normal restricted workspace. The prompt tells it to leave pytest to the controller. Setup and host checks are skipped by default.
- **Discovery** uses `--mode discovery` and a discovery manifest. Run `--readiness-only` to verify that Codex's local tool client receives source contents with zero model sessions. When qualifying a new setup, approve one synthetic session with `scripts/discovery_smoke.py` before running blind trials. Discovery retains private raw tool logs and marks unusable navigation **INVALID**. The offline oracle scores the answer and no application tests run. [Discovery manifest](examples/tasks.discovery.example.json)
- **Optional host tests** use `--allow-host-tests` in addition to model approval. First run with `--dry-run --show-commands` and review every listed command and the code it can execute. When enabled, setup and tests run after Codex in the edited worktree with the controller's host permissions, outside the Codex sandbox. Use only with trusted projects and commands. Skipped or insufficient checks leave the architecture verdict inconclusive while still showing measured usage.
- **Advanced verification** uses [isolated-verifier mode](references/isolated-verifier.md) for an offline VM. The default flow needs no VM, Python sandbox probe or model smoke.

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

Raw JSONL is private and opt-in with `--keep-raw`. Use `--keep-worktrees` only for manual inspection. Verifier results retain output hashes and diagnostic labels. Codex edit sessions keep the workspace-write sandbox. Discovery uses a checked read-only profile.

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
- `scripts/discovery_readiness.py` checks source delivery through Codex's local MCP client. `discovery_smoke.py` checks delivery to the model in one separately approved synthetic session.
- `scripts/report_codex.py` reports validity and per-task measurements and can replay old JSONL. `compare_traces.py` supports saved before/after files.
