"""Phase 2.2 (ADR-060): verify the TaskScheduler execution-cluster mixin split.

The 8 execution methods (_execute_task / _execute_task_locked / _execute_action /
_execute_checkin / _execute_llm_task + _task_needs_model_slot / _log_to_assistant /
_format_email_output) moved verbatim out of the TaskScheduler god-class into
`class _ExecutionMixin` (src/scheduler_execution.py); TaskScheduler now inherits
(_ExecutionMixin, _NotificationMixin) and self.* resolves the methods via the MRO. This
is the slice that takes task_scheduler.py under the 1500 cap (the last Phase 2.2
god-file). The full suite is the behavioral gate (the scheduler-delivery + notification
tests drive these methods through real instances); this pins the structure + the R14
ship_paused seeding-cluster invariant so a future edit cannot quietly move or merge them.
"""

import inspect

import src.task_scheduler as ts
import src.scheduler_execution as se

_EXEC_METHODS = (
    "_execute_task", "_execute_task_locked", "_execute_action", "_execute_checkin",
    "_execute_llm_task", "_task_needs_model_slot", "_log_to_assistant", "_format_email_output",
)


def test_execution_methods_live_in_the_mixin():
    for m in _EXEC_METHODS:
        assert hasattr(ts.TaskScheduler, m), m
        # resolved object is the mixin's (qualname carries the defining class)
        assert getattr(ts.TaskScheduler, m).__qualname__.startswith("_ExecutionMixin"), m
        assert hasattr(se._ExecutionMixin, m), m


def test_taskscheduler_mro_includes_both_mixins_in_order():
    names = [c.__name__ for c in ts.TaskScheduler.__mro__]
    assert names[:4] == ["TaskScheduler", "_ExecutionMixin", "_NotificationMixin", "object"]


def test_seeding_cluster_and_ship_paused_pin_stay_in_main_class():
    # R14: the housekeeping seeding cluster (which carries the ship_paused source-text
    # pin guarded by test_auth_regressions) must NOT have moved into the mixin.
    for m in ("ensure_defaults", "ensure_assistant_defaults"):
        assert getattr(ts.TaskScheduler, m).__qualname__.startswith("TaskScheduler"), m
        assert not hasattr(se._ExecutionMixin, m), m
    assert 'defs.get("ship_paused")' in inspect.getsource(ts.TaskScheduler.ensure_defaults)


def test_mixin_module_is_a_near_leaf_no_cycle():
    # scheduler_execution imports its shared helpers from the scheduler_helpers leaf,
    # never from task_scheduler, so there is no import cycle.
    src = inspect.getsource(se)
    assert "from src.scheduler_helpers import" in src
    assert "import src.task_scheduler" not in src
    assert "from src.task_scheduler" not in src
