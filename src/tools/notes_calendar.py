"""Notes + calendar tool handlers (the personal-organizer group).

Extracted verbatim from src/tool_implementations.py (Phase 2.2 / ADR-033).
Behavior is unchanged; both handlers are re-exported from
src.tool_implementations so existing import paths keep working (tool_execution,
task_scheduler, email_pollers). This module owns its own logger (name
src.tools.notes_calendar); the shared _parse_tool_args is provided by a thin
lazy shim to avoid a circular import with the tool_implementations module.
"""

import json
import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _parse_tool_args(content):
    from src.tool_implementations import _parse_tool_args as _impl
    return _impl(content)


async def do_manage_notes(content: str, owner: Optional[str] = None) -> Dict:
    """Handle manage_notes tool calls: CRUD on notes and checklists."""
    import uuid as _uuid
    from core.database import SessionLocal, Note
    from sqlalchemy.orm.attributes import flag_modified

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    # Action aliases — match what models actually emit. `create` is the most
    # common alternative to `add`. Hyphenated forms also accepted.
    action = (args.get("action") or "").replace("-", "_").strip().lower()
    _NOTE_ACTION_ALIASES = {
        "create": "add",
        "new": "add",
        "save": "add",
        "remind": "add",
        "remove": "delete",
        "remove_item": "toggle_item",
    }
    action = _NOTE_ACTION_ALIASES.get(action, action)
    db = SessionLocal()

    def _norm_note_title(value: str) -> str:
        text = (value or "").strip().lower()
        text = re.sub(r"^\s*reminder\s*:\s*", "", text)
        return re.sub(r"\s+", " ", text)

    try:
        if action == "list":
            q = db.query(Note)
            if owner is not None:
                q = q.filter(Note.owner == owner)
            if args.get("label"):
                q = q.filter(Note.label == args["label"])
            show_archived = args.get("archived", False)
            q = q.filter(Note.archived == show_archived)
            notes = q.order_by(Note.pinned.desc(), Note.updated_at.desc()).all()
            if not notes:
                return {"response": "No notes found.", "exit_code": 0}
            lines = []
            for n in notes:
                pin = " [PINNED]" if n.pinned else ""
                typ = " [checklist]" if n.note_type == "checklist" else ""
                lbl = f" #{n.label}" if n.label else ""
                title = n.title or "(untitled)"
                lines.append(f"- [{n.id[:8]}] **{title}**{pin}{typ}{lbl}")
                if n.note_type == "checklist" and n.items:
                    try:
                        items = json.loads(n.items)
                        for i, item in enumerate(items):
                            mark = "x" if item.get("done") else " "
                            lines.append(f"  [{mark}] {i}: {item.get('text', '')}")
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif n.content:
                    snippet = n.content[:80].replace("\n", " ")
                    lines.append(f"  {snippet}")
            return {"results": "\n".join(lines)}

        elif action == "add":
            # Accept the various field names models emit: `text` is the most
            # common stand-in for "title or body content" when the model
            # treats the note as a single string. If text was supplied and
            # neither title nor content, use it as the title.
            title = (args.get("title") or "").strip()
            content_raw = args.get("content")
            text_raw = args.get("text") or args.get("body")
            if not title and not content_raw and text_raw:
                title = text_raw.strip()
            elif not content_raw and text_raw:
                content_raw = text_raw
            items_raw = args.get("items")
            items_json = json.dumps(items_raw) if items_raw is not None else None
            note_type = args.get("note_type", "checklist" if items_raw else "note")
            # Accept natural-language due_date ("tomorrow at 1pm") in
            # addition to ISO. Use the user-tz-aware parser so the LLM's
            # naive times ("today at 9pm") are anchored to the USER's clock,
            # not the server's. Returns ISO with explicit offset so frontend
            # `new Date()` resolves the right absolute moment regardless of
            # where the user is.
            due_raw = args.get("due_date")
            due_iso = None
            if due_raw:
                try:
                    from routes.calendar_routes import parse_due_for_user as _pdt_user
                    due_iso = _pdt_user(due_raw)
                except Exception:
                    due_iso = due_raw  # fall through; trust the model
            if due_iso and title:
                # Calendar event reminders are represented as Notes. If the
                # model creates a calendar event with reminder_minutes and then
                # also creates a separate note reminder for the same title/time,
                # keep the existing note so the user gets only one dispatch.
                existing_q = db.query(Note).filter(
                    Note.archived == False,  # noqa: E712
                    Note.due_date == due_iso,
                )
                if owner is not None:
                    existing_q = existing_q.filter(Note.owner == owner)
                target_title = _norm_note_title(title)
                for existing in existing_q.limit(25).all():
                    if _norm_note_title(existing.title or "") == target_title:
                        return {
                            "response": f"Reminder already exists: \"{existing.title or title}\" (id: {existing.id[:8]})",
                            "note_id": existing.id,
                            "duplicate": True,
                            "exit_code": 0,
                        }
            note = Note(
                id=str(_uuid.uuid4()),
                owner=owner,
                title=title,
                content=content_raw,
                items=items_json,
                note_type=note_type,
                color=args.get("color"),
                label=args.get("label"),
                pinned=args.get("pinned", False),
                due_date=due_iso,
                source="agent",
                session_id=args.get("session_id"),
            )
            db.add(note)
            db.commit()
            return {"response": f"Note created: \"{title or '(untitled)'}\" (id: {note.id[:8]})", "exit_code": 0}

        elif action == "update":
            note_id = args.get("id", "")
            note = db.query(Note).filter(Note.id.startswith(note_id)).first() if note_id else None
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if owner is not None and note.owner and note.owner != owner:
                return {"error": "Note not found", "exit_code": 1}
            for field in ("title", "content", "note_type", "color", "label", "due_date"):
                if field in args and args[field] is not None:
                    setattr(note, field, args[field])
            if "items" in args and args["items"] is not None:
                note.items = json.dumps(args["items"])
                flag_modified(note, "items")
            if "pinned" in args:
                note.pinned = args["pinned"]
            if "archived" in args:
                note.archived = args["archived"]
            db.commit()
            return {"response": f"Note updated: \"{note.title or '(untitled)'}\"", "exit_code": 0}

        elif action == "delete":
            note_id = args.get("id", "")
            note = db.query(Note).filter(Note.id.startswith(note_id)).first() if note_id else None
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if owner is not None and note.owner and note.owner != owner:
                return {"error": "Note not found", "exit_code": 1}
            title = note.title
            db.delete(note)
            db.commit()
            return {"response": f"Deleted note: \"{title or '(untitled)'}\"", "exit_code": 0}

        elif action == "toggle_item":
            note_id = args.get("id", "")
            index = args.get("index", 0)
            note = db.query(Note).filter(Note.id.startswith(note_id)).first() if note_id else None
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if owner is not None and note.owner and note.owner != owner:
                return {"error": "Note not found", "exit_code": 1}
            if not note.items:
                return {"error": "Note has no checklist items", "exit_code": 1}
            items = json.loads(note.items)
            if index < 0 or index >= len(items):
                return {"error": f"Item index {index} out of range (0-{len(items)-1})", "exit_code": 1}
            items[index]["done"] = not items[index].get("done", False)
            note.items = json.dumps(items)
            flag_modified(note, "items")
            db.commit()
            mark = "done" if items[index]["done"] else "undone"
            return {"response": f"Item '{items[index].get('text', '')}' marked {mark}", "exit_code": 0}

        else:
            return {"error": f"Unknown action: {action}. Use list/add/update/delete/toggle_item", "exit_code": 1}
    except Exception as e:
        logger.error(f"manage_notes error: {e}")
        return {"error": str(e), "exit_code": 1}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Calendar tool — CalDAV-backed event CRUD
