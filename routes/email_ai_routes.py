"""Email AI routes (ADR-040, Phase 2.2).

The /extract-style, /summarize and /ai-reply endpoints, split verbatim out of
routes/email_routes.py::setup_email_routes(). They use the module-level _imap()
context manager + email_helpers + src.llm_core.llm_call_async (no bound pool/cache
locals and no sync workers), so the registrar takes only the router.
"""

import asyncio
import email as email_mod
import email.utils
import json
import logging
import re
import sqlite3 as _sql3
from datetime import datetime

from fastapi import Depends

from routes.email_helpers import (
    ExtractStyleRequest,
    require_owner,
    _imap,
    _q,
    _strip_think,
    _extract_reply,
    _apply_email_style_mechanics,
    _assert_owns_account,
    _detect_sent_folder,
    _fetch_sender_thread_context,
    _pre_retrieve_context,
    _extract_attachment_text,
    _load_settings,
    _save_settings,
    _EMAIL_REPLY_SYS_PROMPT_BASE,
    SCHEDULED_DB,
)
from routes.email_route_helpers import _imap_uid_fetch
from src.llm_core import llm_call_async

logger = logging.getLogger(__name__)


def register_ai_routes(router):
    @router.post("/extract-style")
    async def extract_writing_style(req: ExtractStyleRequest, owner: str = Depends(require_owner)):
        """Extract writing style from sent emails using LLM.

        IMAP fetch is offloaded to a worker thread; the LLM call uses the
        async client. Otherwise this handler froze the event loop for ~5s
        on the IMAP step alone with a remote server.
        """

        def _gather_samples() -> tuple[list[str], str | None]:
            try:
                with _imap(owner=owner) as imap:
                    imap.select(_q(_detect_sent_folder(imap)), readonly=True)
                    status, data = imap.search(None, "ALL")
                    if status != "OK" or not data[0]:
                        return [], "No sent emails found"
                    uid_list = data[0].split()[-req.sample_count:]

                    out = []
                    for uid in uid_list:
                        try:
                            status, msg_data = imap.fetch(uid, "(RFC822)")
                            if status != "OK":
                                continue
                            raw = msg_data[0][1]
                            msg = email_mod.message_from_bytes(raw)
                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() == "text/plain":
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            charset = part.get_content_charset() or "utf-8"
                                            body = payload.decode(charset, errors="replace")
                                            break
                            else:
                                payload = msg.get_payload(decode=True)
                                if payload:
                                    charset = msg.get_content_charset() or "utf-8"
                                    body = payload.decode(charset, errors="replace")
                            if body.strip() and len(body) > 20:
                                out.append(body[:1000])
                        except Exception:
                            continue
                    return out, None
            except Exception as e:
                return [], str(e)

        try:
            samples, err = await asyncio.to_thread(_gather_samples)
            if err and not samples:
                return {"success": False, "error": err}

            if len(samples) < 3:
                return {"success": False, "error": f"Only found {len(samples)} usable sent emails, need at least 3"}

            # Call LLM to analyze writing style. Prefer the utility model;
            # fall back to the default chat model when utility isn't set
            # (matches how the background email tasks behave).
            from src.endpoint_resolver import resolve_endpoint

            url, model, headers = resolve_endpoint("utility", owner=owner)
            if not url or not model:
                url, model, headers = resolve_endpoint("default", owner=owner)
            if not url or not model:
                return {"success": False, "error": "No LLM endpoint configured — set a Utility or Default Chat model in Settings → AI Defaults."}

            sample_text = "\n\n---EMAIL---\n\n".join(samples[:15])
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are analyzing a user's email writing style. Based on the sample emails below, "
                        "describe their writing style in 3-5 concise sentences. Cover: tone (formal/informal), "
                        "typical greeting and sign-off patterns, sentence structure (short/long), "
                        "any distinctive phrases or habits, and overall communication approach. "
                        "Write this as instructions for an AI to mimic this style. "
                        "Start with 'Write emails in this style:'"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Here are {len(samples)} recently sent emails:\n\n{sample_text}",
                },
            ]

            style = await llm_call_async(url, model, messages, headers=headers, max_tokens=2048)
            style = _strip_think(style or "")
            if not style:
                return {"success": False, "error": "LLM failed to generate style description"}

            # Save to settings
            settings = _load_settings()
            settings["email_writing_style"] = style
            _save_settings(settings)

            logger.info("Writing style extracted and saved")
            return {"success": True, "style": style}

        except Exception as e:
            logger.error(f"Failed to extract writing style: {e}")
            return {"success": False, "error": "Mail operation failed"}

    @router.post("/summarize")
    async def summarize_email(data: dict, owner: str = Depends(require_owner)):
        """Generate a quick AI summary of an email body."""
        try:
            from src.endpoint_resolver import resolve_endpoint
            from src.llm_core import _uses_max_completion_tokens
            import requests as _req

            body = data.get("body", "")
            subject = data.get("subject", "")
            sender = data.get("from", "")
            uid = data.get("uid", "")
            folder = data.get("folder", "INBOX") or "INBOX"
            account_id = data.get("account_id")
            if account_id:
                _assert_owns_account(account_id, owner)
            if not body:
                return {"success": False, "error": "No body provided"}

            # If we know which UID this is, fetch the raw message and pull
            # attachment text so the summary can reference invoice totals,
            # contract clauses, etc. — not just the body.
            att_text = ""
            if uid:
                try:
                    def _fetch_atts():
                        with _imap(account_id, owner=owner) as conn:
                            conn.select(_q(folder), readonly=True)
                            status, msg_data = _imap_uid_fetch(conn, str(uid), "(BODY.PEEK[])")
                            if status != "OK" or not msg_data or not msg_data[0]:
                                return ""
                            raw = msg_data[0][1]
                            msg_obj = email_mod.message_from_bytes(raw)
                            return _extract_attachment_text(msg_obj, max_chars=6000)
                    att_text = await asyncio.to_thread(_fetch_atts)
                except Exception as _ae:
                    logger.debug(f"on-demand summarize attachment fetch failed for uid={uid}: {_ae}")

            body_for_llm = body
            if att_text:
                body_for_llm = body + "\n\n--- ATTACHMENTS ---\n\n" + att_text

            url, model, headers = resolve_endpoint("utility", owner=owner)
            if not url:
                url, model, headers = resolve_endpoint("default", owner=owner)
            if not url or not model:
                return {"success": False, "error": "No LLM endpoint configured"}

            req_headers = {"Content-Type": "application/json"}
            if headers:
                req_headers.update(headers)
            tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are an email summarizer. Format: 1-3 short bullet points (use '- '). Cover: main point, action items, deadlines. If the email has attachments (marked '--- ATTACHMENTS ---'), USE THEIR CONTENTS — pull invoice totals, deadlines, key clauses, concrete numbers/dates from PDFs/docs into the bullets. Be terse.\n\nOUTPUT FORMAT: Put ONLY the bullet points between these exact markers, each on its own line:\n<<<SUMMARY>>>\n- ...\n<<<END>>>\nAny reasoning must come BEFORE <<<SUMMARY>>> (ideally inside <think>...</think>). Only the text between the markers is kept."},
                    {"role": "user", "content": f"From: {sender}\nSubject: {subject}\n\n{body_for_llm[:12000]}\n\n---\n\nSummarize the email. Output the bullets between <<<SUMMARY>>> and <<<END>>>."},
                ],
                tok_key: 8192,
                "temperature": 0.3,
                "stream": False,
            }
            resp = await asyncio.to_thread(
                _req.post, url, json=payload, headers=req_headers, timeout=180
            )
            if not resp.ok:
                return {"success": False, "error": f"LLM HTTP {resp.status_code}"}
            rdata = resp.json()
            msg = (rdata.get("choices") or [{}])[0].get("message", {})
            content = (msg.get("content") or "").strip()
            content = _extract_reply(content)

            if not content:
                # Model put everything in reasoning_content — extract bullet points
                rc = (msg.get("reasoning_content") or "").strip()
                # Find bullet-point style output (lines starting with -, •, *, or numbered)
                bullet_lines = []
                for line in rc.split("\n"):
                    stripped = line.strip()
                    if re.match(r"^[-•*]\s+|^\d+[.)]\s+", stripped):
                        bullet_lines.append(stripped)
                if bullet_lines:
                    content = "\n".join(bullet_lines)
                else:
                    # Last resort: take the last paragraph
                    paragraphs = [p.strip() for p in rc.split("\n\n") if p.strip()]
                    content = paragraphs[-1] if paragraphs else rc[:500]

            if not content:
                return {"success": False, "error": "Empty response from model"}

            # Cache the summary if we have a message_id
            mid = data.get("message_id", "")
            if mid:
                try:
                    import sqlite3 as _sql3
                    _c = _sql3.connect(SCHEDULED_DB)
                    _c.execute("""
                        INSERT OR REPLACE INTO email_summaries
                        (message_id, uid, folder, subject, sender, summary, model_used, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        mid, data.get("uid", ""), data.get("folder", ""),
                        subject, sender, content, model, datetime.utcnow().isoformat(),
                    ))
                    _c.commit()
                    _c.close()
                except Exception as e:
                    logger.warning(f"Failed to cache summary: {e}")

            return {"success": True, "summary": content, "model_used": model}
        except Exception as e:
            logger.error(f"Failed to summarize: {e}")
            return {"success": False, "error": "Mail operation failed"}

    @router.post("/ai-reply")
    async def ai_reply(data: dict, owner: str = Depends(require_owner)):
        """Generate an AI-drafted reply to an email using the user's writing style."""
        try:
            from src.endpoint_resolver import resolve_endpoint

            to = data.get("to", "")
            subject = data.get("subject", "")
            original_body = data.get("original_body", "")
            requested_model = data.get("model", "").strip()
            session_id = data.get("session_id", "").strip()
            message_id = (data.get("message_id") or "").strip()
            source_uid = (data.get("uid") or "").strip()
            source_folder = (data.get("folder") or "INBOX").strip()
            fast_reply = bool(data.get("fast", False))

            if not original_body:
                return {"success": False, "error": "No email body provided"}

            if message_id:
                try:
                    _c = _sql3.connect(SCHEDULED_DB)
                    _row = _c.execute(
                        "SELECT reply, model_used FROM email_ai_replies WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                    _c.close()
                    if _row and _row[0]:
                        cached_reply = _apply_email_style_mechanics(_extract_reply(_row[0] or ""))
                        if cached_reply:
                            return {
                                "success": True,
                                "reply": cached_reply,
                                "model_used": _row[1] or "cached",
                                "cached": True,
                            }
                except Exception as e:
                    logger.warning(f"AI reply cache lookup failed: {e}")

            settings = _load_settings()
            style = settings.get("email_writing_style", "")

            # Try session's endpoint first if session_id provided
            url = None
            model = requested_model
            headers = None
            if session_id:
                try:
                    # The chat-session ORM model is `Session`, not `ChatSession`
                    # — the old import threw ImportError, was swallowed by the
                    # except, and left url=None so EVERY reply silently fell back
                    # to the "default" endpoint (wrong model). Its auth lives in
                    # `headers` (JSON), and `endpoint_url` is already the full
                    # chat-completions URL the chat path uses verbatim — so use
                    # those directly rather than rebuilding via a nonexistent
                    # `api_key` field.
                    from core.database import SessionLocal as _SL, Session as _CS
                    _db = _SL()
                    sess = _db.query(_CS).filter(_CS.id == session_id).first()
                    if sess and sess.endpoint_url:
                        url = sess.endpoint_url
                        # Some sessions stored headers double-encoded (a JSON
                        # string inside the JSON column), so the ORM hands back
                        # a str, not a dict — and llm_call_async's h.update()
                        # then throws "dictionary update sequence element…".
                        # Unwrap until we have a dict (or give up → no headers).
                        _h = sess.headers
                        for _ in range(3):
                            if isinstance(_h, str):
                                try:
                                    _h = json.loads(_h)
                                except Exception:
                                    _h = None
                                    break
                            else:
                                break
                        headers = _h if isinstance(_h, dict) and _h else None
                        if not requested_model:
                            model = sess.model
                    _db.close()
                except Exception as e:
                    logger.warning(f"Failed to read session endpoint: {e}")

            if not url:
                # Match the rest of email AI: prefer the caller's Utility
                # model, then fall back to their Default chat model. Using the
                # global default here could hit a stale provider/key even when
                # chat and summaries worked for the current user.
                url, fallback_model, headers = resolve_endpoint("utility", owner=owner)
                if not url:
                    url, fallback_model, headers = resolve_endpoint("default", owner=owner)
                if not model:
                    model = fallback_model

            if not url or not model:
                return {"success": False, "error": "No LLM endpoint configured"}

            # Resolve the model against what the endpoint actually serves. A
            # stored session model can drift from the server's
            # --served-model-name, giving a 404 "model does not exist". Match
            # by exact id, then basename; fall back to the first served model.
            try:
                from src.llm_core import list_model_ids
                _avail = list_model_ids(url, headers=headers)
                if _avail and model not in _avail:
                    import os as _os
                    _base = _os.path.basename((model or "").rstrip("/"))
                    _match = next((a for a in _avail if _os.path.basename(a.rstrip("/")) == _base), None)
                    model = _match or _avail[0]
            except Exception as _e:
                logger.warning(f"AI reply model resolve failed: {_e}")

            logger.info(f"AI reply using model={model} url={url}")

            # Manual AI Reply should feel immediate. The heavier context mining
            # can involve multiple IMAP folder searches and attachment parsing;
            # reserve that for callers that explicitly opt out of fast mode.
            context_snippets, _terms = ([], [])
            if not fast_reply:
                context_snippets, _terms = _pre_retrieve_context(original_body, to)

            # NEW: also pull the last few emails from the original sender +
            # their attachments. The "to" field on this endpoint is the
            # recipient of the *outgoing* reply — that is, the original
            # sender we're answering. So `to` doubles as the address we want
            # the thread context for.
            referenced = ""
            if not fast_reply:
                try:
                    from_addr_for_ctx = email.utils.parseaddr(to or "")[1]
                    referenced = _fetch_sender_thread_context(
                        sender_addr=from_addr_for_ctx,
                        exclude_uid=source_uid,
                        exclude_folder=source_folder,
                        limit=3,
                    )
                except Exception as _e:
                    logger.warning(f"sender-thread-context failed: {_e}")

            system_prompt = _EMAIL_REPLY_SYS_PROMPT_BASE
            if style:
                system_prompt += f"\n\nWRITING STYLE TO MATCH:\n{style}"
            if context_snippets:
                system_prompt += "\n\nRELEVANT CONTEXT FROM PAST EMAILS AND CONTACTS:\n" + "\n\n---\n\n".join(context_snippets[:5])
            if referenced:
                system_prompt += (
                    "\n\nREFERENCED MATERIAL — the last few emails from this sender, "
                    "plus any text extracted from their attachments. Use this to "
                    "answer numbered questions or refer to documents they previously "
                    "sent. Do NOT cite this material verbatim unless the sender "
                    "directly asked about something in it.\n\n" + referenced[:18000]
                )

            user_msg = (
                f"Recipient: {to}\nSubject: {subject}\n\n"
                f"Original email and any current draft:\n{original_body[:6000]}\n\n"
                f"Draft a reply. Return only the reply body text."
            )

            # Build a candidate chain so a stale session-stored API key
            # (the most common cause of "authentication failed" here)
            # doesn't kill AI Reply outright — fall through to the
            # user's Utility / Default endpoints AND their configured
            # fallback chains. Dedupe by url+model so we don't retry
            # the same broken endpoint.
            from src.llm_core import llm_call_async_with_fallback
            from src.endpoint_resolver import (
                resolve_utility_fallback_candidates,
                resolve_chat_fallback_candidates,
            )
            _seen = set()
            _candidates = []
            def _add(_url, _model, _headers):
                key = (_url or "", _model or "")
                if not _url or not _model or key in _seen:
                    return
                _seen.add(key)
                _candidates.append((_url, _model, _headers))
            # Session endpoint first (may be the broken one).
            _add(url, model, headers)
            # Primary utility endpoint — this is what the user has actually
            # configured as their background-task model, with fresh creds.
            try:
                _u_url, _u_model, _u_headers = resolve_endpoint("utility", owner=owner)
                _add(_u_url, _u_model, _u_headers)
            except Exception:
                pass
            # Primary default chat endpoint — last working chat config.
            try:
                _d_url, _d_model, _d_headers = resolve_endpoint("default", owner=owner)
                _add(_d_url, _d_model, _d_headers)
            except Exception:
                pass
            # Configured fallback chains last.
            for cand in resolve_utility_fallback_candidates(owner=owner) or []:
                _add(*cand)
            for cand in resolve_chat_fallback_candidates() or []:
                _add(*cand)
            try:
                reply = await llm_call_async_with_fallback(
                    _candidates,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.7,
                    max_tokens=1024 if fast_reply else 6144,
                    timeout=60 if fast_reply else 180,
                )
            except Exception as e:
                detail = getattr(e, "detail", None) or str(e)
                _attempted = ", ".join(f"{m}@{u.split('/')[2] if '/' in u else u}" for u, m, _ in _candidates) or "no candidates"
                return {"success": False, "error": f"All endpoints failed ({_attempted}): {detail}. Check your API keys in Settings → Services."}

            reply = _apply_email_style_mechanics(_extract_reply(reply or ""))
            if not reply:
                return {"success": False, "error": "LLM returned empty response"}

            # Cache so next click is instant
            if message_id:
                try:
                    _c = _sql3.connect(SCHEDULED_DB)
                    _c.execute("""
                        INSERT OR REPLACE INTO email_ai_replies
                        (message_id, uid, folder, reply, model_used, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (message_id, source_uid, source_folder, reply, model, datetime.utcnow().isoformat()))
                    _c.commit()
                    _c.close()
                except Exception as e:
                    logger.warning(f"Failed to cache ai_reply: {e}")

            return {"success": True, "reply": reply, "model_used": model}
        except Exception as e:
            logger.error(f"Failed to generate AI reply: {e}")
            return {"success": False, "error": "Mail operation failed"}
