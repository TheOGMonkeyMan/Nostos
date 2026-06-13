"""Phase 2.3 (slice 1): verify the app.py page-serve routes split.

serve_index + the per-tool SPA deep-link routes (/notes, /calendar, ...) + the
backgrounds/login static pages + the _serve_html_with_nonce helper moved verbatim out of
app.py into routes/page_routes.py (only @app.get -> @router.get changed). app.py includes
the router via setup_page_routes(), so the served paths + CSP-nonce behavior are
unchanged. This pins the route set, the leaf property (no import-cycle back to app), and
the nonce injection so a future edit cannot silently drop a deep-link or the nonce.
"""

import inspect
import types

from fastapi import APIRouter

import routes.page_routes as pr

_PATHS = ("/", "/notes", "/calendar", "/cookbook", "/email", "/memory",
          "/gallery", "/tasks", "/library", "/backgrounds", "/login")


def test_setup_page_routes_registers_every_path():
    router = pr.setup_page_routes()
    assert isinstance(router, APIRouter)
    got = {r.path for r in router.routes}
    for p in _PATHS:
        assert p in got, p


def test_page_routes_is_a_leaf_no_cycle():
    src = inspect.getsource(pr)
    # pulls shared helpers from the leaves app.py also uses, never from app
    assert "from src.app_helpers import abs_join" in src
    assert "from core.constants import BASE_DIR" in src
    assert "import app" not in src and "from app " not in src


def test_serve_html_with_nonce_injects_request_nonce(tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<script nonce=\"{{CSP_NONCE}}\">x</script>", encoding="utf-8")
    req = types.SimpleNamespace(state=types.SimpleNamespace(csp_nonce="abc123"))
    resp = pr._serve_html_with_nonce(req, str(html))
    assert b"abc123" in resp.body and b"{{CSP_NONCE}}" not in resp.body


def test_serve_html_with_nonce_blank_when_no_nonce(tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<script nonce=\"{{CSP_NONCE}}\">x</script>", encoding="utf-8")
    req = types.SimpleNamespace(state=types.SimpleNamespace())  # no csp_nonce attr
    resp = pr._serve_html_with_nonce(req, str(html))
    assert b"{{CSP_NONCE}}" not in resp.body  # replaced with "" (getattr default)
