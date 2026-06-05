"""Trusted-mode grants for the sandbox (Phase 1.1d).

By default a sandboxed run sees only its per-session workspace and no network.
"Trusted mode" lets the operator explicitly widen that boundary via env vars
(consistent with SANDBOX_BACKEND; ADR-016). Grants are NEVER implicit.

- SANDBOX_MOUNTS: comma-separated `host:target[:ro|rw]` entries exposing real
  host directories. Mode defaults to `ro` (read grant != write grant). POSIX
  paths (the mounts only bite on the Linux/Docker backends).
    e.g. SANDBOX_MOUNTS="/data/in:/in:ro,/data/out:/out:rw"
- SANDBOX_ALLOW_NETWORK: truthy (1/true/yes/on) grants network to sandboxed runs.
"""

from __future__ import annotations

import os
from typing import List, Tuple

from .base import Mount

_TRUTHY = {"1", "true", "yes", "on"}


def parse_mounts(spec: str | None) -> List[Mount]:
    """Parse a SANDBOX_MOUNTS spec into Mounts. Malformed entries are skipped."""
    mounts: List[Mount] = []
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue  # need at least host:target
        source, target = parts[0], parts[1]
        mode = parts[2].strip().lower() if len(parts) > 2 else "ro"
        mounts.append(Mount(source=source, target=target, read_only=(mode != "rw")))
    return mounts


def network_granted(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def trusted_grants() -> Tuple[List[Mount], bool]:
    """The currently-configured (mounts, network) grants. Empty/False by default
    so the boundary stays closed unless the operator opens it explicitly."""
    return parse_mounts(os.getenv("SANDBOX_MOUNTS")), network_granted(
        os.getenv("SANDBOX_ALLOW_NETWORK")
    )
