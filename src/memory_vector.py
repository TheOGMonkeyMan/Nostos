"""
memory_vector.py

Embedded LanceDB-backed vector store for memory entries (Phase 2.5, ADR-062).
Replaces the standalone-ChromaDB-service backend with a local, file-based LanceDB
table so no separate vector-DB container is required. Shares the EmbeddingClient
with RAG; stores pre-computed, normalized embeddings (LanceDB does not embed).

The public interface (add / remove / search / find_similar / rebuild / count /
healthy) and the score semantics are byte-for-byte compatible with the previous
ChromaDB implementation: LanceDB cosine distance == 1 - cosine_similarity, so
`score = 1.0 - distance` is unchanged.
"""

import logging
import os
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

TABLE_NAME = "odysseus_memories"


def _esc(value: str) -> str:
    """Escape single quotes for a LanceDB SQL filter literal."""
    return value.replace("'", "''")


class MemoryVectorStore:
    """Vector index over memory entries for semantic retrieval (LanceDB-backed)."""

    COLLECTION_NAME = TABLE_NAME  # retained for backward compatibility

    def __init__(self, data_dir: str, embedding_model=None):
        self._model = embedding_model
        self._data_dir = data_dir
        self._db = None
        self._table = None
        self._healthy = False

        self._initialize()

    def _initialize(self):
        try:
            import lancedb

            if self._model is None:
                from src.embeddings import get_embedding_client
                self._model = get_embedding_client()
                if self._model is None:
                    raise RuntimeError("No embedding backend available")
                logger.info(f"MemoryVectorStore using embeddings: {self._model.url}")

            path = os.path.join(self._data_dir, "lance_memory")
            os.makedirs(path, exist_ok=True)
            self._db = lancedb.connect(path)
            self._table = self._open_existing()

            self._healthy = True
            logger.info(f"MemoryVectorStore ready (entries={self.count()})")

        except Exception as e:
            logger.error(f"MemoryVectorStore init failed: {e}")

    def _open_existing(self):
        """Open the table if it already exists on disk, else None (created on first
        write). Avoids the deprecated table_names()/paginated list_tables() APIs."""
        try:
            return self._db.open_table(TABLE_NAME)
        except Exception:
            return None

    @property
    def healthy(self) -> bool:
        return self._healthy

    def _embed(self, texts: List[str]) -> List[List[float]]:
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return vecs.tolist()

    def _row(self, memory_id: str, text: str, vector: List[float]) -> Dict:
        return {"id": memory_id, "text": text, "vector": vector, "source": "memory"}

    def count(self) -> int:
        """Return the number of stored vectors."""
        if not self._healthy or self._table is None:
            return 0
        return self._table.count_rows()

    def add(self, memory_id: str, text: str):
        """Add a single memory entry to the vector index."""
        if not self._healthy:
            return
        # Skip if already exists
        if self._table is not None and self._table.count_rows(f"id = '{_esc(memory_id)}'") > 0:
            return
        vector = self._embed([text])[0]
        row = self._row(memory_id, text, vector)
        if self._table is None:
            # First write creates the table (schema inferred from the row).
            self._table = self._db.create_table(TABLE_NAME, data=[row])
        else:
            self._table.add([row])

    def remove(self, memory_id: str):
        """Remove a memory entry. O(1) — no rebuild needed."""
        if not self._healthy or self._table is None:
            return
        try:
            self._table.delete(f"id = '{_esc(memory_id)}'")
        except Exception as e:
            logger.warning(f"memory remove {memory_id}: {e}")

    def search(self, query: str, k: int = 8) -> List[Dict]:
        """Search for the most relevant memory IDs by semantic similarity.
        Returns list of {"memory_id": str, "score": float}.

        LanceDB cosine distance = 1 - cosine_similarity; we convert back so the
        returned score is cosine similarity, identical to the prior backend.
        """
        n = self.count()
        if n == 0:
            return []

        vector = self._embed([query])[0]
        actual_k = min(k, n)
        results = self._table.search(vector).metric("cosine").limit(actual_k).to_list()

        out = []
        for r in results:
            out.append({
                "memory_id": r["id"],
                "score": round(1.0 - r["_distance"], 4),
            })
        return out

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]:
        """Check if a near-duplicate exists. Returns memory_id if found, else None."""
        if self.count() == 0:
            return None

        vector = self._embed([text])[0]
        results = self._table.search(vector).metric("cosine").limit(1).to_list()
        if results:
            similarity = 1.0 - results[0]["_distance"]
            if similarity >= threshold:
                return results[0]["id"]
        return None

    def rebuild(self, memories: List[Dict]):
        """Rebuild the entire index from a list of memory entries.
        Each entry must have 'id' and 'text' keys."""
        if not self._healthy:
            return

        # Drop and recreate the table for a clean rebuild.
        try:
            self._db.drop_table(TABLE_NAME)
        except Exception:
            pass
        self._table = None

        pending = [(m.get("id", ""), m.get("text", "").strip()) for m in memories]
        pending = [(mid, text) for mid, text in pending if mid and text]

        if pending:
            data = []
            # Batch embedding in chunks of 100 to bound memory use.
            for i in range(0, len(pending), 100):
                batch = pending[i:i + 100]
                vectors = self._embed([text for _, text in batch])
                for (mid, text), vector in zip(batch, vectors):
                    data.append(self._row(mid, text, vector))
            self._table = self._db.create_table(TABLE_NAME, data=data)

        logger.info(f"MemoryVectorStore rebuilt with {len(pending)} entries")
