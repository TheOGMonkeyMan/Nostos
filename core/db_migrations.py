"""Database schema migrations (ADR-059, Phase 2.2).

The idempotent _migrate_* functions that evolve the SQLite schema on startup, split
verbatim out of core/database.py. They use raw sqlite3 (lazy-imported) + sqlalchemy
text/engine + DATABASE_URL/Base from core.db_base - NO ORM models - so this is a clean
leaf (no cycle). init_db() stays in core.database and imports these to run them in order.
"""

import json
import logging
import os
from datetime import datetime

from sqlalchemy import text

from core.db_base import engine, Base, DATABASE_URL

logger = logging.getLogger(__name__)


def _migrate_add_last_message_at_column():
    """Add last_message_at to sessions + backfill from the latest message
    timestamp per session (fallback to last_accessed / created_at when a
    session has no messages). Idempotent: column-add is guarded, and the
    backfill only touches rows where last_message_at is still NULL so it
    won't clobber live values on later restarts."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "last_message_at" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_message_at DATETIME")
        # Backfill any NULL rows: newest message timestamp, else last_accessed,
        # else created_at. Only fills NULLs so it's safe on every startup.
        conn.execute(
            """
            UPDATE sessions
               SET last_message_at = COALESCE(
                   (SELECT MAX(timestamp) FROM chat_messages
                     WHERE chat_messages.session_id = sessions.id),
                   last_accessed,
                   created_at
               )
             WHERE last_message_at IS NULL
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sessions_last_message_at "
            "ON sessions(archived, last_message_at)"
        )
        conn.commit()
        conn.close()
        logging.getLogger(__name__).info("Migrated: added + backfilled 'last_message_at' on sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"last_message_at migration failed: {e}")

def _migrate_add_document_archived_column():
    """Add `archived` to documents (soft-archive flag). Guarded + idempotent."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(documents)")
        columns = [row[1] for row in cursor.fetchall()]
        if "archived" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN archived BOOLEAN DEFAULT 0")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'archived' to documents")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"documents.archived migration failed: {e}")


def _migrate_add_owner_column():
    """Add owner column to sessions table if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "owner" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_sessions_owner ON sessions(owner)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'owner' column to sessions")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check failed: {e}")

def _migrate_model_endpoints():
    """Recreate model_endpoints table if schema changed (url->base_url)."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "base_url" not in columns:
            conn.execute("DROP TABLE IF EXISTS model_endpoints")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: dropped old model_endpoints table (schema change)")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_endpoints migration check failed: {e}")

def _migrate_add_hidden_models_column():
    """Add hidden_models column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "hidden_models" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN hidden_models TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'hidden_models' column to model_endpoints")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"hidden_models migration failed: {e}")

def _migrate_add_model_endpoint_owner_column():
    """Add owner column to model_endpoints if it doesn't exist.

    Without this column, the per-user model picker query
    `(owner == user) | (owner IS NULL)` fails with `OperationalError:
    no such column: model_endpoints.owner`, leaving non-admin users
    with an empty picker even when `allowed_models` is unrestricted.
    Backfills NULL for existing rows (treated as shared by the filter).
    """
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "owner" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN owner VARCHAR")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_model_endpoints_owner ON model_endpoints(owner)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'owner' column + index to model_endpoints")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_endpoints.owner migration failed: {e}")


def _migrate_add_model_type_column():
    """Add model_type column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "model_type" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN model_type TEXT DEFAULT 'llm'")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'model_type' column to model_endpoints")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_type migration failed: {e}")

def _migrate_add_task_run_model_column():
    """Add model column to task_runs if it doesn't exist (records which model ran)."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(task_runs)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "model" not in columns:
            conn.execute("ALTER TABLE task_runs ADD COLUMN model TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'model' column to task_runs")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"task_runs model migration failed: {e}")

def _migrate_add_supports_tools_column():
    """Add supports_tools column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "supports_tools" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN supports_tools BOOLEAN")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'supports_tools' column to model_endpoints")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"supports_tools migration failed: {e}")


def _migrate_add_cached_models_column():
    """Add cached_models column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "cached_models" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN cached_models TEXT")
            conn.commit()
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"cached_models migration failed: {e}")

