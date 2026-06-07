#!/usr/bin/env python3
"""Fail if any module path exists under BOTH src/ and services/ (Phase 2.1 / ADR-028).

A module duplicated across the two package trees is a maintenance hazard: the
copies drift apart and different callers import different copies (exactly what
happened with search/ before it was collapsed into src.search). This guard runs
in CI to prevent a NEW duplicate from creeping back in.

Root-package markers (`__init__.py` directly under src/ or services/) are not
duplicates - every package has one - so they are ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _rel_modules(base: Path) -> set[str]:
    out: set[str] = set()
    if not base.is_dir():
        return out
    for p in base.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(base).as_posix()
        if rel == "__init__.py":  # the package-root marker is not a dupe
            continue
        out.add(rel)
    return out


def find_dupes(root: Path = _ROOT) -> list[str]:
    """Return sorted relative module paths present under both src/ and services/."""
    src = _rel_modules(root / "src")
    services = _rel_modules(root / "services")
    return sorted(src & services)


def main() -> int:
    dupes = find_dupes()
    if dupes:
        print("ERROR: modules duplicated under BOTH src/ and services/:", file=sys.stderr)
        for d in dupes:
            print(f"  - src/{d}  ==  services/{d}", file=sys.stderr)
        print(
            "Collapse each into one canonical package (prefer src/), repoint "
            "imports, delete the stale copy. See DECISIONS.md ADR-028.",
            file=sys.stderr,
        )
        return 1
    print("OK: no src/ <-> services/ module-path duplicates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
