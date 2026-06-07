"""Phase 1.3a: human-in-the-loop approval broker + gate (ADR-027).

Offline + deterministic: a fake async `emit` captures the request event, and the
test resolves it on the same event loop (as the real approve/deny endpoint would).
"""
import asyncio
from types import SimpleNamespace

import pytest

import src.approval_broker as ab
import src.audit_log as al


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    ab._reset_for_tests()
    monkeypatch.setattr(al, "_AUDIT_DIR", tmp_path)
    al._reset_cache_for_tests()
    yield
    ab._reset_for_tests()
    al._reset_cache_for_tests()


def _capturing_emit():
    events = []

    async def emit(ev):
        events.append(ev)

    emit.events = events
    return emit


async def test_approve_resolves_to_approved():
    emit = _capturing_emit()
    task = asyncio.create_task(
        ab.request_approval("bash", "alice", "rm -rf /tmp/x", emit=emit, timeout=5)
    )
    await asyncio.sleep(0)  # let it register + emit
    assert len(emit.events) == 1
    ev = emit.events[0]
    assert ev["type"] == "approval_request" and ev["tool"] == "bash"
    assert ab.resolve(ev["request_id"], True, ab.SCOPE_ONCE) is True
    decision = await task
    assert decision.approved is True
    assert decision.scope == ab.SCOPE_ONCE


async def test_deny_resolves_to_denied():
    emit = _capturing_emit()
    task = asyncio.create_task(ab.request_approval("python", "alice", "code", emit=emit, timeout=5))
    await asyncio.sleep(0)
    assert ab.resolve(emit.events[0]["request_id"], False) is True
    decision = await task
    assert decision.approved is False
    assert decision.scope == ab.SCOPE_DENY


async def test_timeout_denies_fail_closed():
    emit = _capturing_emit()
    decision = await ab.request_approval("bash", "alice", "x", emit=emit, timeout=0.05)
    assert decision.approved is False
    assert decision.scope == ab.SCOPE_DENY
    assert ab.pending_count() == 0


async def test_emit_failure_denies_fail_closed():
    async def bad_emit(ev):
        raise RuntimeError("no channel")

    decision = await ab.request_approval("bash", "alice", "x", emit=bad_emit, timeout=5)
    assert decision.approved is False


async def test_resolve_unknown_request_returns_false():
    assert ab.resolve("does-not-exist", True) is False


async def test_decision_is_audited():
    emit = _capturing_emit()
    task = asyncio.create_task(ab.request_approval("bash", "alice", "x", emit=emit, timeout=5))
    await asyncio.sleep(0)
    ab.resolve(emit.events[0]["request_id"], False)
    await task
    recs = al.read_records()
    assert any("approval denied" in (r.get("detail") or "") for r in recs)


# ── Gate wiring in execute_tool_block (default-off + approve/deny paths) ─────


async def test_gate_skipped_when_no_emit(monkeypatch):
    # No approval channel -> gate never engages, even if the setting were on.
    import src.tool_execution as te

    async def fake_impl(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "bash: done", {"output": "ran", "exit_code": 0}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)
    block = SimpleNamespace(tool_type="bash", content="echo hi")
    desc, result = await te.execute_tool_block(block, owner="alice")  # no approval_emit
    assert result["exit_code"] == 0 and "ran" in result["output"]


async def test_gate_denies_block_when_enabled_and_denied(monkeypatch):
    import src.settings as settings
    import src.tool_execution as te

    monkeypatch.setattr(settings, "load_settings", lambda: {"approvals_enabled": True})

    impl_called = {"n": 0}

    async def fake_impl(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        impl_called["n"] += 1
        return "bash: done", {"output": "ran", "exit_code": 0}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)

    emit = _capturing_emit()

    async def driver():
        return await te.execute_tool_block(
            SimpleNamespace(tool_type="bash", content="rm -rf /"),
            owner="alice", approval_emit=emit,
        )

    task = asyncio.create_task(driver())
    await asyncio.sleep(0)
    # bash is privileged (requires_approval) -> an approval was requested.
    assert len(emit.events) == 1
    ab.resolve(emit.events[0]["request_id"], False)  # deny
    desc, result = await task
    assert desc.endswith("BLOCKED")
    assert result["exit_code"] == 1
    assert impl_called["n"] == 0  # tool never ran


async def test_gate_allows_run_when_approved(monkeypatch):
    import src.settings as settings
    import src.tool_execution as te

    monkeypatch.setattr(settings, "load_settings", lambda: {"approvals_enabled": True})

    async def fake_impl(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "bash: done", {"output": "ran", "exit_code": 0}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)
    emit = _capturing_emit()

    async def driver():
        return await te.execute_tool_block(
            SimpleNamespace(tool_type="bash", content="ls"),
            owner="alice", approval_emit=emit,
        )

    task = asyncio.create_task(driver())
    await asyncio.sleep(0)
    ab.resolve(emit.events[0]["request_id"], True)  # approve
    desc, result = await task
    assert result["exit_code"] == 0 and "ran" in result["output"]


async def test_gate_skips_read_only_tool(monkeypatch):
    # A read-only tool (web_search) does not require approval -> no request, runs.
    import src.settings as settings
    import src.tool_execution as te

    monkeypatch.setattr(settings, "load_settings", lambda: {"approvals_enabled": True})

    async def fake_impl(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "web_search: done", {"results": "hits", "exit_code": 0}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)
    emit = _capturing_emit()
    desc, result = await te.execute_tool_block(
        SimpleNamespace(tool_type="web_search", content="cats"),
        owner="alice", approval_emit=emit,
    )
    assert len(emit.events) == 0  # no approval requested
    assert result["exit_code"] == 0
