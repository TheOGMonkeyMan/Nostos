"""
builtin_actions.py

Registry of built-in automation actions that can be executed by the task
scheduler without needing an LLM call.
"""

import logging
import os
from datetime import datetime
from typing import List, Tuple

from pydantic import BaseModel, Field

from src.auth_helpers import owner_filter
from core.platform_compat import IS_WINDOWS, find_bash

logger = logging.getLogger(__name__)


# --- action exceptions moved to src/actions/base.py (ADR-043); re-imported
# so `from src.builtin_actions import TaskNoop` / `TaskDeferred` keep working.
from src.actions.base import TaskNoop, TaskDeferred  # noqa: E402,F401


# --- maintenance/tidy actions moved to src/actions/maintenance.py (ADR-043);
# re-imported so the BUILTIN_ACTIONS registry + callers keep working.
from src.actions.maintenance import (  # noqa: E402,F401
    action_tidy_sessions,
    action_tidy_documents,
    action_consolidate_memory,
)


# Registry: action name -> async function(owner, **kwargs) -> (result_str, success_bool)


# --- exec/shell actions moved to src/actions/shell.py (ADR-042); re-imported
# so the BUILTIN_ACTIONS registry + any direct callers keep working.
from src.actions.shell import (  # noqa: E402,F401
    _run_subprocess,
    action_ssh_command,
    action_run_script,
    action_run_local,
)


async def action_tidy_research(owner: str, **kwargs) -> Tuple[str, bool]:
    """Remove only broken research files (empty or unparseable JSON).

    Research history lives entirely in data/deep_research/<id>.json and is NOT
    backed by chat-session rows — so a file must never be deleted just because
    no chat session matches its id. Only prune files that fail to load."""
    try:
        from pathlib import Path
        import json as _json
        research_dir = Path("data/deep_research")
        if not research_dir.exists():
            raise TaskNoop("no research directory")
        files = list(research_dir.glob("*.json"))
        removed = []
        for p in files:
            try:
                txt = p.read_text(encoding="utf-8").strip()
                if not txt:
                    raise ValueError("empty file")
                _json.loads(txt)  # valid JSON → keep
            except Exception:
                p.unlink(missing_ok=True)
                removed.append(p.stem[:8])
        if not removed:
            raise TaskNoop(f"scanned {len(files)} research file(s), none broken")
        return f"Removed {len(removed)} broken research file(s) of {len(files)}", True
    except Exception as e:
        logger.error(f"tidy_research action failed: {e}")
        return str(e), False


async def action_tidy_calendar(owner: str, **kwargs) -> Tuple[str, bool]:
    """Find duplicate calendar events (same title + start time) and DELETE the dups,
    keeping the oldest (first-seen) instance.

    Incremental: remembers the newest `created_at` already scanned in
    data/tidy_calendar_state.json. If no events have been added since then,
    short-circuits. Otherwise only events newer than the watermark are candidates
    for deletion, but they're checked against the FULL existing set so a new
    duplicate of an old event still gets caught.
    """
    try:
        import json
        from pathlib import Path
        from core.database import SessionLocal, CalendarEvent
        from sqlalchemy import func

        STATE_FILE = Path("data/tidy_calendar_state.json")
        last_watermark = None
        try:
            if STATE_FILE.exists():
                saved = json.loads(STATE_FILE.read_text())
                if saved.get("last_created_at"):
                    last_watermark = datetime.fromisoformat(saved["last_created_at"])
        except Exception:
            last_watermark = None

        db = SessionLocal()
        try:
            newest = db.query(func.max(CalendarEvent.created_at)).scalar()
            db.query(CalendarEvent).count()

            # Short-circuit: nothing new since last run
            if last_watermark is not None and newest is not None and newest <= last_watermark:
                raise TaskNoop(f"no new events since watermark {last_watermark.strftime('%Y-%m-%d %H:%M')}")

            events = db.query(CalendarEvent).order_by(CalendarEvent.dtstart).all()
            # Build full seen-set from events at or before the watermark (known-clean).
            # Events after the watermark are candidates for deletion.
            seen = {}
            candidates = []
            no_title = 0
            for e in events:
                title = (e.summary or "").strip()
                if not title:
                    no_title += 1
                    continue
                if last_watermark is None or (e.created_at and e.created_at <= last_watermark):
                    # Known-clean region: first occurrence wins
                    key = (title.lower(), e.dtstart)
                    if key not in seen:
                        seen[key] = e
                    # If a dup exists in the known-clean region (first run, or imported later
                    # with the same created_at), still remove it — fall through to candidate check.
                    else:
                        candidates.append(e)
                else:
                    candidates.append(e)

            removed = []
            for e in candidates:
                title = (e.summary or "").strip()
                key = (title.lower(), e.dtstart)
                if key in seen:
                    when = e.dtstart.strftime('%Y-%m-%d %H:%M') if e.dtstart else '?'
                    removed.append(f"{title} @ {when}")
                    db.delete(e)
                else:
                    seen[key] = e

            if removed:
                db.commit()

            # Persist the new watermark (newest created_at among events that survive)
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                if newest is not None:
                    STATE_FILE.write_text(json.dumps({
                        "last_created_at": newest.isoformat(),
                        "last_run_at": datetime.utcnow().isoformat(),
                        "scanned": len(events),
                        "removed": len(removed),
                    }, indent=2))
            except Exception as se:
                logger.warning(f"tidy_calendar watermark save failed: {se}")

            new_since = len(candidates)
            parts = [f"Scanned {len(events)} event(s), {new_since} new since last run"]
            if removed:
                preview = "; ".join(removed[:5])
                if len(removed) > 5:
                    preview += f" (+{len(removed) - 5} more)"
                parts.append(f"removed {len(removed)} duplicate(s): {preview}")
            if no_title:
                parts.append(f"{no_title} untitled (kept)")
            if not removed and not no_title:
                parts.append("no duplicates")
            return " · ".join(parts), True
        finally:
            db.close()
    except Exception as e:
        logger.error(f"tidy_calendar action failed: {e}")
        return str(e), False


def _result_has_work(result: str | None) -> bool:
    """Heuristic: did the email pass actually process anything?

    `_run_auto_summarize_once` returns strings like 'Processed 0 emails',
    'No new emails to summarize', 'Tagged 0 / Moved 0', etc. when nothing
    was done. Used to decide whether to record the run or noop it.
    """
    if not result:
        return False
    low = result.lower()
    if "processed 0" in low or "no new" in low or "nothing to" in low:
        return False
    # "Tagged 0 / Moved 0" or similar zero-count summaries
    if low.count(" 0") >= 2 and ("tagged" in low or "moved" in low or "drafted" in low):
        return False
    return True


