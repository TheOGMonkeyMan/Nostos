"""Email urgency-triage scheduler action (ADR-044, Phase 2.2).

action_check_email_urgency plus its triage model + helpers (_EmailTriage,
_normalize_triage, _email_triage_verdict, _TRIAGE_TAG_REMAP, _TRIAGE_RUBRIC),
split verbatim out of src/builtin_actions.py. Re-imported there so the
BUILTIN_ACTIONS registry and the external test imports keep working.
"""

import logging
import os
from datetime import datetime
from typing import List, Tuple

from pydantic import BaseModel, Field

from src.actions.base import TaskNoop

logger = logging.getLogger(__name__)


class _EmailTriage(BaseModel):
    """Schema the quarantine extracts from an untrusted email body (Phase 1.4b)."""

    score: int = Field(
        0,
        description="0 trivial/promotional, 1 informational no reply, 2 reply within a day, 3 urgent reply now (deadline/blocker)",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="zero or more of: newsletter, marketing, notification, finance, bills, receipt, travel, security, shopping, social, work, personal, calendar",
    )
    spam: bool = Field(
        False,
        description="true for scams, phishing, junk, cold sales, generic ads, or no-personal-action bulk mail",
    )
    reason: str = Field("", description="one short phrase")


_TRIAGE_TAG_REMAP = {"promo": "marketing"}

_TRIAGE_RUBRIC = (
    "Triage ONE unread email into the fields above.\n"
    "score: 0 = trivial / promotional; 1 = informational, no reply needed; "
    "2 = should reply within a day; 3 = urgent, reply now (deadline, blocker).\n"
    "Use 'marketing' for ads, promos, sales, offers, cold sales. Use 'newsletter' "
    "for newsletters, digests, recurring content. spam=true for scams, phishing, "
    "junk, cold sales, generic ads, or no-personal-action bulk mail.\n"
    "If the body says 'I'm outside' / 'waiting outside' / 'at the door' / "
    "'locked out' / 'can't get in', score 3 unless clearly historical."
)


def _normalize_triage(score, raw_tags, spam, reason, category_tags) -> dict:
    """Coerce loose verdict output into {score,tags,spam,reason}, filtering tags to
    the allowed category set. Shared by the legacy and quarantined paths."""
    try:
        score_i = int(score)
    except (TypeError, ValueError):
        score_i = 0
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags: List[str] = []
    for t in raw_tags or []:
        if not isinstance(t, str):
            continue
        tag = t.strip().lower().replace("_", "-")
        tag = _TRIAGE_TAG_REMAP.get(tag, tag)
        if tag in category_tags and tag not in tags:
            tags.append(tag)
    if isinstance(spam, bool):
        spam_b = spam
    elif isinstance(spam, (int, float)):
        spam_b = bool(spam)
    else:
        spam_b = str(spam or "").strip().lower() in {"1", "true", "yes", "y"}
    return {"score": score_i, "tags": tags, "spam": spam_b, "reason": str(reason or "")}


