"""Phase 1.1c/1.1e: routing of the agent's bash/python tools through the sandbox.

DEFAULT-ON (ADR-020): SANDBOX_BACKEND unset/empty resolves to 'auto' (strongest
available jail for this OS, degrading to pathjail, never no-isolation). bash/python
always run through the sandbox in the per-session workspace, and an EXPLICIT
backend fails CLOSED if unavailable on this host. SANDBOX_BACKEND=none opts into
dev-only direct-host execution (still in the workspace, via NoSandbox).
"""

import sys

import pytest

from src.tool_execution import _direct_fallback, _sandbox_backend


def test_sandbox_backend_gate(monkeypatch):
    # Default-on: unset/empty => 'auto'. An explicit value is passed through.
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    assert _sandbox_backend() == "auto"
    monkeypatch.setenv("SANDBOX_BACKEND", "")
    assert _sandbox_backend() == "auto"
    monkeypatch.setenv("SANDBOX_BACKEND", " none ")
    assert _sandbox_backend() == "none"


async def test_bash_unset_routes_through_default_auto_sandbox(monkeypatch):
    # With SANDBOX_BACKEND unset, bash now runs sandboxed via 'auto' (pathjail on
    # this dev box / bubblewrap on a bwrap Linux host), in the per-session
    # workspace - never raw on the host cwd.
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    assert _sandbox_backend() == "auto"
    res = await _direct_fallback("bash", "echo hi", session_id="auto-default")
    assert res["exit_code"] == 0
    assert "hi" in res["output"]
    from src.sandbox import clean_workspace

    clean_workspace("auto-default")


async def test_python_routes_through_sandbox_into_workspace(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "none")
    res = await _direct_fallback(
        "python",
        "import os; print(os.path.basename(os.getcwd()))",
        session_id="sbx-route-test",
    )
    assert res["exit_code"] == 0
    # NoSandbox ran the command in the per-session workspace dir.
    assert "sbx-route-test" in res["output"]
    from src.sandbox import clean_workspace

    clean_workspace("sbx-route-test")


async def test_opt_in_unavailable_backend_fails_closed(monkeypatch):
    # Opting into a backend not available on this host must NOT silently run
    # unsandboxed - it returns an error and the command never runs.
    if sys.platform.startswith("linux"):
        pytest.skip("bubblewrap may actually be available on Linux")
    monkeypatch.setenv("SANDBOX_BACKEND", "bubblewrap")
    res = await _direct_fallback("bash", "echo should-not-run", session_id="t")
    assert res["exit_code"] == 1
    assert "unavailable" in res["error"].lower()
    assert "should-not-run" not in str(res)


class _RecordingSandbox:
    def __init__(self):
        self.calls = []

    async def run(self, cmd, *, cwd, limits, network=False, mounts=None):
        from src.sandbox import SandboxResult

        self.calls.append({"cmd": cmd, "cwd": cwd, "network": network, "mounts": mounts})
        return SandboxResult(stdout="ok", stderr="", exit_code=0)


async def test_trusted_grants_reach_sandbox_run(monkeypatch):
    # Phase 1.1d: SANDBOX_MOUNTS + SANDBOX_ALLOW_NETWORK env flow through
    # _run_sandboxed into sandbox.run() as Mounts + network=True.
    import src.sandbox as sbpkg

    rec = _RecordingSandbox()
    monkeypatch.setattr(sbpkg, "get_sandbox", lambda backend: rec)
    monkeypatch.setenv("SANDBOX_BACKEND", "none")
    monkeypatch.setenv("SANDBOX_MOUNTS", "/host/in:/in:ro,/host/out:/out:rw")
    monkeypatch.setenv("SANDBOX_ALLOW_NETWORK", "1")

    res = await _direct_fallback("bash", "echo hi", session_id="grants")
    assert res["exit_code"] == 0
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["network"] is True
    assert [(m.target, m.read_only) for m in call["mounts"]] == [("/in", True), ("/out", False)]
