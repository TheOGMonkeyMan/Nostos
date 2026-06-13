"""Regression test (bugfix): admin "wipe memory" must clear the semantic-memory
vector store, not just the SQL Memory table + memory.json sidecars.

Pre-fix bug (routes/admin_wipe_routes.py): the wipe path tried to drop the vector
store via a lazy `from src.memory_vector import get_memory_vector_store` followed by
`mv.clear()`, but NEITHER symbol existed -- the import always raised ImportError
(swallowed) and MemoryVectorStore had no clear() method. So a memory wipe left the
LanceDB index populated and semantic search kept surfacing "ghost" memories.

The fix threads the real MemoryVectorStore into setup_admin_wipe_routes (mirroring
setup_memory_routes) and calls mv.rebuild([]) -- which drops + recreates the table --
when kind == "memory".

The store is embedded LanceDB now, so this drives the real store against a temp dir
with a deterministic fake encoder (see tests/test_memory_vector_lance.py for the
pattern) and isolates the SQL + on-disk-file side effects via monkeypatch.
"""

import numpy as np

from src.memory_vector import MemoryVectorStore
import routes.admin_wipe_routes as awr


class _FakeEncoder:
    """Deterministic bag-of-words encoder. Shared-word texts -> similar vectors."""
    url = "fake://test"
    DIM = 64

    def encode(self, texts, normalize_embeddings=True):
        out = []
        for t in texts:
            v = np.zeros(self.DIM, dtype=np.float32)
            for tok in t.lower().split():
                idx = (sum(ord(c) for c in tok) * 2654435761) % self.DIM
                v[idx] += 1.0
            if not v.any():
                v[0] = 1.0
            if normalize_embeddings:
                v = v / np.linalg.norm(v)
            out.append(v)
        return np.array(out)


class _FakeDB:
    """Minimal SQLAlchemy-session stand-in: the wipe handler only needs
    query().count()/delete(), commit(), rollback(), close()."""

    def query(self, *a, **k):
        return self

    def count(self):
        return 0

    def delete(self):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _AppState:
    auth_manager = None


class _App:
    state = _AppState()


class _ReqState:
    current_user = None


class _Req:
    """Fake Request that satisfies require_admin when AUTH_ENABLED=false."""

    state = _ReqState()
    app = _App()
    headers = {}


def _get_wipe_handler(router):
    for rt in router.routes:
        if getattr(rt, "path", "") == "/api/admin/wipe/{kind}":
            return rt.endpoint
    raise AssertionError("wipe route not registered")


def test_wipe_memory_clears_vector_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    # Isolate from the real SQL DB + on-disk memory files; this test is about
    # the vector-store side of the wipe.
    monkeypatch.setattr(awr, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(awr, "_wipe_memory_files", lambda: None)

    store = MemoryVectorStore(str(tmp_path), embedding_model=_FakeEncoder())
    store.add("m1", "alpha bravo charlie")
    store.add("m2", "delta echo foxtrot")
    assert store.count() == 2
    # Pre-wipe the memory is semantically findable -- this is exactly the
    # "ghost" that used to survive a wipe.
    assert store.search("alpha bravo charlie")[0]["memory_id"] == "m1"

    router = awr.setup_admin_wipe_routes(session_manager=object(), memory_vector=store)
    handler = _get_wipe_handler(router)

    result = handler("memory", _Req())

    assert result["status"] == "deleted"
    assert result["kind"] == "memory"
    # The fix: the vector index is dropped, so no ghost memories survive.
    assert store.count() == 0
    assert store.search("alpha bravo charlie") == []


def test_wipe_memory_tolerates_no_vector_store(tmp_path, monkeypatch):
    """A deployment with the vector store degraded/absent (memory_vector=None)
    must still complete a memory wipe without raising."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(awr, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(awr, "_wipe_memory_files", lambda: None)

    router = awr.setup_admin_wipe_routes(session_manager=object(), memory_vector=None)
    handler = _get_wipe_handler(router)

    result = handler("memory", _Req())
    assert result["status"] == "deleted"
    assert result["kind"] == "memory"
