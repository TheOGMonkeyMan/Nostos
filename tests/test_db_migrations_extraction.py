"""Phase 2.2 (ADR-059): verify the core/db_migrations.py schema-migration split.

The first 29 contiguous, model-free _migrate_* functions moved verbatim out of
core/database.py into a clean leaf core/db_migrations.py (imports engine/Base/
DATABASE_URL from core.db_base + sqlalchemy.text + stdlib; NO ORM models, no cycle).
init_db() stays in core.database and re-imports them by name to run them at startup;
the 4 interspersed models (Note/CalendarCal/CalendarEvent/Integration) and the trailing
_migrate_seed_email_account stay put. The full (DB-heavy) suite is the behavioral gate -
importing core.database executes init_db(), so every test exercises the moved code. This
pins the structural facts so a future edit cannot quietly re-merge or re-home them.
"""

import core.database as d
import core.db_migrations as m


def test_moved_migrations_live_in_db_migrations():
    # A representative sample of the 29 moved fns now belong to core.db_migrations,
    # yet remain callable via core.database (re-imported -> SAME object).
    for name in (
        "_migrate_add_last_message_at_column",
        "_migrate_model_endpoints",
        "_migrate_add_owner_to_table",
        "_migrate_add_assistant_columns",
    ):
        fn = getattr(d, name)
        assert fn.__module__ == "core.db_migrations", name
        assert fn is getattr(m, name), name


def test_models_and_seed_migration_stay_in_database():
    # The 4 ORM models interspersed in the original migration region stay home...
    for model in ("Note", "CalendarCal", "CalendarEvent", "Integration"):
        assert getattr(d, model).__module__ == "core.database", model
    # ...as does the trailing seed migration (it follows the models, not the block).
    assert d._migrate_seed_email_account.__module__ == "core.database"


def test_db_migrations_is_a_clean_leaf():
    # No ORM models defined here; Base/engine/DATABASE_URL are the shared db_base objects.
    import core.db_base as b
    assert m.Base is b.Base
    assert m.engine is b.engine
    assert m.DATABASE_URL == b.DATABASE_URL


def test_init_db_intact_and_schema_complete():
    # init_db stays in core.database and, having run at import, produced the full schema.
    assert d.init_db.__module__ == "core.database"
    assert len(d.Base.metadata.tables) >= 20
    assert "sessions" in d.Base.metadata.tables
