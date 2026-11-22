# Designing real agent benchmark tasks and oracles

An architecture benchmark is only useful when it evaluates **task success**, not the appearance of a diff. Freeze prompts and oracles before refactoring. Do not hand the agent the expected owner path, test target, or desired code edit.

## Minimal manifest

```json
{
  "tasks": [
    {
      "id": "cap-discount",
      "prompt": "Ensure a promotional discount never exceeds the order subtotal, including when the promo value is greater than the subtotal.",
      "owners": {
        "before": ["src/billing/discounts.ts"],
        "after": ["src/checkout/discounts.ts"]
      },
      "success_checks": [["npm", "run", "test:discount-oracle"]],
      "regression_checks": [["npm", "run", "test:unit"]],
      "setup_commands": [["npm", "ci", "--offline"]]
    }
  ]
}
```

Paths and test commands above are placeholders—customize for the actual repo. The success oracle should fail before Codex edits the code on BOTH architecture snapshots, and pass afterward only if the new behavior exists; the regression checks must pass on both snapshots before the task. The runner enforces preflight by default. If `setup_commands` is not necessary, omit it.

## Good task mix

- One localized business-rule modification.
- One bug fix with a negative path or edge-case assertion.
- One feature involving a real dependency seam.
- One change requiring a test to be found and updated, without exposing the target file in the prompt.
- If possible, hold out a couple of new tasks that were **not** part of architecture selection.

## Correctness design

Success checks should be deterministic and specific to the requested behavior; do not use vague checks like an unchanged unit test suite as the only correctness signal. For feature changes, consider independent tests stored outside the repo and invoked by manifest argv, so Codex can't optimize against the oracle. External test scripts must have a stable, trusted path and should avoid requiring internet. Do not infer success from Codex saying "done" or from a pretty patch.

Regression checks should cover existing contracts and no unexpected behavior changes. Verify the test runner itself works before launching Codex, including dependencies, environment, and platform. A before-refactor failure that's actually a broken import is not a valid red test.

## Controlling fairness

- Same prompt, agent version, model, tool configuration, authentication scope, sandbox, timeouts, check commands and hardware environment as far as possible.
- Same behavioral semantics, explicit `--before` and `--after` immutable refs, equivalent dependency pinning.
- Fresh Codex sessions; no reused conversation or instructions revealing the owner. The evaluator's `owners` is not sent to Codex.
- No task-specific source map injected for one version. Existing repository-native `AGENTS.md` remains part of the environment; account for changes to it.
- Keep failing attempts in the outcome denominator; report all paired outcomes before successful-trial cost medians.
- Run a control experiment with the same commit as both revisions if you need to quantify day-to-day variance and harness overhead.

## Result interpretation

A source-level improvement should reduce code-reading and navigation **without lowering test success**. Improvements in `source_tokens_read` reflect only the parser's visible-shell-output proxy; real provider `total_input_tokens` may tell a different story due to cached context or other prompts. Consider both and investigate when they disagree.
