"""BubblewrapSandbox - Linux namespace isolation (Phase 1.1b).

Runs a command inside a bubblewrap (`bwrap`) sandbox: a fresh set of namespaces
with a read-only view of the system dirs, network unshared by default, and ONLY
the per-call `cwd` plus explicitly-granted `mounts` writable/visible. This is the
default backend on Linux (ADR-002).

Scope of this version (see DECISIONS.md ADR-013):
- Enforced: filesystem write-isolation (host is read-only; writes land only in
  cwd / granted rw mounts), network isolation (no egress unless network=True),
  wall-clock timeout, output truncation.
- DEFERRED: hard memory/pid caps. Reliable RSS/pid limits need cgroups, which an
  unprivileged process on a CI runner cannot delegate, and RLIMIT_AS would kill
  ordinary interpreters (Python over-reserves virtual memory). Those land with a
  cgroup-capable path (DockerSandbox / cgroup-v2). `--unshare-pid` still gives a
  private pid namespace; it just is not a hard count cap.

Requires unprivileged user namespaces (bwrap creates a user namespace to gain
caps over the namespaces it makes). Most distros ship this enabled or provide a
setuid `bwrap`; ubuntu 24.04 restricts it via AppArmor
(`kernel.apparmor_restrict_unprivileged_userns`), which must be relaxed or a
profile installed for an unprivileged bwrap to set up its net namespace.

Never raises into the agent loop: failures come back as a SandboxResult.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import List, Optional, Union

from .base import Mount, SandboxLimits, SandboxResult

_BWRAP = shutil.which("bwrap")


class BubblewrapSandbox:
    """Linux namespace sandbox. Available only where `bwrap` exists."""

    @staticmethod
    def is_available() -> bool:
        return sys.platform.startswith("linux") and _BWRAP is not None

    def __init__(self) -> None:
        if not self.is_available():
            raise RuntimeError("BubblewrapSandbox requires Linux and the 'bwrap' binary")

    def _bwrap_argv(self, cwd: str, network: bool, mounts: Optional[List[Mount]]) -> List[str]:
        argv: List[str] = [
            _BWRAP,  # type: ignore[list-item]  # guarded by is_available()
            # Read-only view of the system so commands can find their interpreter
            # and libraries, but cannot modify the host.
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind-try",
            "/bin",
            "/bin",
            "--ro-bind-try",
            "/sbin",
            "/sbin",
            "--ro-bind-try",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--ro-bind-try",
            "/etc",
            "/etc",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            # Isolate every namespace. Net is re-shared below only when granted.
            "--unshare-user",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--die-with-parent",
            "--new-session",
            # The workspace is the only writable host path by default.
            "--bind",
            cwd,
            cwd,
            "--chdir",
            cwd,
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            cwd,
        ]
        if not network:
            argv += ["--unshare-net"]
        for m in mounts or []:
            argv += (["--ro-bind"] if m.read_only else ["--bind"]) + [m.source, m.target]
        return argv

    async def run(
        self,
        cmd: Union[str, List[str]],
        *,
        cwd: str,
        limits: SandboxLimits,
        network: bool = False,
        mounts: Optional[List[Mount]] = None,
    ) -> SandboxResult:
        argv = self._bwrap_argv(cwd, network, mounts)
        if isinstance(cmd, str):
            argv += ["/bin/sh", "-c", cmd]
        else:
            argv += list(cmd)

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=limits.timeout_s)
            out = out_b.decode(errors="replace")
            err = err_b.decode(errors="replace")
            cap = limits.max_output_bytes
            truncated = len(out) > cap or len(err) > cap
            return SandboxResult(
                stdout=out[:cap],
                stderr=err[:cap],
                exit_code=proc.returncode if proc.returncode is not None else -1,
                truncated=truncated,
            )
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            return SandboxResult(
                stdout="",
                stderr=f"Command timed out after {limits.timeout_s}s",
                exit_code=-1,
                timed_out=True,
            )
        except Exception as exc:  # never raise into the agent loop
            return SandboxResult(stdout="", stderr=str(exc), exit_code=-1)
