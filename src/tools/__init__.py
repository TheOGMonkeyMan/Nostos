"""Decomposed tool-handler submodules (Phase 2.2 / ADR-029).

Cohesive groups of ``do_*`` handlers are being carved out of the oversized
``src/tool_implementations.py`` god-file into focused modules here. The original
``src.tool_implementations`` import paths stay valid via re-exports, so existing
callers do not change. See DECISIONS.md ADR-029.
"""
