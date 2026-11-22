"""Check Codex's local MCP client without starting a model turn."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from environment_probe import terminate_process_tree


def probe_navigation(argv: list[str], cwd: Path, artifacts: Path, label: str, timeout: int,
                     path: str, symbol: str, profile: str) -> dict:
    logs = {name: f"{label}.{name}" for name in ("rpc.jsonl", "stderr.txt")}
    result = {"status": "blocked", "reason": "app_server_startup_failed", "logs": logs}
    source = (cwd / path).read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(source, 1) if line.strip())
    expected = "".join(source[start - 1:start + 7])
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix=".navigation-client-", dir=artifacts) as home:
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL") if key in os.environ}
        env["CODEX_HOME"] = home
        with open(artifacts / logs["rpc.jsonl"], "x", encoding="utf-8", opener=private_opener) as transcript, open(
            artifacts / logs["stderr.txt"], "x", encoding="utf-8", opener=private_opener
        ) as stderr:
            try:
                with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=stderr, text=True, start_new_session=os.name == "posix") as process:
                    messages = queue.Queue()

                    def read_output():
                        for line in process.stdout:
                            messages.put(line)
                        messages.put(None)

                    reader = threading.Thread(target=read_output, daemon=True)
                    reader.start()

                    def rpc(method, params, request_id=None):
                        message = {"method": method, "params": params}
                        if request_id is not None:
                            message["id"] = request_id
                        payload = json.dumps(message)
                        print(json.dumps({"request": message}), file=transcript, flush=True)
                        process.stdin.write(payload + "\n")
                        process.stdin.flush()
                        if request_id is None:
                            return None
                        while time.monotonic() < deadline:
                            line = messages.get(timeout=max(0, deadline - time.monotonic()))
                            if line is None:
                                raise ValueError("App server closed")
                            reply = json.loads(line)
                            print(json.dumps({"received": reply}), file=transcript, flush=True)
                            if reply.get("id") == request_id:
                                return reply["result"]
                        raise queue.Empty

                    try:
                        rpc("initialize", {"clientInfo": {"name": "alc_readiness", "version": "1"},
                                           "capabilities": {"experimentalApi": True}}, 1)
                        rpc("initialized", {})
                        opened = rpc("thread/start", {"cwd": str(cwd), "approvalPolicy": "never", "ephemeral": True}, 2)
                        sandbox = opened.get("sandbox") or {}
                        if (sandbox.get("type") != "readOnly" or sandbox.get("networkAccess") is not False or
                                opened.get("approvalPolicy") != "never" or
                                (opened.get("activePermissionProfile") or {}).get("id") != profile):
                            result["reason"] = "app_server_read_only_profile_unconfirmed"
                            return result
                        thread_id = opened["thread"]["id"]
                        server = {"threadId": thread_id, "serverName": "alc_navigation"}
                        result["reason"] = "mcp_inventory_unavailable"
                        inventory = rpc("mcpServerStatus/list", server, 3)["data"]
                        if not any(item.get("name") == "alc_navigation" and
                                   item.get("runtimeStatus") == "connected" and not item.get("toolsError") and
                                   {"list_files", "search", "read"} <= set(item.get("tools", {}))
                                   for item in inventory):
                            return result
                        checks = (
                            ("list_files", {"contains": path}, lambda output: path in output.splitlines()),
                            ("search", {"query": symbol, "contains": path},
                             lambda output: any(line.startswith(path + ":") and symbol in line
                                                for line in output.splitlines())),
                            ("read", {"path": path, "start": start, "end": start + 7},
                             lambda output: output == expected),
                        )
                        for request_id, (tool, arguments, valid) in enumerate(checks, 4):
                            result["reason"] = f"mcp_{tool}_result_unusable"
                            reply = rpc("mcpServer/tool/call", {"threadId": thread_id, "server": "alc_navigation",
                                                              "tool": tool, "arguments": arguments}, request_id)
                            blocks = reply.get("content")
                            if reply.get("isError") or not isinstance(blocks, list) or not valid("".join(
                                block["text"] for block in blocks if block.get("type") == "text"
                            )):
                                return result
                        return {**result, "status": "ready", "reason": None, "confirmed_source_contents": True}
                    finally:
                        terminate_process_tree(process)
                        reader.join(timeout=1)
            except queue.Empty:
                return {**result, "timed_out": True}
            except (OSError, IndexError, KeyError, TypeError, ValueError):
                return result


def private_opener(path, flags):
    return os.open(path, flags, 0o600)
