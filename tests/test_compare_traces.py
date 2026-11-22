"""Standard-library regression tests for the optional trace comparator."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_traces.py"


class TraceComparatorTests(unittest.TestCase):
    def compare(self, before: list[dict], after: list[dict]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as dirname:
            folder = Path(dirname)
            (folder / "before.json").write_text(json.dumps({"runs": before}), encoding="utf-8")
            (folder / "after.json").write_text(json.dumps({"runs": after}), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(folder / "before.json"), str(folder / "after.json")],
                capture_output=True, text=True, check=False
            )

    @staticmethod
    def run_record(trial: str = "A", tokens: int = 4000, **overrides) -> dict:
        record = {
            "task_id": "change-rule", "trial_id": trial, "correct": True,
            "verified": True, "source_tokens_to_owner": tokens // 2,
            "source_tokens_read": tokens, "files_opened": ["a.py", "b.py"],
        }
        record.update(overrides)
        return record

    def test_single_run_compatibility(self):
        before = self.run_record(tokens=4000)
        after = self.run_record(tokens=2000)
        before.pop("trial_id")
        after.pop("trial_id")
        result = self.compare([before], [after])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-50.0%", result.stdout)

    def test_multiple_trials_report_median(self):
        before = [self.run_record("A", 5000), self.run_record("B", 4000)]
        after = [self.run_record("A", 3000), self.run_record("B", 3000)]
        result = self.compare(before, after)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4500 → 3000 (-33.3%)", result.stdout)
        self.assertIn("n=2", result.stdout)

    def test_correctness_regression_blocks_claim(self):
        result = self.compare([self.run_record()], [self.run_record(correct=False)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("regressions: 1", result.stdout)

    def test_mismatched_trials_fail(self):
        result = self.compare([self.run_record("A")], [self.run_record("B")])
        self.assertEqual(result.returncode, 2)
        self.assertIn("Task/trial IDs differ", result.stderr)

    def test_duplicate_trial_fails(self):
        result = self.compare([self.run_record(), self.run_record()], [self.run_record()])
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)

    def test_invalid_counts_fail(self):
        result = self.compare([self.run_record(source_tokens_read=-1)], [self.run_record()])
        self.assertEqual(result.returncode, 2)
        self.assertIn("nonnegative", result.stderr)

    def test_unrecorded_metric_is_not_zero(self):
        first = self.run_record()
        second = self.run_record()
        first.pop("source_tokens_read")
        first.pop("source_tokens_to_owner")
        second.pop("source_tokens_read")
        second.pop("source_tokens_to_owner")
        result = self.compare([first], [second])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recorded source-context units through task: unrecorded", result.stdout)


if __name__ == "__main__":
    unittest.main()
