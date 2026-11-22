#!/usr/bin/env python3
"""Create an evidence-linked (not causal) before/after Codex navigation report."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

FIELDS = (
    ("total_input_tokens", "Codex model input tokens (reported)"),
    ("source_tokens_to_owner", "Navigation output units to first owner mention (estimated)"),
    ("source_tokens_read", "Navigation output units overall (estimated)"),
    ("search_steps", "Search commands"),
    ("owner_steps", "Shell commands to first owner mention"),
    ("navigation_steps", "Shell commands"),
    ("repeated_searches", "Repeated search fingerprints"),
)


def median_display(values: list[int | float]) -> str:
    if not values:
        return "unavailable"
    m = statistics.median(values)
    return f"{m:,.1f}" if isinstance(m, float) and not m.is_integer() else f"{int(m):,}"


def diagnostic(run: dict[str, Any]) -> list[tuple[str, str]]:
    observations: list[tuple[str, str]] = []
    task = f"{run.get('task_id', '?')} trial {run.get('trial_id', '?')} ({run.get('variant', '?')})"
    if run.get("thread_failed") or run.get("codex_timed_out") or run.get("codex_exit_code") not in (0, None):
        observations.append(("Run failure", f"{task}: Codex did not complete; inspect CLI/environment before assigning an architectural cause"))
    if run.get("owner_path_was_seen") is False:
        observations.append(("Owner not observed", f"{task}: configured owner path never appeared in parsed shell commands/results; inspect naming and task equivalence, or incomplete capture"))
    if isinstance(run.get("owner_steps"), int) and run["owner_steps"] >= 8:
        observations.append(("Late owner appearance", f"{task}: owner first appeared at shell command {run['owner_steps']}; inspect path naming, aliases and duplicated handlers"))
    if run.get("non_owner_read_count_before_owner", 0) >= 3:
        n = run["non_owner_read_count_before_owner"]
        paths = ", ".join((run.get("non_owner_reads_before_owner") or [])[:5])
        observations.append(("Non-owner reads", f"{task}: {n} non-owner files read before first owner-path mention ({paths}); determine whether these were necessary dependencies or false leads"))
    if run.get("repeated_searches", 0) >= 2:
        observations.append(("Repeated search", f"{task}: {run['repeated_searches']} redundant search fingerprint(s); inspect discoverability and ambiguous domain terms"))
    if run.get("search_steps", 0) >= 7:
        observations.append(("Search churn", f"{task}: {run['search_steps']} searches; assess whether feature ownership is scattered or task vocabulary differs from source names"))
    changed = run.get("changed_files", [])
    if isinstance(changed, list) and len(changed) >= 5:
        paths = ", ".join(changed[:6])
        observations.append(("Change amplification", f"{task}: {len(changed)} changed paths ({paths}); check if task is inherently cross-cutting before deepening a module"))
    if run.get("trace_complete_for_source_proxy") is False:
        observations.append(("Telemetry gap", f"{task}: source-output proxy unavailable due to missing/empty navigation output, malformed events or unsupported MCP tool calls; do not claim source-context savings"))
    return observations


def render_report(doc: dict[str, Any]) -> tuple[str, bool]:
    runs = doc.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("expected results.json containing a nonempty runs array")
    lookup: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for r in runs:
        if not isinstance(r, dict):
            raise ValueError("runs entries must be objects")
        task, trial, variant = r.get("task_id"), r.get("trial_id"), r.get("variant")
        if not isinstance(task, str) or not isinstance(trial, str) or variant not in {"before", "after"}:
            raise ValueError("run missing task_id, trial_id or valid variant")
        if variant in lookup[(task, trial)]:
            raise ValueError(f"duplicate result for {task}/{trial}/{variant}")
        lookup[(task, trial)][variant] = r
    pairs = []
    for key, variants in sorted(lookup.items()):
        if set(variants) != {"before", "after"}:
            raise ValueError(f"Missing before/after pair for task/trial {key}")
        pairs.append((key, variants["before"], variants["after"]))

    pass_run = lambda r: r.get("correct") is True and r.get("verified") is True
    regressions = [(task, trial) for (task, trial), a, b in pairs if pass_run(a) and not pass_run(b)]
    valid_pairs = [(key, a, b) for key, a, b in pairs if pass_run(a) and pass_run(b)]
    before_ok = sum(pass_run(a) for _, a, _ in pairs)
    after_ok = sum(pass_run(b) for _, _, b in pairs)
    stable_or_better = not regressions and after_ok >= before_ok
    lines = [
        "# Codex agent-navigation benchmark — v3", "",
        f"**Task trials:** {len(pairs)} paired. **Correct + verified:** before {before_ok}/{len(pairs)}, after {after_ok}/{len(pairs)}.",
        f"**Paired correctness regressions:** {len(regressions)}. **Both-pass pairs:** {len(valid_pairs)}.",
        "",
        "**Interpretation:** Observational experiment, not proof of causality. Do not optimize only successful trials: the pass-rate and regression gates above take priority over cost.",
        "",
        "## Comparability and telemetry", "",
    ]
    meta = doc.get("metadata") or {}
    if isinstance(meta, dict):
        lines.append(f"- Codex CLI: `{meta.get('codex_cli_version') or 'unrecorded'}`; requested model: `{meta.get('model_requested') or 'UNPINNED'}`.")
        lines.append(f"- Before / after Git commits: `{(meta.get('revisions') or {}).get('before', '?')}` / `{(meta.get('revisions') or {}).get('after', '?')}`.")
        lines.append(f"- Baseline-red preflight: **{'yes' if meta.get('preflight_performed') else 'no'}**.")
    missing = sum(not r.get("trace_complete_for_source_proxy", False) for r in runs)
    unlogged = sum(r.get("total_input_tokens") is None for r in runs)
    lines.append(f"- Source-output proxy incomplete: {missing}/{len(runs)} runs; Codex model input usage unavailable: {unlogged}/{len(runs)} runs.")
    lines.append("- `total_input_tokens` is **Codex-reported**. `source_tokens_*` are **rough regex units of observed shell search/read output**, not billing tokens, not full model context, and not proof the agent understood the owner. An owner 'seen' means its path appeared in a command or output.")
    lines.extend(["", "## Paired medians (only trials that pass on both sides)", "", "| Metric | Before | After | Change | Matched pairs |", "|---|---:|---:|---:|---:|"])
    for field, label in FIELDS:
        data = [(a.get(field), b.get(field)) for _, a, b in valid_pairs]
        measured = [(a, b) for a, b in data if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool)]
        if not measured:
            lines.append(f"| {label} | — | — | unavailable | 0 |")
            continue
        med_a = statistics.median(x for x, _ in measured)
        med_b = statistics.median(y for _, y in measured)
        change = f"{(med_b - med_a) / med_a * 100:+.1f}%" if med_a else "no % baseline"
        lines.append(f"| {label} | {median_display([med_a])} | {median_display([med_b])} | {change} | {len(measured)} |")
    lines.extend(["", "## Task and trial outcomes", "", "| Task | Trial | Before | After | Owner path visible (before → after) |", "|---|---|---|---|---|"])
    for (task, trial), a, b in pairs:
        status = lambda r: "PASS" if pass_run(r) else "FAIL"
        visible = f"{a.get('owner_path_was_seen')} → {b.get('owner_path_was_seen')}"
        lines.append(f"| {task} | {trial} | {status(a)} | {status(b)} | {visible} |")
    findings: dict[str, list[str]] = defaultdict(list)
    for r in runs:
        for category, text in diagnostic(r):
            findings[category].append(text)
    lines.extend(["", "## Navigation failure hypotheses (not automatic diagnoses)", ""])
    if findings:
        for category, items in sorted(findings.items(), key=lambda x: (-len(x[1]), x[0])):
            lines.append(f"### {category} ({len(items)} observations)")
            for item in items[:8]:
                lines.append(f"- {item}")
            if len(items) > 8:
                lines.append(f"- … {len(items) - 8} additional occurrences")
            lines.append("")
    else:
        lines.append("No heuristic flags. Review real traces manually before claiming architecture is optimal.\n")
    lines.extend(["## Decision gate", ""])
    if regressions:
        lines.append(f"**BLOCKED:** a previously passing task regressed in {len(regressions)} paired trial(s). Do not claim an efficiency win.")
    elif not valid_pairs:
        lines.append("**INSUFFICIENT:** no matched pair passed on both versions; no efficiency comparison is justified.")
    elif not stable_or_better:
        lines.append("**BLOCKED:** aggregate correctness decreased even without matched before-pass/after-fail samples. Review run coverage.")
    elif missing:
        lines.append("**PARTIAL:** correctness gate passed, but some source exposure estimates are incomplete. Model-token data may still be comparable for observed both-pass pairs.")
    else:
        lines.append("**ELIGIBLE FOR REVIEW:** no observed correctness regression and source proxy captured. Efficiency must still be assessed using paired costs and source-level maintainability review.")
    lines.append("\n**Next step:** inspect flagged trace events, attempt the smallest refactor supported by evidence, and retest untouched holdout tasks. Retain changes only when tests and maintenance review agree.\n")
    return "\n".join(lines), (stable_or_better and bool(valid_pairs))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path, help="results.json produced by benchmark_codex.py")
    p.add_argument("--out", type=Path, help="Markdown report path")
    p.add_argument("--enforce", action="store_true", help="Return nonzero if the correctness comparison gate fails")
    args = p.parse_args()
    try:
        report, eligible = render_report(json.loads(args.results.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"Report error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 1 if args.enforce and not eligible else 0


if __name__ == "__main__":
    raise SystemExit(main())
