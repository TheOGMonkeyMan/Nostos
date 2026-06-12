"""Phase 2.2 (ADR-049): verify the document tidy route-group split.

The POST /api/documents/tidy + /api/documents/ai-tidy routes moved verbatim out
of setup_document_routes() into
routes/document_tidy_routes.py::register_tidy_routes(router). They use neither
shared module-level helper (_load_pdf_viewer_fitz / _locate_current_user_upload),
so the registrar takes only the router.
"""

from fastapi import APIRouter
from routes.document_routes import setup_document_routes

_PATHS = ["/api/documents/tidy", "/api/documents/ai-tidy"]


def test_register_tidy_routes_registers_the_group():
    from routes.document_tidy_routes import register_tidy_routes

    r = APIRouter()
    register_tidy_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_document_routes_still_registers_tidy():
    router = setup_document_routes(None)
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
