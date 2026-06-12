"""Email compose/send routes (ADR-041, Phase 2.2).

The /compose-upload (+ delete), /schedule, /scheduled (+ delete),
/resolve-contact, /send and /draft endpoints, split verbatim out of
routes/email_routes.py::setup_email_routes(). The group carries its own async
worker _send_email_sync and references none of the bound pool/cache locals or the
list/read sync workers, so the registrar takes only the router.
"""

import asyncio
import email as email_mod
import email.utils
import html
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import BackgroundTasks, Depends, File, HTTPException, Query, UploadFile

from routes.email_helpers import (
    SendEmailRequest,
    require_owner,
    _q,
    _imap,
    _decode_header,
    _get_email_config,
    _assert_owns_account,
    _attach_compose_uploads,
    _cleanup_compose_uploads,
    _send_smtp_message,
    _detect_sent_folder,
    _detect_drafts_folder,
    COMPOSE_UPLOADS_DIR,
    SCHEDULED_DB,
)
from routes.email_route_helpers import (
    _resolve_send_config,
    _apply_odysseus_headers,
    _md_to_email_html,
    _sanitize_email_html,
)

logger = logging.getLogger(__name__)


def register_compose_routes(router):
    @router.post("/compose-upload")
    async def compose_upload(file: UploadFile = File(...), owner: str = Depends(require_owner)):
        """Upload a file for attaching to a compose email. Returns a token."""
        # 25MB cap (matches typical SMTP limits w/ base64 overhead)
        MAX_BYTES = 25 * 1024 * 1024
        try:
            # Sanitize filename and generate a unique token
            safe_name = re.sub(r"[^\w\s\-.]", "_", file.filename or "file").strip()
            token = f"{uuid.uuid4().hex}_{safe_name}"
            filepath = COMPOSE_UPLOADS_DIR / token
            content = await file.read()
            if len(content) > MAX_BYTES:
                raise HTTPException(413, f"Attachment exceeds {MAX_BYTES // (1024*1024)}MB limit")
            with open(filepath, "wb") as f:
                f.write(content)
            return {
                "success": True,
                "token": token,
                "filename": safe_name,
                "size": len(content),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to upload attachment: {e}")
            return {"success": False, "error": "Mail operation failed"}

    @router.delete("/compose-upload/{token}")
    async def delete_compose_upload(token: str, owner: str = Depends(require_owner)):
        """Delete a staged compose upload."""
        try:
            # Prevent path traversal
            safe_token = Path(token).name
            filepath = COMPOSE_UPLOADS_DIR / safe_token
            if filepath.exists():
                filepath.unlink()
            return {"success": True}
        except Exception as e:
            logger.error(f"delete_compose_upload {token!r} failed: {e}")
            return {"success": False, "error": "Mail operation failed"}

    async def _send_email_sync(
        to, cc, bcc, subject, body, in_reply_to, references, attachments,
        account_id=None, owner="", odysseus_kind=None, odysseus_ref=None,
    ):
        """Shared send logic used by both /send and scheduled delivery.

        SECURITY: callers MUST pass `owner` (the authed user) so the config
        lookup is scoped — otherwise the fallback picks whichever account
        happens to be is_default globally and the message ships through
        someone else's SMTP creds + From-address.
        """
        cfg = _resolve_send_config(account_id, owner=owner)
        has_atts = bool(attachments)
        if has_atts:
            outer = MIMEMultipart("mixed")
            body_container = MIMEMultipart("alternative")
        else:
            outer = MIMEMultipart("alternative")
            body_container = outer

        outer["From"] = cfg["from_address"]
        outer["To"] = to
        if cc:
            outer["Cc"] = cc
        outer["Subject"] = subject or ""
        outer["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        _apply_odysseus_headers(outer, odysseus_kind or "scheduled", odysseus_ref)
        if in_reply_to:
            outer["In-Reply-To"] = in_reply_to
        if references:
            outer["References"] = references

        body_container.attach(MIMEText(body or "", "plain", "utf-8"))
        body_container.attach(MIMEText(_md_to_email_html(body or ""), "html", "utf-8"))

        if has_atts:
            outer.attach(body_container)
            _attach_compose_uploads(outer, attachments)

        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if cc:
            recipients.extend([r.strip() for r in cc.split(",") if r.strip()])
        if bcc:
            recipients.extend([r.strip() for r in bcc.split(",") if r.strip()])

        _send_smtp_message(cfg, cfg["from_address"], recipients, outer.as_string())

        _cleanup_compose_uploads(attachments)

    @router.post("/schedule")
    async def schedule_email(req: dict, owner: str = Depends(require_owner)):
        """Schedule an email to be sent at a specific time. ISO8601 UTC."""
        import sqlite3
        import uuid as _uuid
        try:
            send_at = req.get("send_at")
            if not send_at:
                return {"success": False, "error": "send_at required (ISO8601 UTC)"}
            # Body-based account_id — dep can't see it, check here.
            _acct = req.get("account_id")
            if _acct:
                _assert_owns_account(_acct, owner)
            # Validate parseable + reject past times (the poller fires
            # anything in the past immediately on the next tick — a
            # 1970-dated schedule would deliver right now).
            from datetime import datetime as _dt, timezone as _tz
            try:
                parsed_at = _dt.fromisoformat(send_at.replace("Z", "+00:00"))
            except ValueError:
                return {"success": False, "error": "send_at must be ISO8601"}
            now_utc = _dt.now(_tz.utc) if parsed_at.tzinfo else _dt.utcnow()
            # Tiny 30s grace so a user clicking Send right at the chosen
            # minute doesn't trip the past-time guard.
            if parsed_at < now_utc:
                return {"success": False, "error": "send_at must be in the future"}

            sid = _uuid.uuid4().hex[:16]
            conn = sqlite3.connect(SCHEDULED_DB)
            conn.execute("""
                INSERT INTO scheduled_emails
                (id, to_addr, cc, bcc, subject, body, in_reply_to, references_hdr, attachments, send_at, created_at, status, account_id, odysseus_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                sid,
                req.get("to", ""),
                req.get("cc") or None,
                req.get("bcc") or None,
                req.get("subject") or "",
                req.get("body") or "",
                req.get("in_reply_to") or None,
                req.get("references") or None,
                json.dumps(req.get("attachments") or []),
                send_at,
                datetime.utcnow().isoformat(),
                req.get("account_id") or None,
                req.get("odysseus_kind") or "scheduled",
            ))
            conn.commit()
            conn.close()
            logger.info(f"Scheduled email {sid} for {send_at}")
            return {"success": True, "id": sid, "send_at": send_at}
        except Exception as e:
            logger.error(f"Failed to schedule email: {e}")
            return {"success": False, "error": "Mail operation failed"}

    @router.get("/scheduled")
    async def list_scheduled(owner: str = Depends(require_owner)):
        """List all scheduled (pending) emails."""
        import sqlite3
        try:
            conn = sqlite3.connect(SCHEDULED_DB)
            rows = conn.execute("""
                SELECT id, to_addr, cc, subject, send_at, created_at, status, error
                FROM scheduled_emails
                WHERE status IN ('pending', 'failed')
                ORDER BY send_at ASC
            """).fetchall()
            conn.close()
            return {"scheduled": [
                {
                    "id": r[0], "to": r[1], "cc": r[2], "subject": r[3],
                    "send_at": r[4], "created_at": r[5], "status": r[6], "error": r[7],
                } for r in rows
            ]}
        except Exception as e:
            logger.error(f"list_scheduled failed: {e}")
            return {"scheduled": [], "error": "Mail operation failed"}

    @router.delete("/scheduled/{sid}")
    async def cancel_scheduled(sid: str, owner: str = Depends(require_owner)):
        """Cancel a scheduled email."""
        import sqlite3
        try:
            conn = sqlite3.connect(SCHEDULED_DB)
            conn.execute("DELETE FROM scheduled_emails WHERE id = ? AND status = 'pending'", (sid,))
            conn.commit()
            conn.close()
            return {"success": True}
        except Exception as e:
            logger.error(f"cancel_scheduled {sid!r} failed: {e}")
            return {"success": False, "error": "Mail operation failed"}

    @router.get("/resolve-contact")
    async def resolve_contact(name: str = Query(..., description="Name to search for"), owner: str = Depends(require_owner)):
        """Search Sent folder for a contact by name. Returns matching email addresses."""
        try:
            with _imap() as conn:
                matches = {}
                for folder in ["Sent", "INBOX", "Drafts"]:
                    try:
                        st, _ = conn.select(_q(folder), readonly=True)
                        if st != "OK":
                            continue
                        st, data = conn.search(None, "ALL")
                        if st != "OK" or not data[0]:
                            continue
                        uids = data[0].split()[-200:]
                        for uid in reversed(uids):
                            try:
                                st2, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO CC)])")
                                if st2 != "OK":
                                    continue
                                raw = msg_data[0][1] if msg_data[0] and len(msg_data[0]) > 1 else b""
                                hdr = email_mod.message_from_bytes(raw)
                                for field in ["From", "To", "Cc"]:
                                    val = _decode_header(hdr.get(field, ""))
                                    if not val:
                                        continue
                                    for part in val.split(","):
                                        part = part.strip()
                                        if name.lower() in part.lower():
                                            addr_match = re.search(r'<([^>]+)>', part)
                                            addr = addr_match.group(1) if addr_match else part
                                            addr = addr.strip().lower()
                                            if addr and "@" in addr:
                                                display = part.split("<")[0].strip().strip('"') or addr
                                                if addr not in matches:
                                                    matches[addr] = display
                            except Exception:
                                continue
                    except Exception:
                        continue
                    if len(matches) >= 10:
                        break
                results = [{"email": addr, "name": display} for addr, display in matches.items()]
                return {"contacts": results[:10], "query": name}
        except Exception as e:
            logger.error(f"resolve_contact {name!r} failed: {e}")
            return {"contacts": [], "error": "Mail operation failed"}

    @router.post("/send")
    async def send_email(req: SendEmailRequest, background_tasks: BackgroundTasks, owner: str = Depends(require_owner)):
        """Queue an email for SMTP delivery. Returns immediately; send runs in background.

        Uses req.account_id to pick the sending account (falls back to default)."""
        # Body-based account_id — dep can't see it, check here.
        if req.account_id:
            _assert_owns_account(req.account_id, owner)

        try:
            cfg = _resolve_send_config(req.account_id, owner=owner)
        except Exception as e:
            return {"success": False, "error": str(e) or "No SMTP-capable email account configured"}

        # Use 'mixed' if we have attachments, 'alternative' otherwise
        has_attachments = bool(req.attachments)
        logger.info(f"Sending email to {req.to}: subject={req.subject!r}, attachments={req.attachments}")
        if has_attachments:
            outer = MIMEMultipart("mixed")
            body_container = MIMEMultipart("alternative")
        else:
            outer = MIMEMultipart("alternative")
            body_container = outer

        outer["From"] = cfg["from_address"]
        outer["To"] = req.to
        if req.cc:
            outer["Cc"] = req.cc
        outer["Subject"] = req.subject
        outer["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        outer["Message-ID"] = email.utils.make_msgid(domain="odysseus.local")

        if req.in_reply_to:
            outer["In-Reply-To"] = req.in_reply_to
        if req.references:
            outer["References"] = req.references
        if req.odysseus_kind:
            _apply_odysseus_headers(outer, req.odysseus_kind)

        # Plain + HTML body. Escape user content so a `<script>` or
        # `<img onerror=...>` paste in compose doesn't end up as live HTML
        # in the recipient's MUA.
        body_container.attach(MIMEText(req.body, "plain", "utf-8"))
        # HTML part: prefer the WYSIWYG composer's HTML (sanitized via allowlist);
        # otherwise render the markdown body. Both routes escape untrusted text,
        # so neither can introduce live script/handlers.
        _html_part = (_sanitize_email_html(req.body_html) if req.body_html else None) \
            or _md_to_email_html(req.body)
        body_container.attach(MIMEText(_html_part, "html", "utf-8"))

        if has_attachments:
            outer.attach(body_container)
            _attach_compose_uploads(outer, req.attachments)

        # Build recipient list
        recipients = [r.strip() for r in req.to.split(",") if r.strip()]
        if req.cc:
            recipients.extend([r.strip() for r in req.cc.split(",") if r.strip()])
        if req.bcc:
            recipients.extend([r.strip() for r in req.bcc.split(",") if r.strip()])

        # Serialize what the background task needs so the request object can be GC'd
        outer_bytes = outer.as_bytes()
        outer_str = outer.as_string()
        _from = cfg["from_address"]
        _smtp_host = cfg["smtp_host"]
        _smtp_port = cfg["smtp_port"]
        _smtp_user = cfg["smtp_user"]
        _smtp_pw = cfg["smtp_password"]
        _recipients = list(recipients)
        _to_label = req.to
        _subject = req.subject
        _atts = list(req.attachments or [])
        _message_id = outer["Message-ID"]

        _account_id = cfg.get("account_id") or req.account_id  # capture for the IMAP append in the closure
        _in_reply_to = (req.in_reply_to or "").strip()

        def _deliver():
            try:
                _send_smtp_message(
                    {
                        "smtp_host": _smtp_host,
                        "smtp_port": _smtp_port,
                        "smtp_user": _smtp_user,
                        "smtp_password": _smtp_pw,
                    },
                    _from,
                    _recipients,
                    outer_str,
                )
                logger.info(f"Email sent to {_to_label}: {_subject}")
                delivery_result = {
                    "success": True,
                    "account_id": cfg.get("account_id") or _account_id,
                    "sent_folder": None,
                    "sent_uid": None,
                    "message_id": _message_id,
                }
                try:
                    with _imap(_account_id, owner=owner) as imap:
                        sent_folder = _detect_sent_folder(imap)
                        sent_uid = None
                        append_st, append_data = imap.append(sent_folder, "\\Seen", None, outer_bytes)
                        if append_st == "OK" and append_data:
                            m = re.search(rb"APPENDUID\s+\d+\s+(\d+)", append_data[0] or b"")
                            if m:
                                sent_uid = m.group(1).decode("ascii", errors="ignore")
                        if not sent_uid:
                            try:
                                st_sel, _ = imap.select(_q(sent_folder), readonly=True)
                                if st_sel == "OK":
                                    mid = (_message_id or "").strip().lstrip("<").rstrip(">").replace('"', '\\"')
                                    st_uid, uid_data = imap.uid("SEARCH", None, f'HEADER Message-ID "{mid}"')
                                    if st_uid == "OK" and uid_data and uid_data[0]:
                                        sent_uid = uid_data[0].split()[-1].decode("ascii", errors="ignore")
                            except Exception:
                                pass
                        # Auto-mark the source email as Answered/done so it
                        # disappears from "undone" filters.
                        if _in_reply_to:
                            try:
                                # Strip any angle brackets and quote for IMAP
                                mid = _in_reply_to.strip().lstrip("<").rstrip(">").replace('"', '\\"')
                                # Search common folders for the source message.
                                folder_candidates = (
                                    "INBOX",
                                    sent_folder,
                                    "Sent",
                                    "[Gmail]/Sent Mail",
                                    "Archive",
                                    "All Mail",
                                    "[Gmail]/All Mail",
                                )
                                for folder_name in dict.fromkeys(folder_candidates):
                                    try:
                                        st, _sel = imap.select(_q(folder_name), readonly=False)
                                        if st != "OK":
                                            continue
                                        st2, sd = imap.search(None, f'HEADER Message-ID "{mid}"')
                                        if st2 == "OK" and sd and sd[0]:
                                            for u in sd[0].split():
                                                imap.store(u, "+FLAGS", "\\Answered")
                                            logger.info(f"Marked source {mid[:60]!r} as \\Answered in {folder_name}")
                                            break
                                    except Exception:
                                        continue
                            except Exception as e:
                                logger.warning(f"Failed to auto-mark source as answered: {e}")
                        delivery_result = {
                            "success": True,
                            "account_id": cfg.get("account_id") or _account_id,
                            "sent_folder": sent_folder,
                            "sent_uid": sent_uid,
                            "message_id": _message_id,
                        }
                except Exception as e:
                    logger.warning(f"Failed to append to Sent: {e}")
                _cleanup_compose_uploads(_atts)
                return delivery_result
            except Exception as e:
                logger.error(f"Failed to send email to {_to_label}: {e}")
                return {"success": False, "error": str(e) or "Failed to send email"}

        if req.wait_for_delivery:
            result = await asyncio.to_thread(_deliver)
            if result.get("success"):
                return {"success": True, "queued": False, "message": f"Email sent to {req.to}", **result}
            return result

        background_tasks.add_task(_deliver)
        return {
            "success": True,
            "queued": True,
            "account_id": cfg.get("account_id") or req.account_id,
            "message": f"Email queued for {req.to}",
        }

    @router.post("/draft")
    async def save_draft(req: SendEmailRequest, owner: str = Depends(require_owner)):
        """Save email as draft in IMAP Drafts folder.

        IMAP append is sync; offload via asyncio.to_thread so the event loop
        stays responsive on slow remote IMAP servers.
        """
        if req.account_id:
            _assert_owns_account(req.account_id, owner)
        cfg = _get_email_config(req.account_id, owner=owner)

        # Multipart plain+HTML when the WYSIWYG composer supplied HTML, so a
        # reopened draft keeps its formatting; plain MIMEText otherwise.
        _draft_html = _sanitize_email_html(req.body_html) if req.body_html else None
        if _draft_html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(req.body, "plain", "utf-8"))
            msg.attach(MIMEText(_draft_html, "html", "utf-8"))
        else:
            msg = MIMEText(req.body, "plain", "utf-8")
        msg["From"] = cfg["from_address"]
        msg["To"] = req.to
        if req.cc:
            msg["Cc"] = req.cc
        if req.bcc:
            msg["Bcc"] = req.bcc
        msg["Subject"] = req.subject
        msg["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

        if req.in_reply_to:
            msg["In-Reply-To"] = req.in_reply_to
        if req.references:
            msg["References"] = req.references

        _draft_acct = req.account_id

        def _do_append():
            try:
                with _imap(_draft_acct, owner=owner) as imap:
                    drafts_folder = _detect_drafts_folder(imap)
                    imap.append(drafts_folder, "\\Draft", None, msg.as_bytes())
                return None
            except Exception as e:
                return str(e)

        err = await asyncio.to_thread(_do_append)
        if err:
            logger.error(f"Failed to save draft: {err}")
            return {"success": False, "error": err}
        logger.info(f"Draft saved: {req.subject}")
        return {"success": True, "message": "Draft saved"}
