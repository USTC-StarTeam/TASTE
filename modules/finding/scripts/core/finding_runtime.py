from __future__ import annotations


# ---- runtime paths ----

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = PACKAGE_DIR.parent
MODULE_ROOT = SCRIPTS_ROOT.parent
DATA_DIR = MODULE_ROOT / "data"
RUNTIME_DIR = Path(os.environ.get("FINDING_RUNTIME_DIR") or MODULE_ROOT / ".runtime").expanduser()
RUNS_DIR = RUNTIME_DIR / "runs"
CACHE_DIR = RUNTIME_DIR / "cache"
STATE_DIR = CACHE_DIR / "state"
FINDING_CACHE_DIR = CACHE_DIR / "finding_cache"
LOCAL_DATABASE_DIR = CACHE_DIR / "local_database"
LATEST_RUN_DIR = RUNTIME_DIR / "latest_run"
CONFIG_PATH = RUNTIME_DIR / ".config.json"


def display_path(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve().relative_to(MODULE_ROOT).as_posix()
    except (OSError, ValueError):
        try:
            return candidate.relative_to(MODULE_ROOT).as_posix()
        except ValueError:
            return candidate.as_posix()


def ensure_directories() -> None:
    for path in (
        RUNS_DIR,
        CACHE_DIR,
        STATE_DIR,
        FINDING_CACHE_DIR,
        LOCAL_DATABASE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


# ---- source selection ----

from datetime import date
from typing import Any


DEFAULT_VENUE_IDS: list[str] = []


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _normalize_year_values(value: Any, default_years: list[int] | None = None) -> list[int]:
    raw_values = value if isinstance(value, list) else ([] if value is None else [value])
    years: list[int] = []
    seen: set[int] = set()
    for item in raw_values:
        try:
            year = int(item)
        except (TypeError, ValueError):
            continue
        if year < 2000 or year > 2100 or year in seen:
            continue
        seen.add(year)
        years.append(year)
    return years or list(default_years or [])


def _normalize_venue_year_pairs(value: Any) -> list[dict[str, int | str]]:
    if not isinstance(value, list):
        return []
    pairs: list[dict[str, int | str]] = []
    seen: set[tuple[str, int]] = set()
    for item in value:
        venue_id = ""
        raw_years: Any = None
        if isinstance(item, dict):
            venue_id = str(item.get("venue_id") or item.get("venue") or item.get("id") or "").strip()
            raw_years = item.get("years") if isinstance(item.get("years"), list) else item.get("year")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            venue_id = str(item[0] or "").strip()
            raw_years = item[1]
        if not venue_id:
            continue
        for year in _normalize_year_values(raw_years, []):
            key = (venue_id, year)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"venue_id": venue_id, "year": year})
    return pairs


def _pairs_from_venues_and_years(venue_ids: list[str], years: list[int]) -> list[dict[str, int | str]]:
    pairs: list[dict[str, int | str]] = []
    seen: set[tuple[str, int]] = set()
    for venue_id in venue_ids:
        for year in years:
            key = (venue_id, int(year))
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"venue_id": venue_id, "year": int(year)})
    return pairs


