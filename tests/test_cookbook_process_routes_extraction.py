"""Phase 2.2 (ADR-047): verify the cookbook process/GPU route-group split.

The GPU-probe helpers (_run_nvidia_smi / _probe_gpu_device_processes /
_probe_amd_sysfs, used only by the gpus route) + GET /api/cookbook/gpus + POST
/api/cookbook/kill-pid moved verbatim out of setup_cookbook_routes() into
routes/cookbook_process_routes.py::register_process_routes(router). The helpers
move with the group; the registrar needs only the router.
"""

from fastapi import APIRouter
from routes.cookbook_routes import setup_cookbook_routes

_PATHS = ["/api/cookbook/gpus", "/api/cookbook/kill-pid"]


def test_register_process_routes_registers_the_group():
    from routes.cookbook_process_routes import register_process_routes

    r = APIRouter()
    register_process_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_cookbook_routes_still_registers_process():
    router = setup_cookbook_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
