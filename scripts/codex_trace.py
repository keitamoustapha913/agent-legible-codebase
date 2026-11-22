#!/usr/bin/env python3
"""Normalize the *observed* parts of `codex exec --json` JSONL.

Standard-library only. No claims of exact source/model token accounting: model
usage comes from Codex `turn.completed`; source navigation is a rough proxy from
captured command outputs, which can be incomplete in some Codex versions.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Not intended as a full source-file detector; intentionally conservative.
_SOURCE_SUFFIXES = (
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "kt", "kts", "go",
    "rs", "c", "cc", "cpp", "h", "hpp", "cs", "rb", "php", "swift", "scala",
    "ex", "exs", "erl", "hs", "lua", "sql", "sh", "bash", "zsh", "vue", "svelte",
    "css", "scss", "html", "xml", "json", "toml", "yaml", "yml", "proto",
)
FILE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:\./)?[A-Za-z0-9_@./-]+\."
    + "(?:" + "|".join(_SOURCE_SUFFIXES) + r")(?=[\s:'\"(),;\]\}]|$)"
)
TOKEN_PROXY = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_output_units(text: str) -> int:
    """Comparable regex units, NOT a model tokenizer or billing count."""
    return len(TOKEN_PROXY.findall(text))


def _normalized_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def extract_paths(text: str) -> set[str]:
    result: set[str] = set()
    for m in FILE_PATTERN.finditer(text):
        candidate = _normalized_path(m.group(0))
        if not candidate or ".." in candidate.split("/"):
            continue
        result.add(candidate)
    return result


def command_kind(cmd: str) -> str:
    """Categorize obvious shell navigation; do not infer invisible reads."""
    c = cmd.strip()
    if re.search(r"(?:^|[\s;&|])(rg|ripgrep)(?:\s+--files\b|\s+-[a-zA-Z]*l\b)", c):
        return "list"
    if re.search(r"(?:^|[\s;&|])(?:rg|ripgrep|grep|ag|git\s+grep)(?=\s|$)", c):
        return "search"
    if re.search(r"(?:^|[\s;&|])(?:cat|sed|bat|head|tail|less|more|nl|awk)(?=\s|$)", c):
        return "read"
    if re.search(r"(?:^|[\s;&|])git\s+(?:show|diff|blame)(?=\s|$)", c):
        return "read"
    if re.search(r"(?:^|[\s;&|])(?:ls|find|fd|tree)(?=\s|$)", c):
        return "list"
    if re.search(r"(?:^|[\s;&|])(?:pytest|jest|vitest|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test)(?=\s|$)", c):
        return "test"
    return "other"


def command_label(command: str, kind: str) -> str:
    """Keep useful search intent but not entire potentially secret-bearing cmd."""
    if kind != "search":
        return kind
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    # Reduce repeated search detection without recording search terms verbatim.
    filtered = [p for p in parts if not p.startswith("-") and p not in {"rg", "grep", "git", "ag", "ripgrep"}]
    import hashlib
    return "search:" + hashlib.sha256(" ".join(filtered).encode()).hexdigest()[:12]


def _owner_matches(seen_paths: set[str], owner_paths: list[str]) -> bool:
    # Exact suffix match for source paths (repo prefix may be absolute).
    for raw_owner in owner_paths:
        owner = _normalized_path(raw_owner)
        if owner and any(path == owner or path.endswith("/" + owner) for path in seen_paths):
            return True
    return False


def summarize_jsonl(path: Path, owners: list[str] | None = None) -> dict[str, Any]:
    owners = owners or []
    completed_ids: set[str] = set()
    updated: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    opened: set[str] = set()
    encountered: set[str] = set()
    read_before_owner: set[str] = set()
    first_owner_kind: str | None = None
    command_count = 0
    search_count = 0
    total_units = 0
    owner_at: int | None = None
    owner_units: int | None = None
    owner_seen = False
    incomplete = False
    empty_successful_navigations = 0
    tool_unobserved = 0
    malformed_lines = 0
    turns = 0
    thread_error = False
    # Preserve null for any unavailable provider counters.
    usage: dict[str, int] = Counter()
    usage_seen = False

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                incomplete = True
                continue
            if not isinstance(entry, dict):
                incomplete = True
                continue
            event_type = entry.get("type")
            if event_type == "turn.completed":
                turns += 1
                u = entry.get("usage")
                if isinstance(u, dict):
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                        if isinstance(u.get(key), int) and not isinstance(u[key], bool) and u[key] >= 0:
                            usage[key] += u[key]
                    usage_seen = usage_seen or isinstance(u.get("input_tokens"), int)
            elif event_type in {"turn.failed", "error"}:
                thread_error = True
            elif event_type == "item.updated":
                i = entry.get("item") or {}
                if isinstance(i, dict) and i.get("type") == "command_execution" and i.get("id"):
                    output = i.get("aggregated_output")
                    if isinstance(output, str) and len(output) > len(updated.get(str(i["id"]), "")):
                        updated[str(i["id"])] = output
            elif event_type == "item.completed":
                item = entry.get("item")
                if not isinstance(item, dict):
                    continue
                typ = item.get("type")
                if typ == "mcp_tool_call":
                    tool_unobserved += 1
                    incomplete = True  # The tool may have supplied uncounted source.
                    continue
                if typ != "command_execution":
                    continue
                item_id = str(item.get("id", f"line-{line_number}"))
                if item_id in completed_ids:
                    continue
                completed_ids.add(item_id)
                command_count += 1
                cmd = item.get("command", "")
                cmd = cmd if isinstance(cmd, str) else ""
                kind = command_kind(cmd)
                if kind == "search":
                    search_count += 1
                output = item.get("aggregated_output")
                missing_output = not isinstance(output, str)
                if missing_output:
                    output = ""
                if not output and updated.get(item_id):
                    output = updated[item_id]
                if kind in {"search", "read", "list"} and not output and item.get("exit_code") == 0:
                    empty_successful_navigations += 1
                    # May be a legitimate empty result OR a Codex JSON output gap.
                    # Do not silently turn it into zero source exposure.
                    if kind in {"read", "search"}:
                        incomplete = True
                if missing_output and kind in {"read", "search"}:
                    incomplete = True
                output_units = estimate_output_units(output) if kind in {"search", "read"} else 0
                total_units += output_units
                from_output = extract_paths(output)
                from_command = extract_paths(cmd)
                all_seen = from_output | from_command
                encountered.update(all_seen)
                if kind == "read":
                    opened.update(from_command)
                    # git show / sed can identify paths only in output.
                if owners and not owner_seen and kind == "read":
                    # Supporting reads are not necessarily mistakes; label as non-owner,
                    # not 'wrong' or 'irrelevant' files.
                    read_before_owner.update(p for p in from_command if not _owner_matches({p}, owners))
                if owners and not owner_seen and _owner_matches(all_seen, owners):
                    owner_seen = True
                    owner_at = command_count
                    owner_units = total_units
                    first_owner_kind = kind
                events.append({
                    "step": command_count,
                    "kind": kind,
                    "search_fingerprint": command_label(cmd, kind) if kind == "search" else None,
                    "output_units_estimate": output_units,
                    "paths_mentioned": sorted(all_seen)[:100],
                    "exit_code": item.get("exit_code"),
                    "output_missing_or_empty": missing_output or (not output and item.get("exit_code") == 0),
                })
    if malformed_lines or turns == 0:
        thread_error = True
        incomplete = True
    search_frequencies = Counter(e["search_fingerprint"] for e in events if e["kind"] == "search")
    repeated = sum(max(0, n - 1) for n in search_frequencies.values())
    return {
        "source_tokens_to_owner": owner_units if not incomplete else None,
        "source_tokens_read": total_units if not incomplete else None,
        "total_input_tokens": usage.get("input_tokens") if usage_seen else None,
        "cached_input_tokens": usage.get("cached_input_tokens") if usage_seen else None,
        "output_tokens": usage.get("output_tokens") if usage_seen else None,
        "owner_steps": owner_at,
        "owner_first_mention_kind": first_owner_kind,
        "non_owner_reads_before_owner": sorted(read_before_owner) if owners else None,
        "non_owner_read_count_before_owner": len(read_before_owner) if owners else None,
        "search_steps": search_count,
        "files_opened": sorted(opened),
        "files_mentioned": sorted(encountered),
        "navigation_steps": command_count,
        "repeated_searches": repeated,
        "tool_calls_not_instrumented": tool_unobserved,
        "empty_successful_navigations": empty_successful_navigations,
        "trace_complete_for_source_proxy": not incomplete,
        "trace_malformed_lines": malformed_lines,
        "turns_completed": turns,
        "thread_failed": thread_error,
        "estimator": "regex_word_or_punctuation_v1 (rough proxy, not model tokenizer)",
        "owner_path_was_seen": owner_seen if owners else None,
        "events": events,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("jsonl", type=Path, help="Captured output of codex exec --json")
    p.add_argument("--owner", action="append", default=[], help="Expected owner path, not sent to Codex")
    p.add_argument("--output", type=Path, help="Write sanitized structured trace to JSON")
    opts = p.parse_args()
    try:
        summary = summarize_jsonl(opts.jsonl, opts.owner)
    except OSError as exc:
        print(f"Unable to parse trace: {exc}", file=sys.stderr)
        return 2
    output = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if opts.output:
        opts.output.parent.mkdir(parents=True, exist_ok=True)
        opts.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0 if not summary["thread_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