async def action_summarize_emails(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run one pass of email summary background processing."""
    try:
        from routes.email_pollers import _run_auto_summarize_once
        result = await _run_auto_summarize_once(do_summary=True, do_reply=False)
        if not _result_has_work(result):
            raise TaskNoop(f"summarize: {result or 'no new emails'}")
        return result, True
    except Exception as e:
        logger.error(f"summarize_emails action failed: {e}")
        return str(e), False


async def action_draft_email_replies(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run one pass of AI reply drafting."""
    try:
        from routes.email_pollers import _run_auto_summarize_once
        result = await _run_auto_summarize_once(
            do_summary=False,
            do_reply=True,
            days_back=7,
            progress_cb=kwargs.get("progress_cb"),
        )
        if not _result_has_work(result):
            raise TaskNoop(f"draft replies: {result or 'no new emails'}")
        return result, True
    except Exception as e:
        logger.error(f"draft_email_replies action failed: {e}")
        return str(e), False


_TYPE_COLORS = {
    "work":     "#5b8abf",  # blue
    "personal": "#a07ae0",  # purple
    "health":   "#e06c75",  # red
    "travel":   "#e5a33a",  # orange
    "meal":     "#d8b974",  # tan
    "social":   "#82c882",  # green
    "admin":    "#888888",  # gray
    "other":    "#6b9cb5",  # default
}

_HEURISTIC_TYPES = {
    "health":  ["doctor", "dentist", "clinic", "hospital", "appointment", "checkup", "therapy",
                "physio", "chiropract", "vaccine", "blood test", "xray", "scan", "surgery"],
    "travel":  ["flight", "airport", "train", "shinkansen", "boarding", "uber", "taxi", "trip",
                "hotel", "airbnb", "depart", "arrival", "check-in", "checkout"],
    "meal":    ["lunch", "dinner", "breakfast", "brunch", "coffee", "drinks", "restaurant",
                "reservation", "bar", "cafe"],
    "social":  ["birthday", "party", "hangout", "wedding", "date with", "drinks with",
                "anniversary", "baby shower", "graduation", "picnic", "bbq"],
    "admin":   ["bill", "renewal", "tax", "deadline", "filing", "submit", "due date",
                "registration", "license", "passport", "visa", "form"],
    "work":    ["meeting", "standup", "sync", "1:1", "1on1", "review", "interview",
                "demo", "presentation", "kickoff", "retro", "all-hands", "town hall",
                "call with", "client", "deck"],
}

_HEURISTIC_HIGH = ["flight", "interview", "wedding", "surgery", "exam", "deadline",
                   "court", "presentation", "demo", "kickoff", "launch"]
_HEURISTIC_CRITICAL = ["surgery", "court", "wedding day", "funeral", "delivery date"]


def _classify_event_heuristic(summary: str) -> tuple:
    """Quick heuristic classification — returns (event_type, importance) or (None, None) if unclear."""
    s = (summary or "").lower()
    etype = None
    for t, kws in _HEURISTIC_TYPES.items():
        if any(k in s for k in kws):
            etype = t
            break
    if any(k in s for k in _HEURISTIC_CRITICAL):
        return etype, "critical"
    if any(k in s for k in _HEURISTIC_HIGH):
        return etype, "high"
    return etype, None


async def action_classify_events(owner: str, **kwargs) -> Tuple[str, bool]:
    """Hybrid classification of upcoming calendar events: fast heuristic for
    obvious cases, LLM fallback for ambiguous ones. Assigns event_type +
    importance + color. Re-classifies anything not already set."""
    try:
        from datetime import timedelta
        from core.database import SessionLocal, CalendarEvent
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async
        import re as _re, json as _json

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            horizon = now + timedelta(days=30)
            events = db.query(CalendarEvent).filter(
                CalendarEvent.dtstart >= now,
                CalendarEvent.dtstart <= horizon,
                CalendarEvent.status != "cancelled",
            ).all()
            if not events:
                return "No upcoming events to classify", True

            llm_url, llm_model, llm_headers = resolve_endpoint("utility")
            if not llm_url:
                llm_url, llm_model, llm_headers = resolve_endpoint("default")
            llm_available = bool(llm_url and llm_model)

            # Pull user memories so the LLM has personal context (relationships,
            # job, hobbies). Helps it know e.g. "<name> is your spouse" so their
            # events are personal/social, not work.
            _memory_context = ""
            try:
                from core.database import Memory as _Mem
                _mems = db.query(_Mem).filter(_Mem.owner == owner).limit(60).all() if owner else []
                if _mems:
                    _lines = []
                    for m in _mems:
                        c = (m.content or "").strip()
                        if c:
                            _lines.append(f"- {c[:200]}")
                    if _lines:
                        _memory_context = "USER CONTEXT (relationships, work, life):\n" + "\n".join(_lines[:40]) + "\n\n"
            except Exception as _me:
                logger.debug(f"Could not load memory for classify: {_me}")

            classified_h = 0
            classified_llm = 0
            failed = 0
            unchanged = 0
            # Pass 1: heuristic for obvious cases, collect ambiguous for LLM batch
            llm_queue = []  # list of CalendarEvent objects needing LLM
            for ev in events:
                if ev.event_type and ev.importance and ev.importance != "normal":
                    unchanged += 1
                    continue
                etype, importance = _classify_event_heuristic(ev.summary or "")
                if etype and importance:
                    ev.event_type = etype
                    ev.color = _TYPE_COLORS.get(etype)
                    ev.importance = importance
                    classified_h += 1
                    continue
                # Apply partial heuristic; queue for LLM to fill missing
                if etype:
                    ev.event_type = etype
                    ev.color = _TYPE_COLORS.get(etype)
                if llm_available:
                    llm_queue.append(ev)
                elif etype:
                    classified_h += 1
            # Persist heuristic results before LLM pass (in case LLM is slow/unavailable)
            try:
                db.commit()
            except Exception:
                pass

            # Pass 2: batch LLM classification (10 events per call)
            BATCH = 10
            for i in range(0, len(llm_queue), BATCH):
                batch = llm_queue[i:i+BATCH]
                items = [
                    {"i": idx, "title": (ev.summary or "")[:120],
                     "when": ev.dtstart.isoformat() if ev.dtstart else "",
                     "loc": (ev.location or "")[:80]}
                    for idx, ev in enumerate(batch)
                ]
                prompt = (
                    _memory_context +
                    "Classify these calendar events using the USER CONTEXT above (people they know, "
                    "their job, hobbies). Return ONLY a raw JSON array, no prose, no markdown.\n"
                    "Each item: {\"i\": <index>, \"type\": \"work|personal|health|travel|meal|social|admin|other\", "
                    "\"importance\": \"low|normal|high|critical\"}\n\n"
                    "Type guidance:\n"
                    "- personal = family, partner, kids, pets, errands, home stuff\n"
                    "- social = friends, parties, birthdays, hangouts\n"
                    "- work = the user's own job/career commitments only (not their partner's)\n"
                    "- health = doctor, gym, therapy\n"
                    "- travel = flights, trips, hotels\n"
                    "- meal = lunch/dinner/coffee specifically\n"
                    "- admin = bills, taxes, paperwork\n"
                    "- other = anything else\n\n"
                    "Importance guide: critical = surgery/court/wedding day; high = flight/interview/big presentation/exam; "
                    "normal = regular meetings/appointments; low = recurring routine.\n\n"
                    f"EVENTS: {_json.dumps(items)}"
                )
                try:
                    raw = await llm_call_async(
                        url=llm_url, model=llm_model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1, max_tokens=16384,
                        headers=llm_headers, timeout=180,
                    )
                    from src.text_helpers import strip_think as _st
                    raw = _st(raw or "", prose=False, prompt_echo=False)
                    raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.MULTILINE).strip()
                    m = _re.search(r"\[.*\]", raw, _re.DOTALL)
                    if not m:
                        logger.warning(f"[classify-llm] no JSON array in response: {raw[:300]!r}")
                        failed += len(batch)
                        continue
                    arr = _json.loads(m.group())
                    by_idx = {x.get("i"): x for x in arr if isinstance(x, dict)}
                    for idx, ev in enumerate(batch):
                        x = by_idx.get(idx)
                        if not x:
                            failed += 1
                            continue
                        t = (x.get("type") or "other").lower()
                        imp = (x.get("importance") or "normal").lower()
                        if t in _TYPE_COLORS:
                            ev.event_type = t
                            ev.color = _TYPE_COLORS[t]
                        if imp in ("low", "normal", "high", "critical"):
                            ev.importance = imp
                        classified_llm += 1
                        logger.info(f"[classify-llm] '{ev.summary}' → type={t} importance={imp}")
                except Exception as e:
                    logger.warning(f"[classify-llm] batch failed: {e}")
                    failed += len(batch)
                # Commit after each batch so partial progress persists
                try:
                    db.commit()
                except Exception as ce:
                    logger.warning(f"[classify-llm] commit failed: {ce}")
            # Final commit covers heuristic-only updates from pass 1
            db.commit()
            parts = [f"Scanned {len(events)} upcoming event(s)"]
            if classified_h:
                parts.append(f"{classified_h} via heuristic")
            if classified_llm:
                parts.append(f"{classified_llm} via LLM")
            if unchanged:
                parts.append(f"{unchanged} already set (skipped)")
            if failed:
                parts.append(f"{failed} LLM failed")
            return " · ".join(parts), True
        finally:
            db.close()
    except Exception as e:
        logger.error(f"classify_events action failed: {e}")
        return str(e), False


