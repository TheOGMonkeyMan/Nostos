"""Phase 2.2 (ADR-048): verify the skill-audit helper split.

The 5 module-level skill-audit helpers (_audit_auto_publish_policy,
_skill_duplicate_blocker, _audit_flag_text, _audit_generic_blocker,
_audit_finalize_status) moved verbatim out of routes/skills_routes.py into the
new routes/skills_helpers.py, re-imported so the route closures keep calling them
as module globals.
"""

import routes.skills_routes as sr


def test_audit_helpers_reexported_from_skills_routes():
    names = [
        "_audit_auto_publish_policy",
        "_skill_duplicate_blocker",
        "_audit_flag_text",
        "_audit_generic_blocker",
        "_audit_finalize_status",
    ]
    for n in names:
        assert hasattr(sr, n), f"{n} missing from skills_routes namespace"
        assert getattr(sr, n).__module__ == "routes.skills_helpers"


def test_audit_flag_text_is_hermetic_and_pure():
    # _audit_flag_text(*parts) -> str joins the non-empty parts; pure, no I/O.
    from routes.skills_helpers import _audit_flag_text
    out = _audit_flag_text("a", "", "b")
    assert isinstance(out, str)
    assert "a" in out and "b" in out
