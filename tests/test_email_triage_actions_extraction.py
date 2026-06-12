"""Phase 2.2 (ADR-044): verify the email urgency-triage action split.

action_check_email_urgency + its triage model/helpers (_EmailTriage,
_normalize_triage, _email_triage_verdict, _TRIAGE_*) moved verbatim into
src/actions/email_triage.py, re-imported into builtin_actions.py. This pins the
registry contract and the re-export of the test-facing helpers (the existing
test_email_triage_quarantine.py imports _email_triage_verdict / _normalize_triage
from src.builtin_actions and must keep working).
"""

import src.builtin_actions as ba


def test_check_email_urgency_registry_resolves_to_new_module():
    assert "check_email_urgency" in ba.BUILTIN_ACTIONS
    assert ba.BUILTIN_ACTIONS["check_email_urgency"].__module__ == "src.actions.email_triage"


def test_triage_helpers_reexported_for_existing_callers():
    # test_email_triage_quarantine.py relies on these import paths.
    from src.builtin_actions import _email_triage_verdict, _normalize_triage, _EmailTriage  # noqa: F401
    assert _normalize_triage.__module__ == "src.actions.email_triage"
    assert _email_triage_verdict.__module__ == "src.actions.email_triage"
