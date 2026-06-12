"""Phase 2.2 (ADR-043): verify the maintenance/tidy action split + the new
dependency-free action-exceptions module.

action_tidy_sessions / action_tidy_documents / action_consolidate_memory moved
verbatim into src/actions/maintenance.py, and TaskNoop/TaskDeferred into the
leaf module src/actions/base.py (so action submodules import them without a
cycle). builtin_actions.py re-imports both. These checks pin the re-import
contract and - critically - the exception CLASS IDENTITY across modules, so
`except TaskNoop` in the scheduler still catches what the actions raise.
"""

import src.actions.base as base
import src.builtin_actions as ba


def test_maintenance_registry_resolves_to_new_module():
    for name in ("tidy_sessions", "tidy_documents", "consolidate_memory"):
        assert name in ba.BUILTIN_ACTIONS, f"{name} missing from BUILTIN_ACTIONS"
        assert ba.BUILTIN_ACTIONS[name].__module__ == "src.actions.maintenance"


def test_exception_identity_preserved():
    # builtin_actions re-exports the SAME class objects, so cross-module
    # `except TaskNoop` / isinstance checks keep working.
    assert ba.TaskNoop is base.TaskNoop
    assert ba.TaskDeferred is base.TaskDeferred
    assert issubclass(base.TaskNoop, BaseException)
    # TaskDeferred still carries its reason/delay payload.
    d = base.TaskDeferred("later", delay_seconds=42)
    assert d.reason == "later" and d.delay_seconds == 42


def test_external_import_path_intact():
    from src.builtin_actions import TaskNoop, TaskDeferred  # noqa: F401
    assert TaskNoop is base.TaskNoop
