#!/usr/bin/env python3
"""Run a paired, cold-start Codex CLI benchmark in disposable Git worktrees.

Requires Git and Codex CLI to run real experiments. Python standard library only.
Host commands are opt-in. Review them and the code they execute.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import tempfile
from datetime import datetime, timezone
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from codex_trace import navigation_invalid_reason, summarize_jsonl
from trace_attribution import path_category, shell_body
from discovery_oracle import PROMPT_FORMAT, owner_symbols, score_answer, score_discovery_facts, valid_expected, validate_task
from discovery_readiness import private_opener, probe_navigation
from sandbox_runtime import NETWORK_DISABLED, check_runtime
from isolated_verifier import IsolatedVerifier, REGRESSION_ASSERTION, protected_digests
from report_codex import render_report, write_private
from environment_probe import (
    bounded_output, configuration_fingerprint, diagnostic_labels, execute_command, resolve_cli_version, run_process,
)


class BenchmarkError(ValueError):
    pass


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, text=True,
                          check=False, timeout=30)
    if proc.returncode:
        raise BenchmarkError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout if "-z" in args else proc.stdout.strip()


def checked_argv(value: Any, field: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise BenchmarkError(f"{field} must be a nonempty array of command argument arrays")
    result: list[list[str]] = []
    for item in value:
        if not isinstance(item, list) or not item or any(not isinstance(v, str) or not v for v in item):
            raise BenchmarkError(f"{field}: each command must be a nonempty array of nonempty strings")
        result.append(item)
    return result


def parse_manifest(path: Path, *, raw: bytes | None = None, mode: str = "edit",
                   require_checks: bool = False) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_bytes() if raw is None else raw)
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
        if task.get("mode", mode) != mode:
            raise BenchmarkError(f"{task['id']}: task mode must match --mode")
        if mode == "discovery":
            validate_task(task)
            continue
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            raise BenchmarkError(f"{task['id']}: missing prompt")
        for stage in ("success_checks", "regression_checks"):
            if require_checks or stage in task:
                checked_argv(task.get(stage), f"{task['id']}.{stage}")
            task.setdefault(stage, [])
        owners = task.get("owners", {})
        if not isinstance(owners, dict) or any(not isinstance(owners.get(v, []), list) or any(not isinstance(p, str) for p in owners.get(v, [])) for v in ("before", "after")):
            raise BenchmarkError(f"{task['id']}: owners must have before/after arrays of paths")
        setup = task.get("setup_commands", [])
        if setup != []:
            checked_argv(setup, f"{task['id']}.setup_commands")
    return tasks


def execute_checks(commands: list[list[str]], cwd: Path, timeout: int,
                   contracts: list[dict] | None = None) -> list[dict[str, Any]]:
    results = []
    for index, argv in enumerate(commands):
        result = execute_command(argv, cwd, timeout, expected_failure=contracts[index] if contracts else None)
        results.append(result)
        if result["failure_reason"] == "interrupted":
            break
    return results


def all_pass(results: list[dict[str, Any]]) -> bool:
    return bool(results) and all(c["exit_code"] == 0 and not c["timed_out"] for c in results)


def new_worktree(repo: Path, root: Path, name: str, revision: str) -> Path:
    location = root / name
    proc = subprocess.run(["git", "-C", str(repo), "-c", f"core.hooksPath={os.devnull}",
                           "worktree", "add", "--detach", str(location), revision],
                          capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode:
        raise BenchmarkError(f"Could not create {name} worktree: {proc.stderr.strip()[:400]}")
    return location


def remove_worktree(repo: Path, path: Path) -> None:
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
                          capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode:
        raise BenchmarkError(f"Could not remove disposable worktree {path}")


def modified_files(path: Path, commit: str) -> list[str]:
    tracked = run_git(path, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", commit, "--").split("\x00")
    untracked = run_git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\x00")
    return sorted(set(x for x in tracked + untracked if x))


def failure_contracts(task: dict, expected: Any) -> list[dict] | None:
    if expected is None:
        return None
    contracts = [expected] if isinstance(expected, dict) else expected
    if not isinstance(contracts, list) or len(contracts) != len(task["success_checks"]):
        raise BenchmarkError(f"{task['id']}: expected_failure needs one contract per success check")
    for entry in contracts:
        if not isinstance(entry, dict) or not isinstance(entry.get("regex"), str) or not entry["regex"].strip():
            raise BenchmarkError(f"{task['id']}: expected_failure requires a behavior-specific regex")
        code = entry.get("exit_code", 1)
        if type(code) is not int or not 1 <= code <= 125 or code == 124:
            raise BenchmarkError(f"{task['id']}: expected_failure requires a normal assertion exit code")
        try:
            pattern = re.compile(entry["regex"])
        except re.error as exc:
            raise BenchmarkError(f"{task['id']}: invalid expected_failure regex") from exc
        if pattern.search(""):
            raise BenchmarkError(f"{task['id']}: expected_failure regex must not match empty text")
    return contracts


def bind_oracles(tasks: list[dict], contract_path: Path | None, manifest_hash: str) -> str | None:
    overrides = {}
    override_hash = None
    if contract_path:
        raw = contract_path.read_bytes()
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("manifest_sha256") != manifest_hash or not isinstance(doc.get("tasks"), dict):
            raise BenchmarkError("Oracle contract must name the SHA-256 of this frozen manifest and supply a tasks object")
        overrides = doc["tasks"]
        if overrides.keys() - {task["id"] for task in tasks}:
            raise BenchmarkError("Oracle contract refers to unknown tasks")
        override_hash = hashlib.sha256(raw).hexdigest()
    for task in tasks:
        task["_contracts"] = failure_contracts(task, overrides.get(task["id"], task.get("expected_failure")))
    return override_hash


def plan_trials(tasks: list[dict], trials: int, seed: int) -> list[tuple[dict, str, str]]:
    rng = random.Random(seed)
    pairs = []
    for task in tasks:
        after_first = rng.choice([False, True])
        for trial in range(1, trials + 1):
            sides = ("after", "before") if after_first else ("before", "after")
            pairs.append([(task, str(trial), side) for side in sides])
            after_first = not after_first
    rng.shuffle(pairs)
    return [run for pair in pairs for run in pair]


def discovery_preflight(repo: Path, revisions: dict[str, str], tasks: list[dict], root: Path, *,
                        opts: argparse.Namespace | None = None) -> list[dict]:
    results = []
    for variant, revision in revisions.items():
        cwd = new_worktree(repo, root, f"preflight-{variant}", revision)
        try:
            boundary = discovery_boundary(opts, cwd) if opts else None
            for task in tasks:
                expected = task["expected"][variant]
                valid = valid_expected(expected, cwd)
                sandbox_ready = boundary is None or boundary["exit_code"] == 0
                navigation = None
                if opts and valid and sandbox_ready:
                    path, symbols = next(iter(owner_symbols(expected).items()))
                    navigation = check_navigation(opts, cwd, root, f"readiness-{task['id']}-{variant}",
                                                  path, symbols[0].rsplit(".", 1)[-1])
                reason = ("read_only_sandbox_blocked" if not sandbox_ready else
                          "discovery_expected_answer_not_in_snapshot" if not valid else
                          navigation["reason"] if navigation else None)
                results.append({"variant": variant, "task_id": task["id"],
                                "status": "ready" if reason is None else "preflight_invalid",
                                "reason": reason, "sandbox_boundary": boundary, "navigation": navigation})
        finally:
            remove_worktree(repo, cwd)
    return results


def preflight(repo: Path, root: Path, revisions: dict[str, str], tasks: list[dict[str, Any]],
              timeout: int, *, verifier: IsolatedVerifier | None = None,
              manifest_sha: str = "") -> list[dict]:
    results = []
    for variant, revision in revisions.items():
        for task in tasks:
            entry = {"task_id": task["id"], "variant": variant, "status": "preflight_invalid",
                     "setup_checks": [], "success_checks": [], "regression_checks": []}
            results.append(entry)
            if not task["_contracts"]:
                entry["reason"] = "missing_expected_failure_contract"
                continue
            cwd = new_worktree(repo, root, f"preflight-{variant}-{task['id']}", revision)
            try:
                if verifier:
                    try:
                        checks = verifier.verify(cwd, task, revision, manifest_sha, timeout,
                                                 protected=protected_digests(cwd, verifier.protected), phase="baseline")
                    except (ValueError, OSError, subprocess.SubprocessError):
                        entry["reason"] = "vm_or_artifact_invalid"
                        continue
                    entry["verification"] = checks
                    if checks["status"] != "ready":
                        entry["reason"] = checks["status"]
                        continue
                    for key in ("setup_checks", "success_checks", "regression_checks"):
                        entry[key] = checks[key]
                else:
                    entry["setup_checks"] = execute_checks(task.get("setup_commands", []), cwd, timeout)
                    if entry["setup_checks"] and not all_pass(entry["setup_checks"]):
                        entry["reason"] = "setup_failed"
                        continue
                    if run_git(cwd, "diff", "--name-only", revision, "--"):
                        entry["reason"] = "setup_modified_frozen_tracked_files"
                        continue
                    entry["success_checks"] = execute_checks(task["success_checks"], cwd, timeout, task["_contracts"])
                    entry["regression_checks"] = execute_checks(task["regression_checks"], cwd, timeout)
                if len(entry["setup_checks"]) != len(task.get("setup_commands", [])) or (
                    entry["setup_checks"] and not all_pass(entry["setup_checks"])
                ):
                    entry["reason"] = "setup_failed"
                    continue
                if len(entry["success_checks"]) != len(task["success_checks"]) or any(
                    c["failure_reason"] != "behavioral_assertion" for c in entry["success_checks"]
                ):
                    entry["reason"] = "baseline_red_unproven"
                elif not all_pass(entry["regression_checks"]):
                    entry["reason"] = "vm_regression_not_green" if verifier else "host_regression_not_green"
                elif run_git(cwd, "diff", "--name-only", revision, "--"):
                    entry["reason"] = "verifier_modified_frozen_tracked_files"
                else:
                    entry["status"] = "ready"
            finally:
                remove_worktree(repo, cwd)
            if any(check.get("failure_reason") == "interrupted" for stage in ("setup", "success", "regression")
                   for check in entry[f"{stage}_checks"]):
                return results
    return results


DISCOVERY_PROFILE = "agent_legible_discovery_v4"
ISOLATED_PROFILE = "agent_legible_isolated_v4"
EDIT_INSTRUCTIONS = (
    "Use the restricted workspace to discover and edit the code. Do not run pytest or invoke it through a "
    "test wrapper. Any configured independent tests are run by the benchmark controller after you finish."
)
ISOLATED_INSTRUCTIONS = (
    "Use the read-only navigation tools and the sandboxed patch tool. Independent checks will run in an offline "
    "verifier VM after you finish. You have no shell or test tool."
)


def discovery_permissions(manifest: Path, cwd: Path) -> list[str]:
    denied = ", ".join(f"{json.dumps(str(path))}=\"deny\"" for path in (manifest, cwd / ".git"))
    return ["-c", f'permissions.{DISCOVERY_PROFILE}.extends=":read-only"',
            "-c", f"permissions.{DISCOVERY_PROFILE}.filesystem={{{denied}}}"]


def discovery_boundary(opts: argparse.Namespace, cwd: Path) -> dict:
    # This local Codex sandbox subcommand runs no model. Exit successfully only
    # when the manifest is unreadable and the worktree cannot be written.
    probe = """import os