# ---------------------------------------------------------------------------

async def do_manage_calendar(content: str, owner: Optional[str] = None) -> Dict:
    """Handle manage_calendar tool calls: list/create/update/delete calendar events (local SQLite)."""
    from datetime import datetime, timedelta
    from core.database import SessionLocal, CalendarCal, CalendarEvent, Note
    from routes.calendar_routes import _ensure_default_calendar, _parse_dt, _parse_dt_pair, parse_due_for_user, _resolve_base_uid
    import uuid as _uuid

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    # Normalize action — some models emit hyphens ("list-calendars") instead
    # of underscores. Treat them as equivalent so we don't bounce a
    # cosmetic typo back to the model and waste a round-trip. Also accept
    # short forms (`create`, `update`, `delete`) as aliases for the
    # full `<verb>_event` names — models keep emitting the short forms.
    action = (args.get("action") or "list_events").replace("-", "_").strip().lower()
    _ACTION_ALIASES = {
        "create": "create_event",
        "update": "update_event",
        "delete": "delete_event",
        "list": "list_events",
    }
    action = _ACTION_ALIASES.get(action, action)
    db = SessionLocal()

    def _calendar_query():
        q = db.query(CalendarCal)
        if owner is not None:
            q = q.filter(CalendarCal.owner == owner)
        return q

    def _event_query():
        q = db.query(CalendarEvent).join(CalendarCal)
        if owner is not None:
            q = q.filter(CalendarCal.owner == owner)
        return q

    def _reminder_minutes(raw_args) -> Optional[int]:
        raw = (
            raw_args.get("reminder_minutes")
            or raw_args.get("remind_before_minutes")
            or raw_args.get("alarm_minutes")
            or raw_args.get("reminder")
            or raw_args.get("alarm")
        )
        if raw in (None, ""):
            desc = str(raw_args.get("description") or "")
            if re.search(r"\b(remind|reminder|alarm)\b", desc, re.I):
                raw = desc
        if raw in (None, "", False):
            return None
        if raw is True:
            return 10
        if isinstance(raw, (int, float)):
            return max(0, int(raw))
        text = str(raw).strip().lower()
        if text in {"none", "no", "off", "false"}:
            return None
        m = re.search(r"(\d+)\s*(?:m|min|minute|minutes)\b", text)
        if m:
            return max(0, int(m.group(1)))
        m = re.search(r"(\d+)\s*(?:h|hr|hour|hours)\b", text)
        if m:
            return max(0, int(m.group(1)) * 60)
        if text.isdigit():
            return max(0, int(text))
        return None

    def _event_description(raw_args, minutes_before: Optional[int]) -> str:
        desc = str(raw_args.get("description", "") or "")
        if minutes_before is None:
            return desc
        reminder_only = re.compile(
            r"^\s*(?:remind(?:er)?|alarm)\s*:?\s*\d+\s*"
            r"(?:m|min|minute|minutes|h|hr|hour|hours)\b.*$",
            re.I,
        )
        return "" if reminder_only.match(desc) else desc

    def _parse_event_dt(raw: str) -> tuple[datetime, bool]:
        """Parse agent event datetimes in the user's timezone when available."""
        return _parse_dt_pair(parse_due_for_user(raw))

    def _create_calendar_reminder(summary: str, location: str, dtstart: datetime,
                                  all_day: bool, minutes_before: int,
                                  is_utc: bool = False) -> tuple[Optional[str], Optional[str]]:
        remind_at = dtstart - timedelta(minutes=minutes_before)
        now = datetime.utcnow() if is_utc else datetime.now()
        if dtstart <= now:
            return None, "event already passed"
        if remind_at <= now:
            # If the requested "before" time already passed but the event is
            # still upcoming, create an immediate Note reminder instead of
            # silently dropping it.
            remind_at = now
        start_fmt = dtstart.strftime("%a %b %d") if all_day else dtstart.strftime("%a %b %d %H:%M")
        loc = f" @ {location}" if location else ""
        text = f"{summary}{loc} — {start_fmt}"
        due_date = remind_at.isoformat() + ("Z" if is_utc else "")
        expected_title = f"Reminder: {summary}"
        existing_q = db.query(Note).filter(
            Note.archived == False,  # noqa: E712
            Note.due_date == due_date,
        )
        if owner is not None:
            existing_q = existing_q.filter(Note.owner == owner)
        target_title = re.sub(r"^\s*reminder\s*:\s*", "", expected_title.strip().lower())
        for existing in existing_q.limit(25).all():
            existing_title = re.sub(r"^\s*reminder\s*:\s*", "", (existing.title or "").strip().lower())
            if existing_title == target_title:
                return existing.id, "duplicate reminder already exists"
        note = Note(
            id=str(_uuid.uuid4()),
            owner=owner,
            title=expected_title,
            items=json.dumps([{"text": text, "done": False, "checked": False}]),
            note_type="todo",
            label="calendar",
            due_date=due_date,
            source="calendar",
        )
        db.add(note)
        return note.id, None

    try:
        if action == "list_calendars":
            _ensure_default_calendar(db, owner)
            cals = _calendar_query().all()
            result = [{"name": c.name, "href": c.id} for c in cals]
            if result:
                lines = [f"Found {len(result)} calendar(s):"]
                for c in result:
                    lines.append(f"- {c['name']} ({c['href'][:8]})")
                response_text = "\n".join(lines)
            else:
                response_text = "No calendars found."
            return {"response": response_text, "calendars": result, "exit_code": 0}

        elif action == "list_events":
            try:
                if args.get("start"):
                    start_dt = _parse_dt(args["start"])
                else:
                    start_dt = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                if args.get("end"):
                    end_dt = _parse_dt(args["end"])
                else:
                    end_dt = start_dt + timedelta(days=14)
            except ValueError as e:
                return {"error": f"Invalid date format: {e}", "exit_code": 1}

            q = _event_query().filter(
                CalendarEvent.dtstart < end_dt,
                CalendarEvent.dtend > start_dt,
                CalendarEvent.status != "cancelled",
            )
            calendar_filter = args.get("calendar")
            if calendar_filter:
                q = q.filter(
                    (CalendarEvent.calendar_id == calendar_filter) |
                    (CalendarCal.name == calendar_filter)
                )
            rows = q.order_by(CalendarEvent.dtstart).all()
            events = []
            for ev in rows:
                if ev.all_day:
                    s, e = ev.dtstart.strftime("%Y-%m-%d"), ev.dtend.strftime("%Y-%m-%d")
                else:
                    suffix = "Z" if getattr(ev, "is_utc", False) else ""
                    s, e = ev.dtstart.isoformat() + suffix, ev.dtend.isoformat() + suffix
                events.append({
                    "uid": ev.uid, "summary": ev.summary or "", "dtstart": s, "dtend": e,
                    "all_day": ev.all_day, "description": ev.description or "",
                    "location": ev.location or "",
                    "calendar": ev.calendar.name if ev.calendar else "",
                    "calendar_href": ev.calendar_id,
                    "event_type": ev.event_type or "",
                    "importance": ev.importance or "normal",
                })
            if not events:
                response_text = f"No events between {start_dt.date().isoformat()} and {end_dt.date().isoformat()}."
            else:
                lines = [f"Found {len(events)} event(s) between {start_dt.date().isoformat()} and {end_dt.date().isoformat()}:"]
                for ev in events:
                    when = ev["dtstart"]
                    when_str = f"{when} (all day)" if ev.get("all_day") else f"{when} -> {ev.get('dtend', '')}"
                    # Clickable anchor — opens the calendar on the event's day.
                    line = f"- {when_str}: [{ev['summary']}](#event-{ev['uid']})"
                    if ev.get("event_type"):
                        line += f" #{ev['event_type']}"
                    if ev.get("importance") and ev["importance"] != "normal":
                        line += f" !{ev['importance']}"
                    if ev.get("location"):
                        line += f" @ {ev['location']}"
                    if ev.get("calendar"):
                        line += f" ({ev['calendar']})"
                    if ev.get("description"):
                        desc = ev["description"].strip().replace("\n", " ")
                        if len(desc) > 120:
                            desc = desc[:117] + "..."
                        line += f"\n    {desc}"
                    lines.append(line)
                response_text = "\n".join(lines)
            return {"response": response_text, "events": events, "exit_code": 0}

        elif action == "create_event":
            summary = args.get("summary")
            # Accept the various names models like to use for the start
            # field: dtstart (canonical), start, start_time, when.
            dtstart_str = (args.get("dtstart") or args.get("start")
                           or args.get("start_time") or args.get("when"))
            if not summary or not dtstart_str:
                return {"error": "summary and dtstart are required", "exit_code": 1}

            # Accept either an href OR a calendar name/short-id like "Main"
            # or "62e545d8" — saves the model from having to memorize hrefs
            # after a `list_calendars` call returned short prefixes.
            cal_href = args.get("calendar_href") or args.get("calendar")
            cal = None
            if cal_href:
                cal = (_calendar_query()
                       .filter(CalendarCal.id == cal_href)
                       .first())
                if not cal:
                    # Try by name (case-insensitive) or by short-id prefix
                    cal = (_calendar_query()
                           .filter(CalendarCal.name.ilike(cal_href))
                           .first())
                if not cal:
                    cal = (_calendar_query()
                           .filter(CalendarCal.id.like(f"{cal_href}%"))
                           .first())
            if not cal:
                cal = _ensure_default_calendar(db, owner)

            all_day = bool(args.get("all_day", False))
            try:
                dtstart, dtstart_is_utc = _parse_event_dt(dtstart_str)
            except ValueError as e:
                return {"error": f"Could not parse dtstart {dtstart_str!r}: {e}", "exit_code": 1}
            dtend_raw = args.get("dtend") or args.get("end") or args.get("end_time")
            if dtend_raw:
                try:
                    dtend, dtend_is_utc = _parse_event_dt(dtend_raw)
                    dtstart_is_utc = dtstart_is_utc or dtend_is_utc
                except ValueError as e:
                    return {"error": f"Could not parse dtend {dtend_raw!r}: {e}", "exit_code": 1}
            else:
                # Support duration: "1h", "30m", "90min", "1hr30m"
                dur = (args.get("duration") or "").strip().lower()
                delta = None
                if dur:
                    import re as _re_d
                    h = _re_d.search(r'(\d+)\s*(?:h|hr|hours?)', dur)
                    m = _re_d.search(r'(\d+)\s*(?:m|min|minutes?)', dur)
                    secs = (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0)
                    if secs > 0:
                        delta = timedelta(seconds=secs)
                if delta is not None:
                    dtend = dtstart + delta
                elif all_day:
                    dtend = dtstart + timedelta(days=1)
                else:
                    dtend = dtstart + timedelta(hours=1)

            # Dedup: if a non-cancelled event with the same title + start time already
            # exists, return its UID instead of creating a fresh copy. Prevents the
            # email triage from multiplying events when several emails reference the
            # same meeting. Compare case-insensitively since LLM-extracted titles
            # can vary in capitalisation.
            from sqlalchemy import func as _func
            existing = (
                _event_query()
                .filter(
                    CalendarEvent.dtstart == dtstart,
                    CalendarEvent.status != "cancelled",
                    _func.lower(CalendarEvent.summary) == summary.lower(),
                )
                .first()
            )
            if existing is not None:
                reminder_note_id = None
                reminder_skipped_reason = None
                minutes_before = _reminder_minutes(args)
                if minutes_before is not None:
                    reminder_note_id, reminder_skipped_reason = _create_calendar_reminder(
                        existing.summary or summary,
                        existing.location or "",
                        existing.dtstart,
                        existing.all_day,
                        minutes_before,
                        bool(existing.is_utc),
                    )
                    if reminder_note_id:
                        db.commit()
                reminder_text = ""
                if minutes_before is not None:
                    reminder_text = (
                        f"; reminder set {minutes_before} min before"
                        if reminder_note_id
                        else f"; reminder not set ({reminder_skipped_reason or 'reminder time already passed'})"
                    )
                return {
                    "response": (
                        f"Event already exists: '{summary}' on {dtstart_str}"
                        + reminder_text
                    ),
                    "uid": existing.uid,
                    "reminder_note_id": reminder_note_id,
                    "reminder_skipped_reason": reminder_skipped_reason,
                    "duplicate": True,
                    "exit_code": 0,
                }

            # Optional tag/category and importance — friendly aliases.
            event_type = (args.get("event_type") or args.get("tag")
                          or args.get("category") or args.get("type") or "") or None
            importance = args.get("importance") or "normal"
            minutes_before = _reminder_minutes(args)

            uid = str(_uuid.uuid4())
            ev = CalendarEvent(
                uid=uid, calendar_id=cal.id, summary=summary,
                description=_event_description(args, minutes_before),
                location=args.get("location", "") or "",
                dtstart=dtstart, dtend=dtend, all_day=all_day,
                is_utc=dtstart_is_utc and not all_day,
                rrule=args.get("rrule", "") or "",
                event_type=event_type,
                importance=importance,
            )
            db.add(ev)
            reminder_note_id = None
            reminder_skipped_reason = None
            if minutes_before is not None:
                reminder_note_id, reminder_skipped_reason = _create_calendar_reminder(
                    summary,
                    args.get("location", "") or "",
                    dtstart,
                    all_day,
                    minutes_before,
                    dtstart_is_utc and not all_day,
                )
            db.commit()
            tag_blurb = f" [{event_type}]" if event_type else ""
            if minutes_before is None:
                reminder_blurb = ""
            elif reminder_note_id:
                reminder_blurb = f" with reminder {minutes_before} min before"
            else:
                reminder_blurb = f" without reminder ({reminder_skipped_reason or 'reminder time already passed'})"
            # Return a clickable anchor so the agent can surface a link
            # that opens the calendar on that day. See the markdown
            # anchor convention ([Name](#event-<uid>)).
            return {
                "response": f"Created event [{summary}](#event-{uid}){tag_blurb} on {dtstart_str}{reminder_blurb}",
                "uid": uid,
                "anchor": f"[{summary}](#event-{uid})",
                "reminder_note_id": reminder_note_id,
                "reminder_skipped_reason": reminder_skipped_reason,
                "exit_code": 0,
            }

        elif action == "update_event":
            uid = args.get("uid")
            if not uid:
                return {"error": "uid is required", "exit_code": 1}
            try:
                base_uid = _resolve_base_uid(uid)
            except ValueError as e:
                return {"error": str(e), "exit_code": 1}
            ev = _event_query().filter(CalendarEvent.uid == base_uid).first()
            if not ev:
                return {"error": f"Event {uid} not found", "exit_code": 1}
            if args.get("summary") is not None:
                ev.summary = args["summary"]
            if args.get("description") is not None:
                ev.description = args["description"]
            if args.get("location") is not None:
                ev.location = args["location"]
            if args.get("dtstart") is not None:
                ev.dtstart = _parse_dt(args["dtstart"])
            if args.get("dtend") is not None:
                ev.dtend = _parse_dt(args["dtend"])
            if args.get("all_day") is not None:
                ev.all_day = args["all_day"]
            # Tag/category + importance updates (any of these aliases).
            _tag = (args.get("event_type") or args.get("tag")
                    or args.get("category") or args.get("type"))
            if _tag is not None:
                ev.event_type = _tag or None
            if args.get("importance") is not None:
                ev.importance = args["importance"]
            db.commit()
            return {"response": f"Updated event {uid}", "exit_code": 0}

        elif action == "delete_event":
            uid = args.get("uid")
            if not uid:
                return {"error": "uid is required", "exit_code": 1}
            try:
                base_uid = _resolve_base_uid(uid)
            except ValueError as e:
                return {"error": str(e), "exit_code": 1}
            ev = _event_query().filter(CalendarEvent.uid == base_uid).first()
            if not ev:
                return {"error": f"Event {uid} not found", "exit_code": 1}
            db.delete(ev)
            db.commit()
            return {"response": f"Deleted event {uid}", "exit_code": 0}

        else:
            return {
                "error": f"Unknown action: {action}. Use list_events, create_event, update_event, delete_event, list_calendars",
                "exit_code": 1,
            }

    except Exception as e:
        db.rollback()
        logger.error(f"manage_calendar error: {e}")
        return {"error": str(e), "exit_code": 1}
    finally:
        db.close()
