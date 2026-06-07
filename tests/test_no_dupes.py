"""Phase 2.1 / ADR-028: no module is duplicated under both src/ and services/."""
import importlib.util
from pathlib import Path


def _load_checker():
    path = Path(__file__).resolve().parent.parent / "scripts" / "check_no_dupes.py"
    spec = importlib.util.spec_from_file_location("check_no_dupes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_src_services_module_dupes():
    dupes = _load_checker().find_dupes()
    assert dupes == [], f"src/ <-> services/ duplicate modules: {dupes}"


def test_checker_root_init_is_not_flagged(tmp_path):
    # Sanity: a package-root __init__.py under both trees is NOT a dupe, but a
    # real submodule present in both IS.
    checker = _load_checker()
    (tmp_path / "src").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "services" / "__init__.py").write_text("")
    assert checker.find_dupes(tmp_path) == []
    (tmp_path / "src" / "thing.py").write_text("")
    (tmp_path / "services" / "thing.py").write_text("")
    assert checker.find_dupes(tmp_path) == ["thing.py"]
