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
# Active document state
# ---------------------------------------------------------------------------

_active_document_id: Optional[str] = None
_active_model: Optional[str] = None


def set_active_document(doc_id: Optional[str]):
    """Set the active document ID for document tool execution."""
    global _active_document_id
    _active_document_id = doc_id


def set_active_model(model: Optional[str]):
    """Set the current model name for version summaries."""
    global _active_model
    _active_model = model


def get_active_document():
    return _active_document_id


# ---------------------------------------------------------------------------
# Document tools — create/update/edit/suggest living documents
# ---------------------------------------------------------------------------

def _sniff_doc_language(text: str) -> str:
    """Best-effort detect a document's language from its content when the model
    didn't specify one. Defaults to 'markdown' (prose). Recognizes the common
    markup/code types the editor supports so e.g. an SVG isn't saved as markdown."""
    import json as _json, re as _re2
    s = (text or "").strip()
    if not s:
        return "markdown"
    head = s[:600]
    hl = head.lower()
    if _looks_like_email_document(s):
        return "email"
    # Markup (unambiguous)
    if "<svg" in hl:
        return "svg"
    if hl.startswith("<?xml"):
        return "xml"
    if (hl.startswith("<!doctype html") or hl.startswith("<html")
            or _re2.search(r"<(div|body|head|p|span|table|button|h[1-6]|ul|ol|li|img)\b", hl)):
        return "html"
    # JSON
    if s[0] in "{[":
        try:
            _json.loads(s)
            return "json"
        except Exception:
            pass
    # Shebang
    first = s.split("\n", 1)[0].strip().lower()
    if first.startswith("#!"):
        return "python" if "python" in first else "bash"
    # Code by strong leading signals (line-anchored so prose with stray words won't match)
    if _re2.search(r"(?m)^\s*(def \w|class \w|import \w|from \w[\w.]* import )", s):
        return "python"
    if _re2.search(r"(?m)^\s*(function \w|const \w|let \w|export |import .* from )", s):
        return "javascript"
    if _re2.search(r"(?mi)^\s*(select .* from |create table |insert into |update \w)", s):
        return "sql"
    if _re2.search(r"(?m)^[.#]?[\w-]+\s*\{[^{}]*:[^{}]*;", s):
        return "css"
    return "markdown"


def _looks_like_email_document(text: str = "", title: str = "") -> bool:
    import re as _re
    title_l = (title or "").strip().lower()
    if title_l in {"new email", "new mail", "new message"}:
        return True
    s = (text or "").lstrip()
    if "\n---\n" in s and _re.search(r"(?im)^To:\s*", s) and _re.search(r"(?im)^Subject:\s*", s):
        return True
    return bool(_re.search(r"(?im)^To:\s*", s) and _re.search(r"(?im)^Subject:\s*", s))


def _coerce_email_document_content(existing: str, incoming: str) -> str:
    """Keep email docs in the To/Subject/---/body shape even if a model writes
    only the body or dumps header labels without the separator."""
    import re as _re
    old = existing or ""
    new = (incoming or "").strip()
    if "\n---\n" in new:
        return new
    header = old.split("\n---\n", 1)[0] if "\n---\n" in old else "To: \nSubject: "
    if _looks_like_email_document(new):
        lines = new.splitlines()
        last_header_idx = -1
        header_re = _re.compile(r"^(To|Cc|Bcc|Subject|In-Reply-To|References|X-Source-UID|X-Source-Folder|X-Attachments):", _re.I)
        for i, line in enumerate(lines):
            if header_re.match(line.strip()):
                last_header_idx = i
        body_lines = lines[last_header_idx + 1:] if last_header_idx >= 0 else lines
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        body = "\n".join(body_lines).strip()
    else:
        body = new
    return header.rstrip() + "\n---\n" + body


