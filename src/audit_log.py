"""Append-only, tamper-evident audit log of agent tool calls (Phase 1.3 / ADR-026).

Every tool invocation (allowed, blocked, or errored) is recorded as one JSON line
under ``data/audit/audit-YYYY-MM-DD.jsonl``. Records form a hash chain: each line
carries ``prev`` (the previous record's hash) and ``hash`` (sha256 over prev +
the record body), so silently editing or deleting a past line is detectable with
``verify_chain()``.

Arguments are stored only as a sha256 ``args_hash`` (never raw), so the audit log
cannot itself become a place where secrets land (R15). Recording is best-effort
and exception-safe: a failure to write the audit log never breaks tool execution.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_AUDIT_DIR = Path(__file__).resolve().parent.parent / "data" / "audit"
_GENESIS = "0" * 64
_lock = threading.Lock()
_last_hash_by_file: Dict[str, str] = {}

VALID_OUTCOMES = ("ok", "blocked", "error")


def _today_path() -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _AUDIT_DIR / f"audit-{day}.jsonl"


def _hash_args(args: Any) -> str:
    if args is None:
        return ""
    try:
        if isinstance(args, (dict, list)):
            canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        else:
            canonical = str(args)
    except Exception:
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()


def _record_hash(prev: str, body: Dict[str, Any]) -> str:
    payload = prev + json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _last_hash(path: Path) -> str:
    """Hash of the last record in `path` (genesis if new). Cached per file."""
    key = str(path)
    if key in _last_hash_by_file:
        return _last_hash_by_file[key]
    last = _GENESIS
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line).get("hash", last)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            last = _GENESIS
    _last_hash_by_file[key] = last
    return last


def record(
    tool: Optional[str],
    owner: Optional[str],
    outcome: str,
    *,
    args: Any = None,
    session_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Append one audit record. Best-effort: never raises into the caller."""
    try:
        if outcome not in VALID_OUTCOMES:
            outcome = "ok"
        path = _today_path()
        body = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool or "",
            "owner": owner or "",
            "outcome": outcome,
            "args_hash": _hash_args(args),
            "session_id": session_id or "",
        }
        if detail:
            body["detail"] = str(detail)[:300]
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            prev = _last_hash(path)
            h = _record_hash(prev, body)
            line = json.dumps({**body, "prev": prev, "hash": h}, ensure_ascii=False)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _last_hash_by_file[str(path)] = h
    except Exception as exc:  # never break the tool path on an audit failure
        logger.debug("audit record failed: %s", exc)


def read_records(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read all records from `path` (today's file by default)."""
    path = path or _today_path()
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def verify_chain(path: Optional[Path] = None) -> bool:
    """True if every record's hash matches sha256(prev + body) and the chain
    links unbroken from genesis. Detects edits, reorders, and deletions."""
    path = path or _today_path()
    prev = _GENESIS
    for rec in read_records(path):
        if not isinstance(rec, dict) or "hash" not in rec or "prev" not in rec:
            return False
        if rec["prev"] != prev:
            return False
        body = {k: v for k, v in rec.items() if k not in ("prev", "hash")}
        if _record_hash(prev, body) != rec["hash"]:
            return False
        prev = rec["hash"]
    return True


def _reset_cache_for_tests() -> None:
    """Clear the per-file last-hash cache (tests that point at a tmp dir)."""
    _last_hash_by_file.clear()
