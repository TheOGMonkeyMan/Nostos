"""Lower-level helper functions for the email routes (IMAP folder/flag/move
utilities, SMTP-config resolution, email-HTML rendering and sanitizing).

Extracted verbatim from routes/email_routes.py (Phase 2.2 / ADR-036). Behavior
is unchanged; these helpers are re-imported into email_routes.py so the route
handlers below and the external task_scheduler _resolve_send_config import keep
working. This module owns its own logger (name routes.email_route_helpers).
"""

import html
import logging
import re
import sqlite3 as _sql3
from datetime import datetime
from html.parser import HTMLParser as _HTMLParser

from routes.email_helpers import _get_email_config, _q, SCHEDULED_DB

logger = logging.getLogger(__name__)

ODYSSEUS_MAIL_ORIGIN = "odysseus-ui"


def _email_tag_owner_aliases(account_id: str | None, owner: str = "") -> list[str]:
    aliases = [owner or ""]
    try:
        from core.database import SessionLocal as _SL, EmailAccount as _EA
        db = _SL()
        try:
            resolved_account_id = account_id
            if not resolved_account_id:
                try:
                    cfg = _get_email_config(None, owner=owner)
                    resolved_account_id = cfg.get("account_id") or None
                    aliases.extend([
                        cfg.get("imap_user") or "",
                        cfg.get("smtp_user") or "",
                        cfg.get("from_address") or "",
                    ])
                except Exception:
                    resolved_account_id = None
            row = db.get(_EA, resolved_account_id) if resolved_account_id else None
            if row:
                aliases.extend([row.owner or "", row.imap_user or "", row.from_address or ""])
        finally:
            db.close()
    except Exception:
        pass
    out = []
    for a in aliases:
        a = (a or "").strip()
        if a not in out:
            out.append(a)
    return out or [""]


def _record_email_received_events(owner: str, account_id: str | None, folder: str, emails: list[dict]):
    """Baseline inbox messages, then fire `email_received` for new arrivals."""
    if not owner or (folder or "INBOX").upper() != "INBOX" or not emails:
        return
    try:
        from src.event_bus import fire_event
        account_key = (account_id or "default").strip() or "default"
        now = datetime.utcnow().isoformat() + "Z"
        keys = []
        for e in emails:
            key = (e.get("message_id") or e.get("uid") or "").strip()
            if key and key not in keys:
                keys.append(key)
        if not keys:
            return

        conn = _sql3.connect(SCHEDULED_DB)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS email_event_seen ("
                "owner TEXT NOT NULL, account_key TEXT NOT NULL, folder TEXT NOT NULL, "
                "message_key TEXT NOT NULL, first_seen_at TEXT NOT NULL, "
                "PRIMARY KEY (owner, account_key, folder, message_key))"
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM email_event_seen WHERE owner=? AND account_key=? AND folder=?",
                (owner, account_key, folder),
            ).fetchone()[0]
            existing = set()
            if count:
                placeholders = ",".join("?" * len(keys))
                rows = conn.execute(
                    f"SELECT message_key FROM email_event_seen "
                    f"WHERE owner=? AND account_key=? AND folder=? AND message_key IN ({placeholders})",
                    (owner, account_key, folder, *keys),
                ).fetchall()
                existing = {r[0] for r in rows}
            new_keys = [k for k in keys if k not in existing]
            conn.executemany(
                "INSERT OR IGNORE INTO email_event_seen "
                "(owner, account_key, folder, message_key, first_seen_at) VALUES (?, ?, ?, ?, ?)",
                [(owner, account_key, folder, k, now) for k in keys],
            )
            conn.commit()
        finally:
            conn.close()

        if count and new_keys:
            for _ in new_keys[:50]:
                fire_event("email_received", owner)
            logger.info("Fired email_received for %d new message(s)", min(len(new_keys), 50))
    except Exception:
        logger.debug("email_received event detection skipped", exc_info=True)


def _folder_name_from_list_line(line) -> str | None:
    decoded = line.decode() if isinstance(line, bytes) else str(line)
    match = re.search(r'"([^"]*)"\s*$|(\S+)\s*$', decoded)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _list_imap_folders(conn) -> tuple[list, list[str]]:
    try:
        status, folders = conn.list()
        if status != "OK" or not folders:
            return [], []
        names = [name for name in (_folder_name_from_list_line(f) for f in folders) if name]
        return folders, names
    except Exception:
        return [], []


