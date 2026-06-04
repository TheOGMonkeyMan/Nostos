"""PathJailSubprocess - the weak Mac/Windows fallback (Phase 1.1b).

Platforms without bubblewrap (macOS, Windows) have no unprivileged namespace
equivalent, so this backend does what it CAN: run the command from a clean
working directory with a scrubbed, minimal environment, a wall-clock timeout, an
output cap, and (on POSIX) rlimit backstops.

HONEST LIMITATIONS (see DECISIONS.md ADR-014): this is NOT a strong filesystem
or network boundary. A determined command can still read/write outside the
workspace and reach the network - that needs OS-level sandboxing this backend
does not yet apply. It is selected by `auto` on Mac/Windows only because a clean
cwd + scrubbed env + resource caps is meaningfully safer than raw direct-host
execution. For REAL isolation on those platforms, use `SANDBOX_BACKEND=docker`
(DockerSandbox), or run on Linux (BubblewrapSandbox). A future increment may add
macOS `sandbox-exec` write-deny for a genuine filesystem boundary on Mac.

Never raises into the agent loop: failures come back as a SandboxResult.
"""

from __future__ import annotations

import asyncio
import os
from typing import Dict, List, Optional, Union

from .base import Mount, SandboxLimits, SandboxResult


class PathJailSubprocess:
    """Restricted-but-weak subprocess fallback. Available everywhere."""

    @staticmethod
    def is_available() -> bool:
        return True

    def _scrubbed_env(self, cwd: str) -> Dict[str, str]:
        """A minimal environment so host secrets in os.environ do not leak into
        the command. HOME/TMP point at the workspace."""
        env: Dict[str, str] = {
            "HOME": cwd,
            "PATH": os.environ.get("PATH", ""),
        }
        if os.name == "nt":
            # A few vars Windows binaries genuinely need to start.
            for key in (
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "NUMBER_OF_PROCESSORS",
                "PROCESSOR_ARCHITECTURE",
            ):
                if key in os.environ:
                    env[key] = os.environ[key]
            env["TEMP"] = cwd
            env["TMP"] = cwd
        else:
            env["TMPDIR"] = cwd
        return env

    def _posix_preexec(self, limits: SandboxLimits):
        """POSIX-only rlimit backstops (pids + cpu time). Not a hard memory cap
        (see ADR-013). Returns None on Windows, which has no preexec_fn."""
        if os.name == "nt":
            return None

        def _apply() -> None:
            import resource

            if limits.pids:
                try:
                    resource.setrlimit(resource.RLIMIT_NPROC, (limits.pids, limits.pids))
                except (ValueError, OSError):
                    pass
            # CPU-time backstop a little above the wall-clock timeout.
            cpu = int(limits.timeout_s) + 5
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            except (ValueError, OSError):
                pass

        return _apply

    async def run(
        self,
        cmd: Union[str, List[str]],
        *,
        cwd: str,
        limits: SandboxLimits,
        network: bool = False,
        mounts: Optional[List[Mount]] = None,
    ) -> SandboxResult:
        # network/mounts are accepted for interface parity. This backend cannot
        # enforce a network boundary; that is a documented limitation.
        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": cwd,
            "env": self._scrubbed_env(cwd),
        }
        preexec = self._posix_preexec(limits)
        if preexec is not None:
            kwargs["preexec_fn"] = preexec

        proc = None
        try:
            if isinstance(cmd, str):
                proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
            else:
                proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
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
