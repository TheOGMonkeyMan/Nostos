"""Tests for the file-length ratchet (scripts/check_file_length.py)."""

import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_file_length.py"
_spec = importlib.util.spec_from_file_location("check_file_length", _SCRIPT)
clf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clf)


def test_under_cap_ok(tmp_path):
    f = tmp_path / "small.py"
    f.write_text("x = 1\n" * 10)
    assert clf.find_offenders([str(f)], {}, max_lines=100) == []


def test_over_cap_flagged(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("x = 1\n" * 50)
    off = clf.find_offenders([str(f)], {}, max_lines=10)
    assert len(off) == 1
    assert off[0][1] == 50  # counted lines
    assert off[0][2] == 10  # effective cap


def test_grandfathered_not_flagged(tmp_path):
    """A baselined file pinned at its current size is allowed (no growth)."""
    f = tmp_path / "god.py"
    f.write_text("x = 1\n" * 50)
    baseline = {clf._norm(str(f)): 50}
    assert clf.find_offenders([str(f)], baseline, max_lines=10) == []


def test_grandfathered_growth_flagged(tmp_path):
    """A baselined file that GREW past its recorded size is a violation."""
    f = tmp_path / "god.py"
    f.write_text("x = 1\n" * 60)
    baseline = {clf._norm(str(f)): 50}
    off = clf.find_offenders([str(f)], baseline, max_lines=10)
    assert len(off) == 1
    assert off[0][2] == 50  # capped at the baselined count, not the global max


def test_non_python_files_ignored(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("line\n" * 50)
    assert clf.find_offenders([str(f)], {}, max_lines=10) == []


def test_load_baseline_parses(tmp_path):
    bl = tmp_path / "baseline.txt"
    bl.write_text("# comment\nsrc/foo.py 4043\nroutes/bar.py 3158\n")
    parsed = clf.load_baseline(bl)
    assert parsed["src/foo.py"] == 4043
    assert parsed["routes/bar.py"] == 3158
