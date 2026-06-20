from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())


_ABSTRACT_UI_CONTROL_RE = re.compile(
    r"(?:\s*(?:show\s+(?:more|less)|read\s+(?:more|less)|显示更多|显示较少|展开|收起)\s*[。.]?\s*)+$",
    re.IGNORECASE,
)


def _strip_abstract_ui_controls(value: object) -> str:
    return _ABSTRACT_UI_CONTROL_RE.sub("", _clean_text(str(value or ""))).strip()


def _presentation_type_from_url(url: object) -> str:
    text = str(url or "").lower()
    if re.search(r"/(?:best[-_]?paper|award)(?:/|$)", text):
        return "best paper/award"
    if re.search(r"/oral(?:/|$)", text):
        return "oral"
    if re.search(r"/(?:spotlight|highlight)(?:/|$)", text):
        return "spotlight"
    if re.search(r"/poster(?:/|$)", text):
        return "poster"
    return ""


def _presentation_type_from_text(value: object) -> str:
    text = _clean_text(str(value or "")).lower()
    if not text:
        return ""
    if re.search(r"\b(best|award|outstanding|distinguished)[-\s]+paper\b", text):
        return "best paper/award"
    if re.search(r"\boral\b", text):
        return "oral"
    if re.search(r"\bspotlight\b|\bhighlight\b", text):
        return "spotlight"
    if re.search(r"\bposter\b", text):
        return "poster"
    return ""


def _presentation_display_label(venue: object, year: object, presentation_type: str) -> str:
    label = str(presentation_type or "").strip()
    if not label:
        return ""
    display = " ".join(part for part in [str(venue or "").strip(), str(year or "").strip(), label.title()] if part).strip()
    return display or label


def _set_presentation_metadata(paper: dict, presentation_type: str, *, source: str) -> None:
    label = str(presentation_type or "").strip().lower()
    if not label:
        return
    metadata = paper.setdefault("metadata", {})
    display = _presentation_display_label(paper.get("venue"), paper.get("year"), label)
    paper.setdefault("track", display)
    paper.setdefault("presentation_type", label)
    paper.setdefault("presentation_label", display)
    if isinstance(metadata, dict):
        metadata.setdefault("presentation_type", label)
        metadata.setdefault("presentation_label", display)
        metadata.setdefault("presentation_source", source)


def _title_key(value: str) -> str:
    text = html.unescape(_clean_text(value)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()

def normalize_date(value: str = "") -> str:
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day).isoformat()
    return text


def _in_date_range(value: str, start_date: str = "", end_date: str = "") -> bool:
    current = normalize_date((value or "")[:10])
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    if not current:
        return True
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True

def is_neurips_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "neurips" in text or "neural information processing systems" in text


def is_acl_family_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["acl", "emnlp", "naacl", "association for computational linguistics"])


def is_iclr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()
    return "iclr" in text or "learning representations" in text


def is_cvf_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["cvpr", "iccv", "eccv"])


def is_pmlr_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return any(key in text for key in ["icml", "aistats", "colt", "uai"])


def is_icml_venue(venue: dict) -> bool:
    text = f"{venue.get('name', '')} {venue.get('full_name', '')}".lower()
    return "icml" in text or "international conference on machine learning" in text




OPENREVIEW_VENUE_PATTERNS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("neurips", "neural information processing systems"), ("NeurIPS.cc/{year}/Conference",)),
    (("iclr", "learning representations"), ("ICLR.cc/{year}/Conference",)),
    (("icml", "international conference on machine learning"), ("ICML.cc/{year}/Conference",)),
    (("aistats", "artificial intelligence and statistics"), ("aistats.org/AISTATS/{year}/Conference",)),
    (("uai", "uncertainty in artificial intelligence"), ("auai.org/UAI/{year}/Conference",)),
    (("colt", "conference on learning theory"), ("learningtheory.org/COLT/{year}/Conference",)),
    (("corl", "conference on robot learning"), ("robot-learning.org/CoRL/{year}/Conference",)),
    (("colm", "conference on language modeling"), ("colmweb.org/COLM/{year}/Conference",)),
    (("rlc", "reinforcement learning conference"), ("rl-conference.cc/RLC/{year}/Conference",)),
    (("log", "learning on graphs"), ("logconference.io/LOG/{year}/Conference",)),
    (("midl", "medical imaging with deep learning"), ("MIDL.io/{year}/Conference",)),
    (("tmlr", "transactions on machine learning research"), ("TMLR",)),
]


def _venue_text(venue: dict) -> str:
    return f"{venue.get('name', '')} {venue.get('full_name', '')} {venue.get('address', '')}".lower()


def _matches_venue_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if " " in keyword:
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _openreview_patterns_for_venue(venue: dict) -> list[str]:
    text = _venue_text(venue)
    patterns: list[str] = []
    for keywords, venue_patterns in OPENREVIEW_VENUE_PATTERNS:
        if any(_matches_venue_keyword(text, keyword) for keyword in keywords):
            patterns.extend(venue_patterns)
    return patterns


def is_openreview_supported_venue(venue: dict) -> bool:
    return bool(_openreview_patterns_for_venue(venue))


def _openreview_venue_ids(venue: dict, year: int) -> list[str]:
    venue_ids = []
    for pattern in _openreview_patterns_for_venue(venue):
        venue_ids.append(pattern.format(year=year) if "{year}" in pattern else pattern)
    return list(dict.fromkeys(venue_ids))
