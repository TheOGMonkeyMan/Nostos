"""Page-serve routes (Phase 2.3): the SPA shell + static HTML pages.

Extracted verbatim from app.py. serve_index renders static/index.html with the per-request
CSP nonce injected into inline <script> tags; the per-tool deep-link routes (/notes,
/calendar, /cookbook, ...) all delegate to it so the SPA's JS opens the matching modal from
window.location.pathname. /backgrounds and /login serve their own static pages.
setup_page_routes() returns the router app.py includes; bodies + served paths are unchanged.
"""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.constants import BASE_DIR
from src.app_helpers import abs_join

router = APIRouter()


def _serve_html_with_nonce(request: Request, file_path: str) -> HTMLResponse:
    """Read an HTML file and inject the CSP nonce into inline <script> tags."""
    with open(file_path, "r", encoding="utf-8") as f:
        html = f.read()
    nonce = getattr(request.state, "csp_nonce", "")
    html = html.replace("{{CSP_NONCE}}", nonce)
    return HTMLResponse(html)

@router.get("/")
async def serve_index(request: Request):
    static_path = abs_join(BASE_DIR, "static/index.html")
    if os.path.exists(static_path):
        return _serve_html_with_nonce(request, static_path)
    root_path = abs_join(BASE_DIR, "index.html")
    if os.path.exists(root_path):
        return _serve_html_with_nonce(request, root_path)
    raise HTTPException(404, "index.html not found")

@router.get("/notes")
async def serve_notes(request: Request):
    return await serve_index(request)

@router.get("/calendar")
async def serve_calendar(request: Request):
    return await serve_index(request)

# Per-tool deep-link routes — all serve the same SPA, the JS auto-opens
# the matching modal based on window.location.pathname. Each route also
# gets a unique favicon + page title via inline script in index.html so
# bookmarks render with tool-specific icons.
@router.get("/cookbook")
async def serve_cookbook(request: Request):
    return await serve_index(request)

@router.get("/email")
async def serve_email(request: Request):
    return await serve_index(request)

@router.get("/memory")
async def serve_memory(request: Request):
    return await serve_index(request)

@router.get("/gallery")
async def serve_gallery(request: Request):
    return await serve_index(request)

@router.get("/tasks")
async def serve_tasks(request: Request):
    return await serve_index(request)

@router.get("/library")
async def serve_library(request: Request):
    return await serve_index(request)

@router.get("/backgrounds")
async def serve_backgrounds(request: Request):
    """Sandbox page for prototyping background effects. No auth required."""
    return _serve_html_with_nonce(request, abs_join(BASE_DIR, "static/backgrounds.html"))

@router.get("/login")
async def serve_login(request: Request):
    return _serve_html_with_nonce(request, abs_join(BASE_DIR, "static/login.html"))


def setup_page_routes() -> APIRouter:
    return router
