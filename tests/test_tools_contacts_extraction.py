"""Phase 2.2 (ADR-030): characterize + verify the contact-tools extraction.

The CardDAV contact handlers (do_resolve_contact, do_manage_contact) were
carved out of src/tool_implementations.py into src/tools/contacts.py and
re-exported so callers (src/tool_execution.py) keep working unchanged. The
input-validation branches asserted here return before any network or
contacts-module access, so they are hermetic.
"""

from src import tool_implementations as ti


# --- behavior preservation (public interface, hermetic) --------------------

async def test_do_resolve_contact_invalid_json_returns_error():
    assert await ti.do_resolve_contact("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_resolve_contact_missing_name_returns_error():
    assert await ti.do_resolve_contact("{}") == {
        "error": "name is required",
        "exit_code": 1,
    }


async def test_do_manage_contact_invalid_json_returns_error():
    assert await ti.do_manage_contact("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


# --- extraction target (RED until src/tools/contacts.py exists) -------------

def test_contacts_module_exists_and_exports():
    from src.tools import contacts

    for name in ("do_resolve_contact", "do_manage_contact"):
        assert hasattr(contacts, name), f"src.tools.contacts is missing {name}"


def test_contacts_handlers_are_reexported_with_identity():
    from src.tools import contacts

    assert ti.do_resolve_contact is contacts.do_resolve_contact
    assert ti.do_manage_contact is contacts.do_manage_contact
