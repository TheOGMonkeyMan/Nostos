"""Sandbox interface (Phase 1.1a) - the contract every backend implements.

Isolates execution of agent-invoked code/commands (shell, python) so a malicious
or mistaken call cannot harm the host beyond an explicitly-granted boundary,
while preserving the power-user ability to let the agent act on real directories
when trusted. See contracts/sandbox.md and DECISIONS.md ADR-002.

This module defines ONLY the interface + value types. Concrete backends
(BubblewrapSandbox, PathJailSubprocess, DockerSandbox, NoSandbox) land in later
increments (1.1b); routing the shell + python-exec paths through it is 1.1c.

Hard rules carried from the contract:
- Network and extra mounts are NEVER implicit. Both are explicit, per-call.
- A backend must never raise into the agent loop: limit breaches/timeouts come
  back as a SandboxResult with a non-zero exit_code and a clear stderr.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Union, runtime_checkable


@dataclass(frozen=True)
class Mount:
    """An explicit, user-granted exposure of a real host directory.

    Mounts are the *only* way real paths (beyond the per-session workspace) enter
    the sandbox. Default read-only so a grant to read does not also grant write.
    """

    source: str  # absolute host path
    target: str  # path as seen inside the sandbox
    read_only: bool = True


@dataclass
class SandboxLimits:
    """Resource ceilings applied to a single run. Defaults preserve the existing
    shell-service limits (30s / 200KB) and add memory/pid/cpu caps that the
    namespace and container backends enforce (best-effort on the path-jail
    fallback)."""

    timeout_s: int = 30
    max_output_bytes: int = 200_000
    memory_mb: Optional[int] = 512
    pids: Optional[int] = 256
    cpus: Optional[float] = 1.0


@dataclass
class SandboxResult:
    """Outcome of a run. `timed_out` and `truncated` are flags, not errors - a
    health-of-the-agent-loop concern, so callers can react without try/except."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    truncated: bool = False


@runtime_checkable
class Sandbox(Protocol):
    """Structural interface for every backend. `runtime_checkable` so callers /
    tests can assert an object satisfies it without importing a concrete class."""

    async def run(
        self,
        cmd: Union[str, List[str]],
        *,
        cwd: str,
        limits: SandboxLimits,
        network: bool = False,
        mounts: Optional[List[Mount]] = None,
    ) -> SandboxResult:
        """Execute `cmd` with `cwd` as the working directory under `limits`.

        network defaults to denied; mounts default to none. Neither is ever
        granted implicitly. Returns a SandboxResult; must not raise into the
        agent loop on limit breach/timeout.
        """
        ...


__all__ = ["Mount", "SandboxLimits", "SandboxResult", "Sandbox"]
