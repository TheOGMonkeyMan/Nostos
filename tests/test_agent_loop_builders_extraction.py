"""Phase 2.2 (ADR-054): verify the agent_loop prompt-BUILDER cluster split.

The prompt+context builders (_build_system_prompt, _build_base_prompt) + the
message helpers (_detect_admin_intent, _extract_last_user_message,
_recent_context_for_retrieval) + the _API_HOSTS/_MCP_KEYWORDS/_ADMIN_* constants
moved verbatim out of src/agent_loop.py and were appended to src/agent_prompt.py,
re-imported so the run loop (stream_agent_loop) + external callers keep working.
This is slice 2 of the agent_loop decomposition (after ADR-053); it takes
agent_loop.py under the 1500 cap. The AGENT_SYSTEM_PROMPT guardrail sha256 lock
lives in test_agent_prompt_extraction.py and is unaffected.
"""

import src.agent_loop as a


def test_builders_reexported_from_agent_loop():
    for fn in (
        "_build_system_prompt",
        "_build_base_prompt",
        "_detect_admin_intent",
        "_extract_last_user_message",
        "_recent_context_for_retrieval",
    ):
        assert hasattr(a, fn), f"{fn} missing from agent_loop namespace"
        assert getattr(a, fn).__module__ == "src.agent_prompt"


def test_run_loop_and_constants_intact():
    # The runtime loop stays in agent_loop; the constants it uses re-import cleanly.
    assert a.stream_agent_loop.__module__ == "src.agent_loop"
    for c in ("_API_HOSTS", "_MCP_KEYWORDS", "_ADMIN_SCHEMA_NAMES", "_TOOL_SELECTION_TIMEOUT_SECONDS"):
        assert hasattr(a, c)
