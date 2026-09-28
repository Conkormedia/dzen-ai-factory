"""Owner-stated positioning layered over the auto-built knowledge dossier.

The dossier is an LLM's reading of the site and brief; when the owner says how
the product must be pitched ("OTP is customer login, not account protection"),
that statement has to win for both topic generation and article writing -
otherwise the next re-research quietly brings the old framing back."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

LIST_KEYS = ("audience", "value_props", "features", "pains", "objections", "facts", "content_pillars",
             "cta_variants")


def owner_of(project: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(project.get("owner_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        log.warning("project %s has invalid owner_json, ignoring it", project.get("slug"))
        return {}
    return data if isinstance(data, dict) else {}


def apply_owner(knowledge: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Owner lists go first (or replace the dossier's list for keys named in
    "replace"); dossier items matching "drop_patterns" are removed so a
    site-derived claim can't contradict the owner (e.g. founding year)."""
    owner = owner_of(project)
    if not owner:
        return knowledge
    merged = dict(knowledge)
    drop = [re.compile(p, re.I) for p in owner.get("drop_patterns") or []]
    replace = set(owner.get("replace") or [])
    for key in LIST_KEYS:
        own = [x for x in owner.get(key) or [] if x]
        if key in replace:
            merged[key] = own
            continue
        base = [x for x in knowledge.get(key) or [] if not any(r.search(str(x)) for r in drop)]
        merged[key] = own + [x for x in base if x not in own]
    for key in ("summary", "tone"):
        if owner.get(key):
            merged[key] = owner[key]
    merged["platforms"] = platforms_of(project)
    return merged


def platforms_of(project: dict[str, Any]) -> list[str]:
    """Third-party platforms the product itself runs on (BizGateWay's own
    channels are WhatsApp and Telegram) - the one exception to the ban on
    naming anyone else, since an article can't explain the product without them."""
    return [str(x).strip() for x in owner_of(project).get("platforms") or [] if str(x).strip()]


def owner_directives(project: dict[str, Any]) -> str:
    """Prompt block with the owner's must-do / must-not; empty when unset."""
    owner = owner_of(project)
    focus = str(owner.get("focus") or "").strip()
    avoid = [str(x).strip() for x in owner.get("avoid") or [] if str(x).strip()]
    platforms = platforms_of(project)
    if not focus and not avoid and not platforms:
        return ""
    out = "ПОЗИЦИОНИРОВАНИЕ ОТ ВЛАДЕЛЬЦА (главный приоритет, важнее всего остального в этом брифе):\n"
    if focus:
        out += f"{focus}\n"
    if platforms:
        out += (f"Разрешённые платформы: {', '.join(platforms)} — это каналы самого сервиса, называй их прямо "
                "по имени (в заголовке тоже, если тема про них); любые другие сторонние названия по-прежнему запрещены.\n")
    if avoid:
        out += "Так НЕЛЬЗЯ:\n" + "\n".join(f"- {x}" for x in avoid) + "\n"
    return out
