"""
agent_loop.py

Streaming agent loop for odysseus-ui.
Wraps stream_llm() with multi-round tool execution.
The LLM decides when to use tools by writing fenced code blocks.
"""

import asyncio
import collections
import json
import re
import time
import logging
from typing import AsyncGenerator, List, Dict, Optional, Set

from src.llm_core import stream_llm, stream_llm_with_fallback
from src.model_context import estimate_tokens
from src.settings import get_setting
from src.prompt_security import untrusted_context_message
from src.tool_security import blocked_tools_for_owner
from src.agent_tools import (
    parse_tool_blocks,
    strip_tool_blocks,
    execute_tool_block,
    format_tool_result,
    set_active_document,
    set_active_model,
    function_call_to_tool_block,
    get_mcp_manager,
    FUNCTION_TOOL_SCHEMAS,
    TOOL_TAGS,
    ToolBlock,
    MAX_AGENT_ROUNDS,
)

logger = logging.getLogger(__name__)


def _load_mcp_disabled_map() -> Dict[str, set]:
    """Load per-server disabled tool sets from the database."""
    from core.database import McpServer, SessionLocal
    disabled_map: Dict[str, set] = {}
    db = SessionLocal()
    try:
        for srv in db.query(McpServer).all():
            if srv.disabled_tools:
                try:
                    names = json.loads(srv.disabled_tools)
                    if names:
                        disabled_map[srv.id] = set(names)
                except (json.JSONDecodeError, TypeError):
                    pass
    finally:
        db.close()
    return disabled_map

# --- system-prompt constants + assembly moved to src/agent_prompt.py (ADR-053);
# re-imported (byte-identical; AGENT_SYSTEM_PROMPT sha256 locked by a test) so the
# prompt builders below + external callers keep working.
from src.agent_prompt import (  # noqa: E402,F401
    _AGENT_PREAMBLE,
    _AGENT_RULES,
    _API_AGENT_RULES,
    TOOL_SECTIONS,
    get_builtin_overrides,
    _section_text,
    _assemble_prompt,
    AGENT_SYSTEM_PROMPT,
)


# --- prompt + context builders moved to src/agent_prompt.py (ADR-054); re-imported
# so the loop below + external callers keep working.
from src.agent_prompt import (  # noqa: E402,F401
    _API_HOSTS,
    _MCP_KEYWORDS,
    _ADMIN_SCHEMA_NAMES,
    _TOOL_SELECTION_TIMEOUT_SECONDS,
    _detect_admin_intent,
    _extract_last_user_message,
    _recent_context_for_retrieval,
    _build_system_prompt,
    _build_base_prompt,
)



def _resolve_tool_blocks(round_response: str, native_tool_calls: list, round_num: int):
    """Choose native function calls or fenced code block parsing. Returns (tool_blocks, used_native)."""
    used_native = False
    if native_tool_calls:
        tool_blocks = []
        for tc in native_tool_calls:
            tc_name = tc.get("name", "")
            tc_args = tc.get("arguments", "{}")
            block = function_call_to_tool_block(tc_name, tc_args)
            if block:
                tool_blocks.append(block)
                logger.info(f"  -> converted: {tc_name} -> {block.tool_type}")
            else:
                logger.warning(f"  -> FAILED to convert native call: {tc_name} args={tc_args[:200]}")
        if tool_blocks:
            used_native = True
    if not used_native:
        tool_blocks = parse_tool_blocks(round_response)
        if tool_blocks:
            logger.info(f"Agent round {round_num}: {len(tool_blocks)} fenced tool block(s) detected")

    resp_preview = round_response[:200].replace('\n', '\\n') if round_response else "(empty)"
    logger.info(f"Agent round {round_num} summary: {len(round_response)} chars, "
                f"{len(native_tool_calls)} native calls, "
                f"{len(tool_blocks)} tool blocks. Preview: {resp_preview}")

    return tool_blocks, used_native


def _append_tool_results(
    messages: List[Dict],
    round_response: str,
    native_tool_calls: list,
    tool_results: list,
    tool_result_texts: list,
    used_native: bool,
    round_num: int,
    round_reasoning: str = "",
):
    """Append tool execution results back into the message history for the next LLM round.

    `round_reasoning` (DeepSeek / vLLM reasoning-parser deltas) is echoed
    back via `reasoning_content` on the assistant message — DeepSeek's API
    rejects follow-up requests in thinking mode that don't include the
    prior reasoning. Other vendors ignore the extra field.
    """
    if used_native and native_tool_calls:
        assistant_msg = {"role": "assistant"}
        assistant_msg["content"] = round_response if round_response.strip() else ""
        if round_reasoning:
            assistant_msg["reasoning_content"] = round_reasoning
        assistant_msg["tool_calls"] = [
            {
                "id": tc.get("id", f"call_{round_num}_{j}"),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", "{}"),
                },
            }
            for j, tc in enumerate(native_tool_calls)
        ]
        messages.append(assistant_msg)
        for j, tc in enumerate(native_tool_calls):
            result_text = tool_result_texts[j] if j < len(tool_result_texts) else ""
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{round_num}_{j}"),
                "content": result_text,
            })
    else:
        tool_output_text = "\n\n".join(tool_results)
        msg = {"role": "assistant", "content": round_response}
        if round_reasoning:
            msg["reasoning_content"] = round_reasoning
        messages.append(msg)
        messages.append(
            {"role": "user", "content": f"[Tool execution results]\n\n{tool_output_text}"}
        )


