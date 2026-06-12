"""Cookbook process/GPU routes (ADR-047, Phase 2.2).

The GPU availability probes (_run_nvidia_smi / _probe_gpu_device_processes /
_probe_amd_sysfs, used only by the gpus route) + GET /api/cookbook/gpus (probe
GPU state locally or via SSH) + POST /api/cookbook/kill-pid, split verbatim out
of routes/cookbook_routes.py::setup_cookbook_routes(). They reference none of the
nested secret/state helpers, so the registrar takes only the router.
"""

import asyncio
import os
import shlex
import subprocess

from fastapi import HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from core.platform_compat import IS_WINDOWS, kill_process_tree, pid_alive
from routes.cookbook_helpers import _SSH_PORT_RE, _validate_remote_host


def register_process_routes(router):
    # ── GPU availability probe ──

    async def _run_nvidia_smi(query: str, host: str | None, ssh_port: str | None, timeout: int = 8):
        """Run nvidia-smi locally or over SSH. Returns (stdout, error_or_None)."""
        if host:
            pf = f"-p {ssh_port} " if ssh_port and ssh_port != "22" else ""
            cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no {pf}{host} '{query}'"
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(query),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return None, "nvidia-smi timed out"
        if proc.returncode != 0:
            err = (stderr.decode("utf-8", errors="replace") or "").strip()[:200]
            return None, err or "nvidia-smi failed"
        return stdout.decode("utf-8", errors="replace"), None

    async def _run_gpu_shell(cmd_text: str, host: str | None, ssh_port: str | None, timeout: int = 8):
        """Run a small GPU probe shell command locally or over SSH."""
        if host:
            pf = f"-p {ssh_port} " if ssh_port and ssh_port != "22" else ""
            quoted_cmd = shlex.quote(cmd_text)
            remote_cmd = (
                f"if command -v sh >/dev/null 2>&1; then sh -lc {quoted_cmd}; "
                f"elif command -v bash >/dev/null 2>&1; then bash -lc {quoted_cmd}; "
                f"elif command -v zsh >/dev/null 2>&1; then zsh -lc {quoted_cmd}; "
                "else echo 'No POSIX shell found for GPU probe' >&2; exit 127; fi"
            )
            cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no {pf}{host} {shlex.quote(remote_cmd)}"
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd_text, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return None, "GPU probe timed out"
        if proc.returncode != 0:
            err = (stderr.decode("utf-8", errors="replace") or "").strip()[:200]
            return None, err or f"GPU probe failed ({proc.returncode})"
        return stdout.decode("utf-8", errors="replace"), None

    async def _gpu_read_file(path: str, host: str | None, ssh_port: str | None) -> str | None:
        out, err = await _run_gpu_shell(f"cat {shlex.quote(path)} 2>/dev/null", host, ssh_port, timeout=4)
        if err is not None or out is None:
            return None
        return out.strip()

    async def _probe_gpu_device_processes(host: str | None, ssh_port: str | None) -> list[dict]:
        pid_cmd = (
            "{ command -v lsof >/dev/null 2>&1 && "
            "lsof -w -t /dev/kfd /dev/dri/renderD* 2>/dev/null || true; "
            "command -v fuser >/dev/null 2>&1 && "
            "fuser /dev/kfd /dev/dri/renderD* 2>/dev/null || true; } "
            "| tr ' ' '\\n' | sed '/^[0-9][0-9]*$/!d' | sort -n -u"
        )
        out, err = await _run_gpu_shell(pid_cmd, host, ssh_port, timeout=5)
        if err is not None or not out:
            return []
        processes = []
        seen = set()
        for raw in out.splitlines():
            try:
                pid = int(raw.strip())
            except ValueError:
                continue
            if pid in seen:
                continue
            seen.add(pid)
            name_out, _ = await _run_gpu_shell(f"ps -p {pid} -o comm= 2>/dev/null", host, ssh_port, timeout=3)
            name = (name_out or "").strip().splitlines()[0] if (name_out or "").strip() else "process"
            processes.append({"pid": pid, "name": name[:80], "used_mb": 0})
        return processes

    async def _probe_amd_sysfs(host: str | None, ssh_port: str | None) -> list[dict]:
        out, err = await _run_gpu_shell("ls -1 /sys/class/drm 2>/dev/null", host, ssh_port, timeout=4)
        if err is not None or not out:
            return []
        gpus = []
        for entry in out.split():
            if not entry.startswith("card") or "-" in entry:
                continue
            base = f"/sys/class/drm/{entry}/device"
            vendor = await _gpu_read_file(f"{base}/vendor", host, ssh_port)
            if vendor != "0x1002":
                continue
            vram_raw = await _gpu_read_file(f"{base}/mem_info_vram_total", host, ssh_port)
            vis_raw = await _gpu_read_file(f"{base}/mem_info_vis_vram_total", host, ssh_port)
            gtt_raw = await _gpu_read_file(f"{base}/mem_info_gtt_total", host, ssh_port)
            vram_bytes = int(vram_raw) if vram_raw and vram_raw.isdigit() else 0
            vis_bytes = int(vis_raw) if vis_raw and vis_raw.isdigit() else 0
            gtt_bytes = int(gtt_raw) if gtt_raw and gtt_raw.isdigit() else 0
            total_bytes = max(vram_bytes, vis_bytes)
            used_attr = "mem_info_vis_vram_used" if vis_bytes and vis_bytes >= vram_bytes else "mem_info_vram_used"
            unified = bool(vis_bytes and vis_bytes >= vram_bytes)
            if total_bytes <= 0:
                total_bytes = gtt_bytes
                used_attr = "mem_info_gtt_used"
                unified = True
            if total_bytes <= 0:
                continue
            used_raw = await _gpu_read_file(f"{base}/{used_attr}", host, ssh_port)
            used_bytes = int(used_raw) if used_raw and used_raw.isdigit() else 0
            name = await _gpu_read_file(f"{base}/product_name", host, ssh_port)
            if not name:
                device = await _gpu_read_file(f"{base}/device", host, ssh_port)
                name = f"AMD GPU {device or entry}"
            total_mb = max(0, int(total_bytes / (1024 * 1024)))
            used_mb = max(0, min(total_mb, int(used_bytes / (1024 * 1024))))
            free_mb = max(0, total_mb - used_mb)
            gpus.append({
                "index": len(gpus), "name": name, "uuid": entry,
                "free_mb": free_mb, "total_mb": total_mb, "used_mb": used_mb,
                "util_pct": 0, "busy": bool(total_mb and (free_mb / total_mb) < 0.85),
                "processes": [], "backend": "rocm", "source": "amd-sysfs",
                "unified_memory": unified,
            })
        if gpus:
            processes = await _probe_gpu_device_processes(host, ssh_port)
            if processes:
                gpus[0]["processes"] = processes
                gpus[0]["busy"] = True
        return gpus

    @router.get("/api/cookbook/gpus")
    async def list_gpus(request: Request, host: str | None = None, ssh_port: str | None = None):
        """Probe GPU memory/process state locally or via SSH.

        Probe order:
            1. NVIDIA via nvidia-smi
            2. AMD/ROCm and unified-memory APUs via /sys/class/drm
            3. Generic GPU device holders via /dev/kfd and /dev/dri/renderD*

        Returned shape:
            { "ok": True, "gpus": [
                {"index": 0, "name": "...", "free_mb": int, "total_mb": int,
                 "used_mb": int, "util_pct": int, "busy": bool,
                 "uuid": "GPU-...",
                 "processes": [{"pid": int, "name": str, "used_mb": int}, ...]
                }, ...
            ]}
        `busy` is True when free_mb/total_mb < 0.5.
        """
        require_admin(request)
        host = _validate_remote_host(host)
        if ssh_port is not None and ssh_port != "" and not _SSH_PORT_RE.fullmatch(ssh_port):
            raise HTTPException(400, "Invalid ssh_port")
        gpu_query = "nvidia-smi --query-gpu=index,name,memory.free,memory.total,memory.used,utilization.gpu,uuid --format=csv,noheader,nounits"
        nvidia_error = None
        try:
            gpu_out, err = await _run_nvidia_smi(gpu_query, host, ssh_port)
            if err is not None:
                nvidia_error = err
                gpu_out = ""
        except FileNotFoundError:
            nvidia_error = "nvidia-smi not found"
            gpu_out = ""
        except Exception as e:
            nvidia_error = str(e)[:200]
            gpu_out = ""

        gpus = []
        uuid_to_idx: dict[str, int] = {}
        for line in (gpu_out or "").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                idx = int(parts[0])
                name = parts[1]
                free_mb = int(float(parts[2]))
                total_mb = int(float(parts[3]))
                used_mb = int(float(parts[4]))
                util_pct = int(float(parts[5]))
                gpu_uuid = parts[6]
            except (ValueError, IndexError):
                continue
            busy = total_mb > 0 and (free_mb / total_mb) < 0.5
            uuid_to_idx[gpu_uuid] = idx
            gpus.append({
                "index": idx, "name": name, "uuid": gpu_uuid,
                "free_mb": free_mb, "total_mb": total_mb,
                "used_mb": used_mb, "util_pct": util_pct,
                "busy": busy, "processes": [],
            })

        # Best-effort process listing — skip silently if it fails
        proc_query = "nvidia-smi --query-compute-apps=pid,gpu_uuid,process_name,used_memory --format=csv,noheader,nounits"
        try:
            proc_out, proc_err = await _run_nvidia_smi(proc_query, host, ssh_port, timeout=5)
            if proc_err is None and proc_out:
                gpus_by_idx = {g["index"]: g for g in gpus}
                for line in proc_out.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 4:
                        continue
                    try:
                        pid = int(parts[0])
                        pname = parts[2]
                        pmem = int(float(parts[3]))
                    except (ValueError, IndexError):
                        continue
                    idx = uuid_to_idx.get(parts[1])
                    if idx is None or idx not in gpus_by_idx:
                        continue
                    gpus_by_idx[idx]["processes"].append({
                        "pid": pid, "name": pname, "used_mb": pmem,
                    })
        except Exception:
            pass

        if gpus:
            return {"ok": True, "gpus": gpus, "backend": "cuda", "source": "nvidia-smi"}

        amd_gpus = await _probe_amd_sysfs(host, ssh_port)
        if amd_gpus:
            return {
                "ok": True,
                "gpus": amd_gpus,
                "backend": "rocm",
                "source": "amd-sysfs",
                "fallback_from": "nvidia-smi",
                "nvidia_error": nvidia_error,
            }

        processes = await _probe_gpu_device_processes(host, ssh_port)
        if processes:
            return {
                "ok": True,
                "gpus": [{
                    "index": 0, "name": "GPU device holders", "uuid": "dev-dri",
                    "free_mb": 0, "total_mb": 0, "used_mb": 0, "util_pct": 0,
                    "busy": True, "processes": processes,
                    "backend": "generic", "source": "gpu-devices",
                }],
                "backend": "generic",
                "source": "gpu-devices",
                "fallback_from": "nvidia-smi",
                "nvidia_error": nvidia_error,
            }

        return {"ok": False, "error": nvidia_error or "No GPU memory probe available", "gpus": []}

    class KillPidRequest(BaseModel):
        pid: int
        host: str | None = None
        ssh_port: str | None = None
        signal: str = "TERM"  # TERM (graceful) or KILL (force)

    @router.post("/api/cookbook/kill-pid")
    async def kill_pid(request: Request, req: KillPidRequest):
        """Kill a PID that's holding GPU memory.

        Admin-gated. Validates PID is positive int, signal is TERM/KILL, and
        forbids low PIDs (<100) to avoid accidentally signalling init/system
        daemons. Uses `kill -<sig> <pid>` locally or over SSH.
        """
        require_admin(request)
        if req.pid < 100:
            raise HTTPException(400, f"Refusing to signal PID {req.pid} (<100, likely system process)")
        sig = (req.signal or "TERM").upper()
        if sig not in ("TERM", "KILL", "INT"):
            raise HTTPException(400, "signal must be TERM, KILL, or INT")
        host = _validate_remote_host(req.host)
        if req.ssh_port and not _SSH_PORT_RE.fullmatch(req.ssh_port):
            raise HTTPException(400, "Invalid ssh_port")
        kill_cmd = f"kill -{sig} {req.pid}"
        try:
            if host:
                pf = f"-p {req.ssh_port} " if req.ssh_port and req.ssh_port != "22" else ""
                cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no {pf}{host} '{kill_cmd}'"
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
            elif IS_WINDOWS:
                # No `kill` binary / POSIX signals on Windows. taskkill /F /T tears
                # down the PID and its children. There's no graceful-vs-force
                # distinction, so TERM/KILL/INT all map to the same forced kill.
                # NB: never use os.kill(pid, 0) to probe here — on Windows that
                # routes to TerminateProcess and would kill the process.
                if not pid_alive(req.pid):
                    return {"ok": False, "error": f"PID {req.pid} is not running"}
                await asyncio.to_thread(kill_process_tree, req.pid)
                return {"ok": True, "pid": req.pid, "signal": sig}
            else:
                proc = await asyncio.create_subprocess_exec(
                    "kill", f"-{sig}", str(req.pid),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                err = (stderr.decode("utf-8", errors="replace") or "").strip()[:200]
                return {"ok": False, "error": err or f"kill returned {proc.returncode}"}
            return {"ok": True, "pid": req.pid, "signal": sig}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "kill command timed out"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
