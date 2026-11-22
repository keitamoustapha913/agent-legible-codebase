"""Run and diagnose local checks and opt-in Codex subprocesses."""
from __future__ import annotations

import hashlib
import os
import platform
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

GRACE_SECONDS = 0.2
DIAGNOSTIC_LIMIT = 128 * 1024
INFRASTRUCTURE = {
    "missing_dependency": r"\b(?:ModuleNotFoundError|ImportError|Cannot find module|No module named)\b",
    "permission_denied": r"\b(?:PermissionError|Permission denied|Operation not permitted|sandbox denied)\b",
    "missing_executable_or_file": r"\b(?:command not found|No such file or directory|file or directory not found)\b",
    "test_collection_or_usage": r"\b(?:ERROR collecting|ERROR at setup|collected 0 items|unrecognized arguments|INTERNALERROR)\b",
    "runtime_fault": r"\b(?:Segmentation fault|Fatal Python error|ConnectionRefusedError)\b",
}


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """Signal the whole POSIX group, including after its leader exits."""
    if os.name != "posix":
        proc.kill()
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    time.sleep(GRACE_SECONDS)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_process(argv: list[str], cwd: Path, timeout: float, *, stdout: Any, stderr: Any,
                prompt: str | None = None) -> dict:
    started = time.monotonic()
    returncode = None
    reason = None
    try:
        with subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr,
                              stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                              text=True, start_new_session=os.name == "posix") as proc:
            try:
                proc.communicate(prompt, timeout=timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                reason = "timeout"
                terminate_process_tree(proc)
            except KeyboardInterrupt:
                reason = "interrupted"
                terminate_process_tree(proc)
    except OSError:
        reason = "environment_error"
    return {"exit_code": returncode, "timed_out": reason == "timeout", "failure_reason": reason,
            "duration_seconds": round(time.monotonic() - started, 3)}


def bounded_output(handle: Any) -> tuple[str, dict]:
    handle.flush()
    handle.seek(0)
    digest = hashlib.sha256()
    excerpt = b""
    size = 0
    for chunk in iter(lambda: handle.read(65536), b""):
        digest.update(chunk)
        size += len(chunk)
        if len(excerpt) < DIAGNOSTIC_LIMIT:
            excerpt += chunk[:DIAGNOSTIC_LIMIT - len(excerpt)]
    if size > DIAGNOSTIC_LIMIT:
        handle.seek(-DIAGNOSTIC_LIMIT // 2, os.SEEK_END)
        excerpt = excerpt[:DIAGNOSTIC_LIMIT // 2] + b"\n[diagnostic capture gap]\n" + handle.read()
    return excerpt.decode("utf-8", errors="replace"), {
        "bytes": size, "sha256": digest.hexdigest(), "scan_truncated": size > DIAGNOSTIC_LIMIT,
    }


def diagnostic_labels(output: str) -> list[str]:
    return [label for label, pattern in INFRASTRUCTURE.items() if re.search(pattern, output, re.IGNORECASE)]


def execute_command(argv: list[str], cwd: Path, timeout: float, *,
                    expected_failure: dict | None = None) -> dict:
    # Anonymous host temp files keep raw streams outside the checkout.
    with tempfile.TemporaryFile(dir=cwd.parent) as stdout, tempfile.TemporaryFile(dir=cwd.parent) as stderr:
        result = run_process(argv, cwd, timeout, stdout=stdout, stderr=stderr)
        out, out_info = bounded_output(stdout)
        err, err_info = bounded_output(stderr)
    combined = out + "\n" + err
    labels = diagnostic_labels(combined)
    code = result["exit_code"]
    reason = result["failure_reason"]
    if reason is None and code != 0:
        if code in (124, 137):
            reason = "timeout" if code == 124 else "environment_error"
        elif labels or code in (126, 127) or (code is not None and code < 0):
            reason = "environment_error"
        elif (
            expected_failure
            and code == expected_failure.get("exit_code", 1)
            and not (out_info["scan_truncated"] or err_info["scan_truncated"])
            and re.search(expected_failure["regex"], combined, re.MULTILINE)
        ):
            reason = "behavioral_assertion"
        else:
            reason = "unknown"
    result.update({
        "failure_reason": reason, "stdout": out_info, "stderr": err_info,
        # Only fixed diagnostic labels leave the private subprocess streams.
        "diagnostic_excerpt": ", ".join(labels) if labels else None,
    })
    return result


def resolve_cli_version(executable: str, cwd: Path, timeout: float) -> str | None:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = run_process([executable, "--version"], cwd, timeout, stdout=stdout, stderr=stderr)
        out, _ = bounded_output(stdout)
    match = re.fullmatch(r"codex(?:-cli)? [a-zA-Z0-9._+-]{1,64}", out.strip())
    return match[0] if result["exit_code"] == 0 and match else None


def configuration_fingerprint(executable: str, settings: dict) -> dict:
    """Hash selected settings and global context; never persist configuration values."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    digest = hashlib.sha256()
    digest.update(repr(sorted(settings.items())).encode())
    try:
        binary = Path(executable).stat()
        digest.update(repr((binary.st_dev, binary.st_ino, binary.st_size, binary.st_mtime_ns)).encode())
    except OSError:
        digest.update(b"<missing-executable>")
    paths = [codex_home / "config.toml", codex_home / "AGENTS.md", codex_home / "AGENTS.override.md"]
    for root in (codex_home / "skills", codex_home / "plugins", Path.home() / ".agents" / "skills",
                 codex_home / "rules"):
        paths.extend(sorted(
            path for path in root.rglob("*") if path.is_file() and (
                path.name in {"SKILL.md", "plugin.json", ".mcp.json"} or path.suffix == ".rules")
        ))
    for path in paths:
        try:
            if path.is_file():
                digest.update(str(path).encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"<unreadable>")
    for name in ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "PYTEST_ADDOPTS", "VIRTUAL_ENV"):
        digest.update(repr((name, os.environ.get(name))).encode())
    return {"sha256": digest.hexdigest(), "scope": "selected settings and global context files",
            "python": platform.python_version(), "platform": platform.system(), "codex_executable": Path(executable).name}
