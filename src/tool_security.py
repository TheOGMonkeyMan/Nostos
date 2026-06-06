"""Server-side tool safety policy."""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Optional, Set

logger = logging.getLogger(__name__)


def is_public_blocked_tool(tool_name: Optional[str]) -> bool:
    """True when a non-admin user must not execute this tool: i.e. its policy
    tier is PRIVILEGED. Unregistered tools and `mcp__*` default to PRIVILEGED
    (default-deny), so a tool added without a policy is closed, not exposed. The
    risk-tier registry below is the single source of truth - no denylist
    (Phase 1.2 / ADR-003). STATEFUL + READ_ONLY tools are NOT blocked here
    (stateful tools are the user's own scope)."""
    if not tool_name:
        return False
    return policy_for(tool_name).risk_tier is RiskTier.PRIVILEGED


def owner_is_admin_or_single_user(
    owner: Optional[str],
    origin: "Optional[RequestOrigin]" = None,
) -> bool:
    """Return True for admins, or - in unconfigured first-run mode - for a
    LOOPBACK caller only.

    Origin-aware fail-open (Phase 1.2c / ADR-021): the "auth not configured yet
    => treat as admin" convenience now applies to LOOPBACK requests only. A
    REMOTE caller on an unconfigured instance fails CLOSED (not admin), so an
    instance accidentally bound to a non-loopback interface with auth unset does
    not hand admin (shell/python/email/secrets) to the network.

    Defense in depth: `auth_helpers.require_user` already rejects non-loopback
    callers in unconfigured mode at the route layer; this hardens the tool layer
    if that gate is bypassed (LOCALHOST_BYPASS, AUTH_ENABLED=false, SSRF from a
    sibling service).

    `origin` defaults to LOOPBACK so existing callers that do not yet thread the
    request origin keep their current behaviour; `authorize()` passes the real
    origin. Wiring the live tool gate to pass a real origin is a follow-up."""
    if origin is None:
        origin = RequestOrigin.LOOPBACK
    try:
        from core.auth import AuthManager

        auth = AuthManager()
        if not auth.is_configured:
            # First-run convenience, but never for remote callers.
            return origin is RequestOrigin.LOOPBACK
        return bool(owner and auth.is_admin(owner))
    except Exception as exc:
        logger.warning("Unable to evaluate owner admin status: %s", exc)
        return False


def blocked_tools_for_owner(owner: Optional[str]) -> Set[str]:
    """Registered tools to hide/disable for this owner: the PRIVILEGED ones for
    non-admins (admins see everything). Unknown tools are blocked at execution by
    is_public_blocked_tool's default-deny."""
    if owner_is_admin_or_single_user(owner):
        return set()
    return {name for name, pol in _TOOL_POLICIES.items() if pol.risk_tier is RiskTier.PRIVILEGED}


# ===========================================================================
# Capability authorization model (Phase 1.2, ADR-003) - PROPOSED, NOT WIRED.
#
# An allowlist driven by per-tool risk metadata that REPLACES the denylist
# above: any unregistered tool defaults to PRIVILEGED/denied (so a new tool
# added without a policy is closed, not exposed). This is built alongside the
# live denylist for review; `authorize()` is not yet on the live decision path
# (is_public_blocked_tool / blocked_tools_for_owner are unchanged). A separate
# migration wires it, and the fail-open in owner_is_admin_or_single_user is
# tightened in its own follow-up.
# ===========================================================================


class RiskTier(enum.Enum):
    READ_ONLY = "read_only"  # no state change, no external reach -> all users
    STATEFUL = "stateful"  # changes the user's own state -> authenticated users
    PRIVILEGED = "privileged"  # shell/python, email, serving, settings, secrets -> admin


class Role(enum.Enum):
    USER = "user"
    ADMIN = "admin"


class RequestOrigin(enum.Enum):
    LOOPBACK = "loopback"
    REMOTE = "remote"


@dataclass(frozen=True)
class ToolPolicy:
    # Default-deny: an unknown tool is privileged + admin-only + needs approval.
    risk_tier: RiskTier = RiskTier.PRIVILEGED
    min_role: Role = Role.ADMIN
    requires_approval: bool = True


@dataclass(frozen=True)
class Decision:
    allowed: bool
    requires_approval: bool
    reason: str


def _policy(tier: RiskTier, *, approval: Optional[bool] = None) -> ToolPolicy:
    role = Role.ADMIN if tier is RiskTier.PRIVILEGED else Role.USER
    if approval is None:
        approval = tier is RiskTier.PRIVILEGED
    return ToolPolicy(risk_tier=tier, min_role=role, requires_approval=approval)


