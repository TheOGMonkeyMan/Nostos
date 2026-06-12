"""Module-level helpers for the task scheduler (ADR-045, Phase 2.2).

The singleflight TTL cache (_cached + its shared state), the schedule -> next-run
computation (compute_next_run), and the per-task timezone resolver
(_resolve_task_timezone), split verbatim out of src/task_scheduler.py so the
oversized module shrinks toward its TaskScheduler class. Re-imported there;
compute_next_run is also part of the public API (routes/task_routes.py,
src/tools/management.py, routes/assistant_routes.py import it from
src.task_scheduler). The HOUSEKEEPING_DEFAULTS / RETIRED_HOUSEKEEPING_ACTIONS
constants intentionally stay in task_scheduler.py (a security regression test
pins their ship_paused defaults to that source file).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Tuple

logger = logging.getLogger(__name__)


# ── Shared TTL cache (singleflight) ────────────────────────────────────────
# Multiple scheduled tasks firing in the same minute often need the same
# external data (Miniflux unreads, MCP tool snapshots, etc.). This cache
# deduplicates those fetches — in-flight requests for the same key await the
# same underlying coroutine, and completed results are reused until TTL expiry.
_shared_cache: Dict[Tuple, Tuple[float, Any]] = {}
_shared_cache_pending: Dict[Tuple, asyncio.Future] = {}
_shared_cache_lock = asyncio.Lock()


async def _cached(key: Tuple, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """Return a cached result for `key` if fresh, else call `fetch()` and store.

    Concurrent callers for the same missing key share one `fetch()` call.
    Exceptions propagate to every waiter and do not poison the cache.
    """
    now = time.monotonic()
    async with _shared_cache_lock:
        entry = _shared_cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
        fut = _shared_cache_pending.get(key)
        if fut is not None:
            pending = fut
            owner = False
        else:
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            _shared_cache_pending[key] = fut
            pending = fut
            owner = True
    if not owner:
        return await pending
    try:
        val = await fetch()
        async with _shared_cache_lock:
            _shared_cache[key] = (time.monotonic() + ttl, val)
            _shared_cache_pending.pop(key, None)
        pending.set_result(val)
        return val
    except Exception as e:
        async with _shared_cache_lock:
            _shared_cache_pending.pop(key, None)
        pending.set_exception(e)
        raise


def compute_next_run(schedule: str, scheduled_time: str,
                     scheduled_day: int = None,
                     scheduled_date: datetime = None,
                     after: datetime = None,
                     cron_expression: str = None,
                     tz_name: str = None) -> datetime | None:
    """Compute the next run datetime (stored as naive UTC) based on schedule type.

    If `tz_name` is provided (IANA zone, e.g. "America/New_York"), `scheduled_time` /
    `scheduled_day` are interpreted as local wall-clock time in that zone and
    the result is converted to naive UTC for DB storage. If `tz_name` is None,
    the legacy behavior (`scheduled_time` interpreted as naive-UTC wall clock)
    is preserved so existing tasks don't shift.
    """
    from datetime import timezone
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None

    tz = None
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None

    # "now" used for comparisons. When tz is set we work entirely in local tz
    # and convert to UTC at the end. Otherwise we use naive UTC (legacy).
    if tz is not None:
        now_utc = after or datetime.utcnow()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now = now_utc.astimezone(tz)
    else:
        now = after or datetime.utcnow()

    def _to_utc_naive(dt: datetime) -> datetime:
        """Convert a tz-aware datetime to naive UTC for DB storage."""
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    if schedule == "cron" and cron_expression:
        try:
            from croniter import croniter
            cron = croniter(cron_expression, now)
            nxt = cron.get_next(datetime)
            if tz is not None and nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=tz)
            return _to_utc_naive(nxt) if tz is not None else nxt
        except Exception as e:
            logger.warning(f"Invalid cron expression '{cron_expression}': {e}")
            return None

    if schedule == "once":
        if scheduled_date and scheduled_date > (now.replace(tzinfo=None) if tz is not None else now):
            return scheduled_date
        return None

    if not scheduled_time:
        return None

    # Parse HH:MM
    parts = scheduled_time.split(":")
    hour, minute = int(parts[0]), int(parts[1])

    if schedule == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return _to_utc_naive(candidate) if tz is not None else candidate

    if schedule == "weekly":
        day = scheduled_day if scheduled_day is not None else 0  # 0=Monday
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = day - candidate.weekday()
        if days_ahead < 0 or (days_ahead == 0 and candidate <= now):
            days_ahead += 7
        candidate += timedelta(days=days_ahead)
        return _to_utc_naive(candidate) if tz is not None else candidate

    if schedule == "monthly":
        day = scheduled_day if scheduled_day is not None else 1
        try:
            candidate = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            candidate = now
        if candidate <= now:
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1)
            else:
                next_month = now.replace(month=now.month + 1, day=1)
            try:
                candidate = next_month.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                if next_month.month == 12:
                    last = next_month.replace(year=next_month.year + 1, month=1, day=1) - timedelta(days=1)
                else:
                    last = next_month.replace(month=next_month.month + 1, day=1) - timedelta(days=1)
                candidate = last.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return _to_utc_naive(candidate) if tz is not None else candidate

    return None


def _resolve_task_timezone(db, task) -> str | None:
    """Look up the IANA timezone name for a task via its linked CrewMember, if any."""
    if not getattr(task, "crew_member_id", None):
        return None
    try:
        from core.database import CrewMember
        cm = db.query(CrewMember).filter(CrewMember.id == task.crew_member_id).first()
        if cm and cm.timezone:
            return cm.timezone
    except Exception:
        pass
    return None
