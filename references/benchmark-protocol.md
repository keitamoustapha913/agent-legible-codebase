# Benchmark: discoverability, context cost and maintainability

**Never replace correctness with a lower context metric.** Evaluate common, real changes under comparable task prompts, fresh agent contexts, and equivalent verification. This protocol is separate from the skill's code-design rules: source structure should make retrieval economical, not be distorted to win an artificial benchmark.

## Define workloads before refactoring

Choose 2–3 representative issues or bugfix-style requests from active code (one for a narrow change). Include at least one behavior change and one failure/edge case if meaningful. Prefer actual issue/change history. Write task descriptions without revealing the owning path, the expected tests, or the refactor's target files. Freeze the prompts *before* making structural changes.

A task is comparable only if the desired observable behavior, acceptance checks, model/tool access, and scope remain equivalent before and after. If the codebase's behavior is intentionally changed as part of a feature, compare an equivalent task objective carefully or mark the run not comparable.

## Navigate from cold start

1. Start each task with a fresh context. If available, use an independent agent not involved in the refactor; do not give it a map of the answer.
2. Let it explore naturally with the same tool permissions, model class, and effective budget. It can use existing search, symbol/navigation and test tools. Don't force it to load all source or one artificially narrow technique.
3. Record the **first correct owner** (not just the first guess). Capture cumulative source tokens shown by that point, and source tokens shown until it makes a verified change. Count actual content in file reads, search snippets, diffs and source-containing tool outputs, without treating unopened files as read.
4. Track which source files were opened, navigation/search steps, dependency hops and edited source files. Note cached context, hints, failed searches, and any noncomparable conditions.
5. Validate the outcome with equivalent tests and checks. Record `correct` separately from `verified`; if tests could not run, `verified: false`.
6. Run repeated independent trials where practical and use medians plus failure rate. Do not select only the fastest successful sample. The comparator pairs runs by task and trial ID.

You may use a consistent tokenizer estimate for exposed source text if actual source token counts are not available; name the tokenizer/estimator. **Do not call estimates observed model usage.** If a provider exposes total model input tokens, record them separately from source tokens. Total billed tokens also depend on non-source prompts, outputs and cache policies.

## Example trace format

Place two JSON files on disk, one for before and one for after:

```json
{
  "runs": [
    {
      "task_id": "change-coupon-cap",
      "trial_id": "A",
      "correct": true,
      "verified": true,
      "source_tokens_to_owner": 1800,
      "source_tokens_read": 5200,
      "search_steps": 4,
      "owner_steps": 6,
      "files_opened": ["checkout/pricing.ts", "checkout/pricing.test.ts"],
      "dependency_hops": 2,
      "changed_files": ["checkout/pricing.ts", "checkout/pricing.test.ts"]
    }
  ]
}
```

**All numbers above are purely illustrative** and must be replaced with actual observations. `total_input_tokens` is optional and must only be filled with real telemetry. Other numeric metrics may be omitted when not recorded; this produces "unrecorded," not zero. To repeat a task, include separate entries with distinct `trial_id` strings (e.g. `A`, `B`, `C`) in each file; keep corresponding IDs paired and comparable across versions. A single run can omit `trial_id` (defaults to `"1"`).

Then run:

```bash
python scripts/compare_traces.py before.json after.json
```

The script validates schema, matches tasks and trials, shows verification gates and medians for observed data, and reports correctness regressions. It cannot gather traces, prove cause and effect, independently check tests, or assign missing telemetry. If some runs fail the correctness gate, it reports that rather than silently discarding those failures.

## What to evaluate

**Primary:** all tasks remain correct and verified, with no unacceptable new security, compatibility or performance regression. Track failures and don't make an unconditional efficiency claim unless the comparison meets this gate.

**Discoverability:** source tokens and steps until identifying the actual owner, ambiguity of competing search results, whether tests were found without hints. A good design shortens the path from task vocabulary to relevant code.

**Context efficiency:** source tokens shown through the verified edit, distinct relevant source files and dependency hops. Compare actual total input tokens only if logged. Fewer opened files does not always mean fewer tokens, especially if one new file is massive.

**Maintainability:** files/behaviors changed for a common task, stability of the public contract, whether future changes are likely localized, high-value automated architecture checks, test execution cost, and whether the refactor introduced invisible wiring or cross-feature coupling.

## Confounders and disclosure

- The same agent learning during a previous task makes the later attempt non-cold; use fresh contexts.
- A change that simply renames the task to match the source artificially helps search; preserve task phrasing.
- Different tool snippet sizes, agent models, search indexing, or cached context distort token results.
- Choosing tasks after the refactor or dropping failed samples inflates improvements.
- If only a manual simulation is available, describe the probable improvement qualitatively. No made-up percentage, measured token count, or reduction guarantee.

**Report format:** per task/task-trial: baseline vs proposed verification, owner discovery path, observed source/model token counts if available, navigation steps, changed files, qualitative risks. State the number of independently repeated trials, and separate actual data from hypotheses.
