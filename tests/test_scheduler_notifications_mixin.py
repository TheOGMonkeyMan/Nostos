"""Phase 2.2 (ADR-057): verify the TaskScheduler notification mixin split.

add_notification / pop_notifications moved verbatim out of the TaskScheduler
god-class into `class _NotificationMixin` (src/scheduler_notifications.py);
TaskScheduler inherits it. This is the first god-class -> mixin slice. The
owner-filter security behavior (tests/test_auth_regressions.py::
test_pop_notifications_owner_filtered) is the load-bearing check and still passes;
this file pins the mixin structure + the same behavior at the unit level.
"""

from src.task_scheduler import TaskScheduler
import src.scheduler_notifications as m


def test_taskscheduler_inherits_notification_mixin():
    assert m._NotificationMixin in TaskScheduler.__mro__
    assert TaskScheduler.add_notification.__qualname__ == "_NotificationMixin.add_notification"
    assert TaskScheduler.pop_notifications.__qualname__ == "_NotificationMixin.pop_notifications"


def test_owner_filter_behavior_preserved_through_mixin():
    sch = TaskScheduler.__new__(TaskScheduler)  # bypass __init__ (network etc.)
    sch._pending_notifications = []
    sch.add_notification("t1", "success", "id1", owner="alice")
    sch.add_notification("t2", "error", "id2", owner="bob")
    sch.add_notification("t3", "success", "id3", owner=None)
    alice = sch.pop_notifications(owner="alice")
    assert {n["task_name"] for n in alice} == {"t1"}
    # bob's row + the legacy null-owner row stay queued (no cross-tenant drain)
    assert len(sch._pending_notifications) == 2
