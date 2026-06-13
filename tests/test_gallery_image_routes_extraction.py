"""Phase 2.2 (ADR-050): verify the gallery image-processing route-group split.

The 7 /api/image/* routes (inpaint, harmonize, sharpen, denoise, upscale-local,
remove-bg, enhance-face) moved verbatim out of setup_gallery_routes() into
routes/gallery_image_routes.py::register_image_routes(router). The heavy image
libraries are lazy-imported inside the routes, so the registrar takes only the
router.
"""

from fastapi import APIRouter
from routes.gallery_routes import setup_gallery_routes

_PATHS = [
    "/api/image/inpaint",
    "/api/image/harmonize",
    "/api/image/sharpen",
    "/api/image/denoise",
    "/api/image/upscale-local",
    "/api/image/remove-bg",
    "/api/image/enhance-face",
]


def test_register_image_routes_registers_the_group():
    from routes.gallery_image_routes import register_image_routes

    r = APIRouter()
    register_image_routes(r)
    paths = {getattr(rt, "path", "") for rt in r.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"


def test_setup_gallery_routes_still_registers_image_routes():
    router = setup_gallery_routes()
    paths = {getattr(rt, "path", "") for rt in router.routes}
    for p in _PATHS:
        assert p in paths, f"missing {p}"
