"""Phase 2.2 (ADR-035): characterize + verify the documents-group extraction.

The living-document handlers, their pure parsers/sniffers, and the active-document
state (the _active_document_id / _active_model module globals plus the
set/get_active_document/set_active_model accessors) were carved out of
src/tool_implementations.py into src/tools/documents.py. Everything is re-exported
so callers (agent_loop, agent_tools, chat_routes, document_routes, pdf_form_doc,
tool_execution, and management.py's shims) keep working. The active-document
state must remain a single shared global after the move.
"""

from src import tool_implementations as ti

_PUBLIC = [
    "set_active_document",
    "set_active_model",
    "get_active_document",
    "_sniff_doc_language",
    "_looks_like_email_document",
    "_coerce_email_document_content",
    "parse_edit_blocks",
    "parse_suggest_blocks",
    "do_create_document",
    "do_update_document",
    "do_edit_document",
    "do_suggest_document",
]


# --- behavior preservation (public interface, hermetic) --------------------

def test_active_document_state_roundtrip():
    orig = ti.get_active_document()
    try:
        ti.set_active_document("doc-roundtrip-xyz")
        assert ti.get_active_document() == "doc-roundtrip-xyz"
        ti.set_active_document(None)
        assert ti.get_active_document() is None
    finally:
        ti.set_active_document(orig)


def test_sniff_doc_language_returns_str():
    assert isinstance(ti._sniff_doc_language("def foo():\n    return 1\n"), str)


def test_looks_like_email_document_returns_bool():
    assert isinstance(ti._looks_like_email_document("hello", "subject"), bool)


def test_parse_edit_blocks_returns_list():
    assert isinstance(ti.parse_edit_blocks(""), list)


# --- extraction target (RED until src/tools/documents.py exists) ------------

def test_documents_module_exists_and_exports():
    from src.tools import documents

    for name in _PUBLIC:
        assert hasattr(documents, name), f"src.tools.documents is missing {name}"


def test_documents_symbols_reexported_with_identity():
    from src.tools import documents

    for name in _PUBLIC:
        assert getattr(ti, name) is getattr(documents, name), f"{name} not re-exported with identity"


def test_active_document_state_is_shared_across_modules():
    # The re-exported accessor and the new module must share ONE global.
    from src.tools import documents

    orig = ti.get_active_document()
    try:
        ti.set_active_document("shared-state-check")
        assert documents.get_active_document() == "shared-state-check"
    finally:
        ti.set_active_document(orig)
