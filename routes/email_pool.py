"""IMAP connection pool + list/read caches for the email routes.

build_email_pool(router) creates the per-request IMAP connection pool and the
list/read response caches, wires them into the module-level _imap() via
_POOL_HOOKS and onto router._email_pool, and returns the helper callables.
setup_email_routes() binds those to local names so the route closures call
them unchanged. Extracted verbatim from email_routes.py (Phase 2.2 / ADR-037).
"""

import threading as _threading
import time as _time

from routes.email_helpers import _imap_connect, _POOL_HOOKS


def build_email_pool(router):
    """Build + wire the IMAP pool and caches; return the helper callables."""
    _LIST_CACHE = {}
    _LIST_TTL = 8.0
    _READ_CACHE = {}
    _READ_TTL = 30 * 60.0
    _IMAP_POOL = {}
    _IMAP_IDLE_MAX = 60.0
    _pool_lock = _threading.Lock()

    def _pooled_connect(account_id, owner=""):
        """Reuse a live IMAP connection if one is in the pool and still
        responsive. Otherwise open fresh and store it. Caller must release
        via _pooled_release after use (not strictly required — the pool
        holds the same conn handle, and we lock to serialize access).

        SECURITY: `owner` is forwarded to `_imap_connect` so the fallback
        config lookup (when `account_id` is None) is scoped to this user's
        accounts only. The pool key is (account_id, owner) so two users
        with `account_id=None` don't share a pooled connection.
        """
        pool_key = (account_id, owner)
        now = _time.monotonic()
        with _pool_lock:
            entry = _IMAP_POOL.get(pool_key)
            if entry:
                conn, last_used = entry
                if (now - last_used) < _IMAP_IDLE_MAX:
                    try:
                        conn.noop()
                        # Pop it out of the pool while we use it (serialize)
                        del _IMAP_POOL[pool_key]
                        return conn, True  # reused
                    except Exception:
                        try: conn.logout()
                        except Exception: pass
                        del _IMAP_POOL[pool_key]
                else:
                    try: conn.logout()
                    except Exception: pass
                    del _IMAP_POOL[pool_key]
        # Fresh connection
        return _imap_connect(account_id, owner=owner), False

    def _pooled_release(account_id, conn, ok=True, owner=""):
        # SECURITY: match the (account_id, owner) key used by _pooled_connect
        # so a pooled handle is returned to the same per-user slot.
        if not ok:
            try: conn.logout()
            except Exception: pass
            return
        with _pool_lock:
            _IMAP_POOL[(account_id, owner)] = (conn, _time.monotonic())

    def _list_cache_key(account_id, folder, filter_, limit, offset, from_addr=""):
        return (account_id or "", folder, filter_, int(limit), int(offset), from_addr or "")

    def _read_cache_key(account_id, folder, uid, owner=""):
        # SECURITY: include owner so two users with `account_id == ""` /
        # None (i.e. resolved through the per-user default) don't share
        # a cached message body.
        return (account_id or "", folder, str(uid), owner)

    def _list_cache_get(key):
        v = _LIST_CACHE.get(key)
        if not v: return None
        if v[0] < _time.monotonic():
            _LIST_CACHE.pop(key, None)
            return None
        return v[1]

    def _list_cache_put(key, value):
        _LIST_CACHE[key] = (_time.monotonic() + _LIST_TTL, value)
        # Cap size
        if len(_LIST_CACHE) > 64:
            for k in list(_LIST_CACHE.keys())[:-32]:
                _LIST_CACHE.pop(k, None)

    def _invalidate_list_cache(account_id=None, folder=None):
        """Drop list cache entries that the caller's mutation may have stale-ed.

        Called from flag-mutating endpoints (mark-read/unread/answered, archive,
        delete, move) so the UI doesn't show stale read/unread counts for up to
        the 8s TTL after a manual flag change. With no args, clears everything.
        """
        if account_id is None and folder is None:
            _LIST_CACHE.clear()
            return
        for k in list(_LIST_CACHE.keys()):
            k_acct = k[0] if len(k) > 0 else ""
            k_folder = k[1] if len(k) > 1 else ""
            if (account_id is None or k_acct == (account_id or "")) and \
               (folder is None or k_folder == folder):
                _LIST_CACHE.pop(k, None)

    def _read_cache_get(key):
        v = _READ_CACHE.get(key)
        if not v: return None
        if v[0] < _time.monotonic():
            _READ_CACHE.pop(key, None)
            return None
        return v[1]

    def _read_cache_put(key, value):
        _READ_CACHE[key] = (_time.monotonic() + _READ_TTL, value)
        if len(_READ_CACHE) > 256:
            for k in list(_READ_CACHE.keys())[:-128]:
                _READ_CACHE.pop(k, None)

    # Expose helpers in the closure to be used by handlers below
    router._email_pool = {
        "connect": _pooled_connect,
        "release": _pooled_release,
        "list_cache_get": _list_cache_get,
        "list_cache_put": _list_cache_put,
        "list_cache_key": _list_cache_key,
        "read_cache_get": _read_cache_get,
        "read_cache_put": _read_cache_put,
        "read_cache_key": _read_cache_key,
    }
    # Wire the module-level _imap() context manager into the pool so every
    # `with _imap(account_id, owner=owner) as conn:` reuses an existing connection
    # instead of paying TCP+TLS+LOGIN per request.
    _POOL_HOOKS["connect"] = _pooled_connect
    _POOL_HOOKS["release"] = _pooled_release

    return {
        "_pooled_connect": _pooled_connect,
        "_pooled_release": _pooled_release,
        "_list_cache_key": _list_cache_key,
        "_read_cache_key": _read_cache_key,
        "_list_cache_get": _list_cache_get,
        "_list_cache_put": _list_cache_put,
        "_invalidate_list_cache": _invalidate_list_cache,
        "_read_cache_get": _read_cache_get,
        "_read_cache_put": _read_cache_put,
    }
