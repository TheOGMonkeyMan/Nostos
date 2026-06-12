"""Phase 2.2 (ADR-033): characterize + verify the notes/calendar extraction.

The personal-organizer handlers (do_manage_notes, do_manage_calendar) were
carved out of src/tool_implementations.py into src/tools/notes_calendar.py and
re-exported so callers (src/tool_execution.py, task_scheduler, email_pollers)
keep working. The invalid-JSON branch returns before any database access, so it
is hermetic.
"""

from src import tool_implementations as ti


# --- behavior preservation (public interface, hermetic) --------------------

async def test_do_manage_notes_invalid_json():
    assert await ti.do_manage_notes("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_manage_calendar_invalid_json():
    assert await ti.do_manage_calendar("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


# --- extraction target (RED until src/tools/notes_calendar.py exists) -------

def test_notes_calendar_module_exists_and_exports():
    from src.tools import notes_calendar

    for name in ("do_manage_notes", "do_manage_calendar"):
        assert hasattr(notes_calendar, name), f"src.tools.notes_calendar is missing {name}"


def test_notes_calendar_handlers_reexported_with_identity():
    from src.tools import notes_calendar

    assert ti.do_manage_notes is notes_calendar.do_manage_notes
    assert ti.do_manage_calendar is notes_calendar.do_manage_calendar
