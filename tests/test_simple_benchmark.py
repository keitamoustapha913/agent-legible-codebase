"""Exercise the default benchmark with no paid sessions or VM tooling."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from report_codex import render_report


class SimpleBenchmarkTests(unittest.TestCase):
    def test_default_skips_all_host_commands_and_probe_opt_in_runs_checks_after_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "calc.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.org",
                            "commit", "-qm", "baseline"], check=True)
            marker = root / "host-runs"
            app_check = (
                "from pathlib import Path; assert 'VALUE = 2' in Path('calc.py').read_text(); "
                f"with_marker = open({str(marker)!r}, 'a'); with_marker.write('check\\n'); with_marker.close()"
            )
            host_command = [sys.executable, "-c", app_check]
            task = {"id": "adjust-value", "prompt": "Set the value to 2", "success_checks": [host_command],
                    "regression_checks": [host_command], "setup_commands": [host_command]}
            manifest = root / "tasks.json"
            manifest.write_text(json.dumps({"tasks": [task]}))
            fake = root / "fake-codex"
            fake.write_text(f"#!{sys.executable}\n" + '''
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli fixture")
    raise SystemExit(0)
assert args[:3] == ["-a", "never", "exec"], args
assert args[args.index("--sandbox") + 1] == "workspace-write"
assert "sandbox_workspace_write.network_access=false" in args
assert "sandbox_workspace_write.writable_roots=[]" in args
assert "sandbox_workspace_write.exclude_slash_tmp=true" in args
assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in args
prompt = sys.stdin.read().lower()
assert "pytest" in prompt and "do not run" in prompt
(Path(__file__).parent / "agent-called").touch()
Path("calc.py").write_text("VALUE = 2\\n")
print(json.dumps({"type":"item.completed","item":{"id":"read","type":"command_execution","command":"cat calc.py","aggregated_output":"VALUE = 1","exit_code":0,"cwd":str(Path.cwd())}}))
if (Path(__file__).parent / "attempt-pytest").exists():
    print(json.dumps({"type":"item.completed","item":{"id":"pytest","type":"command_execution",
        "command":"python -m pytest -q","aggregated_output":"","exit_code":124,"cwd":str(Path.cwd())}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":410,"cached_input_tokens":200,"output_tokens":15}}))
''')
            fake.chmod(0o700)
            argv = [sys.executable, str(SCRIPTS / "benchmark_codex.py"), "--repo", str(repo), "--manifest",
                    str(manifest), "--before", "HEAD", "--after", "HEAD", "--trials", "1", "--model", "fixture",
                    "--codex", str(fake)]
            off = root / "off"
            dry = subprocess.run(argv + ["--out", str(off), "--dry-run", "--show-commands"],
                                 text=True, capture_output=True, timeout=25)
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertIn("--allow-host-tests", dry.stdout)
            self.assertFalse((root / "agent-called").exists())
            self.assertFalse(off.exists())

            default = subprocess.run(argv + ["--out", str(off), "--approve-manifest"],
                                     text=True, capture_output=True, timeout=30)
            self.assertEqual(default.returncode, 0, default.stdout + default.stderr)
            self.assertFalse(marker.exists())
            doc = json.loads((off / "results.json").read_text())
            self.assertNotIn("sandbox_runtime", doc["metadata"])
            self.assertEqual(len(doc["runs"]), 2)
            self.assertTrue(all(r["behavior"] == "not_evaluated" and r["regression"] == "not_evaluated"
                                for r in doc["runs"]))
            self.assertIn("**Inconclusive**", (off / "report.md").read_text())
            self.assertIn("410", (off / "report.md").read_text())

            on = root / "on"
            enabled = subprocess.run(argv + ["--out", str(on), "--approve-manifest", "--allow-host-tests"],
                                     text=True, capture_output=True, timeout=30)
            self.assertEqual(enabled.returncode, 0, enabled.stdout + enabled.stderr)
            self.assertEqual(marker.read_text().splitlines(), ["check"] * 6)
            doc = json.loads((on / "results.json").read_text())
            self.assertTrue(all(r["correct"] and r["verified"] for r in doc["runs"]))
            self.assertEqual((repo / "calc.py").read_text(), "VALUE = 1\n")
            self.assertFalse([p for p in on.iterdir() if p.is_dir()])

            (root / "attempt-pytest").touch()
            stall = root / "stall"
            blocked = subprocess.run(argv + ["--out", str(stall), "--approve-manifest", "--allow-host-tests"],
                                     text=True, capture_output=True, timeout=30)
            self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
            self.assertIn("pytest execution observed", (stall / "report.md").read_text())
            self.assertEqual(len(marker.read_text().splitlines()), 6, "confounded trial must skip host checks")

            task = {"id": "adjust-value", "prompt": "Set the value to 2"}
            manifest.write_text(json.dumps({"tasks": [task]}))
            bare = subprocess.run(argv + ["--out", str(root / "bare"), "--dry-run"],
                                  text=True, capture_output=True, timeout=30)
            self.assertEqual(bare.returncode, 0, bare.stdout + bare.stderr)

    def test_simple_report_separates_correctness_from_cost(self):
        runs = []
        for trial in ("1", "2", "3"):
            for variant in ("before", "after"):
                runs.append({
                    "task_id": "edit", "trial_id": trial, "variant": variant,
                    "agent_completion": "completed", "environment": "ready",
                    "behavior": "correct", "regression": "passed", "total_input_tokens": 405 if variant == "before" else 300,
                    "search_steps": 5, "files_opened": ["calc.py"], "changed_source_files": ["calc.py"],
                })
        doc = {"schema_version": 4, "metadata": {"workflow": "simple", "mode": "edit", "experiment_status": "completed",
               "planned_run_count": 6, "model_requested": "fixture", "verification_environment": "host"}, "runs": runs}
        helped, _ = render_report(doc)
        self.assertIn("**Helped**", helped)
        self.assertIn("Maintenance effort", helped)
        self.assertIn("Discoverability", helped)
        for run in runs[1::2]:
            run["total_input_tokens"] = 500
        self.assertIn("**Worsened**", render_report(doc)[0])
        for run in runs[::2]:
            run["behavior"] = "not_evaluated"
        summary, _ = render_report(doc)
        self.assertIn("**Inconclusive**", summary)
        self.assertIn("500", summary)
