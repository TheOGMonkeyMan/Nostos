"""Phase 2.2 (ADR-056): verify the ai_interaction chat-handler split (slice 2).

do_chat_with_model / do_ask_teacher / do_second_opinion moved out of
src/ai_interaction.py into src/ai_chat.py, with the 2 _session_manager reads
repointed to get_session_manager() (provable no-op; lazy shims + lazy
AI_CHAT_TIMEOUT avoid the cycle). ai_interaction re-imports them for the
dispatcher. This slice takes ai_interaction.py under the 1500 cap. The full suite
(which exercises chat/teacher) is the behavioral no-op proof.
"""

import src.ai_interaction as ai


def test_chat_handlers_reexported_from_ai_interaction():
    for fn in ("do_chat_with_model", "do_ask_teacher", "do_second_opinion"):
        assert hasattr(ai, fn), f"{fn} missing from ai_interaction namespace"
        assert getattr(ai, fn).__module__ == "src.ai_chat"


def test_dispatcher_and_session_handlers_still_present():
    # the dispatcher stays in ai_interaction; the slice-1 handlers still re-export
    assert ai.stream_ai_tool.__module__ == "src.ai_interaction"
    assert ai.do_create_session.__module__ == "src.ai_sessions"