def _migrate_add_notes_sort_order():
    """Add sort_order, image_url, repeat columns to notes if they don't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(notes)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "sort_order" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN sort_order INTEGER DEFAULT 0")
        if columns and "image_url" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN image_url TEXT")
        if columns and "repeat" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN repeat TEXT DEFAULT 'none'")
        if columns and "ai_classification" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN ai_classification TEXT")
        if columns and "ai_content_hash" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN ai_content_hash TEXT")
        if columns and "agent_session_id" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN agent_session_id TEXT")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"notes migration failed: {e}")

def _migrate_add_mode_column():
    """Add mode column to sessions table if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "mode" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN mode TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'mode' column to sessions")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check for mode failed: {e}")

def _migrate_add_folder_column():
    """Add folder column to sessions table if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "folder" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN folder TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'folder' column to sessions")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check for folder failed: {e}")

def _migrate_add_token_columns():
    """Add cumulative token tracking columns to sessions table."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "total_input_tokens" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN total_input_tokens INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE sessions ADD COLUMN total_output_tokens INTEGER DEFAULT 0")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added token tracking columns to sessions")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check for token columns failed: {e}")

def _migrate_add_owner_to_table(table_name: str, index_name: str):
    """Generic helper: add owner TEXT column + index to a table if missing."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        columns = [row[1] for row in cursor.fetchall()]
        if "owner" not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN owner TEXT")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}(owner)")
            conn.commit()
            logging.getLogger(__name__).info(f"Migrated: added 'owner' column to {table_name}")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration owner column for {table_name} failed: {e}")

def _migrate_add_multiuser_owner_columns():
    """Add owner column to memories, gallery_images, user_tools, comparisons."""
    _migrate_add_owner_to_table("memories", "ix_memories_owner")
    _migrate_add_owner_to_table("gallery_images", "ix_gallery_images_owner")
    _migrate_add_owner_to_table("user_tools", "ix_user_tools_owner")
    _migrate_add_owner_to_table("comparisons", "ix_comparisons_owner")
    _migrate_add_owner_to_table("api_tokens", "ix_api_tokens_owner")
    # documents derived ownership from their session join until this column
    # existed; the legacy-owner sweep (below) backfills it on the next boot.
    _migrate_add_owner_to_table("documents", "ix_documents_owner")


def _migrate_add_api_token_scopes_column():
    """Add API token scopes for existing installs.

    Existing tokens get the current only-supported scope (`chat`) so they keep
    working after the schema migration, but route checks no longer treat tokens
    as an unscoped bearer credential.
    """
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(api_tokens)").fetchall()]
        if columns and "scopes" not in columns:
            conn.execute("ALTER TABLE api_tokens ADD COLUMN scopes TEXT NOT NULL DEFAULT 'chat'")
            conn.execute("UPDATE api_tokens SET scopes = 'chat' WHERE scopes IS NULL OR scopes = ''")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added scopes column to api_tokens")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"api_tokens.scopes migration failed: {e}")

def _migrate_assign_legacy_owner():
    """Assign all null-owner data to the first (admin) user.

    Runs at boot AND periodically (sweep_null_owners) so that data created
    while auth is disabled / middleware is bypassed via localhost doesn't
    sit in the DB as world-visible. Previously only swept 5 tables; the
    actual set of owner-bearing tables is much larger.
    """
    import sqlite3
    import json as _json

    # Find admin user from auth.json. The auth schema uses `is_admin: True`,
    # not `role: "admin"` — old code looked for the wrong field and silently
    # fell through to "first user" every time.
    auth_path = os.path.join(os.path.dirname(DATABASE_URL.replace("sqlite:///", "")), "auth.json")
    if not os.path.isabs(auth_path):
        auth_path = os.path.join("data", "auth.json")
    admin_user = None
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            auth_data = _json.load(f)
        users = auth_data.get("users", {})
        if users:
            for uname, udata in users.items():
                if udata.get("is_admin") is True:
                    admin_user = uname
                    break
            if not admin_user:
                admin_user = next(iter(users))
    except Exception:
        pass

    if not admin_user:
        return

    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return

    logger = logging.getLogger(__name__)
    try:
        conn = sqlite3.connect(db_path)
        # Every table with an `owner` column. New tables added later will be
        # picked up automatically because we only UPDATE when the column
        # exists; the explicit list documents intent.
        tables = [
            "sessions", "memories", "gallery_images", "user_tools",
            "comparisons", "documents", "signatures", "notes",
            "calendars", "calendar_events", "integrations",
            "scheduled_tasks", "task_runs", "crew_members",
            "gallery_albums", "gallery_people", "user_tool_data",
            "api_tokens", "webhooks",
        ]
        for table in tables:
            try:
                cursor = conn.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]
                if "owner" in columns:
                    res = conn.execute(f"UPDATE {table} SET owner = ? WHERE owner IS NULL", (admin_user,))
                    if res.rowcount > 0:
                        logger.info(f"Assigned {res.rowcount} legacy rows in {table} to '{admin_user}'")
            except Exception as e:
                logger.warning(f"Legacy owner assignment for {table} failed: {e}")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Legacy owner migration failed: {e}")

    # Also migrate memory.json
    mem_path = os.path.join("data", "memory.json")
    try:
        if os.path.exists(mem_path):
            with open(mem_path, "r", encoding="utf-8") as f:
                memories = _json.load(f)
            changed = False
            for m in memories:
                if not m.get("owner"):
                    m["owner"] = admin_user
                    changed = True
            if changed:
                with open(mem_path, "w", encoding="utf-8") as f:
                    _json.dump(memories, f, ensure_ascii=False, indent=2)
                logger.info(f"Assigned {sum(1 for _ in memories)} legacy memories in memory.json to '{admin_user}'")
    except Exception as e:
        logger.warning(f"memory.json legacy migration failed: {e}")

    # Also migrate user_prefs.json to per-user format
    prefs_path = os.path.join("data", "user_prefs.json")
    try:
        if os.path.exists(prefs_path):
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = _json.load(f)
            if "_users" not in prefs and prefs:
                # Flat format → nest under admin user
                new_prefs = {"_users": {admin_user: prefs}}
                with open(prefs_path, "w", encoding="utf-8") as f:
                    _json.dump(new_prefs, f, indent=2)
                logger.info(f"Migrated user_prefs.json to per-user format under '{admin_user}'")
    except Exception as e:
        logger.warning(f"user_prefs.json migration failed: {e}")


def _migrate_backfill_document_owner_from_session():
    """Backfill documents.owner from the owner of the linked chat session.

    Must run AFTER the owner column is added and BEFORE the blanket
    legacy-owner sweep, so session-linked docs get their *true* owner
    while only genuinely orphaned (sessionless) docs fall through to the
    admin assignment. Idempotent — only touches NULL-owner rows."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(documents)"))]
            if "owner" not in cols:
                return
            res = conn.execute(text(
                "UPDATE documents SET owner = ("
                "  SELECT s.owner FROM sessions s WHERE s.id = documents.session_id"
                ") WHERE owner IS NULL AND session_id IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM sessions s WHERE s.id = documents.session_id "
                "            AND s.owner IS NOT NULL)"
            ))
            conn.commit()
            if res.rowcount:
                logging.getLogger(__name__).info(
                    f"Backfilled owner on {res.rowcount} session-linked documents")
    except Exception as e:
        logging.getLogger(__name__).warning(f"document owner backfill: {e}")