def _resolve_mail_folder(conn, preferred: str, role: str = "") -> str:
    """Resolve provider-specific names such as Gmail's [Gmail]/Bin/Spam."""
    folders, names = _list_imap_folders(conn)
    if preferred and preferred in names:
        return preferred
    role_flags = {
        "trash": ("\\Trash",),
        "archive": ("\\Archive", "\\All"),
        "junk": ("\\Junk",),
    }.get(role, ())
    for f in folders:
        decoded = f.decode() if isinstance(f, bytes) else str(f)
        if any(flag in decoded for flag in role_flags):
            name = _folder_name_from_list_line(f)
            if name:
                return name
    candidates = {
        "trash": ("Trash", "[Gmail]/Trash", "[Google Mail]/Trash", "Bin", "[Gmail]/Bin", "Deleted Messages", "Deleted Items"),
        "archive": ("Archive", "Archives", "[Gmail]/All Mail", "[Google Mail]/All Mail", "All Mail"),
        "junk": ("Junk", "Spam", "[Gmail]/Spam", "[Google Mail]/Spam"),
    }.get(role, ())
    lower_map = {n.lower(): n for n in names}
    for candidate in candidates:
        found = lower_map.get(candidate.lower())
        if found:
            return found
    return preferred


def _folder_role_from_name(name: str) -> str:
    lower = (name or "").lower()
    if "trash" in lower or "bin" in lower or "deleted" in lower:
        return "trash"
    if "spam" in lower or "junk" in lower:
        return "junk"
    if "archive" in lower or "all mail" in lower:
        return "archive"
    return ""


def _uid_bytes(uid: str | bytes) -> bytes:
    return uid if isinstance(uid, bytes) else str(uid).encode()


def _uid_exists(conn, uid: str) -> bool:
    try:
        status, data = conn.uid("FETCH", _uid_bytes(uid), "(UID)")
        if status != "OK":
            return False
        for part in data or []:
            meta = part[0] if isinstance(part, tuple) else part
            meta_b = meta if isinstance(meta, bytes) else str(meta).encode()
            if re.search(rb"\bUID\s+\d+\b", meta_b):
                return True
        return False
    except Exception:
        return False


def _imap_uid_search(conn, criteria: str):
    return conn.uid("SEARCH", None, criteria)


def _imap_uid_fetch(conn, uid_set: str | bytes, query: str):
    return conn.uid("FETCH", _uid_bytes(uid_set), query)


def _uid_from_fetch_meta(meta_b: bytes) -> str:
    m = re.search(rb"\bUID\s+(\d+)\b", meta_b)
    return m.group(1).decode() if m else ""


def _smtp_ready(cfg: dict) -> bool:
    return bool(cfg.get("smtp_host") and cfg.get("smtp_user") and cfg.get("smtp_password"))


def _resolve_send_config(account_id: str | None = None, owner: str = "") -> dict:
    """Resolve an account for outbound SMTP.

    If the caller explicitly picked an account, use only that account and
    return a clear error when it cannot send. If no account was picked and
    the default is receive-only, fall back to the first SMTP-capable account
    owned by the same user.
    """
    cfg = _get_email_config(account_id, owner=owner)
    if _smtp_ready(cfg):
        return cfg
    if account_id:
        raise ValueError(f"Email account {cfg.get('account_name') or account_id} has no SMTP configured")
    try:
        from core.database import SessionLocal as _SL, EmailAccount as _EA
        from sqlalchemy import and_, or_
        db = _SL()
        try:
            q = db.query(_EA).filter(_EA.enabled == True)  # noqa: E712
            if owner:
                unowned = or_(_EA.owner == None, _EA.owner == "")  # noqa: E711
                same_mailbox = or_(_EA.imap_user == owner, _EA.from_address == owner)
                q = q.filter(or_(_EA.owner == owner, and_(unowned, same_mailbox)))
            for row in q.order_by(_EA.is_default.desc(), _EA.created_at.asc()).all():
                trial = _get_email_config(account_id=row.id, owner=owner)
                if _smtp_ready(trial):
                    return trial
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"SMTP-capable account fallback failed: {e}")
    raise ValueError("No SMTP-capable email account configured")


def _store_email_flag(conn, uid: str, flag: str, add: bool = True) -> bool:
    op = "+FLAGS" if add else "-FLAGS"
    if _uid_exists(conn, uid):
        status, _ = conn.uid("STORE", _uid_bytes(uid), op, flag)
    else:
        status, _ = conn.store(_uid_bytes(uid), op, flag)
    return status == "OK"


