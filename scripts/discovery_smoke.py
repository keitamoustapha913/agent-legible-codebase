#!/usr/bin/env python3
"""One opt-in Codex session proving a search and a source read reached the model."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from benchmark_codex import (
    call_agent, check_navigation, discovery_boundary, modified_files, resolve_cli_version, run_git, write_json,
)
from codex_trace import summarize_jsonl
from discovery_eligibility import navigation_invalid_reason

SYMBOL = "navigation_smoke_value"
SOURCE_FILE = "sample.py"
PROMPT = (
    f"Use the read-only navigation search tool to find the symbol {SYMBOL}. "
    "Then use the read tool to open the source file and find the string it returns. "
    "Reply with exactly the returned string, without quotes or markup. Do not run tests or edit files."
)


def check_trace(trace: Path, cwd: Path, witness: str) -> dict:
    summary = summarize_jsonl(trace, repo_root=cwd, discovery_tools=True, capture_answer=True)
    answer = summary.pop("_discovery_answer")
    checks = {
        "complete_trace": not summary["thread_failed"],
        "usable_navigation": navigation_invalid_reason(summary) is None,
        "search_returned_symbol": False,
        "read_returned_witness": False,
        "model_returned_witness": isinstance(answer, str) and answer.strip() == witness,
        "read_only_tools": not any(summary[key] for key in
                                   ("shell_commands", "file_change_events", "tool_calls_not_instrumented")),
    }
    accepted = {e["event_line"]: e for e in summary["events"] if e.get("event_type") == "mcp_tool_call" and
                e["output_status"] == "available" and not e["navigation_failure"]}
    with trace.open(encoding="utf-8", errors="replace") as handle:
        for line, payload in enumerate(handle, 1):
            if line not in accepted:
                continue
            item = json.loads(payload)["item"]
            output = "".join(block["text"] for block in item["result"]["content"])
            event = accepted[line]
            if event["tool"] == "search" and any(
                match.startswith(SOURCE_FILE + ":") and SYMBOL in match for match in output.splitlines()
            ):
                checks["search_returned_symbol"] = True
            if (checks["search_returned_symbol"] and event["tool"] == "read" and
                    SOURCE_FILE in event["source_paths_read"] and witness in output):
                checks["read_returned_witness"] = True
    return {"checks": checks, "events": summary["events"], "model_usage_coverage": summary["model_usage_coverage"],
            "total_input_tokens": summary["total_input_tokens"], "output_tokens": summary["output_tokens"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Fresh directory for private evidence")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve-model-session", action="store_true")
    args = parser.parse_args()
    print(f"Read-only synthetic navigation smoke. Model {args.model}. Planned live sessions 1.")
    print("Search for a known symbol, open its file, and return a fresh string stored only in the source.")
    print("No Sales checkout or application tests. Private evidence saved under", args.out.resolve())
    if args.dry_run:
        print("Dry-run. Zero Codex processes started. Live smoke requires --approve-model-session.")
        return 0
    if not args.approve_model_session:
        parser.error("Live smoke requires --approve-model-session")
    try:
        executable = shutil.which(args.codex)
        if not executable:
            raise ValueError("Codex CLI executable unavailable")
        args.codex = str(Path(executable).absolute())
        out = args.out.resolve()
        out.mkdir(parents=True, mode=0o700)
        args.mode, args.keep_raw, args.timeout, args.check_timeout = "discovery", True, 120, 15
        record = {"status": "INVALID", "model": args.model, "model_sessions_started": 0,
                  "codex_version": resolve_cli_version(args.codex, out, args.check_timeout)}
        try:
            with tempfile.TemporaryDirectory(prefix=".fixture-", dir=out) as folder:
                root = Path(folder)
                cwd = root / "repo"
                cwd.mkdir()
                # Exercise the same grading-input denial without exposing a real manifest.
                args.manifest = root / "controller-only.json"
                args.manifest.write_text("{}\n")
                witness = "alc-" + secrets.token_hex(16)
                (cwd / SOURCE_FILE).write_text(f"def {SYMBOL}():\n    return {witness!r}\n")
                run_git(cwd, "init", "--quiet")
                run_git(cwd, "add", SOURCE_FILE)
                run_git(cwd, "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgSign=false",
                        "-c", "user.name=Navigation smoke", "-c", "user.email=smoke@example.invalid", "commit",
                        "--quiet", "-m", "Synthetic fixture")
                record["sandbox_boundary"] = discovery_boundary(args, cwd)
                if record["sandbox_boundary"]["exit_code"] != 0:
                    return 2
                record["readiness"] = check_navigation(args, cwd, out, "readiness", SOURCE_FILE, SYMBOL)
                if record["readiness"]["status"] != "ready":
                    return 2
                record["model_sessions_started"] = 1
                agent, evidence = call_agent(args, cwd, out, "smoke", PROMPT,
                                             lambda trace: check_trace(trace, cwd, witness))
                evidence["checks"]["clean_checkout"] = not (
                    modified_files(cwd, "HEAD") or run_git(cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
                )
                record.update(agent=agent, **evidence)
                if agent["exit_code"] == 0 and all(evidence["checks"].values()):
                    record["status"] = "PASS"
                return 0 if record["status"] == "PASS" else 2
        finally:
            write_json(out / "smoke.json", record)
            print(f"{record['status']} — {out / 'smoke.json'}")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
