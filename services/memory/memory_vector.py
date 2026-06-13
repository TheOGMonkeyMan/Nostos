"""Re-export of the canonical memory vector store (Phase 2.5, ADR-062).

This module used to hold a byte-identical COPY of src/memory_vector.py. To avoid
maintaining the implementation twice (and to ensure services/ picks up the LanceDB
backend), it now re-exports the single source of truth in src.memory_vector.
"""

from src.memory_vector import MemoryVectorStore  # noqa: F401

__all__ = ["MemoryVectorStore"]
