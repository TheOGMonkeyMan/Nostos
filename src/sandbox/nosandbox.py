"""NoSandbox - the no-isolation backend (Phase 1.1b).

Runs commands DIRECTLY on the host, exactly like the legacy ShellService. There
is no boundary here, so `network` and `mounts` are accepted (for interface
parity) but ignored - there is nothing to grant across. This backend is
**dev-only** and reachable solely via an explicit `SANDBOX_BACKEND=none`; the
`auto` factory never selects it (default-deny - see factory.py).

It still honors the SandboxLimits timeout + output cap and, per the contract,
never raises into the agent loop: failures come back as a SandboxResult.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Union

from .base import Mount, SandboxLimits, SandboxResult


class NoSandbox:
    """Direct-host execution. No isolation. Explicit opt-in only."""

    async def run(
        self,
        cmd: Union[str, List[str]],
        *,
        cwd: str,
        limits: SandboxLimits,
        network: bool = False,
        mounts: Optional[List[Mount]] = None,
    ) -> SandboxResult:
        proc = None
        try:
            if isinstance(cmd, str):
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
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
