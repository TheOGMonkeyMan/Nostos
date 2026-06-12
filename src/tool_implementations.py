"""
tool_implementations.py

Extracted tool implementation functions (do_* and helpers) from agent_tools.py.
These handle the actual execution logic for each tool type.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

MAX_OUTPUT_CHARS = 10_000
MAX_READ_CHARS = 20_000


def get_mcp_manager():
    from src import agent_tools
    return agent_tools.get_mcp_manager()


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    return text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_tool_args(content):
    """Parse a tool-call argument blob.

    Accepts either a JSON string or an already-decoded dict. Unwraps the
    common `{"body": {...}}` envelope that smaller models emit when they
    read tool descriptions like "Body is JSON: {...}" literally — they
    pass `body` as a field name rather than treating it as a noun.

    Returns a dict on success, raises ValueError on bad JSON.
    """
    if isinstance(content, str):
        try:
            args = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(str(e))
    elif isinstance(content, dict):
        args = content
    else:
        args = {}
    # Unwrap {"body": {...}} envelope — but only if `body` is the sole key
    # and points at a dict. We don't want to clobber a legitimate `body`
    # field on tools where it's a real arg (e.g. send_email body text).
    if (
        isinstance(args, dict)
        and len(args) == 1
        and "body" in args
        and isinstance(args["body"], dict)
        and "action" in args["body"]  # extra safety: only unwrap if the inner dict looks like a tool call
    ):
        args = args["body"]
    return args


# ---------------------------------------------------------------------------
# Search chats
# ---------------------------------------------------------------------------

async def do_search_chats(query: str, limit: int = 20, owner: str | None = None) -> Dict:
    """Search past chat messages for the calling user's sessions only.

    Without an owner filter this used to leak EVERY user's chat history
    into the agent's `search_chats` results (v2 review HIGH-11). The
    caller in `tool_execution.execute_tool_block` now plumbs the owner
    through; legacy callers without owner pass through as before but
    will only see legacy/null-owner rows.
    """
    from src.database import SessionLocal, ChatMessage as DBChatMessage, Session as DBSession
    # Escape LIKE wildcards in the user-supplied query so a stray % or _
    # doesn't widen the match (and to keep the response deterministic).
    safe_q = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    db = SessionLocal()
    try:
        q = (
            db.query(DBChatMessage, DBSession.id, DBSession.name)
            .join(DBSession, DBChatMessage.session_id == DBSession.id)
            .filter(
                DBSession.archived == False,
                DBChatMessage.content.ilike(f"%{safe_q}%", escape="\\"),
                DBChatMessage.role.in_(["user", "assistant"]),
            )
        )
        if owner is not None:
            # Restrict to this user's sessions plus legacy null-owner
            # rows (so single-user upgrades keep seeing their own data).
            q = q.filter((DBSession.owner == owner) | (DBSession.owner.is_(None)))
        rows = q.order_by(DBChatMessage.timestamp.desc()).limit(limit).all()

        if not rows:
            return {"results": f"No chats found matching \"{query}\"."}

        # Group by session to avoid duplicate links
        seen_sessions = {}
        for msg, session_id, session_name in rows:
            if session_id not in seen_sessions:
                content = msg.content or ""
                lower_content = content.lower()
                idx = lower_content.find(query.lower())
                if idx == -1:
                    snippet = content[:150]
                else:
                    start = max(0, idx - 60)
                    end = min(len(content), idx + len(query) + 60)
                    snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
                seen_sessions[session_id] = {
                    "name": session_name or "Untitled",
                    "snippet": snippet,
                    "role": msg.role,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                }

        lines = [f"Found {len(seen_sessions)} session(s) matching \"{query}\":\n"]
        for sid, info in seen_sessions.items():
            lines.append(f"- **{info['name']}** (#{sid})")
            lines.append(f"  Link: [Open chat](#{sid})")
            lines.append(f"  > {info['snippet']}")
            lines.append("")

        return {"results": "\n".join(lines)}
    except Exception as e:
        logger.error(f"search_chats failed: {e}")
        return {"error": str(e), "exit_code": 1}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API call tool
# ---------------------------------------------------------------------------

async def do_api_call(content: str) -> Dict:
    """Execute an API call to a registered integration."""
    from src.integrations import execute_api_call, load_integrations
    try:
        args = json.loads(content)
    except json.JSONDecodeError:
        # Try line-based format: integration\nmethod path\nbody
        lines = content.strip().split("\n")
        args = {"integration": lines[0].strip() if lines else ""}
        if len(lines) > 1:
            parts = lines[1].strip().split(" ", 1)
            args["method"] = parts[0] if parts else "GET"
            args["path"] = parts[1] if len(parts) > 1 else "/"
        if len(lines) > 2:
            try:
                args["body"] = json.loads("\n".join(lines[2:]))
            except json.JSONDecodeError:
                pass

    integration_name = args.get("integration", "")
    integrations = load_integrations()
    intg = next((i for i in integrations if i["id"] == integration_name
                 or i["name"].lower() == integration_name.lower()), None)
    if not intg:
        available = ", ".join(i["name"] for i in integrations if i.get("enabled", True))
        return {"error": f"No integration matching '{integration_name}'. Available: {available or 'none configured'}", "exit_code": 1}

    return await execute_api_call(
        intg["id"],
        args.get("method", "GET"),
        args.get("path", "/"),
        params=args.get("params"),
        body=args.get("body"),
        extra_headers=args.get("headers"),
    )


# ── Gallery tools ──

async def do_edit_image(content: str, owner: Optional[str] = None) -> Dict:
    """Edit a gallery image (upscale, rembg, inpaint, harmonize)."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    image_id = args.get("image_id", "")
    action = args.get("action", "")
    if not image_id or not action:
        return {"error": "image_id and action are required", "exit_code": 1}
    payload = {"image_id": image_id}
    if args.get("prompt"):
        payload["prompt"] = args["prompt"]
    if args.get("scale"):
        payload["scale"] = args["scale"]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"http://localhost:7000/api/gallery/{action}", json=payload)
            data = resp.json()
        if data.get("success") or data.get("id"):
            return {"output": f"Image edited ({action}). New image ID: {data.get('id', '?')}", "exit_code": 0}
        return {"error": data.get("error", f"{action} failed"), "exit_code": 1}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


