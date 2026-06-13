"""Phase 2.2 (ADR-055): verify the ai_interaction session-mgmt handler split.

do_create_session / do_list_sessions / do_send_to_session moved out of
src/ai_interaction.py into src/ai_sessions.py. Their reads of the rebindable
global _session_manager were repointed to the get_session_manager() accessor (a
provable no-op), with get_session_manager/_resolve_model provided as lazy shims to
avoid an import cycle. ai_interaction re-imports the 3 handlers for the dispatcher.
"""

import asyncio

import src.ai_interaction as ai


def test_handlers_reexported_from_ai_interaction():
    for fn in ("do_create_session", "do_list_sessions", "do_send_to_session"):
        assert hasattr(ai, fn), f"{fn} missing from ai_interaction namespace"
        assert getattr(ai, fn).__module__ == "src.ai_sessions"


def test_accessor_repoint_is_a_noop_guard():
    # With no session manager set, get_session_manager() returns None and the
    # handler hits the same "not available" guard as the old `if not _session_manager`.
    ai.set_session_manager(None)
    out = asyncio.run(ai.do_create_session("hi"))
    assert out.get("error"), out
    out2 = asyncio.run(ai.do_send_to_session("hi"))
    assert out2.get("error"), out2
