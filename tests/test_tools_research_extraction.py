"""Phase 2.2 (ADR-031): characterize + verify the research-tools extraction.

The deep-research handlers (do_manage_research, do_trigger_research) were carved
out of src/tool_implementations.py into src/tools/research.py and re-exported so
callers (src/tool_execution.py) keep working. The branches asserted here return
before any filesystem or network access, so they are hermetic.
"""

from src import tool_implementations as ti


# --- behavior preservation (public interface, hermetic) --------------------

async def test_do_manage_research_invalid_id_rejected():
    # Path-traversal id must be rejected before any filesystem access.
    assert await ti.do_manage_research('{"action":"read","id":"../etc"}') == {
        "error": "Invalid research id."
    }


async def test_do_manage_research_read_without_id():
    assert await ti.do_manage_research('{"action":"read"}') == {
        "error": "Provide the research id (from action='list')."
    }


async def test_do_trigger_research_invalid_json():
    assert await ti.do_trigger_research("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_trigger_research_missing_topic():
    assert await ti.do_trigger_research("{}") == {
        "error": "topic (or query) is required",
        "exit_code": 1,
    }


# --- extraction target (RED until src/tools/research.py exists) -------------

def test_research_module_exists_and_exports():
    from src.tools import research

    for name in ("do_manage_research", "do_trigger_research"):
        assert hasattr(research, name), f"src.tools.research is missing {name}"


def test_research_handlers_reexported_with_identity():
    from src.tools import research

    assert ti.do_manage_research is research.do_manage_research
    assert ti.do_trigger_research is research.do_trigger_research
