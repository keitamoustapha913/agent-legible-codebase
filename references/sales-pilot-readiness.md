# Sales benchmark readiness

The simple edit and discovery plans are ready to review. No model sessions have run under either plan. The original revisions and edit manifest remain frozen.

| Plan | Sessions for one trial | Status |
|---|---:|---|
| Simple edit | 4 | Two tasks on two revisions. No setup, host tests, sandbox probe or model smoke by default. |
| Discovery | 2 | One question on two revisions. The actual read-only sandbox and offline answer checks passed on both. |
| Isolated edit | 4 | The offline QEMU flow passed a synthetic fixture. A reviewed Sales guest image and frozen baseline checks are still needed. |

## Simple edit pilot

Run from the skill repository to preview the exact plan:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_codex.py \
  --repo /home/theyeq-admin-lap/master-workspace-2/Qworks-erp-native-register-json-recovery \
  --manifest /tmp/qworks-sales-benchmark.W7QcTc/tasks.json \
  --before d682cc8884773063b361e40354b544c0950a228f \
  --after a3ece821fa3112858175fd7c8081eec1765eeeb9 \
  --out /tmp/alc-v4-sales-simple-pilot \
  --codex /home/theyeq-admin-lap/.local/bin/codex \
  --model gpt-6.2-sol --seed 1337 --trials 1 --dry-run --show-commands
```

After approving four model sessions, replace `--dry-run --show-commands` with `--approve-manifest`. The default pilot collects telemetry and leaves correctness unevaluated. Codex is instructed to skip pytest in its restricted workspace.

To verify correctness on the host, repeat the dry-run with `--allow-host-tests --oracle-contract examples/sales-pilot.oracle.json`. Review the printed commands. Leave those two options in the approved command only if you explicitly accept that the controller will execute edited code outside Codex's sandbox. The published oracle sidecar verifies the assertion patterns without changing the frozen manifest.

## Discovery pilot

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_codex.py \
  --repo /home/theyeq-admin-lap/master-workspace-2/Qworks-erp-native-register-json-recovery \
  --manifest examples/sales.discovery.json --mode discovery \
  --before d682cc8884773063b361e40354b544c0950a228f \
  --after a3ece821fa3112858175fd7c8081eec1765eeeb9 \
  --out /tmp/alc-v4-sales-discovery-pilot \
  --codex /home/theyeq-admin-lap/.local/bin/codex \
  --model gpt-6.2-sol --seed 1337 --trials 1 --dry-run
```

After approving two model sessions, replace `--dry-run` with `--approve-manifest`. This is a discovery instrumentation check. No application test suite runs.

## Earlier environment evidence

Host checks passed on both frozen revisions. The independent date and currency oracles reached the expected failing assertions, and the original regression suite passed all 28 tests each time. The same regression command timed out in the restricted sandbox on both revisions. A standard-library fixture reproduced the local socket `EPERM` and an asyncio stall under Codex CLI 0.171.0. This affects tests inside the restricted sandbox. The simple mode leaves these tests to the optional host controller. [Runtime diagnosis](sandbox-runtime.md)

The isolated QEMU flow passed a real offline guest probe, an expected failing synthetic oracle, a passing synthetic candidate and both regression checks. A real QEMU deadline terminated the guest and removed its job disk. See [isolated-verifier setup](isolated-verifier.md) for the separate Sales VM qualification and four-session plan.
