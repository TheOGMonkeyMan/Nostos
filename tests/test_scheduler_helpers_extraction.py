"""Phase 2.2 (ADR-045): verify the task_scheduler.py module-level helper split.

The singleflight TTL cache (_cached + shared state), compute_next_run and
_resolve_task_timezone moved verbatim into src/scheduler_helpers.py and are
re-imported into src/task_scheduler.py, so the public API (compute_next_run -
imported by task_routes, management tools, assistant_routes) and the bare-name
references inside the TaskScheduler class keep resolving. HOUSEKEEPING_DEFAULTS +
RETIRED_HOUSEKEEPING_ACTIONS intentionally STAY defined in task_scheduler.py (a
security regression test pins their ship_paused defaults to that source file).
"""

import src.task_scheduler as ts


def test_extracted_functions_reexported_from_task_scheduler():
    from src.task_scheduler import compute_next_run, _resolve_task_timezone, _cached  # noqa: F401
    assert compute_next_run.__module__ == "src.scheduler_helpers"
    assert _resolve_task_timezone.__module__ == "src.scheduler_helpers"
    assert _cached.__module__ == "src.scheduler_helpers"


def test_housekeeping_constants_stay_in_task_scheduler():
    # These did NOT move (security test pins them); still importable + usable.
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, RETIRED_HOUSEKEEPING_ACTIONS
    assert isinstance(HOUSEKEEPING_DEFAULTS, dict)
    assert isinstance(RETIRED_HOUSEKEEPING_ACTIONS, frozenset)


def test_class_bare_name_references_resolve():
    # The TaskScheduler class body calls these as module globals.
    for name in ("compute_next_run", "_resolve_task_timezone", "_cached",
                 "HOUSEKEEPING_DEFAULTS", "RETIRED_HOUSEKEEPING_ACTIONS"):
        assert hasattr(ts, name), f"{name} missing from task_scheduler namespace"
    assert hasattr(ts, "TaskScheduler")