def default_source_selection() -> dict[str, Any]:
    default_year = date.today().year
    venue_ids = list(DEFAULT_VENUE_IDS)
    years = [default_year]
    return {
        "venue_ids": venue_ids,
        "years": years,
        "venue_years": _pairs_from_venues_and_years(venue_ids, years),
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
    raw_venue_ids = raw.get("venue_ids", raw.get("venues", defaults["venue_ids"]))
    venue_ids = _unique_strings(raw_venue_ids if isinstance(raw_venue_ids, list) else defaults["venue_ids"])
    years = _normalize_year_values(raw.get("years", defaults["years"]), defaults["years"])
    venue_years = _normalize_venue_year_pairs(raw.get("venue_years"))
    if not venue_years:
        venue_years = _pairs_from_venues_and_years(venue_ids, years)
    if venue_years:
        venue_ids = _unique_strings([pair["venue_id"] for pair in venue_years])
        years = _normalize_year_values([pair["year"] for pair in venue_years], years)
    return {
        "venue_ids": venue_ids,
        "years": years,
        "venue_years": venue_years,
        "include_arxiv": bool(raw.get("include_arxiv", defaults["include_arxiv"])),
        "include_biorxiv": bool(raw.get("include_biorxiv", defaults["include_biorxiv"])),
        "include_huggingface": bool(raw.get("include_huggingface", defaults["include_huggingface"])),
        "include_github": bool(raw.get("include_github", defaults["include_github"])),
        "include_nature": bool(raw.get("include_nature", defaults["include_nature"])),
        "include_science": bool(raw.get("include_science", defaults["include_science"])),
    }


# ---- models ----

import os
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field



ClassificationSource = Literal["official", "llm_inferred", "fallback"]
LLMRole = Literal["find", "read", "idea_generator", "idea_judge", "plan_generator", "plan_evaluator"]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


class LLMRoleConfig(BaseModel):
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


class EmailConfig(BaseModel):
    smtp_server: str = ""
    smtp_port: int = 465
    sender: str = ""
    receivers: list[str] = Field(default_factory=list)
    smtp_password: str = ""
    manual_enabled: bool = True
    auto_send_enabled: bool = False
    auto_send_stages: list[str] = Field(default_factory=lambda: ["find"])


class AppConfig(BaseModel):
    research_topic: str = ""
    research_interest: str = ""
    researcher_profile: str = ""
    provider: str = Field(default_factory=lambda: os.environ.get("LLM_PROVIDER") or "openai")
    base_url: str = Field(default_factory=lambda: os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1")
    api_key: str = Field(default_factory=lambda: os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY") or "")
    model: str = Field(default_factory=lambda: os.environ.get("LLM_MODEL") or "gpt-4o-mini")
    temperature: float = Field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.4))
    llm_roles: dict[str, LLMRoleConfig] = Field(default_factory=dict)
    llm_concurrency: int = 10
    idea_parallel_workers: int = 2
    nonvenue_fetch_limit: int = 5000
    max_recommended_papers: int = 20
    max_ideas: int = 6
    venue_title_scan_limit: int = 0
    venue_title_scan_fraction: float = 1.0
    title_abstract_scoring_limit: int = 1000
    full_venue_corpus_audit: bool = True
    title_filter_timeout_sec: int = 120
    abstract_scoring_max_workers: int = 10
    abstract_scoring_batch_size: int = 10
    abstract_scoring_timeout_sec: int = 180
    arxiv_max_queries: int = 3
    arxiv_timeout_sec: int = 15
    arxiv_categories: list[str] = Field(default_factory=list)
    arxiv_queries: list[str] = Field(default_factory=list)
    arxiv_start_date: str = ""
    arxiv_end_date: str = ""
    arxiv_llm_candidate_limit: int = 0
    arxiv_llm_candidates_per_category: int = 0
    biorxiv_categories: list[str] = Field(default_factory=list)
    biorxiv_start_date: str = ""
    biorxiv_end_date: str = ""
    biorxiv_llm_candidate_limit: int = 0
    biorxiv_llm_candidates_per_category: int = 0
    nature_journals: list[str] = Field(default_factory=lambda: ["nature", "natmachintell", "natcomputsci", "nmeth", "ncomms"])
    nature_article_types: list[str] = Field(default_factory=lambda: ["article"])
    nature_start_date: str = ""
    nature_end_date: str = ""
    nature_candidate_limit: int = 200
    science_journals: list[str] = Field(default_factory=lambda: ["science", "sciadv"])
    science_article_types: list[str] = Field(default_factory=lambda: ["Research Article"])
    science_start_date: str = ""
    science_end_date: str = ""
    science_candidate_limit: int = 200
    github_languages: list[str] = Field(default_factory=lambda: ["all"])
    github_since: Literal["daily", "weekly", "monthly"] = "daily"
    hf_include_papers: bool = True
    hf_include_models: bool = True
    runtime_tuning: dict[str, Any] = Field(default_factory=dict)
    default_find_selection: dict[str, Any] = Field(default_factory=dict)
    email: EmailConfig = Field(default_factory=EmailConfig)


_RUNTIME_TUNING_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_RUNTIME_TUNING_ALLOWED_PREFIXES = (
    "ABSTRACT_",
    "ARXIV_",
    "BIORXIV_",
    "CONFERENCE_",
    "DEFAULT_VENUE_",
    "DETAIL_",
    "DISABLE_",
    "FINAL_",
    "FIND_",
    "FORCE_",
    "FULL_",
    "ICML_",
    "LARGE_",
    "LARGE_VENUE_",
    "LLM_REQUEST_",
    "LLM_TITLE_",
    "LOCAL_TITLE_",
    "MIN_",
    "NATURE_",
    "NEURIPS_",
    "NONVENUE_",
    "OMITTED_",
    "OPENALEX_",
    "OPENREVIEW_",
    "PRESENTATION_",
    "QUALITY_",
    "SCIENCE_",
    "SCREENING_",
    "SINGLE_",
    "SOURCE_",
    "STRONG_RECOMMENDATION_",
    "TITLE_",
    "TRANSLATE_",
    "TRIAGE_",
    "USE_",
    "VENUE_",
)


def runtime_tuning_env(config: AppConfig | dict[str, Any]) -> dict[str, str]:
    tuning = config.get("runtime_tuning", {}) if isinstance(config, dict) else getattr(config, "runtime_tuning", {})
    if not isinstance(tuning, dict):
        return {}
    result: dict[str, str] = {}
    for raw_key, value in tuning.items():
        env_name = str(raw_key or "").strip().replace("-", "_").upper()
        if not env_name or value in (None, ""):
            continue
        if any(marker in env_name for marker in _RUNTIME_TUNING_SECRET_MARKERS):
            continue
        if env_name in {"OPENAI_API_KEY", "LLM_API_KEY", "ANTHROPIC_API_KEY"}:
            continue
        if not any(env_name.startswith(prefix) for prefix in _RUNTIME_TUNING_ALLOWED_PREFIXES):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        result[env_name] = "1" if isinstance(value, bool) and value else "0" if isinstance(value, bool) else str(value)
    return result


def apply_runtime_tuning_env(config: AppConfig | dict[str, Any], env: dict[str, str] | None = None) -> dict[str, str]:
    target = env if env is not None else os.environ
    applied = runtime_tuning_env(config)
    for key, value in applied.items():
        target[key] = value
    return applied


class VenueSelection(BaseModel):
    venue_ids: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=lambda: [date.today().year])
    venue_years: list[dict[str, Any]] = Field(default_factory=list)
    include_arxiv: bool = False
    include_biorxiv: bool = False
    include_huggingface: bool = False
    include_github: bool = False
    include_nature: bool = False
    include_science: bool = False


