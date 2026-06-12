"""Decomposed scheduler-action submodules (Phase 2.2 / ADR-042).

Cohesive groups of ``action_*`` handlers are being carved out of the oversized
``src/builtin_actions.py`` god-file into focused modules here. The original
``src.builtin_actions`` import paths stay valid via re-imports (so the
``BUILTIN_ACTIONS`` registry and existing callers do not change). See
DECISIONS.md ADR-042.
"""
