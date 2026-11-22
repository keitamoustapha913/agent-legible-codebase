#!/usr/bin/env python3
"""Run a paired, cold-start Codex CLI benchmark in disposable Git worktrees.

Requires Git and Codex CLI to run real experiments. Python standard library only.
IMPORTANT: manifest commands are local processes; review them before running.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from codex_trace import summarize_jsonl
from report_codex import render_report


class BenchmarkError(ValueError):
    pass


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if proc.returncode:
        raise BenchmarkError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def checked_argv(value: Any, field: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise BenchmarkError(f"{field} must be a nonempty array of command argument arrays")
    result: list[list[str]] = []
    for item in value:
        if not isinstance(item, list) or not item or any(not isinstance(v, str) or not v for v in item):
            raise BenchmarkError(f"{field}: each command must be a nonempty array of nonempty strings")
        result.append(item)
    return result


def parse_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"Cannot load manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        raise BenchmarkError("Manifest must contain a nonempty tasks array")
    tasks = data["tasks"]
    ids = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"].strip():
            raise BenchmarkError("Each task needs a nonempty string id")
        if task["id"] in ids or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in task["id"]):
            raise BenchmarkError(f"Duplicate or unsafe task id: {task['id']!r}")
        ids.add(task["id"])
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            raise BenchmarkError(f"{task['id']}: missing prompt")
        checked_argv(task.get("success_checks"), f"{task['id']}.success_checks")
        checked_argv(task.get("regression_checks"), f"{task['id']}.regression_checks")
        owners = task.get("owners", {})
        if not isinstance(owners, dict) or any(not isinstance(owners.get(v, []), list) or any(not isinstance(p, str) for p in owners.get(v, [])) for v in ("before", "after")):
            raise BenchmarkError(f"{task['id']}: owners must have before/after arrays of paths")
        setup = task.get("setup_commands", [])
        if setup:
            checked_argv(setup, f"{task['id']}.setup_commands")
    return tasks


def execute_command(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout, check=False, start_new_session=(os.name == "posix"))
        return {"command": argv, "exit_code": proc.returncode, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"command": argv, "exit_code": None, "timed_out": True}
    except OSError as exc:
        return {"command": argv, "exit_code": None, "timed_out": False, "execution_error": str(exc)}


def execute_checks(commands: list[list[str]], cwd: Path, timeout: int) -> list[dict[str, Any]]:
    return [execute_command(argv, cwd, timeout) for argv in commands]


def all_pass(results: list[dict[str, Any]]) -> bool:
    return bool(results) and all(c["exit_code"] == 0 and not c["timed_out"] for c in results)


def new_worktree(repo: Path, root: Path, name: str, revision: str) -> Path:
    location = root / name
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(location), revision],
                          capture_output=True, text=True, check=False)
    if proc.returncode:
        raise BenchmarkError(f"Could not create {name} worktree: {proc.stderr.strip()[:400]}")
    return location


def remove_worktree(repo: Path, path: Path) -> None:
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
                          capture_output=True, text=True, check=False)
    if proc.returncode:
        print(f"WARNING: could not remove worktree {path}: {proc.stderr.strip()}", file=sys.stderr)


def modified_files(path: Path, commit: str) -> list[str]:
    tracked = run_git(path, "diff", "--name-only", commit, "--").splitlines()
    untracked = run_git(path, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(x for x in tracked + untracked if x))


def preflight(repo: Path, root: Path, revisions: dict[str, str], tasks: list[dict[str, Any]],
              timeout: int) -> None:
    print("Preflight: require behavioral success checks to fail and regression checks to pass at both revisions.", flush=True)
    for variant, revision in revisions.items():
        for task in tasks:
            location = new_worktree(repo, root, f"preflight-{variant}-{task['id']}", revision)
            try:
                setup = execute_checks(task.get("setup_commands", []), location, timeout) if task.get("setup_commands") else []
                if setup and not all_pass(setup):
                    raise BenchmarkError(f"Preflight setup failed: {variant}/{task['id']}; check tooling/dependencies")
                success = execute_checks(task["success_checks"], location, timeout)
                regression = execute_checks(task["regression_checks"], location, timeout)
                if all_pass(success):
                    raise BenchmarkError(f"Preflight task already passes: {variant}/{task['id']}; use an unsolved task oracle")
                if not all_pass(regression):
                    raise BenchmarkError(f"Preflight regressions failed: {variant}/{task['id']}; baseline not green")
            finally:
                remove_worktree(repo, location)


def collect_run(repo: Path, root: Path, task: dict[str, Any], variant: str, revision: str,
                trial: str, opts: argparse.Namespace, model_id: str | None) -> dict[str, Any]:
    label = f"{task['id']}-{trial}-{variant}"
    cwd = new_worktree(repo, root, label, revision)
    print(f"Running {label}", flush=True)
    trace_file = root / f"{label}.jsonl"
    error_file = root / f"{label}.stderr.txt"
    return_code: int | None = None
    timed_out = False
    setup_results: list[dict[str, Any]] = []
    success: list[dict[str, Any]] = []
    regression: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    edits: list[str] = []
    preagent_changes: list[str] = []
    try:
        if task.get("setup_commands"):
            setup_results = execute_checks(task["setup_commands"], cwd, opts.check_timeout)
            if not all_pass(setup_results):
                raise BenchmarkError(f"Setup failed for {label}. Nothing was sent to Codex")
        preagent_changes = modified_files(cwd, revision)
        args = [str(opts.codex), "exec", "--json", "--ephemeral", "--sandbox", "workspace-write", "--cd", str(cwd)]
        if opts.model:
            args.extend(["--model", opts.model])
        args.append("-")
        with trace_file.open("w", encoding="utf-8") as stdout, error_file.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                    text=True, start_new_session=(os.name == "posix"))
            try:
                proc.communicate(input=task["prompt"], timeout=opts.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.communicate()
            return_code = proc.returncode
        summary = summarize_jsonl(trace_file, task.get("owners", {}).get(variant, []))
        # Capture changed paths BEFORE verifiers; tests may generate files.
        # Paths already touched by setup are not confidently attributable to Codex.
        edits = sorted(set(modified_files(cwd, revision)) - set(preagent_changes))
        success = execute_checks(task["success_checks"], cwd, opts.check_timeout)
        regression = execute_checks(task["regression_checks"], cwd, opts.check_timeout)
    finally:
        if not opts.keep_worktrees:
            remove_worktree(repo, cwd)
        if not opts.keep_raw:
            trace_file.unlink(missing_ok=True)
            error_file.unlink(missing_ok=True)
    pass_session = return_code == 0 and not timed_out and not summary.get("thread_failed", True)
    correctness = pass_session and all_pass(success)
    verification = pass_session and all_pass(regression)
    record = {
        "task_id": task["id"], "trial_id": trial, "variant": variant,
        "correct": correctness, "verified": verification,
        "codex_exit_code": return_code, "codex_timed_out": timed_out,
        "model_requested": model_id,
        "success_checks": success, "regression_checks": regression,
        "setup_checks": setup_results,
        "changed_files": edits, "preagent_changed_files": preagent_changes,
        "worktree_retained": str(cwd) if opts.keep_worktrees else None,
    }
    record.update(summary)
    # Failed or interrupted sessions cannot establish a reliable total source cost.
    if not pass_session:
        record["source_tokens_read"] = None
        record["source_tokens_to_owner"] = None
    return record


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True, help="Git repo to benchmark (not modified)")
    p.add_argument("--manifest", type=Path, required=True, help="Frozen task manifest JSON")
    p.add_argument("--before", required=True, help="Baseline Git commit/tag")
    p.add_argument("--after", required=True, help="Refactored Git commit/tag")
    p.add_argument("--out", type=Path, required=True, help="Fresh output dir OUTSIDE the repo")
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--codex", default="codex", help="Codex executable")
    p.add_argument("--model", help="Codex model; strongly recommended for comparability")
    p.add_argument("--timeout", type=int, default=900, help="Timeout per Codex run in seconds")
    p.add_argument("--check-timeout", type=int, default=120, help="Timeout per verifier in seconds")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--keep-raw", action="store_true", help="Retain sensitive raw JSONL + stderr logs")
    p.add_argument("--keep-worktrees", action="store_true", help="Retain Codex changes for manual review")
    p.add_argument("--skip-preflight", action="store_true", help="Skip baseline-red/regression-green preflight (lower confidence)")
    p.add_argument("--dry-run", action="store_true", help="Show plan, do not invoke Codex or tests")
    opts = p.parse_args()
    try:
        if opts.trials < 1 or opts.timeout < 1 or opts.check_timeout < 1:
            raise BenchmarkError("Trials and timeouts must be positive")
        repo = opts.repo.resolve()
        out = opts.out.resolve()
        if not repo.is_dir():
            raise BenchmarkError("--repo does not exist")
        top = Path(run_git(repo, "rev-parse", "--show-toplevel")).resolve()
        if top != repo:
            raise BenchmarkError(f"Use the Git toplevel as --repo: {top}")
        if out == repo or repo in out.parents:
            raise BenchmarkError("--out MUST be outside repo to prevent benchmark-file contamination")
        if out.exists() and any(out.iterdir()):
            raise BenchmarkError(f"--out already contains files: {out}")
        tasks = parse_manifest(opts.manifest.resolve())
        revisions = {v: run_git(repo, "rev-parse", f"{r}^{{commit}}") for v, r in (("before", opts.before), ("after", opts.after))}
        plan = [(task, str(trial), variant) for task in tasks for trial in range(1, opts.trials + 1) for variant in ("before", "after")]
        rng = random.Random(opts.seed)
        rng.shuffle(plan)
        print(f"Repo: {repo}\nRevisions: {revisions}\nTasks: {len(tasks)}, trials: {opts.trials}, total Codex runs: {len(plan)}")
        for task, trial, variant in plan:
            print(f"  {task['id']} / {trial} / {variant}")
        if opts.dry_run:
            print("DRY RUN: no worktrees, tests or Codex processes started")
            return 0
        if not shutil.which(opts.codex) and not Path(opts.codex).is_file():
            raise BenchmarkError("Codex CLI not found; install Codex and authenticate first")
        out.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(out, 0o700)
        cli_version = subprocess.run([str(opts.codex), "--version"], capture_output=True, text=True, check=False).stdout.strip()
        print(f"Codex CLI: {cli_version or 'unknown'}; model requested: {opts.model or 'UNPINNED'}")
        if not opts.model:
            print("WARNING: no pinned --model; compare only when actual model/config matches", file=sys.stderr)
        if opts.skip_preflight:
            print("WARNING: preflight omitted; task checks may already pass or baseline may be broken", file=sys.stderr)
        else:
            preflight(repo, out, revisions, tasks, opts.check_timeout)
        results: list[dict[str, Any]] = []
        for task, trial, variant in plan:
            result = collect_run(repo, out, task, variant, revisions[variant], trial, opts, opts.model)
            results.append(result)
            # Checkpoint in case a later invocation fails.
            (out / "results.partial.json").write_text(json.dumps({"runs": results}, indent=2) + "\n", encoding="utf-8")
        metadata = {
            "tool": "agent-legible-codebase v3 Codex benchmark",
            "codex_cli_version": cli_version, "model_requested": opts.model,
            "revisions": revisions, "trials_per_task": opts.trials, "seed": opts.seed,
            "preflight_performed": not opts.skip_preflight,
            "raw_logs_retained": opts.keep_raw,
            "note": "Codex input_tokens are provider-reported; source_token fields are regex proxy from visible shell output, never exact source/billing tokens.",
        }
        (out / "results.json").write_text(json.dumps({"metadata": metadata, "runs": results}, indent=2) + "\n", encoding="utf-8")
        for variant in ("before", "after"):
            (out / f"{variant}.json").write_text(json.dumps({"runs": [r for r in results if r["variant"] == variant]}, indent=2) + "\n", encoding="utf-8")
        (out / "results.partial.json").unlink(missing_ok=True)
        report, eligible = render_report({"metadata": metadata, "runs": results})
        (out / "report.md").write_text(report, encoding="utf-8")
        print(f"Finished. Files: {out / 'results.json'}, {out / 'before.json'}, {out / 'after.json'}, {out / 'report.md'}")
        if not eligible:
            print("NOTE: Correctness gate did not pass; inspect report.md before any efficiency claims", file=sys.stderr)
        return 0
    except (BenchmarkError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
