"""Sandbox tests.

Phase 1.1a: interface-contract shape (limits/result/mount defaults, Protocol).
Phase 1.1b: NoSandbox behavior (run/timeout/truncate/never-raise), the
default-deny factory, and the workspace lifecycle.

Isolation behavior tests for the namespace backend (write-outside-workspace
denied, network egress denied, fork bomb / memory hog killed) land with
BubblewrapSandbox and only run meaningfully on the Linux CI lane.
"""

import dataclasses
import sys

import pytest

from src.sandbox import (
    Mount,
    NoSandbox,
    Sandbox,
    SandboxLimits,
    SandboxResult,
    SandboxUnavailable,
    clean_workspace,
    ensure_workspace,
    get_sandbox,
    resolve_backend_name,
    workspace_path,
)


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


# --- NoSandbox (direct host, dev-only) -------------------------------------


async def test_nosandbox_runs_command(tmp_path):
    sb = NoSandbox()
    res = await sb.run(
        [sys.executable, "-c", "print('hello-sbx')"],
        cwd=str(tmp_path),
        limits=SandboxLimits(),
    )
    assert res.exit_code == 0
    assert "hello-sbx" in res.stdout
    assert res.timed_out is False


async def test_nosandbox_honors_timeout(tmp_path):
    sb = NoSandbox()
    res = await sb.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=str(tmp_path),
        limits=SandboxLimits(timeout_s=1),
    )
    assert res.timed_out is True
    assert res.exit_code != 0


async def test_nosandbox_truncates_output(tmp_path):
    sb = NoSandbox()
    res = await sb.run(
        [sys.executable, "-c", "print('x' * 100)"],
        cwd=str(tmp_path),
        limits=SandboxLimits(max_output_bytes=10),
    )
    assert res.truncated is True
    assert len(res.stdout) <= 10


async def test_nosandbox_never_raises_on_bad_command(tmp_path):
    sb = NoSandbox()
    res = await sb.run(
        ["definitely-not-a-real-binary-xyz"],
        cwd=str(tmp_path),
        limits=SandboxLimits(),
    )
    assert res.exit_code == -1
    assert res.stderr  # a clear error string, not an exception


# --- factory (default-deny) ------------------------------------------------


def test_factory_none_returns_nosandbox():
    assert isinstance(get_sandbox("none"), NoSandbox)


def test_factory_auto_is_default_deny_until_backends_land():
    # Only NoSandbox is implemented; auto resolves to a per-OS backend that
    # is not registered yet, so it must REFUSE, never fall back to no-isolation.
    with pytest.raises(SandboxUnavailable):
        get_sandbox("auto")


def test_factory_unimplemented_backend_raises():
    with pytest.raises(SandboxUnavailable):
        get_sandbox("bubblewrap")


def test_resolve_backend_name_auto_maps_per_os():
    name = resolve_backend_name("auto")
    if sys.platform.startswith("linux"):
        assert name == "bubblewrap"
    else:
        assert name == "pathjail"
    assert resolve_backend_name("none") == "none"


# --- workspace lifecycle ---------------------------------------------------


def test_ensure_workspace_creates_dir():
    path = ensure_workspace("session-abc")
    import os

    assert os.path.isdir(path)
    assert path.endswith("session-abc")
    clean_workspace("session-abc")
    assert not os.path.exists(path)


def test_workspace_sanitizes_traversal():
    # A hostile session id must not escape the workspaces root.
    p = workspace_path("../../etc/passwd")
    parts = p.parts
    assert ".." not in parts
    # the leaf is a single sanitized segment
    assert "/" not in p.name and "\\" not in p.name
