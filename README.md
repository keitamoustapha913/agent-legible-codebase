# Agent-Legible Codebase v3 — for Codex

A source-first architecture and empirical testing skill: deep, maintainable modules with obvious ownership, clear names, explicit runtime wiring, local tests and fewer irrelevant code-reading steps for AI coding agents.

This is an **independent synthesis** inspired by the principles of Matt Pocock's [codebase-design](https://github.com/mattpocock/skills) and [improve-codebase-architecture](https://github.com/mattpocock/skills) skills, plus an original Codex-oriented navigation benchmark. It is not affiliated with or endorsed by Matt Pocock or OpenAI. It does not copy those skills verbatim.

## What changed since v2

- **Trace collection from real Codex output:** `scripts/codex_trace.py` parses `codex exec --json` JSONL. It distinguishes provider-reported model token counts from rough source-output estimates and refuses incomplete data.
- **Isolated paired experiments:** `scripts/benchmark_codex.py` launches independent Codex sessions against pinned Git commits using disposable Git worktrees, deterministic task manifests, repeated trials, randomized ordering, and baseline-red/regression-green preflights.
- **Correctness-gated failure analysis:** `scripts/report_codex.py` reports pass rates, per-task outcomes, navigational churn and hypothesis-level architecture findings. `scripts/compare_traces.py` from v2 remains compatible.
- **Preserved v2 architecture discipline:** deep modules, cohesive ownership, progressive code disclosure, explicit dependency wiring, localized tests, architectural guardrails, and no pointless file splitting.
- **Confounder and privacy controls:** prompts never receive expected owner paths; outputs are outside the repo; raw logs are deleted unless opted into; no unsafe `--yolo` mode. Unlike v2, there is a functional recorder and experiment runner.

## Install into Codex

Unzip the downloaded package; it contains one directory, `agent-legible-codebase/`. Copy **that directory** to:

- Project-level: `<your-repo>/.agents/skills/agent-legible-codebase/`
- User-level: `~/.agents/skills/agent-legible-codebase/`

Codex skill folders contain a `SKILL.md` with `name` and `description` frontmatter; the remaining references/scripts load only as needed. See the [OpenAI skill layout](https://developers.openai.com/plugins/build/skills), and the [Codex CLI options](https://learn.chatgpt.com/docs/cli/reference).

Start a fresh Codex session and prompt:

> `$agent-legible-codebase Audit my current feature architecture for expensive navigation, unclear ownership, shallow modules and hard-to-find tests. Prioritize code-level improvements, not new docs.`

Or, when you explicitly want code changed:

> `$agent-legible-codebase Refactor the checkout domain so agent discovery and behavior edits are localized. Preserve public behavior, run focused/regression tests, and explain the before/after ownership.`

## Run a Codex benchmark on real revisions

Requirements: **Python 3.10+, Git, authenticated Codex CLI**. Codex CLI is **not** bundled in this ZIP. The code ships without any third-party Python dependencies.

1. Freeze task prompts and trusted command-list tests in your task manifest. Start with `examples/tasks.example.json`, replacing placeholder paths/test commands. Success checks must fail on untouched commits (a meaningful unsolved behavior) and regression checks pass on them.
2. Commit the before and after source snapshots as immutable refs. Each snapshot should have equivalent external behavior before the benchmark task is solved.
3. Run a dry plan from the installed skill directory (or replace the script path with your extracted local directory):

```sh
python .agents/skills/agent-legible-codebase/scripts/benchmark_codex.py \
  --repo "$PWD" \
  --manifest /absolute/path/to/tasks.json \
  --before BASELINE_COMMIT --after REFACTOR_COMMIT \
  --out /tmp/agent-legible-v3-run1 \
  --trials 3 --model YOUR_SUPPORTED_CODEX_MODEL --dry-run
```

4. If the manifest is trusted, the task oracle is valid, and you accept model usage, rerun the command **without `--dry-run`**. By default the script runs preflight success/regression checks first, then fresh Codex sessions with a workspace-write sandbox in disposable Git worktrees.
5. `report.md` is generated automatically. To regenerate it from recorded results:

```sh
python .agents/skills/agent-legible-codebase/scripts/report_codex.py \
  /tmp/agent-legible-v3-run1/results.json \
  --out /tmp/agent-legible-v3-run1/report.md

python .agents/skills/agent-legible-codebase/scripts/compare_traces.py \
  /tmp/agent-legible-v3-run1/before.json /tmp/agent-legible-v3-run1/after.json
```

With six tasks, three trials each and two versions, expect **36 Codex executions** plus tests. You can start with two tasks and one trial to validate instrumentation, but one trial is not robust evidence of an architecture win.

## Analyze already captured Codex JSONL (without running Codex)

```sh
python .agents/skills/agent-legible-codebase/scripts/codex_trace.py \
  /path/to/codex-exec.jsonl \
  --owner src/checkout/discounts.ts \
  --output /tmp/codex-trace-summary.json
```

For comparable experiments, expected owner paths are offline labels, not inserted into the agent prompt.

## Reading the numbers correctly

`total_input_tokens` is the **Codex provider-reported** count if `turn.completed` includes usage; source/navigation metrics are a **regex proxy over observed shell command output**, not actual source billing tokens or complete model context. Incomplete/empty Codex command output or unsupported tool calls make those source metrics unavailable, not zero. `owner_steps` refers to the first observed mention of a known correct owner, not proof of cognitive recognition. Tests are only as strong as your independent success and regression oracles.

The collector reads **non-interactive `codex exec --json` traces only**; it does **not** automatically intercept interactive Codex TUI sessions, personal Codex history, browser traces, or hidden reasoning. It does not run Codex or inspect a repository until you explicitly invoke the benchmark. Running Codex may contact OpenAI services and consume your plan or API usage. Worktree files and raw JSONL are deleted by default; `--keep-worktrees` / `--keep-raw` retain potentially sensitive content. Trusted manifest verifier commands run on the host, outside Codex sandbox, so review them first.

## Run the included tests

```sh
python -m unittest discover -s .agents/skills/agent-legible-codebase/tests -v
```

The tests exercise synthetic JSONL parsing, correctness-regression reporting, and a complete fake-Codex run against a temporary Git repo. **They do not constitute a live Codex benchmark on your codebase.**

## Package structure

```text
agent-legible-codebase/
├── SKILL.md
├── README.md
├── scripts/
│   ├── benchmark_codex.py
│   ├── codex_trace.py
│   ├── report_codex.py
│   └── compare_traces.py
├── examples/
│   └── tasks.example.json
├── references/
│   ├── codex-benchmark.md
│   ├── task-design.md
│   ├── failure-taxonomy.md
│   ├── design-principles.md
│   ├── context-locality.md
│   ├── architecture-guardrails.md
│   └── benchmark-protocol.md
└── tests/
    ├── test_codex_trace.py
    ├── test_benchmark_codex.py
    └── test_compare_traces.py
```
