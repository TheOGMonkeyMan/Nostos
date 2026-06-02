"""Shared test configuration — ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# core.database builds its SQLite engine at import time from DATABASE_URL
# (default sqlite:///./data/app.db). On a clean checkout the gitignored ./data
# dir does not exist, so importing core.database during collection fails with
# "unable to open database file". Create it up front (relative to both the CWD
# the engine resolves against and the project root) so the suite is
# self-sufficient on any fresh checkout — CI runners included.
for _data_dir in {os.path.join(os.getcwd(), "data"), os.path.join(_PROJECT_ROOT, "data")}:
    os.makedirs(_data_dir, exist_ok=True)

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db