class FindRequest(BaseModel):
    config: AppConfig | None = None
    selection: VenueSelection = Field(default_factory=lambda: VenueSelection(**default_source_selection()))
    force_new_find: bool = False
    restart_full_cycle: bool = False
    human_approved_new_find: bool = False
    approval_reason: str = ""


# ---- jobs ----

class JobCancelled(Exception):
    """Raised by the Finding pipeline when a soft cancellation request is observed."""


# ---- storage ----

import json
import fcntl
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any



def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def create_run_dir(prefix: str = "run") -> tuple[str, Path]:
    ensure_directories()
    run_id = f"{prefix}_{utc_run_id()}"
    path = RUNS_DIR / run_id
    path.mkdir(parents=True, exist_ok=False)
    return run_id, path


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def publish_latest_run_for_review(run_path: str | Path) -> Path:
    source = Path(run_path).expanduser()
    if not source.is_dir():
        raise FileNotFoundError(f"Run directory not found: {source}")
    try:
        source.resolve().relative_to(RUNS_DIR.resolve())
    except ValueError as exc:
        raise ValueError(f"latest_run can only mirror a directory under {display_path(RUNS_DIR)}") from exc
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = RUNTIME_DIR / ".latest_run.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        temp_dir = RUNTIME_DIR / f".latest_run_{source.name}_{utc_run_id()}.tmp"
        _remove_path(temp_dir)
        try:
            shutil.copytree(source, temp_dir)
            _remove_path(LATEST_RUN_DIR)
            temp_dir.rename(LATEST_RUN_DIR)
            return LATEST_RUN_DIR
        finally:
            _remove_path(temp_dir)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def run_dir(run_id: str) -> Path:
    path = RUNS_DIR / run_id
    if path.exists():
        return path
    raise FileNotFoundError(f"Run not found: {run_id}")


