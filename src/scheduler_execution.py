"""Task-execution mixin for the task scheduler (ADR-060, Phase 2.2).

The execution cluster - _execute_task / _execute_task_locked / _execute_action /
_execute_checkin / _execute_llm_task + their helpers (_task_needs_model_slot,
_log_to_assistant, _format_email_output) - split verbatim out of the TaskScheduler
god-class into a mixin (method bodies byte-identical, 4-space indent preserved; self.*
resolves via the MRO, so self-calls into the methods that stay - ensure_defaults,
_set_run_progress, add_notification, _deliver_task_result - keep working). TaskScheduler
inherits this mixin, so all callers + tests are unchanged. Cross-module imports stay
LAZY in-method (core.database, agent_tools, builtin_actions, llm_core, etc.); this module
references no task_scheduler module-level name, so there is no import cycle.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

from src.scheduler_helpers import _cached, compute_next_run, _resolve_task_timezone

logger = logging.getLogger(__name__)


class _ExecutionMixin:
    async def _execute_task(self, task_id: str, *, bypass_model_slot: bool = False, release_executing: bool = True):
        # Create the run record with status="queued" BEFORE waiting on the
        # semaphore so the UI can show that a manually-triggered task is in
        # line behind another. Once we acquire the slot, flip to "running"
        # and hand off to _execute_task_locked.
        from core.database import SessionLocal, TaskRun
        current = asyncio.current_task()
        if current:
            self._task_handles[task_id] = current
        run_id = str(uuid.uuid4())
        _q_db = SessionLocal()
        try:
            run = TaskRun(
                id=run_id,
                task_id=task_id,
                started_at=datetime.utcnow(),
                status="queued",
                result="Queued — waiting for a free slot…",
            )
            _q_db.add(run)
            _q_db.commit()
        except Exception:
            logger.exception(f"Failed to create queued run row for task {task_id}")
        finally:
            _q_db.close()

        if bypass_model_slot or not self._task_needs_model_slot(task_id):
            await self._execute_task_locked(task_id, run_id, release_executing=release_executing)
            return

        async with self._run_semaphore:
            await self._execute_task_locked(task_id, run_id, release_executing=release_executing)

    async def _execute_task_locked(self, task_id: str, run_id: str, *, release_executing: bool = True):
        from core.database import SessionLocal, ScheduledTask, TaskRun

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task or task.status != "active":
                # Task was paused/deleted while queued — record that outcome
                # so the run row doesn't sit as "queued" forever.
                stale = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if stale and stale.status == "queued":
                    stale.status = "skipped"
                    stale.finished_at = datetime.utcnow()
                    stale.error = f"Task no longer active (status={task.status if task else 'deleted'})"
                    db.commit()
                return

            # Flip the run from queued → running. Reset started_at to the
            # actual execution start so queue wait time is visible from
            # created_at vs started_at if we ever surface that.
            run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
            if run:
                run.status = "running"
                run.started_at = datetime.utcnow()
                run.result = "Starting…"
                db.commit()
            else:
                # Defensive: row may have been wiped; recreate so the rest of
                # the code can look it up by run_id without crashing.
                run = TaskRun(
                    id=run_id,
                    task_id=task.id,
                    started_at=datetime.utcnow(),
                    status="running",
                    result="Starting…",
                )
                db.add(run)
                db.commit()

            task_type = task.task_type or "llm"

            from src.builtin_actions import TaskDeferred, TaskNoop

            # Cleared each run so an action task (no model) doesn't inherit a
            # previous llm/research run's model. The executors set it once the
            # model is resolved.
            self._last_run_model = None
            try:
                if task_type == "action":
                    result, success = await self._execute_action(task, run_id=run_id)
                    run.status = "success" if success else "error"
                    run.result = result
                    if not success:
                        run.error = result
                elif task_type == "research":
                    result = await self._execute_research_task(task, db)
                    run.status = "success"
                    run.result = result
                else:
                    # LLM task — use agent loop for tool access
                    result = await self._execute_llm_task(task, db)
                    run.status = "success"
                    run.result = result
                # Record which model actually ran (resolved inside the executor).
                if getattr(self, "_last_run_model", None):
                    run.model = self._last_run_model
                if run.status == "success":
                    await self._deliver_task_result(task, result, db, model=getattr(self, "_last_run_model", None))
            except TaskDeferred as defer:
                count = self._task_defer_counts.get(task_id, 0) + 1
                self._task_defer_counts[task_id] = count
                delay_seconds = int(getattr(defer, "delay_seconds", 20 * 60) or (20 * 60))
                if count > 2:
                    delay_seconds = max(delay_seconds, 40 * 60)
                when = datetime.utcnow() + timedelta(seconds=delay_seconds)
                logger.info(
                    "Task '%s' deferred for %ss after %s quiet-window hit(s): %s",
                    task.name, delay_seconds, count, defer,
                )
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj:
                    db.delete(run_obj)
                task.next_run = when
                db.commit()
                return
            except asyncio.CancelledError:
                logger.info("Task '%s' stopped by user", task.name)
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj:
                    run_obj.status = "aborted"
                    run_obj.error = "Stopped by user"
                    run_obj.result = run_obj.result or "Stopped by user"
                    run_obj.finished_at = datetime.utcnow()
                task.last_run = datetime.utcnow()
                if (task.trigger_type or "schedule") == "schedule":
                    task.next_run = compute_next_run(
                        task.schedule, task.scheduled_time,
                        task.scheduled_day, task.scheduled_date,
                        after=datetime.utcnow(),
                        cron_expression=task.cron_expression,
                        tz_name=_resolve_task_timezone(db, task),
                    )
                else:
                    task.next_run = None
                db.commit()
                return
            except TaskNoop as noop:
                # Action reported "nothing to do". Mark the run as `skipped`
                # with the reason in `result` so it surfaces in Activity as a
                # slim "skipped — <reason>" row instead of vanishing silently.
                # (Previous behavior was `db.delete(run)`, which made the user
                # think queued tasks had been dropped on the floor.)
                logger.info(f"Task '{task.name}' no-op: {noop}")
                run.status = "skipped"
                run.result = str(noop)
                run.finished_at = datetime.utcnow()
                task.last_run = datetime.utcnow()
                if (task.trigger_type or "schedule") == "schedule":
                    task.next_run = compute_next_run(
                        task.schedule, task.scheduled_time,
                        task.scheduled_day, task.scheduled_date,
                        after=datetime.utcnow(),
                        cron_expression=task.cron_expression,
                        tz_name=_resolve_task_timezone(db, task),
                    )
                else:
                    task.next_run = None
                db.commit()
                return

            run.finished_at = datetime.utcnow()

            # Update task
            task.last_run = datetime.utcnow()
            task.run_count = (task.run_count or 0) + 1
            self._task_defer_counts.pop(task_id, None)

            # Compute next run only for schedule-triggered tasks
            if (task.trigger_type or "schedule") == "schedule":
                task.next_run = compute_next_run(
                    task.schedule, task.scheduled_time,
                    task.scheduled_day, task.scheduled_date,
                    after=datetime.utcnow(),
                    cron_expression=task.cron_expression,
                    tz_name=_resolve_task_timezone(db, task),
                )
                if task.next_run is None and task.schedule == "once":
                    task.status = "completed"
            else:
                task.next_run = None

            db.commit()
            logger.info(f"Task '{task.name}' completed (run {run_id})")
            output = task.output_target or "session"
            # Per-task notification gate. Default True (notifications_enabled
            # defaults to True at column level), but skip when the user has
            # explicitly turned them off for this task — quiets chatty
            # housekeeping cron tasks without disabling them entirely.
            should_notify = (
                (task.task_type or "llm") in {"llm", "research"}
                and getattr(task, "notifications_enabled", True)
            )
            if should_notify:
                self.add_notification(
                    task.name,
                    run.status,
                    task_id,
                    owner=task.owner,
                    body=run.result if output == "notification" else None,
                )

            # Log result to the assistant chat so all task activity is visible.
            # Skip skipped/error rows — user shouldn't see "skipped: …" noise
            # for cron tasks that no-op'd, or duplicate error spam for tasks
            # that already fired an error notification above.
            if run.status == "success":
                self._log_to_assistant(db, task, run.result or "[success]")

            # Task chaining — trigger the next task on success
            if run.status == "success" and task.then_task_id:
                chain_id = task.then_task_id
                if not self._has_chain_cycle(db, chain_id):
                    logger.info(f"Chaining: '{task.name}' → task {chain_id}")
                    asyncio.create_task(self._run_chained(chain_id))
                else:
                    logger.warning(f"Skipping chain from '{task.name}': cycle detected")

        except Exception as exec_exc:
            logger.exception(f"Task {task_id} execution error")
            # Fetch the task's owner so the error notification reaches
            # the same user the success notification would have.
            _owner = None
            try:
                _t = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                _owner = _t.owner if _t else None
            except Exception:
                pass
            _should_notify_error = False
            try:
                _t_for_notify = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                _should_notify_error = (
                    bool(_t_for_notify)
                    and (_t_for_notify.task_type or "llm") in {"llm", "research"}
                    and getattr(_t_for_notify, "notifications_enabled", True)
                )
            except Exception:
                _should_notify_error = False
            if _should_notify_error:
                self.add_notification(f"Task {task_id}", "error", task_id, owner=_owner)
            try:
                # Persist the actual exception message so the UI can show it
                err_text = f"{type(exec_exc).__name__}: {exec_exc}"
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj and run_obj.status in ("running", "success"):
                    run_obj.status = "error"
                    run_obj.error = err_text[:2000]
                    run_obj.finished_at = datetime.utcnow()
                # Advance next_run even on failure so a broken task doesn't
                # busy-loop the scheduler every tick with a stale past date.
                task_obj = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                if task_obj and (task_obj.trigger_type or "schedule") == "schedule":
                    task_obj.last_run = datetime.utcnow()
                    try:
                        task_obj.next_run = compute_next_run(
                            task_obj.schedule, task_obj.scheduled_time,
                            task_obj.scheduled_day, task_obj.scheduled_date,
                            after=datetime.utcnow(),
                            cron_expression=task_obj.cron_expression,
                            tz_name=_resolve_task_timezone(db, task_obj),
                        )
                    except Exception:
                        pass
                try:
                    db.commit()
                except Exception as commit_err:
                    # Commit failed — without a fallback the run row stays
                    # "running" forever AND next_run stays in the past, so the
                    # scheduler busy-loops dispatching the same task every tick
                    # until restart. Force the recovery in a fresh session.
                    logger.warning("Task %s error-path commit failed: %s — falling back", task_id, commit_err)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    from datetime import timedelta as _td
                    _recover_db = SessionLocal()
                    try:
                        _r = _recover_db.query(TaskRun).filter(TaskRun.id == run_id).first()
                        if _r and _r.status in ("running", "queued"):
                            _r.status = "aborted"
                            _r.error = f"commit_failed: {type(commit_err).__name__}: {commit_err}"[:2000]
                            _r.finished_at = datetime.utcnow()
                        _t = _recover_db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                        if _t and (_t.trigger_type or "schedule") == "schedule":
                            # Push next_run forward 5min as a safe stall so the
                            # scheduler doesn't immediately re-dispatch.
                            _t.next_run = datetime.utcnow() + _td(minutes=5)
                            _t.last_run = datetime.utcnow()
                        _recover_db.commit()
                    except Exception as recover_err:
                        logger.error("Task %s recovery commit ALSO failed: %s", task_id, recover_err)
                    finally:
                        _recover_db.close()
            except Exception:
                logger.exception("Task %s error-path failed unexpectedly", task_id)
        finally:
            db.close()
            handle = self._task_handles.get(task_id)
            if handle is asyncio.current_task():
                self._task_handles.pop(task_id, None)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)



    # Built-in housekeeping actions whose output is pure infra (no user-facing
    # content) — don't pollute the assistant chat session with their summaries.
    # Activity log + reminder email already carry everything the user needs.
    _SILENT_ACTIONS = frozenset({
        "check_email_urgency",
        "mark_email_boundaries",
        "learn_sender_signatures",
        "summarize_emails",
        "draft_email_replies",
        "extract_email_events",
        "classify_events",
        "tidy_sessions",
        "tidy_documents",
        "consolidate_memory",
        "tidy_research",
        "test_skills",
        "audit_skills",
    })

    _MODEL_BACKED_ACTIONS = frozenset({
        "summarize_emails",
        "draft_email_replies",
        "extract_email_events",
        "classify_events",
        "mark_email_boundaries",
        "learn_sender_signatures",
        "check_email_urgency",
        "test_skills",
        "audit_skills",
        "consolidate_memory",
    })

    def _task_needs_model_slot(self, task_id: str) -> bool:
        """Only LLM/research/model-backed actions should wait in the model
        queue. Pure housekeeping actions can run immediately."""
        from core.database import SessionLocal, ScheduledTask

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task:
                return True
            task_type = task.task_type or "llm"
            if task_type != "action":
                return True
            return (task.action or "") in self._MODEL_BACKED_ACTIONS
        finally:
            db.close()

    def _log_to_assistant(self, db, task, result_text: str):
        """Log a task result to the assistant's chat session."""
        # Don't double-log check-ins (they already save directly)
        if "check-in" in (task.name or "").lower():
            return
        # Built-in housekeeping noise stays out of the chat.
        if (task.action or "") in self._SILENT_ACTIONS:
            return
        from src.assistant_log import log_to_assistant
        log_to_assistant(
            task.owner,
            result_text[:1000],
            category=(task.name or "Task"),
        )

    async def _execute_action(self, task, run_id: str | None = None) -> tuple:
        """Execute a built-in action (no LLM needed)."""
        from src.builtin_actions import BUILTIN_ACTIONS

        action_fn = BUILTIN_ACTIONS.get(task.action)
        if not action_fn:
            return f"Unknown action: {task.action}", False

        from src.builtin_actions import TaskNoop
        try:
            # Pass task prompt as script/command for ssh_command/run_script actions.
            def _progress(message: str):
                self._set_run_progress(run_id, message)

            kwargs = {"owner": task.owner, "task_name": task.name, "progress_cb": _progress}
            if task.action in ("run_script", "run_local", "ssh_command") and task.prompt:
                kwargs["script" if task.action in ("run_script", "run_local") else "command"] = task.prompt
            result, success = await action_fn(**kwargs)
            return result, success
        except TaskNoop:
            # Bubble up so _execute_task_locked can drop the run row silently.
            raise
        except Exception as e:
            logger.error(f"Action '{task.action}' failed: {e}")
            return str(e), False

    # ── Check-in source discovery ──
    # Pattern-based: if an MCP server has a tool matching a pattern, it becomes
    # a check-in source. Add new patterns here to support new integrations —
    # no code changes needed elsewhere.
    CHECKIN_MCP_PATTERNS = [
        {"detect": "list_emails",   "section": "Email",    "tool": "list_emails",
         "args": {"mailbox": "INBOX", "limit": 10, "unread_only": True},
         "label_from_identity": True,
         "formatter": "_format_email_output"},
        {"detect": "search_emails", "section": "Email",    "tool": "search_emails",
         "args": {"query": "is:unread", "limit": 10},
         "label_from_identity": True,
         "formatter": "_format_email_output"},
        {"detect": "get_feed",      "section": "RSS",      "tool": "get_feed",
         "args": {},
         "label_from_identity": False},
        {"detect": "list_feeds",    "section": "RSS",      "tool": "list_feeds",
         "args": {},
         "label_from_identity": False},
        {"detect": "list_messages", "section": "Messages", "tool": "list_messages",
         "args": {"limit": 10},
         "label_from_identity": True},
    ]

    @staticmethod
    def _format_email_output(raw: str) -> str:
        """Clean up raw MCP email list output into readable format."""
        import re as _re
        lines = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Skip header lines like "📬 [INBOX] 856 emails..."
            if line.startswith(("\U0001f4ec", "📬", "No emails", "---", "Page ")):
                continue
            # Skip "more pages available" etc
            if "page" in line.lower() and "/" in line:
                continue
            # Parse: [1778] Re: Subject From: Name | Date
            m = _re.match(r'\[?\d+\]?\s*(?:↩️\s*|📎\s*|🔵\s*|⭐\s*)?(.+?)(?:\s*From:\s*(.+?))?(?:\s*\|\s*(\S+))?$', line)
            if m:
                subject = m.group(1).strip().rstrip('|').strip()
                sender = (m.group(2) or "").strip().rstrip('|').strip()
                if sender:
                    lines.append(f"- {sender} — {subject}")
                else:
                    lines.append(f"- {subject}")
            elif line.startswith("[") or line.startswith("-"):
                # Generic cleanup
                cleaned = _re.sub(r'^\[?\d+\]?\s*(?:↩️\s*|📎\s*)?', '', line.lstrip('- '))
                if cleaned.strip():
                    lines.append(f"- {cleaned.strip()}")
        if not lines:
            return "No unread emails"
        return "\n".join(lines[:10])

    async def _execute_checkin(self, task, crew, db, session_id: str,
                               endpoint_url: str, model: str) -> str:
        """Gather raw data from all integrations, hand it to the LLM to write the check-in."""
        from src.tool_implementations import do_manage_notes
        from src.agent_tools import get_mcp_manager

        tz_name = _resolve_task_timezone(db, task)
        try:
            if tz_name:
                from zoneinfo import ZoneInfo
                from datetime import timezone, timedelta
                now = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
            else:
                from datetime import timedelta
                now = datetime.utcnow()
            time_str = now.strftime("%A, %B %d %Y, %H:%M")
        except Exception:
            from datetime import timedelta
            now = datetime.utcnow()
            time_str = now.strftime("%H:%M UTC")

        raw = {}

        # Calendar: today+tomorrow, this week, month ahead
        # Pull directly from DB so we can include event_type and importance.
        try:
            from core.database import SessionLocal as _SL, CalendarEvent as _CE
            _db = _SL()
            try:
                for label, start, end in [
                    ("today_tomorrow", now, now + timedelta(days=2)),
                    ("this_week",      now + timedelta(days=2), now + timedelta(days=7)),
                    ("next_30_days",   now + timedelta(days=8), now + timedelta(days=30)),
                ]:
                    # Strip timezone for naive DB comparison
                    _s = start.replace(tzinfo=None) if start.tzinfo else start
                    _e = end.replace(tzinfo=None) if end.tzinfo else end
                    evs = _db.query(_CE).filter(
                        _CE.dtstart >= _s,
                        _CE.dtstart <= _e,
                        _CE.status != "cancelled",
                    ).order_by(_CE.dtstart).all()
                    if not evs:
                        continue
                    # Group by importance for richer output
                    by_imp = {"critical": [], "high": [], "normal": [], "low": []}
                    for ev in evs:
                        imp = (ev.importance or "normal").lower()
                        by_imp.setdefault(imp, []).append(ev)
                    lines = []
                    for tier in ("critical", "high", "normal", "low"):
                        items = by_imp.get(tier, [])
                        if not items:
                            continue
                        marker = {"critical": "[!!]", "high": "[!]", "normal": "  ", "low": " ·"}[tier]
                        for ev in items:
                            t = ev.dtstart.strftime("%a %b %d %H:%M")
                            tag = f" ({ev.event_type})" if ev.event_type else ""
                            loc = f" @ {ev.location}" if ev.location else ""
                            lines.append(f"{marker} {t} — {ev.summary}{tag}{loc}")
                    if lines:
                        raw[f"calendar_{label}"] = "\n".join(lines)
            finally:
                _db.close()
        except Exception as e:
            raw["calendar"] = f"Error: {e}"

        # Notes/Tasks
        try:
            r = await do_manage_notes(json.dumps({"action": "list"}), owner=task.owner)
            raw["notes_tasks"] = r.get("results") or r.get("response") or "No notes"
        except Exception as e:
            raw["notes_tasks"] = f"Error: {e}"

        # Auto-discover API integrations (Miniflux RSS, etc.) from integrations.json
        try:
            import httpx
            from pathlib import Path as _P
            integrations_file = _P("data/integrations.json")
            if integrations_file.exists():
                integrations = json.loads(integrations_file.read_text(encoding="utf-8"))
                for integ in integrations:
                    if not integ.get("enabled"):
                        continue
                    preset = integ.get("preset", "")
                    base_url = integ.get("base_url", "").rstrip("/")
                    api_key = integ.get("api_key", "")
                    if not base_url:
                        continue

                    # Build auth headers
                    headers = {}
                    if integ.get("auth_type") == "header" and api_key:
                        headers[integ.get("auth_header", "X-Auth-Token")] = api_key
                    elif integ.get("auth_type") == "bearer" and api_key:
                        headers["Authorization"] = f"Bearer {api_key}"

                    # Miniflux: fetch unread entries (cached 3 min across tasks)
                    if preset == "miniflux":
                        async def _fetch_miniflux(_base=base_url, _headers=dict(headers)):
                            async with httpx.AsyncClient(timeout=10) as client:
                                resp = await client.get(
                                    f"{_base}/v1/entries",
                                    params={"status": "unread", "limit": 15, "order": "published_at", "direction": "desc"},
                                    headers=_headers,
                                )
                                if resp.status_code != 200:
                                    return None
                                entries = resp.json().get("entries", []) or []
                                if not entries:
                                    return None
                                lines = []
                                for e in entries[:15]:
                                    title = e.get("title", "?")
                                    feed = (e.get("feed") or {}).get("title", "?")
                                    url = e.get("url", "")
                                    lines.append(f"- [{feed}] {title} — {url}")
                                return "\n".join(lines)
                        try:
                            val = await _cached(("miniflux_unread", base_url), 180, _fetch_miniflux)
                            if val:
                                raw["rss_miniflux_unread"] = val
                        except Exception as e:
                            logger.warning(f"Miniflux fetch failed: {e}")
        except Exception as e:
            logger.warning(f"Integrations discovery failed: {e}")

        # Auto-discover MCP sources
        mcp = get_mcp_manager()
        if mcp:
            discovered = set()
            for server_id, tools in mcp._tools.items():
                if mcp.is_builtin(server_id):
                    continue
                conn = mcp._connections.get(server_id, {})
                if conn.get("status") != "connected":
                    continue
                identity = conn.get("identity", "")
                tool_names = {t["name"] for t in tools}
                for pattern in self.CHECKIN_MCP_PATTERNS:
                    if pattern["detect"] not in tool_names:
                        continue
                    key = f"{pattern['section']}_{server_id}"
                    if key in discovered:
                        continue
                    discovered.add(key)
                    label = f"{pattern['section']} ({identity})" if identity else pattern["section"]
                    qualified = f"mcp__{server_id}__{pattern['tool']}"
                    args = dict(pattern.get("args", {}))
                    args["account"] = "default"
                    try:
                        # Cache 3 min: different scheduled tasks firing at the
                        # same minute share the same MCP snapshot.
                        async def _call_mcp(_q=qualified, _args=args):
                            return await mcp.call_tool(_q, _args)
                        cache_key = ("mcp_snapshot", qualified, json.dumps(args, sort_keys=True))
                        result = await _cached(cache_key, 180, _call_mcp)
                        if result.get("exit_code", 0) != 0:
                            continue
                        content = result.get("stdout") or result.get("output") or ""
                        if content.strip():
                            raw[label] = content[:3000]
                    except Exception:
                        pass

        # Build the data dump and hand it to the LLM
        data_dump = f"Current time: {time_str}\n\n"
        for key, val in raw.items():
            data_dump += f"--- {key} ---\n{val}\n\n"

        context = (
            data_dump +
            f"---\n\n{task.prompt}\n\n"
            "Write the check-in. YOU decide what matters, what to skip, how to format. "
            "Only show future events. Calendar events are pre-tagged with importance: "
            "[!!] critical, [!] high, plain = normal, ' ·' = low. "
            "GROUP your output by importance — lead with critical/high, then normal, "
            "skip low entirely unless explicitly relevant. Mention event type (work/health/travel/etc) "
            "where it adds context (e.g. 'leave 1h early for travel'). "
            "Flag anything coming up that needs prep (birthdays, deadlines, holidays). "
            "Use tools to take action if needed. Keep it concise — no raw data dumps."
        )

        return await self._run_agent_loop(
            endpoint_url, model, task, session_id,
            system_prompt=(crew.personality or "").strip() if crew else None,
            disabled_tools=None, relevant_tools=None,
            override_user_message=context,
        )

    async def _execute_llm_task(self, task, db) -> str:
        """Execute an LLM task with full tool access via the agent loop."""
        from core.database import Session as DbSession, ChatMessage, CrewMember

        # If this task is wired to a CrewMember (personal assistant, custom
        # crew), prefer the crew member's persona/model/endpoint as overrides.
        crew = None
        if getattr(task, "crew_member_id", None):
            try:
                crew = db.query(CrewMember).filter(CrewMember.id == task.crew_member_id).first()
            except Exception:
                crew = None

        # Determine endpoint + model
        endpoint_url = task.endpoint_url
        model = task.model
        if (not endpoint_url or not model) and crew:
            endpoint_url = endpoint_url or crew.endpoint_url
            model = model or crew.model
        if not endpoint_url or not model:
            endpoint_url, model = self._resolve_defaults(db, task.owner)
        if not endpoint_url or not model:
            raise RuntimeError("No model/endpoint configured")
        # Record the resolved model so _execute_task_locked can persist it on
        # the run (tasks rarely pin a model, so this is the only record of
        # which model actually produced the output).
        self._last_run_model = model

        # Ensure a session exists for output
        session_id = task.session_id
        if not session_id:
            session_id = str(uuid.uuid4())
            sess = DbSession(
                id=session_id,
                name=f"[Task] {task.name}",
                endpoint_url=endpoint_url,
                model=model,
                owner=task.owner,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(sess)
            task.session_id = session_id
            db.commit()
            if self._session_manager:
                try:
                    self._session_manager.sessions[session_id] = self._session_manager._db_to_session(sess)
                except Exception:
                    pass

        # For assistant check-ins: call each tool directly and post results
        # as separate messages. More reliable than hoping the model calls tools.
        is_checkin = crew and crew.is_default_assistant and "check-in" in (task.name or "").lower()
        if is_checkin:
            return await self._execute_checkin(task, crew, db, session_id, endpoint_url, model)

        # Build system prompt: crew member persona overrides the default.
        system_prompt = (
            (crew.personality or "").strip()
            if crew and crew.personality
            else "You are a helpful assistant executing a scheduled task. Use available tools to complete the task thoroughly."
        )
        # Inject current time so the model knows what's past vs upcoming
        tz_name = _resolve_task_timezone(db, task)
        try:
            if tz_name:
                from zoneinfo import ZoneInfo
                from datetime import timezone
                now_local = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
                time_str = now_local.strftime("%A, %B %d %Y, %H:%M %Z")
            else:
                time_str = datetime.utcnow().strftime("%A, %B %d %Y, %H:%M UTC")
        except Exception:
            time_str = datetime.utcnow().strftime("%A, %B %d %Y, %H:%M UTC")
        system_prompt = f"Current time: {time_str}\n\n{system_prompt}"

        # Compute tool filter from CrewMember.enabled_tools if set
        disabled_tools = None
        if crew and crew.enabled_tools:
            try:
                enabled = json.loads(crew.enabled_tools)
                if isinstance(enabled, list) and enabled:
                    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
                    all_tools = set(BUILTIN_TOOL_DESCRIPTIONS.keys())
                    disabled_tools = all_tools - set(enabled)
            except Exception:
                pass

        # RAG-select relevant tools for this prompt + always-available assistant tools.
        # Without this, all 40+ tools get sent and models hit their tool limit.
        relevant_tools = None
        try:
            from src.tool_index import get_tool_index, ASSISTANT_ALWAYS_AVAILABLE
            tool_idx = get_tool_index()
            if tool_idx:
                rag_tools = tool_idx.get_tools_for_query(task.prompt or "", k=8)
                relevant_tools = (rag_tools | ASSISTANT_ALWAYS_AVAILABLE)
                if disabled_tools:
                    relevant_tools -= disabled_tools
                logger.info(f"[assistant] RAG selected {len(rag_tools)} tools + {len(ASSISTANT_ALWAYS_AVAILABLE)} always-available = {len(relevant_tools)} total for '{task.name}'")
        except Exception as e:
            logger.warning(f"[assistant] RAG tool selection failed, using all: {e}")

        # Try using the agent loop for full tool access
        try:
            result = await self._run_agent_loop(
                endpoint_url, model, task, session_id,
                system_prompt=system_prompt, disabled_tools=disabled_tools,
                relevant_tools=relevant_tools,
            )
        except Exception as e:
            logger.warning(f"Agent loop failed for task '{task.name}', falling back to simple call: {e}")
            from src.llm_core import llm_call_async
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task.prompt},
            ]
            result = await llm_call_async(url=endpoint_url, model=model, messages=messages, timeout=120)

        # Strip the model's chain-of-thought before saving/delivering. Task
        # output is LLM-only, so prose=True (which also removes untagged
        # "The user wants me to…" reasoning) is safe here — without this the
        # thinking leaked into the saved result.
        try:
            from src.text_helpers import strip_think
            result = strip_think(result or "", prose=True, prompt_echo=True).strip() or result
        except Exception:
            pass

        return result

