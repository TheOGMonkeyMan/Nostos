"""Sandbox tests.

Phase 1.1a: interface-contract shape (limits/result/mount defaults, Protocol).
Phase 1.1b: NoSandbox behavior (run/timeout/truncate/never-raise), the
default-deny factory, and the workspace lifecycle.

Isolation behavior tests for the namespace backend (write-outside-workspace
denied, network egress denied, fork bomb / memory hog killed) land with
BubblewrapSandbox and only run meaningfully on the Linux CI lane.
"""

import dataclasses
import os
import shutil
import sys

import pytest

from src.sandbox import (
    BubblewrapSandbox,
    Mount,
    NoSandbox,
    PathJailSubprocess,
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

# bubblewrap isolation is verifiable only on Linux with the bwrap binary; on
# this Windows dev box these tests SKIP and only run on the ubuntu CI lane.
_BWRAP = sys.platform.startswith("linux") and shutil.which("bwrap") is not None
_requires_bwrap = pytest.mark.skipif(not _BWRAP, reason="requires Linux + bwrap")


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


def test_factory_pathjail_returns_pathjail():
    # pathjail is the weak fallback, available on every platform.
    assert isinstance(get_sandbox("pathjail"), PathJailSubprocess)


def test_factory_auto_resolves_per_os():
    if _BWRAP:
        # Linux + bwrap: auto -> bubblewrap.
        assert isinstance(get_sandbox("auto"), BubblewrapSandbox)
    elif sys.platform.startswith("linux"):
        # Linux without bwrap: the per-OS backend is unavailable -> fail closed,
        # never silently run unsandboxed.
        with pytest.raises(SandboxUnavailable):
            get_sandbox("auto")
    else:
        # mac / windows: auto -> pathjail (the weak fallback).
        assert isinstance(get_sandbox("auto"), PathJailSubprocess)


def test_factory_bubblewrap_availability_is_host_gated():
    if _BWRAP:
        assert isinstance(get_sandbox("bubblewrap"), BubblewrapSandbox)
    else:
        # Registered but unavailable on this host -> fail closed.
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


# --- BubblewrapSandbox isolation (Linux CI lane only; SKIPS elsewhere) ------
#
# These prove the security guarantees and use only shell/coreutils/iproute2,
# which live under the bound /usr - NOT the host's setup-python interpreter
# (which is outside the sandbox's bind mounts).


@_requires_bwrap
async def test_bwrap_workspace_write_allowed(tmp_path):
    sb = BubblewrapSandbox()
    res = await sb.run(
        "echo hi > out.txt && cat out.txt", cwd=str(tmp_path), limits=SandboxLimits()
    )
    assert res.exit_code == 0
    assert "hi" in res.stdout


@_requires_bwrap
async def test_bwrap_write_outside_workspace_denied(tmp_path):
    sb = BubblewrapSandbox()
    res = await sb.run("echo x > /etc/sbx_should_fail", cwd=str(tmp_path), limits=SandboxLimits())
    assert res.exit_code != 0  # host /etc is read-only inside the sandbox


@_requires_bwrap
async def test_bwrap_network_denied_by_default(tmp_path):
    sb = BubblewrapSandbox()
    # Fresh, unshared net namespace has no routes -> no egress possible.
    res = await sb.run("ip route show default", cwd=str(tmp_path), limits=SandboxLimits())
    assert res.exit_code == 0
    assert res.stdout.strip() == ""


@_requires_bwrap
async def test_bwrap_network_visible_when_granted(tmp_path):
    sb = BubblewrapSandbox()
    res = await sb.run(
        "ip route show default",
        cwd=str(tmp_path),
        limits=SandboxLimits(),
        network=True,
    )
    assert res.exit_code == 0
    assert "default" in res.stdout  # host default route is visible when granted


@_requires_bwrap
async def test_bwrap_timeout(tmp_path):
    sb = BubblewrapSandbox()
    res = await sb.run("sleep 5", cwd=str(tmp_path), limits=SandboxLimits(timeout_s=1))
    assert res.timed_out is True


@_requires_bwrap
async def test_bwrap_truncates_output(tmp_path):
    sb = BubblewrapSandbox()
    res = await sb.run(
        "echo xxxxxxxxxxxxxxxxxxxx", cwd=str(tmp_path), limits=SandboxLimits(max_output_bytes=10)
    )
    assert res.truncated is True
    assert len(res.stdout) <= 10


# --- PathJailSubprocess (weak Mac/Windows fallback; runs cross-OS) ----------
#
# pathjail is NOT a strong fs/network boundary (it can use the host python), so
# these pin the guarantees it DOES make: it runs, honors timeout/truncation,
# scrubs the environment, and starts in the workspace cwd.


async def test_pathjail_runs_command(tmp_path):
    sb = PathJailSubprocess()
    res = await sb.run(
        [sys.executable, "-c", "print('pj-ok')"], cwd=str(tmp_path), limits=SandboxLimits()
    )
    assert res.exit_code == 0
    assert "pj-ok" in res.stdout


async def test_pathjail_honors_timeout(tmp_path):
    sb = PathJailSubprocess()
    res = await sb.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=str(tmp_path),
        limits=SandboxLimits(timeout_s=1),
    )
    assert res.timed_out is True


async def test_pathjail_truncates_output(tmp_path):
    sb = PathJailSubprocess()
    res = await sb.run(
        [sys.executable, "-c", "print('x' * 100)"],
        cwd=str(tmp_path),
        limits=SandboxLimits(max_output_bytes=10),
    )
    assert res.truncated is True
    assert len(res.stdout) <= 10


async def test_pathjail_scrubs_environment(tmp_path, monkeypatch):
    # A secret in the parent environment must NOT leak into the command.
    monkeypatch.setenv("PATHJAIL_LEAK_TEST", "super-secret")
    sb = PathJailSubprocess()
    res = await sb.run(
        [sys.executable, "-c", "import os; print(os.environ.get('PATHJAIL_LEAK_TEST', 'ABSENT'))"],
        cwd=str(tmp_path),
        limits=SandboxLimits(),
    )
    assert res.exit_code == 0
    assert "super-secret" not in res.stdout
    assert "ABSENT" in res.stdout


async def test_pathjail_starts_in_workspace(tmp_path):
    sb = PathJailSubprocess()
    res = await sb.run(
        [sys.executable, "-c", "import os; print(os.path.realpath(os.getcwd()))"],
        cwd=str(tmp_path),
        limits=SandboxLimits(),
    )
    assert res.exit_code == 0
    assert os.path.realpath(res.stdout.strip()) == os.path.realpath(str(tmp_path))
