"""Interface-contract tests for the sandbox (Phase 1.1a).

Behavior tests (write-outside-workspace denied, network denied, fork bomb / memory
hog killed, timeout honored, truncation flagged) land with the concrete backends
in later increments. These pin the public shape from contracts/sandbox.md.
"""

import dataclasses

import pytest

from src.sandbox import Mount, Sandbox, SandboxLimits, SandboxResult


def test_limits_defaults_match_contract():
    lim = SandboxLimits()
    assert lim.timeout_s == 30
    assert lim.max_output_bytes == 200_000
    assert lim.memory_mb == 512
    assert lim.pids == 256
    assert lim.cpus == 1.0


def test_result_defaults():
    r = SandboxResult(stdout="", stderr="", exit_code=0)
    assert r.timed_out is False
    assert r.truncated is False


def test_mount_defaults_read_only_and_is_frozen():
    m = Mount(source="/host/x", target="/x")
    assert m.read_only is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.read_only = False  # type: ignore[misc]


class _DummySandbox:
    async def run(self, cmd, *, cwd, limits, network=False, mounts=None):
        return SandboxResult(stdout=str(cmd), stderr="", exit_code=0)


def test_impl_satisfies_runtime_checkable_protocol():
    assert isinstance(_DummySandbox(), Sandbox)


def test_non_impl_does_not_satisfy_protocol():
    class _NotASandbox:
        pass

    assert not isinstance(_NotASandbox(), Sandbox)


async def test_run_accepts_documented_kwargs_and_returns_result():
    sb = _DummySandbox()
    res = await sb.run(
        "echo hi",
        cwd="/tmp",
        limits=SandboxLimits(),
        network=False,
        mounts=[Mount(source="/host/data", target="/data")],
    )
    assert isinstance(res, SandboxResult)
    assert res.exit_code == 0
