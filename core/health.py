"""Lightweight readiness probe backing the /healthz route.

Reports per-component status (db, vector_store, search, providers) for
readiness checks (the docker smoke test, container orchestrators). Every probe
is deliberately cheap and NON-networked, and wrapped so it can never raise — a
health endpoint must not hang or 500. Overall status is "error" only when the
database (the single hard dependency) is unreachable; missing optional
components yield "degraded", not "error".
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from typing import Any, Dict


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _check_db() -> Dict[str, str]:
    """Real round-trip: SELECT 1 against the configured engine."""
    try:
        from sqlalchemy import text

        from core.database import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # never propagate out of a health check
        return {"status": "error", "detail": type(exc).__name__}


def _check_providers() -> Dict[str, Any]:
    """Count configured model endpoints (cheap local query, no network)."""
    try:
        from core.database import ModelEndpoint, SessionLocal

        db = SessionLocal()
        try:
            count = db.query(ModelEndpoint).count()
        finally:
            db.close()
        return {"status": "ok" if count > 0 else "none", "configured": count}
    except Exception as exc:
        return {"status": "unknown", "detail": type(exc).__name__}


def _check_vector_store() -> Dict[str, str]:
    """Report whether the embedding backend is installed. NON-networked: the
    vector store is now embedded LanceDB (no standalone service to probe, ADR-065),
    and a health endpoint must never hang on a network call regardless."""
    return {"status": "ok" if _module_available("fastembed") else "unavailable"}


def _check_search() -> Dict[str, str]:
    """Report whether the search subsystem is importable (non-networked)."""
    return {"status": "ok" if _module_available("src.search") else "unavailable"}


def check_health() -> Dict[str, Any]:
    """Probe every component and return a readiness report. Never raises.

    overall status:
      - "error"    -> the database is unreachable (hard failure)
      - "degraded" -> db ok but an optional component is unavailable/unknown
      - "ok"       -> everything reporting healthy
    """
    components: Dict[str, Dict[str, Any]] = {
        "db": _check_db(),
        "vector_store": _check_vector_store(),
        "search": _check_search(),
        "providers": _check_providers(),
    }
    if components["db"]["status"] == "error":
        status = "error"
    elif all(c.get("status") in ("ok", "none") for c in components.values()):
        status = "ok"
    else:
        status = "degraded"
    return {
        "status": status,
        "components": components,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
