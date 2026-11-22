"""Exercise verifier failures and subprocess cleanup without Codex."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark_codex import execute_command


class EnvironmentTests(unittest.TestCase):
    def test_only_expected_behavior_counts_as_red(self):
        with tempfile.TemporaryDirectory() as temp:
            for script, reason in (
                ("raise AssertionError('BEHAVIOR_ORACLE')", "behavioral_assertion"),
                ("import this_dependency_does_not_exist", "environment_error"),
                ("import time; time.sleep(60)", "timeout"),
                ("raise SystemExit(1)", "unknown"),
            ):
                with self.subTest(reason=reason):
                    result = execute_command([sys.executable, "-c", script], Path(temp), 0.3,
                                             expected_failure={"regex": "^AssertionError: BEHAVIOR_ORACLE$"})
                    self.assertEqual(result.get("failure_reason"), reason)
                    self.assertNotIn(script, str(result))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_verifier_timeout_terminates_stubborn_child(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            child = ("import os, signal, time; from pathlib import Path; "
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                     "Path('child.pid').write_text(str(os.getpid())); time.sleep(60)")
            parent = ("import subprocess, sys, time; "
                      f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)")
            try:
                result = execute_command([sys.executable, "-c", parent], path, 1)
                self.assertTrue(result["timed_out"])
                pid = (path / "child.pid").read_text()
                for _ in range(25):
                    proc = subprocess.run(["ps", "-o", "stat=", "-p", pid], capture_output=True, text=True)
                    if proc.returncode or proc.stdout.strip().startswith("Z"):
                        break
                    time.sleep(0.04)
                else:
                    self.fail("timed-out verifier left a child running")
            finally:
                if (path / "child.pid").exists() and 'proc' in locals() and proc.returncode == 0:
                    if not proc.stdout.strip().startswith("Z"):
                        os.kill(int((path / "child.pid").read_text()), 9)
