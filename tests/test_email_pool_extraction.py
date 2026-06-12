"""Phase 2.2 (ADR-037): characterize the email_routes IMAP-pool extraction.

The IMAP connection pool + list/read caches moved into
routes/email_pool.py::build_email_pool(router). setup_email_routes() binds the
returned helpers to local names so the route closures are unchanged. These tests
pin that the factory returns the helpers + wires the router, and that the same
email routes still register.
"""

from routes.email_routes import setup_email_routes
from routes.email_pool import build_email_pool

_HELPERS = [
    "_pooled_connect", "_pooled_release", "_list_cache_get", "_list_cache_put",
    "_invalidate_list_cache", "_read_cache_get", "_read_cache_put",
    "_list_cache_key", "_read_cache_key",
]


def test_build_email_pool_returns_helpers_and_wires_router():
    class _R:
        pass

    r = _R()
    pool = build_email_pool(r)
    for k in _HELPERS:
        assert callable(pool[k]), f"missing helper {k}"
    assert hasattr(r, "_email_pool") and "connect" in r._email_pool


def test_setup_email_routes_registers_the_same_endpoints(monkeypatch):
    # Avoid starting the real background poller during the test.
    monkeypatch.setattr("routes.email_routes._start_poller", lambda: None)
    router = setup_email_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in (
        "/api/email/list",
        "/api/email/read/{uid}",
        "/api/email/send",
        "/api/email/config",
        "/api/email/accounts",
        "/api/email/folders",
        "/api/email/search",
    ):
        assert p in paths, f"missing route {p}"
    assert len(router.routes) >= 40
