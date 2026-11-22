"""Exercise manifest validation, dry-run safety, and a fake Codex end-to-end."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from benchmark_codex import BenchmarkError, parse_manifest
from report_codex import render_report


class BenchmarkTests(unittest.TestCase):
    def test_manifest_requires_real_oracles(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tasks.json"
            path.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "fix x", "success_checks": []}]}))
            with self.assertRaises(BenchmarkError):
                parse_manifest(path)

    def test_report_does_not_claim_success_on_regression(self):
        data = {"runs": [
            {"task_id": "bug", "trial_id": "1", "variant": "before", "correct": True, "verified": True, "total_input_tokens": 900},
            {"task_id": "bug", "trial_id": "1", "variant": "after", "correct": False, "verified": True, "total_input_tokens": 500},
        ]}
        report, eligible = render_report(data)
        self.assertFalse(eligible)
        self.assertIn("BLOCKED", report)

    def test_end_to_end_fake_codex_uses_worktrees_and_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "calc.py").write_text("def value():\n    return 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "calc.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            manifest = root / "tasks.json"
            manifest.write_text(json.dumps({"tasks": [{
                "id": "change-value", "prompt": "Update the value function to return 2.",
                "owners": {"before": ["calc.py"], "after": ["calc.py"]},
                "success_checks": [[sys.executable, "-c", "import calc; assert calc.value() == 2"]],
                "regression_checks": [[sys.executable, "-c", "import calc; assert callable(calc.value)"]],
            }]}))
            fake = root / "fake-codex"
            fake.write_text("#!" + sys.executable + "\n" + '''
import json, sys
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli fake-0.1')
    sys.exit(0)
if 'exec' not in args or '--json' not in args:
    sys.exit(3)
cwd = Path(args[args.index('--cd') + 1])
sys.stdin.read()
source = (cwd/'calc.py').read_text()
(cwd/'calc.py').write_text(source.replace('return 1', 'return 2'))
for event in [
    {'type': 'thread.started', 'thread_id': 'test'},
    {'type': 'item.completed', 'item': {'id': '1', 'type': 'command_execution', 'command': 'cat calc.py', 'aggregated_output': source, 'exit_code': 0}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 420, 'cached_input_tokens': 0, 'output_tokens': 15}},
]:
    print(json.dumps(event), flush=True)
''')
            fake.chmod(0o755)
            out = root / "results"
            argv = [sys.executable, str(SCRIPTS / "benchmark_codex.py"), "--repo", str(repo),
                    "--manifest", str(manifest), "--before", "HEAD", "--after", "HEAD", "--out", str(out),
                    "--trials", "1", "--codex", str(fake), "--model", "synthetic-test", "--timeout", "15"]
            dry = subprocess.run(argv + ["--dry-run"], capture_output=True, text=True, check=False)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse(out.exists())
            run = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            data = json.loads((out / "results.json").read_text())
            self.assertEqual(len(data["runs"]), 2)
            self.assertTrue(all(r["correct"] and r["verified"] for r in data["runs"]))
            self.assertTrue(all(r["total_input_tokens"] == 420 for r in data["runs"]))
            self.assertEqual((repo / "calc.py").read_text(), "def value():\n    return 1\n")
            self.assertFalse(list(out.glob("*.jsonl")), "raw traces must be deleted by default")
            report = subprocess.run([sys.executable, str(SCRIPTS / "report_codex.py"), str(out / "results.json"),
                                     "--out", str(out / "report.md")], capture_output=True, text=True, check=False)
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertIn("Codex agent-navigation benchmark", (out / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
