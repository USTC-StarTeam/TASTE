from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from finding_runtime import DATA_DIR


POLICY_FILENAME = "priority_venue_metadata_sources.json"


def _clean_key(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")


@lru_cache(maxsize=1)
def _policy_payload() -> dict[str, Any]:
    path = DATA_DIR / POLICY_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"venues": {}}
    return data if isinstance(data, dict) else {"venues": {}}


@lru_cache(maxsize=1)
def _policy_index() -> dict[str, dict[str, Any]]:
    venues = _policy_payload().get("venues")
    if not isinstance(venues, dict):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for venue_id, raw_policy in venues.items():
        if not isinstance(raw_policy, dict):
            continue
        policy = dict(raw_policy)
        policy["venue_id"] = str(venue_id)
        keys = {_clean_key(venue_id)}
        for alias in policy.get("aliases") or []:
            key = _clean_key(alias)
            if key:
                keys.add(key)
        for key in keys:
            index[key] = policy
    return index


def priority_venue_policy(
    *,
    venue_id: object = "",
    venue: object = "",
    source_adapter: object = "",
) -> dict[str, Any]:
    """Return strict metadata policy for a priority venue, or {} for generic venues."""
    index = _policy_index()
    for value in (venue_id, venue):
        key = _clean_key(value)
        if key in index:
            return dict(index[key])
    adapter = str(source_adapter or "").strip().lower()
    if adapter.startswith("openreview_reference") and _clean_key(venue) == "iclr":
        return dict(index.get("openreview_iclr", {}))
    return {}


def priority_venue_policy_for_audit(audit: dict[str, Any] | None) -> dict[str, Any]:
    audit = audit if isinstance(audit, dict) else {}
    return priority_venue_policy(
        venue_id=audit.get("venue_id") or audit.get("id") or "",
        venue=audit.get("venue") or audit.get("venue_name") or "",
        source_adapter=audit.get("source_adapter") or audit.get("adapter") or "",
    )


def policy_requires_full_abstracts(audit: dict[str, Any] | None) -> bool:
    policy = priority_venue_policy_for_audit(audit)
    return bool(policy and policy.get("full_abstract_required") and not policy.get("allow_title_only_verified_cache"))


def policy_requires_official_categories(audit: dict[str, Any] | None) -> bool:
    policy = priority_venue_policy_for_audit(audit)
    return bool(policy and policy.get("official_categories_expected"))


def policy_summary(policy: dict[str, Any]) -> dict[str, Any]:
    if not policy:
        return {}
    return {
        "venue_id": policy.get("venue_id") or "",
        "preferred_adapters": [
            str(item.get("adapter") or "")
            for item in policy.get("preferred_sources") or []
            if isinstance(item, dict) and item.get("adapter")
        ],
        "full_abstract_required": bool(policy.get("full_abstract_required")),
        "official_accepted_list_required": bool(policy.get("official_accepted_list_required", True)),
        "official_categories_expected": bool(policy.get("official_categories_expected")),
        "allow_title_only_verified_cache": bool(policy.get("allow_title_only_verified_cache")),
        "allow_indexed_abstract_enrichment": bool(policy.get("allow_indexed_abstract_enrichment")),
        "indexed_abstract_enrichment_sources": [
            str(item)
            for item in policy.get("indexed_abstract_enrichment_sources") or []
            if str(item)
        ],
        "fallback_policy": policy.get("fallback_policy") or "",
    }