def _migrate_add_tidy_verdict():
    """Add tidy_verdict column to documents table if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(documents)"))]
            if "tidy_verdict" not in cols:
                conn.execute(text("ALTER TABLE documents ADD COLUMN tidy_verdict VARCHAR"))
                conn.commit()
                logging.getLogger(__name__).info("Added tidy_verdict column to documents")
    except Exception as e:
        logging.getLogger(__name__).warning(f"tidy_verdict migration: {e}")


def _migrate_add_doc_source_email_cols():
    """Add source-email provenance columns to documents (for the Sign-and-Reply flow)."""
    cols_to_add = {
        "source_email_uid":        "VARCHAR",
        "source_email_folder":     "VARCHAR",
        "source_email_account_id": "VARCHAR",
        "source_email_message_id": "VARCHAR",
    }
    try:
        with engine.connect() as conn:
            existing = {r[1] for r in conn.execute(text("PRAGMA table_info(documents)"))}
            for col, spec in cols_to_add.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE documents ADD COLUMN {col} {spec}"))
                    logging.getLogger(__name__).info(f"Added {col} column to documents")
            # Index for lookup-by-message-id (the "find existing draft" path)
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_documents_source_email_message_id "
                "ON documents (source_email_message_id)"
            ))
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"doc source-email migration: {e}")

def _migrate_add_task_automation_columns():
    """Add automation columns to scheduled_tasks table if missing."""
    new_cols = {
        "task_type": "VARCHAR DEFAULT 'llm'",
        "action": "VARCHAR",
        "trigger_type": "VARCHAR DEFAULT 'schedule'",
        "trigger_event": "VARCHAR",
        "trigger_count": "INTEGER",
        "trigger_counter": "INTEGER DEFAULT 0",
    }
    try:
        with engine.connect() as conn:
            cols_info = list(conn.execute(text("PRAGMA table_info(scheduled_tasks)")))
            col_names = [r[1] for r in cols_info]
            for col_name, col_def in new_cols.items():
                if col_name not in col_names:
                    conn.execute(text(f"ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}"))

            # Check if prompt/schedule/scheduled_time are still NOT NULL — need table rebuild
            notnull_map = {r[1]: r[3] for r in cols_info}
            needs_rebuild = (
                notnull_map.get("prompt", 0) == 1 or
                notnull_map.get("schedule", 0) == 1 or
                notnull_map.get("scheduled_time", 0) == 1
            )
            if needs_rebuild:
                logging.getLogger(__name__).info("Rebuilding scheduled_tasks to make prompt/schedule/scheduled_time nullable")
                conn.execute(text("ALTER TABLE scheduled_tasks RENAME TO _old_scheduled_tasks"))
                conn.execute(text("""
                    CREATE TABLE scheduled_tasks (
                        id VARCHAR PRIMARY KEY,
                        owner VARCHAR,
                        name VARCHAR NOT NULL,
                        prompt TEXT,
                        schedule VARCHAR,
                        scheduled_time VARCHAR,
                        scheduled_day INTEGER,
                        scheduled_date DATETIME,
                        next_run DATETIME,
                        last_run DATETIME,
                        status VARCHAR,
                        output_target VARCHAR,
                        session_id VARCHAR,
                        model VARCHAR,
                        endpoint_url VARCHAR,
                        run_count INTEGER,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        task_type VARCHAR DEFAULT 'llm',
                        action VARCHAR,
                        trigger_type VARCHAR DEFAULT 'schedule',
                        trigger_event VARCHAR,
                        trigger_count INTEGER,
                        trigger_counter INTEGER DEFAULT 0
                    )
                """))
                conn.execute(text("""
                    INSERT INTO scheduled_tasks
                    SELECT id, owner, name, prompt, schedule, scheduled_time,
                           scheduled_day, scheduled_date, next_run, last_run,
                           status, output_target, session_id, model, endpoint_url,
                           run_count, created_at, updated_at,
                           task_type, action, trigger_type, trigger_event,
                           trigger_count, trigger_counter
                    FROM _old_scheduled_tasks
                """))
                conn.execute(text("DROP TABLE _old_scheduled_tasks"))

            conn.commit()
            logging.getLogger(__name__).info("Task automation columns migration complete")
    except Exception as e:
        logging.getLogger(__name__).warning(f"task automation migration: {e}")

def _migrate_add_oauth_config():
    """Add oauth_config column to mcp_servers table if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mcp_servers)"))]
            if "oauth_config" not in cols:
                conn.execute(text("ALTER TABLE mcp_servers ADD COLUMN oauth_config TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added oauth_config column to mcp_servers")
    except Exception as e:
        logging.getLogger(__name__).warning(f"oauth_config migration: {e}")

def _migrate_add_disabled_tools():
    """Add disabled_tools column to mcp_servers table if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mcp_servers)"))]
            if "disabled_tools" not in cols:
                conn.execute(text("ALTER TABLE mcp_servers ADD COLUMN disabled_tools TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added disabled_tools column to mcp_servers")
    except Exception as e:
        logging.getLogger(__name__).warning(f"disabled_tools migration: {e}")

def _migrate_add_task_v2_columns():
    """Add cron_expression, then_task_id, webhook_token to scheduled_tasks."""
    new_cols = {
        "cron_expression": "VARCHAR",
        "then_task_id": "VARCHAR",
        "webhook_token": "VARCHAR",
    }
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(scheduled_tasks)"))]
            for col_name, col_def in new_cols.items():
                if col_name not in cols:
                    conn.execute(text(f"ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}"))
            if "webhook_token" not in cols:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_scheduled_tasks_webhook ON scheduled_tasks(webhook_token)"))
            conn.commit()
            logging.getLogger(__name__).info("Task v2 columns migration complete")
    except Exception as e:
        logging.getLogger(__name__).warning(f"task v2 migration: {e}")

def _migrate_drop_ping_notes_tasks():
    """One-time cleanup: ping_notes and ping_events used to be seeded as
    user-facing tasks. They're now pure background scanners inside the
    scheduler (no LLM, don't belong in the Tasks UI). Remove existing rows
    + their runs for both. (tidy_sessions/documents/research stay as tasks.)"""
    targets = ("ping_notes", "ping_events")
    try:
        with engine.connect() as conn:
            for action in targets:
                conn.execute(text(
                    "DELETE FROM task_runs WHERE task_id IN "
                    "(SELECT id FROM scheduled_tasks WHERE action=:a)"
                ), {"a": action})
                r = conn.execute(text("DELETE FROM scheduled_tasks WHERE action=:a"), {"a": action})
                if r.rowcount:
                    logging.getLogger(__name__).info(f"Dropped {r.rowcount} {action} task row(s)")
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).debug(f"drop_ping_notes_tasks: {e}")


def _migrate_add_notifications_enabled():
    """Per-task notification on/off toggle (default ON)."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(scheduled_tasks)"))]
            if "notifications_enabled" not in cols:
                conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN notifications_enabled BOOLEAN DEFAULT 1"))
                conn.commit()
                logging.getLogger(__name__).info("Added notifications_enabled column to scheduled_tasks")
    except Exception as e:
        logging.getLogger(__name__).warning(f"notifications_enabled migration: {e}")


def _migrate_add_crew_member_id():
    """Add crew_member_id column to sessions and scheduled_tasks tables if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sessions)"))]
            if "crew_member_id" not in cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN crew_member_id TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added crew_member_id column to sessions")
            cols2 = [r[1] for r in conn.execute(text("PRAGMA table_info(scheduled_tasks)"))]
            if "crew_member_id" not in cols2:
                conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN crew_member_id TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added crew_member_id column to scheduled_tasks")
    except Exception as e:
        logging.getLogger(__name__).warning(f"crew_member_id migration: {e}")

def _migrate_add_assistant_columns():
    """Add is_default_assistant + timezone columns to crew_members for the personal-assistant feature."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(crew_members)"))]
            if "is_default_assistant" not in cols:
                conn.execute(text("ALTER TABLE crew_members ADD COLUMN is_default_assistant BOOLEAN DEFAULT 0"))
                conn.commit()
                logging.getLogger(__name__).info("Added is_default_assistant column to crew_members")
            if "timezone" not in cols:
                conn.execute(text("ALTER TABLE crew_members ADD COLUMN timezone TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added timezone column to crew_members")
    except Exception as e:
        logging.getLogger(__name__).warning(f"assistant columns migration: {e}")





