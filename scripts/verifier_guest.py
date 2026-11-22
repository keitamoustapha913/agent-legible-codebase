"""Trusted QEMU guest driver. The VM image starts this from the readonly job disk."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

from environment_probe import execute_command

UID = GID = 65534
GUEST_ENV = ["PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "HOME=/tmp/alc-home",
             "UV_CACHE_DIR=/var/cache/alc-uv", "UV_PYTHON_INSTALL_DIR=/opt/alc-python"]
DROP = ["/usr/bin/env", "-i", *GUEST_ENV, "/usr/bin/setpriv", "--reuid", str(UID),
        "--regid", str(GID), "--clear-groups", "--no-new-privs", "--"]


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def extract(archive: Path, checkout: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            if (not path.parts or path.is_absolute() or path.as_posix() != member.filename
                    or {"..", ".git"} & set(path.parts) or "\\" in member.filename):
                raise ValueError("Unsafe candidate archive member")
            target = checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("xb") as dest:
                shutil.copyfileobj(source, dest)
            target.chmod(0o755 if (member.external_attr >> 16) & 0o111 else 0o644)


def isolation_probe(payload: Path) -> bool:
    # Trust the pinned initramfs and QEMU boundary. Check the guest side of
    # those settings as well: no NIC, no shared mount, no inherited secrets,
    # and no access to the input disk or verdict channel from the test UID.
    if (os.getuid() != 0 or set(p.name for p in Path("/sys/class/net").iterdir()) - {"lo"}
            or " - 9p " in Path("/proc/self/mountinfo").read_text()
            or " - virtiofs " in Path("/proc/self/mountinfo").read_text()
            or payload.stat().st_dev == payload.parent.stat().st_dev):
        return False
    probe = """import asyncio, os, sys
from pathlib import Path
assert os.getuid() == 65534 and os.getgid() == 65534 and not os.getgroups()
assert not os.access(sys.argv[1], os.R_OK)
assert not os.access('/dev/ttyS0', os.W_OK)
assert not os.access('/dev/console', os.W_OK)
assert not os.access('/dev/vda', os.R_OK)
assert set(os.environ) <= {'PATH','LANG','HOME','UV_CACHE_DIR','UV_PYTHON_INSTALL_DIR'}
assert asyncio.run(asyncio.to_thread(lambda: 7)) == 7
"""
    return execute_command([*DROP, sys.executable, "-I", "-S", "-c", probe,
                            str(payload / "request.json")], Path("/tmp"), 5)["exit_code"] == 0


def make_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        os.chown(path, UID, GID, follow_symlinks=False)


def place_external(payload: Path, externals: list[dict]) -> None:
    for item in externals:
        source = payload / item["name"]
        destination = Path(item["destination"])
        if (Path("/tmp") not in destination.parents or destination.resolve() != destination
                or sha256(source) != item["sha256"]):
            raise ValueError("External oracle artifact mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target, source.open("rb") as original:
            shutil.copyfileobj(original, target)
        destination.chmod(0o444)


def protect_checkout(checkout: Path, expected: dict[str, str]) -> None:
    for name, digest in expected.items():
        path = checkout / name
        if sha256(path) != digest:
            raise ValueError("Frozen verifier path changed")
        os.chown(path, 0, 0)
        path.chmod(0o444)
        subprocess.run(["mount", "--bind", str(path), str(path)], check=True, timeout=5)
        subprocess.run(["mount", "-o", "remount,bind,ro", str(path)], check=True, timeout=5)
        for parent in path.parents:
            os.chown(parent, 0, 0)
            parent.chmod(0o755)
            if parent == checkout:
                break


def execute_job(payload: Path, request: dict) -> dict:
    result = {stage: [] for stage in request["commands"]}
    source = payload / "candidate.zip"
    if sha256(source) != request["binding"]["artifact_sha256"]:
        raise ValueError("Candidate digest mismatch")
    home = Path("/tmp/alc-home")
    home.mkdir(mode=0o700)
    os.chown(home, UID, GID)
    place_external(payload, request["external"])
    # The guest powers off after a single job. Keep mountpoints intact
    # until shutdown rather than traversing candidate-created paths at cleanup.
    checkout = Path(tempfile.mkdtemp(prefix="alc-candidate-", dir="/tmp"))
    extract(source, checkout)
    make_writable(checkout)
    for name in request["scratch"]:
        scratch = checkout / name
        scratch.mkdir()
        os.chown(scratch, UID, GID)
    protect_checkout(checkout, request["protected"])
    for stage, commands in request["commands"].items():
        contracts = request["contracts"].get(stage)
        for index, argv in enumerate(commands):
            outcome = execute_command([*DROP, *argv], checkout, request["check_timeout"],
                                      expected_failure=contracts[index] if contracts else None)
            if outcome["failure_reason"] == "timeout":
                outcome["timed_out"] = True
            result[stage].append(outcome)
            if outcome["timed_out"] or outcome["failure_reason"] in {"interrupted", "environment_error"}:
                return result
            if stage == "setup_checks" and outcome["exit_code"] != 0:
                return result
    return result


def main() -> int:
    payload = Path(sys.argv[1])
    request = json.loads((payload / "request.json").read_text())
    result = {"binding": request["binding"]}
    try:
        if isolation_probe(payload):
            result["isolation"] = "offline_unprivileged"
            result["checks"] = {} if request["phase"] == "probe" else execute_job(payload, request)
        else:
            result["error"] = "isolation_check_failed"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        result["error"] = "guest_job_failed"
    print("ALC-VERDICT " + json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
