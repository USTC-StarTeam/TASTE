from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .local_index import LOCAL_DATABASE_DIR


CONFERENCE_QUALITY_TABLE = LOCAL_DATABASE_DIR / "conference_quality_levels.json"
JOURNAL_QUALITY_TABLE = LOCAL_DATABASE_DIR / "journal_quality_levels.json"


def _load_json(path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def _conference_table() -> dict[str, Any]:
    return _load_json(CONFERENCE_QUALITY_TABLE)


@lru_cache(maxsize=1)
def _journal_table() -> dict[str, Any]:
    return _load_json(JOURNAL_QUALITY_TABLE)


def _norm(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_/|:;,\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _metadata(item: dict) -> dict:
    metadata = item.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _venue_ids(item: dict) -> set[str]:
    metadata = _metadata(item)
    ids = {
        str(item.get("venue_id") or ""),
        str(metadata.get("venue_id") or ""),
    }
    return {value for value in ids if value}


def _venue_names(item: dict) -> set[str]:
    return {
        _norm(item.get("venue")),
        _norm(item.get("source")),
    } - {""}


def _label_candidates(item: dict) -> set[str]:
    metadata = _metadata(item)
    values = [
        item.get("track"),
        item.get("category"),
        item.get("primary_area"),
        metadata.get("track"),
        metadata.get("category"),
        metadata.get("primary_area"),
        metadata.get("presentation_type"),
        metadata.get("presentation_label"),
    ]
    source_records = metadata.get("source_records")
    if isinstance(source_records, dict):
        for record in source_records.values():
            if isinstance(record, dict):
                values.extend([
                    record.get("track"),
                    record.get("category"),
                    record.get("primary_area"),
                    record.get("presentation_type"),
                    record.get("presentation_label"),
                ])
    return {_norm(value) for value in values if _norm(value)}


def _quality_payload(kind: str, source_file: str, tier: str, bonus: object, reason: str) -> dict[str, Any]:
    try:
        numeric_bonus = float(bonus)
    except (TypeError, ValueError):
        numeric_bonus = 0.0
    return {
        "quality_kind": kind,
        "quality_tier": tier or "unknown",
        "quality_bonus_available": round(max(0.0, numeric_bonus), 2),
        "quality_bonus": 0.0,
        "quality_source": source_file,
        "quality_reason": reason,
    }


def _lookup_conference_quality(item: dict) -> dict[str, Any] | None:
    table = _conference_table()
    conferences = table.get("conferences")
    if not isinstance(conferences, dict):
        return None
    venue_ids = _venue_ids(item)
    venue_names = _venue_names(item)
    labels = _label_candidates(item)
    year = str(item.get("year") or "")
    for key, conference in conferences.items():
        if not isinstance(conference, dict):
            continue
        table_ids = {str(value) for value in conference.get("venue_ids", []) if value}
        table_names = {_norm(value) for value in conference.get("names", []) if _norm(value)}
        if not (venue_ids & table_ids or venue_names & table_names or _norm(key) in venue_names):
            continue
        years = conference.get("years") if isinstance(conference.get("years"), dict) else {}
        aliases = years.get(year, {}).get("label_aliases", {}) if isinstance(years.get(year), dict) else {}
        if isinstance(aliases, dict):
            for label in labels:
                mapping = aliases.get(label)
                if isinstance(mapping, dict):
                    return _quality_payload(
                        "conference",
                        "conference_quality_levels.json",
                        str(mapping.get("tier") or "unknown"),
                        mapping.get("bonus", 0.0),
                        f"Matched conference label '{label}' for {key} {year}.",
                    )
        return _quality_payload(
            "conference",
            "conference_quality_levels.json",
            "unknown",
            0.0,
            f"Conference quality table has {key} {year}, but no item label matched.",
        )
    return None


def _lookup_journal_quality(item: dict) -> dict[str, Any] | None:
    table = _journal_table()
    journals = table.get("journals")
    if not isinstance(journals, dict):
        return None
    metadata = _metadata(item)
    source = str(item.get("source") or "")
    slug = str(metadata.get("journal_slug") or "").strip()
    candidates = set(_venue_ids(item))
    if source == "nature" and slug:
        candidates.add(f"nature_family_{slug}")
    if source == "science" and slug:
        candidates.add(f"science_family_{slug}")
    for journal_id in candidates:
        journal = journals.get(journal_id)
        if isinstance(journal, dict):
            return _quality_payload(
                "journal",
                "journal_quality_levels.json",
                str(journal.get("tier") or "unknown"),
                journal.get("bonus", 0.0),
                str(journal.get("notes") or f"Matched journal quality table entry {journal_id}."),
            )
    venue_name = _norm(item.get("venue"))
    if venue_name:
        for journal_id, journal in journals.items():
            if not isinstance(journal, dict):
                continue
            names = {_norm(value) for value in journal.get("names", []) if _norm(value)}
            if venue_name in names:
                return _quality_payload(
                    "journal",
                    "journal_quality_levels.json",
                    str(journal.get("tier") or "unknown"),
                    journal.get("bonus", 0.0),
                    str(journal.get("notes") or f"Matched journal quality table entry {journal_id}."),
                )
    return None


def attach_quality_metadata(item: dict) -> dict:
    quality = _lookup_journal_quality(item) or _lookup_conference_quality(item)
    if not quality:
        return item
    item.update(quality)
    metadata = item.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["quality"] = dict(quality)
    return item


def attach_quality_metadata_many(items: list[dict]) -> list[dict]:
    return [attach_quality_metadata(item) for item in items]
