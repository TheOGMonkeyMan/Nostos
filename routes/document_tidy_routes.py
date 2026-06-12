"""Document tidy routes (ADR-049, Phase 2.2).

The POST /api/documents/tidy (remove broken/empty documents) and POST
/api/documents/ai-tidy routes, split verbatim out of
routes/document_routes.py::setup_document_routes(). They use neither shared
module-level helper (_load_pdf_viewer_fitz / _locate_current_user_upload), so the
registrar takes only the router.
"""

import json
import logging
from typing import Any, Dict

from fastapi import HTTPException, Request

from core.database import Document, SessionLocal, Session as DbSession
from src.auth_helpers import get_current_user
from routes.document_helpers import _owner_session_filter, _derive_title

logger = logging.getLogger(__name__)


def register_tidy_routes(router):
    # ---- POST /api/documents/tidy — clean up broken/empty documents ----
    @router.post("/api/documents/tidy")
    async def tidy_documents(request: Request) -> Dict[str, Any]:
        """Fix empty titles and remove broken/empty documents (user's docs only)."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = (
                db.query(Document)
                .outerjoin(DbSession, Document.session_id == DbSession.id)
                .filter(Document.is_active == True)
                .filter((Document.archived == False) | (Document.archived.is_(None)))
            )
            q = _owner_session_filter(q, user)
            docs = q.all()
            fixed_titles = 0
            deleted = 0

            # Same junk-detection logic as the scheduled tidy_documents
            # action (src/document_actions.py). Keep these two in sync.
            import re as _re
            from src.document_actions import _JUNK_TITLES

            to_delete = []
            for doc in docs:
                content = (doc.current_content or "").strip()
                title_raw = (doc.title or "").strip()
                title = title_raw.lower()

                # Strip markdown noise to get a "real" character count
                stripped = _re.sub(r"^#{1,6}\s+", "", content, flags=_re.MULTILINE)
                stripped = _re.sub(r"[*_`>\-=]+", "", stripped)
                stripped = _re.sub(r"\s+", " ", stripped).strip()
                real_len = len(stripped)

                # Detect email-scaffold stubs: "To: \nSubject: \n---\n" style
                # bodies with nothing typed in. Stub = every meaningful line
                # is a header label (To:/From:/Subject:/...) with no real
                # value (blank, "empty", "(empty)", "-", "none", "n/a").
                _is_email_stub = False
                _HEADER_RE = _re.compile(r"^(to|from|cc|bcc|subject|reply-to):\s*(.*)$", _re.I)
                _PLACEHOLDER_VALS = {"", "empty", "(empty)", "-", "—", "none", "n/a", "na", "tbd"}
                if title in ("new email", "new mail", "new message") or doc.language == "email":
                    body_lines = [ln.strip() for ln in content.split("\n")
                                  if ln.strip() and ln.strip() != "---"]
                    def _is_filler(ln):
                        m = _HEADER_RE.match(ln)
                        if not m:
                            return False
                        val = (m.group(2) or "").strip().lower()
                        return val in _PLACEHOLDER_VALS
                    has_real_body = any(not _is_filler(ln) for ln in body_lines)
                    if body_lines and not has_real_body:
                        _is_email_stub = True

                # Hard-delete obviously empty / junk documents
                if not content or content in ("", "# Untitled"):
                    to_delete.append(doc); deleted += 1; continue
                if _is_email_stub:
                    to_delete.append(doc); deleted += 1; continue
                if title in _JUNK_TITLES:
                    to_delete.append(doc); deleted += 1; continue
                if real_len < 30:
                    to_delete.append(doc); deleted += 1; continue
                if "\n" not in content and real_len < 50:
                    to_delete.append(doc); deleted += 1; continue

                # Fix empty or placeholder titles on survivors
                if not title_raw or title_raw == "Untitled":
                    new_title = _derive_title(content)
                    if new_title and new_title != "Untitled":
                        doc.title = new_title
                        fixed_titles += 1

            for doc in to_delete:
                db.delete(doc)

            # Also clean up inactive empty docs from previous soft-deletes
            inactive_q = (
                db.query(Document)
                .outerjoin(DbSession, Document.session_id == DbSession.id)
                .filter(Document.is_active == False)
                .filter((Document.current_content == None) | (Document.current_content == ""))
            )
            inactive_q = _owner_session_filter(inactive_q, user)
            inactive_docs = inactive_q.all()
            for doc in inactive_docs:
                db.delete(doc)
            deleted += len(inactive_docs)

            db.commit()
            return {
                "fixed_titles": fixed_titles,
                "deleted": deleted,
                "message": f"Fixed {fixed_titles} title{'s' if fixed_titles != 1 else ''}, removed {deleted} empty document{'s' if deleted != 1 else ''}",
            }
        except Exception as e:
            db.rollback()
            logger.error(f"Document tidy failed: {e}")
            raise HTTPException(500, f"Tidy failed: {e}")
        finally:
            db.close()

    # ---- POST /api/documents/ai-tidy — AI-powered cleanup of junk/test documents ----
    @router.post("/api/documents/ai-tidy")
    async def ai_tidy_documents(request: Request) -> Dict[str, Any]:
        """Use AI to judge if documents are junk/test/accidental, then delete them.
        Caches verdicts so previously-reviewed docs are skipped."""
        from src.task_endpoint import resolve_task_endpoint
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async

        user = get_current_user(request)
        url, model, headers = resolve_task_endpoint()
        if not url or not model:
            # Fall back to default endpoint
            url, model, headers = resolve_endpoint("default")
        if not url or not model:
            raise HTTPException(500, "No endpoint configured for AI tidy")

        db = SessionLocal()
        try:
            q = (
                db.query(Document)
                .outerjoin(DbSession, Document.session_id == DbSession.id)
                .filter(Document.is_active == True)
                .filter((Document.archived == False) | (Document.archived.is_(None)))
            )
            q = _owner_session_filter(q, user)
            docs = q.all()

            # Only review docs that haven't been reviewed yet
            to_review = [d for d in docs if not d.tidy_verdict]
            if not to_review:
                return {"deleted": 0, "reviewed": 0, "message": "All documents already reviewed"}

            # Build a batch prompt — review up to 30 at a time
            batch = to_review[:30]
            doc_list = []
            for i, doc in enumerate(batch):
                preview = (doc.current_content or "")[:300].strip()
                doc_list.append(f"[{i}] title=\"{doc.title}\" lang={doc.language or 'text'} content_preview=\"{preview}\"")

            prompt = (
                "You are a document library cleaner. For each document below, decide if it is JUNK "
                "(test, accidental, placeholder, empty-ish, tool-test, throwaway) or KEEP (real content worth saving).\n\n"
                "Respond with ONLY a JSON array of verdicts, one per document, like: [\"junk\",\"keep\",\"junk\",...]\n"
                "No explanation, no markdown, just the JSON array.\n\n"
                + "\n".join(doc_list)
            )

            response = await llm_call_async(
                url, model,
                [{"role": "system", "content": "You classify documents as junk or keep. Respond only with a JSON array."},
                 {"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
                headers=headers,
                timeout=30,
            )

            # Parse verdicts
            import re
            match = re.search(r'\[.*?\]', response, re.DOTALL)
            if not match:
                raise HTTPException(500, "AI returned invalid response")

            import json as _json
            verdicts = _json.loads(match.group())

            deleted = 0
            reviewed = 0
            for i, doc in enumerate(batch):
                if i >= len(verdicts):
                    break
                verdict = verdicts[i].lower().strip()
                if verdict == "junk":
                    doc.tidy_verdict = "junk"
                    db.delete(doc)
                    deleted += 1
                else:
                    doc.tidy_verdict = "keep"
                reviewed += 1

            db.commit()
            return {
                "deleted": deleted,
                "reviewed": reviewed,
                "remaining": len(to_review) - len(batch),
                "message": f"Reviewed {reviewed}, removed {deleted} junk document{'s' if deleted != 1 else ''}",
            }
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"AI tidy failed: {e}")
            raise HTTPException(500, f"AI tidy failed: {e}")
        finally:
            db.close()
