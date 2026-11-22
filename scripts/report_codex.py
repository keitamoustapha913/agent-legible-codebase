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
from discovery_eligibility import assess_discovery_run
from trace_metrics import numeric_metric

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
DISCOVERY_METRICS = (
    ("total_input_tokens", "Input tokens"), ("uncached_input_tokens", "Uncached input"),
    ("search_steps", "Searches"), ("confirmed_source_reads", "Confirmed source reads"),
    ("successful_navigation_calls", "Successful navigation calls"),
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
    measured = []
    for _, before, after in pairs:
        first = numeric_metric(before, field)
        second = numeric_metric(after, field)
        if first is not None and second is not None:
            measured.append((first, second))
    return measured


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
    if mode == "isolated-verifier" and (
        (meta.get("verifier_readiness") or {}).get("status") != "ready"
        or (meta.get("isolated_agent_boundary") or {}).get("exit_code") != 0
    ):
        return "INVALID", "The isolated verifier VM or restricted agent boundary could not be validated."
    runtime = meta.get("sandbox_runtime") or {}
    if runtime.get("status") == "environment_blocked":
        if runtime.get("reason") == "sandbox_python_local_ipc":
            return "INVALID", "Sandbox infrastructure blocked Python local IPC or thread wakeups before any model session."
        return "INVALID", "The non-model workspace-write runtime check could not validate the environment."
    if (meta.get("preflight_status") in {"preflight_invalid", "interrupted"} or
        meta.get("sandbox_smoke_status") in {"environment_blocked", "environment_confounded"} or
        meta.get("experiment_status") in {"invalid", "environment_blocked", "environment_confounded"} or
        any(r.get("environment") in {"blocked", "confounded"} for r in runs)):
        return "INVALID", "Oracle, setup, or environment evidence blocks an efficiency decision."
    if meta.get("experiment_status") == "readiness_checked":
        return "INCONCLUSIVE", "Non-model readiness checks passed. No model sessions were run."
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


def simple_outcome(meta: dict, pairs: list[tuple[str, dict, dict]],
                   tasks: list[str], missing: int, run_count: int) -> tuple[str, str]:
    state = meta.get("experiment_status")
    if state == "readiness_checked":
        return "Inconclusive", "Non-model readiness passed. No Codex sessions ran."
    if state in {"environment_blocked", "environment_confounded", "invalid"}:
        return "Inconclusive", "A runner or environment failure prevented a reliable comparison."
    mode = meta["mode"]
    for _, before, after in pairs:
        if (pass_run(before, mode) and after.get("agent_completion") == "completed"
                and after.get("environment") == "ready"
                and (after.get("behavior") == "incorrect" or after.get("regression") == "failed")):
            return "Worsened", "An independently checked task passed before the refactor and failed after it."
    if state != "completed" or missing or meta.get("planned_run_count") != run_count:
        return "Inconclusive", "The planned pairs did not all complete."
    if not pairs or any(not (pass_run(a, mode) and pass_run(b, mode)) for _, a, b in pairs):
        return "Inconclusive", "The tasks did not all pass independent checks on both revisions."
    if not meta.get("model_requested"):
        return "Inconclusive", "Pin the same model to interpret usage differences."
    deltas = []
    for task in tasks:
        subset = [pair for pair in pairs if pair[0] == task]
        inputs = get_metrics(subset, "total_input_tokens")
        if len(inputs) < 3 or len(inputs) != len(subset):
            return "Inconclusive", "A preliminary usage trend requires at least three verified pairs per task with reported input."
        deltas.append(statistics.median(after - before for before, after in inputs))
    if deltas and all(n <= 0 for n in deltas) and any(n < 0 for n in deltas):
        if mode == "discovery":
            return "Helped", "Strictly correct discovery trials used less provider input on this benchmark. The result is task-specific."
        return "Helped", "Verified tasks used less provider input in at least one task and none used more."
    if deltas and all(n >= 0 for n in deltas) and any(n > 0 for n in deltas):
        return "Worsened", "Verified tasks used more provider input in at least one task and none used less."
    return "Inconclusive", "Provider input was unchanged or moved in opposite directions across tasks."


def simple_measure(run: dict, field: str) -> int | None:
    value = run.get(field)
    if isinstance(value, list):
        return len(value)
    return value if type(value) is int else None


def simple_table(title: str, tasks: list[str], pairs: list[tuple[str, dict, dict]],
                 fields: tuple[tuple[str, str], ...]) -> list[str]:
    lines = ["", f"## {title}", "", row("Task", *(label for _, label in fields)),
             row("---", *("---" for _ in fields))]
    for task in tasks:
        cells = []
        for key, _ in fields:
            measurements = [(a, b) for name, before, after in pairs if name == task
                            if (a := simple_measure(before, key)) is not None
                            and (b := simple_measure(after, key)) is not None]
            if measurements:
                a, b = (statistics.median(values) for values in zip(*measurements))
                cells.append(f"{a:,.1f}".removesuffix(".0") + " → "
                             + f"{b:,.1f}".removesuffix(".0") + f" ({len(measurements)})")
            else:
                cells.append(None)
        lines.append(row(task, *cells))
    return lines


def discovery_measure(run: dict, key: str) -> int | None:
    if key == "successful_navigation_calls":
        journal = run.get("agent", {}).get("navigation_server") or {}
        return simple_measure(journal, "succeeded") if journal.get("unparsed") == 0 else None
    return simple_measure(run, key)


def discovery_scores(run: dict) -> tuple[str, str]:
    strict = run.get("discovery_score")
    return ("pass" if strict["passed"] else "fail") if strict else "unscored", (
        run.get("discovery_semantic_score") or {}
    ).get("status", "unscored")


def discovery_comparison(tasks: list[str], runs: list[dict], pairs: list[tuple[str, dict, dict]]) -> list[str]:
    labels = ("Strict format", "Semantic facts", *(label for _, label in DISCOVERY_METRICS))
    lines = ["", "## Discovery scores and medians", "",
             "Strict grading checks the exact answer fields. Semantic grading checks whether the expected "
             "paths and declarations appear in the structured answer, regardless of field placement. "
             "It permits extra entries and leaves the prose rationale to human review.",
             "Medians use completed pairs with usable navigation. Parentheses show the number of measurements. "
             "Successful calls count server replies, including replies shortened by the output limit.",
             "", row("Task", "Revision", *labels), row(*(["---"] * (2 + len(labels))))]
    for task in tasks:
        for index, side in enumerate(("before", "after"), 1):
            selected = [pair[index] for pair in pairs if pair[0] == task]
            scores = list(zip(*(discovery_scores(run) for run in selected)))
            totals = [f"{statuses.count('pass')}/{len(selected)} passed"
                      + (f" ({statuses.count('unscorable') + statuses.count('unscored')} unscored)"
                         if any(status not in {"pass", "fail"} for status in statuses) else "")
                      for statuses in scores] if selected else [None, None]
            medians = []
            for key, _ in DISCOVERY_METRICS:
                values = [value for run in selected if (value := discovery_measure(run, key)) is not None]
                medians.append(f"{statistics.median(values):,.1f}".removesuffix(".0") + f" ({len(values)})" if values else None)
            lines.append(row(task, side, *totals, *medians))

    lines.extend(["", "## Individual discovery trials", "", row("Task", "Trial", "Revision", "Order", *labels),
                  row(*(["---"] * (4 + len(labels))))])
    for run in runs:
        strict, semantic = discovery_scores(run)
        lines.append(row(run["task_id"], run["trial_id"], run["variant"], run.get("execution_order"),
                         strict, semantic, *(discovery_measure(run, key) for key, _ in DISCOVERY_METRICS)))
    for run in runs:
        strict, semantic = discovery_scores(run)
        if (strict, semantic) != ("pass", "pass"):
            lines.append(f"- {cell(run['task_id'])} trial {cell(run['trial_id'])} ({cell(run['variant'])}) "
                         f"scored {cell(strict)} on strict grading and {cell(semantic)} on semantic grading.")
    for task in tasks:
        task_pairs = [(before, after) for name, before, after in pairs if name == task]
        for key, label in DISCOVERY_METRICS:
            deltas = [(before["trial_id"], after_value - before_value) for before, after in task_pairs
                      if (before_value := discovery_measure(before, key)) is not None
                      and (after_value := discovery_measure(after, key)) is not None]
            if len(deltas) < 2:
                continue
            median = statistics.median(delta for _, delta in deltas)
            differing = [f"{cell(trial)} ({delta:+,})" for trial, delta in deltas
                         if (delta > 0, delta < 0) != (median > 0, median < 0)]
            if differing:
                lines.append(f"- {cell(task)} {label.lower()} moved differently from the median paired change "
                             f"in trial {', '.join(differing)}.")
    return lines


def render_simple_report(meta: dict, runs: list[dict]) -> tuple[str, bool]:
    pairs, missing = pair_runs(runs)
    tasks = sorted({r["task_id"] for r in runs} | set(meta.get("task_ids", [])))
    outcome, reason = simple_outcome(meta, pairs, tasks, missing, len(runs))
    discovery = meta["mode"] == "discovery"
    if discovery:
        assessments = {id(r): assess_discovery_run(r) for r in runs}
        if any(result.reason for result in assessments.values()) or meta.get("experiment_status") in {
            "invalid", "environment_blocked", "environment_confounded"
        }:
            outcome, reason = "INVALID", "Discovery readiness or navigation evidence failed. The runs cannot measure architecture."
        observed = [p for p in pairs if all(assessments[id(r)].eligible for r in p[1:])]
    else:
        assessments = {}
        observed = [p for p in pairs if all(r.get("agent_completion") == "completed" and
                                            r.get("environment") == "ready" for r in p[1:])]
    lines = [
        "# Codex benchmark", "", f"**{outcome}** — {reason}", "",
        f"Model {cell(meta.get('model_requested'))}. Completed pairs with usable observations {len(observed)}/{len(pairs) + missing}.",
        "The tables show before → after medians and the number of measured pairs in parentheses.",
        "", "## Correctness", "",
    ]
    if meta.get("verification_environment") == "host":
        lines.append("Reviewed host commands run after Codex in the candidate checkout. They can execute edited code "
                     "with the controller's host permissions.")
    elif meta["mode"] == "discovery":
        lines.append("The offline oracle scores the discovery answer. Application tests are outside the discovery task.")
    else:
        lines.append("Host tests were not authorized. Edits and token usage were observed, and correctness is unevaluated.")
    lines.extend(["", row("Task", "Trial", "Revision", "Agent", "Behavior", "Regression", "Provider input", "Environment"),
                  row(*("---" for _ in range(8)))])
    for r in runs:
        assessment = assessments.get(id(r))
        invalid = assessment is not None and assessment.reason is not None
        environment = ("pytest execution observed" if r.get("agent_test_execution_observed")
                       else r.get("environment"))
        lines.append(row(r["task_id"], r["trial_id"], r["variant"], r.get("agent_completion"),
                         "unevaluated" if invalid else r.get("behavior"),
                         r.get("regression"), r.get("total_input_tokens"),
                         "INVALID" if invalid else environment))
    for r in runs:
        failures = {stage: check_status(r, stage) for stage in ("setup", "success", "regression")
                    if any(c.get("exit_code") != 0 for c in r.get(f"{stage}_checks", []))}
        if failures:
            lines.append(f"- {cell(r['task_id'])}/{cell(r['trial_id'])}/{cell(r['variant'])}: "
                         + ", ".join(f"{stage} {cell(status)}" for stage, status in failures.items()))
        if discovery:
            label = f"{cell(r['task_id'])}/{cell(r['trial_id'])}/{cell(r['variant'])}"
            assessment = assessments[id(r)]
            if assessment.reason:
                issue = "discovery violation" if assessment.environment == "confounded" else "invalid navigation"
                lines.append(f"- {label} {issue} — {cell(assessment.reason)}.")
            for event in r.get("events", []):
                if event.get("event_type") == "mcp_tool_call" and (
                    event.get("navigation_failure") or event.get("output_status") != "available"
                ):
                    lines.append(f"- {label} JSONL line {event.get('event_line')} "
                                 f"{cell(event.get('tool', event.get('category')))} — "
                                 f"{cell(event.get('navigation_failure') or event.get('execution_status'))}, "
                                 f"output {cell(event.get('output_status'))}.")
            if r.get("agent", {}).get("raw_logs"):
                lines.append(f"- {label} private raw logs — " + ", ".join(map(cell, r["agent"]["raw_logs"])) + ".")
            journal = r.get("agent", {}).get("navigation_server")
            if journal is not None:
                lines.append(f"- {label} MCP server recorded {journal['succeeded']} successful and "
                             f"{journal['failed']} failed tool replies.")
    for task in meta.get("discovery_preflight", []):
        detail = task.get("reason") or (
            "listing, search and source contents confirmed through local Codex MCP" if task.get("navigation") else None
        )
        lines.append(f"- {cell(task.get('task_id'))} {cell(task.get('variant'))} readiness {cell(task.get('status'))}"
                     + (f" — {cell(detail)}." if detail else "."))
    if discovery and any("discovery_semantic_score" in run for run in runs):
        if meta.get("offline_semantic_replay"):
            lines.extend(["", "Offline replay scored the archived answers. The original strict scores and traces were preserved."])
        lines.extend(discovery_comparison(tasks, runs, observed))
    lines.extend(simple_table("Provider token usage", tasks, observed, PROVIDER))
    lines.extend(simple_table("Discoverability", tasks, observed, (
        ("navigation_steps", "Navigation actions"), ("search_steps", "Searches"),
        ("files_mentioned", "Paths mentioned"), ("files_opened", "Distinct confirmed file reads"),
        ("owner_steps", "First owner mention"),
    )))
    lines.extend(simple_table("Maintenance effort", tasks, observed, (
        ("changed_files", "Paths edited"), ("changed_source_files", "Source files edited"),
        ("changed_source_directories", "Source directories edited"),
    )))
    lines.extend(["", "Provider tokens cover all logged context. File and edit counts are signals of navigation "
                  "and change scope. See results.json for per-run trace coverage, observed paths and verifier details.", ""])
    return "\n".join(lines), outcome not in {"Inconclusive", "INVALID"} and all(pass_run(r, meta["mode"]) for r in runs)


def render_report(doc: dict[str, Any]) -> tuple[str, bool]:
    runs = doc.get("runs")
    if not isinstance(runs, list) or not isinstance(doc.get("metadata", {}), dict):
        raise ValueError("expected benchmark metadata and a runs array")
    meta = doc.get("metadata", {})
    if meta.get("workflow") == "simple" and not meta.get("offline_reanalysis"):
        return render_simple_report(meta, runs)
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
        f"- Mode {cell(mode)}, verification {cell(meta.get('verification_environment'))}, "
        f"preflight {cell(meta.get('preflight_status'))}, sandbox smoke {cell(meta.get('sandbox_smoke_status'))}.",
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
    if mode == "isolated-verifier":
        index = lines.index("## Experiment validity") + 2
        vm = meta.get("verifier_readiness") or {}
        binding = vm.get("binding") or {}
        boundary = meta.get("isolated_agent_boundary") or {}
        lines.insert(index, f"- Verifier VM {cell(vm.get('status'))}, agent boundary exit {cell(boundary.get('exit_code'))}, "
                     f"VM profile SHA-256 {cell(binding.get('profile_sha256'))}.")
    runtime = meta.get("sandbox_runtime")
    if runtime:
        index = lines.index("## Experiment validity") + 2
        lines.insert(index, f"- Workspace-write runtime check: {cell(runtime.get('status'))} ({cell(runtime.get('reason'))}).")
        for side in ("host", "sandbox"):
            result = runtime.get(side) or {}
            lines.insert(index + 1, f"- Runtime {side}: Python {cell(result.get('python_version'))}, "
                         f"socketpair {cell(result.get('socketpair_ok'))}, errno {cell(result.get('socket_errno'))}, "
                         f"worker completed {cell(result.get('worker_completed'))}, "
                         f"asyncio closed {cell(result.get('asyncio_closed'))}, timeout {cell(result.get('timed_out'))}.")
            index += 1
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
    for check in (meta.get("host_preflight", []) + meta.get("sandbox_smoke", [])
                  + meta.get("verifier_preflight", []) + meta.get("discovery_preflight", [])):
        lines.append(f"\n- {cell(check.get('task_id'))}/{cell(check.get('variant'))} preflight {cell(check.get('status'))}. "
                     f"Setup {check_status(check, 'setup')}. Success {check_status(check, 'success')}. "
                     f"Regression {check_status(check, 'regression')}. "
                     f"Reason {cell(check.get('reason'))}. Sandbox evidence {cell(check.get('evidence'))}.")
    if mode == "isolated-verifier":
        lines.extend(["", "### Independent VM verification", "",
                      "| Task | Trial | Revision | VM result | Candidate SHA-256 |",
                      "|---|---|---|---|---|"])
        for r in runs:
            evaluation = r.get("verification") or {}
            lines.append(row(r["task_id"], r["trial_id"], r["variant"], evaluation.get("status"),
                             (evaluation.get("binding") or {}).get("artifact_sha256")))
    lines.extend([
        "", "## Provider usage", "",
        "Uncached input is total input less cached input. Input includes all provider-reported context. "
        "Changes are descriptive and include both-success pairs with the metric present.",
        "", *paired_table(comparison_pairs, tasks, PROVIDER),
        "", "## Navigation / source attribution", "",
        "Path mentions can come from listings, commands and results. Confirmed reads require attributable content. "
        "Source bytes and regex units describe captured shell output and have no exact model-token equivalent.",
        "", *paired_table([] if legacy else comparison_pairs, tasks, NAVIGATION), "",
        "| Task | Trial | Revision | Paths mentioned | Owner event | Confirmed reads | Observed source bytes | Mixed bytes | Discovery score |",
        "|---|---|---|---:|---|---:|---:|---:|---|",
    ])
    for r in runs:
        values = (len(r["files_mentioned"]) if "files_mentioned" in r else None,
                  r.get("owner_first_mention_event_id"), r.get("confirmed_source_reads"),
                  r.get("observed_source_output_bytes"), r.get("mixed_output_bytes_observed"),
                  (r.get("discovery_score") or {}).get("passed"))
        lines.append(row(r["task_id"], r["trial_id"], r["variant"], *([None] * len(values) if legacy else values)))
    lines.extend(["", "## Architecture evidence", "",
                  "| Task | Trial | Revision | Changed source files | Parent directories |",
                  "|---|---|---|---|---:|"])
    for r in runs:
        lines.append(row(r["task_id"], r["trial_id"], r["variant"],
                         None if legacy else r.get("changed_source_files"),
                         None if legacy else r.get("changed_source_directories")))
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
    action = next_action[outcome]
    if runtime and runtime.get("status") == "environment_blocked":
        action = "Restore compatibility under the intended sandbox policy and rerun the non-model runtime check."
    lines.extend(["", "## Evidence-linked hypotheses", "", *hypotheses(runs), "", "## Recommended next action", "",
                  action, ""])
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
                discovery_tools=meta.get("mode") in {"discovery", "isolated-verifier"},
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
