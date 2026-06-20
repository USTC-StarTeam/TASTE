from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from artifact_io.workspace import compact_text


TITLE_KEYS = ("title", "paper_title", "name", "display_name")
URL_KEYS = ("url", "pdf_url", "source_url", "link", "paper_url")
ID_KEYS = ("paper_id", "id", "arxiv_id", "doi")
SUMMARY_KEYS = (
    "summary",
    "abstract",
    "abstract_zh",
    "tl_dr",
    "tldr",
    "one_sentence_summary",
    "main_contribution",
    "contribution",
)
DETAIL_KEYS = (
    "motivation",
    "motivation_zh",
    "method",
    "method_details",
    "method_details_zh",
    "experiments",
    "experiments_zh",
    "results",
    "limitations",
    "limitations_zh",
    "novelty",
    "key_findings",
    "critique",
    "implementation_notes",
)
KNOWN_FILE_NAMES = (
    "read_results.json",
    "read.md",
    "find_results.json",
    "paper_quality.json",
    "literature_tool_packet.json",
    "literature_tool_packet.md",
)


@dataclass(slots=True)
class ReadingEvidence:
    title: str
    source_path: str
    source_type: str = "reading"
    paper_id: str = ""
    url: str = ""
    summary: str = ""
    details: dict[str, str] = field(default_factory=dict)
    raw_keys: list[str] = field(default_factory=list)

    def to_prompt_dict(self, detail_limit: int = 2200) -> dict[str, Any]:
        details = {key: compact_text(value, detail_limit) for key, value in self.details.items() if compact_text(value, 80)}
        return {
            "title": self.title,
            "paper_id": self.paper_id,
            "url": self.url,
            "source_type": self.source_type,
            "source_path": self.source_path,
            "summary": compact_text(self.summary, 1800),
            "details": details,
        }


def _first_text(row: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = row.get(key)
        text = compact_text(value, 5000)
        if text:
            return text
    return ""


def _as_detail_mapping(row: dict[str, Any]) -> dict[str, str]:
    details: dict[str, str] = {}
    for key in DETAIL_KEYS:
        text = compact_text(row.get(key), 5000)
        if text:
            details[key] = text
    for key in ("method_advantages_zh", "method_disadvantages_zh", "failure_modes", "bad_cases", "open_questions"):
        text = compact_text(row.get(key), 3000)
        if text:
            details[key] = text
    return details


def _row_has_signal(row: dict[str, Any]) -> bool:
    if _first_text(row, TITLE_KEYS):
        return True
    return any(compact_text(row.get(key), 120) for key in (*SUMMARY_KEYS, *DETAIL_KEYS))


def _normalize_row(row: dict[str, Any], source_path: Path, source_type: str) -> ReadingEvidence | None:
    if not isinstance(row, dict) or not _row_has_signal(row):
        return None
    title = _first_text(row, TITLE_KEYS) or Path(source_path).stem
    summary = _first_text(row, SUMMARY_KEYS)
    if not summary:
        summary = compact_text({key: row.get(key) for key in DETAIL_KEYS if key in row}, 1800)
    return ReadingEvidence(
        title=title[:300],
        source_path=str(source_path),
        source_type=source_type,
        paper_id=_first_text(row, ID_KEYS)[:160],
        url=_first_text(row, URL_KEYS)[:600],
        summary=summary,
        details=_as_detail_mapping(row),
        raw_keys=sorted(str(key) for key in row.keys())[:80],
    )


def _flatten_json_rows(payload: Any) -> list[tuple[dict[str, Any], str]]:
    rows: list[tuple[dict[str, Any], str]] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                rows.append((item, "json_list"))
        return rows
    if not isinstance(payload, dict):
        return rows
    direct_keys = {"title", "paper_title", "summary", "abstract", "method", "experiments"}
    if direct_keys.intersection(payload.keys()):
        rows.append((payload, "json_object"))
    for key in (
        "readings",
        "papers",
        "articles",
        "selected_papers",
        "recommended_papers",
        "deep_readings",
        "fragments",
        "items",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    rows.append((item, key))
    for key in ("find_results", "read_results", "literature", "paper_quality"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            rows.extend(_flatten_json_rows(value))
    return rows


def _markdown_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:300] or fallback
    return fallback


def _load_json_file(path: Path) -> list[ReadingEvidence]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[ReadingEvidence] = []
    for row, source_type in _flatten_json_rows(payload):
        evidence = _normalize_row(row, path, source_type)
        if evidence:
            rows.append(evidence)
    return rows


def _load_markdown_file(path: Path) -> list[ReadingEvidence]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    title = _markdown_title(text, path.stem)
    sections = _markdown_sections(text)
    summary = sections.get("摘要") or sections.get("Summary") or sections.get("概述") or compact_text(text, 1800)
    details = {key: value for key, value in sections.items() if key != title}
    if not details:
        details = {"full_text_excerpt": compact_text(text, 6000)}
    return [ReadingEvidence(title=title, source_path=str(path), source_type="markdown", summary=summary, details=details)]


def _markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = "正文"
    for line in text.splitlines():
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if match:
            current = match.group(2).strip()[:120]
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {key: compact_text("\n".join(value), 6000) for key, value in sections.items() if compact_text(value, 80)}


def _input_files_from_dir(path: Path) -> list[Path]:
    files: list[Path] = []
    for name in KNOWN_FILE_NAMES:
        candidate = path / name
        if candidate.exists() and candidate.is_file():
            files.append(candidate)
    fragment_dirs = [
        path / "current_find_deep_read_fragments",
        path / "planning" / "finding" / "current_find_deep_read_fragments",
    ]
    for fragment_dir in fragment_dirs:
        if fragment_dir.is_dir():
            files.extend(sorted(fragment_dir.glob("*.json"))[:80])
    if not files:
        files.extend(sorted(item for item in path.glob("*.json") if item.is_file())[:80])
        files.extend(sorted(item for item in path.glob("*.md") if item.is_file())[:20])
    return files


def discover_input_files(paths: Sequence[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"输入路径不存在：{path}")
        if path.is_dir():
            files.extend(_input_files_from_dir(path))
        elif path.is_file():
            files.append(path)
    seen: set[str] = set()
    out: list[Path] = []
    for item in files:
        key = str(item.resolve())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def load_reading_evidence(paths: Sequence[str | Path], max_items: int = 24) -> list[ReadingEvidence]:
    files = discover_input_files(paths)
    evidence: list[ReadingEvidence] = []
    errors: list[str] = []
    for path in files:
        try:
            if path.suffix.lower() == ".json":
                evidence.extend(_load_json_file(path))
            elif path.suffix.lower() in {".md", ".markdown", ".txt"}:
                evidence.extend(_load_markdown_file(path))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    deduped = _dedupe_evidence(evidence)
    if not deduped:
        detail = "；".join(errors[:5])
        raise ValueError(f"没有从输入中解析出可用论文精读证据。{detail}")
    return deduped[:max_items]


def _dedupe_evidence(rows: Sequence[ReadingEvidence]) -> list[ReadingEvidence]:
    out: list[ReadingEvidence] = []
    seen: set[str] = set()
    for row in rows:
        key = "|".join(part for part in (row.title.lower().strip(), row.url.lower().strip(), row.paper_id.lower().strip()) if part)
        if key and key not in seen:
            seen.add(key)
            out.append(row)
    return out
