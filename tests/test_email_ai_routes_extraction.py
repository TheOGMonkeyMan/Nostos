"""Phase 2.2 (ADR-040): verify the email AI route-group split.

The AI routes (/extract-style, /summarize, /ai-reply) moved out of
setup_email_routes() into routes/email_ai_routes.py::register_ai_routes(router).
They use the module-level _imap() context manager + email_helpers + llm_call_async
(no bound pool/cache locals and no sync workers), so the registrar needs only the
router.
"""

from fastapi import APIRouter
from routes.email_routes import setup_email_routes

_PATHS = [
    "/api/email/extract-style",
    "/api/email/summarize",
    "/api/email/ai-reply",
]


def test_register_ai_routes_registers_the_group():
    from routes.email_ai_routes import register_ai_routes

    r = APIRouter(prefix="/api/email", tags=["email"])
    register_ai_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_email_routes_still_registers_ai(monkeypatch):
    monkeypatch.setattr("routes.email_routes._start_poller", lambda: None)
    router = setup_email_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