async def do_create_document(content_block: str, session_id: Optional[str] = None) -> Dict:
    """Create a new document. Supports two formats:
      1) Line-based: line 1 = title, line 2 (optional) = language, rest = content
      2) XML-like tags: <title>...</title><language>...</language><content>...</content>
    Some models mix them — strip any XML-style tags and fall back to line parsing."""
    import uuid, re as _re
    from src.database import SessionLocal, Document, DocumentVersion, Session as DbSession

    raw = content_block or ""

    # Known languages the editor understands (match the <select> in HTML)
    _KNOWN_LANGS = {
        "python", "javascript", "typescript", "html", "css", "markdown", "json",
        "yaml", "bash", "sql", "rust", "go", "java", "c", "cpp", "xml", "toml",
        "ini", "ruby", "php", "csv", "email", "text", "plain", "svg",
    }

    # Try XML tag extraction first
    title = None
    language = None
    content = None
    mt = _re.search(r"<title>\s*(.*?)\s*</title>", raw, _re.DOTALL | _re.IGNORECASE)
    ml = _re.search(r"<language>\s*(.*?)\s*</language>", raw, _re.DOTALL | _re.IGNORECASE)
    mc = _re.search(r"<content>\s*(.*?)\s*</content>", raw, _re.DOTALL | _re.IGNORECASE)
    if mt or mc:
        title = mt.group(1).strip() if mt else None
        language = ml.group(1).strip().lower() if ml else None
        content = mc.group(1) if mc else None

    # Fall back to line-based parsing. First strip any stray XML-ish tags.
    if title is None or content is None:
        cleaned = _re.sub(r"</?(?:title|language|content)>", "", raw)
        lines = cleaned.strip().split("\n")
        if title is None:
            title = lines[0].strip() if lines else "Untitled"
            lines = lines[1:]
        # Only consume second line as language if it looks like a valid short lang token
        if language is None and lines:
            candidate = lines[0].strip().lower()
            if candidate and len(candidate) < 20 and " " not in candidate and candidate in _KNOWN_LANGS:
                language = candidate
                lines = lines[1:]
        if content is None:
            content = "\n".join(lines)

    # Validate language: must be in known set, else default based on content
    if language and language not in _KNOWN_LANGS:
        language = None
    if not language:
        # No explicit language — sniff it from the content so an SVG / HTML / JSON
        # / code document isn't silently saved as markdown. Prose → markdown.
        language = _sniff_doc_language(content)
    if _looks_like_email_document(content, title):
        language = "email"

    if not title:
        title = "Untitled"

    if not session_id:
        return {"error": "No session context for document creation"}

    db = SessionLocal()
    try:
        doc_id = str(uuid.uuid4())
        ver_id = str(uuid.uuid4())

        # Inherit ownership from the chat session so the doc survives that
        # session later being deleted (session_id → NULL).
        _sess = db.query(DbSession).filter(DbSession.id == session_id).first()
        _owner = _sess.owner if _sess else None

        doc = Document(
            id=doc_id,
            session_id=session_id,
            title=title,
            language=language,
            current_content=content,
            version_count=1,
            is_active=True,
            owner=_owner,
        )
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_number=1,
            content=content,
            summary=f"Created by {_active_model or 'AI'}",
            source="ai",
        )
        db.add(doc)
        db.add(ver)
        db.commit()

        set_active_document(doc_id)
        try:
            from src.event_bus import fire_event
            fire_event("document_created", _owner)
        except Exception:
            logger.debug("document_created event dispatch failed", exc_info=True)

        return {
            "action": "create",
            "doc_id": doc_id,
            "title": title,
            "language": language,
            "content": content,
            "version": 1,
        }
    except Exception as e:
        db.rollback()
        return {"error": f"Failed to create document: {e}"}
    finally:
        db.close()


async def do_update_document(content: str, doc_id: Optional[str] = None) -> Dict:
    """Update an existing document. Content = full new document text."""
    import uuid
    from src.database import SessionLocal, Document, DocumentVersion

    target_id = doc_id or _active_document_id

    db = SessionLocal()
    try:
        doc = None
        if target_id:
            doc = db.query(Document).filter(Document.id == target_id).first()
        if not doc:
            doc = db.query(Document).order_by(Document.updated_at.desc()).first()
            if doc:
                target_id = doc.id
                set_active_document(target_id)
                logger.info(f"update_document: fell back to most recent doc id={target_id}")
        if not doc:
            return {"error": "No documents exist to update"}

        is_email_doc = doc.language == "email" or _looks_like_email_document(doc.current_content or "", doc.title or "")
        new_content = _coerce_email_document_content(doc.current_content or "", content) if is_email_doc else content.strip()
        if is_email_doc:
            doc.language = "email"

        new_ver = doc.version_count + 1
        ver = DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=target_id,
            version_number=new_ver,
            content=new_content,
            summary=f"Updated by {_active_model or 'AI'}",
            source="ai",
        )
        doc.current_content = new_content
        doc.version_count = new_ver
        db.add(ver)
        db.commit()

        return {
            "action": "update",
            "doc_id": target_id,
            "title": doc.title,
            "language": doc.language,
            "content": new_content,
            "version": new_ver,
        }
    except Exception as e:
        db.rollback()
        return {"error": f"Failed to update document: {e}"}
    finally:
        db.close()


def parse_edit_blocks(content: str) -> list:
    """Parse <<<FIND>>>...<<<REPLACE>>>...<<<END>>> blocks."""
    edits = []
    pattern = r'<<<FIND>>>\n(.*?)\n<<<REPLACE>>>\n(.*?)\n<<<END>>>'
    for m in re.finditer(pattern, content, re.DOTALL):
        edits.append({"find": m.group(1), "replace": m.group(2)})
    return edits


