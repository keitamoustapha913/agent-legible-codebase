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
import hashlib
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from environment_probe import diagnostic_labels
from trace_attribution import (
    NAVIGATION, git_metadata_only, inspect_command, mentions, path_category, relative_path, scoped_search,
)

TOKEN_PROXY = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_output_units(text: str) -> int:
    """Comparable regex units, NOT a model tokenizer or billing count."""
    return len(TOKEN_PROXY.findall(text))


USAGE = ("input_tokens", "cached_input_tokens", "output_tokens")
LEGACY_KIND = {
    "source_search": "search", "source_read": "read", "file_listing": "list",
    "test_execution": "test", "git_inspection": "read",
}


def command_kind(cmd: str) -> str:
    """Compatibility helper for callers of the v3 command-label API."""
    category, _, _ = inspect_command(cmd, Path.cwd(), Path.cwd())
    return LEGACY_KIND.get(category, "other")


def usage_summary(turns: list[dict], incomplete: bool) -> dict:
    counts = {}
    for key in USAGE:
        values = [turn.get(key) for turn in turns]
        counts[key] = (sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None)
    total, cached, output = (counts[key] for key in USAGE)
    uncached = total - cached if total is not None and cached is not None and all(
        turn["cached_input_tokens"] <= turn["input_tokens"] for turn in turns) else None
    status = "missing"
    if any(turn.get(key) is not None for turn in turns for key in USAGE):
        status = "partial" if incomplete or uncached is None or output is None else "complete_for_reported_model_usage"
    return {"total_input_tokens": total, "cached_input_tokens": cached, "output_tokens": output,
            "uncached_input_tokens": uncached, "model_usage_coverage": status}


def command_evidence(item: dict, item_id: str, step: int, line: int, fallback: str,
                     root: Path | None, default_cwd: Path | None) -> dict:
    command = item.get("command")
    command = command if isinstance(command, str) else ""
    cwd = default_cwd
    if isinstance(item.get("cwd"), str):
        reported_cwd = Path(item["cwd"])
        cwd = reported_cwd if reported_cwd.is_absolute() else root / reported_cwd if root else None
    category, operations, targets = inspect_command(command, root, cwd)
    final = item.get("aggregated_output")
    output = final if isinstance(final, str) and final else fallback
    status = "available"
    if not output:
        status = "missing"
    elif not final or len(fallback) > len(final):
        status = "partial"
    elif (item.get("output_truncated") or item.get("truncated") or
          re.search(r"(?im)^\s*(?:(?:warning:\s*)?\[?(?:output (?:was )?truncated|truncated output)\b|(?:…|\.\.\.)\s*\d+ tokens truncated)", output)):
        status = "truncated"
    exit_code = item.get("exit_code")
    successful = type(exit_code) is int and exit_code == 0 and item.get("status") in (None, "completed")
    usable = status == "available" and successful
    size = len(output.encode("utf-8", errors="replace")) if output else None
    attribution = "unknown"
    source_bytes = None
    source_units = None
    read_paths = []
    if category == "mixed_unattributed":
        attribution = "mixed"
    elif usable and (category == "source_read" or category == "source_search" and scoped_search(output, root, cwd)):
        attribution = "source"
        source_bytes, source_units = size, estimate_output_units(output)
        if category == "source_read":
            read_paths = [relative_path(target, root, cwd) for target in targets]
    elif usable and (category in {"agent_instructions", "project_docs", "file_listing", "test_execution",
                                  "environment_diagnostics"} or category == "git_inspection" and git_metadata_only(command)):
        attribution = "non_source"
        source_bytes = source_units = 0
    check_like = bool({"test_execution", "environment_diagnostics"} & set(operations))
    signature = hashlib.sha256(command.encode(errors="replace")).hexdigest()[:12]
    seen = mentions(command, root, cwd)
    if category not in {"agent_instructions", "test_execution", "environment_diagnostics"}:
        seen.update(mentions(output, root, cwd))
    return {
        "step": step, "event_id": item_id, "event_type": "command_execution", "event_line": line,
        "category": category, "kind": LEGACY_KIND.get(category, "other"), "operations_detected": operations,
        "output_bytes_observed": size, "output_status": status, "attribution_status": attribution,
        "source_output_bytes": source_bytes, "source_output_proxy_units": source_units,
        "source_paths_read": read_paths, "paths_mentioned": sorted(seen)[:100],
        "exit_code": exit_code, "execution_status": item.get("status"), "command_fingerprint": signature,
        "environment_signals": diagnostic_labels(output) if check_like and exit_code != 0 else [],
        "test_fingerprint": signature if "test_execution" in operations else None,
        "output_missing_or_empty": status in {"missing", "partial"},
        "search_fingerprint": signature if "source_search" in operations else None,
    }


