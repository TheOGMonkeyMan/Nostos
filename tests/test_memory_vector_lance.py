"""Phase 2.5 (ADR-062): behavioral tests for the LanceDB-backed MemoryVectorStore.

The memory vector store moved from the standalone-ChromaDB-service backend to embedded
LanceDB. There were NO tests before (it needed a running Chroma server); LanceDB is
embedded, so these exercise the real store against a temp directory with a deterministic
fake encoder (bag-of-words -> normalized vector; identical text -> identical vector, so
an exact query scores ~1.0 and disjoint-word text scores ~0). This locks the public
contract (add / search / find_similar / remove / rebuild / count / healthy) and the
cosine score semantics (score = 1 - distance) the rest of the app depends on.
"""

import numpy as np
import pytest

from src.memory_vector import MemoryVectorStore


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


@pytest.fixture
def store(tmp_path):
    return MemoryVectorStore(str(tmp_path), embedding_model=_FakeEncoder())


def test_healthy_and_empty(store):
    assert store.healthy is True
    assert store.count() == 0
    assert store.search("anything") == []
    assert store.find_similar("anything") is None


def test_add_count_and_exact_search(store):
    store.add("m1", "alpha bravo charlie")
    store.add("m2", "delta echo foxtrot")
    store.add("m3", "golf hotel india")
    assert store.count() == 3

    hits = store.search("alpha bravo charlie", k=3)
    assert hits[0]["memory_id"] == "m1"
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-3)
    assert {h["memory_id"] for h in hits} == {"m1", "m2", "m3"}


def test_add_is_idempotent_by_id(store):
    store.add("dup", "same text here")
    store.add("dup", "same text here")
    store.add("dup", "different but same id")
    assert store.count() == 1


def test_find_similar_threshold(store):
    store.add("m1", "alpha bravo charlie")
    # exact text -> similarity 1.0 >= threshold -> returns the id
    assert store.find_similar("alpha bravo charlie", threshold=0.92) == "m1"
    # disjoint words -> ~0 similarity -> below threshold -> None
    assert store.find_similar("xenon yarrow zulu", threshold=0.92) is None


def test_remove(store):
    store.add("m1", "alpha bravo")
    store.add("m2", "charlie delta")
    store.remove("m1")
    assert store.count() == 1
    assert all(h["memory_id"] != "m1" for h in store.search("alpha bravo", k=5))


def test_rebuild_replaces_index(store):
    store.add("old1", "one two")
    store.add("old2", "three four")
    store.rebuild([
        {"id": "new1", "text": "five six"},
        {"id": "new2", "text": "seven eight"},
        {"id": "skip", "text": "   "},   # blank text is dropped
        {"id": "", "text": "nine ten"},  # blank id is dropped
    ])
    assert store.count() == 2
    ids = {h["memory_id"] for h in store.search("five six", k=5)}
    assert "new1" in ids and "old1" not in ids


def test_persists_across_reopen(tmp_path):
    enc = _FakeEncoder()
    s1 = MemoryVectorStore(str(tmp_path), embedding_model=enc)
    s1.add("persist", "kept on disk")
    # a new instance over the same dir sees the table (embedded, file-based)
    s2 = MemoryVectorStore(str(tmp_path), embedding_model=enc)
    assert s2.count() == 1
    assert s2.search("kept on disk")[0]["memory_id"] == "persist"
