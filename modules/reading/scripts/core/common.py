from __future__ import annotations

import json
import html
import os
import re
import shutil
import threading
import time
import datetime as dt
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux is the supported TASTE runtime.
    fcntl = None


READING_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = READING_ROOT / "config"
CONFIG_FILE = CONFIG_ROOT / "reading.json"
READ_ENV_FILE = CONFIG_ROOT / "read.env"


def load_read_env_file(path: Path = READ_ENV_FILE) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return loaded
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


READ_ENV = load_read_env_file()


_TITLE_SUPERSCRIPT_TRANSLATION = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾", "0123456789+-=()")
_TITLE_SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()")


def clean_fixed_chinese_abstract(value: object) -> str:
    text = html.unescape(str(value or "")).replace("\u00a0", " ")
    text = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1\s*>", " ", text, flags=re.I)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<h([1-6])\b[^>]*>[\s\S]*?</h\1\s*>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</(?:p|div|li|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^>]*)?\s*/?>", "", text)
    lines = [" ".join(line.split()) for line in html.unescape(text).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def display_paper_title(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s*<sup\b[^>]*>(.*?)</sup\s*>", lambda match: match.group(1).translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")), text, flags=re.I | re.S)
    text = re.sub(r"\s*<sub\b[^>]*>(.*?)</sub\s*>", lambda match: match.group(1).translate(str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")), text, flags=re.I | re.S)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = re.sub(r"(?<!\\)[*_`]", "", text)
    return " ".join(text.split()).strip()


def normalized_paper_title(value: object) -> str:
    text = display_paper_title(value).translate(_TITLE_SUPERSCRIPT_TRANSLATION).translate(_TITLE_SUBSCRIPT_TRANSLATION)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"[^\w-]+", " ", text, flags=re.UNICODE)
    return " ".join(text.casefold().split())


def is_placeholder_paper_title(value: object) -> bool:
    return normalized_paper_title(value) in {
        "论文标题",
        "未命名论文",
        "paper title",
        "title",
        "untitled",
    }


def paper_title_tokens(value: object) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "towards", "toward", "with"}
    normalized = re.sub(r"[\u2010-\u2015]", "-", str(value or ""))
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", normalized)
        if len(token) >= 2 and token.lower() not in stop
    }


def paper_title_similarity(left: object, right: object) -> float:
    left_tokens = paper_title_tokens(left)
    right_tokens = paper_title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def paper_author_family_tokens(value: object) -> set[str]:
    names = [str(item or "") for item in value] if isinstance(value, list) else re.split(r"[,;]", str(value or ""))
    tokens: set[str] = set()
    for name in names:
        parts = [part.lower() for part in re.findall(r"[A-Za-z][A-Za-z-]+", name)]
        if parts:
            tokens.add(parts[-1])
    return tokens


def best_full_text_title(paper: dict[str, Any], extracted_text: object) -> str:
    lines: list[str] = []
    for raw_line in str(extracted_text or "").splitlines():
        line = re.sub(r"-\s*\n\s*", "", raw_line)
        line = re.sub(r"\s+", " ", line).strip()[:240].rstrip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith(("abstract", "references", "acknowledgments", "acknowledgements")):
            break
        if lowered.startswith(("keywords", "introduction")) and lines:
            break
        lines.append(line)
        if len(lines) >= 36:
            break
    if not lines:
        return ""
    expected_title = str(paper.get("title") or "").strip()
    best_title = ""
    best_similarity = 0.0
    for start in range(min(10, len(lines))):
        for count in range(1, min(10, len(lines) - start) + 1):
            candidate = " ".join(lines[start:start + count]).strip()
            candidate = re.sub(r"([A-Za-z]{2,})-\s+([A-Za-z]{1,3})(?=\b)", r"\1\2", candidate)
            candidate = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1 \2", candidate)
            candidate = re.sub(r"\s+", " ", candidate).strip()
            similarity = paper_title_similarity(expected_title, candidate)
            if similarity > best_similarity:
                best_similarity = similarity
                best_title = candidate
    expected_authors = paper_author_family_tokens(paper.get("authors"))
    found_authors = paper_author_family_tokens(", ".join(lines[:36]))
    if expected_authors:
        overlap = expected_authors & found_authors
        accepted = (
            (best_similarity >= 0.82 and bool(overlap))
            or (best_similarity >= 0.78 and len(overlap) >= 2)
            or (best_similarity >= 0.70 and len(overlap) >= 4)
        )
    else:
        accepted = best_similarity >= 0.92
    return best_title if accepted else ""


def has_substantive_chinese(value: object, *, minimum: int = 4) -> bool:
    text = str(value or "")
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    return chinese_count >= minimum and (
        latin_count == 0 or chinese_count / (chinese_count + latin_count) >= 0.15
    )


_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_ENGLISH_FUNCTION_WORDS = {
    "a", "an", "and", "are", "as", "by", "for", "from", "in", "is", "of",
    "on", "our", "that", "the", "this", "to", "we", "which", "with",
}


def _english_words(value: object) -> list[str]:
    return [word.casefold() for word in _ENGLISH_WORD_RE.findall(str(value or ""))]


def _copies_english_source(candidate: str, source: str) -> bool:
    source_words = _english_words(source)
    candidate_words = _english_words(candidate)
    window = 10
    if len(source_words) < window or len(candidate_words) < window:
        return False
    source_windows = {
        tuple(source_words[index:index + window])
        for index in range(len(source_words) - window + 1)
    }
    return any(
        tuple(candidate_words[index:index + window]) in source_windows
        for index in range(len(candidate_words) - window + 1)
    )


def _contains_long_english_prose(value: str) -> bool:
    text = re.sub(r"https?://\S+|`[^`]*`|\$[^$]*\$", " ", str(value or ""))
    for segment in re.split(r"(?<=[.!?])\s+|\n+", text):
        words = _english_words(segment)
        if len(words) < 12:
            continue
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", segment))
        function_word_count = sum(word in _ENGLISH_FUNCTION_WORDS for word in words)
        if chinese_count <= 2 and function_word_count >= 2:
            return True
    return False


def chinese_translation_quality_issue(value: object, source_english: object = "") -> str:
    text = str(value or "").strip()
    if not has_substantive_chinese(text):
        return "missing_substantive_chinese"
    if _copies_english_source(text, str(source_english or "")):
        return "copied_english_source"
    if _contains_long_english_prose(text):
        return "long_english_prose"
    return ""


DEFAULT_READING_CONFIG: dict[str, Any] = {
    "default_channels": [
        "nips",
        "iclr",
        "icml",
        "sigkdd",
        "sigir",
        "cikm",
        "aaai",
        "iccv",
        "www",
        "cvpr",
        "acl",
        "ijcai",
        "eccv",
        "emnlp",
    ],
    "runtime": {
        "root": ".runtime",
        "output_dir": ".runtime/output",
        "latest_run_dir": ".runtime/latest_run",
    },
    "reading": {
        "default_max_papers": 50,
    },
    "http": {
        "default_timeout_sec": 30,
        "max_retry_after_sec": 120,
        "user_agent": "TASTE-Reading/1.0",
        "min_interval_sec": {
            "arxiv": 3.1,
            "biorxiv": 3.0,
            "science": 3.0,
            "openreview": 1.0,
            "iclr": 3.0,
            "icml": 3.0,
            "crossref": 0.34,
            "openalex": 0.05,
            "semanticscholar": 1.0,
            "europepmc": 0.25,
            "springernature": 0.7,
            "unpaywall": 0.25,
            "acm": 5.0,
            "reader": 2.0,
            "chatpaper": 10.0,
            "github": 1.0,
            "web_search": 1.0,
            "generic": 0.05,
        },
        "challenge_cooldown_sec": {
            "biorxiv": 60.0,
            "science": 30.0,
            "openreview": 30.0,
            "iclr": 60.0,
            "icml": 60.0,
            "acm": 30.0,
            "generic": 10.0,
        },
        "access_denied_cooldown_sec": {
            "openreview": 30.0,
            "iclr": 30.0,
            "icml": 30.0,
        },
        "process_access_blocker_sec": {
            "reader": 60.0,
            "web_search": 30.0,
            "github": 60.0,
            "generic": 30.0,
        },
        "rate_limit_cooldown_sec": {
            "arxiv": 6.0,
            "biorxiv": 15.0,
            "science": 15.0,
            "openreview": 10.0,
            "iclr": 15.0,
            "icml": 15.0,
            "crossref": 2.0,
            "openalex": 2.0,
            "semanticscholar": 5.0,
            "europepmc": 2.0,
            "springernature": 5.0,
            "unpaywall": 2.0,
            "acm": 15.0,
            "reader": 10.0,
            "chatpaper": 5.0,
            "github": 60.0,
            "web_search": 30.0,
            "generic": 10.0,
        },
        "batch_challenge_cooldown_wait_cap_sec": {
            "arxiv": 10.0,
            "biorxiv": 60.0,
            "science": 30.0,
            "openreview": 30.0,
            "iclr": 60.0,
            "icml": 60.0,
            "acm": 30.0,
            "github": 60.0,
            "generic": 20.0,
        },
        "batch_cooldown_requeue_wait_cap_sec": 30.0,
    },
    "openreview": {
        "allow_anonymous_http": True,
        "allow_anonymous_official_client": True,
        "reader_pdf_text": True,
    },
    "semantic_scholar": {
        "enabled_without_key": False,
    },
    "search": {
        "query_limit": 3,
        "acm_query_limit": 8,
        "duckduckgo_direct_attempts": 1,
        "duckduckgo_timeout_sec": 5.0,
        "startpage_timeout_sec": 5.0,
    },
    "pdf": {
        "failure_sleep_sec": 0.3,
    },
    "full_text_min_chars": 1200,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_reading_config() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
    return _deep_merge(DEFAULT_READING_CONFIG, payload)


READING_CONFIG = load_reading_config()


def config_value(path: str, default: Any = None) -> Any:
    value: Any = READING_CONFIG
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def config_bool(path: str, default: bool = False) -> bool:
    value = config_value(path, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def config_float(path: str, default: float) -> float:
    try:
        return float(config_value(path, default))
    except Exception:
        return default


def config_int(path: str, default: int) -> int:
    try:
        return int(config_value(path, default))
    except Exception:
        return default


def _configured_path(path: str, fallback: str) -> Path:
    raw = str(config_value(path, fallback) or fallback).strip()
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else READING_ROOT / candidate


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disable", "disabled"}


RUNTIME_ROOT = _configured_path("runtime.root", ".runtime")
OUTPUT_ROOT = _configured_path("runtime.output_dir", ".runtime/output")
INPUT_ROOT = OUTPUT_ROOT
LATEST_RUN_ROOT = _configured_path("runtime.latest_run_dir", ".runtime/latest_run")
WORKSPACE_ROOT = OUTPUT_ROOT
RUNS_ROOT = OUTPUT_ROOT
BATCH_TESTS_ROOT = OUTPUT_ROOT
LEGACY_RUNTIME_RUNS_ROOT = RUNTIME_ROOT / "runs"
LEGACY_RUNTIME_BATCH_TESTS_ROOT = RUNTIME_ROOT / "batch_tests"
LEGACY_WORKSPACE_ROOT = READING_ROOT / "workspace"
CACHE_BATCH_TEST_ROOTS = (OUTPUT_ROOT,)
CACHE_RUN_ROOTS = (OUTPUT_ROOT,)
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}\d{6}Z$")


def _as_path(value: str | Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else READING_ROOT / path


def resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def ensure_inside_reading(path: Path, *, label: str = "path") -> Path:
    candidate = resolved(_as_path(path))
    root = resolved(READING_ROOT)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于当前工作区内") from exc
    return candidate


def ensure_inside_runtime(path: Path, *, label: str = "path") -> Path:
    candidate = ensure_inside_reading(path, label=label)
    runtime_root = resolved(RUNTIME_ROOT)
    try:
        candidate.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于 .runtime 下") from exc
    return candidate


def ensure_inside_output(path: Path, *, label: str = "path") -> Path:
    candidate = ensure_inside_reading(path, label=label)
    output_root = resolved(OUTPUT_ROOT)
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于 .runtime/output 下") from exc
    return candidate


def ensure_inside_input(path: Path, *, label: str = "path") -> Path:
    candidate = ensure_inside_output(path, label=label)
    output_root = resolved(OUTPUT_ROOT)
    try:
        rel = candidate.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于 .runtime/output 下") from exc
    if len(rel.parts) < 3 or rel.parts[1] != "input":
        raise ValueError(f"{label} 必须位于 .runtime/output/<run-id>/input/ 下")
    return candidate


def ensure_output_root() -> Path:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return OUTPUT_ROOT


def ensure_input_root() -> Path:
    return ensure_output_root()


def ensure_workspace() -> Path:
    return ensure_output_root()


def timestamp_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("Reading 运行目录名必须是 UTC 精确时间戳，格式为 YYYYMMDDTHHMMSSffffffZ")
    return value


def create_run_dir() -> Path:
    ensure_output_root()
    for _attempt in range(100):
        run_id = timestamp_run_id()
        path = ensure_inside_output(RUNS_ROOT / run_id, label="运行目录")
        try:
            path.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            time.sleep(0.001)
            continue
        (path / "run_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "runtime_policy": "Reading run directory is created once at run start and all process outputs stay under this fixed directory.",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path
    raise RuntimeError("无法创建唯一 Reading 时间戳运行目录")


def run_dir(run_id: str) -> Path:
    value = validate_run_id(run_id)
    path = ensure_inside_output(RUNS_ROOT / value, label="运行目录")
    path.mkdir(parents=True, exist_ok=True)
    return path


def existing_run_dir(run_id: str) -> Path:
    value = validate_run_id(run_id)
    path = ensure_inside_output(RUNS_ROOT / value, label="运行目录")
    if not path.is_dir():
        raise ValueError(f"Reading 运行目录尚未创建：{path}")
    return path


def refresh_latest_run(source: Path) -> Path:
    source_dir = ensure_inside_output(source, label="latest_run 来源")
    if not source_dir.is_dir():
        raise ValueError("latest_run 来源必须是已存在的运行目录")
    LATEST_RUN_ROOT.parent.mkdir(parents=True, exist_ok=True)
    lock_path = LATEST_RUN_ROOT.parent / ".latest_run.lock"
    temp_dir = LATEST_RUN_ROOT.parent / f".latest_run.tmp.{os.getpid()}.{source_dir.name}"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            shutil.copytree(source_dir, temp_dir, ignore=shutil.ignore_patterns("__pycache__"))
            if LATEST_RUN_ROOT.exists():
                if LATEST_RUN_ROOT.is_symlink() or LATEST_RUN_ROOT.is_file():
                    LATEST_RUN_ROOT.unlink()
                else:
                    shutil.rmtree(LATEST_RUN_ROOT)
            temp_dir.rename(LATEST_RUN_ROOT)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return LATEST_RUN_ROOT


def relative_to_reading(path: Path) -> str:
    candidate = ensure_inside_reading(path)
    return candidate.relative_to(resolved(READING_ROOT)).as_posix()


def resolve_reading_path(path: str | Path) -> Path:
    return ensure_inside_reading(_as_path(path), label="Reading 相对路径")


def _relative_path_string(value: str) -> str:
    text = str(value)
    if not text:
        return text
    reading_root = resolved(READING_ROOT).as_posix()
    legacy_segment = "/".join(READING_ROOT.parts[-2:])
    if text == reading_root or text == legacy_segment:
        return "."
    legacy_prefix = legacy_segment + "/"
    for prefix in (reading_root + "/", legacy_prefix):
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    text = text.replace(reading_root + "/", "")
    text = text.replace(legacy_prefix, "")
    text = text.replace(reading_root, ".")
    return text


def make_reading_paths_relative(value: Any) -> Any:
    if isinstance(value, Path):
        try:
            return relative_to_reading(value)
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(key): make_reading_paths_relative(item) for key, item in value.items()}
    if isinstance(value, list):
        return [make_reading_paths_relative(item) for item in value]
    if isinstance(value, tuple):
        return [make_reading_paths_relative(item) for item in value]
    if isinstance(value, str):
        return _relative_path_string(value)
    return value


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(make_reading_paths_relative(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(make_reading_paths_relative(str(text))), encoding="utf-8")


def scrub_reading_paths_in_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    cleaned = str(make_reading_paths_relative(text))
    if cleaned != text:
        path.write_text(cleaned, encoding="utf-8")


def scrub_reading_paths_under(root: Path) -> None:
    if not root.exists():
        return
    allowed_suffixes = {".json", ".md", ".txt", ".log", ".jsonl", ".yaml", ".yml"}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in allowed_suffixes:
            scrub_reading_paths_in_file(path)


def safe_slug(value: Any, fallback: str = "paper", max_len: int = 90) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("_.-")
    return (text or fallback)[:max_len]


_PROSE_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
_MARKDOWN_PROTECTED_SPAN_RE = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([^\n]*?\\\)|(?<!\\)\$[^$\n]*?(?<!\\)\$"
)


def has_unresolved_prose_latex_markup(value: object) -> bool:
    text = str(value or "")
    protected = [(match.start(), match.end()) for match in _MARKDOWN_PROTECTED_SPAN_RE.finditer(text)]
    return any(
        not any(start <= match.start() < end for start, end in protected)
        for match in _PROSE_LATEX_COMMAND_RE.finditer(text)
    )


def coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [part.strip() for part in re.split(r"[,;]", str(value or "")) if part.strip()]


def first_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.I | re.S):
        block = match.group(1).strip()
        try:
            payload = json.loads(block)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


DEFAULT_TIMEOUT = config_int("http.default_timeout_sec", 30)


def service_contact_email(service: str = "") -> str:
    general = str(os.environ.get("READING_CONTACT_EMAIL") or "").strip()
    service_name = str(service or "").strip().lower()
    if service_name == "openalex":
        return str(os.environ.get("OPENALEX_MAILTO") or general).strip()
    if service_name == "crossref":
        return str(os.environ.get("CROSSREF_MAILTO") or general).strip()
    return general


CONTACT_EMAIL = service_contact_email()
USER_AGENT_CONTACT_EMAIL = CONTACT_EMAIL or service_contact_email("openalex") or service_contact_email("crossref")
DEFAULT_USER_AGENT = (
    str(os.environ.get("READING_HTTP_USER_AGENT") or "").strip()
    or (str(config_value("http.user_agent", "TASTE-Reading/1.0")).strip() + (f" (mailto:{USER_AGENT_CONTACT_EMAIL})" if USER_AGENT_CONTACT_EMAIL else ""))
)
FULL_TEXT_MIN_CHARS = config_int("full_text_min_chars", 1200)

SERVICE_MIN_INTERVAL_SEC = {
    "arxiv": _env_float("READING_ARXIV_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.arxiv", 3.1)),
    "biorxiv": _env_float("READING_BIORXIV_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.biorxiv", 3.0)),
    "science": _env_float("READING_SCIENCE_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.science", 3.0)),
    "openreview": _env_float("READING_OPENREVIEW_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.openreview", 1.0)),
    "iclr": _env_float("READING_ICLR_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.iclr", 3.0)),
    "icml": _env_float("READING_ICML_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.icml", 3.0)),
    "crossref": _env_float("READING_CROSSREF_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.crossref", 0.34)),
    "openalex": _env_float("READING_OPENALEX_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.openalex", 0.05)),
    "semanticscholar": _env_float("READING_SEMANTIC_SCHOLAR_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.semanticscholar", 1.0)),
    "europepmc": _env_float("READING_EUROPEPMC_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.europepmc", 0.25)),
    "springernature": _env_float("READING_SPRINGER_NATURE_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.springernature", 0.7)),
    "unpaywall": _env_float("READING_UNPAYWALL_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.unpaywall", 0.25)),
    "acm": _env_float("READING_ACM_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.acm", 5.0)),
    "reader": _env_float("READING_READER_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.reader", 2.0)),
    "chatpaper": _env_float("READING_CHATPAPER_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.chatpaper", 10.0)),
    "github": _env_float("READING_GITHUB_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.github", 1.0)),
    "web_search": _env_float("READING_WEB_SEARCH_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.web_search", 1.0)),
    "generic": _env_float("READING_GENERIC_MIN_INTERVAL_SEC", config_float("http.min_interval_sec.generic", 0.05)),
}

