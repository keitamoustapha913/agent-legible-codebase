"""Exercise the discovery CLI without an actual Codex session."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

FAKE = '''
import json, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli fake-1')
    raise SystemExit(0)
if args[0] == 'sandbox':
    assert '--include-managed-config' in args
    assert any('extends=":read-only"' in a for a in args)
    assert any('tasks.json"="deny"' in a for a in args)
    raise SystemExit(0)
assert any('extends=":read-only"' in a for a in args)
assert any('tasks.json"="deny"' in a for a in args)
assert any(a.startswith('default_permissions=') for a in args)
assert '--ephemeral' in args and '--ignore-user-config' in args
assert {'shell_tool', 'unified_exec', 'apps', 'plugins'} <= {args[i+1] for i, a in enumerate(args) if a == '--disable'}
assert any(a == 'mcp_servers.alc_navigation.required=true' for a in args)
prompt = sys.stdin.read()
assert 'private_dependencies.py' not in prompt
assert 'owner.py' not in prompt
assert 'calculate_rebate' not in prompt
assert 'Do not run tests' in prompt
argv = json.loads(next(a.split('=', 1)[1] for a in args if a.startswith('mcp_servers.alc_navigation.args=')))
request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'read', 'arguments': {'path': 'owner.py'}}}
server = subprocess.run([sys.executable, *argv], input=json.dumps(request) + '\\n', capture_output=True, text=True, check=True)
reply = json.loads(server.stdout)['result']
assert 'calculate_rebate' in reply['content'][0]['text']
assert 'tasks.json' not in prompt
assert 'scripts/discovery_oracle.py' not in ' '.join(sys.argv)
if 'try to change' in prompt:
    Path('owner.py').write_text('changed by fake')
answer = json.dumps({
    'owners': [{'path': 'owner.py', 'symbols': ['calculate_rebate']}],
    'dependency_paths': ['private_dependencies.py'],
    'rationale': 'Applies eligibility rules to the rebate.',
})
for event in [
    {'type': 'item.completed', 'item': {'id': 'read', 'type': 'mcp_tool_call', 'server': 'alc_navigation',
                                       'tool': 'read', 'arguments': {'path': 'owner.py'}, 'result': reply,
                                       'status': 'completed'}},
    {'type': 'item.completed', 'item': {'id': 'response', 'type': 'agent_message', 'text': answer}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 420, 'cached_input_tokens': 200, 'output_tokens': 30}},
]:
    print(json.dumps(event))
'''


class DiscoveryBenchmarkTests(unittest.TestCase):
    def test_discovery_config_prompt_and_offline_grading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            source = "def calculate_rebate():\n    return 2\n"
            (repo / "owner.py").write_text(source)
            (repo / "private_dependencies.py").write_text("ELIGIBLE = True\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                            "commit", "-qm", "fixture"], check=True)
            fake = root / "fake"
            fake.write_text("#!" + sys.executable + "\n" + FAKE)
            fake.chmod(0o755)
            manifest = root / "tasks.json"
            truth = {"owner_paths": ["owner.py"], "symbols": ["calculate_rebate"],
                     "dependency_paths": ["private_dependencies.py"]}
            task = {"id": "rebate", "question": "Where is the authoritative eligibility adjustment?",
                    "mode": "discovery", "expected": {"before": truth, "after": truth}}
            argv = [sys.executable, str(SCRIPTS / "benchmark_codex.py"), "--repo", str(repo), "--manifest", str(manifest),
                    "--before", "HEAD", "--after", "HEAD", "--mode", "discovery", "--trials", "1", "--codex", str(fake),
                    "--model", "synthetic", "--approve-manifest"]
            for attempt in (False, True):
                with self.subTest(mutation=attempt):
                    if attempt:
                        task["question"] += " try to change"
                    manifest.write_text(json.dumps({"tasks": [task]}))
                    out = root / f"result-{attempt}"
                    proc = subprocess.run(argv + ["--out", str(out)], capture_output=True, text=True, timeout=45)
                    self.assertEqual(proc.returncode, 2 if attempt else 0, proc.stdout + proc.stderr)
                    data = json.loads((out / "results.json").read_text())
                    self.assertEqual(data["metadata"]["sandbox_mode"], "read-only")
                    self.assertEqual((repo / "owner.py").read_text(), source)
                    for run in data["runs"]:
                        self.assertEqual(run["regression"], "not_evaluated")
                        self.assertEqual(run["regression_checks"], [])
                        self.assertEqual(run["confirmed_source_reads"], 1)
                        self.assertIsNone(run.get("_discovery_answer"))
                        if attempt:
                            self.assertEqual(run["environment"], "confounded")
                            self.assertFalse(run["correct"])
                        else:
                            self.assertTrue(run["discovery_score"]["passed"])
                            self.assertEqual(run["source_attribution_coverage"], "complete")
