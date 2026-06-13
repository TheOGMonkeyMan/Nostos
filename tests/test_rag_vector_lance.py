"""Phase 2.5 (ADR-063): behavioral tests for the LanceDB-backed VectorRAG (document RAG).

VectorRAG moved from the standalone-ChromaDB-service backend to embedded LanceDB. It had
no tests before (it needed a running Chroma server). These exercise the real store
against a temp dir with a deterministic fake encoder, locking the public contract
(add_document / add_documents_batch / search hybrid scoring / owner filtering / metadata
round-trip / delete_by_source / remove_directory / get_stats / retrieve / rebuild) and the
LanceDB metadata-column + metadata_json design.
"""

import numpy as np
import pytest

from src.rag_vector import VectorRAG


class _FakeEncoder:
    url = "fake://test"
    model = "fake-model"
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
def rag(tmp_path):
    return VectorRAG(persist_directory=str(tmp_path), embedding_model=_FakeEncoder())


def test_healthy_and_empty(rag):
    assert rag.healthy is True
    assert rag.get_count() == 0
    assert rag.search("anything") == []


def test_add_and_hybrid_search(rag):
    assert rag.add_document("alpha bravo charlie", {"source": "/a"}) is True
    assert rag.add_document("delta echo foxtrot", {"source": "/b"}) is True
    assert rag.get_count() == 2

    hits = rag.search("alpha bravo charlie", k=2)
    assert hits[0]["document"] == "alpha bravo charlie"
    # exact text -> vector_sim ~1 and keyword_score 1 -> hybrid ~1
    assert hits[0]["vector_similarity"] == pytest.approx(1.0, abs=1e-3)
    assert hits[0]["similarity"] == pytest.approx(1.0, abs=1e-3)


def test_add_is_idempotent(rag):
    rag.add_document("same text", {"source": "/a"})
    rag.add_document("same text", {"source": "/a"})
    assert rag.get_count() == 1


def test_search_owner_filter(rag):
    rag.add_document("shared words alpha", {"source": "/a", "owner": "alice"})
    rag.add_document("shared words bravo", {"source": "/b", "owner": "bob"})
    rag.add_document("shared words nobody", {"source": "/c"})  # no owner

    hits = rag.search("shared words", k=10, owner="alice")
    owners = {h["metadata"].get("owner") for h in hits}
    assert owners == {"alice"}  # bob + owner-less excluded by the prefilter


def test_metadata_roundtrip(rag):
    meta = {"source": "/docs/x.txt", "filename": "x.txt", "type": ".txt",
            "owner": "alice", "chunk_id": 3, "nested": {"a": 1}}
    rag.add_document("round trip text", meta)
    hit = rag.search("round trip text", k=1)[0]
    assert hit["metadata"] == meta


def test_add_documents_batch_dedup(rag):
    res = rag.add_documents_batch([
        ("one two", {"source": "/1"}),
        ("one two", {"source": "/1"}),   # duplicate within batch
        ("three four", {"source": "/2"}),
    ])
    assert res["success"] is True
    assert res["added_count"] == 2
    assert rag.get_count() == 2


def test_delete_by_source(rag):
    rag.add_document("doc one", {"source": "/docs/a.txt"})
    rag.add_document("doc two", {"source": "/docs/b.txt"})
    removed = rag.delete_by_source("/docs/a.txt")
    assert removed == 1
    assert rag.get_count() == 1


def test_remove_directory_path_and_name(rag):
    rag.add_document("c1", {"source": "/proj/docs/a.txt", "directory": "/proj/docs"})
    rag.add_document("c2", {"source": "/proj/docs/b.txt", "directory": "/proj/docs"})
    rag.add_document("c3", {"source": "/other/c.txt", "directory": "/other"})
    res = rag.remove_directory("/proj/docs")  # has "/" -> source LIKE match
    assert res["removed_count"] == 2
    assert rag.get_count() == 1


def test_get_stats_and_retrieve(rag):
    rag.add_document("retrievable text here", {"source": "/a"})
    stats = rag.get_stats()
    assert stats["document_count"] == 1 and stats["healthy"] is True
    assert rag.retrieve("retrievable text here", k=1) == ["retrievable text here"]


def test_rebuild_index_clears(rag):
    rag.add_document("to be cleared", {"source": "/a"})
    assert rag.get_count() == 1
    assert rag.rebuild_index() is True
    assert rag.get_count() == 0
    # still usable after rebuild
    rag.add_document("fresh", {"source": "/b"})
    assert rag.get_count() == 1


def test_persist_across_reopen(tmp_path):
    enc = _FakeEncoder()
    r1 = VectorRAG(persist_directory=str(tmp_path), embedding_model=enc)
    r1.add_document("kept on disk", {"source": "/a"})
    r2 = VectorRAG(persist_directory=str(tmp_path), embedding_model=enc)
    assert r2.get_count() == 1
    assert r2.retrieve("kept on disk", k=1) == ["kept on disk"]