def write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            tmp_name = handle.name
            handle.write(payload)
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_safely(path: Path, default: Any = None) -> Any:
    try:
        return read_json(path, default)
    except (OSError, json.JSONDecodeError):
        return default


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@contextmanager
def json_file_lock(path: Path, *, timeout_sec: float = 60.0, stale_after_sec: float = 600.0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + max(1.0, float(timeout_sec or 60.0))
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} created_at={datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"))
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > max(30.0, float(stale_after_sec or 600.0)):
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for JSON cache lock: {display_path(lock_path)}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_json_dicts(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_json_dicts(current, value)
        else:
            merged[key] = value
    return merged


def write_json_cache(path: Path, data: Any, *, merge_existing: bool = False) -> None:
    with json_file_lock(path):
        payload = data
        if merge_existing and isinstance(data, dict):
            existing = read_json_safely(path, {})
            if isinstance(existing, dict):
                payload = _merge_json_dicts(existing, data)
        write_json(path, payload)


def redacted_config(data: dict[str, Any]) -> dict[str, Any]:
    secret_keys = {"api_key", "smtp_password", "password", "sender_password"}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("********" if key in secret_keys and item else redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(dict(data))


def update_manifest(path: Path, stage: str) -> None:
    manifest_path = path / "manifest.json"
    with json_file_lock(manifest_path):
        manifest = read_json_safely(manifest_path, {})
        if not isinstance(manifest, dict):
            manifest = {}
        manifest.setdefault("created_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        manifest.setdefault("layout_schema", "finding_run_layout_v2")
        manifest.setdefault("layout", {
            "root": "Direct public/compatibility files: find.md, find_results.json, find_progress.json, source_status.md, manifest.json.",
            "inputs": "Redacted config and explicit selection snapshots.",
            "final": "Structured final machine outputs and candidate Markdown packets other than root find.md.",
            "intermediate": "Source raw pools, prefiltered pools, and stage outputs used to build final results.",
            "reports": "Source health, category/title filter reports, and source-specific review packets.",
            "logs": "Live progress and mutable run status snapshots.",
        })
        stages = manifest.setdefault("stages", [])
        if isinstance(stages, list) and stage not in stages:
            stages.append(stage)
        write_json(manifest_path, manifest)


# ---- markdown ----

import re
from typing import Iterable


_PLACEHOLDER_ABSTRACT_MARKERS = (
    "当前候选缺少真实摘要",
    "当前索引元数据缺少真实摘要",
    "lacks a real abstract",
    "No abstract available in metadata",
    "Abstract not available in the indexed venue metadata",
)

_ABSTRACT_UI_CONTROL_RE = re.compile(
    r"(?:\s*(?:show\s+(?:more|less)|read\s+(?:more|less)|显示更多|显示较少|展开|收起)\s*[。.]?\s*)+$",
    re.IGNORECASE,
)


def _strip_abstract_ui_controls(value: object) -> str:
    return _ABSTRACT_UI_CONTROL_RE.sub("", " ".join(str(value or "").split())).strip()


def table(headers: list[str], rows: Iterable[list[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe = [str(value).replace("\n", " ").replace("|", "\\|") for value in row]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines)


def _normalize_public_latex_markup(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"\\url\{(https?://[^{}\s]+)\}", lambda match: f"[{match.group(1)}]({match.group(1)})", text)
    text = re.sub(r"\\href\{(https?://[^{}\s]+)\}\{([^{}]+)\}", lambda match: f"[{match.group(2)}]({match.group(1)})", text)
    text = re.sub(r"\\([A-Za-z][A-Za-z0-9]*)\{\}", lambda match: match.group(1), text)
    return text


def _paper_title_text(value: object) -> str:
    text = _normalize_public_latex_markup(value).strip() or "Untitled"
    text = re.sub(r"\$([^$\n]{1,80})\$", lambda match: match.group(1).strip(), text)
    return " ".join(text.split())


def _paper_text(paper: dict, keys: list[str]) -> str:
    for key in keys:
        value = _normalize_public_latex_markup(paper.get(key)).strip()
        if not value:
            continue
        if any(marker.lower() in value.lower() for marker in _PLACEHOLDER_ABSTRACT_MARKERS):
            continue
        return _strip_abstract_ui_controls(value) if key in {"abstract_zh", "summary_zh", "tldr_zh", "abstract_en", "abstract", "summary", "tldr"} else value
    return ""


def _metadata_dict(paper: dict) -> dict:
    return paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}


def _clean_link_url(value: object) -> str:
    text = str(value or "").strip().rstrip(".,);]")
    return text if text.startswith(("http://", "https://")) else ""


def _paper_doi(paper: dict) -> str:
    metadata = _metadata_dict(paper)
    doi = str(paper.get("doi") or metadata.get("doi") or "").strip()
    if doi:
        return doi
    for key in ["url", "pdf_url", "doi_url", "publisher_url", "acm_abs_url"]:
        value = str(paper.get(key) or metadata.get(key) or "")
        match = re.search(r'10\.\d{4,9}/[^\s"<>]+', value)
        if match:
            return match.group(0).rstrip(".,);]")
    return ""


def _append_link(parts: list[str], seen: set[str], label: str, url: str) -> None:
    clean = _clean_link_url(url)
    if clean and clean not in seen:
        seen.add(clean)
        parts.append(f"[{label}]({clean})")


def _paper_link_line(paper: dict) -> str:
    metadata = _metadata_dict(paper)
    parts: list[str] = []
    seen: set[str] = set()
    doi = _paper_doi(paper)
    doi_url = f"https://doi.org/{doi}" if doi else ""
    main_url = _clean_link_url(paper.get("url") or metadata.get("url") or metadata.get("publisher_url") or doi_url)
    _append_link(parts, seen, "论文页", main_url)
    _append_link(parts, seen, "PDF", paper.get("pdf_url") or metadata.get("acm_pdf_url") or metadata.get("acm_epdf_url") or "")
    _append_link(parts, seen, "DOI", doi_url)
    return f"- **链接**: {' / '.join(parts)}" if parts else ""


def _paper_score_line(paper: dict) -> str:
    for key in ("recommendation_score", "llm_fit_score", "fit_score", "score"):
        value = paper.get(key)
        if value in (None, ""):
            continue
        try:
            return f"- **推荐分数**: {float(value):.2f} / 10"
        except (TypeError, ValueError):
            continue
    return ""


def _paper_list_text(value: object) -> str:
    if isinstance(value, list):
        return "，".join(str(item) for item in value if str(item).strip())
    return str(value or "").strip()


_PRESENTATION_DISPLAY = {
    "best paper/award": "Best Paper/Award",
    "oral": "Oral",
    "spotlight": "Spotlight",
    "highlight": "Highlight",
    "poster": "Poster",
}


def _paper_presentation_kind(value: object) -> str:
    text = " ".join(str(value or "").split()).lower()
    if not text:
        return ""
    if re.search(r"\b(best|award|outstanding|distinguished)[-\s]+paper\b", text):
        return "best paper/award"
    if re.search(r"\boral\b", text):
        return "oral"
    if re.search(r"\bspotlight\b", text):
        return "spotlight"
    if re.search(r"\bhighlight\b", text):
        return "highlight"
    if re.search(r"\bposter\b", text):
        return "poster"
    return ""


def _paper_presentation_text(paper: dict) -> str:
    metadata = _metadata_dict(paper)
    label = str(paper.get("presentation_label") or metadata.get("presentation_label") or "").strip()
    if label:
        return label
    for value in [
        paper.get("presentation_type"),
        metadata.get("presentation_type"),
        paper.get("presentation"),
        metadata.get("presentation"),
        paper.get("track"),
        metadata.get("track"),
    ]:
        kind = _paper_presentation_kind(value)
        if not kind:
            continue
        venue_year = " ".join(str(paper.get(key) or "").strip() for key in ("venue", "year")).strip()
        display = _PRESENTATION_DISPLAY.get(kind, kind.title())
        return " ".join(part for part in [venue_year, display] if part).strip()
    labels = paper.get("presentation_labels")
    if isinstance(labels, list):
        for value in labels:
            kind = _paper_presentation_kind(value)
            if kind:
                venue_year = " ".join(str(paper.get(key) or "").strip() for key in ("venue", "year")).strip()
                display = _PRESENTATION_DISPLAY.get(kind, kind.title())
                return " ".join(part for part in [venue_year, display] if part).strip()
    return ""


def _paper_presentation_suffix(paper: dict, venue_year: str) -> str:
    text = _paper_presentation_text(paper)
    if not text:
        return ""
    if venue_year and text.lower().startswith(venue_year.lower()):
        suffix = text[len(venue_year):].strip()
        suffix = re.sub(r"^[-–—:/\s]+", "", suffix).strip()
        if suffix:
            return suffix
    return text


def _paper_brief_metadata_lines(paper: dict) -> list[str]:
    lines: list[str] = []
    venue_year = " ".join(str(paper.get(key) or "").strip() for key in ("venue", "year")).strip()
    source = str(paper.get("source") or "").strip()
    category = str(paper.get("category") or "").strip()
    presentation = _paper_presentation_suffix(paper, venue_year)
    venue_year_line = venue_year
    if venue_year_line and presentation:
        venue_year_line = f"{venue_year_line} / {presentation}"
    hit_text = _paper_list_text(paper.get("hit_directions_zh") or paper.get("hit_directions"))
    for line in [
        f"- **会议/年份**: {venue_year_line}" if venue_year_line else "",
        f"- **来源**: {source}" if source else "",
        f"- **会议展示类型**: {presentation}" if presentation and not venue_year_line else "",
        _paper_score_line(paper),
        f"- **方法/主题类别**: {category}" if category else "",
        f"- **命中方向**: {hit_text}" if hit_text else "",
        _paper_link_line(paper),
    ]:
        if line:
            lines.append(line)
    return lines


_TITLE_ZH = {
    "Recommended Articles": "推荐文章",
    "bioRxiv Articles": "bioRxiv 文章",
    "Nature Portfolio Articles": "Nature Portfolio 文章",
    "Science Family Articles": "Science Family 文章",
    "HuggingFace Papers and Models": "HuggingFace 论文和模型",
    "GitHub Trending Repositories": "GitHub 趋势仓库",
}


def _markdown_title(title: str) -> str:
    return _TITLE_ZH.get(str(title or ""), str(title or "推荐文章"))


def paper_markdown(papers: list[dict], title: str = "Recommended Articles") -> str:
    lines = [f"# {_markdown_title(title)}", "", f"- **条目数**: {len(papers)}", ""]
    if not papers:
        lines.append("未选择条目。")
        return "\n".join(lines) + "\n"
    for index, paper in enumerate(papers, 1):
        abstract = _paper_text(paper, ["abstract_zh", "summary_zh", "tldr_zh", "abstract_en", "abstract", "summary", "tldr"])
        reason = _paper_text(paper, ["reason_zh", "recommendation_reason_zh", "reason", "recommendation_reason", "fit_explanation_zh", "fit_explanation", "match_explanation"])
        lines.extend([
            f"## {index}. {_paper_title_text(paper.get('title'))}",
            "",
            *_paper_brief_metadata_lines(paper),
            "",
            "### 摘要",
            "",
            abstract or "当前条目缺少可展示的真实摘要；需要通过详情抓取、URL/PDF 精读或摘要翻译修复后再作为推荐证据。",
            "",
            "### 推荐理由",
            "",
            reason or "推荐理由缺失；需要重新执行标题+摘要评分或理由补全。",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


# ---- llm ----

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any



def _strip_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _json_span(text: str) -> str:
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [x for x in [start_obj, start_arr] if x >= 0]
    if not starts:
        raise ValueError("No JSON object or array found")
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    if end < start:
        raise ValueError("JSON closing bracket not found")
    return text[start : end + 1]


def _escape_invalid_json_backslashes(text: str) -> str:
    return re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', text or "")


def _repair_json_text(text: str) -> str:
    repaired = text.strip().replace("\ufeff", "")
    repaired = _escape_invalid_json_backslashes(repaired)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*\n\s*{", "},{", repaired)
    repaired = re.sub(r"([\"}\]0-9])\s*\n\s*(\"[A-Za-z_][^\"\\]*(?:\\.[^\"\\]*)*\"\s*:)", r"\1,\n\2", repaired)
    repaired = re.sub(r"([\"}\]0-9])\s+(\"[A-Za-z_][^\"\\]*(?:\\.[^\"\\]*)*\"\s*:)", r"\1, \2", repaired)
    return repaired


def _loads_json_lenient(text: str) -> Any:
    decoder = json.JSONDecoder()
    for candidate in (text, _repair_json_text(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                value, _end = decoder.raw_decode(candidate.strip())
                return value
            except json.JSONDecodeError:
                continue
    return json.loads(_repair_json_text(text))


def extract_partial_json_array(raw: str) -> list[Any]:
    text = (raw or "").strip()
    start = text.find("[")
    if start < 0:
        return []
    items: list[Any] = []
    depth = 0
    obj_start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                obj_start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and obj_start >= 0:
                snippet = text[obj_start:index + 1]
                try:
                    items.append(json.loads(snippet))
                except Exception:
                    pass
                obj_start = -1
    return items


def _extract_named_array(raw: str, key: str) -> list[Any]:
    text = _strip_json_fences(raw)
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not match:
        return []
    start = match.end() - 1
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                snippet = text[start : index + 1]
                try:
                    value = _loads_json_lenient(snippet)
                    return value if isinstance(value, list) else []
                except Exception:
                    return extract_partial_json_array(snippet)
    return extract_partial_json_array(text[start:])


def _recover_expected_json(raw: str) -> Any:
    for key in ("evaluations", "scored", "selected", "translations", "ideas", "plans", "readings"):
        rows = _extract_named_array(raw, key)
        if rows:
            return {key: rows}
    rows = extract_partial_json_array(raw)
    if rows:
        return rows
    raise ValueError("No recoverable JSON found")


def extract_json(raw: str) -> Any:
    text = _strip_json_fences(raw)
    try:
        span = _json_span(text)
    except Exception:
        return _recover_expected_json(text)
    try:
        return _loads_json_lenient(span)
    except Exception:
        return _recover_expected_json(span)


def _prompt_with_json_response_hint(prompt: str) -> str:
    text = str(prompt or "")
    if "json" in text.lower():
        return text
    suffix = "Return valid JSON."
    return f"{text.rstrip()}\n\n{suffix}" if text.strip() else suffix


def _chat_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/responses"):
        return url[: -len("/responses")] + "/chat/completions"
    return url + "/chat/completions"


def _responses_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/responses"):
        return url
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/responses"
    return url + "/responses"


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                for key in ["text", "content", "output_text"]:
                    inner = part.get(key)
                    if isinstance(inner, str) and inner.strip():
                        chunks.append(inner)
                        break
        return "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if isinstance(value, dict):
        for key in ["text", "content", "output_text"]:
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _chat_response_debug(raw: Any) -> str:
    if not isinstance(raw, dict):
        return type(raw).__name__
    parts: list[str] = ["top_keys=" + ",".join(sorted(str(key) for key in raw.keys())[:12])]
    choices = raw.get("choices", []) or []
    if choices and isinstance(choices[0], dict):
        choice0 = choices[0]
        message = choice0.get("message", {}) if isinstance(choice0.get("message"), dict) else {}
        parts.append("finish_reason=" + str(choice0.get("finish_reason") or ""))
        parts.append("choice_keys=" + ",".join(sorted(str(key) for key in choice0.keys())[:12]))
        parts.append("message_keys=" + ",".join(sorted(str(key) for key in message.keys())[:12]))
    output = raw.get("output", []) or []
    if output and isinstance(output[0], dict):
        parts.append("output_keys=" + ",".join(sorted(str(key) for key in output[0].keys())[:12]))
    return "; ".join(part for part in parts if part)


def _extract_chat_text(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for choice in raw.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        for key in ["content", "text"]:
            value = _content_to_text(message.get(key, choice.get(key, "")))
            if value:
                return value
        value = _content_to_text(message.get("reasoning_content", choice.get("reasoning_content", "")))
        if value and ("{" in value or "[" in value):
            return value
    for item in raw.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            value = _content_to_text(content)
            if value:
                return value
        value = _content_to_text(item.get("content") or item.get("text"))
        if value:
            return value
    for key in ["output_text", "content", "text"]:
        value = _content_to_text(raw.get(key))
        if value:
            return value
    return ""


class LLMClient:
    def __init__(self, config: AppConfig, role: LLMRole | str | None = None):
        self.config = config
        self.role = role or "global"
        self.provider = config.provider
        self.base_url = config.base_url
        self.api_key = config.api_key
        self.model = config.model
        self.temperature = config.temperature
        if role:
            override = config.llm_roles.get(str(role))
            if override:
                self.provider = override.provider or self.provider
                self.base_url = override.base_url or self.base_url
                self.api_key = override.api_key or self.api_key
                self.model = override.model or self.model
                self.temperature = config.temperature if override.temperature is None else override.temperature
        self.api_mode = os.environ.get("LLM_API_MODE", "chat_completions")
        self.timeout_sec = int(os.environ.get("LLM_TIMEOUT_SEC", "120"))
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "2000"))
        self.retries = max(1, int(os.environ.get("LLM_RETRIES", "3")))
        self.enabled = bool(self.api_key and self.model and self.provider.lower() != "mock")

    def summary(self) -> dict:
        return {
            "role": self.role,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "enabled": self.enabled,
            "api_mode": self.api_mode or "chat_completions",
        }

    def chat(self, prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> str:
        if not self.enabled:
            raise RuntimeError("LLM is not configured")
        api_mode = str(self.api_mode or "chat_completions").strip().lower()
        use_responses = api_mode in {"responses", "response", "openai_responses"}
        response_format = os.environ.get("LLM_RESPONSE_FORMAT", "json_object").strip().lower()
        provider_text = str(self.provider or "").lower()
        is_deepseek = "deepseek" in provider_text or "api.deepseek.com" in str(self.base_url or "").lower() or "deepseek" in str(self.model or "").lower()
        reasoning_effort = os.environ.get("LLM_REASONING_EFFORT", "").strip().lower()
        disable_thinking = os.environ.get("LLM_DISABLE_THINKING", "0").lower() in {"1", "true", "yes", "on"}
        retry_empty_json = os.environ.get("LLM_RETRY_EMPTY_JSON_WITHOUT_RESPONSE_FORMAT", "1").lower() in {"1", "true", "yes", "on"}
        retry_unsupported_optional = os.environ.get("LLM_RETRY_UNSUPPORTED_OPTIONAL_PARAMS", "1").lower() in {"1", "true", "yes", "on"}
        retry_statuses = {408, 409, 429, 500, 502, 503, 504}
        # A non-positive explicit value asks the provider to use its native output limit.
        omit_max_tokens = max_tokens is not None and int(max_tokens) <= 0
        output_tokens = int(self.max_tokens if max_tokens is None else max_tokens)

        def build_payload(*, include_response_format: bool, include_thinking_controls: bool) -> dict[str, Any]:
            wants_json_response = include_response_format and response_format in {"json", "json_object"}
            request_prompt = _prompt_with_json_response_hint(prompt) if wants_json_response else prompt
            if use_responses:
                payload: dict[str, Any] = {
                    "model": self.model,
                    "input": [
                        {"role": "system", "content": [{"type": "input_text", "text": "You are a strict JSON generator. Return valid JSON only."}]},
                        {"role": "user", "content": [{"type": "input_text", "text": request_prompt}]},
                    ],
                    "temperature": self.temperature if temperature is None else temperature,
                }
                if not omit_max_tokens:
                    payload["max_output_tokens"] = output_tokens
                if wants_json_response:
                    payload["text"] = {"format": {"type": "json_object"}}
            else:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You are a strict JSON generator. Return valid JSON only, with no markdown."},
                        {"role": "user", "content": request_prompt},
                    ],
                    "temperature": self.temperature if temperature is None else temperature,
                }
                if not omit_max_tokens:
                    payload["max_tokens"] = output_tokens
                if wants_json_response:
                    payload["response_format"] = {"type": "json_object"}
            if is_deepseek and "v4-flash" in str(self.model or "").lower():
                payload.pop("reasoning_effort", None)
                payload.pop("thinking", None)
                payload.pop("enable_thinking", None)
                payload.pop("extra_body", None)
                payload["temperature"] = self.temperature if temperature is None else temperature
                if not omit_max_tokens:
                    if use_responses:
                        payload["max_output_tokens"] = output_tokens
                    else:
                        payload["max_tokens"] = output_tokens
            if reasoning_effort and reasoning_effort not in {"none", "off", "disable", "disabled", "0", "false", "no"}:
                payload["reasoning_effort"] = reasoning_effort
            if include_thinking_controls and disable_thinking:
                payload["thinking"] = {"type": "disabled"}
                payload["enable_thinking"] = False
                payload["extra_body"] = {"thinking": {"type": "disabled"}}
            return payload

        def request_once(*, include_response_format: bool, include_thinking_controls: bool) -> str:
            payload = build_payload(include_response_format=include_response_format, include_thinking_controls=include_thinking_controls)
            req = urllib.request.Request(
                _responses_url(self.base_url) if use_responses else _chat_url(self.base_url),
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
            last_error: Exception | None = None
            for attempt in range(1, self.retries + 1):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
                        raw = json.loads(response.read().decode("utf-8", "ignore"))
                    text = _extract_chat_text(raw)
                    if text:
                        return text
                    raise RuntimeError("Chat Completions API returned no extractable text; " + _chat_response_debug(raw))
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", "ignore")[:800]
                    last_error = RuntimeError(f"LLM HTTP {exc.code} via {self.api_mode or 'chat_completions'}: {body}")
                    if exc.code not in retry_statuses or attempt >= self.retries:
                        raise last_error from exc
                except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                    last_error = exc
                    if attempt >= self.retries:
                        raise RuntimeError(f"LLM request failed via {self.api_mode or 'chat_completions'} after {self.retries} attempts: {exc}") from exc
                retry_text = str(last_error or "").lower()
                slow_provider = any(marker in str(self.base_url or "").lower() for marker in ["sensenova", "xiaomi", "mi.com", "bigmodel.cn"])
                rate_limited = any(marker in retry_text for marker in ["429", "rate", "rpm", "too many", "timeout", "timed out"])
                base_sleep = min(2 ** (attempt - 1), 8) + 0.1 * attempt
                if slow_provider or rate_limited:
                    base_sleep = max(base_sleep, min(12.0, 2.5 * attempt))
                time.sleep(base_sleep)
            raise RuntimeError(f"LLM request failed via {self.api_mode or 'chat_completions'}: {last_error}")

        attempts: list[tuple[bool, bool]] = [(True, True)]
        if response_format in {"json", "json_object"}:
            attempts.append((False, True))
        if disable_thinking:
            attempts.append((response_format in {"json", "json_object"}, False))
            if response_format in {"json", "json_object"}:
                attempts.append((False, False))
        last_error: Exception | None = None
        seen: set[tuple[bool, bool]] = set()
        for include_response_format, include_thinking_controls in attempts:
            key = (include_response_format, include_thinking_controls)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = request_once(include_response_format=include_response_format, include_thinking_controls=include_thinking_controls)
                if include_response_format and retry_empty_json and response_format in {"json", "json_object"} and text.strip() == "{}":
                    last_error = RuntimeError("LLM returned empty JSON object with response_format")
                    continue
                return text
            except RuntimeError as exc:
                last_error = exc
                message = str(exc).lower()
                if retry_unsupported_optional and include_thinking_controls and any(token in message for token in ["unsupported parameter", "enable_thinking", "thinking"]):
                    continue
                if retry_unsupported_optional and include_response_format and any(token in message for token in ["response_format", "json_object", "unsupported parameter"]):
                    continue
                if include_response_format and response_format in {"json", "json_object"}:
                    continue
                raise
        raise RuntimeError(f"LLM request failed via {self.api_mode or 'chat_completions'}: {last_error}")

    def json_or_none(self, prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> Any | None:
        try:
            return extract_json(self.chat(prompt, temperature=temperature, max_tokens=max_tokens))
        except Exception:
            return None

    def json_or_error(self, prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> dict:
        raw_text = ""
        try:
            raw_text = self.chat(prompt, temperature=temperature, max_tokens=max_tokens)
            return {"ok": True, "data": extract_json(raw_text), "error": "", "raw_text": raw_text[:4000]}
        except Exception as first_exc:
            parse_error = str(first_exc)
            if raw_text and any(token in parse_error.lower() for token in ["closing bracket", "unterminated", "expecting", "delimiter"]):
                try:
                    retry_tokens = 0 if max_tokens is not None and int(max_tokens) <= 0 else max(self.max_tokens * 2, int(os.environ.get("LLM_PARSE_RETRY_MAX_TOKENS", "12000") or 12000))
                    raw_text = self.chat(prompt, temperature=temperature, max_tokens=retry_tokens)
                    return {"ok": True, "data": extract_json(raw_text), "error": "", "raw_text": raw_text[:4000], "parse_retry": True}
                except Exception as retry_exc:
                    return {"ok": False, "data": None, "error": f"{parse_error}; retry_failed: {retry_exc}", "raw_text": raw_text[:4000]}
            return {"ok": False, "data": None, "error": parse_error, "raw_text": raw_text[:4000]}


def clamp_workers(value: int | None, default: int = 16, maximum: int = 32) -> int:
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(maximum, number))


_LOCAL_GENERIC_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "to", "with",
    "paper", "papers", "study", "studies", "using", "use", "used", "method", "methods", "model", "models", "system", "systems",
}


def _text_terms(text: str, *, min_len: int = 2) -> list[str]:
    raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{1,}", (text or "").lower())
    terms: list[str] = []
    for raw in raw_terms:
        term = raw.strip(".,;:!?()[]{}\"'")
        if len(term) < min_len or term in _LOCAL_GENERIC_TERMS:
            continue
        terms.append(term)
    return list(dict.fromkeys(terms))


def keyword_category(title: str, abstract: str = "") -> str:
    terms = _text_terms(f"{title} {abstract}", min_len=3)[:3]
    return "Local topic" if not terms else "Local topic: " + " / ".join(terms)


def _interest_terms(interest: str) -> list[str]:
    return _text_terms(interest, min_len=2)


def _interest_phrases(interest: str) -> list[str]:
    phrases: list[str] = []
    for part in re.split(r"[\n,;，；。.!?、/]+", interest or ""):
        text = " ".join(str(part).lower().split())
        if len(text) >= 4:
            phrases.append(text)
    terms = [term for term in _interest_terms(interest) if re.fullmatch(r"[a-zA-Z0-9_.-]+", term)]
    for size in range(2, min(5, len(terms)) + 1):
        for index in range(0, len(terms) - size + 1):
            phrases.append(" ".join(terms[index:index + size]))
    return list(dict.fromkeys(phrase for phrase in phrases if len(phrase) >= 4))


def fallback_score(interest: str, title: str, abstract: str = "") -> float:
    haystack = f"{title} {abstract}".lower()
    tokens = _interest_terms(interest)
    if not tokens:
        return 6.0
    title_l = (title or "").lower()
    title_hits = sum(1 for token in tokens if token in title_l)
    text_hits = sum(1 for token in tokens if token in haystack)
    coverage = text_hits / max(1, len(tokens))
    phrase_hits = sum(1 for phrase in _interest_phrases(interest) if phrase in haystack)
    score = 4.0 + min(4.0, coverage * 4.0) + min(1.0, title_hits * 0.25) + min(1.0, phrase_hits * 0.35)
    return round(max(0.0, min(9.5, score)), 2)


# Backward-compatible dotted imports for callers that still use the old package layout.
def _register_compat_aliases(*aliases: str) -> None:
    import sys as _sys
    _module = _sys.modules.get(__name__)
    if _module is None:
        return
    globals().setdefault("__path__", [])
    for _alias in aliases:
        _sys.modules.setdefault(_alias, _module)

_register_compat_aliases('finding_runtime.paths', 'finding_runtime.source_selection', 'finding_runtime.models', 'finding_runtime.jobs', 'finding_runtime.storage', 'finding_runtime.markdown', 'finding_runtime.llm')
