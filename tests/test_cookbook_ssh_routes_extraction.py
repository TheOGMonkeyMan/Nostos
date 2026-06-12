"""Phase 2.2 (ADR-046): verify the cookbook ssh-key route-group split.

The GET/POST /api/cookbook/ssh-key routes + their 3 self-contained ssh path
helpers moved verbatim out of setup_cookbook_routes() into
routes/cookbook_ssh_routes.py::register_ssh_key_routes(router). The helpers are
used only by these routes, so they move with the group; the registrar needs only
the router.
"""

from fastapi import APIRouter
from routes.cookbook_routes import setup_cookbook_routes

_PATH = "/api/cookbook/ssh-key"


def test_register_ssh_key_routes_registers_the_group():
    from routes.cookbook_ssh_routes import register_ssh_key_routes

    r = APIRouter()
    register_ssh_key_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    assert _PATH in paths


def test_setup_cookbook_routes_still_registers_ssh_key():
    router = setup_cookbook_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    assert _PATH in paths
