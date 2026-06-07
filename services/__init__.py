# services/__init__.py
"""
Service layer — plug-in capabilities for the chat core.

Each service:
- Does one thing well
- Exposes a clean async interface
- Can run in-process or as a standalone HTTP service
"""

# Search lives in src.search (canonical); the services.search duplicate was
# collapsed in Phase 2.1 (ADR-028). Its SearchService/SearchResult/SearchResponse
# wrapper was unused (re-exported here only), so it was removed with the dupe.
from .docs import DocsService, DocChunk, IndexResult
from .research import ResearchService, ResearchResult, ResearchSource
from .memory import MemoryService, Memory, MemorySearchResult
from .shell import ShellService, ShellResult

__all__ = [
    # Docs
    "DocsService",
    "DocChunk",
    "IndexResult",
    # Research
    "ResearchService",
    "ResearchResult",
    "ResearchSource",
    # Memory
    "MemoryService",
    "Memory",
    "MemorySearchResult",
    # Shell
    "ShellService",
    "ShellResult",
]
