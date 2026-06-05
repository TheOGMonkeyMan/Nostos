"""Phase 1.2: the default-deny capability model (PROPOSED, not yet wired).

Verifies the new allowlist model's logic. The live decision path
(is_public_blocked_tool / blocked_tools_for_owner) is unchanged; a migration
wires authorize() later.
"""

import src.tool_security as ts
from src.tool_security import RiskTier, Role, authorize, policy_for


def test_unregistered_tool_is_default_deny():
    p = policy_for("a_brand_new_unlisted_tool")
    assert p.risk_tier is RiskTier.PRIVILEGED
    assert p.min_role is Role.ADMIN
    assert p.requires_approval is True


def test_mcp_tool_defaults_to_privileged():
    assert policy_for("mcp__some_server__some_tool").risk_tier is RiskTier.PRIVILEGED


def test_known_tiers():
    assert policy_for("web_search").risk_tier is RiskTier.READ_ONLY
    assert policy_for("manage_memory").risk_tier is RiskTier.STATEFUL
    assert policy_for("bash").risk_tier is RiskTier.PRIVILEGED


def test_read_only_allowed_to_everyone(monkeypatch):
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    d = authorize("web_search", owner=None)
    assert d.allowed is True
    assert d.requires_approval is False


def test_privileged_denied_to_non_admin_allowed_to_admin(monkeypatch):
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    assert authorize("bash", owner="alice").allowed is False
    assert authorize("a_brand_new_unlisted_tool", owner="alice").allowed is False  # default-deny
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: True)
    assert authorize("bash", owner="root").allowed is True


def test_stateful_allowed_to_authenticated_user_denied_to_anon(monkeypatch):
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    assert authorize("manage_memory", owner="alice").allowed is True  # own scope
    assert authorize("manage_memory", owner=None).allowed is False  # anonymous


def test_new_model_never_allows_a_denylist_privileged_tool_to_non_admin(monkeypatch):
    # Safety: every currently-blocked tool that the new model tiers PRIVILEGED
    # stays blocked for a non-admin (no weakening of the genuinely-privileged set).
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    for tool in ts.NON_ADMIN_BLOCKED_TOOLS:
        if ts.policy_for(tool).risk_tier is RiskTier.PRIVILEGED:
            assert authorize(tool, owner="alice").allowed is False, tool
