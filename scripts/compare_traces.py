#!/usr/bin/env python3
"""Compare user-recorded cold-start agent task traces.

Usage: python scripts/compare_traces.py before.json after.json

This tool reads existing measurements. It does not collect telemetry or
claim that a change in source tokens alone establishes total token savings.
See references/benchmark-protocol.md for instructions.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from trace_metrics import numeric_metric


class TraceError(ValueError):
    """The supplied trace set cannot be compared reliably."""


NUMERIC_FIELDS = (
    ("source_tokens_to_owner", "Recorded source-context units until owner"),
    ("source_tokens_read", "Recorded source-context units through task"),
    ("source_output_bytes", "V4 attributable source bytes"),
    ("source_output_proxy_units", "V4 attributable source regex units"),
    ("cached_input_tokens", "Cached model input tokens"),
    ("uncached_input_tokens", "Uncached model input tokens"),
    ("output_tokens", "Model output tokens"),
    ("total_input_tokens", "Total model input tokens (if logged)"),
    ("owner_steps", "Steps to correct owner"),
    ("search_steps", "Search steps"),
    ("files_opened", "Distinct source files opened"),
    ("dependency_hops", "Dependency hops"),
    ("changed_files", "Distinct source files changed"),
)


def read_trace(path: str) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        content = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TraceError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(content, dict) or not isinstance(content.get("runs"), list):
        raise TraceError(f"{path}: expected object with a 'runs' array")

    runs: dict[tuple[str, str], dict[str, Any]] = {}
    for i, run in enumerate(content["runs"]):
        if not isinstance(run, dict):
            raise TraceError(f"{path}: runs[{i}] must be an object")
        task = run.get("task_id")
        trial = run.get("trial_id", "1")
        if not isinstance(task, str) or not task.strip():
            raise TraceError(f"{path}: runs[{i}] needs nonempty string task_id")
        if not isinstance(trial, str) or not trial.strip():
            raise TraceError(f"{path}: {task}: trial_id must be a nonempty string")
        key = (task, trial)
        if key in runs:
            raise TraceError(f"{path}: duplicate task/trial {task!r}/{trial!r}; give repeated runs unique trial_id values")
        for field in ("correct", "verified"):
            if not isinstance(run.get(field), bool):
                raise TraceError(f"{path}: {task}/{trial}: {field} must be boolean")
        run = {**run, "schema_version": content.get("schema_version", run.get("schema_version", 3))}
        for field, _ in NUMERIC_FIELDS:
            if field in ("files_opened", "changed_files"):
                values = run.get(field)
                if values is not None and (not isinstance(values, list) or any(not isinstance(v, str) for v in values)):
                    raise TraceError(f"{path}: {task}/{trial}: {field} must be an array of strings")
            elif run.get(field) is not None and (type(run[field]) is not int or run[field] < 0):
                raise TraceError(f"{path}: {task}/{trial}: {field} must be a nonnegative integer")
        if (run.get("source_tokens_to_owner") is not None and run.get("source_tokens_read") is not None
                and run["source_tokens_to_owner"] > run["source_tokens_read"]):
            raise TraceError(f"{path}: {task}/{trial}: source_tokens_to_owner exceeds source_tokens_read")
        runs[key] = run
    if not runs:
        raise TraceError(f"{path}: no runs supplied")
    return runs


def metric(run: dict[str, Any], field: str) -> int | None:
    if field in ("files_opened", "changed_files"):
        value = run.get(field)
        return len(set(value)) if value is not None else None
    return numeric_metric(run, field, schema_version=run.get("schema_version", 3))


def show_delta(before: int | float | None, after: int | float | None) -> str:
    if before is None or after is None:
        return "unrecorded"
    before_fmt = f"{before:g}"
    after_fmt = f"{after:g}"
    if before == 0:
        return f"{before_fmt} → {after_fmt} (no percentage baseline)"
    percent = (after - before) / before * 100
    return f"{before_fmt} → {after_fmt} ({percent:+.1f}%)"


def main(args: list[str]) -> int:
    if len(args) != 2:
        print("Usage: python scripts/compare_traces.py before.json after.json", file=sys.stderr)
        return 2
    try:
        old = read_trace(args[0])
        new = read_trace(args[1])
        if old.keys() != new.keys():
            missing_after = sorted(old.keys() - new.keys())
            missing_before = sorted(new.keys() - old.keys())
            raise TraceError(f"Task/trial IDs differ; only in before: {missing_after}; only in after: {missing_before}")
    except TraceError as exc:
        print(f"Trace error: {exc}", file=sys.stderr)
        return 2

    by_task: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for task, trial in sorted(old):
        by_task[task].append((trial, old[(task, trial)], new[(task, trial)]))

    if any(r["schema_version"] != 4 for r in (*old.values(), *new.values())):
        print("Historical source-context units use their original definitions and cannot establish v4 source coverage.")
    else:
        print("For experiment validity and environment gates, use report_codex.py with results.json.")

    comparable_count = 0
    regressions = 0
    for task, trials in sorted(by_task.items()):
        successful: list[tuple[dict[str, Any], dict[str, Any]]] = []
        print(f"\n{task} ({len(trials)} paired trial(s))")
        for trial_id, first, last in trials:
            before_ok = first["correct"] and first["verified"] and first.get("environment", "ready") == "ready"
            after_ok = last["correct"] and last["verified"] and last.get("environment", "ready") == "ready"
            status = "PASS" if before_ok and after_ok else "NOT COMPARABLE"
            print(f"  Trial {trial_id}: before={'PASS' if before_ok else 'FAIL'}, after={'PASS' if after_ok else 'FAIL'} — {status}")
            if before_ok and not after_ok:
                regressions += 1
            if before_ok and after_ok:
                successful.append((first, last))
                comparable_count += 1

        if len(successful) < len(trials):
            print("  Some trials did not pass verification on both sides; no blanket efficiency claim is justified.")
        if not successful:
            continue
        for field, label in NUMERIC_FIELDS:
            pairs = [(metric(before, field), metric(after, field)) for before, after in successful]
            measured = [(a, b) for a, b in pairs if a is not None and b is not None]
            if not measured:
                print(f"  {label}: unrecorded")
                continue
            before_median = statistics.median(a for a, _ in measured)
            after_median = statistics.median(b for _, b in measured)
            print(f"  {label}: {show_delta(before_median, after_median)} [median; n={len(measured)}]")
        if len(successful) < 2:
            print("  One successful pair: result is illustrative, not a robust estimate.")

    total = len(old)
    print(f"\nCorrect+verified pairs: {comparable_count}/{total}; after-version correctness regressions: {regressions}")
    print("Recorded trace comparisons only; not causal proof or an estimate of billing token reduction.")
    return 0 if comparable_count == total and regressions == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