async def _email_triage_verdict(candidates, urgency_prompt, item, category_tags, *, quarantined):
    """Return {'score','tags','spam','reason'} for one email, or None if the model
    gave nothing usable.

    When `quarantined`, the untrusted body is reduced via `src.quarantine` (a
    tool-less model call, data/instruction separation, schema-validated), so an
    injected instruction in the body cannot rewrite the triage rules. On a
    quarantine failure we abort this email (return None) - never fall back to
    feeding the raw body inline."""
    from src.llm_core import llm_call_async_with_fallback

    sender = item.get("from", "") or ""
    subject = item.get("subject", "") or ""
    body = item.get("body", "") or ""

    if quarantined:
        from src.quarantine import QuarantineError
        from src.quarantine import process as _q_process

        async def _model_call(messages):
            return await llm_call_async_with_fallback(
                candidates, messages, temperature=0.1, max_tokens=220, timeout=30
            )

        instructions = _TRIAGE_RUBRIC + (
            f"\n\nUser's rules:\n{urgency_prompt}" if urgency_prompt else ""
        )
        # From/Subject travel WITH the body inside the untrusted data block - the
        # model still sees them, but as DATA, not instructions.
        source = f"From: {sender}\nSubject: {subject}\n\n{body}"
        try:
            v = await _q_process(
                source,
                _EmailTriage,
                label=f"email-triage:{sender[:80]}",
                instructions=instructions,
                model_call=_model_call,
            )
        except QuarantineError as exc:
            logger.debug(f"urgency: quarantine aborted triage: {exc}")
            return None
        return _normalize_triage(v.score, v.tags, v.spam, v.reason, category_tags)

    # ── Legacy inline path (default; behaviour unchanged) ──
    prompt = (
        "You are triaging ONE unread email. Return ONLY JSON: "
        "{\"score\":0|1|2|3,\"tags\":[\"...\"],\"spam\":false,"
        "\"reason\":\"one short phrase\"}.\n"
        "0 = trivial / promotional · 1 = informational, no reply needed · "
        "2 = should reply within a day · 3 = urgent, reply now (deadline, blocker).\n\n"
        "Allowed tags: newsletter, marketing, notification, finance, bills, receipt, "
        "travel, security, shopping, social, work, personal, calendar.\n"
        "Use marketing for ads, promos, sales, offers, and cold sales. Use newsletter "
        "for newsletters, digests, and recurring content. spam=true for scams, phishing, "
        "junk, cold sales, generic ads, or no-personal-action bulk mail.\n"
        "Important: 'I'm outside', 'I am outside', 'waiting outside', 'at the door', "
        "'locked out', or 'can't get in' means score 3 unless clearly historical.\n\n"
        f"User's rules:\n{urgency_prompt}\n\n"
        f"Email:\nFrom: {sender}\nSubject: {subject}\n"
        f"Snippet:\n{body}\n"
    )
    try:
        raw = await llm_call_async_with_fallback(
            candidates,
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=220,
            timeout=30,
        )
    except Exception as exc:
        logger.debug(f"urgency: legacy triage call failed: {exc}")
        return None
    import json as _json

    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        nl = txt.find("\n")
        if nl >= 0:
            txt = txt[nl + 1:]
    s = txt.find("{")
    e = txt.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = _json.loads(txt[s:e + 1])
    except (ValueError, TypeError):
        return None
    return _normalize_triage(
        obj.get("score", 0), obj.get("tags"), obj.get("spam"), obj.get("reason", ""), category_tags
    )