def _compute_final_metrics(
    messages: List[Dict],
    full_response: str,
    total_duration: float,
    time_to_first_token,
    context_length: int,
    real_input_tokens: int,
    real_output_tokens: int,
    has_real_usage: bool,
    tool_events: list,
    round_texts: list,
    model: str = "",
    last_round_input_tokens: int = 0,
    prep_timings: Optional[Dict[str, float]] = None,
) -> dict:
    """Compute token counts, TPS, and build the final metrics dict."""
    if has_real_usage:
        input_tokens = real_input_tokens
        output_tokens = real_output_tokens
    else:
        input_content = ""
        for msg in messages:
            if isinstance(msg.get("content"), str):
                input_content += msg["content"] + "\n"
        input_tokens = len(input_content) // 4
        output_tokens = len(full_response) // 4
    tps = output_tokens / total_duration if total_duration > 0 else 0
    # Use last round's input tokens for context % (peak usage) when available
    ctx_tokens = last_round_input_tokens if last_round_input_tokens > 0 else input_tokens
    ctx_pct = min(round((ctx_tokens / context_length) * 100, 1), 100.0) if context_length else 0

    metrics = {
        "response_time": round(total_duration, 2),
        "time_to_first_token": round(time_to_first_token, 2) if time_to_first_token else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_per_second": round(tps, 2),
        "total_tokens": input_tokens + output_tokens,
        "context_length": context_length,
        "context_percent": ctx_pct,
        "usage_source": "real" if has_real_usage else "estimated",
        "model": model,
    }
    if prep_timings:
        prep_total = round(sum(prep_timings.values()), 3)
        metrics["agent_prep_time"] = prep_total
        metrics["agent_model_wait_time"] = round(max((time_to_first_token or 0) - prep_total, 0), 3)
        metrics["agent_prep_breakdown"] = {
            key: round(value, 3) for key, value in prep_timings.items()
        }
    if tool_events:
        metrics["tool_events"] = tool_events
        metrics["round_texts"] = round_texts
    return metrics


# ── Completion verifier ──
# Tools whose effects produce a checkable artifact. A turn that used one of
# these is "effectful" and worth an independent completion check; pure
# read-only / Q&A turns are not.
_VERIFIER_EFFECTFUL_TOOLS = {
    "create_document", "update_document", "edit_document",
    "bash", "python", "write_file",
}
_VERIFIER_MAX_ROUNDS = 2  # cap re-verify cycles per turn — never loop forever


def _build_actions_snapshot(tool_events: list, limit: int = 8000) -> str:
    """Compact record of what the agent actually did this turn, for the
    verifier to judge against. One block per tool execution: the command and
    a head of its output."""
    parts = []
    for ev in tool_events:
        tool = ev.get("tool", "?")
        cmd = (ev.get("command") or "").strip()
        out = (ev.get("output") or "").strip()
        rc = ev.get("exit_code")
        head = f"[{tool}] {cmd}" if cmd else f"[{tool}]"
        rc_s = f" (exit {rc})" if rc not in (None, 0) else ""
        body = (out[:1200] + " …") if len(out) > 1200 else (out or "(no output)")
        parts.append(f"{head}{rc_s}\n-> {body}")
    snap = "\n\n".join(parts)
    return snap[:limit] if len(snap) > limit else snap


async def _run_verifier_subagent(
    instruction: str, actions_snapshot: str,
    *, endpoint_url: str, model: str, headers: dict,
) -> list:
    """Fresh-context completion verifier. A second model instance with NO
    shared history reads the user's request + a record of what the agent did
    and judges whether the task is genuinely complete. The independent context
    is the whole point: a model checking its own work rationalizes; one that
    didn't do the work reads it cold. Returns a list of failure reasons
    (empty = pass, or silently empty on any error so it can't block a valid
    completion)."""
    from src.llm_core import llm_call_async
    prompt = (
        "You are an independent verifier. Another assistant just claimed the "
        "following task is complete. Using ONLY the request and the record of "
        "what it actually did, decide whether that claim is correct. Be strict: "
        "only say SUCCESS if the work genuinely satisfies the request.\n\n"
        f"<user_request>\n{(instruction or '')[:4000]}\n</user_request>\n\n"
        f"<actions_taken>\n{actions_snapshot[:8000]}\n</actions_taken>\n\n"
        "<checklist>\n"
        "1. Every concrete deliverable the request asked for was actually produced\n"
        "2. Outputs/edits match what was asked — nothing missing, no extra or unrequested changes\n"
        "3. Tool results show success, not errors or empty output that got ignored\n"
        "4. Anything the request said to leave alone was left unchanged\n"
        "</checklist>\n\n"
        "Reason briefly (2-3 sentences max). Then output EXACTLY one of:\n"
        "  VERIFICATION: SUCCESS\n"
        "  VERIFICATION: FAIL: <one short sentence per issue, semicolon-separated>\n"
        "Output nothing after the VERIFICATION line."
    )
    try:
        raw = await llm_call_async(
            url=endpoint_url, model=model,
            messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.0, max_tokens=600, timeout=60,
        )
    except Exception as e:
        logger.warning(f"[agent] verifier subagent failed: {e}")
        return []
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL | re.IGNORECASE)
    last_v = None
    for line in raw.splitlines():
        if "VERIFICATION:" in line:
            last_v = line.strip()
    if not last_v or "VERIFICATION: FAIL:" not in last_v:
        return []
    reasons = last_v.split("VERIFICATION: FAIL:", 1)[1].strip()
    return [r.strip() for r in reasons.split(";") if r.strip()]


