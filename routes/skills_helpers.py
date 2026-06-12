"""Skill-route helper functions (ADR-048, Phase 2.2).

Cohesive helper groups carved out of routes/skills_routes.py (which is mostly
module-level helper functions plus the route setup). This module starts with the
skill-audit helpers; they are re-imported into skills_routes so the route closures
keep calling them as module globals.
"""

import re
from typing import Optional


def _audit_auto_publish_policy(owner) -> tuple[bool, float]:
    """Return (auto_publish_enabled, minimum_confidence) for audit finalization."""
    try:
        from routes.prefs_routes import _load_for_user
        prefs = _load_for_user(owner) or {}
    except Exception:
        prefs = {}
    try:
        from src.settings import get_setting
        default_min = get_setting("skill_autosave_min_confidence", 0.85)
    except Exception:
        default_min = 0.85
    enabled = bool(prefs.get("auto_approve_skills", True))
    try:
        min_conf = float(prefs.get("skill_min_confidence", default_min))
    except (TypeError, ValueError):
        min_conf = 0.85
    return enabled, max(0.0, min(1.0, min_conf))


def _skill_duplicate_blocker(skills_manager, name: str, owner) -> Optional[str]:
    """Cheap duplicate guard matching the UI's duplicate grouping.

    The LLM necessity check catches semantic redundancy, but the UI also has a
    cheap similarity pass. Use the same broad signal before auto-publishing so
    a high-scoring lower-priority duplicate stays draft.
    """
    import re as _re

    def _tokens(sk: dict) -> set[str]:
        text = " ".join([
            str(sk.get("name") or ""),
            str(sk.get("description") or ""),
            str(sk.get("when_to_use") or ""),
            " ".join(sk.get("procedure") or []),
            " ".join(sk.get("tags") or []),
        ]).lower()
        text = _re.sub(r"-\d+\b", "", text)
        return {
            t for t in _re.split(r"[^a-z0-9]+", text)
            if len(t) > 2 and t not in {"the", "and", "with", "for", "from", "using"}
        }

    def _sim(a: dict, b: dict) -> float:
        A, B = _tokens(a), _tokens(b)
        if not A or not B:
            return 0.0
        return len(A & B) / max(1, len(A | B))

    def _base(n: str) -> str:
        return _re.sub(r"-\d+$", "", str(n or ""))

    def _score(sk: dict) -> float:
        return (
            (100000 if (sk.get("status") == "published") else 0)
            + int(sk.get("uses") or 0) * 100
            + round(float(sk.get("confidence") or 0) * 100)
            + (-5 if sk.get("audit_by_teacher") else 0)
            - (len(str(sk.get("name") or "")) / 1000)
        )

    skills = skills_manager.load(owner=owner)
    current = next((s for s in skills if (s.get("name") or s.get("id")) == name), None)
    if not current:
        return None
    duplicates = []
    cur_name = current.get("name") or current.get("id") or name
    for other in skills:
        other_name = other.get("name") or other.get("id")
        if not other_name or other_name == cur_name:
            continue
        if _base(cur_name) == _base(other_name) or _sim(current, other) >= 0.38:
            duplicates.append(other)
    if not duplicates:
        return None
    keeper = sorted([current, *duplicates], key=_score, reverse=True)[0]
    keeper_name = keeper.get("name") or keeper.get("id") or ""
    if keeper_name and keeper_name != cur_name:
        try:
            skills_manager.set_necessity(
                cur_name,
                False,
                [keeper_name],
                f"Lower-priority duplicate of {keeper_name}",
            )
        except Exception:
            pass
        return keeper_name
    return None


def _audit_flag_text(*parts) -> str:
    text_parts = []
    for part in parts:
        if isinstance(part, dict):
            text_parts.extend(str(v or "") for v in part.values())
        elif isinstance(part, (list, tuple, set)):
            text_parts.extend(str(v or "") for v in part)
        else:
            text_parts.append(str(part or ""))
    return " ".join(text_parts).lower()


def _audit_generic_blocker(skill: Optional[dict], necessity: Optional[dict],
                           verdict_data: Optional[dict]) -> Optional[str]:
    """Return a short reason when a generic/trivial skill must stay draft."""
    generic_re = re.compile(
        r"\b(too[-\s]?generic|generic|trivial|capable assistant|without a saved|"
        r"not need|unnecessary|irrelevant)\b",
        re.I,
    )
    if isinstance(necessity, dict):
        reason = str(necessity.get("reason") or "")
        if necessity.get("necessary") is False and generic_re.search(reason):
            return reason or "Generic or unnecessary skill"

    if isinstance(skill, dict):
        tag_text = _audit_flag_text(skill.get("tags") or [])
        if generic_re.search(tag_text):
            return "Skill is tagged generic"

    if isinstance(verdict_data, dict):
        verdict_text = _audit_flag_text(
            verdict_data.get("summary"),
            verdict_data.get("issues") or [],
        )
        if generic_re.search(verdict_text):
            return "Audit flagged the skill as generic or unnecessary"
    return None


def _audit_finalize_status(skills_manager, name: str, owner, verdict: str,
                           confidence: Optional[float], necessity: Optional[dict] = None,
                           verdict_data: Optional[dict] = None) -> str:
    """Apply the user's audit publishing policy.

    Audit is the final pass: skills that pass at/above the threshold are
    published; anything below threshold, inconclusive, failing, or marked
    unnecessary/redundant is returned to draft. This intentionally demotes a
    previously-published skill when a fresh audit no longer clears policy.
    """
    auto_publish, min_conf = _audit_auto_publish_policy(owner)
    necessary = True
    current = next((s for s in skills_manager.load(owner=owner) if s.get("name") == name), None)
    generic_reason = _audit_generic_blocker(current, necessity, verdict_data)
    if isinstance(necessity, dict) and necessity.get("necessary") is False:
        necessary = False
    if generic_reason:
        necessary = False
        try:
            skills_manager.set_necessity(name, False, [], generic_reason)
        except Exception:
            pass
    duplicate_of = _skill_duplicate_blocker(skills_manager, name, owner) if verdict == "pass" else None
    if duplicate_of:
        necessary = False
    c = float(confidence or 0.0)
    status = "published" if (auto_publish and necessary and verdict == "pass" and c >= min_conf) else "draft"
    try:
        skills_manager.update_skill(name, {"status": status})
    except Exception:
        pass
    return status