_RO = _policy(RiskTier.READ_ONLY)
_ST = _policy(RiskTier.STATEFUL)
_PV = _policy(RiskTier.PRIVILEGED)

# PROPOSED tiering of the current tool set (src/tool_schemas.py) + the denylist's
# extra tools (vault_*, search_chats). Tiers are a judgment call for review; the
# registry, not a denylist, is the source of truth (contract: "additions are
# allow-decisions, never block-lists").
_TOOL_POLICIES: dict[str, ToolPolicy] = {
    # READ_ONLY - no state change, no sensitive data
    "web_search": _RO,
    "web_fetch": _RO,
    "list_models": _RO,
    "list_sessions": _RO,
    "list_served_models": _RO,
    "list_downloads": _RO,
    "list_cookbook_servers": _RO,
    "list_serve_presets": _RO,
    "list_cached_models": _RO,
    "search_hf_models": _RO,
    "ui_control": _RO,
    "ask_teacher": _RO,
    # STATEFUL - the user's own data/scope
    "create_document": _ST,
    "edit_document": _ST,
    "update_document": _ST,
    "suggest_document": _ST,
    "manage_documents": _ST,
    "search_chats": _ST,
    "chat_with_model": _ST,
    "create_session": _ST,
    "send_to_session": _ST,
    "manage_session": _ST,
    "pipeline": _ST,
    "manage_memory": _ST,
    "manage_tasks": _ST,
    "manage_skills": _ST,
    "manage_calendar": _ST,
    "trigger_research": _ST,
    "edit_image": _ST,
    # PRIVILEGED - host/runtime/external/secrets -> admin only
    "bash": _PV,
    "python": _PV,
    "read_file": _PV,
    "write_file": _PV,
    "manage_endpoints": _PV,
    "manage_mcp": _PV,
    "manage_webhooks": _PV,
    "manage_tokens": _PV,
    "manage_settings": _PV,
    "api_call": _PV,
    "app_api": _PV,
    "download_model": _PV,
    "serve_model": _PV,
    "stop_served_model": _PV,
    "cancel_download": _PV,
    "adopt_served_model": _PV,
    "serve_preset": _PV,
    "send_email": _PV,
    "reply_to_email": _PV,
    "bulk_email": _PV,
    "delete_email": _PV,
    "archive_email": _PV,
    "mark_email_read": _PV,
    "read_email": _PV,
    "list_emails": _PV,
    "list_email_accounts": _PV,
    "resolve_contact": _PV,
    "manage_contact": _PV,
    "vault_search": _PV,
    "vault_get": _PV,
    "vault_unlock": _PV,
}

# Default for any unregistered tool (including mcp__*): privileged + denied.
_DEFAULT_POLICY = ToolPolicy()


def policy_for(tool_name: Optional[str]) -> ToolPolicy:
    """The policy for a tool. Unregistered / mcp__* -> default-deny (privileged)."""
    if not tool_name:
        return _DEFAULT_POLICY
    return _TOOL_POLICIES.get(tool_name, _DEFAULT_POLICY)


def authorize(
    tool_name: Optional[str],
    owner: Optional[str],
    origin: RequestOrigin = RequestOrigin.LOOPBACK,
) -> Decision:
    """Allow/deny a tool for an owner (PROPOSED model; not yet on the live path).

    READ_ONLY  -> any caller. STATEFUL -> authenticated user (own scope).
    PRIVILEGED -> admin only. Unknown tool -> privileged -> denied.
    The unconfigured fail-open is now ORIGIN-AWARE (Phase 1.2c / ADR-021): a
    REMOTE caller on an unconfigured instance is NOT auto-admin, so a privileged
    tool is denied to the network even when auth is unset. `origin` is threaded
    into the admin check here; wiring the LIVE tool gate to pass a real origin
    is a follow-up.
    """
    policy = policy_for(tool_name)
    tier = policy.risk_tier
    is_admin = owner_is_admin_or_single_user(owner, origin)

    if tier is RiskTier.READ_ONLY:
        return Decision(True, policy.requires_approval, "read-only tool")
    if tier is RiskTier.STATEFUL:
        if is_admin or owner:
            return Decision(True, policy.requires_approval, "stateful tool, own scope")
        return Decision(
            False, policy.requires_approval, "stateful tool requires an authenticated user"
        )
    # PRIVILEGED (and the default for unknown tools)
    if is_admin:
        return Decision(True, policy.requires_approval, "privileged tool, admin")
    return Decision(False, policy.requires_approval, "privileged tool requires admin")
