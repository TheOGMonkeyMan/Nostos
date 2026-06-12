"""Maintenance / tidy scheduler actions (ADR-043, Phase 2.2).

action_tidy_sessions, action_tidy_documents and action_consolidate_memory, split
verbatim out of src/builtin_actions.py. Re-imported there so the BUILTIN_ACTIONS
registry + existing callers are unchanged.
"""

import logging
from typing import Tuple

from src.actions.base import TaskNoop

logger = logging.getLogger(__name__)


async def action_tidy_sessions(owner: str, **kwargs) -> Tuple[str, bool]:
    """Delete empty/throwaway sessions for the owner. Pure heuristic —
    the LLM folder-sort phase is skipped (user opted to keep this task
    LLM-free; sorting can be triggered manually via the Chats UI)."""
    try:
        import asyncio
        from src.session_actions import run_auto_sort
        result = await asyncio.wait_for(run_auto_sort(owner, skip_llm=True), timeout=60)
        return result, True
    except asyncio.TimeoutError:
        logger.error("tidy_sessions action timed out")
        return "Chat session tidy timed out", False
    except Exception as e:
        logger.error(f"tidy_sessions action failed: {e}")
        return str(e), False


async def action_tidy_documents(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run tidy on documents for the owner."""
    try:
        from src.document_actions import run_document_tidy
        result = await run_document_tidy(owner)
        return result, True
    except Exception as e:
        logger.error(f"tidy_documents action failed: {e}")
        return str(e), False


async def action_consolidate_memory(owner: str, **kwargs) -> Tuple[str, bool]:
    """Consolidate/deduplicate memories for the owner."""
    try:
        import json
        import re
        from src.constants import DATA_DIR
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async
        from src.memory import MemoryManager

        manager = MemoryManager(DATA_DIR)
        all_memories = manager.load_all()

        # When the scheduled task was created without an explicit owner
        # (the common case for built-in housekeeping rows), task.owner
        # arrives as "" or None. The old filter then required memories
        # with a matching empty owner — which excluded every real memory
        # and the action no-op'd with "nothing to consolidate" even
        # though hundreds of memories were sitting there. Treat empty
        # owner as "no filter" so the housekeeping action actually runs.
        _owner_clean = (owner or "").strip()
        if _owner_clean:
            def _belongs_to_owner(mem: dict) -> bool:
                mem_owner = (mem.get("owner") or "").strip()
                return mem_owner == _owner_clean or not mem_owner
        else:
            def _belongs_to_owner(mem: dict) -> bool:
                return True

        owner_memories = [m for m in all_memories if _belongs_to_owner(m)]
        if not owner_memories:
            raise TaskNoop("no memories to consolidate")

        url, model, headers = resolve_endpoint("utility", owner=owner)
        if not url or not model:
            url, model, headers = resolve_endpoint("default", owner=owner)

        if url and model and len(owner_memories) >= 2:
            try:
                items = [
                    {
                        "id": m.get("id"),
                        "category": m.get("category", "fact"),
                        "text": (m.get("text") or "").strip()[:600],
                    }
                    for m in owner_memories
                    if m.get("id") and (m.get("text") or "").strip()
                ]
                prompt = (
                    "You are tidying a user's saved personal memories. Return ONLY raw JSON, no markdown.\n"
                    "Remove memories that are empty, broken, trivial conversation filler, duplicates, or obsolete "
                    "because a clearer newer memory replaces them. Preserve useful personal facts, preferences, "
                    "contacts, project context, and instructions. If memories conflict, keep the clearest/latest "
                    "one and drop the obsolete one.\n\n"
                    "JSON shape:\n"
                    "{\"keep\":[{\"id\":\"existing id\",\"text\":\"cleaned text\",\"category\":\"fact|preference|identity|event|contact|project|instruction\"}],"
                    "\"drop\":[{\"id\":\"existing id\",\"reason\":\"short reason\"}]}\n\n"
                    f"MEMORIES:\n{json.dumps(items, ensure_ascii=False)}"
                )
                raw = await llm_call_async(
                    url=url,
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=4096,
                    headers=headers,
                    timeout=120,
                )
                from src.text_helpers import strip_think

                raw = strip_think(raw or "", prose=False, prompt_echo=False).strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
                start = raw.find("{")
                end = raw.rfind("}")
                if start != -1 and end != -1 and end > start:
                    decision = json.loads(raw[start:end + 1])
                    keep_items = decision.get("keep") if isinstance(decision, dict) else None
                    drop_items = decision.get("drop") if isinstance(decision, dict) else None
                    if isinstance(keep_items, list) and isinstance(drop_items, list):
                        by_id = {m.get("id"): m for m in owner_memories}
                        keep_ids = set()
                        cleaned_by_id = {}
                        for item in keep_items:
                            if not isinstance(item, dict):
                                continue
                            mid = item.get("id")
                            if mid not in by_id:
                                continue
                            text = (item.get("text") or "").strip()
                            if not text:
                                continue
                            keep_ids.add(mid)
                            cleaned_by_id[mid] = {
                                "text": text,
                                "category": (item.get("category") or by_id[mid].get("category") or "fact").strip(),
                            }

                        if keep_ids:
                            changed_text = 0
                            kept_all = []
                            for mem in all_memories:
                                if not _belongs_to_owner(mem):
                                    kept_all.append(mem)
                                    continue
                                mid = mem.get("id")
                                if mid not in keep_ids:
                                    continue
                                cleaned = cleaned_by_id.get(mid) or {}
                                if cleaned.get("text") and cleaned["text"] != mem.get("text"):
                                    mem["text"] = cleaned["text"]
                                    changed_text += 1
                                if cleaned.get("category"):
                                    mem["category"] = cleaned["category"]
                                if owner and not mem.get("owner"):
                                    mem["owner"] = owner
                                kept_all.append(mem)

                            removed = len(owner_memories) - len(keep_ids)
                            if removed or changed_text:
                                manager.save(kept_all)
                                reasons = [
                                    (d.get("reason") or "").strip()
                                    for d in drop_items
                                    if isinstance(d, dict) and (d.get("reason") or "").strip()
                                ][:3]
                                reason_text = f": {'; '.join(reasons)}" if reasons else ""
                                return (
                                    f"AI tidied {len(owner_memories)} memories: "
                                    f"removed {removed}, cleaned {changed_text}{reason_text}",
                                    True,
                                )

                            raise TaskNoop(f"AI scanned {len(owner_memories)} memories, no changes")
            except TaskNoop:
                raise
            except Exception as ai_err:
                logger.warning("AI memory tidy failed; falling back to duplicate cleanup: %s", ai_err)

        seen = {}
        keep_ids = set()
        removed_examples = []
        for mem in owner_memories:
            text = (mem.get("text") or "").strip()
            key = " ".join(text.lower().split())
            if not key:
                removed_examples.append("(empty)")
                continue
            if key in seen:
                if len(removed_examples) < 3:
                    removed_examples.append(text[:60] + ("..." if len(text) > 60 else ""))
                continue
            seen[key] = mem
            keep_ids.add(mem.get("id"))

        removed = len(owner_memories) - len(keep_ids)
        if removed == 0:
            raise TaskNoop(f"scanned {len(owner_memories)} memories, no duplicates")

        kept_all = [
            m for m in all_memories
            if not _belongs_to_owner(m) or m.get("id") in keep_ids
        ]
        if owner:
            for mem in kept_all:
                if mem.get("id") in keep_ids and not mem.get("owner"):
                    mem["owner"] = owner
        manager.save(kept_all)
        preview = "; ".join(removed_examples)
        extra = f" (+{removed - len(removed_examples)} more)" if removed > len(removed_examples) else ""
        return f"Removed {removed} duplicate(s) of {len(owner_memories)}: {preview}{extra}", True
    except Exception as e:
        logger.error(f"consolidate_memory action failed: {e}")
        return str(e), False
