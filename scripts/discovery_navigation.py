"""Read-only stdio MCP server for discovery trials. No shell or test tool."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from discovery_oracle import resolved_file

MAX_RESPONSE = 16000
MAX_FILE = 512 * 1024
TOOLS = [
    {"name": "list_files", "description": "List tracked paths, optionally using a literal path substring.",
     "inputSchema": {"type": "object", "properties": {"contains": {"type": ["string", "null"]}}, "additionalProperties": False}},
    {"name": "search", "description": "Find a literal snippet in tracked text files. Returns path, line number and match.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "contains": {"type": ["string", "null"]}},
                     "required": ["query"], "additionalProperties": False}},
    {"name": "read", "description": "Read a tracked text file, optionally limiting to lines start through end.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": ["integer", "null"]},
                                                     "end": {"type": ["integer", "null"]}},
                     "required": ["path"], "additionalProperties": False}},
]


class Navigation:
    def __init__(self, root: Path, paths: list[str]):
        self.root = root
        self.paths = sorted(paths)
        self.tracked = set(paths)

    def read_lines(self, path: str) -> list[str]:
        if path not in self.tracked:
            raise ValueError("File is not tracked in this checkout")
        file = resolved_file(self.root, path)
        if file.stat().st_size > MAX_FILE:
            raise ValueError("File exceeds the navigation tool limit")
        content = file.read_text(encoding="utf-8")
        if "\x00" in content:
            raise ValueError("File is not text")
        return content.splitlines(keepends=True)

    def call(self, name: str, arguments: dict) -> str:
        if name == "read":
            start = arguments.get("start")
            start = 1 if start is None else start
            end = arguments.get("end")
            if end is None and type(start) is int:
                end = start + 199
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= start + 199:
                raise ValueError("Specify a valid range of up to 200 lines")
            lines = self.read_lines(arguments.get("path"))
            return "".join(lines[start - 1:end])
        contains = arguments.get("contains")
        contains = "" if contains is None else contains
        if not isinstance(contains, str):
            raise ValueError("Expected a literal path filter")
        paths = (p for p in self.paths if contains.casefold() in p.casefold())
        if name == "list_files":
            return "\n".join(paths)
        if name != "search":
            raise ValueError("Unknown navigation tool")
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or "\n" in query:
            raise ValueError("Expected a short, single-line literal")
        found = []
        deadline = time.monotonic() + 8
        for path in paths:
            if time.monotonic() >= deadline:
                return "\n".join(found) + "\n[Output truncated by navigation tool]"
            try:
                for number, line in enumerate(self.read_lines(path), 1):
                    if query.casefold() in line.casefold():
                        found.append(f"{path}:{number}:{line.rstrip()}")
                        if len(found) >= 100 or sum(map(len, found)) >= MAX_RESPONSE:
                            return "\n".join(found) + "\n[Output truncated by navigation tool]"
            except (OSError, ValueError):
                continue
        return "\n".join(found)


def dispatch(request: dict, navigation: Navigation) -> dict | None:
    if not isinstance(request, dict) or "id" not in request:
        return None
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "alc_navigation", "version": "4"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            params = request["params"]
            output = navigation.call(params["name"], params.get("arguments") or {})
            if len(output) > MAX_RESPONSE:
                output = output[:MAX_RESPONSE] + "\n[Output truncated by navigation tool]"
            result = {"content": [{"type": "text", "text": output}], "isError": False}
        except UnicodeError:
            result = {"content": [{"type": "text", "text": "File is not UTF-8 text."}], "isError": True}
        except ValueError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except (AttributeError, KeyError, TypeError, OSError):
            result = {"content": [{"type": "text", "text": "Navigation request rejected or file unavailable."}],
                      "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def serve(navigation: Navigation, journal=None) -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = dispatch(request, navigation)
        except (ValueError, TypeError):
            continue
        if journal is not None:
            print(json.dumps({"request": request, "response": response}), file=journal, flush=True)
        if response is not None:
            print(json.dumps(response), flush=True)


def main() -> int:
    root = Path(sys.argv[1]).resolve(strict=True)
    listing = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], timeout=15)
    navigation = Navigation(root, listing.decode("utf-8", errors="replace").split("\x00")[:-1])
    if len(sys.argv) > 2:
        # The controller creates this private file outside the worktree.
        with Path(sys.argv[2]).open("a", encoding="utf-8") as journal:
            serve(navigation, journal)
    else:
        serve(navigation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
