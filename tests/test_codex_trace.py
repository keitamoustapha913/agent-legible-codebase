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
            return summarize_jsonl(path, owners)

    def test_normal_navigation_and_model_usage(self):
        events = [
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "item.started", "item": {"id": "s", "type": "command_execution", "command": "rg discount"}},
            command("s", "rg discount src", "src/checkout/discounts.ts:7:export function discount() {}\n"),
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
        self.assertEqual(result["source_tokens_to_owner"], estimate_output_units(events[2]["item"]["aggregated_output"]))

    def test_non_owner_reads_before_owner_are_not_labelled_wrong(self):
        entries = [command("a", "cat src/misc.py", "value = 1\n"),
                   command("b", "cat src/owner.py", "correct = 2\n"),
                   {"type": "turn.completed", "usage": {"input_tokens": 12}}]
        result = self.parse(entries, ["src/owner.py"])
        self.assertEqual(result["non_owner_reads_before_owner"], ["src/misc.py"])
        self.assertEqual(result["owner_first_mention_kind"], "read")

    def test_duplicate_completed_not_counted_twice(self):
        a = command("same", "rg coupon", "src/coupon.ts:4:coupon\n")
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
        result = self.parse([command("1", "rg coupon", "src/a.ts:1:coupon\n"),
                             command("2", "rg coupon", "src/a.ts:1:coupon\n"),
                             command("3", "rg checkout", "src/b.ts:1:checkout\n")])
        self.assertEqual(result["repeated_searches"], 1)
        self.assertEqual(result["search_steps"], 3)

    def test_updated_output_fallback(self):
        result = self.parse([{"type": "item.updated", "item": {"id": "r", "type": "command_execution", "aggregated_output": "code\n"}},
                             command("r", "cat src/model.py", ""),
                             {"type": "turn.completed", "usage": {"input_tokens": 100}}])
        self.assertTrue(result["trace_complete_for_source_proxy"])
        self.assertGreater(result["source_tokens_read"], 0)

    def test_truncated_without_completed_turn_is_incomplete(self):
        result = self.parse([command("a", "cat src/model.py", "some code\n")])
        self.assertTrue(result["thread_failed"])
        self.assertIsNone(result["source_tokens_read"])

    def test_command_kinds(self):
        self.assertEqual(command_kind("rg --files src"), "list")
        self.assertEqual(command_kind("rg -n 'total' ."), "search")
        self.assertEqual(command_kind("git diff --stat"), "read")
        self.assertEqual(command_kind("sed -n '1,80p' src/a.py"), "read")


if __name__ == "__main__":
    unittest.main()
