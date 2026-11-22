"""No-model tests of the workspace-write Python runtime gate."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sandbox_runtime import check_runtime


class SandboxRuntimeTests(unittest.TestCase):
    def test_host_control_and_socketpair_wakeup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "codex-fixture"
            codex.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "assert sys.argv[1] == 'sandbox' and ':workspace' in sys.argv\n"
                "i = sys.argv.index('--')\n"
                "os.execv(sys.argv[i + 1], sys.argv[i + 1:])\n"
            )
            codex.chmod(0o700)
            report = check_runtime(str(codex), sys.executable, root, timeout=3)
            self.assertEqual(report["status"], "ready")
            for side in ("host", "sandbox"):
                self.assertTrue(report[side]["socketpair_ok"])
                self.assertTrue(report[side]["to_thread_completed"])
                self.assertFalse(report[side]["timed_out"])

    def test_permission_denied_and_worker_timeout_are_infrastructure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "codex-fixture"
            events = [
                {"probe": "socketpair", "status": "error", "errno": 1},
                {"probe": "to_thread", "status": "worker_completed"},
            ]
            codex.write_text(
                f"#!{sys.executable}\n"
                "import time\n"
                + "".join(f"print({json.dumps(event)!r}, flush=True)\n" for event in events)
                + "time.sleep(30)\n"
            )
            codex.chmod(0o700)
            report = check_runtime(str(codex), sys.executable, root, timeout=1.5)
            self.assertEqual(report["status"], "environment_blocked")
            self.assertEqual(report["reason"], "sandbox_python_local_ipc")
            self.assertTrue(report["sandbox"]["worker_completed"])
            self.assertTrue(report["sandbox"]["timed_out"])
            self.assertFalse(report["sandbox"]["to_thread_completed"])

    def test_cli_support_or_host_failure_is_not_a_confirmed_ipc_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "codex-fixture"
            codex.write_text(f"#!{sys.executable}\nprint('secret raw text')\nraise SystemExit(2)\n")
            codex.chmod(0o700)
            report = check_runtime(str(codex), sys.executable, root, timeout=3)
            self.assertEqual(report["reason"], "sandbox_probe_failed")
            self.assertNotIn("secret raw text", str(report))
            host_failed = check_runtime(str(codex), str(root / "missing-python"), root, timeout=3)
            self.assertEqual(host_failed["reason"], "host_probe_failed")
            self.assertIsNone(host_failed["sandbox"])
