"""Notification mixin for the task scheduler (ADR-057, Phase 2.2).

The add_notification / pop_notifications methods, split verbatim out of the
TaskScheduler god-class into a mixin (the first god-class -> mixin slice; method
bodies are byte-identical, self.* resolves via the MRO). TaskScheduler inherits
this mixin, so callers + the auth-regression owner-filter test are unchanged.
pop_notifications enforces the per-owner notification scope (security-relevant -
see tests/test_auth_regressions.py::test_pop_notifications_owner_filtered).
"""

from datetime import datetime


class _NotificationMixin:
    def add_notification(self, task_name: str, status: str, task_id: str = None, owner: str = None, body: str = None):
        """Store a notification about a completed task run. Tagged with the
        task's owner so `pop_notifications` can return only that user's
        notifications and prevent cross-tenant drain. `body` is the result
        text — populated when output_target='notification' so the client can
        show a rich browser Notification, not just a toast."""
        self._pending_notifications.append({
            "task_name": task_name,
            "status": status,
            "task_id": task_id,
            "owner": owner,
            "body": (body[:500] + "…") if body and len(body) > 500 else body,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })
        # Cap at 50 to avoid unbounded growth
        if len(self._pending_notifications) > 50:
            self._pending_notifications = self._pending_notifications[-50:]

    def pop_notifications(self, owner: str = None) -> list:
        """Return and clear pending notifications.

        When `owner` is set, only matching notifications are returned (and
        cleared). Notifications stored before owner-tagging existed (or
        from owner-less tasks) are included when the caller is anonymous
        or when no owner filter is given — preserves backward behaviour
        for the legacy single-user deploy.
        """
        if owner is None:
            notes = self._pending_notifications[:]
            self._pending_notifications.clear()
            return notes
        # Strict owner scope — used to OR-in null-owner notifications for
        # "legacy single-user" compat but that leaked notification bodies to
        # any authenticated user once a second account existed.
        keep, take = [], []
        for n in self._pending_notifications:
            if n.get("owner") == owner:
                take.append(n)
            else:
                keep.append(n)
        self._pending_notifications = keep
        return take
