# Codex CLI benchmark protocol (v3)

## Prerequisites and trusted execution

Git, Python 3.10+, authenticated Codex CLI on `PATH`, a project with runnable tests. No Python packages are required for the scripts. The runner intentionally runs verifier commands **outside Codex's sandbox**; task manifests and repository test scripts must be trusted. Codex is constrained with `--sandbox workspace-write`, but a sandbox is not a substitute for running untrusted repositories in an isolated machine. The runner does **not** pass `--yolo` or bypass sandbox approvals.

Manual Codex install path: `.agents/skills/agent-legible-codebase/` inside the target repo, or `~/.agents/skills/agent-legible-codebase/` for the user. See README.

Official JSONL and CLI references:
- https://learn.chatgpt.com/docs/cli/reference
- https://github.com/openai/codex/blob/main/sdk/typescript/src/events.ts
- https://github.com/openai/codex/blob/main/sdk/typescript/src/items.ts

## Freeze task design *before* refactoring

Each task describes a real desired observable change in product language, without a path hint. Define a **task-specific success check** that fails on both untouched commits and succeeds only when the behavior is implemented, plus **regression checks** already passing on both. Both stages should be comparable and deterministic. For a refactored filesystem layout, use the separate `owners.before` and `owners.after` paths as offline evaluator metadata. They are **never injected into the agent prompt**. See `references/task-design.md`.

Choose tasks covering: a straightforward business-rule update; a bug fix with an edge case; a multi-step but bounded integration; and (if relevant) a change at a real seam. Keep some tasks as holdouts, not used to decide architecture.

## Prepare pinned revisions and manifest

Create two commits or immutable tags representing **behavior-equivalent** baseline and refactored architecture. Uncommitted changes are not included: the harness creates worktrees from those commits. Use a committed stable dependency lockfile. Run `git status`, record CLI version, and ensure tests work without interactive access.

Use `examples/tasks.example.json` as a template. Each verifier is an **array of argv tokens**. No `shell=True` or implicit shell expansion is used; pass wrappers explicitly if needed. Do not place secrets into prompts or test outputs. `setup_commands` is optional but can execute arbitrary host programs: review it just like verifiers.

```bash
python .agents/skills/agent-legible-codebase/scripts/benchmark_codex.py \
  --repo "$PWD" \
  --manifest /absolute/path/to/tasks.json \
  --before PRE_REFACTOR_COMMIT \
  --after POST_REFACTOR_COMMIT \
  --out /tmp/codex-benchmark-01 \
  --model YOUR_SUPPORTED_CODEX_MODEL \
  --trials 3 --dry-run
```

After reviewing, rerun without `--dry-run`. By default, preflight insists that success oracles are RED before the agent and regression checks GREEN. `--skip-preflight` is for exploratory runs; it reduces confidence and is recorded in results. Preflight and each agent run use new Git worktrees. Agent worktrees and raw traces are deleted by default; use `--keep-worktrees` or `--keep-raw` only if you intend to inspect those potentially sensitive artifacts.

Outputs, outside the repo: `results.json`, `before.json`, `after.json`, and auto-generated `report.md`. A checkpoint `results.partial.json` exists while running and can help diagnose interruptions. The CLI must be authenticated in the environment where the command is executed; the script cannot authenticate on your behalf. It also cannot run a real Codex benchmark without user-provided Git revisions and a valid task oracle.

## Report

```bash
python .agents/skills/agent-legible-codebase/scripts/report_codex.py \
  /tmp/codex-benchmark-01/results.json --out /tmp/codex-benchmark-01/report.md

python .agents/skills/agent-legible-codebase/scripts/compare_traces.py \
  /tmp/codex-benchmark-01/before.json /tmp/codex-benchmark-01/after.json
```

Report on: all-run pass rate, paired correctness regressions, medians over both-pass pairs, owners first *seen*, non-owner source reads before that first mention, repeated searches, shell navigation steps, changed source files, and tool coverage. `changed_files` is computed **after Codex but before the external verifier commands**, excluding paths touched during setup; if setup modifies a source path later edited by Codex, that overlap cannot be attributed reliably and is listed in `preagent_changed_files`. The harness does **not** infer dependency graph edges or prove that a non-owner read was a mistake; check such claims manually. Never discard failed trials to improve an efficiency average.

## Measurement and comparability limitations

1. **Input token telemetry:** `turn.completed.usage.input_tokens` is provider/CLI-reported. Sum completed turns. A timeout or CLI crash may omit usage. This is total model input context, including task prompt, system instructions, non-source material, and possibly cache accounting; it is not solely source-code consumption. Record `cached_input_tokens` separately. Same model, Codex version, tools, config, index state and prompt are essential.
2. **Source/output proxy:** Only known shell search/read commands are instrumented. It uses regex word/punctuation units, **not tiktoken**. Paths in a tool result can indicate an owner was *mentioned*, not recognized. Other tool calls (notably MCP) may contain source but are not accounted for.
3. **Known Codex output caveat:** some `--json` streams may contain missing or empty `aggregated_output`. See https://github.com/openai/codex/issues/48346 and https://github.com/openai/codex/issues/10141. The parser marks such traces incomplete and sets source-exposure estimates to null; do not treat missing data as a reduction. Zero-output search flags can also be legitimate; conservatism may produce false incompleteness.
4. **Order and non-determinism:** a shuffled run plan mitigates temporal order effects; three or more trials reduce sensitivity to one lucky search. It does not prove causality. Model upgrades, network/cached context, tool-index variation, prompt wording, and tests can distort results.
5. **Correctness:** the harness verifies only what configured check commands verify. An inadequate check is not proof that the task was properly implemented. Consider human review, unseen behavior oracles, smoke checks, and regression tests. A failed behavioral oracle that was already red due to missing dependencies is a bad task; fix the environment first.
6. **Cost:** a six-task × three-trial × two-version experiment is **36 agent executions**, plus preflight and verifier runs. Do not initiate it without the user's authorization and budget awareness.
7. **Privacy:** captured raw JSONL or verifier outputs may contain repository source, commands, file paths, secrets, or code. Raw traces are removed by default; sanitized summary retains paths and command fingerprints but not tool output bodies. Keep results private.

## Acceptance criteria

**Hard gate:** no new paired before-pass/after-fail correctness regression, and no materially worse success rate. Then examine whether median model input tokens and/or source-output proxy improved among valid paired trials, with sufficient nonmissing telemetry. Finally perform a human architecture review: fewer false leads, clearer interface, reduced dependency knowledge, no compatibility/security/performance degradation. If benefits are unclear or context estimates missing, report **inconclusive**.
