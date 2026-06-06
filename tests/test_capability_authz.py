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
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner, origin=None:False)
    d = authorize("web_search", owner=None)
    assert d.allowed is True
    assert d.requires_approval is False


def test_privileged_denied_to_non_admin_allowed_to_admin(monkeypatch):
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner, origin=None:False)
    assert authorize("bash", owner="alice").allowed is False
    assert authorize("a_brand_new_unlisted_tool", owner="alice").allowed is False  # default-deny
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner, origin=None:True)
    assert authorize("bash", owner="root").allowed is True


def test_stateful_allowed_to_authenticated_user_denied_to_anon(monkeypatch):
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner, origin=None:False)
    assert authorize("manage_memory", owner="alice").allowed is True  # own scope
    assert authorize("manage_memory", owner=None).allowed is False  # anonymous


def test_privileged_tools_denied_to_non_admin(monkeypatch):
    # The genuinely-privileged tools (host/runtime/external/secrets) + unknown +
    # mcp__* are denied to a non-admin. (The denylist constant is gone in 1.2;
    # the registry is the source of truth.)
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner, origin=None:False)
    for tool in (
        "bash",
        "python",
        "read_file",
        "write_file",
        "send_email",
        "delete_email",
        "manage_settings",
        "manage_tokens",
        "vault_get",
        "serve_model",
        "api_call",
        "app_api",
        "a_brand_new_unlisted_tool",
        "mcp__email__send_email",
    ):
        assert authorize(tool, owner="alice").allowed is False, tool


# --- Phase 1.2c: origin-aware unconfigured fail-open (ADR-021) --------------


def _stub_unconfigured_auth(monkeypatch):
    """Make `from core.auth import AuthManager` yield an UNCONFIGURED manager."""
    import sys
    import types

    mod = types.ModuleType("core.auth")

    class _Unconfigured:
        is_configured = False

        def is_admin(self, username):
            return False

    mod.AuthManager = _Unconfigured
    monkeypatch.setitem(sys.modules, "core.auth", mod)


def test_unconfigured_fail_open_is_loopback_only(monkeypatch):
    from src.tool_security import RequestOrigin, owner_is_admin_or_single_user

    _stub_unconfigured_auth(monkeypatch)
    # First-run convenience holds on loopback...
    assert owner_is_admin_or_single_user("anyone", RequestOrigin.LOOPBACK) is True
    assert owner_is_admin_or_single_user(None, RequestOrigin.LOOPBACK) is True
    # ...the default origin (un-threaded callers) stays loopback = unchanged...
    assert owner_is_admin_or_single_user("anyone") is True
    # ...but a REMOTE caller on an unconfigured instance fails CLOSED.
    assert owner_is_admin_or_single_user("anyone", RequestOrigin.REMOTE) is False


def test_authorize_denies_privileged_to_remote_when_unconfigured(monkeypatch):
    from src.tool_security import RequestOrigin, authorize

    _stub_unconfigured_auth(monkeypatch)
    # Loopback first-run: privileged tool allowed (single-user convenience).
    assert authorize("bash", owner=None, origin=RequestOrigin.LOOPBACK).allowed is True
    # Remote caller on the same unconfigured instance: privileged tools DENIED.
    assert authorize("bash", owner=None, origin=RequestOrigin.REMOTE).allowed is False
    assert authorize("send_email", owner=None, origin=RequestOrigin.REMOTE).allowed is False


def test_authorize_threads_origin_into_admin_check(monkeypatch):
    from src.tool_security import RequestOrigin, authorize

    seen = []
    monkeypatch.setattr(
        ts,
        "owner_is_admin_or_single_user",
        lambda owner, origin=None: (seen.append(origin) or False),
    )
    authorize("bash", owner="x", origin=RequestOrigin.REMOTE)
    assert seen and seen[-1] is RequestOrigin.REMOTE
