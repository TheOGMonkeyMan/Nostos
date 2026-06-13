"""Phase 2.5 (ADR-064): behavioral tests for the LanceDB-backed ToolIndex.

ToolIndex (RAG-based tool selection) moved from the standalone-ChromaDB-service backend
to embedded LanceDB. It had no tests before (it needed a running Chroma server). The
prior chroma `upsert`-by-type is emulated as delete-by-filter(tool_type) + add. These
lock indexing (builtin + mcp), the tool_type isolation, idempotent re-index, retrieval,
and the mcp-generation skip.
"""

import tempfile

import numpy as np

from src.tool_index import ToolIndex, BUILTIN_TOOL_DESCRIPTIONS


class _Enc:
    url = "fake://test"
    model = "fake-model"
    DIM = 48

    def encode(self, texts, normalize_embeddings=True):
        out = []
        for t in texts:
            v = np.zeros(self.DIM, dtype=np.float32)
            for tok in t.lower().split():
                v[(sum(ord(c) for c in tok) * 2654435761) % self.DIM] += 1.0
            if not v.any():
                v[0] = 1.0
            out.append(v / np.linalg.norm(v))
        return np.array(out)


class _FakeMcp:
    def __init__(self, text, gen=1):
        self._generation = gen
        self._text = text

    def get_tool_descriptions_for_prompt(self, disabled_map):
        return self._text


_MCP_TEXT = """**myserver:**
- fetch_data: Fetches records from the remote API
- store_record: Persists a record to disk
"""


def _index(tmp=None):
    return ToolIndex(persist_directory=tmp or tempfile.mkdtemp(), embedding_model=_Enc())


def test_index_builtin_and_count():
    ti = _index()
    ti.index_builtin_tools()
    assert ti.get_count() == len(BUILTIN_TOOL_DESCRIPTIONS)


def test_reindex_builtin_does_not_duplicate():
    ti = _index()
    ti.index_builtin_tools()
    first = ti.get_count()
    ti.index_builtin_tools()   # delete-by-type + add -> same set, no duplicates
    assert ti.get_count() == first


def test_retrieve_surfaces_relevant_tool():
    ti = _index()
    ti.index_builtin_tools()
    names = ti.retrieve("send an email to a contact", k=8)
    assert "send_email" in names


def test_builtin_and_mcp_coexist_by_type():
    ti = _index()
    ti.index_builtin_tools()
    n_builtin = ti.get_count()
    ti.index_mcp_tools(_FakeMcp(_MCP_TEXT, gen=1))
    # both types present; mcp added on top of builtin
    assert ti.get_count() == n_builtin + 2
    assert "fetch_data" in ti.retrieve("fetch records from the remote api", k=8)


def test_reindex_mcp_replaces_only_mcp():
    ti = _index()
    ti.index_builtin_tools()
    n_builtin = ti.get_count()
    ti.index_mcp_tools(_FakeMcp(_MCP_TEXT, gen=1))
    # a new generation with a single different tool -> old mcp gone, builtin intact
    ti.index_mcp_tools(_FakeMcp("**srv:**\n- only_tool: does one thing\n", gen=2))
    assert ti.get_count() == n_builtin + 1
    names = ti.retrieve("does one thing", k=8)
    assert "only_tool" in names
    assert "fetch_data" not in ti.retrieve("fetch records", k=8)


def test_mcp_generation_skip():
    ti = _index()
    ti.index_mcp_tools(_FakeMcp(_MCP_TEXT, gen=5))
    c = ti.get_count()
    # same generation -> no-op (would otherwise re-delete/re-add)
    ti.index_mcp_tools(_FakeMcp("**srv:**\n- different: x\n", gen=5))
    assert ti.get_count() == c


def test_empty_retrieve():
    ti = _index()
    assert ti.retrieve("anything") == []
