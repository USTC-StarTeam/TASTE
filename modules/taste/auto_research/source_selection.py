from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH
import json
import os


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PROJECTS_ROOT = WORKSPACE_ROOT / "projects"
PROJECT_CONFIG_ENV = "PROJECT_CONFIG"
PROJECT_ENV = "PROJECT_ID"

DEFAULT_VENUE_IDS = [
    "openreview_iclr_2026",
    "openreview_neurips",
    "dblp_icml",
    "dblp_kdd",
]


def default_source_selection() -> dict[str, Any]:
    return {
        "venue_ids": list(DEFAULT_VENUE_IDS),
        "years": [date.today().year],
        "include_arxiv": False,
        "include_biorxiv": False,
        "include_huggingface": False,
        "include_github": False,
        "include_nature": False,
        "include_science": False,
    }


def normalize_source_selection(selection: Any) -> dict[str, Any]:
    raw = selection if isinstance(selection, dict) else {}
    defaults = default_source_selection()
    venue_ids = raw.get("venue_ids") or raw.get("venues") or defaults["venue_ids"]
    if not isinstance(venue_ids, list):
        venue_ids = defaults["venue_ids"]
    years = raw.get("years") or defaults["years"]
    if not isinstance(years, list):
        years = defaults["years"]
    normalized_years: list[int] = []
    for item in years:
        try:
            normalized_years.append(int(item))
        except (TypeError, ValueError):
            continue
    if not normalized_years:
        normalized_years = defaults["years"]
    return {
        "venue_ids": [str(item) for item in venue_ids if str(item).strip()],
        "years": normalized_years,
        "include_arxiv": bool(raw.get("include_arxiv", defaults["include_arxiv"])),
        "include_biorxiv": bool(raw.get("include_biorxiv", defaults["include_biorxiv"])),
        "include_huggingface": bool(raw.get("include_huggingface", defaults["include_huggingface"])),
        "include_github": bool(raw.get("include_github", defaults["include_github"])),
        "include_nature": bool(raw.get("include_nature", defaults["include_nature"])),
        "include_science": bool(raw.get("include_science", defaults["include_science"])),
    }


def _read_json(path: Path | None, default: Any) -> Any:
    if path is None or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _configured_project_path() -> Path | None:
    explicit = os.environ.get(PROJECT_CONFIG_ENV, "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.exists() else None
    project = os.environ.get(PROJECT_ENV, "").strip()
    if project:
        candidate = PROJECTS_ROOT / project / "project.json"
        return candidate if candidate.exists() else None
    candidates = sorted(PROJECTS_ROOT.glob("*/project.json")) if PROJECTS_ROOT.exists() else []
    return candidates[0] if len(candidates) == 1 else None


def project_config_path() -> Path | None:
    return _configured_project_path()


def _project_selection(project_path: Path | None) -> dict[str, Any] | None:
    config = _read_json(project_path, {}) if project_path else {}
    if not isinstance(config, dict):
        return None
    discovery = config.get("discovery", {}) if isinstance(config.get("discovery"), dict) else {}
    raw = discovery.get("canonical_source_selection") or config.get("default_find_selection")
    return normalize_source_selection(raw) if isinstance(raw, dict) else None


def canonical_source_selection(config_path: Path = CONFIG_PATH, project_config_path: Path | None = None) -> dict[str, Any]:
    project_selection = _project_selection(project_config_path or _configured_project_path())
    if project_selection is not None:
        return project_selection
    config = _read_json(config_path, {}) if config_path.exists() else {}
    raw = config.get("default_find_selection") if isinstance(config, dict) else {}
    return normalize_source_selection(raw)


def _enabled_sources_for(selection: dict[str, Any]) -> list[str]:
    enabled_sources = ["manual", "semantic_scholar"]
    if selection.get("include_arxiv"):
        enabled_sources.append("arxiv")
    if selection.get("include_biorxiv"):
        enabled_sources.append("biorxiv")
    if selection.get("include_nature"):
        enabled_sources.append("nature")
    if selection.get("include_science"):
        enabled_sources.append("science")
    if selection.get("include_github"):
        enabled_sources.append("github")
    if selection.get("include_huggingface"):
        enabled_sources.append("huggingface")
    return enabled_sources


def save_canonical_source_selection(selection: Any, config_path: Path = CONFIG_PATH, project_config_path: Path | None = None) -> dict[str, Any]:
    normalized = normalize_source_selection(selection)
    project_path = project_config_path or _configured_project_path()
    if project_path:
        project_config = _read_json(project_path, {})
        if not isinstance(project_config, dict):
            project_config = {}
        discovery = dict(project_config.get("discovery") or {}) if isinstance(project_config.get("discovery"), dict) else {}
        discovery["canonical_source_selection"] = normalized
        discovery["enabled_sources"] = _enabled_sources_for(normalized)
        project_config["discovery"] = discovery
        project_config["default_find_selection"] = normalized
        _write_json(project_path, project_config)
    config = _read_json(config_path, {}) if config_path.exists() else {}
    if not isinstance(config, dict):
        config = {}
    config["default_find_selection"] = normalized
    _write_json(config_path, config)
    return normalized


def source_enabled(selection: Any, source: str) -> bool:
    normalized = normalize_source_selection(selection)
    key = str(source or "").strip().lower().replace("-", "_")
    if key in {"venue", "venues", "conference", "conferences"}:
        return bool(normalized.get("venue_ids"))
    if key in {"arxiv", "xiv"}:
        return bool(normalized.get("include_arxiv"))
    if key in {"biorxiv", "bio_rxiv"}:
        return bool(normalized.get("include_biorxiv"))
    if key in {"nature", "nature_portfolio"}:
        return bool(normalized.get("include_nature"))
    if key in {"science", "science_family"}:
        return bool(normalized.get("include_science"))
    if key in {"huggingface", "hf", "hugging_face"}:
        return bool(normalized.get("include_huggingface"))
    if key in {"github", "git_hub"}:
        return bool(normalized.get("include_github"))
    return True


def paper_source_allowed(item: Any, selection: Any) -> bool:
    if not isinstance(item, dict):
        return False
    source = str(item.get("source") or "").strip().lower()
    venue = str(item.get("venue") or "").strip().lower()
    url = str(item.get("url") or item.get("pdf_url") or "").strip().lower()
    if source == "arxiv" or venue == "arxiv" or "arxiv.org" in url:
        return source_enabled(selection, "arxiv")
    if source == "biorxiv" or venue == "biorxiv" or "biorxiv.org" in url:
        return source_enabled(selection, "biorxiv")
    if source == "nature" or "nature.com" in url:
        return source_enabled(selection, "nature")
    if source == "science" or "science.org" in url:
        return source_enabled(selection, "science")
    if source in {"huggingface", "hf"} or "huggingface.co" in url:
        return source_enabled(selection, "huggingface")
    if source == "github" or "github.com" in url:
        return source_enabled(selection, "github")
    return True


def filter_papers_by_source_selection(items: Any, selection: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [row for row in items if paper_source_allowed(row, selection)]


def filter_source_status_by_selection(items: Any, selection: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or row.get("venue") or "")
        if source_enabled(selection, source):
            rows.append(row)
    return rows
