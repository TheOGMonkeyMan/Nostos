"""Sandbox backend selection (Phase 1.1b; auto-degrade added 1.1e / ADR-020).

`SANDBOX_BACKEND` = auto (default) | bubblewrap | pathjail | docker | none.

`auto` resolves to the STRONGEST AVAILABLE jail for this OS and never falls back
to no-isolation: on Linux it prefers bubblewrap and DEGRADES to pathjail (the
documented weak fallback, ADR-014) when bwrap is unavailable (e.g. the stock
Ubuntu 24.04 unprivileged-userns restriction); on Mac/Windows it is pathjail.
NoSandbox (direct host) is NEVER selected by `auto` - it is reachable only by
naming `none` explicitly. An EXPLICIT backend name still fails closed: naming
`bubblewrap` on a host without it raises SandboxUnavailable rather than degrading
(the caller asked for that boundary specifically).
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Dict

from .base import Sandbox
from .bubblewrap import BubblewrapSandbox
from .nosandbox import NoSandbox
from .pathjail import PathJailSubprocess


class SandboxUnavailable(RuntimeError):
    """Raised when the requested/auto-selected backend is not available. The
    caller must fail closed - never degrade to unsandboxed execution."""


# Registry of implemented backends. A backend may expose a static is_available()
# (e.g. bubblewrap needs Linux + the bwrap binary); if it reports unavailable on
# this host, get_sandbox fails closed rather than constructing it.
_BACKENDS: Dict[str, Callable[[], Sandbox]] = {
    "none": NoSandbox,
    "bubblewrap": BubblewrapSandbox,
    "pathjail": PathJailSubprocess,
}


def _auto_backend() -> str:
    """The backend `auto` PREFERS for this OS (per ADR-002). This is the
    strongest jail for the platform regardless of host availability; the actual
    selection (with degradation) is `resolve_available_backend`."""
    if sys.platform.startswith("linux"):
        return "bubblewrap"
    if sys.platform == "darwin":
        return "pathjail"
    return "pathjail"  # windows + anything else


def _auto_preferences() -> list[str]:
    """Ordered `auto` candidates for this OS, strongest first. NoSandbox is
    deliberately ABSENT: `auto` never degrades to no-isolation. pathjail is the
    always-available weak floor (ADR-014), so this list always ends usable."""
    if sys.platform.startswith("linux"):
        return ["bubblewrap", "pathjail"]
    return ["pathjail"]  # mac / windows + anything else


def _is_available(name: str) -> bool:
    """True if backend `name` is registered AND available on this host."""
    factory = _BACKENDS.get(name)
    if factory is None:
        return False
    check = getattr(factory, "is_available", None)
    return True if check is None else bool(check())


def resolve_backend_name(backend: str | None = None) -> str:
    """The per-OS PREFERRED concrete name for `auto` (no availability check).
    Kept for callers that want the intended backend; `get_sandbox` /
    `resolve_available_backend` apply degradation + fail-closed on top."""
    name = (backend or os.getenv("SANDBOX_BACKEND") or "auto").strip().lower()
    return _auto_backend() if name == "auto" else name


def resolve_available_backend(backend: str | None = None) -> str:
    """The concrete backend `get_sandbox` will actually construct.

    For `auto` (the default): the strongest AVAILABLE jail for this OS, degrading
    down `_auto_preferences()` (bubblewrap -> pathjail on Linux) and NEVER to
    NoSandbox. For an EXPLICIT name: that name verbatim (get_sandbox then fails
    closed if it is unavailable - an explicit request is not silently degraded).
    """
    raw = (backend or os.getenv("SANDBOX_BACKEND") or "auto").strip().lower()
    if raw != "auto":
        return raw
    for candidate in _auto_preferences():
        if _is_available(candidate):
            return candidate
    # pathjail.is_available() is always True, so this is effectively unreachable;
    # return the weak floor rather than ever yielding no-isolation.
    return _auto_preferences()[-1]


def get_sandbox(backend: str | None = None) -> Sandbox:
    """Return a Sandbox for the requested/configured backend.

    `auto` degrades to the strongest available jail (never no-isolation). An
    explicit backend that is unimplemented or unavailable raises
    SandboxUnavailable (fail closed) - the caller must not run unsandboxed.
    """
    name = resolve_available_backend(backend)
    factory = _BACKENDS.get(name)
    if factory is None:
        raise SandboxUnavailable(
            f"sandbox backend '{name}' is not implemented "
            f"(implemented: {sorted(_BACKENDS)}). "
            f"Set SANDBOX_BACKEND=none for dev-only direct-host execution."
        )
    if not _is_available(name):
        raise SandboxUnavailable(
            f"sandbox backend '{name}' is implemented but not available on this "
            f"host (e.g. bubblewrap requires Linux + the 'bwrap' binary). "
            f"Use SANDBOX_BACKEND=auto to degrade to the weak pathjail fallback."
        )
    return factory()