_SERVICE_LOCKS_LOCK = threading.Lock()
_SERVICE_LOCKS: dict[str, threading.Lock] = {}
_SERVICE_STATE_ROOT = Path(
    os.environ.get("FRAMEWORK_RUNTIME_DIR")
    or READING_ROOT.parents[1] / "framework" / ".runtime"
) / "locks" / "crawl_services"
_PROCESS_BLOCKERS_LOCK = threading.Lock()
_PROCESS_BLOCKERS: dict[str, dict[str, Any]] = {}
_PROCESS_BACKEND_LOCKS_LOCK = threading.Lock()
_PROCESS_BACKEND_LOCKS: dict[str, threading.Lock] = {}

SERVICE_CHALLENGE_COOLDOWN_SEC = {
    "biorxiv": _env_float("READING_BIORXIV_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.biorxiv", 60.0)),
    "science": _env_float("READING_SCIENCE_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.science", 30.0)),
    "openreview": _env_float("READING_OPENREVIEW_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.openreview", 30.0)),
    "iclr": _env_float("READING_ICLR_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.iclr", 60.0)),
    "icml": _env_float("READING_ICML_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.icml", 60.0)),
    "acm": _env_float("READING_ACM_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.acm", 30.0)),
    "generic": _env_float("READING_GENERIC_CHALLENGE_COOLDOWN_SEC", config_float("http.challenge_cooldown_sec.generic", 10.0)),
}

