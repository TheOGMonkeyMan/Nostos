"""Phase 2.2 (ADR-039): verify the email attachment route-group split.

The attachment routes (/attachments/{uid}, /attachment/{uid}/{index},
/attachment-as-doc/{uid}/{index}, /attachment-path/{uid}/{index}) moved out of
setup_email_routes() into
routes/email_attachment_routes.py::register_attachment_routes(router). They use
the module-level _imap() context manager + email_helpers attachment functions,
not the bound pool/sync locals, so the registrar needs only the router.
"""

from fastapi import APIRouter
from routes.email_routes import setup_email_routes

_PATHS = [
    "/api/email/attachments/{uid}",
    "/api/email/attachment/{uid}/{index}",
    "/api/email/attachment-as-doc/{uid}/{index}",
    "/api/email/attachment-path/{uid}/{index}",
]


def test_register_attachment_routes_registers_the_group():
    from routes.email_attachment_routes import register_attachment_routes

    r = APIRouter(prefix="/api/email", tags=["email"])
    register_attachment_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_email_routes_still_registers_attachments(monkeypatch):
    monkeypatch.setattr("routes.email_routes._start_poller", lambda: None)
    router = setup_email_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