import sys
try:
    with open(sys.argv[1], "rb") as manifest:
        manifest.read(1)
except PermissionError:
    if not os.access(".", os.W_OK):
        raise SystemExit(0)
raise SystemExit(1)
"""
    argv = [opts.codex, "sandbox", "--include-managed-config", *discovery_permissions(opts.manifest, cwd),
            "-P", DISCOVERY_PROFILE, "-C", str(cwd), sys.executable, "-B", "-c", probe, str(opts.manifest)]
    return execute_command(argv, cwd, opts.check_timeout)


def isolated_inputs(opts: argparse.Namespace, cwd: Path) -> list[Path]:
    vm = opts.verifier
    paths = [opts.manifest, cwd / ".git", vm.profile, vm.kernel, vm.initramfs, *map(Path, vm.external)]
    if opts.oracle_contract:
        paths.append(opts.oracle_contract.resolve())
    return list(dict.fromkeys(paths))


def isolated_permissions(opts: argparse.Namespace, cwd: Path) -> list[str]:
    rules = [json.dumps(str(path)) + '="deny"' for path in isolated_inputs(opts, cwd)]
    entries = ", ".join(['":root"="read"', '":workspace_roots"="write"', *rules])
    return ["-c", f"permissions.{ISOLATED_PROFILE}.filesystem={{{entries}}}",
            "-c", f"permissions.{ISOLATED_PROFILE}.network.enabled=false"]


def isolated_boundary(opts: argparse.Namespace, cwd: Path) -> dict:
    probe = """import errno, os, socket, sys