SERVICE_ACCESS_DENIED_COOLDOWN_SEC = {
    "openreview": _env_float("READING_OPENREVIEW_ACCESS_DENIED_COOLDOWN_SEC", config_float("http.access_denied_cooldown_sec.openreview", 30.0)),
    "iclr": _env_float("READING_ICLR_ACCESS_DENIED_COOLDOWN_SEC", config_float("http.access_denied_cooldown_sec.iclr", 30.0)),
    "icml": _env_float("READING_ICML_ACCESS_DENIED_COOLDOWN_SEC", config_float("http.access_denied_cooldown_sec.icml", 30.0)),
}
_RATE_LIMIT_DEFAULTS = {
    "arxiv": 6.0, "biorxiv": 15.0, "science": 15.0, "openreview": 10.0,
    "iclr": 15.0, "icml": 15.0, "crossref": 2.0, "openalex": 2.0,
    "semanticscholar": 5.0, "europepmc": 2.0, "springernature": 5.0,
    "unpaywall": 2.0, "acm": 15.0, "reader": 10.0, "chatpaper": 5.0,
    "github": 60.0, "web_search": 30.0, "generic": 10.0,
}
_GLOBAL_RATE_LIMIT_OVERRIDE = str(os.environ.get("READING_RATE_LIMIT_COOLDOWN_SEC") or "").strip()
SERVICE_RATE_LIMIT_COOLDOWN_SEC_BY_SERVICE = {
    service: _env_float(
        f"READING_{service.upper()}_RATE_LIMIT_COOLDOWN_SEC",
        _env_float("READING_RATE_LIMIT_COOLDOWN_SEC", default)
        if _GLOBAL_RATE_LIMIT_OVERRIDE
        else config_float(f"http.rate_limit_cooldown_sec.{service}", default),
    )
    for service, default in _RATE_LIMIT_DEFAULTS.items()
}
SERVICE_RATE_LIMIT_COOLDOWN_SEC = SERVICE_RATE_LIMIT_COOLDOWN_SEC_BY_SERVICE["generic"]
_PROCESS_ACCESS_DEFAULTS = {"reader": 60.0, "web_search": 30.0, "github": 60.0, "generic": 30.0}
_GLOBAL_PROCESS_ACCESS_OVERRIDE = str(os.environ.get("READING_PROCESS_ACCESS_BLOCKER_SEC") or "").strip()
PROCESS_ACCESS_BLOCKER_SEC_BY_SERVICE = {
    service: _env_float(
        f"READING_{service.upper()}_PROCESS_ACCESS_BLOCKER_SEC",
        _env_float("READING_PROCESS_ACCESS_BLOCKER_SEC", default)
        if _GLOBAL_PROCESS_ACCESS_OVERRIDE
        else config_float(f"http.process_access_blocker_sec.{service}", default),
    )
    for service, default in _PROCESS_ACCESS_DEFAULTS.items()
}
PROCESS_ACCESS_BLOCKER_SEC = PROCESS_ACCESS_BLOCKER_SEC_BY_SERVICE["generic"]


