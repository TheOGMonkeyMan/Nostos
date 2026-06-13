"""Phase 2.2 (ADR-058): verify the core/db_base.py foundational-primitives split.

Base, TimestampMixin, DATABASE_URL, engine, SessionLocal and EncryptedText moved
verbatim out of core/database.py into a dependency-free leaf core/db_base.py,
re-imported so the ORM models + migrations + the ~46 external importers all share
the SAME objects. The load-bearing property is IDENTITY: `core.database.Base IS
core.db_base.Base`, so every model subclasses the same declarative base and
`from core.database import Base/SessionLocal/engine` keeps returning the right
objects. The full (DB-heavy) suite is the behavioral gate; this pins the identity.
"""

import core.db_base as b
import core.database as d


def test_primitive_identity_preserved_across_modules():
    assert d.Base is b.Base
    assert d.SessionLocal is b.SessionLocal
    assert d.engine is b.engine
    assert d.EncryptedText is b.EncryptedText
    assert d.TimestampMixin is b.TimestampMixin
    assert d.DATABASE_URL == b.DATABASE_URL


def test_all_models_registered_on_the_shared_base():
    # Models stay in core.database and subclass the re-imported Base, so they
    # register on the shared metadata (24 tables at time of writing).
    assert b.Base is d.Base
    assert len(d.Base.metadata.tables) >= 20
    assert "sessions" in d.Base.metadata.tables
    assert issubclass(d.Session, b.Base)
