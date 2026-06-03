#!/usr/bin/env python3
"""Enforce the file-length ratchet (engineering-standards): no new Python file
may exceed the cap (1500 lines initially, tightening to 1000 then 800).

Existing over-cap "god-files" are grandfathered via a baseline that pins each to
its CURRENT size: a baselined file may shrink (good - Phase 2.2 decomposition)
but must never GROW past its recorded count, and any non-baselined file must
stay within the global cap. This is the ratchet: things can only get better.

Usage:
    check_file_length.py [--max N] [FILE ...]   # check given files (or all tracked .py)
    check_file_length.py --write-baseline       # regenerate the baseline file

pre-commit passes staged filenames as FILE args. Exits 1 (and lists offenders)
when any file breaks its effective cap.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_MAX = 1500
_REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = _REPO_ROOT / "scripts" / "file_length_baseline.txt"


def _norm(path: str) -> str:
    """Repo-relative, forward-slash key (matches git ls-files / pre-commit)."""
    p = Path(path)
    try:
        p = p.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        pass
    return p.as_posix()


def count_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def load_baseline(path: Path = BASELINE_PATH) -> Dict[str, int]:
    """Parse 'relative/path.py <count>' lines. Missing file -> empty baseline."""
    baseline: Dict[str, int] = {}
    if not path.is_file():
        return baseline
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, _, count = line.rpartition(" ")
        if rel and count.isdigit():
            baseline[_norm(rel)] = int(count)
    return baseline


def tracked_py_files() -> List[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py"],
            capture_output=True,
            text=True,
            check=True,
            cwd=_REPO_ROOT,
        )
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [str(p.relative_to(_REPO_ROOT)) for p in _REPO_ROOT.rglob("*.py")]


def find_offenders(
    files: List[str], baseline: Dict[str, int], max_lines: int = DEFAULT_MAX
) -> List[Tuple[str, int, int]]:
    """Return (path, count, effective_cap) for every file over its cap.

    effective_cap = the baselined count if the file is grandfathered, else
    max_lines. A baselined file is allowed up to its recorded size (no growth).
    """
    offenders: List[Tuple[str, int, int]] = []
    for f in files:
        p = Path(f)
        if p.suffix != ".py" or not p.is_file():
            continue
        key = _norm(f)
        cap = baseline.get(key, max_lines)
        n = count_lines(p)
        if n > cap:
            offenders.append((key, n, cap))
    return offenders


def write_baseline(path: Path = BASELINE_PATH, max_lines: int = DEFAULT_MAX) -> int:
    """Regenerate the baseline from the current tree (files over max_lines)."""
    entries = []
    for f in tracked_py_files():
        fp = _REPO_ROOT / f
        n = count_lines(fp)
        if n > max_lines:
            entries.append((_norm(f), n))
    entries.sort(key=lambda x: (-x[1], x[0]))
    header = (
        "# file-length ratchet baseline - paths already over the cap, pinned to\n"
        "# their current line count. They may SHRINK but not GROW; new files must\n"
        f"# stay <= {max_lines}. Regenerate with: python scripts/check_file_length.py --write-baseline\n"
    )
    path.write_text(header + "".join(f"{rel} {n}\n" for rel, n in entries), encoding="utf-8")
    return len(entries)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max", type=int, default=DEFAULT_MAX, dest="max_lines")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)

    if args.write_baseline:
        n = write_baseline(max_lines=args.max_lines)
        print(f"Wrote baseline with {n} grandfathered file(s) over {args.max_lines} lines.")
        return 0

    files = args.files or tracked_py_files()
    baseline = load_baseline()
    offenders = find_offenders(files, baseline, args.max_lines)
    if offenders:
        print("File-length ratchet violated:", file=sys.stderr)
        for rel, n, cap in sorted(offenders, key=lambda x: -x[1]):
            why = f"cap {cap}" + (
                " (baseline - must shrink, not grow)" if cap != args.max_lines else ""
            )
            print(f"  {n:>6} lines  {rel}  [{why}]", file=sys.stderr)
        print(
            "\nSplit the file by responsibility (keep imports stable via __init__ "
            "re-exports), or - only if deliberate - raise the cap / update the baseline.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
