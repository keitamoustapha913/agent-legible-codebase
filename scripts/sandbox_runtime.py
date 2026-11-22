#!/usr/bin/env python3
"""Probe local Unix socket I/O and asyncio thread wakeups without a model."""
from __future__ import annotations

import argparse
import asyncio
import errno
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

PROBE_TIMEOUT = 4
NETWORK_DISABLED = "sandbox_workspace_write.network_access=false"


def emit(probe: str, status: str, **details: str | int | None) -> None:
    print(json.dumps({"probe": probe, "status": status, **details}), flush=True)


def probe() -> int:
    emit("python", "started", version=sys.version.split()[0])
    socket_ok = False
    try:
        writer, reader = socket.socketpair()
        with writer, reader:
            writer.settimeout(1)
            reader.settimeout(1)
            socket_ok = writer.send(b"x") == 1 and reader.recv(1) == b"x"
    except OSError as exc:
        emit("socketpair", "error", errno=exc.errno)
    else:
        emit("socketpair", "ok" if socket_ok else "error")

    def worker() -> None:
        # Give the event loop time to enter its selector before completing.
        time.sleep(0.2)
        emit("to_thread", "worker_completed")

    async def run_worker() -> None:
        await asyncio.to_thread(worker)
        emit("to_thread", "completed")

    # No in-loop watchdog: a timer could wake the blocked selector and mask the fault.
    asyncio.run(run_worker())
    emit("asyncio", "closed")
    return 0 if socket_ok else 1


def run_probe(argv: list[str], cwd: Path, timeout: float) -> dict:
    from environment_probe import bounded_output, diagnostic_labels, run_process

    with tempfile.TemporaryFile(dir=cwd) as stdout, tempfile.TemporaryFile(dir=cwd) as stderr:
        result = run_process(argv, cwd, timeout, stdout=stdout, stderr=stderr)
        out, out_info = bounded_output(stdout)
        err, err_info = bounded_output(stderr)
    events = {}
    worker_completed = False
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("probe") in ("python", "socketpair", "to_thread", "asyncio"):
            events[event["probe"]] = event
            worker_completed |= event.get("probe") == "to_thread" and event.get("status") == "worker_completed"
    version = events.get("python", {}).get("version")
    if not isinstance(version, str) or len(version) > 32 or not version.isascii() or not version.replace(".", "").isalnum():
        version = None
    socket_errno = events.get("socketpair", {}).get("errno")
    return {
        **result, "python_version": version, "stdout": out_info, "stderr": err_info,
        "diagnostics": diagnostic_labels(err), "socketpair_ok": events.get("socketpair", {}).get("status") == "ok",
        "socket_errno": socket_errno if type(socket_errno) is int else None,
        "worker_completed": worker_completed, "to_thread_completed": events.get("to_thread", {}).get("status") == "completed",
        "asyncio_closed": events.get("asyncio", {}).get("status") == "closed",
    }


def probe_passed(result: dict) -> bool:
    return result["exit_code"] == 0 and bool(result["python_version"]) and all(
        result[key] for key in ("socketpair_ok", "worker_completed", "to_thread_completed", "asyncio_closed")
    )


def check_runtime(codex: str, python: str, cwd: Path, *, timeout: float = PROBE_TIMEOUT) -> dict:
    command = [python, "-I", "-S", str(Path(__file__).resolve()), "--probe-child"]
    host = run_probe(command, cwd, timeout)
    sandbox = None
    if not probe_passed(host):
        reason = "host_probe_failed"
    else:
        sandbox_command = [
            codex, "sandbox", "--include-managed-config", "-P", ":workspace", "-c", NETWORK_DISABLED,
            "-C", str(cwd), "--", *command,
        ]
        sandbox = run_probe(sandbox_command, cwd, timeout)
        if probe_passed(sandbox) and sandbox["python_version"] == host["python_version"]:
            reason = None
        elif sandbox["socket_errno"] in (errno.EPERM, errno.EACCES) or (
            sandbox["timed_out"] and sandbox["worker_completed"] and not sandbox["asyncio_closed"]
        ):
            reason = "sandbox_python_local_ipc"
        else:
            reason = "sandbox_probe_failed"
    return {
        "status": "ready" if reason is None else "environment_blocked",
        "reason": reason, "timeout_seconds": timeout, "sandbox_mode": "workspace-write",
        "network_access": False, "host": host, "sandbox": sandbox,
    }


def main() -> int:
    if sys.argv[1:] == ["--probe-child"]:
        return probe()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--python", default=sys.executable, help="Python executable used by the test runtime")
    args = parser.parse_args()
    codex = shutil.which(args.codex)
    python = shutil.which(args.python)
    if not codex or not python:
        parser.error("Both Codex CLI and the selected Python executable must exist")
    with tempfile.TemporaryDirectory(prefix="alc-sandbox-runtime-") as temp:
        result = check_runtime(str(Path(codex).absolute()), str(Path(python).absolute()), Path(temp).resolve())
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
