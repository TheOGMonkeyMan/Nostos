"""Phase 2.2 (ADR-038): verify the email config/accounts/style route-group split.

The settings + account-CRUD routes (/style, /config, /urgency-state, /accounts*)
moved out of setup_email_routes() into
routes/email_account_routes.py::register_account_routes(router), which
setup_email_routes() now calls. They use none of the IMAP pool/cache/sync
machinery, so the registrar needs only the router.
"""

from fastapi import APIRouter
from routes.email_routes import setup_email_routes

_PATHS = [
    "/api/email/style",
    "/api/email/config",
    "/api/email/urgency-state",
    "/api/email/accounts",
    "/api/email/accounts/{account_id}",
    "/api/email/accounts/test",
    "/api/email/accounts/{account_id}/set-default",
]


def test_register_account_routes_registers_the_group():
    from routes.email_account_routes import register_account_routes

    r = APIRouter(prefix="/api/email", tags=["email"])
    register_account_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_email_routes_still_registers_account_group(monkeypatch):
    monkeypatch.setattr("routes.email_routes._start_poller", lambda: None)
    router = setup_email_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
