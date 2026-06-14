"""Admin Danger Zone — per-category wipes.

Each endpoint is admin-only and truncates exactly one domain so the
user can selectively reset memory / skills / notes / etc. without
nuking everything. The catch-all `chats` endpoint mirrors the
existing /api/sessions/all so the Danger Zone speaks one URL pattern.

URL shape: DELETE /api/admin/wipe/{kind}
Kinds: chats, memory, skills, notes, tasks, documents, gallery, calendar.
"""

import json
import logging
import os
import shutil
from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from core.database import (
    SessionLocal,
    Session as DbSession,
    ChatMessage as DbChatMessage,
    Memory,
    Note,
    ScheduledTask,
    TaskRun,
    Document,
    DocumentVersion,
    GalleryImage,
    CalendarEvent,
    CalendarCal,
)
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)


def _wipe_memory_files():
    """Blank memory.json + drop the per-owner tidy-state sidecar so the
    next audit doesn't try to diff against gone memories."""
    for name in ("memory.json", "memory_tidy_state.json"):
        p = os.path.join(DATA_DIR, name)
        if not os.path.exists(p):
            continue
        try:
            if name == "memory.json":
                with open(p, "w", encoding="utf-8") as f:
                    json.dump([], f)
            else:
                os.remove(p)
        except OSError as e:
            logger.warning(f"Could not reset {name}: {e}")


def _rmtree_quiet(path: str):
    """rmtree that doesn't crash if the path doesn't exist."""
    if os.path.isdir(path):
        try:
            shutil.rmtree(path)
        except OSError as e:
            logger.warning(f"Could not remove {path}: {e}")


def setup_admin_wipe_routes(session_manager, memory_vector=None):
    """The session_manager is passed in so we can also clear its
    in-memory cache when wiping chats — without it the DB is empty
    but the next /api/sessions returns stale entries.

    memory_vector (the embedded LanceDB MemoryVectorStore, or None when the
    vector backend is degraded/absent) is threaded in so wiping "memory" also
    drops the semantic index. Without it, semantic search keeps surfacing
    wiped "ghost" memories.
    """
    router = APIRouter(prefix="/api/admin")

    @router.delete("/wipe/{kind}")
    def wipe(kind: str, request: Request):
        require_admin(request)
        kind = (kind or "").strip().lower()

        db = SessionLocal()
        try:
            if kind == "chats":
                count = db.query(DbSession).count()
                db.query(DbChatMessage).delete()
                db.query(DbSession).delete()
                db.commit()
                try:
                    session_manager.sessions.clear()
                except Exception:
                    pass
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "memory":
                count = db.query(Memory).count()
                db.query(Memory).delete()
                db.commit()
                _wipe_memory_files()
                # Drop the vector index too so semantic search doesn't return
                # ghosts. rebuild([]) drops + recreates the table empty. The SQL
                # wipe is already committed, so a vector-store hiccup must not
                # fail the whole request (that would falsely read as "nothing
                # wiped"); log and carry on.
                if memory_vector is not None and memory_vector.healthy:
                    try:
                        memory_vector.rebuild([])
                    except Exception as e:
                        logger.warning(f"Memory vector clear failed: {e}")
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "skills":
                # Skills live as SKILL.md files under data/skills/. Drop
                # the entire directory; the SkillsManager re-creates the
                # tree on next write.
                skills_dir = os.path.join(DATA_DIR, "skills")
                count = 0
                if os.path.isdir(skills_dir):
                    # Count SKILL.md files for the response — quick walk.
                    for _, _, files in os.walk(skills_dir):
                        count += sum(1 for f in files if f == "SKILL.md")
                    _rmtree_quiet(skills_dir)
                # Legacy fallback file
                legacy = os.path.join(DATA_DIR, "skills.json")
                if os.path.exists(legacy):
                    try:
                        os.remove(legacy)
                    except OSError:
                        pass
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "notes":
                count = db.query(Note).count()
                db.query(Note).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "tasks":
                # TaskRun rows reference tasks via FK — clear them first.
                db.query(TaskRun).delete()
                count = db.query(ScheduledTask).count()
                db.query(ScheduledTask).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "documents":
                # DocumentVersion FKs Document — clear children first.
                db.query(DocumentVersion).delete()
                count = db.query(Document).count()
                db.query(Document).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "gallery":
                count = db.query(GalleryImage).count()
                db.query(GalleryImage).delete()
                db.commit()
                # Also drop the upload dir so disk doesn't keep orphans.
                _rmtree_quiet(os.path.join(DATA_DIR, "gallery"))
                _rmtree_quiet(os.path.join(DATA_DIR, "gallery_uploads"))
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "calendar":
                # Events FK calendars — clear children first, then both.
                db.query(CalendarEvent).delete()
                count = db.query(CalendarCal).count()
                db.query(CalendarCal).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            raise HTTPException(400, f"Unknown wipe kind: {kind!r}")
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.exception(f"Wipe {kind} failed")
            raise HTTPException(500, f"Wipe {kind} failed: {e}")
        finally:
            db.close()

    return router
