#!/usr/bin/env python3
"""Create an evidence-linked (not causal) before/after Codex navigation report."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from codex_trace import summarize_jsonl

PROVIDER = (
    ("total_input_tokens", "Total input"), ("cached_input_tokens", "Cached input"),
    ("uncached_input_tokens", "Uncached input"), ("output_tokens", "Output"),
)
NAVIGATION = (
    ("navigation_steps", "Unmixed navigation commands"), ("search_steps", "Commands with detected searches"),
    ("repeated_searches", "Repeated search fingerprints"), ("confirmed_source_reads", "Confirmed file reads"),
    ("owner_steps", "Shell command of first owner mention"), ("source_output_bytes", "Attributable source bytes"),
    ("source_output_proxy_units", "Attributable source regex units"),
)


def cell(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(value).replace("|", "&#124;").replace("\r", " ").replace("\n", " ").replace("<", "&lt;")


def row(*values: Any) -> str:
    return "| " + " | ".join(map(cell, values)) + " |"


def pass_run(run: dict, mode: str) -> bool:
    if "agent_completion" not in run:
        return run.get("correct") is True and run.get("verified") is True
    return (
        run["agent_completion"] == "completed" and run.get("behavior") == "correct"
        and run.get("regression") == ("not_evaluated" if mode == "discovery" else "passed")
        and run.get("environment") == "ready"
    )


def pair_runs(runs: list[dict]) -> tuple[list[tuple[str, dict, dict]], int]:
    lookup: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("runs entries must be objects")
        task, trial, variant = run.get("task_id"), run.get("trial_id"), run.get("variant")
        if not isinstance(task, str) or not isinstance(trial, str) or variant not in {"before", "after"}:
            raise ValueError("run missing task_id, trial_id or valid variant")
        if variant in lookup[(task, trial)]:
            raise ValueError(f"duplicate result for {task}/{trial}/{variant}")
        lookup[(task, trial)][variant] = run
    pairs = [(task, sides["before"], sides["after"]) for (task, _), sides in sorted(lookup.items()) if len(sides) == 2]
    return pairs, len(lookup) - len(pairs)


def get_metrics(pairs: list[tuple[str, dict, dict]], field: str) -> list[tuple[int, int]]:
    return [(a[field], b[field]) for _, a, b in pairs
            if type(a.get(field)) is int and type(b.get(field)) is int
            and (not field.startswith("source_output") or (
                a.get("source_attribution_coverage") == b.get("source_attribution_coverage") == "complete"
            ))]


def paired_table(pairs: list[tuple[str, dict, dict]], tasks: list[str], fields: tuple) -> list[str]:
    lines = ["| Task | Metric | Before median | After median | Change | Measured pairs |",
             "|---|---|---:|---:|---:|---:|"]
    for task in [*tasks, None]:
        subset = [p for p in pairs if task is None or p[0] == task]
        for field, label in fields:
            points = get_metrics(subset, field)
            if points:
                before, after = [statistics.median(side) for side in zip(*points)]
                change = f"{(after / before - 1) * 100:+.1f}%" if before else "zero baseline"
                medians = [f"{n:,.1f}".removesuffix(".0") for n in (before, after)]
                lines.append(row(task or "All tasks", label, *medians, change, len(points)))
            else:
                lines.append(row(task or "All tasks", label, None, None, None, 0))
    return lines


def decision(meta: dict, runs: list[dict], pairs: list[tuple[str, dict, dict]],
             matched: list[tuple[str, dict, dict]], tasks: list[str], incomplete: int,
             legacy: bool, mode: str) -> tuple[str, str]:
    if (meta.get("preflight_status") in {"preflight_invalid", "interrupted"} or
        meta.get("sandbox_smoke_status") in {"environment_blocked", "environment_confounded"} or
        meta.get("experiment_status") in {"invalid", "environment_blocked", "environment_confounded"} or
        any(r.get("environment") in {"blocked", "confounded"} for r in runs)):
        return "INVALID", "Oracle, setup, or environment evidence blocks an efficiency decision."
    if any(pass_run(a, mode) and (
        b.get("behavior") == "incorrect" or b.get("regression") == "failed"
        or "agent_completion" not in b and (b.get("correct") is False or b.get("verified") is False)
    ) for _, a, b in pairs):
        return "REGRESSION", "A matched after run lost correctness or failed a regression check."
    if legacy or meta.get("offline_reanalysis"):
        return "INCONCLUSIVE", "Historical results lack independently validated v4 experimental controls."
    if (meta.get("preflight_status") != "ready" or not meta.get("model_requested") or
            not meta.get("codex_cli_version") or not (meta.get("configuration") or {}).get("sha256")):
        return "INCONCLUSIVE", "Verified preflight and pinned CLI and model conditions are required."
    if (incomplete or meta.get("experiment_status") != "completed"
            or len(runs) != meta.get("planned_run_count") or len(matched) != len(pairs)):
        return "INCONCLUSIVE", "Some planned pairs are missing or unsuccessful on a required check."
    if not tasks or any(sum(task == key for task, _, _ in matched) < 3 for key in tasks):
        return "INCONCLUSIVE", "At least three both-success pairs per task are required for a preliminary trend."
    seen_orders = set()
    for key in tasks:
        orders = [(a.get("execution_order"), b.get("execution_order")) for task, a, b in matched if task == key]
        valid_order = all(type(a) is int and type(b) is int and abs(a - b) == 1 for a, b in orders)
        if not valid_order or abs(sum(a < b for a, b in orders) * 2 - len(orders)) > 1:
            return "INCONCLUSIVE", "Recorded pair order does not establish counterbalancing."
        seen_orders.update(n for pair in orders for n in pair)
    ordered = sorted(runs, key=lambda r: r["execution_order"])
    actual = [{key: r[key] for key in ("task_id", "trial_id", "variant")} for r in ordered]
    if seen_orders != set(range(1, len(runs) + 1)) or actual != meta.get("planned_order"):
        return "INCONCLUSIVE", "The actual run sequence is incomplete."
    if any(r.get("model_usage_coverage") != "complete_for_reported_model_usage" or
           r.get("source_attribution_coverage") != "complete" or
           type(r.get("total_input_tokens")) is not int or type(r.get("uncached_input_tokens")) is not int
           for r in runs):
        return "INCONCLUSIVE", "Provider or source-attribution coverage is incomplete."
    improved = False
    for key in tasks:
        task_pairs = [pair for pair in matched if pair[0] == key]
        deltas = [b - a for a, b in get_metrics(task_pairs, "total_input_tokens")]
        uncached = [b - a for a, b in get_metrics(task_pairs, "uncached_input_tokens")]
        if statistics.median(deltas) > 0 or statistics.median(uncached) > 0:
            return "NO_DEMONSTRATED_GAIN", "At least one task used more provider input in its matched trials."
        improved |= statistics.median(deltas) < 0
    if improved:
        return "PROMISING", "Matched trials preserved correctness and used less provider input on at least one task without a task-level increase."
    return "NO_DEMONSTRATED_GAIN", "Matched trials did not show a lower provider-input trend."


def check_status(run: dict, stage: str) -> str:
    checks = run.get(f"{stage}_checks", [])
    return ", ".join("passed" if c.get("exit_code") == 0 else c.get("failure_reason") or "unknown"
                     for c in checks) or "not evaluated"


def hypotheses(runs: list[dict]) -> list[str]:
    lines = []
    for r in runs:
        label = "/".join(cell(r[k]) for k in ("task_id", "trial_id", "variant"))
        events = r.get("events", [])
        prior_reads = set(r.get("non_owner_reads_before_owner") or [])
        early = [(e.get("event_id"), prior_reads & set(e.get("source_paths_read", []))) for e in events]
        if len(prior_reads) >= 3:
            links = [f"{cell(event)} ({', '.join(map(cell, sorted(paths)))})" for event, paths in early if paths]
            if links:
                lines.append(f"- {label}: check whether unclear ownership caused early reads at {', '.join(links[:5])}.")
        seen_searches = set()
        for e in events:
            fingerprint = e.get("search_fingerprint")
            if fingerprint and fingerprint in seen_searches and e.get("paths_mentioned"):
                paths = ", ".join(map(cell, e["paths_mentioned"][:3]))
                lines.append(f"- {label}: check for ambiguous naming around {paths} after repeated search at {cell(e.get('event_id'))}.")
            if fingerprint:
                seen_searches.add(fingerprint)
        timeouts = [cell(e.get("event_id")) for e in events if e.get("exit_code") == 124]
        signals = [f"{cell(e.get('event_id'))} ({', '.join(e['environment_signals'])})"
                   for e in events if e.get("environment_signals")]
        if signals:
            lines.append(f"- {label}: check dependencies and permissions at {', '.join(signals[:5])}.")
        if timeouts:
            lines.append(f"- {label}: investigate environment timeouts at {', '.join(timeouts)}.")
    return lines or ["No evidence-linked architecture hypothesis was generated from the available events."]


def render_report(doc: dict[str, Any]) -> tuple[str, bool]:
    runs = doc.get("runs")
    if not isinstance(runs, list) or not isinstance(doc.get("metadata", {}), dict):
        raise ValueError("expected benchmark metadata and a runs array")
    meta = doc.get("metadata", {})
    legacy = doc.get("schema_version") != 4
    mode = meta.get("mode", "edit")
    pairs, incomplete = pair_runs(runs)
    matched = [p for p in pairs if pass_run(p[1], mode) and pass_run(p[2], mode)]
    tasks = sorted({r["task_id"] for r in runs} | set(meta.get("task_ids", [])))
    outcome, reason = decision(meta, runs, pairs, matched, tasks, incomplete, legacy, mode)
    comparison_pairs = [] if outcome in {"INVALID", "REGRESSION"} else matched
    lines = [
        "# Codex agent-navigation benchmark", "", "## Executive outcome", "", f"**{outcome}** — {reason}",
        "", "## Correctness gate", "",
        f"Both-success pairs: {len(matched)}/{len(pairs)}. Excluded: {len(pairs) - len(matched)} unsuccessful pairs, "
        f"{incomplete} missing one side.", "",
        "| Task | Trial | Revision | Agent | Behavior | Regression | Setup | Success verifier | Regression verifier |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        lines.append(row(r["task_id"], r["trial_id"], r["variant"], r.get("agent_completion"),
                         r.get("behavior", r.get("correct")), r.get("regression", r.get("verified")),
                         *(check_status(r, stage) for stage in ("setup", "success", "regression"))))
    lines.extend([
        "", "## Experiment validity", "",
        f"- Mode {cell(mode)}, host preflight {cell(meta.get('preflight_status'))}, sandbox smoke {cell(meta.get('sandbox_smoke_status'))}.",
        f"- CLI {cell(meta.get('codex_cli_version'))}, model {cell(meta.get('model_requested'))}, "
        f"sandbox {cell(meta.get('sandbox_mode'))}, order seed {cell(meta.get('seed'))}.",
        f"- Revisions {cell(meta.get('revisions'))}. Manifest SHA-256 {cell(meta.get('manifest_sha256'))}.",
        f"- Study {cell(meta.get('study_kind'))}. Holdouts {cell(meta.get('holdout_tasks'))}. "
        "Tasks used to choose the architecture can overstate gains.",
        "- Small sample. Three matched pairs per task support a preliminary trend. A single pair is an instrumentation pilot.",
        "- Mixed output, missing or truncated capture and unsupported tools limit source attribution.",
        "- Unknown values indicate missing or ineligible evidence.",
        "", "| Task | Trial | Revision | Actual order | Started UTC | Environment | Provider usage | Source attribution | Output gaps | Mixed events | Unknown tools |",
        "|---|---|---|---:|---|---|---|---|---:|---:|---:|",
    ])
    if meta.get("offline_reanalysis"):
        replay = meta["offline_reanalysis"]
        lines.insert(lines.index("## Experiment validity") + 2,
                     f"- Offline replay reclassified {replay['parsed']} of {len(runs)} runs. "
                     "Missing raw traces have unknown source coverage. Historical verifier results were imported.")
    if legacy:
        lines.insert(lines.index("## Experiment validity") + 2,
                     "- Historical v3 source units and completeness flags are excluded from v4 source measurements.")
    for r in runs:
        events = r.get("events", [])
        observed = not legacy and "events" in r
        gaps = sum(e.get("output_status") in {"missing", "partial", "truncated"} for e in events)
        mixed = (r.get("category_counts") or {}).get("mixed_unattributed", 0)
        lines.append(row(*(r.get(k) for k in ("task_id", "trial_id", "variant", "execution_order", "started_at", "environment")),
                         r.get("model_usage_coverage"), "legacy unknown" if legacy else r.get("source_attribution_coverage"),
                         gaps if observed else None, mixed if observed else None,
                         r.get("tool_calls_not_instrumented") if observed else None))
    for check in meta.get("host_preflight", []) + meta.get("sandbox_smoke", []):
        lines.append(f"\n- {cell(check.get('task_id'))}/{cell(check.get('variant'))} preflight {cell(check.get('status'))}. "
                     f"Setup {check_status(check, 'setup')}. Success {check_status(check, 'success')}. "
                     f"Regression {check_status(check, 'regression')}. "
                     f"Reason {cell(check.get('reason'))}. Sandbox evidence {cell(check.get('evidence'))}.")
    lines.extend([
        "", "## Provider usage", "",
        "Uncached input is total input less cached input. Input includes all provider-reported context. "
        "Changes are descriptive and include both-success pairs with the metric present.",
        "", *paired_table(comparison_pairs, tasks, PROVIDER),
        "", "## Navigation / source attribution", "",
        "Path mentions can come from listings, commands and results. Confirmed reads require attributable content. "
        "Source bytes and regex units describe captured shell output and have no exact model-token equivalent.",
        "", *paired_table([] if legacy else comparison_pairs, tasks, NAVIGATION), "",
        "| Task | Trial | Revision | Paths mentioned | Owner event | Confirmed reads | Observed source bytes | Mixed bytes | Discovery score | Changed source files | Parent directories |",
        "|---|---|---|---:|---|---:|---:|---:|---|---|---:|",
    ])
    for r in runs:
        values = (len(r["files_mentioned"]) if "files_mentioned" in r else None,
                  r.get("owner_first_mention_event_id"), r.get("confirmed_source_reads"),
                  r.get("observed_source_output_bytes"), r.get("mixed_output_bytes_observed"),
                  (r.get("discovery_score") or {}).get("passed"), r.get("changed_source_files"),
                  r.get("changed_source_directories"))
        lines.append(row(r["task_id"], r["trial_id"], r["variant"], *([None] * len(values) if legacy else values)))
    lines.extend(["", "## Environment overhead", "",
                  "| Task | Trial | Revision | Observed test commands | Identical test retries | Diagnostics | Exit 124 | Runner timeouts | Agent failure reason |",
                  "|---|---|---|---:|---:|---:|---:|---:|---|"])
    for r in runs:
        events = r.get("events", [])
        counts = [sum(kind in e.get("operations_detected", []) for e in events)
                  for kind in ("test_execution", "environment_diagnostics")]
        counts.insert(1, r.get("repeated_test_commands"))
        counts.append(sum(e.get("exit_code") == 124 for e in events))
        checks = [c for stage in ("setup", "success", "regression") for c in r.get(f"{stage}_checks", [])]
        timeout_count = sum(c.get("timed_out") is True for c in checks) + int(r.get("codex_timed_out") is True)
        if legacy or "events" not in r:
            counts = [None] * 4
        lines.append(row(r["task_id"], r["trial_id"], r["variant"], *counts, timeout_count,
                         (r.get("agent") or {}).get("failure_reason")))
    next_action = {
        "INVALID": "Resolve the recorded environment or oracle failure and repeat preflight.",
        "REGRESSION": "Investigate the failed correctness or regression check before using efficiency metrics.",
        "INCONCLUSIVE": "Resolve coverage and comparability gaps before repeating with sufficient matched trials.",
        "NO_DEMONSTRATED_GAIN": "Review per-task results and linked observations before selecting follow-up changes.",
        "PROMISING": "Repeat on untouched holdout tasks and review whether the code remains maintainable.",
    }
    lines.extend(["", "## Evidence-linked hypotheses", "", *hypotheses(runs), "", "## Recommended next action", "",
                  next_action[outcome], ""])
    return "\n".join(lines), outcome in {"PROMISING", "NO_DEMONSTRATED_GAIN"}


def reanalyze(doc: dict, trace_dir: Path, *, owners: dict | None = None,
              worktree_root: Path | None = None, assume_root_cwd: bool = False) -> dict:
    """Replay saved events while retaining the historical status of the experiment."""
    if not trace_dir.is_dir():
        raise ValueError("Raw trace directory does not exist")
    old = doc.get("runs")
    if not isinstance(old, list):
        raise ValueError("Expected historical runs")
    pair_runs(old)
    meta = doc.get("metadata", {})
    output = []
    parsed = 0
    for index, previous in enumerate(old, 1):
        task, trial, variant = (previous[k] for k in ("task_id", "trial_id", "variant"))
        if not re.fullmatch(r"[\w-]+", task) or not re.fullmatch(r"[\w-]+", trial):
            raise ValueError("Unsafe historical task or trial identifier")
        label = f"{task}-{trial}-{variant}"
        raw = trace_dir / f"{label}.jsonl"
        row_data = {k: previous[k] for k in (
            "task_id", "trial_id", "variant", "correct", "verified", "codex_exit_code", "codex_timed_out",
            "total_input_tokens", "cached_input_tokens", "output_tokens", "changed_files"
        ) if k in previous}
        row_data.update({"schema_version": 4, "execution_order": previous.get("execution_order", index),
                         "environment": "unknown", "source_tokens_read": None, "source_tokens_to_owner": None,
                         "source_attribution_coverage": "insufficient", "trace_complete_for_source_proxy": False,
                         "raw_trace_status": "missing", "model_usage_coverage":
                         "partial" if row_data.get("total_input_tokens") is not None else "missing"})
        if raw.is_file() and not raw.is_symlink():
            summary = summarize_jsonl(
                raw, (owners or {}).get(task, {}).get(variant, []),
                repo_root=(worktree_root or trace_dir) / label, relative_cwd_known=assume_root_cwd,
            )
            row_data.update(summary)
            row_data["raw_trace_status"] = "reclassified"
            parsed += 1
        output.append(row_data)
    return {"schema_version": 4, "metadata": {
        **meta, "preflight_status": "historical_unvalidated", "study_kind": "historical_instrumentation_pilot"
        if meta.get("trials_per_task") == 1 else "historical", "offline_reanalysis": {
            "origin_schema_version": doc.get("schema_version", 3), "parsed": parsed,
            "relative_root_cwd_assumed": assume_root_cwd,
        },
    }, "runs": output}


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        if os.name == "posix":
            os.fchmod(file.fileno(), 0o600)
        file.write(text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path, help="results.json produced by benchmark_codex.py")
    p.add_argument("--out", type=Path, help="Markdown report path")
    p.add_argument("--reanalyze-dir", type=Path, help="Offline directory containing saved task-trial-variant.jsonl")
    p.add_argument("--legacy-manifest", type=Path, help="Optional frozen manifest for offline owner mentions")
    p.add_argument("--worktree-root", type=Path, help="Original parent directory of worktrees if traces were moved")
    p.add_argument("--assume-root-cwd", action="store_true", help="Use root for relative paths when cwd is known externally")
    p.add_argument("--reanalysis-json", type=Path, help="Write sanitized replay results")
    p.add_argument("--enforce", action="store_true", help="Return nonzero if the correctness comparison gate fails")
    args = p.parse_args()
    try:
        doc = json.loads(args.results.read_text(encoding="utf-8"))
        if args.reanalyze_dir:
            owners = {}
            if args.legacy_manifest:
                manifest = json.loads(args.legacy_manifest.read_text(encoding="utf-8"))
                owners = {t["id"]: t.get("owners", {}) for t in manifest["tasks"]}
            doc = reanalyze(doc, args.reanalyze_dir, owners=owners, worktree_root=args.worktree_root,
                            assume_root_cwd=args.assume_root_cwd)
        if args.reanalysis_json:
            if not args.reanalyze_dir:
                raise ValueError("--reanalysis-json requires --reanalyze-dir")
            write_private(args.reanalysis_json, json.dumps(doc, indent=2) + "\\n")
        report, eligible = render_report(doc)
    except (KeyError, TypeError, OSError, ValueError) as exc:
        print(f"Report error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        write_private(args.out, report)
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 1 if args.enforce and not eligible else 0


if __name__ == "__main__":
    raise SystemExit(main())