def service_rate_limit_cooldown(service: str) -> float:
    return max(
        0.0,
        SERVICE_RATE_LIMIT_COOLDOWN_SEC_BY_SERVICE.get(
            str(service or "generic").strip().lower(),
            SERVICE_RATE_LIMIT_COOLDOWN_SEC,
        ),
    )


def batch_cooldown_wait_cap(service: str) -> float:
    service_name = str(service or "generic").strip().lower() or "generic"
    default = {
        "arxiv": 10.0, "biorxiv": 60.0, "science": 30.0, "openreview": 30.0,
        "iclr": 60.0, "icml": 60.0, "acm": 30.0, "github": 60.0,
        "generic": 20.0,
    }.get(service_name, 20.0)
    return max(0.0, config_float(
        f"http.batch_challenge_cooldown_wait_cap_sec.{service_name}",
        config_float("http.batch_challenge_cooldown_wait_cap_sec.generic", default),
    ))


def _rate_limit_reset_seconds(response: requests.Response, service: str) -> float:
    headers = getattr(response, "headers", {}) or {}
    retry_after = retry_after_seconds(headers.get("retry-after"))
    if retry_after > 0:
        return retry_after
    reset_text = str(headers.get("x-ratelimit-reset") or "").strip()
    try:
        reset_value = float(reset_text)
    except ValueError:
        return 0.0
    if str(service or "").lower() == "openalex":
        return retry_after_seconds(reset_value)
    return retry_after_seconds(max(0.0, reset_value - time.time()))