# --- Phase 2.2 (ADR-031): research tools moved to src/tools/research.py ----
# Re-exported so callers importing from src.tool_implementations keep working
# (notably src/tool_execution.py). See DECISIONS.md ADR-031.
from src.tools.research import (  # noqa: E402,F401
    do_manage_research,
    do_trigger_research,
)


# --- Phase 2.2 (ADR-030): contact tools moved to src/tools/contacts.py -----
# Re-exported so callers importing from src.tool_implementations keep working
# (notably src/tool_execution.py). See DECISIONS.md ADR-030.
from src.tools.contacts import (  # noqa: E402,F401
    do_resolve_contact,
    do_manage_contact,
)


# --- Phase 2.2 (ADR-029): vault tools moved to src/tools/vault.py -----------
# Re-exported so callers importing from src.tool_implementations keep working
# (notably src/tool_execution.py). See DECISIONS.md ADR-029.
from src.tools.vault import (  # noqa: E402,F401
    do_vault_search,
    do_vault_get,
    do_vault_unlock,
)


# --- Phase 2.2 (ADR-032): cookbook/model-serving tools moved to src/tools/cookbook.py
# Re-exported so callers importing from src.tool_implementations keep working
# (src/tool_execution.py, and research.py's lazy _COOKBOOK_BASE / _internal_headers).
# See DECISIONS.md ADR-032.
from src.tools.cookbook import (  # noqa: E402,F401
    _COOKBOOK_BASE,
    _internal_headers,
    do_app_api,
    do_download_model,
    do_serve_model,
    do_list_served_models,
    do_stop_served_model,
    do_list_downloads,
    do_cancel_download,
    do_search_hf_models,
    do_adopt_served_model,
    do_list_cookbook_servers,
    do_list_serve_presets,
    do_serve_preset,
    do_list_cached_models,
)


# --- Phase 2.2 (ADR-033): notes + calendar tools moved to src/tools/notes_calendar.py
# Re-exported so callers importing from src.tool_implementations keep working
# (src/tool_execution.py, task_scheduler, email_pollers). See DECISIONS.md ADR-033.
from src.tools.notes_calendar import (  # noqa: E402,F401
    do_manage_notes,
    do_manage_calendar,
)


# --- Phase 2.2 (ADR-034): manage_* tools moved to src/tools/management.py
# Re-exported so callers importing from src.tool_implementations keep working
# (tool_execution, agent_tools, teacher_escalation, cookbook.py). See DECISIONS.md ADR-034.
from src.tools.management import (  # noqa: E402,F401
    do_manage_skills,
    do_manage_tasks,
    do_manage_endpoints,
    do_manage_mcp,
    do_manage_webhooks,
    do_manage_tokens,
    do_manage_documents,
    do_manage_settings,
)


# --- Phase 2.2 (ADR-035): documents group moved to src/tools/documents.py
# Re-exported so callers importing from src.tool_implementations keep working
# (agent_loop, agent_tools, chat_routes, document_routes, pdf_form_doc,
# tool_execution, management.py). See DECISIONS.md ADR-035.
from src.tools.documents import (  # noqa: E402,F401
    set_active_document,
    set_active_model,
    get_active_document,
    _sniff_doc_language,
    _looks_like_email_document,
    _coerce_email_document_content,
    parse_edit_blocks,
    parse_suggest_blocks,
    do_create_document,
    do_update_document,
    do_edit_document,
    do_suggest_document,
)
