"""Phase 2.2 (ADR-029): characterize + verify the vault-tools extraction.

The Bitwarden/Vaultwarden tool handlers were the first cohesive slice carved
out of the ``src/tool_implementations.py`` god-file into ``src/tools/vault.py``.

These tests pin two things:
  1. Behavior preservation - the input-validation branches of each ``do_vault_*``
     handler return byte-identical results (exercised through the *public*
     ``src.tool_implementations`` import path callers still use). These branches
     return before any ``bw`` subprocess or ``data/vault.json`` access, so they
     are hermetic (no Bitwarden CLI required).
  2. The extraction itself - the handlers now live in ``src.tools.vault`` and are
     re-exported (same object identity) from ``src.tool_implementations`` so that
     ``src/tool_execution.py`` keeps importing them unchanged.
"""

from src import tool_implementations as ti


# --- behavior preservation (public interface, hermetic) --------------------

async def test_do_vault_search_invalid_json_returns_error():
    assert await ti.do_vault_search("{not valid json") == {
        "error": "Invalid JSON arguments",
        "exit_code": 1,
    }


async def test_do_vault_search_missing_query_returns_error():
    assert await ti.do_vault_search("{}") == {
        "error": "query is required",
        "exit_code": 1,
    }


async def test_do_vault_get_missing_item_id_returns_error():
    assert await ti.do_vault_get("{}") == {
        "error": "item_id is required",
        "exit_code": 1,
    }


async def test_do_vault_get_missing_reason_returns_error():
    res = await ti.do_vault_get('{"item_id": "abc123"}')
    assert res["exit_code"] == 1
    assert "reason is required" in res["error"]


async def test_do_vault_unlock_missing_master_password_returns_error():
    assert await ti.do_vault_unlock("{}") == {
        "error": "master_password is required",
        "exit_code": 1,
    }


# --- extraction target (RED until src/tools/vault.py exists) ----------------

def test_vault_module_exists_and_exports():
    from src.tools import vault

    for name in (
        "do_vault_search",
        "do_vault_get",
        "do_vault_unlock",
        "_load_vault_config",
        "_run_bw",
    ):
        assert hasattr(vault, name), f"src.tools.vault is missing {name}"


def test_vault_handlers_are_reexported_with_identity():
    from src.tools import vault

    assert ti.do_vault_search is vault.do_vault_search
    assert ti.do_vault_get is vault.do_vault_get
    assert ti.do_vault_unlock is vault.do_vault_unlock