async def action_ping_events(owner: str, **kwargs) -> Tuple[str, bool]:
    """Calendar event reminders are now dispatched by Notes."""
    raise TaskNoop("calendar event reminders are handled by Notes")


async def action_extract_email_events(owner: str, **kwargs) -> Tuple[str, bool]:
    """Scan recent emails for booking confirmations / meetings / events
    and auto-add them to the calendar."""
    import asyncio as _aio
    try:
        from routes.email_pollers import _run_auto_summarize_once
        try:
            # Hard wall-clock budget: 5 min total. Per-LLM call already has its own timeout.
            result = await _aio.wait_for(
                _run_auto_summarize_once(
                    do_summary=False, do_reply=False, do_calendar=True, days_back=3,
                ),
                timeout=300,
            )
            if not _result_has_work(result):
                raise TaskNoop(f"email→calendar: {result or 'no new emails'}")
            return f"{result} (3d window)", True
        except _aio.TimeoutError:
            return "Email→calendar pass exceeded 5 min budget — try fewer emails or a faster model", False
    except Exception as e:
        logger.error(f"extract_email_events action failed: {e}")
        return str(e), False


async def action_mark_email_boundaries(owner: str, **kwargs) -> Tuple[str, bool]:
    """LLM-based signature / quoted-reply boundary detection. For each new
    inbox email that we haven't analyzed yet, ask the model to return char
    offsets where the signature and quoted-reply start. Cache the offsets
    keyed by Message-ID — once cached, the renderer uses them directly with
    no further LLM calls. Caps at 30 emails per pass to keep cost bounded.
    """
    try:
        import sqlite3 as _sql3
        import json as _json
        import re as _re
        import email as _email_mod
        import asyncio as _aio
        from datetime import datetime as _dt
        from routes.email_helpers import _imap_connect, _decode_header, SCHEDULED_DB
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async

        # Pull recent inbox UIDs + Message-IDs directly via IMAP (the
        # nested helpers in email_routes aren't importable, and this keeps
        # the action self-contained).
        def _pull_recent():
            results = []
            conn = _imap_connect(None)
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.search(None, "ALL")
                if status != "OK" or not data or not data[0]:
                    return results
                uids = data[0].split()[-50:][::-1]  # newest 50
                for uid in uids:
                    try:
                        st, msg_data = conn.fetch(uid, "(RFC822.HEADER)")
                        if st != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                        if not raw:
                            continue
                        msg = _email_mod.message_from_bytes(raw)
                        results.append({
                            "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                            "message_id": (msg.get("Message-ID") or "").strip(),
                            "subject": _decode_header(msg.get("Subject", "")),
                        })
                    except Exception:
                        continue
            finally:
                try: conn.logout()
                except Exception: pass
            return results

        mails = await _aio.to_thread(_pull_recent)
        if not mails:
            raise TaskNoop("no emails to analyze")

        url, model, headers = resolve_endpoint("utility")
        if not url or not model:
            url, model, headers = resolve_endpoint("default")
        if not url or not model:
            return "No LLM endpoint available", False

        c = _sql3.connect(SCHEDULED_DB)
        already = {r[0] for r in c.execute(
            "SELECT message_id FROM email_boundaries"
        ).fetchall()}
        c.close()

        analyzed = 0
        skipped = 0
        for em in mails[:30]:
            mid = (em.get("message_id") or "").strip()
            if not mid or mid in already:
                skipped += 1
                continue
            uid = em.get("uid")
            if not uid:
                continue
            def _fetch_body(_uid):
                conn = _imap_connect(None)
                try:
                    conn.select("INBOX", readonly=True)
                    st, data = conn.fetch(_uid, "(BODY.PEEK[TEXT])")
                    if st != "OK" or not data or not data[0]:
                        return ""
                    raw = data[0][1] if isinstance(data[0], tuple) else None
                    if not raw:
                        return ""
                    try:
                        return raw.decode("utf-8", errors="replace")
                    except Exception:
                        return str(raw)
                finally:
                    try: conn.logout()
                    except Exception: pass
            try:
                body = (await _aio.to_thread(_fetch_body, str(uid))).strip()
            except Exception as e:
                logger.warning(f"boundary detection: IMAP fetch failed for uid={uid} mid={mid}: {e}")
                continue
            if not body or len(body) < 100:
                continue
            # Truncate very long bodies — boundaries usually live in the
            # first few KB of plain text.
            truncated = body[:8000]

            prompt = (
                "Identify where the signature and the quoted-reply start in "
                "this email body. Return ONLY raw JSON, no prose. Schema:\n"
                '{"sig_start": <int>, "quote_start": <int>}\n\n'
                "Rules:\n"
                "- sig_start = char offset where the sender's signature block "
                "begins (closing phrase like 'Best regards' / 'Mit freundlichen' / "
                "'Med vänliga' / contact details / disclaimer / job title block). "
                "Use -1 if none.\n"
                "- quote_start = char offset where any quoted-reply / forwarded "
                "thread begins (lines like 'On <date>, <name> wrote:', "
                "'From: ... Sent: ... Subject:' in any language — German 'Von:', "
                "French 'De :', Spanish 'De:', etc.). Use -1 if none.\n"
                "- Both offsets are byte/char positions in the input string starting "
                "from 0. The signature/quote should INCLUDE the marker line itself.\n"
                "- If both exist, sig_start is normally before quote_start (sig of "
                "the current message, then quoted thread underneath).\n\n"
                f"BODY (length={len(truncated)}):\n{truncated}"
            )
            try:
                raw = await llm_call_async(
                    url=url, model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=200,
                    headers=headers, timeout=60,
                )
                from src.text_helpers import strip_think as _st
                raw = _st(raw or "", prose=False, prompt_echo=False)
                raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.MULTILINE).strip()
                # Balanced-brace match: handles {"sig_start": 10, "info": {}}
                # which the previous [^{}] class would have broken on.
                start = raw.find("{")
                m_text = None
                if start != -1:
                    depth = 0
                    for i in range(start, len(raw)):
                        if raw[i] == "{":
                            depth += 1
                        elif raw[i] == "}":
                            depth -= 1
                            if depth == 0:
                                m_text = raw[start:i + 1]
                                break
                if not m_text:
                    logger.warning(f"boundary detection: no JSON object in LLM response for mid={mid}: {raw[:200]!r}")
                    continue
                parsed = _json.loads(m_text)
                sig = int(parsed.get("sig_start", -1))
                quote = int(parsed.get("quote_start", -1))
            except Exception as e:
                logger.warning(f"boundary detection failed for mid={mid}: {e}")
                continue

            # Also pre-parse the thread tree so the client never has to.
            try:
                from src.email_thread_parser import parse_thread, THREAD_PARSER_VERSION
                # The boundary loop only has the plaintext body; parse_thread
                # also accepts None for HTML so this is safe.
                turns = parse_thread(None, body)
                turns_json = (
                    _json.dumps({"v": THREAD_PARSER_VERSION, "turns": turns})
                    if turns else None
                )
            except Exception as _pe:
                logger.debug(f"thread parse failed for {mid}: {_pe}")
                turns_json = None

            try:
                c = _sql3.connect(SCHEDULED_DB)
                c.execute(
                    "INSERT OR REPLACE INTO email_boundaries "
                    "(message_id, uid, folder, sig_start, quote_start, model_used, created_at, turns_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (mid, str(uid), "INBOX", sig, quote, model, _dt.utcnow().isoformat(), turns_json),
                )
                c.commit()
                c.close()
                analyzed += 1
            except Exception as e:
                logger.warning(f"could not cache boundaries for {mid}: {e}")

        if analyzed == 0:
            # All recent emails already had boundaries cached — nothing new
            # to do, don't pollute Activity.
            raise TaskNoop(f"boundaries already cached for {skipped} email(s)")
        return f"Marked boundaries: {analyzed} new, {skipped} cached", True
    except Exception as e:
        logger.error(f"mark_email_boundaries failed: {e}")
        return str(e), False


