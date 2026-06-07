"""Human-in-the-loop approval for privileged tool calls (Phase 1.3a / ADR-027).

Interface + gate, default OFF (the `approvals_enabled` setting). When enabled and
an approval channel is present, a tool whose policy requires approval pauses for a
human decision before it runs:

- the broker emits an ``approval_request`` event via an INJECTED ``emit`` callback
  (decoupled from SSE, so this is deterministic and offline-testable),
- then awaits a decision resolved out of band by ``resolve(request_id, approved,
  scope)`` (which the approve/deny endpoint will call),
- and FAILS CLOSED: a timeout, a missing channel, or any error denies.

The human-facing ``args_summary`` is shown live but never persisted; the audit log
(ADR-026) records only the decision, with args hashed, so secrets are not stored.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# async emit(event: dict) -> None
EmitFn = Callable[[dict], Awaitable[None]]

DEFAULT_TIMEOUT_S = 300.0

SCOPE_ONCE = "once"
SCOPE_ALWAYS = "always"  # always-in-workspace for this session
SCOPE_DENY = "deny"


@dataclass(frozen=True)
class Decision:
    approved: bool
    scope: str  # once | always | deny


_pending: Dict[str, asyncio.Future] = {}


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _audit_decision(
    tool: Optional[str],
    owner: Optional[str],
    approved: bool,
    scope: str,
    session_id: Optional[str],
    note: str = "",
) -> None:
    try:
        from src.audit_log import record as _audit

        outcome = "ok" if approved else "blocked"
        detail = f"approval {'approved' if approved else 'denied'} scope={scope}"
        if note:
            detail += f" ({note})"
        _audit(tool, owner, outcome, session_id=session_id, detail=detail)
    except Exception:
        pass


async def request_approval(
    tool: Optional[str],
    owner: Optional[str],
    args_summary: str,
    *,
    emit: EmitFn,
    timeout: float = DEFAULT_TIMEOUT_S,
    session_id: Optional[str] = None,
) -> Decision:
    """Request human approval for a privileged tool. Awaits `resolve()` for this
    request id, or DENIES on timeout / emit failure / error (fail-closed)."""
    request_id = _new_request_id()
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[request_id] = fut
    try:
        try:
            await emit(
                {
                    "type": "approval_request",
                    "request_id": request_id,
                    "tool": tool or "",
                    "owner": owner or "",
                    "args_summary": (args_summary or "")[:500],
                }
            )
        except Exception as exc:
            logger.debug("approval emit failed, denying: %s", exc)
            _audit_decision(tool, owner, False, SCOPE_DENY, session_id, "emit failed")
            return Decision(False, SCOPE_DENY)

        try:
            approved, scope = await asyncio.wait_for(fut, timeout=timeout)
            # A denial is always scope=deny; an approval defaults to once.
            if not approved:
                scope = SCOPE_DENY
            elif not scope:
                scope = SCOPE_ONCE
            decision = Decision(bool(approved), scope)
        except asyncio.TimeoutError:
            decision = Decision(False, SCOPE_DENY)
        except Exception as exc:
            logger.debug("approval wait failed, denying: %s", exc)
            decision = Decision(False, SCOPE_DENY)
    finally:
        _pending.pop(request_id, None)

    _audit_decision(tool, owner, decision.approved, decision.scope, session_id)
    return decision


def resolve(request_id: str, approved: bool, scope: str = SCOPE_ONCE) -> bool:
    """Resolve a pending approval (called by the approve/deny endpoint, on the
    app's event loop). Returns True if a pending request was found and resolved.

    Must run on the same event loop as the awaiting `request_approval`; the
    FastAPI endpoint and the agent loop share one loop, so this is the case."""
    fut = _pending.get(request_id)
    if fut is None or fut.done():
        return False
    try:
        fut.set_result((bool(approved), scope))
        return True
    except asyncio.InvalidStateError:
        return False


def pending_count() -> int:
    """Number of approval requests currently awaiting a decision (diagnostics)."""
    return sum(1 for f in _pending.values() if not f.done())


def _reset_for_tests() -> None:
    _pending.clear()
