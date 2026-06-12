"""Phase 2.2 (ADR-041): verify the email compose/send route-group split.

The compose/send routes (/compose-upload + delete, /schedule, /scheduled +
delete, /resolve-contact, /send, /draft) moved out of setup_email_routes() into
routes/email_compose_routes.py::register_compose_routes(router). The group
carries its own async worker _send_email_sync and references none of the bound
pool/cache locals or the list/read sync workers, so the registrar needs only the
router.
"""

from fastapi import APIRouter
from routes.email_routes import setup_email_routes

_PATHS = [
    "/api/email/compose-upload",
    "/api/email/compose-upload/{token}",
    "/api/email/schedule",
    "/api/email/scheduled",
    "/api/email/scheduled/{sid}",
    "/api/email/resolve-contact",
    "/api/email/send",
    "/api/email/draft",
]


def test_register_compose_routes_registers_the_group():
    from routes.email_compose_routes import register_compose_routes

    r = APIRouter(prefix="/api/email", tags=["email"])
    register_compose_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_email_routes_still_registers_compose(monkeypatch):
    monkeypatch.setattr("routes.email_routes._start_poller", lambda: None)
    router = setup_email_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
