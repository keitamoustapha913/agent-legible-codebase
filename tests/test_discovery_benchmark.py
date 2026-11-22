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
argv = json.loads(next(a.split('=', 1)[1] for a in args if a.startswith('mcp_servers.alc_navigation.args=')))
if args[0] == 'app-server':
    assert 'turn/start' not in sys.argv
    for line in sys.stdin:
        request = json.loads(line)
        method = request['method']
        if method == 'initialize':
            result = {}
        elif method == 'initialized':
            continue
        elif method == 'thread/start':
            result = {'thread': {'id': 'local'}, 'sandbox': {'type': 'readOnly', 'networkAccess': False},
                      'approvalPolicy': 'never', 'activePermissionProfile': {'id': 'agent_legible_discovery_v4'}}
        elif method == 'mcpServerStatus/list':
            result = {'data': [{'name': 'alc_navigation', 'runtimeStatus': 'connected',
                                'tools': dict.fromkeys(('list_files', 'search', 'read'))}]}
        elif method == 'mcpServer/tool/call':
            params = request['params']
            mcp = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': params['tool'], 'arguments': params['arguments']}}
            server = subprocess.run([sys.executable, *argv], input=json.dumps(mcp) + '\\n', capture_output=True,
                                    text=True, check=True)
            result = json.loads(server.stdout)['result']
            if Path(sys.argv[0]).with_suffix('.fail-readiness').exists() and params['tool'] == 'read':
                result = {'content': [{'type': 'text', 'text': 'Wrong source contents'}]}
        else:
            raise AssertionError(method)
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
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
request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
           'params': {'name': 'read', 'arguments': {'path': 'owner.py', 'start': None, 'end': None}}}
server = subprocess.run([sys.executable, *argv], input=json.dumps(request) + '\\n', capture_output=True, text=True, check=True)
reply = json.loads(server.stdout)['result']
assert 'calculate_rebate' in reply['content'][0]['text']
assert 'tasks.json' not in prompt
assert 'scripts/discovery_oracle.py' not in ' '.join(sys.argv)
if 'try to change' in prompt:
    Path('owner.py').write_text('changed by fake')
if 'missing result' in prompt:
    reply = None
failed = 'failed tool' in prompt
if 'server rejected request' in prompt:
    reply = {'isError': True, 'content': [{'type': 'text', 'text': 'fixture file not tracked'}]}
answer = json.dumps({
    'owners': [{'path': 'owner.py', 'symbols': ['calculate_rebate']}],
    'dependency_paths': ['private_dependencies.py'],
    'rationale': 'Applies eligibility rules to the rebate.',
})
for event in [
    {'type': 'item.completed', 'item': {'id': 'read', 'type': 'mcp_tool_call', 'server': 'alc_navigation',
                                       'tool': 'read', 'arguments': {'path': 'owner.py'}, 'result': reply,
                                       'status': 'failed' if failed else 'completed',
                                       'error': {'message': 'fixture transport failure'} if failed else None}},
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
            failures = {"missing result": "missing_tool_result", "failed tool": "codex_tool_error",
                        "server rejected request": "mcp_returned_error"}
            for attempt in ("healthy", "try to change", *failures, "readiness failure"):
                with self.subTest(attempt=attempt):
                    task["question"] = "Where is the authoritative eligibility adjustment? " + attempt
                    if attempt == "readiness failure":
                        fake.with_suffix(".fail-readiness").touch()
                    manifest.write_text(json.dumps({"tasks": [task]}))
                    out = root / f"result-{attempt}"
                    proc = subprocess.run(argv + ["--out", str(out)], capture_output=True, text=True, timeout=45)
                    self.assertEqual(proc.returncode, 0 if attempt == "healthy" else 2, proc.stdout + proc.stderr)
                    data = json.loads((out / "results.json").read_text())
                    self.assertEqual(data["metadata"]["sandbox_mode"], "read-only")
                    self.assertEqual((repo / "owner.py").read_text(), source)
                    if attempt in failures or attempt == "readiness failure":
                        self.assertIn("**INVALID**", (out / "report.md").read_text())
                    if attempt == "readiness failure":
                        self.assertEqual(data["runs"], [])
                        self.assertEqual(data["metadata"]["discovery_preflight"][0]["reason"], "mcp_read_result_unusable")
                        continue
                    self.assertTrue(all(check["navigation"]["confirmed_source_contents"]
                                        for check in data["metadata"]["discovery_preflight"]))
                    for run in data["runs"]:
                        self.assertEqual(run["regression"], "not_evaluated")
                        self.assertEqual(run["regression_checks"], [])
                        self.assertIsNone(run.get("_discovery_answer"))
                        for raw in run["agent"]["raw_logs"]:
                            self.assertEqual((out / raw).stat().st_mode & 0o777, 0o600)
                        journal = out / run["agent"]["raw_logs"][-1]
                        self.assertIn("calculate_rebate", journal.read_text())
                        self.assertEqual(run["agent"]["navigation_server"]["succeeded"], 1)
                        if attempt in failures:
                            self.assertEqual(run["confirmed_source_reads"], 0)
                            self.assertEqual(run["behavior"], "not_evaluated")
                            self.assertEqual(run["events"][0]["navigation_failure"], failures[attempt])
                        elif attempt == "try to change":
                            self.assertEqual(run["environment"], "confounded")
                            self.assertFalse(run["correct"])
                        else:
                            self.assertEqual(run["confirmed_source_reads"], 1)
                            self.assertTrue(run["discovery_score"]["passed"])
                            self.assertEqual(run["source_attribution_coverage"], "complete")
