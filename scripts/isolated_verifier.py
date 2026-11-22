"""Run a verification job in a fresh, offline QEMU guest."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import zipfile

from environment_probe import bounded_output, run_process

MAX_CANDIDATE_BYTES = 512 * 1024 * 1024
VERDICT_PREFIX = "ALC-VERDICT "
STAGES = ("setup_checks", "success_checks", "regression_checks")
REGRESSION_ASSERTION = {"regex": r"^(?:E\s+.*(?:assert|AssertionError)|AssertionError:?.*)$"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(raw: str) -> bool:
    path = PurePosixPath(raw)
    return bool(path.parts) and path.as_posix() == raw and not path.is_absolute() and "\\" not in raw and not (
        any(ord(c) < 32 for c in raw) or
        {".git", ".."} & set(path.parts)
    )


def protected_digests(cwd: Path, paths: list[str]) -> dict[str, str]:
    checksums = {}
    for name in paths:
        file = cwd / name
        if file.resolve() != file or not file.is_file() or file.stat().st_nlink != 1:
            raise ValueError("A protected verifier path is absent or unsafe")
        checksums[name] = sha256(file)
    return checksums


def file_identity(entry: os.stat_result) -> tuple:
    return (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_nlink, entry.st_size,
            entry.st_mtime_ns, entry.st_ctime_ns)


@contextmanager
def open_candidate(cwd: Path, name: str):
    # Resolve intermediate directories via owned descriptors with symlinks disabled.
    with ExitStack() as stack:
        directory = os.open(cwd, os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            directory = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            yield handle


def pack_candidate(cwd: Path, destination: Path) -> str:
    listing = subprocess.check_output([
        "git", "--no-optional-locks", "-C", str(cwd), "ls-files", "--cached",
        "--others", "--exclude-standard", "-z",
    ], timeout=30)
    size = 0
    with zipfile.ZipFile(destination, "w") as bundle:
        for raw in sorted(set(listing.decode("utf-8").split("\0")) - {""}):
            if not safe_relative(raw):
                raise ValueError("Unsafe candidate path")
            source = cwd / raw
            if source.resolve() != source:
                raise ValueError("Symlinks are not accepted in verifier artifacts")
            try:
                entry = source.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ValueError("Verifier artifacts accept only regular, unlinked files")
            size += entry.st_size
            if size > MAX_CANDIDATE_BYTES:
                raise ValueError("Candidate exceeds the verifier artifact limit")
            with open_candidate(cwd, raw) as handle:
                if file_identity(os.fstat(handle.fileno())) != file_identity(entry):
                    raise ValueError("Candidate changed during export")
                info = zipfile.ZipInfo(raw)
                info.external_attr = (0o755 if entry.st_mode & 0o111 else 0o644) << 16
                with bundle.open(info, "w") as member:
                    remaining = entry.st_size
                    while remaining:
                        chunk = handle.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError("Candidate changed during export")
                        member.write(chunk)
                        remaining -= len(chunk)
                    if handle.read(1):
                        raise ValueError("Candidate changed during export")
                if file_identity(os.fstat(handle.fileno())) != file_identity(entry):
                    raise ValueError("Candidate changed during export")
    return sha256(destination)


def pinned(path: Path, digest: str) -> Path:
    path = path.resolve(strict=True)
    if not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256(path) != digest:
        raise ValueError("Verifier input digest does not match the VM profile")
    return path


class IsolatedVerifier:
    def __init__(self, profile: Path, executable: str, root: Path, *, boot_timeout: float = 120):
        self.profile = profile.resolve()
        self.profile_sha256 = sha256(self.profile)
        self.config = json.loads(self.profile.read_bytes())
        if not isinstance(self.config, dict):
            raise ValueError("Expected a verifier VM profile object")
        self.kernel = self.asset("kernel")
        self.initramfs = self.asset("initramfs")
        self.protected = self.config.get("protected_paths", [])
        self.external = self.config.get("external_inputs", {})
        self.scratch = self.config.get("scratch_paths", [])
        if not isinstance(self.protected, list) or not all(isinstance(p, str) and safe_relative(p) for p in self.protected):
            raise ValueError("Invalid protected verifier paths")
        if (not isinstance(self.scratch, list)
                or any(not isinstance(p, str) or not safe_relative(p) for p in self.scratch)
                or any(PurePosixPath(s) == PurePosixPath(p) or PurePosixPath(s) in PurePosixPath(p).parents
                       for s in self.scratch for p in self.protected)):
            raise ValueError("Invalid writable guest scratch paths")
        if not isinstance(self.external, dict):
            raise ValueError("Invalid external verifier inputs")
        for name, digest in self.external.items():
            path = Path(name)
            if not path.is_absolute() or not safe_relative(name[1:]) or Path("/tmp") not in path.parents or path.resolve() != path:
                raise ValueError("External verifier paths must be regular absolute paths under /tmp")
            pinned(path, digest)
        qemu = shutil.which(executable)
        if not qemu:
            raise ValueError("QEMU verifier executable unavailable")
        self.executable = str(Path(qemu).resolve())
        self.qemu_sha256 = sha256(Path(self.executable))
        self.root = root
        self.boot_timeout = boot_timeout

    def asset(self, key: str) -> Path:
        asset = self.config.get(key, {})
        if not isinstance(asset, dict) or not isinstance(asset.get("path"), str) or not isinstance(asset.get("sha256"), str):
            raise ValueError(f"Missing pinned VM {key}")
        path = Path(asset["path"])
        return pinned(self.profile.parent / path, asset["sha256"])

    def probe(self) -> dict:
        return self.run(None, {"phase": "probe"}, self.boot_timeout)

    def verify(self, cwd: Path, task: dict, revision: str, manifest_sha: str, timeout: int, *,
               protected: dict[str, str], phase: str) -> dict:
        commands = {
            "setup_checks": task.get("setup_commands", []),
            "success_checks": task["success_checks"],
            "regression_checks": task["regression_checks"],
        }
        request = {
            "phase": phase, "task_id": task["id"], "revision": revision, "manifest_sha256": manifest_sha,
            "commands": commands, "contracts": {
                "success_checks": task["_contracts"],
                "regression_checks": [REGRESSION_ASSERTION] * len(commands["regression_checks"]),
            }, "check_timeout": timeout, "protected": protected, "scratch": self.scratch,
        }
        return self.run(cwd, request, self.boot_timeout + timeout * sum(map(len, commands.values())))

    def run(self, cwd: Path | None, request: dict, timeout: float) -> dict:
        if sha256(Path(self.executable)) != self.qemu_sha256:
            raise ValueError("QEMU executable changed during the experiment")
        if sha256(self.profile) != self.profile_sha256:
            raise ValueError("VM profile changed during the experiment")
        for key, path in (("kernel", self.kernel), ("initramfs", self.initramfs)):
            pinned(path, self.config[key]["sha256"])
        with tempfile.TemporaryDirectory(prefix=".verifier-vm-", dir=self.root) as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir(mode=0o700)
            binding = {
                "nonce": secrets.token_hex(16), "phase": request["phase"],
                "artifact_sha256": pack_candidate(cwd, data / "candidate.zip") if cwd else None,
                "profile_sha256": self.profile_sha256, "qemu_sha256": self.qemu_sha256,
                **{k: request.get(k) for k in ("task_id", "revision", "manifest_sha256")},
            }
            external = []
            if cwd:
                for index, (name, digest) in enumerate(sorted(self.external.items())):
                    member = f"input-{index}"
                    shutil.copyfile(pinned(Path(name), digest), data / member)
                    pinned(data / member, digest)
                    external.append({"name": member, "destination": name, "sha256": digest})
            (data / "request.json").write_text(json.dumps({**request, "binding": binding, "external": external}))
            for script in ("verifier_guest.py", "environment_probe.py"):
                shutil.copyfile(Path(__file__).with_name(script), data / script)
            image = root / "payload.img"
            size = sum(p.stat().st_size for p in data.iterdir())
            with image.open("wb") as stream:
                stream.truncate(size * 2 + 8 * 1024 * 1024)
            subprocess.run(["mke2fs", "-q", "-t", "ext2", "-F", "-d", str(data), str(image)],
                           check=True, timeout=60, capture_output=True)
            return self.launch(root, image, binding, request.get("commands", {}), timeout)

    def launch(self, root: Path, image: Path, binding: dict, commands: dict, timeout: float) -> dict:
        drive = f"file={str(image).replace(',', ',,')},format=raw,if=virtio,readonly=on"
        argv = [
            self.executable, "-no-user-config", "-nodefaults", "-machine", "q35,accel=tcg",
            "-m", "4096", "-smp", "2", "-display", "none", "-monitor", "none",
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-serial", "stdio", "-nic", "none", "-no-reboot", "-kernel", str(self.kernel),
            "-initrd", str(self.initramfs), "-append", "console=ttyS0 quiet panic=1 rdinit=/init",
            "-drive", drive,
        ]
        with tempfile.TemporaryFile(dir=root) as stdout, tempfile.TemporaryFile(dir=root) as stderr:
            process = run_process(argv, root, timeout, stdout=stdout, stderr=stderr,
                                  env={"PATH": os.defpath + ":/usr/sbin", "HOME": str(root), "LANG": "C"})
            output, stdout_info = bounded_output(stdout)
            _, stderr_info = bounded_output(stderr)
        record = {"binding": binding, "process": process, "stdout": stdout_info, "stderr": stderr_info}
        if process["timed_out"]:
            return {**record, "status": "vm_timeout"}
        if process["exit_code"] != 0:
            return {**record, "status": "vm_launch_failed"}
        try:
            messages = [json.loads(line[len(VERDICT_PREFIX):]) for line in output.splitlines()
                        if line.startswith(VERDICT_PREFIX)]
            if len(messages) != 1 or messages[0].get("binding") != binding:
                return {**record, "status": "artifact_mismatch"}
            message = messages[0]
            if message.get("isolation") != "offline_unprivileged":
                return {**record, "status": "guest_isolation_failed"}
            if message.get("error"):
                return {**record, "status": "guest_job_failed"}
            checks = validate_checks(message["checks"], commands)
        except (KeyError, TypeError, AttributeError, ValueError):
            return {**record, "status": "invalid_guest_result"}
        return {**record, "status": "ready", **checks}


def validate_checks(checks: dict, commands: dict) -> dict:
    if not isinstance(checks, dict) or set(checks) != set(commands):
        raise ValueError("VM check stages do not match job")
    sanitized = {}
    stopped = False
    for stage, expected in commands.items():
        entries = checks[stage]
        if not isinstance(entries, list) or len(entries) > len(expected) or (stopped and entries):
            raise ValueError("VM check count does not match job")
        sanitized[stage] = []
        for entry in entries:
            code, timeout, reason = (entry[key] for key in ("exit_code", "timed_out", "failure_reason"))
            if (code is not None and type(code) is not int or type(timeout) is not bool or
                reason not in (None, "timeout", "environment_error", "behavioral_assertion", "unknown", "interrupted")
                or timeout != (reason == "timeout") or (code == 0) != (reason is None)):
                raise ValueError("Invalid VM check result")
            if stopped:
                raise ValueError("VM returned checks after a stopping failure")
            sanitized[stage].append({"exit_code": code, "timed_out": timeout, "failure_reason": reason})
            stopped |= timeout or reason in {"interrupted", "environment_error"} or (stage == "setup_checks" and code != 0)
        if len(entries) < len(expected) and not stopped:
            raise ValueError("VM omitted expected checks")
    return sanitized
