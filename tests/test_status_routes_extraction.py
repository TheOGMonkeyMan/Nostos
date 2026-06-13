"""Phase 2.3 (slice 2): verify the app.py status/diagnostic routes split.

/api/version, /api/health, /healthz, /api/runtime moved verbatim out of app.py into
routes/status_routes.py behind setup_status_routes() (only @app.get -> @router.get).
Pins the route set + the leaf property (no import cycle back to app).
"""

import inspect

from fastapi import APIRouter

import routes.status_routes as sr

_PATHS = ("/api/version", "/api/health", "/healthz", "/api/runtime")


def test_setup_status_routes_registers_every_path():
    router = sr.setup_status_routes()
    assert isinstance(router, APIRouter)
    got = {r.path for r in router.routes}
    for p in _PATHS:
        assert p in got, p


def test_status_routes_is_a_leaf_no_cycle():
    src = inspect.getsource(sr)
    assert "import app" not in src and "from app " not in src