def _move_email_message(conn, uid: str, dest: str, role: str = "") -> bool:
    dest = _resolve_mail_folder(conn, dest, role or _folder_role_from_name(dest))
    if _uid_exists(conn, uid):
        status, _ = conn.uid("MOVE", _uid_bytes(uid), _q(dest))
        if status == "OK":
            return True
        status, _ = conn.uid("COPY", _uid_bytes(uid), _q(dest))
        if status != "OK":
            return False
        status, _ = conn.uid("STORE", _uid_bytes(uid), "+FLAGS", "\\Deleted")
    else:
        status, _ = conn.copy(_uid_bytes(uid), _q(dest))
        if status != "OK":
            return False
        status, _ = conn.store(_uid_bytes(uid), "+FLAGS", "\\Deleted")
    if status == "OK":
        conn.expunge()
        return True
    return False


def _apply_odysseus_headers(msg, kind: str | None = None, ref_id: str | None = None):
    msg["X-Odysseus-Origin"] = ODYSSEUS_MAIL_ORIGIN
    if kind:
        msg["X-Odysseus-Kind"] = re.sub(r"[^A-Za-z0-9_.-]", "-", kind)[:64]
    if ref_id:
        msg["X-Odysseus-Ref"] = re.sub(r"[^A-Za-z0-9_.:-]", "-", ref_id)[:128]


def _md_to_email_html(text: str) -> str:
    """Render the compose markdown body to a SAFE HTML fragment for the email's
    text/html part. Everything is HTML-escaped FIRST (so a pasted <script> /
    <img onerror=...> can never become live HTML in the recipient's client),
    then the toolbar's formatting is layered on with controlled regex: bold,
    italic, strike, inline code, http(s) links, headings, and bullet/numbered
    lists. Plain-text readers still get the raw markdown via the text/plain part.
    """
    def _inline(s: str) -> str:
        s = html.escape(s)                                  # escape BEFORE formatting
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
        s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # links: text + http(s) url only (escape() already neutralised quotes)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        return s

    parts: list[str] = []
    in_ul = in_ol = False
    for ln in (text or "").split("\n"):
        m_h = re.match(r"^(#{1,3})\s+(.*)$", ln)
        m_ul = re.match(r"^\s*[-*]\s+(.*)$", ln)
        m_ol = re.match(r"^\s*\d+\.\s+(.*)$", ln)
        if m_h:
            if in_ul: parts.append("</ul>"); in_ul = False
            if in_ol: parts.append("</ol>"); in_ol = False
            lvl = len(m_h.group(1))
            parts.append(f"<h{lvl}>{_inline(m_h.group(2))}</h{lvl}>")
        elif m_ul:
            if in_ol: parts.append("</ol>"); in_ol = False
            if not in_ul: parts.append("<ul>"); in_ul = True
            parts.append(f"<li>{_inline(m_ul.group(1))}</li>")
        elif m_ol:
            if in_ul: parts.append("</ul>"); in_ul = False
            if not in_ol: parts.append("<ol>"); in_ol = True
            parts.append(f"<li>{_inline(m_ol.group(1))}</li>")
        else:
            if in_ul: parts.append("</ul>"); in_ul = False
            if in_ol: parts.append("</ol>"); in_ol = False
            parts.append(_inline(ln) + "<br>")
    if in_ul: parts.append("</ul>")
    if in_ol: parts.append("</ol>")
    return "<html><body>" + "\n".join(parts) + "</body></html>"


# Tags the WYSIWYG email composer may legitimately produce.
_EMAIL_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "s", "strike", "del", "a", "br", "p", "div",
    "ul", "ol", "li", "blockquote", "span", "h1", "h2", "h3", "code", "pre",
}


class _EmailHtmlSanitizer(_HTMLParser):
    """Allowlist sanitizer for WYSIWYG-composed email HTML. Emits only known
    formatting tags (all attributes dropped except a safe href on <a>), escapes
    all text, and discards <script>/<style> content entirely — so client-sent
    HTML can never carry live script/handlers into the recipient's client."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip = 0  # depth inside <script>/<style>

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag not in _EMAIL_ALLOWED_TAGS:
            return
        if tag == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v and re.match(r"^(https?:|mailto:)", v.strip(), re.I):
                    href = v.strip()
            self.out.append(
                f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
                if href else "<a>")
        else:
            self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("<br>")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if tag == "br" or tag not in _EMAIL_ALLOWED_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip:
            return
        self.out.append(html.escape(data))


def _sanitize_email_html(raw: str) -> str:
    """Return a safe <html><body>…</body></html> from client-supplied compose
    HTML, or None if it can't be parsed."""
    p = _EmailHtmlSanitizer()
    try:
        p.feed(raw or "")
        p.close()
    except Exception:
        return None
    inner = "".join(p.out).strip()
    if not inner:
        return None
    return f"<html><body>{inner}</body></html>"
