"""IMAP folder + message-parsing helpers for the email MCP server (ADR-052, Phase 2.2).

The folder-resolution helpers (_detect_sent_folder, _folder_name_from_list_line,
_list_folder_lines, _resolve_folder, _folder_role_from_name) and the message
parsing helpers (_decode_header, _extract_text), split verbatim out of
mcp_servers/email_server.py. Self-contained: they operate on a passed-in IMAP conn
+ email.message objects (deps: email.header, html, re). Re-imported into
email_server so its tool handlers keep calling them as module globals.
"""

import email.header
import html
import re


def _detect_sent_folder(conn):
    """Find the account's Sent folder name; fall back to 'Sent'."""
    candidates = ("Sent", "[Gmail]/Sent Mail", "Sent Mail", "Sent Items", "INBOX.Sent")
    try:
        status, folders = conn.list()
        if status != "OK" or not folders:
            return "Sent"
        names = []
        for f in folders:
            decoded = f.decode() if isinstance(f, bytes) else str(f)
            m = re.search(r'"([^"]*)"\s*$|(\S+)\s*$', decoded)
            if m:
                names.append(m.group(1) or m.group(2))
        for f in folders:
            decoded = f.decode() if isinstance(f, bytes) else str(f)
            if r"\Sent" in decoded:
                m = re.search(r'"([^"]*)"\s*$|(\S+)\s*$', decoded)
                if m:
                    return m.group(1) or m.group(2)
        for c in candidates:
            if c in names:
                return c
    except Exception:
        pass
    return "Sent"


def _folder_name_from_list_line(line) -> str | None:
    decoded = line.decode() if isinstance(line, bytes) else str(line)
    m = re.search(r'"([^"]*)"\s*$|(\S+)\s*$', decoded)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _list_folder_lines(conn) -> list:
    try:
        status, folders = conn.list()
        if status != "OK" or not folders:
            return []
        return folders
    except Exception:
        return []


def _resolve_folder(conn, preferred: str, role: str) -> str:
    """Resolve provider-specific folder names like Gmail's [Gmail]/Trash."""
    folders = _list_folder_lines(conn)
    names = [name for name in (_folder_name_from_list_line(f) for f in folders) if name]
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
        "trash": ("Trash", "[Gmail]/Trash", "[Google Mail]/Trash", "Bin", "Deleted Messages", "Deleted Items"),
        "archive": ("Archive", "Archives", "[Gmail]/All Mail", "[Google Mail]/All Mail"),
        "junk": ("Junk", "Spam", "[Gmail]/Spam", "[Google Mail]/Spam"),
    }.get(role, ())
    lower_map = {n.lower(): n for n in names}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return preferred


def _folder_role_from_name(name: str) -> str:
    lower = (name or "").lower()
    if "trash" in lower or "bin" in lower or "deleted" in lower:
        return "trash"
    if "junk" in lower or "spam" in lower:
        return "junk"
    if "archive" in lower or "all mail" in lower:
        return "archive"
    return ""


def _decode_header(raw):
    """Decode MIME encoded header."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _extract_text(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        text_parts = []
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_parts.append(payload.decode(charset, errors="replace"))
            elif ct == "text/html" and not text_parts and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    raw_html = payload.decode(charset, errors="replace")
                    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
                    text = re.sub(r"<[^>]+>", "", text)
                    text = html.unescape(text)
                    text_parts.append(text.strip())
        return "\n".join(text_parts)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""
