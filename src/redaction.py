"""Secret redaction (Phase 1.5 / ADR-024).

Masks known secret VALUES (app-configured API keys/tokens in the environment)
and common secret PATTERNS before text re-enters the model context (via
`format_tool_result`) or is written to logs (via `RedactingLogFilter`).

Best-effort and exception-safe: any internal error returns the input unchanged
rather than breaking the tool or log path. The threat it addresses (Phase 1.5
DoD): a known secret must not appear in model-bound messages or in logs.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Pattern, Tuple

REDACTED = "[REDACTED]"

# Env var NAMES whose VALUES are secret and should be masked verbatim wherever
# they appear. Matched case-insensitively as substrings of the var name.
_SECRET_NAME_HINTS = (
    "api_key", "apikey", "secret", "token", "password", "passwd",
    "credential", "private_key", "access_key", "session_secret",
)
# Names that match a hint but are NOT secrets (config flags, not values).
_NAME_ALLOWLIST = {
    "auth_enabled", "localhost_bypass", "token_cache_ttl", "access_key_id_header",
}
_MIN_SECRET_LEN = 8  # don't mask short flag-ish values ("true", "1", "off")

_secret_values_cache: List[str] | None = None


def _env_secret_values() -> List[str]:
    """Cached list of secret-looking env VALUES, longest first so overlapping
    secrets mask fully. Call `refresh_secret_values()` if the environment
    changes at runtime."""
    global _secret_values_cache
    if _secret_values_cache is not None:
        return _secret_values_cache
    vals: List[str] = []
    try:
        for name, val in os.environ.items():
            if not val or len(val) < _MIN_SECRET_LEN:
                continue
            lname = name.lower()
            if lname in _NAME_ALLOWLIST:
                continue
            if any(hint in lname for hint in _SECRET_NAME_HINTS):
                vals.append(val)
    except Exception:
        vals = []
    _secret_values_cache = sorted(set(vals), key=len, reverse=True)
    return _secret_values_cache


def refresh_secret_values() -> None:
    """Drop the cached env-secret values (call after setting secret env vars)."""
    global _secret_values_cache
    _secret_values_cache = None


# (compiled pattern, replacement) for structural secret shapes.
_PATTERNS: List[Tuple[Pattern, str]] = [
    # PEM private key blocks (multiline).
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S,
    ), REDACTED),
    # Anthropic-style keys (before the generic sk- rule).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), REDACTED),
    # OpenAI-style sk- keys.
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), REDACTED),
    # App API tokens: ody_ + base64-ish.
    (re.compile(r"\body_[A-Za-z0-9_\-]{16,}"), REDACTED),
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_).
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    # AWS access key id.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    # Bearer tokens (keep the scheme word, mask the credential).
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer " + REDACTED),
    # Authorization header style "Authorization: <token>".
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)[A-Za-z0-9._\-]{20,}"), r"\1" + REDACTED),
]


def redact(text):
    """Return `text` with known secret values + common secret patterns masked.

    Exception-safe: on any internal error returns the input unchanged so the
    tool/log path is never broken by redaction."""
    if not text:
        return text
    try:
        s = text if isinstance(text, str) else str(text)
        for val in _env_secret_values():
            if val and val in s:
                s = s.replace(val, REDACTED)
        for pat, repl in _PATTERNS:
            s = pat.sub(repl, s)
        return s
    except Exception:
        return text


class RedactingLogFilter(logging.Filter):
    """Logging filter that redacts secrets from the formatted log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = redact(msg)
            if red != msg:
                record.msg = red
                record.args = ()
        except Exception:
            pass
        return True  # never drop records


def install_log_redaction() -> None:
    """Attach the redaction filter to the root logger's handlers (idempotent).

    Filters live on handlers (not the root logger) so records propagated from
    child loggers are covered too. Call AFTER logging is configured."""
    root = logging.getLogger()
    handlers = root.handlers or []
    for h in handlers:
        if not any(isinstance(f, RedactingLogFilter) for f in h.filters):
            h.addFilter(RedactingLogFilter())
    # Also guard the root logger itself for records logged directly to it.
    if not any(isinstance(f, RedactingLogFilter) for f in root.filters):
        root.addFilter(RedactingLogFilter())