async def do_edit_document(content: str, doc_id: Optional[str] = None) -> Dict:
    """Apply targeted FIND/REPLACE edits to an existing document."""
    import uuid
    from src.database import SessionLocal, Document, DocumentVersion

    target_id = doc_id or _active_document_id

    edits = parse_edit_blocks(content)
    if not edits:
        return {"error": "No valid <<<FIND>>>...<<<REPLACE>>>...<<<END>>> blocks found"}

    db = SessionLocal()
    try:
        doc = None
        if target_id:
            doc = db.query(Document).filter(Document.id == target_id).first()
        if not doc:
            # Fallback: most recently updated document. Avoids "no active doc" errors
            # after server restart or when the agent loses track of which doc to edit.
            doc = db.query(Document).order_by(Document.updated_at.desc()).first()
            if doc:
                target_id = doc.id
                set_active_document(target_id)
                logger.info(f"edit_document: fell back to most recent doc id={target_id} title={doc.title!r}")
        if not doc:
            return {"error": "No documents exist to edit"}

        updated_content = doc.current_content
        applied = 0
        skipped = 0
        for edit in edits:
            _find = edit["find"]
            if _find in updated_content:
                updated_content = updated_content.replace(_find, edit["replace"], 1)
                applied += 1
            else:
                # Defensive: the active-doc context shows a "N\t" line-number
                # gutter for reference. Weaker models sometimes copy that prefix
                # into FIND. If the exact match failed, retry with a leading
                # "<digits><tab>" stripped from each FIND line — but only use it
                # when that stripped form actually matches, so we never corrupt a
                # legitimately tab-prefixed document.
                _stripped = "\n".join(re.sub(r"^\d+\t", "", _l) for _l in _find.split("\n"))
                if _stripped != _find and _stripped in updated_content:
                    updated_content = updated_content.replace(_stripped, edit["replace"], 1)
                    applied += 1
                    logger.info("edit_document: matched after stripping line-number gutter from FIND")
                else:
                    logger.warning(f"edit_document: FIND text not found, skipping: {_find[:80]!r}")
                    skipped += 1

        if applied == 0:
            return {"error": f"No edits applied — none of the FIND blocks matched the document content (skipped {skipped})"}

        new_ver = doc.version_count + 1
        ver = DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=target_id,
            version_number=new_ver,
            content=updated_content,
            summary=f"Edited by {_active_model or 'AI'} ({applied} edit(s))",
            source="ai",
        )
        doc.current_content = updated_content
        doc.version_count = new_ver
        db.add(ver)
        db.commit()

        return {
            "action": "edit",
            "doc_id": target_id,
            "title": doc.title,
            "language": doc.language,
            "content": updated_content,
            "version": new_ver,
            "applied": applied,
            "skipped": skipped,
        }
    except Exception as e:
        db.rollback()
        return {"error": f"Failed to edit document: {e}"}
    finally:
        db.close()


def parse_suggest_blocks(content: str) -> list:
    """Parse <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks."""
    suggestions = []
    _skip_phrases = ["no change", "clear", "fine as", "looks good", "no improvement", "keep as"]
    pattern = r'<<<FIND>>>\n(.*?)\n<<<SUGGEST>>>\n(.*?)\n<<<REASON>>>\n(.*?)\n<<<END>>>'
    for m in re.finditer(pattern, content, re.DOTALL):
        find_text = m.group(1)
        replace_text = m.group(2)
        reason = m.group(3).strip()
        # Skip no-op suggestions where find == replace or reason says no change
        if find_text.strip() == replace_text.strip():
            continue
        if any(phrase in reason.lower() for phrase in _skip_phrases):
            continue
        suggestions.append({
            "id": f"sugg-{len(suggestions)+1}",
            "find": find_text,
            "replace": replace_text,
            "reason": reason,
        })
    return suggestions


async def do_suggest_document(content: str, doc_id: str = None) -> Dict:
    """Create inline suggestions for the active document WITHOUT modifying it."""
    from src.database import SessionLocal, Document

    target_id = doc_id or _active_document_id
    if not target_id:
        return {"error": "No active document to suggest on"}

    suggestions = parse_suggest_blocks(content)
    if not suggestions:
        return {"error": "No valid <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks found"}

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == target_id).first()
        if not doc:
            return {"error": f"Document {target_id} not found"}

        # Validate that FIND text exists in document
        valid = []
        for s in suggestions:
            if s["find"] in doc.current_content:
                valid.append(s)
            else:
                logger.warning(f"suggest_document: FIND text not found, skipping: {s['find'][:80]!r}")

        if not valid:
            return {"error": "No suggestions matched the document content"}

        return {
            "action": "suggest",
            "doc_id": target_id,
            "suggestions": valid,
            "count": len(valid),
        }
    finally:
        db.close()


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
