# Workspace-write Python runtime incompatibility

## Reproduce without a model or application code

Run from the skill repository. The wrapper runs the same minimal Python program on the host and via the local Codex workspace-write profile. It uses isolated Python startup, an empty temporary working directory, and a four-second external timeout per invocation. It does not run `codex exec` or import application code.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sandbox_runtime.py --python "$(uv python find 3.14.6)"
```

The program creates an anonymous local `socket.socketpair()`, sends and receives one byte, then awaits `asyncio.to_thread()`. The worker emits a marker before returning. There is no asyncio timer that could accidentally wake a stuck selector. Exit 0 means both sides completed. Exit 2 means the runtime gate blocked.

| Python | Outside the sandbox | Codex CLI 0.171.0, workspace-write with networking disabled |
|---|---|---|
| 3.14.6 | Socket send and receive succeeded. The worker returned, the await resumed, and asyncio closed. | Socket send returned `EPERM` (errno 1). The worker finished, the await never resumed, and the parent enforced its deadline. |
| 3.10.12 | Same successful control. | Same permission error and wakeup stall. |

Linux implements `socket.send()` through `sendto` on this path. Codex 0.171.0's tagged restricted seccomp policy permits anonymous AF_UNIX pair creation and unconditionally denies `SYS_sendto`. The Unix asyncio event loop uses the connected pair to wake its selector from another thread. If its write fails, the worker can finish while the awaiting event loop remains asleep. This matches the two open upstream issues. The issue's proposed destination-aware rule is absent from the installed release's restricted rule. [Codex 0.171.0 seccomp policy](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/linux-sandbox/src/seccomp.rs#L223-L251) · [Issue #15053](https://github.com/openai/codex/issues/15053) · [Issue #33793](https://github.com/openai/codex/issues/33793)

No supported local-IPC-only switch was verified for the installed restricted workspace profile. The tagged source has a separate managed proxy policy, which changes network routing rather than exposing a local wakeup toggle. A network-enabled run was unnecessary to establish the failure and was left untested. A supported release whose restricted profile passes the standalone probe is needed to qualify tests inside that sandbox. Confirm the exact sandbox regression command after that before approving a model pilot. [Codex 0.171.0 network mode selection](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/linux-sandbox/src/seccomp.rs#L103-L122)

## Benchmark gate

Only advanced edit qualification runs the local workspace-write probe. The default edit path tells Codex to skip pytest and leaves host checks off until explicitly authorized. The runner invokes the standalone sandbox locally and explicitly pins network access off for both the probe and edit sessions. Select the same Python runtime as your tests with `--sandbox-probe-python /path/to/python`. The default is the runner's Python. Discovery has a separate read-only check.

A successful host control and the sandbox permission error or completed worker with a stalled loop produce `environment_blocked` with reason `sandbox_python_local_ipc`. Failure to launch or validate the host or sandbox probe has its own diagnostic reason. Blocked runs write an `INVALID` report with zero model runs and no inference about application behavior or architectural efficiency. The probe covers local socket I/O and asyncio thread completion. The exact application tests and approved model smoke remain separate gates.

## Isolated verifier

[Isolated-verifier mode](isolated-verifier.md) now implements a separate, offline QEMU verification path. It preserves the optional advanced runtime gate. The historical Sales model-smoke plan remains blocked until the restricted sandbox can pass the exact regression command.
