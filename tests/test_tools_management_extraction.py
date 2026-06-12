"""Phase 2.2 (ADR-034): characterize + verify the management-tools extraction.

The eight entity-management handlers (skills, tasks, endpoints, mcp, webhooks,
tokens, documents, settings) were carved out of src/tool_implementations.py into
src/tools/management.py and re-exported so callers (tool_execution, agent_tools,
teacher_escalation, and cookbook.py's lazy do_manage_endpoints) keep working.
The invalid-JSON branch of each handler returns before any side effects, so it
is hermetic.
"""

from src import tool_implementations as ti

_HANDLERS = [
    "do_manage_skills",
    "do_manage_tasks",
    "do_manage_endpoints",
    "do_manage_mcp",
    "do_manage_webhooks",
    "do_manage_tokens",
    "do_manage_documents",
    "do_manage_settings",
]


# --- behavior preservation (public interface, hermetic) --------------------

async def test_do_manage_skills_invalid_json():
    assert await ti.do_manage_skills("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_manage_tasks_invalid_json():
    assert await ti.do_manage_tasks("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_manage_endpoints_invalid_json():
    assert await ti.do_manage_endpoints("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_manage_settings_invalid_json():
    assert await ti.do_manage_settings("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


# --- extraction target (RED until src/tools/management.py exists) -----------

def test_management_module_exists_and_exports():
    from src.tools import management

    for name in _HANDLERS:
        assert hasattr(management, name), f"src.tools.management is missing {name}"


def test_management_handlers_reexported_with_identity():
    from src.tools import management

    for name in _HANDLERS:
        assert getattr(ti, name) is getattr(management, name), f"{name} not re-exported with identity"