DISCOVERY_TOOLS = {"list_files": "file_listing", "search": "source_search", "read": "source_read"}


def navigation_evidence(item: dict, item_id: str, step: int, line: int, root: Path | None) -> dict:
    tool = item["tool"]
    category = DISCOVERY_TOOLS[tool]
    args = item.get("arguments")
    args = args if isinstance(args, dict) else {}
    raw_path = args.get("path")
    path = relative_path(raw_path, root, root) if isinstance(raw_path, str) else None
    if tool == "read":
        category = path_category(raw_path, root, root) if path else "other"
    result = item.get("result")
    blocks = result.get("content") if isinstance(result, dict) else None
    text = blocks[0].get("text") if isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict) else None
    status = "missing"
    if isinstance(text, str):
        status = "truncated" if re.search(r"(?i)truncated by navigation tool|tokens truncated", text) else "available"
    usable = (
        status == "available" and item.get("status") == "completed" and not item.get("error")
        and not result.get("isError") and text != "Navigation request rejected or unsupported."
    )
    size = len(text.encode("utf-8", errors="replace")) if isinstance(text, str) else None
    attribution = "unknown"
    source_bytes = source_units = None
    paths = mentions(text or "", root, root)
    if path:
        paths.add(path)
    source_paths = []
    if usable and (category == "source_read" or category == "source_search" and scoped_search(text, root, root)):
        attribution = "source"
        source_bytes, source_units = size, estimate_output_units(text)
        if category == "source_read" and text:
            source_paths.append(path)
    elif usable and (category in {"file_listing", "agent_instructions", "project_docs"} or
                     category == "source_search" and text == ""):
        attribution = "non_source"
        source_bytes = source_units = 0
    return {
        "step": step, "event_id": item_id, "event_type": "mcp_tool_call", "event_line": line,
        "category": category, "kind": LEGACY_KIND.get(category, "other"),
        "operations_detected": [DISCOVERY_TOOLS[tool]], "output_bytes_observed": size, "output_status": status,
        "attribution_status": attribution, "source_output_bytes": source_bytes,
        "source_output_proxy_units": source_units, "source_paths_read": source_paths,
        "paths_mentioned": sorted(paths)[:100], "exit_code": None, "execution_status": item.get("status"),
        "output_missing_or_empty": status == "missing",
        "search_fingerprint": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]
        if tool == "search" else None,
    }


