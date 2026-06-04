"""Per-session sandbox workspace lifecycle (Phase 1.1b).

The sandbox owns `data/workspaces/<session>/` - the default writable area a run
gets when no extra mounts are granted. Session ids are sanitized to a single
path segment so a hostile id (e.g. "../../etc") can never escape the workspaces
root.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# Relative to the process CWD, consistent with the app's other data paths
# (e.g. core.database's sqlite default). Resolved to absolute on use.
WORKSPACES_ROOT = Path("data") / "workspaces"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_segment(session_id: str) -> str:
    """Collapse a session id into one safe path segment (no separators, no
    traversal). Empty / all-unsafe ids fall back to 'default'."""
    seg = _UNSAFE.sub("_", session_id or "").strip("._")[:128]
    return seg or "default"


def workspace_path(session_id: str) -> Path:
    return WORKSPACES_ROOT / _safe_segment(session_id)


def ensure_workspace(session_id: str) -> str:
    """Create (idempotently) and return the absolute workspace dir."""
    p = workspace_path(session_id)
    p.mkdir(parents=True, exist_ok=True)
    return str(p.resolve())


def clean_workspace(session_id: str) -> None:
    """Remove a session's workspace. Refuses to delete anything that does not
    resolve to a child of the workspaces root (defense in depth)."""
    p = workspace_path(session_id).resolve()
    root = WORKSPACES_ROOT.resolve()
    if root in p.parents and p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
