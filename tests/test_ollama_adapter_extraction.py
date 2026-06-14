"""Phase 3.1 (ADR-068): verify the Ollama adapter extraction from src/llm_core.py.

The 5 Ollama helpers moved verbatim into src/adapters/ollama.py (first slice of the
inference-broker refactor). llm_core re-imports them, so the public src.llm_core.* names
stay valid (routes/model_routes does a runtime `from src.llm_core import
_build_ollama_payload`). Pins the re-export identity, the leaf property, and the helper
behavior the wire format depends on.
"""

import inspect

import src.llm_core as L
import src.adapters.ollama as O

_NAMES = ("_is_ollama_native_url", "_ollama_api_root", "_normalize_ollama_url",
          "_build_ollama_payload", "_parse_ollama_response")


def test_reexport_identity():
    for n in _NAMES:
        assert getattr(L, n) is getattr(O, n), n
        assert getattr(O, n).__module__ == "src.adapters.ollama", n


def test_adapter_is_a_leaf():
    src = inspect.getsource(O)
    assert "import llm_core" not in src and "from src.llm_core" not in src


def test_ollama_url_helpers():
    assert O._is_ollama_native_url("http://localhost:11434/api/chat") is True
    assert O._is_ollama_native_url("https://api.openai.com/v1/chat/completions") is False
    assert O._ollama_api_root("https://ollama.com/api/chat") == "https://ollama.com/api"
    assert O._normalize_ollama_url("http://localhost:11434/api") == "http://localhost:11434/api/chat"


def test_ollama_payload_and_parse():
    p = O._build_ollama_payload("m", [{"role": "user", "content": "hi"}], 0.5, 100, stream=True)
    assert p["model"] == "m" and p["stream"] is True
    assert p["options"] == {"temperature": 0.5, "num_predict": 100}
    assert O._parse_ollama_response({"message": {"content": "hello"}}) == "hello"
    assert O._parse_ollama_response({"response": "fallback"}) == "fallback"
