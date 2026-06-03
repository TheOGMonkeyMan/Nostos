"""Sandbox package - isolation for agent-invoked code/commands.

Re-exports the interface from base.py. Concrete backends (1.1b) and the factory
that selects one per SANDBOX_BACKEND will be re-exported here as they land.
"""

from .base import Mount, Sandbox, SandboxLimits, SandboxResult

__all__ = ["Mount", "SandboxLimits", "SandboxResult", "Sandbox"]
