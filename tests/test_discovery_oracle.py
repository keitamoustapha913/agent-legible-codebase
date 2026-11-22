"""Offline discovery scoring and navigation-tool boundaries."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import discovery_oracle
import discovery_navigation


class DiscoveryTests(unittest.TestCase):
    def test_navigation_catalog_advertises_only_read_only_tools(self):
        nav = discovery_navigation.Navigation(Path.cwd(), [])
        response = discovery_navigation.dispatch({"id": 1, "method": "tools/list"}, nav)
        tools = {tool["name"]: tool.get("annotations") for tool in response["result"]["tools"]}
        self.assertEqual(tools, {
            name: {"readOnlyHint": True, "openWorldHint": False}
            for name in ("list_files", "search", "read")
        })

    def test_graded_symbols_must_be_declared_in_the_submitted_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "owner.py").write_text("from other import calc\n")
            (root / "other.py").write_text("def calc():\n    return 1\n")
            expected = {"owner_paths": ["owner.py"], "symbols": ["calc"], "dependency_paths": ["other.py"]}
            answer = json.dumps({"owners": [{"path": "owner.py", "symbols": ["calc"]}],
                                 "dependency_paths": ["other.py"], "rationale": "Calculates the result."})
            self.assertFalse(discovery_oracle.score_answer(answer, expected, root)["passed"])
            (root / "owner.py").write_text("def calc():\n    return 2\n")
            score = discovery_oracle.score_answer(answer, expected, root)
            self.assertTrue(score["passed"])
            self.assertEqual(score["human_review"], "pending")

    def test_navigation_reads_only_approved_files_and_exposes_no_command_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "owner.py").write_text("def calc():\n    return 2\n")
            (root / "config").symlink_to("/etc/passwd")
            nav = discovery_navigation.Navigation(root, ["owner.py", "config"])
            self.assertIn("def calc", nav.call("read", {"path": "owner.py"}))
            self.assertIn("owner.py:1:def calc", nav.call("search", {"query": "def calc"}))
            self.assertIn("owner.py", nav.call("list_files", {"contains": None}))
            self.assertIn("owner.py:1:def calc", nav.call("search", {"query": "def calc", "contains": None}))
            self.assertEqual(nav.call("read", {"path": "owner.py", "start": None, "end": None}),
                             "def calc():\n    return 2\n")
            for path in ("../expected.json", "/etc/passwd", "config", ".git"):
                with self.assertRaises(ValueError):
                    nav.call("read", {"path": path})
            with self.assertRaises(ValueError):
                nav.call("exec", {"command": "pytest"})
            failure = discovery_navigation.dispatch({
                "id": 1, "method": "tools/call", "params": {"name": "read", "arguments": {"path": "absent.py"}},
            }, nav)["result"]
            self.assertTrue(failure["isError"])
            self.assertIn("not tracked", failure["content"][0]["text"])
