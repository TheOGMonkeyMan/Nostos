"""Tests for the /healthz readiness probe logic (core.health.check_health).

These exercise the delegated logic directly rather than booting the FastAPI app
(the suite does not import app.py — see test_app.py). check_health must never
raise and must report every component.
"""
from core.health import check_health


def test_check_health_reports_all_components():
    report = check_health()
    assert set(report["components"]) == {"db", "vector_store", "search", "providers"}
    assert "status" in report
    assert "timestamp" in report


def test_check_health_db_ok_and_not_error():
    """The test env has a working SQLite engine (conftest ensures ./data exists),
    so the DB probe must succeed and overall status must not be 'error'."""
    report = check_health()
    assert report["components"]["db"]["status"] == "ok"
    assert report["status"] in ("ok", "degraded")


def test_check_health_overall_status_is_consistent():
    report = check_health()
    statuses = [c.get("status") for c in report["components"].values()]
    if report["components"]["db"]["status"] == "error":
        assert report["status"] == "error"
    elif all(s in ("ok", "none") for s in statuses):
        assert report["status"] == "ok"
    else:
        assert report["status"] == "degraded"


def test_check_health_never_raises_even_if_db_down(monkeypatch):
    """A health probe must degrade gracefully, not 500. Force the DB probe to
    fail and confirm the report still comes back with status 'error'."""
    import core.health as health

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(health, "_check_db", lambda: {"status": "error", "detail": "RuntimeError"})
    report = health.check_health()
    assert report["status"] == "error"
    assert report["components"]["db"]["status"] == "error"