def service_from_url(url: str) -> str:
    host = urlparse(str(url or "")).netloc.lower()
    if "arxiv.org" in host:
        return "arxiv"
    if host == "biorxiv.org" or host.endswith(".biorxiv.org") or host == "medrxiv.org" or host.endswith(".medrxiv.org"):
        return "biorxiv"
    if host == "science.org" or host.endswith(".science.org"):
        return "science"
    if "openreview.net" in host:
        return "openreview"
    if host == "iclr.cc" or host.endswith(".iclr.cc"):
        return "iclr"
    if host == "icml.cc" or host.endswith(".icml.cc"):
        return "icml"
    if "api.openalex.org" in host or host.endswith("openalex.org"):
        return "openalex"
    if "api.crossref.org" in host:
        return "crossref"
    if "semanticscholar.org" in host:
        return "semanticscholar"
    if "europepmc.org" in host or "ebi.ac.uk" in host:
        return "europepmc"
    if "springernature.com" in host or "nature.com" in host or "springer.com" in host:
        return "springernature"
    if "unpaywall.org" in host:
        return "unpaywall"
    if "dl.acm.org" in host:
        return "acm"
    if host in {"r.jina.ai", "s.jina.ai"}:
        return "reader"
    if host == "api.github.com" or host == "raw.githubusercontent.com":
        return "github"
    if host.endswith("duckduckgo.com") or host.endswith("startpage.com"):
        return "web_search"
    if host == "chatpaper.com" or host.endswith(".chatpaper.com"):
        return "chatpaper"
    normalized_host = re.sub(r"[^a-z0-9_.-]+", "_", host).strip("_.-")
    return f"host_{normalized_host}" if normalized_host else "generic"


