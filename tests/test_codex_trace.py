"""Codex JSONL event fixtures: deterministic parser tests, no network/CLI needed."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_trace import command_kind, estimate_output_units, summarize_jsonl


def command(item_id: str, cmd: str, output: str, exit_code=0) -> dict:
    return {"type": "item.completed", "item": {"id": item_id, "type": "command_execution", "command": cmd,
                                              "aggregated_output": output, "exit_code": exit_code, "status": "completed"}}


class TraceTests(unittest.TestCase):
    def parse(self, entries: list[dict], owners=None) -> dict:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in entries) + "\n", encoding="utf-8")
            return summarize_jsonl(path, owners, repo_root=Path(td))

    def test_normal_navigation_and_model_usage(self):
        events = [
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "item.started", "item": {"id": "s", "type": "command_execution", "command": "rg discount"}},
            command("s", "rg -n discount src", "src/checkout/discounts.ts:7:export function discount() {}\n"),
            command("r", "sed -n '1,20p' src/checkout/discounts.ts", "export function discount() { return 1; }\n"),
            {"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 100}},
        ]
        result = self.parse(events, ["src/checkout/discounts.ts"])
        self.assertTrue(result["owner_path_was_seen"])
        self.assertEqual(result["owner_steps"], 1)
        self.assertEqual(result["total_input_tokens"], 1200)
        self.assertEqual(result["cached_input_tokens"], 200)
        self.assertEqual(result["files_opened"], ["src/checkout/discounts.ts"])
        self.assertTrue(result["trace_complete_for_source_proxy"])
        self.assertIsNone(result["source_tokens_to_owner"])
        self.assertEqual(result["events"][0]["source_output_proxy_units"], estimate_output_units(events[2]["item"]["aggregated_output"]))

    def test_non_owner_reads_before_owner_are_not_labelled_wrong(self):
        entries = [command("a", "cat src/misc.py", "value = 1\n"),
                   command("b", "cat src/owner.py", "correct = 2\n"),
                   {"type": "turn.completed", "usage": {"input_tokens": 12}}]
        result = self.parse(entries, ["src/owner.py"])
        self.assertEqual(result["non_owner_reads_before_owner"], ["src/misc.py"])
        self.assertEqual(result["owner_first_mention_kind"], "read")

    def test_duplicate_completed_not_counted_twice(self):
        a = command("same", "rg -n coupon", "src/coupon.ts:4:coupon\n")
        result = self.parse([a, a, {"type": "turn.completed", "usage": {"input_tokens": 2}}])
        self.assertEqual(result["navigation_steps"], 1)

    def test_empty_completed_output_not_silently_zero(self):
        result = self.parse([command("a", "cat src/model.py", ""),
                             {"type": "turn.completed", "usage": {"input_tokens": 123}}], ["src/model.py"])
        self.assertFalse(result["trace_complete_for_source_proxy"])
        self.assertIsNone(result["source_tokens_read"])
        self.assertEqual(result["total_input_tokens"], 123)

    def test_mcp_call_blocks_proxy(self):
        result = self.parse([{"type": "item.completed", "item": {"id": "x", "type": "mcp_tool_call"}}])
        self.assertFalse(result["trace_complete_for_source_proxy"])
        self.assertEqual(result["tool_calls_not_instrumented"], 1)

    def test_missing_telemetry_is_null(self):
        result = self.parse([command("r", "rg hi", "lib/a.rs:1:hi\n")], ["lib/other.rs"])
        self.assertIsNone(result["total_input_tokens"])
        self.assertIsNone(result["owner_steps"])
        self.assertFalse(result["owner_path_was_seen"])

    def test_repeated_searches_are_observed(self):
        result = self.parse([command("1", "rg -n coupon", "src/a.ts:1:coupon\n"),
                             command("2", "rg -n coupon", "src/a.ts:1:coupon\n"),
                             command("3", "rg -n checkout", "src/b.ts:1:checkout\n")])
        self.assertEqual(result["repeated_searches"], 1)
        self.assertEqual(result["search_steps"], 3)

    def test_updated_output_fallback(self):
        result = self.parse([{"type": "item.updated", "item": {"id": "r", "type": "command_execution", "aggregated_output": "code\n"}},
                             command("r", "cat src/model.py", ""),
                             {"type": "turn.completed", "usage": {"input_tokens": 100}}])
        self.assertFalse(result["trace_complete_for_source_proxy"])
        self.assertEqual(result["events"][0]["output_status"], "partial")
        self.assertIsNone(result["source_tokens_read"])

    def test_truncated_without_completed_turn_is_incomplete(self):
        result = self.parse([command("a", "cat src/model.py", "some code\n")])
        self.assertTrue(result["thread_failed"])
        self.assertIsNone(result["source_tokens_read"])

    def test_mixed_listing_and_external_instructions(self):
        result = self.parse([command("x", "bash -lc 'rg --files; cat /external/skills/tool/SKILL.md'",
                                     "AGENTS.md\nLarge instructions")])
        event = result["events"][0]
        self.assertEqual(event["category"], "mixed_unattributed")
        self.assertEqual(event["operations_detected"], ["file_listing", "agent_instructions"])
        self.assertIsNone(event["source_output_proxy_units"])
        self.assertIn("AGENTS.md", event["paths_mentioned"])

    def test_external_instructions_only_have_no_source_exposure(self):
        for cmd in ("cat /external/skills/tool/SKILL.md",
                    "bash -lc 'for skill in first second; do cat /external/skills/$skill/SKILL.md; done'"):
            result = self.parse([command("x", cmd, "instructions"),
                                 {"type": "turn.completed", "usage": {}}])
            self.assertEqual(result["events"][0]["category"], "agent_instructions")
            self.assertEqual(result["source_output_bytes"], 0)
            self.assertEqual(result["agent_instruction_output_bytes"], 12)

    def test_mixed_output_does_not_destroy_other_observations(self):
        result = self.parse([command("x", "cat src/a.py && python -m pytest", "both"),
                             command("y", "sed -n '1,2p' src/b.py", "é = 1\n"),
                             {"type": "turn.completed", "usage": {
                                 "input_tokens": 120, "cached_input_tokens": 90, "output_tokens": 4}}])
        self.assertIsNone(result["events"][0]["source_output_bytes"])
        self.assertEqual(result["observed_source_output_bytes"], 7)
        self.assertIsNone(result["source_output_bytes"])
        self.assertEqual(result["source_attribution_coverage"], "partial")
        self.assertEqual(result["model_usage_coverage"], "complete_for_reported_model_usage")
        self.assertEqual(result["uncached_input_tokens"], 30)

    def test_truncation_is_unknown_even_with_output(self):
        entry = command("x", "cat src/a.py", "value = 1")
        entry["item"]["output_truncated"] = True
        result = self.parse([entry, {"type": "turn.completed", "usage": {}}])
        self.assertEqual(result["source_attribution_coverage"], "insufficient")
        self.assertIsNone(result["events"][0]["source_output_bytes"])

    def test_codex_truncation_banner_is_not_source(self):
        result = self.parse([command("x", "cat src/a.py",
                                     "Warning: truncated output (original token count: 1344)\\ncontents"),
                             {"type": "turn.completed"}])
        self.assertEqual(result["events"][0]["output_status"], "truncated")
        self.assertIsNone(result["events"][0]["source_output_bytes"])

    def test_path_listing_is_not_contents(self):
        result = self.parse([command("x", "rg --files", "src/a.py\n"),
                             {"type": "turn.completed", "usage": {}}], ["src/a.py"])
        self.assertTrue(result["owner_path_was_seen"])
        self.assertEqual(result["files_opened"], [])
        self.assertEqual(result["source_output_bytes"], 0)

    def test_opaque_command_and_external_source_have_unknown_scope(self):
        for cmd in ("cat /external/value.py", "cat src/a.py | head", "python -c 'print(42)'",
                    "cat src/a.py$(echo extra)", "cat src/*.py", "cat src/a.py src/b.py",
                    "cat src/a.py --help", "sed -n '1e whoami' src/a.py"):
            result = self.parse([command("x", cmd, "value = 3"), {"type": "turn.completed"}])
            self.assertIsNone(result["source_output_bytes"], cmd)

    def test_git_patches_and_blobs_are_unattributed(self):
        for cmd in ("git show HEAD:src/a.py", "git diff -- src/a.py", "git log --stat -p"):
            result = self.parse([command("x", cmd, "value = 3"), {"type": "turn.completed"}])
            self.assertEqual(result["events"][0]["category"], "git_inspection")
            self.assertIsNone(result["source_output_bytes"])
        result = self.parse([command("x", "git diff --stat", "src/a.py | 2 +-"), {"type": "turn.completed"}])
        self.assertEqual(result["source_output_bytes"], 0)

    def test_command_kinds(self):
        self.assertEqual(command_kind("rg --files src"), "list")
        self.assertEqual(command_kind("rg -n 'total' ."), "search")
        self.assertEqual(command_kind("git diff --stat"), "read")
        self.assertEqual(command_kind("sed -n '1,80p' src/a.py"), "read")


if __name__ == "__main__":
    unittest.main()