async def action_check_email_urgency(owner: str, **kwargs) -> Tuple[str, bool]:
    """Scan unread emails across all accounts, LLM-triage new ones, cache
    per-UID verdicts, tag the inbox, and fire a reminder when a previously
    unseen UID scores reply-soon/urgent (>=2). State persists under
    data/email_urgency_state_* so the UI can color the unread dot by tier.

    Design notes:
    - Only classifies emails newer than 7 days (first-run scale guard).
    - Cache key = `<account_id>:<uid>` so the same UID across accounts doesn't collide.
    - Re-notify gate: only when at least one UID NEW to `notified_uids` scores ≥2.
      Repeat scans where the set is unchanged stay silent.
    """
    from src.settings import load_settings

    try:
        settings = load_settings()
        import json as _json
        import email as _email_mod
        import asyncio as _aio
        import os as _os
        import re as _re
        import time as _time
        import httpx
        from datetime import datetime as _dt, timedelta as _td
        from pathlib import Path as _P
        from core.database import SessionLocal as _SL, EmailAccount as _EA
        from routes.email_helpers import _imap_connect, _decode_header
        from src.endpoint_resolver import resolve_endpoint, resolve_utility_fallback_candidates
        from src.llm_core import llm_call_async_with_fallback

        # Per-owner state file so multi-user runs don't clobber each other's
        # notified_uids / urgency counts. Empty owner falls back to a generic
        # filename for single-user installs (matches prior behaviour).
        _owner_slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
        STATE_PATH = _P(f"data/email_urgency_state_{_owner_slug}.json")
        CACHE_DIR = _P("data/email_urgency_cache")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGE_CUTOFF = _dt.utcnow() - _td(days=7)
        TRIAGE_VERSION = 3
        CATEGORY_TAGS = {
            "newsletter", "marketing", "notification", "finance", "bills",
            "receipt", "travel", "security", "shopping", "social", "work",
            "personal", "calendar",
        }
        MANAGED_TAGS = CATEGORY_TAGS | {"urgent", "reply-soon", "promo"}

        # ── 1. Resolve LLM candidates (utility primary + utility fallbacks; fall
        # through to default chat as a last resort).
        url, model, headers = resolve_endpoint("utility")
        if not url or not model:
            url, model, headers = resolve_endpoint("default")
        if not url or not model:
            return "No LLM endpoint available", False
        candidates = [(url, model, headers)] + resolve_utility_fallback_candidates()

        # ── 2. Enumerate enabled accounts. Match this task's owner AND fall
        # back to the legacy "unowned account whose imap_user / from_address
        # == this owner" pattern — same rule `_get_email_config` uses, so a
        # pre-multi-user account row still gets picked up for the seeded task.
        db = _SL()
        try:
            from sqlalchemy import and_ as _and, or_ as _or
            q = db.query(_EA).filter(_EA.enabled == True)  # noqa: E712
            if owner:
                unowned = _or(_EA.owner == None, _EA.owner == "")  # noqa: E711
                same_mailbox = _or(_EA.imap_user == owner, _EA.from_address == owner)
                q = q.filter(_or(_EA.owner == owner, _and(unowned, same_mailbox)))
            accounts = q.all()
        finally:
            db.close()
        if not accounts:
            raise TaskNoop("no email accounts configured")

        urgency_prompt = settings.get("urgent_email_prompt", "")
        _quarantine_on = bool(settings.get("quarantine_enabled", False))
        per_uid_scores = {}   # key = "<acc_id>:<uid>" → {"score": 0-3, "reason": "..."}
        all_unread_keys = set()  # for cache pruning
        llm_attempts = 0
        saved_classifications = 0
        failed_classifications = []
        scanned = 0

        # ── 3. Per-account scan: pull headers + lightweight body for new UIDs
        # since 7 days ago, score via LLM, cache the verdict.
        for acc in accounts:
            cache_file = CACHE_DIR / f"{acc.id}.json"
            try:
                cache = _json.loads(cache_file.read_text()) if cache_file.exists() else {"uids": {}}
            except Exception:
                cache = {"uids": {}}

            def _scan_one(account=acc, cache_uids=cache.get("uids", {})):
                """Sync IMAP work runs in a thread."""
                results = []
                conn = _imap_connect(account.id)
                try:
                    conn.select("INBOX", readonly=True)
                    # IMAP date is the only practical pre-filter — UNSEEN AND
                    # SINCE 7-days-ago. Date format is DD-Mon-YYYY.
                    since_str = AGE_CUTOFF.strftime("%d-%b-%Y")
                    status, data = conn.search(None, f'(UNSEEN SINCE {since_str})')
                    if status != "OK" or not data or not data[0]:
                        return results
                    uids = data[0].split()
                    for uid_b in uids:
                        uid = uid_b.decode() if isinstance(uid_b, bytes) else str(uid_b)
                        key = f"{account.id}:{uid}"
                        cached = cache_uids.get(uid)
                        cached_ok = isinstance(cached, dict) and cached.get("triage_version") == TRIAGE_VERSION
                        results.append({"key": key, "uid": uid, "cached": cached if cached_ok else None})
                        if cached_ok:
                            # Already classified — skip the fetch.
                            continue
                        # Pull headers + first ~800 chars of plaintext body.
                        try:
                            st, msg_data = conn.fetch(uid_b, "(RFC822.HEADER BODY.PEEK[TEXT]<0.800>)")
                            if st != "OK" or not msg_data:
                                continue
                            # Headers + body land in different tuples in the
                            # response — concatenate the bytes for parsing.
                            raw = b""
                            for part in msg_data:
                                if isinstance(part, tuple) and part[1]:
                                    raw += part[1] + b"\n\n"
                            if not raw:
                                continue
                            msg = _email_mod.message_from_bytes(raw)
                            # Skip Odysseus-generated reminders so the scanner
                            # doesn't classify its own emails as urgent and
                            # trigger a feedback loop. Match on either the
                            # stamped headers OR the subject prefix.
                            _ody_origin = (msg.get("X-Odysseus-Origin") or "").strip().lower()
                            _ody_kind = (msg.get("X-Odysseus-Kind") or "").strip().lower()
                            _raw_subj = (msg.get("Subject") or "").lower()
                            # MCP path drops custom headers (email_server's
                            # schema doesn't accept them), so we ALSO match the
                            # `[Task]` subject prefix that `_deliver_via_mcp`
                            # always stamps. Anything that looks self-generated
                            # is dropped before classification to prevent the
                            # scanner from labelling its own emails "urgent".
                            if (_ody_origin == "odysseus-ui" or _ody_kind == "reminder"
                                    or _raw_subj.startswith("reminder (odysseus):")
                                    or _raw_subj.startswith("reminder:")
                                    or _raw_subj.startswith("[task]")):
                                # Drop this candidate entirely — don't list it
                                # in results so its UID never enters the cache
                                # nor counts toward `scanned`.
                                results.pop()
                                continue
                            subject = _decode_header(msg.get("Subject") or "")
                            from_raw = _decode_header(msg.get("From") or "")
                            header_blob = "\n".join(
                                f"{name}: {msg.get(name, '')}"
                                for name in (
                                    "From", "Subject", "List-Unsubscribe", "List-ID",
                                    "Precedence", "X-Mailchimp-Campaign-Id",
                                    "X-Campaign", "X-MC-User",
                                )
                                if msg.get(name)
                            )
                            body_snippet = ""
                            try:
                                if msg.is_multipart():
                                    for part in msg.walk():
                                        if part.get_content_type() == "text/plain":
                                            body_snippet = part.get_payload(decode=True).decode("utf-8", errors="ignore")[:1600]
                                            break
                                else:
                                    body_snippet = (msg.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")[:1600]
                            except Exception:
                                body_snippet = ""
                            results[-1].update({
                                "subject": subject,
                                "from": from_raw,
                                "headers": header_blob,
                                "body": body_snippet.strip(),
                                "message_id": (msg.get("Message-ID") or "").strip(),
                            })
                        except Exception as _fe:
                            logger.debug(f"urgency: header fetch for uid {uid} failed: {_fe}")
                finally:
                    try: conn.logout()
                    except Exception: pass
                return results

            try:
                items = await _aio.to_thread(_scan_one)
            except Exception as e:
                logger.warning(f"urgency: IMAP scan failed for account {acc.id}: {e}")
                continue

            for item in items:
                scanned += 1
                key = item["key"]
                all_unread_keys.add(key)
                if item.get("cached"):
                    per_uid_scores[key] = item["cached"]
                    continue
                # Skip uids we couldn't fetch (no subject/from/body).
                if not item.get("subject") and not item.get("from"):
                    continue
                # ── LLM-classify. Optionally via the structural injection
                # quarantine (data/instruction separation + schema validation +
                # tool-less model) when `quarantine_enabled` is set; otherwise the
                # legacy inline-prompt parse. Both yield score/tags/spam/reason.
                llm_attempts += 1
                _v = await _email_triage_verdict(
                    candidates, urgency_prompt, item, CATEGORY_TAGS,
                    quarantined=_quarantine_on,
                )
                if _v is None:
                    failed_classifications.append({
                        "subject": item.get("subject") or "(no subject)",
                        "from": item.get("from") or "",
                        "reason": "model returned no usable verdict",
                    })
                    continue
                try:
                    score = int(_v["score"])
                    reason = str(_v["reason"])[:200]
                    tags = list(_v["tags"])
                    spam = bool(_v["spam"])
                    _blob = f"{item.get('headers','')}\n{item.get('subject','')}\n{item.get('body','')}".lower()
                    if _re.search(r"\b(i'?m|i am|im|we'?re|we are)\s+outside\b", _blob) or _re.search(
                        r"\b(waiting outside|at the door|locked out|can'?t get in|cannot get in)\b", _blob
                    ):
                        if score < 3:
                            reason = "person is waiting outside"
                        score = max(score, 3)
                    bulkish = bool(_re.search(
                        r"\b(list-unsubscribe|list-id|mailchimp|mailchimpapp|view this email in your browser|unsubscribe|newsletter|digest|precedence:\s*bulk)\b",
                        _blob,
                    ))
                    marketingish = bool(_re.search(
                        r"\b(advertisement|sponsored|promo|promotion|sale|discount|offer|limited time|deal|tickets?|tour|merch|stream|purchase|sold out|low tickets|coupon|shop now|buy now)\b",
                        _blob,
                    ))
                    if "newsletter" not in tags and bulkish:
                        tags.append("newsletter")
                    if "marketing" not in tags and marketingish:
                        tags.append("marketing")
                    if (bulkish or marketingish) and score < 2:
                        score = 0
                        if not reason or "urgent" in reason.lower():
                            reason = "Bulk marketing/newsletter; no personal reply needed"
                    # Strip "Name <addr>" to bare display name for compact summary.
                    _from_raw = item.get("from", "") or ""
                    if "<" in _from_raw:
                        _from_short = _from_raw.split("<", 1)[0].strip().strip('"') or _from_raw
                    else:
                        _from_short = _from_raw
                    verdict = {
                        "score": max(0, min(3, score)),
                        "tags": tags[:4],
                        "spam": spam,
                        "reason": reason,
                        "subject": (item.get("subject") or "")[:200],
                        "from": _from_short[:120],
                        "triage_version": TRIAGE_VERSION,
                        # Cache the message_id too so re-scans of already-cached
                        # UIDs can still write the inbox tag without re-LLM'ing.
                        "message_id": (item.get("message_id") or "").strip(),
                        "ts": _time.time(),
                    }
                    cache.setdefault("uids", {})[item["uid"]] = verdict
                    per_uid_scores[key] = verdict
                    saved_classifications += 1
                except Exception as e:
                    failed_classifications.append({
                        "subject": item.get("subject") or "(no subject)",
                        "from": item.get("from") or "",
                        "reason": str(e)[:120] or "classification failed",
                    })
                    logger.debug(f"urgency: LLM classify failed for {key}: {e}")
                    continue

            # ── Prune cache entries for UIDs that are no longer unread (replied
            # / archived / deleted). Compare against `items` (everything UNSEEN
            # in this scan window).
            seen_uids = {it["uid"] for it in items}
            cache_uids = cache.get("uids", {})
            for stale in [u for u in cache_uids if u not in seen_uids]:
                cache_uids.pop(stale, None)

            try:
                cache_file.write_text(_json.dumps(cache))
            except Exception as e:
                logger.warning(f"urgency: cache write failed for {acc.id}: {e}")

        # ── 3.5  Mirror triage verdicts into email_tags so inbox filters and
        # pills show urgency + category tags. Runs for BOTH cached and freshly
        # classified items; message_id lives on the cached verdict so this is cheap.
        try:
            import sqlite3 as _sql3
            from routes.email_helpers import SCHEDULED_DB, _init_scheduled_db
            from datetime import datetime as _dt2
            _init_scheduled_db()
            _conn = _sql3.connect(SCHEDULED_DB)
            try:
                for _key, _v in per_uid_scores.items():
                    _msg_id = (_v.get("message_id") or "").strip()
                    _score = _v.get("score", 0)
                    if not _msg_id:
                        continue
                    _new_tags = []
                    if _score >= 3:
                        _new_tags.append("urgent")
                    elif _score >= 2:
                        _new_tags.append("reply-soon")
                    for _tag in (_v.get("tags") or []):
                        _tag = str(_tag).strip().lower().replace("_", "-")
                        if _tag == "promo":
                            _tag = "marketing"
                        if _tag in CATEGORY_TAGS and _tag not in _new_tags:
                            _new_tags.append(_tag)
                    _spam = 1 if _v.get("spam") else 0
                    # _key is "<account_id>:<uid>" — extract uid for the row.
                    _uid_only = _key.split(":", 1)[-1]
                    _owner_key = owner or ""
                    _row = _conn.execute(
                        "SELECT tags FROM email_tags WHERE message_id=? AND owner=?",
                        (_msg_id, _owner_key),
                    ).fetchone()
                    if _row:
                        try:
                            _existing = _json.loads(_row[0] or "[]")
                            if not isinstance(_existing, list):
                                _existing = []
                        except Exception:
                            _existing = []
                        # Drop previous triage-owned tags so re-classification
                        # can upgrade/downgrade/clear without touching manual tags.
                        _existing = [
                            str(t).strip().lower().replace("_", "-")
                            for t in _existing
                            if str(t).strip().lower().replace("_", "-") not in MANAGED_TAGS
                        ]
                        for _tag in _new_tags:
                            if _tag not in _existing:
                                _existing.append(_tag)
                        _conn.execute(
                            "UPDATE email_tags SET tags=?, spam_verdict=?, spam_reason=?, uid=?, folder=?, subject=?, sender=? "
                            "WHERE message_id=? AND owner=?",
                            (_json.dumps(_existing), _spam, _v.get("reason", ""), _uid_only, "INBOX",
                             _v.get("subject", ""), _v.get("from", ""), _msg_id, _owner_key),
                        )
                    else:
                        if not _new_tags and not _spam:
                            continue
                        _conn.execute(
                            "INSERT INTO email_tags "
                            "(message_id, owner, uid, folder, subject, sender, tags, spam_verdict, spam_reason, created_at) "
                            "VALUES (?, ?, ?, 'INBOX', ?, ?, ?, ?, ?, ?)",
                            (_msg_id, _owner_key, _uid_only, _v.get("subject", ""),
                             _v.get("from", ""), _json.dumps(_new_tags), _spam, _v.get("reason", ""),
                             _dt2.utcnow().isoformat()),
                        )
                _conn.commit()
            finally:
                _conn.close()
        except Exception as _te:
            logger.warning(f"urgency: bulk tag write failed: {_te}")

        # ── 4. Aggregate state. urgent = score ≥ 2.
        urgent_keys = [k for k, v in per_uid_scores.items() if v.get("score", 0) >= 2]
        max_score = max((v.get("score", 0) for v in per_uid_scores.values()), default=0)
        total_urgent = len(urgent_keys)

        # Load prior state to know which urgent UIDs we've already notified.
        try:
            prior = _json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        except Exception:
            prior = {}
        notified_uids = set(prior.get("notified_uids", []))

        # ── 5. Fire reminder ONLY when a previously-unnotified UID scores urgent.
        new_urgent = [k for k in urgent_keys if k not in notified_uids]
        newly_notified = set()
        notify_failed = set()
        if new_urgent:
            title = "Urgent email" if total_urgent == 1 else f"{total_urgent} urgent emails"
            # Build a real listing — subject · sender · reason for each urgent
            # one — so the reminder email tells you which messages to act on,
            # not just "4 needing reply". Optional deep-link when the user has
            # `app_public_url` configured in Settings (so the email row links
            # straight into the Odysseus Email tab).
            # Sort: highest-scored UIDs first; cap at 10 to keep the email tidy.
            sorted_urgent = sorted(
                ((k, per_uid_scores[k]) for k in urgent_keys),
                key=lambda kv: kv[1].get("score", 0), reverse=True,
            )[:10]
            _pub = (settings.get("app_public_url") or "").strip().rstrip("/")
            from urllib.parse import quote as _quote
            lines = [f"{total_urgent} email" + ("" if total_urgent == 1 else "s") + " need an urgent reply:", ""]
            for i, (k, v) in enumerate(sorted_urgent, 1):
                subj = (v.get("subject") or "(no subject)")[:160]
                frm = v.get("from") or ""
                why = v.get("reason") or ""
                uid_for_link = str(k).split(":", 1)[-1]
                hash_link = f"#email={_quote('INBOX', safe='')}:{uid_for_link}"
                open_link = f"{_pub}/{hash_link}" if _pub else hash_link
                line = f"{i}. {subj}"
                if frm:
                    line += f"  —  {frm}"
                if why:
                    line += f"  ·  {why}"
                lines.append(line)
                lines.append(f"   Open email: {open_link}")
            if total_urgent > len(sorted_urgent):
                lines.append("")
                lines.append(f"…and {total_urgent - len(sorted_urgent)} more.")
            body = "\n".join(lines)
            try:
                # Call dispatch_reminder DIRECTLY (no HTTP/auth roundtrip — the
                # endpoint version 401's the background scheduler because it
                # has no session cookie).
                from routes.note_routes import dispatch_reminder
                dispatch_result = await dispatch_reminder(
                    title=title, note_body=body, note_id="urgent-email",
                    owner=owner or "",
                )
                channel = (settings.get("reminder_channel") or "browser").strip().lower()
                delivered = bool(dispatch_result.get("browser_sent"))
                if channel == "email":
                    delivered = bool(dispatch_result.get("email_sent"))
                elif channel == "ntfy":
                    delivered = bool(dispatch_result.get("ntfy_sent"))
                if delivered:
                    newly_notified.update(new_urgent)
                else:
                    notify_failed.update(new_urgent)
                    logger.warning(f"urgency: reminder dispatch returned no successful delivery path: {dispatch_result}")
            except Exception as e:
                logger.warning(f"urgency: reminder dispatch failed: {e}")
                notify_failed.update(new_urgent)
            # Mark only successfully delivered UIDs as notified so a transient
            # SMTP/ntfy/browser failure retries instead of lying forever.
            notified_uids.update(newly_notified)

        # Prune notified_uids that aren't unread anymore (so a future re-urgent
        # message with the same UID — rare but possible after archive→unarchive
        # — can re-notify). Keep only UIDs still in `all_unread_keys`.
        notified_uids = {u for u in notified_uids if u in all_unread_keys}

        state = {
            "ts": _time.time(),
            "owner": owner or "",
            "total_unread": len(all_unread_keys),
            "total_urgent": total_urgent,
            "max_score": max_score,
            "per_uid": per_uid_scores,
            "notified_uids": sorted(notified_uids),
        }
        try:
            STATE_PATH.write_text(_json.dumps(state))
        except Exception as e:
            logger.warning(f"urgency: state write failed: {e}")

        # ── 6. Activity-log summary — counts line on top, then per-tier
        # bulleted breakdown so the user can see WHICH emails ranked where
        # (subject · sender · reason) and which ones triggered notifications.
        tier_counts = {0: 0, 1: 0, 2: 0, 3: 0}
        for v in per_uid_scores.values():
            tier_counts[v.get("score", 0)] = tier_counts.get(v.get("score", 0), 0) + 1
        if scanned == 0:
            raise TaskNoop("no unread emails in last 7 days")
        head = (
            f"scanned {scanned} · urgent {tier_counts[3]} · "
            f"reply-soon {tier_counts[2]} · info {tier_counts[1]} · trivial {tier_counts[0]} · "
            f"{saved_classifications} saved classifications"
        )
        if llm_attempts != saved_classifications:
            head += f" · {llm_attempts - saved_classifications} failed"
        if newly_notified:
            head += f" · notified {len(newly_notified)}"
        if notify_failed:
            head += f" · notify failed {len(notify_failed)}"

        def _fmt_one(v, newly_notified_set, failed_set, key):
            subj = (v.get("subject") or "(no subject)")[:80]
            frm = v.get("from") or ""
            why = v.get("reason") or ""
            tag = " · *notified now*" if key in newly_notified_set else (" · *notify failed*" if key in failed_set else "")
            line = f"- **{subj}**" + (f" — _{frm}_" if frm else "")
            if why:
                line += f" — {why}"
            return line + tag

        # Sort each tier by reason length (longest reason first → most info).
        by_tier = {3: [], 2: [], 1: [], 0: []}
        for k, v in per_uid_scores.items():
            by_tier.setdefault(v.get("score", 0), []).append((k, v))
        lines = [head]
        tier_labels = {3: "Urgent", 2: "Reply soon", 1: "Informational", 0: "Trivial"}
        for tier in (3, 2, 1, 0):
            items_t = by_tier.get(tier, [])
            if not items_t:
                continue
            lines.append("")
            lines.append(f"**{tier_labels[tier]} ({len(items_t)}):**")
            # Cap each tier at 8 rows to keep the activity entry readable.
            for k, v in items_t[:8]:
                lines.append(_fmt_one(v, newly_notified, notify_failed, k))
            if len(items_t) > 8:
                lines.append(f"…and {len(items_t) - 8} more")
        if failed_classifications:
            lines.append("")
            lines.append(f"**Unclassified ({len(failed_classifications)}):**")
            for v in failed_classifications[:8]:
                subj = (v.get("subject") or "(no subject)")[:80]
                frm = v.get("from") or ""
                why = v.get("reason") or ""
                line = f"- **{subj}**" + (f" — _{frm}_" if frm else "")
                if why:
                    line += f" — {why}"
                lines.append(line)
            if len(failed_classifications) > 8:
                lines.append(f"…and {len(failed_classifications) - 8} more")
        return "\n".join(lines), True
    except TaskNoop:
        raise
    except Exception as e:
        logger.exception("check_email_urgency action failed")
        return str(e), False
