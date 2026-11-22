"""Validity and interpretation of benchmark reports."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import report_codex
from report_codex import render_report


def run(task, trial, variant, tokens, **overrides):
    return {
        "task_id": task, "trial_id": str(trial), "variant": variant, "correct": True, "verified": True,
        "agent_completion": "completed", "behavior": "correct", "regression": "passed", "environment": "ready",
        "model_usage_coverage": "complete_for_reported_model_usage", "source_attribution_coverage": "complete",
        "total_input_tokens": tokens, "cached_input_tokens": 0, "uncached_input_tokens": tokens, "output_tokens": 4,
        "source_output_bytes": 10, "execution_order": 2 * trial - int((variant == "before") == (trial % 2 == 1)),
        **overrides,
    }


class ReportTests(unittest.TestCase):
    def test_discovery_report_shows_medians_and_trial_reversal(self):
        runs = [run("invoice", trial, side, tokens, regression="not_evaluated", confirmed_source_reads=2,
                    discovery_score={"passed": trial != 2}, discovery_semantic_score={"status": "pass"},
                    search_steps=3, agent={"navigation_server": {"succeeded": 5, "failed": 0, "unparsed": 0}})
                for trial, after in enumerate((90, 110, 80), 1)
                for side, tokens in (("before", 100), ("after", after))]
        report, _ = render_report({"runs": runs, "metadata": {
            "workflow": "simple", "mode": "discovery", "experiment_status": "completed",
            "planned_run_count": 6, "model_requested": "synthetic"}})
        self.assertIn("| invoice | after | 2/3 passed | 3/3 passed | 90 (3) | 90 (3) | 3 (3) | 2 (3) | 5 (3) |",
                      report)
        self.assertIn("invoice input tokens moved differently from the median paired change in trial 2 (+10).", report)
        self.assertIn("invoice trial 2 (after) scored fail on strict grading and pass on semantic grading.", report)

    def test_host_green_but_sandbox_blocked_invalidates_experiment(self):
        report, eligible = render_report({
            "schema_version": 4, "metadata": {"preflight_status": "ready", "sandbox_smoke_status": "environment_blocked"},
            "runs": [],
        })
        self.assertFalse(eligible)
        self.assertIn("**INVALID**", report)
        self.assertIn("environment_blocked", report)

    def test_provider_and_source_coverage_are_independent(self):
        runs = [run("date", 1, "before", 400, source_attribution_coverage="partial"),
                run("date", 1, "after", 200)]
        report, _ = render_report({"schema_version": 4, "runs": runs})
        self.assertIn("complete_for_reported_model_usage", report)
        self.assertIn("partial", report)
        self.assertIn("mixed", report.lower())

    def test_v3_complete_flag_does_not_certify_v4_attribution(self):
        runs = [run("date", 1, side, 400, trace_complete_for_source_proxy=True, source_tokens_read=123456)
                for side in ("before", "after")]
        report, eligible = render_report({"runs": runs})
        self.assertFalse(eligible)
        self.assertIn("Historical v3", report)
        self.assertNotIn("123456", report)

    def test_per_task_provider_usage_exposes_opposing_directions(self):
        runs = [run(task, t, variant, tokens)
                for task, baseline, refactored in (("date", 500, 200), ("currency", 200, 500))
                for t in range(1, 4) for variant, tokens in (("before", baseline), ("after", refactored))]
        report, _ = render_report({"schema_version": 4, "runs": runs})
        self.assertRegex(report, r"\| date \| Total input \| 500 \| 200 \| -60.0% \| 3 \|")
        self.assertRegex(report, r"\| currency \| Total input \| 200 \| 500 \| \+150.0% \| 3 \|")

    def test_offline_replay_never_reuses_historical_attribution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = {"runs": [run("date", 1, side, 400, trace_complete_for_source_proxy=True, source_tokens_read=123456)
                             for side in ("before", "after")]}
            entry = {
                "type": "item.completed", "item": {
                    "id": "listing", "type": "command_execution",
                    "command": "rg --files; cat /external/skill/SKILL.md",
                    "aggregated_output": "src.py", "exit_code": 0,
                },
            }
            (root / "date-1-before.jsonl").write_text(
                json.dumps(entry) + "\n" + json.dumps({"type": "turn.completed"}) + "\n")
            replay = report_codex.reanalyze(data, root)
            self.assertIsNone(replay["runs"][0]["source_tokens_read"])
            self.assertEqual(replay["runs"][0]["events"][0]["category"], "mixed_unattributed")
            self.assertEqual(replay["runs"][1]["source_attribution_coverage"], "insufficient")
            report, eligible = render_report(replay)
            self.assertFalse(eligible)
            self.assertIn("1 of 2", report)

    def test_outcomes_require_planned_order_and_adequate_evidence(self):
        runs = [run("date", t, side, 400 if side == "before" else 200)
                for t in range(1, 4) for side in ("before", "after")]
        by_order = sorted(runs, key=lambda r: r["execution_order"])
        metadata = {"model_requested": "pinned", "codex_cli_version": "codex-cli fake",
                    "preflight_status": "ready", "experiment_status": "completed",
                    "configuration": {"sha256": "fixture"}, "planned_run_count": 6,
                    "planned_order": [{k: r[k] for k in ("task_id", "variant", "trial_id")} for r in by_order]}
        doc = {"schema_version": 4, "metadata": metadata, "runs": runs}
        report, eligible = render_report(doc)
        self.assertTrue(eligible)
        self.assertIn("**PROMISING**", report)
        for after in runs[1::2]:
            after.update(total_input_tokens=400, uncached_input_tokens=400)
        self.assertIn("**NO_DEMONSTRATED_GAIN**", render_report(doc)[0])
        runs[0]["source_attribution_coverage"] = "partial"
        self.assertIn("**INCONCLUSIVE**", render_report(doc)[0])
        runs[1]["environment"] = "confounded"
        self.assertIn("**INVALID**", render_report(doc)[0])
