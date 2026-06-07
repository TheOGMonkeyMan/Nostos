"""CardDAV contact tool handlers.

Extracted verbatim from src/tool_implementations.py (Phase 2.2 / ADR-030).
Behavior is unchanged; these handlers are re-exported from
src.tool_implementations so existing import paths keep working. The shared
_parse_tool_args helper is imported lazily inside each handler to avoid a
circular import with the tool_implementations module.
"""

from typing import Dict, Optional


async def do_resolve_contact(content: str, owner: Optional[str] = None) -> Dict:
    """Look up a contact by name. Searches: CardDAV -> email history -> memory."""
    import httpx
    from src.tool_implementations import _parse_tool_args
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    name = args.get("name", "")
    if not name:
        return {"error": "name is required", "exit_code": 1}

    contacts = {}  # email -> {name, source}

    # 1. CardDAV (Radicale) — structured contacts. Call in-process: a
    # server-side httpx GET to /api/contacts/search carries no session
    # cookie and would 401 under require_user.
    try:
        import asyncio
        from routes import contacts_routes as cc
        all_contacts = await asyncio.to_thread(cc._fetch_contacts)
        q = name.lower()
        for c in (all_contacts or []):
            hay_name = (c.get("name") or "").lower()
            match = q in hay_name or any(q in (e or "").lower() for e in c.get("emails", []))
            if not match:
                continue
            for email in (c.get("emails") or []):
                email = (email or "").strip().lower()
                if email and "@" in email:
                    contacts[email] = {"name": c.get("name") or email, "source": "contacts"}
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=30) as client:
        # 2. Email history (sent/received)
        try:
            resp = await client.get("http://localhost:7000/api/email/resolve-contact", params={"name": name})
            if resp.status_code == 200:
                for c in (resp.json().get("contacts") or []):
                    email = (c.get("email") or "").strip().lower()
                    if email and email not in contacts:
                        contacts[email] = {"name": c.get("name") or email, "source": "email history"}
        except Exception:
            pass

    if not contacts:
        return {"output": f"No contacts found matching '{name}'.", "exit_code": 0}

    lines = [f"Contacts matching '{name}':"]
    for email, info in contacts.items():
        lines.append(f"- {info['name']} <{email}> ({info['source']})")
    return {"output": "\n".join(lines), "exit_code": 0}


async def do_manage_contact(content: str, owner: Optional[str] = None) -> Dict:
    """Add / update / delete / list CardDAV contacts. Calls the contacts
    helpers IN-PROCESS rather than over HTTP — a server-side httpx call to
    /api/contacts/* carries no session cookie and would be rejected by
    require_user (401), so the tool would see zero contacts even though
    the browser-side UI works fine."""
    from src.tool_implementations import _parse_tool_args
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    action = (args.get("action") or "").strip().lower()
    try:
        from routes import contacts_routes as cc
    except Exception as e:
        return {"error": f"Contacts module unavailable: {e}", "exit_code": 1}
    # The contacts helpers are sync (httpx blocking calls to CardDAV) — run
    # them in a thread so we don't block the event loop.
    import asyncio
    try:
        if action == "list":
            rows = await asyncio.to_thread(cc._fetch_contacts, True)
            if not rows:
                return {"output": "No contacts.", "exit_code": 0}
            lines = [f"{len(rows)} contacts:"]
            for c in rows:
                em = ", ".join(c.get("emails") or [])
                lines.append(f"- {c.get('name') or '(no name)'} <{em}>  [uid={c.get('uid','')}]")
            return {"output": "\n".join(lines), "exit_code": 0}

        if action == "add":
            email = (args.get("email") or "").strip()
            if not email:
                return {"error": "email is required for add", "exit_code": 1}
            name = (args.get("name") or "").strip() or email.split("@")[0]
            # Dedupe by email (same as the /add route).
            existing = await asyncio.to_thread(cc._fetch_contacts)
            for c in existing:
                if email.lower() in [e.lower() for e in c.get("emails", [])]:
                    return {"output": f"{email} is already a contact ({c.get('name','')}).", "exit_code": 0}
            ok = await asyncio.to_thread(cc._create_contact, name, email)
            return {"output": f"{'Added' if ok else 'Failed to add'} {name} <{email}>.", "exit_code": 0 if ok else 1}

        if action in ("update", "edit"):
            uid = (args.get("uid") or "").strip()
            if not uid:
                return {"error": "uid is required for update (use action=list to find it)", "exit_code": 1}
            name = (args.get("name") or "").strip()
            emails = args.get("emails")
            if emails is None and args.get("email"):
                emails = [args["email"]]
            emails = [e.strip() for e in (emails or []) if e and e.strip()]
            phones = [p.strip() for p in (args.get("phones") or []) if p and p.strip()]
            if not name and not emails:
                return {"error": "Provide a name or emails to update", "exit_code": 1}
            if not name and emails:
                name = emails[0].split("@")[0]
            ok = await asyncio.to_thread(cc._update_contact, uid, name, emails, phones)
            return {"output": "Contact updated." if ok else "Update failed.", "exit_code": 0 if ok else 1}

        if action == "delete":
            uid = (args.get("uid") or "").strip()
            if not uid:
                return {"error": "uid is required for delete (use action=list to find it)", "exit_code": 1}
            ok = await asyncio.to_thread(cc._delete_contact, uid)
            return {"output": "Contact deleted." if ok else "Delete failed.", "exit_code": 0 if ok else 1}

        return {"error": f"Unknown action '{action}'. Use list, add, update, or delete.", "exit_code": 1}
    except Exception as e:
        return {"error": f"Contact operation failed: {e}", "exit_code": 1}
