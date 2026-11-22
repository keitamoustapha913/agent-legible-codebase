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
        self.assertIn("REGRESSION", report)

    def test_counterbalanced_plan_has_both_orders(self):
        import benchmark_codex
        tasks = [{"id": "date"}, {"id": "currency"}]
        plan = benchmark_codex.plan_trials(tasks, 3, seed=42)
        self.assertEqual(plan, benchmark_codex.plan_trials(tasks, 3, seed=42))
        for task in tasks:
            trials = [plan[i:i+2] for i in range(0, len(plan), 2) if plan[i][0] == task]
            self.assertEqual(len(trials), 3)
            self.assertEqual({pair[0][2] for pair in trials}, {"before", "after"})
            self.assertTrue(all(a[:2] == b[:2] for a, b in trials))

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
                "success_checks": [[sys.executable, "-c", "import calc; assert calc.value() == 2, \"VALUE_NOT_TWO\""]],
                "expected_failure": {"regex": "^AssertionError: VALUE_NOT_TWO$"},
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
if args[0] == 'sandbox':
    assert ':workspace' in args and 'sandbox_workspace_write.network_access=false' in args
    if '--' in args and (Path(__file__).parent / 'block-runtime').exists():
        print(json.dumps({'probe': 'socketpair', 'status': 'error', 'errno': 1}))
        sys.exit(1)
    import os
    i = args.index('--')
    os.execv(args[i + 1], args[i + 1:])
(Path(__file__).parent / 'model-called').touch()
if 'exec' not in args or '--json' not in args:
    sys.exit(3)
assert args[args.index('--sandbox') + 1] == 'workspace-write'
assert 'sandbox_workspace_write.network_access=false' in args
cwd = Path(args[args.index('--cd') + 1])
prompt = sys.stdin.read()
if prompt.startswith("Run this one command"):
    cmd = prompt.splitlines()[-1]
    print(json.dumps({"type": "item.completed", "item": {"id": "smoke", "type": "command_execution",
                                                        "command": cmd, "exit_code": 124}}))
    print(json.dumps({"type": "turn.completed"}))
    sys.exit(0)
source = (cwd/'calc.py').read_text()
(cwd/'calc.py').write_text(source.replace('return 1', 'return 2'))
for event in [
    {'type': 'thread.started', 'thread_id': 'test'},
    {'type': 'item.completed', 'item': {'id': '1', 'type': 'command_execution', 'command': 'cat calc.py', 'aggregated_output': source, 'exit_code': 0, 'cwd': str(cwd)}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 420, 'cached_input_tokens': 0, 'output_tokens': 15}},
]:
    print(json.dumps(event), flush=True)
''')
            fake.chmod(0o755)
            out = root / "results"
            argv = [sys.executable, str(SCRIPTS / "benchmark_codex.py"), "--repo", str(repo),
                    "--manifest", str(manifest), "--before", "HEAD", "--after", "HEAD", "--out", str(out),
                     "--trials", "1", "--approve-manifest", "--allow-host-tests", "--sandbox-probe-python", sys.executable, "--codex", str(fake), "--model", "synthetic-test", "--timeout", "15"]
            dry = subprocess.run(argv + ["--dry-run"], capture_output=True, text=True, check=False)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse(out.exists())
            self.assertFalse((root / "model-called").exists())
            (root / "block-runtime").touch()
            blocked = root / "blocked"
            result = subprocess.run(argv + ["--out", str(blocked), "--sandbox-smoke"], capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            blocked_doc = json.loads((blocked / "results.json").read_text())
            self.assertEqual(blocked_doc["metadata"]["sandbox_runtime"]["reason"], "sandbox_python_local_ipc")
            self.assertNotIn("host_preflight", blocked_doc["metadata"])
            self.assertEqual(blocked_doc["runs"], [])
            self.assertIn("sandbox infrastructure", (blocked / "report.md").read_text().lower())
            self.assertFalse((root / "model-called").exists())
            (root / "block-runtime").unlink()
            run = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            data = json.loads((out / "results.json").read_text())
            self.assertEqual(data.get("schema_version"), 4)
            self.assertEqual(data["metadata"]["preflight_status"], "ready")
            self.assertEqual(len(data["runs"]), 2)
            self.assertEqual([run["execution_order"] for run in data["runs"]], [1, 2])
            self.assertEqual(len(subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain"]).split(b"worktree ")), 2)
            self.assertTrue(all(r["correct"] and r["verified"] for r in data["runs"]))
            self.assertTrue(all(r["total_input_tokens"] == 420 for r in data["runs"]))
            self.assertTrue(all(r["confirmed_source_reads"] == 1 for r in data["runs"]))
            self.assertNotIn("VALUE_NOT_TWO", (out / "results.json").read_text())
            self.assertEqual((repo / "calc.py").read_text(), "def value():\n    return 1\n")
            self.assertFalse(list(out.glob("*.jsonl")), "raw traces must be deleted by default")
            report = subprocess.run([sys.executable, str(SCRIPTS / "report_codex.py"), str(out / "results.json"),
                                     "--out", str(out / "report.md")], capture_output=True, text=True, check=False)
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertIn("Codex agent-navigation benchmark", (out / "report.md").read_text())
            smoke = root / "smoke"
            smoke_run = subprocess.run(argv + ["--out", str(smoke), "--sandbox-smoke"], capture_output=True,
                                       text=True, check=False, timeout=90)
            self.assertEqual(smoke_run.returncode, 2, smoke_run.stdout + smoke_run.stderr)
            smoke_doc = json.loads((smoke / "results.json").read_text())
            self.assertEqual(smoke_doc["metadata"]["preflight_status"], "ready")
            self.assertEqual(smoke_doc["metadata"]["sandbox_smoke_status"], "environment_blocked")
            self.assertEqual(smoke_doc["runs"], [])
            self.assertIn("**INVALID**", (smoke / "report.md").read_text())

            invalid = json.loads(manifest.read_text())
            invalid["tasks"][0]["success_checks"][0] = [sys.executable, "-c", "import no_such_fixture_module"]
            manifest.write_text(json.dumps(invalid))
            invalid_out = root / "invalid"
            invalid_run = subprocess.run(argv + ["--out", str(invalid_out)], capture_output=True,
                                         text=True, check=False, timeout=90)
            self.assertEqual(invalid_run.returncode, 2, invalid_run.stdout + invalid_run.stderr)
            invalid_doc = json.loads((invalid_out / "results.json").read_text())
            self.assertEqual(invalid_doc["metadata"]["preflight_status"], "preflight_invalid")
            self.assertEqual(invalid_doc["runs"], [])
            self.assertEqual(invalid_doc["metadata"]["host_preflight"][0]["success_checks"][0]["failure_reason"],
                             "environment_error")


if __name__ == "__main__":
    unittest.main()
