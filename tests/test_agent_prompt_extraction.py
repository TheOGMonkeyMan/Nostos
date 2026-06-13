"""Phase 2.2 (ADR-053): verify the agent system-prompt block split + lock the guardrails.

The system-prompt constants (_AGENT_PREAMBLE, _AGENT_RULES, _API_AGENT_RULES,
TOOL_SECTIONS) + assembly fns (get_builtin_overrides, _section_text,
_assemble_prompt) + the computed AGENT_SYSTEM_PROMPT moved verbatim out of
src/agent_loop.py into src/agent_prompt.py, re-imported so the prompt builders +
external callers keep working.

AGENT_SYSTEM_PROMPT is the agent's GUARDRAIL content. The sha256 below is pinned:
the ADR-053 move was byte-identical, and this lock catches ANY future accidental
change to the prompt text (a security-relevant regression).
"""

import hashlib

import src.agent_loop as a
import src.agent_prompt as p

# Pinned BEFORE the ADR-053 move (len 28087). Update this ONLY with a deliberate,
# reviewed prompt change - never to make a refactor pass.
_AGENT_SYSTEM_PROMPT_SHA256 = "6bf21beadba28a17de74d63fcf8497e46625c8ae93ac957ccad82f32e6e94cf9"


def test_agent_system_prompt_guardrail_unchanged():
    h = hashlib.sha256(a.AGENT_SYSTEM_PROMPT.encode()).hexdigest()
    assert h == _AGENT_SYSTEM_PROMPT_SHA256, (
        "AGENT_SYSTEM_PROMPT content changed - if intentional, update the pinned sha256; "
        "if not, a refactor altered the agent's guardrail text."
    )


def test_prompt_block_reexported_from_agent_loop():
    # Same object after re-import (identity), and the fns resolve to the new module.
    assert a.AGENT_SYSTEM_PROMPT is p.AGENT_SYSTEM_PROMPT
    assert a.TOOL_SECTIONS is p.TOOL_SECTIONS
    for fn in ("get_builtin_overrides", "_section_text", "_assemble_prompt"):
        assert getattr(a, fn).__module__ == "src.agent_prompt"
    # external callers (skills_routes) import these from src.agent_loop
    from src.agent_loop import get_builtin_overrides, TOOL_SECTIONS  # noqa: F401