async def action_learn_sender_signatures(owner: str, **kwargs) -> Tuple[str, bool]:
    """For each sender with ≥3 recent inbox emails, ask the LLM to extract
    the common signature block across their messages. The cached sig is
    served on the `/read` endpoint so the renderer can fold signatures
    consistently from that address (no more heuristic regex juggling).
    Caps at 20 senders per pass; re-runs after 30 days per sender."""
    try:
        import sqlite3 as _sql3
        import re as _re
        import email as _email_mod
        import asyncio as _aio
        from datetime import datetime as _dt, timedelta as _td
        from routes.email_helpers import _imap_connect, SCHEDULED_DB
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async

        # 1. Pull recent UIDs + From headers cheaply (header-only fetch).
        def _pull_headers():
            results = []
            conn = _imap_connect(None)
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.search(None, "ALL")
                if status != "OK" or not data or not data[0]:
                    return results
                uids = data[0].split()[-300:][::-1]  # newest 300
                for uid in uids:
                    try:
                        st, msg_data = conn.fetch(
                            uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])"
                        )
                        if st != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                        if not raw:
                            continue
                        msg = _email_mod.message_from_bytes(raw)
                        from_raw = msg.get("From", "")
                        from_addr = _email_mod.utils.parseaddr(from_raw)[1].lower().strip()
                        if not from_addr or "@" not in from_addr:
                            continue
                        results.append({
                            "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                            "from_address": from_addr,
                        })
                    except Exception:
                        continue
            finally:
                try: conn.logout()
                except Exception: pass
            return results

        mails = await _aio.to_thread(_pull_headers)
        if not mails:
            return "No emails to scan", True

        # 2. Group by sender; drop addresses that don't carry useful sigs.
        SKIP_PREFIXES = (
            "noreply", "no-reply", "donotreply", "do-not-reply",
            "mailer-daemon", "notifications", "notification", "bounce",
            "newsletter", "support@", "info@", "admin@",
        )
        by_sender: dict[str, list[dict]] = {}
        for m in mails:
            addr = m["from_address"]
            local = addr.split("@", 1)[0]
            if any(local == p or local.startswith(p) for p in SKIP_PREFIXES):
                continue
            # Skip plus-aliases / list-style addresses too.
            if "+" in local or "-noreply" in addr or "-bounces" in addr:
                continue
            by_sender.setdefault(addr, []).append(m)

        # 3. Eligibility: ≥3 emails AND (no cache OR cache > 30 days old).
        try:
            conn = _sql3.connect(SCHEDULED_DB)
            cached = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT from_address, last_built_at FROM sender_signatures"
                ).fetchall()
            }
            conn.close()
        except Exception:
            cached = {}

        cutoff_iso = (_dt.utcnow() - _td(days=30)).isoformat()
        eligible: list[tuple[str, list[dict]]] = []
        for addr, msgs in by_sender.items():
            if len(msgs) < 3:
                continue
            if cached.get(addr, "") > cutoff_iso:
                continue
            eligible.append((addr, msgs[:5]))  # use up to last 5 emails

        if not eligible:
            return "All sender sigs already cached (or no eligible senders)", True

        url, model, headers = resolve_endpoint("utility")
        if not url or not model:
            url, model, headers = resolve_endpoint("default")
        if not url or not model:
            return "No LLM endpoint available", False

        analyzed = 0
        no_sig = 0
        for addr, msgs in eligible[:20]:  # cost cap per run

            def _fetch_bodies(_msgs):
                bodies = []
                conn2 = _imap_connect(None)
                try:
                    conn2.select("INBOX", readonly=True)
                    for mm in _msgs:
                        try:
                            st, data = conn2.fetch(mm["uid"], "(BODY.PEEK[TEXT])")
                            if st != "OK" or not data or not data[0]:
                                continue
                            raw = data[0][1] if isinstance(data[0], tuple) else None
                            if not raw:
                                continue
                            text = raw.decode("utf-8", errors="replace")
                            bodies.append(text[:4000])
                        except Exception:
                            continue
                finally:
                    try: conn2.logout()
                    except Exception: pass
                return bodies

            try:
                bodies = await _aio.to_thread(_fetch_bodies, msgs)
            except Exception as e:
                logger.warning(f"sig learner: fetch bodies failed for {addr}: {e}")
                continue
            if len(bodies) < 2:
                continue

            joined = "\n\n---NEXT EMAIL---\n\n".join(bodies[:5])
            prompt = (
                "You are extracting the literal common SIGNATURE block that "
                "appears at the END of multiple emails from the same sender.\n\n"
                "Return ONLY the exact signature text, verbatim, with original "
                "line breaks preserved. If there is no clear common signature "
                "block across these emails, respond with the single token: "
                "NONE\n\n"
                "INCLUDE: title, company, address, phone, email/url lines, "
                "legal disclaimer block.\n"
                "EXCLUDE: greetings ('Hi', 'Dear'), closing phrases on their "
                "own ('Best regards'), the sender's name on its own line, the "
                "body content, quoted/forwarded threads (lines starting with "
                "'>' or 'On ... wrote:' or 'From: ... Sent:').\n\n"
                f"EMAILS FROM {addr}:\n{joined}"
            )

            try:
                raw = await llm_call_async(
                    url=url, model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=600,
                    headers=headers, timeout=60,
                )
                from src.text_helpers import strip_think as _st
                sig = _st(raw or "", prose=False, prompt_echo=False).strip()
                # Strip surrounding code fences if the LLM added them.
                sig = _re.sub(r"^```[\w]*\n?", "", sig)
                sig = _re.sub(r"\n?```\s*$", "", sig)
                sig = sig.strip()
            except Exception as e:
                logger.warning(f"sig LLM call failed for {addr}: {e}")
                continue

            # NONE sentinel or out-of-bounds → cache a NULL row so we don't
            # re-try for 30 days, then move on.
            if (
                not sig
                or sig.upper().strip().strip(".") == "NONE"
                or len(sig) < 15
                or len(sig) > 3000
            ):
                cached_sig: str | None = None
                no_sig += 1
            else:
                cached_sig = sig

            try:
                conn = _sql3.connect(SCHEDULED_DB)
                conn.execute(
                    "INSERT OR REPLACE INTO sender_signatures "
                    "(from_address, signature_text, sample_count, last_built_at, model_used, source) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (addr, cached_sig, len(bodies), _dt.utcnow().isoformat(), model, "llm"),
                )
                conn.commit()
                conn.close()
                analyzed += 1
            except Exception as e:
                logger.warning(f"sig cache write failed for {addr}: {e}")

        return f"Learned sigs: {analyzed - no_sig} found, {no_sig} no-sig, of {len(eligible)} eligible", True
    except Exception as e:
        logger.error(f"learn_sender_signatures failed: {e}")
        return str(e), False


