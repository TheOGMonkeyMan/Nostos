"""Status / diagnostic routes (Phase 2.3): version, health, readiness, runtime.

Extracted verbatim from app.py. /healthz is the readiness probe (per-component status
via core.health; 503 only when the DB is down); /api/health is a trivial liveness ping;
/api/version + /api/runtime report build + environment info. setup_status_routes()
returns the router app.py includes; bodies + paths are unchanged.
"""

import os
from datetime import datetime
from typing import Dict

from fastapi import APIRouter
from starlette.responses import JSONResponse as _JSONResponse

router = APIRouter()


@router.get("/api/version")
async def get_version():
    from core.constants import APP_VERSION
    return {"version": APP_VERSION}

@router.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@router.get("/healthz")
async def healthz():
    """Readiness probe: per-component status (db, vector store, search,
    providers). No auth, lightweight, non-networked. Returns 503 only when the
    database (the hard dependency) is unreachable so orchestrators can gate on
    it; otherwise 200 with overall status ok|degraded. Logic lives in
    core.health so the handler stays thin (engineering-standards)."""
    from core.health import check_health
    report = check_health()
    code = 503 if report["status"] == "error" else 200
    return _JSONResponse(report, status_code=code)

@router.get("/api/runtime")
async def runtime_info() -> Dict[str, object]:
    in_docker = os.path.exists("/.dockerenv")
    if not in_docker:
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as fh:
                cg = fh.read()
            in_docker = any(marker in cg for marker in ("docker", "containerd", "kubepods"))
        except Exception:
            in_docker = False
    ollama_url = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_URL")
        or ("http://host.docker.internal:11434/v1" if in_docker else "http://127.0.0.1:11434/v1")
    )
    return {
        "in_docker": in_docker,
        "ollama_base_url": ollama_url,
    }


def setup_status_routes() -> APIRouter:
    return router