def summarize_jsonl(path: Path, owners: list[str] | None = None, *,
                    repo_root: Path | None = None, relative_cwd_known: bool = True,
                    capture_answer: bool = False, discovery_tools: bool = False) -> dict[str, Any]:
    root = repo_root.resolve() if repo_root is not None else None
    cwd = root if relative_cwd_known else None
    expected = {p for raw in owners or [] if (p := relative_path(raw, root, root)) is not None}
    events: list[dict] = []
    pending: set[str] = set()
    completed: set[str] = set()
    updates: dict[str, str] = {}
    turns: list[dict] = []
    malformed = unsupported = file_changes = 0
    turn_failed = False
    answer = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line, text in enumerate(handle, 1):
            if not text.strip():
                continue
            try:
                entry = json.loads(text)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(entry, dict):
                malformed += 1
                continue
            event_type = entry.get("type")
            if event_type == "turn.completed":
                usage = entry.get("usage")
                turns.append(usage if isinstance(usage, dict) else {})
            elif event_type in {"turn.failed", "error"}:
                turn_failed = True
            item = entry.get("item")
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            item_id = str(item.get("id", f"line-{line}"))
            if kind in {"command_execution", "mcp_tool_call"} and event_type in {"item.started", "item.updated"}:
                if item_id not in completed:
                    pending.add(item_id)
                    output = item.get("aggregated_output")
                    if isinstance(output, str) and len(output) > len(updates.get(item_id, "")):
                        updates[item_id] = output
            if event_type != "item.completed" or item_id in completed:
                continue
            completed.add(item_id)
            pending.discard(item_id)
            if kind == "command_execution":
                events.append(command_evidence(item, item_id, len(events) + 1, line,
                                               updates.pop(item_id, ""), root, cwd))
            elif (discovery_tools and kind == "mcp_tool_call" and item.get("server") == "alc_navigation"
                  and item.get("tool") in DISCOVERY_TOOLS):
                events.append(navigation_evidence(item, item_id, len(events) + 1, line, root))
            elif kind == "file_change":
                file_changes += 1
            elif kind == "agent_message" and capture_answer:
                answer = item.get("text")
            elif kind not in {"agent_message", "reasoning", "file_change", "todo_list", "plan_update"}:
                unsupported += 1
                events.append({"step": None, "event_id": item_id, "event_type": kind, "event_line": line,
                               "category": "other", "operations_detected": [], "attribution_status": "unknown"})

    counters = Counter(e["category"] for e in events)
    search_fingerprints = Counter(e["search_fingerprint"] for e in events if e.get("search_fingerprint"))
    test_fingerprints = Counter(e["test_fingerprint"] for e in events if e.get("test_fingerprint"))
    seen: set[str] = set()
    opened: set[str] = set()
    prior_reads: set[str] = set()
    first_owner = None
    for event in events:
        paths = set(event.get("paths_mentioned", []))
        reads = set(event.get("source_paths_read", []))
        seen.update(paths)
        opened.update(reads)
        if first_owner is None:
            if expected & paths:
                first_owner = event
            else:
                prior_reads.update(reads - expected)
    failed = not turns or turn_failed or bool(malformed)
    commands = [e for e in events if e["step"] is not None]
    unknown = sum(e["attribution_status"] in {"mixed", "unknown"} for e in commands)
    source_events = [e for e in commands if e["attribution_status"] == "source"]
    incomplete = failed or pending or unsupported or unknown or root is None
    coverage = ("partial" if source_events else "insufficient") if incomplete else "complete"
    observed_bytes = sum(e["source_output_bytes"] for e in source_events) if source_events or not incomplete else None
    observed_units = sum(e["source_output_proxy_units"] for e in source_events) if source_events or not incomplete else None
    result = {
        "schema_version": 4, "source_tokens_to_owner": None, "source_tokens_read": None,
        "trace_complete_for_source_proxy": coverage == "complete",
        "source_attribution_coverage": coverage,
        "source_output_bytes": observed_bytes if coverage == "complete" else None,
        "source_output_proxy_units": observed_units if coverage == "complete" else None,
        "observed_source_output_bytes": observed_bytes, "observed_source_output_proxy_units": observed_units,
        "mixed_output_bytes_observed": sum(e.get("output_bytes_observed") or 0 for e in commands
                                           if e["attribution_status"] == "mixed"),
        "agent_instruction_output_bytes": sum(e.get("output_bytes_observed") or 0 for e in commands
                                                if e["category"] == "agent_instructions"),
        "category_counts": dict(counters), "shell_commands": sum(e["event_type"] == "command_execution" for e in commands),
        "file_change_events": file_changes, "source_read_steps": counters["source_read"],
        "confirmed_source_reads": sum(bool(e["source_paths_read"]) for e in source_events),
        "navigation_steps": sum(e["category"] in NAVIGATION for e in commands),
        "search_steps": sum(search_fingerprints.values()),
        "repeated_searches": sum(n - 1 for n in search_fingerprints.values()),
        "repeated_test_commands": sum(n - 1 for n in test_fingerprints.values()),
        "owner_steps": first_owner["step"] if first_owner else None,
        "owner_first_mention_kind": first_owner["kind"] if first_owner else None,
        "owner_first_mention_event_id": first_owner["event_id"] if first_owner else None,
        "owner_path_was_seen": bool(first_owner) if owners else None,
        "non_owner_reads_before_owner": sorted(prior_reads) if owners else None,
        "non_owner_read_count_before_owner": len(prior_reads) if owners else None,
        "files_opened": sorted(opened), "files_mentioned": sorted(seen),
        "tool_calls_not_instrumented": unsupported, "unfinished_commands": len(pending),
        "empty_successful_navigations": sum(e["exit_code"] == 0 and e["output_missing_or_empty"] and
                                            e["category"] in NAVIGATION for e in commands),
        "trace_malformed_lines": malformed, "turns_completed": len(turns), "thread_failed": failed,
        "estimator": "regex_word_or_punctuation_v1 (rough proxy, never exact model-source tokens)",
        "events": events,
    }
    result.update(usage_summary(turns, failed))
    if capture_answer:
        result["_discovery_answer"] = answer
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("jsonl", type=Path, help="Captured output of codex exec --json")
    p.add_argument("--owner", action="append", default=[], help="Expected owner path, not sent to Codex")
    p.add_argument("--repo-root", type=Path, help="Repository root in the captured trace")
    p.add_argument("--unknown-cwd", action="store_true", help="Attribute relative paths only with recorded command cwd")
    p.add_argument("--output", type=Path, help="Write sanitized structured trace to JSON")
    opts = p.parse_args()
    try:
        summary = summarize_jsonl(opts.jsonl, opts.owner, repo_root=opts.repo_root,
                                  relative_cwd_known=not opts.unknown_cwd)
    except OSError as exc:
        print(f"Unable to parse trace: {exc}", file=sys.stderr)
        return 2
    output = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if opts.output:
        opts.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(opts.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(output)
    else:
        print(output, end="")
    return 0 if not summary["thread_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
