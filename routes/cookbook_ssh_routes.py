"""Cookbook ssh-key routes (ADR-046, Phase 2.2).

The GET/POST /api/cookbook/ssh-key endpoints + their 3 self-contained ssh path
helpers (_cookbook_ssh_dir / _cookbook_ssh_key_path / _read_cookbook_public_key),
split verbatim out of routes/cookbook_routes.py::setup_cookbook_routes(). The
helpers are used only by these routes, so they move with the group; the registrar
takes only the router.
"""

import asyncio
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from fastapi import Request

from core.middleware import require_admin
from core.platform_compat import (
    IS_WINDOWS,
    detached_popen_kwargs,
    find_bash,
    safe_chmod,
    which_tool,
)
from routes.shell_routes import TMUX_LOG_DIR


def register_ssh_key_routes(router):
    def _cookbook_ssh_dir() -> Path:
        # The Docker image keeps cookbook keys under /app/.ssh; that path only
        # exists inside the container. On Windows (and any non-container host)
        # fall back to the user profile's ~/.ssh, which OpenSSH on Win10+ uses.
        if not IS_WINDOWS:
            app_ssh = Path("/app/.ssh")
            if Path("/app").exists():
                return app_ssh
        return Path.home() / ".ssh"

    def _cookbook_ssh_key_path() -> Path:
        return _cookbook_ssh_dir() / "id_ed25519"

    def _read_cookbook_public_key() -> str:
        pub = _cookbook_ssh_key_path().with_suffix(".pub")
        if not pub.exists():
            return ""
        return pub.read_text(encoding="utf-8", errors="replace").strip()

    @router.get("/api/cookbook/ssh-key")
    async def get_cookbook_ssh_key(request: Request):
        require_admin(request)
        public_key = _read_cookbook_public_key()
        return {
            "configured": bool(public_key),
            "public_key": public_key,
        }

    @router.post("/api/cookbook/ssh-key")
    async def generate_cookbook_ssh_key(request: Request):
        require_admin(request)
        ssh_dir = _cookbook_ssh_dir()
        key_path = _cookbook_ssh_key_path()
        ssh_dir.mkdir(parents=True, exist_ok=True)
        # safe_chmod no-ops on Windows (~/.ssh is already ACL-restricted to the
        # user profile); applies 0o700 on POSIX.
        safe_chmod(ssh_dir, 0o700)
        if not key_path.exists():
            # ssh-keygen ships with the OpenSSH client on Win10+; resolve it via
            # which_tool so the .exe is found even when PATHEXT is unusual.
            ssh_keygen = which_tool("ssh-keygen") or "ssh-keygen"
            proc = await asyncio.create_subprocess_exec(
                ssh_keygen, "-t", "ed25519", "-N", "", "-C", "odysseus-cookbook", "-f", str(key_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace").strip()[-500:]
                return {"ok": False, "error": detail or "Failed to generate SSH key"}
        safe_chmod(key_path, 0o600)
        safe_chmod(key_path.with_suffix(".pub"), 0o644)
        return {"ok": True, "public_key": _read_cookbook_public_key()}

    def _user_shell_path_bootstrap() -> list[str]:
        return [
            'ODYSSEUS_USER_SHELL="${SHELL:-}"',
            'if [ -n "$ODYSSEUS_USER_SHELL" ] && [ -x "$ODYSSEUS_USER_SHELL" ]; then',
            '  ODYSSEUS_USER_PATH="$("$ODYSSEUS_USER_SHELL" -ic \'printf "__ODYSSEUS_PATH__%s\\n" "$PATH"\' 2>/dev/null | sed -n \'s/^__ODYSSEUS_PATH__//p\' | tail -n 1 || true)"',
            '  if [ -n "$ODYSSEUS_USER_PATH" ]; then export PATH="$ODYSSEUS_USER_PATH:$PATH"; fi',
            'fi',
        ]

    def _needs_binary(cmd: str, binary: str) -> bool:
        return bool(re.search(rf"(^|[\s;&|()]){re.escape(binary)}($|[\s;&|()])", cmd or ""))

    def _missing_binary_message(binary: str, target: str) -> str:
        if binary == "tmux":
            return (
                f"tmux is required for Cookbook background downloads/serves on {target}. "
                "Install it with your OS package manager, or run Cookbook server setup for that server."
            )
        if binary == "docker":
            return (
                f"Docker is required by this Cookbook launch command on {target}, but the docker CLI was not found. "
                "Install Docker and make sure this user can run `docker`, then retry."
            )
        return f"{binary} is required on {target}, but it was not found."

    async def _remote_binary_available(remote: str, ssh_port: str | None, binary: str, *, windows: bool = False) -> bool:
        _port = ssh_port or ""
        _pf = ["-p", _port] if _port and _port != "22" else []
        if windows:
            check = f"powershell -NoProfile -Command \"if (Get-Command {binary} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 127 }}\""
        else:
            check = f"command -v {shlex.quote(binary)} >/dev/null 2>&1"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=no",
                *_pf, remote, check,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0
        except Exception:
            return False

    async def _binary_available(binary: str, remote: str | None, ssh_port: str | None, *, windows: bool = False) -> bool:
        if remote:
            return await _remote_binary_available(remote, ssh_port, binary, windows=windows)
        return shutil.which(binary) is not None

    def _launch_local_detached(session_id: str, bash_lines: list[str]) -> dict:
        """Windows-native stand-in for a LOCAL tmux session (tmux doesn't exist
        on Windows). Mirrors shell_routes._generate_win_detached / bg_jobs.launch:
        runs the wrapper detached so it survives a browser/SSE disconnect (the
        whole point of the tmux feature for long downloads/serves), writing a
        <session>.log the status poller tails and a <session>.pid for liveness.

        `bash_lines` is the same bash wrapper used on POSIX. Prefers Git Bash
        for full command-syntax parity; falls back to a cmd.exe wrapper that
        runs the script through whatever bash is reachable, else best-effort
        directly (simple commands only). Returns the launched job record."""
        log_path = TMUX_LOG_DIR / f"{session_id}.log"
        pid_path = TMUX_LOG_DIR / f"{session_id}.pid"
        bash = find_bash()
        if bash:
            # Run the existing bash wrapper verbatim through Git Bash, redirecting
            # all output to the log the poller reads. Paths handed to bash use
            # POSIX form + shell-quoting so drive paths / spaces survive.
            inner = TMUX_LOG_DIR / f"{session_id}_run.sh"
            inner.write_text("\n".join(bash_lines) + "\n", encoding="utf-8")
            lp = shlex.quote(log_path.as_posix())
            ip = shlex.quote(inner.as_posix())
            script_path = TMUX_LOG_DIR / f"{session_id}.sh"
            script_path.write_text(
                f"bash {ip} > {lp} 2>&1\n",
                encoding="utf-8",
            )
            argv = [bash, str(script_path)]
        else:
            # No bash on this Windows host: the bash wrapper can't run. Fall back
            # to a cmd.exe wrapper that just records a clear error to the log so
            # the UI surfaces "install Git Bash" instead of silently hanging.
            script_path = TMUX_LOG_DIR / f"{session_id}.cmd"
            script_path.write_text(
                "@echo off\r\n"
                f'echo Cookbook LOCAL execution on Windows needs Git Bash ^(bash.exe^) on PATH. > "{log_path}" 2>&1\r\n'
                f'echo Install Git for Windows, then retry. >> "{log_path}"\r\n',
                encoding="utf-8",
            )
            argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", str(script_path)]
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **detached_popen_kwargs(),
        )
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        return {"pid": proc.pid, "log_path": str(log_path)}