async def action_daily_brief(owner: str, **kwargs) -> Tuple[str, bool]:
    """Build a short morning digest: today's calendar events, unread email count
    + top-N senders/subjects, active todos."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        import json as _json

        from core.database import SessionLocal, CalendarEvent, CalendarCal, Note
        from routes.email_helpers import _imap_connect, _decode_header

        # ----- Calendar: today's events -----
        today = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + _td(days=1)
        # v2 review HIGH-12: gate the OR-null branch on single-user
        # (unconfigured) deploys only. In a multi-user deploy, one
        # user's daily brief must not include another user's notes or
        # events that happen to be stored with owner=None.
        try:
            from core.auth import AuthManager
            _allow_null = not AuthManager().is_configured
        except Exception:
            _allow_null = False
        db = SessionLocal()
        try:
            ev_q = db.query(CalendarEvent).join(CalendarCal).filter(
                CalendarEvent.dtstart < tomorrow,
                CalendarEvent.dtend > today,
                CalendarEvent.status != "cancelled",
            )
            if owner:
                ev_q = owner_filter(ev_q, CalendarCal, owner, include_shared=_allow_null)
            events = ev_q.order_by(CalendarEvent.dtstart).all()
            # ----- Notes: pinned + non-archived todos with at least one undone item -----
            n_q = db.query(Note).filter(Note.archived == False)  # noqa: E712
            if owner:
                n_q = owner_filter(n_q, Note, owner, include_shared=_allow_null)
            notes = n_q.all()
        finally:
            db.close()

        # ----- Email: unread count + top 5 inbox subjects (best-effort) -----
        # Direct IMAP: cheaper than the full _list_emails_sync helper and
        # avoids the module/import coupling that broke this once already.
        unread_count = 0
        recent_subjects: list[tuple[str, str]] = []
        try:
            import email as _email
            conn = _imap_connect(None)
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.search(None, "UNSEEN")
                uids = (data[0].split() if status == "OK" and data and data[0] else [])
                unread_count = len(uids)
                # Grab headers for the most recent 5 unread (UIDs increase with arrival)
                for uid in uids[-5:][::-1]:
                    try:
                        _, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                        if not msg_data or not msg_data[0]:
                            continue
                        hdr = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                        parsed = _email.message_from_bytes(hdr)
                        subject = _decode_header(parsed.get("Subject") or "") or "(no subject)"
                        from_raw = _decode_header(parsed.get("From") or "") or "?"
                        # Extract just the display name if "Name <addr>" form
                        if "<" in from_raw:
                            name = from_raw.split("<", 1)[0].strip().strip('"') or from_raw
                        else:
                            name = from_raw
                        recent_subjects.append((name, subject))
                    except Exception as fe:
                        logger.debug(f"daily_brief: header fetch for uid {uid} failed: {fe}")
            finally:
                try: conn.logout()
                except Exception: pass
        except Exception as ee:
            logger.debug(f"daily_brief: email fetch failed: {ee}")

        # Pull active todo items from notes
        todo_lines: list[str] = []
        for n in notes:
            if n.note_type == "checklist" and n.items:
                try:
                    items = _json.loads(n.items)
                    pending = [it.get("text", "") for it in items if not it.get("done")]
                    for t in pending[:3]:
                        if t:
                            todo_lines.append(f"{n.title or 'Checklist'}: {t}")
                except Exception:
                    continue
            elif n.pinned and n.title:
                todo_lines.append(n.title)

        # ----- Compose -----
        # %-d is GNU-only; format the day with str() so the brief works on
        # Windows / non-glibc Python builds too.
        date_label = today.strftime(f"%A, %B {today.day}, %Y")

        plain = [f"Daily brief — {date_label}", ""]
        if events:
            plain.append("Calendar:")
            for e in events:
                t = e.dtstart.strftime("%H:%M") if not e.all_day else "all day"
                loc = f" @ {e.location}" if e.location else ""
                plain.append(f"  {t}  {e.summary}{loc}")
            plain.append("")
        else:
            plain.append("Calendar: nothing scheduled.")
            plain.append("")

        plain.append(f"Email: {unread_count} unread")
        for sender, subj in recent_subjects:
            plain.append(f"  · {sender} — {subj}")
        plain.append("")

        if todo_lines:
            plain.append("Todos:")
            for t in todo_lines[:10]:
                plain.append(f"  · {t}")
        else:
            plain.append("Todos: none active.")

        plain_body = "\n".join(plain)

        return plain_body, True
    except Exception as e:
        logger.error(f"daily_brief action failed: {e}")
        return str(e), False


async def action_test_skills(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run the per-skill Test on every skill: agent runs the procedure in a
    sandbox, LLM judges the transcript, verdict is recorded on the skill.
    ADVISORY ONLY — only writes set_audit (never rewrites SKILL.md, never
    demotes status, never overrides confidence)."""
    try:
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        from routes.skills_routes import _run_skill_test_once, _skill_test_task
        from src.endpoint_resolver import resolve_endpoint

        # #3 SCOPE GUARD: refuse to run on a None/empty owner — otherwise
        # `sm.load(owner=None)` returns every user's skills and we'd cross-
        # test (and write audit verdicts to) other users' data in a
        # multi-user deployment.
        if not owner:
            return "test_skills requires an owner on the task — refusing to run without scope.", False

        sm = SkillsManager(DATA_DIR)
        skills = sm.load(owner=owner)
        names = [s.get("name") for s in skills if s.get("name")]
        if not names:
            raise TaskNoop("no skills to test")

        url, model, headers = resolve_endpoint("default")
        if not url or not model:
            return "No Default/Utility model configured — set one in Settings.", False

        # #2 NO SILENT MODEL SWAP: if the configured model isn't served by the
        # endpoint, try a basename match — but fail loudly instead of grabbing
        # `avail[0]` which could be an embedding-only model and produce 36
        # garbage transcripts → 36 'unknown' verdicts with no hint why.
        try:
            from src.llm_core import list_model_ids
            avail = list_model_ids(url, headers=headers)
            if avail and model not in avail:
                import os as _os
                base = _os.path.basename((model or "").rstrip("/"))
                m = next((a for a in avail if _os.path.basename(a.rstrip("/")) == base), None)
                if m:
                    model = m
                else:
                    return (f"Default model '{model}' not served by endpoint {url}. "
                            f"Available: {', '.join(avail[:8])}{'…' if len(avail) > 8 else ''}. "
                            "Set a valid Default model in Settings."), False
        except Exception as _e:
            logger.warning(f"test_skills model resolve check failed (continuing): {_e}")

        logger.info(f"test_skills: starting on {len(names)} skills, model={model}, owner={owner!r}")

        from collections import Counter
        tally = Counter()
        per_skill_log = []
        for skill in skills:
            name = skill.get("name")
            if not name:
                continue
            md = sm.read_skill_md(name) or ""
            if not md:
                tally["skipped"] += 1
                per_skill_log.append(f"{name}: skipped (no SKILL.md)")
                continue
            task = _skill_test_task(skill)
            try:
                transcript, verdict = await _run_skill_test_once(md, task, url, model, headers, owner)
                v = (verdict or {}).get("verdict") or "unknown"
                tally[v] += 1
                summary = (verdict or {}).get("summary") or ""
                tlen = len(transcript or "")
                detail = ""
                if v in ("unknown", "inconclusive", "fail", "needs_work"):
                    bits = []
                    if summary: bits.append(summary[:160])
                    if tlen < 200: bits.append(f"transcript {tlen}b")
                    if bits: detail = " — " + "; ".join(bits)
                per_skill_log.append(f"{name}: {v}{detail}")
                # #4 + #8 + #12: ONLY persist a real verdict (pass / needs_work /
                # fail / inconclusive). Skip 'unknown' — that's the judge's
                # "couldn't parse" sentinel, not a real result, and persisting
                # it pollutes the verified-badge UI. Also skip the confidence
                # rewrite entirely — update_skill() re-serialises SKILL.md
                # (contradicts "advisory only" docstring) and overwriting a
                # user-set value (e.g. 1.0 → 0.95) is destructive.
                if v in ("pass", "needs_work", "fail", "inconclusive"):
                    try:
                        sm.set_audit(name, v, by_teacher=False, worker_model=model)
                    except Exception as _e:
                        logger.warning(f"test_skills set_audit({name}) failed: {_e}")
                if v == "unknown":
                    logger.warning(f"test_skills: {name} → unknown — {summary[:200]}; transcript_len={tlen}")
            except Exception as e:
                logger.exception(f"test_skills: {name} errored")
                tally["error"] += 1
                per_skill_log.append(f"{name}: error — {str(e)[:200]}")

        parts = []
        for k in ("pass", "needs_work", "fail", "inconclusive", "unknown", "skipped", "error"):
            if tally.get(k):
                parts.append(f"{tally[k]} {k}")
        header = f"Tested {len(names)} skill(s): " + (" · ".join(parts) or "0")
        # Multi-line result: summary first, then per-skill detail. The Tasks
        # Activity feed renders this verbatim, so the user can see per-skill
        # outcomes + the judge's "why" without checking uvicorn stdout.
        body = "\n".join(per_skill_log)
        return f"{header}\nmodel={model}\n\n{body}", True
    except TaskNoop:
        raise
    except Exception as e:
        logger.error(f"test_skills action failed: {e}")
        return str(e), False


