"""Sandbox package - isolation for agent-invoked code/commands.

Re-exports the interface from base.py. Concrete backends (1.1b) and the factory
that selects one per SANDBOX_BACKEND will be re-exported here as they land.
"""

from .base import Mount, Sandbox, SandboxLimits, SandboxResult
from .bubblewrap import BubblewrapSandbox
from .config import network_granted, parse_mounts, trusted_grants
from .factory import SandboxUnavailable, get_sandbox, resolve_backend_name
from .nosandbox import NoSandbox
from .pathjail import PathJailSubprocess
from .workspace import clean_workspace, ensure_workspace, workspace_path

__all__ = [
    "Mount",
    "SandboxLimits",
    "SandboxResult",
    "Sandbox",
    "NoSandbox",
    "BubblewrapSandbox",
    "PathJailSubprocess",
    "get_sandbox",
    "resolve_backend_name",
    "SandboxUnavailable",
    "ensure_workspace",
    "clean_workspace",
    "workspace_path",
    "trusted_grants",
    "parse_mounts",
    "network_granted",
]
