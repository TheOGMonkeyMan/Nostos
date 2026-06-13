"""AI chat / teacher / second-opinion handlers (ADR-056, Phase 2.2).

do_chat_with_model / do_ask_teacher / do_second_opinion, split out of
src/ai_interaction.py (slice 2). The session manager is read via the
get_session_manager() accessor (repointed from the bare rebindable global - a
provable no-op); get_session_manager + _resolve_model are lazy shims and
AI_CHAT_TIMEOUT is lazy-imported, all to avoid an import cycle (ai_interaction
re-imports these handlers). Re-imported into ai_interaction for the dispatcher.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def get_session_manager():
    from src.ai_interaction import get_session_manager as _g
    return _g()


def _resolve_model(*args, **kwargs):
    from src.ai_interaction import _resolve_model as _r
    return _r(*args, **kwargs)


async def do_chat_with_model(content: str, session_id: Optional[str] = None) -> Dict:
    """Send a message to a specific model and return its response.

    Content format:
      Line 1: model_name (or model_name@endpoint_name)
      Line 2+: the message to send
    """
    from src.llm_core import llm_call_async
    from src.ai_interaction import AI_CHAT_TIMEOUT

    lines = content.strip().split("\n", 1)
    if not lines or not lines[0].strip():
        return {"error": "First line must be the model name"}

    model_spec = lines[0].strip()
    message = lines[1].strip() if len(lines) > 1 else ""
    if not message:
        return {"error": "No message provided (line 2+ is the message)"}

    try:
        url, model, headers = _resolve_model(model_spec)
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = await llm_call_async(
            url, model,
            [{"role": "user", "content": message}],
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        # Truncate very long responses
        if len(response) > 10000:
            response = response[:10000] + "\n... (truncated)"
        return {"model": model, "response": response}
    except Exception as e:
        logger.error(f"chat_with_model failed: {e}")
        return {"error": f"Failed to get response from {model_spec}: {e}"}


_TEACHER_SYSTEM_PROMPT = (
    "You are a senior AI mentor. A less capable model is stuck on a problem and asking for help. "
    "Provide clear, actionable guidance:\n"
    "1. Brief analysis of the problem\n"
    "2. Recommended approach (step by step)\n"
    "3. Key things to watch out for\n\n"
    "Be concise and practical. No preamble."
)


async def do_ask_teacher(content: str, session_id: Optional[str] = None) -> Dict:
    """Ask a more capable model for help.

    Content format:
      Line 1: model_name (or 'auto')
      Line 2+: the problem description
    """
    from src.llm_core import llm_call_async
    from src.ai_interaction import AI_CHAT_TIMEOUT
    from src.settings import get_setting

    lines = content.strip().split("\n", 1)
    model_spec = lines[0].strip() if lines else "auto"
    problem = lines[1].strip() if len(lines) > 1 else ""

    if not problem:
        return {"error": "No problem description provided"}

    if model_spec.lower() in ("auto", ""):
        model_spec = get_setting("teacher_model", "")
        if not model_spec:
            return {"error": "No teacher model configured. Specify a model name or set teacher_model in settings."}

    try:
        url, model, headers = _resolve_model(model_spec)
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = await llm_call_async(
            url, model,
            [
                {"role": "system", "content": _TEACHER_SYSTEM_PROMPT},
                {"role": "user", "content": f"Problem:\n{problem}"},
            ],
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        if len(response) > 8000:
            response = response[:8000] + "\n... (truncated)"
        return {"model": model, "response": response, "teacher": True}
    except Exception as e:
        logger.error(f"ask_teacher failed: {e}")
        return {"error": f"Teacher call failed ({model_spec}): {e}"}


async def do_second_opinion(content: str, session_id: Optional[str] = None) -> Dict:
    """Get a second opinion from another model, then have the original model
    evaluate the feedback and produce a unified version.

    Content format:
      Line 1: model_name (or model_name@endpoint_name)
      Line 2+ (optional): specific question or focus area

    Flow:
      1. Pull recent conversation context
      2. Send to reviewer model → get honest feedback
      3. Send feedback back to the session's own model → evaluate & unify
      4. Return both the review and the unified response
    """
    from src.llm_core import llm_call_async
    from src.ai_interaction import AI_CHAT_TIMEOUT

    lines = content.strip().split("\n", 1)
    if not lines or not lines[0].strip():
        return {"error": "First line must be the model name"}

    model_spec = lines[0].strip()
    focus = lines[1].strip() if len(lines) > 1 else ""

    try:
        reviewer_url, reviewer_model, reviewer_headers = _resolve_model(model_spec)
    except ValueError as e:
        return {"error": str(e)}

    # Pull recent conversation context from current session
    context_text = ""
    sess = None
    if session_id and get_session_manager():
        sess = get_session_manager().get_session(session_id)
        if sess:
            messages = sess.get_context_messages()
            recent = messages[-15:] if len(messages) > 15 else messages
            parts = []
            for m in recent:
                role = m.get("role", "unknown").upper()
                text = m.get("content", "")
                if isinstance(text, list):
                    text = " ".join(
                        p.get("text", "") for p in text if isinstance(p, dict)
                    )
                if text:
                    parts.append(f"[{role}]: {text[:2000]}")
            context_text = "\n\n".join(parts)

    if not context_text:
        return {"error": "No conversation context found to review"}

    # ── Step 1: Get the reviewer's feedback ──
    reviewer_system = (
        "You are giving a second opinion on a conversation between a user and an AI assistant. "
        "Your job is to be genuinely helpful and honest — not a yes-man, but not a contrarian either.\n\n"
        "Guidelines:\n"
        "- If the plan/idea is solid, say so clearly. Don't manufacture problems that aren't there.\n"
        "- If you spot a real flaw, blind spot, or simpler approach — call it out directly.\n"
        "- Be practical. Don't over-engineer or over-analyze. Real-world tradeoffs matter.\n"
        "- If there's a meaningfully better way to do something, suggest it concretely.\n"
        "- Give credit where it's due — highlight what's working well.\n"
        "- Keep it concise and actionable. No fluff.\n"
        "- You're a second pair of eyes, not a professor grading a paper."
    )

    reviewer_message = f"Here's the conversation so far:\n\n{context_text}"
    if focus:
        reviewer_message += f"\n\n---\nSpecifically, I want your take on: {focus}"
    else:
        reviewer_message += "\n\n---\nGive me your honest second opinion on what's being discussed."

    try:
        review = await llm_call_async(
            reviewer_url, reviewer_model,
            [
                {"role": "system", "content": reviewer_system},
                {"role": "user", "content": reviewer_message},
            ],
            headers=reviewer_headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        if len(review) > 8000:
            review = review[:8000] + "\n... (truncated)"
    except Exception as e:
        logger.error(f"second_opinion reviewer call failed: {e}")
        return {"error": f"Failed to get second opinion from {model_spec}: {e}"}

    # ── Step 2: Send review back to session's own model for evaluation ──
    unified = ""
    original_model = "unknown"
    if sess:
        original_url = sess.endpoint_url
        original_model = sess.model
        original_headers = getattr(sess, "headers", None) or {}

        unify_system = (
            "Another AI model just reviewed the conversation you've been having with the user. "
            "Read their feedback carefully, then respond with:\n\n"
            "1. **What you agree with** — acknowledge valid points honestly.\n"
            "2. **What you disagree with** — explain why, briefly.\n"
            "3. **Unified version** — produce an updated/refined version of whatever was being discussed, "
            "incorporating the feedback you found valid. Don't accept every note blindly — "
            "use your judgment on what actually improves things vs what's unnecessary.\n\n"
            "Be concise and practical. The user wants a better result, not a meta-discussion."
        )

        unify_message = (
            f"Here's the conversation context:\n\n{context_text}\n\n"
            f"---\n\n"
            f"**Review from {reviewer_model}:**\n\n{review}\n\n"
            f"---\n\n"
            f"Evaluate this feedback and produce a unified improved version."
        )

        try:
            unified = await llm_call_async(
                original_url, original_model,
                [
                    {"role": "system", "content": unify_system},
                    {"role": "user", "content": unify_message},
                ],
                headers=original_headers,
                timeout=AI_CHAT_TIMEOUT,
            )
            if len(unified) > 10000:
                unified = unified[:10000] + "\n... (truncated)"
        except Exception as e:
            logger.error(f"second_opinion unify call failed: {e}")
            unified = f"(Failed to get unified response: {e})"

    # Build combined result
    combined = (
        f"## Second Opinion from {reviewer_model}\n\n{review}"
        f"\n\n---\n\n"
        f"## {original_model}'s Response\n\n{unified}"
    )

    return {
        "model": reviewer_model,
        "response": combined,
        "instruction": "Present these results to the user exactly as they are. Do NOT call second_opinion again. The user can continue the conversation from here.",
    }
