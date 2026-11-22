"""Exercise the live smoke acceptance criteria with offline traces."""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from discovery_smoke import SOURCE_FILE, SYMBOL, check_trace
from benchmark_codex import navigation_config


def tool(name, arguments, output):
    return {"type": "item.completed", "item": {
        "id": name, "type": "mcp_tool_call", "server": "alc_navigation", "tool": name,
        "arguments": arguments, "status": "completed",
        "result": {"content": [{"type": "text", "text": output}], "isError": False},
    }}


class DiscoverySmokeTests(unittest.TestCase):
    def test_dotted_fixture_path_is_an_inline_project_key(self):
        repo = Path("/tmp/alc-smoke/.fixture-123/repo")
        options = argparse.Namespace(mode="discovery", manifest=repo.parent / "controller-only.json")
        args = navigation_config(options, repo)
        overrides = [value for value in args if value.startswith(("projects.", "projects="))]
        self.assertEqual(overrides, [f'projects={{{json.dumps(str(repo))}={{trust_level="untrusted"}}}}'])

    def test_requires_search_read_and_correct_model_answer(self):
        witness = "random-value"
        search = tool("search", {"query": SYMBOL}, f"{SOURCE_FILE}:1:def {SYMBOL}():")
        read = tool("read", {"path": SOURCE_FILE}, f"def {SYMBOL}():\n    return {witness!r}\n")
        answer = {"type": "item.completed", "item": {"type": "agent_message", "text": witness}}
        turn = {"type": "turn.completed", "usage": {"input_tokens": 42, "cached_input_tokens": 0,
                                                   "output_tokens": 7}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace = root / "trace.jsonl"
            for events, expected in (
                ([search, read, answer, turn], True),
                ([search, answer, turn], False),
                ([read, search, answer, turn], False),
                ([search, read, turn], False),
                ([search, tool("read", {"path": SOURCE_FILE}, "unrelated"), answer, turn], False),
            ):
                trace.write_text("".join(json.dumps(event) + "\n" for event in events))
                self.assertEqual(all(check_trace(trace, root, witness)["checks"].values()), expected)

    def test_dry_run_and_no_approval_never_execute_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results"
            command = [sys.executable, str(SCRIPTS / "discovery_smoke.py"), "--codex", "/missing",
                       "--model", "test", "--out", str(out)]
            dry = subprocess.run([*command, "--dry-run"], capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("Planned live sessions 1", dry.stdout)
            denied = subprocess.run(command, capture_output=True, text=True)
            self.assertIn("requires --approve-model-session", denied.stderr)
            self.assertFalse(out.exists())
