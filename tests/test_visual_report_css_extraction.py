"""Phase 2.2 (ADR-051): verify the per-category CSS builder split.

_category_css (a ~516-line pure CSS-template function) moved verbatim out of
src/visual_report.py into src/visual_report_css.py, re-imported so
generate_visual_report keeps calling it.
"""

import src.visual_report as v


def test_category_css_reexported_from_visual_report():
    assert hasattr(v, "_category_css")
    assert v._category_css.__module__ == "src.visual_report_css"


def test_category_css_is_hermetic_and_pure():
    from src.visual_report_css import _category_css
    # No category -> empty string (the early-return branch).
    assert _category_css(None) == ""
    assert _category_css("") == ""
    # A known category -> a non-empty CSS string.
    out = _category_css("factcheck")
    assert isinstance(out, str) and len(out) > 0
