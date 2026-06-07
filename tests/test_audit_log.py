"""Phase 1.3b: append-only, tamper-evident audit log (ADR-026)."""
import hashlib
from types import SimpleNamespace

import pytest

import src.audit_log as al


@pytest.fixture(autouse=True)
def _tmp_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "_AUDIT_DIR", tmp_path)
    al._reset_cache_for_tests()
    yield
    al._reset_cache_for_tests()


def test_record_writes_required_fields():
    al.record("bash", "alice", "ok", args="echo hi", session_id="s1")
    recs = al.read_records()
    assert len(recs) == 1
    r = recs[0]
    for k in ("ts", "tool", "owner", "outcome", "args_hash", "session_id", "prev", "hash"):
        assert k in r, k
    assert r["tool"] == "bash"
    assert r["owner"] == "alice"
    assert r["outcome"] == "ok"
    assert r["session_id"] == "s1"


def test_args_are_hashed_not_stored_raw():
    secret = "rm -rf / --secret-token=tok_DEADBEEF12345"
    al.record("bash", "alice", "ok", args=secret)
    recs = al.read_records()
    assert secret not in str(recs[0])  # raw args never written
    assert recs[0]["args_hash"] == hashlib.sha256(secret.encode()).hexdigest()


def test_append_only_chain_links_and_verifies():
    al.record("web_search", "alice", "ok", args="a")
    al.record("bash", "alice", "blocked", args="b")
    al.record("python", "bob", "error", args="c")
    recs = al.read_records()
    assert len(recs) == 3
    # Each record's prev is the previous record's hash; first links to genesis.
    assert recs[0]["prev"] == al._GENESIS
    assert recs[1]["prev"] == recs[0]["hash"]
    assert recs[2]["prev"] == recs[1]["hash"]
    assert al.verify_chain() is True


def test_verify_chain_detects_tampering():
    al.record("bash", "alice", "ok", args="a")
    al.record("bash", "alice", "ok", args="b")
    path = al._today_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    # Tamper: flip the first record's outcome without recomputing its hash.
    lines[0] = lines[0].replace('"outcome": "ok"', '"outcome": "blocked"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert al.verify_chain() is False


def test_invalid_outcome_is_coerced():
    al.record("bash", "alice", "totally-bogus")
    assert al.read_records()[0]["outcome"] == "ok"


def test_record_is_exception_safe(monkeypatch):
    # Even if the directory can't be made, record() must not raise.
    monkeypatch.setattr(al, "_today_path", lambda: (_ for _ in ()).throw(OSError("boom")))
    al.record("bash", "alice", "ok")  # should swallow


async def test_execute_tool_block_wrapper_records_outcome(monkeypatch):
    import src.tool_execution as te

    async def fake_impl(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "bash: BLOCKED", {"error": "Tool 'bash' requires an admin user.", "exit_code": 1}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)
    block = SimpleNamespace(tool_type="bash", content="rm -rf /")
    desc, result = await te.execute_tool_block(block, owner="alice", session_id="s9")

    # Wrapper returns the impl's tuple unchanged.
    assert desc == "bash: BLOCKED"
    recs = al.read_records()
    assert len(recs) == 1
    assert recs[0]["outcome"] == "blocked"
    assert recs[0]["tool"] == "bash"
    assert recs[0]["owner"] == "alice"
    # Raw command (potentially dangerous / secret-bearing) is hashed, not stored.
    assert "rm -rf /" not in str(recs[0])


async def test_execute_tool_block_wrapper_records_ok(monkeypatch):
    import src.tool_execution as te

    async def fake_impl(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "web_search: done", {"results": "some text", "exit_code": 0}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)
    block = SimpleNamespace(tool_type="web_search", content="cats")
    await te.execute_tool_block(block, owner=None)
    assert al.read_records()[0]["outcome"] == "ok"