class ServiceCooldownActive(RuntimeError):
    def __init__(self, service: str, remaining: float, reason: str = "") -> None:
        self.service = service
        self.remaining = max(0.0, remaining)
        self.reason = reason
        super().__init__(f"{service} access cooldown active for {self.remaining:.1f}s")


class RobotsPolicyBlocked(RuntimeError):
    pass


def official_robots_block_reason(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if service_from_url(url) not in {"iclr", "icml"}:
        return ""
    path = parsed.path.lower()
    if path.startswith(("/static", "/admin", "/rpc", "/search")):
        return "official_robots_disallowed_path"
    if re.fullmatch(r"/virtual/[^/]+/search/?", path):
        return "official_robots_disallowed_virtual_search"
    if re.search(r"(?:^|&)page=", parsed.query, flags=re.I):
        return "official_robots_disallowed_page_query"
    return ""


def _service_state(handle: Any) -> dict[str, Any]:
    handle.seek(0)
    try:
        payload = json.load(handle)
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _write_service_state(handle: Any, state: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle, ensure_ascii=True, sort_keys=True)
    handle.flush()


@contextmanager
def service_request_slot(service: str, *, allow_during_cooldown: bool = False) -> Iterator[dict[str, Any]]:
    """Serialize one service/host across threads and processes, including external clients."""
    service_name = re.sub(r"[^a-z0-9_.-]+", "_", str(service or "generic").lower()) or "generic"
    min_interval = max(0.0, SERVICE_MIN_INTERVAL_SEC.get(service_name, SERVICE_MIN_INTERVAL_SEC["generic"]))
    with _SERVICE_LOCKS_LOCK:
        service_lock = _SERVICE_LOCKS.setdefault(service_name, threading.Lock())
    with service_lock:
        _SERVICE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        state_path = _SERVICE_STATE_ROOT / f"{service_name}.lock"
        with state_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = _service_state(handle)
                now = time.time()
                cooldown_until = float(state.get("cooldown_until") or 0.0)
                if cooldown_until > now and not allow_during_cooldown:
                    raise ServiceCooldownActive(service_name, cooldown_until - now, str(state.get("cooldown_reason") or ""))
                wait = max(0.0, min_interval - (now - float(state.get("last_finished_at") or 0.0)))
                if wait:
                    time.sleep(wait)
                gate: dict[str, Any] = {"service": service_name, "waited_sec": round(wait, 3)}
                try:
                    yield gate
                finally:
                    finished_at = time.time()
                    state["last_finished_at"] = finished_at
                    state["pid"] = os.getpid()
                    response = gate.get("response")
                    status_code = int(gate.get("status_code") or getattr(response, "status_code", 0) or 0)
                    headers = getattr(response, "headers", {}) if response is not None else {}
                    cooldown = max(0.0, float(gate.get("cooldown_sec") or 0.0))
                    reason = str(gate.get("cooldown_reason") or "")
                    if str(headers.get("cf-mitigated") or "").lower() == "challenge":
                        cooldown = max(cooldown, SERVICE_CHALLENGE_COOLDOWN_SEC.get(service_name, SERVICE_CHALLENGE_COOLDOWN_SEC["generic"]))
                        reason = reason or "cloudflare_challenge"
                    if status_code == 429:
                        provider_wait = _rate_limit_reset_seconds(response, service_name) if response is not None else 0.0
                        cooldown = max(cooldown, provider_wait or service_rate_limit_cooldown(service_name))
                        reason = reason or "http_429"
                    if (
                        status_code == 403
                        and service_name == "github"
                        and str(headers.get("x-ratelimit-remaining") or "").strip() == "0"
                    ):
                        cooldown = max(cooldown, _rate_limit_reset_seconds(response, service_name) or service_rate_limit_cooldown(service_name))
                        reason = reason or "github_rate_limit_exhausted"
                    if status_code == 403 and service_name in SERVICE_ACCESS_DENIED_COOLDOWN_SEC:
                        cooldown = max(cooldown, SERVICE_ACCESS_DENIED_COOLDOWN_SEC[service_name])
                        reason = reason or "http_403"
                    if cooldown > 0:
                        state["cooldown_until"] = max(float(state.get("cooldown_until") or 0.0), finished_at + cooldown)
                        state["cooldown_reason"] = reason
                    _write_service_state(handle, state)
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def service_cooldown_remaining(service: str) -> float:
    service_name = re.sub(r"[^a-z0-9_.-]+", "_", str(service or "generic").lower()) or "generic"
    with _SERVICE_LOCKS_LOCK:
        service_lock = _SERVICE_LOCKS.setdefault(service_name, threading.Lock())
    with service_lock:
        state_path = _SERVICE_STATE_ROOT / f"{service_name}.lock"
        if not state_path.exists():
            return 0.0
        with state_path.open("r", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                remaining = float(_service_state(handle).get("cooldown_until") or 0.0) - time.time()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return round(max(0.0, remaining), 3)


def process_blocker(name: str) -> dict[str, Any]:
    key = re.sub(r"[^a-z0-9_.-]+", "_", str(name or "").lower())
    if not key:
        return {}
    with _PROCESS_BLOCKERS_LOCK:
        blocker = dict(_PROCESS_BLOCKERS.get(key) or {})
        if blocker and float(blocker.get("until_monotonic") or 0.0) <= time.monotonic():
            _PROCESS_BLOCKERS.pop(key, None)
            return {}
    return blocker


def mark_process_blocker(name: str, reason: str, *, ttl_sec: float | None = None) -> dict[str, Any]:
    key = re.sub(r"[^a-z0-9_.-]+", "_", str(name or "").lower())
    if not key:
        return {}
    ttl = max(1.0, float(PROCESS_ACCESS_BLOCKER_SEC if ttl_sec is None else ttl_sec))
    blocker = {
        "reason": str(reason or "session_backend_unavailable"),
        "ttl_sec": round(ttl, 3),
        "until_monotonic": time.monotonic() + ttl,
    }
    with _PROCESS_BLOCKERS_LOCK:
        _PROCESS_BLOCKERS[key] = blocker
    return dict(blocker)


@contextmanager
def process_backend_slot(name: str) -> Iterator[dict[str, Any]]:
    """Serialize one fallback backend so queued workers see its first failure."""
    key = re.sub(r"[^a-z0-9_.-]+", "_", str(name or "backend").lower()) or "backend"
    with _PROCESS_BACKEND_LOCKS_LOCK:
        backend_lock = _PROCESS_BACKEND_LOCKS.setdefault(key, threading.Lock())
    with backend_lock:
        yield process_blocker(key)


def jina_api_key_configured() -> bool:
    return bool(str(os.environ.get("JINA_API_KEY") or "").strip())


def jina_request_headers(*, accept: str = "text/plain,*/*") -> dict[str, str]:
    headers = {"Accept": accept}
    api_key = str(os.environ.get("JINA_API_KEY") or "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers


def _headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    out = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        out.update({key: value for key, value in headers.items() if key.lower() != "user-agent"})
        for key, value in headers.items():
            if key.lower() == "user-agent":
                out["User-Agent"] = value
    return out


def retry_after_seconds(value: object, *, cap: float | None = None) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        seconds = float(text)
    except ValueError:
        try:
            seconds = max(0.0, parsedate_to_datetime(text).timestamp() - time.time())
        except Exception:
            seconds = 0.0
    max_wait = cap
    if max_wait is None:
        max_wait = _env_float("READING_MAX_RETRY_AFTER_SEC", config_float("http.max_retry_after_sec", 20.0))
    return min(max(0.0, seconds), max_wait)


def process_http_blocker_ttl(response: requests.Response) -> float:
    status_code = int(getattr(response, "status_code", 0) or 0)
    service = service_from_url(str(getattr(response, "url", "") or ""))
    if status_code == 429:
        provider_wait = _rate_limit_reset_seconds(response, service)
        return provider_wait if provider_wait > 0 else service_rate_limit_cooldown(service)
    return PROCESS_ACCESS_BLOCKER_SEC_BY_SERVICE.get(service, PROCESS_ACCESS_BLOCKER_SEC)


def mark_process_http_blocker(name: str, response: requests.Response, reason: str) -> dict[str, Any]:
    return mark_process_blocker(
        name,
        reason,
        ttl_sec=process_http_blocker_ttl(response),
    )


def service_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    service: str | None = None,
    **kwargs: Any,
) -> requests.Response:
    robots_reason = official_robots_block_reason(url)
    if robots_reason:
        raise RobotsPolicyBlocked(f"{robots_reason}: {url}")
    service_name = service or service_from_url(url)
    with service_request_slot(service_name) as gate:
        response = requests.get(url, params=params, timeout=timeout, headers=_headers(headers), **kwargs)
        gate["response"] = response
    return response


def _response_header_subset(response: requests.Response) -> dict[str, str]:
    allowed = [
        "cf-mitigated",
        "retry-after",
        "server",
        "x-cache",
        "x-served-by",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "ratelimit-policy",
    ]
    subset: dict[str, str] = {}
    for key in allowed:
        value = response.headers.get(key)
        if value:
            subset[key.lower()] = str(value)[:160]
    return subset


def response_receipt(response: requests.Response, *, service: str | None = None) -> dict[str, Any]:
    service_name = service or service_from_url(response.url)
    receipt = {
        "service": service_name,
        "url": response.url,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "bytes": len(response.content or b""),
        "retry_after": response.headers.get("retry-after") or "",
        "rate_limit_policy": {
            "min_interval_sec": SERVICE_MIN_INTERVAL_SEC.get(service_name, SERVICE_MIN_INTERVAL_SEC["generic"]),
            "hard_concurrency": 1,
            "cross_process": True,
            "challenge_cooldown_sec": SERVICE_CHALLENGE_COOLDOWN_SEC.get(service_name, SERVICE_CHALLENGE_COOLDOWN_SEC["generic"]),
            "user_agent": DEFAULT_USER_AGENT,
        },
    }
    header_subset = _response_header_subset(response)
    if header_subset:
        receipt["headers_subset"] = header_subset
    if str(header_subset.get("cf-mitigated") or "").lower() == "challenge":
        receipt["challenge_type"] = "cloudflare"
    return receipt


def missing_official_access_reason(service: str) -> dict[str, Any]:
    if service == "openalex" and not str(os.environ.get("OPENALEX_API_KEY") or "").strip():
        return {
            "service": "openalex",
            "reason": "missing_openalex_api_key",
            "message_zh": "未设置 OPENALEX_API_KEY；OpenAlex 仍可低频尝试，但 2026 年起推荐使用 API key，未配置时可能被限流或降级。",
        }
    if service == "springernature" and not str(os.environ.get("SPRINGER_API_KEY") or os.environ.get("SPRINGER_NATURE_API_KEY") or "").strip():
        return {
            "service": "springernature",
            "reason": "missing_springer_nature_api_key",
            "message_zh": "未设置 SPRINGER_API_KEY/SPRINGER_NATURE_API_KEY；无法使用 Springer Nature Open Access/TDM API，只能尝试公开 article PDF/HTML。",
        }
    if service == "unpaywall" and not str(os.environ.get("UNPAYWALL_EMAIL") or "").strip():
        return {
            "service": "unpaywall",
            "reason": "missing_unpaywall_email",
            "message_zh": "未设置 UNPAYWALL_EMAIL；跳过 Unpaywall 官方 DOI 开放全文位置查询。",
        }
    if service == "semanticscholar" and not (
        str(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY") or "").strip()
        or str(os.environ.get("READING_ENABLE_SEMANTIC_SCHOLAR") or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    ):
        return {
            "service": "semanticscholar",
            "reason": "semantic_scholar_disabled",
            "message_zh": "未设置 Semantic Scholar API key，也未显式启用无 key 模式；跳过该增强源以避免共享限流拖慢主流程。",
        }
    return {}
