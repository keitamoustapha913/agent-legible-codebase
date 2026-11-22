#!/usr/bin/env python3
"""Add offline semantic scores to a copy of archived discovery results."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from benchmark_codex import new_worktree, parse_manifest, remove_worktree, run_git, write_json
from codex_trace import summarize_jsonl
from discovery_eligibility import assess_discovery_run
from discovery_oracle import score_discovery_facts
from report_codex import pair_runs, render_report, write_private


def replay(repo: Path, archive: Path, manifest: Path) -> dict:
    source = archive / "results.json"
    archived_bytes = source.read_bytes()
    doc = json.loads(archived_bytes)
    meta = doc["metadata"]
    if meta.get("mode") != "discovery":
        raise ValueError("Archived results must be a discovery benchmark")
    manifest_bytes = manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != meta.get("manifest_sha256"):
        raise ValueError("Manifest hash does not match the archived experiment")
    tasks = {task["id"]: task for task in parse_manifest(manifest, raw=manifest_bytes, mode="discovery")}
    runs = doc["runs"]
    pair_runs(runs)
    for run in runs:
        if run["task_id"] not in tasks or not re.fullmatch(r"[A-Za-z0-9_-]+", run["trial_id"]):
            raise ValueError("Invalid archived trial or unknown task")
    with tempfile.TemporaryDirectory(prefix="alc-discovery-replay-") as temp:
        for side in ("before", "after"):
            revision = meta["revisions"][side]
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) or run_git(
                repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"
            ) != revision:
                raise ValueError("Archived revision must resolve to its exact Git commit")
            selected = [run for run in runs if run["variant"] == side]
            if not selected:
                continue
            cwd = new_worktree(repo, Path(temp), side, revision)
            try:
                for run in selected:
                    if not assess_discovery_run(run).eligible:
                        run["discovery_semantic_score"] = {"status": "unscorable", "reason": "unusable_run"}
                        continue
                    trace = archive / f"{run['task_id']}-{run['trial_id']}-{side}.jsonl"
                    if not trace.is_file() or trace.is_symlink():
                        run["discovery_semantic_score"] = {"status": "unscorable", "reason": "raw_trace_unavailable"}
                        continue
                    summary = summarize_jsonl(trace, capture_answer=True)
                    run["discovery_semantic_score"] = (
                        {"status": "unscorable", "reason": "incomplete_raw_trace"} if summary["thread_failed"]
                        else score_discovery_facts(summary["_discovery_answer"], tasks[run["task_id"]]["expected"][side], cwd)
                    )
            finally:
                remove_worktree(repo, cwd)
    meta["offline_semantic_replay"] = {"original_results_sha256": hashlib.sha256(archived_bytes).hexdigest()}
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("repo", "archive", "manifest", "out"):
        parser.add_argument(f"--{argument}", type=Path, required=True)
    args = parser.parse_args()
    try:
        repo, archive, manifest, out = (getattr(args, field).resolve() for field in ("repo", "archive", "manifest", "out"))
        git_dir = Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if any(out == root or root in out.parents for root in (repo, archive, git_dir)):
            raise ValueError("Replay output must be outside the repository and original archive")
        if out.exists():
            raise ValueError("Replay output must be a fresh directory")
        doc = replay(repo, archive, manifest)
        out.mkdir(parents=True, mode=0o700)
        write_json(out / "results.json", doc)
        report, _ = render_report(doc)
        write_private(out / "report.md", report)
        print(f"Offline semantic replay report at {out / 'report.md'}")
        return 0
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
