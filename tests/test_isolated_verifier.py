"""Isolated verifier tests with synthetic candidates and a fake QEMU transport."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from isolated_verifier import IsolatedVerifier, pack_candidate


class IsolatedVerifierTests(unittest.TestCase):
    def test_candidate_rejects_symlinks_and_preserves_deleted_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "checkout"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "gone.py").write_text("old")
            subprocess.run(["git", "-C", str(repo), "add", "gone.py"], check=True)
            (repo / "gone.py").unlink()
            (repo / "src.py").write_text("new")
            (repo / "secret.py").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                pack_candidate(repo, root / "source.zip")
            (repo / "secret.py").unlink()
            pack_candidate(repo, root / "source.zip")
            import zipfile
            with zipfile.ZipFile(root / "source.zip") as bundle:
                self.assertEqual(bundle.namelist(), ["src.py"])

    @unittest.skipUnless(shutil.which("mke2fs") and shutil.which("debugfs"), "ext2 tools required")
    def test_isolation_flags_integrity_timeout_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile.json"
            for name in ("kernel", "initramfs"):
                (root / name).write_text(name)
            config = {name: {"path": str(root / name), "sha256": hashlib.sha256(name.encode()).hexdigest()}
                      for name in ("kernel", "initramfs")}
            profile.write_text(json.dumps(config))
            qemu = root / "fake-qemu"
            qemu.write_text(f"#!{sys.executable}\n" + '''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
assert args[args.index("-nic") + 1] == "none"
assert "-nodefaults" in args and "-no-user-config" in args and "-virtfs" not in args
assert "elevateprivileges=deny" in args[args.index("-sandbox") + 1]
assert os.environ.get("ALC_TEST_SECRET") is None
drive = args[args.index("-drive") + 1]
assert "readonly=on" in drive and "format=raw" in drive
image = drive.split(",")[0].removeprefix("file=")
req = json.loads(subprocess.check_output(["debugfs", "-R", "cat request.json", image], stderr=subprocess.DEVNULL))
mode = (Path(__file__).parent / "mode").read_text()
if mode == "timeout":
    time.sleep(30)
if mode == "error":
    sys.exit(1)
if mode == "tampered":
    req["binding"]["nonce"] = "wrong"
print("ALC-VERDICT " + json.dumps({"binding": req["binding"], "isolation": "offline_unprivileged",
                                  "checks": {}}))
''')
            qemu.chmod(0o700)
            os.environ["ALC_TEST_SECRET"] = "must not reach the launcher"
            self.addCleanup(os.environ.pop, "ALC_TEST_SECRET", None)
            vm = IsolatedVerifier(profile, str(qemu), root, boot_timeout=0.75)
            for scenario, expected in (("ok", "ready"), ("tampered", "artifact_mismatch"),
                                       ("error", "vm_launch_failed"), ("timeout", "vm_timeout")):
                with self.subTest(scenario=scenario):
                    (root / "mode").write_text(scenario)
                    self.assertEqual(vm.probe()["status"], expected)
                    self.assertFalse(list(root.glob(".verifier-vm-*")))
            (root / "kernel").write_text("altered")
            with self.assertRaises(ValueError):
                vm.probe()

    @unittest.skipUnless(shutil.which("mke2fs") and shutil.which("debugfs"), "ext2 tools required")
    def test_runner_routes_baseline_and_candidate_checks_to_vm_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "src.py").write_text("BASELINE = True\n")
            (repo / "frozen.py").write_text("frozen")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=fixture@example.org",
                            "commit", "-qm", "baseline"], check=True)
            forbidden = root / "host-check-must-not-run"
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(forbidden)!r}).touch()"]
            manifest = root / "tasks.json"
            manifest.write_text(json.dumps({"tasks": [{"id": "fixture", "prompt": "edit source",
                "setup_commands": [command], "success_checks": [command], "regression_checks": [command],
                "expected_failure": {"regex": "^EXPECTED$"}}]}))
            profile = {"protected_paths": ["frozen.py"]}
            for name in ("kernel", "initramfs"):
                (root / name).write_text(name)
                profile[name] = {"path": str(root / name), "sha256": hashlib.sha256(name.encode()).hexdigest()}
            profile_file = root / "vm.json"
            profile_file.write_text(json.dumps(profile))
            qemu = root / "fake-qemu"
            qemu.write_text(f"#!{sys.executable}\n" + '''
import hashlib, io, json, subprocess, sys, zipfile
from pathlib import Path
args = sys.argv
image = args[args.index("-drive") + 1].split(",")[0].removeprefix("file=")
def read(name):
    return subprocess.check_output(["debugfs", "-R", "cat " + name, image], stderr=subprocess.DEVNULL)
req = json.loads(read("request.json"))
binding = req["binding"]
phase = binding["phase"]
checks = {}
if phase != "probe":
    archive = read("candidate.zip")
    assert binding["artifact_sha256"] == hashlib.sha256(archive).hexdigest()
    assert binding["manifest_sha256"]
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        assert hashlib.sha256(source.read("frozen.py")).hexdigest() == req["protected"]["frozen.py"]
        assert (b"BASELINE = True" in source.read("src.py")) == (phase == "baseline")
        assert ".git" not in source.namelist()
    green = {"exit_code": 0, "timed_out": False, "failure_reason": None}
    red = {"exit_code": 1, "timed_out": False, "failure_reason": "behavioral_assertion"}
    checks = {"setup_checks": [green], "success_checks": [red if phase == "baseline" else green], "regression_checks": [green]}
print("ALC-VERDICT " + json.dumps({"binding": binding, "isolation": "offline_unprivileged", "checks": checks}))
''')
            codex = root / "fake-codex"
            codex.write_text(f"#!{sys.executable}\n" + '''
import json, sys
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli fixture')
    sys.exit(0)
if args[0] == 'sandbox':
    assert args[args.index("-P") + 1] == "agent_legible_isolated_v4"
    assert 'permissions.agent_legible_isolated_v4.network.enabled=false' in args
    sys.exit(0)
assert args[:2] == ["-a", "never"]
assert 'default_permissions="agent_legible_isolated_v4"' in args and "shell_tool" in args
assert "You have no shell or test tool" in sys.stdin.read()
Path("src.py").write_text("BASELINE = False\\n")
(Path(__file__).parent / "fake-agent-ran").touch()
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":300,"cached_input_tokens":200,"output_tokens":31}}))
''')
            for executable in (qemu, codex):
                executable.chmod(0o700)
            out = root / "results"
            argv = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/benchmark_codex.py"),
                    "--mode", "isolated-verifier", "--repo", str(repo), "--manifest", str(manifest),
                    "--before", "HEAD", "--after", "HEAD", "--out", str(out), "--verifier-vm-profile",
                    str(profile_file), "--verifier-qemu", str(qemu), "--codex", str(codex),
                    "--trials", "1", "--check-timeout", "5", "--approve-manifest"]
            blocked_out = root / "blocked"
            blocked = subprocess.run(argv + ["--out", str(blocked_out), "--verifier-qemu", str(root / "missing")],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
            self.assertIn("**INVALID**", (blocked_out / "report.md").read_text())
            self.assertFalse((root / "fake-agent-ran").exists())
            self.assertFalse(forbidden.exists())
            ready = root / "ready"
            check = subprocess.run(argv + ["--out", str(ready), "--readiness-only"], capture_output=True,
                                   text=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertFalse((root / "fake-agent-ran").exists())
            result = subprocess.run(argv, capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads((out / "results.json").read_text())
            self.assertEqual(data["metadata"]["verification_environment"], "isolated_qemu")
            self.assertEqual(len(data["metadata"]["verifier_preflight"]), 2)
            self.assertNotIn("sandbox_runtime", data["metadata"])
            self.assertEqual(len(data["runs"]), 2)
            for run in data["runs"]:
                self.assertTrue(run["correct"] and run["verified"])
                self.assertEqual(run["changed_source_files"], ["src.py"])
                self.assertEqual(run["total_input_tokens"], 300)
                self.assertEqual(run["verification"]["binding"]["phase"], "candidate")
            report = (out / "report.md").read_text()
            for section in ("Independent VM verification", "Provider usage", "Navigation / source attribution", "Architecture evidence"):
                self.assertIn(section, report)
            self.assertFalse(forbidden.exists())
            self.assertFalse(list(out.glob(".verifier-vm-*")))
            self.assertEqual((repo / "src.py").read_text(), "BASELINE = True\n")

    def test_guest_rejects_path_traversal_and_invalid_verdict(self):
        from isolated_verifier import validate_checks
        from verifier_guest import extract
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with zipfile.ZipFile(root / "candidate.zip", "w") as bundle:
                bundle.writestr("../outside", "unsafe")
            cwd = root / "checkout"
            cwd.mkdir()
            with self.assertRaises(ValueError):
                extract(root / "candidate.zip", cwd)
            self.assertFalse((root / "outside").exists())
        commands = {"success_checks": [["check"]], "regression_checks": [["regression"]]}
        with self.assertRaises(ValueError):
            validate_checks({"success_checks": [], "regression_checks": []}, commands)
        exceeded = {"exit_code": None, "timed_out": True, "failure_reason": "timeout"}
        checks = validate_checks({"success_checks": [exceeded], "regression_checks": []}, commands)
        self.assertEqual(checks["success_checks"], [exceeded])