async def stream_agent_loop(
    endpoint_url: str,
    model: str,
    messages: List[Dict],
    headers: Optional[Dict] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    prompt_type: Optional[str] = None,
    max_rounds: int = MAX_AGENT_ROUNDS,
    max_tool_calls: int = 0,
    context_length: int = 0,
    active_document=None,
    session_id: Optional[str] = None,
    disabled_tools: Optional[Set[str]] = None,
    owner: Optional[str] = None,
    relevant_tools: Optional[Set[str]] = None,
    fallbacks: Optional[List[tuple]] = None,
    _is_teacher_run: bool = False,
) -> AsyncGenerator[str, None]:
    """Streaming agent loop generator.

    Yields SSE events:
      - data: {"delta": "text"}                             (text chunks)
      - data: {"type": "tool_start", "tool": "...", ...}    (before execution)
      - data: {"type": "tool_output", "tool": "...", ...}   (after execution)
      - data: {"type": "agent_step", "round": N}            (next round)
      - data: {"type": "metrics", "data": {...}}            (final metrics)
      - data: [DONE]                                        (end)
    """

    mcp_mgr = get_mcp_manager()
    prep_timings: Dict[str, float] = {}
    disabled_tools = set(disabled_tools or [])
    public_blocked_tools = blocked_tools_for_owner(owner)
    if public_blocked_tools:
        disabled_tools.update(public_blocked_tools)
        # MCP tools are namespaced dynamically, so hide all MCP schemas for
        # public/non-admin users rather than trying to enumerate every tool.
        mcp_mgr = None

    _t0 = time.time()
    _needs_admin = _detect_admin_intent(messages)
    _last_user = _extract_last_user_message(messages)
    # Tool retrieval keys on recent conversation context (last few user turns),
    # not just the latest message, so short follow-ups don't drop just-used tools.
    _retrieval_query = _recent_context_for_retrieval(messages) or _last_user
    _mcp_disabled_map = _load_mcp_disabled_map() if mcp_mgr else {}
    prep_timings["request_setup"] = time.time() - _t0

    # RAG-based tool selection: retrieve relevant tools for this query.
    # If caller provided a pre-computed set (e.g. task_scheduler), use that.
    _relevant_tools = relevant_tools
    _t1 = time.time()
    if _relevant_tools:
        logger.info(f"[tool-rag] Using caller-provided relevant_tools ({len(_relevant_tools)} tools)")
    if not _relevant_tools:
        try:
            from src.tool_index import get_tool_index, ALWAYS_AVAILABLE
            tool_idx = get_tool_index()
            if tool_idx:
                if mcp_mgr:
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(tool_idx.index_mcp_tools, mcp_mgr, _mcp_disabled_map),
                            timeout=_TOOL_SELECTION_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[tool-rag] MCP tool indexing exceeded %.1fs; continuing without reindex",
                            _TOOL_SELECTION_TIMEOUT_SECONDS,
                        )
                if _retrieval_query:
                    try:
                        _relevant_tools = await asyncio.wait_for(
                            asyncio.to_thread(tool_idx.get_tools_for_query, _retrieval_query, 8),
                            timeout=_TOOL_SELECTION_TIMEOUT_SECONDS,
                        )
                        logger.info(f"[tool-rag] Retrieved tools for query: {sorted(_relevant_tools - ALWAYS_AVAILABLE)}")
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[tool-rag] Retrieval exceeded %.1fs; falling back to always-available tools",
                            _TOOL_SELECTION_TIMEOUT_SECONDS,
                        )
                        _relevant_tools = set(ALWAYS_AVAILABLE)
        except Exception as e:
            logger.warning(f"[tool-rag] Retrieval failed, using keyword fallback: {e}")
            _relevant_tools = None

    # Fallback: if RAG unavailable, use keyword-based tool selection
    # instead of sending ALL tools (which overwhelms the model).
    if not _relevant_tools and _retrieval_query:
        from src.tool_index import ALWAYS_AVAILABLE, ToolIndex
        _relevant_tools = set(ALWAYS_AVAILABLE)
        ql = _retrieval_query.lower()
        for keywords, tools in ToolIndex._KEYWORD_HINTS.items():
            if any(kw in ql for kw in keywords):
                _relevant_tools.update(tools)
        # Always include core document/memory tools
        _relevant_tools.update({"create_document", "manage_memory", "manage_notes"})
        logger.info(f"[tool-rag] Keyword fallback selected: {sorted(_relevant_tools - ALWAYS_AVAILABLE)}")

    # If a document is open the model needs the editing tools available
    # regardless of which selection path (RAG, keyword, caller-provided) ran
    # or what keywords were in the latest user message.
    if _relevant_tools is not None and active_document is not None:
        _relevant_tools.update({"edit_document", "update_document", "suggest_document"})

    prep_timings["tool_selection"] = time.time() - _t1

    _t2 = time.time()
    # Hosted-API match by URL, OR the model name looks like a recent model
    # known to follow OpenAI-style function calling (DeepSeek, GPT*, Claude,
    # Gemini, Qwen3+, Mixtral, Llama 3.1+). Caught the DeepSeek-via-local-
    # vLLM case where endpoint_url doesn't include a vendor host.
    _model_lc = (model or "").lower()
    # Step 1: per-endpoint override (set at registration time from the
    # serve command — `--enable-auto-tool-choice` flips it on. UI can
    # also toggle per endpoint). NULL = unknown, fall through to the
    # keyword heuristic + host check.
    _endpoint_supports: Optional[bool] = None
    try:
        from core.database import SessionLocal as _SL, ModelEndpoint as _ME
        _db = _SL()
        try:
            _ep = _db.query(_ME).filter(_ME.base_url == endpoint_url).first()
            if not _ep and endpoint_url:
                _u = endpoint_url.rstrip("/")
                _ep = _db.query(_ME).filter(_ME.base_url == _u).first() or \
                      _db.query(_ME).filter(_ME.base_url == _u + "/").first()
            if _ep is not None:
                _endpoint_supports = _ep.supports_tools
        finally:
            _db.close()
    except Exception as _e:
        logger.debug(f"endpoint supports_tools lookup failed: {_e}")
    _model_supports_tools = any(kw in _model_lc for kw in (
        "deepseek", "gpt-4", "gpt-5", "gpt-o", "claude", "gemini",
        "qwen3", "qwen2.5", "mixtral", "mistral", "llama-3.1", "llama-3.2",
        "llama-3.3", "llama-4",
        # Local-served models that follow OpenAI-style function calling
        # via vLLM's `--enable-auto-tool-choice`. Belt-and-suspenders
        # with the per-endpoint flag above.
        "minimax", "kimi", "yi-", "phi-3", "phi-4", "command-r",
        "glm-4", "internlm", "hermes",
    ))
    if _endpoint_supports is True:
        _is_api_model = True
    elif _endpoint_supports is False:
        _is_api_model = False
    else:
        _is_api_model = any(h in endpoint_url for h in _API_HOSTS) or _model_supports_tools
    messages, mcp_schemas = _build_system_prompt(
        messages, model, active_document, mcp_mgr, disabled_tools,
        needs_admin=_needs_admin, relevant_tools=_relevant_tools,
        mcp_disabled_map=_mcp_disabled_map,
        compact=_is_api_model,
        owner=owner,
    )
    prep_timings["prompt_build"] = time.time() - _t2

    _t3 = time.time()
    try:
        from src.context_compactor import trim_for_context

        soft_budget = int(get_setting("agent_input_token_budget", 6000) or 0)
        if soft_budget > 0:
            before_trim_tokens = estimate_tokens(messages)
            reserve_tokens = min(max(max_tokens or 1024, 512), 2048)
            effective_budget = min(context_length or soft_budget, soft_budget)
            trimmed_messages = trim_for_context(
                messages,
                effective_budget,
                reserve_tokens=reserve_tokens,
            )
            after_trim_tokens = estimate_tokens(trimmed_messages)
            if after_trim_tokens < before_trim_tokens:
                logger.info(
                    "[agent] soft-trimmed context: %s -> %s tokens (budget=%s, reserve=%s)",
                    before_trim_tokens,
                    after_trim_tokens,
                    effective_budget,
                    reserve_tokens,
                )
                messages = trimmed_messages
    except Exception as e:
        logger.warning("[agent] Soft context trim skipped: %s", e)
    prep_timings["context_trim"] = time.time() - _t3

    # Strip internal metadata keys before sending to the LLM API
    messages = [{k: v for k, v in msg.items() if k != "_protected"} for msg in messages]

    yield f"data: {json.dumps({'type': 'agent_prep', 'data': {k: round(v, 3) for k, v in prep_timings.items()}})}\n\n"

    full_response = ""
    total_start = time.time()
    time_to_first_token = None
    first_token_received = False
    tool_events = []   # Persist tool executions for history reload
    round_texts = []   # Cleaned text per round for history reload
    # Completion-verifier state (mechanism 3a). _effectful_used flips on when
    # a tool that produces a checkable artifact runs; the verifier only fires
    # on such turns and at most _VERIFIER_MAX_ROUNDS times.
    _effectful_used = False
    _verifier_rounds = 0
    _verifier_instruction = _extract_last_user_message(messages)
    real_input_tokens = 0   # Accumulated real usage from API
    real_output_tokens = 0
    last_round_input_tokens = 0  # Last round's input tokens (for context % peak)
    has_real_usage = False
    total_tool_calls = 0  # for budget enforcement

    # Loop-breaker state. Small models (e.g. deepseek-v4-flash) can get
    # stuck firing the same tool call over and over with no text — burns
    # all 20 rounds, looks like the chat "died". Track recent call
    # signatures + consecutive no-text tool rounds to bail early.
    _recent_call_sigs = collections.deque(maxlen=6)
    _stuck_rounds = 0
    _tool_type_counts: collections.Counter = collections.Counter()
    _THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)
    _force_answer = False  # set by loop-breaker → next round runs with NO tools

    # Document streaming state (persists across rounds)
    _doc_acc = ""          # accumulated tool-call JSON arguments
    _doc_opened = False    # whether doc_stream_open was sent
    _doc_last_len = 0      # last content length sent

    for round_num in range(1, max_rounds + 1):
        round_response = ""
        round_reasoning = ""  # reasoning_content deltas (DeepSeek-thinking, vLLM --reasoning-parser)
        native_tool_calls = []  # populated if model uses function calling
        # Reset doc streaming state per round
        _doc_acc = ""
        _doc_opened = False
        _doc_last_len = 0
        _doc_fence_offset = 0  # offset into round_response for text-fence content
        # Cursor for the multi-block scanner — when a `create_document`
        # fenced block closes we advance this so the next iteration can
        # detect a SUBSEQUENT block in the same round.
        _doc_scan_from = 0

        # Merge native tool schemas with MCP tool schemas, filtering out
        # Only send function schemas for API models (OpenAI, Anthropic, etc.).
        # Local models use fenced code blocks or <tool_code> — schemas add overhead.
        if _force_answer:
            # Loop-breaker decided the model has enough info but keeps
            # calling tools. Send NO tools this round so it's forced to
            # write the answer instead of flailing further.
            all_tool_schemas = []
        elif _is_api_model:
            # Filter schemas by RAG-selected tools (if available)
            if _relevant_tools:
                base_schemas = [
                    s for s in FUNCTION_TOOL_SCHEMAS
                    if s.get("function", {}).get("name") in _relevant_tools
                ]
                _mcp_filtered = [
                    s for s in mcp_schemas
                    if s.get("function", {}).get("name") in _relevant_tools
                ]
                all_tool_schemas = base_schemas + _mcp_filtered
            else:
                base_schemas = FUNCTION_TOOL_SCHEMAS if _needs_admin else [
                    s for s in FUNCTION_TOOL_SCHEMAS
                    if s.get("function", {}).get("name") not in _ADMIN_SCHEMA_NAMES
                ]
                all_tool_schemas = base_schemas + mcp_schemas
            if disabled_tools:
                all_tool_schemas = [
                    t for t in all_tool_schemas
                    if t.get("function", {}).get("name") not in disabled_tools
                    and t.get("name") not in disabled_tools
                ]
        else:
            # Local: only MCP schemas when message suggests MCP tool usage
            _last_content = _last_user.lower()
            _wants_mcp = any(kw in _last_content for kw in _MCP_KEYWORDS)
            all_tool_schemas = mcp_schemas if (_wants_mcp and mcp_schemas) else []
        agent_stream_timeout = int(get_setting("agent_stream_timeout_seconds", 300) or 300)

        _tool_names_sent = [t.get("function", {}).get("name") for t in (all_tool_schemas or []) if t.get("function")]
        logger.info(f"[agent-debug] round={round_num} model={model} _is_api_model={_is_api_model} tools_sent={len(_tool_names_sent)} tool_names={_tool_names_sent[:15]} relevant_tools={sorted(_relevant_tools)[:15] if _relevant_tools else 'ALL'}")

        # Primary target + any configured fallback models. stream_llm_with_fallback
        # only switches on a pre-content failure, so streamed output is never
        # duplicated; the dead-host cooldown keeps repeat primary attempts cheap.
        _candidates = [(endpoint_url, model, headers)] + list(fallbacks or [])
        # stream_llm enforces a per-read INACTIVITY timeout (httpx read=timeout),
        # which kills a wedged/silent endpoint. This wall-clock deadline is the
        # complementary cap for the rare stream that trickles bytes forever and
        # so never trips the inactivity timeout. Generous — only catches runaway.
        _round_deadline = time.time() + max(agent_stream_timeout * 4, 1200)
        async for chunk in stream_llm_with_fallback(
            _candidates,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_type=prompt_type if round_num == 1 else None,
            tools=all_tool_schemas if all_tool_schemas else None,
            timeout=agent_stream_timeout,
        ):
            if time.time() > _round_deadline:
                logger.warning(f"[agent] round {round_num} stream exceeded wall-clock deadline; cutting off")
                break
            # Forward error events from stream_llm to the frontend
            if chunk.startswith("event: error"):
                yield chunk
                continue
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    data = json.loads(chunk[6:])
                    # IMPORTANT: check type-based events BEFORE "delta" key,
                    # because tool_call_delta also has an "arg_delta" field.
                    if data.get("type") == "tool_call_delta":
                        # Stream document content to frontend as AI generates it
                        logger.debug(f"tool_call_delta: name={data.get('name')}, len(arg_delta)={len(data.get('arg_delta', ''))}")
                        _doc_acc += data.get("arg_delta", "")
                        if not _doc_opened:
                            tm = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', _doc_acc)
                            if tm:
                                _doc_opened = True
                                try:
                                    title = json.loads('"' + tm.group(1) + '"')
                                except Exception:
                                    title = tm.group(1)
                                lm = re.search(r'"language"\s*:\s*"((?:[^"\\]|\\.)*)"', _doc_acc)
                                lang = ""
                                if lm:
                                    try:
                                        lang = json.loads('"' + lm.group(1) + '"')
                                    except Exception:
                                        lang = lm.group(1)
                                logger.info(f"Doc streaming: open title={title!r} lang={lang!r}")
                                yield f'data: {json.dumps({"type": "doc_stream_open", "title": title, "language": lang})}\n\n'
                        if _doc_opened:
                            cm = re.search(r'"content"\s*:\s*"', _doc_acc)
                            if cm:
                                raw = _doc_acc[cm.end():]
                                raw = re.sub(r'"\s*\}\s*$', '', raw)
                                try:
                                    decoded = json.loads('"' + raw + '"')
                                except Exception:
                                    try:
                                        decoded = json.loads('"' + raw.rstrip('\\') + '"')
                                    except Exception:
                                        decoded = raw.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                                if len(decoded) > _doc_last_len:
                                    _doc_last_len = len(decoded)
                                    yield f'data: {json.dumps({"type": "doc_stream_delta", "content": decoded})}\n\n'
                    elif data.get("type") == "tool_calls":
                        native_tool_calls = data.get("calls", [])
                        logger.info(f"Agent round {round_num}: received {len(native_tool_calls)} native tool call(s)")
                    elif data.get("type") == "usage":
                        u = data.get("data", {})
                        round_input = u.get("input_tokens", 0)
                        real_input_tokens += round_input
                        real_output_tokens += u.get("output_tokens", 0)
                        last_round_input_tokens = round_input
                        has_real_usage = True
                    elif "delta" in data:
                        if not first_token_received:
                            time_to_first_token = time.time() - total_start
                            first_token_received = True
                        # Keep reasoning deltas in a separate accumulator so
                        # we can echo them back via `reasoning_content` on the
                        # next request (DeepSeek requires this; harmless for
                        # other vendors). Regular content still flows into
                        # round_response unchanged.
                        if data.get("thinking"):
                            round_reasoning += data["delta"]
                        else:
                            round_response += data["delta"]
                            full_response += data["delta"]
                        yield chunk  # Stream all rounds
                        # Detect text-fence doc streaming for rounds 2+
                        # (round 1 is handled by frontend fence detection + server fenced block path)
                        if round_num > 1 and not _doc_acc:
                            _fence_marker = '```create_document\n'
                            # Open a new block if we're not currently inside one
                            # and there's an unstreamed marker in the response.
                            # The marker search starts at the byte after the
                            # last block's closing fence so the SECOND
                            # `create_document` block in the same round gets
                            # detected (previously only the first one was
                            # streamed and the rest were silently dropped).
                            if not _doc_opened and _fence_marker in round_response[_doc_scan_from:]:
                                _fi = round_response.index(_fence_marker, _doc_scan_from)
                                _fa = round_response[_fi + len(_fence_marker):]
                                _fl = _fa.split('\n')
                                if _fl and _fl[0].strip():
                                    _doc_opened = True
                                    _ft = _fl[0].strip()
                                    _kl = {'python','py','javascript','js','typescript','ts','html','css','json','yaml','bash','sql','rust','go','java','c','cpp','markdown','text'}
                                    _flang = _fl[1].strip() if len(_fl) > 1 and _fl[1].strip().lower() in _kl else ''
                                    _doc_fence_offset = _fi + len(_fence_marker) + len(_fl[0]) + 1
                                    if _flang:
                                        _doc_fence_offset += len(_fl[1]) + 1
                                    _doc_last_len = 0
                                    yield f'data: {json.dumps({"type": "doc_stream_open", "title": _ft, "language": _flang})}\n\n'
                            if _doc_opened:
                                _rc = round_response[_doc_fence_offset:]
                                _ci = _rc.find('\n```')
                                if _ci >= 0:
                                    _rc = _rc[:_ci]
                                if len(_rc) > _doc_last_len:
                                    _doc_last_len = len(_rc)
                                    yield f'data: {json.dumps({"type": "doc_stream_delta", "content": _rc})}\n\n'
                                # If the closing fence has arrived, finalise
                                # this block and arm detection of the NEXT
                                # one. The model can emit multiple
                                # `create_document` blocks in a single round.
                                if _ci >= 0:
                                    _doc_opened = False
                                    _doc_scan_from = _doc_fence_offset + _ci + len('\n```')
                                    _doc_fence_offset = 0
                                    _doc_last_len = 0
                    elif data.get("error"):
                        err_msg = data.get("error", "unknown")
                        logger.error(f"Agent round {round_num}: stream error: {err_msg}")
                        yield f'data: {json.dumps({"delta": chr(10) + chr(10) + "*[Stream error: " + str(err_msg) + "]*"})}\n\n'
                except json.JSONDecodeError:
                    if round_num == 1:
                        yield chunk
            elif chunk.startswith("event: "):
                # Forward error events to frontend as visible text
                yield chunk
            # Intercept [DONE] — don't forward until all rounds finish

        tool_blocks, used_native = _resolve_tool_blocks(round_response, native_tool_calls, round_num)

        # Force-answer round: we told the model to STOP calling tools and
        # answer. If it ignored that and emitted a (possibly DSML) tool
        # call anyway, discard it — don't execute, don't re-loop. Keep
        # only the prose; if there's none, emit a graceful fallback.
        if _force_answer:
            if tool_blocks:
                logger.info(f"[agent] force-answer round {round_num}: discarding {len(tool_blocks)} ignored tool call(s)")
            tool_blocks = []
            if not _THINK_RE.sub("", strip_tool_blocks(round_response)).strip():
                # The model burned its budget gathering data but never wrote a
                # final answer (common with weaker models on multi-source
                # briefings). Salvage it: one blunt non-streaming synthesis call
                # over the full conversation (which already holds every tool
                # result) before falling back to the canned apology.
                _synth = ""
                try:
                    from src.llm_core import llm_call_async
                    _synth_messages = list(messages) + [{
                        "role": "user",
                        "content": (
                            "Using ONLY the information already gathered above, write "
                            "the final answer for the user now. Do NOT call any tools, "
                            "do NOT explain your reasoning — output the finished response "
                            "directly. If some data couldn't be fetched, just work with "
                            "what you have and note what's missing in one short line."
                        ),
                    }]
                    _raw = await llm_call_async(
                        url=endpoint_url, model=model, messages=_synth_messages,
                        headers=headers, temperature=0.3, max_tokens=max_tokens, timeout=60,
                    )
                    _synth = _THINK_RE.sub("", strip_tool_blocks(_raw or "")).strip()
                except Exception as _e:
                    logger.warning(f"[agent] grace synthesis failed: {_e}")
                if _synth:
                    yield f'data: {json.dumps({"delta": _synth})}\n\n'
                    full_response += _synth
                else:
                    _fb = ("I gathered some search results but couldn't pull a clean "
                           "answer together. Want me to try a more specific question, "
                           "or summarize what I did find?")
                    yield f'data: {json.dumps({"delta": _fb})}\n\n'
                    full_response += _fb

        # ── Fallback: auto-create document if model dumped large code in chat ──
        # If no create_document tool was used, check for big code blocks in text
        has_doc_tool = any(
            b.tool_type in ("create_document", "update_document")
            for b in tool_blocks
        ) or any(
            tc.get("name") in ("create_document", "update_document")
            for tc in native_tool_calls
        )
        if not has_doc_tool and session_id and "create_document" not in (disabled_tools or set()):
            _code_block_re = re.compile(r'```(\w*)\n([\s\S]*?)```')
            for m in _code_block_re.finditer(round_response):
                lang_tag = m.group(1).lower()
                code_body = m.group(2).strip()
                # Skip small blocks and known tool tags
                if code_body.count('\n') < 30:
                    continue
                if lang_tag in TOOL_TAGS:
                    continue  # already handled as a tool execution
                # Auto-create a document from this code block
                lang_map = {"py": "python", "js": "javascript", "ts": "typescript", "": "text"}
                doc_lang = lang_map.get(lang_tag, lang_tag or "text")
                doc_title = f"Code ({doc_lang})"
                tb = ToolBlock("create_document", f"{doc_title}\n{doc_lang}\n{code_body}")
                tool_blocks.append(tb)
                # Stream the document open event
                yield f'data: {json.dumps({"type": "doc_stream_open", "title": doc_title, "language": doc_lang})}\n\n'
                yield f'data: {json.dumps({"type": "doc_stream_delta", "content": code_body})}\n\n'
                logger.info(f"Auto-created document from {lang_tag} code block ({code_body.count(chr(10))+1} lines)")
                break  # only auto-create one document per round

        # Save cleaned round text for history persistence
        # Keep <think> blocks so they render in the thinking section on reload
        cleaned_round = strip_tool_blocks(round_response).strip()
        round_texts.append(cleaned_round)

        if not tool_blocks:
            # ── Completion verifier (mechanism 3a) ────────────────────
            # The model is finishing. If this was an effectful agentic turn,
            # have a fresh-context verifier independently check the work
            # before we accept "done". On FAIL, surface the issues and let
            # the model fix them (capped, and it must do new effectful work
            # to re-trigger). Skipped on force-answer rounds (no tools to
            # fix with), pure Q&A, and when the toggle is off.
            _claimed_done = bool(_THINK_RE.sub("", cleaned_round).strip())
            if (_effectful_used and not _force_answer
                    and _claimed_done
                    and _verifier_rounds < _VERIFIER_MAX_ROUNDS
                    # Default OFF: on weak local models the verifier can't judge
                    # from the action-snapshot (no doc body), so it false-rejects
                    # ("content not shown") and forces a costly extra round every
                    # effectful turn. Opt-in via setting for strong models.
                    and get_setting("agent_verifier_subagent", False)):
                # Brief "working" indicator while the verifier runs.
                yield f'data: {json.dumps({"type": "agent_step", "round": round_num})}\n\n'
                _vfail = await _run_verifier_subagent(
                    _verifier_instruction,
                    _build_actions_snapshot(tool_events),
                    endpoint_url=endpoint_url, model=model, headers=headers,
                )
                if _vfail:
                    _verifier_rounds += 1
                    logger.info(f"[agent] verifier flagged {len(_vfail)} issue(s) on round {round_num}: {_vfail}")
                    _note = "\n\n_Double-checked the work and found something to fix._\n\n"
                    yield f'data: {json.dumps({"delta": _note})}\n\n'
                    full_response += _note
                    messages.append({
                        "role": "system",
                        "content": (
                            "An independent verifier reviewed your work against the "
                            "original request and found issues that must be fixed before "
                            "this is actually done:\n- " + "\n- ".join(_vfail) +
                            "\n\nFix these now using tools, then finish."
                        ),
                    })
                    # Require fresh effectful work before verifying again, so we
                    # never re-verify an unchanged state in a loop.
                    _effectful_used = False
                    continue
            break  # no tools — done

        # ── Loop-breaker (Terminus-style stall detector) ──────────────
        # Stall detector for repeated no-progress tool loops.
        # A round is "useless" ONLY when it re-issues a recent tool call AND
        # writes no answer text — i.e. the model is going in circles.
        # Genuine exploration (new, distinct calls) is never useless, so
        # multi-step work (file hunts, multi-host ssh, build→test→fix) rides
        # all the way to a real answer. We bail only on a streak of useless
        # rounds, or a single tool fired an absurd number of times (hard
        # runaway backstop). On bail we don't give up — we force one
        # tool-free round so the model declares done or declares blocked,
        # mirroring Terminus's explicit-completion handshake.
        _sig = "|".join(sorted(f"{b.tool_type}:{(b.content or '').strip()[:120]}" for b in tool_blocks))
        _is_repeat = _sig in _recent_call_sigs
        _recent_call_sigs.append(_sig)
        for _b in tool_blocks:
            _tool_type_counts[_b.tool_type] += 1
        # "Real" answer text = round text minus <think> blocks. Empty-think
        # rounds (just "<think>\n\n</think>" + a tool call) must not read as
        # progress, so strip think before checking.
        _real_text = _THINK_RE.sub("", cleaned_round).strip()
        # Circling = repeating a recent call with nothing written. Any
        # progress (a NEW distinct call, or actual answer text) resets it.
        if _is_repeat and not _real_text:
            _stuck_rounds += 1
        else:
            _stuck_rounds = 0
        _runaway = next((t for t, n in _tool_type_counts.items() if n >= 15), None)
        if _stuck_rounds >= 4 or _runaway:
            reason = (f"calling {_runaway} over and over" if _runaway
                      else "repeating the same tool calls without new progress")
            logger.warning(f"[agent] loop-breaker tripped on round {round_num} ({reason}); sig={_sig[:80]!r}")
            # The model has been executing tools, so its results are already
            # in context. Force ONE tool-free round to converge: write the
            # answer from what it has, or state plainly what's blocking it.
            # The force-answer handler above salvages (grace synthesis) or
            # apologizes honestly if it still writes nothing.
            _off = [t for t in ("web_search", "bash")
                    if disabled_tools and t in disabled_tools]
            _off_note = (f" ({', '.join(_off)} is currently disabled — say so if "
                         f"you needed it.)" if _off else "")
            _force_answer = True
            messages.append({
                "role": "system",
                "content": (
                    "You're repeating tool calls without converging. STOP calling "
                    "tools and end the turn one of two ways: (a) write your best "
                    "final answer NOW from the information already gathered, or "
                    "(b) if you're genuinely blocked, say plainly what's blocking "
                    "you in a sentence or two." + _off_note
                ),
            })
            full_response += "\n\n"
            yield f'data: {json.dumps({"type": "agent_step", "round": round_num + 1})}\n\n'
            continue

        # Pre-stream document content for fenced tool blocks (non-native path)
        # Native path already streamed via tool_call_delta above
        # For round 1 fenced blocks, frontend fence detection already handled streaming
        if not _doc_opened and round_num == 1:
            for block in tool_blocks:
                if block.tool_type == "create_document":
                    _doc_opened = True
                    break

        if not _doc_opened:
            for block in tool_blocks:
                if block.tool_type == "create_document":
                    lines = block.content.strip().split("\n")
                    title = lines[0].strip() if lines else "Untitled"
                    lang = ""
                    content_start = 1
                    if len(lines) > 1 and len(lines[1].strip()) < 20 and lines[1].strip().isalpha():
                        lang = lines[1].strip()
                        content_start = 2
                    content = "\n".join(lines[content_start:]) if len(lines) > content_start else ""
                    yield f'data: {json.dumps({"type": "doc_stream_open", "title": title, "language": lang})}\n\n'
                    if content:
                        yield f'data: {json.dumps({"type": "doc_stream_delta", "content": content})}\n\n'
                    break
                elif block.tool_type == "update_document":
                    # Pre-stream the full replacement content so user sees it immediately
                    content = block.content.strip()
                    yield f'data: {json.dumps({"type": "doc_stream_open", "title": "", "language": ""})}\n\n'
                    yield f'data: {json.dumps({"type": "doc_stream_delta", "content": content})}\n\n'
                    break

        # Execute each tool block
        tool_results = []
        tool_result_texts = []  # plain text for native tool role messages
        budget_hit = False
        for i, block in enumerate(tool_blocks):
            # --- Tool budget check ---
            if max_tool_calls > 0 and total_tool_calls >= max_tool_calls:
                yield f'data: {json.dumps({"type": "budget_exceeded", "limit": max_tool_calls, "used": total_tool_calls})}\n\n'
                budget_hit = True
                break

            total_tool_calls += 1
            # Build a short display string for the frontend tool bubble.
            # Document tools show a brief summary instead of dumping full content.
            is_doc_tool = block.tool_type in ("create_document", "update_document", "edit_document", "suggest_document")
            if is_doc_tool:
                cmd_display = block.content.split("\n")[0].strip()[:80]
            else:
                cmd_display = block.content.strip()

            yield (
                f'data: {json.dumps({"type": "tool_start", "tool": block.tool_type, "command": cmd_display, "round": round_num})}\n\n'
            )

            # Streaming progress for long-running tools (bash, python).
            # The bash/python branches inside _direct_fallback emit
            # periodic {elapsed_s, tail} payloads via this callback;
            # we forward each one as a `tool_progress` SSE event so
            # the UI can render live elapsed-time + tail-of-output.
            _progress_q: asyncio.Queue = asyncio.Queue()
            async def _push_progress(payload):
                await _progress_q.put(payload)

            async def _run_tool():
                try:
                    return await execute_tool_block(
                        block,
                        session_id=session_id,
                        disabled_tools=disabled_tools,
                        owner=owner,
                        progress_cb=_push_progress,
                    )
                finally:
                    # Sentinel so the drainer knows to stop.
                    await _progress_q.put(None)

            _tool_task = asyncio.create_task(_run_tool())
            # Drain progress events as they arrive — block until the
            # next event OR the tool finishes (sentinel = None).
            while True:
                evt = await _progress_q.get()
                if evt is None:
                    break
                yield (
                    f'data: {json.dumps({"type": "tool_progress", "tool": block.tool_type, "round": round_num, **evt})}\n\n'
                )
            desc, result = await _tool_task

            # Extract structured web sources from web_search tool output
            _src_text = result.get("results") or result.get("stdout") or ""
            if block.tool_type == "web_search" and _src_text:
                _src_marker = "<!-- SOURCES:"
                _src_idx = _src_text.find(_src_marker)
                if _src_idx >= 0:
                    _src_end = _src_text.find(" -->", _src_idx)
                    if _src_end >= 0:
                        try:
                            _extracted_sources = json.loads(_src_text[_src_idx + len(_src_marker):_src_end])
                            yield f'data: {json.dumps({"type": "web_sources", "data": _extracted_sources})}\n\n'
                            # Strip the marker from the result so it doesn't show in chat
                            _clean = _src_text[:_src_idx].rstrip()
                            if "results" in result:
                                result["results"] = _clean
                            elif "stdout" in result:
                                result["stdout"] = _clean
                        except (json.JSONDecodeError, Exception):
                            pass

            # Emit doc-specific event for document tools — the frontend
            # document panel handles this; no need to show content in chat.
            if is_doc_tool and "action" in result:
                if result["action"] == "suggest":
                    yield (
                        f'data: {json.dumps({"type": "doc_suggestions", "doc_id": result["doc_id"], "suggestions": result["suggestions"]})}\n\n'
                    )
                else:
                    yield (
                        f'data: {json.dumps({"type": "doc_update", "doc_id": result["doc_id"], "content": result["content"], "version": result["version"], "title": result.get("title", ""), "language": result.get("language")})}\n\n'
                    )

            # Emit ui_control event for frontend to apply UI changes
            if "ui_event" in result:
                yield (
                    f'data: {json.dumps({"type": "ui_control", "data": result})}\n\n'
                )

            # Build output for frontend tool bubble.
            # Document tools get a short summary — content goes to the editor panel.
            output_text = ""
            if is_doc_tool and "action" in result:
                action = result["action"]
                title = result.get("title", "")
                ver = result.get("version", "?")
                if action == "create":
                    output_text = f'Document created: "{title}" (v{ver})'
                elif action == "edit":
                    output_text = f'Document edited: "{title}" (v{ver}, {result.get("applied", 0)} edit(s))'
                elif action == "update":
                    output_text = f'Document updated: "{title}" (v{ver})'
            elif "stdout" in result:
                # On a bash/python timeout the result carries error + (often
                # empty) stdout/stderr; fall back to the error so the "timed
                # out" reason reaches the UI instead of a blank result.
                output_text = (result["stdout"] or result["stderr"] or result.get("error", ""))[:2000]
            elif "output" in result:
                # bash / python canonical result: {"output": ..., "exit_code": ...}
                output_text = (result["output"] or "")[:2000]
            elif "response" in result:
                # AI interaction tools (chat_with_model, send_to_session)
                label = result.get("model", result.get("session_name", "AI"))
                output_text = f"{label}: {result['response']}"[:4000]
            elif "content" in result:
                output_text = result["content"][:2000]
            elif "results" in result:
                output_text = result["results"][:4000]
            elif "session_id" in result and "name" in result:
                output_text = f"Session created: {result['name']} (id: {result['session_id']})"
            elif "success" in result:
                output_text = (
                    f"Written: {result.get('path', '')}"
                    if result["success"]
                    else f"Error: {result.get('error', '')}"
                )
            elif "error" in result:
                output_text = result["error"][:2000]

            # Emit tool_output (include ui_event data if present)
            tool_output_data = {"type": "tool_output", "tool": block.tool_type, "command": cmd_display, "output": output_text, "exit_code": result.get("exit_code")}
            if "ui_event" in result:
                tool_output_data["ui_event"] = result["ui_event"]
                for k in ("toggle_name", "state", "mode", "model", "endpoint_url", "theme_name", "colors"):
                    if k in result:
                        tool_output_data[k] = result[k]
            # Forward image data from generate_image tool
            for k in ("image_url", "image_prompt", "image_model", "image_size", "image_quality"):
                if k in result:
                    tool_output_data[k] = result[k]
            # Forward screenshots from browser tools (base64 images)
            if result.get("images"):
                img = result["images"][0]
                tool_output_data["screenshot"] = f"data:{img['mimeType']};base64,{img['data']}"
            yield f'data: {json.dumps(tool_output_data)}\n\n'

            # Native document tools open in the editor + carry the REAL doc id.
            # Emit a doc_update so the frontend opens/activates it and sends it
            # back as active_doc_id next turn (otherwise the agent can't "see"
            # the document it just created on the follow-up message).
            if block.tool_type in ("create_document", "update_document", "edit_document") and result.get("doc_id"):
                yield (
                    'data: ' + json.dumps({
                        "type": "doc_update",
                        "doc_id": result["doc_id"],
                        "title": result.get("title", ""),
                        "language": result.get("language", ""),
                        "content": result.get("content", ""),
                        "version": result.get("version", 1),
                    }) + '\n\n'
                )

            # Inline research: emit the open-link as part of the assistant's
            # actual response text — a `#research-<id>` anchor that chatRenderer
            # turns into a regular clickable link. Saved with the message, so it
            # PERSISTS across refresh (unlike the old ephemeral injected chip).
            _rsid = result.get("research_session_id")
            if _rsid:
                _anchor = f"\n\n[Open in Deep Research](#research-{_rsid})\n"
                yield 'data: ' + json.dumps({"delta": _anchor}) + '\n\n'

            # Save for history persistence
            tool_event = {
                "round": round_num,
                "tool": block.tool_type,
                "command": cmd_display,
                "output": output_text,
                "exit_code": result.get("exit_code"),
            }
            if result.get("image_url"):
                for ik in ("image_url", "image_prompt", "image_model", "image_size", "image_quality"):
                    if result.get(ik):
                        tool_event[ik] = result[ik]
            if result.get("doc_id"):
                tool_event["doc_id"] = result["doc_id"]
                tool_event["doc_title"] = result.get("title", "")
            tool_events.append(tool_event)
            if block.tool_type in _VERIFIER_EFFECTFUL_TOOLS:
                _effectful_used = True

            formatted = format_tool_result(desc, result)
            tool_results.append(formatted)
            tool_result_texts.append(formatted)

        # If budget was hit, stop the loop
        if budget_hit:
            break

        # Feed results back to LLM for next round
        _append_tool_results(messages, round_response, native_tool_calls,
                             tool_results, tool_result_texts, used_native, round_num,
                             round_reasoning=round_reasoning)

        # Emit agent_step event
        yield (
            f'data: {json.dumps({"type": "agent_step", "round": round_num + 1})}\n\n'
        )

        # Separator in accumulated response
        full_response += "\n\n"

    # --- Final metrics ---
    total_duration = time.time() - total_start
    metrics = _compute_final_metrics(
        messages, full_response, total_duration, time_to_first_token,
        context_length, real_input_tokens, real_output_tokens,
        has_real_usage, tool_events, round_texts, model=model,
        last_round_input_tokens=last_round_input_tokens,
        prep_timings=prep_timings,
    )
    yield f"data: {json.dumps({'type': 'metrics', 'data': metrics})}\n\n"

    # Teacher-escalation: inline takeover visible in the chat stream.
    # The student just finished; if Tier 1 flags failure, the teacher
    # gets a turn (with its own tool calls forwarded to the user) and
    # a skill is saved ONLY if the teacher actually succeeds. Skipped
    # when we ARE the teacher to avoid recursion.
    if not _is_teacher_run:
        try:
            from src.teacher_escalation import run_teacher_inline
            async for evt in run_teacher_inline(
                student_endpoint_url=endpoint_url,
                student_messages=messages,
                student_tool_events=tool_events,
                student_reply=full_response,
                owner=owner,
            ):
                yield evt
        except Exception as _esc_err:
            logger.warning(f"teacher escalation hook failed: {_esc_err}", exc_info=True)

    yield "data: [DONE]\n\n"
