# Designing real agent benchmark tasks and oracles

An architecture benchmark is only useful when it evaluates **task success**, not the appearance of a diff. Freeze prompts and oracles before refactoring. Do not hand the agent the expected owner path, test target, or desired code edit.

An edit task only needs an ID and a prompt. The default skips application setup and test commands. See the [minimal edit example](../examples/tasks.example.json).

## Manifest with optional verification

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
      "expected_failure": {"regex": "^AssertionError: DISCOUNT_EXCEEDS_SUBTOTAL$", "exit_code": 1},
      "regression_checks": [["npm", "run", "test:unit"]],
      "setup_commands": [["npm", "ci", "--offline"]]
    }
  ]
}
```

Paths and test commands above are placeholders—customize for the actual repo. The success oracle should fail before Codex edits the code on BOTH architecture snapshots, and pass afterward only if the new behavior exists; the regression checks must pass on both snapshots before the task. In simple mode, `--allow-host-tests` runs configured checks after the model session. Advanced qualification or isolated verification validates both baselines first. Omit setup when checks need no preparation.

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

A source-level improvement should reduce code-reading and navigation **without lowering test success**. Compare attributable source bytes and provider input independently. Tool output coverage, instructions and caching can affect the two measurements differently. Investigate disagreements before changing the design.

## v4 oracle contracts and discovery

For advanced edit verification, give each success check a behavior-specific `expected_failure` contract. For example, an offline verifier can exit 1 and print a stable `AssertionError: DISCOUNT_EXCEEDS_SUBTOTAL` line only when the known task behavior is absent. Supply `{"regex": "^AssertionError: DISCOUNT_EXCEEDS_SUBTOTAL$", "exit_code": 1}`. Both frozen revisions must show this outcome for each success check. Use different host regression checks to prove ordinary existing behavior stays green. Keep expected answers and independent tests outside the agent worktree. Use a checksum-bound sidecar if changing the manifest would break its freeze.

For discovery, use [the discovery example](../examples/tasks.discovery.example.json). Ask a natural domain question and keep exact paths, symbols and dependency paths under each revision's private `expected` block in a file outside the tested repo. For multiple owner files, make `symbols` a mapping from each owner path to its declared names. A single owner can use a symbol list. The evaluator validates both frozen answers before starting and grades owner paths, symbol declarations and expected dependency paths offline. The rationale has a separate human-review field. Python declarations are checked with the AST. Other supported languages use conservative declaration patterns. Design tasks around declarations those checks can verify. See [the environment reference](benchmark-environment.md) for tool and sandbox restrictions.
