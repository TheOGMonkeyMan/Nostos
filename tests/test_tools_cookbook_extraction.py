"""Phase 2.2 (ADR-032): characterize + verify the cookbook/model-serving extraction.

The cookbook + model-serving handlers (the largest cohesive block in
src/tool_implementations.py) were carved out into src/tools/cookbook.py and
re-exported so callers (src/tool_execution.py) and the research handlers (which
lazy-import _COOKBOOK_BASE / _internal_headers from src.tool_implementations)
keep working. The branches asserted here return before any network access, so
they are hermetic.
"""

from src import tool_implementations as ti

_HANDLERS = [
    "do_app_api",
    "do_download_model",
    "do_serve_model",
    "do_list_served_models",
    "do_stop_served_model",
    "do_list_downloads",
    "do_cancel_download",
    "do_search_hf_models",
    "do_adopt_served_model",
    "do_list_cookbook_servers",
    "do_list_serve_presets",
    "do_serve_preset",
    "do_list_cached_models",
]


# --- behavior preservation (public interface, hermetic) --------------------

async def test_do_app_api_invalid_json():
    assert await ti.do_app_api("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_download_model_missing_repo_id():
    assert await ti.do_download_model("{}") == {
        "error": "repo_id is required",
        "exit_code": 1,
    }


async def test_do_download_model_invalid_json():
    assert await ti.do_download_model("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_serve_model_invalid_json():
    assert await ti.do_serve_model("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


# --- extraction target (RED until src/tools/cookbook.py exists) -------------

def test_cookbook_module_exists_and_exports():
    from src.tools import cookbook

    for name in _HANDLERS + ["_COOKBOOK_BASE", "_internal_headers"]:
        assert hasattr(cookbook, name), f"src.tools.cookbook is missing {name}"


def test_cookbook_handlers_reexported_with_identity():
    from src.tools import cookbook

    for name in _HANDLERS:
        assert getattr(ti, name) is getattr(cookbook, name), f"{name} not re-exported with identity"


def test_cookbook_shared_symbols_still_importable_from_tool_implementations():
    # research.py lazy-imports these from src.tool_implementations; the re-export
    # must keep that path resolving after the move.
    from src.tool_implementations import _COOKBOOK_BASE, _internal_headers
    from src.tools.cookbook import _COOKBOOK_BASE as cb_base, _internal_headers as cb_hdr

    assert _COOKBOOK_BASE == cb_base
    assert _internal_headers is cb_hdr