async def action_audit_skills(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run the real skills audit pipeline for skills that have not been audited.

    Unlike test_skills, this uses the same audit logic as the UI Audit all flow:
    metadata narrowing, self-edit/retry, optional teacher rewrite, necessity
    tagging, and publish/draft finalization from the user's confidence threshold.
    """
    try:
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        from routes.skills_routes import (
            _resolve_audit_models, _run_audit_all_job, _skill_audit_jobs,
        )

        if not owner:
            return "audit_skills requires an owner — refusing to run without scope.", False

        key = (owner or "",)
        existing = _skill_audit_jobs.get(key)
        if existing and existing.get("status") == "running":
            raise TaskNoop("skill audit already running")

        sm = SkillsManager(DATA_DIR)
        skills = sm.load(owner=owner)
        names = [
            s.get("name") for s in skills
            if s.get("name") and not s.get("audit_verdict")
        ]
        if not names:
            raise TaskNoop("no unaudited skills")

        url, model, headers, teacher = _resolve_audit_models()
        try:
            from src.llm_core import seconds_since_model_activity
            recent = seconds_since_model_activity(url, model)
        except Exception:
            recent = None
        if recent is not None and recent < (20 * 60):
            raise TaskDeferred(
                f"audit model {model} was used {int(recent)}s ago; waiting for quiet window",
                delay_seconds=20 * 60,
            )

        import time as _time
        _skill_audit_jobs[key] = {
            "status": "running", "scope": "scheduled-unchecked", "model": model,
            "teacher": teacher[1] if teacher else None,
            "total": len(names), "done": 0, "current": None,
            "results": [], "log": [
                f"Scheduled audit of {len(names)} unaudited skill(s) with {model}"
                + (f"; teacher {teacher[1]}" if teacher else "")
            ],
            "started": _time.time(), "cancel": False,
        }
        await _run_audit_all_job(key, sm, names, url, model, headers, teacher, owner)
        job = _skill_audit_jobs.get(key, {})
        counts = {}
        for r in job.get("results", []):
            k = r.get("result") or "unknown"
            counts[k] = counts.get(k, 0) + 1
        summary = " · ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "0 results"
        return f"Audited {job.get('done', 0)}/{len(names)} unaudited skill(s): {summary}", True
    except TaskNoop:
        raise
    except Exception as e:
        logger.error(f"audit_skills action failed: {e}")
        return str(e), False


async def action_ping_notes(owner: str, **kwargs) -> Tuple[str, bool]:
    """Background note-due scanner. Fires a reminder for any note whose
    `due_date` falls in the current ±5-minute window and hasn't been pinged
    within the last 25 minutes. Mirrors `action_ping_events` for calendar.

    State (`data/note_pings.json`): {note_id: iso_ts_of_last_ping}. Pruned
    on each run by dropping entries for notes that are gone/archived/replied.
    """
    try:
        import json as _json
        import time as _time
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from pathlib import Path as _P
        from core.database import SessionLocal as _SL, Note as _N

        # Per-owner state file so cache-pruning doesn't cross-delete other
        # users' entries (review C4). Legacy path kept as fallback so a
        # single-user install (empty owner) doesn't lose its history.
        _owner_slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
        STATE = _P(f"data/note_pings_{_owner_slug}.json")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        # One-time migration: if legacy global file exists and per-owner file
        # doesn't, seed from global (entries for OTHER owners still get pruned
        # on their first run — acceptable, prevents silent loss).
        _legacy = _P("data/note_pings.json")
        if _legacy.exists() and not STATE.exists():
            try:
                STATE.write_text(_legacy.read_text())
            except Exception:
                pass
        # Scanner ticks every 60s in _note_pings_loop. 90s window guarantees
        # every note's due time lands inside at least one tick's window.
        WINDOW_SEC = 90
        REPING_MIN = 25     # don't re-ping same note more often than this

        def _parse_due(s: str):
            """Accept '2026-05-29T16:31' (local) or '...Z' (UTC). Returns UTC datetime."""
            if not s:
                return None
            try:
                # Handle the JS-style 'Z' suffix.
                if s.endswith("Z"):
                    return _dt.fromisoformat(s[:-1]).replace(tzinfo=_tz.utc)
                # Naive → assume local server time.
                d = _dt.fromisoformat(s)
                if d.tzinfo is None:
                    d = d.astimezone().astimezone(_tz.utc)
                return d.astimezone(_tz.utc)
            except Exception:
                return None

        try:
            cache = _json.loads(STATE.read_text()) if STATE.exists() else {}
        except Exception:
            cache = {}

        db = _SL()
        try:
            q = db.query(_N).filter(_N.archived == False)  # noqa: E712
            q = q.filter(_N.due_date.isnot(None), _N.due_date != "")
            if owner:
                # Match owner OR legacy null-owner notes (single-user installs).
                q = owner_filter(q, _N, owner)
            notes = q.all()
            if not notes:
                raise TaskNoop("no notes with due dates")

            now = _dt.now(_tz.utc)
            window = _td(seconds=WINDOW_SEC)
            reping_cutoff = now - _td(minutes=REPING_MIN)
            seen_ids = set()
            sent = []

            for n in notes:
                seen_ids.add(n.id)
                due = _parse_due(n.due_date)
                if not due:
                    continue
                # Inside the ±5min window?
                if abs((due - now).total_seconds()) > window.total_seconds():
                    continue
                # Recently pinged? Skip.
                last = cache.get(n.id)
                if last:
                    try:
                        if isinstance(last, dict):
                            last = last.get("at")
                        last_dt = _dt.fromisoformat(str(last))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=_tz.utc)
                        if last_dt >= reping_cutoff:
                            continue
                    except Exception:
                        pass
                # Compose + dispatch.
                title = (n.title or "Reminder").strip() or "Reminder"
                body_parts = []
                if n.content:
                    body_parts.append(n.content[:400])
                # Items: list pending checklist entries inline.
                if n.items:
                    try:
                        items = _json.loads(n.items)
                        pending = [
                            it.get("text", "")
                            for it in items
                            if not it.get("done") and not it.get("checked")
                        ]
                        if pending:
                            body_parts.append("Pending:\n" + "\n".join(f"- {t}" for t in pending[:8]))
                    except Exception:
                        pass
                body = "\n\n".join(p for p in body_parts if p) or title
                try:
                    from routes.note_routes import dispatch_reminder
                    await dispatch_reminder(
                        title=title, note_body=body, note_id=n.id,
                        owner=n.owner or owner or "",
                    )
                    cache[n.id] = now.isoformat()
                    sent.append(title)
                except Exception as e:
                    logger.warning(f"ping_notes: dispatch failed for {n.id}: {e}")

            # Prune cache entries for notes that no longer exist.
            for stale in [k for k in cache if k not in seen_ids]:
                cache.pop(stale, None)

            try:
                STATE.write_text(_json.dumps(cache))
            except Exception as e:
                logger.warning(f"ping_notes: cache write failed: {e}")

            if not sent:
                raise TaskNoop(f"scanned {len(notes)} note(s), none due in ±{WINDOW_SEC}s")
            preview = "; ".join(sent[:3])
            extra = f" (+{len(sent) - 3} more)" if len(sent) > 3 else ""
            return f"Pinged {len(sent)} note(s): {preview}{extra}", True
        finally:
            db.close()
    except TaskNoop:
        raise
    except Exception as e:
        logger.exception("ping_notes action failed")
        return str(e), False


# --- email-triage action moved to src/actions/email_triage.py (ADR-044);
# re-imported so BUILTIN_ACTIONS + external callers (tests) keep working.
from src.actions.email_triage import (  # noqa: E402,F401
    _EmailTriage,
    _TRIAGE_TAG_REMAP,
    _TRIAGE_RUBRIC,
    _normalize_triage,
    _email_triage_verdict,
    action_check_email_urgency,
)


BUILTIN_ACTIONS = {
    "tidy_sessions": action_tidy_sessions,
    "tidy_documents": action_tidy_documents,
    "consolidate_memory": action_consolidate_memory,
    "tidy_research": action_tidy_research,
    "summarize_emails": action_summarize_emails,
    "draft_email_replies": action_draft_email_replies,
    "extract_email_events": action_extract_email_events,
    "classify_events": action_classify_events,
    # ping_events removed from the user-facing registry. Calendar reminders
    # are represented as Notes, so note pings are the single dispatch path.
    "daily_brief": action_daily_brief,
    "mark_email_boundaries": action_mark_email_boundaries,
    "learn_sender_signatures": action_learn_sender_signatures,
    "ssh_command": action_ssh_command,
    "run_script": action_run_script,
    "run_local": action_run_local,
    "test_skills": action_test_skills,
    "audit_skills": action_audit_skills,
    "check_email_urgency": action_check_email_urgency,
    # ping_notes removed from the registry — runs only inside `_note_pings_loop`.
}

# Descriptions for the UI/API
BUILTIN_ACTION_INFO = {
    "tidy_sessions": "Clean up empty chat sessions and auto-sort into folders",
    "tidy_documents": "Remove junk/empty documents",
    "consolidate_memory": "Remove duplicate memories",
    "tidy_research": "Remove orphaned research files (sessions that were deleted)",
    "summarize_emails": "Pre-generate AI summaries for new inbox emails",
    "draft_email_replies": "Pre-draft AI reply suggestions for new inbox emails",
    "extract_email_events": "Scan emails for booking/meeting confirmations and auto-add to calendar",
    "classify_events": "Tag upcoming events with importance (low/normal/high/critical) and type (work/health/travel/etc.); colors them too",
    "daily_brief": "Build a morning digest: today's calendar, unread email count + top senders, active todos",
    "mark_email_boundaries": "LLM-detect signature & quoted-reply offsets in new emails; cached so future renders fold without further LLM calls",
    "learn_sender_signatures": "LLM learns each sender's signature from 3+ of their recent emails; cached per address so future renders fold sigs reliably without heuristics",
    "ssh_command": "Run a shell command on a local or remote host",
    "run_script": "Run a script locally or on ODYSSEUS_SCRIPT_HOST",
    "test_skills": "Run the per-skill Test on every skill: agent run + LLM judge → records verdict on the skill (pass/needs_work/fail/inconclusive). Advisory only — never rewrites or demotes anything.",
    "audit_skills": "Audit unaudited skills after enough new skills are added: test, narrow metadata, self-edit/retry, optional teacher rewrite, tag duplicates/trivial skills, and publish/draft using the auto-approve threshold.",
    "check_email_urgency": "Scan unread emails hourly, tag urgent/reply-soon/newsletter/marketing/spam, and send a reminder when a new email needs a fast reply.",
}
