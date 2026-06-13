"""
rag_vector.py

Vector-based RAG using embedded LanceDB for storage and API-based embeddings
(Phase 2.5, ADR-063 - migrated off the standalone ChromaDB service to a local,
file-based LanceDB table; no separate container).
Features: persistent storage, hybrid search (vector + keyword), sentence-aware
chunking, configurable embedding endpoint via EMBEDDING_URL env var.

Arbitrary per-document metadata is preserved verbatim in a `metadata_json` column;
the fields actually filtered on (owner / source / directory) are mirrored into typed
columns so they can be pushed down into LanceDB SQL `where` clauses. LanceDB cosine
distance == 1 - cosine_similarity, so the scores match the previous backend.
"""

import os
import re
import json
import logging
import numpy as np
from typing import List, Dict, Any, Optional, Set
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FILE_EXTENSIONS: Set[str] = {
    '.txt', '.md', '.py', '.json', '.yaml', '.yml',
    '.csv', '.html', '.css', '.js', '.pdf'
}

VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3

TABLE_NAME = "odysseus_rag"


def _esc(value: str) -> str:
    """Escape single quotes for a LanceDB SQL filter literal."""
    return str(value).replace("'", "''")


class VectorRAG:
    """RAG system using embedded LanceDB vector storage with hybrid search."""

    def __init__(self, persist_directory: str = "data/chroma", embedding_model=None):
        self.persist_directory = persist_directory
        self._db = None
        self._table = None
        self._model = embedding_model
        self._healthy = False

        Path(self.persist_directory).mkdir(parents=True, exist_ok=True)
        self._initialize_system()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _initialize_system(self) -> bool:
        try:
            import lancedb

            if self._model is None:
                from src.embeddings import get_embedding_client
                self._model = get_embedding_client()
            if self._model is None:
                raise RuntimeError("No embedding backend available")
            logger.info(f"Embedding: {self._model.url} model={self._model.model}")

            self._db = lancedb.connect(self.persist_directory)
            self._table = self._open_existing()

            count = self.get_count()
            logger.info(f"VectorRAG ready ({count} docs)")
            self._healthy = True
            return True

        except Exception as e:
            logger.error(f"VectorRAG init failed: {e}")
            self._healthy = False
            return False

    def _open_existing(self):
        """Open the table if it already exists, else None (created on first write)."""
        try:
            return self._db.open_table(TABLE_NAME)
        except Exception:
            return None

    def _embed(self, texts: List[str]) -> List[List[float]]:
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return np.array(vecs, dtype=np.float32).tolist()

    def _row(self, doc_id: str, text: str, vector: List[float], metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Build a table row: filterable columns mirror metadata; metadata_json keeps
        the full original dict for verbatim round-trip on read."""
        return {
            "id": doc_id,
            "text": text,
            "vector": vector,
            "owner": str(metadata.get("owner", "")),
            "source": str(metadata.get("source", "")),
            "directory": str(metadata.get("directory", "")),
            "metadata_json": json.dumps(metadata),
        }

    def get_count(self) -> int:
        return self._table.count_rows() if self._table is not None else 0

    def _exists(self, doc_id: str) -> bool:
        return self._table is not None and self._table.count_rows(f"id = '{_esc(doc_id)}'") > 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def collection(self):
        """Vestigial accessor (was the ChromaDB collection). Returns the LanceDB
        table; retained only for backward compatibility - nothing reads it."""
        return self._table

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    def add_document(self, text: str, metadata: Dict[str, Any]) -> bool:
        if not self.healthy:
            logger.error("Collection not initialized")
            return False
        if not text or not isinstance(text, str):
            return False
        if not metadata or not isinstance(metadata, dict):
            return False

        try:
            doc_id = f"doc_{hash(text) % 10**16}"
            if self._exists(doc_id):
                return True  # already exists
            vector = self._embed([text])[0]
            row = self._row(doc_id, text, vector, metadata)
            if self._table is None:
                self._table = self._db.create_table(TABLE_NAME, data=[row])
            else:
                self._table.add([row])
            return True
        except Exception as e:
            logger.error(f"add_document failed: {e}")
            return False

    def add_documents_batch(self, docs: List[tuple]) -> Dict[str, Any]:
        if not self.healthy:
            return {"success": False, "message": "Collection not initialized"}
        if not docs:
            return {"success": False, "message": "Empty document list"}

        valid = [
            (t, m) for t, m in docs
            if t and isinstance(t, str) and m and isinstance(m, dict)
        ]
        if not valid:
            return {"success": False, "message": "No valid documents"}

        try:
            # Collect new, de-duplicated rows (skip ids already stored or repeated
            # within this batch - LanceDB has no unique-id constraint).
            new_rows: List[Dict[str, Any]] = []
            seen_ids = set()
            new_texts: List[str] = []
            new_metas: List[Dict[str, Any]] = []
            new_ids: List[str] = []
            for t, m in valid:
                doc_id = f"doc_{hash(t) % 10**16}"
                if doc_id in seen_ids or self._exists(doc_id):
                    continue
                seen_ids.add(doc_id)
                new_texts.append(t)
                new_metas.append(m)
                new_ids.append(doc_id)

            if new_texts:
                # Batch in chunks of 100 to bound embedding memory.
                for i in range(0, len(new_texts), 100):
                    batch_texts = new_texts[i:i + 100]
                    batch_ids = new_ids[i:i + 100]
                    batch_metas = new_metas[i:i + 100]
                    embeddings = self._embed(batch_texts)
                    for did, txt, mta, vec in zip(batch_ids, batch_texts, batch_metas, embeddings):
                        new_rows.append(self._row(did, txt, vec, mta))

            if new_rows:
                if self._table is None:
                    self._table = self._db.create_table(TABLE_NAME, data=new_rows)
                else:
                    self._table.add(new_rows)

            return {
                "success": True,
                "added_count": len(new_ids),
                "total_count": len(docs),
                "failed_count": len(docs) - len(valid),
            }
        except Exception as e:
            logger.error(f"add_documents_batch failed: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Search — hybrid: vector similarity + keyword overlap
    # ------------------------------------------------------------------

    def search(self, query: str, k: int = 5, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.healthy:
            return []
        if not query or not isinstance(query, str):
            return []
        count = self.get_count()
        if count == 0:
            return []

        try:
            # Fetch extra candidates when owner-filtering
            fetch_k = min(k * 3, max(k, 20), count)
            if owner:
                fetch_k = min(fetch_k * 2, count)

            query_vector = self._embed([query])[0]

            # Push the owner filter into LanceDB (prefilter so we still get fetch_k hits).
            builder = self._table.search(query_vector)
            if owner:
                builder = builder.where(f"owner = '{_esc(owner)}'", prefilter=True)
            results = builder.metric("cosine").limit(fetch_k).to_list()

            query_words = set(query.lower().split())
            candidates = []

            for r in results:
                doc_id = r["id"]
                distance = r["_distance"]
                doc_text = r["text"]
                meta = json.loads(r["metadata_json"])

                # LanceDB cosine distance = 1 - cosine_similarity
                vector_sim = 1.0 - distance

                # Keyword overlap score
                doc_words = set(doc_text.lower().split())
                overlap = len(query_words & doc_words)
                keyword_score = overlap / len(query_words) if query_words else 0.0

                hybrid_score = (VECTOR_WEIGHT * vector_sim) + (KEYWORD_WEIGHT * keyword_score)

                candidates.append({
                    "id": doc_id,
                    "document": doc_text,
                    "metadata": meta,
                    "distance": round(distance, 4),
                    "similarity": round(hybrid_score, 4),
                    "vector_similarity": round(vector_sim, 4),
                    "keyword_score": round(keyword_score, 4),
                })

            candidates.sort(key=lambda c: c["similarity"], reverse=True)
            top = candidates[:k]
            logger.info(f"Hybrid search for '{query[:60]}': {len(top)} results")
            return top

        except Exception as e:
            logger.error(f"search failed: {e}")
            return self._keyword_search_fallback(query, k, owner=owner)

    def _keyword_search_fallback(self, query: str, k: int = 5, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            count = self.get_count()
            if count == 0:
                return []

            # Full scan: a vector search with limit=count returns every row (ordering
            # is irrelevant here - we re-score by keyword overlap below).
            probe = self._embed([query])[0]
            all_docs = self._table.search(probe).limit(count).to_list()
            if not all_docs:
                return []

            query_words = query.lower().split()
            scored = []
            for r in all_docs:
                meta = json.loads(r["metadata_json"])
                if owner:
                    doc_owner = meta.get("owner")
                    if doc_owner and doc_owner != owner:
                        continue
                doc = r["text"]
                doc_lower = doc.lower()
                score = sum(1 for w in query_words if w in doc_lower)
                if score > 0:
                    scored.append({
                        "id": r["id"],
                        "document": doc,
                        "metadata": meta,
                        "distance": 0,
                        "similarity": score,
                        "search_type": "keyword_fallback",
                    })

            scored.sort(key=lambda x: x["similarity"], reverse=True)
            return scored[:k]
        except Exception as e:
            logger.error(f"keyword fallback failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def rebuild_index(self) -> bool:
        try:
            if self._db is None:
                return False
            try:
                self._db.drop_table(TABLE_NAME)
            except Exception:
                pass
            self._table = None  # recreated lazily on the next write
            self._healthy = True
            return True
        except Exception as e:
            logger.error(f"rebuild_index failed: {e}")
            self._healthy = False
            return False

    def get_stats(self) -> Dict[str, Any]:
        if not self.healthy:
            return {"error": "Collection not initialized"}
        try:
            return {
                "document_count": self.get_count(),
                "embedding_model": f"{self._model.model} @ {self._model.url}" if self._model else "N/A",
                "persist_directory": self.persist_directory,
                "collection_name": TABLE_NAME,
                "healthy": True,
            }
        except Exception as e:
            logger.error(f"get_stats failed: {e}")
            return {"error": str(e), "healthy": False}

    # ------------------------------------------------------------------
    # Directory indexing
    # ------------------------------------------------------------------

    def index_personal_documents(
        self, directory: str, file_extensions: Optional[set] = None, owner: Optional[str] = None
    ) -> Dict[str, Any]:
        if file_extensions is None:
            file_extensions = DEFAULT_FILE_EXTENSIONS

        indexed = 0
        failed = 0

        try:
            for root, _, files in os.walk(directory):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    ext = Path(fname).suffix.lower()
                    if ext not in file_extensions:
                        continue

                    try:
                        if ext == '.pdf':
                            from src.personal_docs import extract_pdf_text
                            content = extract_pdf_text(fpath)
                        else:
                            with open(fpath, 'r', encoding='utf-8') as f:
                                content = f.read()

                        if not content or not content.strip():
                            continue

                        meta = {
                            'source': fpath,
                            'filename': fname,
                            'directory': root,
                            'type': ext,
                        }
                        if owner:
                            meta['owner'] = owner

                        for i, chunk in enumerate(self._split_into_chunks(content)):
                            if self.add_document(chunk, {**meta, 'chunk_id': i}):
                                indexed += 1
                            else:
                                failed += 1
                    except Exception as e:
                        logger.error(f"index {fpath}: {e}")
                        failed += 1

            return {
                'success': True,
                'indexed_count': indexed,
                'failed_count': failed,
                'message': f'Indexed {indexed} chunks from {directory}',
            }
        except Exception as e:
            logger.error(f"index_personal_documents {directory}: {e}")
            return {'success': False, 'indexed_count': indexed, 'failed_count': failed, 'message': str(e)}

    def remove_directory(self, directory: str) -> Dict[str, Any]:
        """Remove all chunks from a directory. O(1) per chunk via a LanceDB filter."""
        if not self.healthy or self._table is None:
            return {"success": False, "message": "Collection not initialized"} if not self.healthy \
                else {"success": True, "removed_count": 0, "message": "No docs found"}
        try:
            # Match the prior semantics: substring match on source when the directory
            # looks like a path, else an exact directory-column match.
            if "/" in directory:
                where = f"source LIKE '%{_esc(directory)}%'"
            else:
                where = f"directory = '{_esc(directory)}'"
            n = self._table.count_rows(where)
            if n == 0:
                return {"success": True, "removed_count": 0, "message": "No docs found"}

            self._table.delete(where)
            logger.info(f"Removed {n} chunks from {directory}")
            return {"success": True, "removed_count": n, "message": f"Removed {n} chunks"}
        except Exception as e:
            logger.error(f"remove_directory {directory}: {e}")
            return {"success": False, "message": str(e)}

    def reindex_directory(
        self, directory: str, file_extensions: Optional[set] = None
    ) -> Dict[str, Any]:
        remove_result = self.remove_directory(directory)
        if not remove_result.get("success"):
            return remove_result
        index_result = self.index_personal_documents(directory, file_extensions)
        return {
            "success": index_result.get("success", False),
            "message": (
                f"Re-index for {directory}: removed {remove_result.get('removed_count', 0)}, "
                f"{index_result.get('message', '')}"
            ),
            "removed_count": remove_result.get("removed_count", 0),
            "indexed_count": index_result.get("indexed_count", 0),
            "failed_count": index_result.get("failed_count", 0),
        }

    # ------------------------------------------------------------------
    # Sentence-boundary-aware chunking
    # ------------------------------------------------------------------

    def _split_into_chunks(
        self, text: str, chunk_size: int = 1000, overlap: int = 200
    ) -> List[str]:
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        # Split into sentences first
        sentences = re.split(r'(?<=[.!?])\s+|\n{2,}', text)
        sentences = [s.strip() for s in sentences if s.strip()]

        chunks: List[str] = []
        current_chunk: List[str] = []
        current_len = 0

        for sentence in sentences:
            sent_len = len(sentence)

            # If a single sentence exceeds chunk_size, split it by character
            if sent_len > chunk_size:
                # Flush current chunk first
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                    current_chunk = []
                    current_len = 0

                # Hard-split the long sentence
                for start in range(0, sent_len, chunk_size - overlap):
                    chunks.append(sentence[start:start + chunk_size])
                continue

            if current_len + sent_len + 1 > chunk_size and current_chunk:
                chunks.append(' '.join(current_chunk))
                # Keep last few sentences for overlap
                overlap_sentences: List[str] = []
                overlap_len = 0
                for s in reversed(current_chunk):
                    if overlap_len + len(s) > overlap:
                        break
                    overlap_sentences.insert(0, s)
                    overlap_len += len(s) + 1
                current_chunk = overlap_sentences
                current_len = sum(len(s) for s in current_chunk) + max(0, len(current_chunk) - 1)

            current_chunk.append(sentence)
            current_len += sent_len + (1 if current_len > 0 else 0)

        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks if chunks else [text]

    # ------------------------------------------------------------------
    # Delete by metadata
    # ------------------------------------------------------------------

    def delete_by_source(self, source: str) -> int:
        """Remove all chunks whose metadata['source'] matches *source*.
        Returns the number of removed chunks."""
        if not self.healthy or self._table is None:
            return 0
        try:
            where = f"source = '{_esc(source)}'"
            n = self._table.count_rows(where)
            if n == 0:
                return 0
            self._table.delete(where)
            logger.info(f"Deleted {n} chunks for source={source}")
            return n
        except Exception as e:
            logger.error(f"delete_by_source failed: {e}")
            return 0

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def retrieve(self, query: str, k: int = 5) -> List[str]:
        return [r['document'] for r in self.search(query, k)]
