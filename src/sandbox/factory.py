"""Sandbox backend selection (Phase 1.1b).

`SANDBOX_BACKEND` = auto (default) | bubblewrap | pathjail | docker | none.

Default-deny: `auto` resolves to the per-OS backend and RAISES SandboxUnavailable
if that backend is not implemented yet, rather than silently falling back to
no-isolation. `none` (NoSandbox, direct host) is reachable ONLY by naming it
explicitly. As real backends land (BubblewrapSandbox, PathJailSubprocess,
DockerSandbox) they register in _BACKENDS and `auto` starts resolving to them.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Dict

from .base import Sandbox
from .nosandbox import NoSandbox


class SandboxUnavailable(RuntimeError):
    """Raised when the requested/auto-selected backend is not available. The
    caller must fail closed - never degrade to unsandboxed execution."""


# Registry of implemented backends. NoSandbox is intentionally the only entry
# until the isolating backends land; auto never points here.
_BACKENDS: Dict[str, Callable[[], Sandbox]] = {
    "none": NoSandbox,
}


def _auto_backend() -> str:
    """The backend `auto` should prefer for this OS (per ADR-002)."""
    if sys.platform.startswith("linux"):
        return "bubblewrap"
    if sys.platform == "darwin":
        return "pathjail"
    return "pathjail"  # windows + anything else


def resolve_backend_name(backend: str | None = None) -> str:
    name = (backend or os.getenv("SANDBOX_BACKEND") or "auto").strip().lower()
    return _auto_backend() if name == "auto" else name


def get_sandbox(backend: str | None = None) -> Sandbox:
    """Return a Sandbox for the requested/configured backend.

    Raises SandboxUnavailable for any backend not yet implemented (default-deny).
    """
    name = resolve_backend_name(backend)
    factory = _BACKENDS.get(name)
    if factory is None:
        raise SandboxUnavailable(
            f"sandbox backend '{name}' is not available "
            f"(implemented: {sorted(_BACKENDS)}). "
            f"Set SANDBOX_BACKEND=none for dev-only direct-host execution."
        )
    return factory()