from pathlib import Path
for path in sys.argv[1:]:
    try:
        with Path(path).open("rb") as handle:
            handle.read(1)
    except PermissionError:
        continue
    raise SystemExit(1)
assert os.access(".", os.W_OK) and not os.access("..", os.W_OK)
try:
    socket.socket(socket.AF_INET)
except OSError as error:
    raise SystemExit(0 if error.errno in (errno.EPERM, errno.EACCES) else 1)
raise SystemExit(1)
"""
    command = [opts.codex, "sandbox", "--include-managed-config", *isolated_permissions(opts, cwd),
               "-P", ISOLATED_PROFILE, "-C", str(cwd), "--", sys.executable, "-I", "-S", "-c", probe,
               *map(str, isolated_inputs(opts, cwd))]
    return execute_command(command, cwd, opts.check_timeout)


def navigation_config(opts: argparse.Namespace, cwd: Path, journal: Path | None = None) -> list[str]:
    script = Path(__file__).resolve().with_name("discovery_navigation.py")
    server_args = ["-B", str(script), str(cwd)]
    if journal is not None:
        server_args.append(str(journal))
    profile = DISCOVERY_PROFILE if opts.mode == "discovery" else ISOLATED_PROFILE
    permissions = discovery_permissions(opts.manifest, cwd) if opts.mode == "discovery" else isolated_permissions(opts, cwd)
    return [
        *permissions, "-c", f'default_permissions="{profile}"',
        "--strict-config", "--disable", "shell_tool", "--disable", "unified_exec",
        "--disable", "apps", "--disable", "plugins", "--disable", "hooks",
        "-c", "web_search=" + json.dumps("disabled"),
        "-c", f"mcp_servers.alc_navigation.command={json.dumps(sys.executable)}",
        "-c", f"mcp_servers.alc_navigation.args={json.dumps(server_args)}",
        "-c", "mcp_servers.alc_navigation.required=true",
        "-c", "mcp_servers.alc_navigation.tool_timeout_sec=30",
        # A checkout path may contain dots that Codex interprets as override separators.
        "-c", f'projects={{{json.dumps(str(cwd))}={{trust_level="untrusted"}}}}',
    ]


def check_navigation(opts: argparse.Namespace, cwd: Path, root: Path, label: str, path: str, symbol: str) -> dict:
    journal = root / f"{label}.mcp.jsonl"
    with open(journal, "x", opener=private_opener):
        pass
    argv = [opts.codex, "app-server", *navigation_config(opts, cwd, journal)]
    result = probe_navigation(argv, cwd, root, label, opts.check_timeout, path, symbol, DISCOVERY_PROFILE)
    result["logs"]["mcp.jsonl"] = journal.name
    if result["status"] == "ready" and journal.stat().st_size == 0:
        result.update(status="blocked", reason="mcp_server_telemetry_missing", confirmed_source_contents=False)
    return result


def codex_args(opts: argparse.Namespace, cwd: Path, journal: Path | None = None) -> list[str]:
    args = [opts.codex, "-a", "never", "exec", "--json", "--ephemeral", "--cd", str(cwd)]
    if opts.mode in {"discovery", "isolated-verifier"}:
        args.extend([
            "--ignore-user-config", *navigation_config(opts, cwd, journal),
        ])
    else:
        args.extend(["--sandbox", opts.sandbox, "-c", NETWORK_DISABLED])
        if not opts.qualify_sandbox:
            args.extend(["-c", "sandbox_workspace_write.writable_roots=[]",
                         "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                         "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"])
    if opts.model:
        args.extend(["--model", opts.model])
    return [*args, "-"]


def navigation_server_counts(journal: Path) -> dict:
    counts = {"succeeded": 0, "failed": 0, "unparsed": 0}
    with journal.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
                if event["request"].get("method") == "tools/call":
                    reply = event["response"]
                    status = "failed" if "error" in reply or reply["result"].get("isError") else "succeeded"
                    counts[status] += 1
            except (KeyError, TypeError, ValueError, AttributeError):
                counts["unparsed"] += 1
    return counts


def call_agent(opts: argparse.Namespace, cwd: Path, root: Path, label: str, prompt: str,
               read_trace: Callable[[Path], dict]) -> tuple[dict, dict]:
    # Removing the private directory also cleans partial logs on exceptions.
    with tempfile.TemporaryDirectory(prefix=".capture-", dir=root) as capture:
        trace = Path(capture) / "trace.jsonl"
        stderr_file = Path(capture) / "stderr.txt"
        journal = root / f"{label}.mcp.jsonl" if opts.mode == "discovery" else None
        if journal is not None:
            with open(journal, "x", opener=private_opener):
                pass
        with trace.open("wb") as stdout, stderr_file.open("w+b") as stderr:
            agent = run_process(codex_args(opts, cwd, journal), cwd, opts.timeout, stdout=stdout, stderr=stderr, prompt=prompt)
            err, info = bounded_output(stderr)
            agent["stderr"] = info
            agent["diagnostic_excerpt"] = ", ".join(diagnostic_labels(err)) or None
        if opts.keep_raw or journal is not None:
            for source, suffix in ((trace, ".jsonl"), (stderr_file, ".stderr.txt")):
                raw_file = root / (label + suffix)
                source.chmod(0o600)
                source.replace(raw_file)
            trace = root / f"{label}.jsonl"
            agent["raw_logs"] = [trace.name, f"{label}.stderr.txt"]
            if journal is not None:
                agent["raw_logs"].append(journal.name)
                agent["navigation_server"] = navigation_server_counts(journal)
        return agent, read_trace(trace)


def smoke_evidence(trace: Path, command: list[str], trace_cwd: Path) -> dict:
    matches = []
    completed = False
    failed = False
    with trace.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
                event_type = event.get("type")
                completed |= event_type == "turn.completed"
                failed |= event_type in {"turn.failed", "error"}
                item = event.get("item", {})
                if event_type != "item.completed" or item.get("type") != "command_execution":
                    continue
                reported_cwd = item.get("cwd")
                body = shell_body(item.get("command", "")).strip()
                if body == shlex.join(command) and item.get("status") in (None, "completed") and (
                    reported_cwd is None or Path(reported_cwd).resolve() == trace_cwd
                ):
                    matches.append({"event_id": item.get("id"), "exit_code": item.get("exit_code")})
            except (AttributeError, TypeError, ValueError):
                failed = True
    return {"matches": matches, "passed": completed and not failed and len(matches) == 1 and matches[0]["exit_code"] == 0}


def sandbox_smoke(repo: Path, root: Path, revisions: dict, tasks: list[dict], opts: argparse.Namespace) -> list[dict]:
    print("Approved sandbox smoke will consume one Codex session per task and revision. Running the first host-green regression command.", flush=True)
    results = []
    for variant, revision in revisions.items():
        for task in tasks:
            label = f"smoke-{variant}-{task['id']}"
            cwd = new_worktree(repo, root, label, revision)
            try:
                setup = execute_checks(task.get("setup_commands", []), cwd, opts.check_timeout)
                entry = {"variant": variant, "task_id": task["id"], "status": "environment_blocked",
                         "setup_checks": setup}
                results.append(entry)
                if setup and not all_pass(setup):
                    return results
                command = task["regression_checks"][0]
                agent, evidence = call_agent(opts, cwd, root, label,
                    "Run this one command exactly once from the current repository root. Do not edit files, retry, "
                    "or ask for elevated permissions. Then state whether the command exited successfully.\n\n"
                    + shlex.join(command), lambda trace: smoke_evidence(trace, command, cwd))
                entry.update({"agent": agent, "evidence": evidence})
                if agent["exit_code"] != 0 or not evidence["passed"]:
                    return results
                entry["status"] = "ready"
            finally:
                remove_worktree(repo, cwd)
    return results


def collect_run(repo: Path, root: Path, task: dict[str, Any], variant: str, revision: str,
                trial: str, opts: argparse.Namespace, order: int) -> dict[str, Any]:
    label = f"{task['id']}-{trial}-{variant}"
    record: dict[str, Any] = {
        "schema_version": 4, "task_id": task["id"], "trial_id": trial, "variant": variant,
        "execution_order": order, "started_at": datetime.now(timezone.utc).isoformat(),
        "behavior": "not_evaluated", "regression": "not_evaluated", "environment": "unknown",
        "agent_completion": "failed", "model_requested": opts.model, "correct": False, "verified": False,
        "model_usage_coverage": "missing", "source_attribution_coverage": "insufficient",
        "setup_checks": [], "success_checks": [], "regression_checks": [],
        "source_tokens_read": None, "source_tokens_to_owner": None,
    }
    cwd = new_worktree(repo, root, label, revision)
    print(f"Running {label}", flush=True)
    try:
        discovery = opts.mode == "discovery"
        verifier = getattr(opts, "verifier", None)
        frozen = {}
        if verifier:
            boundary = isolated_boundary(opts, cwd)
            record["isolated_agent_sandbox"] = boundary
            if boundary["exit_code"] != 0:
                record["environment"] = "blocked"
                return record
            frozen = protected_digests(cwd, verifier.protected)
        setup = execute_checks(task.get("setup_commands", []), cwd, opts.check_timeout) if opts.qualify_sandbox else []
        record["setup_checks"] = setup
        if (setup and not all_pass(setup)) or run_git(cwd, "diff", "--name-only", revision, "--"):
            record["environment"] = "blocked"
            return record
        before = modified_files(cwd, revision)
        if discovery:
            boundary = discovery_boundary(opts, cwd)
            record["discovery_sandbox_check"] = boundary
            if boundary["exit_code"] != 0:
                record["environment"] = "blocked"
                return record
            expected = task["expected"][variant]
            prompt = task["question"] + "\n" + PROMPT_FORMAT
            owners = expected["owner_paths"]
        else:
            prompt = task["prompt"]
            prompt = (ISOLATED_INSTRUCTIONS if verifier else EDIT_INSTRUCTIONS) + "\n\n" + prompt
            owners = task.get("owners", {}).get(variant, [])
        agent, summary = call_agent(opts, cwd, root, label, prompt, lambda trace:
                                    summarize_jsonl(trace, owners, repo_root=cwd, relative_cwd_known=False,
                                                    capture_answer=discovery, discovery_tools=discovery or verifier is not None))
        answer = summary.pop("_discovery_answer", None)
        record.update(summary)
        complete = agent["exit_code"] == 0 and not summary["thread_failed"]
        failure = agent["failure_reason"]
        if not complete and failure is None:
            failure = "environment_error" if agent["diagnostic_excerpt"] else "agent_error"
        agent["failure_reason"] = failure
        status = "completed" if complete else {"timeout": "timed_out", "interrupted": "interrupted"}.get(failure, "failed")
        edits = sorted(set(modified_files(cwd, revision)) - set(before))
        sources = [p for p in edits if path_category(p, cwd, cwd) == "source_read"]
        record.update({
            "agent": agent, "agent_completion": status, "codex_exit_code": agent["exit_code"],
            "codex_timed_out": agent["timed_out"], "changed_files": edits, "preagent_changed_files": before,
            "changed_source_files": sources, "changed_source_directories": len({str(Path(p).parent) for p in sources}),
            "environment": "ready" if complete else "unknown",
        })
        if failure == "environment_error":
            record["environment"] = "blocked"
        if any(e.get("environment_signals") or e.get("exit_code") == 124 and
               set(e.get("operations_detected", [])) & {"test_execution", "environment_diagnostics"}
               for e in summary["events"]):
            record["environment"] = "confounded"
        if opts.mode == "edit" and any(e.get("pytest_execution") for e in summary["events"]):
            record["agent_test_execution_observed"] = True
            record["environment"] = "confounded"
        if status == "interrupted":
            return record
        if discovery:
            invalid_reason = navigation_invalid_reason(record)
            if not invalid_reason and summary["confirmed_source_reads"] and not agent["navigation_server"]["succeeded"]:
                invalid_reason = "mcp_server_telemetry_missing"
            ignored = run_git(cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
            if edits or ignored or summary.get("file_change_events") or summary["shell_commands"] or summary["tool_calls_not_instrumented"]:
                record["environment"] = "confounded"
                record["discovery_violation"] = "Unexpected tool or checkout change during discovery"
            elif invalid_reason:
                record["environment"] = "blocked"
                record["discovery_invalid_reason"] = invalid_reason
            elif complete:
                score = score_answer(answer, expected, cwd)
                record["discovery_score"] = score
                record["discovery_semantic_score"] = score_discovery_facts(answer, expected, cwd)
                record["behavior"] = "correct" if score["passed"] else "incorrect"
                record["correct"] = score["passed"]
                # Legacy verified indicates an independent oracle, which is the offline scorer here.
                record["verified"] = score["passed"]
            return record
        if verifier:
            if summary["shell_commands"] or summary["tool_calls_not_instrumented"]:
                record["environment"] = "confounded"
                record["verification"] = {"status": "unexpected_agent_tool"}
                return record
            try:
                if frozen != protected_digests(cwd, verifier.protected):
                    raise ValueError("Protected path changed")
                evaluation = verifier.verify(cwd, task, revision, opts.manifest_sha, opts.check_timeout,
                                             protected=frozen, phase="candidate")
            except (ValueError, OSError, subprocess.SubprocessError):
                record["environment"] = "blocked"
                record["verification"] = {"status": "candidate_or_verifier_input_invalid"}
                return record
            record["verification"] = evaluation
            setup = evaluation.get("setup_checks", [])
            record["setup_checks"] = setup
            if (evaluation["status"] != "ready" or len(setup) != len(task.get("setup_commands", []))
                    or setup and not all_pass(setup)):
                record["environment"] = "blocked"
                return record
            success, regression = evaluation["success_checks"], evaluation["regression_checks"]
        else:
            if not opts.allow_host_tests or not complete or record["environment"] != "ready":
                return record
            if not opts.qualify_sandbox:
                setup = execute_checks(task.get("setup_commands", []), cwd, opts.check_timeout)
                record["setup_checks"] = setup
                if setup and not all_pass(setup):
                    record["environment"] = "blocked"
                    return record
            success = execute_checks(task["success_checks"], cwd, opts.check_timeout, task["_contracts"])
            regression = execute_checks(task["regression_checks"], cwd, opts.check_timeout,
                                        [REGRESSION_ASSERTION] * len(task["regression_checks"]))
        record.update({"success_checks": success, "regression_checks": regression})
        for stage, checks, passed, failed in (("behavior", success, "correct", "incorrect"),
                                                ("regression", regression, "passed", "failed")):
            if all_pass(checks):
                record[stage] = passed
            elif any(c["exit_code"] != 0 and c["failure_reason"] != "behavioral_assertion" for c in checks):
                record["environment"] = "confounded"
            else:
                record[stage] = failed
        record["correct"] = complete and record["behavior"] == "correct"
        record["verified"] = complete and record["regression"] == "passed"
        return record
    finally:
        if opts.keep_worktrees:
            record["worktree_retained"] = str(cwd)
        else:
            remove_worktree(repo, cwd)


def write_json(path: Path, doc: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_private(temporary, json.dumps(doc, indent=2) + "\n")
    temporary.replace(path)


def write_results(out: Path, metadata: dict, results: list[dict]) -> None:
    doc = {"schema_version": 4, "metadata": metadata, "runs": results}
    write_json(out / "results.json", doc)
    for variant in ("before", "after"):
        write_json(out / f"{variant}.json", {**doc, "runs": [r for r in results if r["variant"] == variant]})
    text, _ = render_report(doc)
    write_private(out / "report.md", text)
    (out / "results.partial.json").unlink(missing_ok=True)


def options() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True, help="Git repo to benchmark")
    p.add_argument("--manifest", type=Path, required=True, help="Frozen task manifest JSON")
    p.add_argument("--before", required=True, help="Baseline Git commit/tag")
    p.add_argument("--after", required=True, help="Refactored Git commit/tag")
    p.add_argument("--out", type=Path, required=True, help="Fresh output directory outside the repo")
    p.add_argument("--mode", choices=("edit", "discovery", "isolated-verifier"), default="edit")
    p.add_argument("--verifier-vm-profile", type=Path, help="Pinned offline QEMU image and verifier inputs")
    p.add_argument("--verifier-qemu", default="qemu-system-x86_64", help="Trusted QEMU executable")
    p.add_argument("--verifier-boot-timeout", type=int, default=120, help="QEMU boot/shutdown allowance in seconds")
    p.add_argument("--readiness-only", action="store_true", help="Run discovery or VM readiness without model sessions")
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--codex", default="codex", help="Codex executable")
    p.add_argument("--model", help="Pin the Codex model for comparability")
    p.add_argument("--timeout", type=int, default=900, help="Timeout per Codex session in seconds")
    p.add_argument("--check-timeout", type=int, default=120, help="Timeout per host check in seconds")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--keep-raw", action="store_true", help="Retain private raw JSONL and Codex stderr")
    p.add_argument("--keep-worktrees", action="store_true", help="Retain agent changes for manual review")
    p.add_argument("--skip-preflight", action="store_true", help="Skip host preflight; comparison will be inconclusive")
    p.add_argument("--dry-run", action="store_true", help="Show plan without Codex, checks or worktree creation")
    p.add_argument("--approve-manifest", action="store_true",
                   help="Approve planned Codex usage and any selected VM checks")
    p.add_argument("--allow-host-tests", action="store_true",
                   help="Explicitly authorize reviewed setup and tests to execute edited code on the development host")
    p.add_argument("--show-commands", action="store_true", help="Print exact host command arguments during dry-run review")
    p.add_argument("--sandbox-probe-python",
                   help="Opt into advanced sandbox qualification with this Python executable")
    p.add_argument("--sandbox-smoke", action="store_true", help="Approve one extra Codex session per task and revision to replay a host-green check")
    p.add_argument("--oracle-contract", type=Path, help="Optional SHA-256-bound expected-failure metadata for unchanged legacy manifests")
    return p.parse_args()


def main() -> int:
    opts = options()
    opts.sandbox = "read-only" if opts.mode == "discovery" else "workspace-write"
    isolated = opts.mode == "isolated-verifier"
    opts.qualify_sandbox = opts.mode == "edit" and bool(opts.sandbox_smoke or opts.sandbox_probe_python)
    opts.verifier = None
    try:
        if opts.mode != "edit" and (opts.allow_host_tests or opts.sandbox_probe_python):
            raise BenchmarkError("Host test opt-in and the edit sandbox probe require edit mode")
        if opts.show_commands and not opts.dry_run:
            raise BenchmarkError("--show-commands is available only with --dry-run")
        if opts.readiness_only and (opts.mode == "edit" or opts.skip_preflight or opts.sandbox_smoke):
            raise BenchmarkError("Readiness-only requires discovery or isolated verification with its normal readiness gates")
        if isolated and (not opts.verifier_vm_profile or opts.sandbox_smoke or opts.skip_preflight or opts.keep_worktrees):
            raise BenchmarkError("Isolated verification requires a VM profile and its own readiness gates and cleanup")
        if not isolated and opts.verifier_vm_profile:
            raise BenchmarkError("A VM profile requires --mode isolated-verifier")
        if min(opts.trials, opts.timeout, opts.check_timeout, opts.verifier_boot_timeout) < 1:
            raise BenchmarkError("Trials and timeouts must be positive")
        repo, out, manifest = (path.resolve() for path in (opts.repo, opts.out, opts.manifest))
        opts.manifest = manifest
        top = Path(run_git(repo, "rev-parse", "--show-toplevel")).resolve()
        if top != repo:
            raise BenchmarkError(f"Use the Git toplevel as --repo: {top}")
        git_dir = Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if any(out == protected or protected in out.parents for protected in (repo, git_dir)):
            raise BenchmarkError("--out must be outside the repo and its Git directory")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise BenchmarkError("--out must be a fresh or empty directory")
        raw = manifest.read_bytes()
        manifest_hash = hashlib.sha256(raw).hexdigest()
        if opts.mode == "discovery" and (
            repo in manifest.parents or out in manifest.parents or opts.oracle_contract or opts.sandbox_smoke
            or opts.skip_preflight
        ):
            raise BenchmarkError("Discovery requires an external manifest and its local readiness checks; "
                                 "edit oracles and sandbox test replay are unavailable")
        opts.manifest_sha = manifest_hash
        tasks = parse_manifest(manifest, raw=raw, mode="edit" if isolated else opts.mode,
                               require_checks=isolated or opts.qualify_sandbox)
        oracle_hash = bind_oracles(tasks, opts.oracle_contract, manifest_hash) if opts.mode != "discovery" else None
        revisions = {side: run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
                     for side, rev in (("before", opts.before), ("after", opts.after))}
        plan = plan_trials(tasks, opts.trials, opts.seed)
        smoke_count = 2 * len(tasks) if opts.sandbox_smoke else 0
        print(f"Repo: {repo}\nManifest: {manifest}\nRevisions: {revisions}\nManifest SHA-256: {manifest_hash}\n"
              f"Tasks: {len(tasks)}, trials: {opts.trials}, planned Codex sessions: "
              f"{0 if opts.readiness_only else len(plan)} plus {smoke_count} smoke")
        for index, (task, trial, variant) in enumerate(plan, 1):
            print(f"  {index}. {task['id']} / {trial} / {variant}")
        missing = [task["id"] for task in tasks if (isolated or opts.qualify_sandbox) and not task["_contracts"]]
        if isolated:
            print("Independent checks run only in disposable offline VMs; no model smoke is scheduled.")
        if missing:
            print("Missing expected_failure contracts: " + ", ".join(missing))
        if opts.mode == "edit":
            if opts.allow_host_tests:
                print("Host execution authorized: reviewed manifest commands can execute edited code outside the Codex sandbox.")
                if not opts.qualify_sandbox:
                    print("Host setup and checks run after each completed Codex session.")
            else:
                print("Host commands skipped unless explicitly authorized with --allow-host-tests.")
            if opts.qualify_sandbox:
                print(f"Advanced sandbox gate uses {opts.sandbox_probe_python or sys.executable}; requires --allow-host-tests.")
        if opts.dry_run:
            if opts.mode == "discovery":
                print("Read-only discovery. Local MCP content check first; no application tests. Raw logs retained privately.")
                print("After a successful one-session navigation smoke, approve live discovery with --approve-manifest.")
            if opts.show_commands:
                for task in tasks:
                    for stage in ("setup_commands", "success_checks", "regression_checks"):
                        for argv in task.get(stage, []):
                            print(f"  {task['id']} / {stage} / {shlex.join(argv)}")
            print("DRY RUN: no worktrees, checks or Codex processes started")
            return 0
        if not opts.approve_manifest and not (opts.readiness_only and opts.mode == "discovery"):
            raise BenchmarkError("Authorize planned model usage and selected VM checks with --approve-manifest")
        if opts.qualify_sandbox and not opts.allow_host_tests:
            raise BenchmarkError("Advanced edit qualification requires explicit --allow-host-tests")
        if opts.sandbox_smoke and opts.skip_preflight:
            raise BenchmarkError("Sandbox smoke requires host preflight")
        executable = shutil.which(opts.codex)
        if not executable:
            raise BenchmarkError("Codex CLI executable unavailable")
        opts.codex = str(Path(executable).absolute())
        if opts.qualify_sandbox:
            python = shutil.which(opts.sandbox_probe_python or sys.executable)
            if not python:
                raise BenchmarkError("Python executable for the sandbox runtime check is unavailable")
            opts.sandbox_probe_python = str(Path(python).absolute())
        out.mkdir(parents=True, mode=0o700, exist_ok=True)
        out.chmod(0o700)
        settings = {"mode": opts.mode, "workflow": "advanced" if isolated or opts.qualify_sandbox else "simple",
                    "sandbox_mode": opts.sandbox, "model_requested": opts.model,
                    "agent_timeout_seconds": opts.timeout, "check_timeout_seconds": opts.check_timeout,
                    "json": True, "ephemeral": True, "approval_policy": "never",
                    "sandbox_probe_python": opts.sandbox_probe_python if opts.mode == "edit" else None,
                    "workspace_network_access": False,
                    "verification_environment": "isolated_qemu" if isolated else "offline_discovery" if opts.mode == "discovery"
                    else "host" if opts.allow_host_tests else "none",
                    "tools": {"discovery": "read_only_navigation_mcp",
                              "isolated-verifier": "navigation_mcp_and_sandboxed_patch"}.get(opts.mode, "configured_codex_tools")}
        meta = {
            **settings, "tool": "agent-legible-codebase v4", "revisions": revisions, "manifest_sha256": manifest_hash,
            "oracle_contract_sha256": oracle_hash, "trials_per_task": opts.trials, "seed": opts.seed,
            "task_ids": [t["id"] for t in tasks], "holdout_tasks": [t["id"] for t in tasks if t.get("holdout") is True],
            "planned_run_count": 0 if opts.readiness_only else len(plan),
            "planned_model_session_count": 0 if opts.readiness_only else len(plan) + smoke_count,
            "planned_order": [
                {"task_id": t["id"], "trial_id": trial, "variant": v} for t, trial, v in plan],
            "raw_logs_retained": opts.keep_raw or opts.mode == "discovery",
            "study_kind": "instrumentation_pilot" if opts.trials == 1 else "performance_study",
            "codex_cli_version": resolve_cli_version(opts.codex, out, opts.check_timeout),
            "git_version": run_git(repo, "--version"),
            "configuration": configuration_fingerprint(opts.codex, settings), "preflight_status": "skipped",
            "sandbox_smoke_status": "not_requested", "experiment_status": "running",
        }
        results: list[dict] = []
        try:
            if isolated:
                try:
                    opts.verifier = IsolatedVerifier(opts.verifier_vm_profile, opts.verifier_qemu, out,
                                                     boot_timeout=opts.verifier_boot_timeout)
                    vm = opts.verifier
                    inputs = [manifest, vm.profile, vm.kernel, vm.initramfs, *map(Path, vm.external)]
                    if opts.oracle_contract:
                        inputs.append(opts.oracle_contract.resolve())
                    if any(repo in path.parents or out in path.parents for path in inputs):
                        raise ValueError("VM and oracle inputs must be external to both repo and output")
                    meta["verifier_readiness"] = vm.probe()
                    if meta["verifier_readiness"]["status"] == "ready":
                        boundary_cwd = new_worktree(repo, out, "isolated-agent-boundary", revisions["before"])
                        try:
                            meta["isolated_agent_boundary"] = isolated_boundary(opts, boundary_cwd)
                        finally:
                            remove_worktree(repo, boundary_cwd)
                except (ValueError, OSError, subprocess.SubprocessError):
                    meta["verifier_readiness"] = {"status": "vm_configuration_or_artifact_invalid"}
                if (meta["verifier_readiness"]["status"] != "ready"
                        or (meta.get("isolated_agent_boundary") or {}).get("exit_code") != 0):
                    meta["experiment_status"] = "environment_blocked"
                    return 2
            if opts.qualify_sandbox:
                with tempfile.TemporaryDirectory(prefix=".sandbox-runtime-", dir=out) as temp:
                    runtime = check_runtime(opts.codex, opts.sandbox_probe_python, Path(temp))
                meta["sandbox_runtime"] = runtime
                if runtime["status"] != "ready":
                    meta["experiment_status"] = "environment_blocked"
                    print(f"Sandbox runtime blocked: {runtime['reason']}. No model sessions started.", file=sys.stderr)
                    return 2
            if (isolated or opts.qualify_sandbox or opts.mode == "discovery") and not opts.skip_preflight:
                checks = (discovery_preflight(repo, revisions, tasks, out, opts=opts) if opts.mode == "discovery"
                          else preflight(repo, out, revisions, tasks, opts.check_timeout, verifier=opts.verifier,
                                         manifest_sha=manifest_hash))
                meta["verifier_preflight" if isolated else "discovery_preflight" if opts.mode == "discovery"
                     else "host_preflight"] = checks
                meta["preflight_status"] = "ready" if len(checks) == 2 * len(tasks) and all(
                    c["status"] == "ready" for c in checks) else "preflight_invalid"
                if meta["preflight_status"] != "ready":
                    meta["experiment_status"] = "environment_blocked" if isolated and any(
                        c.get("reason") in {"vm_or_artifact_invalid", "vm_timeout", "vm_launch_failed",
                                           "artifact_mismatch", "guest_isolation_failed", "guest_job_failed",
                                           "invalid_guest_result"} or any(
                            stage_result.get("failure_reason") in {"timeout", "environment_error"}
                            for stage in ("setup_checks", "success_checks", "regression_checks")
                            for stage_result in c.get(stage, [])
                        ) for c in checks
                    ) else "invalid"
                    return 2
            if opts.readiness_only:
                meta["experiment_status"] = "readiness_checked"
                return 0
            if opts.sandbox_smoke:
                checks = sandbox_smoke(repo, out, revisions, tasks, opts)
                meta["sandbox_smoke"] = checks
                meta["sandbox_smoke_status"] = "ready" if len(checks) == 2 * len(tasks) and all(
                    c["status"] == "ready" for c in checks) else "environment_blocked"
                if meta["sandbox_smoke_status"] != "ready":
                    meta["experiment_status"] = "environment_blocked"
                    return 2
            for index, (task, trial, variant) in enumerate(plan, 1):
                if configuration_fingerprint(opts.codex, settings) != meta["configuration"]:
                    meta["experiment_status"] = "environment_confounded"
                    return 2
                run = collect_run(repo, out, task, variant, revisions[variant], trial, opts, index)
                results.append(run)
                write_json(out / "results.partial.json", {"schema_version": 4, "metadata": meta, "runs": results})
                if run["environment"] in {"blocked", "confounded"}:
                    meta["experiment_status"] = ("invalid" if run.get("discovery_invalid_reason") else
                                                 f"environment_{run['environment']}")
                    return 2
                if run["agent_completion"] == "interrupted":
                    meta["experiment_status"] = "interrupted"
                    return 130
            meta["experiment_status"] = "completed"
            return 0
        except KeyboardInterrupt:
            meta["experiment_status"] = "interrupted"
            return 130
        except (BenchmarkError, OSError, subprocess.SubprocessError):
            meta["experiment_status"] = "invalid"
            raise
        finally:
            write_results(out, meta, results)
            print(f"Report: {out / 'report.md'}")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
