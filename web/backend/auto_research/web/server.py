from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import time
import threading
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path(__file__).resolve().parents[4]).expanduser().resolve()
_IMPORT_PRIORITY_DIRS = [
    WORKSPACE_ROOT / "framework" / "scripts",
    WORKSPACE_ROOT / "web" / "backend",
]
for _entry in [str(path) for path in reversed(_IMPORT_PRIORITY_DIRS) if path.exists()]:
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from bridges.finding_catalog import catalog_by_id, fetch_venue_sample, load_catalog
from bridges.project_bridge import action_gate_blocker, job_stage, create_project_config, detect_runtime_config, list_projects as list_projects, project_summary, run_action, runtime_status, update_project_config, update_runtime_config, _cleruntime_caches, _current_find_pipeline_summary, _current_find_source_status_rows, _venue_metadata_counts
from contracts.web_models import AppConfig, EmailJobRequest, FindRequest, IdeaMarkdownUpdate, IdeaPatch, IdeaRequest, LLMRoleConfig, PlanMarkdownUpdate, PlanPolishRequest, PlanRequest, ReadRequest, VenueHealthRequest
from integrations.emailer import send_run_email
from integrations.web_llm import LLMClient, extract_json
from policies.source_selection import canonical_source_selection, normalize_source_selection, save_canonical_source_selection, project_config_path
from project.project_paths import configured_max_read_papers
from reporting.paper_state import active_paper_state as load_active_paper_state
from runtime.framework_paths import CONFIG_PATH, FINDING_RUNS_DIR, RUNS_DIR, RUNS_SEARCH_DIRS, STATE_DIR, ensure_directories
from runtime.jobs import JobCancelled
from runtime.resource_locks import crawl_resource_lease
from runtime.run_storage import delete_run, list_runs, read_json, redacted_config, run_dir, write_json
from runtime.taste_pythonpath import taste_pythonpath_string
from auto_research.web.auth import AuthError, AuthStore, AuthUser, SESSION_COOKIE, SESSION_DAYS

ensure_directories()

app = FastAPI(title="TASTE Local API", version="0.1.0")
_cors_origins = [origin.strip() for origin in os.environ.get("TASTE_CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

AUTH_STORE = AuthStore(Path(os.environ.get("TASTE_AUTH_DB") or WORKSPACE_ROOT / "web" / ".runtime" / "auth.sqlite3"))
_CURRENT_ACCOUNT: ContextVar[AuthUser | None] = ContextVar("taste_current_account", default=None)


def _current_account() -> AuthUser:
    account = _CURRENT_ACCOUNT.get()
    if account is None:
        raise RuntimeError("Authenticated account context is required.")
    return account


def _account_prefix(account: AuthUser | None = None) -> str:
    return f"u_{(account or _current_account()).id}__"


def _account_runtime_dir(account: AuthUser | None = None) -> Path:
    current = account or _CURRENT_ACCOUNT.get()
    if current is None:
        return STATE_DIR.parent
    target = WORKSPACE_ROOT / "web" / ".runtime" / "accounts" / current.id
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def _account_project_id(value: Any, *, require_exists: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw or not re.fullmatch(r"[A-Za-z0-9_.-]+", raw):
        raise ValueError("Invalid project name. Use only letters, numbers, dash, underscore, and dot.")
    account = _CURRENT_ACCOUNT.get()
    if account is None:
        return raw
    prefix = _account_prefix(account)
    if raw.startswith(prefix):
        project = raw
    elif re.match(r"^u_[0-9a-f]{32}__", raw):
        raise ValueError("Project not found.")
    else:
        if len(raw) > 80:
            raise ValueError("Project name must be at most 80 characters.")
        project = prefix + raw
    if require_exists and not (PROJECT_IDS_ROOT / project).is_dir():
        raise ValueError("Project not found.")
    return project


def _account_owns_project(project: Any) -> bool:
    account = _CURRENT_ACCOUNT.get()
    return bool(account is not None and str(project or "").strip().startswith(_account_prefix(account)))


def _public_account_payload(value: Any) -> Any:
    account = _CURRENT_ACCOUNT.get()
    if account is None:
        return value
    prefix = _account_prefix(account)
    if isinstance(value, dict):
        return {key: _public_account_payload(item) for key, item in value.items() if key != "owner_id"}
    if isinstance(value, list):
        return [_public_account_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_account_payload(item) for item in value)
    if isinstance(value, str):
        if value.startswith(prefix):
            return value[len(prefix):]
        value = value.replace(f"/projects/{prefix}", "/projects/")
        value = value.replace(f"projects/{prefix}", "projects/")
    return value


def _request_uses_https(request: Request) -> bool:
    # Uvicorn validates trusted proxy IPs and normalizes the ASGI scheme before
    # this application sees the request. Reading the raw header here would let
    # direct clients on port 8879 spoof proxy trust.
    return request.url.scheme == "https"


def _set_session_cookie(response: JSONResponse, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=_request_uses_https(request),
        samesite="lax",
        path="/",
    )


@app.middleware("http")
async def _authenticate_api_requests(request: Request, call_next):
    path = str(request.url.path or "")
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return await call_next(request)
    account = AUTH_STORE.user_for_session(request.cookies.get(SESSION_COOKIE))
    if account is None:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    request.state.account = account
    token = _CURRENT_ACCOUNT.set(account)
    try:
        return await call_next(request)
    finally:
        _CURRENT_ACCOUNT.reset(token)


@app.middleware("http")
async def _prevent_stale_frontend_cache(request, call_next):
    response = await call_next(request)
    path = str(request.url.path or "")
    if _request_uses_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if path == "/" or path.startswith("/assets/") or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


CLIENT_DIST = WORKSPACE_ROOT / "web" / "frontend" / "client" / "dist"
PROJECT_IDS_ROOT = WORKSPACE_ROOT / "projects"
FRAMEWORK_RUNS_DIR = WORKSPACE_ROOT / "framework" / "workspace" / "runs"
ENVIRONMENT_RUNS_DIR = WORKSPACE_ROOT / "modules" / "environment" / ".runtime" / "runs"
DEFAULT_FIND_CONFIG_PATH = WORKSPACE_ROOT / "modules" / "finding" / "config" / "find.config.json"
DEFAULT_LOCAL_LLM_CONFIG_PATH = WORKSPACE_ROOT / "modules" / "finding" / "config" / "llm.local.json"
LARGE_JSON_ARTIFACT_LIMIT_BYTES = int(os.environ.get("LARGE_JSON_ARTIFACT_LIMIT_BYTES", "5000000") or 5000000)
LARGE_MARKDOWN_ARTIFACT_LIMIT_BYTES = int(os.environ.get("LARGE_MARKDOWN_ARTIFACT_LIMIT_BYTES", "500000") or 500000)
MARKDOWN_ARTIFACT_PREVIEW_CHARS = int(os.environ.get("MARKDOWN_ARTIFACT_PREVIEW_CHARS", "120000") or 120000)
if CLIENT_DIST.exists():
    assets = CLIENT_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")


def _config_with_env_overrides(config: AppConfig) -> AppConfig:
    """Fill missing LLM fields from the service environment.

    The web UI is the canonical interactive configuration surface. Environment
    variables are only a startup fallback for empty fields; otherwise a saved UI
    config would appear to revert after refresh and future jobs would ignore it.
    """
    if _CURRENT_ACCOUNT.get() is not None:
        return config
    updates: dict[str, Any] = {}
    provider = os.environ.get("LLM_PROVIDER")
    base_url = os.environ.get("LLM_API_BASE")
    model = os.environ.get("LLM_MODEL")
    key_env = os.environ.get("LLM_API_KEY_ENV", "OPENAI_API_KEY")
    api_key = os.environ.get(key_env, "") or os.environ.get("LLM_API_KEY", "")
    temperature = os.environ.get("LLM_TEMPERATURE")
    if provider and not str(config.provider or "").strip():
        updates["provider"] = provider
    if base_url and not str(config.base_url or "").strip():
        updates["base_url"] = base_url
    if model and not str(config.model or "").strip():
        updates["model"] = model
    if api_key and not str(config.api_key or "").strip():
        updates["api_key"] = api_key
    if temperature and config.temperature is None:
        try:
            updates["temperature"] = float(temperature)
        except ValueError:
            pass
    if updates:
        config = config.model_copy(update=updates)
    return config


def _local_llm_config_path() -> Path:
    if _CURRENT_ACCOUNT.get() is None:
        raw = os.environ.get("FINDING_LLM_CONFIG", "").strip()
        return Path(raw).expanduser() if raw else DEFAULT_LOCAL_LLM_CONFIG_PATH
    return _account_runtime_dir() / "llm.local.json"


def _read_local_llm_config() -> dict[str, Any]:
    path = _local_llm_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_local_llm_config(payload: dict[str, Any]) -> None:
    path = _local_llm_config_path()
    write_json(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _missing_config_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _config_with_local_llm_config(config: AppConfig, *, override_defaults: bool = False) -> AppConfig:
    local = _read_local_llm_config()
    if not local:
        return config
    updates: dict[str, Any] = {}
    for key in ("provider", "base_url", "model", "temperature"):
        value = local.get(key)
        if _missing_config_value(value):
            continue
        if override_defaults or _missing_config_value(getattr(config, key, None)):
            updates[key] = value
    local_key = str(local.get("api_key") or "").strip()
    if local_key:
        updates["api_key"] = local_key

    local_roles = local.get("llm_roles") if isinstance(local.get("llm_roles"), dict) else {}
    if local_roles:
        merged_roles = dict(config.llm_roles or {})
        for role, raw_role_config in local_roles.items():
            if not isinstance(raw_role_config, dict):
                continue
            current = merged_roles.get(str(role)) or LLMRoleConfig()
            current_payload = current.model_dump() if hasattr(current, "model_dump") else {}
            role_updates: dict[str, Any] = {}
            for key in ("provider", "base_url", "model", "temperature"):
                value = raw_role_config.get(key)
                if _missing_config_value(value):
                    continue
                if override_defaults or _missing_config_value(current_payload.get(key)):
                    role_updates[key] = value
            role_key = str(raw_role_config.get("api_key") or "").strip()
            if role_key:
                role_updates["api_key"] = role_key
            if role_updates:
                merged_roles[str(role)] = current.model_copy(update=role_updates)
        updates["llm_roles"] = merged_roles
    return config.model_copy(update=updates) if updates else config


def _persist_local_llm_config_from_config(config: AppConfig) -> None:
    current = _read_local_llm_config()
    payload: dict[str, Any] = {
        key: value
        for key, value in current.items()
        if key in {"provider", "base_url", "model", "api_key", "temperature", "llm_roles"}
    }
    for key in ("provider", "base_url", "model", "temperature"):
        value = getattr(config, key, None)
        if not _missing_config_value(value):
            payload[key] = value
    api_key = str(config.api_key or "").strip()
    if api_key:
        payload["api_key"] = api_key

    roles_payload = payload.get("llm_roles") if isinstance(payload.get("llm_roles"), dict) else {}
    roles_payload = dict(roles_payload)
    for role, role_config in (config.llm_roles or {}).items():
        role_data = role_config.model_dump() if hasattr(role_config, "model_dump") else dict(role_config or {})
        target = dict(roles_payload.get(str(role)) or {})
        for key in ("provider", "base_url", "model", "temperature"):
            value = role_data.get(key)
            if not _missing_config_value(value):
                target[key] = value
        role_key = str(role_data.get("api_key") or "").strip()
        if role_key:
            target["api_key"] = role_key
        if target:
            roles_payload[str(role)] = target
    if roles_payload:
        payload["llm_roles"] = roles_payload
    if payload and payload != current:
        _write_local_llm_config(payload)


def _persist_local_llm_config_from_find_request(config: AppConfig, request_config: AppConfig | None) -> None:
    request_key = str(getattr(request_config, "api_key", "") or "").strip() if request_config is not None else ""
    provider = str(config.provider or "").strip().lower()
    if provider == "mock" and not request_key:
        return
    _persist_local_llm_config_from_config(config)


def _strip_llm_secrets_from_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data["api_key"] = ""
    roles = data.get("llm_roles") if isinstance(data.get("llm_roles"), dict) else {}
    for role_cfg in roles.values():
        if isinstance(role_cfg, dict):
            role_cfg["api_key"] = ""
    if roles:
        data["llm_roles"] = roles
    return data


FIND_CONFIG_INPUT_FIELDS = {"research_topic", "research_interest", "researcher_profile", "arxiv_queries"}
FIND_CONFIG_LLM_FIELDS = {"provider", "base_url", "api_key", "model", "temperature", "llm_roles"}
FIND_CONFIG_EXCLUDED_FIELDS = FIND_CONFIG_INPUT_FIELDS | FIND_CONFIG_LLM_FIELDS | {"default_find_selection", "email"}


def _finding_config_path() -> Path:
    if _CURRENT_ACCOUNT.get() is None:
        raw = os.environ.get("FINDING_CONFIG", "").strip()
        if raw:
            return Path(raw).expanduser()
        llm_raw = os.environ.get("FINDING_LLM_CONFIG", "").strip()
        if llm_raw:
            return Path(llm_raw).expanduser().parent / "find.config.json"
        return DEFAULT_FIND_CONFIG_PATH
    return _account_runtime_dir() / "find.config.json"


def _account_config_path() -> Path:
    return _account_runtime_dir() / "config.json" if _CURRENT_ACCOUNT.get() is not None else CONFIG_PATH


def _project_finding_config_path(project_root: Path | None = None, project_path: Path | None = None) -> Path | None:
    if project_root is not None:
        return project_root / "config" / "finding.json"
    return project_path.parent / "config" / "finding.json" if project_path else None


def _read_finding_config_payload(path: Path | None = None) -> dict[str, Any]:
    payload = read_json(path or _finding_config_path(), {})
    return payload if isinstance(payload, dict) else {}


def _split_finding_config_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}, {}
    if any(key in payload for key in ("config", "selection")):
        config_payload = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        selection_payload = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
        config_payload = dict(config_payload)
    else:
        config_payload = dict(payload)
        selection_payload = {}
    embedded_selection = config_payload.pop("default_find_selection", None)
    if isinstance(embedded_selection, dict) and not selection_payload:
        selection_payload = embedded_selection
    return config_payload, dict(selection_payload)


def _write_finding_config_payload(path: Path, config_payload: dict[str, Any], selection: dict[str, Any]) -> None:
    existing = _read_finding_config_payload(path)
    payload = dict(existing)
    payload["schema_version"] = int(payload.get("schema_version") or 1)
    payload["config"] = dict(config_payload)
    payload["selection"] = normalize_source_selection(selection)
    write_json(path, payload)


def _app_find_config_payload(config: AppConfig) -> dict[str, Any]:
    payload = config.model_dump()
    return {
        key: value
        for key, value in payload.items()
        if key not in FIND_CONFIG_EXCLUDED_FIELDS
    }


def _ensure_project_finding_config(project_root: Path | None = None, project_path: Path | None = None) -> Path | None:
    target = _project_finding_config_path(project_root=project_root, project_path=project_path)
    if target is None:
        return None
    if target.exists():
        return target
    source_payload = _read_finding_config_payload(_finding_config_path())
    if not source_payload:
        source_payload = {"schema_version": 1, "config": {}, "selection": normalize_source_selection({})}
    write_json(target, source_payload)
    return target


def _active_finding_config_path(*, create_project_copy: bool = False) -> Path:
    return _finding_config_path()


def _persist_finding_config_from_config(config: AppConfig, selection: dict[str, Any], *, project_root: Path | None = None) -> Path:
    project_target = _ensure_project_finding_config(project_root=project_root) if project_root is not None else _active_finding_config_path(create_project_copy=True)
    target = project_target or _finding_config_path()
    _write_finding_config_payload(target, _app_find_config_payload(config), selection)
    return target


def load_config() -> AppConfig:
    config_path = _active_finding_config_path(create_project_copy=True)
    finding_payload = _read_finding_config_payload(config_path)
    config_payload, selection_payload = _split_finding_config_payload(finding_payload)
    legacy_payload = read_json(_account_config_path(), {})
    if isinstance(legacy_payload, dict):
        config_payload = {**legacy_payload, **config_payload}
    config = AppConfig(**config_payload) if config_payload else AppConfig()
    canonical = normalize_source_selection(selection_payload or {})
    if config.default_find_selection != canonical:
        config = config.model_copy(update={"default_find_selection": canonical})
    config = _config_with_project_research_preferences(config)
    config = _config_with_local_llm_config(config, override_defaults=True)
    return _config_with_env_overrides(config)


def _frontend_version() -> dict[str, Any]:
    index = CLIENT_DIST / "index.html"
    assets_dir = CLIENT_DIST / "assets"
    files: list[Path] = [index] if index.exists() else []
    if assets_dir.exists():
        files.extend(sorted(path for path in assets_dir.iterdir() if path.is_file() and path.suffix in {".js", ".css"}))
    digest = hashlib.sha256()
    newest = 0.0
    names: list[str] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        names.append(path.name)
        digest.update(path.name.encode("utf-8", errors="ignore"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_size).encode("ascii"))
    version = digest.hexdigest()[:16] if files else "dev"
    built_at = datetime.fromtimestamp(newest, UTC).isoformat() if newest else ""
    return {"version": version, "built_at": built_at, "files": names}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _source_status_markdown_from_find_results(find_results: Any, fallback: str = "") -> str:
    if not isinstance(find_results, dict):
        return fallback
    rows: list[dict[str, Any]] = []
    venue_health = _as_list(find_results.get("venue_health_report"))
    for row in venue_health:
        if not isinstance(row, dict):
            continue
        venue = row.get("venue") or row.get("venue_id") or "venue"
        effective_years = ",".join(str(year) for year in _as_list(row.get("effective_years")))
        message_parts = []
        if row.get("adapter"):
            message_parts.append("adapter=" + str(row.get("adapter")))
        if effective_years:
            message_parts.append("years=" + effective_years)
        if row.get("corpus_count") is not None:
            message_parts.append("corpus=" + str(row.get("corpus_count")))
        if row.get("candidate_count") is not None:
            message_parts.append("screen_input=" + str(row.get("candidate_count")))
        if row.get("sample_count") is not None:
            message_parts.append("fetched=" + str(row.get("sample_count")))
        if row.get("year_fallback_reason"):
            message_parts.append(str(row.get("year_fallback_reason")))
        if row.get("error"):
            message_parts.append(str(row.get("error")))
        rows.append({
            "source": str(venue),
            "ok": bool(row.get("ok")),
            "limited": False,
            "count": row.get("candidate_count") or row.get("sample_count") or row.get("corpus_count") or 0,
            "message": "; ".join(message_parts) or ("ok" if row.get("ok") else "No papers fetched."),
        })
    if not rows:
        rows = [
            row for row in _as_list(find_results.get("source_status"))
            if isinstance(row, dict)
            and str(row.get("source") or "").strip().lower() not in {"venues", "venue summary", "venue_summary"}
            and str(row.get("source_kind") or "").strip().lower() != "venue_summary"
        ]
    if not rows:
        return fallback
    lines = ["# Source Status", ""]
    for row in rows:
        state = "limited" if row.get("limited") else ("ok" if row.get("ok") else "failed")
        source = row.get("source") or row.get("venue") or "source"
        message = row.get("message") or row.get("error") or ""
        lines.extend([
            "## " + str(source),
            "",
            "- **Status**: " + state,
            "- **Count**: " + str(row.get("count", 0)),
            "- **Message**: " + str(message),
            "",
        ])
    summary = next((row for row in _as_list(find_results.get("source_status")) if isinstance(row, dict) and str(row.get("source") or "").strip().lower() in {"venues", "venue summary", "venue_summary"}), None)
    if isinstance(summary, dict):
        lines.extend([
            "## Venue Summary",
            "",
            "- **Retrieval pool**: " + str(summary.get("count", 0)),
            "- **Raw title index**: " + str(summary.get("raw_title_index_count", 0)),
            "- **Detail fetched**: " + str(summary.get("detail_fetched_count", 0)),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _sync_project_llm_from_config(config: AppConfig) -> None:
    return


def _current_project_research_preferences() -> dict[str, str]:
    return {}


def _config_with_project_research_preferences(config: AppConfig, provided_fields: set[str] | None = None) -> AppConfig:
    provided = provided_fields or set()
    project_prefs = _current_project_research_preferences()
    updates: dict[str, Any] = {}
    for key in ("research_interest", "researcher_profile"):
        if key in provided:
            continue
        current = str(getattr(config, key, "") or "").strip()
        project_value = str(project_prefs.get(key) or "").strip()
        if not current and project_value:
            updates[key] = project_value
    effective_interest = str(updates.get("research_interest") or getattr(config, "research_interest", "") or "").strip()
    effective_profile = str(updates.get("researcher_profile") or getattr(config, "researcher_profile", "") or "").strip()
    project_topic = str(project_prefs.get("topic") or "").strip()
    if not effective_interest and not effective_profile and project_topic:
        updates["research_interest"] = project_topic
    return config.model_copy(update=updates) if updates else config


def _sync_project_research_preferences_from_config(config: AppConfig, target_project_path: Path | None = None) -> None:
    project_path = target_project_path
    if project_path is None:
        return
    project_config = read_json(project_path, {})
    if not isinstance(project_config, dict):
        return
    updates: dict[str, str] = {}
    topic_value = str(getattr(config, "research_topic", "") or "").strip()
    if topic_value:
        updates["topic"] = topic_value
    for key in ("research_interest", "researcher_profile"):
        value = str(getattr(config, key, "") or "").strip()
        if value:
            updates[key] = value
    if not updates:
        return
    changed = False
    for key, value in updates.items():
        if project_config.get(key) != value:
            project_config[key] = value
            changed = True
    if changed:
        write_json(project_path, project_config)


def _sync_project_finding_config_from_request(config: AppConfig, target_project_path: Path | None = None) -> None:
    project_path = target_project_path
    if project_path is None:
        return
    _persist_finding_config_from_config(config, config.default_find_selection, project_root=project_path.parent)
    project_config = read_json(project_path, {})
    if isinstance(project_config, dict) and "finding" in project_config:
        project_config.pop("finding", None)
        write_json(project_path, project_config)


def save_config(config: AppConfig) -> AppConfig:
    project_path = None
    canonical = normalize_source_selection(config.default_find_selection)
    config = config.model_copy(update={"default_find_selection": canonical})
    _persist_local_llm_config_from_config(config)
    _persist_finding_config_from_config(config, canonical, project_root=project_path.parent if project_path is not None else None)
    payload = _strip_llm_secrets_from_config_payload(config.model_dump())
    if project_path is not None:
        # Source selection is project state. LLM secrets live in the local
        # modules/finding/config/llm.local.json file, not project/runtime state.
        payload.pop("default_find_selection", None)
    if read_json(_account_config_path(), {}) != payload:
        write_json(_account_config_path(), payload)
    _sync_project_research_preferences_from_config(config)
    _sync_project_finding_config_from_request(config)
    return config


def _request_config_with_persisted_secrets(request_config: AppConfig | None) -> AppConfig:
    base_config = load_config()
    if request_config is None:
        return _config_with_project_research_preferences(base_config, set())

    # API callers often send a partial run override. Pydantic fills omitted
    # fields with model defaults, so merge omitted fields back from the saved UI
    # config before preserving secrets; otherwise a partial Find request can
    # silently reset provider/base_url/model to defaults and use the saved key
    # against the wrong endpoint.
    provided_fields = set(getattr(request_config, "model_fields_set", set()) or set())
    if provided_fields:
        base_payload = base_config.model_dump()
        merged_payload = request_config.model_dump()
        for key, value in base_payload.items():
            if key not in provided_fields:
                merged_payload[key] = value
        merged = AppConfig(**merged_payload)
    else:
        merged = request_config

    updates: dict[str, Any] = {}
    if not str(merged.api_key or "").strip() and str(base_config.api_key or "").strip():
        updates["api_key"] = base_config.api_key
    if (
        not str(getattr(merged.email, "smtp_password", "") or "").strip()
        and str(getattr(base_config.email, "smtp_password", "") or "").strip()
    ):
        updates["email"] = merged.email.model_copy(update={"smtp_password": base_config.email.smtp_password})
    merged_roles = dict(merged.llm_roles or {})
    for role, base_role in (base_config.llm_roles or {}).items():
        current = merged_roles.get(role)
        if current is None:
            merged_roles[role] = base_role
            continue
        role_updates: dict[str, Any] = {}
        if not str(current.api_key or "").strip() and str(base_role.api_key or "").strip():
            role_updates["api_key"] = base_role.api_key
        if role_updates:
            merged_roles[role] = current.model_copy(update=role_updates)
    if merged_roles != (merged.llm_roles or {}):
        updates["llm_roles"] = merged_roles
    if updates:
        merged = merged.model_copy(update=updates)
    merged = _config_with_project_research_preferences(merged, provided_fields)
    return _config_with_env_overrides(merged)


def _public_config_response(config: AppConfig) -> dict[str, Any]:
    """Return UI-editable config without sending saved secrets to the browser."""
    data = config.model_dump()
    config_file = _active_finding_config_path(create_project_copy=False)
    try:
        config_stat = config_file.stat() if config_file.exists() else None
    except OSError:
        config_stat = None
    api_key_value = str(data.get("api_key") or "")
    data["api_key_saved"] = bool(api_key_value.strip())
    data["api_key_suffix"] = api_key_value[-4:] if api_key_value else ""
    data["config_saved_at"] = datetime.fromtimestamp(config_stat.st_mtime, UTC).isoformat() if config_stat else ""
    data["config_path"] = str(config_file)
    data["project_llm_synced"] = _local_llm_config_path().exists()
    data["api_key"] = ""
    roles = data.get("llm_roles") if isinstance(data.get("llm_roles"), dict) else {}
    for role_cfg in roles.values():
        if isinstance(role_cfg, dict):
            role_key = str(role_cfg.get("api_key") or "")
            role_cfg["api_key_saved"] = bool(role_key.strip())
            role_cfg["api_key_suffix"] = role_key[-4:] if role_key else ""
            role_cfg["api_key"] = ""
    email = data.get("email") if isinstance(data.get("email"), dict) else {}
    if isinstance(email, dict):
        email["smtp_password_saved"] = bool(str(email.get("smtp_password") or "").strip())
        email["smtp_password"] = ""
    return data


def _find_llm_configuration_error(config: AppConfig) -> dict[str, Any] | None:
    client = LLMClient(config, "find")
    if str(client.provider or "").strip().lower() == "mock":
        return None
    missing = [
        name
        for name, value in (
            ("base_url", client.base_url),
            ("model", client.model),
            ("api_key", client.api_key),
        )
        if not str(value or "").strip()
    ]
    if not missing:
        return None
    return {
        "error": "user_llm_config_required",
        "code": "user_llm_config_required",
        "missing": missing,
        "message": "请先保存当前账户自己的 Find LLM 配置；服务端不会使用其他账户或系统环境中的 LLM API key。",
    }


def _strip_legacy_recommendation_card_blocks(text: str) -> str:
    lines = str(text or "").splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if re.match(r"^#\d+\b", stripped):
            window = "\n".join(lines[index:index + 8])
            if (
                re.search(r"(?:Fit|Score)\s*=", window, flags=re.I)
                or re.search(r"^\s*(?:URL|PDF)\s*$", window, flags=re.I | re.M)
                or re.search(r"全文状态|方法类型|core method reference|core reading", window, flags=re.I)
                or re.search(r"/\s*(?:推荐|recommended)\s*/", window, flags=re.I)
            ):
                index += 1
                while index < len(lines) and lines[index].strip():
                    if re.match(r"^#\d+\b", lines[index].strip()):
                        break
                    index += 1
                if index < len(lines) and not lines[index].strip():
                    index += 1
                continue
        kept.append(lines[index])
        index += 1
    cleaned = "\n".join(kept)
    if text.endswith("\n") and cleaned:
        cleaned += "\n"
    return cleaned


def _strip_legacy_artifact_pointer_lines(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue
        lower = stripped.lower()
        points_to_find_artifact = "find.md" in lower
        pointer_verb = bool(re.search(r"(?:见|查看|打开|see|open|refer)", stripped, flags=re.I))
        duplicated_article_fields = bool(re.search(r"(?:摘要|推荐理由|完整|abstract|recommendation)", stripped, flags=re.I))
        legacy_metadata_line = bool(
            re.match(r"^(?:[-*]\s*)?(?:\*\*)?(?:id|url|pdf|fit(?:\s*分数)?|score|final\s*score|最终分数)(?:\*\*)?\s*[:：]\s*.*$", stripped, flags=re.I)
            or re.match(r"^(?:url|pdf)$", stripped, flags=re.I)
        )
        legacy_score_line = ("/" in stripped or "|" in stripped) and bool(re.search(r"(?:Fit|Score)\s*=", stripped, flags=re.I))
        if (points_to_find_artifact and pointer_verb and duplicated_article_fields) or legacy_metadata_line or legacy_score_line:
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\s*/\s*(?:Fit|Score)\s*=\s*[^\n/]+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[（(]\s*(?:Fit|Score)\s*=\s*[^）)]+[）)]", "", cleaned, flags=re.I)
    return cleaned


def _normalize_public_workspace_paths(text: str) -> str:
    """Rewrite stale sibling workspace paths before API text reaches the browser."""
    current_root = str(WORKSPACE_ROOT)
    parent = str(WORKSPACE_ROOT.parent)
    if parent and current_root.startswith(parent):
        sibling_root = re.escape(parent) + r"/[A-Z][A-Z0-9_]*(?=/(?:projects|modules)(?:/|$))"
        text = re.sub(sibling_root, current_root, text)
    legacy_find_dir = "ar" + "_finding"
    text = text.replace(f"/planning/{legacy_find_dir}/", "/planning/finding/")
    text = re.sub(r"\b[A-Z]{2}\s+(研究主题|运行环境|任务栏|主流程|写作)", r"\1", text)
    text = re.sub(r"当前\s+[A-Z]{2}\s+项目", "当前项目", text)
    text = re.sub(r"创建\s+[A-Z]{2}\s+项目", "创建项目", text)
    text = re.sub(r"当前\s+[A-Z]{2}\s+实时状态", "当前项目实时状态", text)
    return text


def _public_text(value: str) -> str:
    text = _normalize_public_workspace_paths(_strip_legacy_artifact_pointer_lines(_strip_legacy_recommendation_card_blocks(re.sub(r"\[TASTE\]\s*", "", value))))
    replacements = [
        ("no improvement claim is allowed", "不得据此声称改进成立"),
        ("No improvement claim is allowed", "不得据此声称改进成立"),
        ("native frontend skipped", "finding frontend skipped"),
        ("native frontend", "finding frontend"),
        ("当前阶段", "当前阶段"),
        ("阶段", "阶段"),
        ("全局 任务栏", "全局 任务栏"),
        ("底部 任务栏", "底部 任务栏"),
        ("taskbar", "taskbar"),
        ("PaperOrchestra", "writing"),
        ("paper/orchestra", "paper/writing"),
        ("paper-orchestra", "writing"),
        ("paper_orchestra", "writing"),
        ("deterministic base-switch gate", "deterministic base-switch gate"),
        ("deterministic base switch gate", "deterministic base-switch gate"),
        ("base-switch gate", "base-switch gate"),
        ("base_switch_gate", "base-switch gate"),
        ("base_switch_execution", "base-switch execution receipt"),
        ("selected_base_viability_gate", "experiment_evidence_audit"),
        ("selected_base_viability", "experiment_evidence_audit"),
        ("environment_claude_code", "environment review"),
        ("claude_code_current_find_takeover", "current Find reading output"),
        ("waiting_for_environment_review", "等待环境审查"),
        ("waiting_for_environment_base_selection", "等待环境选择基底"),
        ("wait_for_environment_base_selection", "等待环境选择基底"),
        ("waiting_for_repo_selection", "等待仓库选择"),
        ("waiting for repo selection", "等待仓库选择"),
        ("论文/claim", "论文或结论提升"),
        ("improvement claim", "提升结论"),
        ("paper claim", "论文结论"),
        ("claim promotion", "结论提升"),
        ("full research cycle", "完整科研循环"),
        ("writing", "writing"),
        ("taste_reviewer", "writing_reviewer"),
        ("planning/finding", "planning/finding"),
        ("state/finding", "state/finding"),
        ("finding_frontend", "finding_frontend"),
        ("finding", "finding"),
        ("run_frontend", "run_finding"),
        ("run-finding", "run-finding"),
            ]
    for source, target in replacements:
        text = text.replace(source, target)
    text = re.sub(r"当前还?缺少\s+[^；。\n]+?\s+下可审计", "当前主线还缺少可审计", text)
    text = re.sub(r"当前缺少\s+[^；。\n]+?\s+下可审计", "当前主线缺少可审计", text)
    text = re.sub(r"保持\s+[^；。\n]+?\s+作为当前基底", "保持当前基底不变", text)
    text = re.sub(r"environment-stage base selection", "environment review", text, flags=re.I)
    text = re.sub(r"environment-stage base selected", "environment selected", text, flags=re.I)
    return text.strip()


def _strip_public_taste_marker(value: Any) -> Any:
    """Sanitize internal source-module names from public API/websocket payloads."""
    if isinstance(value, str):
        return _public_text(value)
    if isinstance(value, list):
        return [_strip_public_taste_marker(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_public_taste_marker(item) for key, item in value.items()}
    return value


def _public_job_summary_text(value: Any) -> str:
    """Clean job summary/status fields while leaving bounded log rows detailed."""
    text = _redact_public_log_text(_public_text(str(value or "")))
    lowered_initial = text.lower().strip()
    if lowered_initial in {"find complete", "find completed"}:
        return "Find 已完成。"
    if lowered_initial in {"read complete", "read completed"}:
        return "精读阶段已完成。"
    if lowered_initial in {"task cancelled by user", "task cancelled by user.", "research action cancelled by user", "research action cancelled by user."}:
        return "任务已取消。"
    failed_match = re.fullmatch(r"(?:research action|subprocess|task)?\s*failed with exit code\s+(-?\d+)\.?", lowered_initial)
    if failed_match:
        return f"任务执行失败：后端流程返回错误码 {failed_match.group(1)}；详细原因见保留日志和对应模块产物。"
    running_match = re.fullmatch(r"running\s+(environment|experiment|paper|find|read|idea|plan)\s+for\s+(.+)", lowered_initial)
    if running_match:
        return _job_status_message(running_match.group(1), "running")
    complete_match = re.fullmatch(r"(environment|experiment|paper|find|read|idea|plan) complete\.?", lowered_initial)
    if complete_match:
        return _job_status_message(complete_match.group(1), "complete")
    if re.search(r"\b(?:read|idea|plan)\b.*stopped at an evidence gate", lowered_initial):
        return "当前 Find 的精读/想法/计划仍未完成；请查看本阶段状态并继续运行对应步骤。"
    if "stopped at an evidence gate" in lowered_initial:
        return "当前任务停在后续证据门控。"
    text = re.sub(
        r"missing bib entries for cited keys=[^；。\n]+",
        "引用/参考文献仍需修复；具体修复清单已交由 Writing 主控 Claude 处理",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"latex_undefined_citations[^；。\n]*",
        "引用/参考文献仍需修复；具体修复清单已交由 Writing 主控 Claude 处理",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?:Claude Code|项目代理)\s*自审未通过[^；。\n]*",
        "论文自审未通过，具体修复项已交由 Writing 主控 Claude 处理",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"项目代理需独立读\s*PDF/TeX/BibTeX/log/venue contract\s*后修复并写\s*receipt",
        "具体修复项已交由 Writing 主控 Claude 处理",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"self_review_hash_mismatch[^；。\n]*",
        "论文自审未通过，具体修复项已交由 Writing 主控 Claude 处理",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"下一步由\s*project agent\s*继续真实实验迭代",
        "具体下一步由对应模块主控 Claude 读取证据后决定",
        text,
        flags=re.I,
    )
    lowered = text.lower()
    if (
        ("当前 find" in lowered or "current find" in lowered)
        and ("精读" in text or "read" in lowered)
        and ("idea" in lowered or "plan" in lowered)
        and ("claude" in lowered or "接管" in text or "takeover" in lowered)
    ):
        return "正在生成当前 Find 的全文精读、Idea 和 Plan。"
    return text.strip()


def _public_job_log_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_public_log_text(_public_text(value)).strip()
    if isinstance(value, list):
        return [_public_job_log_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_job_log_payload(item) for key, item in value.items()}
    return value


def _public_job_api_payload(value: Any, key: str = "") -> Any:
    if key == "logs":
        return _public_job_log_payload(value)
    if key == "status" and isinstance(value, str) and value.strip().lower() == "interrupted":
        return "stale"
    if isinstance(value, str):
        return _public_job_summary_text(value)
    if isinstance(value, list):
        return [_public_job_api_payload(item) for item in value]
    if isinstance(value, dict):
        return {item_key: _public_job_api_payload(item_value, str(item_key)) for item_key, item_value in value.items()}
    return value


def _public_job_artifact_labels(artifacts: Any) -> list[str]:
    rows = artifacts if isinstance(artifacts, list) else list(artifacts.values()) if isinstance(artifacts, dict) else []
    labels: list[str] = []
    for item in rows:
        name = Path(str(item or '')).name
        if not name:
            continue
        lower = name.lower()
        if lower in {'full_research_cycle.json', 'supervision_tick.json'}:
            label = '科研循环状态'
        elif 'reference_reproduction' in lower:
            label = '参考复现审计'
        elif 'experiment' in lower and ('audit' in lower or 'record' in lower or lower.endswith('.csv')):
            label = '实验记录/审计'
        elif 'scientific_progress' in lower:
            label = '科学进展审计'
        elif 'submission_readiness' in lower:
            label = '投稿准备度审计'
        elif 'blocker_action_plan' in lower:
            label = '下一步行动计划'
        elif 'active_repo' in lower or 'repo_selection' in lower:
            label = '当前仓库证据'
        elif 'base_switch' in lower or 'route_authorization' in lower or 'viability' in lower:
            label = '实验证据审查'
        else:
            label = _public_text(name)
        if label and label not in labels:
            labels.append(label)
    return labels[:12]


def _paper_preview_artifact_available(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("blocked_preview_available") or row.get("raw_pdf_path") or row.get("pdf_path") or row.get("latest_generated_pdf_path"):
        return True
    nested = row.get("paper_stage") if isinstance(row.get("paper_stage"), dict) else {}
    return bool(
        nested.get("blocked_preview_available")
        or nested.get("raw_pdf_path")
        or nested.get("pdf_path")
        or nested.get("latest_generated_pdf_path")
    )


_PAPER_PREVIEW_GATE_BLOCKED_STATUSES = {"blocked_preview_gate", "normality_blocked", "preview_pdf_blocked"}


def _paper_preview_gate_blocked_status(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    values = [row.get("paper_stage_status"), row.get("status"), row.get("paper_status")]
    nested = row.get("paper_stage") if isinstance(row.get("paper_stage"), dict) else {}
    values.extend([nested.get("paper_stage_status"), nested.get("status"), nested.get("paper_status")])
    return any(str(value or "").strip().lower() in _PAPER_PREVIEW_GATE_BLOCKED_STATUSES for value in values)


def _paper_preview_project_key(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    nested = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}
    return str(result.get("project") or nested.get("project") or "")


def _job_api_dedupe_key(item: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(item, dict):
        return ("invalid", "")
    job_id = str(item.get("job_id") or "").strip()
    if job_id:
        return ("job_id", job_id)
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    project = str(result.get("project") or "").strip()
    run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
    log_path = str(result.get("log_path") or result.get("stdout_path") or "").strip()
    pid = str(result.get("pid") or item.get("pid") or "").strip()
    return ("shape", "|".join([str(item.get("stage") or ""), str(item.get("status") or ""), project, run_id, pid, log_path]))


def _dedupe_job_items_for_api(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _job_api_dedupe_key(item)
        previous = by_key.get(key)
        if previous is None or str(item.get("created_at") or "") >= str(previous.get("created_at") or ""):
            by_key[key] = item
    return sorted(by_key.values(), key=lambda row: str(row.get("created_at") or ""), reverse=True)


def _dedupe_persisted_paper_preview_jobs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep persisted task history useful: only the newest paper preview per project.

    Full-cycle, experiment, Find and error/cancel records remain intact. Paper
    preview rows are user-triggered regenerated artifacts, so keeping many stale
    copies makes the taskbar look broken instead of informative.
    """
    latest_created: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(item.get("status") or "").lower()
        if not _is_paper_job(stage, item.get("job_id", ""), result, item.get("logs")):
            continue
        if status not in {"preview_available", "needs_writing", "completed", "done", "blocked"}:
            continue
        key = _paper_preview_project_key(item) or "__global_paper_preview__"
        created = str(item.get("created_at") or "")
        if key not in latest_created or created > latest_created[key]:
            latest_created[key] = created
    if not latest_created:
        return items
    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(item.get("status") or "").lower()
        if not _is_paper_job(stage, item.get("job_id", ""), result, item.get("logs")) or status not in {"preview_available", "needs_writing", "completed", "done", "blocked"}:
            kept.append(item)
            continue
        key = _paper_preview_project_key(item) or "__global_paper_preview__"
        if str(item.get("created_at") or "") == latest_created.get(key):
            kept.append(item)
    return kept


def _public_paper_status(raw_status: Any, row: dict[str, Any]) -> str:
    status = str(raw_status or "").strip() or "queued"
    if _paper_content_policy_blocked(row) and status not in {"running", "queued", "cancelling", "error", "cancelled"}:
        return "blocked"
    if _paper_preview_artifact_available(row) and status not in {"running", "queued", "cancelling", "error", "cancelled"}:
        return "preview_available"
    if _paper_preview_gate_blocked_status(row) and status not in {"running", "queued", "cancelling", "error", "cancelled"}:
        return "preview_pdf_blocked"
    return status



def _is_full_cycle_job(stage: Any = "", job_id: Any = "", result: Any = None, logs: Any = None) -> bool:
    stage_text = str(stage or "").lower().replace("_", "-")
    job_id_text = str(job_id or "").lower().replace("_", "-")
    if job_id_text.startswith("current-find-worker") or "current-find" in stage_text or "current-find" in job_id_text:
        return False
    if isinstance(result, dict):
        kind_text = str(result.get("kind") or result.get("raw_stage") or "").lower().replace("_", "-")
        cmd_text = str(result.get("cmd") or result.get("command") or "").lower().replace("_", "-")
        current_find_framework_cmd = (
            "current-find-research-plan" in cmd_text
            and (
                "run-module.py" in cmd_text
                or ("framework/scripts/main.py" in cmd_text and " module " in f" {cmd_text} ")
            )
        )
        if kind_text.startswith("current-find") or current_find_framework_cmd:
            return False
    hay_parts = [str(stage or ""), str(job_id or "")]
    if isinstance(result, dict):
        for key in ["cmd", "command", "raw_stage", "kind", "log_path"]:
            hay_parts.append(str(result.get(key) or ""))
    if isinstance(logs, list):
        hay_parts.extend(str(line or "") for line in logs[:30])
    hay = "\n".join(hay_parts).lower().replace("_", "-")
    return any(marker in hay for marker in [
        "full-cycle",
        "run-full-research-cycle.py",
        "detached full-cycle",
        "full-cycle worker",
        "full-research-cycle",
    ])


def _current_find_worker_phase_and_kind(cmd: str) -> tuple[str, str, int]:
    lowered = str(cmd or "").lower().replace("_", "-")
    framework_module = "run-module.py" in lowered or (
        "framework/scripts/main.py" in lowered and " module " in f" {lowered} "
    )
    if framework_module and "current-find-research-plan" in lowered:
        return "read", "current_find_read_framework", 2
    return "", "", 99


def _is_current_find_worker_cmd(cmd: Any) -> bool:
    return bool(_current_find_worker_phase_and_kind(str(cmd or ""))[1])


def _process_has_current_find_ancestor(row: Any, rows: list[dict[str, Any]] | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    source_rows = rows or _all_process_rows()
    by_pid = {str(item.get("pid") or ""): item for item in source_rows if isinstance(item, dict)}
    current = str(row.get("pid") or "").strip()
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        item = by_pid.get(current)
        if not item:
            return False
        if _is_current_find_worker_cmd(item.get("cmd")):
            return True
        current = str(item.get("ppid") or "").strip()
    return False


def _normalize_module_controller_panel_stage(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    if raw in {"paper", "paperwrite", "paper-write", "paper-writing"} or raw.startswith("paper") or "writing" in raw:
        return "paper"
    if raw in {"environment", "env"} or raw.startswith("environment") or "repo-env" in raw:
        return "environment"
    if raw == "experiment" or raw.startswith("experiment") or "trajectory" in raw or "autonomous" in raw:
        return "experiment"
    return ""


def _panel_stage_from_module_controller_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ["panel_stage", "requested_stage", "stage"]:
        normalized = _normalize_module_controller_panel_stage(result.get(key))
        if normalized:
            return normalized
    return ""


def _initial_module_controller_job_result(payload: Any, stage: Any) -> dict[str, Any]:
    row = payload if isinstance(payload, dict) else {}
    action = str(row.get("action") or stage or "").strip()
    requested_stage = str(row.get("stage") or "").strip()
    panel_stage = _normalize_module_controller_panel_stage(requested_stage)
    if not panel_stage:
        panel_stage = _normalize_module_controller_panel_stage(action)
    if action not in {"claude-message", "agent-guidance", "environment-chat", "environment_chat", "experimenting-chat", "experiment-chat", "writing-chat", "paper-chat"} and not panel_stage:
        return {}
    controller_ids = {
        "environment": "environment_controller",
        "experiment": "experimenting_controller",
        "paper": "writing_controller",
    }
    result = {
        "project": str(row.get("project") or "").strip(),
        "action": action,
        "agent_id": controller_ids.get(panel_stage, ""),
        "requested_stage": requested_stage,
        "panel_stage": panel_stage,
        "status": "running",
    }
    if panel_stage and action in {"environment-chat", "environment_chat", "experimenting-chat", "experiment-chat", "writing-chat", "paper-chat", "claude-message"}:
        result["instruction"] = str(row.get("message") or row.get("prompt") or "").strip()
        result["interrupt_current"] = bool(row.get("interrupt_current"))
    return result


def _is_module_controller_panel_job(stage: Any = "", job_id: Any = "", result: Any = None) -> bool:
    parts = [str(stage or ""), str(job_id or "")]
    if isinstance(result, dict):
        parts.extend(str(result.get(key) or "") for key in ["action", "kind", "raw_stage"])
    haystack = "\n".join(parts).lower().replace("_", "-")
    action = str(result.get("action") or "").strip().lower().replace("_", "-") if isinstance(result, dict) else ""
    return action in {"claude-message", "agent-guidance", "environment-chat", "experimenting-chat", "experiment-chat", "writing-chat", "paper-chat"} or any(marker in haystack for marker in [
        "claude-message",
        "environment-chat",
        "experimenting-chat",
        "experiment-chat",
        "project-agent-guidance",
        "agent-guidance",
        "writing-chat",
        "paper-chat",
    ])


def _is_paper_job(stage: Any = "", job_id: Any = "", result: Any = None, logs: Any = None) -> bool:
    if _is_module_controller_panel_job(stage, job_id, result):
        return _panel_stage_from_module_controller_result(result) == "paper"
    stage_norm = str(stage or "").strip().lower().replace("_", "-")
    job_norm = str(job_id or "").strip().lower().replace("_", "-")
    action_norm = str((result or {}).get("action") or "").strip().lower().replace("_", "-") if isinstance(result, dict) else ""
    if stage_norm in {"paper", "paperwrite", "paper-write", "paper-writing", "writing-chat", "paper-chat"} or job_norm.startswith("paper-") or action_norm in {"paper", "writing-chat", "paper-chat"}:
        return True
    hay_parts = [str(stage or ""), str(job_id or "")]
    if isinstance(result, dict):
        for key in ["cmd", "command", "raw_stage", "paper_summary", "paper_stage_status", "paper_orchestra_bridge_status"]:
            hay_parts.append(str(result.get(key) or ""))
        if isinstance(result.get("paper_stage"), dict):
            hay_parts.append("paper_stage")
    if isinstance(logs, list):
        hay_parts.extend(str(line or "") for line in logs[:20])
    hay = "\n".join(hay_parts).lower().replace("_", "-")
    return any(marker in hay for marker in [
        "run-paper-pipeline.py",
        "paper-pipeline",
        "build-conference-preview-paper.py",
        "repair-paper-preview-loop.py",
        "compile-paper-pdf.py",
        "paper-normality",
        "conference-preview",
    ])


def _known_project_ids() -> list[str]:
    now = time.monotonic()
    cached = _KNOWN_PROJECT_IDS_CACHE.get("ids")
    if isinstance(cached, list) and now < float(_KNOWN_PROJECT_IDS_CACHE.get("expires_at") or 0.0):
        return [str(item) for item in cached if str(item)]
    ids: list[str] = []
    try:
        if PROJECT_IDS_ROOT.exists():
            ids = sorted(root.name for root in PROJECT_IDS_ROOT.iterdir() if root.is_dir())
    except Exception:
        ids = []
    _KNOWN_PROJECT_IDS_CACHE["ids"] = ids
    _KNOWN_PROJECT_IDS_CACHE["expires_at"] = time.monotonic() + KNOWN_PROJECT_IDS_TTL_SEC
    return ids


def _project_from_job_id_fast(job_id: Any) -> str:
    text = str(job_id or "").strip()
    if not text:
        return ""
    patterns = [
        r"^(?:reference-reproduction|full_cycle|full-cycle|safe-unblock|safe_unblock)_([A-Za-z0-9_.-]+)$",
        r"^(?:experiment|project)-worker_([A-Za-z0-9_.-]+)_\d+$",
        r"^current-find-(?:read|idea|plan)_([A-Za-z0-9_.-]+)_find_",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    known = _known_project_ids()
    for project_id in known:
        if text == project_id or text.startswith(f"{project_id}_") or text.endswith(f"_{project_id}") or f"_{project_id}_" in text or f"-{project_id}-" in text:
            return project_id
    return ""


def _web_job_project_map() -> dict[str, str]:
    now = time.monotonic()
    cached = _WEB_JOB_PROJECT_MAP_CACHE.get("map")
    if isinstance(cached, dict) and now < float(_WEB_JOB_PROJECT_MAP_CACHE.get("expires_at") or 0.0):
        return {str(key): str(value) for key, value in cached.items() if str(key)}
    mapping: dict[str, str] = {}
    if PROJECT_IDS_ROOT.exists():
        state_files = [
            Path("state/full_cycle_job.json"),
            Path("state/full_research_cycle.json"),
            Path("paper/metadata/paper_pipeline.json"),
        ]
        try:
            project_roots = sorted(root for root in PROJECT_IDS_ROOT.iterdir() if root.is_dir())
        except Exception:
            project_roots = []
        for root in project_roots:
            for rel_path in state_files:
                payload = _read_project_json(root / rel_path, {})
                if not isinstance(payload, dict):
                    continue
                nested = payload.get("full_cycle_job") if isinstance(payload.get("full_cycle_job"), dict) else {}
                for value in [payload.get("web_job_id"), payload.get("job_id"), payload.get("id"), nested.get("web_job_id"), nested.get("job_id")]:
                    key = str(value or "").strip()
                    if key:
                        mapping[key] = root.name
    _WEB_JOB_PROJECT_MAP_CACHE["map"] = dict(mapping)
    _WEB_JOB_PROJECT_MAP_CACHE["expires_at"] = time.monotonic() + WEB_JOB_PROJECT_CACHE_TTL_SEC
    return dict(mapping)


def _project_for_web_job_id(job_id: Any) -> str:
    target = str(job_id or "").strip()
    if not target:
        return ""
    fast = _project_from_job_id_fast(target)
    if fast:
        return fast
    cache_key = target
    now = time.monotonic()
    cached = _WEB_JOB_PROJECT_CACHE.get(cache_key)
    if isinstance(cached, dict) and now < float(cached.get("expires_at") or 0.0):
        return str(cached.get("project") or "")
    found = _web_job_project_map().get(target, "")
    _WEB_JOB_PROJECT_CACHE[cache_key] = {"expires_at": time.monotonic() + WEB_JOB_PROJECT_CACHE_TTL_SEC, "project": found}
    return found


def _pid_from_project_worker_job_id(job_id: Any) -> str:
    match = re.search(r"^(?:experiment|project)-worker_[A-Za-z0-9_.-]+_(\d+)$", str(job_id or ""))
    return match.group(1) if match else ""


def _project_from_job_payload(job_id: Any, live_job: Any = None, known_job: Any = None) -> str:
    sources: list[Any] = []
    if isinstance(live_job, dict):
        sources.append(live_job.get("result") if isinstance(live_job.get("result"), dict) else {})
        sources.append(live_job)
        sources.extend(live_job.get("logs") if isinstance(live_job.get("logs"), list) else [])
    if known_job is not None:
        known_result = known_job.result if isinstance(getattr(known_job, "result", None), dict) else {}
        sources.append(known_result)
        sources.extend(getattr(known_job, "logs", []) or [])
    for source in sources:
        if isinstance(source, dict):
            project = str(source.get("project") or "").strip()
            if project:
                return project
            haystack = json.dumps(source, ensure_ascii=False)
        else:
            haystack = str(source or "")
        for pattern in [r"--project\s+([A-Za-z0-9_.-]+)", r"project=([A-Za-z0-9_.-]+)"]:
            match = re.search(pattern, haystack)
            if match:
                return match.group(1).strip()
    return _project_for_web_job_id(job_id)


def _phase_hint_from_job(job_id: Any, live_job: Any = None, known_job: Any = None) -> str:
    job_text = str(job_id or "").strip().lower().replace("_", "-")
    if job_text.startswith(("paper",)):
        return "paper"
    if job_text.startswith(("environment",)):
        return "environment"
    if job_text.startswith(("experiment", "autonomous")):
        return "experiment"
    if isinstance(live_job, dict):
        result = live_job.get("result") if isinstance(live_job.get("result"), dict) else {}
        panel_stage = _panel_stage_from_module_controller_result(result)
        if panel_stage:
            return panel_stage
        if _is_paper_job(live_job.get("stage"), live_job.get("job_id") or job_id, result, live_job.get("logs")):
            return "paper"
        phase = str(result.get("phase") or live_job.get("stage") or "").strip().lower()
        if phase in {"paper", "experiment", "environment", "literature"}:
            return phase
    if known_job is not None:
        known_result = known_job.result if isinstance(getattr(known_job, "result", None), dict) else {}
        panel_stage = _panel_stage_from_module_controller_result(known_result)
        if panel_stage:
            return panel_stage
        if _is_paper_job(getattr(known_job, "stage", ""), getattr(known_job, "job_id", "") or job_id, known_result, getattr(known_job, "logs", [])):
            return "paper"
        stage = _public_taste_stage(getattr(known_job, "stage", ""))
        if stage in {"environment", "experiment", "paper"}:
            return stage
    return ""


def _live_job_with_active_child(job_id: str, live_job: Any, project_id: str, phase_hint: str = "") -> dict[str, Any]:
    base = dict(live_job) if isinstance(live_job, dict) else {"job_id": job_id, "status": "running", "logs": [], "progress": {}}
    if not project_id:
        return base
    root = PROJECT_IDS_ROOT / project_id
    if not root.exists():
        return base
    child = _active_project_child_process(project_id, root, phase_hint=phase_hint)
    if not child and phase_hint:
        child = _active_project_child_process(project_id, root)
    if not child:
        return base
    result = base.get("result") if isinstance(base.get("result"), dict) else {}
    current_pid = str(result.get("pid") or "").strip()
    child_pid = str(child.get("pid") or "").strip()
    child_phase = str(child.get("phase") or phase_hint or result.get("phase") or base.get("stage") or "experiment").strip()
    child_kind = str(child.get("kind") or "").strip()
    should_prefer_child = bool(
        child_pid
        and (
            phase_hint == "paper"
            or child_phase == "paper"
            or not _pid_alive_local(current_pid)
            or current_pid != child_pid
        )
    )
    if not should_prefer_child:
        return base
    logs = base.get("logs") if isinstance(base.get("logs"), list) else []
    child_cmd = child.get("cmd") or result.get("command") or result.get("cmd") or "active project worker"
    merged_result = {
        **result,
        "project": project_id,
        "pid": child_pid,
        "phase": child_phase,
        "raw_stage": child_kind or result.get("raw_stage") or child_phase,
        "command": child_cmd,
        "cmd": child_cmd,
        "process_alive": True,
        "status": "running",
        "kind": child_kind or result.get("kind") or "active_child_worker",
    }
    return {
        **base,
        "job_id": str(base.get("job_id") or job_id),
        "stage": child_phase,
        "status": "running",
        "result": merged_result,
        "logs": [*logs, f"Recovered active {child_phase} worker PID={child_pid} for this research job."],
    }


_LIVE_TASK_STATUSES = {"queued", "running", "cancelling"}


def _job_status_is_live(status: Any) -> bool:
    return str(status or "").strip().lower() in _LIVE_TASK_STATUSES




def _parse_job_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


STAGE_LABELS_ZH = {
    "find": "发现",
    "read": "精读",
    "idea": "想法",
    "plan": "计划",
    "environment": "环境配置",
    "experiment": "实验迭代",
    "paper": "论文撰写",
    "full-cycle": "完整科研循环",
    "full_research_cycle": "完整科研循环",
}



def _stage_label_zh(stage: str) -> str:
    return STAGE_LABELS_ZH.get(str(stage or "").strip(), str(stage or "任务"))


def _job_status_message(stage: str, status: str) -> str:
    label = _stage_label_zh(stage)
    if status == "started":
        return f"{label}任务已启动"
    if status == "cancelled":
        return f"{label}任务已取消"
    if status in {"interrupted", "stale"}:
        return f"{label}任务已停止"
    if status == "complete":
        return f"{label}任务已完成"
    if status == "running":
        return f"{label}任务仍在后台运行"
    if status == "blocked":
        return f"{label}任务在证据门控处停止"
    return f"{label}任务状态：{status}"


def _reconcile_stale_cancelling_jobs(grace_seconds: float = 8.0) -> None:
    # Release web jobs that were cancelled after their child process already exited.
    now = datetime.now(UTC)
    changed = False
    with JOBS_LOCK:
        jobs_snapshot = list(JOBS.values())
    for job in jobs_snapshot:
        status = str(getattr(job, "status", "") or "").strip().lower()
        if status != "cancelling" or not bool(getattr(job, "cancel_requested", False)):
            continue
        cancelled_at = _parse_job_timestamp(getattr(job, "cancelled_at", "")) or _parse_job_timestamp(getattr(job, "created_at", "")) or now
        age = max(0.0, (now - cancelled_at).total_seconds())
        if not getattr(job, "done", threading.Event()).is_set() and age < grace_seconds:
            continue
        project_id = _project_from_job_payload(getattr(job, "job_id", ""), None, job)
        phase_hint = _phase_hint_from_job(getattr(job, "job_id", ""), None, job)
        active_child = None
        if project_id:
            root = PROJECT_IDS_ROOT / project_id
            if root.exists():
                active_child = _active_project_child_process(project_id, root, phase_hint=phase_hint) or _active_project_child_process(project_id, root)
        child_pid = str((active_child or {}).get("pid") or "").strip()
        if child_pid and _pid_alive_local(child_pid):
            continue
        with JOBS_LOCK:
            if str(getattr(job, "status", "") or "").strip().lower() != "cancelling":
                continue
            job.status = "cancelled"
            job.error = job.error or "research action cancelled by user."
            job.cancelled_at = job.cancelled_at or now.isoformat().replace("+00:00", "Z")
            job.progress = {"phase": "cancelled", "current": 0, "total": 1, "percent": 0, "message": "research action cancelled by user."}
            job.progress_version += 1
            job.logs.append("Reconciled stale cancelling job: no active child process remains, so the web stage lock was released.")
            job.mark_finished(job.cancelled_at)
            job.done.set()
            changed = True
    if changed:
        _LIVE_JOBS_CACHE.clear()
        _persist_jobs()


def _paper_substage_from_cmd(cmd: Any, fallback: str = "") -> str:
    text = str(cmd or "")
    lowered = text.lower()
    if "repair_paper_preview_loop.py" in lowered or "writing_revision_prompt" in lowered or "--agent-id writing_revision" in lowered:
        return "paper:preview-repair"
    if "repair_paper_figures_loop.py" in lowered:
        return "paper:figure-repair"
    match = re.search(r"(?:^|\s)--stage(?:=|\s+)([^\s]+)", text)
    if match:
        stage = match.group(1).strip().strip("'\"")
        if stage:
            return stage
    if "run_paper_orchestra_bridge.py" in lowered:
        return "writing:orchestra"
    if "modules/writing/main.py" in lowered and "--action chat" in lowered:
        return "paper:chat"
    if "modules/writing/main.py" in lowered and "--action work" in lowered:
        return "paper:pipeline"
    if "fetch_latex_template.py" in lowered:
        return "paper:template"
    if "resolve_venue_requirements.py" in lowered:
        return "paper:venue-requirements"
    if "compile_paper_pdf.py" in lowered or "latexmk" in lowered or "pdflatex" in lowered:
        return "paper:compile"
    if "claude" in lowered:
        return "writing:claude"
    return str(fallback or "paper").strip() or "paper"


def _paper_worker_projection_from_process(row: dict[str, Any], *, controller_pid: str = "") -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    cmd = str(row.get("cmd") or row.get("command") or "")
    if not cmd or _is_inspection_or_wrapper_cmd(cmd):
        return {}
    lowered = cmd.lower()
    kind = ""
    priority = 99
    if re.search(r"(?:^|/)claude\s+-p\b", cmd) or "bin/claude -p" in lowered:
        kind = "paper_claude_cli"
        priority = 1
    elif any(marker in lowered for marker in ["repair_paper_preview_loop.py", "repair_paper_figures_loop.py"]):
        kind = "paper_repair_loop"
        priority = 2
    elif "run_paper_orchestra_bridge.py" in lowered:
        kind = "paper_orchestra_bridge"
        priority = 3
    elif "modules/writing/main.py" in lowered and "--action chat" in lowered:
        kind = "paper_claude_session"
        priority = 0
    elif "modules/writing/main.py" in lowered and "--action work" in lowered:
        kind = "paper_pipeline"
        priority = 4
    elif any(marker in lowered for marker in ["fetch_latex_template.py", "resolve_venue_requirements.py", "compile_paper_pdf.py", "latexmk", "pdflatex"]):
        kind = "paper_subprocess"
        priority = 5
    if not kind:
        return {}
    pid = str(row.get("pid") or "").strip()
    if not pid:
        return {}
    substage = _paper_substage_from_cmd(cmd, fallback="paper")
    return {
        "pid": pid,
        "ppid": str(row.get("ppid") or "").strip(),
        "elapsed": str(row.get("elapsed") or "").strip(),
        "pcpu": str(row.get("pcpu") or "").strip(),
        "pmem": str(row.get("pmem") or "").strip(),
        "cmd": cmd,
        "kind": kind,
        "current_substage": substage,
        "priority": priority + (1 if controller_pid and pid == controller_pid else 0),
    }


def _paper_live_worker_projection(project: Any, result: dict[str, Any], job_status: Any = "") -> dict[str, Any]:
    project_id = re.sub(r"[^A-Za-z0-9_.-]+", "", str(project or result.get("project") or ""))
    if not project_id:
        return {}
    root = PROJECT_IDS_ROOT / project_id
    if not root.exists():
        return {}
    controller_pid = str(result.get("pid") or "").strip()
    controller_cmd = str(result.get("cmd") or result.get("command") or "").strip()
    live_requested = _job_status_is_live(job_status) or result.get("process_alive") is True
    candidates: list[dict[str, Any]] = []
    if controller_pid and _pid_alive_local(controller_pid):
        for row in _process_tree_rows(controller_pid):
            worker = _paper_worker_projection_from_process(row, controller_pid=controller_pid)
            if worker:
                candidates.append(worker)
    if not candidates:
        for row in _active_project_child_processes(project_id, root, phase_hint="paper"):
            worker = _paper_worker_projection_from_process(row, controller_pid=controller_pid)
            if worker:
                candidates.append(worker)
    if not candidates and live_requested and controller_pid and _pid_alive_local(controller_pid):
        candidates.append({
            "pid": controller_pid,
            "ppid": "",
            "elapsed": "",
            "pcpu": "",
            "pmem": "",
            "cmd": controller_cmd,
            "kind": "paper_pipeline",
            "current_substage": _paper_substage_from_cmd(controller_cmd, fallback="paper"),
            "priority": 10,
        })
    if not candidates:
        return {}
    def worker_sort_key(row: dict[str, Any]) -> tuple[int, int]:
        raw_priority = row.get("priority")
        try:
            priority = int(raw_priority) if raw_priority not in (None, "") else 99
        except Exception:
            priority = 99
        try:
            pid_value = int(str(row.get("pid") or "0"))
        except Exception:
            pid_value = 0
        return priority, pid_value

    candidates.sort(key=worker_sort_key)
    worker = dict(candidates[0])
    substage = str(worker.get("current_substage") or "paper").strip() or "paper"
    projection = {
        "project": project_id,
        "status": "running",
        "process_alive": True,
        "alive": True,
        "phase": "paper",
        "raw_stage": substage,
        "current_substage": substage,
        "paper_current_substage": substage,
        "paper_execution_alive": True,
        "paper_execution_state": "running",
        "paper_execution_message": f"后台写作进程正在运行；当前子阶段={substage}。",
        "pid": worker.get("pid"),
        "cmd": worker.get("cmd") or controller_cmd,
        "command": worker.get("cmd") or controller_cmd,
        "kind": worker.get("kind") or "paper_worker",
        "paper_worker_pid": worker.get("pid"),
        "paper_worker_kind": worker.get("kind") or "paper_worker",
    }
    if controller_pid:
        projection["paper_controller_pid"] = controller_pid
    if controller_cmd and controller_cmd != projection.get("cmd"):
        projection["paper_controller_cmd"] = controller_cmd
    for key in ("elapsed", "pcpu", "pmem"):
        if worker.get(key):
            projection[f"paper_worker_{key}"] = worker.get(key)
    return projection


def _paper_live_status_message(result: dict[str, Any], progress: dict[str, Any] | None = None) -> str:
    result = result if isinstance(result, dict) else {}
    progress = progress if isinstance(progress, dict) else {}
    substage = str(result.get("paper_current_substage") or result.get("current_substage") or progress.get("phase") or "paper").strip()
    worker_pid = str(result.get("paper_worker_pid") or result.get("pid") or "").strip()
    controller_pid = str(result.get("paper_controller_pid") or "").strip()
    bits = [f"paper 正在运行：{substage or 'paper'}"]
    if worker_pid:
        bits.append(f"worker PID={worker_pid}")
    if controller_pid and controller_pid != worker_pid:
        bits.append(f"controller PID={controller_pid}")
    kind = str(result.get("paper_worker_kind") or result.get("kind") or "").strip()
    if kind:
        bits.append(f"worker={kind}")
    bits.append("投稿/证据门控保持真实状态")
    return "；".join(bits) + "。"


def _paper_execution_projection(row: dict[str, Any]) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    if row.get("process_alive") is True or row.get("alive") is True:
        substage = str(row.get("paper_current_substage") or row.get("current_substage") or row.get("phase") or "paper").strip() or "paper"
        return {
            "paper_execution_alive": True,
            "paper_execution_state": "running",
            "paper_execution_message": f"后台写作进程正在运行；当前子阶段={substage}。",
        }
    labels = _paper_venue_labels(row)
    preview_label = labels.get("preview_zh", "会议格式论文预览")
    content_policy_blocked = _paper_content_policy_blocked(row)
    preview_available = _paper_preview_artifact_available(row)
    conference_ready = bool(row.get("conference_preview_ready") and row.get("pdf_path"))
    if content_policy_blocked:
        state = "finished_content_policy_blocked"
        message = "后台写作进程未在运行；候选稿已生成，但内容策略门控阻塞。" if preview_available else "后台写作进程未在运行；候选稿内容策略门控阻塞，尚无可检查预览产物。"
    elif conference_ready:
        state = "finished_preview_ready"
        message = f"后台写作进程未在运行；{preview_label}已通过预览门控。"
    elif preview_available:
        state = "finished_preview_gate_blocked"
        message = f"后台写作进程未在运行；{preview_label}已生成，但质量/自审门控仍阻塞。"
    else:
        state = "needs_writing"
        message = f"后台写作进程未在运行；尚无{preview_label}产物，等待生成或修订。"
    return {
        "paper_execution_alive": False,
        "paper_execution_state": state,
        "paper_execution_message": message,
    }


_PAPER_EXECUTION_KEYS = ("paper_execution_alive", "paper_execution_state", "paper_execution_message")


def _paper_result_has_live_execution(result: Any, status: Any = "") -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("process_alive") is True or result.get("alive") is True:
        return True
    return bool(_job_status_is_live(status or result.get("status")) and result.get("paper_execution_alive") is True)


def _merge_paper_live_execution_projection(result: dict[str, Any], live_projection: dict[str, Any]) -> dict[str, Any]:
    merged = {**result, **live_projection}
    paper_stage = merged.get("paper_stage") if isinstance(merged.get("paper_stage"), dict) else None
    if paper_stage is not None:
        nested = dict(paper_stage)
        for key in _PAPER_EXECUTION_KEYS:
            if key in live_projection:
                nested[key] = live_projection[key]
        merged["paper_stage"] = nested
    return merged


def _public_taste_stage(stage: Any) -> str:
    """Map internal research job labels to the seven public workflow stages."""
    raw = str(stage or '').strip()
    lowered = raw.lower().replace('_', '-')
    if lowered == 'plan-polish':
        return 'plan'
    if lowered in {'healthcheck', 'status', 'init'}:
        return 'environment'
    if lowered == 'email':
        return 'paper'
    if lowered in {"writing-chat", "paper-chat"} or lowered == 'paper' or lowered.startswith('paper-') or lowered.startswith('paper_'):
        return 'paper'
    if lowered in {"environment-chat"}:
        return "environment"
    if lowered in {'find', 'read', 'idea', 'plan', 'environment', 'experiment', 'paper'}:
        return lowered
    if not lowered:
        return raw
    # Native taskbar rows should expose only top-level workflow stages.
    # Keep detailed internal stage names in result.raw_stage/log lines instead.
    environment_literature_markers = [
        'sync-outputs', 'literature-sync', 'literature-tool-packet', 'build-literature-tool-packet',
        'fresh-research-base-selection', 'research-base-selection', 'base-selection', 'fresh-base', 'base-candidate',
        'literature-base-candidate', 'literature-base-audit', 'method-stack-sync',
    ]
    if any(marker in lowered for marker in environment_literature_markers):
        return 'environment'
    fresh_find_markers = ['literature-survey', 'run-finding', 'run-driver', 'run-literature-tool']
    if any(marker in lowered for marker in fresh_find_markers) or lowered in {'find', 'literature', 'finding'}:
        return 'find'
    if 'read' in lowered:
        return 'read'
    if 'idea' in lowered or 'ideation' in lowered:
        return 'idea'
    if 'plan' in lowered:
        return 'plan'
    if any(marker in lowered for marker in ['environment', 'env', 'loader', 'reference', 'safe-unblock']):
        return 'environment'
    if any(marker in lowered for marker in ['paper-evidence-audit', 'paper-normality-audit', 'submission-readiness']):
        return 'experiment'
    if any(marker in lowered for marker in ['paper-pipeline', 'paper-preview', 'paper-figure', 'conference-preview', 'latex', 'email']):
        return 'paper'
    if any(marker in lowered for marker in ['experiment', 'autonomous', 'trajectory', 'evidence', 'blocker', 'research', 'guidance']):
        return 'experiment'
    return raw



PUBLIC_WORKFLOW_STAGES = {"find", "read", "idea", "plan", "environment", "experiment", "paper"}


def _public_full_cycle_stage(raw_stage: Any, progress: Any = None, result: Any = None) -> str:
    """Expose a running full-cycle row as its real seven-step workflow phase."""
    candidates: list[Any] = []
    if isinstance(progress, dict):
        candidates.append(progress.get("phase"))
    if isinstance(result, dict):
        candidates.extend([result.get("phase"), result.get("raw_stage"), result.get("stage")])
        full_cycle = result.get("full_cycle_job") if isinstance(result.get("full_cycle_job"), dict) else {}
        candidates.extend([full_cycle.get("stage"), full_cycle.get("child_stage"), full_cycle.get("phase")])
    candidates.append(raw_stage)

    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        lowered = value.lower().replace("_", "-")
        if lowered in {"literature", "finding"}:
            return "find"
        if lowered.startswith("full-cycle-"):
            lowered = lowered[len("full-cycle-"):]
        mapped = _public_taste_stage(lowered)
        if mapped in PUBLIC_WORKFLOW_STAGES:
            return mapped
    return "experiment"



class JobState:
    def __init__(self, job_id: str, stage: str):
        self.job_id = job_id
        self.stage = stage
        self.status = "queued"
        self.created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.finished_at = ""
        self.logs: list[str] = []
        self.result: Any = None
        self.error: str = ""
        self.cancel_requested = False
        self.cancelled_at = ""
        self.run_id = ""
        self.progress = {"phase": "queued", "current": 0, "total": 0, "percent": 0, "message": "Queued"}
        self.internal = False
        self.display = ""
        self.progress_version = 0
        self.done = threading.Event()
        account = _CURRENT_ACCOUNT.get()
        self.owner_id = account.id if account is not None else ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        job = cls(str(data.get("job_id") or "job_unknown"), str(data.get("stage") or "unknown"))
        job.status = str(data.get("status") or "queued")
        job.created_at = str(data.get("created_at") or job.created_at)
        job.finished_at = str(data.get("finished_at") or "")
        job.logs = [str(line) for line in data.get("logs", [])]
        job.result = data.get("result")
        job.internal = bool(data.get("internal"))
        job.display = str(data.get("display") or "")
        job.owner_id = str(data.get("owner_id") or "")
        if job.stage == "safe-unblock" or job.job_id.startswith("safe-unblock_"):
            job.internal = True
            job.display = job.display or "hidden"
        job.error = str(data.get("error") or "")
        job.cancel_requested = bool(data.get("cancel_requested", False))
        job.cancelled_at = str(data.get("cancelled_at") or "")
        job.run_id = str(data.get("run_id") or "")
        if not job.run_id:
            for line in job.logs:
                match = re.search(r"Created run\s+(\S+)", line)
                if match:
                    job.run_id = match.group(1)
                    break
        progress = data.get("progress")
        if isinstance(progress, dict):
            job.progress = progress
        if job.status in {"done", "error", "cancelled", "blocked", "interrupted", "stale"}:
            job.done.set()
        return job

    def log(self, message: str) -> None:
        line = str(message)
        if not self.run_id:
            match = re.search(r"Created run\s+(\S+)", line)
            if match:
                self.run_id = match.group(1)
        self.logs.append(line)
        if _public_taste_stage(self.stage) == "read":
            self.progress_version += 1
        _persist_jobs()

    def as_dict(self, *, compact: bool = False) -> dict:
        result_payload = _compact_job_result(self.result, self.stage, self.job_id, self.logs) if compact else self.result
        panel_stage = _panel_stage_from_module_controller_result(result_payload if isinstance(result_payload, dict) else self.result)
        paper_job = _is_paper_job(self.stage, self.job_id, result_payload if isinstance(result_payload, dict) else self.result, self.logs)
        public_stage = panel_stage or ("paper" if paper_job else _public_taste_stage(self.stage))
        progress_payload = self.progress
        if compact and paper_job and public_stage == "paper" and isinstance(result_payload, dict):
            progress_payload = dict(self.progress if isinstance(self.progress, dict) else {})
            live_paper_job = _job_status_is_live(self.status)
            if live_paper_job:
                project_id = _project_from_job_payload(self.job_id, {"result": result_payload}, self)
                source_result = self.result if isinstance(self.result, dict) else {}
                live_projection = _paper_live_worker_projection(project_id, {**source_result, **result_payload}, self.status)
                if live_projection:
                    result_payload = _merge_paper_live_execution_projection(result_payload, live_projection)
                    progress_payload["message"] = _paper_live_status_message(result_payload, progress_payload)
                    progress_payload["phase"] = str(result_payload.get("paper_current_substage") or progress_payload.get("phase") or "paper")
                    progress_payload["current"] = progress_payload.get("current") or 0
                    progress_payload["total"] = progress_payload.get("total") or 0
                    progress_payload["percent"] = progress_payload.get("percent") or 0
                else:
                    result_payload = {**result_payload, "status": self.status}
                    progress_payload["phase"] = str(progress_payload.get("phase") or "paper")
            else:
                paper_summary = str(result_payload.get("paper_summary") or "").strip()
                if paper_summary:
                    progress_payload["message"] = paper_summary
                    progress_payload["phase"] = str(result_payload.get("status") or progress_payload.get("phase") or "paper")
        if compact and public_stage == "environment":
            decision_projection = _environment_decision_public_projection(self.job_id, self.run_id, result_payload if isinstance(result_payload, dict) else self.result, self.created_at)
            project_id = _project_from_job_payload(self.job_id, {"result": result_payload} if isinstance(result_payload, dict) else {"result": self.result}, self)
            stale_projection = _stale_environment_handoff_job_projection(project_id)
            if stale_projection and decision_projection.get("status") == "ready_for_experimenting":
                decision_projection = {**decision_projection, **stale_projection}
            if decision_projection:
                progress_payload = dict(progress_payload if isinstance(progress_payload, dict) else {})
                progress_payload.update({
                    "phase": decision_projection.get("status") or "blocked",
                    "current": 1,
                    "total": 1,
                    "percent": 100,
                    "message": str(decision_projection.get("summary") or ""),
                })
                base_result = result_payload if isinstance(result_payload, dict) else {}
                result_payload = {
                    **base_result,
                    "status": decision_projection.get("status") or base_result.get("status") or self.status,
                    "summary": decision_projection.get("summary") or base_result.get("summary") or "",
                    "environment_decision": decision_projection,
                    "run_id": decision_projection.get("run_id") or base_result.get("run_id"),
                }
        if public_stage == "read":
            progress_payload = _read_job_progress_payload(self.logs, progress_payload, result_payload if isinstance(result_payload, dict) else self.result, status=self.status)
            if isinstance(result_payload, dict) and isinstance(progress_payload.get("read_progress"), dict):
                result_payload = {**result_payload, "read_progress": progress_payload.get("read_progress")}
                if self.status in {"cancelling", "cancelled"}:
                    result_payload["status"] = self.status
                elif self.status == "error":
                    result_payload["status"] = "error"
        log_stage = panel_stage or ("paper" if paper_job else self.stage)
        logs = _public_job_logs(log_stage, self.logs, progress_payload, result_payload if isinstance(result_payload, dict) else self.result, limit=80) if compact or public_stage == "read" else self.logs
        if public_stage != self.stage and isinstance(self.result, dict):
            self.result.setdefault("raw_stage", self.stage)
        if compact and isinstance(result_payload, dict) and self.run_id and not str(result_payload.get("run_id") or "").strip():
            result_payload["run_id"] = self.run_id
        public_status = _public_paper_status(self.status, result_payload if isinstance(result_payload, dict) else {}) if paper_job and public_stage == "paper" else self.status
        public_status = _normalize_public_job_status(public_status, progress_payload, self.error, result_payload if isinstance(result_payload, dict) else self.result, public_stage)
        if str(public_status).strip().lower() == "interrupted":
            public_status = "stale"
        if public_stage == "environment" and public_status == "blocked" and isinstance(progress_payload, dict):
            message = str(progress_payload.get("message") or "")
            if re.search(r"(?:exit code|错误码|返回错误码)\s*30\b", message, flags=re.I):
                progress_payload = dict(progress_payload)
                progress_payload["phase"] = "blocked"
                progress_payload["current"] = 1
                progress_payload["total"] = 1
                progress_payload["percent"] = 100
                progress_payload["message"] = "环境配置停在真实证据门控；详细原因见保留日志和对应模块产物。"
        payload = {
            "job_id": self.job_id,
            "stage": public_stage,
            "status": public_status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "logs": logs,
            "log_count": len(self.logs),
            "run_id": self.run_id or (str(result_payload.get("run_id") or "") if isinstance(result_payload, dict) else ""),
            "result": result_payload,
            "internal": self.internal,
            "display": self.display,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "cancelled_at": self.cancelled_at,
            "progress": progress_payload,
        }
        return _strip_public_taste_marker(payload)

    def should_cancel(self) -> bool:
        return self.cancel_requested

    def request_cancel(self) -> None:
        if self.status in {"done", "error", "cancelled", "blocked", "interrupted", "stale"}:
            return
        self.cancel_requested = True
        self.status = "cancelling"
        self.log("Cancellation requested.")
        _persist_jobs()

    def mark_finished(self, finished_at: str = "") -> None:
        if not self.finished_at:
            self.finished_at = str(finished_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    def set_progress(self, phase: str, current: int = 0, total: int = 0, message: str = "") -> None:
        percent = 0
        if total > 0:
            percent = max(0, min(100, int(round((current / total) * 100))))
        self.progress = {
            "phase": phase,
            "current": max(0, current),
            "total": max(0, total),
            "percent": percent,
            "message": message or phase,
        }
        self.progress_version += 1
        _persist_jobs()


JOBS: dict[str, JobState] = {}
JOBS_PATH = STATE_DIR / "web_jobs.json"
JOBS_LOCK = threading.RLock()
JOBS_PERSIST_LOCK = threading.RLock()
LIVE_JOBS_TTL_SEC = float(os.environ.get("LIVE_JOBS_TTL_SEC", "5.0") or 5.0)
_LIVE_JOBS_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": []}
JOB_PROJECT_ID_CACHE_TTL_SEC = float(os.environ.get("JOB_PROJECT_ID_CACHE_TTL_SEC", "30.0") or 30.0)
_JOB_PROJECT_ID_CACHE: dict[str, Any] = {}
PROCESS_ROWS_TTL_SEC = float(os.environ.get("PROCESS_ROWS_TTL_SEC", "1.5") or 1.5)
_PROCESS_ROWS_CACHE: dict[str, Any] = {"expires_at": 0.0, "rows": []}
ACTIVE_LAUNCHER_ROWS_TTL_SEC = float(os.environ.get("ACTIVE_LAUNCHER_ROWS_TTL_SEC", "3.0") or 3.0)
_ACTIVE_LAUNCHER_ROWS_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
KNOWN_PROJECT_IDS_TTL_SEC = float(os.environ.get("KNOWN_PROJECT_IDS_TTL_SEC", "10.0") or 10.0)
_KNOWN_PROJECT_IDS_CACHE: dict[str, Any] = {"expires_at": 0.0, "ids": []}
WEB_JOB_PROJECT_CACHE_TTL_SEC = float(os.environ.get("WEB_JOB_PROJECT_CACHE_TTL_SEC", "30.0") or 30.0)
_WEB_JOB_PROJECT_CACHE: dict[str, dict[str, Any]] = {}
_WEB_JOB_PROJECT_MAP_CACHE: dict[str, Any] = {"expires_at": 0.0, "map": {}}
JOB_LIST_PROJECT_SUMMARY_TTL_SEC = float(os.environ.get("JOB_LIST_PROJECT_SUMMARY_TTL_SEC", "30.0") or 30.0)
_JOB_LIST_PROJECT_SUMMARY_CACHE: dict[tuple[str, bool, int], dict[str, Any]] = {}
DETACHED_JOB_RECONCILE_TTL_SEC = float(os.environ.get("DETACHED_JOB_RECONCILE_TTL_SEC", "10.0") or 10.0)
_DETACHED_JOB_RECONCILE_CACHE: dict[str, Any] = {"expires_at": 0.0}
RUN_PROJECT_CACHE_TTL_SEC = float(os.environ.get("RUN_PROJECT_CACHE_TTL_SEC", "30.0") or 30.0)
_RUN_PROJECT_CACHE: dict[str, dict[str, Any]] = {}
RUN_ARTIFACTS_CACHE_TTL_SEC = float(os.environ.get("RUN_ARTIFACTS_CACHE_TTL_SEC", "10.0") or 10.0)
RUNS_CACHE_TTL_SEC = float(os.environ.get("RUNS_CACHE_TTL_SEC", "10.0") or 10.0)
_RUN_ARTIFACTS_CACHE: dict[str, Any] = {}
_RUNS_CACHE: dict[str, Any] = {"expires_at": 0.0, "fingerprint": None, "items": []}

MARKDOWN_ARTIFACT_NAMES = [
    "find.md",
    "biorxiv.md", "nature.md", "science.md", "hf.md", "github.md", "source_status.md",
    "read.md", "idea.md", "plan.md",
]
JSON_ARTIFACT_NAMES = [
    "find_progress.json", "find_results.json", "stage0_profile.json",
    "venue_health_report.json", "category_scan_report.json", "title_filter_report.json",
    "venue_filter1.json", "filter2_trace.json", "filter2_survivors.json", "enriched_pre_filter3.json",
    "arxiv_raw.json", "arxiv_prefiltered.json", "biorxiv_raw.json", "biorxiv_prefiltered.json",
    "nature_raw.json", "nature_prefiltered.json", "science_raw.json", "science_prefiltered.json",
    "huggingface_raw.json", "github_raw.json",
    "read_results.json", "ideas.json", "plans.json", "config.json", "selection.json", "email_report.json",
]
LIGHT_ARTIFACT_MARKDOWN_NAMES = ["find.md", "source_status.md", "read.md", "idea.md", "plan.md"]
LIGHT_ARTIFACT_JSON_NAMES = ["find_progress.json", "find_results.json", "read_results.json", "ideas.json", "plans.json", "selection.json"]
CURRENT_FIND_ARTIFACT_SCOPES = {
    "find": (["find.md", "source_status.md"], ["find_progress.json", "find_results.json", "selection.json"]),
    "read": (["read.md"], ["read_results.json"]),
    "ideas": (["idea.md"], ["ideas.json"]),
    "plan": (["plan.md"], ["ideas.json", "plans.json"]),
}
FIND_SURVEY_COUNT_KEYS = [
    "raw_title_index_papers", "title_total_papers", "venue_total_papers_available", "venue_corpus_audited_papers",
    "category_corpus_audited_papers", "category_filtered_papers", "venue_category_selected_papers",
    "tfidf_screened_papers", "venue_title_filter_input_papers", "title_score_input_papers", "llm_title_scored_papers",
    "venue_final_title_candidates", "abstract_scored_papers", "venue_detail_fetched_candidates", "venue_evaluated_candidates",
    "llm_scored_candidates", "recommended_papers", "abstract_fetch_failed_candidates", "final_llm_scoring_skipped_candidates",
    "category_scan_reports", "title_filter_reports", "arxiv_raw_count", "arxiv_prefiltered_count", "arxiv_pages_fetched",
]
ENVIRONMENT_ARTIFACT_MARKDOWN_NAMES = ["workflow_status.md", "运行说明.txt"]
ENVIRONMENT_ARTIFACT_JSON_NAMES = [
    "frontend_status.json", "module_contracts_payload.json", "module_contracts.json",
    "environment_deployment_decision.json", "repo_info.json", "claude_repo_candidate_review.json",
    "machine_profile.json", "input_plan.normalized.json", "paper_evidence.json",
]
WRITING_ARTIFACT_MARKDOWN_NAMES = [
    "prompts/writing_claude_prompt.md",
    "prompts/writing_chat_claude_prompt.md",
    "workspace/chat/response.md",
    "workspace/final/paper.tex",
    "workspace/refs.bib",
    "workspace/audits/claude_quality_audit.md",
    "workspace/audits/round_00/claude_quality_audit.md",
    "workspace/repair_rounds/round_01/repair_plan.md",
]
WRITING_ARTIFACT_JSON_NAMES = [
    "run_summary.json",
    "run_manifest.json",
    "run_meta.json",
    "audit_repair_loop.json",
    "workspace/chat/chat_result.json",
    "workspace/provenance.json",
    "venue/venue_requirements.json",
    "venue/template_source.json",
    "workspace/audits/quality_audit_from_main.json",
    "workspace/audits/claude_quality_audit.json",
    "workspace/audits/round_00/claude_quality_audit.json",
    "workspace/repair_rounds/round_01/repair_report.json",
]


def _safe_run_id_text(run_id: Any) -> str:
    text = str(run_id or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        raise FileNotFoundError(f"Run not found: {run_id}")
    return text


def _run_id_variants(run_id: Any) -> list[str]:
    raw = _safe_run_id_text(run_id)
    variants: list[str] = []
    for item in [raw, raw.lower()]:
        if item and item not in variants:
            variants.append(item)
    return variants


def _case_insensitive_run_child(root: Path, run_id: str) -> Path | None:
    for name in _run_id_variants(run_id):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    lower = run_id.lower()
    try:
        for item in root.iterdir():
            if item.is_dir() and item.name.lower() == lower:
                return item
    except OSError:
        return None
    return None


def _run_artifact_roots(run_id: str) -> list[Path]:
    roots: list[Path] = []
    try:
        roots.append(run_dir(run_id))
    except FileNotFoundError:
        pass
    for root in [FINDING_RUNS_DIR, FRAMEWORK_RUNS_DIR, ENVIRONMENT_RUNS_DIR]:
        candidate = _case_insensitive_run_child(root, run_id)
        if candidate and candidate not in roots:
            roots.append(candidate)
    if not roots:
        raise FileNotFoundError(f"Run not found: {run_id}")
    return roots


def _run_artifact_public_path(root: Path, name: str) -> Path:
    direct = root / name
    if direct.exists():
        return direct
    public = root / "public" / name
    if public.exists():
        return public
    return direct


def _run_artifact_path(name: str, roots: list[Path], fallback_root: Path) -> Path:
    for root in roots:
        candidate = _run_artifact_public_path(root, name)
        if candidate.exists():
            return candidate
    return _run_artifact_public_path(fallback_root, name)


def _environment_artifact_run(roots: list[Path], run_id: str) -> bool:
    if str(run_id or "").lower().startswith("web_environment_"):
        return True
    return any((root / "environment_deployment_decision.json").exists() for root in roots)


def _runs_fingerprint() -> tuple[tuple[str, int, int, int], ...]:
    entries: list[tuple[str, int, int, int]] = []
    for root in RUNS_SEARCH_DIRS:
        try:
            root_stat = root.stat()
        except OSError:
            entries.append((str(root), 0, 0, 0))
            continue
        count = 0
        newest_manifest = 0
        try:
            children = list(root.iterdir())
        except OSError:
            entries.append((str(root), root_stat.st_mtime_ns, 0, 0))
            continue
        for item in children:
            if not item.is_dir():
                continue
            count += 1
            try:
                newest_manifest = max(newest_manifest, (item / "manifest.json").stat().st_mtime_ns)
            except OSError:
                continue
        entries.append((str(root), root_stat.st_mtime_ns, count, newest_manifest))
    return tuple(entries)


def _run_stage_names_from_artifacts(path_value: Any, existing: Any = None) -> list[str]:
    stages = [str(item) for item in (existing if isinstance(existing, list) else []) if str(item or "").strip()]
    path = Path(str(path_value or ""))
    if not path.exists():
        return stages
    checks = [
        ("find", ["find_results.json", "find.md"]),
        ("read", ["read.md", "read_results.json"]),
        ("idea", ["ideas.json", "idea.md"]),
        ("plan", ["plans.json", "plan.md"]),
    ]
    for stage, names in checks:
        if any((path / name).exists() for name in names) and stage not in stages:
            stages.append(stage)
    return stages


def _cached_list_runs() -> list[dict]:
    now = time.monotonic()
    fingerprint = _runs_fingerprint()
    if _RUNS_CACHE.get("fingerprint") == fingerprint and float(_RUNS_CACHE.get("expires_at") or 0) > now:
        return [dict(item) for item in _RUNS_CACHE.get("items", [])]
    items = list_runs()
    for item in items:
        if isinstance(item, dict):
            item["stages"] = _run_stage_names_from_artifacts(item.get("path"), item.get("stages"))
    _RUNS_CACHE.update({"fingerprint": fingerprint, "expires_at": now + RUNS_CACHE_TTL_SEC, "items": [dict(item) for item in items]})
    return items


def _clerun_caches(run_id: str = "") -> None:
    _RUNS_CACHE.update({"expires_at": 0.0, "fingerprint": None, "items": []})
    if run_id:
        for key in [key for key in _RUN_ARTIFACTS_CACHE if key == run_id or key.startswith(f"{run_id}:")]:
            _RUN_ARTIFACTS_CACHE.pop(key, None)
    else:
        _RUN_ARTIFACTS_CACHE.clear()


def _compact_count(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    return None


def _human_progress_message(value: Any, *, fallback: str = "") -> str:
    if isinstance(value, dict):
        for key in ["human_summary", "summary_zh", "summary", "message", "status"]:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()[:240]
        blocker = value.get("blocker") if isinstance(value.get("blocker"), dict) else {}
        if blocker:
            return _human_progress_message(blocker, fallback=fallback)
        full_cycle = value.get("full_research_cycle") if isinstance(value.get("full_research_cycle"), dict) else {}
        if full_cycle:
            return _human_progress_message(full_cycle, fallback=fallback)
        return fallback or "当前任务停在 TASTE 门控；详情见日志和产物路径。"
    text = str(value or "").strip()
    if (text.startswith("{") and "'project'" in text) or (text.startswith("{") and '"project"' in text):
        return fallback or "当前任务停在 TASTE 门控；详情见日志和产物路径。"
    text = re.sub(r"\s+", " ", text)
    return (text[:240] if text else fallback)


def _paper_int(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except Exception:
        return 0


def _paper_venue_labels(row: dict | None = None, venue: str = "") -> dict[str, str]:
    row = row if isinstance(row, dict) else {}
    policy = row.get("venue_submission_policy") if isinstance(row.get("venue_submission_policy"), dict) else {}
    venue_text = str(venue or row.get("venue") or row.get("target_venue") or row.get("venue_slug") or "")
    family = str(policy.get("template_family") or row.get("template_family") or "").lower()
    slug = venue_text.lower()
    is_journal = family == "springer-nature" or "nature" in slug or "journal" in slug
    return {
        "venue_zh": "期刊" if is_journal else "会议",
        "preview_zh": "期刊稿预览" if is_journal else "会议格式论文预览",
        "current_preview_zh": "当前期刊稿预览" if is_journal else "当前会议格式论文预览",
        "requirement_zh": "期刊要求" if is_journal else "会议要求",
    }


def _project_configured_venue(cfg: dict[str, Any] | None, fallback: str = '') -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    paper = cfg.get('paper') if isinstance(cfg.get('paper'), dict) else {}
    return str(cfg.get('target_venue') or cfg.get('venue') or paper.get('target_venue') or fallback or '').strip()


def _paper_venue_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _project_configured_venue_slug(root: Path) -> str:
    cfg = _read_project_json(root / "project.json", {})
    cfg = cfg if isinstance(cfg, dict) else {}
    paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    venue = cfg.get("target_venue") or cfg.get("venue") or paper.get("target_venue") or paper.get("venue") or paper.get("venue_slug") or ""
    return _paper_venue_slug(venue)


def _paper_receipt_stale_for_current_venue(root: Path, payload: Any, response_text: Any = "") -> bool:
    current_slug = _project_configured_venue_slug(root)
    if not current_slug:
        return False
    row = payload if isinstance(payload, dict) else {}
    for key in ["target_venue", "venue", "venue_slug", "active_venue"]:
        value = str(row.get(key) or "").strip()
        if value:
            slug = _paper_venue_slug(value)
            if slug and slug != current_slug:
                return True
    text = "\n".join([
        str(response_text or ""),
        str(row.get("instruction") or ""),
        str(row.get("response_markdown") or ""),
        str(row.get("response") or ""),
        str(row.get("raw_response") or ""),
        str(row.get("stdout") or ""),
        str(row.get("raw_stdout") or ""),
        json.dumps(row.get("claude_json") if isinstance(row.get("claude_json"), dict) else {}, ensure_ascii=False),
    ]).lower()
    explicit_slugs = {
        match.group(1).strip("-")
        for match in re.finditer(r"paper/(?:output|writing|venues|orchestra)/([a-z0-9-]+)", text)
        if match.group(1).strip("-")
    }
    if explicit_slugs and current_slug not in explicit_slugs:
        return True
    if current_slug not in {"nature", "springer-nature"} and any(
        marker in text for marker in ["springernature.com", "sn-jnl", "sn-nature", "nature article", "paper/output/nature"]
    ):
        return True
    if current_slug != "iclr" and any(marker in text for marker in ["github.com/iclr/master-template", "iclr2026_conference", "paper/output/iclr"]):
        return True
    return False


def _active_paper_state(root: Path, project: str, cfg: dict[str, Any] | None = None, venue: str = '') -> dict[str, Any]:
    state = load_active_paper_state(root, project, cfg, venue=venue or _project_configured_venue(cfg))
    return state if isinstance(state, dict) else {}


def _compact_official_sources(sources: Any) -> list[dict[str, str]]:
    """Expose concise official-source facts; full evidence remains in state files."""
    out: list[dict[str, str]] = []
    if not isinstance(sources, list):
        return out
    for item in sources[:5]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "官方来源").strip()
        url = str(item.get("url") or "").strip()
        if label or url:
            out.append({"label": label, "url": url})
    return out


def _venue_requirements_summary(root: Path, venue: str, paper_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Human-readable summary of the resolved official venue rules."""
    paper_state = paper_state if isinstance(paper_state, dict) else {}
    slug = re.sub(r"[^a-z0-9]+", "-", str(venue or paper_state.get("venue") or "").strip().lower()).strip("-") or "venue"
    req_path = root / "paper" / "venues" / slug / "venue_requirements.json"
    req = _read_project_json(req_path, {})
    if not isinstance(req, dict) or not req:
        policy = paper_state.get("venue_submission_policy") if isinstance(paper_state.get("venue_submission_policy"), dict) else {}
        return {
            "status": "missing",
            "venue": str(venue or paper_state.get("venue") or ""),
            "path": str(req_path),
            "summary": "目标会议官方模板和投稿要求尚未解析；writing 不应猜测页数、模板或引用要求。",
            "body_page_max": policy.get("body_page_max", ""),
            "official_min_references": policy.get("official_min_references", ""),
            "reference_quality_target": policy.get("reference_quality_target") or policy.get("reference_quality_target") or "",
            "reference_target_source": policy.get("reference_target_source", ""),
            "official_sources": [],
        }
    template = req.get("template") if isinstance(req.get("template"), dict) else {}
    policy = req.get("venue_submission_policy") if isinstance(req.get("venue_submission_policy"), dict) else {}
    page_policy = req.get("page_policy") if isinstance(req.get("page_policy"), dict) else {}
    citation_policy = req.get("citation_policy") if isinstance(req.get("citation_policy"), dict) else {}
    sources = req.get("official_sources") if isinstance(req.get("official_sources"), list) else []
    body_max = policy.get("body_page_max") or page_policy.get("body_page_max") or ""
    official_min = policy.get("official_min_references") or citation_policy.get("official_min_verified_references") or 0
    quality_target = policy.get("reference_quality_target") or policy.get("reference_quality_target") or citation_policy.get("quality_target_min_verified_references") or 0
    reference_source = policy.get("reference_target_source") or ("official" if official_min else "quality_target" if quality_target else "none")
    repo = template.get("verified_repository_url") or template.get("repository_url") or template.get("official_source_url") or ""
    commit = template.get("verified_repository_commit") or req.get("official_repository_commit") or ""
    directory = str(template.get("verified_directory_hint") or template.get("directory_hint") or "").strip("/")
    main_tex = template.get("main_tex") or ""
    source_label = "官方来源" if official_min else "写作质量目标"
    bits = []
    if body_max:
        bits.append(f"正文上限 {body_max} 页")
    if official_min:
        bits.append(f"官方最少引用 {official_min}")
    elif quality_target:
        bits.append(f"写作引用质量目标 {quality_target}")
    if directory or main_tex:
        bits.append("模板 " + "/".join(str(x) for x in [directory, main_tex] if x))
    if repo:
        bits.append("来源已核对")
    return {
        "status": str(req.get("status") or ""),
        "venue": str(req.get("venue") or venue or paper_state.get("venue") or ""),
        "path": str(req_path),
        "source_checked_at": req.get("source_checked_at") or req.get("updated_at") or "",
        "official_source_count": len(sources),
        "official_sources": _compact_official_sources(sources),
        "template_repository": repo,
        "template_commit": commit,
        "template_commit_short": str(commit)[:12] if commit else "",
        "template_directory": directory,
        "template_main_tex": main_tex,
        "template_family": template.get("family") or policy.get("template_family") or "",
        "body_page_max": body_max,
        "reference_page_max": policy.get("reference_page_max") or page_policy.get("reference_page_max") or "",
        "total_page_max": policy.get("total_page_max") or page_policy.get("total_page_max") or "",
        "official_min_references": official_min,
        "reference_quality_target": quality_target,
        "reference_target_source": reference_source,
        "reference_target_label": source_label,
        "summary": "；".join(bits) if bits else "目标会议官方要求已解析。",
    }


def _paper_public_blocker_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "legacy_route_story_in_manuscript" in lowered:
        match = re.search(r"legacy_route_story_in_manuscript:([^;，,\s]+)", text, flags=re.IGNORECASE)
        term = match.group(1) if match else "历史/未授权路线"
        return f"候选稿仍包含未授权或历史路线叙事：{term}；writing 必须基于当前 selected-route evidence 重新生成或修正稿件。"
    if "manuscript_candidate_rejected" in lowered or "manuscript_content_policy_violation" in lowered:
        return "候选稿内容策略未通过；不能作为当前会议格式预览或投稿稿，需要由 writing 从当前证据重新生成。"
    if "missing bib entries" in lowered or "missing bibliography entries" in lowered or "cited keys=" in lowered or "latex_undefined_citations" in lowered or "undefined citations" in lowered:
        return "引用/参考文献仍需修复；具体修复清单已交由 Writing 主控 Claude 处理。"
    if "nature_numeric_style_textual_citations" in lowered or "\\citet" in text or "\\citeauthor" in text or "作者型引用命令" in text:
        return "Nature 数字引用模板下检测到作者型引用命令；应改为正常叙述加数字引用，重新编译并确认 PDF 不再出现 `(author?)`。"
    if "pdf_unresolved_citation_markers" in lowered or "未解析引用标记" in text or "[?]" in text or "??" in text:
        return "引用渲染失败：PDF 正文含 `(author?)`、`[?]` 或 `??` 等未解析引用标记，不能作为正常论文预览通过。"
    if "natbib_author_undefined" in lowered or "author undefined" in lowered or "(author?)" in lowered:
        return "引用渲染失败：natbib Author undefined 导致 PDF 出现 `(author?) [n]`，TASTE 写作必须修复引用命令、BibTeX 字段或模板兼容性后重新编译。"
    if "references/citation" in lowered or "reference_quality_target" in lowered or "reference_count" in lowered:
        return "参考文献覆盖不足：当前引用数量还没有达到 写作引用质量目标，需要补充真实且相关的已验证引用。"
    if "wide" in lowered and "graphic" in lowered:
        return "图表版面需要调整：有宽图被压进单栏，优先处理图表占地和单栏适配。"
    return _public_text(text)


def _paper_public_layout_warning_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "wide" in lowered and "graphic" in lowered and "single-column" in lowered:
        return "图表版面提示：宽图被放入单栏，当前应优先改为跨栏图、重绘或缩减图表占地。"
    if "large single-column figure footprint" in lowered:
        return "图表版面提示：单栏图占地偏大，正文页数紧张时应先调整图表尺寸或重绘。"
    return _public_text(text)


def _paper_existing_file(root: Path, value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [Path(text)]
    if not Path(text).is_absolute():
        candidates.append(root / text)
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return path
        except Exception:
            continue
    return None


def _first_existing_paper_file(root: Path, values: list[Any]) -> Path | None:
    for value in values:
        path = _paper_existing_file(root, value)
        if path:
            return path
    return None


def _paper_candidate_audit_projection(root: Path, audit: Any) -> dict[str, Any]:
    rows = audit if isinstance(audit, list) else []
    blockers: list[dict[str, Any]] = []
    first_pdf: Path | None = None
    first_tex: Path | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        first_pdf = first_pdf or _paper_existing_file(root, row.get("pdf"))
        first_tex = first_tex or _paper_existing_file(root, row.get("tex"))
        violations = [str(item) for item in (row.get("violations") or []) if str(item).strip()]
        if not violations:
            continue
        label = str(row.get("label") or "candidate").strip() or "candidate"
        raw_detail = f"{label}: " + "; ".join(violations[:8])
        public_detail = _paper_public_blocker_text(raw_detail)
        blockers.append({
            "id": "manuscript_candidate_rejected",
            "status": "block",
            "detail": public_detail,
            "public_detail": public_detail,
            "source": "paper_content_candidate_audit",
            "preview_blocker": True,
            "submission_blocker": True,
        })
    summary = str(blockers[0].get("public_detail") or blockers[0].get("detail") or "") if blockers else ""
    return {"pdf": first_pdf, "tex": first_tex, "blockers": blockers, "summary": summary}


def _paper_content_blocker_summary(root: Path, paper_state: dict[str, Any]) -> str:
    content_status = str(paper_state.get("paper_content_policy_status") or "").strip().lower()
    stage_status = str(paper_state.get("paper_stage_status") or paper_state.get("status") or "").strip().lower()
    content_blocked = content_status == "blocked" or stage_status in {"blocked_content_policy", "content_policy_blocked"}
    violations = paper_state.get("paper_content_policy_violations") if isinstance(paper_state.get("paper_content_policy_violations"), list) else []
    if violations:
        public = _paper_public_blocker_text("; ".join(str(item) for item in violations[:8]))
        if public:
            return public
    audit = _paper_candidate_audit_projection(root, paper_state.get("paper_content_candidate_audit"))
    if content_blocked or audit.get("summary"):
        return str(audit.get("summary") or "")
    return ""


def _paper_content_policy_blocked(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    nested = row.get("paper_stage") if isinstance(row.get("paper_stage"), dict) else {}
    for candidate in (row, nested):
        status = str(candidate.get("status") or candidate.get("paper_stage_status") or "").strip().lower()
        content_status = str(candidate.get("paper_content_policy_status") or "").strip().lower()
        if status in {"blocked_content_policy", "content_policy_blocked"} or content_status == "blocked":
            return True
        summary = str(candidate.get("paper_content_blocker_summary") or "").strip().lower()
        if content_status != "pass" and ("legacy_route_story_in_manuscript" in summary or "内容策略" in summary):
            return True
    return False


def _paper_public_self_review_evidence_projection(category: str, detail: str) -> dict[str, str]:
    marker = f"{category} {detail}".lower()
    if "missing_empirical_validation" in marker or "zero empirical" in marker or "untested architecture" in marker:
        return {
            "public_title": "缺少新方法实验验证",
            "public_summary": "当前论文预览还没有用同一数据、同一 seed、同一指标验证拟议新方法；已有数字只适合作为参考基底校准或初始化检查。",
            "public_next_action": "继续由 Experimenting 主控 Claude 执行候选方法、基线和关键消融实验，写入可审计指标后再刷新论文。",
        }
    if "results_contains_untested_design_space" in marker or "method design space" in marker or "untested architectural variants" in marker:
        return {
            "public_title": "Results 含未验证设计空间",
            "public_summary": "结果部分仍包含未经实验比较的设计维度；这会让预览稿看起来像已经完成了大量实验。",
            "public_next_action": "把未验证设计移出结果结论，或补齐真实对比实验后再作为结果呈现。",
        }
    if "evaluation_scope_mismatch" in marker or ("contribution" in marker and "backbone" in marker):
        return {
            "public_title": "贡献表述范围不匹配",
            "public_summary": "当前贡献表述把参考基底校准写成了新方法贡献；预览稿需要区分前置复现和真正的新方法验证。",
            "public_next_action": "收窄贡献措辞，并等待候选方法本地证据通过后再提升结论。",
        }
    if "data_code_availability" in marker or "data availability" in marker or "code availability" in marker:
        return {
            "public_title": "数据/代码可用性缺少明确链接",
            "public_summary": "数据和代码可用性表述还没有映射到具体公开仓库、数据地址或当前项目 artifact。",
            "public_next_action": "补齐真实 URL、仓库名和本地 artifact 路径；匿名预览可以保守，但不能标记投稿就绪。",
        }
    if "citation" in marker or "author?" in marker or "bibtex" in marker:
        return {
            "public_title": "引用渲染或参考文献仍需修复",
            "public_summary": "PDF 或编译日志仍显示引用/参考文献渲染问题；该 PDF 只能作为检查预览。",
            "public_next_action": "修复引用命令、BibTeX 字段或模板兼容性并重新编译审计。",
        }
    return {
        "public_title": "科研证据待补齐",
        "public_summary": "Claude Code 独立审稿发现一项未解决的科研证据问题；完整原文保留在后端审计 artifact。",
        "public_next_action": "让对应模块主控 Claude 根据审稿 receipt 继续修实验、证据和论文，再刷新审计。",
    }


def _paper_public_self_review_evidence_blocker(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        category = str(value.get("category") or value.get("id") or "self_review_evidence")
        detail = str(value.get("detail") or value.get("issue") or "").strip()
        if not detail:
            detail = _compact_public_text(value)
        public = _paper_public_self_review_evidence_projection(category, detail)
        return {
            "id": str(value.get("id") or category),
            "category": category,
            "detail": public["public_summary"],
            "public_detail": public["public_summary"],
            **public,
            "source": "Claude Code 独立审稿",
            "preview_blocker": False,
            "submission_blocker": True,
        }
    text = str(value or "").strip()
    public = _paper_public_self_review_evidence_projection("self_review_evidence", text)
    return {"id": "self_review_evidence", "category": "self_review_evidence", "detail": public["public_summary"], "public_detail": public["public_summary"], **public, "source": "Claude Code 独立审稿", "preview_blocker": False, "submission_blocker": True}


def _paper_stage_from_project_snapshot(project: str) -> dict[str, Any]:
    project = re.sub(r"[^A-Za-z0-9_.-]+", "", str(project or ""))
    if not project:
        return {}
    root = PROJECT_IDS_ROOT / project
    if not root.exists():
        return {}
    cfg = _read_project_json(root / "project.json", {})
    venue_raw = _project_configured_venue(cfg)
    paper_state = _active_paper_state(root, project, cfg if isinstance(cfg, dict) else {}, venue=venue_raw)
    if not isinstance(paper_state, dict) or not paper_state:
        return {}
    venue_raw = str(paper_state.get("venue") or venue_raw or paper_state.get("target_venue") or paper_state.get("active_venue") or "").strip()
    venue = re.sub(r"[^a-z0-9]+", "-", venue_raw.lower()).strip("-") or "venue"
    venue_requirements = _venue_requirements_summary(root, venue_raw, paper_state)
    output_dir = root / "paper" / "output" / venue
    workspace_dir = _paper_existing_file(root, paper_state.get("paper_orchestra_workspace"))
    workspace_final_pdf = (workspace_dir / "final" / "paper.pdf") if workspace_dir else root / "__missing_workspace_final_paper.pdf"
    workspace_final_tex = (workspace_dir / "final" / "paper.tex") if workspace_dir else root / "__missing_workspace_final_paper.tex"
    candidate_audit = _paper_candidate_audit_projection(root, paper_state.get("paper_content_candidate_audit"))
    paper_stage_status = str(paper_state.get("paper_stage_status") or paper_state.get("status") or "").strip().lower()
    preview_gate_blocked = paper_stage_status in _PAPER_PREVIEW_GATE_BLOCKED_STATUSES
    accepted_pdf_path = _first_existing_paper_file(root, [paper_state.get("conference_preview_pdf"), paper_state.get("pdf_path")])
    accepted_tex_path = _first_existing_paper_file(root, [paper_state.get("conference_preview_tex"), paper_state.get("rendered_tex")])
    output_pdf_path = _paper_existing_file(root, output_dir / "paper.pdf")
    output_tex_path = _paper_existing_file(root, output_dir / "paper.tex")
    if not accepted_pdf_path and paper_state.get("conference_preview_ready"):
        accepted_pdf_path = output_pdf_path
    if not accepted_tex_path and paper_state.get("conference_preview_ready"):
        accepted_tex_path = output_tex_path
    blocked_pdf_path = _first_existing_paper_file(root, [paper_state.get("blocked_preview_pdf"), paper_state.get("latest_preview_pdf")])
    blocked_tex_path = _first_existing_paper_file(root, [paper_state.get("blocked_preview_tex"), paper_state.get("latest_preview_tex")])
    if preview_gate_blocked and not blocked_pdf_path:
        blocked_pdf_path = output_pdf_path
    if preview_gate_blocked and not blocked_tex_path:
        blocked_tex_path = output_tex_path
    raw_pdf_path = _first_existing_paper_file(root, [
        paper_state.get("latest_generated_pdf_path"),
        paper_state.get("paper_orchestra_final_pdf"),
        output_dir / "writing_raw.pdf",
        workspace_final_pdf,
        candidate_audit.get("pdf"),
    ])
    raw_tex_path = _first_existing_paper_file(root, [
        paper_state.get("latest_generated_tex_path"),
        paper_state.get("paper_orchestra_final_tex"),
        output_dir / "writing_raw.tex",
        workspace_final_tex,
        candidate_audit.get("tex"),
    ])
    pdf_path = accepted_pdf_path
    tex_path = accepted_tex_path or output_tex_path or blocked_tex_path or raw_tex_path or (output_dir / "paper.tex")
    latest_pdf_path = accepted_pdf_path or blocked_pdf_path or raw_pdf_path
    latest_tex_path = accepted_tex_path or blocked_tex_path or raw_tex_path
    policy = paper_state.get("venue_submission_policy") if isinstance(paper_state.get("venue_submission_policy"), dict) else {}
    blockers = paper_state.get("conference_preview_blockers") if isinstance(paper_state.get("conference_preview_blockers"), list) else []
    self_review_blockers = paper_state.get("paper_self_review_blockers") if isinstance(paper_state.get("paper_self_review_blockers"), list) else []
    self_review_evidence_blockers = paper_state.get("paper_self_review_evidence_blockers") if isinstance(paper_state.get("paper_self_review_evidence_blockers"), list) else []
    raw_warnings = paper_state.get("paper_layout_footprint_warnings") if isinstance(paper_state.get("paper_layout_footprint_warnings"), list) else []
    warnings = [item for item in (_paper_public_layout_warning_text(value) for value in raw_warnings) if item]
    first = blockers[0] if blockers else ""
    raw_blocker_text = str(first.get("public_detail") or first.get("detail") or first.get("id") or "") if isinstance(first, dict) else str(first or "")
    blocker_text = _paper_public_blocker_text(raw_blocker_text)
    content_blocker_text = _paper_content_blocker_summary(root, paper_state)
    if not blocker_text and content_blocker_text:
        blocker_text = content_blocker_text
    content_policy_blocked = _paper_content_policy_blocked({**paper_state, "status": paper_state.get("paper_stage_status") or paper_state.get("status")})
    body_pages = _paper_int(paper_state.get("conference_preview_body_pages") or paper_state.get("paper_normality_body_pages"))
    body_limit = _paper_int(policy.get("body_page_max") or venue_requirements.get("body_page_max"))
    citation_count = _paper_int(paper_state.get("paper_normality_citation_count"))
    citation_target = _paper_int(
        paper_state.get("paper_normality_reference_target")
        or paper_state.get("paper_reference_quality_target")
        or policy.get("reference_quality_target")
        or policy.get("reference_quality_target")
        or policy.get("official_min_references")
        or policy.get("min_references")
    )
    citation_target_source = str(paper_state.get("paper_normality_reference_target_source") or policy.get("reference_target_source") or "").strip()
    diagnostics: list[str] = []
    if body_pages and body_limit:
        labels = _paper_venue_labels(paper_state if isinstance(paper_state, dict) else {})
        diagnostics.append(f"正文页数 {body_pages}/{body_limit}，符合当前{labels['venue_zh']}正文页数要求。" if body_pages <= body_limit else f"正文页数 {body_pages}/{body_limit}，需先定位图表、表格和参考文献占页来源。")
    if citation_count and citation_target:
        label = "官方引用要求" if citation_target_source == "official" else "写作引用质量目标"
        diagnostics.append(f"{label} {citation_count}/{citation_target}。")
    if warnings:
        diagnostics.append(f"图表版面有 {len(warnings)} 项提示，优先处理图表占地和单栏适配。")
    if blocker_text:
        if "reference_count" in blocker_text or "reference_quality_target" in blocker_text or "references/citation" in blocker_text:
            diagnostics.append("当前 写作引用质量目标未达：参考文献覆盖不足，需要补充真实且相关的已验证引用。")
        else:
            diagnostics.append("预览仍需完善：" + blocker_text)
    if self_review_blockers or str(paper_state.get("paper_self_review_status") or "").strip().lower() == "block":
        diagnostics.append("论文自审未通过；具体修复项已交由 Writing 主控 Claude 处理。")
    if self_review_evidence_blockers:
        diagnostics.append(f"Claude Code 独立审稿发现 {len(self_review_evidence_blockers)} 项未解决科研证据问题；PDF 只能作为检查预览，不能标记为投稿通过。")
    if pdf_path and paper_state.get("conference_preview_ready"):
        paper_status = "preview_available"
    elif content_policy_blocked:
        paper_status = "blocked_content_policy"
    elif blocked_pdf_path:
        paper_status = "preview_available"
    elif preview_gate_blocked and (latest_pdf_path or output_pdf_path):
        paper_status = "preview_available"
    elif preview_gate_blocked:
        paper_status = "preview_pdf_blocked"
    elif raw_pdf_path:
        paper_status = "blocked"
    else:
        paper_status = "needs_writing" if blockers or not paper_state.get("conference_preview_ready") else str(paper_state.get("status") or "preview_available")
    row = {
        "status": paper_status,
        "venue": str(paper_state.get("venue") or venue_raw or ""),
        "target_venue": str(paper_state.get("target_venue") or venue_raw or paper_state.get("venue") or ""),
        "venue_slug": venue,
        "template_family": str(policy.get("template_family") or venue_requirements.get("template_family") or paper_state.get("template_family") or ""),
        "paper_normality_status": paper_state.get("paper_normality_status", ""),
        "paper_venue_format_status": paper_state.get("paper_venue_format_status", ""),
        "paper_figure_quality_status": paper_state.get("paper_figure_quality_status", ""),
        "paper_normality_citation_count": citation_count or paper_state.get("paper_normality_citation_count", ""),
        "paper_normality_citation_target": citation_target,
        "paper_normality_reference_target_source": citation_target_source,
        "paper_normality_pages": paper_state.get("paper_normality_pages") or paper_state.get("conference_preview_pages", ""),
        "paper_normality_body_pages": body_pages or paper_state.get("paper_normality_body_pages", ""),
        "paper_normality_estimated_reference_pages": paper_state.get("paper_normality_estimated_reference_pages") or paper_state.get("conference_preview_reference_pages", ""),
        "normal_preview_ready": bool(paper_state.get("normal_preview_ready") or paper_state.get("paper_normality_ready")),
        "paper_reference_quality_target": paper_state.get("paper_reference_quality_target", ""),
        "paper_reference_official_min": paper_state.get("paper_reference_official_min", ""),
        "paper_citation_render_status": paper_state.get("paper_citation_render_status", ""),
        "paper_citation_render_ready": bool(paper_state.get("paper_citation_render_ready") or paper_state.get("paper_citation_render_status") == "pass"),
        "paper_citation_render_blockers": [],
        "paper_self_review_status": paper_state.get("paper_self_review_status", ""),
        "paper_self_review_ready": bool(paper_state.get("paper_self_review_ready")),
        "paper_self_review_receipt": paper_state.get("paper_self_review_receipt", ""),
        "paper_self_review_blockers": [],
        "paper_self_review_evidence_blockers": [],
        "paper_self_review_evidence_blocker_count": int(paper_state.get("paper_self_review_evidence_blocker_count") or len(self_review_evidence_blockers) or 0),
        "paper_self_review_preview_only_ready": bool(paper_state.get("paper_self_review_preview_only_ready")),
        "paper_self_review_submission_evidence_ready": bool(paper_state.get("paper_self_review_submission_evidence_ready")),
        "paper_self_review_independent_findings_count": paper_state.get("paper_self_review_independent_findings_count", 0),
        "paper_self_review_repairs_count": paper_state.get("paper_self_review_repairs_count", 0),
        "conference_preview_ready": bool(paper_state.get("conference_preview_ready")),
        "conference_preview_pages": paper_state.get("conference_preview_pages", ""),
        "conference_preview_body_pages": body_pages or paper_state.get("conference_preview_body_pages", ""),
        "conference_preview_body_page_limit": body_limit,
        "conference_preview_reference_pages": paper_state.get("conference_preview_reference_pages", ""),
        "conference_preview_blocker_summary": blocker_text,
        "conference_preview_blockers": [],
        "paper_content_policy_status": paper_state.get("paper_content_policy_status", ""),
        "paper_content_blocker_summary": content_blocker_text,
        "paper_stage_status": paper_state.get("paper_stage_status", ""),
        "paper_layout_summary": str(warnings[0]) if warnings else "",
        "paper_layout_footprint_warnings": warnings[:8],
        "paper_public_diagnostics": diagnostics,
        "paper_preview_repair_loop_status": "blocked" if not paper_state.get("conference_preview_ready") else paper_state.get("paper_preview_repair_loop_status", ""),
        "paper_preview_repair_rounds": paper_state.get("paper_preview_repair_rounds", ""),
        "paper_current_regeneration_requested": bool(paper_state.get("paper_current_regeneration_requested")),
        "venue_requirements_status": paper_state.get("venue_requirements_status", "") or venue_requirements.get("status", ""),
        "venue_requirements_path": paper_state.get("venue_requirements_path", "") or venue_requirements.get("path", ""),
        "venue_requirements_summary": venue_requirements,
        "venue_requirements_public_summary": venue_requirements.get("summary", ""),
        "pdf_ready": bool(pdf_path),
        "pdf_path": str(pdf_path) if pdf_path else "",
        "blocked_preview_available": bool(blocked_pdf_path),
        "blocked_pdf_path": str(blocked_pdf_path) if blocked_pdf_path else "",
        "blocked_tex_path": str(blocked_tex_path) if blocked_tex_path else "",
        "latest_generated_pdf_path": str(latest_pdf_path) if latest_pdf_path else "",
        "latest_generated_tex_path": str(latest_tex_path) if latest_tex_path else "",
        "raw_pdf_path": str(raw_pdf_path) if raw_pdf_path else "",
        "raw_tex_path": str(raw_tex_path) if raw_tex_path else "",
        "venue_submission_policy": policy,
    }
    row.update(_paper_execution_projection(row))
    row["summary"] = _paper_stage_job_message(row)
    return row


def _paper_stage_from_job_result(result: dict[str, Any]) -> dict[str, Any]:
    project = str(result.get("project") or "")
    snapshot = _paper_stage_from_project_snapshot(project) if project else {}
    direct = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}
    if snapshot:
        merged = dict(direct)
        merged.update(snapshot)
        if _paper_result_has_live_execution(result):
            for key, value in _paper_execution_projection(result).items():
                if key in _PAPER_EXECUTION_KEYS:
                    merged[key] = value
        return merged
    if direct:
        if _paper_result_has_live_execution(result):
            merged = dict(direct)
            merged.update(_paper_execution_projection(result))
            return merged
        return direct
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    stages = summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
    nested = stages.get("paper") if isinstance(stages.get("paper"), dict) else {}
    if isinstance(nested, dict) and nested:
        return nested
    return {}


def _paper_stage_job_message(row: dict[str, Any]) -> str:
    policy = row.get("venue_submission_policy") if isinstance(row.get("venue_submission_policy"), dict) else {}
    parts: list[str] = []
    content_policy_blocked = _paper_content_policy_blocked(row)
    content_blocker = str(row.get("paper_content_blocker_summary") or "").strip()
    if content_policy_blocked and (row.get("latest_generated_pdf_path") or row.get("raw_pdf_path") or row.get("blocked_preview_available")):
        parts.append("候选稿已生成但内容策略未通过")
    elif row.get("blocked_preview_available") or row.get("raw_pdf_path") or row.get("pdf_path"):
        parts.append(_paper_venue_labels(row).get("preview_zh", "会议格式论文预览") + "已生成")
    elif row.get("latest_generated_pdf_path"):
        parts.append(_paper_venue_labels(row).get("preview_zh", "会议格式论文预览") + "有最近产物")
    elif _paper_preview_gate_blocked_status(row):
        parts.append(_paper_venue_labels(row).get("preview_zh", "会议格式论文预览") + "预览门控未通过")
    citation_count = _paper_int(row.get("paper_normality_citation_count"))
    citation_target = _paper_int(
        row.get("paper_normality_citation_target")
        or row.get("paper_reference_quality_target")
        or policy.get("reference_quality_target")
        or policy.get("reference_quality_target")
        or policy.get("official_min_references")
        or policy.get("min_references")
    )
    citation_target_source = str(row.get("paper_normality_reference_target_source") or policy.get("reference_target_source") or "").strip()
    body_pages = _paper_int(row.get("conference_preview_body_pages"))
    body_limit = _paper_int(row.get("conference_preview_body_page_limit") or policy.get("body_page_max"))
    if body_pages and body_limit:
        parts.append(f"正文页数 {body_pages}/{body_limit}")
    elif body_pages:
        parts.append(f"正文页数 {body_pages}")
    if citation_count and citation_target:
        label = "官方引用要求" if citation_target_source == "official" else "写作引用质量目标"
        parts.append(f"{label} {citation_count}/{citation_target}")
    elif citation_count:
        parts.append(f"参考文献数量 {citation_count}")
    warnings = row.get("paper_layout_footprint_warnings") if isinstance(row.get("paper_layout_footprint_warnings"), list) else []
    if warnings:
        parts.append(f"图表版面提示 {len(warnings)} 项，优先处理图表占地")
    if body_pages and body_limit and body_pages <= body_limit and (warnings or (citation_count and citation_target and citation_count < citation_target)):
        parts.append("正文页数已符合" + _paper_venue_labels(row).get("requirement_zh", "会议要求") + "，后续重点是图表占地、真实引用覆盖和模板细节")
    blocker = str(row.get("conference_preview_blocker_summary") or "").strip()
    if content_policy_blocked and content_blocker and content_blocker not in "；".join(parts):
        parts.append("候选稿内容策略未通过：" + content_blocker)
    elif blocker:
        if "reference_count" in blocker or "reference_quality_target" in blocker or "references/citation" in blocker:
            parts.append("写作质量目标未达：参考文献覆盖不足")
        elif "参考文献覆盖不足" in blocker:
            parts.append("写作质量目标未达：参考文献覆盖不足")
        else:
            parts.append("预览仍需完善：" + blocker)
    self_review_blockers = row.get("paper_self_review_blockers") if isinstance(row.get("paper_self_review_blockers"), list) else []
    self_review_status = str(row.get("paper_self_review_status") or "").strip().lower()
    if self_review_blockers or self_review_status == "block":
        parts.append("论文自审未通过，具体修复项已交由 Writing 主控 Claude 处理")
    self_review_evidence_blockers = row.get("paper_self_review_evidence_blockers") if isinstance(row.get("paper_self_review_evidence_blockers"), list) else []
    if self_review_evidence_blockers:
        parts.append(f"论文自审发现 {len(self_review_evidence_blockers)} 项未解决科研证据问题，预览不能标记为投稿通过")
    if not row.get("conference_preview_ready") or str(row.get("status") or "").startswith("blocked") or _paper_preview_artifact_available(row):
        parts.append("投稿/证据门控仍按真实状态保留，不标记为投稿通过")
    return "；".join(parts) + ("。" if parts else "")


def _compact_job_result(result: Any, stage: Any = "", job_id: Any = "", logs: Any = None) -> Any:
    if not isinstance(result, dict):
        return result
    compact = {"run_id": result.get("run_id")}
    paper_stage = _paper_stage_from_job_result(result) if _is_paper_job(stage, job_id, result, logs) else {}
    if paper_stage:
        paper_keys = [
            "status", "venue", "target_venue", "venue_slug", "template_family", "paper_normality_status", "paper_venue_format_status",
            "paper_figure_quality_status", "paper_normality_citation_count",
            "paper_normality_citation_target", "paper_normality_reference_target_source",
            "paper_normality_pages", "paper_normality_body_pages", "paper_normality_estimated_reference_pages",
            "paper_reference_quality_target", "paper_reference_official_min", "paper_citation_render_status", "paper_citation_render_ready", "paper_citation_render_blockers", "paper_self_review_status", "paper_self_review_ready", "paper_self_review_receipt", "paper_self_review_blockers", "paper_self_review_evidence_blockers", "paper_self_review_evidence_blocker_count", "paper_self_review_preview_only_ready", "paper_self_review_submission_evidence_ready", "paper_self_review_independent_findings_count", "paper_self_review_repairs_count", "conference_preview_ready",
            "conference_preview_pages", "conference_preview_body_pages",
            "conference_preview_body_page_limit", "conference_preview_reference_pages",
            "conference_preview_blocker_summary", "paper_layout_summary",
            "paper_public_diagnostics", "paper_layout_footprint_warnings",
            "conference_preview_blockers", "venue_requirements_status",
            "venue_requirements_path", "venue_requirements_summary", "venue_requirements_public_summary", "blocked_preview_available", "blocked_pdf_path",
            "blocked_tex_path", "latest_generated_pdf_path", "latest_generated_tex_path", "raw_pdf_path", "raw_tex_path", "paper_content_policy_status", "paper_content_blocker_summary", "paper_stage_status", "paper_execution_alive", "paper_execution_state", "paper_execution_message",
            "paper_current_regeneration_requested", "paper_preview_repair_loop_status", "paper_preview_repair_rounds", "pdf_path", "tex_path",
        ]
        for key in paper_keys:
            if key in paper_stage:
                compact[key] = paper_stage.get(key)
        compact["paper_stage"] = {key: compact[key] for key in paper_keys if key in compact}
        compact["paper_summary"] = str(paper_stage.get("summary") or _paper_stage_job_message(paper_stage))
    for key in [
        "project",
        "topic",
        "status",
        "action",
        "agent_id",
        "target_agent_id",
        "requested_stage",
        "panel_stage",
        "target_venue",
        "pid",
        "cmd",
        "kind",
        "log_path",
        "artifact_dir",
        "scoring_policy_version",
        "created_at",
        "diagnostics",
        "artifact_semantics",
        "scoring_runtime",
        "survey_stats",
        "paper_status",
        "paper_summary",
        "paper_stage",
        "paper_normality_status",
        "paper_venue_format_status",
        "paper_figure_quality_status",
        "paper_normality_citation_count",
        "paper_normality_citation_target",
        "paper_normality_reference_target_source",
        "paper_reference_quality_target",
        "paper_reference_official_min",
        "paper_citation_render_status",
        "paper_citation_render_ready",
        "paper_citation_render_blockers",
        "paper_self_review_status",
        "paper_self_review_ready",
        "paper_self_review_receipt",
        "paper_self_review_blockers",
        "paper_self_review_evidence_blockers",
        "paper_self_review_evidence_blocker_count",
        "paper_self_review_preview_only_ready",
        "paper_self_review_submission_evidence_ready",
        "paper_self_review_independent_findings_count",
        "paper_self_review_repairs_count",
        "conference_preview_ready",
        "conference_preview_pages",
        "conference_preview_body_pages",
        "conference_preview_body_page_limit",
        "conference_preview_reference_pages",
        "conference_preview_blocker_summary",
        "paper_layout_summary",
        "paper_public_diagnostics",
        "paper_layout_footprint_warnings",
        "conference_preview_blockers",
        "venue_requirements_status",
        "venue_requirements_path",
        "blocked_preview_available",
        "blocked_pdf_path",
        "blocked_tex_path",
        "latest_generated_pdf_path",
        "latest_generated_tex_path",
        "raw_pdf_path",
        "raw_tex_path",
        "paper_content_policy_status",
        "paper_content_blocker_summary",
        "paper_stage_status",
        "pdf_path",
        "tex_path",
    ]:
        if key in result:
            compact[key] = result.get(key)
    counts = {}
    for key in [
        "raw_title_index",
        "retrieval_candidates",
        "title_candidates",
        "evaluated_candidates",
        "screened_ranking",
        "strong_recommendations",
        "read_candidates",
        "triage_candidates",
        "audit_candidates",
        "critique_candidates",
        "articles",
        "huggingface",
        "github",
        "source_status",
        "venue_health_report",
        "category_scan_report",
        "title_filter_report",
        "arxiv_raw",
        "arxiv_prefiltered",
        "biorxiv_raw",
        "biorxiv_prefiltered",
        "nature_raw",
        "nature_prefiltered",
        "science_raw",
        "science_prefiltered",
    ]:
        count = _compact_count(result.get(key))
        if count is not None:
            counts[key] = count
    if paper_stage:
        for key in paper_keys:
            if key in paper_stage:
                compact[key] = paper_stage.get(key)
        diagnostics = compact.get("paper_public_diagnostics") if isinstance(compact.get("paper_public_diagnostics"), list) else []
        compact["paper_public_diagnostics"] = [
            ("当前 写作引用质量目标未达：参考文献覆盖不足，需要补充真实且相关的已验证引用。"
             if ("当前格式阻塞" in str(item) and ("reference_count" in str(item) or "reference_quality_target" in str(item) or "references/citation" in str(item))) else item)
            for item in diagnostics
        ]
        for hidden_key in ("paper_citation_render_blockers", "paper_self_review_blockers", "paper_self_review_evidence_blockers", "conference_preview_blockers"):
            compact[hidden_key] = []
        compact["paper_summary"] = _paper_stage_job_message(compact)
        compact["paper_stage"] = {key: compact[key] for key in paper_keys if key in compact}
    if counts:
        compact["artifact_counts"] = counts
    return compact


def _public_paper_command(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return (
        text.replace("--force-refresh", "--refresh-current-paper")
        .replace("--force-venue-refresh", "--refresh-current-venue")
        .replace("--force-template", "--generate-paper-preview")
        .replace("--generate-inspection-paper", "--generate-paper-preview")
    )




def _public_paper_progress_message(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if text in {"{", "}", "[", "]"}:
        return ""
    if text.startswith(("\"", "'")) and ("/home/" in text or text.rstrip(",").endswith((".json\"", ".md\"", ".tex\"", ".pdf\"", ".log\""))):
        return "writing 正在刷新论文产物与审计状态。"
    if text.startswith("/") and text.endswith((".json", ".md", ".tex", ".pdf", ".log")):
        return "writing 正在刷新论文产物与审计状态。"
    if any(token in lowered for token in ["--generate-paper-preview", "--refresh-current-paper"]):
        return "writing 正在生成/修订当前稿件预览；证据门控保持真实状态。"
    if any(token in lowered for token in ["--force-template", "--force-refresh", "--generate-inspection-paper"]):
        return "writing 正在生成/修订当前稿件预览；证据门控保持真实状态。"
    if any(marker in lowered for marker in ["inspection draft", "blocked/non-submission", "candidate_observation_only", "unsupported or negative"]):
        return "writing 正在生成/修订当前稿件预览；证据门控保持真实状态。"
    return _public_paper_command(text)[:180]


HANDOFF_EXPERIMENT_NEXT_ACTION = "使用 handoff repo/env 进入 experimenting，运行真实评估并绑定 designability、scRMSD、pLDDT、TM-score 等论文指标；未完成前不提升论文结论。"


def _public_full_cycle_job_logs(logs: Any, progress: Any = None, result: Any = None, *, limit: int = 40) -> list[str]:
    raw = [str(line or "").strip() for line in (logs if isinstance(logs, list) else []) if str(line or "").strip()]
    progress = progress if isinstance(progress, dict) else {}
    result = result if isinstance(result, dict) else {}
    out: list[str] = []
    project = str(result.get("project") or "").strip()
    if project:
        out.append("project=" + project)
    status = str(result.get("status") or progress.get("phase") or "").strip()
    handoff_ready = status == "ready_for_experimenting" or "环境已交付" in str(result.get("summary") or progress.get("message") or "")
    process_alive = result.get("process_alive")
    if process_alive is not None:
        out.append("process_alive=" + str(bool(process_alive)).lower())
    message = str(progress.get("message") or result.get("summary") or "").strip()
    if message:
        if result.get("process_alive") is not True and any(marker in message.lower() for marker in ["gate=", "候选路线", "独立授权", "base_switch", "selected_base", "deterministic"]):
            message = "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"
        elif "正在运行" in message and "没有正在运行" not in message and result.get("process_alive") is not True:
            message = "历史 full-cycle 启动器已停止；当前状态以项目门控摘要为准。"
        cleaned_message = _public_text(message)
        if cleaned_message:
            out.append("当前状态：" + cleaned_message[:500])
    claude_activity = result.get("claude_activity") if isinstance(result.get("claude_activity"), dict) else {}
    activity_summary = str(claude_activity.get("summary") or "").strip()
    if activity_summary:
        out.append(_public_text(activity_summary)[:700])
    recent_activity = claude_activity.get("recent") if isinstance(claude_activity.get("recent"), list) else []
    for item in recent_activity[-3:]:
        text = _public_text(str(item or "").strip())
        if text and text != activity_summary:
            out.append("Claude 最近动作：" + text[:650])
    if status:
        out.append("阶段状态：" + status)
    if handoff_ready:
        out.append("当前目标：" + HANDOFF_EXPERIMENT_NEXT_ACTION)
        out.append("下一步：" + HANDOFF_EXPERIMENT_NEXT_ACTION)
    current_summary = str(result.get("summary") or "").strip()
    for line in raw:
        low = line.lower()
        if result.get("process_alive") is not True and current_summary and (line.startswith("summary=") or line.startswith("门控阻塞：")) and current_summary not in line:
            continue
        if line.startswith("Workflow command:"):
            out.append("命令：" + _public_paper_command(line.split(":", 1)[1]))
            continue
        if line.startswith("Runtime PATH head:"):
            out.append("运行环境 PATH 前缀：" + line.split(":", 1)[1].strip())
            continue
        if "detached full-cycle worker stopped" in low or "detached full-cycle worker is no longer running" in low:
            out.append("历史 full-cycle 启动器已停止；当前项目门控状态见上方摘要。")
            continue
        if "marked interrupted after server restart" in low or "reclassified stale" in low:
            out.append("服务重启前的旧任务已停止；不是当前运行错误。")
            continue
        if line.startswith("summary="):
            summary = line.split("=", 1)[1].strip()
            if result.get("process_alive") is not True and any(marker in summary.lower() for marker in ["候选路线", "独立授权", "base_switch", "selected_base", "deterministic"]):
                continue
            if "正在运行" in summary and result.get("process_alive") is not True:
                continue
            cleaned_summary = _public_text(summary)
            if cleaned_summary:
                out.append("summary=" + cleaned_summary[:500])
            continue
        if line.startswith("log=") or line.startswith("cmd=") or line.startswith("artifact="):
            if line.startswith("artifact=") or line.startswith("cmd="):
                continue
            out.append(line[:900])
            continue
        if line.startswith("门控阻塞：") or line.startswith("下一步：") or line.startswith("当前目标：") or line.startswith("当前阶段："):
            if handoff_ready and (line.startswith("下一步：") or line.startswith("当前目标：")):
                continue
            if result.get("process_alive") is not True and any(marker in line.lower() for marker in ["候选路线", "独立授权", "base_switch", "selected_base", "deterministic"]):
                continue
            cleaned_line = _public_text(line)
            if cleaned_line:
                out.append(cleaned_line[:700])
            continue
    for key, label in [("log_path", "日志"), ("command", "命令"), ("cmd", "命令")]:
        value = str(result.get(key) or "").strip()
        if not value:
            continue
        if label == "命令" and len(value) > 180:
            out.append("命令：已记录，完整命令保留在后端任务审计。")
        else:
            out.append(f"{label}：{value}")
    if not out:
        out = ["full-cycle 历史任务已记录；当前状态以项目实时门控摘要为准。"]
    dedup: list[str] = []
    seen: set[str] = set()
    for line in out:
        if line in seen:
            continue
        seen.add(line)
        dedup.append(line)
    return dedup[-limit:]


def _public_stage_label(stage: Any) -> str:
    public_stage = _public_taste_stage(stage)
    if public_stage == "environment":
        return "环境配置"
    if public_stage == "experiment":
        return "实验迭代"
    return public_stage


def _public_stage_command_message(stage: Any, value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text:
        return ""
    if (
        text.startswith("$")
        or lowered.startswith("workflow command:")
        or "bin/python" in lowered
        or " scripts/" in lowered
        or "claude -p" in lowered
        or lowered.startswith("runtime path head:")
    ):
        return f"{_public_stage_label(stage)}正在运行阶段审计命令，完整命令保留在后端任务审计。"
    return ""


def _public_module_controller_progress_message(stage: Any, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    command = _public_stage_command_message(stage, text)
    if command:
        return command
    lowered = text.lower()
    stage_label = _public_stage_label(stage)
    if lowered.startswith("running environment-chat") or lowered.startswith("environment-chat started"):
        return "Environment 主控 Claude 正在处理环境配置请求。"
    if lowered.startswith("running claude-message") or lowered.startswith("claude-message started"):
        return f"模块主控 Claude 正在处理{stage_label}请求。"
    if lowered.startswith("claude: executable=") or lowered.startswith("claude: permission_mode") or lowered.startswith("claude: session_key="):
        return f"模块主控 Claude 会话已启动，正在处理{stage_label}请求。"
    if "调用工具" in text or "tool use" in lowered or "read file=" in lowered or "edit file=" in lowered or "bash command=" in lowered:
        return f"模块主控 Claude 正在读取/修改当前项目证据以处理{stage_label}门控。"
    text = _redact_public_log_text(_public_text(text))
    text = re.sub(r'/[^\s;,\"\']*/(?:workspace|TASTE|projects|runtime|\.nvm|miniforge)[^\s;,\"\']*', '[local-path]', text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220]


def _public_stage_job_logs(stage: Any, logs: Any, progress: Any = None, result: Any = None, *, limit: int = 8) -> list[str]:
    """Compact environment/experiment taskbar logs without exposing raw commands."""
    raw = [str(line or "").strip() for line in (logs if isinstance(logs, list) else []) if str(line or "").strip()]
    progress = progress if isinstance(progress, dict) else {}
    result = result if isinstance(result, dict) else {}
    public_stage = _public_taste_stage(stage)
    stage_label = _public_stage_label(public_stage)
    public_prefixes = ("当前状态：", "阶段摘要：", "门控：", "审计进展：", "详细日志：", "产物：", "日志：")

    def strip_public_prefixes(value: Any) -> str:
        text = str(value or "").strip()
        changed = True
        while changed:
            changed = False
            for prefix in public_prefixes:
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    changed = True
        return text

    if raw and all(any(line.startswith(prefix) for prefix in public_prefixes) for line in raw) and not (progress.get("message") or result.get("summary")):
        deduped: list[str] = []
        seen_public: set[str] = set()
        for line in raw:
            if line.startswith("当前状态："):
                replacement = _public_stage_command_message(public_stage, line.removeprefix("当前状态："))
                if replacement:
                    line = "当前状态：" + replacement
            if line in seen_public:
                continue
            seen_public.add(line)
            deduped.append(line)
        return deduped[-max(1, min(limit, 8)):]

    def clean(value: Any, max_len: int = 220) -> str:
        text = str(_strip_public_taste_marker(value or "")).strip()
        if not text:
            return ""
        text = re.sub(r"(?i)(api[_-]?key|authorization|bearer|token)(\s*[:=]\s*)[^\s,'\"]+", r"\1\2***", text)
        text = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})", "sk-***", text)
        text = re.sub(r"/[^\s;,'\"]*/(?:miniforge|workspace|\.nvm)[^\s;,'\"]*", "[local-path]", text)
        text = re.sub(r"\s+", " ", text)
        return text[: max_len - 1].rstrip() + "…" if len(text) > max_len else text

    out: list[str] = []
    seen: set[str] = set()

    def add(prefix: str, value: Any, max_len: int = 220) -> None:
        text = clean(value, max_len=max_len)
        if not text:
            return
        line = f"{prefix}{text}" if prefix else text
        if line in seen:
            return
        seen.add(line)
        out.append(line)

    status = result.get("status") or progress.get("phase")
    message = progress.get("message")
    if message:
        add("当前状态：", _public_stage_command_message(public_stage, message) or message)
    elif status:
        add("当前状态：", status)

    summary = result.get("summary")
    if isinstance(summary, dict):
        add("阶段摘要：", summary.get("progress_summary") or summary.get("summary") or summary.get("status"), max_len=260)
        blocker = summary.get("current_blocker") if isinstance(summary.get("current_blocker"), dict) else {}
        add("门控：", blocker.get("human_summary") or blocker.get("summary") or blocker.get("issue"), max_len=260)
    else:
        add("阶段摘要：", summary, max_len=260)

    for key, label in [("artifact_dir", "产物"), ("log_path", "日志")]:
        if result.get(key):
            add(f"{label}：", "已记录，完整内容保留在后端任务审计。")

    if raw:
        checkpoints: list[str] = []
        for line in raw:
            line_text = strip_public_prefixes(line)
            lowered = line_text.lower()
            if line_text.startswith("$") or "bin/python" in lowered or "claude -p" in lowered or lowered.startswith("workflow command:") or lowered.startswith("runtime path head:"):
                continue
            if "traceback" in lowered or 'file "' in lowered:
                continue
            if "optional command failed" in lowered:
                checkpoints.append("有可恢复的候选审计命令失败；当前门控仍按阶段摘要展示。")
                continue
            if "environment blocked" in lowered:
                checkpoints.append("环境配置停在真实门控阻塞状态。")
                continue
            if "experiment blocked" in lowered:
                checkpoints.append("实验迭代停在真实门控阻塞状态。")
                continue
            if "selected_active_repo=none" in lowered:
                checkpoints.append("未选择可审计基底仓库。")
                continue
            if "repo_search_running" in lowered or "audit complete" in lowered or "ready=0" in lowered or "ingested=" in lowered:
                checkpoints.append(clean(line_text, max_len=180))
                continue
        for line in checkpoints[-3:]:
            add("审计进展：", line, max_len=220)
        add("详细日志：", f"已保留 {len(raw)} 行原始日志；任务栏只显示当前摘要。")
    elif not out:
        add("当前状态：", f"{stage_label}任务已记录。")

    return out[-max(1, limit):]



def _public_task_error_detail(logs: list[str], progress: dict[str, Any], result: dict[str, Any]) -> str:
    """Select one concrete, redacted exception for the public taskbar."""
    states = [progress.get("phase"), result.get("status")]
    if not any(str(value or "").strip().lower() in {"error", "failed", "fail"} for value in states):
        return ""
    for raw_line in reversed([line for raw in logs for line in str(raw or "").splitlines()]):
        detail = _redact_public_log_text(_public_text(raw_line)).strip()
        if detail.startswith("错误详情："):
            return detail[:505]
        if not re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):\s+.+$", detail):
            continue
        if re.search(
            r"(?:research action|subprocess|task|framework\s+(?:read|ideation|planning))\s+failed with exit code",
            detail,
            flags=re.I,
        ):
            continue
        detail = re.sub(r"(?<!:)/(?:zssd|home|tmp|var/tmp)(?:/[^\s,;'\"]+)+", "[local-path]", detail)
        return "错误详情：" + detail[:500]
    return ""


def _with_public_task_error_detail(
    lines: list[str],
    logs: list[str],
    progress: dict[str, Any],
    result: dict[str, Any],
    *,
    limit: int,
) -> list[str]:
    detail = _public_task_error_detail(logs, progress, result)
    if not detail or detail in lines:
        return lines
    out = list(lines)
    insert_at = next((index + 1 for index, line in enumerate(out) if line.startswith("运行编号：")), min(1, len(out)))
    out.insert(insert_at, detail)
    return out[:max(1, limit)]


def _public_find_job_logs(logs: list[str], progress: dict[str, Any], result: dict[str, Any], *, limit: int = 8) -> list[str]:
    out: list[str] = []
    phase = str(progress.get("phase") or result.get("phase") or "find").strip()
    message = _public_job_summary_text(progress.get("message") or phase or "Find")
    percent = progress.get("percent")
    current = progress.get("current")
    total = progress.get("total")
    if phase == "complete" or str(result.get("status") or "").lower() == "done":
        out.append("当前状态：Find 已完成。")
    elif message:
        progress_bits = []
        if current not in (None, "") and total not in (None, ""):
            progress_bits.append(f"{current}/{total}")
        if percent not in (None, ""):
            progress_bits.append(f"{percent}%")
        suffix = ("（" + "，".join(progress_bits) + "）") if progress_bits else ""
        out.append("当前阶段：" + message + suffix)
    run_id = str(result.get("run_id") or "").strip()
    if run_id:
        out.append("运行编号：" + run_id)
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    stats = result.get("survey_stats") if isinstance(result.get("survey_stats"), dict) else diagnostics.get("survey_stats") if isinstance(diagnostics.get("survey_stats"), dict) else {}
    if isinstance(stats, dict) and stats:
        title_total = stats.get("title_total_papers") or stats.get("raw_title_index_papers")
        category_count = stats.get("category_filtered_papers")
        tfidf_count = stats.get("tfidf_screened_papers")
        title_scored = stats.get("llm_title_scored_papers")
        abstract_scored = stats.get("abstract_scored_papers") or stats.get("llm_scored_candidates")
        recommended = stats.get("recommended_papers")
        pieces = []
        if title_total not in (None, ""):
            pieces.append(f"题录入口 {title_total}")
        if category_count not in (None, ""):
            pieces.append(f"进入标题筛选 {category_count}")
        if tfidf_count not in (None, ""):
            pieces.append(f"进入标题 LLM {tfidf_count}")
        if title_scored not in (None, ""):
            pieces.append(f"标题 LLM 已评分 {title_scored}")
        if abstract_scored not in (None, ""):
            pieces.append(f"标题+摘要 LLM 已评分 {abstract_scored}")
        if recommended not in (None, ""):
            pieces.append(f"推荐 {recommended}")
        if pieces:
            out.append("阶段数量：" + " / ".join(pieces))
    quality = result.get("recommendation_quality") if isinstance(result.get("recommendation_quality"), dict) else diagnostics.get("recommendation_quality") if isinstance(diagnostics.get("recommendation_quality"), dict) else {}
    if isinstance(quality, dict) and quality.get("status") == "ok":
        count = quality.get("recommendation_count")
        out.append("推荐质量：已生成" + (str(count) if count not in (None, "") else "") + "篇，摘要和推荐理由检查通过。")
    if logs and not out:
        out.append(f"详细日志：已保留 {len(logs)} 行原始日志；任务栏只显示当前摘要。")
    return out[-max(1, limit):]


def _read_progress_percent(current: Any, total: Any) -> int:
    try:
        total_int = int(total or 0)
        current_int = int(current or 0)
    except (TypeError, ValueError):
        return 0
    if total_int <= 0:
        return 0
    return max(0, min(100, int(round((current_int / total_int) * 100))))


def _read_progress_title(value: Any, *, max_len: int = 180) -> str:
    text = _redact_public_log_text(_public_text(str(value or ""))).strip()
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text[: max_len - 1].rstrip() + "…" if len(text) > max_len else text


def _read_progress_status_label(value: Any, *, phase: str) -> str:
    text = _read_progress_title(value, max_len=180)
    lowered = text.lower()
    if not text:
        return ""
    if phase == "full_text":
        if "full_text=false" in lowered or lowered.endswith("false"):
            return "全文未就绪"
        if "full_text=true" in lowered or "prepared_full_text" in lowered or "verified_full_text" in lowered:
            return "全文可用"
    if phase == "deep_read":
        if "deep_read=true" in lowered or "complete" in lowered:
            return "精读完成"
        if "deep_read=false" in lowered or "missing" in lowered or "invalid" in lowered or "blocked" in lowered:
            return "精读未完成"
    return text


def _read_phase_row(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "current": 0,
        "total": 0,
        "percent": 0,
        "status": "pending",
        "workers": 0,
        "active_index": 0,
        "active_title": "",
        "last_index": 0,
        "last_title": "",
        "last_status": "",
    }


def _read_log_detail(prefix: str, index: int, total: int, title: str, extra: str = "", *, count_label: str = "") -> str:
    count = count_label or (f"{index}/{total}" if total else str(index or ""))
    pieces = [prefix, count]
    if title:
        pieces.append(title)
    if extra:
        pieces.append(extra)
    return "：".join([pieces[0], " ".join(piece for piece in pieces[1:] if piece)]) if len(pieces) > 1 else prefix


def _read_job_artifact_progress(result: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    project = str(result.get("project") or "").strip()
    run_id = str(result.get("run_id") or result.get("find_run_id") or "").strip()
    projection: dict[str, Any] = {}
    read_payload: dict[str, Any] = {}
    project_read_md_present = False
    if project:
        try:
            root = _safe_project_root(project)
        except Exception:
            root = None
        if root is not None:
            try:
                project_read_md_present = bool((root / "planning" / "finding" / "read.md").read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                project_read_md_present = False
            payload = _read_project_json(root / "planning" / "finding" / "read_results.json", {})
            if isinstance(payload, dict):
                payload_run_id = str(payload.get("source_run_id") or payload.get("run_id") or "").strip()
                if not run_id or not payload_run_id or payload_run_id == run_id:
                    read_payload = payload
                    projection["run_id"] = payload_run_id or run_id

    readings = read_payload.get("readings") if isinstance(read_payload.get("readings"), list) else []
    artifact_counts = result.get("artifact_counts") if isinstance(result.get("artifact_counts"), dict) else {}
    validation = read_payload.get("reading_validation") if isinstance(read_payload.get("reading_validation"), dict) else {}
    result_validation = result.get("reading_validation") if isinstance(result.get("reading_validation"), dict) else {}
    prepared_input = result.get("prepared_reading_input") if isinstance(result.get("prepared_reading_input"), dict) else {}

    def as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    result_readings_count = result.get("readings")
    if isinstance(result_readings_count, list):
        result_readings_count = len(result_readings_count)

    message = str(progress.get("message") or result.get("summary") or "")
    msg_match = re.search(
        r"生成\s*(\d+)\s*/\s*(\d+)\s*条\s*Reading\s*记录.*?全文证据\s*(\d+)\s*篇.*?待补全文\s*(\d+)\s*篇.*?待补\s*(?:Claude/)?subagent\s*深读\s*(\d+)\s*篇",
        message,
        flags=re.I,
    )
    message_read_current = message_read_total = message_full_current = message_pending_full = message_pending_deep = 0
    if msg_match:
        message_read_current = as_int(msg_match.group(1))
        message_read_total = as_int(msg_match.group(2))
        message_full_current = as_int(msg_match.group(3))
        message_pending_full = as_int(msg_match.group(4))
        message_pending_deep = as_int(msg_match.group(5))

    total = max(
        as_int(read_payload.get("selected_reading_count")),
        as_int(read_payload.get("expected_reading_count")),
        as_int(read_payload.get("processed_reading_count")),
        as_int(read_payload.get("input_article_count")),
        as_int(result.get("selected_reading_count")),
        as_int(result.get("expected_reading_count")),
        as_int(result.get("input_article_count")),
        as_int(prepared_input.get("selected_reading_count")),
        as_int(prepared_input.get("expected_reading_count")),
        as_int(prepared_input.get("input_article_count")),
        as_int(validation.get("expected_reading_count")),
        as_int(validation.get("selected_reading_count")),
        as_int(result_validation.get("expected_reading_count")),
        as_int(result_validation.get("selected_reading_count")),
        as_int(artifact_counts.get("recommended_reading")),
        as_int(artifact_counts.get("read")),
        len(readings),
        as_int(result_readings_count),
        message_read_total,
    )
    full_ready_from_rows = sum(
        1
        for row in readings
        if isinstance(row, dict)
        and (
            row.get("full_text_available") is True
            or row.get("pdf_text_read") is True
            or str(row.get("full_text_status") or "").strip() in {"pdf_text_read", "html_text_read", "xml_text_read", "full_text_ready"}
        )
    )
    full_current = max(
        as_int(read_payload.get("full_text_ready_count")),
        as_int(read_payload.get("full_text_reading_count")),
        as_int(result.get("full_text_ready_count")),
        as_int(result.get("full_text_reading_count")),
        as_int(validation.get("full_text_reading_count")),
        as_int(validation.get("full_text_evidence_count")),
        as_int(result_validation.get("full_text_reading_count")),
        as_int(result_validation.get("full_text_evidence_count")),
        as_int(artifact_counts.get("full_text_reading")),
        full_ready_from_rows,
        message_full_current,
    )
    result_pending_full = max(
        as_int(result.get("pending_full_text_reading_count")),
        as_int(validation.get("pending_full_text_reading_count")),
        as_int(result_validation.get("pending_full_text_reading_count")),
        message_pending_full,
    )
    if total and result_pending_full:
        full_current = max(full_current, total - result_pending_full)
    read_attempt_current = max(
        as_int(read_payload.get("deep_read_attempted_count")),
        len(readings),
        as_int(result_readings_count),
        message_read_current,
    )
    result_pending_deep = max(
        as_int(result.get("pending_deep_read_synthesis_count")),
        as_int(validation.get("pending_deep_read_synthesis_count")),
        as_int(result_validation.get("pending_deep_read_synthesis_count")),
        message_pending_deep,
    )
    deep_current = max(
        as_int(read_payload.get("deep_read_complete_count")),
        as_int(result.get("deep_read_complete_count")),
        as_int(validation.get("deep_read_complete_count")),
        as_int(result_validation.get("deep_read_complete_count")),
        sum(1 for row in readings if isinstance(row, dict) and row.get("deep_read_complete") is True),
    )
    if total and result_pending_deep:
        deep_current = max(deep_current, total - result_pending_deep)
    if total <= 0 and (full_current or read_attempt_current or deep_current):
        total = max(full_current, read_attempt_current, deep_current)
    if total <= 0:
        return {}

    validation_valid = validation.get("valid") is True or result_validation.get("valid") is True
    public_read_md_present = bool(read_payload.get("public_final_artifact_present") or project_read_md_present)
    warning_details = read_payload.get("warning_details") if isinstance(read_payload.get("warning_details"), list) else []
    error_details = read_payload.get("error_details") if isinstance(read_payload.get("error_details"), list) else []
    full_current = max(0, min(total, full_current))
    read_attempt_current = max(0, min(total, read_attempt_current))
    deep_current = max(0, min(total, deep_current))
    pending_full = max(0, total - full_current, result_pending_full)
    pending_deep = max(0, total - deep_current, result_pending_deep)
    projection.update({
        "total": total,
        "full_text_current": full_current,
        "deep_read_current": deep_current,
        "deep_read_attempted": read_attempt_current,
        "pending_full_text": pending_full,
        "pending_deep_read": pending_deep,
        "public_read_md_present": public_read_md_present,
        "validation_valid": validation_valid,
        "warning_count": max(as_int(read_payload.get("warning_count")), len(warning_details)),
        "error_count": max(as_int(read_payload.get("error_count")), len(error_details)),
    })
    return projection


def _read_job_project_read_payload(result: dict[str, Any]) -> dict[str, Any]:
    project = str(result.get("project") or "").strip()
    run_id = str(result.get("run_id") or result.get("find_run_id") or "").strip()
    if not project:
        return {}
    try:
        root = _safe_project_root(project)
    except Exception:
        return {}
    payload = _read_project_json(root / "planning" / "finding" / "read_results.json", {})
    if not isinstance(payload, dict):
        return {}
    payload_run_id = str(payload.get("source_run_id") or payload.get("run_id") or "").strip()
    if run_id and payload_run_id and payload_run_id != run_id:
        return {}
    return payload


def _read_job_machine_warning_lines(result: dict[str, Any], *, limit: int = 8) -> list[str]:
    payload = _read_job_project_read_payload(result)
    if not payload:
        return []
    validation = payload.get("reading_validation") if isinstance(payload.get("reading_validation"), dict) else {}
    aggregation = payload.get("read_markdown_aggregation") if isinstance(payload.get("read_markdown_aggregation"), dict) else {}
    details: list[dict[str, Any]] = []
    for source in [
        payload.get("error_details"),
        validation.get("error_details") if isinstance(validation, dict) else None,
        payload.get("warning_details"),
        validation.get("warning_details") if isinstance(validation, dict) else None,
        aggregation.get("warning_items") if isinstance(aggregation, dict) else None,
    ]:
        if isinstance(source, list):
            details.extend(item for item in source if isinstance(item, dict))
    out: list[str] = []
    seen: set[str] = set()
    for detail in details:
        title = _read_progress_title(detail.get("title") or "Untitled", max_len=140)
        phase_raw = _read_progress_title(detail.get("phase") or "read", max_len=80)
        phase = {
            "full_text_acquisition": "爬文章",
            "full_text": "爬文章",
            "read": "读文章",
            "deep_read": "读文章",
        }.get(phase_raw.lower(), phase_raw)
        status_raw = _read_progress_title(detail.get("status") or detail.get("message") or "", max_len=140)
        status = {
            "blocked_full_text_unavailable": "全文未就绪",
            "blocked_article_markdown_missing": "精读产物缺失",
            "blocked_deep_read_incomplete": "精读未完成",
        }.get(status_raw.lower(), status_raw)
        err_type = _read_progress_title(detail.get("error_type") or "", max_len=80)
        err_msg = _read_progress_title(detail.get("error_message") or "", max_len=180)
        prefix = "错误" if err_type or str(status).startswith("error_") else "警告"
        if status_raw.lower() == "blocked_full_text_unavailable":
            line = f"警告：{title} 的全文未就绪，因此未进入读文章阶段。"
        else:
            detail_text = err_msg or err_type or status or "未完成"
            line = f"{prefix}：{title} 在{phase}阶段{detail_text}。"
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= limit:
            break
    if len(out) < limit:
        warning_texts: list[str] = []
        for source in [payload.get("warnings"), validation.get("warnings") if isinstance(validation, dict) else None]:
            if isinstance(source, list):
                warning_texts.extend(str(item).strip() for item in source if str(item).strip())
        for warning in warning_texts:
            readable_warning = _read_progress_title(warning, max_len=260)
            readable_warning = readable_warning.replace("当前 Find 推荐", "论文")
            readable_warning = re.sub(r"；错误/警告只进入任务日志和\s*read_results\.json。?$", "。", readable_warning)
            text = "警告：" + readable_warning
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                break
    return out


def _read_job_progress_from_logs(
    logs: Any,
    progress: Any = None,
    result: Any = None,
    *,
    status: Any = "",
) -> dict[str, Any]:
    raw = [str(line or "").strip() for line in (logs if isinstance(logs, list) else []) if str(line or "").strip()]
    progress = progress if isinstance(progress, dict) else {}
    result = result if isinstance(result, dict) else {}
    existing = progress.get("read_progress") if isinstance(progress.get("read_progress"), dict) else result.get("read_progress") if isinstance(result.get("read_progress"), dict) else {}
    phases = {
        "full_text": _read_phase_row("爬文章"),
        "deep_read": _read_phase_row("读文章"),
    }
    details: list[str] = []
    errors: list[str] = []
    completed_full_text: set[int] = set()
    completed_deep_read: set[int] = set()
    exact_totals: set[str] = set()
    signal_seen = False

    def mark_total(key: str, total: int, workers: int = 0, *, exact: bool = False) -> None:
        if total > 0:
            if exact:
                phases[key]["total"] = total
                exact_totals.add(key)
            elif key not in exact_totals:
                phases[key]["total"] = max(int(phases[key].get("total") or 0), total)
        if workers > 0:
            phases[key]["workers"] = workers

    def mark_active(key: str, index: int, total: int, title: str, status_value: str) -> None:
        mark_total(key, total)
        phases[key]["active_index"] = index
        phases[key]["active_title"] = title
        phases[key]["status"] = status_value

    def mark_completed(key: str, completed: set[int], index: int, total: int, title: str, status_value: str) -> None:
        mark_total(key, total)
        completed.add(index)
        phases[key]["last_index"] = index
        phases[key]["last_title"] = title
        phases[key]["last_status"] = status_value
        if phases[key].get("active_index") == index:
            phases[key]["active_index"] = 0
            phases[key]["active_title"] = ""

    for line in raw:
        text = _strip_public_taste_marker(line)
        lowered = str(text).lower()
        if text == _job_status_message("read", "started") or text == "精读任务已启动":
            signal_seen = True
            phases["full_text"]["status"] = "running"
            continue
        full_phase = re.search(r"full[- ]text acquisition phase:\s*(\d+)\s+papers(?:,\s*(\d+)\s+workers)?", text, flags=re.I)
        if full_phase:
            signal_seen = True
            total = int(full_phase.group(1))
            workers = int(full_phase.group(2) or 0)
            mark_total("full_text", total, workers)
            phases["full_text"]["status"] = "running"
            startup_prefix = f"爬文章启动：共 {total} 篇"
            details = [item for item in details if not item.startswith(startup_prefix)]
            details.append(startup_prefix + (f"，并发 {workers}" if workers else ""))
            continue
        full_start = re.search(r"(acquiring|queueing)\s+full[- ]text(?:\s+acquisition)?\s+(\d+)\s*/\s*(\d+)\s*:\s*(.+)", text, flags=re.I)
        if full_start:
            signal_seen = True
            action = full_start.group(1).lower()
            index = int(full_start.group(2))
            total = int(full_start.group(3))
            title = _read_progress_title(full_start.group(4))
            if action == "queueing":
                mark_total("full_text", total)
            else:
                mark_active("full_text", index, total, title, "running")
            details.append(_read_log_detail("排队爬文章" if action == "queueing" else "正在爬文章", index, total, title))
            continue
        full_done = re.search(r"(?:finished|completed)\s+full[- ]text\s+acquisition\s+(\d+)\s*/\s*(\d+)\s*:\s*(.*?)(?:\s+-\s+(.+))?$", text, flags=re.I)
        if full_done:
            signal_seen = True
            index = int(full_done.group(1))
            total = int(full_done.group(2))
            raw_status_text = _read_progress_title(full_done.group(3), max_len=120)
            status_text = _read_progress_status_label(raw_status_text, phase="full_text")
            title = _read_progress_title(full_done.group(4) or "")
            mark_completed("full_text", completed_full_text, index, total, title, status_text)
            details.append(_read_log_detail("文章爬取完成", index, total, title, status_text))
            continue
        read_phase = re.search(r"reading subagent phase:\s*(\d+)\s+papers(?:,\s*(\d+)\s+workers)?", text, flags=re.I)
        if read_phase:
            signal_seen = True
            total = int(read_phase.group(1))
            workers = int(read_phase.group(2) or 0)
            mark_total("deep_read", total, workers, exact=True)
            phases["deep_read"]["status"] = "running" if total else "complete"
            details.append(f"读文章启动：共 {total} 篇" + (f"，并发 {workers}" if workers else ""))
            continue
        read_start = re.search(r"(starting|queueing)\s+reading subagent\s+(\d+)\s*/\s*(\d+)\s*:\s*(.+)", text, flags=re.I)
        if read_start:
            signal_seen = True
            action = read_start.group(1).lower()
            index = int(read_start.group(2))
            total = int(read_start.group(3))
            title = _read_progress_title(read_start.group(4))
            if action == "queueing":
                mark_total("deep_read", total)
            else:
                mark_active("deep_read", index, total, title, "running")
            details.append(_read_log_detail("排队读文章" if action == "queueing" else "正在读文章", index, total, title, count_label=f"第{index}篇"))
            continue
        read_done = re.search(r"(?:finished|completed)\s+reading subagent\s+(\d+)\s*/\s*(\d+)\s*:\s*(.*?)(?:\s+-\s+(.+))?$", text, flags=re.I)
        if read_done:
            signal_seen = True
            index = int(read_done.group(1))
            total = int(read_done.group(2))
            raw_status_text = _read_progress_title(read_done.group(3), max_len=120)
            status_text = _read_progress_status_label(raw_status_text, phase="deep_read")
            title = _read_progress_title(read_done.group(4) or "")
            if re.search(r"deep_read\s*=\s*true", raw_status_text, flags=re.I):
                mark_completed("deep_read", completed_deep_read, index, total, title, status_text)
            else:
                mark_total("deep_read", total)
                phases["deep_read"]["last_index"] = index
                phases["deep_read"]["last_title"] = title
                phases["deep_read"]["last_status"] = status_text
            details.append(_read_log_detail("文章阅读完成", index, total, title, status_text, count_label=f"第{index}篇"))
            continue
        if re.search(r"final read\.md aggregation phase", text, flags=re.I):
            signal_seen = True
            details.append("正在生成最终 read.md：汇总所有论文精读结果")
            continue
        if re.search(r"final read\.md aggregation complete", text, flags=re.I):
            signal_seen = True
            details.append("最终 read.md 汇总完成：" + _read_progress_title(text.split(":", 1)[-1], max_len=140))
            continue
        public_progress = re.search(
            r"阶段进度[:：]\s*爬取全文\s*(\d+)\s*/\s*(\d+|\?)\s*[；;]\s*精读全文\s*(\d+)\s*/\s*(\d+|\?)",
            text,
            flags=re.I,
        )
        if public_progress:
            signal_seen = True
            full_current = int(public_progress.group(1))
            full_total = int(public_progress.group(2)) if public_progress.group(2).isdigit() else 0
            deep_current = int(public_progress.group(3))
            deep_total = int(public_progress.group(4)) if public_progress.group(4).isdigit() else 0
            phases["full_text"]["current"] = max(int(phases["full_text"].get("current") or 0), full_current)
            phases["full_text"]["total"] = max(int(phases["full_text"].get("total") or 0), full_total)
            phases["deep_read"]["current"] = max(int(phases["deep_read"].get("current") or 0), deep_current)
            if "deep_read" not in exact_totals:
                phases["deep_read"]["total"] = max(int(phases["deep_read"].get("total") or 0), deep_total)
            continue
        if re.match(r"^(?:当前状态|状态|当前动作|阶段进度|运行编号|细节)[:：]", text):
            continue
        if any(marker in lowered for marker in ["traceback", "failed", "error", "blocked", "exception", "失败", "错误", "阻塞"]):
            cleaned_error = _read_progress_title(text, max_len=260)
            if cleaned_error:
                while cleaned_error.startswith("错误：错误："):
                    cleaned_error = cleaned_error.removeprefix("错误：")
                while cleaned_error.startswith("错误：警告："):
                    cleaned_error = cleaned_error.removeprefix("错误：")
                errors.append(cleaned_error)

    for key, completed in [("full_text", completed_full_text), ("deep_read", completed_deep_read)]:
        if completed:
            phases[key]["current"] = max(int(phases[key].get("current") or 0), len(completed))
        total = int(phases[key].get("total") or 0)
        current = int(phases[key].get("current") or 0)
        if total > 0 and current >= total:
            phases[key]["status"] = "complete"
            phases[key]["active_index"] = 0
            phases[key]["active_title"] = ""
        phases[key]["percent"] = _read_progress_percent(phases[key].get("current"), phases[key].get("total"))

    status_text = " ".join(
        str(value or "").strip().lower()
        for value in [status, result.get("status"), progress.get("phase")]
        if str(value or "").strip()
    )
    cancelled_status = any(marker in status_text for marker in ["cancelled", "canceled"])
    live_read_status = any(marker in status_text for marker in ["running", "queued", "cancelling"])
    finished_read_status = not live_read_status and any(marker in status_text for marker in ["done", "complete", "framework_synced"])
    if not signal_seen and existing:
        existing_payload = dict(existing)
        existing_payload["has_signal"] = existing_payload.get("has_signal", True)
        return existing_payload
    prefer_live_log_progress = signal_seen and live_read_status
    if live_read_status and not signal_seen:
        signal_seen = True
        phases["full_text"]["status"] = "running"
        prefer_live_log_progress = True
    if cancelled_status:
        signal_seen = True
        errors = []
        for key in ["full_text", "deep_read"]:
            phase = phases[key]
            phase_current = int(phase.get("current") or 0)
            phase_total = int(phase.get("total") or 0)
            if phase_total > 0 and phase_current >= phase_total:
                phase["status"] = "complete"
            elif phase_current > 0 or str(phase.get("status") or "") == "running":
                phase["status"] = "cancelled"
            else:
                phase["status"] = "pending"
            phases[key]["active_index"] = 0
            phases[key]["active_title"] = ""
    if not prefer_live_log_progress and not cancelled_status:
        try:
            result_input_count = int(result.get("input_article_count") or 0)
        except (TypeError, ValueError):
            result_input_count = 0
        try:
            result_full_count = int(result.get("full_text_ready_count") or 0)
        except (TypeError, ValueError):
            result_full_count = 0
        try:
            result_deep_count = int(result.get("deep_read_complete_count") or result.get("readings") or 0)
        except (TypeError, ValueError):
            result_deep_count = 0
        if result_input_count > 0:
            signal_seen = True
            phases["full_text"]["total"] = max(int(phases["full_text"].get("total") or 0), result_input_count)
            phases["deep_read"]["total"] = max(int(phases["deep_read"].get("total") or 0), result_input_count)
        if result_full_count > 0:
            signal_seen = True
            phases["full_text"]["current"] = max(int(phases["full_text"].get("current") or 0), result_full_count)
        if result_deep_count > 0:
            signal_seen = True
            phases["deep_read"]["current"] = max(int(phases["deep_read"].get("current") or 0), result_deep_count)

        artifact_progress = _read_job_artifact_progress(result, progress)
        if artifact_progress:
            signal_seen = True
            total = int(artifact_progress.get("total") or 0)
            full_current = int(artifact_progress.get("full_text_current") or 0)
            deep_current = int(artifact_progress.get("deep_read_current") or 0)
            deep_attempted = int(artifact_progress.get("deep_read_attempted") or 0)
            pending_full = int(artifact_progress.get("pending_full_text") or 0)
            pending_deep = int(artifact_progress.get("pending_deep_read") or 0)
            public_read_md_present = artifact_progress.get("public_read_md_present") is True
            validation_valid = artifact_progress.get("validation_valid") is True
            warning_count = int(artifact_progress.get("warning_count") or 0)
            warning_suffix = f"；warning {warning_count} 项" if warning_count else ""
            readable_total = max(0, total - pending_full) if total else max(deep_attempted, deep_current)
            if total:
                phases["full_text"]["total"] = max(int(phases["full_text"].get("total") or 0), total)
                phases["deep_read"]["total"] = readable_total if pending_full else max(int(phases["deep_read"].get("total") or 0), total)
            phases["full_text"]["current"] = max(int(phases["full_text"].get("current") or 0), full_current)
            phases["deep_read"]["current"] = deep_current
            if pending_full:
                phases["full_text"]["current"] = total
                phases["full_text"]["status"] = "warning"
                phases["full_text"]["last_status"] = f"已处理 {total}/{total}；全文可用 {full_current} 篇，未就绪 {pending_full} 篇"
            elif total and full_current >= total:
                phases["full_text"]["status"] = "complete"
                phases["full_text"]["last_status"] = "同篇全文证据已覆盖"
            if pending_deep:
                if pending_full:
                    eligible_complete = readable_total > 0 and deep_current >= readable_total
                    phases["deep_read"]["status"] = "complete" if eligible_complete else "blocked"
                    phases["deep_read"]["last_status"] = f"已完成精读 {deep_current}/{readable_total}；{pending_full} 篇因缺少同篇全文未进入精读"
                elif public_read_md_present and validation_valid:
                    phases["deep_read"]["status"] = "warning"
                    phases["deep_read"]["last_status"] = f"已完成精读 {deep_current}/{total}；{pending_deep} 篇未进入最终 read.md，仅记录在任务日志和机器状态{warning_suffix}"
                else:
                    phases["deep_read"]["status"] = "blocked"
                    phases["deep_read"]["last_status"] = f"已完成精读 {deep_current}/{total}；已启动 {deep_attempted}/{total}，待精读 {pending_deep} 篇"
                details.append(str(phases["deep_read"].get("last_status") or ""))
            elif total and deep_current >= total:
                phases["deep_read"]["status"] = "complete"
                phases["deep_read"]["last_status"] = f"已完成精读 {deep_current}/{total}"
    for key in ["full_text", "deep_read"]:
        phases[key]["percent"] = _read_progress_percent(phases[key].get("current"), phases[key].get("total"))

    current_stage = ""
    current_action = ""
    full_total_for_stage = int(phases["full_text"].get("total") or 0)
    full_current_for_stage = int(phases["full_text"].get("current") or 0)
    if cancelled_status:
        current_stage = "deep_read" if str(phases["deep_read"].get("status") or "") == "cancelled" else "full_text"
        cancelled_phase = phases[current_stage]
        cancelled_label = str(cancelled_phase.get("label") or ("读文章" if current_stage == "deep_read" else "爬文章"))
        cancelled_current = int(cancelled_phase.get("current") or 0)
        cancelled_total = int(cancelled_phase.get("total") or 0)
        current_action = f"任务已取消：{cancelled_label}停在 {cancelled_current}/{cancelled_total}。" if cancelled_total else "任务已取消。"
    elif (
        str(phases["full_text"].get("status") or "") == "blocked"
        and phases["full_text"].get("last_status")
        and (not full_total_for_stage or full_current_for_stage < full_total_for_stage)
    ):
        current_stage = "full_text"
        current_action = "爬文章阻塞：" + str(phases["full_text"].get("last_status") or "")
    elif str(phases["deep_read"].get("status") or "") == "blocked" and phases["deep_read"].get("last_status"):
        current_stage = "deep_read"
        current_action = "读文章阻塞：" + str(phases["deep_read"].get("last_status") or "")
    elif str(phases["deep_read"].get("status") or "") == "warning" and phases["deep_read"].get("last_status"):
        current_stage = "deep_read"
        current_action = "读文章完成并有警告：" + str(phases["deep_read"].get("last_status") or "")
    elif finished_read_status and str(phases["deep_read"].get("status") or "") == "complete":
        current_stage = "deep_read"
        current_action = "读文章已完成：" + str(phases["deep_read"].get("last_status") or "全部可读论文均已完成")
        if str(phases["full_text"].get("status") or "") == "warning":
            current_action += "；" + str(phases["full_text"].get("last_status") or "")
    elif phases["deep_read"].get("active_title"):
        current_stage = "deep_read"
        current_action = _read_log_detail(
            "正在读文章",
            int(phases["deep_read"].get("active_index") or 0),
            int(phases["deep_read"].get("total") or 0),
            str(phases["deep_read"].get("active_title") or ""),
        )
    elif phases["full_text"].get("active_title"):
        current_stage = "full_text"
        current_action = _read_log_detail(
            "正在爬文章",
            int(phases["full_text"].get("active_index") or 0),
            int(phases["full_text"].get("total") or 0),
            str(phases["full_text"].get("active_title") or ""),
        )
    elif details:
        current_stage = "deep_read" if any("精读" in item or "读文章" in item or "read.md" in item for item in details[-3:]) else "full_text"
        if details[-1].startswith(("爬文章启动：", "读文章启动：")):
            current_action = details[-1]
        else:
            prefix = "读文章并发处理中，最近进展" if current_stage == "deep_read" else "爬文章并发处理中，最近进展"
            current_action = f"{prefix}：{details[-1]}"
    if not current_action and live_read_status and details:
        current_stage = "deep_read" if any("精读" in item or "读文章" in item or "read.md" in item for item in details[-3:]) else "full_text"
        current_action = details[-1]
    if not current_action:
        message = _read_progress_title(progress.get("message") or result.get("summary") or status, max_len=220)
        if message.strip().lower() in {"queued", "started", "running", "read started"}:
            message = ""
        if live_read_status and ("阻塞" in message or "blocked" in message.lower() or "准备当前 Find 输入" in message):
            message = ""
        current_stage = "full_text" if live_read_status else str(progress.get("phase") or status or "full_text")
        current_action = message or "等待爬文章开始。"

    overall_deep_total = int(phases["deep_read"].get("total") or phases["full_text"].get("total") or 0)
    overall_total = int(phases["full_text"].get("total") or 0) + overall_deep_total
    overall_current = int(phases["full_text"].get("current") or 0) + int(phases["deep_read"].get("current") or 0)
    if overall_total <= 0 and int(progress.get("total") or 0) > 0:
        overall_total = int(progress.get("total") or 0)
        overall_current = int(progress.get("current") or 0)
    recent_details = details[-10:]
    if cancelled_status:
        recent_details.append("任务已取消。")
        recent_details = recent_details[-10:]
    if not recent_details:
        for line in raw[-30:]:
            text = _strip_public_taste_marker(line)
            if re.match(
                r"^(?:queueing|acquiring|finished|completed|starting)\s+(?:full[- ]text(?:\s+acquisition)?|reading subagent)\s+\d+\s*/\s*\d+\s*:",
                text,
                flags=re.I,
            ) or re.match(r"^reusing verified full[- ]text packet", text, flags=re.I):
                cleaned = _read_progress_title(text, max_len=300)
                if cleaned:
                    recent_details.append(cleaned)
        recent_details = recent_details[-10:]
    recent_details = [item for item in recent_details if "准备当前 Find 输入" not in item and "精读任务已启动" not in item]
    if not recent_details and current_action:
        recent_details = [current_action]
    return {
        "has_signal": signal_seen,
        "current_stage": current_stage,
        "current_action": current_action,
        "overall_current": overall_current,
        "overall_total": overall_total,
        "overall_percent": _read_progress_percent(overall_current, overall_total),
        "phases": phases,
        "recent_details": recent_details,
        "recent_errors": errors[-5:],
    }


def _read_job_progress_payload(logs: Any, progress: Any = None, result: Any = None, *, status: Any = "") -> dict[str, Any]:
    progress_payload = dict(progress if isinstance(progress, dict) else {})
    read_progress = _read_job_progress_from_logs(logs, progress_payload, result, status=status)
    if read_progress.get("has_signal"):
        progress_payload["read_progress"] = read_progress
        progress_payload["phase"] = str(read_progress.get("current_stage") or progress_payload.get("phase") or "read")
        progress_payload["message"] = str(read_progress.get("current_action") or progress_payload.get("message") or "")
        phases = read_progress.get("phases") if isinstance(read_progress.get("phases"), dict) else {}
        current_phase = phases.get(str(read_progress.get("current_stage") or "")) if isinstance(phases.get(str(read_progress.get("current_stage") or "")), dict) else {}
        if int(current_phase.get("total") or 0) > 0:
            progress_payload["current"] = int(current_phase.get("current") or 0)
            progress_payload["total"] = int(current_phase.get("total") or 0)
            progress_payload["percent"] = int(current_phase.get("percent") or 0)
        elif int(read_progress.get("overall_total") or 0) > 0:
            progress_payload["current"] = int(read_progress.get("overall_current") or 0)
            progress_payload["total"] = int(read_progress.get("overall_total") or 0)
            progress_payload["percent"] = int(read_progress.get("overall_percent") or 0)
    return progress_payload


def _public_read_job_logs(logs: list[str], progress: dict[str, Any], result: dict[str, Any], *, limit: int = 24) -> list[str]:
    read_progress = _read_job_progress_from_logs(logs, progress, result, status=result.get("status") or progress.get("phase"))
    live_status = str(result.get("status") or progress.get("phase") or "").strip().lower()
    live_read_job = live_status in {"queued", "running", "cancelling", "full_text", "deep_read", "read"}
    if not read_progress.get("has_signal"):
        out: list[str] = []
        action = str(read_progress.get("current_action") or progress.get("message") or result.get("summary") or "").strip()
        if live_status in {"error", "failed", "fail"}:
            action = _public_job_summary_text(action)
        if "准备当前 Find 输入" in action or "精读任务已启动" in action:
            action = ""
        out.append("当前状态：" + (action or "等待爬文章开始。"))
        run_id = str(result.get("run_id") or result.get("find_run_id") or "").strip()
        if run_id:
            out.append("运行编号：" + run_id)
        for line in (logs or [])[-20:]:
            text = _read_progress_title(_strip_public_taste_marker(line), max_len=300)
            lowered = text.lower()
            if not text or text.startswith(("{", "}", "[", "]", "$")) or '":' in text[:160]:
                continue
            if lowered.startswith("traceback") or re.match(r'^file\s+["\']', text, flags=re.I):
                continue
            if live_status in {"error", "failed", "fail"} and re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):", text):
                continue
            if "bin/python" in lowered or "claude -p" in lowered or lowered.startswith("delegating current find read/idea/plan"):
                continue
            out.append("细节：" + text)
        if not live_read_job and live_status not in {"cancelled", "canceled"}:
            out.extend(_read_job_machine_warning_lines(result, limit=6))
        dedup: list[str] = []
        seen: set[str] = set()
        for line in out:
            if line in seen:
                continue
            seen.add(line)
            dedup.append(line)
        return dedup[:max(1, min(limit, 12))]
    header: list[str] = []
    action = str(read_progress.get("current_action") or "").strip()
    if live_status in {"error", "failed", "fail"}:
        action = _public_job_summary_text(action)
    if action:
        header.append("当前状态：" + action)
    run_id = str(result.get("run_id") or result.get("find_run_id") or "").strip()
    if run_id:
        header.append("运行编号：" + run_id)
    phases = read_progress.get("phases") if isinstance(read_progress.get("phases"), dict) else {}
    full_text = phases.get("full_text") if isinstance(phases.get("full_text"), dict) else {}
    deep_read = phases.get("deep_read") if isinstance(phases.get("deep_read"), dict) else {}
    current_stage = str(read_progress.get("current_stage") or "")
    current_phase = (
        deep_read
        if current_stage in {"deep_read", "complete"} and int(deep_read.get("total") or 0) > 0
        else full_text if current_stage == "full_text" else {}
    )
    if current_phase:
        label = str(current_phase.get("label") or ("读文章" if current_stage == "deep_read" else "爬文章"))
        total = int(current_phase.get("total") or 0)
        if total > 0:
            header.append(f"阶段进度：{label} {int(current_phase.get('current') or 0)}/{total}")
        else:
            header.append(f"阶段进度：{label} 准备中")
    else:
        full_total = int(full_text.get("total") or 0)
        if full_total > 0:
            header.append(f"阶段进度：爬文章 {int(full_text.get('current') or 0)}/{full_total}")
        else:
            header.append("阶段进度：准备中")
    detail_lines: list[str] = []
    normalized_action = _read_progress_title(action, max_len=300)
    for detail in read_progress.get("recent_details") or []:
        text = _read_progress_title(detail, max_len=300)
        if text and text != normalized_action and "准备当前 Find 输入" not in text and "精读任务已启动" not in text:
            detail_lines.append("细节：" + text)
    error_lines: list[str] = []
    for error in read_progress.get("recent_errors") or []:
        text = _read_progress_title(error, max_len=300)
        if text:
            if text.lower().startswith("traceback") or re.match(r'^file\s+["\']', text, flags=re.I):
                continue
            if live_status in {"error", "failed", "fail"} and re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):", text):
                continue
            while text.startswith("错误：错误："):
                text = text.removeprefix("错误：")
            while text.startswith("错误：警告："):
                text = text.removeprefix("错误：")
            if text.startswith("警告：") or text.startswith("警告:"):
                error_lines.append(text)
            else:
                error_lines.append("错误：" + text)
    machine_warning_lines = [] if live_read_job or live_status in {"cancelled", "canceled"} else _read_job_machine_warning_lines(result, limit=6)
    detail_budget = max(0, limit - len(header) - min(len(error_lines), 3) - min(len(machine_warning_lines), 6))
    out = [*header, *detail_lines[-detail_budget:], *error_lines[-3:], *machine_warning_lines[:6]]
    dedup: list[str] = []
    seen: set[str] = set()
    for line in out:
        if line in seen:
            continue
        seen.add(line)
        dedup.append(line)
    return dedup[:max(1, limit)]


def _public_plan_stage_detail(logs: list[str]) -> str:
    for raw_line in reversed(logs[-12:]):
        line = _public_text(raw_line)
        if line.startswith("Planning idea:"):
            title = line.split(":", 1)[1].strip() or "已批准 Idea"
            return f"正在为 Idea 生成 Plan：{title}"
        if line.startswith("Plan stage complete"):
            return "Plan 已产出，正在同步项目产物。"
    return ""


def _public_read_idea_plan_job_logs(stage: str, logs: list[str], progress: dict[str, Any], result: dict[str, Any], *, limit: int = 8) -> list[str]:
    """Keep literature-reasoning taskbar rows human-facing, not agent transcripts."""
    label_map = {"read": "精读", "idea": "Idea", "plan": "Plan"}
    label = label_map.get(stage, "文献推理")
    out: list[str] = []
    result_status = str(result.get("status") or "").strip()
    progress_phase = str(progress.get("phase") or "").strip()
    status = result_status or progress_phase
    message = str(progress.get("message") or result.get("summary") or status or "").strip()
    lowered = "\n".join([message, status, "\n".join(logs[-12:])]).lower()
    stage_detail = _public_plan_stage_detail(logs) if stage == "plan" else ""
    error_state = any(value.lower() in {"error", "failed", "fail"} for value in [result_status, progress_phase] if value)
    if error_state:
        cleaned_message = _public_job_summary_text(_public_text(message))
        out.append("当前状态：" + (cleaned_message[:220] if cleaned_message else f"{label}任务执行失败。"))
    elif "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection" in lowered or "claude_takeover_ready" in lowered:
        out.append("当前状态：精读、Idea 和 Plan 已完成，等待环境阶段选择基底。")
    elif any(marker in lowered for marker in ["blocked_current_find_deep_read_pending", "claude_deep_read_required", "pending_deep_read_synthesis"]):
        out.append("当前状态：当前 Find 的全文证据已覆盖，但仍有论文未完成精读；TASTE 需要继续运行 Reading subagent。")
    elif any(marker in lowered for marker in ["blocked_current_find_full_text_evidence_pending", "full_text_evidence_missing", "pending_full_text_reading"]):
        out.append("当前状态：当前 Find 仍缺少同篇全文证据，Read 已停在全文证据门控。")
    elif any(marker in lowered for marker in ["evidence gate", "waiting_for_environment", "waiting for environment", "blocked"]):
        cleaned_message = _public_job_summary_text(_public_text(message))
        generic_status = cleaned_message.lower().replace("-", "_") in {"blocked", "blocking", "阻塞"} or cleaned_message.lower().startswith("blocked_")
        out.append("当前状态：" + (cleaned_message[:220] if cleaned_message and not generic_status else "当前阶段仍未完成，TASTE 已暂停下游发布。"))
    elif "complete" in lowered or "done" in lowered:
        out.append(f"当前状态：{label}阶段已完成。")
    elif stage_detail:
        out.append("当前状态：" + stage_detail[:220])
    elif message:
        cleaned_message = _public_job_summary_text(_public_text(message))
        if cleaned_message:
            out.append("当前状态：" + cleaned_message[:220])
    else:
        if stage == "read":
            out.append("当前状态：等待爬文章开始。")
        else:
            out.append(f"当前状态：{label}任务已记录。")
    run_id = str(result.get("run_id") or result.get("find_run_id") or "").strip()
    if run_id:
        out.append("运行编号：" + run_id)
    counts: list[str] = []
    for key, text in [
        ("reading_count", "精读"),
        ("actual_reading_count", "精读"),
        ("idea_count", "Idea"),
        ("plan_count", "Plan"),
    ]:
        value = result.get(key)
        if value not in (None, "", 0):
            counts.append(f"{text} {value}")
    if counts:
        out.append("阶段数量：" + " / ".join(counts))
    if logs and stage != "read":
        out.append(f"详细日志：已保留 {len(logs)} 行原始日志；任务栏只显示阶段摘要。")
    dedup: list[str] = []
    seen: set[str] = set()
    for line in out:
        if line in seen:
            continue
        seen.add(line)
        dedup.append(line)
    return dedup[-max(1, limit):]


def _public_job_logs(stage: Any, logs: Any, progress: Any = None, result: Any = None, *, limit: int = 40) -> list[str]:
    """Return taskbar logs that are useful to a human, not raw JSON dumps."""
    raw = [str(line or "").strip() for line in (logs if isinstance(logs, list) else []) if str(line or "").strip()]
    raw_stage = str(stage or "")
    public_stage = _public_taste_stage(raw_stage)
    progress = progress if isinstance(progress, dict) else {}
    result = result if isinstance(result, dict) else {}

    if _is_full_cycle_job(raw_stage, "", result, raw):
        return _public_full_cycle_job_logs(raw, progress, result, limit=limit)

    if public_stage in {"environment", "experiment"}:
        return _public_stage_job_logs(public_stage, raw, progress, result, limit=min(limit, 8))

    if public_stage == "find" or raw_stage.startswith("find"):
        stage_limit = min(limit, 8)
        return _with_public_task_error_detail(_public_find_job_logs(raw, progress, result, limit=stage_limit), raw, progress, result, limit=stage_limit)

    if public_stage == "read":
        stage_limit = min(limit, 24)
        return _with_public_task_error_detail(_public_read_job_logs(raw, progress, result, limit=stage_limit), raw, progress, result, limit=stage_limit)

    if public_stage in {"idea", "plan"}:
        stage_limit = min(limit, 8)
        return _with_public_task_error_detail(_public_read_idea_plan_job_logs(public_stage, raw, progress, result, limit=stage_limit), raw, progress, result, limit=stage_limit)

    if public_stage == "paper" or raw_stage.startswith("paper"):
        out: list[str] = []
        message_source = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else result
        result_summary = _paper_stage_job_message(message_source if isinstance(message_source, dict) else result).strip()
        live_summary = _paper_live_status_message(result, progress) if result.get("process_alive") is True else ""
        execution_message = str(result.get("paper_execution_message") or (message_source.get("paper_execution_message") if isinstance(message_source, dict) else "") or "").strip()
        message = _public_paper_progress_message(progress.get("message") or "")
        phase = str(progress.get("phase") or "").strip()
        if live_summary:
            out.append("当前状态：" + live_summary)
            if result_summary:
                out.append("论文产物状态：" + result_summary)
        elif execution_message:
            out.append("执行状态：" + execution_message)
            if result_summary:
                out.append("当前状态：" + result_summary)
        elif result_summary:
            out.append("当前状态：" + result_summary)
        elif message:
            out.append("当前状态：" + message)
        elif phase:
            out.append("当前阶段：" + phase)
        citation_count = result.get("paper_normality_citation_count")
        citation_target = result.get("paper_normality_citation_target") or result.get("paper_reference_quality_target")
        citation_source = str(result.get("paper_normality_reference_target_source") or "").strip()
        if citation_count:
            label = "官方引用要求" if citation_source == "official" else "写作引用质量目标"
            out.append(label + "：" + str(citation_count) + (("/" + str(citation_target)) if citation_target else ""))
        venue_summary = result.get("venue_requirements_summary") if isinstance(result.get("venue_requirements_summary"), dict) else {}
        venue_public = str(result.get("venue_requirements_public_summary") or venue_summary.get("summary") or "").strip()
        if venue_public:
            out.append("目标要求：" + venue_public)
        body_pages = result.get("conference_preview_body_pages")
        body_limit = result.get("conference_preview_body_page_limit")
        if body_pages:
            out.append("正文页数：" + str(body_pages) + (("/" + str(body_limit)) if body_limit else ""))
        layout_summary = str(result.get("paper_layout_summary") or "").strip()
        if layout_summary:
            out.append("图表版面：" + layout_summary)
        blocker_summary = str(result.get("conference_preview_blocker_summary") or "").strip()
        if blocker_summary:
            if "reference_count" in blocker_summary or "reference_quality_target" in blocker_summary or "references/citation" in blocker_summary:
                out.append("写作质量目标未达：参考文献覆盖不足")
            elif "参考文献覆盖不足" in blocker_summary:
                out.append("写作质量目标未达：参考文献覆盖不足")
            else:
                out.append("预览仍需完善：" + blocker_summary)
        cmd = _public_paper_command(result.get("cmd") or result.get("command") or "")
        if cmd:
            out.append("命令：" + cmd)
        for key, label in [
            ("log_path", "日志"),
            ("artifact_dir", "产物目录"),
            ("pdf_path", "PDF"),
            ("tex_path", "TeX"),
            ("blocked_pdf_path", "论文预览 PDF"),
            ("latest_generated_pdf_path", "最近生成 PDF"),
        ]:
            value = str(result.get(key) or "").strip()
            if value:
                out.append(f"{label}：{value}")
        for line in raw:
            low = line.lower()
            if line.startswith("Workflow command:"):
                out.append("命令：" + _public_paper_command(line.split(":", 1)[1]))
                continue
            if line.startswith("Runtime PATH head:"):
                out.append("运行环境 PATH 前缀：" + line.split(":", 1)[1].strip())
                continue
            if "ar writing conference preview generated" in low or "conference preview generated" in low:
                out.append("writing 已生成当前稿件预览；保留当前 PDF，不用旧 Markdown 输出覆盖。")
                continue
            if "paper blocked" in low:
                out.append("论文任务停在真实门控阻塞状态。")
                continue
            if "paper complete" in low:
                out.append("论文任务已完成。")
                continue
            if "paper pipeline skipped" in low:
                out.append("论文流水线已保留真实门控状态；当前输出作为论文预览，不标记为投稿通过。")
                continue
            if "paper pipeline generated" in low or "generated a compile report" in low or "conference preview" in low:
                mapped = _public_paper_progress_message(line)
                out.append(mapped or "writing 正在生成当前稿件预览；证据门控保持真实状态。")
        detail_tail: list[str] = []
        summary_prefixes = (
            "当前状态：", "执行状态：", "当前阶段：", "写作引用质量目标：", "官方引用要求：", "目标要求：",
            "正文页数：", "图表版面：", "预览仍需完善：", "写作质量目标未达：",
            "命令：", "运行环境 PATH 前缀：", "日志：", "产物目录：", "PDF：",
            "TeX：", "论文预览 PDF：", "最近生成 PDF：", "论文产物状态：", "详细日志：",
        )
        for raw_line in raw[-12:]:
            cleaned = _redact_public_log_text(_public_text(raw_line)).strip()
            lowered_cleaned = cleaned.lower()
            if not cleaned or cleaned.startswith(summary_prefixes):
                continue
            if "heartbeat" in lowered_cleaned or ("waiting for" in lowered_cleaned and "logs" in lowered_cleaned):
                continue
            if len(cleaned) > 800:
                cleaned = cleaned[:797] + "..."
            detail_tail.append("详细日志：" + cleaned)
        if detail_tail:
            out.extend(detail_tail)
        if not out:
            out = ["论文生成任务已记录；详细文件见产物路径。"]
        dedup: list[str] = []
        seen: set[str] = set()
        for line in out:
            if line in seen:
                continue
            seen.add(line)
            dedup.append(line)
        return dedup[-limit:]

    return _compact_log_lines(raw, limit=limit)

def _fresh_find_result_for_job(job: "JobState") -> Any:
    """Return a compact, fresh-enough Find result without loading huge artifacts."""
    if str(job.stage or "") != "find" or not isinstance(job.result, dict):
        return job.result
    run_id = str(job.result.get("run_id") or "")
    if not run_id:
        return job.result
    result = dict(job.result)
    result["run_id"] = run_id
    try:
        directory = run_dir(run_id)
    except FileNotFoundError:
        result.setdefault("artifact_missing", True)
        result.setdefault("artifact_missing_reason", f"Run artifact directory is no longer available: {run_id}")
        return result
    result.setdefault("artifact_dir", str(directory))
    result.setdefault("artifact_paths", {
        "find_results": str(directory / "find_results.json"),
        "find": str(directory / "find.md"),
        "source_status": str(directory / "source_status.md"),
    })
    find_path = directory / "find_results.json"
    if find_path.exists():
        try:
            stat = find_path.stat()
            result["find_results_path"] = str(find_path)
            result["find_results_size_bytes"] = stat.st_size
            result["find_results_mtime"] = stat.st_mtime
        except OSError:
            pass
    # Preserve existing compact counters, but never hydrate large arrays here.
    for key in [
        "raw_title_index", "retrieval_candidates", "title_candidates",
        "evaluated_candidates", "screened_ranking", "strong_recommendations",
        "triage_candidates", "audit_candidates", "critique_candidates",
        "source_status", "venue_health_report", "category_scan_report",
        "title_filter_report",
    ]:
        value = result.get(key)
        if isinstance(value, list):
            result[key + "_count"] = len(value)
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key + "_count"] = len(value)
            result.pop(key, None)
    return result

def _artifact_compact_text(value: Any, limit: int = 650) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _artifact_compact_paper_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    keys = [
        "id", "title", "venue", "venue_id", "year", "track", "presentation_type", "presentation_label", "presentation_labels", "quality_labels", "doi", "url", "pdf_url",
        "fit_score", "llm_fit_score", "diversity_score", "score", "recommendation_score", "recommendation_score_v2",
        "taste_pool", "taste_pool_role", "hit_directions", "hit_directions_zh", "hit_directions_en",
        "abstract", "abstract_zh", "abstract_en",
        "reason", "reason_zh", "reason_en", "fit_explanation", "fit_explanation_zh", "fit_explanation_en",
    ]
    out = {key: row.get(key) for key in keys if key in row}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    doi = str(row.get("doi") or metadata.get("doi") or "").strip()
    if doi:
        out.setdefault("doi", doi)
    if not str(out.get("url") or "").strip():
        for key in ["url", "doi_url", "publisher_url", "acm_abs_url", "dblp_record_url"]:
            value = str(row.get(key) or metadata.get(key) or "").strip()
            if value:
                out["url"] = value
                break
    if not str(out.get("pdf_url") or "").strip():
        for key in ["pdf_url", "acm_pdf_url", "acm_epdf_url", "open_access_pdf_url"]:
            value = str(row.get(key) or metadata.get(key) or "").strip()
            if value:
                out["pdf_url"] = value
                break
    for key in ["abstract", "abstract_zh", "abstract_en", "reason", "reason_zh", "reason_en", "fit_explanation", "fit_explanation_zh", "fit_explanation_en", "recommendation_note", "recommendation_note_zh", "recommendation_note_en"]:
        if key in out:
            out[key] = _artifact_compact_text(out[key], 650)
    if "title" in out:
        out["title"] = _artifact_compact_text(out["title"], 220)
    return out


def _strip_redundant_find_public_json_aliases(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    for key in ("articles", "read_candidates"):
        payload.pop(key, None)
    for counts_key in ("counts", "artifact_counts"):
        counts = payload.get(counts_key)
        if isinstance(counts, dict):
            counts.pop("articles", None)
            counts.pop("read_candidates", None)
            if not counts:
                payload.pop(counts_key, None)
    return payload



def _find_survey_stats_from_payloads(*payloads: Any) -> dict[str, Any]:
    survey_stats: dict[str, Any] = {}

    def merge_dict(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if item not in (None, ""):
                survey_stats[key] = item

    def merge_counts(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key in FIND_SURVEY_COUNT_KEYS:
            item = value.get(key)
            if item not in (None, ""):
                survey_stats[key] = item
        alias_pairs = [
            ("raw_title_index", "raw_title_index_papers"),
            ("title_candidates", "venue_final_title_candidates"),
            ("detail_fetched", "venue_detail_fetched_candidates"),
            ("evaluated_candidates", "venue_evaluated_candidates"),
        ]
        for source_key, target_key in alias_pairs:
            if survey_stats.get(target_key) in (None, "") and value.get(source_key) not in (None, ""):
                survey_stats[target_key] = value.get(source_key)

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        merge_dict(payload.get("survey_stats"))
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        merge_dict(diagnostics.get("survey_stats"))
        merge_counts(payload.get("counts"))
    if survey_stats.get("raw_title_index_papers") not in (None, ""):
        survey_stats.setdefault("title_total_papers", survey_stats.get("raw_title_index_papers"))
        survey_stats.setdefault("venue_total_papers_available", survey_stats.get("raw_title_index_papers"))
        survey_stats.setdefault("venue_corpus_audited_papers", survey_stats.get("raw_title_index_papers"))
    return survey_stats

def _project_root_for_find_run(run_id: str) -> Path | None:
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    try:
        project_roots = [path for path in PROJECT_IDS_ROOT.iterdir() if path.is_dir()]
    except Exception:
        return None
    for root in project_roots:
        for rel in ["state/finding_frontend.json", "state/literature_tool_packet.json", "state/current_find_research_plan.json"]:
            payload = _read_project_json(root / rel, {})
            if isinstance(payload, dict) and run_id in {str(payload.get("taste_run_id") or ""), str(payload.get("run_id") or ""), str(payload.get("source_run_id") or ""), str(payload.get("find_run_id") or "")}:
                return root
    return None


def _project_id_for_find_run(run_id: str) -> str:
    run_id = str(run_id or "").strip()
    if not run_id:
        return ""
    now = time.monotonic()
    cached = _RUN_PROJECT_CACHE.get(run_id)
    if isinstance(cached, dict) and now < float(cached.get("expires_at") or 0.0):
        return str(cached.get("project") or "")
    root = _project_root_for_find_run(run_id)
    project_id = root.name if root else ""
    _RUN_PROJECT_CACHE[run_id] = {"project": project_id, "expires_at": now + RUN_PROJECT_CACHE_TTL_SEC}
    return project_id


def _run_belongs_to_project(run_id: str, project: str) -> bool:
    project = str(project or "").strip()
    if not project:
        return True
    return _project_id_for_find_run(run_id) == project


def _with_run_project(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    run_id = str(row.get("run_id") or "").strip()
    project = str(row.get("project") or "").strip()
    if run_id and not project:
        project = _project_id_for_find_run(run_id)
        if project:
            row["project"] = project
    return row


def _api_query_str(value: Any, default: str = "") -> str:
    return str(value).strip() if isinstance(value, str) else default


def _filter_runs_for_project(items: list[dict], project: str) -> list[dict]:
    project = _api_query_str(project)
    rows = [_with_run_project(item if isinstance(item, dict) else {}) for item in items]
    if not project:
        return rows
    return [row for row in rows if str(row.get("project") or "") == project]


def _job_project_cache_key(item: dict[str, Any]) -> str:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
    return json.dumps(
        [
            item.get("job_id"),
            item.get("stage"),
            item.get("run_id"),
            result.get("project"),
            result.get("run_id"),
            progress.get("phase"),
        ],
        ensure_ascii=False,
        default=str,
    )


def _job_project_id(item: dict[str, Any]) -> str:
    cache_key = _job_project_cache_key(item)
    now = time.monotonic()
    cached = _JOB_PROJECT_ID_CACHE.get(cache_key)
    if isinstance(cached, dict) and now < float(cached.get("expires_at") or 0.0):
        return str(cached.get("project") or "")

    def finish(project_id: str) -> str:
        _JOB_PROJECT_ID_CACHE[cache_key] = {
            "expires_at": time.monotonic() + JOB_PROJECT_ID_CACHE_TTL_SEC,
            "project": str(project_id or ""),
        }
        if len(_JOB_PROJECT_ID_CACHE) > 512:
            for key in list(_JOB_PROJECT_ID_CACHE)[:128]:
                _JOB_PROJECT_ID_CACHE.pop(key, None)
        return str(project_id or "")

    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    paper_stage = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}
    project = str(result.get("project") or result.get("project_id") or paper_stage.get("project") or "").strip()
    if project:
        return finish(project)
    run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
    if run_id:
        project = _project_id_for_find_run(run_id)
        if project:
            return finish(project)
    job_id = str(item.get("job_id") or "")
    fast_project = _project_from_job_id_fast(job_id)
    if fast_project:
        return finish(fast_project)
    try:
        return finish(_project_from_job_payload(job_id, item))
    except Exception:
        return finish("")


def _job_belongs_to_project(item: dict[str, Any], project: str) -> bool:
    project = str(project or "").strip()
    if not project:
        return True
    item_project = _job_project_id(item)
    if item_project:
        return item_project == project
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
    return bool(run_id and _run_belongs_to_project(run_id, project))


def _job_state_project_filter_payload(job: "JobState") -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "stage": job.stage,
        "status": job.status,
        "created_at": job.created_at,
        "logs": job.logs[-12:],
        "run_id": job.run_id,
        "result": job.result if isinstance(job.result, dict) else {},
        "progress": job.progress if isinstance(job.progress, dict) else {},
    }


def _job_state_belongs_to_project(job: "JobState", project: str) -> bool:
    return _job_belongs_to_project(_job_state_project_filter_payload(job), project)


def _current_find_recommendation_projection(project_root: Path, run_id: str = "") -> dict[str, Any]:
    projection = _read_project_json(project_root / "state" / "current_find_recommendation_projection.json", {})
    if not isinstance(projection, dict):
        return {}
    expected = str(run_id or "").strip()
    actual = str(projection.get("run_id") or projection.get("source_run_id") or "").strip()
    if expected and actual and expected != actual:
        return {}
    return projection


PROJECT_ARTIFACT_NAMES = {
    "find.md", "read.md", "idea.md", "plan.md", "source_status.md",
    "find_progress.json", "find_results.json", "read_results.json", "ideas.json", "plans.json",
    "selection.json",
}
PROJECT_DOWNSTREAM_MARKDOWN_RUN_GUARDS = {
    "read.md": "read_results.json",
    "idea.md": "ideas.json",
    "plan.md": "plans.json",
}


def _payload_run_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("run_id") or value.get("taste_run_id") or value.get("source_run_id") or value.get("find_run_id") or value.get("current_find_run_id") or "").strip()


def _project_current_find_run_id(project_root: Path, *, allow_large_find_results: bool = True) -> str:
    small_rels = [
        "state/finding_frontend.json",
        "planning/finding/find_progress.json",
        "state/current_find_recommendation_projection.json",
        "state/current_find_research_plan.json",
        "state/current_find_claude_reading_validation.json",
        "planning/finding/read_results.json",
        "planning/finding/ideas.json",
        "planning/finding/plans.json",
    ]
    for rel in small_rels:
        path = project_root / rel
        try:
            if path.exists() and path.stat().st_size > LARGE_JSON_ARTIFACT_LIMIT_BYTES:
                continue
        except OSError:
            pass
        payload = _read_project_json(path, {})
        run_id = _payload_run_id(payload)
        if run_id:
            return run_id
    if allow_large_find_results:
        payload = _read_project_json(project_root / "planning" / "finding" / "find_results.json", {})
        run_id = _payload_run_id(payload)
        if run_id:
            return run_id
    return ""


def _project_markdown_declared_run_id(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= 120:
                    break
                match = re.match(
                    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:`)?(?:run_id|taste_run_id|source_run_id|find_run_id|current_find_run_id)(?:`)?(?:\*\*)?\s*[:：]\s*[`'\"]?([^`'\"\s,;]+)",
                    line,
                    flags=re.I,
                )
                if match:
                    return match.group(1).strip()
    except OSError:
        return ""
    return ""


def _project_downstream_markdown_matches_run(project_root: Path, candidate: Path, name: str, expected: str) -> bool:
    companion_name = PROJECT_DOWNSTREAM_MARKDOWN_RUN_GUARDS.get(name)
    if not companion_name:
        return True
    companion = project_root / "planning" / "finding" / companion_name
    payload = _read_project_json(companion, {})
    actual = _payload_run_id(payload)
    declared = _project_markdown_declared_run_id(candidate)
    if actual:
        return actual == expected and (not declared or declared == expected)
    if declared:
        return declared == expected
    return False


def _blocked_current_find_downstream_markdown_names(project_root: Path | None, run_id: str) -> set[str]:
    if project_root is None or not run_id:
        return set()
    try:
        summary = _current_find_pipeline_summary(project_root)
    except Exception:
        return set(PROJECT_DOWNSTREAM_MARKDOWN_RUN_GUARDS)
    summary_run_id = str(summary.get("run_id") or "").strip()
    if summary_run_id != str(run_id or "").strip():
        return set()
    if summary.get("content_ready") or summary.get("read_idea_plan_ready") or summary.get("takeover_ready"):
        return set()
    blocked = set(PROJECT_DOWNSTREAM_MARKDOWN_RUN_GUARDS)
    read_payload = _read_project_json(project_root / "planning" / "finding" / "read_results.json", {})
    validation = _read_project_json(project_root / "state" / "current_find_claude_reading_validation.json", {})
    if (
        isinstance(read_payload, dict)
        and isinstance(validation, dict)
        and _payload_run_id(read_payload) == str(run_id or "").strip()
        and _payload_run_id(validation) == str(run_id or "").strip()
        and validation.get("valid") is True
        and read_payload.get("public_final_artifact_present") is True
    ):
        blocked.discard("read.md")
    return blocked


def _project_taste_artifact_path(project_root: Path | None, run_id: str, name: str) -> Path | None:
    if project_root is None or name not in PROJECT_ARTIFACT_NAMES:
        return None
    candidate = project_root / "planning" / "finding" / name
    if not candidate.exists():
        return None
    expected = str(run_id or "").strip()
    if not expected:
        return None
    if name in {"find_progress.json", "find_results.json"}:
        current = _project_current_find_run_id(project_root, allow_large_find_results=False)
        return candidate if current == expected else None
    if name.endswith(".json"):
        payload = _read_project_json(candidate, {})
        actual = _payload_run_id(payload)
        if actual and actual != expected:
            return None
        if name in {"read_results.json", "ideas.json", "plans.json"} and not actual:
            return None
        return candidate
    if name in PROJECT_DOWNSTREAM_MARKDOWN_RUN_GUARDS:
        if name == "read.md":
            read_payload = _read_project_json(project_root / "planning" / "finding" / "read_results.json", {})
            validation = _read_project_json(project_root / "state" / "current_find_claude_reading_validation.json", {})
            read_run = _payload_run_id(read_payload)
            validation_run = _payload_run_id(validation)
            if (
                read_run != expected
                or validation_run != expected
                or validation.get("valid") is not True
                or read_payload.get("public_final_artifact_present") is not True
            ):
                return None
        return candidate if _project_downstream_markdown_matches_run(project_root, candidate, name, expected) else None
    current = _project_current_find_run_id(project_root, allow_large_find_results=False)
    return candidate if current == expected else None


def _project_current_find_light_artifact_names(project_root: Path | None, run_id: str) -> tuple[list[str], list[str]]:
    if project_root is None or not str(run_id or "").startswith("find_"):
        return [], []
    markdown_names = [
        name
        for name in LIGHT_ARTIFACT_MARKDOWN_NAMES
        if _project_taste_artifact_path(project_root, run_id, name) is not None
    ]
    json_names = [
        name
        for name in LIGHT_ARTIFACT_JSON_NAMES
        if _project_taste_artifact_path(project_root, run_id, name) is not None
    ]
    return markdown_names, json_names


def _compact_large_markdown_artifact(path: Path, size_bytes: int) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    preview = text[:MARKDOWN_ARTIFACT_PREVIEW_CHARS].rstrip()
    omitted = max(0, len(text) - len(preview))
    if omitted:
        preview += f"\n\n<!-- TASTE web preview truncated {omitted} characters; full artifact remains at {path}. -->\n"
    preview = _public_text(preview)
    return {
        "content": preview,
        "content_truncated": True,
        "size_bytes": size_bytes,
        "truncation_reason": "large markdown artifact compacted for responsive web loading; full file remains on disk",
    }


CURRENT_FIND_PUBLIC_MARKDOWN_PAPER_REF_RE = re.compile(r"\bpaper[_-][0-9a-f]{8,}\b", re.IGNORECASE)


def _current_find_reference_artifact_paths(project_root: Path | None) -> list[Path]:
    if project_root is None:
        return []
    return [
        project_root / "planning" / "finding" / "find_results.json",
        project_root / "planning" / "finding" / "read_results.json",
        project_root / "planning" / "finding" / "full_text_reading" / "full_text_packet.json",
    ]


def _current_find_public_paper_label(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    title = str(row.get("title") or row.get("paper_title") or "").strip()
    if not title or CURRENT_FIND_PUBLIC_MARKDOWN_PAPER_REF_RE.fullmatch(title):
        return ""
    venue = str(row.get("venue") or row.get("source") or "").strip()
    year = str(row.get("year") or "").strip()
    url = str(row.get("url") or row.get("abs_url") or row.get("pdf_url") or "").strip()
    meta = " ".join(item for item in [venue, year] if item)
    label = f"{title} ({meta})" if meta else title
    if url:
        label = f"{label} - {url}"
    return label


def _iter_current_find_public_paper_rows(value: Any):
    if isinstance(value, dict):
        if value.get("title") or value.get("paper_title"):
            yield value
        for key in ["strong_recommendations", "recommendations", "articles", "read_candidates", "readings", "papers", "selected_papers", "items"]:
            rows = value.get(key)
            if isinstance(rows, list):
                for item in rows:
                    yield from _iter_current_find_public_paper_rows(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_current_find_public_paper_rows(item)


def _current_find_public_paper_ref_index(project_root: Path | None) -> dict[str, str]:
    index: dict[str, str] = {}
    for path in _current_find_reference_artifact_paths(project_root):
        payload = _read_project_json(path, {})
        for row in _iter_current_find_public_paper_rows(payload):
            label = _current_find_public_paper_label(row)
            if not label:
                continue
            for key in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url"]:
                raw = str(row.get(key) or "").strip().lower()
                if raw and raw not in index:
                    index[raw] = label
    return index


def _hydrate_current_find_markdown_paper_refs(content: str, project_root: Path | None, name: str) -> str:
    if name != "plan.md" or project_root is None or not CURRENT_FIND_PUBLIC_MARKDOWN_PAPER_REF_RE.search(content):
        return content
    index = _current_find_public_paper_ref_index(project_root)
    if not index:
        return content

    def replace_ref(match: re.Match[str]) -> str:
        raw = match.group(0)
        return index.get(raw.lower(), raw)

    return CURRENT_FIND_PUBLIC_MARKDOWN_PAPER_REF_RE.sub(replace_ref, content)


def _compact_artifact_json_value(
    value: Any, *, max_items: int = 50, max_text_chars: int = 1200, depth: int = 0
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_text_chars else value[:max_text_chars].rstrip() + "..."
    if depth >= 4:
        return _artifact_compact_text(value, max_text_chars)
    if isinstance(value, list):
        items = [
            _compact_artifact_json_value(
                item, max_items=max_items, max_text_chars=max_text_chars, depth=depth + 1
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            items.append({"content_truncated": True, "omitted_items": len(value) - max_items})
        return items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                out["content_truncated"] = True
                out["omitted_items"] = len(value) - max_items
                break
            out[str(key)] = _compact_artifact_json_value(
                item, max_items=max_items, max_text_chars=max_text_chars, depth=depth + 1
            )
        return out
    return _artifact_compact_text(value, max_text_chars)



def _compact_find_source_row_count(row: Any) -> int:
    if not isinstance(row, dict):
        return 0
    for key in ["raw_title_index_count", "corpus_count", "candidate_count", "selected_category_count", "count", "sample_count"]:
        try:
            value = int(row.get(key) or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    return 0


def _compact_find_source_rows_need_refresh(rows: list[dict[str, Any]]) -> bool:
    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    if not rows or sum(_compact_find_source_row_count(row) for row in rows) <= 0:
        return True
    dynamic_sources = {"arxiv", "biorxiv", "nature", "science"}
    for row in rows:
        source = str(row.get("source") or row.get("venue") or "").strip().lower()
        if source not in dynamic_sources and source not in {"nature portfolio", "science family"}:
            continue
        count = _compact_find_source_row_count(row)
        visible_count = as_int(row.get("count") or row.get("raw_count") or row.get("prefiltered_count"))
        if row.get("ok") is False and count > 0:
            return True
        if visible_count <= 0 and as_int(row.get("candidate_count")) > 0:
            return True
    return False


def _hydrate_compact_find_source_state(payload: dict[str, Any], project_root: Path | None) -> dict[str, Any]:
    if project_root is None or not isinstance(payload, dict):
        return payload
    payload_rows = [row for row in _as_list(payload.get("source_status")) if isinstance(row, dict)]
    rows = payload_rows if not _compact_find_source_rows_need_refresh(payload_rows) else []
    if not rows:
        progress_payload = _read_project_json(project_root / "planning" / "finding" / "find_progress.json", {})
        progress_rows = [row for row in _as_list(progress_payload.get("source_status")) if isinstance(row, dict)] if isinstance(progress_payload, dict) else []
        if not _compact_find_source_rows_need_refresh(progress_rows):
            rows = progress_rows
    if not rows:
        try:
            rows = _current_find_source_status_rows(project_root)
        except Exception:
            rows = []
    if not rows:
        return payload
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return payload
    compact_rows = _compact_artifact_json_value(rows[:20])
    payload["source_status"] = compact_rows
    venue_rows = [row for row in _as_list(payload.get("venue_health_report")) if isinstance(row, dict)]
    if not venue_rows or sum(_compact_find_source_row_count(row) for row in venue_rows) <= 0:
        payload["venue_health_report"] = compact_rows
    try:
        venue_counts = _venue_metadata_counts(rows)
    except Exception:
        venue_counts = {}
    if isinstance(venue_counts, dict) and venue_counts:
        source_totals: dict[str, Any] = {}
        for key, value in venue_counts.items():
            if value in (None, ""):
                continue
            try:
                source_totals[key] = int(value)
            except Exception:
                source_totals[key] = value
        if source_totals:
            payload["source_status_totals"] = source_totals
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        counts = dict(counts)
        survey_stats = payload.get("survey_stats") if isinstance(payload.get("survey_stats"), dict) else {}
        survey_stats = dict(survey_stats)
        run_funnel_keys = {
            "raw_title_index_papers",
            "category_filtered_papers",
            "tfidf_screened_papers",
            "venue_title_filter_input_papers",
            "title_score_input_papers",
            "llm_title_scored_papers",
            "venue_final_title_candidates",
            "venue_detail_fetched_candidates",
            "abstract_scored_papers",
            "llm_scored_candidates",
            "recommended_papers",
        }
        has_run_funnel_counts = any(counts.get(key) not in (None, "", 0) or survey_stats.get(key) not in (None, "", 0) for key in run_funnel_keys)
        metadata_keys = {"metadata_complete_venue_count", "metadata_venue_count", "venues_without_official_categories"}
        fallback_keys = {
            "raw_title_index_papers",
            "venue_total_papers_available",
            "venue_corpus_audited_papers",
            "category_selected_papers",
            "venue_category_selected_papers",
            "venue_title_filter_input_papers",
        }
        for key, numeric in source_totals.items():
            if key in metadata_keys or (not has_run_funnel_counts and key in fallback_keys):
                if counts.get(key) in (None, "", 0):
                    counts[key] = numeric
                if survey_stats.get(key) in (None, "", 0):
                    survey_stats[key] = numeric
        if counts:
            payload["counts"] = counts
        if survey_stats:
            payload["survey_stats"] = survey_stats
    return payload

def _compact_large_find_progress_artifact(
    path: Path, run_id: str, size_bytes: int, project_root: Path | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "content_truncated": True,
        "artifact_size_bytes": size_bytes,
        "truncation_reason": "find_progress.json is large; API returns compact progress/source health state so artifact polling remains responsive.",
    }
    keep_keys = [
        "updated_at",
        "generated_at",
        "phase",
        "status",
        "selection",
        "source_status",
        "venue_health_report",
        "counts",
        "abstract_translation_status",
        "strong_recommendation_count",
        "strict_strong_anchor_count",
        "recommendation_target_count",
        "recommendation_shortfall",
        "recommendation_policy",
        "source_health_refreshed_at",
        "source_health_refresh_policy",
        "live_progress",
    ]
    keep_key_set = set(keep_keys)

    def absorb_progress(source: Any) -> bool:
        if not isinstance(source, dict):
            return False
        actual = _payload_run_id(source)
        if actual and actual != str(run_id or ""):
            return False
        copied = False
        for key in keep_keys:
            if source.get(key) not in (None, "", [], {}):
                payload[key] = _compact_artifact_json_value(source.get(key))
                copied = True
        diagnostics = source.get("diagnostics") if isinstance(source.get("diagnostics"), dict) else {}
        if diagnostics:
            payload["diagnostics"] = _compact_artifact_json_value(diagnostics)
            copied = True
        survey_stats = _find_survey_stats_from_payloads(source)
        if survey_stats:
            merged = payload.get("survey_stats") if isinstance(payload.get("survey_stats"), dict) else {}
            merged.update(survey_stats)
            payload["survey_stats"] = merged
            copied = True
        return copied

    copied_any = False
    if project_root is not None:
        copied_any |= absorb_progress(_read_project_json(project_root / "state" / "finding_frontend.json", {}))
        copied_any |= absorb_progress(_current_find_recommendation_projection(project_root, run_id))
    if not copied_any:
        progress = read_json(path, {})
        if isinstance(progress, dict):
            copied_any |= absorb_progress(progress)
            omitted_keys = [key for key in progress.keys() if key not in keep_key_set | {"diagnostics"}]
            if omitted_keys:
                payload["omitted_keys"] = omitted_keys
    _hydrate_compact_find_source_state(payload, project_root)
    return _strip_public_taste_marker(_strip_redundant_find_public_json_aliases(payload))


def _compact_large_find_results_artifact(directory: Path, run_id: str, size_bytes: int) -> dict[str, Any]:
    project_root = _project_root_for_find_run(run_id)
    progress: dict[str, Any] = {}
    if project_root is not None:
        projection = _current_find_recommendation_projection(project_root, run_id)
        frontend_state = _read_project_json(project_root / "state" / "finding_frontend.json", {})
        if isinstance(projection, dict) and projection:
            progress = projection
        elif isinstance(frontend_state, dict) and _payload_run_id(frontend_state) == run_id:
            progress = frontend_state
    if not progress:
        fallback = _read_project_json_if_small(directory / "find_progress.json", {})
        progress = fallback if isinstance(fallback, dict) else {}
    payload: dict[str, Any] = {
        "run_id": run_id,
        "content_truncated": True,
        "artifact_size_bytes": size_bytes,
        "truncation_reason": "find_results.json is large; API returns compact sidecar state plus current strong-paper rows so polling cannot block the web worker.",
    }
    survey_stats: dict[str, Any] = {}

    def merge_survey_stats(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if item not in (None, ""):
                    survey_stats[key] = item

    if isinstance(progress, dict):
        for key in ["phase", "counts", "strong_recommendation_count", "recommendation_target_count", "recommendation_shortfall", "source_status", "venue_health_report", "selection", "live_progress", "updated_at", "generated_at"]:
            if key in progress:
                payload[key] = progress.get(key)
        merge_survey_stats(_find_survey_stats_from_payloads(progress))
    if project_root is not None:
        packet = _read_project_json(project_root / "state" / "literature_tool_packet.json", {})
        frontend_state = _read_project_json(project_root / "state" / "finding_frontend.json", {})
        current_plan = _read_project_json(project_root / "state" / "current_find_research_plan.json", {})
        projection = _current_find_recommendation_projection(project_root, run_id)
        if isinstance(frontend_state, dict) and _payload_run_id(frontend_state) == run_id:
            merge_survey_stats(_find_survey_stats_from_payloads(frontend_state))
        if isinstance(projection, dict) and projection:
            merge_survey_stats(_find_survey_stats_from_payloads(projection))
            recommendation_rows = projection.get("strong_recommendations") if isinstance(projection.get("strong_recommendations"), list) else projection.get("recommendations") if isinstance(projection.get("recommendations"), list) else projection.get("articles") if isinstance(projection.get("articles"), list) else []
            read_rows = projection.get("read_candidates") if isinstance(projection.get("read_candidates"), list) else recommendation_rows
            compact_recommendation_limit = max(len(recommendation_rows), len(read_rows), 1)
            if recommendation_rows:
                compact_rows = [_artifact_compact_paper_row(row) for row in recommendation_rows[:compact_recommendation_limit] if isinstance(row, dict)]
                payload["strong_recommendations"] = compact_rows
            triage_rows = projection.get("triage_candidates") if isinstance(projection.get("triage_candidates"), list) else projection.get("audit_candidates") if isinstance(projection.get("audit_candidates"), list) else []
            if triage_rows:
                payload["triage_candidates"] = [_artifact_compact_paper_row(row) for row in triage_rows[:50] if isinstance(row, dict)]
            counts = projection.get("counts") if isinstance(projection.get("counts"), dict) else {}
            if counts:
                merged_counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
                for key, value in counts.items():
                    if value in (None, ""):
                        continue
                    if key in {"recommended", "strong_recommendations", "strict_strong_anchor_count"} or merged_counts.get(key) in (None, "", 0):
                        merged_counts[key] = value
                payload["counts"] = merged_counts
            for key in ["recommendation_target_count", "recommendation_shortfall", "strict_strong_anchor_count", "recommendation_quality", "coverage_explanation_i18n", "semantics"]:
                if projection.get(key) not in (None, "", []):
                    payload[key] = projection.get(key)
        if isinstance(packet, dict):
            summary = packet.get("summary") if isinstance(packet.get("summary"), dict) else {}
            strict_rows = packet.get("strong_papers") if isinstance(packet.get("strong_papers"), list) else []
            if strict_rows:
                payload["strict_strong_anchors"] = [_artifact_compact_paper_row(row) for row in strict_rows[:20] if isinstance(row, dict)]
                payload.setdefault("strict_strong_anchor_count", len(payload.get("strong_recommendations") or []))
            if summary:
                payload.setdefault("packet_summary", summary)
                payload.setdefault("strict_strong_anchor_count", len(payload.get("strong_recommendations") or []))
                payload.setdefault("recommendation_target_count", summary.get("recommendation_target_count"))
                payload.setdefault("recommendation_shortfall", summary.get("recommendation_shortfall"))
        if isinstance(current_plan, dict) and not payload.get("strong_recommendations"):
            readings = current_plan.get("readings") if isinstance(current_plan.get("readings"), list) else []
            if readings:
                payload["strong_recommendations"] = [_artifact_compact_paper_row(row) for row in readings[:max(len(readings), 1)] if isinstance(row, dict)]
    if survey_stats:
        payload["survey_stats"] = survey_stats
        merged_counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        for key in FIND_SURVEY_COUNT_KEYS:
            value = survey_stats.get(key)
            if value not in (None, ""):
                merged_counts[key] = value
        payload["counts"] = merged_counts
    _hydrate_compact_find_source_state(payload, project_root)
    return _strip_public_taste_marker(_strip_redundant_find_public_json_aliases(payload))


def _job_is_hollow_route(item: dict[str, Any]) -> bool:
    stage = str(item.get("stage") or "").strip().lower()
    status = str(item.get("status") or "").strip().lower()
    if stage not in {"find", "read", "idea", "plan", "plan-polish", "email"} or status not in {"done", "completed"}:
        return False
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    useful_keys = {
        "run_id",
        "artifact_dir",
        "artifact_paths",
        "raw_title_index",
        "retrieval_candidates",
        "title_candidates",
        "evaluated_candidates",
        "screened_ranking",
        "strong_recommendations",
        "read_candidates",
        "ideas",
        "plans",
    }
    has_useful_result = any(result.get(key) for key in useful_keys)
    if has_useful_result:
        return False
    logs = [str(line or "").strip().lower() for line in item.get("logs", []) if str(line or "").strip()]
    if not logs:
        return True
    return logs == [f"{stage} started", f"{stage} complete"]





def _read_project_json(path: Path, default: Any) -> Any:
    try:
        return read_json(path, default)
    except Exception:
        return default


def _read_project_json_if_small(path: Path, default: Any, *, max_bytes: int = LARGE_JSON_ARTIFACT_LIMIT_BYTES) -> Any:
    try:
        if not path.exists() or path.stat().st_size > max_bytes:
            return default
        return read_json(path, default)
    except Exception:
        return default


def _selected_base_public_label(root: Path) -> str:
    selection = _read_project_json(root / "state" / "evidence_ready_repo_selection.json", {})
    selected = selection.get("selected") if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    label = str(
        selected.get("name")
        or selected.get("repo_name")
        or selected.get("repo")
        or selected.get("literature_base_title")
        or selected.get("title")
        or ""
    ).strip()
    return label or "当前基底"


def _selected_repo_path(root: Path) -> Path:
    selection = _read_project_json(root / "state" / "evidence_ready_repo_selection.json", {})
    selected = selection.get("selected") if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    repo_text = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    if repo_text:
        repo_path = Path(repo_text)
        if not repo_path.is_absolute():
            repo_path = (root / repo_path).resolve()
        if repo_path.exists():
            return repo_path
    selected_root = root / "repos" / "selected"
    if selected_root.exists():
        dirs = [path for path in selected_root.iterdir() if path.is_dir()]
        if len(dirs) == 1:
            return dirs[0]
    return Path()


def _taskbgate_projection(root: Path, cycle_status: Any = "", raw_issue: Any = "", raw_next: Any = "") -> dict[str, str]:
    """Human-facing projection for the taskbar.

    Deterministic state files may contain long audit strings. The taskbar should
    show the current research decision and next action, while raw evidence stays
    available through the listed artifact paths.
    """
    status_text = str(cycle_status or "")
    issue_text = str(raw_issue or "")
    next_text = str(raw_next or "")
    hay = "\n".join([status_text, issue_text, next_text]).lower()
    selected_base_gate = _read_project_json(root / "state" / "selected_base_viability_gate.json", {})
    base_switch_gate = _read_project_json(root / "state" / "base_switch_gate.json", {})
    blocker_plan = _read_project_json(root / "state" / "blocker_action_plan.json", {})
    science_gate = _read_project_json(root / "state" / "scientific_progress_gate.json", {})
    ref_gate = _read_project_json(root / "state" / "reference_reproduction_gate.json", {})
    selected_blocked = bool(
        isinstance(selected_base_gate, dict)
        and str(selected_base_gate.get("status") or "").lower() == "blocked"
    )
    science_blocked = bool(
        isinstance(science_gate, dict)
        and str(science_gate.get("status") or "").lower() == "blocked"
    )
    ref_passed = bool(
        isinstance(ref_gate, dict)
        and (str(ref_gate.get("status") or "").lower() == "pass" or str(ref_gate.get("decision") or "").lower() == "continue_base")
    )
    base_switch_required = bool(
        isinstance(selected_base_gate, dict)
        and str(selected_base_gate.get("decision") or "").lower() == "base_switch_gate_required"
    )
    base_switch_authorized = bool(
        isinstance(base_switch_gate, dict)
        and str(base_switch_gate.get("status") or "").lower() == "pass"
        and str(base_switch_gate.get("decision") or "").lower() == "authorize_base_switch"
        and base_switch_gate.get("switch_authorized") is True
    )
    if base_switch_required and not base_switch_authorized:
        plan_summary = blocker_plan.get("summary") if isinstance(blocker_plan, dict) and isinstance(blocker_plan.get("summary"), dict) else {}
        top_action = str(plan_summary.get("top_action") or "").strip()
        if any(marker in top_action.lower() for marker in ["deterministic", "base-switch", "base_switch", "route_scope", "launcher", "selected-base"]):
            top_action = ""
        return {
            "gate": "候选路线仍在取证",
            "issue": "参考复现已通过；候选新路线尚未获得独立授权，当前主线基底保持不变，不能自动切换基底或提升论文结论。",
            "goal": "候选路线仍需补齐可审计证据。",
            "next": "等待 Experimenting 主控 Claude 基于当前候选路线证据给出下一步判断；门控通过前不自动切换基底或提升论文结论。",
        }
    selected_evidence_block = (
        "blocked_selected_base_viability_gate" in hay
        or "selected_base_viability" in hay
        or "no audit-ready promotable" in hay
        or "scientific_progress_gate" in hay
        or "paper_evidence_audit recommends" in hay
        or "evidence_gate_allows_template" in hay
        or selected_blocked
        or (ref_passed and science_blocked)
    )
    if selected_evidence_block:
        return {
            "gate": "缺少主线候选实验证据",
            "issue": "参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验证据。论文稿可以生成检查版，但不能被标记为投稿通过。",
            "goal": "当前基底保持不变，继续补齐当前主线候选实验证据。",
            "next": "等待 Experimenting 主控 Claude 读取当前门控证据后给出下一步实验判断；门控通过前不提升论文结论。",
        }
    if issue_text.strip():
        return {
            "gate": "科研门控阻塞",
            "issue": issue_text.strip()[:500],
            "goal": "当前科研门控未通过，需继续补齐证据。",
            "next": next_text.strip()[:500] or "等待对应模块主控 Claude 读取当前证据后给出下一步判断。",
        }
    return {
        "gate": str(cycle_status or "科研门控").replace("_", " "),
        "issue": "当前科研门控未通过；原始证据保留在 state/report 产物中。",
        "goal": "当前科研门控未通过，需继续补齐证据。",
        "next": next_text.strip()[:500] or "等待对应模块主控 Claude 读取当前证据后给出下一步判断。",
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _current_project_for_find_guard() -> tuple[str, Path] | None:
    try:
        projects = [row for row in list_projects() if _account_owns_project(row.get("id"))]
    except Exception:
        projects = []
    if len(projects) == 1 and isinstance(projects[0], dict):
        row = projects[0]
        project = str(row.get("id") or row.get("name") or "").strip()
        root = Path(str(row.get("path") or "")) if row.get("path") else Path("projects") / project
        if project:
            if not root.is_absolute():
                root = (Path.cwd() / root).resolve()
            return project, root
    return None


def _project_for_find_request(request: FindRequest) -> tuple[str, Path] | None:
    requested_project = str(request.project or "").strip()
    if requested_project:
        return requested_project, _safe_project_root(requested_project)
    return _current_project_for_find_guard()


def _project_context_for_find_run(run_id: str) -> tuple[str, Path] | None:
    project = _project_id_for_find_run(run_id)
    if not project or not _account_owns_project(project):
        return None
    root = (PROJECT_IDS_ROOT / project).resolve()
    if project and (root / "state").exists():
        return project, root
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _new_find_request_reason(request: FindRequest | None) -> str:
    if request is None:
        return ""
    if not (request.force_new_find or request.restart_full_cycle or request.human_approved_new_find):
        return ""
    return str(request.approval_reason or "explicit API request approved a fresh Find run").strip()


def _record_new_find_restart_approval(root: Path, project: str, *, source: str, reason: str) -> None:
    payload = {
        "project": project,
        "approved": True,
        "source": source,
        "reason": reason,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(state_dir / "latest_new_find_restart_approval.json", payload)
    with (state_dir / "new_find_restart_audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")



def _parse_last_json_object_from_lines(lines: list[str]) -> dict[str, Any]:
    joined = "\n".join(lines)
    for start in [idx for idx, char in enumerate(joined) if char == "{"][-16:]:
        try:
            candidate = json.loads(joined[start:])
        except Exception:
            continue
        if isinstance(candidate, dict):
            return candidate
    return {}


def _new_find_guard_blocker(request: FindRequest | None = None) -> dict[str, Any] | None:
    current = _current_project_for_find_guard()
    if not current:
        return None
    project, root = current
    live_full_cycle = _live_full_cycle_for_project(project, root)
    if live_full_cycle:
        return _blocked_by_live_full_cycle_payload(project, "find", live_full_cycle)
    plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
    progress = _read_project_json(root / "planning" / "finding" / "find_progress.json", {})
    packet = _read_project_json(root / "state" / "literature_tool_packet.json", {})
    full_cycle = _read_project_json(root / "state" / "full_research_cycle.json", {})
    gate = plan.get("literature_gate") if isinstance(plan, dict) and isinstance(plan.get("literature_gate"), dict) else {}
    packet_summary = packet.get("summary") if isinstance(packet, dict) and isinstance(packet.get("summary"), dict) else {}
    packet_counts = packet.get("pool_counts") if isinstance(packet, dict) and isinstance(packet.get("pool_counts"), dict) else {}
    statuses = [
        plan.get("status") if isinstance(plan, dict) else "",
        gate.get("status") if isinstance(gate, dict) else "",
        progress.get("recommendation_gate_status") if isinstance(progress, dict) else "",
        packet_summary.get("recommendation_gate_status") if isinstance(packet_summary, dict) else "",
        full_cycle.get("status") if isinstance(full_cycle, dict) else "",
    ]
    strong = next((value for value in [
        _as_int(gate.get("strong_recommendations"), -1),
        _as_int(progress.get("strong_recommendation_count") if isinstance(progress, dict) else None, -1),
        _as_int(packet_summary.get("strong_paper_anchors") if isinstance(packet_summary, dict) else None, -1),
    ] if value >= 0), 0)
    target = next((value for value in [
        _as_int(gate.get("recommendation_target_count"), -1),
        _as_int(progress.get("recommendation_target_count") if isinstance(progress, dict) else None, -1),
        _as_int(packet_summary.get("recommendation_target_count") if isinstance(packet_summary, dict) else None, -1),
        _as_int(packet_counts.get("recommendation_target_count") if isinstance(packet_counts, dict) else None, -1),
    ] if value >= 0), 0)
    shortfall = next((value for value in [
        _as_int(gate.get("recommendation_shortfall"), -1),
        _as_int(progress.get("recommendation_shortfall") if isinstance(progress, dict) else None, -1),
        _as_int(packet_summary.get("recommendation_shortfall") if isinstance(packet_summary, dict) else None, -1),
        _as_int(packet_counts.get("recommendation_shortfall") if isinstance(packet_counts, dict) else None, -1),
    ] if value >= 0), 0)
    if not shortfall and target and strong < target:
        shortfall = target - strong
    marker_blocked = any(
        str(status or "").strip().lower() in {"blocked_literature_recommendation_gate", "shortfall", "recommendation_shortfall"}
        or "literature_recommendation_gate" in str(status or "").strip().lower()
        for status in statuses
    )
    if shortfall <= 0 and not marker_blocked:
        return None
    approval_path = root / "state" / "allow_new_find_once.json"
    approval = _read_project_json(approval_path, {})
    approved = bool(
        isinstance(approval, dict)
        and approval.get("approved") is True
        and str(approval.get("project") or project) == project
    )
    if approved:
        _record_new_find_restart_approval(root, project, source="allow_new_find_once", reason=str(approval.get("reason") or "state/allow_new_find_once.json"))
        return None
    explicit_reason = _new_find_request_reason(request)
    if explicit_reason:
        _record_new_find_restart_approval(root, project, source="api_jobs_find", reason=explicit_reason)
        return None
    return {
        "error": "new_find_blocked_by_current_literature_gate",
        "status": "blocked_new_find_guard",
        "project": project,
        "current_find_run_id": str((progress if isinstance(progress, dict) else {}).get("run_id") or (plan if isinstance(plan, dict) else {}).get("run_id") or ""),
        "strong_recommendations": strong,
        "recommendation_target_count": target,
        "recommendation_shortfall": shortfall,
        "approval_path": str(approval_path),
        "message": "Current Find recommendation gate is short; use force_new_find/restart_full_cycle for an explicit fresh Find, or framework/scripts/main.py module finding --action literature_tool for controlled targeted repair.",
        "message_zh": "当前 Find 推荐门控未过；显式重新 Find 请使用 force_new_find/restart_full_cycle，受控补检索请走 framework/scripts/main.py module finding --action literature_tool。",
    }


def _active_web_stage_job_blocker(project: str, stage: str) -> dict[str, Any] | None:
    """Block overlapping in-process workflow writes for the same project."""
    project = str(project or "").strip()
    stage_key = _public_taste_stage(str(stage or "").strip().lower())
    if not stage_key:
        return None
    _reconcile_stale_cancelling_jobs()
    try:
        with JOBS_LOCK:
            active_jobs = list(JOBS.values())
    except NameError:
        return None
    for job in active_jobs:
        status = str(getattr(job, "status", "") or "").strip().lower()
        if status not in {"queued", "running", "cancelling"}:
            continue
        job_stage = _public_taste_stage(str(getattr(job, "stage", "") or "").strip().lower())
        literature_stages = {"find", "read", "idea", "plan"}
        same_stage_or_literature_pipeline = job_stage == stage_key or (
            job_stage in literature_stages and stage_key in literature_stages
        )
        if not same_stage_or_literature_pipeline:
            continue
        job_project = _project_from_job_payload(getattr(job, "job_id", ""), None, job)
        if project and job_project and job_project != project:
            continue
        progress = getattr(job, "progress", {}) if isinstance(getattr(job, "progress", {}), dict) else {}
        return {
            "error": "project_stage_already_running",
            "status": "blocked_existing_project_stage_running",
            "project": project or job_project,
            "stage": stage_key,
            "existing_job_id": getattr(job, "job_id", ""),
            "existing_run_id": getattr(job, "run_id", ""),
            "existing_status": status,
            "progress": progress,
            "message": "A conflicting workflow job for this project is already running; overlapping writes are blocked.",
            "message_zh": "当前项目已有会写入同一工作流状态的任务正在运行；已阻止重叠启动。",
        }
    return None


def _live_full_cycle_for_project(project: str, root: Path) -> dict[str, Any]:
    state_dir = root / "state"

    def normalize(row: Any, source: str) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        pid = row.get("pid")
        if not _pid_alive_local(pid):
            return {}
        command = str(row.get("command") or row.get("cmd") or "")
        stage = str(row.get("stage") or row.get("child_stage") or row.get("kind") or row.get("status") or "")
        kind = str(row.get("kind") or "")
        if not (
            "run_full_research_cycle.py" in command
            or kind == "full_cycle"
            or stage.startswith("full-cycle")
            or row.get("process_alive") is True
            or row.get("alive") is True
        ):
            return {}
        return {
            "project": project,
            "pid": int(pid),
            "status": "running",
            "process_alive": True,
            "alive": True,
            "kind": "full_cycle",
            "stage": stage or "full-cycle",
            "command": command,
            "log_path": str(row.get("log_path") or row.get("stdout_path") or ""),
            "source": source,
        }

    candidates: list[tuple[str, Any]] = []
    job = _read_project_json(state_dir / "full_cycle_job.json", {})
    candidates.append(("full_cycle_job.json", job))
    full = _read_project_json(state_dir / "full_research_cycle.json", {})
    if isinstance(full, dict):
        candidates.append(("full_research_cycle.full_cycle_job", full.get("full_cycle_job")))
        candidates.append(("full_research_cycle.current_running_stage", full.get("current_running_stage")))
    tick = _read_project_json(state_dir / "supervision_tick.json", {})
    if isinstance(tick, dict):
        candidates.append(("supervision_tick.full_cycle_job", tick.get("full_cycle_job")))
    for source, row in candidates:
        live = normalize(row, source)
        if live:
            return live
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,ppid=,stat=,etimes=,cmd="], text=True, capture_output=True, timeout=3)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    for line in str(proc.stdout or "").splitlines():
        if "run_full_research_cycle.py" not in line:
            continue
        if f"--project {project}" not in line and f"--project={project}" not in line:
            continue
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        pid, _ppid, stat, elapsed, cmd = parts
        if "Z" in stat.upper() or not _pid_alive_local(pid):
            continue
        return {
            "project": project,
            "pid": int(pid),
            "status": "running",
            "process_alive": True,
            "alive": True,
            "kind": "full_cycle",
            "stage": "full-cycle",
            "command": cmd,
            "elapsed_sec": int(elapsed) if str(elapsed).isdigit() else elapsed,
            "source": "ps",
        }
    return {}


def _blocked_by_live_full_cycle_payload(project: str, stage: str, live_full_cycle: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": "full_cycle_already_running",
        "status": "blocked_existing_full_cycle_running",
        "project": project,
        "stage": stage,
        "message": "A full research cycle is already running; duplicate stage launch is blocked.",
        "message_zh": "完整科研流程正在运行；已阻止重复启动新的 Find/Read/Idea/Plan 阶段任务。需要人工介入时请使用对应模块主控指令框。",
        "existing_full_cycle": live_full_cycle,
    }


def _taste_stage_live_full_cycle_blocker(stage: str) -> dict[str, Any] | None:
    current = _current_project_for_find_guard()
    if not current:
        return None
    project, root = current
    live = _live_full_cycle_for_project(project, root)
    if not live:
        return None
    return _blocked_by_live_full_cycle_payload(project, stage, live)


def _pid_alive_local(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        proc = subprocess.run(
            ["ps", "-p", str(value), "-o", "stat="],
            text=True,
            capture_output=True,
            timeout=1,
        )
    except Exception:
        proc = None
    if proc is not None:
        stat = str(proc.stdout or "").strip()
        if proc.returncode != 0 or not stat:
            return False
        if "Z" in stat.upper():
            return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _ps_row_for_pid(pid: Any) -> dict[str, Any]:
    try:
        value = str(int(pid))
    except Exception:
        return {}
    try:
        proc = subprocess.run(
            ["ps", "-p", value, "-o", "pid=,ppid=,stat=,etime=,%cpu=,%mem=,cmd="],
            text=True,
            capture_output=True,
            timeout=1,
        )
    except Exception:
        return {}
    line = next((row.strip() for row in proc.stdout.splitlines() if row.strip()), "")
    if not line:
        return {}
    parts = line.split(None, 6)
    if len(parts) < 7:
        return {}
    if "Z" in str(parts[2]).upper():
        return {}
    return {"pid": parts[0], "ppid": parts[1], "stat": parts[2], "elapsed": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": parts[6]}



def _run_id_from_command_line(command: Any) -> str:
    text = str(command or "")
    if not text:
        return ""
    matches = re.findall(r"(?:^|\s)--run-id(?:=|\s+)([^\s]+)", text)
    if not matches:
        matches = re.findall(r"(?:^|\s)--run_id(?:=|\s+)([^\s]+)", text)
    if matches:
        return matches[-1].strip().strip("'\"")
    match = re.search(r"\b(web_environment_[^\s/]+_\d{8}T\d{6}Z)\b", text)
    if match:
        return match.group(1).strip().strip("'\"")
    return ""


def _suppress_same_phase_descendant_workers(
    rows: list[dict[str, Any]],
    process_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep one taskbar row for a stage wrapper and fold its children into logs.

    A standalone environment/experiment/paper launch often starts a Python
    wrapper which then spawns selector/probe/Claude child processes. Showing each
    child as another project-worker makes the UI look like duplicate launches.
    """
    classified_by_pid = {str(row.get('pid') or ''): row for row in rows if isinstance(row, dict) and str(row.get('pid') or '')}
    process_by_pid = {
        str(row.get("pid") or ""): row
        for row in (process_rows or rows)
        if isinstance(row, dict) and str(row.get("pid") or "")
    }

    def has_same_phase_ancestor(row: dict[str, Any]) -> bool:
        phase = str(row.get('phase') or '').strip().lower()
        current = str(row.get('ppid') or '').strip()
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            parent = process_by_pid.get(current)
            if not parent:
                return False
            classified_parent = classified_by_pid.get(current)
            if classified_parent:
                parent_phase = str(classified_parent.get('phase') or '').strip().lower()
                parent_kind = str(classified_parent.get('kind') or '').strip().lower()
                if phase and parent_phase == phase and parent_kind != 'full_cycle':
                    return True
            current = str(parent.get('ppid') or '').strip()
        return False

    return [row for row in rows if isinstance(row, dict) and not has_same_phase_ancestor(row)]


def _active_project_child_processes(project: str, root: Path, phase_hint: str = "") -> list[dict[str, Any]]:
    markers = [str(project), str(root), str(root / "tmp" / "finding")]
    process_rows = _all_process_rows()
    rows: list[dict[str, Any]] = []
    for proc_row in process_rows:
        cmd = str(proc_row.get("cmd") or "")
        if _is_inspection_or_wrapper_cmd(cmd):
            continue
        cwd = str(proc_row.get("cwd") or "")
        if not any(marker and (marker in cmd or marker in cwd) for marker in markers):
            continue
        lowered = cmd.lower()
        kind = ""
        phase = "full-cycle"
        priority = 99
        current_find_phase, current_find_kind, current_find_priority = _current_find_worker_phase_and_kind(cmd)
        current_find_child = _process_has_current_find_ancestor(proc_row, process_rows)
        if current_find_kind:
            kind = current_find_kind
            phase = current_find_phase
            priority = current_find_priority
        elif current_find_child:
            kind = "current_find_child"
            phase = "read"
            priority = 3
        elif "run_driver.py" in lowered:
            kind = "driver_recovery"
            phase = "literature"
            priority = 0
        elif "run_frontend.py" in lowered:
            kind = "frontend_recovery"
            phase = "literature"
            priority = 1
        elif any(marker in lowered for marker in ["run_environment_stage.py", "run_literature_base_audit.py", "select_evidence_ready_repo.py", "repo_env_bootstrap", "run_selected_base_reference"]) or ("modules/environment/main.py" in lowered) or ("modules/finding/main.py" in lowered and "run_literature_base_audit" in lowered):
            kind = "environment_stage"
            phase = "environment"
            priority = 2
        elif _looks_like_experiment_training_cmd(cmd):
            kind = "experiment_training"
            phase = "experiment"
            priority = 3
        elif "modules/writing/main.py" in lowered and "--action chat" in lowered:
            kind = "paper_claude_session"
            phase = "paper"
            priority = 4
        elif "modules/writing/main.py" in lowered and "--action work" in lowered:
            kind = "paper_pipeline"
            phase = "paper"
            priority = 4
        elif "repair_paper_preview_loop.py" in lowered or "repair_paper_figures_loop.py" in lowered:
            kind = "paper_repair_loop"
            phase = "paper"
            priority = 4
        elif "run_full_research_cycle.py" in lowered:
            kind = "full_cycle"
            phase = "full-cycle"
            priority = 10
        if not kind:
            continue
        rows.append({"pid": proc_row.get("pid"), "ppid": proc_row.get("ppid"), "stat": proc_row.get("stat"), "elapsed": proc_row.get("elapsed"), "pcpu": proc_row.get("pcpu"), "pmem": proc_row.get("pmem"), "cmd": cmd, "cwd": cwd, "kind": kind, "phase": phase, "priority": priority})
    known_pids = {str(row.get("pid") or "") for row in rows}
    for launcher_row in _active_launcher_experiment_runs(root):
        launcher_pid = str(launcher_row.get("pid") or "")
        if launcher_pid and launcher_pid not in known_pids:
            rows.append(dict(launcher_row))
            known_pids.add(launcher_pid)
    rows = _suppress_same_phase_descendant_workers(rows, process_rows)
    target_phase = str(phase_hint or "").strip().lower()
    if target_phase:
        phase_rows = [row for row in rows if str(row.get("phase") or "").strip().lower() == target_phase]
        if phase_rows:
            rows = phase_rows
    rows.sort(key=lambda row: (int(row.get("priority", 99)), int(str(row.get("pid") or "0"))))
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.pop("priority", None)
        cleaned.append(item)
    return cleaned


def _active_project_child_process(project: str, root: Path, phase_hint: str = "") -> dict[str, Any]:
    rows = _active_project_child_processes(project, root, phase_hint=phase_hint)
    return rows[0] if rows else {}


def _process_rows_snapshot() -> list[dict[str, Any]]:
    now = time.monotonic()
    cached_rows = _PROCESS_ROWS_CACHE.get("rows")
    if isinstance(cached_rows, list) and now < float(_PROCESS_ROWS_CACHE.get("expires_at") or 0.0):
        return [dict(row) for row in cached_rows if isinstance(row, dict)]
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,stat=,etime=,%cpu=,%mem=,cmd="],
            text=True,
            capture_output=True,
            timeout=2,
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        if "Z" in str(parts[2]).upper():
            continue
        rows.append({"pid": parts[0], "ppid": parts[1], "stat": parts[2], "elapsed": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": parts[6], "cwd": _proc_cwd(parts[0])})
    _PROCESS_ROWS_CACHE["rows"] = [dict(row) for row in rows]
    _PROCESS_ROWS_CACHE["expires_at"] = time.monotonic() + PROCESS_ROWS_TTL_SEC
    return [dict(row) for row in rows]


def _process_tree_rows(pid: Any) -> list[dict[str, Any]]:
    try:
        root_pid = str(int(pid))
    except Exception:
        return []
    rows_by_pid: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    for row in _process_rows_snapshot():
        pid_text = str(row.get("pid") or "")
        ppid_text = str(row.get("ppid") or "")
        if not pid_text:
            continue
        rows_by_pid[pid_text] = row
        children.setdefault(ppid_text, []).append(pid_text)
    if root_pid not in rows_by_pid:
        return []
    result: list[dict[str, Any]] = []
    stack = [root_pid]
    seen: set[str] = set()
    while stack:
        current = stack.pop(0)
        if current in seen:
            continue
        seen.add(current)
        row = rows_by_pid.get(current)
        if row:
            result.append(dict(row))
        stack.extend(children.get(current, []))
    return result

def _proc_cwd(pid: Any) -> str:
    try:
        return os.path.realpath(os.readlink(f"/proc/{int(pid)}/cwd"))
    except Exception:
        return ""


def _all_process_rows() -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in _process_rows_snapshot()
        if not _is_inspection_or_wrapper_cmd(row.get("cmd"))
    ]


def _active_launcher_experiment_runs(root: Path, *, limit: int = 16) -> list[dict[str, Any]]:
    cache_key = (str(root.resolve() if root.exists() else root), int(limit))
    now = time.monotonic()
    cached = _ACTIVE_LAUNCHER_ROWS_CACHE.get(cache_key)
    if isinstance(cached, dict) and now < float(cached.get("expires_at") or 0.0):
        rows = cached.get("rows")
        return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    artifact_root = root / "artifacts"
    if not artifact_root.exists():
        _ACTIVE_LAUNCHER_ROWS_CACHE[cache_key] = {"expires_at": now + ACTIVE_LAUNCHER_ROWS_TTL_SEC, "rows": []}
        return []
    artifact_dirs: set[Path] = set()
    for pattern in ("**/launcher.pid.json", "**/run_contract.json"):
        try:
            artifact_dirs.update(path.parent for path in artifact_root.glob(pattern) if path.is_file())
        except Exception:
            continue

    def artifact_mtime(directory: Path) -> float:
        mtimes: list[float] = []
        for name in ("launcher.pid.json", "run_contract.json", "stdout_stderr.log"):
            try:
                mtimes.append((directory / name).stat().st_mtime)
            except Exception:
                continue
        return max(mtimes) if mtimes else 0.0

    rows: list[dict[str, Any]] = []
    for artifact in sorted(artifact_dirs, key=artifact_mtime, reverse=True)[: max(1, limit * 4)]:
        try:
            artifact = artifact.resolve()
        except Exception:
            pass
        try:
            if (artifact / "CONTAMINATED_DO_NOT_IMPORT.txt").exists() or (artifact / "FAILED_DO_NOT_IMPORT.txt").exists():
                continue
        except Exception:
            continue
        contract_path = artifact / "run_contract.json"
        pid_path = artifact / "launcher.pid.json"
        contract = _read_project_json(contract_path, {})
        pid_payload = _read_project_json(pid_path, {})
        if not isinstance(contract, dict):
            contract = {}
        if not isinstance(pid_payload, dict):
            pid_payload = {}
        pid = str(pid_payload.get("pid") or contract.get("pid") or "").strip()
        if not pid or not _pid_alive_local(pid):
            continue
        ps_row = _ps_row_for_pid(pid)
        command_value = contract.get("command")
        command_display = str(contract.get("command_display") or "").strip()
        if not command_display and isinstance(command_value, list):
            command_display = " ".join(str(part) for part in command_value)
        cmd = str(ps_row.get("cmd") or command_display).strip()
        stdout_path = str(
            contract.get("stdout_path")
            or (contract.get("expected_outputs") if isinstance(contract.get("expected_outputs"), dict) else {}).get("stdout")
            or artifact / "stdout_stderr.log"
        )
        metadata = contract.get("experiment_metadata") if isinstance(contract.get("experiment_metadata"), dict) else {}
        row = {
            "pid": pid,
            "ppid": ps_row.get("ppid") or "",
            "stat": ps_row.get("stat") or "",
            "elapsed": ps_row.get("elapsed") or "",
            "pcpu": ps_row.get("pcpu") or "",
            "pmem": ps_row.get("pmem") or "",
            "cmd": cmd,
            "cwd": ps_row.get("cwd") or str(contract.get("cwd") or _proc_cwd(pid) or ""),
            "kind": "experiment_training",
            "phase": "experiment",
            "priority": 3,
            "artifact_dir": str(artifact),
            "stdout_path": stdout_path,
            "contract_path": str(contract_path) if contract_path.exists() else "",
            "launcher_sidecar": str(pid_path) if pid_path.exists() else "",
            "dataset": str(metadata.get("dataset") or ""),
            "method": str(metadata.get("method") or ""),
            "source": "launcher_contract",
        }
        rows.append(row)
        if len(rows) >= limit:
            break
    _ACTIVE_LAUNCHER_ROWS_CACHE[cache_key] = {
        "expires_at": time.monotonic() + ACTIVE_LAUNCHER_ROWS_TTL_SEC,
        "rows": [dict(row) for row in rows],
    }
    return rows


def _path_is_within(path_text: Any, parent: Path) -> bool:
    text = str(path_text or "").strip()
    if not text:
        return False
    try:
        path = Path(text).resolve()
        parent_resolved = parent.resolve()
        return path == parent_resolved or parent_resolved in path.parents
    except Exception:
        parent_text = str(parent)
        return text == parent_text or text.startswith(parent_text.rstrip("/") + "/")


def _is_inspection_or_wrapper_cmd(command: Any) -> bool:
    text = str(command or "")
    lowered = text.lower()
    if not text:
        return False
    if any(marker in lowered for marker in ["api/jobs", "api/projects", "urllib.request.urlopen", "http://127.0.0.1:8879/api/", "curl -s http://127.0.0.1:8879/api/"]):
        return True
    if "python - <<" in lowered or "python3 - <<" in lowered or "python3.11 - <<" in lowered or "python -c" in lowered or "python3 -c" in lowered or "python3.11 -c" in lowered:
        inspection_terms = [
            "curl -ss",
            "api/jobs",
            "api/projects",
            "state/full_research_cycle.json",
            "state/reference_reproduction_gate.json",
            "fresh_base_reference_full_reproduction_job.json",
            "pgrep -af",
            "ps -eo",
            "tail -n",
            "json.loads",
            "read_text",
        ]
        if any(term in lowered for term in inspection_terms):
            return True
    shell_prefixes = ("bash -c", "sh -c", "zsh -c")
    if lowered.strip().startswith(shell_prefixes) and any(marker in lowered for marker in ["curl -ss", "pgrep -af", "ps -eo", "sed -n", "tail -n"]):
        return True
    return False


def _looks_like_experiment_training_cmd(command: Any) -> bool:
    lowered = str(command or "").lower()
    if not lowered:
        return False
    if _is_inspection_or_wrapper_cmd(command):
        return False
    if "finetune.py" in lowered or "finetune_llm.py" in lowered:
        return True
    if "exp_text_init_standard_train.py" in lowered or "exp_text_init" in lowered:
        return True
    if "python" in lowered and "--artifact_dir" in lowered and "/artifacts/" in lowered:
        return True
    if re.search(r"(?:^|\s)(?:\S*/)?(?:finetune|train)[\w.-]*\.py\b", lowered) and ("python" in lowered or "conda" in lowered):
        return True
    # Some project entrypoints use main.py for reference/pretrain runs. Treat it as an active
    # experiment only when a dataset flag is present, so generic main.py tools
    # outside the project do not pollute the taskbar.
    if re.search(r"(?:^|\s)(?:\S*/)?main\.py\b", lowered) and re.search(r"(?:^|\s)--data(?:=|\s+)", lowered):
        return True
    pattern = r"(?:^|\s)(?:python\S*|conda)(?:\s+\S+){0,10}\s+(?:-u\s+)?(?:\S*/)?(?:finetune|train)[\w.-]*\.py\b"
    return bool(re.search(pattern, lowered))


def _launcher_experiment_row(row: dict[str, Any]) -> bool:
    return bool(str(row.get("artifact_dir") or "").strip() and (str(row.get("stdout_path") or "").strip() or str(row.get("contract_path") or "").strip()))


def _row_is_active_experiment_training(row: dict[str, Any]) -> bool:
    return _looks_like_experiment_training_cmd(row.get("cmd")) or _launcher_experiment_row(row)


def _is_contaminated_artifact_log(path: Path) -> bool:
    try:
        parent = path.parent
        return (parent / "CONTAMINATED_DO_NOT_IMPORT.txt").exists()
    except Exception:
        return False


def _candidate_experiment_log_paths(root: Path, command: Any, experiment_start_ts: float = 0.0) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_path(path_text: Any) -> None:
        raw = str(path_text or "").strip().strip("'\"")
        if not raw:
            return
        try:
            path = Path(raw)
            if not path.is_absolute():
                path = (root / path).resolve()
            if not path.exists() or not path.is_file():
                return
            if _is_contaminated_artifact_log(path):
                return
            if experiment_start_ts and path.stat().st_mtime + 60 < experiment_start_ts:
                return
            key = str(path)
            if key in seen:
                return
            seen.add(key)
            candidates.append(path)
        except Exception:
            return

    command_text = str(command or "")
    for match in re.finditer(r"(?:^|\s)(?:2>&1\s*)?(?:>>?|tee(?:\s+-a)?)\s+([^\s;&|]+\.log)\b", command_text):
        add_path(match.group(1))

    # Claude Code often starts training in a subprocess and only records the
    # redirected stdout path in the supervision log, not in the python ps row.
    supervision_log = _latest_project_log(root, "logs/supervision/full_research_cycle_*.log")
    if supervision_log:
        try:
            for line in _tail_file_lines(Path(supervision_log), limit=180, max_bytes=262144):
                lowered = line.lower()
                if not any(marker in lowered for marker in ("experiment", "finetune", "text_embed", "text_init", "artifact_dir", "stdout_stderr.log", "semantic")):
                    continue
                for match in re.finditer(r"(/tmp/[^\s\"'`]+?\.log)\b", line):
                    add_path(match.group(1))
        except Exception:
            pass

    descri_match = re.search(r"(?:^|\s)--descri(?:=|\s+)([^\s]+)", command_text)
    data_match = re.search(r"(?:^|\s)--data(?:=|\s+)([^\s]+)", command_text)
    tmp_patterns = ["experiment*.log"]
    if descri_match:
        token = re.sub(r"[^A-Za-z0-9_.-]", "", descri_match.group(1))
        if token:
            tmp_patterns.extend([f"*{token}*.log", f"*{token.replace('_30epoch', '')}*.log"])
    if data_match:
        token = re.sub(r"[^A-Za-z0-9_.-]", "", data_match.group(1))
        if token:
            tmp_patterns.append(f"*{token}*.log")
    try:
        for pattern in tmp_patterns:
            for path in Path("/tmp").glob(pattern):
                add_path(path)
    except Exception:
        pass
    return candidates


def _is_machine_index_log_line(line: Any) -> bool:
    text = str(line or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if re.fullmatch(r"[{}\[\],]+", text):
        return True
    if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*"?/home/[^"\n]+"?,?', text):
        return True
    if text.lower().startswith("full-cycle: running /home/") and "/scripts/" in text.lower():
        return True
    if text.startswith("/home/") and " " not in text:
        return True
    if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*(?:"[^"\n]*"|true|false|null|\d+(?:\.\d+)?|[{}\[\]])\s*,?', text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*"?[^"\n]*\.(json|md|csv|txt|log|tex|pdf)"?,?', text, flags=re.IGNORECASE):
        return True
    if text.startswith(("log_tail=", "full_cycle_output=")):
        payload = text.split("=", 1)[1].strip()
        payload_lower = payload.lower()
        if re.fullmatch(r"[{}\[\],]+", payload):
            return True
        if payload_lower.startswith("full-cycle: running /home/") and "/scripts/" in payload_lower:
            return True
        if payload.startswith("/home/") and " " not in payload:
            return True
        if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*"?/home/[^"\n]+"?,?', payload):
            return True
        if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*(?:"[^"\n]*"|true|false|null|\d+(?:\.\d+)?|[{}\[\]])\s*,?', payload, flags=re.IGNORECASE):
            return True
        if payload.count(str(WORKSPACE_ROOT) + "/") >= 1 and (
            payload.count('"') >= 2
            or payload_lower.endswith((".json", ".md", ".csv", ".txt", ".log", ".tex", ".pdf", '",', '"'))
        ):
            return True
        machine_state_markers = (
            "research_landscape_assessment",
            "evolutionary_memory_ledger",
            "trajectory_optimization_plan",
            "trajectory_checkpoints",
            "evolutionary_memory_index",
            "evoscientist_cycle_summary",
            "recoverable_cycle_summary",
            "evidence_review_board",
            "research_skill_contracts",
            "trajectory_execution_protocol",
            "research_trajectory_capability_audit",
            "research_trajectory_end_to_end_verification",
            "writing_state",
            "writing_bridge",
            "paper_normality_audit",
            "third_party_research_stack",
        )
        if any(marker in payload_lower for marker in machine_state_markers):
            return True
    return False


def _is_low_signal_claude_tool_line(line: Any) -> bool:
    text = str(line or "").strip()
    lowered = text.lower()
    if not text:
        return True
    if _is_machine_index_log_line(text):
        return True
    if re.fullmatch(r"[|:\-\s]+", text):
        return True
    if "|" in text and re.fullmatch(r"[|:\-\sA-Za-z0-9@._%/+]+", text):
        return True
    if lowered.startswith("claude: still running; waiting for claude code output"):
        return True
    if lowered.startswith("claude: let me let it run"):
        return True
    if lowered.startswith("claude: workspace=") or lowered.startswith("workspace="):
        return True
    if lowered.startswith("claude: saved session result") or lowered.startswith("claude: status=") or lowered.startswith("claude: result"):
        return True
    low_signal_markers = (
        "subprocess pid",
        "parsed metrics",
        "metrics.json",
        "artifacts created in",
        "saved session result to",
        "running /home/",
        "项目内证据文件 --project",
    )
    if any(marker in lowered for marker in low_signal_markers):
        return True
    if re.fullmatch(r"claude:\s*(epoch\s+0:.*|0?\.?\d{3,}|\d+\.?\d*)", lowered):
        return True
    # Claude Code tool-call chatter is not useful in the taskbar. Keep
    # natural-language conclusions and real stdout/log tails instead.
    if "调用工具:" in text:
        return True
    if "bash command=" in lowered and any(marker in lowered for marker in ("sleep ", "nvidia-smi", "tail -", "wc -c", "ps -p")):
        return True
    unverified_comparison_markers = (
        " better",
        "improvement",
        "outperform",
        "out-performing",
        "trending up",
        "clear upward trend",
        "results!",
        "% result",
        "produced the",
    )
    if any(marker in lowered for marker in unverified_comparison_markers):
        return True
    if "+" in lowered and any(marker in lowered for marker in ("result", "improvement", "better", "outperform", "%")):
        return True
    if len(text) < 18 and not any(marker in lowered for marker in ("ready", "running", "blocked", "completed", "生成", "运行")):
        return True
    if lowered in {"good.", "now i have a clear picture.", "let me", "this avoids re-running api calls.", "alternative approaches.", ").", "."}:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?\s*\([^)]{8,}\)\.?", lowered):
        return True
    if re.fullmatch(r"[a-z0-9_./-]+`?\s*(doesn'?t exist\.?|is\.?|was\.?)", lowered):
        return True
    # The taskbar must reflect audited gates, not Claude narration from a stale
    # or speculative route. Keep commands/logs/metrics, but suppress promotion
    # or legacy-route claims that contradict state/*.json gates.
    unaudited_gate_or_route_markers = (
        "no remaining blockers",
        "all 26 submission readiness checks pass",
        "submission readiness checks pass",
        "promotion gate is `allow-template`",
        "promotion gate is allow-template",
        "allow-template",
        "paper promotion",
        "proceed to experiment execution on non-current route",
        "alternative reference",
        "sasrec baseline",
        "train_llm_cond_diff.py",
    )
    if any(marker in lowered for marker in unaudited_gate_or_route_markers):
        return True
    if lowered.startswith("claude:") and any(marker in lowered for marker in ("trend", "flat", "excellent result", "consistent")):
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s*[x×]\b", lowered) and any(marker in lowered for marker in ("better", "improvement", "outperform")):
        return True
    # Live process rows and active stdout/stderr fd logs are authoritative.
    # Suppress generic stale duplicate-writer narration; do not encode project routes here.
    stale_duplicate_markers = ("stale duplicate", "duplicate writer", "terminated duplicate", "killed duplicate", "stopped duplicate")
    stale_running_markers = ("is running", "let me let it run", "let it run to completion", "alive")
    if any(marker in lowered for marker in stale_duplicate_markers) and any(marker in lowered for marker in stale_running_markers):
        return True
    return False



def _is_stale_or_internal_full_cycle_tail(value: Any) -> bool:
    lowered = str(value or '').lower()
    markers = (
        'authorize base switch',
        'base-switch',
        'base switch',
        'negative-result paper',
        'negative result paper',
        'honest negative-result',
        'honest negative result',
        'scope paper to a narrower unsupported route',
        'exclude llm claims',
        'candidate repos:',
        'legacy route',
        'non-current route',
        'non current route',
        'stale route',
        'stale claim',
        'unauthorized route switch',
        'unverified route switch',
        'structural metadata limitation',
        'paper claims are supported',
        'submission blockers cleared',
        'allow-template',
        'paper production',
        'needs_final_packaging',
        '### next actions',
        '### commands run',
        '### evidence',
        '### still blocked',
        'root cause',
        'files changed',
        'state/data_unavailability_policy.json',
        'state/repo_viability_assessment.json',
        'memory.md updated',
    )
    return any(marker in lowered for marker in markers)


def _tail_file_lines(path: Path, *, limit: int = 20, max_bytes: int = 65536) -> list[str]:
    try:
        if not path.exists() or not path.is_file():
            return []
        size = path.stat().st_size
        if size <= 0:
            return []
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    lines = [line.strip() for line in data.splitlines() if line.strip()]
    return lines[-limit:]


def _active_process_output_log_paths(pids: list[str]) -> set[str]:
    paths: set[str] = set()
    for raw_pid in pids:
        pid = re.sub(r"\D", "", str(raw_pid or ""))
        if not pid:
            continue
        for fd in ("1", "2"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            target = target.split(" (deleted)", 1)[0].strip()
            if not target or target.startswith(("pipe:", "socket:", "anon_inode:")):
                continue
            try:
                output_path = Path(target)
                if not output_path.is_absolute() or not output_path.exists() or not output_path.is_file():
                    continue
                paths.add(str(output_path.resolve()))
            except Exception:
                continue
    return paths


def _active_process_output_logs_by_pid(pids: list[str]) -> dict[str, set[str]]:
    by_pid: dict[str, set[str]] = {}
    for raw_pid in pids:
        pid = re.sub(r"\D", "", str(raw_pid or ""))
        if not pid:
            continue
        paths: set[str] = set()
        for fd in ("1", "2"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            target = target.split(" (deleted)", 1)[0].strip()
            if not target or target.startswith(("pipe:", "socket:", "anon_inode:")):
                continue
            try:
                output_path = Path(target)
                if output_path.is_absolute() and output_path.exists() and output_path.is_file():
                    paths.add(str(output_path.resolve()))
            except Exception:
                continue
        by_pid[pid] = paths
    return by_pid


def _command_dataset_label(command: Any) -> str:
    text = str(command or "")
    match = re.search(r"(?:^|\s)--data(?:=|\s+)([^\s]+)", text)
    if match:
        value = re.sub(r"[^A-Za-z0-9_.-]", "", match.group(1))
        if value:
            return value
    return "run"


def _active_training_run_lines(training_rows: list[dict[str, Any]], output_logs_by_pid: dict[str, set[str]]) -> list[str]:
    lines: list[str] = []
    child_pids_with_logs = {
        str(row.get("pid") or "")
        for row in training_rows
        if output_logs_by_pid.get(str(row.get("pid") or ""))
    }
    parent_pids_with_logged_children = {
        str(row.get("ppid") or "")
        for row in training_rows
        if str(row.get("pid") or "") in child_pids_with_logs
    }
    for row in training_rows[:6]:
        pid = str(row.get("pid") or "")
        cmd = str(row.get("cmd") or "")
        paths = sorted(output_logs_by_pid.get(pid) or [])
        if not paths and pid in parent_pids_with_logged_children:
            continue
        label = str(row.get("dataset") or "").strip() or _command_dataset_label(cmd)
        path = paths[0] if paths else str(row.get("stdout_path") or "")
        status = "输出日志已连接" if path and _tail_file_lines(Path(path), limit=1) else "训练进程运行中，当前输出日志为空或等待缓冲" if path else "训练进程运行中，未发现 stdout/stderr 文件路径"
        elapsed = row.get("elapsed")
        line = f"experiment_run={label}; pid={pid}; elapsed={elapsed}; log={path or '-'}; status={status}; cmd={cmd[:500]}"
        lines.append(line)
    return lines


def _latest_project_log(root: Path, pattern: str) -> str:
    try:
        candidates = [path for path in root.glob(pattern) if path.is_file()]
    except Exception:
        candidates = []
    if not candidates:
        return ""
    try:
        return str(sorted(candidates, key=lambda item: item.stat().st_mtime)[-1])
    except Exception:
        return str(candidates[-1])


def _launcher_artifact_registry_status_message(root: Path, artifact: Path) -> str:
    try:
        artifact = artifact.resolve()
    except Exception:
        pass
    artifact_name = str(artifact.name or "").strip()
    artifact_text = str(artifact)
    if not artifact_name:
        return ""

    registry_payload = _read_project_json(root / "state" / "experiment_registry.json", [])
    registry_rows = registry_payload if isinstance(registry_payload, list) else registry_payload.get("experiments", []) if isinstance(registry_payload, dict) else []
    matched_row: dict[str, Any] = {}
    for row in registry_rows if isinstance(registry_rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_artifact = str(row.get("artifact_path") or "")
        row_id = str(row.get("experiment_id") or row.get("name") or "")
        if row_id == artifact_name or row_artifact == artifact_text or row_artifact.endswith("/" + artifact_name):
            matched_row = row
            break
    if matched_row:
        audit_ready = bool(matched_row.get("audit_ready"))
        promotion = str(matched_row.get("promotion_status") or matched_row.get("evidence_status") or matched_row.get("claim_verdict") or "").strip().lower()
        comparison = str(matched_row.get("comparison_status") or "").strip().lower()
        metric_name = str(matched_row.get("metric_name") or "").strip()
        metric_value = matched_row.get("metric_value")
        metric_part = f"；{metric_name}={metric_value}" if metric_name and metric_value not in (None, "") else ""
        if audit_ready and ("not" in promotion or "candidate_observation_only" in promotion or "unsupported" in promotion or "not_above" in comparison):
            return "最近一次实验训练已结束并已登记审计" + metric_part + "；当前结果未通过科研进展门控，等待 Experimenting 主控 Claude 基于失败证据规划下一步。"
        if audit_ready:
            return "最近一次实验训练已结束并已登记审计" + metric_part + "；正在等待科研进展门控给出下一步。"
        return "最近一次实验训练已结束并已写入实验登记" + metric_part + "；等待审计和门控刷新。"

    gate = _read_project_json(root / "state" / "scientific_progress_gate.json", {})
    if isinstance(gate, dict):
        non_promotable = gate.get("non_promotable_candidate_runs") if isinstance(gate.get("non_promotable_candidate_runs"), list) else []
        if artifact_name in {str(item) for item in non_promotable}:
            return "最近一次实验训练已结束并已被科研进展门控识别；当前候选结果不可提升，等待 Experimenting 主控 Claude 基于负向证据规划下一步。"
    return ""


def _active_detail_lines(root: Path, full_cycle_pid: Any, phase: str) -> tuple[list[str], list[str]]:
    if phase != "experiment":
        return [], []
    logs: list[str] = []
    artifacts: list[str] = []
    current_repo_path = _selected_repo_path(root)
    process_rows = _process_tree_rows(full_cycle_pid)
    seen_pids = {str(row.get("pid") or "") for row in process_rows}
    if current_repo_path.exists():
        for row in _all_process_rows():
            pid_text = str(row.get("pid") or "")
            if not pid_text or pid_text in seen_pids:
                continue
            cmd = str(row.get("cmd") or "")
            if not _looks_like_experiment_training_cmd(cmd):
                continue
            if not _path_is_within(row.get("cwd"), current_repo_path):
                continue
            process_rows.append(row)
            seen_pids.add(pid_text)
    launcher_rows = _active_launcher_experiment_runs(root)
    launcher_by_pid = {str(row.get("pid") or ""): row for row in launcher_rows if str(row.get("pid") or "")}
    launcher_fields = ["kind", "phase", "priority", "artifact_dir", "stdout_path", "contract_path", "launcher_sidecar", "dataset", "method", "source"]
    for row in process_rows:
        launcher_row = launcher_by_pid.get(str(row.get("pid") or ""))
        if not launcher_row:
            continue
        for key in launcher_fields:
            if launcher_row.get(key) not in (None, ""):
                row[key] = launcher_row.get(key)
        if not str(row.get("cmd") or "").strip():
            row["cmd"] = str(launcher_row.get("cmd") or "")
        if not str(row.get("cwd") or "").strip():
            row["cwd"] = str(launcher_row.get("cwd") or "")
    for launcher_row in launcher_rows:
        pid_text = str(launcher_row.get("pid") or "")
        if pid_text and pid_text not in seen_pids:
            process_rows.append(dict(launcher_row))
            seen_pids.add(pid_text)
    interesting: list[tuple[int, dict[str, Any], str]] = []
    for row in process_rows:
        cmd = str(row.get("cmd") or "")
        lowered = cmd.lower()
        label = ""
        priority = 99
        if _row_is_active_experiment_training(row):
            label = "实验训练进程"
            priority = 0
            if "llm_candidate" in lowered:
                priority = -3
            elif "conda run" in lowered:
                priority = -2
            elif "finetune_llm.py" in lowered and "nohup" not in lowered:
                priority = -1
        elif "run_autonomous_research.py" in lowered:
            label = "TASTE 自主科研"
            priority = 2
        elif "run_full_research_cycle.py" in lowered:
            label = "TASTE 完整循环"
            priority = 3
        elif "/bin/claude" in lowered or lowered.endswith("/claude") or " /claude " in lowered:
            label = "Claude Code"
            priority = 4
        elif "conda run" in lowered:
            label = "实验环境命令"
            priority = 5
        if label:
            interesting.append((priority, row, label))
    interesting.sort(key=lambda item: (item[0], int(str(item[1].get("pid") or "0"))))
    experiment_cmd = ""
    experiment_pids: list[str] = []
    for _priority, row, label in interesting[:10]:
        cmd = str(row.get("cmd") or "")
        if _row_is_active_experiment_training(row):
            experiment_pids.append(str(row.get("pid") or ""))
        if not experiment_cmd and _row_is_active_experiment_training(row):
            experiment_cmd = cmd
        logs.append(
            "process="
            + f"{label}; pid={row.get('pid')}; elapsed={row.get('elapsed')}; cpu={row.get('pcpu')}%; mem={row.get('pmem')}%; cmd={cmd[:700]}"
        )
    if experiment_cmd:
        logs.insert(0, "experiment_cmd=" + experiment_cmd[:900])
    experiment_cmds = [
        str(row.get("cmd") or "")
        for _priority, row, _label in interesting
        if _row_is_active_experiment_training(row)
    ]
    training_rows = [
        row
        for _priority, row, _label in interesting
        if _row_is_active_experiment_training(row)
    ]
    output_logs_by_pid = _active_process_output_logs_by_pid(experiment_pids)
    logs.extend(_active_training_run_lines(training_rows, output_logs_by_pid))

    experiment_start_ts = 0.0
    for pid in experiment_pids:
        try:
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists():
                continue
            ticks = int(stat.read_text().split()[21])
            clk_tck = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
            boot_time = 0.0
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    boot_time = float(line.split()[1])
                    break
            if boot_time and clk_tck:
                started = boot_time + (ticks / float(clk_tck))
                experiment_start_ts = started if not experiment_start_ts else min(experiment_start_ts, started)
        except Exception:
            continue
    active_output_logs = set().union(*output_logs_by_pid.values()) if output_logs_by_pid else _active_process_output_log_paths(experiment_pids)
    active_log_paths: set[str] = set(active_output_logs)

    def _command_flag_value(command: str, flag: str) -> str:
        match = re.search(rf"(?:^|\s)--{re.escape(flag)}(?:=|\s+)([^\s]+)", command)
        if not match:
            return ""
        return re.sub(r"[^A-Za-z0-9_.-]", "", match.group(1))

    def _existing_log_key(path: Path) -> str:
        try:
            if path.exists() and path.is_file():
                return str(path.resolve())
        except Exception:
            return ""
        return ""

    def _add_active_log_candidate(path: Path) -> None:
        key = _existing_log_key(path)
        if key:
            active_log_paths.add(key)

    def _add_active_log_glob(pattern: str) -> None:
        try:
            for path in (current_repo_path / "log").glob(pattern):
                _add_active_log_candidate(path)
        except Exception:
            return

    def _register_active_command_logs(command: str) -> None:
        if not current_repo_path.exists():
            return
        log_dir = current_repo_path / "log"
        if not log_dir.exists():
            return
        data = _command_flag_value(command, "data")
        descri = _command_flag_value(command, "descri")
        epoch = _command_flag_value(command, "epoch")
        lowered = command.lower()
        if data and descri:
            _add_active_log_candidate(log_dir / f"finetune_{descri}.log")
            no_epoch_descri = re.sub(r"_\d+epoch$", "", descri)
            if no_epoch_descri != descri:
                _add_active_log_candidate(log_dir / f"finetune_{no_epoch_descri}.log")
            if descri.startswith(data + "_"):
                suffix = descri[len(data) + 1:]
                no_epoch_suffix = re.sub(r"_\d+epoch$", "", suffix)
                _add_active_log_candidate(log_dir / f"finetune_{data}_{suffix}.log")
                _add_active_log_candidate(log_dir / f"finetune_{data}_{no_epoch_suffix}.log")
        if data and "finetune_llm.py" in lowered:
            if epoch:
                _add_active_log_candidate(log_dir / f"finetune_{data}_llm_{epoch}epoch.log")
                _add_active_log_glob(f"finetune_{data}_llm*{epoch}epoch*.log")
            elif descri:
                _add_active_log_glob(f"finetune_{data}_llm*{descri}*.log")
            else:
                _add_active_log_candidate(log_dir / f"finetune_{data}_llm.log")
        elif data and "finetune.py" in lowered and descri:
            suffix = descri[len(data) + 1:] if descri.startswith(data + "_") else descri
            suffix = re.sub(r"_\d+epoch$", "", suffix)
            if suffix:
                _add_active_log_candidate(log_dir / f"finetune_{data}_{suffix}.log")

    for row in training_rows:
        artifact_dir = str(row.get("artifact_dir") or "").strip()
        stdout_path = str(row.get("stdout_path") or "").strip()
        if artifact_dir and artifact_dir not in artifacts:
            artifacts.append(artifact_dir)
            logs.append("experiment_artifact=" + artifact_dir)
        if stdout_path:
            _add_active_log_candidate(Path(stdout_path))

    for active_command in experiment_cmds:
        _register_active_command_logs(active_command)

    exp_root = root / "artifacts" / "fresh_base_experiments"
    try:
        exp_dirs = [path for path in exp_root.glob("*") if path.is_dir()] if exp_root.exists() else []
        if experiment_start_ts:
            exp_dirs = [path for path in exp_dirs if path.stat().st_mtime + 1 >= experiment_start_ts]
        exp_dirs = sorted(exp_dirs, key=lambda item: item.stat().st_mtime, reverse=True)[:3]
        if not experiment_cmds and not active_log_paths:
            exp_dirs = []
    except Exception:
        exp_dirs = []
    for directory in exp_dirs:
        artifacts.append(str(directory))
        logs.append("experiment_artifact=" + str(directory))

    log_candidates: list[Path] = []
    for path_text in active_log_paths:
        log_candidates.append(Path(path_text))
    command_log_candidates: list[Path] = []
    for active_command in experiment_cmds or [experiment_cmd]:
        command_log_candidates.extend(_candidate_experiment_log_paths(root, active_command, experiment_start_ts))
    log_candidates.extend(command_log_candidates)
    # When live training commands exist, only stdout/stderr fd targets and
    # command-derived log paths are current. Broad artifact globs are a fallback
    # for completed/no-command states; otherwise parallel or stale runs can steal
    # the black-log tail from the active run.
    if not experiment_cmds and not active_log_paths:
        try:
            if exp_root.exists():
                log_candidates.extend(path for path in exp_root.glob("**/stdout*.log") if path.is_file())
                log_candidates.extend(path for path in exp_root.glob("**/*.log") if path.is_file())
        except Exception:
            pass
        try:
            artifact_root = root / "artifacts"
            if artifact_root.exists():
                log_candidates.extend(path for path in artifact_root.glob("**/stdout*.log") if path.is_file())
                log_candidates.extend(path for path in artifact_root.glob("**/*.log") if path.is_file())
        except Exception:
            pass
        try:
            if current_repo_path.exists():
                log_candidates.extend(path for path in current_repo_path.glob("log/*.log") if path.is_file())
                log_candidates.extend(path for path in current_repo_path.glob("*.log") if path.is_file())
        except Exception:
            pass
    unique_logs: list[Path] = []
    seen_paths: set[str] = set()
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0
    for path in sorted(log_candidates, key=_mtime, reverse=True):
        if _is_contaminated_artifact_log(path):
            continue
        key = str(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        unique_logs.append(path)
    # Prefer real log files over Claude internal task output files. If no .log
    # exists, keep the raw fd target as a fallback so the taskbar is not blank.
    if any(path.suffix.lower() == ".log" for path in unique_logs):
        unique_logs = [path for path in unique_logs if path.suffix.lower() == ".log"]
    nonempty_logs = [path for path in unique_logs if _tail_file_lines(path, limit=1)]
    empty_logs = [path for path in unique_logs if path not in nonempty_logs]
    current_nonempty_logs: list[Path] = []
    stale_nonempty_logs: list[Path] = []

    def _resolved_log_path(path: Path) -> str:
        try:
            return str(path.resolve())
        except Exception:
            return str(path)

    def _is_tmp_experiment_log(path: Path) -> bool:
        try:
            return path.is_absolute() and str(path).startswith("/tmp/") and path.name.startswith("experiment")
        except Exception:
            return False

    def _is_recent_current_repo_training_log(path: Path) -> bool:
        try:
            name = path.name.lower()
            return _path_is_within(path, current_repo_path / "log") and name.startswith(("finetune_", "train_", "main_")) and name.endswith(".log")
        except Exception:
            return False

    def _is_launcher_experiment_stdout_log(path: Path) -> bool:
        try:
            if path.name.lower() != "stdout_stderr.log":
                return False
            if not _path_is_within(path, root / "artifacts"):
                return False
            return (path.parent / "run_contract.json").exists() or (path.parent / "launcher.pid.json").exists()
        except Exception:
            return False

    def _log_tail_error(path: Path) -> str:
        for line in reversed(_tail_file_lines(path, limit=36, max_bytes=131072)):
            lowered = line.lower()
            if "traceback" in lowered or "error" in lowered or "exception" in lowered or "typeerror" in lowered:
                return line[:260]
        return ""

    recent_completed_mode = False
    if not experiment_cmd:
        cutoff = time.time() - 1800
        current_nonempty_logs = []
        for path in nonempty_logs:
            try:
                resolved = _resolved_log_path(path)
                if path.stat().st_mtime < cutoff:
                    continue
                parent_name = path.parent.name.lower()
                if (
                    _is_tmp_experiment_log(path)
                    or _is_recent_current_repo_training_log(path)
                    or _is_launcher_experiment_stdout_log(path)
                    or (path.name.lower() in {"output.log", "stdout_stderr.log"} and _is_recent_current_repo_training_log(path))
                ):
                    current_nonempty_logs.append(path)
            except Exception:
                continue
        # For a completed/no-active-command state, show only the most recent
        # completed training log. Including older completed logs corrupts the
        # current job progress and makes the taskbar look like a parallel run.
        current_nonempty_logs = current_nonempty_logs[:1]
        for path in current_nonempty_logs:
            resolved = _resolved_log_path(path)
            if resolved not in artifacts:
                artifacts.append(resolved)
        if current_nonempty_logs:
            recent_completed_mode = True
            status_message = _launcher_artifact_registry_status_message(root, current_nonempty_logs[0].parent)
            if not status_message:
                status_message = "最近一次实验训练已结束；下方展示该已完成训练的真实日志尾部，等待 Experimenting 主控 Claude 登记审计和刷新门控。"
            logs.append("experiment_output_status=" + status_message)
        else:
            logs.append("experiment_output_status=当前没有检测到活跃训练命令；任务栏仅展示当前 TASTE 子进程和 full-cycle 日志，不把历史训练日志当作当前输出。")
            return logs, artifacts

    ended_after_start_logs: list[Path] = []
    for path in nonempty_logs:
        try:
            if recent_completed_mode:
                continue
            resolved = _resolved_log_path(path)
            if active_log_paths and resolved in active_log_paths:
                current_nonempty_logs.append(path)
                continue
            if active_log_paths and _is_recent_current_repo_training_log(path) and resolved not in active_log_paths:
                if experiment_start_ts and path.stat().st_mtime + 1 < experiment_start_ts:
                    stale_nonempty_logs.append(path)
                else:
                    ended_after_start_logs.append(path)
                continue
            if active_log_paths and _is_tmp_experiment_log(path) and resolved not in active_log_paths:
                continue
            if experiment_start_ts and path.stat().st_mtime + 1 < experiment_start_ts:
                stale_nonempty_logs.append(path)
            else:
                current_nonempty_logs.append(path)
        except Exception:
            current_nonempty_logs.append(path)
    current_empty_logs: list[Path] = []
    if not recent_completed_mode:
        for path in empty_logs:
            try:
                if active_log_paths and _is_recent_current_repo_training_log(path) and _resolved_log_path(path) not in active_log_paths:
                    continue
                if not experiment_start_ts or path.stat().st_mtime + 1 >= experiment_start_ts:
                    current_empty_logs.append(path)
            except Exception:
                if not experiment_start_ts:
                    current_empty_logs.append(path)
    # Once the active training has a non-empty log, do not mix unrelated empty
    # legacy logs from old routes into the current experiment taskbar row.
    if current_nonempty_logs or current_empty_logs:
        display_logs = (current_nonempty_logs + current_empty_logs + ended_after_start_logs + stale_nonempty_logs)[:5]
    else:
        display_logs = (ended_after_start_logs + stale_nonempty_logs)[:4]
    for path in display_logs:
        if path not in stale_nonempty_logs and str(path) not in artifacts:
            artifacts.append(str(path))
        if path in current_empty_logs:
            suffix = "; empty_or_waiting_for_output=true"
        elif path in stale_nonempty_logs:
            suffix = "; stale_before_current_process=true"
        elif path in ended_after_start_logs:
            suffix = "; completed_or_exited_after_current_start=true"
            if _log_tail_error(path):
                suffix += "; crashed_or_errored=true"
        else:
            suffix = ""
        logs.append("experiment_log=" + str(path) + suffix)
    for path in ended_after_start_logs[:2]:
        label = _experiment_source_label(str(path))
        error_tail = _log_tail_error(path)
        status_line = f"{label} 日志已无对应活跃训练进程；不计入当前运行进度"
        if error_tail:
            status_line += f"；尾部错误={error_tail}"
        logs.append("experiment_output_status=" + status_line[:700])
    for path in current_nonempty_logs[:3]:
        tail_lines = _tail_file_lines(path, limit=14)
        if tail_lines:
            logs.append("experiment_output_source=" + str(path))
            for line in tail_lines[-12:]:
                logs.append("experiment_output=" + line[:700])
    if experiment_cmd and not current_nonempty_logs:
        pid_note = f"；PID={experiment_pids[-1]}" if experiment_pids else ""
        logs.append("experiment_output_status=当前实验训练进程仍在运行" + pid_note + "；尚未发现晚于当前进程启动时间的非空 epoch/指标日志，任务栏不会把旧测试日志当作当前 full-run 输出。")
    return logs, artifacts


def _has_active_experiment_training(root: Path, full_cycle_pid: Any) -> bool:
    if _active_launcher_experiment_runs(root, limit=1):
        return True
    current_repo_path = _selected_repo_path(root)
    for row in _process_tree_rows(full_cycle_pid):
        if _looks_like_experiment_training_cmd(row.get("cmd")):
            return True
    if current_repo_path.exists():
        for row in _all_process_rows():
            if not _looks_like_experiment_training_cmd(row.get("cmd")):
                continue
            if _path_is_within(row.get("cwd"), current_repo_path):
                return True
    return False


def _phase_from_stage(stage: Any) -> str:
    text = str(stage or "").strip().lower().replace("_", "-")
    if not text:
        return "experiment"
    gate_precheck_markers = [
        "paper-evidence-audit-precheck",
        "submission-readiness-precheck",
        "trajectory-evidence-refresh",
        "blocker-action-plan-precheck",
    ]
    if any(marker in text for marker in gate_precheck_markers):
        # These are experiment/evidence gates before paper generation, not a paper-writing stage.
        return "experiment"
    if any(marker in text for marker in ["paper-evidence-audit", "paper-normality-audit", "submission-readiness"]):
        return "experiment"
    if any(marker in text for marker in ["paper-pipeline", "paper-preview", "paper-figure", "conference-preview", "latex"]):
        return "paper"
    environment_literature_markers = [
        "sync-outputs", "literature-sync", "literature-tool-packet", "build-literature-tool-packet",
        "fresh-research-base-selection", "research-base-selection", "base-selection", "fresh-base", "base-candidate",
        "literature-base-candidate", "literature-base-audit", "method-stack-sync",
    ]
    if any(marker in text for marker in environment_literature_markers):
        return "environment"
    if any(marker in text for marker in ["initialization", "environment", "loader", "data-acquisition", "smoke", "reference"]):
        return "environment"
    if any(marker in text for marker in ["autonomous-research", "experiment", "trajectory", "scientific-progress", "iteration-audit", "training", "blocker-repair", "blocker-action-plan", "guidance-checkin"]):
        return "experiment"
    if "current-find-selection" in text or "current-find-claude-select-plan" in text or ("plan" in text and "literature-plan" not in text):
        return "plan"
    if "ideation" in text or "idea" in text:
        return "idea"
    if "read" in text:
        return "read"
    fresh_find_markers = ["literature-survey", "run-finding", "run-driver", "run-literature-tool", "semantic-scholar"]
    if any(marker in text for marker in fresh_find_markers) or text in {"find", "literature", "finding", "literature-gate", "literature-recommendation", "literature-plan"}:
        return "literature"
    return "experiment"


def _active_stage_is_fresh_find(stage: Any) -> bool:
    text = str(stage or "").strip().lower().replace("_", "-")
    if not text:
        return False
    blocked_markers = ["sync-outputs", "literature-tool-packet", "build-literature-tool-packet", "ensure-current-find", "current-find-readiness-gate", "current-find-selection", "current-find-claude-select-plan", "blocker-action-plan", "build-blocker-action-plan"]
    if any(marker in text for marker in blocked_markers):
        return False
    return any(marker in text for marker in ["literature-survey", "run-finding", "run-finding.py"])


def _humanize_job_status(status: Any) -> str:
    text = str(status or "").strip()
    lowered = text.lower()
    mapping = {
        "queued": "排队中",
        "running": "运行中",
        "cancelling": "停止中",
        "cancelled": "已取消",
        "blocked": "阻塞",
        "error": "错误",
        "failed": "失败",
        "done": "完成",
        "preview_available": "预览可用",
        "needs_writing": "待撰写",
        "preview_pdf_blocked": "预览受门控",
        "completed": "完成",
        "success": "完成",
        "stopped": "已停止",
    }
    return mapping.get(lowered, text.replace("_", " "))


def _humanize_stage(stage: Any) -> str:
    text = str(stage or "").strip()
    lowered = text.lower().replace("_", "-")
    mappings = [
        ("literature-survey", "Find 文献调研"),
        ("sync-outputs", "同步 Find 产物"),
        ("literature-tool-packet", "构建文献证据包"),
        ("ensure-current-find", "Framework 校验当前 Find 下游就绪状态"),
        ("current-find-selection", "主控 Claude 选择唯一执行计划"),
        ("current-find-claude-select-plan", "主控 Claude 选择唯一执行计划"),
        ("current-find-readiness-gate", "当前 Find Read 就绪门控"),
        ("experiment-postprocess", "实验后处理：整理产物并解析指标"),
        ("paper-evidence-audit", "实验后审计：刷新论文证据门控"),
        ("submission-readiness", "实验后审计：刷新投稿准备度门控"),
        ("selected-base-viability", "实验后审计：检查当前基底可行性"),
        ("reference-reproduction-gate", "实验后审计：刷新参考复现门控"),
        ("blocker-action-plan", "实验后审计：刷新下一步 blocker plan"),
        ("blocker-repair", "实验迭代"),
        ("reference", "环境/参考复现"),
        ("environment", "环境/数据/loader 检查"),
        ("experiment", "实验迭代"),
        ("paper", "论文产物检查"),
    ]
    for marker, label in mappings:
        if marker in lowered:
            return label
    return text or "TASTE 全流程"


def _is_full_cycle_heartbeat_line(line: Any) -> bool:
    text = str(line or "").strip().lower()
    return bool(text.startswith("full-cycle:") and " still running" in text and "lines=" in text)


def _is_frontend_heartbeat_line(line: Any) -> bool:
    text = str(line or "").strip().lower()
    return bool(text.startswith("[frontend] still running") and "elapsed_sec=" in text)


def _is_transient_taste_service_line(line: Any) -> bool:
    text = str(line or "").strip().lower()
    if not text:
        return False
    transient_markers = [
        "transient service error",
        "read operation timed out",
        "too many requests",
        "http 429",
        "queued for bounded single-item retry",
        "single-item retry disabled",
        "fallback-only marking",
        "unresolved-item audit marking",
        "marking unresolved items for audit",
        "latest released venue for freshness bonus",
        "abstract enrichment filled",
        "final scoring abstract enrichment",
        "abstract contract excluded",
        "title-filtered candidates before llm",
        "wrapper emitted structured evidence json",
        "wrapper structured evidence output suppressed",
        "full evidence is stored under",
    ]
    return any(marker in text for marker in transient_markers)


def _looks_like_llm_quota_blocker(value: Any) -> bool:
    if not value:
        return False
    text = json.dumps(value, ensure_ascii=False).lower() if isinstance(value, (dict, list)) else str(value).lower()
    markers = [
        "llm http 429",
        "quota_exceeded",
        "quota exceeded",
        "token plan limit exhausted",
        "rpm exhausted",
        "too many requests",
        "llm quota",
        "rate-limit",
        "rate limit",
    ]
    return any(marker in text for marker in markers)


def _log_tail_is_human_status(line: Any) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    if re.fullmatch(r"[{}\[\],]+", text):
        return False
    if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*("?/[^"\n]+"?|[{}\[\]],?)', text):
        return False
    if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*"?/[^"\n]+"?,?', text):
        return False
    if _is_full_cycle_heartbeat_line(text) or _is_frontend_heartbeat_line(text):
        return False
    # TASTE child processes often print artifact paths. Those belong in artifacts,
    # not in the taskbar's human-readable latest status line.
    if text.startswith("/") and not any(ch.isspace() for ch in text):
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./-]*\.(md|json|csv|txt|log|tex|pdf)", text):
        return False
    return True


def _dedupe_recent_lines(lines: list[str], *, limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in reversed(lines):
        line = str(raw or "").strip()
        if not line or line in seen or _is_full_cycle_heartbeat_line(line) or _is_frontend_heartbeat_line(line):
            continue
        if _is_machine_index_log_line(line) or _is_low_signal_claude_tool_line(line):
            continue
        seen.add(line)
        result.append(line)
        if len(result) >= limit:
            break
    return list(reversed(result))


def _current_stage_stdout_lines(full_cycle: dict[str, Any]) -> list[str]:
    running = full_cycle.get("current_running_stage") if isinstance(full_cycle.get("current_running_stage"), dict) else {}
    stdout_tail = running.get("stdout_tail") if isinstance(running, dict) else ""
    if not isinstance(stdout_tail, str):
        return []
    return [line.strip() for line in stdout_tail.splitlines() if line.strip()]


def _find_activity_lines(full_cycle: dict[str, Any], *, limit: int = 6) -> list[str]:
    lines = _current_stage_stdout_lines(full_cycle)
    useful = [line for line in lines if _log_tail_is_human_status(line)]
    return _dedupe_recent_lines(useful, limit=limit)


def _current_stage_status_line(raw_stage: Any) -> str:
    lowered = str(raw_stage or "").strip().lower().replace("_", "-")
    if not lowered:
        return ""
    if "experiment-postprocess" in lowered:
        return "TASTE：正在整理实验产物并解析指标，随后刷新科研门控。"
    if "reference-reproduction-gate" in lowered:
        return "TASTE：正在刷新参考复现门控，确认当前基底是否仍可作为主线。"
    if "paper-evidence-audit" in lowered:
        return "TASTE：正在刷新论文证据门控；没有审计通过的候选前保持阻塞，不提升结论。"
    if "submission-readiness" in lowered:
        return "TASTE：正在刷新投稿准备度；证据不足时保持只写草稿。"
    if "selected-base-viability" in lowered:
        return "TASTE：正在检查当前主线是否仍可行，历史路线不自动切回主线。"
    if "blocker-action-plan" in lowered:
        return "TASTE：正在刷新 blocker action plan，决定下一轮自主实验修复任务。"
    if "autonomous-research" in lowered:
        return "TASTE：正在运行自主实验子循环；若需要新训练，必须通过 launcher 接管 PID、日志和产物。"
    if "full-cycle-ideation" in lowered or "ideation" in lowered:
        return "Experimenting 主控 Claude 正在设计下一轮当前主线候选实验；未审计通过前不写论文结论。"
    if "trajectory" in lowered:
        return "TASTE：正在刷新科研轨迹、失败假设和下一步优化队列。"
    return ""


def _humanize_find_log_line(line: Any) -> str:
    text = str(line or "").strip()
    if not text:
        return ""
    if text.startswith("find_activity="):
        text = text[len("find_activity="):].strip()
    if text.startswith("[TASTE]"):
        text = text[len("[TASTE]"):].strip()
    source = "Find"
    message = text
    match = re.match(r"^([^:]{1,80}):\s*(.+)$", text)
    if match:
        source = match.group(1).strip() or source
        message = match.group(2).strip() or message
    lower = message.lower()
    step = "运行中"
    detail = ""
    if "fetched" in lower and "corpus" in lower:
        step = "抓取题录"
    elif "category" in lower or "title screening" in lower:
        step = "主题/标题筛选"
    elif "title prefilter" in lower:
        step = "标题预筛"
    elif "fetching details" in lower:
        step = "详情抓取"
        detail_match = re.search(r"fetching details for\s+(\d+)", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（{detail_match.group(1)} 篇）"
    elif "abstract enrichment" in lower:
        step = "摘要补全"
        detail_match = re.search(r"filled\s+(\d+/\d+)", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（{detail_match.group(1)}）"
    elif "final llm scoring pool" in lower:
        step = "汇总最终 LLM 评分池"
        detail_match = re.search(r"pool\s+(\d+/\d+)\s+candidates", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（{detail_match.group(1)} 候选）"
    elif "starting llm final scoring" in lower:
        step = "开始最终 LLM 评分"
        detail_match = re.search(r"for\s+(\d+/\d+)\s+items\s+in\s+(\d+)\s+batches\s+with\s+(\d+)\s+workers", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（{detail_match.group(1)} 项，{detail_match.group(2)} 批，{detail_match.group(3)} 并发）"
    elif "scored batch" in lower or "scoring batch" in lower:
        step = "LLM 评分"
        detail_match = re.search(r"batch\s+(\d+/\d+)", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（批次 {detail_match.group(1)}）"
    elif "scored batch" in lower or "scoring batch" in lower:
        step = "LLM 评分"
        detail_match = re.search(r"batch\s+(\d+/\d+)", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（批次 {detail_match.group(1)}）"
    elif "llm" in lower and "scoring" in lower:
        step = "LLM 评分"
        detail_match = re.search(r"batch\s+(\d+/\d+)", message, flags=re.IGNORECASE)
        if detail_match:
            detail = f"（批次 {detail_match.group(1)}）"
    elif "latest released venue" in lower:
        step = "新鲜度加权"
    return f"Find {source}：{step}{detail}"


def _find_run_status_lines(run_id: str, *, limit: int = 6) -> list[str]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return []
    try:
        directory = run_dir(run_id)
    except (FileNotFoundError, OSError, ValueError):
        return []
    lines: list[str] = []
    progress_payload = read_json(directory / "find_progress.json", {})
    source_rows: list[Any] = []
    if isinstance(progress_payload, dict):
        live = progress_payload.get("live_progress") if isinstance(progress_payload.get("live_progress"), dict) else {}
        if live:
            message = str(live.get("message") or "Find running").strip()
            current = live.get("current", "?")
            total = live.get("total", "?")
            percent = live.get("percent", "?")
            if not _is_transient_taste_service_line(message):
                lines.append(f"find_live_progress={message}；进度 {current}/{total}；{percent}%")
        counts = progress_payload.get("counts") if isinstance(progress_payload.get("counts"), dict) else {}
        if counts:
            llm_progress_text = ""
            live_phase = str(live.get("phase") or "") if live else ""
            if live_phase.startswith("abstract_scoring"):
                llm_progress_text = f"LLM 评分批次 {live.get('current', '?')}/{live.get('total', '?')}"
            else:
                llm_progress_text = f"LLM 已评分 {counts.get('evaluated_candidates') or 0}"
            lines.append(
                "find_run_counts="
                f"标题库 {counts.get('raw_title_index') or 0}；"
                f"标题预筛候选 {counts.get('title_candidates') or 0}；"
                f"详情已抓取 {counts.get('detail_fetched') or 0}；"
                f"{llm_progress_text}"
            )
        source_rows = progress_payload.get("source_status") if isinstance(progress_payload.get("source_status"), list) else []
    if not source_rows:
        status_path = directory / "source_status.md"
        try:
            text = status_path.read_text(encoding="utf-8", errors="replace") if status_path.exists() else ""
        except Exception:
            text = ""
        parsed_rows: list[dict[str, Any]] = []
        for match in re.finditer(r"^##\s+(.+?)\s*$\n\s*-\s*(.+)$", text, flags=re.MULTILINE):
            label = match.group(1).strip()
            detail_text = match.group(2).strip()

            def _extract(pattern: str) -> str:
                found = re.search(pattern, detail_text)
                return found.group(1) if found else ""

            parsed_rows.append({
                "source": label,
                "ok": detail_text.lower().startswith("ok"),
                "raw_title_index_count": _extract(r"raw_title_index=(\d+)"),
                "candidate_count": _extract(r"screen_input=(\d+)"),
                "detail_fetched_count": _extract(r"detail_fetched=(\d+)"),
                "adapter": _extract(r"adapter=([^/;]+)"),
            })
        source_rows = parsed_rows
    for row in source_rows[:limit]:
        if not isinstance(row, dict):
            continue
        label = str(row.get("source") or row.get("venue") or row.get("name") or "source").strip()
        status = "正常" if bool(row.get("ok", True)) else "异常"
        raw_count = row.get("raw_title_index_count") or row.get("corpus_count") or row.get("sample_count") or 0
        screen_count = row.get("candidate_count") or row.get("count") or 0
        detail_count = row.get("detail_fetched_count") or row.get("detail_fetched") or 0
        adapter = str(row.get("adapter") or "").strip()
        adapter_text = f"；来源适配器 {adapter}" if adapter else ""
        lines.append(f"find_source_status={label}：状态 {status}；标题库 {raw_count}；进入筛选 {screen_count}；详情已抓取 {detail_count}{adapter_text}")
    return lines[: max(0, limit + 2)]


def _find_live_progress_message(find_progress: dict[str, Any]) -> str:
    if not isinstance(find_progress, dict):
        return ""
    live = find_progress.get("live_progress") if isinstance(find_progress.get("live_progress"), dict) else {}
    if not live:
        return ""
    message = str(live.get("message") or "").strip()
    if not message:
        phase = str(live.get("phase") or "find").replace("_", " ")
        message = f"Find {phase}"
    message = str(_strip_public_taste_marker(message))
    if _is_transient_taste_service_line(message):
        return ""
    current = live.get("current", "?")
    total = live.get("total", "?")
    percent = live.get("percent", "?")
    return f"{message}; progress {current}/{total}; {percent}%"


_FIND_OVERALL_PROGRESS_CACHE: dict[str, dict[str, int]] = {}


def _find_progress_projection(find_progress: dict[str, Any]) -> dict[str, Any]:
    """Map Find's nested batch progress to stable end-to-end progress."""
    payload = find_progress if isinstance(find_progress, dict) else {}
    live = payload.get("live_progress") if isinstance(payload.get("live_progress"), dict) else {}
    raw_phase = str(live.get("phase") or payload.get("phase") or "initializing").strip().lower()
    raw_current = max(0, _as_int(live.get("current"), 0))
    raw_total = max(0, _as_int(live.get("total"), 0))
    raw_percent = max(0, min(100, _as_int(live.get("percent"), round((raw_current / raw_total) * 100) if raw_total else 0)))
    message = str(live.get("message") or raw_phase.replace("_", " ") or "Find initializing").strip()
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}

    stage_index = 1
    stage_key = "initializing"
    stage_label = "初始化研究画像"
    stage_percent = raw_percent
    base_percent, span_percent = 0, 8

    venue_phases = {
        "venue_title_index", "title_prefilter", "llm_title_filter", "detail_fetch",
        "detail_enrichment", "abstract_enrichment", "venue_scan_complete",
    }
    external_phase_names = ["nature", "science", "arxiv", "biorxiv", "huggingface", "github"]
    external_phases = set(external_phase_names) | {"nature_detail_enrichment", "science_detail_enrichment", "source_collection_complete"}
    scoring_phase = bool(
        raw_phase in {"final_detail_fetch", "final_ranking_prepare", "abstract_contract", "abstract_scoring", "abstract_scoring_retry"}
        or raw_phase.endswith("_llm_scoring_complete")
    )
    finalizing_phase = raw_phase in {
        "preliminary_artifacts_written", "abstract_translation", "abstract_translation_retry",
        "abstract_translation_final",
    }

    if raw_phase == "complete":
        stage_index = 6
        stage_key = "complete"
        stage_label = "Find 完成"
        stage_percent = 100
        base_percent, span_percent = 100, 0
    elif finalizing_phase:
        stage_index = 5
        stage_key = "publishing"
        stage_label = "推荐排序与产物生成"
        base_percent, span_percent = 88, 11
        if raw_phase == "preliminary_artifacts_written":
            stage_percent = 5
        elif raw_phase == "abstract_translation":
            stage_percent = 10 + round(raw_percent * 0.8)
        else:
            stage_percent = 90 + round(raw_percent * 0.09)
    elif scoring_phase:
        stage_index = 4
        stage_key = "llm_evaluation"
        stage_label = "摘要校验与 LLM 综合评估"
        base_percent, span_percent = 58, 30
        selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
        scoring_sources = (["articles"] if selection.get("venue_ids") else []) + [
            name for name in external_phase_names if bool(selection.get(f"include_{name}"))
        ]
        source_match = re.match(r"^([^:]{1,40}):", message)
        source_name = str(source_match.group(1) if source_match else "").strip().lower().replace(" ", "_")
        if raw_phase.endswith("_llm_scoring_complete"):
            source_name = raw_phase.removesuffix("_llm_scoring_complete")
        source_index = scoring_sources.index(source_name) if source_name in scoring_sources else 0
        if raw_phase == "final_detail_fetch":
            source_fraction = 0.02 + (raw_percent / 100) * 0.08
        elif raw_phase == "final_ranking_prepare":
            source_fraction = 0.05 + (raw_percent / 100) * 0.10
        elif raw_phase == "abstract_contract":
            source_fraction = 0.15 + (raw_percent / 100) * 0.05
        elif raw_phase == "abstract_scoring":
            source_fraction = 0.20 + (raw_percent / 100) * 0.70
        elif raw_phase == "abstract_scoring_retry":
            source_fraction = 0.90 + (raw_percent / 100) * 0.08
        else:
            source_fraction = 1.0
        stage_percent = round(((source_index + source_fraction) / max(1, len(scoring_sources))) * 100)
    elif raw_phase in external_phases:
        stage_index = 3
        stage_key = "extended_sources"
        stage_label = "扩展渠道检索"
        base_percent, span_percent = 43, 15
        if raw_phase == "source_collection_complete":
            stage_percent = 100
        else:
            selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
            enabled = [
                name for name in external_phase_names
                if bool(selection.get(f"include_{name}"))
            ]
            root_phase = raw_phase.split("_detail_enrichment", 1)[0]
            if root_phase in enabled and enabled:
                stage_percent = round(((enabled.index(root_phase) + raw_percent / 100) / len(enabled)) * 100)
    elif raw_phase in venue_phases:
        stage_index = 2
        stage_key = "venue_pipeline"
        stage_label = "会议/期刊题录检索与标题筛选"
        base_percent, span_percent = 8, 35
        if raw_phase == "venue_scan_complete":
            stage_percent = 100
        else:
            selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
            requested_venues = selection.get("venue_ids") if isinstance(selection.get("venue_ids"), list) else []
            if requested_venues:
                venue_rows = payload.get("venue_health_report") if isinstance(payload.get("venue_health_report"), list) else []
                venue_count = len(requested_venues)
                observed_count = min(venue_count, len([row for row in venue_rows if isinstance(row, dict)]))
                message_lower = message.lower()
                if raw_phase == "venue_title_index":
                    title_index_ready = "ready" in message_lower or "unavailable" in message_lower
                    current_venue = max(1, observed_count if title_index_ready else min(venue_count, observed_count + 1))
                    venue_fraction = 0.10 if title_index_ready else 0.03
                else:
                    current_venue = max(1, observed_count)
                    if raw_phase == "title_prefilter":
                        venue_fraction = 0.10 + (raw_percent / 100) * 0.10
                    elif raw_phase == "llm_title_filter":
                        venue_fraction = 0.20 + (raw_percent / 100) * 0.35
                    elif raw_phase in {"abstract_enrichment", "detail_enrichment"}:
                        venue_fraction = 0.55 + (raw_percent / 100) * 0.20
                    elif raw_phase == "detail_fetch":
                        venue_fraction = 0.75 + (raw_percent / 100) * 0.25
                    else:
                        venue_fraction = 0.50
                stage_percent = min(99, round(((current_venue - 1 + venue_fraction) / venue_count) * 100))

    stage_percent = max(0, min(100, stage_percent))
    overall_percent = 100 if stage_index == 6 else base_percent + round((stage_percent / 100) * span_percent)
    run_id = str(payload.get("run_id") or "").strip()
    if run_id:
        cached = _FIND_OVERALL_PROGRESS_CACHE.get(run_id, {})
        if stage_index == _as_int(cached.get("stage_index"), 0):
            stage_percent = max(_as_int(cached.get("stage_percent"), 0), stage_percent)
            overall_percent = 100 if stage_index == 6 else base_percent + round((stage_percent / 100) * span_percent)
        overall_percent = max(_as_int(cached.get("overall_percent"), 0), overall_percent)
        _FIND_OVERALL_PROGRESS_CACHE[run_id] = {
            "stage_index": stage_index,
            "stage_percent": stage_percent,
            "overall_percent": overall_percent,
        }
    return {
        "raw_phase": raw_phase,
        "raw_current": raw_current,
        "raw_total": raw_total,
        "stage_index": stage_index,
        "stage_total": 6,
        "stage_key": stage_key,
        "stage_label": stage_label,
        "overall_percent": max(0, min(100, overall_percent)),
        "stage_percent": stage_percent,
        "message": message,
        "counts": dict(counts),
    }


def _compact_log_lines(lines: Any, *, limit: int = 40) -> list[str]:
    raw_lines = [
        str(line)
        for line in (lines or [])
        if str(line or "").strip() and not _is_transient_taste_service_line(line)
    ]
    return _dedupe_recent_lines(raw_lines, limit=limit)


def _redact_public_log_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|authorization|bearer|token)(\s*[:=]\s*)[^\s,'\"]+", r"\1\2***", text)
    text = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})", "sk-***", text)
    return text


def _summarize_claude_taskbline(line: Any) -> str:
    """Return a direct cleaned live-agent/log fragment.

    This function intentionally does not infer a new Claude narrative from
    keywords. If the text did not come from the live agent/log artifact, the
    taskbar should show only deterministic job/gate status elsewhere.
    """
    text = _clean_claude_taskbline(line)
    if not text:
        return ""
    lowered = text.lower()
    if "waiting for claude code output" in lowered:
        return "模块主控 Claude 会话运行中；等待真实输出写入日志。"
    if text.endswith((" for", " and", " or", " to", " with", " while", "—")):
        return ""
    return text[:900]


def _is_generic_claude_taskbsummary(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    generic = {
        "模块主控 Claude 会话运行中；等待真实输出写入日志。",
    }
    return text in generic or "当前动作=；" in text or text.endswith("当前动作=")


def _is_stale_route_switch_taskbline(root: Path, line: Any) -> bool:
    """Hide stale route-switch proposals from the taskbar.

    Raw Claude replies remain in the Claude conversation artifact. The taskbar is
    the current run/job surface, so an unauthorized base-switch proposal
    must not appear as current research state while the selected base remains unchanged.
    """
    text = str(line or "")
    lowered = text.lower()
    route_markers = [
        "authorize base switch",
        "authorize base switch",
        "base switch",
        "paper claims are supported",
        "0.0458",
        "35.5%",
    ]
    if not any(marker in lowered for marker in route_markers):
        return False
    base_switch = _read_project_json(root / "state" / "base_switch_execution.json", {})
    base_gate = _read_project_json(root / "state" / "base_switch_gate.json", {})
    authorized = bool(
        isinstance(base_switch, dict)
        and base_switch.get("switch_authorized") is True
        or isinstance(base_gate, dict)
        and base_gate.get("switch_authorized") is True
    )
    return not authorized


def _latest_claude_activity_from_status_lines(lines: list[str]) -> str:
    fallback = ""
    for raw in reversed(lines):
        line = str(raw or "").strip()
        if not line:
            continue
        if line.startswith("claude_status="):
            match = re.search(r"当前动作=([^；]+)", line)
            if not match:
                continue
            candidate = match.group(1).strip()
        else:
            candidate = line.split("=", 1)[1].strip() if "=" in line else line
        summary = _summarize_claude_taskbline(candidate) or candidate
        if _is_generic_claude_taskbsummary(summary):
            fallback = fallback or summary
            continue
        return summary
    return fallback


def _clean_claude_taskbline(line: Any) -> str:
    text = _redact_public_log_text(line).strip()
    if not text:
        return ""
    if text.startswith("Claude:"):
        text = text[len("Claude:"):].strip()
    lowered = text.lower()
    if _is_low_signal_claude_tool_line(text):
        return ""
    if any(marker in lowered for marker in [
        "调用工具:",
        "bash command=",
        "read file=",
        "write file=",
        "edit file=",
        "multi-edit",
        "glob pattern=",
        "grep pattern=",
        "ls -la ",
        "tail -",
    ]):
        return ""
    if re.fullmatch(r"[-=*_`\s]+", text):
        return ""
    if text.startswith("/") and not any(ch.isspace() for ch in text):
        return ""
    if re.fullmatch(r'"?[A-Za-z0-9_./ -]+"?\s*:\s*"?/[^"\n]+"?,?', text):
        return ""
    root_pattern = re.escape(str(WORKSPACE_ROOT))
    text = re.sub(rf"`({root_pattern}/[^`]+)`", "`项目内证据文件`", text)
    text = re.sub(rf"{root_pattern}/\S+", "项目内证据文件", text)
    return text[:900]


def _is_live_module_controller_row(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status not in {"running", "queued", "cancelling"}:
        return False
    role = str(row.get("role") or "").strip().lower()
    command = " ".join(str(part) for part in row.get("command", [])) if isinstance(row.get("command"), list) else str(row.get("command") or "")
    command_l = command.lower()
    agent_id = str(row.get("id") or "").strip().lower()
    is_controller = role == "module-controller" or agent_id in {"environment_controller", "experimenting_controller", "writing_controller"}
    if not is_controller:
        return False
    pid = row.get("pid") or row.get("claude_pid")
    if status in {"running", "cancelling"} and pid not in (None, "") and not _pid_alive_local(pid):
        return False
    return True


def _module_controller_matches_scope(row: dict[str, Any], stage_scope: str = "") -> bool:
    scope = str(stage_scope or "").strip().lower()
    if not scope:
        return True
    stage = str(row.get("stage") or row.get("last_stage") or "").strip().lower()
    agent_id = str(row.get("id") or "").strip().lower()
    if scope == "environment":
        return agent_id == "environment_controller" or stage.startswith("environment")
    if scope == "experiment":
        return agent_id == "experimenting_controller" or stage.startswith("experiment")
    if scope == "paper":
        return agent_id == "writing_controller" or stage.startswith("writing") or stage.startswith("paper")
    return True


def _latest_module_controller_status_lines(root: Path, *, limit: int = 10, stage_scope: str = "") -> list[str]:
    lines: list[str] = []
    payload = _read_project_json(root / "state" / "agents.json", {})
    agents = payload.get("agents") if isinstance(payload, dict) else []
    if isinstance(agents, list):
        live_candidates = []
        for row in agents:
            if not isinstance(row, dict):
                continue
            if not _is_live_module_controller_row(row) or not _module_controller_matches_scope(row, stage_scope):
                continue
            sort_key = str(row.get("updated_at") or row.get("created_at") or "")
            live_candidates.append({**row, "_sort_key": sort_key})
        if live_candidates:
            latest = sorted(live_candidates, key=lambda item: str(item.get("_sort_key") or ""), reverse=True)[0]
            status = str(latest.get("status") or "").strip()
            stage = str(latest.get("stage") or latest.get("last_stage") or "").strip()
            step = str(latest.get("current_step") or "").strip()
            updated = str(latest.get("updated_at") or "").strip()
            bits = []
            if stage:
                bits.append(f"阶段={_humanize_stage(stage) or stage}")
            if status:
                bits.append(f"状态={_humanize_job_status(status)}")
            if step:
                step_summary = _summarize_claude_taskbline(step) or _clean_claude_taskbline(step)
                if step_summary and not _is_generic_claude_taskbsummary(step_summary):
                    bits.append(f"当前动作={step_summary}")
            if updated:
                bits.append(f"更新={updated}")
            if bits:
                lines.append("claude_status=" + "；".join(bits))
            live_tail_items = list(reversed(latest.get("log_tail", []))) if isinstance(latest.get("log_tail"), list) else []
            for item in live_tail_items:
                cleaned = _summarize_claude_taskbline(item)
                if cleaned and not _is_low_signal_claude_tool_line(cleaned):
                    candidate_line = "claude_current=" + cleaned
                    if candidate_line not in lines:
                        lines.append(candidate_line)
                if len(lines) >= limit:
                    break
            if lines:
                return lines[:limit]

    # Do not use the last completed Claude result as current taskbar output.
    # The experiment page has a separate recent raw reply panel for that purpose.

    # No live Claude worker: do not replay stale Claude/session rows as current taskbar state.
    return []


def _raw_log_tail_lines(lines: list[str], *, limit: int = 24) -> list[str]:
    """Return recent raw-ish research log lines for the taskbar black log panel.

    The status/latest lines are intentionally filtered and translated elsewhere.
    This tail keeps enough command/stdout/context for a human to audit what the
    running controller actually did, while dropping pure heartbeat spam.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in reversed(lines):
        line = str(raw or "").rstrip()
        compact = " ".join(line.split())
        if not compact or compact in seen:
            continue
        if _is_full_cycle_heartbeat_line(compact) or _is_frontend_heartbeat_line(compact):
            continue
        if _is_machine_index_log_line(compact) or _is_stale_or_internal_full_cycle_tail(compact):
            continue
        seen.add(compact)
        out.append(line[:1200])
        if len(out) >= limit:
            break
    return list(reversed(out))


def _experiment_log_chunks(lines: list[str]) -> list[tuple[str, list[str]]]:
    chunks: list[tuple[str, list[str]]] = []
    current_source = ""
    current: list[str] = []
    for raw in lines:
        text = str(raw or "").strip()
        if text.startswith("experiment_output_source="):
            if current:
                chunks.append((current_source, current))
            current_source = text.split("=", 1)[1].strip()
            current = []
            continue
        if text.startswith("experiment_output="):
            current.append(text.split("=", 1)[1].strip())
    if current:
        chunks.append((current_source, current))
    return chunks


def _experiment_source_priority(source: str, chunk: list[str]) -> int:
    lowered = source.lower()
    joined = "\n".join(chunk).lower()
    if lowered.startswith("/tmp/") or "text_embed" in lowered:
        return -1
    if any(marker in lowered for marker in ("candidate", "treatment", "variant")) or any(marker in joined for marker in ("role=candidate", "role: candidate")):
        return 0
    if any(marker in lowered for marker in ("semantic", "text", "embedding")) or any(marker in joined for marker in ("semantic", "text embedding", "embedding")):
        return 1
    if any(marker in lowered for marker in ("reference", "control", "baseline")) or any(marker in joined for marker in ("role=reference", "role=control", "role: reference", "role: control")):
        return 5
    return 3


def _latest_experiment_status_line(lines: list[str]) -> str:
    chunks = _experiment_log_chunks(lines)
    outputs = [line for _source, chunk in chunks for line in chunk]
    parallel_line = _parallel_experiment_progress_line(lines)
    if parallel_line:
        return "并行实验：" + parallel_line.split("=", 1)[1]
    for _source, chunk in sorted(chunks, key=lambda item: _experiment_source_priority(item[0], item[1])):
        for index in range(len(chunk) - 1, -1, -1):
            text = chunk[index]
            lowered = text.lower()
            if re.search(r"\b(?:nameerror|typeerror|runtimeerror|exception|error):", text, flags=re.IGNORECASE):
                return "实验训练异常退出：" + text[:220]
            if "traceback (most recent call last)" in lowered:
                for follow in chunk[index + 1:index + 8]:
                    if re.search(r"\b(?:nameerror|typeerror|runtimeerror|exception|error):", follow, flags=re.IGNORECASE):
                        return "实验训练异常退出：" + follow[:220]
                return "实验训练异常退出：Traceback"
        for text in reversed(chunk):
            if "training complete" in text.lower():
                return text
        for index in range(len(chunk) - 1, -1, -1):
            text = chunk[index].strip()
            values = text.split()
            if len(values) >= 6 and all(re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", item, flags=re.IGNORECASE) for item in values[:6]):
                previous = "\n".join(chunk[max(0, index - 4):index]).upper()
                if "--- TEST" in previous and "NDCG@10" in previous:
                    return f"TEST HR@10={values[0]}；NDCG@10={values[1]}；HR@20={values[2]}；NDCG@20={values[3]}"
        for text in reversed(chunk):
            if re.match(r"^Epoch\s+\d+\b", text):
                return text
    for raw in reversed(lines):
        text = str(raw or "").strip()
        if text.startswith("experiment_output_status="):
            value = text.split("=", 1)[1].strip()
            if value and not _is_low_signal_claude_tool_line(value):
                return value
    for raw in reversed(lines):
        text = str(raw or "").strip()
        if text.startswith(("claude_current=", "stage_output=")):
            value = text.split("=", 1)[1].strip()
            if value and not _is_low_signal_claude_tool_line(value) and not _is_generic_claude_taskbsummary(value):
                return value
    for text in reversed(outputs):
        lowered = text.lower()
        if not text or re.fullmatch(r"[-\s]+", text):
            continue
        if _is_low_signal_claude_tool_line(text):
            continue
        if re.fullmatch(r"(?:BG\s+)?PID=\d+", text, flags=re.IGNORECASE):
            continue
        if "phrase" in lowered or lowered.startswith("evalution cost"):
            continue
        return text
    for prefix in ("stage_output=", "full_cycle_output="):
        for raw in reversed(lines):
            text = str(raw or "").strip()
            if text.startswith(prefix):
                value = text.split("=", 1)[1].strip()
                if value and _log_tail_is_human_status(value) and not _is_low_signal_claude_tool_line(value) and not value.startswith("/home/"):
                    return value
    return ""


def _filter_stale_claude_wait_lines_for_finished_experiment(lines: list[str]) -> list[str]:
    """Keep taskbar human-readable when Claude is still draining old wait commands."""
    generic_markers = ["正在执行当前科研动作；原始流式输出保留在项目日志中"]
    lines = [line for line in lines if not (str(line or "").startswith(("claude_current=", "stage_output=")) and any(marker in str(line or "") for marker in generic_markers))]

    authoritative_experiment_evidence = any(
        str(line or "").startswith((
            "experiment_cmd=",
            "experiment_run=",
            "experiment_log=",
            "experiment_output_status=",
            "experiment_output=",
        ))
        or "EXPERIMENT_FINISHED" in str(line or "")
        for line in lines
    )
    if not authoritative_experiment_evidence:
        return lines
    active_training_evidence = any(
        str(line or "").startswith(("process=实验训练进程", "experiment_run="))
        for line in lines
    )
    stale_markers = [
        "候选实验训练已启动，正在等待新的 epoch/指标日志输出",
        "等待新的 epoch/指标日志输出",
        "正在运行，等待下一段输出",
        "正在执行当前科研动作；原始流式输出保留在项目日志中",
    ]
    if active_training_evidence:
        stale_markers.extend([
            "正在检查候选实验是否已有本地审计记录和实验登记",
            "正在准备刷新论文证据和投稿准备度门控",
            "正在检查科学进展门控和 blocker 刷新脚本",
        ])
    filtered: list[str] = []
    for raw in lines:
        text = str(raw or "")
        if text.startswith(("claude_current=", "stage_output=")) and any(marker in text for marker in stale_markers):
            continue
        filtered.append(raw)
    return filtered


def _command_epoch_totals(lines: list[str], *, command: str = "") -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for raw in lines + (["cmd=" + command] if command else []):
        text = str(raw or "")
        match = re.search(r"(?:^|\s)--epoch(?:=|\s+)(\d+)\b", text)
        if not match:
            continue
        total = int(match.group(1))
        descri_match = re.search(r"(?:^|\s)--descri(?:=|\s+)([^\s]+)", text)
        data_match = re.search(r"(?:^|\s)--data(?:=|\s+)([^\s]+)", text)
        tokens = []
        if descri_match:
            descri = re.sub(r"[^A-Za-z0-9_.-]", "", descri_match.group(1)).lower()
            tokens.append(descri)
            tokens.append(re.sub(r"_\d+epoch$", "", descri))
        if data_match:
            data = re.sub(r"[^A-Za-z0-9_.-]", "", data_match.group(1)).lower()
            tokens.append(data)
            if "finetune_llm.py" in text.lower():
                tokens.append(f"{data}_llm_{total}epoch")
                tokens.append(f"{data}_llm")
        for token in tokens:
            if token:
                rows.append((token, total))
    return rows


def _experiment_source_total(source: str, lines: list[str], *, command: str = "") -> int:
    source_text = str(source or "").lower()
    name = Path(source_text).name.lower()
    for text in (name, source_text):
        match = re.search(r"(?:^|[_-])(\d+)epoch(?:\b|[_.-])", text)
        if match:
            return int(match.group(1))
    for token, total in _command_epoch_totals(lines, command=command):
        if token and token in source_text:
            return total
    totals = [total for _token, total in _command_epoch_totals(lines, command=command)]
    return max(totals) if len(set(totals)) == 1 and totals else 0


def _experiment_epoch_progress(lines: list[str], *, command: str = "") -> tuple[int, int, int]:
    chunks = _experiment_log_chunks(lines)
    progress_rows: list[tuple[int, int, int]] = []
    for source, chunk in sorted(chunks, key=lambda item: _experiment_source_priority(item[0], item[1])):
        latest = 0
        for text in reversed(chunk):
            match = re.match(r"^Epoch\s+(\d+)\b", text)
            if match:
                latest = int(match.group(1)) + 1
                break
        total = _experiment_source_total(source, lines, command=command) if latest else 0
        if latest and total:
            percent = max(0, min(100, int(round((latest / total) * 100))))
            progress_rows.append((percent, latest, total))
    if progress_rows:
        _percent, latest, total = sorted(progress_rows, key=lambda item: (item[0], item[1]))[0]
        return latest, total, _percent
    total = 0
    for _token, value in _command_epoch_totals(lines, command=command):
        total = max(total, value)
    return 0, total, 0


def _experiment_source_label(source: str) -> str:
    source_text = str(source or "")
    path = Path(source_text or "experiment")
    name = path.name.lower()
    parent = path.parent.name.lower() if path.parent else ""
    label_source = parent or name or source_text.lower()
    # Prefer the artifact directory name. Log filenames are often just
    # stdout_stderr.log, while project paths contain unrelated terms such as
    # the project id.
    if "baseline" in label_source or "baseline" in name:
        return "baseline"
    if "fusion" in label_source:
        return "fusion"
    if "seminit" in label_source:
        return "seminit"
    if "cluster" in label_source:
        return "cluster"
    if "description" in label_source or "external_text" in label_source:
        return "text-source"
    if "realtext" in label_source or "real_text" in label_source:
        return "realtext"
    if "llm" in label_source:
        return "llm"
    if name in {"output.log", "stdout_stderr.log"} and parent:
        return parent
    return path.stem or "experiment"


def _parallel_experiment_progress_line(lines: list[str]) -> str:
    chunks = _experiment_log_chunks(lines)
    if len(chunks) < 2:
        return ""
    parts: list[str] = []
    for source, chunk in sorted(chunks, key=lambda item: _experiment_source_priority(item[0], item[1])):
        latest = 0
        for text in reversed(chunk):
            match = re.match(r"^Epoch\s+(\d+)\b", text)
            if match:
                latest = int(match.group(1)) + 1
                break
        if latest:
            denom = _experiment_source_total(source, lines) or "?"
            parts.append(f"{_experiment_source_label(source)} {latest}/{denom}")
    return "parallel_experiments=" + "; ".join(parts) if len(parts) >= 2 else ""


def _compact_experiment_detail_lines(lines: list[str], *, limit: int = 52) -> list[str]:
    meta = [
        str(line) for line in lines
        if str(line).startswith(("experiment_cmd=", "experiment_run=", "process=实验训练进程", "experiment_log=", "experiment_output_status="))
    ]
    parallel_line = _parallel_experiment_progress_line(lines)
    if parallel_line:
        meta.append(parallel_line)
    chunks = sorted(_experiment_log_chunks(lines), key=lambda item: _experiment_source_priority(item[0], item[1]))
    out: list[str] = meta[-15:]
    if not chunks:
        output_lines = [str(line) for line in lines if str(line).startswith("experiment_output=")]
        out.extend(output_lines[-max(0, limit - len(out)):])
        return out[-limit:]
    remaining = max(1, limit - len(out))
    per_chunk = max(4, min(9, remaining // max(1, len(chunks))))
    for source, chunk in chunks:
        if len(out) >= limit:
            break
        if source:
            out.append("experiment_output_source=" + source)
        keep = max(1, per_chunk - (1 if source else 0))
        selected = chunk[-keep:]
        latest_epoch = next((value for value in reversed(chunk) if re.match(r"^Epoch\s+\d+\b", value)), "")
        if latest_epoch and latest_epoch not in selected:
            selected = [latest_epoch] + selected
        for value in selected:
            if len(out) >= limit:
                break
            out.append("experiment_output=" + value)
    return out[-limit:]


def _current_stage(full_cycle: dict[str, Any], log_path: str = "") -> tuple[str, int, str, str]:
    latest = full_cycle.get("latest_step") if isinstance(full_cycle.get("latest_step"), dict) else {}
    running = full_cycle.get("current_running_stage") if isinstance(full_cycle.get("current_running_stage"), dict) else {}
    raw_stage = str(running.get("stage") or latest.get("stage") or "full-cycle")
    phase = _phase_from_stage(raw_stage)
    line_count = running.get("line_count", latest.get("line_count", ""))
    try:
        line_value = int(line_count or 0)
    except Exception:
        line_value = 0
    status_lines: list[str] = []
    stdout_tail = running.get("stdout_tail")
    if isinstance(stdout_tail, str):
        status_lines.extend(stdout_tail.splitlines())
    if log_path:
        try:
            path = Path(log_path)
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                status_lines.extend(lines[-80:])
                if not line_value:
                    line_value = len(lines)
                # The running stage recorded by run_full_research_cycle.py is authoritative.
                # Log tails often mention reused literature artifacts while later experiment
                # stages are active; do not let those artifact names move the job back to Find.
                if not running.get("stage") and phase == "full-cycle":
                    for line in reversed(lines[-80:]):
                        text = line.lower()
                        if any(marker in text for marker in ["paper-pipeline", "paper-preview", "paper-figure", "conference-preview", "latex"]):
                            phase = "paper"
                            raw_stage = "paper"
                            break
                        if any(marker in text for marker in ["run_autonomous_research.py", "autonomous-research", "experiment", "trajectory-supervisor", "reference-reproduction"]):
                            phase = "experiment"
                            raw_stage = "experiment"
                            break
                        if any(marker in text for marker in ["run_frontend", "semantic scholar"]):
                            phase = "literature"
                            raw_stage = "literature"
                            break
        except Exception:
            pass
    preferred_lines = [line for line in status_lines if _log_tail_is_human_status(line)]
    stable_lines = [line for line in preferred_lines if not _is_transient_taste_service_line(line)]
    tail = next((line.strip() for line in reversed(stable_lines)), "")
    if not tail and (raw_stage or running.get("pid") or line_value):
        bits = [f"full-cycle: {raw_stage or 'stage'} still running"]
        if running.get("pid"):
            bits.append(f"pid={running.get('pid')}")
        if line_value:
            bits.append(f"lines={line_value}")
        tail = "; ".join(bits)
    return phase, line_value, tail, raw_stage


def _latest_find_run_id_from_runs(project_root: Path | None = None, *, prefer_run_dir: bool = False) -> str:
    def latest_run_dir_id() -> str:
        try:
            candidates = [
                path
                for runs_root in RUNS_SEARCH_DIRS
                for path in runs_root.glob("find_*")
                if path.is_dir()
            ]
        except Exception:
            return ""
        if not candidates:
            return ""
        return sorted(candidates, key=lambda path: path.name)[-1].name

    if prefer_run_dir:
        latest = latest_run_dir_id()
        if latest:
            return latest
    if project_root is not None:
        current = _project_current_find_run_id(project_root)
        if current:
            return current
    return latest_run_dir_id()


def _web_find_job_id_from_command(command: Any) -> str:
    match = re.search(r"(?:^|\s)--web-job-id(?:=|\s+)([^\s]+)", str(command or ""))
    return match.group(1).strip().strip("'\"") if match else ""


def _active_web_find_run_binding(project: str, web_job_id: str) -> tuple[bool, str]:
    """Return the exact Web Find job binding carried by a Framework worker."""
    project_id = str(project or "").strip()
    target_job_id = str(web_job_id or "").strip()
    if not project_id or not target_job_id:
        return False, ""
    with JOBS_LOCK:
        job = JOBS.get(target_job_id)
    if job is None or _public_taste_stage(job.stage) != "find" or job.cancel_requested:
        return False, ""
    status = str(job.status or "").strip().lower()
    if status not in {"queued", "running", "cancelling", "cancelled", "interrupted", "stale"}:
        return False, ""
    result = job.result if isinstance(job.result, dict) else {}
    if str(result.get("project") or "").strip() != project_id:
        return False, ""
    return True, str(job.run_id or result.get("run_id") or "").strip()


def _active_project_worker_job(project: str, root: Path, worker: dict[str, Any], full_cycle: dict[str, Any], current_plan: dict[str, Any], find_progress: dict[str, Any], *, compact: bool = True, controller_alive: bool = False) -> dict[str, Any]:
    if not isinstance(worker, dict) or not worker:
        return {}
    if str(worker.get("kind") or "") == "full_cycle":
        return {}
    pid = str(worker.get("pid") or "").strip()
    if not pid or not _pid_alive_local(pid):
        return {}
    phase = str(worker.get("phase") or "experiment").strip().lower() or "experiment"
    kind = str(worker.get("kind") or "active_project_worker").strip() or "active_project_worker"
    find_worker = phase == "literature" and kind in {"frontend_recovery", "driver_recovery"}
    cmd = str(worker.get("cmd") or "")
    web_find_job_id = _web_find_job_id_from_command(cmd) if find_worker else ""
    elapsed = str(worker.get("elapsed") or "")
    paper_substage = _paper_substage_from_cmd(cmd, fallback="paper") if phase == "paper" else ""
    current_find_worker = kind.startswith("current_find") or phase == "read"
    web_find_binding, web_find_run_id = _active_web_find_run_binding(project, web_find_job_id) if find_worker else (False, "")
    run_id = web_find_run_id if web_find_binding else (_project_current_find_run_id(root) if current_find_worker else "")
    if not run_id and phase == "environment":
        run_id = _run_id_from_command_line(cmd)
    if not run_id and not find_worker:
        sources = [find_progress, current_plan, full_cycle] if phase == "literature" else [current_plan, find_progress, full_cycle]
        for source in sources:
            if isinstance(source, dict):
                run_id = str(source.get("run_id") or source.get("taste_run_id") or source.get("source_run_id") or source.get("current_find_run_id") or source.get("find_run_id") or "").strip()
                if run_id:
                    break
    live_find_progress = (
        {"run_id": run_id, "live_progress": {"phase": "initializing", "current": 0, "total": 0, "percent": 0, "message": "Find initializing"}, "counts": {}}
        if find_worker
        else (find_progress if isinstance(find_progress, dict) else {})
    )
    if find_worker and run_id:
        try:
            run_progress = read_json(run_dir(run_id) / "find_progress.json", {})
        except (FileNotFoundError, OSError, ValueError):
            run_progress = {}
        if isinstance(run_progress, dict) and run_progress:
            live_find_progress = run_progress
    controller_note = "由当前完整科研循环管理；不是完整科研循环控制器。" if controller_alive else "不是完整科研循环控制器；控制器未存活或需恢复。"
    if find_worker:
        worker_summary = "Find Framework worker 正在运行；由 Web Find 主任务管理。"
    elif current_find_worker:
        worker_summary = f"当前 Find Read Framework worker 正在运行；{controller_note}"
    elif phase == "paper":
        worker_summary = f"论文写作 worker 正在运行：{paper_substage or 'paper'}；{controller_note}"
    else:
        worker_summary = f"项目实验 worker 正在运行；{controller_note}" if phase == "experiment" else f"项目后台 worker 正在运行；{controller_note}"
    logs = [
        worker_summary,
        f"project={project}",
        f"stage={phase}",
        f"pid={pid}",
        "process_alive=true",
        f"worker_kind={kind}",
    ]
    if paper_substage:
        logs.append(f"paper_current_substage={paper_substage}")
    detail_logs: list[str] = []
    detail_artifacts: list[str] = []
    if phase == "experiment":
        detail_logs, detail_artifacts = _active_detail_lines(root, pid, "experiment")
        for line in _compact_experiment_detail_lines(detail_logs, limit=36):
            logs.append(line[:900])
    elif find_worker:
        for line in _find_run_status_lines(run_id, limit=6):
            logs.append(line[:900])
        logs.append(f"cmd={cmd[:900]}")
    else:
        logs.append(f"cmd={cmd[:900]}")
    latest_status = _latest_experiment_status_line(logs) if phase == "experiment" else ""
    if latest_status:
        logs.append("latest=" + latest_status[:700])
    log_path = str((full_cycle if isinstance(full_cycle, dict) else {}).get("full_cycle_job", {}).get("log_path") if isinstance((full_cycle if isinstance(full_cycle, dict) else {}).get("full_cycle_job"), dict) else "")
    if not log_path:
        log_path = _latest_project_log(root, "logs/supervision/full_research_cycle_*.log")
    if log_path:
        logs.append(f"full_cycle_log={log_path}")
    if find_worker and run_id:
        directory = run_dir(run_id)
        artifacts = [
            str(directory / "find_progress.json"),
            str(directory / "source_status.md"),
            str(directory / "find_results.json"),
            str(directory / "find.md"),
        ]
    elif current_find_worker:
        artifacts = [
            str(root / "state" / "current_find_research_plan.json"),
            str(root / "state" / "current_find_claude_reading_validation.json"),
            str(root / "planning" / "finding" / "read_results.json"),
            str(root / "planning" / "finding" / "ideas.json"),
            str(root / "planning" / "finding" / "plans.json"),
        ]
    else:
        artifacts = [
            str(root / "state" / "full_research_cycle.json"),
            str(root / "state" / "experiment_iteration_audit.json"),
            str(root / "state" / "experiment_record_table.json"),
            *detail_artifacts,
        ]
    progress_current = 0
    progress_total = 0
    progress_percent = 0
    find_progress_projection: dict[str, Any] = {}
    if phase == "experiment":
        progress_current, progress_total, progress_percent = _experiment_epoch_progress(logs, command=cmd)
    elif find_worker:
        find_progress_projection = _find_progress_projection(live_find_progress)
        progress_current = _as_int(find_progress_projection.get("overall_percent"), 0)
        progress_total = 100
        progress_percent = progress_current
    if phase == "paper":
        message_bits = [_paper_live_status_message({
            "paper_current_substage": paper_substage or "paper",
            "paper_worker_pid": pid,
            "paper_worker_kind": kind,
        })]
    elif find_worker:
        stage_label = str(find_progress_projection.get("stage_label") or "Find")
        live_message = str(find_progress_projection.get("message") or "Find running")
        message_bits = [f"{stage_label}：{live_message}", f"PID={pid}"]
    else:
        message_bits = [f"{phase} worker running", f"PID={pid}"]
        if elapsed:
            message_bits.append(f"elapsed={elapsed}")
        if latest_status:
            message_bits.append(latest_status[:180])
        elif cmd:
            message_bits.append(cmd[:180])
    progress_phase = paper_substage if phase == "paper" and paper_substage else phase
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z") if phase == "paper" else str((full_cycle if isinstance(full_cycle, dict) else {}).get("started_at") or datetime.now(UTC).isoformat())
    return {
        "job_id": f"experiment-worker_{project}_{pid}" if phase == "experiment" else (f"current-find-worker_{project}_{pid}" if current_find_worker else f"project-worker_{project}_{pid}"),
        "stage": phase,
        "status": "running",
        "created_at": created_at,
        "logs": logs[-80:] if compact else logs,
        "log_count": len(logs),
        "run_id": run_id,
        "result": {
            "project": project,
            "run_id": run_id,
            "pid": pid,
            "phase": phase,
            "raw_stage": paper_substage or kind,
            "current_substage": paper_substage,
            "paper_current_substage": paper_substage,
            "paper_worker_pid": pid if phase == "paper" else "",
            "paper_worker_kind": kind if phase == "paper" else "",
            "command": cmd,
            "cmd": cmd,
            "log_path": log_path,
            "artifacts": artifacts,
            "summary": worker_summary,
            "status": "running",
            "process_alive": True,
            "kind": kind,
            **({"web_job_id": web_find_job_id} if web_find_job_id else {}),
            "not_full_cycle_controller": True,
            **({"find_progress": find_progress_projection} if find_worker else {}),
        },
        "internal": False,
        "display": "",
        "error": "",
        "cancel_requested": False,
        "cancelled_at": "",
        "progress": {"phase": "find" if find_worker else progress_phase, "current": progress_current, "total": progress_total, "percent": progress_percent, "message": "；".join(message_bits)},
    }


def _live_jobs_from_projects(*, compact: bool = True, project_filter: str = "") -> list[dict[str, Any]]:
    """Build live research job rows without calling project_summary.

    This endpoint is polled frequently by the UI. It must only read small state
    files and process metadata; the heavier project summary endpoint feeds the
    main TASTE panels separately.
    """
    now = time.monotonic()
    project_filter = _api_query_str(project_filter)
    cache_key = f"{'compact' if compact else 'full'}:{project_filter}"
    if compact:
        cached = _LIVE_JOBS_CACHE.get(cache_key)
        if isinstance(cached, dict) and now < float(cached.get("expires_at") or 0.0):
            cached_items = cached.get("items")
            if isinstance(cached_items, list):
                return [dict(item) for item in cached_items if isinstance(item, dict)]
    jobs: list[dict[str, Any]] = []
    if project_filter:
        root = PROJECT_IDS_ROOT / project_filter
        cfg = _read_project_json(root / "project.json", {})
        if not root.exists() or not root.is_dir() or not isinstance(cfg, dict):
            return jobs
        projects = [{"id": project_filter, "name": cfg.get("name", project_filter), "path": str(root)}]
    else:
        try:
            projects = list_projects()
        except Exception:
            return jobs
    for project_row in projects:
        project = str(project_row.get("id") or project_row.get("name") or "").strip()
        if not project:
            continue
        root = Path(project_row.get("path") or "") if project_row.get("path") else Path("projects") / project
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        cfg = _read_project_json(root / "project.json", {})
        tick = _read_project_json(root / "state" / "supervision_tick.json", {})
        full_cycle = _read_project_json(root / "state" / "full_research_cycle.json", {})
        find_progress = _read_project_json_if_small(root / "planning" / "finding" / "find_progress.json", {})
        literature_packet = _read_project_json(root / "state" / "literature_tool_packet.json", {})
        current_plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
        reference_live_job = _live_reference_reproduction_job(project, root, current_plan if isinstance(current_plan, dict) else {}, compact=compact)
        reference_live_pid = ""
        if reference_live_job and str(reference_live_job.get("status") or "").lower() in {"queued", "running", "cancelling"}:
            # A wrapper-managed selected-base full reproduction is a real research job
            # launched by the cycle. Surface it in the taskbar while it is alive;
            # do not synthesize completed history after it exits.
            reference_live_job["stage"] = "experiment"
            reference_live_job["job_id"] = str(reference_live_job.get("job_id") or f"reference-reproduction_{project}")
            result = reference_live_job.get("result") if isinstance(reference_live_job.get("result"), dict) else {}
            reference_live_pid = str(result.get("pid") or reference_live_job.get("pid") or "").strip()
            jobs.append(reference_live_job)

        persisted_full_job = _read_project_json(root / "state" / "full_cycle_job.json", {})
        if not isinstance(persisted_full_job, dict):
            persisted_full_job = {}
        full_job = tick.get("full_cycle_job") if isinstance(tick, dict) and isinstance(tick.get("full_cycle_job"), dict) else {}
        if not full_job:
            full_job = dict(persisted_full_job)
        if not isinstance(full_job, dict):
            full_job = {}
        if persisted_full_job and str(persisted_full_job.get("pid") or "") == str(full_job.get("pid") or ""):
            for key in ["web_job_id", "log_path", "stdout_path", "fresh_start", "force_discovery", "use_existing_literature_packet"]:
                if full_job.get(key) in (None, "") and persisted_full_job.get(key) not in (None, ""):
                    full_job[key] = persisted_full_job.get(key)
        pid = str(full_job.get("pid") or "").strip()
        process_alive = _pid_alive_local(pid)
        cycle_status = str(full_cycle.get("status") or "").strip().lower() if isinstance(full_cycle, dict) else ""
        active_project_workers = _active_project_child_processes(project, root)
        if reference_live_pid:
            active_project_workers = [row for row in active_project_workers if str(row.get("pid") or "").strip() != reference_live_pid]
        active_project_worker = active_project_workers[0] if active_project_workers else {}
        if not process_alive and cycle_status == "running" and isinstance(full_cycle, dict):
            running_stage = full_cycle.get("current_running_stage") if isinstance(full_cycle.get("current_running_stage"), dict) else {}
            stage_pid = str(running_stage.get("pid") or "").strip()
            stage_cmd = str(running_stage.get("cmd") or running_stage.get("command") or "")
            if _pid_alive_local(stage_pid) and "run_full_research_cycle.py" in stage_cmd:
                pid = stage_pid
                process_alive = True
                full_job = {
                    **full_job,
                    "pid": pid,
                    "status": "running",
                    "started_at": running_stage.get("started_at") or full_cycle.get("started_at") or full_job.get("started_at"),
                    "cmd": stage_cmd or full_job.get("cmd") or "run_full_research_cycle.py",
                    "log_path": full_job.get("log_path") or "",
                    "kind": "full_cycle",
                }
            else:
                cycle_status = "stale_full_research_cycle_snapshot"
                full_job = {
                    **full_job,
                    "status": "stale",
                    "process_alive": False,
                    "alive": False,
                    "stale_reason": "no_matching_live_full_cycle_process",
                }
        if not process_alive and str(cycle_status).startswith("stale"):
            full_job = {**full_job, "status": "stale", "process_alive": False, "alive": False, "stale_reason": full_job.get("stale_reason") or "no_matching_live_full_cycle_process"}
        full_job_status = str(full_job.get("status") or "").strip().lower()
        include_dead_gate_job = bool(
            cycle_status.startswith("blocked")
            or str(cycle_status).startswith("stale")
            or full_job_status in {"blocked", "stale", "error"}
        )
        if not process_alive and not include_dead_gate_job:
            for worker in active_project_workers:
                worker_job = _active_project_worker_job(project, root, worker, full_cycle if isinstance(full_cycle, dict) else {}, current_plan if isinstance(current_plan, dict) else {}, find_progress if isinstance(find_progress, dict) else {}, compact=compact, controller_alive=False)
                if worker_job:
                    jobs.append(worker_job)
            continue
        if active_project_workers and not process_alive:
            for worker in active_project_workers:
                worker_job = _active_project_worker_job(project, root, worker, full_cycle if isinstance(full_cycle, dict) else {}, current_plan if isinstance(current_plan, dict) else {}, find_progress if isinstance(find_progress, dict) else {}, compact=compact, controller_alive=False)
                if worker_job:
                    jobs.append(worker_job)
        if not process_alive and full_job_status in {"blocked", "stale", "error"} and not cycle_status.startswith("blocked"):
            cycle_status = full_job_status
        ps_row = _ps_row_for_pid(pid) if process_alive else {}
        log_path = str(full_job.get("log_path") or "")
        if not log_path:
            log_path = _latest_project_log(root, "logs/supervision/full_research_cycle_*.log")
        phase, line_count, tail, raw_stage = _current_stage(full_cycle if isinstance(full_cycle, dict) else {}, log_path)
        if process_alive and phase not in {"experiment", "paper"} and _has_active_experiment_training(root, pid):
            phase = "experiment"
            raw_stage = raw_stage or "experiment"
        worker_jobs_for_live_controller: list[dict[str, Any]] = []
        if active_project_workers and process_alive:
            for worker in active_project_workers:
                worker_pid = str(worker.get("pid") or "").strip() if isinstance(worker, dict) else ""
                if not worker_pid or worker_pid == pid:
                    continue
                worker_job = _active_project_worker_job(project, root, worker, full_cycle if isinstance(full_cycle, dict) else {}, current_plan if isinstance(current_plan, dict) else {}, find_progress if isinstance(find_progress, dict) else {}, compact=compact, controller_alive=process_alive)
                if worker_job:
                    worker_jobs_for_live_controller.append(worker_job)
        active_child = _active_project_child_process(project, root) if process_alive else {}
        child_kind = str(active_child.get("kind") or "") if isinstance(active_child, dict) else ""
        child_cmd = str(active_child.get("cmd") or "") if isinstance(active_child, dict) else ""
        child_phase = str(active_child.get("phase") or full_job.get("stage") or "").strip().lower() if isinstance(active_child, dict) else ""
        if process_alive and child_phase in {"paper", "experiment", "literature"}:
            # The live child process is authoritative when an older full-cycle
            # controller has stopped or its log still points at a previous stage.
            if child_phase == "paper" or str(full_job.get("kind") or "") in {"paper_pipeline", "paper_repair_loop", "paper_claude_session", "experiment_training"}:
                phase = child_phase
                raw_stage = child_kind or raw_stage or child_phase
        # A Claude child can invoke framework/scripts/main.py module finding --action literature_tool while the
        # full-cycle remains in read/idea/experiment repair, and that wrapper may
        # spawn run_frontend/run_driver. Treat it as an auxiliary
        # literature subtask unless the public full-cycle phase is Find.
        fresh_find_running = bool(
            process_alive
            and cycle_status == "running"
            and (
                _active_stage_is_fresh_find(raw_stage)
                or (phase == "literature" and child_kind in {"frontend_recovery", "driver_recovery"})
            )
        )
        cmd = str(full_job.get("cmd") or full_job.get("command") or child_cmd or ps_row.get("cmd") or "")
        elapsed = str(ps_row.get("elapsed") or full_job.get("elapsed") or full_job.get("elapsed_sec") or "")
        run_id = ""
        if fresh_find_running or phase == "literature":
            run_id = _latest_find_run_id_from_runs(root, prefer_run_dir=True)
        if not run_id:
            # The taskbar should point at the current project Find packet. Older
            # full-cycle snapshots and selected-base provenance may mention the
            # Find run that originally selected the base; that is audit history,
            # not the current Find/read/idea/plan surface.
            for source in [find_progress, literature_packet, current_plan, tick, full_cycle]:
                if isinstance(source, dict):
                    run_id = str(source.get("run_id") or source.get("taste_run_id") or source.get("source_run_id") or source.get("current_find_run_id") or source.get("find_run_id") or "").strip()
                    if run_id:
                        break
        live_find_progress = find_progress if isinstance(find_progress, dict) else {}
        if fresh_find_running and run_id:
            run_progress = read_json(run_dir(run_id) / "find_progress.json", {})
            if isinstance(run_progress, dict) and run_progress:
                live_find_progress = run_progress
            else:
                live_find_progress = {"run_id": run_id, "live_progress": {"phase": "initializing", "current": 0, "total": 0, "percent": 0, "message": "Find initializing"}, "counts": {}}
        progress_run = str(find_progress.get("run_id") or "") if isinstance(find_progress, dict) else ""
        targeted_tool_status = current_plan.get("targeted_search_tool_status") if isinstance(current_plan.get("targeted_search_tool_status"), dict) else {}
        targeted_status_run = str(targeted_tool_status.get("current_find_run_id") or targeted_tool_status.get("run_id") or targeted_tool_status.get("find_run_id") or "").strip() if isinstance(targeted_tool_status, dict) else ""
        if progress_run and targeted_status_run and targeted_status_run != progress_run:
            targeted_tool_status = {}
        latest_targeted_tool = _read_project_json(root / "state" / "taste_targeted_queries.json", {})
        latest_status = str(latest_targeted_tool.get("status") or "") if isinstance(latest_targeted_tool, dict) else ""
        latest_has_failure = bool(isinstance(latest_targeted_tool, dict) and (latest_targeted_tool.get("failure_summary") or latest_targeted_tool.get("return_codes")))
        latest_run = str(latest_targeted_tool.get("current_find_run_id") or latest_targeted_tool.get("run_id") or latest_targeted_tool.get("find_run_id") or "") if isinstance(latest_targeted_tool, dict) else ""
        latest_matches_current_find = not (progress_run and latest_run and latest_run != progress_run)
        if isinstance(latest_targeted_tool, dict) and latest_matches_current_find and (latest_has_failure or latest_status.startswith("failed")):
            targeted_tool_status = {**targeted_tool_status, **{k: latest_targeted_tool.get(k, targeted_tool_status.get(k)) for k in ["status", "venue", "packet_return_code", "return_codes", "failure_summary", "guardrail", "record_only_requested", "new_find_allowed", "current_find_run_id"]}}
        progress_status = str(find_progress.get("status") or find_progress.get("phase") or "").lower() if isinstance(find_progress, dict) else ""
        current_find_recommendations_ready = bool(
            progress_run
            and progress_status == "complete"
            and _as_int(find_progress.get("strong_recommendation_count"), 0) >= max(1, _as_int(find_progress.get("recommendation_target_count"), 0))
            and _as_int(find_progress.get("recommendation_shortfall"), 0) == 0
        ) if isinstance(find_progress, dict) else False
        targeted_llm_blocked = _looks_like_llm_quota_blocker(targeted_tool_status)
        if current_find_recommendations_ready and "blocked_llm" not in progress_status and "quota" not in progress_status:
            targeted_llm_blocked = False
        llm_quota_blocked = (not fresh_find_running) and (
            "blocked_llm" in progress_status
            or "quota" in progress_status
            or _looks_like_llm_quota_blocker(find_progress.get("blocked_reason") if isinstance(find_progress, dict) else "")
            or targeted_llm_blocked
        )
        llm_blocker_reason = str(
            (find_progress.get("blocked_reason") if isinstance(find_progress, dict) else "")
            or (targeted_tool_status.get("failure_summary") if isinstance(targeted_tool_status, dict) else "")
            or (targeted_tool_status.get("error") if isinstance(targeted_tool_status, dict) else "")
            or "LLM API 额度/配置不可用，Find 必需的摘要评分或补评分无法继续。"
        )
        if llm_quota_blocked:
            cycle_status = "blocked_literature_llm_quota_exhausted"
        elif current_find_recommendations_ready and not process_alive:
            stale_literature_status = str(cycle_status).startswith("blocked_literature_llm_quota") or str(cycle_status).lower() in {"running", "stale", "stale_full_research_cycle_snapshot"}
            if stale_literature_status:
                cycle_status = "blocked_environment_base_selection_required"
        summary = str((full_cycle.get("summary_zh") if isinstance(full_cycle, dict) else "") or (full_cycle.get("summary") if isinstance(full_cycle, dict) else "") or (cfg.get("topic") if isinstance(cfg, dict) else "") or project)
        if llm_quota_blocked:
            summary = llm_blocker_reason
        elif cycle_status == "blocked_environment_base_selection_required":
            summary = "Find 推荐已完成；当前阻塞在环境阶段基底选择，旧 active_repo/旧参考复现不能放行当前流程。"
        elif not process_alive and (str(cycle_status).startswith("stale") or full_job_status == "stale"):
            latest_step = full_cycle.get("latest_step") if isinstance(full_cycle.get("latest_step"), dict) else {}
            stale_stage = str(full_job.get("stage") or latest_step.get("stage") or "full-cycle")
            stale_phase = _phase_from_stage(stale_stage)
            summary = f"完整科研自循环进程已停止；最后步骤={stale_stage}；阶段={stale_phase}；没有正在运行的 full-cycle。"
        elif process_alive and cycle_status == "running":
            public_stage_for_summary = "find" if phase == "literature" else (phase or "full-cycle")
            summary = (
                f"完整科研自循环正在运行；阶段={public_stage_for_summary}；PID={pid or '-'}"
                + (f"；运行时长={elapsed}" if elapsed else "")
            )
            if fresh_find_running:
                summary += "；新的 Find/文献调研正在运行，旧推荐统计仅作历史参考，等待本轮 Find 产物替换。"
            try:
                state_path = root / "state" / "full_research_cycle.json"
                if isinstance(full_cycle, dict) and state_path.exists():
                    current_summary = str(full_cycle.get("summary_zh") or full_cycle.get("summary") or "")
                    desired_goal = ""
                    if phase == "experiment" and _has_active_experiment_training(root, pid):
                        desired_goal = "等待当前训练日志和指标落盘；完成后由 Experimenting 主控 Claude 写本地审计记录、登记实验表，并刷新科学进展、论文证据、投稿准备度和阻塞行动计划门控。"
                    else:
                        raw_goal = str(full_cycle.get("current_goal") or "")
                        raw_goal_lower = raw_goal.lower()
                        if any(marker in raw_goal_lower for marker in ["deterministic", "base-switch", "base_switch", "route_scope", "selected-base", "launcher-scoped"]):
                            projected = _taskbgate_projection(root, cycle_status)
                            desired_goal = str(projected.get("next") or projected.get("goal") or "").strip()
                    desired_current_find_run_id = run_id if str(run_id or "").startswith("find_") else ""
                    needs_reconcile = "没有正在运行的 full-cycle" in current_summary or current_summary != summary
                    if desired_goal and (str(full_cycle.get("current_goal") or "") != desired_goal or bool(full_cycle.get("continuation_required"))):
                        needs_reconcile = True
                    if desired_current_find_run_id and str(full_cycle.get("current_find_run_id") or "") != desired_current_find_run_id:
                        needs_reconcile = True
                    if needs_reconcile:
                        reconciled = dict(full_cycle)
                        reconciled["summary"] = summary
                        reconciled["summary_zh"] = summary
                        if desired_current_find_run_id:
                            reconciled["current_find_run_id"] = desired_current_find_run_id
                        if desired_goal:
                            reconciled["current_goal"] = desired_goal
                            reconciled["continuation_required"] = False
                            reconciled["continuation_reason"] = ""
                        reconciled["updated_at"] = datetime.now(UTC).isoformat()
                        write_json(state_path, reconciled)
                        full_cycle = reconciled
            except Exception:
                pass
        path = str(root)
        common_artifacts = [
            f"{path}/state/full_research_cycle.json",
            f"{path}/state/supervision_tick.json",
        ]
        if cycle_status == "blocked_selected_base_viability_gate" or (
            isinstance(_read_project_json(root / "state" / "selected_base_viability_gate.json", {}), dict)
            and str(_read_project_json(root / "state" / "selected_base_viability_gate.json", {}).get("decision") or "").lower() == "base_switch_gate_required"
        ):
            common_artifacts.extend([
                f"{path}/state/selected_base_viability_gate.json",
                f"{path}/state/base_switch_gate.json",
                f"{path}/state/blocker_action_plan.json",
                f"{path}/state/active_repo.json",
                f"{path}/state/evidence_ready_repo_selection.json",
            ])
        phase_artifacts = {
            "literature": [
                f"{path}/planning/finding/find_results.json",
                f"{path}/planning/finding/find_progress.json",
                f"{path}/state/literature_tool_packet.json",
            ],
            "experiment": [
                f"{path}/experiments/experiment_records.csv",
                f"{path}/reports/reference_reproduction_gate.md",
                f"{path}/reports/experiment_iteration_audit.md",
                f"{path}/state/scientific_progress_gate.json",
            ],
            "paper": [
                f"{path}/paper/metadata/paper_pipeline.json",
                f"{path}/reports/paper_evidence_audit.md",
                f"{path}/state/submission_readiness.json",
            ],
        }
        detail_logs, detail_artifacts = _active_detail_lines(root, pid, phase) if process_alive else ([], [])
        artifacts = common_artifacts + phase_artifacts.get(phase, []) + detail_artifacts
        latest_blockers = full_cycle.get("latest_blockers") if isinstance(full_cycle, dict) and isinstance(full_cycle.get("latest_blockers"), list) else []
        primary_blocker = next((row for row in latest_blockers if isinstance(row, dict)), {})
        primary_blocker_issue = str(primary_blocker.get("issue") or "").strip() if isinstance(primary_blocker, dict) else ""
        primary_blocker_next = str(primary_blocker.get("next_action") or primary_blocker.get("human_summary") or "").strip() if isinstance(primary_blocker, dict) else ""
        job_status = "running" if process_alive else ("blocked" if str(cycle_status).startswith("blocked") else full_job_status or "blocked")
        status_label = "TASTE 完整科研循环正在运行" if process_alive else "TASTE 完整科研循环已停在门控"
        public_stage_for_logs = "find" if phase == "literature" else (phase or "full-cycle")
        logs = [
            status_label,
            f"project={project}",
            f"stage={public_stage_for_logs}",
            f"pid={pid or '-'}",
            f"process_alive={str(process_alive).lower()}",
        ]
        logs.append("当前阶段：" + public_stage_for_logs)
        human_stage = _humanize_stage(raw_stage)
        current_stage_status = _current_stage_status_line(raw_stage)
        if current_stage_status:
            logs.append("当前动作：" + current_stage_status)
            logs.append("stage_output=" + current_stage_status)
        elif human_stage and human_stage != public_stage_for_logs:
            logs.append("当前动作：" + human_stage)
        projected_gate = _taskbgate_projection(root, cycle_status, primary_blocker_issue, primary_blocker_next)
        if not process_alive and str(cycle_status).startswith("blocked"):
            summary = projected_gate["issue"]
            logs.append("门控阻塞：" + projected_gate["issue"])
            logs.append("当前目标：" + projected_gate["goal"])
            logs.append("下一步：" + projected_gate["next"])
        # Experiment details are appended below in compact form to avoid duplicate raw tails.
        logs = [line for line in logs if not _is_low_signal_claude_tool_line(line)]
        claude_status_lines = _latest_module_controller_status_lines(root, limit=12, stage_scope=phase) if process_alive else []
        if claude_status_lines:
            claude_status_lines = [
                line for line in claude_status_lines
                if not _is_stale_route_switch_taskbline(root, line)
            ]
            if _has_active_experiment_training(root, pid):
                claude_status_lines = [
                    line for line in claude_status_lines
                    if not str(line or "").startswith("claude_reply=")
                    and not (str(line or "").startswith("claude_status=") and "状态=完成" in str(line or ""))
                ]
            logs.extend(claude_status_lines)
        if process_alive and isinstance(full_cycle, dict):
            stage_lines = []
            for raw_stage_line in _dedupe_recent_lines(_current_stage_stdout_lines(full_cycle), limit=18):
                if not _log_tail_is_human_status(raw_stage_line) or _is_low_signal_claude_tool_line(raw_stage_line):
                    continue
                cleaned_stage_line = _summarize_claude_taskbline(raw_stage_line)
                if cleaned_stage_line and cleaned_stage_line not in stage_lines:
                    stage_lines.append(cleaned_stage_line)
            for line in stage_lines[-6:]:
                logs.append("stage_output=" + line[:900])
        if log_path:
            try:
                recent_file_lines = _tail_file_lines(Path(log_path), limit=30 if process_alive else 44, max_bytes=131072)
            except Exception:
                recent_file_lines = []
            file_lines = [
                line for line in _dedupe_recent_lines(recent_file_lines, limit=22 if process_alive else 28)
                if _log_tail_is_human_status(line) and not _is_low_signal_claude_tool_line(line)
            ]
            if not process_alive:
                stale_running_markers = [
                    "is still alive",
                    "let the running full-cycle worker proceed",
                    "advance to paper production",
                    "### Active Worker",
                    "### Next Actions",
                    "don't block template promotion",
                    "do not block template promotion",
                ]
                stale_claim_markers = [
                    "authorize base switch",
                    "allow-template",
                    "submission blockers cleared",
                    "needs_final_packaging",
                    "paper production",
                    "paper pipeline can be refreshed",
                    "scope_boundary",
                    "legacy route",
                    "non-current route",
                    "non current route",
                    "stale route",
                    "stale claim",
                    "unauthorized route switch",
                    "unverified route switch",
                    "all 4 submission blockers cleared",
                    "trajectory system reports `phase: repair_or_explore, assurance_status: pass`",
                    "### root cause",
                    "### files changed",
                    "### commands run",
                    "### evidence (gate status)",
                    "### still blocked / cleared",
                    "| gate | before | after |",
                    "|------|--------|-------|",
                    "evidence_gate_allows_template",
                    "paper_evidence_audit_json_pass",
                    "claim_ledger_supported",
                    "writing_audit_pass",
                    "audit_writing.py",
                    "final confirmation",
                    "claude: result success",
                ]
                file_lines = [
                    line for line in file_lines
                    if not any(marker.lower() in line.lower() for marker in stale_running_markers)
                    and not any(marker.lower() in line.lower() for marker in stale_claim_markers)
                ]
            # Always keep a bounded raw tail for auditability. Compact live-agent
            # rows are direct cleaned session/log fragments; deterministic gate
            # summaries stay separately labelled in the projected status lines.
            visible_file_lines = [] if claude_status_lines and process_alive else file_lines[-12 if process_alive else -18:]
            seen_full_cycle_tail: set[str] = set()
            for line in visible_file_lines:
                cleaned = _redact_public_log_text(line).strip()
                if not cleaned or _is_stale_or_internal_full_cycle_tail(cleaned):
                    continue
                seen_full_cycle_tail.add(cleaned)
                logs.append("full_cycle_output=" + cleaned[:900])
            raw_tail = _raw_log_tail_lines(recent_file_lines, limit=24 if process_alive else 32)
            raw_keep = 12 if process_alive and claude_status_lines else (18 if process_alive else 24)
            for line in raw_tail[-raw_keep:]:
                cleaned = _redact_public_log_text(line).strip()
                if not cleaned or cleaned in seen_full_cycle_tail:
                    continue
                if _is_machine_index_log_line(cleaned) or _is_stale_or_internal_full_cycle_tail(cleaned):
                    continue
                seen_full_cycle_tail.add(cleaned)
                logs.append("full_cycle_output=" + cleaned[:1200])
        for artifact_path in artifacts[:10]:
            logs.append("artifact=" + str(artifact_path))
        if phase == "experiment" and detail_logs:
            for line in _compact_experiment_detail_lines(detail_logs, limit=52):
                logs.append(line[:900])
        logs = _filter_stale_claude_wait_lines_for_finished_experiment(logs)
        if llm_quota_blocked:
            logs.append("summary=" + llm_blocker_reason[:500])
        elif str(cycle_status).startswith("blocked"):
            logs.append("summary=" + projected_gate["issue"][:500])
        elif cycle_status == "blocked_environment_base_selection_required":
            logs.append("summary=" + summary[:500])
        elif process_alive and cycle_status == "running":
            logs.append("summary=" + summary[:500])
        elif isinstance(full_cycle, dict) and (full_cycle.get("summary_zh") or full_cycle.get("summary")):
            stored_summary = str(full_cycle.get("summary_zh") or full_cycle.get("summary"))
            if not process_alive and stored_summary.startswith("完整科研自循环正在运行"):
                stored_summary = summary
            logs.append("summary=" + stored_summary[:500])
        if phase == "literature" and isinstance(full_cycle, dict):
            for activity_line in _find_activity_lines(full_cycle, limit=6):
                logs.append("find_activity=" + activity_line)
        if fresh_find_running and run_id:
            for status_line in _find_run_status_lines(run_id, limit=6):
                logs.append(status_line)
        latest_experiment_status = _current_stage_status_line(raw_stage) if phase == "experiment" else ""
        if not latest_experiment_status and phase == "experiment":
            latest_experiment_status = _latest_experiment_status_line(logs)
        # Experiment logs/status are direct evidence from the running command.
        # Do not let an older Claude heartbeat such as "waiting for epoch logs"
        # overwrite a completed epoch tail or an explicit "training ended" line.
        if claude_status_lines and process_alive and not latest_experiment_status:
            latest_claude_activity = _latest_claude_activity_from_status_lines(claude_status_lines)
            if latest_claude_activity and not _is_generic_claude_taskbsummary(latest_claude_activity) and not _is_low_signal_claude_tool_line(latest_claude_activity):
                latest_experiment_status = latest_claude_activity
            elif not latest_experiment_status:
                latest_experiment_status = latest_claude_activity
        if latest_experiment_status and process_alive:
            logs.append("latest=" + latest_experiment_status[:700])
        elif tail and process_alive and not _is_transient_taste_service_line(tail):
            readable_log_tail = tail if phase == "literature" else _humanize_stale_tail(tail, process_alive)
            logs.append("latest=" + (readable_log_tail or _humanize_stale_tail(tail, process_alive)))
        if llm_quota_blocked:
            logs.append("llm_blocker=" + llm_blocker_reason[:500])
        if fresh_find_running:
            logs.append("fresh_find_running=true; previous recommendation counts are historical until this Find completes")
        elif phase == "literature" and isinstance(find_progress, dict):
            counts = find_progress.get("counts") if isinstance(find_progress.get("counts"), dict) else {}
            strong_count = find_progress.get("strong_recommendation_count")
            target_count = find_progress.get("recommendation_target_count")
            shortfall = find_progress.get("recommendation_shortfall")
            if counts:
                logs.append("find_counts=" + ", ".join(f"{key}:{value}" for key, value in counts.items()))
            if strong_count is not None:
                target_label = target_count if target_count is not None else "?"
                logs.append(f"recommendations={strong_count}/{target_label}; shortfall={shortfall or 0}")
        elif isinstance(find_progress, dict):
            strong_count = find_progress.get("strong_recommendation_count")
            target_count = find_progress.get("recommendation_target_count")
            shortfall = find_progress.get("recommendation_shortfall")
            if strong_count is not None:
                target_label = target_count if target_count is not None else "?"
                logs.append(f"recommendations={strong_count}/{target_label}; shortfall={shortfall or 0}; current Find title+abstract LLM scoring complete")
        if phase == "literature" and isinstance(literature_packet, dict) and not fresh_find_running:
            packet_summary = literature_packet.get("summary") if isinstance(literature_packet.get("summary"), dict) else {}
            if packet_summary:
                logs.append("literature_packet=" + ", ".join(f"{key}:{value}" for key, value in packet_summary.items() if isinstance(value, (int, float, str)) )[:220])
        current_plan_run_id = str(current_plan.get("run_id") or current_plan.get("source_run_id") or current_plan.get("find_run_id") or "").strip() if isinstance(current_plan, dict) else ""
        current_plan_matches_run = bool(current_plan_run_id and run_id and current_plan_run_id == run_id)
        if isinstance(current_plan, dict) and not fresh_find_running and current_plan_matches_run:
            takeover = current_plan.get("claude_takeover") if isinstance(current_plan.get("claude_takeover"), dict) else {}
            blockers = current_plan.get("blockers") if isinstance(current_plan.get("blockers"), list) else []
            if phase == "literature" and takeover.get("status"):
                logs.append(f"claude_takeover={takeover.get('status')}")
            if blockers:
                logs.append("literature_blockers=" + " | ".join(str(item) for item in blockers[:3]))
        if log_path:
            logs.append(f"log={log_path}")
        if cmd:
            logs.append(f"cmd={cmd}")
        verb = "running" if process_alive else ("blocked" if str(cycle_status).startswith("blocked") else "stopped")
        display_phase = "find" if phase == "literature" else phase
        pid_text = f"PID={pid or '-'}" if process_alive else f"historical_pid={pid or '-'}"
        message_bits = [f"{display_phase} {verb}", pid_text]
        if elapsed:
            message_bits.append(f"elapsed={elapsed}")
        if cycle_status.startswith("blocked"):
            message_bits.append(f"gate={projected_gate['gate']}")
            message_bits.append(projected_gate["issue"][:180])
        live_progress_message = _find_live_progress_message(live_find_progress if isinstance(live_find_progress, dict) else {}) if phase == "literature" else ""
        if live_progress_message:
            message_bits.append(live_progress_message[:180])
        elif latest_experiment_status and process_alive:
            message_bits.append(latest_experiment_status[:180])
        elif tail and process_alive and not _is_transient_taste_service_line(tail):
            readable_tail = tail if phase == "literature" else _humanize_stale_tail(tail, process_alive)
            message_bits.append((readable_tail or _humanize_stale_tail(tail, process_alive))[:180])
        progress_total = 0 if process_alive else 1
        progress_current = line_count if process_alive else 1
        # Long-running stages do not always have a bounded denominator.
        # Keep percent at 0 when total=0; liveness is shown by status/message/events.
        progress_percent = 0 if process_alive else 100
        if process_alive and progress_current:
            message_bits.append(f"events={progress_current}")
        if process_alive and phase == "experiment" and _has_active_experiment_training(root, pid) and "实验训练异常退出" not in str(latest_experiment_status):
            exp_current, exp_total, exp_percent = _experiment_epoch_progress(logs, command=str(cmd or ""))
            if exp_current and exp_total:
                progress_current = exp_current
                progress_total = exp_total
                progress_percent = exp_percent
        live_progress = live_find_progress.get("live_progress") if isinstance(live_find_progress, dict) and isinstance(live_find_progress.get("live_progress"), dict) else {}
        if process_alive and phase == "literature" and live_progress:
            try:
                live_current = int(live_progress.get("current") or 0)
                live_total = int(live_progress.get("total") or 0)
                live_percent = int(live_progress.get("percent") or 0)
            except Exception:
                live_current = live_total = live_percent = 0
            if live_total > 0:
                progress_current = max(0, live_current)
                progress_total = live_total
                progress_percent = max(0, min(100, live_percent or round((progress_current / progress_total) * 100)))
        display_stage = "find" if phase == "literature" else phase
        # taskbar stages are top-level user workflow steps. Keep TASTE
        # internals such as autonomous-research in result.raw_stage/logs only.
        jobs.extend(worker_jobs_for_live_controller)
        suppress_dead_full_cycle = bool(reference_live_pid and not process_alive and str(full_job.get("kind") or "") == "full_cycle")
        if suppress_dead_full_cycle:
            continue
        jobs.append({
            "job_id": str(full_job.get("web_job_id") or f"full_cycle_{project}"),
            "stage": display_stage,
            "status": job_status,
            "created_at": str(full_job.get("started_at") or (full_cycle.get("started_at") if isinstance(full_cycle, dict) else "") or datetime.now(UTC).isoformat().replace("+00:00", "Z")),
            "logs": logs[-80:] if compact else logs,
            "log_count": len(logs),
            "run_id": run_id,
            "result": {
                "project": project,
                "pid": pid,
                "phase": phase,
                "raw_stage": raw_stage,
                "command": cmd,
                "log_path": log_path,
                "artifacts": artifacts,
                "summary": summary,
                "status": job_status,
                "process_alive": process_alive,
            },
            "internal": False,
            "display": "",
            "error": "",
            "cancel_requested": False,
            "cancelled_at": "",
            "progress": {"phase": phase, "current": progress_current, "total": progress_total, "percent": progress_percent, "message": "；".join(message_bits)},
        })
    if compact:
        _LIVE_JOBS_CACHE[cache_key] = {"items": [dict(item) for item in jobs], "expires_at": time.monotonic() + LIVE_JOBS_TTL_SEC}
    return jobs

def _humanize_stale_tail(text: Any, process_alive: bool) -> str:
    value = str(text or "")
    if process_alive:
        return value
    value = value.replace("full-cycle: running ", "full-cycle last command: ")
    value = value.replace("running ", "last command: ")
    return value




def _job_result_return_code(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    candidates: list[Any] = [
        result.get("returncode"),
        result.get("return_code"),
        result.get("exit_code"),
    ]
    latest_record = result.get("latest_record") if isinstance(result.get("latest_record"), dict) else {}
    candidates.extend([latest_record.get("returncode"), latest_record.get("return_code"), latest_record.get("exit_code")])
    framework = result.get("framework") or result.get("framework_status")
    if isinstance(framework, dict):
        fw_record = framework.get("latest_record") if isinstance(framework.get("latest_record"), dict) else {}
        candidates.extend([fw_record.get("returncode"), fw_record.get("return_code"), fw_record.get("exit_code")])
    for value in candidates:
        try:
            if value in (None, ""):
                continue
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _environment_job_rc30_blocked(stage: Any, progress: Any = None, error: Any = "", result: Any = None) -> bool:
    if str(stage or "").strip().lower() != "environment":
        return False
    if _job_result_return_code(result) == 30:
        return True
    progress_payload = progress if isinstance(progress, dict) else {}
    combined = " ".join(
        str(part or "")
        for part in [
            progress_payload.get("phase"),
            progress_payload.get("message"),
            error,
            result.get("summary") if isinstance(result, dict) else "",
        ]
    ).lower()
    return bool(re.search(r"(?:exit code|错误码|返回错误码)\s*30\b", combined))


def _parse_web_environment_run_timestamp(run_id: Any) -> float:
    match = re.search(r"web_environment_[^_]+_(\d{8})t(\d{6})z", str(run_id or "").strip().lower())
    if not match:
        return 0.0
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
    except Exception:
        return 0.0


def _parse_environment_runtime_dir_timestamp(run_id: Any) -> float:
    match = re.match(r"(\d{8})t(\d{6})(\d{6})z_", str(run_id or "").strip().lower())
    if not match:
        return 0.0
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S%f").replace(tzinfo=UTC).timestamp()
    except Exception:
        return 0.0


def _parse_public_job_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _environment_decision_for_job(run_id: Any, result: Any, created_at: Any) -> dict[str, Any]:
    result_payload = result if isinstance(result, dict) else {}
    project = str(result_payload.get("project") or "").strip()
    runs_root = ENVIRONMENT_RUNS_DIR
    candidates: list[tuple[float, Path]] = []

    explicit_run = str(
        result_payload.get("environment_run_id")
        or result_payload.get("run_id")
        or Path(str(result_payload.get("run_dir") or "")).name
        or run_id
        or ""
    ).strip()
    if explicit_run:
        explicit_path = runs_root / explicit_run / "environment_deployment_decision.json"
        if explicit_path.exists():
            candidates.append((0.0, explicit_path))
        elif explicit_run.startswith("web_environment_"):
            return {}

    created_ts = _parse_public_job_timestamp(created_at)
    try:
        paths = list(runs_root.glob("*/environment_deployment_decision.json"))
    except Exception:
        paths = []
    for path in paths:
        candidate_run = path.parent.name
        meta = _load_json_file(path.parent / "run_meta.json")
        requested_run = str(meta.get("requested_run_id") or "").strip()
        requested_project = str(meta.get("requested_project") or "").strip()
        if project and requested_project and requested_project != project:
            continue
        if project and not requested_project and f"web_environment_{project}_" not in requested_run:
            continue
        run_ts = _parse_web_environment_run_timestamp(requested_run) or _parse_environment_runtime_dir_timestamp(candidate_run)
        if created_ts and run_ts:
            diff = abs(run_ts - created_ts)
            if diff > 20 * 60:
                continue
            score = diff
        else:
            try:
                score = abs(path.stat().st_mtime - created_ts) if created_ts else -path.stat().st_mtime
            except OSError:
                score = 999999999.0
        candidates.append((score, path))

    if not candidates:
        latest_record = _load_json_file(WORKSPACE_ROOT / "projects" / project / "state" / "environment_handoff.json") if project else {}
        source = Path(str(latest_record.get("source") or "")) if isinstance(latest_record, dict) else Path("")
        if source.exists() and source.name == "environment_deployment_decision.json":
            return _load_json_file(source)
        return {}

    _score, path = sorted(candidates, key=lambda item: item[0])[0]
    return _load_json_file(path)


def _environment_failure_taxonomy_summary(decision: dict[str, Any]) -> str:
    verdict = decision.get("verdict") if isinstance(decision.get("verdict"), dict) else {}
    taxonomy = verdict.get("failure_taxonomy") if isinstance(verdict.get("failure_taxonomy"), list) else []
    if not taxonomy:
        reason = str(verdict.get("reject_reason") or decision.get("reject_reason") or "").strip()
        return _public_text(reason)[:260] if reason else "真实环境证据仍未通过。"
    first = taxonomy[0] if isinstance(taxonomy[0], dict) else {}
    category = str(first.get("category") or "").strip()
    label_map = {
        "conda_environment": "Conda 环境依赖",
        "repository": "仓库证据",
        "data": "数据/loader",
        "reproduction": "参考复现",
        "command": "验证命令",
        "workspace_audit": "工作区写入审计",
    }
    label = label_map.get(category, category.replace("_", " ") or "环境门控")
    evidence = first.get("evidence") if isinstance(first.get("evidence"), list) else []
    joined = " ".join(str(item or "") for item in evidence[:3]).lower()
    if "pyg" in joined and ("pytorch" in joined or "torch" in joined):
        detail = "PyG/PyTorch/CUDA/Python 版本组合未解算成功。"
    elif "libmambaunsatisfiableerror" in joined or "unsatisfiable" in joined:
        detail = "Conda 依赖解算失败，需要调整包源或版本约束。"
    elif evidence:
        detail = _public_text(str(evidence[0] or "")).strip()
        detail = re.sub(r"/[^\s;,]+", "[local-path]", detail)
        detail = detail[:220]
    else:
        detail = "真实验证证据仍未通过。"
    return f"{label}未通过：{detail}"


def _environment_decision_public_projection(job_id: Any, run_id: Any, result: Any, created_at: Any) -> dict[str, Any]:
    decision = _environment_decision_for_job(run_id, result, created_at)
    if not decision:
        return {}
    decision_value = str(decision.get("decision") or "").strip()
    exit_code = decision.get("exit_code")
    ready_for_experimenting = bool(
        decision.get("ready_for_experimenting") is True
        or decision_value == "environment_ready"
        or (isinstance(decision.get("environment_handoff"), dict) and decision.get("environment_handoff", {}).get("ready_for_experimenting") is True)
    )
    if decision_value not in {"continue_repair", "reject", "approve", "environment_ready"} and not ready_for_experimenting:
        return {}
    if ready_for_experimenting:
        status = "ready_for_experimenting"
        summary = "环境已交付：真实仓库、run-local Conda、数据准备和 loader/model smoke 已通过；论文指标仍由实验阶段验证。"
    elif decision_value == "approve":
        status = "done"
        summary = "环境配置已通过真实复现和工作区审计，可以进入实验迭代。"
    elif decision_value == "reject":
        status = "blocked"
        summary = "环境配置已拒绝当前路线：" + _environment_failure_taxonomy_summary(decision)
    else:
        status = "blocked"
        summary = "环境配置停在可修复真实门控：" + _environment_failure_taxonomy_summary(decision)
    audit = decision.get("workspace_write_audit") if isinstance(decision.get("workspace_write_audit"), dict) else {}
    if audit.get("status") == "passed" and "工作区写入审计" not in summary:
        summary += " 工作区写入审计已通过。"
    return {
        "run_id": str(decision.get("run_id") or run_id or ""),
        "status": status,
        "summary": summary,
        "decision": decision_value,
        "exit_code": exit_code,
        "allow_next_module": bool(decision.get("allow_next_module")),
        "source": "modules/environment/environment_deployment_decision.json",
    }


def _experiment_acceptance_public_summary(record: dict[str, Any]) -> str:
    blockers = record.get("acceptance_blockers") if isinstance(record.get("acceptance_blockers"), list) else []
    codes = {str(item.get("code") or "") for item in blockers if isinstance(item, dict)}
    if "missing_generation_pipeline" in codes and "missing_evaluation_pipeline" in codes:
        return "实验迭代被验收门控阻断：当前 RigidSSL 仓库缺少生成/采样和评估流水线；本轮只验证了环境、模型、数据、checkpoint 与训练 smoke，不能计为论文实验成功。"
    if blockers:
        messages = []
        for item in blockers[:3]:
            if not isinstance(item, dict):
                continue
            message = _public_text(str(item.get("message") or item.get("code") or "")).strip()
            if message:
                messages.append(message[:160])
        if messages:
            return "实验迭代被验收门控阻断：" + "；".join(messages) + "；本轮不得计为科研成功。"
    acceptance_status = str(record.get("acceptance_status") or "failed_acceptance_gate").strip()
    return f"实验迭代未通过验收门控：{acceptance_status or 'failed_acceptance_gate'}；本轮不得计为科研成功。"


def _latest_experiment_acceptance_projection(project: Any, created_at: Any) -> dict[str, Any]:
    project_id = str(project or "").strip()
    if not project_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", project_id):
        return {}
    project_root = WORKSPACE_ROOT / "projects" / project_id
    latest_run = read_json(project_root / "state" / "latest_experimenting_run.json", {})
    registry_paths = [project_root / "state" / "experiment_registry.json"]
    if isinstance(latest_run, dict) and latest_run.get("project_copy_dir"):
        registry_paths.append(Path(str(latest_run.get("project_copy_dir"))) / "state" / "experiment_registry.json")
    registry_payload: Any = []
    for registry_path in registry_paths:
        payload = read_json(registry_path, [])
        if payload:
            registry_payload = payload
            break
    if isinstance(registry_payload, dict):
        rows = registry_payload.get("experiments") if isinstance(registry_payload.get("experiments"), list) else []
    elif isinstance(registry_payload, list):
        rows = registry_payload
    else:
        rows = []
    created_ts = _parse_public_job_timestamp(created_at)
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        acceptance_status = str(row.get("acceptance_status") or "").strip()
        blockers = row.get("acceptance_blockers") if isinstance(row.get("acceptance_blockers"), list) else []
        row_status = str(row.get("status") or "").strip().lower()
        if not acceptance_status and not blockers:
            continue
        row_ts = _parse_public_job_timestamp(row.get("timestamp"))
        if created_ts and row_ts and row_ts + 60 < created_ts:
            continue
        if not row_ts:
            artifact_path = Path(str(row.get("artifact_path") or ""))
            try:
                row_ts = artifact_path.stat().st_mtime if artifact_path.exists() else 0.0
            except Exception:
                row_ts = 0.0
        if not row_ts:
            row_ts = created_ts or 0.0
        if not (acceptance_status.startswith("blocked_") or blockers or row_status in {"failed", "blocked"}):
            continue
        candidates.append((row_ts, row))
    if not candidates:
        return {}
    record = sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]
    acceptance_status = str(record.get("acceptance_status") or "failed_acceptance_gate").strip()
    return {
        "run_id": str(record.get("run_id") or ""),
        "project": project_id,
        "status": "blocked",
        "acceptance_status": acceptance_status,
        "acceptance_blockers": record.get("acceptance_blockers") if isinstance(record.get("acceptance_blockers"), list) else [],
        "artifact_path": str(record.get("artifact_path") or ""),
        "experiment_iteration_summary_path": str(record.get("experiment_iteration_summary_path") or ""),
        "experiment_iteration_summary_status": str(record.get("experiment_iteration_summary_status") or ""),
        "experiment_iteration_summary_acceptance_status": str(record.get("experiment_iteration_summary_acceptance_status") or ""),
        "summary": _experiment_acceptance_public_summary(record),
    }


def _normalize_public_job_status(public_status: Any, progress: Any = None, error: Any = "", result: Any = None, stage: Any = "") -> str:
    """Make taskbar status agree with terminal progress/error evidence."""
    if _environment_job_rc30_blocked(stage, progress, error, result):
        return "blocked"
    status = str(public_status or "").strip()
    lowered = status.lower()
    if lowered not in {"running", "queued", "cancelling", ""}:
        return status
    progress_payload = progress if isinstance(progress, dict) else {}
    phase = str(progress_payload.get("phase") or "").strip().lower()
    message = str(progress_payload.get("message") or "").strip().lower()
    error_text = str(error or "").strip().lower()
    result_status = ""
    if isinstance(result, dict):
        result_status = str(result.get("status") or "").strip().lower()
    combined = " ".join(part for part in [phase, message, error_text, result_status] if part)
    if phase in {"complete", "completed", "done", "success"}:
        return "done"
    if phase == "cancelled":
        return "cancelled"
    if phase == "interrupted":
        return "stale"
    if phase.startswith("blocked") or result_status.startswith("blocked") or "blocked_tool_policy" in combined or "tool policy" in combined:
        return "blocked"
    if error_text or phase in {"error", "failed", "fail"} or "research action failed" in combined or "exit code" in combined:
        return "error"
    return status or phase or "queued"



def _job_public_project(item: dict[str, Any], project_hint: str = "") -> str:
    return str(_job_project_id(item) or project_hint or "").strip()


def _job_public_stage(item: dict[str, Any]) -> str:
    return str(item.get("stage") or "").strip().lower()


def _job_has_public_artifact(item: dict[str, Any]) -> bool:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    item_status = str(item.get("status") or "").strip().lower()
    result_status = str(result.get("status") or "").strip().lower()
    if item_status == "done" or result_status == "done":
        return True
    for key in ("artifact_dir", "artifact_count", "find_results_path", "pdf_path", "tex_path", "latest_generated_pdf_path"):
        if result.get(key):
            return True
    return False


def _parse_job_time(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _environment_history_superseded_at(project: str) -> float:
    project_id = str(project or "").strip()
    if not project_id:
        return 0.0
    root = PROJECT_IDS_ROOT / project_id
    if not root.exists():
        return 0.0
    state = root / "state"
    record = _read_project_json(state / "environment_handoff.json", {})
    if not isinstance(record, dict):
        record = {}
    handoff = record.get("environment_handoff") if isinstance(record.get("environment_handoff"), dict) else {}
    gate = handoff.get("handoff_gate") if isinstance(handoff.get("handoff_gate"), dict) else {}
    runtime_ok = bool(record.get("valid") is True and handoff.get("ready_for_experimenting") is True and gate.get("passed") is True)
    if runtime_ok:
        ts = _parse_job_time(record.get("updated_at") or handoff.get("finished_at"))
        if ts > 0:
            return ts
    full_job = _read_project_json(state / "fresh_base_reference_full_reproduction_job.json", {})
    if isinstance(full_job, dict):
        status = str(full_job.get("status") or "").strip().lower()
        pid = str(full_job.get("pid") or "").strip()
        if status in {"running", "completed", "done", "pass"} and (status != "running" or _pid_alive_local(pid)):
            ts = _parse_job_time(full_job.get("generated_at") or full_job.get("updated_at"))
            if ts > 0:
                return ts
    audit = _read_project_json(state / "fresh_base_reference_full_reproduction_audit.json", {})
    if isinstance(audit, dict) and audit.get("mode") == "full" and audit.get("return_code") == 0 and audit.get("audit_ready"):
        ts = _parse_job_time(audit.get("generated_at") or audit.get("finished_at"))
        if ts > 0:
            return ts
    return 0.0


def _hide_superseded_stopped_jobs(rows: list[dict[str, Any]], project_hint: str = "") -> list[dict[str, Any]]:
    """Hide old stopped rows once a newer usable job represents the same stage.

    The persisted job file remains the audit trail. The public taskbar should not
    keep showing restart/cancelled rows with no artifact after a newer job for the
    same project stage has completed or is actively carrying the workflow.
    """
    active_statuses = {"queued", "running", "cancelling", "done", "blocked", "preview_available", "needs_writing"}
    stopped_statuses = {"cancelled", "interrupted", "stale", "error"}
    latest_active_created: dict[tuple[str, str], str] = {}
    for item in rows:
        status = str(item.get("status") or "").strip().lower()
        if status not in active_statuses:
            continue
        project = _job_public_project(item, project_hint)
        stage = _job_public_stage(item)
        if not project or not stage:
            continue
        created = str(item.get("created_at") or "")
        key = (project, stage)
        if created > latest_active_created.get(key, ""):
            latest_active_created[key] = created

    env_history_superseded_cache: dict[str, float] = {}
    env_history_statuses = {
        "blocked",
        "blocked_environment_base_selection_required",
        "blocked_environment_bootstrap_failed",
        "blocked_environment_bootstrap_required",
    }
    kept: list[dict[str, Any]] = []
    for item in rows:
        status = str(item.get("status") or "").strip().lower()
        project = _job_public_project(item, project_hint)
        stage = _job_public_stage(item)
        created = str(item.get("created_at") or "")
        superseded_at = latest_active_created.get((project, stage), "")
        if project and stage == "environment" and status in env_history_statuses and not _job_has_public_artifact(item):
            if project not in env_history_superseded_cache:
                env_history_superseded_cache[project] = _environment_history_superseded_at(project)
            env_superseded_at = env_history_superseded_cache.get(project, 0.0)
            if env_superseded_at and _parse_job_time(created) and _parse_job_time(created) < env_superseded_at:
                continue
        if (
            project
            and stage
            and status in stopped_statuses
            and superseded_at
            and created < superseded_at
            and not _job_has_public_artifact(item)
        ):
            continue
        kept.append(item)
    return kept


def _safe_job_fragment(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return text.strip("_") or "unknown"


def _current_find_artifact_timestamp(paths: list[Path], fallback: str = "") -> str:
    newest = 0.0
    for path in paths:
        try:
            if path.exists():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    fallback_text = str(fallback or "").strip()
    if fallback_text:
        try:
            parsed = datetime.fromisoformat(fallback_text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            newest = max(newest, parsed.timestamp())
        except ValueError:
            if newest <= 0:
                return fallback_text
    if newest > 0:
        return datetime.fromtimestamp(newest, UTC).isoformat().replace("+00:00", "Z")
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _current_find_stage_list(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _current_find_selected_id(stage_state: dict[str, Any], payload: dict[str, Any], rows: list[dict[str, Any]], *keys: str) -> str:
    for key in keys:
        value = str(stage_state.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    for row in rows:
        if row.get("selected_for_execution") or row.get("execute_next"):
            for key in keys:
                value = str(row.get(key) or row.get("id") or row.get("plan_id") or "").strip()
                if value:
                    return value
    return ""


def _current_find_stage_job_status(stage_state: dict[str, Any], count: int) -> str:
    raw_status = str(stage_state.get("status") or "").strip().lower()
    if not count or raw_status.startswith("blocked") or raw_status in {"error", "failed", "fail"}:
        return "blocked"
    return "done"


def _json_count_value(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except Exception:
        return 0


def _first_positive_count(*values: Any, default: int = 0) -> int:
    for value in values:
        count = _json_count_value(value)
        if count > 0:
            return count
    return default


def _current_find_read_history_job(project_id: str, root: Path, run_id: str, state_payload: dict[str, Any]) -> dict[str, Any] | None:
    finding = root / "planning" / "finding"
    artifact_path = finding / "read_results.json"
    payload = _read_project_json(artifact_path, {})
    if not isinstance(payload, dict):
        return None
    payload_run_id = _payload_run_id(payload)
    if payload_run_id and payload_run_id != run_id:
        return None

    rows = _current_find_stage_list(payload, "readings") or _current_find_stage_list(state_payload, "readings")
    count = len(rows)
    if count <= 0:
        return None

    validation_path = root / "state" / "current_find_claude_reading_validation.json"
    validation = _read_project_json(validation_path, {})
    if not isinstance(validation, dict):
        validation = {}
    embedded_validation = state_payload.get("reading_validation")
    if isinstance(embedded_validation, dict):
        validation = {**validation, **embedded_validation}

    expected_reading_count = _first_positive_count(
        validation.get("expected_reading_count"),
        validation.get("selected_reading_count"),
        state_payload.get("current_find_reading_count"),
        payload.get("selected_reading_count"),
        payload.get("expected_reading_count"),
        default=count,
    )
    recommendation_count = _first_positive_count(
        payload.get("recommendation_count"),
        validation.get("expected_recommendation_count"),
        validation.get("recommended_reading_count"),
        default=0,
    )
    full_text_count = _first_positive_count(
        validation.get("full_text_reading_count"),
        validation.get("full_text_evidence_count"),
        state_payload.get("full_text_reading_count"),
        payload.get("full_text_reading_count"),
        default=sum(1 for row in rows if row.get("full_text_available") or str(row.get("full_text_status") or "").strip()),
    )
    pending_count = _json_count_value(
        validation.get("pending_full_text_reading_count")
        if "pending_full_text_reading_count" in validation
        else state_payload.get("pending_full_text_reading_count")
    )
    pending_deep_count = _json_count_value(
        validation.get("pending_deep_read_synthesis_count")
        if "pending_deep_read_synthesis_count" in validation
        else state_payload.get("pending_deep_read_synthesis_count")
    )
    deep_read_count = _first_positive_count(
        validation.get("deep_read_complete_count"),
        payload.get("deep_read_complete_count"),
        default=sum(1 for row in rows if row.get("deep_read_complete") is True),
    )
    validation_valid = validation.get("valid")
    read_md_path = finding / "read.md"
    try:
        read_md_present = bool(read_md_path.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        read_md_present = False
    warning_details = validation.get("warning_details") if isinstance(validation.get("warning_details"), list) else []
    status = _current_find_stage_job_status(state_payload, count)
    read_complete_with_warnings = bool(validation_valid is True and read_md_present and pending_count <= 0 and pending_deep_count > 0)
    read_run_complete_with_missing_full_text = bool(
        read_md_present
        and pending_count > 0
        and pending_deep_count <= 0
        and deep_read_count + pending_count >= (expected_reading_count or count)
    )
    if read_run_complete_with_missing_full_text:
        status = "done"
    elif pending_count > 0 or validation_valid is False:
        status = "blocked"
    elif read_complete_with_warnings:
        status = "done"
    elif pending_deep_count > 0:
        status = "blocked"

    if read_run_complete_with_missing_full_text:
        summary = f"Read 阶段已完成并有警告：爬文章已处理 {expected_reading_count or count}/{expected_reading_count or count} 篇，其中同篇全文可用 {full_text_count}/{expected_reading_count or count} 篇；读文章完成 {deep_read_count}/{deep_read_count} 篇；{pending_count} 篇全文未就绪，保留在下游证据门控。"
    elif read_complete_with_warnings:
        summary = f"Read 阶段已完成并有警告：当前展示 {count}/{expected_reading_count or count} 篇；同篇全文证据 {full_text_count or count}/{expected_reading_count or count} 篇；精读完成 {deep_read_count or count}/{expected_reading_count or count} 篇；{pending_deep_count} 篇未进入最终 read.md，仅记录在任务日志和机器状态。"
    elif status == "done":
        warning_suffix = f"；warning {len(warning_details)} 项" if warning_details else ""
        summary = f"Read 阶段已完成：当前展示 {count}/{expected_reading_count or count} 篇；同篇全文证据 {full_text_count or count}/{expected_reading_count or count} 篇；精读完成 {deep_read_count or count}/{expected_reading_count or count} 篇{warning_suffix}。"
    else:
        summary = f"Read 阶段仍需补证：当前展示 {count}/{expected_reading_count or count} 篇；同篇全文证据 {full_text_count}/{expected_reading_count or count} 篇；精读完成 {deep_read_count}/{expected_reading_count or count} 篇；待补全文 {pending_count} 篇，待精读 {pending_deep_count} 篇。"

    created_at = _current_find_artifact_timestamp(
        [artifact_path, validation_path, root / "state" / "current_find_research_plan.json"],
        str(payload.get("generated_at") or state_payload.get("generated_at") or ""),
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "project": project_id,
        "kind": "current_find_downstream_artifact_history",
        "source": "planning/finding/read_results.json",
        "status": status,
        "summary": summary,
        "read_count": count,
        "expected_reading_count": expected_reading_count,
        "selected_reading_count": expected_reading_count,
        "recommendation_count": recommendation_count,
        "full_text_reading_count": full_text_count,
        "pending_full_text_reading_count": pending_count,
        "deep_read_complete_count": deep_read_count,
        "pending_deep_read_synthesis_count": pending_deep_count,
        "reading_validation_valid": validation_valid,
    }
    return {
        "job_id": f"current-find-read_{_safe_job_fragment(project_id)}_{_safe_job_fragment(run_id)}",
        "stage": "read",
        "status": status,
        "created_at": created_at,
        "logs": [f"当前状态：{summary}", f"运行编号：{run_id}"],
        "log_count": 2,
        "run_id": run_id,
        "result": result,
        "internal": False,
        "display": "",
        "error": "",
        "cancel_requested": False,
        "cancelled_at": "",
        "progress": {
            "phase": "complete" if status == "done" else "blocked",
            "current": count,
                "total": max(expected_reading_count, count, 1),
            "percent": 100 if status == "done" else 0,
            "message": summary,
        },
    }


def _current_find_existing_stage_keys(items: list[dict[str, Any]], project_hint: str = "") -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        stage = _job_public_stage(item)
        if stage not in {"idea", "plan"}:
            continue
        project = _job_public_project(item, project_hint)
        run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
        if project and run_id:
            keys.add((project, run_id, stage))
    return keys


def _current_find_downstream_stage_history_jobs(project_filter: str = "", existing_items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Expose current-Find downstream artifacts as seven-stage task history rows.

    Current-Find is executed by a read wrapper, but it writes Read, Idea and Plan
    artifacts. The public taskbar should reflect the stages that exist on disk;
    the persisted raw wrapper jobs remain the detailed audit trail.
    """
    project_filter = _api_query_str(project_filter)
    existing_keys = _current_find_existing_stage_keys(existing_items or [], project_filter)
    if not PROJECT_IDS_ROOT.exists():
        return []
    if project_filter:
        roots = [(project_filter, PROJECT_IDS_ROOT / project_filter)]
    else:
        roots = [(root.name, root) for root in sorted(PROJECT_IDS_ROOT.iterdir()) if root.is_dir()]
    jobs: list[dict[str, Any]] = []
    for project_id, root in roots:
        if not root.exists() or not root.is_dir():
            continue
        finding = root / "planning" / "finding"
        state_path = root / "state" / "current_find_research_plan.json"
        state_payload = _read_project_json(state_path, {})
        if not isinstance(state_payload, dict):
            state_payload = {}
        run_id = _project_current_find_run_id(root, allow_large_find_results=False)
        if not run_id:
            run_id = _payload_run_id(state_payload)
        if not run_id:
            continue
        read_job = _current_find_read_history_job(project_id, root, run_id, state_payload)
        if read_job:
            jobs.append(read_job)
        for stage, artifact_name, rows_key, id_keys, zh_label in [
            ("idea", "ideas.json", "ideas", ("selected_idea_id", "id"), "Idea"),
            ("plan", "plans.json", "plans", ("selected_plan_id", "plan_id"), "Plan"),
        ]:
            if (project_id, run_id, stage) in existing_keys:
                continue
            artifact_path = finding / artifact_name
            payload = _read_project_json(artifact_path, {})
            if not isinstance(payload, dict):
                continue
            payload_run_id = _payload_run_id(payload)
            if payload_run_id and payload_run_id != run_id:
                continue
            rows = _current_find_stage_list(payload, rows_key)
            count = len(rows)
            if count <= 0:
                continue
            selected_id = _current_find_selected_id(state_payload, payload, rows, *id_keys)
            status = _current_find_stage_job_status(state_payload, count)
            if stage == "idea":
                scored_count = sum(1 for row in rows if row.get("score") not in (None, ""))
            else:
                scored_count = sum(1 for row in rows if row.get("selected_for_execution") or row.get("execute_next") or row.get("ready_for_gate"))
            summary_parts = [f"{zh_label} 阶段已形成 {count} 条产物"]
            if scored_count:
                summary_parts.append(f"{scored_count} 条带评分/执行状态")
            summary = "，".join(summary_parts) + "。"
            created_at = _current_find_artifact_timestamp([artifact_path, state_path], str(payload.get("generated_at") or state_payload.get("generated_at") or ""))
            result: dict[str, Any] = {
                "run_id": run_id,
                "project": project_id,
                "kind": "current_find_downstream_artifact_history",
                "source": f"planning/finding/{artifact_name}",
                "status": status,
                "summary": summary,
                f"{stage}_count": count,
                f"{stage}_scored_count": scored_count,
            }
            if selected_id:
                result[f"selected_{stage}_id"] = selected_id
            jobs.append({
                "job_id": f"current-find-{stage}_{_safe_job_fragment(project_id)}_{_safe_job_fragment(run_id)}",
                "stage": stage,
                "status": status,
                "created_at": created_at,
                "logs": [],
                "log_count": 0,
                "run_id": run_id,
                "result": result,
                "internal": False,
                "display": "",
                "error": "",
                "cancel_requested": False,
                "cancelled_at": "",
                "progress": {
                    "phase": "complete" if status == "done" else "blocked",
                    "current": count,
                    "total": max(count, 1),
                    "percent": 100 if status == "done" else 0,
                    "message": summary,
                },
            })
    return jobs


def _collapse_current_find_read_retry_jobs(rows: list[dict[str, Any]], project_hint: str = "") -> list[dict[str, Any]]:
    """Keep one public read row per project/run while preserving active work."""
    latest_real: dict[tuple[str, str], dict[str, Any]] = {}
    latest_history: dict[tuple[str, str], dict[str, Any]] = {}
    for item in rows:
        if _job_public_stage(item) != "read":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        project = _job_public_project(item, project_hint)
        run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
        if not project or not run_id:
            continue
        key = (project, run_id)
        is_history = str(result.get("kind") or "") == "current_find_downstream_artifact_history"
        target = latest_history if is_history else latest_real
        previous = target.get(key)
        if previous is None or str(item.get("created_at") or "") >= str(previous.get("created_at") or ""):
            target[key] = item

    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for key in set(latest_real) | set(latest_history):
        real = latest_real.get(key)
        history = latest_history.get(key)
        if real is None:
            chosen[key] = history
            continue
        if history is None:
            chosen[key] = real
            continue
        real_status = str(real.get("status") or "").strip().lower()
        real_is_current = (
            real_status in {"queued", "running", "cancelling", "done"}
            or str(real.get("created_at") or "") >= str(history.get("created_at") or "")
        )
        chosen[key] = real if real_is_current else history

    kept: list[dict[str, Any]] = []
    for item in rows:
        if _job_public_stage(item) != "read":
            kept.append(item)
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        project = _job_public_project(item, project_hint)
        run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
        key = (project, run_id)
        if project and run_id and chosen.get(key) is not item:
            continue
        kept.append(item)
    return kept


def _live_reference_reproduction_job(project: str, root: Path, current_plan: dict[str, Any], *, compact: bool = True) -> dict[str, Any]:
    reference_job = _read_project_json(root / "state" / "fresh_base_reference_full_reproduction_job.json", {})
    if not isinstance(reference_job, dict) or not reference_job:
        return {}

    pid = str(reference_job.get("pid") or "").strip()
    process_alive = _pid_alive_local(pid)
    status_text = str(reference_job.get("status") or "").strip().lower()
    if not process_alive and status_text not in {"running", "blocked", "error", "stale", "done", "completed", "success", "passed"}:
        return {}

    ps_row = _ps_row_for_pid(pid) if process_alive else {}
    descendant_rows = _process_tree_rows(pid) if process_alive and pid else []
    active_child_rows = []
    for row in descendant_rows:
        child_pid = str(row.get("pid") or "").strip()
        if not child_pid or child_pid == pid:
            continue
        child_cmd = str(row.get("cmd") or "").lower()
        if any(marker in child_cmd for marker in ["python", "torchrun", "accelerate", "train", "finetune", "example"]):
            active_child_rows.append(row)
    def _row_cpu(row: dict[str, Any]) -> float:
        try:
            return float(str(row.get("pcpu") or "0"))
        except Exception:
            return 0.0
    active_child = max(active_child_rows, key=_row_cpu) if active_child_rows else {}
    audit = _read_project_json(root / "state" / "fresh_base_reference_full_reproduction_audit.json", {})
    if not (isinstance(audit, dict) and str(audit.get("mode") or "") == "full"):
        legacy_audit = _read_project_json(root / "state" / "fresh_base_reference_reproduction_audit.json", {})
        audit = legacy_audit if isinstance(legacy_audit, dict) and str(legacy_audit.get("mode") or "") == "full" else {}
    gate = _read_project_json(root / "state" / "reference_reproduction_gate.json", {})
    base_switch = gate.get("base_switch") if isinstance(gate, dict) and isinstance(gate.get("base_switch"), dict) else {}
    fresh_base = base_switch.get("fresh_paper_base") if isinstance(base_switch.get("fresh_paper_base"), dict) else {}
    selected_base = audit.get("selected_base") if isinstance(audit, dict) and isinstance(audit.get("selected_base"), dict) else {}

    paper_title = str(
        fresh_base.get("title")
        or fresh_base.get("literature_base_title")
        or selected_base.get("literature_base_title")
        or ""
    ).strip()
    repo_name = str(
        fresh_base.get("name")
        or fresh_base.get("repo_name")
        or fresh_base.get("repo")
        or audit.get("repo_name")
        or selected_base.get("name")
        or ""
    ).strip()
    run_id = str(
        selected_base.get("fresh_find_run_id")
        or current_plan.get("run_id")
        or current_plan.get("source_run_id")
        or current_plan.get("find_run_id")
        or ""
    ).strip()

    artifact_dir = str(reference_job.get("artifact_dir") or "").strip()
    log_path = str(reference_job.get("log_path") or "").strip()
    if not artifact_dir and log_path:
        try:
            artifact_dir = str(Path(log_path).resolve().parent)
        except Exception:
            artifact_dir = str(Path(log_path).parent)
    compat_enabled = bool(artifact_dir and (Path(artifact_dir) / "compat" / "sitecustomize.py").exists())

    elapsed = str(ps_row.get("elapsed") or reference_job.get("elapsed") or reference_job.get("elapsed_sec") or "").strip()
    cmd = " ".join(str(part) for part in (reference_job.get("command") or reference_job.get("cmd") or [])) if isinstance(reference_job.get("command") or reference_job.get("cmd"), list) else str(reference_job.get("command") or reference_job.get("cmd") or "")
    if process_alive:
        job_status = "running"
    elif status_text in {"done", "completed", "success", "passed"}:
        job_status = "done"
    else:
        job_status = "blocked"

    status_line = "Selected-base full reference reproduction is running." if process_alive else "Selected-base full reference reproduction is not running."
    logs = [
        status_line,
        f"project={project}",
    ]
    if paper_title:
        logs.append(f"paper={paper_title}")
    if repo_name:
        logs.append(f"repo={repo_name}")
    logs.extend([
        f"pid={pid or '-'}",
        f"process_alive={str(process_alive).lower()}",
    ])
    if compat_enabled:
        logs.append("compat=artifact-local sitecustomize torch.load(weights_only=False) shim enabled for trusted checkpoint")
    logs.append("guardrail=不写论文、不提升结论、不启动第二条 Find、不回退历史路线")
    if not process_alive and status_text == "running":
        logs.append("state=wrapper pid missing; awaiting reference reproduction audit/gate refresh")
    elif not process_alive and status_text:
        logs.append(f"state={status_text}")
    if log_path:
        logs.append(f"log={log_path}")
    if artifact_dir:
        logs.append(f"artifact_dir={artifact_dir}")
    if cmd:
        logs.append(f"cmd={cmd}")

    progress_total = _as_int(reference_job.get("epoch") or reference_job.get("epochs"), 0)
    if not progress_total and cmd:
        match = re.search(r"--epochs\s+(\d+)", cmd)
        if match:
            progress_total = _as_int(match.group(1), 0)
    progress_current = 0
    latest_training_log = ""
    latest_training_log_age_sec = 0
    latest_training_log_updated_at = ""
    if artifact_dir:
        try:
            candidates = sorted((Path(artifact_dir) / "models").glob("training_log*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                latest = candidates[0]
                latest_training_log = str(latest)
                stat = latest.stat()
                latest_training_log_age_sec = max(0, int(time.time() - stat.st_mtime))
                latest_training_log_updated_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
                tail = latest.read_text(encoding="utf-8", errors="replace")[-20000:]
                epochs = [int(item) for item in re.findall(r"Epoch:\s*(\d+)", tail)]
                if epochs:
                    progress_current = max(epochs)
        except Exception:
            progress_current = 0
    if latest_training_log:
        logs.append(f"training_log={latest_training_log}")
        if latest_training_log_updated_at:
            logs.append(f"training_log_updated_at={latest_training_log_updated_at}")
        if process_alive and latest_training_log_age_sec > 300:
            logs.append(f"training_log_sparse=true; age_sec={latest_training_log_age_sec}; process tree remains the authoritative liveness signal")
    if active_child:
        logs.append(
            "active_training_child="
            + f"pid={active_child.get('pid')}; elapsed={active_child.get('elapsed')}; cpu={active_child.get('pcpu')}; mem={active_child.get('pmem')}"
        )

    message_bits = [
        f"selected-base full reproduction {'running' if process_alive else job_status}",
        f"PID={pid or '-'}",
    ]
    if elapsed:
        message_bits.append(f"elapsed={elapsed}")
    if active_child:
        child_pid = str(active_child.get("pid") or "").strip()
        child_cpu = str(active_child.get("pcpu") or "").strip()
        if child_pid:
            message_bits.append(f"training_pid={child_pid}" + (f" cpu={child_cpu}%" if child_cpu else ""))
    if progress_total and progress_current:
        message_bits.append(f"epoch={min(progress_current, progress_total)}/{progress_total}")
    if paper_title:
        message_bits.append(paper_title[:96])
    if not process_alive and job_status == "blocked":
        message_bits.append("请以 reference reproduction audit/gate 为准")

    if process_alive and progress_total and progress_current:
        progress_current_display = min(progress_current, progress_total)
        progress_total_display = progress_total
        progress_percent = min(99, int(progress_current_display * 100 / max(progress_total_display, 1)))
        progress_phase = "reference_reproduction_epoch"
    else:
        progress_current_display = 0 if process_alive else 1
        progress_total_display = 1
        progress_percent = 0 if process_alive else 100
        progress_phase = "running" if process_alive else job_status

    return {
        "job_id": str(reference_job.get("web_job_id") or f"reference-reproduction_{project}"),
        "stage": "reference-reproduction",
        "status": job_status,
        "created_at": str(reference_job.get("generated_at") or reference_job.get("started_at") or datetime.now(UTC).isoformat().replace("+00:00", "Z")),
        "logs": logs[-80:] if compact else logs,
        "log_count": len(logs),
        "run_id": run_id,
        "result": {
            "project": project,
            "pid": pid,
            "command": cmd,
            "log_path": log_path,
            "artifact_dir": artifact_dir,
            "summary": status_line,
            "status": job_status,
            "process_alive": process_alive,
        },
        "internal": False,
        "display": "",
        "error": "",
        "cancel_requested": False,
        "cancelled_at": "",
        "progress": {
            "phase": progress_phase,
            "current": progress_current_display,
            "total": progress_total_display,
            "percent": progress_percent,
            "message": "；".join(message_bits),
        },
    }

def _pid_is_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _process_tree_pids(root_pid: Any) -> list[int]:
    try:
        root_value = int(root_pid)
    except Exception:
        return []
    if root_value <= 0:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            text=True,
            capture_output=True,
            timeout=2,
        )
    except Exception:
        return [root_value]
    children: dict[int, list[int]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        children.setdefault(ppid, []).append(pid)
    ordered: list[int] = []
    stack = [root_value]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
        stack.extend(children.get(pid, []))
    return ordered


def _terminate_process_tree(root_pid: Any) -> dict[str, Any]:
    pids = _process_tree_pids(root_pid)
    if not pids:
        return {"requested_pid": str(root_pid or ""), "terminated_pids": [], "terminated_pgids": []}
    pgids: set[int] = set()
    for pid in pids:
        try:
            pgids.add(os.getpgid(pid))
        except Exception:
            pass
    current_pgid = os.getpgid(0)
    pgids.discard(current_pgid)
    for pgid in sorted(pgids, reverse=True):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass
    for pid in sorted(pids, reverse=True):
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.5)
    still_alive = [pid for pid in pids if pid != os.getpid() and _pid_is_alive(pid)]
    if still_alive:
        for pgid in sorted(pgids, reverse=True):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        for pid in sorted(still_alive, reverse=True):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return {"requested_pid": str(root_pid or ""), "terminated_pids": pids, "terminated_pgids": sorted(pgids)}


def _reconcile_detached_launcher_jobs(dynamic_items: list[dict[str, Any]] | None = None, *, force: bool = False) -> None:
    """Keep web-created detached research jobs aligned with project state.

    /api/jobs/project starts full-cycle as a detached worker so the web server can
    restart without killing research. The web-created job id is written into
    state/full_cycle_job.json; this reconciler keeps that same job row
    running/blocked/done instead of showing an idle launcher plus a separate
    synthetic row.
    """
    now = time.monotonic()
    if not force and now < float(_DETACHED_JOB_RECONCILE_CACHE.get("expires_at") or 0.0):
        return
    _DETACHED_JOB_RECONCILE_CACHE["expires_at"] = now + DETACHED_JOB_RECONCILE_TTL_SEC
    changed = False
    if dynamic_items is None:
        try:
            dynamic_items = _live_jobs_from_projects(compact=True)
        except Exception:
            dynamic_items = []
    dynamic_by_id = {str(item.get("job_id") or ""): item for item in dynamic_items if isinstance(item, dict)}
    dynamic_by_project: dict[str, dict[str, Any]] = {}
    for item in dynamic_items:
        result = item.get("result") if isinstance(item, dict) and isinstance(item.get("result"), dict) else {}
        project = str(result.get("project") or "")
        if project:
            dynamic_by_project[project] = item
    with JOBS_LOCK:
        jobs_snapshot = list(JOBS.values())
        for job in jobs_snapshot:
            stage = str(job.stage or "")
            if stage not in {"full-cycle", "full_research_cycle"}:
                continue
            result = job.result if isinstance(job.result, dict) else {}
            full_cycle_job = result.get("full_cycle_job") if isinstance(result.get("full_cycle_job"), dict) else {}
            pid = result.get("pid") or full_cycle_job.get("pid")
            project = str(result.get("project") or full_cycle_job.get("project") or "")
            detached_launcher = any("Detached full-cycle started" in str(line) for line in (job.logs or [])) or bool(pid and result.get("log_path"))
            if not detached_launcher:
                continue
            dynamic = dynamic_by_id.get(job.job_id) or (dynamic_by_project.get(project) if project else None)
            if isinstance(dynamic, dict) and dynamic:
                dyn_result = dynamic.get("result") if isinstance(dynamic.get("result"), dict) else {}
                dyn_status = str(dynamic.get("status") or "")
                if dyn_status:
                    job.status = dyn_status
                if dynamic.get("run_id") and not job.run_id:
                    job.run_id = str(dynamic.get("run_id") or "")
                merged = dict(result)
                merged.update(dyn_result)
                if full_cycle_job:
                    merged["full_cycle_job"] = {**full_cycle_job, **{k: v for k, v in dyn_result.items() if k in {"pid", "project", "command", "cmd", "log_path", "status", "process_alive"}}}
                job.result = merged
                if isinstance(dynamic.get("progress"), dict):
                    job.progress = dynamic.get("progress")
                for line in dynamic.get("logs", [])[-8:] if isinstance(dynamic.get("logs"), list) else []:
                    text = str(line)
                    if text and text not in job.logs:
                        job.logs.append(text)
                if job.status in {"done", "error", "cancelled", "blocked"}:
                    job.mark_finished(str(dynamic.get("finished_at") or ""))
                    job.done.set()
                else:
                    job.done.clear()
                changed = True
                continue
            if pid and not _pid_is_alive(pid):
                full_state = {}
                if project:
                    try:
                        full_state = _read_project_json(PROJECT_IDS_ROOT / project / "state" / "full_research_cycle.json", {})
                    except Exception:
                        full_state = {}
                cycle_status = str((full_state if isinstance(full_state, dict) else {}).get("status") or "")
                if cycle_status in {"completed", "done"}:
                    job.status = "done"
                    job.set_progress("complete", 1, 1, "Detached full-cycle completed and project state is complete.")
                    job.log("Detached full-cycle worker completed; launcher job reconciled to done.")
                else:
                    job.status = "blocked"
                    job.error = ""
                    message = "Detached full-cycle worker is no longer running; current project state is blocked by the evidence gate."
                    if cycle_status.startswith("blocked"):
                        message = f"Detached full-cycle worker stopped at gate={cycle_status}."
                    job.set_progress("blocked", 0, 1, message)
                    job.log("Detached full-cycle worker is no longer running; launcher job reconciled to blocked.")
                job.mark_finished()
                changed = True
    if changed:
        _persist_jobs()


def _job_list_project_summary(project_id: str, *, compact: bool = True) -> dict[str, Any]:
    project_id = str(project_id or "").strip()
    if not project_id:
        return {}
    key = (project_id, bool(compact), id(project_summary))
    now = time.monotonic()
    cached = _JOB_LIST_PROJECT_SUMMARY_CACHE.get(key)
    if isinstance(cached, dict) and now < float(cached.get("expires_at") or 0.0):
        payload = cached.get("payload")
        return dict(payload) if isinstance(payload, dict) else {}
    try:
        payload = project_summary(project_id, compact=compact)
    except TypeError:
        payload = project_summary(project_id)
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        _JOB_LIST_PROJECT_SUMMARY_CACHE[key] = {
            "expires_at": time.monotonic() + JOB_LIST_PROJECT_SUMMARY_TTL_SEC,
            "payload": dict(payload),
        }
        return dict(payload)
    return {}


def _summary_handoff_ready_for_experimenting(summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    stages = summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
    environment = stages.get("environment") if isinstance(stages.get("environment"), dict) else {}
    handoff = summary.get("environment_handoff") if isinstance(summary.get("environment_handoff"), dict) else {}
    selection = environment.get("selection") if isinstance(environment.get("selection"), dict) else {}
    if str(selection.get("reason") or "") == "stale_environment_handoff_projection":
        return False
    if handoff.get("ready_for_experimenting") is True and str(handoff.get("policy_version") or ""):
        return True
    status_values = [environment.get("status"), environment.get("repo_status"), handoff.get("status")]
    return any(str(value or "").strip().lower() == "ready_for_experimenting" for value in status_values)


def _stale_environment_handoff_job_projection(project_id: Any) -> dict[str, str]:
    project = str(project_id or "").strip()
    if not project:
        return {}
    summary = _job_list_project_summary(project, compact=True)
    if not isinstance(summary, dict) or _summary_handoff_ready_for_experimenting(summary):
        return {}
    stages = summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
    environment = stages.get("environment") if isinstance(stages.get("environment"), dict) else {}
    selection = environment.get("selection") if isinstance(environment.get("selection"), dict) else {}
    stale = str(selection.get("reason") or "") == "stale_environment_handoff_projection"
    current_status = str(summary.get("status") or environment.get("status") or "").strip()
    if not stale and current_status != "blocked_environment_base_selection_required":
        return {}
    message = str(summary.get("summary") or environment.get("summary") or "历史 environment handoff 已被当前门控失效；请重新运行环境阶段。")
    return {
        "status": "stale",
        "phase": current_status or "blocked_environment_base_selection_required",
        "summary": "历史环境交接已被当前环境门控失效：" + message,
    }


def _project_handoff_ready_for_experimenting(project_id: Any) -> bool:
    project = str(project_id or "").strip()
    if not project:
        return False
    try:
        return _summary_handoff_ready_for_experimenting(_job_list_project_summary(project, compact=True))
    except Exception:
        return False


def _compact_job_for_list(item: dict[str, Any]) -> dict[str, Any]:
    """Small row for the frequently-polled jobs list."""
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    raw_stage = str(item.get("stage", ""))
    panel_stage = _panel_stage_from_module_controller_result(result)
    paper_job = _is_paper_job(raw_stage, item.get("job_id", ""), result, item.get("logs"))
    full_cycle_job = False if panel_stage else _is_full_cycle_job(raw_stage, item.get("job_id", ""), result, item.get("logs"))
    recovered_project_worker = bool(
        str(item.get("job_id") or "").startswith(("project-worker_", "experiment-worker_"))
        or result.get("not_full_cycle_controller") is True
    )
    if full_cycle_job and recovered_project_worker:
        full_cycle_job = False
    public_stage = panel_stage or ("paper" if paper_job else (_public_full_cycle_stage(raw_stage, item.get("progress"), result) if full_cycle_job else _public_taste_stage(raw_stage)))
    compact_result: dict[str, Any] = {}
    result_keys = ["run_id", "project", "topic", "target_venue", "action", "agent_id", "target_agent_id", "requested_stage", "panel_stage", "pid", "cmd", "kind", "log_path", "artifact_dir", "find_results_path", "find_results_size_bytes", "phase", "raw_stage", "summary", "status", "acceptance_status", "acceptance_blockers", "artifact_path", "process_alive", "alive", "find_progress", "current_substage", "paper_current_substage", "paper_execution_alive", "paper_execution_state", "paper_execution_message", "paper_worker_pid", "paper_worker_kind", "paper_controller_pid", "paper_worker_elapsed", "paper_worker_pcpu", "paper_worker_pmem"]
    if full_cycle_job:
        result_keys = [key for key in result_keys if key != "cmd"]
    for key in result_keys:
        if key in result:
            compact_result[key] = result.get(key)
    if public_stage == "paper":
        paper_stage = _paper_stage_from_job_result(result)
        if paper_stage:
            paper_keys = [
                "paper_summary", "paper_stage", "status", "venue", "target_venue", "venue_slug", "template_family", "paper_normality_status",
                "paper_venue_format_status", "paper_figure_quality_status",
                "paper_normality_citation_count", "paper_normality_citation_target",
                "paper_normality_reference_target_source", "paper_normality_pages",
                "paper_normality_body_pages", "paper_normality_estimated_reference_pages",
                "paper_reference_quality_target", "paper_reference_official_min", "paper_citation_render_status", "paper_citation_render_ready", "paper_citation_render_blockers", "paper_self_review_status", "paper_self_review_ready", "paper_self_review_receipt", "paper_self_review_blockers", "paper_self_review_evidence_blockers", "paper_self_review_evidence_blocker_count", "paper_self_review_preview_only_ready", "paper_self_review_submission_evidence_ready", "paper_self_review_independent_findings_count", "paper_self_review_repairs_count", "conference_preview_ready", "conference_preview_pages",
                "conference_preview_body_pages", "conference_preview_body_page_limit",
                "conference_preview_reference_pages", "conference_preview_blocker_summary",
                "paper_layout_summary", "paper_public_diagnostics",
                "paper_layout_footprint_warnings", "conference_preview_blockers",
                "venue_requirements_status", "venue_requirements_path",
                "venue_requirements_summary", "venue_requirements_public_summary",
                "blocked_preview_available", "blocked_pdf_path", "blocked_tex_path",
                "latest_generated_pdf_path", "latest_generated_tex_path", "raw_pdf_path", "raw_tex_path", "paper_content_policy_status", "paper_content_blocker_summary", "paper_stage_status", "paper_execution_alive", "paper_execution_state", "paper_execution_message", "pdf_path", "tex_path",
            ]
            paper_summary = _paper_stage_job_message(paper_stage) or str(paper_stage.get("summary") or result.get("paper_summary") or "")
            compact_result["paper_summary"] = paper_summary
            for hidden_key in ("paper_citation_render_blockers", "paper_self_review_blockers", "paper_self_review_evidence_blockers", "conference_preview_blockers"):
                paper_stage[hidden_key] = []
            compact_result["paper_stage"] = {key: paper_stage.get(key) for key in paper_keys if key in paper_stage}
            for key in paper_keys:
                if key in paper_stage and key not in {"paper_summary", "paper_stage", "status"}:
                    compact_result[key] = paper_stage.get(key)
            result = {**result, **compact_result}
            if _paper_result_has_live_execution(result, item.get("status")):
                live_execution = _paper_execution_projection(result)
                compact_result.update(live_execution)
                if isinstance(compact_result.get("paper_stage"), dict):
                    compact_result["paper_stage"] = {**compact_result["paper_stage"], **live_execution}
                result = {**result, **compact_result}
    artifacts = result.get("artifacts") or result.get("artifact_paths")
    if isinstance(artifacts, (list, dict)):
        compact_result["artifact_count"] = len(artifacts)
        compact_result["artifact_labels"] = _public_job_artifact_labels(artifacts)
    counts = result.get("artifact_counts") if isinstance(result.get("artifact_counts"), dict) else {}
    for key, value in result.items():
        if key.endswith("_count") and isinstance(value, (int, float)) and not key.startswith(("paper_", "conference_preview_")):
            counts[key[:-6]] = value
    if counts:
        compact_result["artifact_counts"] = counts
    if public_stage != raw_stage and "raw_stage" not in compact_result:
        compact_result["raw_stage"] = raw_stage
    public_status = str(item.get("status", "") or "")
    if public_status.strip().lower() == "interrupted":
        public_status = "stale"
    if panel_stage and isinstance(result, dict):
        result_status = str(result.get("status") or "").strip()
        if result_status:
            public_status = result_status
    if paper_job and public_stage == "paper" and isinstance(result, dict):
        progress_payload = item.get("progress") if isinstance(item.get("progress"), dict) else {}
        paper_row = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else result
        paper_status = str(paper_row.get("status") or "").strip()
        live_paper_job = _job_status_is_live(public_status) or result.get("process_alive") is True
        if live_paper_job:
            if result.get("process_alive") is True and public_status not in {"queued", "cancelling"}:
                public_status = "running"
            compact_result["status"] = public_status
        elif _paper_content_policy_blocked(paper_row) or _paper_content_policy_blocked(compact_result) or paper_status in {"blocked_content_policy", "content_policy_blocked"}:
            public_status = "blocked"
        elif _paper_preview_artifact_available(paper_row) and public_status not in {"running", "queued", "cancelling", "error", "cancelled"}:
            public_status = "preview_available"
        elif paper_status in _PAPER_PREVIEW_GATE_BLOCKED_STATUSES:
            public_status = "preview_available" if (_paper_preview_artifact_available(paper_row) or _paper_preview_artifact_available(compact_result)) else "preview_pdf_blocked"
        elif paper_status:
            public_status = "blocked" if paper_status.startswith("blocked") else paper_status
    public_progress = dict(item.get("progress")) if isinstance(item.get("progress"), dict) else {}
    if paper_job and public_stage == "paper" and isinstance(result, dict) and not (_job_status_is_live(public_status) or result.get("process_alive") is True):
        paper_summary = str(compact_result.get("paper_summary") or result.get("paper_summary") or "").strip()
        paper_stage_row = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else result
        if paper_summary:
            public_progress["message"] = paper_summary
            public_progress["phase"] = str(paper_stage_row.get("status") or compact_result.get("status") or public_status or public_progress.get("phase") or "paper")
            public_progress["current"] = public_progress.get("current") or 1
            public_progress["total"] = public_progress.get("total") or 1
            public_progress["percent"] = public_progress.get("percent") or 100
    if paper_job and public_stage == "paper" and isinstance(result, dict) and (_job_status_is_live(public_status) or result.get("process_alive") is True):
        live_message = _paper_live_status_message(result, public_progress) if result.get("process_alive") is True else str(public_progress.get("message") or "")
        if live_message:
            public_progress["message"] = live_message
        public_progress["phase"] = str(result.get("paper_current_substage") or result.get("current_substage") or public_progress.get("phase") or "paper")
        public_progress["current"] = public_progress.get("current") or 0
        public_progress["total"] = public_progress.get("total") or 0
        public_progress["percent"] = public_progress.get("percent") or 0
    if public_stage in {"environment", "experiment"}:
        command_message = _public_stage_command_message(public_stage, public_progress.get("message"))
        if command_message:
            public_progress["message"] = command_message
        elif _is_module_controller_panel_job(raw_stage, item.get("job_id", ""), result):
            public_progress["message"] = _public_module_controller_progress_message(public_stage, public_progress.get("message"))
    if public_stage == "environment" and _job_status_is_live(public_status) and not full_cycle_job:
        project_id = str(compact_result.get("project") or result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
        result_phase = str(result.get("phase") or compact_result.get("phase") or "").strip().lower()
        worker_kind = str(result.get("kind") or compact_result.get("kind") or "").strip().lower()
        recovered_environment_worker = bool(
            str(item.get("job_id") or "").startswith("project-worker_")
            or recovered_project_worker
        )
        if project_id and result_phase == "environment" and recovered_environment_worker and _project_handoff_ready_for_experimenting(project_id):
            monitor_message = "环境已交付 experimenting：真实 repo/env/data/loader 可用；后台完整复现/审计仍在记录论文指标证据，不阻塞实验入口。"
            public_stage = "handoff_monitor"
            compact_result["raw_stage"] = compact_result.get("raw_stage") or raw_stage or "environment"
            compact_result["phase"] = "environment"
            compact_result["kind"] = worker_kind or compact_result.get("kind") or "environment_stage"
            compact_result["handoff_ready_for_experimenting"] = True
            compact_result["exclusive_stage"] = False
            compact_result["summary"] = monitor_message
            compact_result["status"] = public_status or "running"
            public_progress["phase"] = "ready_for_experimenting"
            public_progress["message"] = monitor_message
    if public_stage == "environment" and public_status == "blocked":
        project_id = str(compact_result.get("project") or result.get("project") or "").strip()
        if project_id:
            try:
                live_summary = _job_list_project_summary(project_id, compact=True)
            except Exception:
                live_summary = {}
            if isinstance(live_summary, dict):
                full_cycle = live_summary.get("full_research_cycle") if isinstance(live_summary.get("full_research_cycle"), dict) else {}
                stages = live_summary.get("stages") if isinstance(live_summary.get("stages"), dict) else {}
                environment = stages.get("environment") if isinstance(stages.get("environment"), dict) else {}
                live_status = str(live_summary.get("status") or full_cycle.get("status") or public_status).strip()
                live_message = _human_progress_message(full_cycle or live_summary, fallback=str(environment.get("summary") or ""))
                has_specific_environment_gate = bool(
                    live_status.startswith("blocked_fresh_base")
                    or live_status.startswith("blocked_environment_bootstrap")
                    or "真实数据/loader" in live_message
                    or "环境 bootstrap" in live_message
                    or "environment bootstrap" in live_message.lower()
                    or "repo_env_bootstrap" in live_message
                    or "环境阶段已选择" in live_message
                    or "current candidate base" in live_message.lower()
                )
                if live_message and has_specific_environment_gate:
                    public_progress["message"] = live_message
                    public_progress["phase"] = live_status or public_progress.get("phase") or "blocked"
                    public_progress["current"] = 1
                    public_progress["total"] = 1
                    public_progress["percent"] = 100
                    compact_result["summary"] = live_message
                    if live_status:
                        compact_result["status"] = live_status
        stale_message = str(public_progress.get("message") or "")
        if "not_started" in stale_message:
            fallback = "历史环境配置任务已阻塞；当前状态以最新环境任务和项目摘要为准。"
            public_progress["message"] = fallback
            public_progress["phase"] = public_progress.get("phase") or "blocked"
            public_progress["current"] = public_progress.get("current") or 1
            public_progress["total"] = public_progress.get("total") or 1
            public_progress["percent"] = public_progress.get("percent") or 100
            compact_result["summary"] = fallback
        progress_phase = str(public_progress.get("phase") or "").strip()
        progress_message = str(public_progress.get("message") or "")
        if "环境 bootstrap" in progress_message or "environment bootstrap" in progress_message.lower() or "repo_env_bootstrap" in progress_message:
            public_status = "blocked_environment_bootstrap_failed"
            if progress_phase in {"", "blocked"}:
                public_progress["phase"] = public_status
            compact_result["status"] = public_status
        elif "真实数据/loader 已通过" in progress_message or "等待参考协议" in progress_message or "reference-protocol" in progress_message.lower():
            public_status = "blocked_fresh_base_reference_probe_required"
            if progress_phase in {"", "blocked", "blocked_fresh_base_data_required"}:
                public_progress["phase"] = public_status
            compact_result["status"] = public_status
        elif "真实数据/loader" in progress_message or "real dataset/loader" in progress_message.lower():
            public_status = "blocked_fresh_base_data_required"
            if progress_phase in {"", "blocked"}:
                public_progress["phase"] = public_status
            compact_result["status"] = public_status
        elif public_status == "done" and progress_phase.startswith("blocked"):
            public_status = progress_phase
            compact_result["status"] = public_status
    if public_stage == "environment":
        progress_phase = str(public_progress.get("phase") or "").strip()
        progress_message = str(public_progress.get("message") or "")
        if "环境 bootstrap" in progress_message or "environment bootstrap" in progress_message.lower() or "repo_env_bootstrap" in progress_message:
            public_status = "blocked_environment_bootstrap_failed"
            if progress_phase in {"", "blocked"}:
                public_progress["phase"] = public_status
            compact_result["status"] = public_status
        elif "真实数据/loader 已通过" in progress_message or "等待参考协议" in progress_message or "reference-protocol" in progress_message.lower():
            public_status = "blocked_fresh_base_reference_probe_required"
            if progress_phase in {"", "blocked", "blocked_fresh_base_data_required"}:
                public_progress["phase"] = public_status
            compact_result["status"] = public_status
        elif "真实数据/loader" in progress_message or "real dataset/loader" in progress_message.lower():
            public_status = "blocked_fresh_base_data_required"
            if progress_phase in {"", "blocked"}:
                public_progress["phase"] = public_status
            compact_result["status"] = public_status
        elif public_status == "done" and progress_phase.startswith("blocked"):
            public_status = progress_phase
            compact_result["status"] = public_status
    if full_cycle_job:
        if isinstance(compact_result.get("summary"), str) and any(marker in compact_result["summary"].lower() for marker in ["候选路线", "独立授权", "base_switch", "selected_base", "deterministic"]):
            compact_result["summary"] = "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"
        if isinstance(public_progress.get("message"), str) and any(marker in public_progress["message"].lower() for marker in ["候选路线", "独立授权", "base_switch", "selected_base", "deterministic", "historical_pid"]):
            public_progress["message"] = "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"
        project_id = str(compact_result.get("project") or result.get("project") or "").strip()
        if project_id and (public_status in {"blocked", "error", "cancelled"} or str(public_status).startswith("blocked")):
            try:
                live_summary = _job_list_project_summary(project_id, compact=True)
            except Exception:
                live_summary = {}
            live_payload = live_summary if isinstance(live_summary, dict) else {}
            blocker = live_payload.get("current_blocker") if isinstance(live_payload.get("current_blocker"), dict) else {}
            live_cycle = live_payload.get("full_research_cycle") if isinstance(live_payload.get("full_research_cycle"), dict) else {}
            live_status = str(live_payload.get("status") or live_cycle.get("status") or "").strip()
            blocker_category = str(blocker.get("category") or "").strip()
            blocker_message = str(blocker.get("summary") or blocker.get("human_summary") or blocker.get("issue") or "").strip()
            cycle_message = str(live_cycle.get("summary") or live_cycle.get("summary_zh") or live_payload.get("summary") or "").strip()
            live_message = blocker_message if (live_status == "blocked_fresh_base_reference_probe_required" or blocker_category == "fresh_base_reference_probe_required") else (cycle_message or blocker_message)
            if live_message:
                public_status = live_status or public_status
                compact_result["status"] = public_status
                compact_result["summary"] = live_message
                public_progress["phase"] = public_status
                public_progress["message"] = live_message
                public_progress["current"] = 1
                public_progress["total"] = 1
                public_progress["percent"] = 100
    if public_stage == "environment":
        decision_projection = _environment_decision_public_projection(
            item.get("job_id", ""),
            item.get("run_id") or compact_result.get("run_id") or result.get("run_id"),
            compact_result or result,
            item.get("created_at", ""),
        )
        project_id = str(compact_result.get("project") or result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
        stale_projection = _stale_environment_handoff_job_projection(project_id)
        if stale_projection and decision_projection.get("status") == "ready_for_experimenting":
            decision_projection = {**decision_projection, **stale_projection}
        if decision_projection:
            public_status = str(decision_projection.get("status") or public_status or "blocked")
            public_progress.update({
                "phase": public_status,
                "current": 1,
                "total": 1,
                "percent": 100,
                "message": str(decision_projection.get("summary") or ""),
            })
            compact_result["status"] = public_status
            compact_result["summary"] = str(decision_projection.get("summary") or compact_result.get("summary") or "")
            compact_result["run_id"] = str(decision_projection.get("run_id") or compact_result.get("run_id") or "")
            compact_result["environment_decision"] = decision_projection
    if public_stage == "experiment":
        project_id = str(compact_result.get("project") or (result.get("project") if isinstance(result, dict) else "") or "").strip()
        needs_acceptance_projection = public_status in {"error", "failed"} or bool(item.get("error")) or str(compact_result.get("status") or "").startswith("blocked_")
        if project_id and needs_acceptance_projection:
            acceptance_projection = _latest_experiment_acceptance_projection(project_id, item.get("created_at", ""))
            if acceptance_projection:
                compact_result.update(acceptance_projection)
                if isinstance(result, dict):
                    result = {**result, **acceptance_projection}
                public_status = str(acceptance_projection.get("status") or "blocked")
                public_progress.update({
                    "phase": public_status,
                    "current": 1,
                    "total": 1,
                    "percent": 100,
                    "message": str(acceptance_projection.get("summary") or ""),
                })
        detailed_status = str(public_status or "").strip()
        if detailed_status.startswith("blocked_"):
            compact_result["acceptance_status"] = compact_result.get("acceptance_status") or detailed_status
            compact_result["status"] = "blocked"
            if isinstance(result, dict):
                result = {**result, "status": "blocked", "acceptance_status": compact_result.get("acceptance_status") or detailed_status}
            public_status = "blocked"
    if public_stage == "read":
        public_progress = _read_job_progress_payload(item.get("logs"), public_progress, result, status=public_status)
        if isinstance(public_progress.get("read_progress"), dict):
            compact_result["read_progress"] = public_progress.get("read_progress")
            result = {**result, "read_progress": public_progress.get("read_progress")} if isinstance(result, dict) else compact_result
    if public_stage == "plan" and str(public_status or "").lower() in {"queued", "running", "cancelling"}:
        raw_logs = item.get("logs") if isinstance(item.get("logs"), list) else []
        stage_detail = _public_plan_stage_detail(raw_logs)
        if stage_detail:
            public_progress["message"] = stage_detail
            if str(public_progress.get("phase") or "").strip().lower() in {"", "queued", "started"}:
                public_progress["phase"] = "planning"
            public_progress["current"] = int(public_progress.get("current") or 0)
            public_progress["total"] = int(public_progress.get("total") or 1)
            public_progress["percent"] = int(public_progress.get("percent") or 0)
    public_status = _normalize_public_job_status(public_status, public_progress, item.get("error", ""), compact_result or result, public_stage)
    if str(public_status).strip().lower() == "interrupted":
        public_status = "stale"
    if public_status == "ready_for_experimenting":
        compact_result["status"] = "ready_for_experimenting"
        public_status = "done"
    elif public_status in {"blocked", "error", "cancelled", "done"}:
        compact_result["status"] = public_status
    public_log_result = compact_result if full_cycle_job or public_stage == "read" else result
    payload = {
        "job_id": item.get("job_id", ""),
        "stage": public_stage,
        "status": public_status,
        "created_at": item.get("created_at", ""),
        "finished_at": item.get("finished_at", ""),
        "logs": _public_job_logs(panel_stage or ("paper" if paper_job else ("full-cycle" if full_cycle_job else raw_stage)), item.get("logs"), public_progress, public_log_result, limit=40),
        "log_count": item.get("log_count", 0),
        "run_id": item.get("run_id", "") or compact_result.get("run_id", ""),
        "result": compact_result,
        "internal": bool(item.get("internal")),
        "display": item.get("display", ""),
        "error": item.get("error", ""),
        "cancel_requested": bool(item.get("cancel_requested")),
        "cancelled_at": item.get("cancelled_at", ""),
        "progress": public_progress,
    }
    return _public_job_api_payload(payload)


def _persist_jobs() -> None:
    with JOBS_PERSIST_LOCK:
        _persist_jobs_locked()


def _persist_jobs_locked() -> None:
    if not globals().get("JOBS_PATH"):
        return
    with JOBS_LOCK:
        jobs_snapshot = list(JOBS.values())
    persisted_snapshot: list[dict[str, Any]] = []
    for job in jobs_snapshot:
        public_stage = _public_taste_stage(getattr(job, "stage", ""))
        compact = not (public_stage == "read" and str(getattr(job, "status", "") or "").lower() in {"queued", "running", "cancelling"})
        item = job.as_dict(compact=compact)
        item["owner_id"] = job.owner_id
        if not _job_is_hollow_route(item):
            persisted_snapshot.append(item)
    items = sorted(
        persisted_snapshot,
        key=lambda item: item["created_at"],
        reverse=True,
    )
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if _is_paper_job(item.get("stage"), item.get("job_id", ""), result, item.get("logs")):
            try:
                normalized_items.append(_compact_job_for_list(item))
                continue
            except Exception:
                pass
        normalized_items.append(item)
    items = normalized_items
    items = _dedupe_persisted_paper_preview_jobs(items)
    write_json(JOBS_PATH, {"jobs": items[:300]})




def _created_at_from_find_run_id(run_id: str, fallback: str = "") -> str:
    match = re.match(r"find_(\d{8})_(\d{6})_", str(run_id or ""))
    if match:
        stamp = match.group(1) + match.group(2)
        try:
            return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    return fallback or datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _find_run_history_jobs_from_runs(existing_run_ids: set[str], *, limit: int = 300, project: str = "") -> list[dict[str, Any]]:
    project = _api_query_str(project)
    jobs: list[dict[str, Any]] = []
    try:
        runs = _cached_list_runs()
    except Exception:
        return jobs
    for row in runs:
        run_id = str((row if isinstance(row, dict) else {}).get("run_id") or "").strip()
        if not run_id.startswith("find_") or run_id in existing_run_ids:
            continue
        project_id = _project_id_for_find_run(run_id)
        if project and project_id != project:
            continue
        try:
            directory = run_dir(run_id)
        except Exception:
            continue
        progress = read_json(directory / "find_progress.json", {})
        if not isinstance(progress, dict):
            progress = {}
        manifest = read_json(directory / "manifest.json", {})
        if not isinstance(manifest, dict):
            manifest = {}
        counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
        find_results_path = directory / "find_results.json"
        find_md_path = directory / "find.md"
        phase = str(progress.get("phase") or ("complete" if find_results_path.exists() else "interrupted"))
        live = progress.get("live_progress") if isinstance(progress.get("live_progress"), dict) else {}
        live_phase = str(live.get("phase") or "")
        active_phases = {
            "running",
            "scoring",
            "fetching",
            "screening",
            "initializing",
            "venue_title_index",
            "title_prefilter",
            "detail_fetch",
            "abstract_enrichment",
            "abstract_contract",
            "abstract_scoring",
            "abstract_scoring_retry",
            "venue_scan_complete",
            "venue_llm_scoring_complete",
            "final_ranking_prepare",
            "preliminary_artifacts_written",
        }
        blocked_phases = {"blocked", "llm_blocked", "quota_blocked"}
        completed = phase == "complete" and find_results_path.exists()
        if completed:
            status = "done"
        elif phase == "interrupted":
            status = "cancelled"
        elif phase.startswith("blocked") or phase in blocked_phases:
            status = "blocked"
        elif phase in active_phases or live_phase in active_phases:
            status = "cancelled"
            phase = "interrupted"
        else:
            status = "cancelled" if not find_results_path.exists() else "blocked"
        if status == "done" and not find_md_path.exists():
            status = "blocked"
        total = _as_int(live.get("total"), 0)
        current = _as_int(live.get("current"), 0)
        percent = _as_int(live.get("percent"), 100 if status == "done" else 0)
        message = str(live.get("message") or phase or "Find run history")
        result = {
            "run_id": run_id,
            "project": project_id,
            "artifact_dir": str(directory),
            "find_results_path": str(directory / "find_results.json"),
            "artifact_paths": {
                "find_results": str(directory / "find_results.json"),
                "find_progress": str(directory / "find_progress.json"),
                "find": str(directory / "find.md"),
                "source_status": str(directory / "source_status.md"),
            },
            "phase": phase,
            "status": status,
            "summary": f"Find run {run_id}; phase={phase}; strong={progress.get('strong_recommendation_count', '')}/{progress.get('recommendation_target_count', '')}",
        }
        for key in ["raw_title_index", "title_candidates", "detail_fetched", "evaluated_candidates"]:
            if key in counts:
                result[f"{key}_count"] = counts.get(key)
        for key in ["strong_recommendation_count", "recommendation_target_count", "recommendation_shortfall"]:
            if key in progress:
                result[key] = progress.get(key)
        try:
            stat = (directory / "find_results.json").stat()
            result["find_results_size_bytes"] = stat.st_size
            result["find_results_mtime"] = stat.st_mtime
        except OSError:
            pass
        created_at = str(manifest.get("created_at") or row.get("created_at") or "")
        jobs.append({
            "job_id": f"find-run-{run_id}",
            "stage": "find",
            "project": project_id,
            "status": status,
            "created_at": _created_at_from_find_run_id(run_id, created_at),
            "logs": [f"Created run {run_id}", message],
            "log_count": 2,
            "run_id": run_id,
            "result": result,
            "internal": False,
            "display": "",
            "error": "",
            "cancel_requested": False,
            "cancelled_at": "",
            "progress": {"phase": phase, "current": current, "total": total, "percent": max(0, min(100, percent)), "message": message},
        })
        if len(jobs) >= limit:
            break
    return jobs

def _load_persisted_jobs() -> None:
    data = read_json(JOBS_PATH, {"jobs": []})
    for item in data.get("jobs", []):
        if not isinstance(item, dict):
            continue
        if _job_is_hollow_route(item):
            continue
        job = JobState.from_dict(item)
        persisted_was_live = job.status in {"queued", "running", "cancelling"}
        stage = str(job.stage or "")
        error_text = str(job.error or "")
        error_lower = error_text.lower()
        stale_restart_error = job.status == "error" and "server restarted before this job completed" in error_lower
        if stale_restart_error and not stage.startswith("full-cycle"):
            job.status = "cancelled"
            job.error = ""
            job.cancelled_at = job.cancelled_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
            job.set_progress("interrupted", 0, 1, "服务重启前的旧任务已停止；不是当前运行错误。")
            job.log("Reclassified stale restart error as interrupted/cancelled, not a live error.")
        if stage.startswith(("full-cycle", "environment", "experiment", "paper", "autonomous")) and job.status == "error" and (
            "exit code -15" in error_text
            or "cancelled" in error_lower
            or "server restarted before this job completed" in error_lower
        ):
            if "full-cycle" in stage and "exit code -15" in error_text:
                job.status = "blocked"
                job.error = ""
                job.set_progress("blocked", 0, 1, "旧 full-cycle 已被修复流程终止；当前状态以 fresh-base evidence gate 为准。")
                job.log("Reclassified terminated full-cycle as blocked by current evidence gate.")
            elif "server restarted before this job completed" in error_lower:
                job.status = "blocked" if "full-cycle" in stage else "cancelled"
                job.error = ""
                job.set_progress(job.status, 0, 1, "服务重启前的旧 workflow 任务已停止；当前监督以项目 evidence gate 为准。")
                job.log("Reclassified stale workflow restart error as superseded by current evidence gate.")
            else:
                job.status = "cancelled"
                job.error = ""
                job.cancelled_at = job.cancelled_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
                job.set_progress("cancelled", 0, 1, "旧 workflow 任务已取消/被新修复流程取代，不是当前运行错误。")
                job.log("Reclassified terminated research job as cancelled/superseded, not a live error.")
        llm_preflight_blocked = (
            stage.startswith("full-cycle")
            and any("Full-cycle blocked before launch by LLM readiness" in str(line) for line in (job.logs or []))
        )
        if llm_preflight_blocked and job.status != "blocked":
            job.status = "blocked"
            job.error = ""
            message = next((str(line).split("LLM readiness:", 1)[-1].strip() for line in (job.logs or []) if "Full-cycle blocked before launch by LLM readiness" in str(line)), "LLM readiness failed before full-cycle startup")
            job.set_progress("blocked", 1, 1, message[:240])
            job.log("Reclassified LLM readiness preflight result as blocked, not complete.")
        if job.status in {"queued", "running", "cancelling"}:
            if job.cancel_requested:
                job.status = "cancelled"
                job.error = ""
                job.cancelled_at = job.cancelled_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
                job.set_progress("cancelled", 0, 1, "服务重启前已请求取消，且没有恢复为活动进程；任务已标记为取消。")
                job.log("Marked cancelled after restart because cancellation had already been requested.")
            elif stage.startswith(("full-cycle", "environment", "experiment", "paper", "autonomous")):
                job.status = "blocked"
                job.error = ""
                job.set_progress("interrupted", 0, 1, "服务重启，旧 workflow 任务已停止；请以项目 state/full_research_cycle.json 和 evidence gate 为准。")
                job.log("Marked interrupted after server restart; displayed as blocked for TASTE gate visibility.")
            else:
                job.status = "cancelled"
                job.error = ""
                job.cancelled_at = job.cancelled_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
                job.set_progress("interrupted", 0, 1, "服务重启前的旧任务已停止；不是当前运行错误。")
                job.log("Marked interrupted/cancelled after server restart.")
        if job.status in {"done", "blocked"}:
            job.result = _fresh_find_result_for_job(job)
        if persisted_was_live and job.status not in {"queued", "running", "cancelling"}:
            job.mark_finished(job.cancelled_at)
        with JOBS_LOCK:
            JOBS[job.job_id] = job
    _persist_jobs()


def _auto_email_after_success(stage: str, result: Any) -> None:
    if stage == "email" or not isinstance(result, dict):
        return
    run_id = result.get("run_id")
    if not run_id:
        return
    config = load_config()
    email_config = config.email
    if not email_config.auto_send_enabled or stage not in set(email_config.auto_send_stages):
        return
    if not email_config.smtp_server or not email_config.sender or not email_config.smtp_password or not email_config.receivers:
        return
    request = EmailJobRequest(run_id=run_id, subject=f"TASTE {stage} complete: {run_id}")
    start_job("email", lambda log, should_cancel, _progress: send_run_email(request, config, log, should_cancel))


def start_job(stage: str, fn: Callable[[Callable[[str], None], Callable[[], bool], Callable[[str, int, int, str], None]], Any], job_id: str | None = None, initial_result: Any = None) -> JobState:
    job_id = job_id or f"{stage}_{uuid4().hex[:10]}"
    job = JobState(job_id, stage)
    if initial_result is not None:
        job.result = initial_result
    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist_jobs()
    account = _CURRENT_ACCOUNT.get()

    def runner() -> None:
        account_token = _CURRENT_ACCOUNT.set(account) if account is not None else None
        job.status = "running"
        _persist_jobs()
        job.log(_job_status_message(stage, "started"))
        job.set_progress("started", 0, 1, _job_status_message(stage, "started"))
        try:
            job.result = fn(job.log, job.should_cancel, job.set_progress)
            result_status = str(job.result.get("status") or "").lower() if isinstance(job.result, dict) else ""
            if job.cancel_requested:
                job.status = "cancelled"
            elif result_status.startswith("blocked"):
                job.status = "blocked"
            elif result_status == "running":
                job.status = "running"
            elif result_status in {"done", "complete", "completed", "success", "ok"}:
                job.status = "done"
            else:
                result_summary = job.result.get("summary", {}) if isinstance(job.result, dict) else {}
                if isinstance(result_summary, dict):
                    full_cycle = result_summary.get("full_research_cycle", {})
                    if isinstance(full_cycle, dict) and str(full_cycle.get("status") or "").startswith("blocked"):
                        job.status = "blocked"
                    else:
                        job.status = "done"
                else:
                    job.status = "done"
            if job.status not in {"queued", "running", "cancelling"}:
                job.mark_finished(job.cancelled_at)
            _persist_jobs()
            if job.status == "cancelled":
                job.cancelled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                job.set_progress("cancelled", 0, 1, _job_status_message(stage, "cancelled"))
            elif job.status == "blocked":
                blocked_message = ""
                if isinstance(job.result, dict):
                    blocker = job.result.get("blocker") if isinstance(job.result.get("blocker"), dict) else {}
                    blocked_message = _human_progress_message(blocker.get("summary") or job.result.get("summary") or blocker, fallback=_job_status_message(stage, "blocked"))
                job.set_progress("blocked", 1, 1, blocked_message or _job_status_message(stage, "blocked"))
            elif job.status == "running":
                current = job.progress if isinstance(job.progress, dict) else {}
                job.set_progress(str(current.get("phase") or "running"), int(current.get("current") or 0), int(current.get("total") or 0), str(current.get("message") or _job_status_message(stage, "running")))
            else:
                job.set_progress("complete", 1, 1, _job_status_message(stage, "complete"))
            final_status = "cancelled" if job.status == "cancelled" else "blocked" if job.status == "blocked" else "running" if job.status == "running" else "complete"
            final_message = _job_status_message(stage, final_status)
            if final_status == "blocked" and isinstance(job.result, dict):
                blocker = job.result.get("blocker") if isinstance(job.result.get("blocker"), dict) else {}
                final_message = _human_progress_message(job.result.get("summary") or blocker or job.result, fallback=final_message)
            job.log(final_message)
            if job.status == "done":
                _auto_email_after_success(stage, job.result)
        except JobCancelled as exc:
            job.status = "cancelled"
            job.error = str(exc)
            job.cancelled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            job.mark_finished(job.cancelled_at)
            _persist_jobs()
            job.set_progress("cancelled", 0, 1, str(exc))
            job.log(str(exc))
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.mark_finished()
            job.set_progress("error", 0, 1, str(exc))
            job.log(str(exc))
            job.log(traceback.format_exc())
        finally:
            if job.status not in {"queued", "running", "cancelling"}:
                job.mark_finished(job.cancelled_at)
            job.done.set()
            _persist_jobs()
            if account_token is not None:
                _CURRENT_ACCOUNT.reset(account_token)

    threading.Thread(target=runner, daemon=True).start()
    return job


_load_persisted_jobs()


def _auth_response(user: AuthUser) -> dict[str, str]:
    return {"id": user.id, "username": user.username}


@app.post("/api/auth/register")
def api_auth_register(payload: dict[str, Any], request: Request):
    try:
        user = AUTH_STORE.register(payload.get("username"), payload.get("password"))
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    token = AUTH_STORE.create_session(user)
    response = JSONResponse({"user": _auth_response(user)})
    _set_session_cookie(response, request, token)
    return response


@app.post("/api/auth/login")
def api_auth_login(payload: dict[str, Any], request: Request):
    user = AUTH_STORE.authenticate(payload.get("username"), payload.get("password"))
    if user is None:
        return JSONResponse({"error": "用户名或密码错误。"}, status_code=401)
    token = AUTH_STORE.create_session(user)
    response = JSONResponse({"user": _auth_response(user)})
    _set_session_cookie(response, request, token)
    return response


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    AUTH_STORE.delete_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=_request_uses_https(request),
        samesite="lax",
    )
    return response


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    user = AUTH_STORE.user_for_session(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    return {"user": _auth_response(user)}


@app.get("/api/config")
def api_get_config() -> dict:
    return _public_config_response(load_config())


@app.post("/api/config")
def api_save_config(config: AppConfig) -> dict:
    merged = _request_config_with_persisted_secrets(config)
    return _public_config_response(save_config(merged))


@app.post("/api/config/llm-probe")
def api_llm_probe() -> dict:
    """Validate the saved Find LLM config through the framework LLM client."""
    cfg = load_config()
    llm = LLMClient(cfg, "find")
    summary = llm.summary()
    try:
        if not llm.enabled:
            result = {"ok": False, "error": "LLM is not configured", "probe": "framework_json_live_probe"}
        else:
            raw = llm.chat(
                'Return exactly this JSON object with no markdown: {"ok": true, "score": 1, "reason": "ready"}',
                temperature=0,
                max_tokens=120,
            )
            parsed = extract_json(raw)
            result = {"ok": bool(isinstance(parsed, dict) and parsed.get("ok")), "probe": "framework_json_live_probe"}
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "probe": "framework_json_live_probe"}
    return {
        "ok": bool(isinstance(result, dict) and result.get("ok")),
        "error": str(result.get("error") or result.get("reason") or "")[:800] if isinstance(result, dict) else "LLM probe failed",
        "probe": str(result.get("probe") or "framework_json_live_probe") if isinstance(result, dict) else "framework_json_live_probe",
        "summary": {key: summary.get(key) for key in ["role", "provider", "base_url", "model", "temperature", "enabled", "api_mode"]},
    }


@app.get("/api/config/meta")
def api_config_meta() -> dict:
    return {"saved": _active_finding_config_path(create_project_copy=False).exists(), "llm_config_saved": _local_llm_config_path().exists()}


@app.get("/api/frontend/version")
def api_frontend_version() -> dict:
    return _frontend_version()


@app.get("/api/catalog/venues")
def api_catalog() -> list[dict]:
    return sorted(
        load_catalog(),
        key=lambda item: (item["source"], item["field"], item["type"], item["rank"], item["name"], item["id"]),
    )


def _venue_health_source_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog = catalog_by_id()
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    rows: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        venue_id = str(result.get("venue_id") or "").strip()
        venue = catalog.get(venue_id, {}) if venue_id else {}
        venue_name = str(result.get("venue") or venue.get("name") or venue_id or "venue").strip()
        try:
            year = int(result.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        try:
            sample_count = int(result.get("sample_count") or 0)
        except (TypeError, ValueError):
            sample_count = 0
        adapter = str(result.get("source_adapter") or result.get("adapter") or "unknown").strip()
        ok = bool(result.get("ok"))
        rows.append({
            "source": f"{venue_name} {year}".strip() if year else venue_name,
            "source_kind": "venue_health",
            "venue_id": venue_id,
            "venue": venue_name,
            "year": year,
            "status": "ok" if ok else "failed",
            "ok": ok,
            "limited": False,
            "count": sample_count,
            "sample_count": sample_count,
            "health_sample_count": sample_count,
            "adapter": adapter,
            "source_adapter": adapter,
            "message": str(result.get("message") or ("ok" if ok else "No papers fetched.")),
            "requested_years": [year] if year else [],
            "effective_years": [year] if year and ok else [],
            "checked_at": checked_at,
        })
    return rows


def _record_project_venue_health(project: str, results: list[dict[str, Any]]) -> None:
    project_id = str(project or "").strip()
    if not project_id:
        return
    try:
        root = _safe_project_root(project_id)
    except Exception:
        return
    rows = _venue_health_source_rows(results)
    if not rows:
        return
    payload = {
        "project": project_id,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_status": rows,
        "results": results,
    }
    write_json(root / "state" / "venue_health_status.json", payload)
    _cleruntime_caches(project_id)


def _venue_health_failure(venue_id: str, year: int, message: str, adapter: str = "timeout") -> dict:
    return {
        "venue_id": venue_id,
        "year": year,
        "ok": False,
        "sample_count": 0,
        "source_adapter": adapter,
        "message": message,
        "samples": [],
    }


def _venue_health_result_for_request(result: Any, venue_id: str, year: int) -> dict:
    payload = dict(result) if isinstance(result, dict) else {}
    payload["venue_id"] = str(payload.get("venue_id") or venue_id)
    try:
        payload["year"] = int(payload.get("year") or year)
    except (TypeError, ValueError):
        payload["year"] = int(year)
    payload["ok"] = bool(payload.get("ok"))
    try:
        payload["sample_count"] = int(payload.get("sample_count") or 0)
    except (TypeError, ValueError):
        payload["sample_count"] = 0
    payload["source_adapter"] = str(payload.get("source_adapter") or payload.get("adapter") or "unknown")
    payload["message"] = str(payload.get("message") or ("ok" if payload["ok"] else "No papers fetched."))
    if not isinstance(payload.get("samples"), list):
        payload["samples"] = []
    return payload


def _fetch_venue_sample_with_timeout(venue: dict, venue_id: str, year: int, sample_limit: int) -> dict:
    try:
        return _venue_health_result_for_request(fetch_venue_sample(venue, year, sample_limit), venue_id, year)
    except Exception as exc:
        return _venue_health_failure(venue_id, year, str(exc) or "Venue health check failed.", adapter="error")


@app.post("/api/catalog/venue-health")
def api_venue_health(request: VenueHealthRequest) -> dict:
    if request.project:
        try:
            request = request.model_copy(update={"project": _account_project_id(request.project, require_exists=True)})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
    catalog = catalog_by_id()

    def normalize_pairs() -> list[tuple[str, int]]:
        pairs: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for item in request.venue_years or []:
            if not isinstance(item, dict):
                continue
            venue_id = str(item.get("venue_id") or item.get("venue") or item.get("id") or "").strip()
            raw_years = item.get("years") if isinstance(item.get("years"), list) else [item.get("year")]
            if not venue_id:
                continue
            for raw_year in raw_years:
                try:
                    year = int(raw_year)
                except (TypeError, ValueError):
                    continue
                key = (venue_id, year)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
        if pairs:
            return pairs
        venue_ids = request.venue_ids or list(catalog.keys())
        years = request.years or [datetime.now(UTC).year]
        for venue_id in venue_ids:
            for raw_year in years:
                try:
                    year = int(raw_year)
                except (TypeError, ValueError):
                    continue
                key = (str(venue_id), year)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
        return pairs

    results = []
    sample_limit = max(1, request.sample_limit)
    project = str(getattr(request, "project", "") or "")
    with crawl_resource_lease(operation="venue_health", project=project):
        for venue_id, year in normalize_pairs():
            venue = catalog.get(venue_id)
            if not venue:
                results.append(_venue_health_failure(venue_id, year, "Unknown venue id.", adapter="unknown"))
                continue
            results.append(_fetch_venue_sample_with_timeout(venue, venue_id, year, sample_limit))
    _record_project_venue_health(getattr(request, "project", ""), results)
    return {"results": results}



@app.post("/api/jobs/find")
def api_find(request: FindRequest) -> dict:
    try:
        if request.project:
            request = request.model_copy(update={"project": _account_project_id(request.project, require_exists=True)})
        current = _project_for_find_request(request)
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    if not current:
        return JSONResponse(status_code=400, content={"error": "No current project is selected for Find."})
    active_blocker = _active_web_stage_job_blocker(current[0] if current else "", "find")
    if active_blocker:
        return JSONResponse(status_code=409, content=active_blocker)
    blocker = _new_find_guard_blocker(request)
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    explicit_reason = _new_find_request_reason(request)
    if current and explicit_reason:
        project, root = current
        _record_new_find_restart_approval(root, project, source="api_jobs_find", reason=explicit_reason)
    # Web owns the user input surface. Before starting framework orchestration,
    # persist the non-secret Find request into project state so framework can
    # produce explicit config/selection JSON for the Finding public CLI.
    config = _request_config_with_persisted_secrets(request.config)
    llm_error = _find_llm_configuration_error(config)
    if llm_error:
        return JSONResponse(status_code=400, content=llm_error)
    if not (str(config.research_interest or "").strip() or str(config.researcher_profile or "").strip()):
        return JSONResponse(
            status_code=400,
            content={
                "code": "research_profile_required",
                "message": "Research interest/profile is required before starting Find; otherwise final title+abstract LLM scoring is skipped.",
            },
        )
    selection = normalize_source_selection(request.selection.model_dump() if request.selection else {})
    project_path = current[1] / "project.json" if current else None
    save_canonical_source_selection(selection, project_config_path=project_path)
    config = config.model_copy(update={"default_find_selection": selection})
    _persist_local_llm_config_from_find_request(config, request.config)
    _sync_project_research_preferences_from_config(config, project_path)
    _sync_project_finding_config_from_request(config, project_path)

    def runtime_env_for_find() -> dict[str, str]:
        # Always pass the account-scoped path. project_bridge treats this as a
        # trust boundary and removes service-wide OpenAI/LLM environment keys.
        return {"FINDING_LLM_CONFIG": str(_local_llm_config_path())}

    project_context = current or _current_project_for_find_guard()
    if not project_context:
        return JSONResponse(status_code=400, content={"error": "No current project is selected for Find."})
    project_id, _root = project_context
    job_id = f"find_{uuid4().hex[:10]}"

    def run_find_and_adopt(log, should_cancel, progress):
        payload = {
            "action": "find",
            "project": project_id,
            "web_job_id": job_id,
            "max_papers": int(config.max_recommended_papers or 20),
            "max_ideas": int(config.max_ideas or 6),
            "queries": [str(query).strip() for query in (config.arxiv_queries or []) if str(query).strip()],
            "selection": selection,
            "runtime_env": runtime_env_for_find(),
        }
        result = run_action(payload, log, should_cancel, progress)
        if isinstance(result, dict):
            result.setdefault("project", project_id)
            result.setdefault("action", "find")
            result.setdefault("web_job_id", job_id)
        return result

    with JOBS_LOCK:
        active_blocker = _active_web_stage_job_blocker(project_id, "find")
        if active_blocker:
            return JSONResponse(status_code=409, content=active_blocker)
        job = start_job(
            "find",
            run_find_and_adopt,
            job_id=job_id,
            initial_result={"project": project_id, "action": "find", "web_job_id": job_id},
        )
    return _public_account_payload(job.as_dict())


def _current_project_find_run_id(root: Path) -> str:
    for rel in [
        ("planning", "finding", "find_progress.json"),
        ("state", "current_find_recommendation_projection.json"),
        ("state", "current_find_research_plan.json"),
        ("state", "finding_frontend.json"),
        ("planning", "finding", "read_results.json"),
        ("planning", "finding", "ideas.json"),
        ("planning", "finding", "plans.json"),
        ("planning", "finding", "find_results.json"),
    ]:
        payload = _read_project_json(root.joinpath(*rel), {})
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or payload.get("current_find_run_id") or "").strip()
        if run_id:
            return run_id
    return ""


def _request_targets_current_project_find(request: ReadRequest, project: str, root: Path) -> bool:
    current_run_id = _current_project_find_run_id(root)
    requested_run_id = str(request.run_id or "").strip()
    return bool(current_run_id and (not requested_run_id or requested_run_id == current_run_id))


def _current_find_read_is_incomplete(root: Path, run_id: str, idea_count: int = 1, *, require_idea_plan: bool = True) -> bool:
    run_id = str(run_id or "").strip()
    if not run_id:
        return False
    taste_dir = root / "planning" / "finding"
    read_payload = _read_project_json(taste_dir / "read_results.json", {})
    idea_payload = _read_project_json(taste_dir / "ideas.json", {})
    plan_payload = _read_project_json(taste_dir / "plans.json", {})
    state_plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
    validation = _read_project_json(root / "state" / "current_find_claude_reading_validation.json", {})

    def payload_run_id(payload: Any) -> str:
        return str((payload if isinstance(payload, dict) else {}).get("run_id") or (payload if isinstance(payload, dict) else {}).get("source_run_id") or (payload if isinstance(payload, dict) else {}).get("find_run_id") or "").strip()

    def same_run(payload: Any) -> bool:
        return payload_run_id(payload) == run_id

    if same_run(state_plan):
        status = str((state_plan if isinstance(state_plan, dict) else {}).get("status") or "").strip().lower()
        next_action = str((state_plan if isinstance(state_plan, dict) else {}).get("next_required_action") or "").strip().lower()
        if status == "pending_current_find_read" or next_action == "run_read_for_current_find":
            return True
    if same_run(validation) and isinstance(validation, dict) and validation.get("valid") is not True:
        return True
    if not same_run(read_payload):
        return True
    if str((read_payload if isinstance(read_payload, dict) else {}).get("source") or "").strip() not in {
        "claude_code_current_find_takeover",
        "framework_current_find_read_adapter",
    }:
        return True
    readings = (read_payload if isinstance(read_payload, dict) else {}).get("readings")
    if not isinstance(readings, list) or not readings:
        return True
    read_md = taste_dir / "read.md"
    try:
        read_md_text = read_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        read_md_text = ""
    if not read_md_text.strip().startswith("# 论文精读"):
        return True
    if not require_idea_plan:
        return False
    try:
        required_ideas = max(1, int(idea_count or 1))
    except (TypeError, ValueError):
        required_ideas = 1
    ideas = (idea_payload if isinstance(idea_payload, dict) else {}).get("ideas")
    plans = (plan_payload if isinstance(plan_payload, dict) else {}).get("plans")
    if not same_run(idea_payload) or not isinstance(ideas, list) or len(ideas) < required_ideas:
        return True
    if not same_run(plan_payload) or not isinstance(plans, list) or len(plans) < required_ideas:
        return True
    return False


def _read_request_should_use_current_find_wrapper(request: ReadRequest, project: str, root: Path) -> bool:
    current_run_id = _current_project_find_run_id(root)
    if not current_run_id:
        return False
    if _request_targets_current_project_find(request, project, root):
        return True
    return _current_find_read_is_incomplete(root, current_run_id, require_idea_plan=False)


def _current_find_downstream_gate_blocker(stage: str, run_id: str = "") -> dict[str, Any] | None:
    current = _project_context_for_find_run(run_id) if run_id else None
    if not current:
        current = _current_project_for_find_guard()
    if not current:
        return None
    project, root = current
    run_id = _current_project_find_run_id(root)
    if not run_id or not _current_find_read_is_incomplete(root, run_id, require_idea_plan=False):
        return None
    validation = _read_project_json(root / "state" / "current_find_claude_reading_validation.json", {})
    state_plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
    try:
        pending = int((validation if isinstance(validation, dict) else {}).get("pending_full_text_reading_count") or 0)
    except (TypeError, ValueError):
        pending = 0
    status = str((validation if isinstance(validation, dict) else {}).get("status") or (state_plan if isinstance(state_plan, dict) else {}).get("status") or "blocked_current_find_read_incomplete").strip()
    next_action = str((state_plan if isinstance(state_plan, dict) else {}).get("next_required_action") or "run_read_for_current_find").strip()
    return {
        "code": "current_find_read_gate_blocked",
        "stage": stage,
        "project": project,
        "run_id": run_id,
        "status": status,
        "pending_full_text_reading_count": pending,
        "next_required_action": next_action,
        "message": "当前 Find 的全文精读/阅读验证尚未通过；不能生成想法或计划。请先运行或修复精读，补齐同一论文的 PDF/HTML/页面全文证据。",
    }


def _current_find_downstream_blocked_job(stage: str, blocker: dict[str, Any]) -> JobState:
    message = str(blocker.get("message") or "当前 Find 的精读验证尚未通过；下游阶段已暂停。").strip()
    job = JobState(f"{stage}_{uuid4().hex[:10]}", stage)
    job.status = "blocked"
    job.run_id = str(blocker.get("run_id") or "")
    job.result = {
        "status": "blocked_current_find_read_gate",
        "stage": stage,
        "project": blocker.get("project"),
        "run_id": job.run_id,
        "source": "current_find_read_gate",
        "blocker": blocker,
        "summary": message,
    }
    with JOBS_LOCK:
        JOBS[job.job_id] = job
    job.set_progress("blocked", 1, 1, message)
    job.log(f"{stage} blocked: {message}")
    job.mark_finished()
    job.done.set()
    _persist_jobs()
    return job


def _current_find_read_validation_requires_repair(root: Path, run_id: str) -> bool:
    validation = _read_project_json(root / "state" / "current_find_claude_reading_validation.json", {})
    if not isinstance(validation, dict):
        return False
    validation_run_id = str(validation.get("run_id") or "").strip()
    if validation_run_id and validation_run_id != run_id:
        return False
    try:
        pending_count = int(validation.get("pending_full_text_reading_count") or 0)
    except (TypeError, ValueError):
        pending_count = 0
    pending_titles = validation.get("pending_full_text_reading_titles")
    try:
        pending_deep_count = int(validation.get("pending_deep_read_synthesis_count") or 0)
    except (TypeError, ValueError):
        pending_deep_count = 0
    pending_deep_titles = validation.get("pending_deep_read_synthesis_titles")
    return bool(
        pending_count > 0
        or (isinstance(pending_titles, list) and pending_titles)
        or pending_deep_count > 0
        or (isinstance(pending_deep_titles, list) and pending_deep_titles)
        or validation.get("warning_details")
        or validation.get("error_details")
        or validation.get("deep_read_content_gap_details")
        or validation.get("blockers")
        or (validation.get("scoring_required") is True and validation.get("scoring_complete") is not True)
        or (validation.get("scoring_required") is True and validation.get("read_quality_complete") is not True)
    )


def _current_find_wrapper_failure_summary(output_lines: list[str]) -> tuple[str, str]:
    tail = [line.strip() for line in output_lines[-20:] if str(line).strip()]
    for line in reversed(tail):
        lowered = line.lower()
        if "traceback" in lowered or line.startswith(("{", "}", "[", "]")) or '":' in line[:160]:
            continue
        if line:
            return ("blocked_current_find_claude_read_failed", line[:800])
    return ("blocked_current_find_claude_read_failed", "")


def _run_current_find_claude_read_job(project: str, root: Path, request: ReadRequest, log, should_cancel, progress) -> dict:
    run_id = _current_project_find_run_id(root)
    if not run_id:
        raise RuntimeError("current project Find run is missing; run Find before Read")
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")
    progress("full_text", 0, 0, "等待爬文章开始。")
    management_python = os.environ.get("MANAGEMENT_PYTHON") or sys.executable
    try:
        configured_idea_count = int(getattr(load_config(), "max_ideas", 0) or 0)
    except Exception:
        configured_idea_count = 0
    idea_count = max(1, configured_idea_count or AppConfig().max_ideas)
    read_limit = configured_max_read_papers(project, explicit=getattr(request, "max_papers", None))
    force_requested = bool(getattr(request, "force", False))
    repair_mode = _current_find_read_validation_requires_repair(root, run_id) or _current_find_read_is_incomplete(root, run_id, idea_count=idea_count, require_idea_plan=False)
    force_deep_read = force_requested
    plan_state = _read_project_json(root / "state" / "current_find_research_plan.json", {})
    find_results = _read_project_json(root / "planning" / "finding" / "find_results.json", {})
    ranked_rows: list[Any] = []
    if isinstance(find_results, dict):
        for key in ("screened_ranking", "final_ranking", "ranked_papers", "evaluated_candidates", "strong_recommendations"):
            candidate_rows = find_results.get(key)
            if isinstance(candidate_rows, list) and candidate_rows:
                ranked_rows = candidate_rows
                break
    persisted_selected_count = _as_int(
        (plan_state if isinstance(plan_state, dict) else {}).get("selected_reading_count")
        or (plan_state if isinstance(plan_state, dict) else {}).get("expected_reading_count"),
        0,
    )
    prepared_count = persisted_selected_count or (min(read_limit, len(ranked_rows)) if ranked_rows else read_limit)
    if prepared_count > 0:
        log(f"Full-text acquisition phase: {prepared_count} papers")
        progress("full_text", 0, prepared_count, f"爬文章：当前 Find 共 {prepared_count} 篇。")
    cmd = [
        management_python,
        str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "reading",
        "--action",
        "current_find_research_plan",
        "--project",
        project,
        "--read-limit",
        str(read_limit),
        "--idea-count",
        str(idea_count),
    ]
    if force_deep_read:
        cmd.append("--force")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    env["PROJECT_ID"] = project
    env["DEFAULT_PROJECT_ID"] = project
    env["READING_REUSE_EXISTING_FULL_TEXT_PACKET"] = "1"
    if not force_requested:
        env["READING_REUSE_EXISTING_DEEP_READ_RESULTS"] = "1"
    env.setdefault("MANAGEMENT_PYTHON", management_python)
    env["PYTHONPATH"] = taste_pythonpath_string(WORKSPACE_ROOT, env.get("PYTHONPATH", ""))
    log(("Delegating current Find Read rerun to Framework wrapper: " if repair_mode else "Delegating current Find Read to Framework wrapper: ") + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=str(WORKSPACE_ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    output_lines: list[str] = []
    suppressed_structured_lines = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                output_lines.append(line)
                stripped = line.strip()
                looks_structured = stripped.startswith(("{", "}", "[", "]", "\"")) or '":' in stripped[:120]
                if looks_structured:
                    suppressed_structured_lines += 1
                    if suppressed_structured_lines == 1:
                        log("Wrapper emitted structured evidence JSON; suppressing verbose taskbar fragments. Full evidence is stored under Reading runtime and synchronized into project artifacts by Framework.")
                    elif suppressed_structured_lines % 250 == 0:
                        log(f"Wrapper structured evidence output suppressed: {suppressed_structured_lines} JSON-like lines read.")
                else:
                    log(line[:1200])
            if should_cancel():
                _terminate_process_tree(proc.pid)
                raise JobCancelled("Task cancelled by user.")
        rc = proc.wait(timeout=5)
        if should_cancel():
            raise JobCancelled("Task cancelled by user.")
    except JobCancelled:
        raise
    except Exception:
        _terminate_process_tree(proc.pid)
        raise
    result_payload: dict[str, Any] = {}
    joined = "\n".join(output_lines)
    for start in [idx for idx, char in enumerate(joined) if char == "{"][-8:]:
        try:
            candidate = json.loads(joined[start:])
        except Exception:
            continue
        if isinstance(candidate, dict):
            result_payload = candidate
            break
    read_payload = _read_project_json(root / "planning" / "finding" / "read_results.json", {})
    idea_payload = _read_project_json(root / "planning" / "finding" / "ideas.json", {})
    plan_payload = _read_project_json(root / "planning" / "finding" / "plans.json", {})
    current_plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
    validation = _read_project_json(root / "state" / "current_find_claude_reading_validation.json", {})
    status = str((result_payload if isinstance(result_payload, dict) else {}).get("status") or (current_plan if isinstance(current_plan, dict) else {}).get("status") or "").strip()
    if rc != 0 and not status.startswith("blocked"):
        status, failure_summary = _current_find_wrapper_failure_summary(output_lines)
    elif not status:
        status = "current_find_claude_read_complete"
    else:
        failure_summary = ""
    summary = str((current_plan if isinstance(current_plan, dict) else {}).get("summary_zh") or (current_plan if isinstance(current_plan, dict) else {}).get("summary") or "").strip()
    if not summary and rc != 0:
        summary = failure_summary
    if not summary and isinstance(validation, dict):
        try:
            pending_deep = int(validation.get("pending_deep_read_synthesis_count") or 0)
        except (TypeError, ValueError):
            pending_deep = 0
        try:
            pending_full_text = int(validation.get("pending_full_text_reading_count") or 0)
        except (TypeError, ValueError):
            pending_full_text = 0
        titles = [str(item).strip() for item in (validation.get("pending_deep_read_synthesis_titles") or []) if str(item).strip()]
        if (("deep_read" in status and "pending" in status) or pending_deep):
            title_hint = "；待精读：" + "、".join(titles[:3]) if titles else ""
            summary = f"当前 Find 全文证据已覆盖，但仍有 {pending_deep or len(titles)} 篇论文未完成精读{title_hint}。"
        elif "full_text_evidence" in status or pending_full_text:
            summary = f"当前 Find 仍有 {pending_full_text} 篇缺少同篇全文证据，Read 已停在全文证据门控。"
    read_valid = bool(
        isinstance(validation, dict)
        and validation.get("valid") is True
        and (
            validation.get("scoring_required") is not True
            or (
                validation.get("scoring_complete") is True
                and validation.get("read_quality_complete") is True
            )
        )
    )
    if read_valid and status.startswith("blocked_current_find_claude_read_failed"):
        status = str(validation.get("status") or "current_find_deep_read_complete")
    progress("complete" if read_valid or (rc == 0 and not status.startswith("blocked")) else "blocked", 1, 1, summary or status)
    return {
        "status": status,
        "project": project,
        "run_id": run_id,
        "source": "current_find_claude_read_wrapper",
        "repair_mode": repair_mode,
        "force_deep_read_requested": force_requested,
        "idea_count": idea_count,
        "max_read_papers": read_limit,
        "return_code": rc,
        "failure_type": (current_plan if isinstance(current_plan, dict) else {}).get("failure_type"),
        "pending_deep_read_synthesis_count": (validation if isinstance(validation, dict) else {}).get("pending_deep_read_synthesis_count"),
        "pending_full_text_reading_count": (validation if isinstance(validation, dict) else {}).get("pending_full_text_reading_count"),
        "scoring_status": (validation if isinstance(validation, dict) else {}).get("scoring_status"),
        "scoring_complete": (validation if isinstance(validation, dict) else {}).get("scoring_complete"),
        "read_quality_complete": (validation if isinstance(validation, dict) else {}).get("read_quality_complete"),
        "readings": len((read_payload if isinstance(read_payload, dict) else {}).get("readings") or []),
        "ideas": len((idea_payload if isinstance(idea_payload, dict) else {}).get("ideas") or []),
        "plans": len((plan_payload if isinstance(plan_payload, dict) else {}).get("plans") or []),
        "current_find_research_plan": str(root / "state" / "current_find_research_plan.json"),
        "read_md": str(root / "planning" / "finding" / "read.md"),
        "public_final_artifact": str(root / "planning" / "finding" / "read.md"),
        "read_results": str(root / "planning" / "finding" / "read_results.json"),
        "prepared_reading_input": {
            "source": "framework",
            "selected_reading_count": prepared_count,
            "expected_reading_count": prepared_count,
        },
        "reading_sync": result_payload.get("reading_sync") if isinstance(result_payload, dict) else {},
        "wrapper_result": result_payload,
        "stdout_tail": output_lines[-20:],
        "summary": summary,
    }


def _read_requires_current_find_job(request: ReadRequest) -> JobState:
    run_id = str(getattr(request, "run_id", "") or "").strip()
    message = "Read 必须从项目当前 Find 后续触发；请先在左侧选择项目并采用当前 Find，再点击 Read。"
    job = JobState(f"read_{uuid4().hex[:10]}", "read")
    job.status = "blocked"
    job.run_id = run_id
    job.result = {
        "status": "blocked_missing_current_project_find_context",
        "stage": "read",
        "run_id": run_id,
        "source": "web_read_project_bridge",
        "message": message,
        "next_required_action": "select_or_adopt_project_current_find_then_run_read",
        "policy": "Web sends Read commands only to framework/scripts/main.py module; Framework prepares inputs, invokes Reading, and synchronizes project artifacts.",
    }
    with JOBS_LOCK:
        JOBS[job.job_id] = job
    job.set_progress("blocked", 1, 1, message)
    job.log(message)
    job.mark_finished()
    job.done.set()
    _persist_jobs()
    return job


def _framework_module_env(config: AppConfig, project: str = "") -> dict[str, str]:
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    if project:
        env["PROJECT_ID"] = project
        env["DEFAULT_PROJECT_ID"] = project
    env.setdefault("MANAGEMENT_PYTHON", os.environ.get("MANAGEMENT_PYTHON") or sys.executable)
    env["PYTHONPATH"] = taste_pythonpath_string(WORKSPACE_ROOT, env.get("PYTHONPATH", ""))
    config_json = json.dumps(_strip_llm_secrets_from_config_payload(config.model_dump()), ensure_ascii=False)
    env["TASTE_IDEATION_CONFIG_JSON"] = config_json
    env["TASTE_APP_CONFIG_JSON"] = config_json
    return env


def _ideation_framework_env(config: AppConfig, project: str) -> dict[str, str]:
    env = _framework_module_env(config, project)
    env["TASTE_IDEATION_CONFIG_JSON"] = json.dumps({
        "research_interest": str(config.research_interest or ""),
        "researcher_profile": str(config.researcher_profile or ""),
        "max_ideas": int(config.max_ideas or 1),
    }, ensure_ascii=False)
    env.pop("TASTE_APP_CONFIG_JSON", None)
    return env


def _run_framework_process(
    cmd: list[str],
    env: dict[str, str],
    log: Callable[[str], None],
    should_cancel: Callable[[], bool],
    *,
    input_text: str = "",
) -> tuple[int, str, dict[str, Any]]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        text=True,
        stdin=subprocess.PIPE if input_text else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    output_lines: list[str] = []
    structured_lines = 0
    output_queue: queue.Queue[str | None] = queue.Queue()
    assert proc.stdout is not None

    def read_output() -> None:
        try:
            for output_line in proc.stdout:
                output_queue.put(output_line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        if input_text and proc.stdin is not None:
            proc.stdin.write(input_text)
            proc.stdin.close()
        while True:
            if should_cancel():
                _terminate_process_tree(proc.pid)
                raise JobCancelled("Task cancelled by user.")
            try:
                line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.rstrip("\n")
            if line:
                output_lines.append(line)
                stripped = line.strip()
                looks_structured = stripped.startswith(("{", "}", "[", "]", "\"")) or '":' in stripped[:120]
                if looks_structured:
                    structured_lines += 1
                    if structured_lines == 1:
                        log("Framework emitted structured JSON; full output remains in the stage runtime artifacts.")
                else:
                    log(line[:1200])
        rc = proc.wait(timeout=5)
        reader.join(timeout=1)
    except JobCancelled:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        reader.join(timeout=1)
        raise
    except Exception:
        try:
            _terminate_process_tree(proc.pid)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        reader.join(timeout=1)
        raise
    stdout_text = "\n".join(output_lines)
    return rc, stdout_text, _parse_last_json_object_from_lines(output_lines)


def _framework_user_error(stdout_text: str, fallback: str) -> str:
    for line in reversed(str(stdout_text or "").splitlines()):
        text = line.strip()
        match = re.match(r"^(?:ValueError|RuntimeError|FileNotFoundError|KeyError):\s*(.+)$", text)
        if match:
            return _redact_public_log_text(match.group(1))[:1600]
        if text.lower().startswith("framework failed to "):
            return _redact_public_log_text(text)[:1600]
    return fallback


def _run_ideation_framework_job(
    project: str,
    request: IdeaRequest,
    config: AppConfig,
    log: Callable[[str], None],
    should_cancel: Callable[[], bool],
    progress: Callable[[str, int, int, str], None],
) -> dict[str, Any]:
    if not project:
        raise RuntimeError("Ideas requires an active project so Framework can validate and prepare current-Find inputs.")
    management_python = os.environ.get("MANAGEMENT_PYTHON") or sys.executable
    cmd = [
        management_python,
        str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "ideation",
        "--action",
        "idea",
        "--project",
        project,
        "--run-id",
        request.run_id,
    ]
    if request.max_ideas:
        cmd.extend(["--max-ideas", str(request.max_ideas)])
    progress("ideation", 0, 1, "正在生成当前 Find 的 Ideas。")
    log("Delegating Ideation to Framework: " + " ".join(cmd))
    rc, stdout_text, result_payload = _run_framework_process(cmd, _ideation_framework_env(config, project), log, should_cancel)
    if rc != 0:
        raise RuntimeError(_framework_user_error(stdout_text, f"Framework Ideation failed with exit code {rc}."))
    _cleruntime_caches(project)
    _clerun_caches(request.run_id)
    progress("ideation", 1, 1, "Ideas 已生成。")
    return {
        "status": "ok",
        "project": project,
        "run_id": request.run_id,
        "source": "framework_ideation_entrypoint",
        "framework": result_payload,
        "stdout_tail": stdout_text[-4000:],
    }


def _run_planning_module_job(
    project: str,
    request: PlanRequest | PlanPolishRequest,
    config: AppConfig,
    action: str,
    log: Callable[[str], None],
    should_cancel: Callable[[], bool],
    progress: Callable[[str, int, int, str], None],
) -> dict[str, Any]:
    if not project:
        raise RuntimeError("Planning requires an active project.")
    cmd = [
        os.environ.get("MANAGEMENT_PYTHON") or sys.executable,
        str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "planning",
        "--action",
        action,
        "--project",
        project,
        "--run-id",
        request.run_id,
    ]
    if isinstance(request, PlanRequest):
        for idea_id in request.idea_ids:
            cmd.extend(["--idea-id", idea_id])
        cmd.extend(["--repair-rounds", str(request.repair_rounds)])
    else:
        cmd.extend(["--plan-id", request.plan_id, "--rounds", str(request.rounds)])
        if request.version_id:
            cmd.extend(["--version-id", request.version_id])
    progress("planning", 0, 1, "正在通过 Framework 运行 Planning。")
    log("Delegating Planning to Framework: " + " ".join(cmd))
    rc, stdout_text, payload = _run_framework_process(cmd, _framework_module_env(config, project), log, should_cancel)
    if rc != 0:
        raise RuntimeError(_framework_user_error(stdout_text, f"Framework Planning failed with exit code {rc}."))
    _cleruntime_caches(project)
    _clerun_caches(request.run_id)
    progress("planning", 1, 1, "Planning 已完成。")
    return {"status": "ok", "project": project, "run_id": request.run_id, "source": "framework_planning_entrypoint", "framework": payload}


@app.post("/api/jobs/read")
def api_read(request: ReadRequest) -> dict:
    if not _account_owns_run(request.run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    blocker = _taste_stage_live_full_cycle_blocker("read")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    current = _project_context_for_find_run(request.run_id) or _current_project_for_find_guard()
    if current:
        project, root = current
        if _read_request_should_use_current_find_wrapper(request, project, root):
            current_run_id = _current_project_find_run_id(root)
            with JOBS_LOCK:
                duplicate = _active_web_stage_job_blocker(project, "read")
                if duplicate:
                    return JSONResponse(status_code=409, content=duplicate)
                job = start_job("read", lambda log, should_cancel, progress: _run_current_find_claude_read_job(project, root, request, log, should_cancel, progress))
                job.run_id = current_run_id
                job.result = {
                    "project": project,
                    "run_id": current_run_id,
                    "source": "current_find_claude_read_wrapper",
                    "status": "running",
                }
                _persist_jobs()
            return _public_account_payload(job.as_dict())
    return _public_account_payload(_read_requires_current_find_job(request).as_dict())


@app.post("/api/jobs/idea")
def api_idea(request: IdeaRequest) -> dict:
    try:
        project = _account_project_id(request.project, require_exists=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if not _account_owns_run(request.run_id) or not _run_belongs_to_project(request.run_id, project):
        return JSONResponse({"error": "run not found"}, status_code=404)
    request = request.model_copy(update={"project": project})
    blocker = _taste_stage_live_full_cycle_blocker("idea")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    config = load_config()
    project = request.project
    with JOBS_LOCK:
        duplicate = _active_web_stage_job_blocker(project, "idea")
        if duplicate:
            return JSONResponse(status_code=409, content=duplicate)
        job = start_job(
            "idea",
            lambda log, should_cancel, progress: _run_ideation_framework_job(project, request, config, log, should_cancel, progress),
            initial_result={"project": project, "run_id": request.run_id, "status": "running", "source": "framework_ideation_entrypoint"},
        )
        job.run_id = request.run_id
        _persist_jobs()
        return _public_account_payload(job.as_dict())


@app.post("/api/jobs/plan")
def api_plan(request: PlanRequest) -> dict:
    if not _account_owns_run(request.run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    blocker = _taste_stage_live_full_cycle_blocker("plan")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    config = load_config()
    current = _project_context_for_find_run(request.run_id) or _current_project_for_find_guard()
    project = current[0] if current else ""
    with JOBS_LOCK:
        duplicate = _active_web_stage_job_blocker(project, "plan")
        if duplicate:
            return JSONResponse(status_code=409, content=duplicate)
        job = start_job(
            "plan",
            lambda log, should_cancel, progress: _run_planning_module_job(project, request, config, "plan", log, should_cancel, progress),
            initial_result={"project": project, "run_id": request.run_id, "status": "running", "source": "framework_planning_entrypoint"},
        )
    job.run_id = request.run_id
    _persist_jobs()
    return _public_account_payload(job.as_dict())


@app.post("/api/jobs/plan-polish")
def api_plan_polish(request: PlanPolishRequest) -> dict:
    if not _account_owns_run(request.run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    blocker = _taste_stage_live_full_cycle_blocker("plan-polish")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    config = load_config()
    current = _project_context_for_find_run(request.run_id) or _current_project_for_find_guard()
    project = current[0] if current else ""
    with JOBS_LOCK:
        duplicate = _active_web_stage_job_blocker(project, "plan")
        if duplicate:
            return JSONResponse(status_code=409, content=duplicate)
        job = start_job(
            "plan-polish",
            lambda log, should_cancel, progress: _run_planning_module_job(project, request, config, "polish", log, should_cancel, progress),
            initial_result={"project": project, "run_id": request.run_id, "status": "running", "source": "framework_planning_entrypoint"},
        )
    job.run_id = request.run_id
    _persist_jobs()
    return _public_account_payload(job.as_dict())


@app.post("/api/jobs/email")
def api_email(request: EmailJobRequest) -> dict:
    if not _account_owns_run(request.run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    config = load_config()
    job = start_job("email", lambda log, should_cancel, _progress: send_run_email(request, config, log, should_cancel))
    return _public_account_payload(job.as_dict())


@app.get("/api/projects")
def api_projects() -> list[dict]:
    rows = [row for row in list_projects() if _account_owns_project(row.get("id"))]
    return _public_account_payload(_strip_public_taste_marker(rows))


@app.post("/api/projects")
def api_project_create(payload: dict[str, Any]) -> dict:
    try:
        project_payload = dict(payload)
        raw_name = project_payload.get("id") or project_payload.get("name")
        project_id = _account_project_id(raw_name)
        project_payload["id"] = project_id
        project_payload["name"] = project_id
        return _public_account_payload(create_project_config(project_payload))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/projects/{project}")
def api_project(project: str, compact: bool = Query(True)) -> dict:
    try:
        project_id = _account_project_id(project, require_exists=True)
        return _public_account_payload(_strip_public_taste_marker(project_summary(project_id, compact=compact)))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


def _safe_project_root(project: str) -> Path:
    project_id = str(project or "").strip()
    if not project_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", project_id):
        raise ValueError("Invalid project name. Use only letters, numbers, dash, underscore, and dot.")
    projects_root = PROJECT_IDS_ROOT.resolve()
    root = (projects_root / project_id).resolve()
    if root != projects_root and projects_root not in root.parents:
        raise ValueError("Project path outside projects root")
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Project not found: {project_id}")
    return root


def _account_project_ids() -> list[str]:
    if not PROJECT_IDS_ROOT.exists():
        return []
    account = _CURRENT_ACCOUNT.get()
    if account is None:
        return [path.name for path in PROJECT_IDS_ROOT.iterdir() if path.is_dir()]
    prefix = _account_prefix(account)
    return [path.name for path in PROJECT_IDS_ROOT.iterdir() if path.is_dir() and path.name.startswith(prefix)]


def _account_project_for_run(run_id: Any) -> str:
    run = str(run_id or "").strip()
    if not run:
        return ""
    project = _project_id_for_find_run(run)
    if project and _account_owns_project(project):
        return project
    for candidate in _account_project_ids():
        if _run_belongs_to_project(run, candidate):
            return candidate
    return ""


def _account_owns_run(run_id: Any) -> bool:
    if _CURRENT_ACCOUNT.get() is None:
        return True
    return bool(_account_project_for_run(run_id))


def _account_owns_job_state(job: JobState | None) -> bool:
    if job is None:
        return False
    if _CURRENT_ACCOUNT.get() is None:
        return True
    account = _current_account()
    if job.owner_id:
        return job.owner_id == account.id
    return _account_owns_job_payload(job.as_dict(compact=True))


def _account_owns_job_payload(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if _CURRENT_ACCOUNT.get() is None:
        return True
    owner_id = str(item.get("owner_id") or "").strip()
    if owner_id:
        return owner_id == _current_account().id
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    project = str(item.get("project") or result.get("project") or "").strip()
    if project:
        return _account_owns_project(project)
    run_id = str(item.get("run_id") or result.get("run_id") or result.get("find_run_id") or "").strip()
    return bool(run_id and _account_owns_run(run_id))


PROJECT_STAGE_EXCLUSIVE_ACTIONS = {"environment", "experiment", "paper", "full-cycle", "full_research_cycle", "autonomous"}
PROJECT_STAGE_EXCLUSIVE_PHASES = {"environment", "experiment", "paper"}
_FIND_PROCESS_WORKER_KINDS = {"frontend_recovery", "driver_recovery"}


def _live_find_process_worker(item: dict[str, Any]) -> bool:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    return bool(
        _public_taste_stage(item.get("stage")) == "find"
        and str(item.get("status") or "").strip().lower() in {"queued", "running", "cancelling"}
        and str(result.get("kind") or "").strip() in _FIND_PROCESS_WORKER_KINDS
        and result.get("process_alive") is True
    )


def _live_find_run_matches_project(project: str, run_id: str) -> bool:
    project_id = str(project or "").strip()
    target_run_id = str(run_id or "").strip()
    if not project_id or not target_run_id:
        return False
    try:
        live_items = _live_jobs_from_projects(compact=True, project_filter=project_id)
    except TypeError as exc:
        if "project_filter" not in str(exc):
            raise
        live_items = _live_jobs_from_projects(compact=True)
    for item in live_items:
        if not isinstance(item, dict) or not _live_find_process_worker(item):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        item_project = str(result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
        item_run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
        if item_project == project_id and item_run_id == target_run_id:
            return True
    return False


def _merge_live_find_workers_into_web_jobs(
    dynamic: list[dict[str, Any]],
    persisted: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project each live Find worker onto the Web job ID carried by its command."""
    workers_by_web_job: dict[str, list[dict[str, Any]]] = {}
    for item in dynamic:
        if not isinstance(item, dict) or not _live_find_process_worker(item):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        web_job_id = str(result.get("web_job_id") or "").strip()
        if web_job_id:
            workers_by_web_job.setdefault(web_job_id, []).append(item)
    if not workers_by_web_job:
        return dynamic, persisted

    def worker_rank(item: dict[str, Any]) -> tuple[int, str]:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        rank = 0 if str(result.get("kind") or "") == "frontend_recovery" else 1
        return rank, str(item.get("created_at") or "")

    merged_web_job_ids: set[str] = set()
    merged_persisted: list[dict[str, Any]] = []
    for item in persisted:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        project_id = str(result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
        web_job_id = str(item.get("job_id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        workers = workers_by_web_job.get(web_job_id, [])
        if (
            item.get("cancel_requested") is True
            or _public_taste_stage(item.get("stage")) != "find"
            or status not in {"queued", "running", "cancelling", "cancelled", "interrupted", "stale"}
            or not workers
        ):
            merged_persisted.append(item)
            continue

        worker = min(workers, key=worker_rank)
        worker_result = worker.get("result") if isinstance(worker.get("result"), dict) else {}
        worker_project = str(worker_result.get("project") or _project_from_job_payload(worker.get("job_id"), worker) or "").strip()
        if not project_id or worker_project != project_id:
            merged_persisted.append(item)
            continue
        run_id = str(item.get("run_id") or result.get("run_id") or "").strip()
        worker_run_id = str(worker.get("run_id") or worker_result.get("run_id") or "").strip()
        execution_keys = {
            "pid", "cmd", "command", "kind", "process_alive", "log_path",
            "artifact_dir", "find_results_path", "phase", "raw_stage", "summary",
        }
        merged_result = dict(result)
        merged_result.update({key: worker_result.get(key) for key in execution_keys if key in worker_result})
        merged_result["project"] = project_id
        merged_result.pop("find_progress", None)
        worker_progress = worker.get("progress") if isinstance(worker.get("progress"), dict) else {}
        if run_id:
            merged_result["run_id"] = run_id
            try:
                bound_progress = read_json(run_dir(run_id) / "find_progress.json", {})
            except (FileNotFoundError, OSError, ValueError):
                bound_progress = {}
            if isinstance(bound_progress, dict) and bound_progress:
                projection = _find_progress_projection(bound_progress)
                merged_result["find_progress"] = projection
                percent = _as_int(projection.get("overall_percent"), 0)
                worker_progress = {
                    "phase": "find",
                    "current": percent,
                    "total": 100,
                    "percent": percent,
                    "message": f"{projection.get('stage_label') or 'Find'}：{projection.get('message') or 'Find running'}",
                }
            elif worker_run_id == run_id and isinstance(worker_result.get("find_progress"), dict):
                merged_result["find_progress"] = worker_result["find_progress"]
            else:
                initializing = _find_progress_projection({"run_id": run_id})
                merged_result["find_progress"] = initializing
                worker_progress = {
                    "phase": "find",
                    "current": 0,
                    "total": 100,
                    "percent": 0,
                    "message": f"{initializing.get('stage_label') or 'Find'}：{initializing.get('message') or 'Find initializing'}",
                }
        else:
            merged_result.pop("run_id", None)
            initializing = _find_progress_projection({})
            merged_result["find_progress"] = initializing
            worker_progress = {
                "phase": "find",
                "current": 0,
                "total": 100,
                "percent": 0,
                "message": f"{initializing.get('stage_label') or 'Find'}：{initializing.get('message') or 'Find initializing'}",
            }

        logs = _dedupe_recent_lines(
            [
                *[str(line) for line in item.get("logs", []) if str(line or "").strip()],
                *[str(line) for line in worker.get("logs", []) if str(line or "").strip()],
            ],
            limit=80,
        )
        merged = {
            **item,
            "status": str(worker.get("status") or "running"),
            "run_id": run_id,
            "result": merged_result,
            "logs": logs,
            "log_count": max(_as_int(item.get("log_count"), 0), len(logs)),
            "error": "",
            "cancelled_at": "",
        }
        if worker_progress:
            merged["progress"] = dict(worker_progress)
        merged_persisted.append(merged)
        merged_web_job_ids.add(web_job_id)

    if not merged_web_job_ids:
        return dynamic, merged_persisted
    visible_dynamic = []
    for item in dynamic:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if str(result.get("web_job_id") or "").strip() in merged_web_job_ids and _live_find_process_worker(item):
            continue
        visible_dynamic.append(item)
    return visible_dynamic, merged_persisted


def _project_stage_running_blocker(payload: dict[str, Any], stage: str) -> dict[str, Any] | None:
    stage_key = str(stage or "").strip().lower()
    if stage_key not in PROJECT_STAGE_EXCLUSIVE_ACTIONS:
        return None
    project = str(payload.get("project") or "").strip()
    if not project:
        return None
    try:
        root = _safe_project_root(project)
    except ValueError:
        return None
    workers = [
        row for row in _active_project_child_processes(project, root, phase_hint=stage_key)
        if str(row.get("phase") or "").strip().lower() in PROJECT_STAGE_EXCLUSIVE_PHASES
    ]
    if stage_key == "experiment" and workers and _project_handoff_ready_for_experimenting(project):
        workers = [row for row in workers if str(row.get("phase") or "").strip().lower() != "environment"]
    if not workers:
        return None
    worker = workers[0]
    return {
        "error": "project_stage_already_running",
        "status": "blocked_existing_project_stage_running",
        "project": project,
        "action": str(payload.get("action") or stage_key),
        "stage": stage_key,
        "message": "A project environment/experiment/paper stage worker is already running; duplicate launch is blocked.",
        "message_zh": "当前项目已有环境/实验/论文阶段任务正在运行；已阻止重复启动。",
        "existing_worker": {
            "pid": worker.get("pid"),
            "phase": worker.get("phase"),
            "kind": worker.get("kind"),
            "elapsed": worker.get("elapsed"),
            "cmd": worker.get("cmd"),
        },
    }


def _claude_json_result_from_text(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    candidates = [raw]
    candidates.extend(line.strip() for line in reversed(raw.splitlines()) if line.strip().startswith("{"))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
            nested = payload.get("claude_json") if isinstance(payload.get("claude_json"), dict) else {}
            nested_result = nested.get("result") if isinstance(nested, dict) else None
            if isinstance(nested_result, str) and nested_result.strip():
                return nested_result.strip()
    return ""


def _completed_claude_response_from_stdout(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    json_result = _claude_json_result_from_text(raw)
    if json_result:
        return json_result
    chunks = [match.group(1) for match in re.finditer(r"(?ms)^Claude:\s?(.*?)(?=^Claude:|^claude:|\Z)", raw)]
    if chunks:
        combined = "".join(chunks).strip()
        parts = re.split(r"\n---\s*\n", combined)
        if len(parts) > 1:
            return ("---\n" + parts[-1].lstrip()).strip()
        return combined
    return raw


def _extract_latest_claude_response(last_result: Any) -> tuple[str, str]:
    payload = last_result if isinstance(last_result, dict) else {}
    claude_json = payload.get("claude_json") if isinstance(payload.get("claude_json"), dict) else {}
    result = claude_json.get("result") if isinstance(claude_json, dict) else ""
    if isinstance(result, str) and result.strip():
        return result.strip(), "claude_json.result"
    for key in ["response_markdown", "response", "raw_response"]:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    for key in ["stdout", "raw_stdout"]:
        value = payload.get(key)
        cleaned = _completed_claude_response_from_stdout(value)
        if cleaned:
            return cleaned, key
    return "", ""


def _tail_file_text(path: Path, max_bytes: int = 1000000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


CLAUDE_RESPONSE_PANEL_STAGES = {"environment", "experiment", "paper"}


def _safe_claude_response_session_key(value: Any = "") -> str:
    key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    return key.strip("._-")[:80] or "main"


def _claude_response_report_path(root: Path, session_key: str = "main") -> Path:
    key = _safe_claude_response_session_key(session_key)
    if key == "environment_controller":
        return root / "reports" / "environment_controller.md"
    if key == "experimenting_controller":
        return root / "reports" / "experimenting_controller.md"
    if key == "writing_controller":
        return root / "reports" / "writing_controller.md"
    return root / "reports" / f"{key}.md"


def _latest_claude_stage_last_result(root: Path, stage: Any) -> tuple[dict[str, Any], str, str]:
    panel = _safe_claude_response_session_key(stage).lower()
    if panel not in CLAUDE_RESPONSE_PANEL_STAGES:
        return {}, "", "module_stage_required"
    if panel == "environment":
        controller = _read_project_json(root / "state" / "environment_controller_last_result.json", {})
        if isinstance(controller, dict) and controller:
            return controller, "environment_controller", ""
        return {}, "environment_controller", "stage_receipt_not_found"
    if panel == "experiment":
        controller = _read_project_json(root / "state" / "experimenting_controller_last_result.json", {})
        if isinstance(controller, dict) and controller:
            return controller, "experimenting_controller", ""
        return {}, "experimenting_controller", "stage_receipt_not_found"
    controller = _read_project_json(root / "state" / "writing_controller_last_result.json", {})
    if not isinstance(controller, dict) or not controller:
        return {}, "writing_controller", "stage_receipt_not_found"
    response, _source = _extract_latest_claude_response(controller)
    if _paper_receipt_stale_for_current_venue(root, controller, response):
        return {}, "writing_controller", "stage_receipt_stale_for_current_venue"
    return controller, "writing_controller", ""


def _latest_claude_response_payload(root: Path, *, max_chars: int = 1000000, stage: str = "") -> dict[str, Any]:
    requested_stage = _safe_claude_response_session_key(stage).lower() if str(stage or "").strip() else ""
    if requested_stage:
        last_result, session_key, fallback_reason = _latest_claude_stage_last_result(root, requested_stage)
    else:
        last_result = {}
        session_key = ""
        fallback_reason = "module_stage_required"
    last_result = last_result if isinstance(last_result, dict) else {}
    response, source = _extract_latest_claude_response(last_result)
    if not response and requested_stage:
        report_path = _claude_response_report_path(root, session_key)
        response = _tail_file_text(report_path, max_bytes=max(262144, min(max_chars, 1000000)))
        source = f"{report_path.relative_to(root)}.tail" if response and root in report_path.parents else (str(report_path) + ".tail" if response else "")
    if requested_stage == "paper" and response and _paper_receipt_stale_for_current_venue(root, last_result, response):
        response = ""
        source = ""
        if not fallback_reason:
            fallback_reason = "stage_receipt_stale_for_current_venue"
    response = _redact_public_log_text(response)
    total_chars = len(response)
    max_chars = max(1000, min(int(max_chars or 1000000), 2000000))
    truncated = total_chars > max_chars
    returned = response[-max_chars:] if truncated else response
    return {
        "status": last_result.get("status", ""),
        "stage": last_result.get("stage", ""),
        "requested_stage": requested_stage,
        "stage_session_key": session_key,
        "stage_local": bool(requested_stage and not fallback_reason),
        "fallback_from_session_key": "",
        "fallback_reason": fallback_reason,
        "return_code": last_result.get("return_code", ""),
        "started_at": last_result.get("started_at", ""),
        "finished_at": last_result.get("finished_at", ""),
        "session_id": last_result.get("session_id", ""),
        "source": source,
        "response_markdown": returned,
        "response_chcount": total_chars,
        "returned_chcount": len(returned),
        "truncated": truncated,
        "truncated_head_chars": max(0, total_chars - len(returned)),
        "full_response_available": bool(response),
        "content_compacted": False,
    }


@app.get("/api/projects/{project}/claude/latest-response")
def api_project_claude_latest_response(project: str, max_chars: int = Query(1000000, ge=1000, le=2000000), stage: str = Query("")) -> dict:
    try:
        root = _safe_project_root(_account_project_id(project, require_exists=True))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    try:
        max_chars_value = int(max_chars)
    except Exception:
        max_chars_value = 1000000
    return _latest_claude_response_payload(root, max_chars=max_chars_value, stage=stage)


@app.get("/api/projects/{project}/runtime")
def api_project_runtime(project: str) -> dict:
    try:
        return _public_account_payload(runtime_status(_account_project_id(project, require_exists=True)))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/projects/{project}/runtime/detect")
def api_project_runtime_detect(project: str) -> dict:
    try:
        project_id = _account_project_id(project, require_exists=True)
        runtime = detect_runtime_config(project_id)
        return _public_account_payload(runtime_status(project_id) | {"runtime": runtime})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/projects/{project}/runtime")
def api_project_runtime_update(project: str, payload: dict[str, Any]) -> dict:
    try:
        project_id = _account_project_id(project, require_exists=True)
        runtime = update_runtime_config(project_id, payload)
        return _public_account_payload(runtime_status(project_id) | {"runtime": runtime})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/projects/{project}/config")
def api_project_config_update(project: str, payload: dict[str, Any]) -> dict:
    try:
        project_id = _account_project_id(project, require_exists=True)
        return _public_account_payload(update_project_config(project_id, payload))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/jobs/project")
def api_job(payload: dict[str, Any]) -> dict:
    try:
        payload = dict(payload)
        payload["project"] = _account_project_id(payload.get("project"), require_exists=True)
        runtime_env = dict(payload.get("runtime_env") or {}) if isinstance(payload.get("runtime_env"), dict) else {}
        runtime_env["FINDING_LLM_CONFIG"] = str(_local_llm_config_path())
        payload["runtime_env"] = runtime_env
        blocker = action_gate_blocker(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    stage = job_stage(payload)
    action = str(payload.get("action") or "").strip().lower().replace("_", "-")
    if action in {"find", "full-cycle", "full-research-cycle"}:
        llm_error = _find_llm_configuration_error(load_config())
        if llm_error:
            return JSONResponse(status_code=400, content=llm_error)
    stage_blocker = _project_stage_running_blocker(payload, stage)
    if stage_blocker:
        return JSONResponse(status_code=409, content=stage_blocker)
    job_id = f"{stage}_{uuid4().hex[:10]}"
    payload_with_job = {**payload, "web_job_id": job_id}
    initial_result = _initial_module_controller_job_result(payload_with_job, stage)
    if not initial_result:
        initial_result = {
            "project": payload["project"],
            "action": str(payload.get("action") or stage),
            "web_job_id": job_id,
            "status": "running",
        }
    with JOBS_LOCK:
        duplicate = _active_web_stage_job_blocker(payload["project"], stage)
        if duplicate:
            return JSONResponse(status_code=409, content=duplicate)
        job = start_job(stage, lambda log, should_cancel, progress: run_action(payload_with_job, log, should_cancel, progress), job_id=job_id)
    if initial_result:
        job.result = initial_result
        panel_stage = str(initial_result.get("panel_stage") or "").strip()
        if panel_stage:
            message = "Environment 主控 Claude 正在处理环境配置请求。" if panel_stage == "environment" else f"模块主控 Claude 正在处理{_public_stage_label(panel_stage)}请求。"
            job.progress = {"phase": panel_stage, "current": 0, "total": 0, "percent": 0, "message": message}
            job.progress_version += 1
        _persist_jobs()
    return _public_account_payload(job.as_dict())


@app.get("/api/projects/{project}/files/{file_path:path}")
def api_project_file(project: str, file_path: str):
    try:
        project_info = project_summary(_account_project_id(project, require_exists=True))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    root = Path(project_info["path"]).resolve()
    public_file_path = file_path.strip().lstrip("/")
    target = (root / public_file_path).resolve()
    if public_file_path == "planning/finding" or public_file_path.startswith("planning/finding/"):
        suffix = public_file_path.removeprefix("planning/finding").lstrip("/")
        legacy_target = (root / "planning" / "finding" / suffix).resolve()
        # Public TASTE naming hides the historical on-disk directory name; keep
        # old artifacts readable until TASTE migrates project files itself.
        if not target.exists() and legacy_target.exists():
            target = legacy_target
    if target != root and root not in target.parents:
        return JSONResponse({"error": "file path outside project"}, status_code=403)
    artifact_roots = [root / "paper" / "output", root / "paper" / "venues", root / "paper" / "writing", root / "experiments", root / "state"]
    planning_roots = [root / "planning" / "finding", root / "planning" / "finding"]
    allowed_roots = artifact_roots + planning_roots
    if not any(base.exists() and (target == base.resolve() or base.resolve() in target.parents) for base in allowed_roots):
        return JSONResponse({"error": "only paper, experiment, state, and find/planning artifact files are exposed"}, status_code=403)
    if any(base.exists() and (target == base.resolve() or base.resolve() in target.parents) for base in planning_roots):
        allowed_suffixes = {".md", ".json", ".txt", ".csv"}
        if target.suffix.lower() not in allowed_suffixes:
            return JSONResponse({"error": "only markdown, json, text, and csv find/planning artifacts are exposed"}, status_code=403)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(target))


@app.post("/api/runs/{run_id}/plans/{plan_id}/finish")
def api_finish_plan(run_id: str, plan_id: str):
    try:
        if not _account_owns_run(run_id):
            raise ValueError("Run not found.")
        current = _project_context_for_find_run(run_id) or _current_project_for_find_guard()
        project = current[0] if current else ""
        if not project:
            raise ValueError("Plan selection requires an active project.")
        cmd = [
            os.environ.get("MANAGEMENT_PYTHON") or sys.executable,
            str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
            "module",
            "planning",
            "--action",
            "finish",
            "--project",
            project,
            "--run-id",
            run_id,
            "--plan-id",
            plan_id,
        ]
        rc, stdout_text, payload = _run_framework_process(cmd, _framework_module_env(load_config(), project), lambda _message: None, lambda: False)
        if rc != 0:
            return JSONResponse({"error": "Framework Planning selection failed", "stdout_tail": stdout_text[-4000:]}, status_code=500)
        _clerun_caches(run_id)
        _cleruntime_caches(project)
        return _public_account_payload(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)




@app.get("/api/jobs")
def api_jobs(
    compact: bool = Query(True),
    limit: int = Query(300, ge=1, le=1000),
    include_history: bool = Query(True),
    project: str = Query(""),
) -> list[dict]:
    raw_project = _api_query_str(project)
    if raw_project:
        try:
            project = _account_project_id(raw_project, require_exists=True)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
    else:
        project = ""
    try:
        dynamic = _live_jobs_from_projects(compact=True, project_filter=project)
    except TypeError as exc:
        if "project_filter" not in str(exc):
            raise
        dynamic = _live_jobs_from_projects(compact=True)
    if not compact:
        try:
            _reconcile_detached_launcher_jobs(dynamic, force=True)
        except TypeError as exc:
            if "force" not in str(exc):
                raise
            _reconcile_detached_launcher_jobs(dynamic)
    _reconcile_stale_cancelling_jobs()
    dynamic = [item for item in dynamic if _account_owns_job_payload(item)]
    if project:
        dynamic = [item for item in dynamic if _job_belongs_to_project(item, project)]

    def live_paper_project(item: dict[str, Any]) -> str:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if str(item.get("stage") or "").strip().lower() != "paper":
            return ""
        if str(item.get("status") or "").strip().lower() not in {"queued", "running", "cancelling"} and result.get("process_alive") is not True:
            return ""
        if result.get("process_alive") is not True:
            return ""
        return str(result.get("project") or "").strip()

    def live_paper_rank(item: dict[str, Any]) -> tuple[int, str]:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        kind = str(result.get("paper_worker_kind") or result.get("kind") or "").strip()
        rank = {
            "paper_claude_session": 0,
            "paper_claude_cli": 1,
            "paper_orchestra_bridge": 2,
            "paper_subprocess": 3,
            "paper_repair_loop": 4,
            "paper_pipeline": 5,
        }.get(kind, 9)
        return rank, str(item.get("created_at") or "")

    preferred_live_paper: dict[str, dict[str, Any]] = {}
    for item in dynamic:
        item_project = live_paper_project(item)
        if not item_project:
            continue
        current = preferred_live_paper.get(item_project)
        if current is None or live_paper_rank(item) < live_paper_rank(current):
            preferred_live_paper[item_project] = item
    if preferred_live_paper:
        dynamic = [
            item for item in dynamic
            if not live_paper_project(item) or preferred_live_paper.get(live_paper_project(item)) is item
        ]
    live_paper_projects = set(preferred_live_paper)

    with JOBS_LOCK:
        job_snapshot = list(JOBS.values())
    job_snapshot = [job for job in job_snapshot if _account_owns_job_state(job)]
    if project:
        job_snapshot = [job for job in job_snapshot if _job_state_belongs_to_project(job, project)]
    if compact:
        effective_limit = min(limit, 30 if include_history else 10)
        persisted = []
        for job in job_snapshot:
            public_stage = _public_taste_stage(getattr(job, "stage", ""))
            source_item = job.as_dict(compact=False) if public_stage in {"read", "idea", "plan"} else job.as_dict(compact=True)
            compact_item = _compact_job_for_list(source_item)
            compact_item["owner_id"] = job.owner_id
            persisted.append(compact_item)
    else:
        effective_limit = limit
        persisted = []
        for job in job_snapshot:
            item = job.as_dict(compact=False)
            item["owner_id"] = job.owner_id
            persisted.append(item)
    dynamic, persisted = _merge_live_find_workers_into_web_jobs(dynamic, persisted)
    hidden_taskbstages = set()
    dynamic_live_projects = {
        str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "")
        for item in dynamic
        if str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}
    }
    dynamic_live_stage_projects = {
        (
            str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or _project_from_job_payload(item.get("job_id"), item) or ""),
            _public_taste_stage(item.get("stage")),
        )
        for item in dynamic
        if str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}
    }
    dynamic_live_stage_projects = {pair for pair in dynamic_live_stage_projects if pair[0] and pair[1] in PROJECT_STAGE_EXCLUSIVE_PHASES}
    dynamic_live_stage_run_ids: dict[tuple[str, str], str] = {}
    for item in dynamic:
        if str(item.get("status") or "").lower() not in {"queued", "running", "cancelling"}:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        project_id = str(result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
        stage_id = _public_taste_stage(item.get("stage"))
        run_id_value = str(item.get("run_id") or result.get("run_id") or "").strip()
        if project_id and stage_id in PROJECT_STAGE_EXCLUSIVE_PHASES and run_id_value:
            dynamic_live_stage_run_ids[(project_id, stage_id)] = run_id_value

    for item in persisted:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if str(item.get("status") or "").lower() not in {"queued", "running", "cancelling"}:
            continue
        project_id = str(result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
        stage_id = _public_taste_stage(item.get("stage"))
        live_run_id = dynamic_live_stage_run_ids.get((project_id, stage_id))
        if live_run_id and not str(item.get("run_id") or result.get("run_id") or "").strip():
            item["run_id"] = live_run_id
            if isinstance(result, dict):
                result["run_id"] = live_run_id

    def _hide_persisted_job(item: dict[str, Any]) -> bool:
        if item.get("internal") or str(item.get("display") or "").lower() == "hidden":
            return True
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        stage = str(item.get("stage") or "")
        raw_stage = str(result.get("raw_stage") or stage)
        job_id = str(item.get("job_id") or "")
        if stage in hidden_taskbstages or raw_stage in hidden_taskbstages:
            return True
        if raw_stage == "safe-unblock" or job_id.startswith(("safe-unblock_", "safe-unblock-")):
            return True
        is_full_cycle_job = (
            raw_stage in {"full-cycle", "full_research_cycle", "full-cycle", "full_research_cycle"}
            or job_id.startswith(("full-cycle_", "full-cycle-", "full_cycle_", "full_cycle-"))
            or "run_full_research_cycle.py" in str(result.get("cmd") or result.get("command") or "")
        )
        if is_full_cycle_job:
            project = str(result.get("project") or "")
            if project and project in dynamic_live_projects:
                return True
        item_project = str(result.get("project") or _project_from_job_payload(job_id, item) or "").strip()
        item_stage = _public_taste_stage(stage)
        item_status = str(item.get("status") or result.get("status") or "").strip().lower()
        item_run_id = str(item.get("run_id") or result.get("run_id") or result.get("find_run_id") or "").strip()
        stopped = item_status not in {"queued", "running", "cancelling"}
        if stopped and item_project and _CURRENT_ACCOUNT.get() is not None and not (PROJECT_IDS_ROOT / item_project).is_dir():
            return True
        if stopped and ("reading_web_smoke" in item_project or "find_reading_web_smoke" in item_run_id):
            return True
        if (
            item_project
            and (item_project, item_stage) in dynamic_live_stage_projects
            and item_status not in {"queued", "running", "cancelling"}
        ):
            return True
        return False

    persisted = [item for item in persisted if not _hide_persisted_job(item)]
    persisted_running_read_projects = {
        str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or _project_from_job_payload(item.get("job_id"), item) or "")
        for item in persisted
        if str(item.get("stage") or "").lower() == "read"
        and str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}
    }
    if persisted_running_read_projects:
        dynamic = [
            item for item in dynamic
            if not (
                str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") in persisted_running_read_projects
                and str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("kind") or "").startswith("current_find")
            )
        ]
    active_current_find_projects = {
        str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "")
        for item in dynamic
        if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("kind") or "").startswith("current_find")
        and str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}
    }
    if active_current_find_projects:
        dynamic = [
            item for item in dynamic
            if not (
                str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") in active_current_find_projects
                and str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("kind") or "") == "current_find_child"
            )
        ]
    current_project_context = _current_project_for_find_guard()
    current_project_id = current_project_context[0] if current_project_context else ""
    exclusive_stage_jobs = {
        (
            str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or _project_from_job_payload(item.get("job_id"), item) or current_project_id),
            str(item.get("stage") or "").strip().lower(),
        )
        for item in persisted
        if str(item.get("stage") or "").strip().lower() in PROJECT_STAGE_EXCLUSIVE_PHASES
        and str(item.get("status") or "").strip().lower() in {"queued", "running", "cancelling"}
    }
    if exclusive_stage_jobs:
        dynamic = [
            item for item in dynamic
            if not (
                (str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or ""), str(item.get("stage") or "").strip().lower()) in exclusive_stage_jobs
                and str(item.get("job_id") or "").startswith(("project-worker_", "experiment-worker_"))
            )
        ]
    dynamic_ids = {str(item.get("job_id") or "") for item in dynamic}
    persisted_items = [item for item in persisted if str(item.get("job_id") or "") not in dynamic_ids]

    def _dedupe_persisted_environment_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_created: dict[str, str] = {}
        for item in rows:
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            stage = _public_taste_stage(item.get("stage"))
            if stage != "environment":
                continue
            project_id = str(result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
            if not project_id:
                continue
            created = str(item.get("created_at") or "")
            if project_id not in latest_created or created > latest_created[project_id]:
                latest_created[project_id] = created
        if not latest_created:
            return rows
        kept: list[dict[str, Any]] = []
        for item in rows:
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            stage = _public_taste_stage(item.get("stage"))
            project_id = str(result.get("project") or _project_from_job_payload(item.get("job_id"), item) or "").strip()
            if stage == "environment" and project_id and str(item.get("created_at") or "") != latest_created.get(project_id):
                continue
            kept.append(item)
        return kept

    persisted_items = _dedupe_persisted_environment_history(persisted_items)
    if live_paper_projects:
        def superseded_by_live_paper(item: dict[str, Any]) -> bool:
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            status = str(item.get("status") or "").strip().lower()
            if status not in {"preview_available", "needs_writing", "completed", "done", "blocked"}:
                return False
            if _public_taste_stage(item.get("stage")) != "paper" and not _is_paper_job(item.get("stage"), item.get("job_id", ""), result, item.get("logs")):
                return False
            paper_stage = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}
            item_project = str(result.get("project") or paper_stage.get("project") or "").strip()
            return item_project in live_paper_projects

        persisted_items = [item for item in persisted_items if not superseded_by_live_paper(item)]

    def _dedupe_completed_paper_previews(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_key: dict[tuple[str, str], str] = {}
        for item in rows:
            stage = str(item.get("stage") or "")
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            status = str(item.get("status") or "").lower()
            is_paper_preview = _is_paper_job(stage, item.get("job_id", ""), result, item.get("logs")) and status in {"preview_available", "needs_writing", "completed", "done", "blocked"}
            if not is_paper_preview:
                continue
            project = str(result.get("project") or ((result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}) or {}).get("project") or "")
            key = (project, "paper-preview")
            created = str(item.get("created_at") or "")
            if key not in latest_key or created > latest_key[key]:
                latest_key[key] = created
        if not latest_key:
            return rows
        kept: list[dict[str, Any]] = []
        for item in rows:
            stage = str(item.get("stage") or "")
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            status = str(item.get("status") or "").lower()
            is_paper_preview = _is_paper_job(stage, item.get("job_id", ""), result, item.get("logs")) and status in {"preview_available", "needs_writing", "completed", "done", "blocked"}
            if not is_paper_preview:
                kept.append(item)
                continue
            project = str(result.get("project") or ((result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}) or {}).get("project") or "")
            if str(item.get("created_at") or "") == latest_key.get((project, "paper-preview")):
                kept.append(item)
        return kept

    persisted_items = _dedupe_completed_paper_previews(persisted_items)
    existing_find_run_ids = {
        str(item.get("run_id") or "")
        for item in dynamic + persisted_items
        if str(item.get("stage") or "") == "find" and str(item.get("run_id") or "")
    }
    run_history = _find_run_history_jobs_from_runs(existing_find_run_ids, limit=max(0, effective_limit - len(dynamic) - len(persisted_items)), project=project) if include_history else []
    stage_history = _current_find_downstream_stage_history_jobs(project, existing_items=dynamic + persisted_items + run_history) if include_history else []
    items = _dedupe_job_items_for_api(dynamic + persisted_items + stage_history + run_history)
    items = _collapse_current_find_read_retry_jobs(items, project_hint=project)
    items = _hide_superseded_stopped_jobs(items, project_hint=project)
    if not include_history:
        items = [item for item in items if str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}]
    if compact:
        items = [_compact_job_for_list(item) for item in items]
    items = [item for item in items if _account_owns_job_payload(item)]
    return _public_account_payload([_public_job_api_payload(item) for item in items[:effective_limit]])


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, compact: bool = Query(True)) -> dict:
    try:
        _reconcile_detached_launcher_jobs(force=True)
    except TypeError as exc:
        if "force" not in str(exc):
            raise
        _reconcile_detached_launcher_jobs()
    live_items = [item for item in _live_jobs_from_projects(compact=compact) if _account_owns_job_payload(item)]
    if job_id:
        live_job = next((item for item in live_items if str(item.get("job_id") or "") == job_id), None)
        if not live_job and job_id.startswith("full_cycle_"):
            project_id = job_id[len("full_cycle_"):]
            live_job = next((item for item in live_items if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") == project_id), None)
        if live_job:
            return _public_account_payload(_public_job_api_payload(live_job))
        if job_id.startswith(("full-cycle_", "full-cycle-", "full_cycle_", "full_cycle-")):
            for item in live_items:
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                command_text = str(result.get("command") or result.get("cmd") or "")
                if "run_full_research_cycle.py" in command_text and str(item.get("status") or "").lower() in {"queued", "running", "cancelling", "blocked"}:
                    return _public_account_payload(_public_job_api_payload({**item, "job_id": job_id}))
    job = JOBS.get(job_id)
    if job is not None and not _account_owns_job_state(job):
        job = None
    if not job:
        if job_id.startswith(("find-run-find_",)):
            run_id = job_id.removeprefix("find-run-")
            history = next((item for item in _find_run_history_jobs_from_runs(set(), limit=300) if str(item.get("run_id") or "") == run_id), None)
            if history and _account_owns_job_payload(history):
                return _public_account_payload(_public_job_api_payload(_compact_job_for_list(history) if compact else history))
        return JSONResponse({"error": "job not found"}, status_code=404)
    source_item = job.as_dict(compact=False) if not compact or _public_taste_stage(job.stage) in {"read", "idea", "plan"} else job.as_dict(compact=True)
    _, merged_items = _merge_live_find_workers_into_web_jobs(live_items, [source_item])
    merged_item = merged_items[0] if merged_items else source_item
    if compact:
        merged_item = _compact_job_for_list(merged_item)
    return _public_account_payload(_public_job_api_payload(merged_item))


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str) -> dict:
    known_job = JOBS.get(job_id)
    if known_job is not None and not _account_owns_job_state(known_job):
        known_job = None
    if job_id:
        _LIVE_JOBS_CACHE.clear()
        live_items = [item for item in _live_jobs_from_projects(compact=False) if _account_owns_job_payload(item)]
        live_job = next((item for item in live_items if str(item.get("job_id") or "") == job_id), None)
        worker_pid = _pid_from_project_worker_job_id(job_id)
        if not live_job and worker_pid:
            live_job = next((
                item for item in live_items
                if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("pid") or "") == worker_pid
                and str(item.get("job_id") or "").startswith(("experiment-worker_", "project-worker_"))
            ), None)
        known_status = str(getattr(known_job, "status", "") or "").lower() if known_job is not None else ""
        if known_job is not None and not live_job and not worker_pid and known_status in {"queued", "running", "cancelling"}:
            known_job.request_cancel()
            project_id = _project_from_job_payload(job_id, None, known_job)
            phase_hint = _phase_hint_from_job(job_id, None, known_job)
            exact_job = known_job.as_dict(compact=False)
            if project_id:
                exact_job = _live_job_with_active_child(job_id, exact_job, project_id, phase_hint=phase_hint)
            result = exact_job.get("result") if isinstance(exact_job.get("result"), dict) else {}
            pid = str(result.get("pid") or "").strip()
            termination = _terminate_process_tree(pid) if pid else {"requested_pid": "", "terminated_pids": [], "terminated_pgids": []}
            if pid:
                known_job.log(f"Termination requested for exact research job child PID={pid}.")
                status = "cancelling"
            else:
                known_job.status = "cancelling"
                known_job.error = ""
                known_job.set_progress("cancelling", 0, 1, "正在停止当前任务；后台批处理会在当前检查点退出。")
                known_job.log("Cancellation requested for in-process job; waiting for the runner to stop.")
                status = "cancelling"
                exact_job = known_job.as_dict(compact=False)
            _LIVE_JOBS_CACHE.clear()
            _persist_jobs()
            return _public_account_payload({**_strip_public_taste_marker(exact_job), "cancel_requested": True, "status": status, "termination": termination})
        project_id = _project_from_job_payload(job_id, live_job, known_job)
        phase_hint = _phase_hint_from_job(job_id, live_job, known_job)
        if not live_job and project_id and not worker_pid:
            live_job = next((item for item in live_items if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") == project_id), None)
            phase_hint = phase_hint or _phase_hint_from_job(job_id, live_job, known_job)
        if not project_id and job_id.startswith(("paper",)):
            phase_hint = "paper"
            recovered_children: list[tuple[str, dict[str, Any]]] = []
            for root in sorted(PROJECT_IDS_ROOT.iterdir()) if PROJECT_IDS_ROOT.exists() else []:
                if not root.is_dir() or not _account_owns_project(root.name):
                    continue
                child = _active_project_child_process(root.name, root, phase_hint="paper")
                if child:
                    recovered_children.append((root.name, child))
            if len(recovered_children) == 1:
                project_id = recovered_children[0][0]
                live_job = {"job_id": job_id, "stage": "paper", "status": "running", "result": {"project": project_id}, "logs": ["Recovered active TASTE paper process for cancellation."], "progress": {}}
        if project_id and not worker_pid:
            result_for_cancel = live_job.get("result") if isinstance(live_job, dict) and isinstance(live_job.get("result"), dict) else {}
            controller_pid = str(result_for_cancel.get("pid") or "").strip()
            controller_cmd = str(result_for_cancel.get("command") or result_for_cancel.get("cmd") or "")
            is_full_cycle_cancel = str(job_id or "").startswith(("full-cycle", "full_cycle"))
            controller_alive = bool(controller_pid and _pid_alive_local(controller_pid) and "run_full_research_cycle.py" in controller_cmd)
            if not (is_full_cycle_cancel and controller_alive):
                live_job = _live_job_with_active_child(job_id, live_job, project_id, phase_hint=phase_hint)
        if live_job:
            result = live_job.get("result") if isinstance(live_job.get("result"), dict) else {}
            pid = str(result.get("pid") or "").strip()
            termination = _terminate_process_tree(pid) if pid else {"requested_pid": "", "terminated_pids": [], "terminated_pgids": []}
            _LIVE_JOBS_CACHE.clear()
            return _public_account_payload({**_strip_public_taste_marker(live_job), "cancel_requested": True, "status": "cancelling", "termination": termination})
    job = known_job
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    job.request_cancel()
    return _public_account_payload(job.as_dict())


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    account = AUTH_STORE.user_for_session(websocket.cookies.get(SESSION_COOKIE))
    if account is None:
        await websocket.close(code=4401)
        return
    account_token = _CURRENT_ACCOUNT.set(account)
    await websocket.accept()
    try:
        sent = 0
        sent_progress = -1
        while True:
            live_job = None
            live_items: list[dict[str, Any]] = []
            if job_id:
                live_items = [item for item in _live_jobs_from_projects(compact=True) if _account_owns_job_payload(item)]
                live_job = next((item for item in live_items if str(item.get("job_id") or "") == job_id), None)
                if not live_job and job_id.startswith("full_cycle_"):
                    project_id = job_id[len("full_cycle_"):]
                    live_job = next((item for item in live_items if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") == project_id), None)
                if not live_job:
                    known_job = JOBS.get(job_id)
                    if known_job is not None and _account_owns_job_state(known_job) and _public_taste_stage(known_job.stage) == "find":
                        source_item = known_job.as_dict(compact=True)
                        _, merged_items = _merge_live_find_workers_into_web_jobs(live_items, [source_item])
                        merged_item = merged_items[0] if merged_items else {}
                        if str(merged_item.get("status") or "").lower() in {"queued", "running", "cancelling"}:
                            live_job = merged_item
            if live_job:
                live_job = _strip_public_taste_marker(live_job)
                live_status = str(live_job.get("status") or "")
                live_stage = _public_taste_stage(live_job.get("stage"))
                if live_status in {"done", "error", "cancelled", "blocked"} or live_status.startswith("blocked"):
                    compact_live_job = _compact_job_for_list(live_job) if live_stage in {"environment", "experiment", "find", "read", "idea", "plan"} else live_job
                    for line in (compact_live_job.get("logs") or []):
                        await websocket.send_json({"type": "log", "message": str(line)})
                    await websocket.send_json({"type": "progress", "progress": _public_job_api_payload(compact_live_job.get("progress") or {})})
                    await websocket.send_json({"type": "complete", "job": compact_live_job})
                    return
                if live_stage in {"find", "read"}:
                    snapshot = _public_job_api_payload(_compact_job_for_list(live_job))
                    await websocket.send_json({"type": "snapshot", "job": snapshot})
                    await asyncio.sleep(2.0)
                    continue
                logs = [str(line) for line in (live_job.get("logs") or [])]
                new_logs = logs[sent:]
                if live_stage in {"environment", "experiment", "read", "idea", "plan"}:
                    new_logs = _public_job_logs(live_job.get("stage"), new_logs, {}, {}, limit=6)
                elif live_stage == "find":
                    new_logs = []
                for line in new_logs:
                    await websocket.send_json({"type": "log", "message": line})
                sent = len(logs)
                live_progress_payload = _public_job_api_payload(live_job.get("progress") or {})
                if live_stage in {"environment", "experiment", "find", "read", "idea", "plan"}:
                    compact_live_job = _compact_job_for_list(live_job)
                    live_progress_payload = _public_job_api_payload(compact_live_job.get("progress") or live_progress_payload)
                await websocket.send_json({"type": "progress", "progress": live_progress_payload})
                await asyncio.sleep(2.0)
                continue
            job = JOBS.get(job_id)
            if not job or not _account_owns_job_state(job):
                await websocket.send_json({"type": "error", "message": "job not found"})
                return
            job_stage = _public_taste_stage(job.stage)
            if job.status in {"done", "error", "cancelled", "blocked"}:
                compact_source = job.as_dict(compact=False) if job_stage in {"read", "idea", "plan"} else job.as_dict(compact=True)
                compact_job = _compact_job_for_list(compact_source) if job_stage in {"environment", "experiment", "find", "read", "idea", "plan"} else compact_source
                for line in (compact_job.get("logs") or []):
                    await websocket.send_json({"type": "log", "message": str(line)})
                await websocket.send_json({"type": "progress", "progress": _public_job_api_payload(compact_job.get("progress") or {})})
                await websocket.send_json({"type": "complete", "job": compact_job})
                return
            if job_stage in {"read", "idea", "plan"}:
                await websocket.send_json({"type": "snapshot", "job": _compact_job_for_list(job.as_dict(compact=False))})
                await asyncio.sleep(2.0)
                continue
            new_logs = _strip_public_taste_marker(job.logs[sent:])
            if job_stage in {"environment", "experiment", "read", "idea", "plan"}:
                new_logs = _public_job_logs(job.stage, new_logs, {}, {}, limit=6)
            elif job_stage == "find":
                new_logs = []
            for line in new_logs:
                await websocket.send_json({"type": "log", "message": line})
            sent = len(job.logs)
            if job.progress_version != sent_progress:
                job_progress_payload = _public_job_api_payload(job.progress)
                if job_stage in {"environment", "experiment", "find", "read", "idea", "plan"}:
                    compact_source = job.as_dict(compact=False) if job_stage in {"read", "idea", "plan"} else job.as_dict(compact=True)
                    compact_job = _compact_job_for_list(compact_source)
                    job_progress_payload = _public_job_api_payload(compact_job.get("progress") or job_progress_payload)
                await websocket.send_json({"type": "progress", "progress": job_progress_payload})
                sent_progress = job.progress_version
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
    finally:
        _CURRENT_ACCOUNT.reset(account_token)


@app.get("/api/runs")
def api_runs(project: str = Query("")) -> list[dict]:
    raw_project = _api_query_str(project)
    if raw_project:
        try:
            project_id = _account_project_id(raw_project, require_exists=True)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        items = _filter_runs_for_project(_cached_list_runs(), project_id)
    else:
        items = [item for item in _cached_list_runs() if _account_owns_run(item.get("run_id"))]
    return _public_account_payload(items)


@app.get("/api/runs/{run_id}/artifacts")
def api_artifacts(run_id: str, light: bool = Query(False), scope: str = Query(""), project: str = Query("")) -> dict:
    light_mode = bool(light) if isinstance(light, bool) else bool(getattr(light, "default", False))
    artifact_scope = str(scope if isinstance(scope, str) else getattr(scope, "default", "") or "").strip().lower()
    raw_project = _api_query_str(project)
    try:
        project_id = _account_project_id(raw_project, require_exists=True) if raw_project else ""
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if not project_id and not _account_owns_run(run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    if artifact_scope and artifact_scope not in CURRENT_FIND_ARTIFACT_SCOPES:
        return JSONResponse({"error": f"Unknown artifact scope: {artifact_scope}"}, status_code=400)
    if project_id:
        try:
            project_root = _safe_project_root(project_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        current_run_id = _project_current_find_run_id(project_root)
        requested_run_id = str(run_id or "").strip()
        live_find_read = artifact_scope == "find" and _live_find_run_matches_project(project_id, requested_run_id)
        if current_run_id != requested_run_id and not live_find_read:
            return JSONResponse({
                "error": "Requested run is not the project's current Find run.",
                "project": project_id,
                "run_id": run_id,
                "current_find_run_id": current_run_id,
            }, status_code=409)
    else:
        project_root = _project_root_for_find_run(run_id)
    try:
        artifact_roots = _run_artifact_roots(run_id)
    except FileNotFoundError as exc:
        if project_root and _project_current_find_run_id(project_root) == str(run_id or "").strip():
            artifact_roots = [project_root / "planning" / "finding"]
        else:
            return JSONResponse({"error": str(exc), "run_id": run_id, "artifacts": []}, status_code=404)
    directory = artifact_roots[0]
    environment_run = _environment_artifact_run(artifact_roots, run_id)

    def artifact_path(name: str) -> Path:
        project_artifact = _project_taste_artifact_path(project_root, run_id, name)
        if project_artifact:
            return project_artifact
        return _run_artifact_path(name, artifact_roots, directory)

    markdown_names: list[str]
    json_names: list[str]
    if artifact_scope:
        markdown_names, json_names = CURRENT_FIND_ARTIFACT_SCOPES[artifact_scope]
    elif environment_run:
        markdown_names = ENVIRONMENT_ARTIFACT_MARKDOWN_NAMES
        json_names = ENVIRONMENT_ARTIFACT_JSON_NAMES
    elif light_mode:
        markdown_names, json_names = _project_current_find_light_artifact_names(project_root, run_id)
    else:
        markdown_names, json_names = [], []
    if not markdown_names and not json_names:
        blocked_markdown_names = _blocked_current_find_downstream_markdown_names(project_root, run_id)
        markdown_names = [
            name
            for name in (LIGHT_ARTIFACT_MARKDOWN_NAMES if light_mode else MARKDOWN_ARTIFACT_NAMES)
            if name not in blocked_markdown_names
        ]
        json_names = LIGHT_ARTIFACT_JSON_NAMES if light_mode else JSON_ARTIFACT_NAMES
    file_names = markdown_names + json_names
    mtimes: list[tuple[str, int, int]] = []
    for name in file_names:
        path = artifact_path(name)
        if not path.exists():
            continue
        try:
            stat = path.stat()
            mtimes.append((name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    if any(name in {"idea.md", "plan.md"} for name in markdown_names):
        for ref_path in _current_find_reference_artifact_paths(project_root):
            try:
                stat = ref_path.stat()
                mtimes.append((f"paper-ref:{ref_path.name}", stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
    cache_slot = f"{run_id}:{project_id}:{artifact_scope or ('light' if light_mode else 'full')}"
    cache_key = json.dumps([run_id, project_id, artifact_scope or ("light" if light_mode else "full"), mtimes], separators=(",", ":"), ensure_ascii=False)
    cached = _RUN_ARTIFACTS_CACHE.get(cache_slot)
    now = time.monotonic()
    if isinstance(cached, dict) and cached.get("key") == cache_key and float(cached.get("expires_at") or 0) > now:
        return cached["payload"]
    artifacts = []
    for name in markdown_names:
        path = artifact_path(name)
        if path.exists():
            try:
                stat = path.stat()
            except OSError:
                stat = None
            size_bytes = stat.st_size if stat is not None else 0
            if size_bytes > LARGE_MARKDOWN_ARTIFACT_LIMIT_BYTES:
                compact = _compact_large_markdown_artifact(path, size_bytes)
                artifacts.append({"name": name, "kind": "markdown", "path": str(path), **compact})
                continue
            content = path.read_text(encoding="utf-8")
            if name == "source_status.md" and not content.strip():
                content = _source_status_markdown_from_find_results(read_json(directory / "find_results.json", {}), content)
            content = _hydrate_current_find_markdown_paper_refs(content, project_root, name)
            artifacts.append({"name": name, "kind": "markdown", "content": _public_text(content), "path": str(path), "content_truncated": False, "size_bytes": size_bytes})
    for name in json_names:
        path = artifact_path(name)
        if path.exists():
            try:
                stat = path.stat()
            except OSError:
                stat = None
            if name == "find_progress.json" and stat is not None and stat.st_size > LARGE_JSON_ARTIFACT_LIMIT_BYTES:
                content = _compact_large_find_progress_artifact(path, run_id, stat.st_size, project_root)
                artifacts.append({"name": name, "kind": "json", "content": content, "path": str(path), "content_truncated": True, "size_bytes": stat.st_size})
                continue
            if name == "find_results.json" and stat is not None and stat.st_size > LARGE_JSON_ARTIFACT_LIMIT_BYTES:
                content = _compact_large_find_results_artifact(directory, run_id, stat.st_size)
                artifacts.append({"name": name, "kind": "json", "content": content, "path": str(path), "content_truncated": True, "size_bytes": stat.st_size})
                continue
            content = read_json(path, {})
            if name == "config.json" and isinstance(content, dict):
                content = redacted_config(content)
            if name in {"find_progress.json", "find_results.json"}:
                content = _strip_redundant_find_public_json_aliases(content)
            artifacts.append({"name": name, "kind": "json", "content": _strip_public_taste_marker(content), "path": str(path), "content_truncated": False, "size_bytes": stat.st_size if stat is not None else 0})
    payload = {"run_id": run_id, "project": project_id, "scope": artifact_scope, "artifacts": artifacts, "artifact_roots": [str(path) for path in artifact_roots]}
    _RUN_ARTIFACTS_CACHE[cache_slot] = {"key": cache_key, "expires_at": now + RUN_ARTIFACTS_CACHE_TTL_SEC, "payload": payload}
    return _public_account_payload(payload)


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str) -> dict:
    if not _account_owns_run(run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    delete_run(run_id)
    _clerun_caches(run_id)
    return {"status": "ok", "run_id": run_id}


@app.patch("/api/runs/{run_id}/ideas/{idea_id}")
def api_patch_idea(
    run_id: str,
    idea_id: str,
    patch: IdeaPatch,
    project: str = Query(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"),
) -> dict:
    try:
        project_id = _account_project_id(project, require_exists=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if not _account_owns_run(run_id) or not _run_belongs_to_project(run_id, project_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    blocker = _active_web_stage_job_blocker(project_id, "idea")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    cmd = [
        os.environ.get("MANAGEMENT_PYTHON") or sys.executable,
        str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "ideation",
        "--action",
        "patch",
        "--project",
        project_id,
        "--run-id",
        run_id,
        "--idea-id",
        idea_id,
    ]
    env = _ideation_framework_env(load_config(), project_id)
    env["TASTE_IDEATION_PATCH_JSON"] = json.dumps(patch.model_dump(exclude_none=True), ensure_ascii=False)
    rc, stdout_text, payload = _run_framework_process(cmd, env, lambda _message: None, lambda: False)
    if rc != 0:
        detail = _framework_user_error(stdout_text, f"Framework rejected Idea patch with exit code {rc}.")
        return JSONResponse(status_code=400, content={"status": "error", "message": detail})
    _clerun_caches(run_id)
    _cleruntime_caches(project_id)
    return _public_account_payload(payload)


@app.put("/api/runs/{run_id}/idea-markdown")
def api_update_idea_markdown(
    run_id: str,
    update: IdeaMarkdownUpdate,
    project: str = Query(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"),
) -> dict:
    try:
        project_id = _account_project_id(project, require_exists=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if not _account_owns_run(run_id) or not _run_belongs_to_project(run_id, project_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    blocker = _active_web_stage_job_blocker(project_id, "idea")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    cmd = [
        os.environ.get("MANAGEMENT_PYTHON") or sys.executable,
        str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "ideation",
        "--action",
        "update_markdown",
        "--project",
        project_id,
        "--run-id",
        run_id,
    ]
    rc, stdout_text, payload = _run_framework_process(
        cmd,
        _ideation_framework_env(load_config(), project_id),
        lambda _message: None,
        lambda: False,
        input_text=update.markdown,
    )
    if rc != 0:
        detail = _framework_user_error(stdout_text, "Framework rejected idea.md update.")
        return JSONResponse(status_code=400, content={"status": "error", "message": detail})
    _clerun_caches(run_id)
    _cleruntime_caches(project_id)
    return _public_account_payload(payload)


@app.put("/api/runs/{run_id}/plan-markdown")
def api_update_plan_markdown(run_id: str, update: PlanMarkdownUpdate, project: str = "") -> dict:
    if not _account_owns_run(run_id):
        return JSONResponse({"error": "run not found"}, status_code=404)
    try:
        project_id_input = _account_project_id(project, require_exists=True) if project else ""
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    current = (project_id_input, PROJECT_IDS_ROOT / project_id_input) if project_id_input else (_project_context_for_find_run(run_id) or ("", None))
    project_id, _root = current
    if not project_id:
        return JSONResponse(status_code=409, content={"status": "error", "message": "Plan Markdown editing requires an active project."})
    cmd = [
        os.environ.get("MANAGEMENT_PYTHON") or sys.executable,
        str(WORKSPACE_ROOT / "framework" / "scripts" / "main.py"),
        "module",
        "planning",
        "--action",
        "update_markdown",
        "--project",
        project_id,
        "--run-id",
        run_id,
    ]
    rc, stdout_text, payload = _run_framework_process(
        cmd,
        _framework_module_env(load_config(), project_id),
        lambda _message: None,
        lambda: False,
        input_text=update.markdown,
    )
    if rc != 0:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Framework rejected plan.md update.", "stdout_tail": stdout_text[-4000:]})
    _clerun_caches(run_id)
    _cleruntime_caches(project_id)
    return _public_account_payload(payload)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/{path_name:path}")
def root(path_name: str = ""):
    index = CLIENT_DIST / "index.html"
    requested = CLIENT_DIST / path_name
    if path_name and requested.exists() and requested.is_file():
        return FileResponse(str(requested))
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse(
        """
<!doctype html>
<html><head><meta charset="utf-8"><title>TASTE</title></head>
<body>
  <h1>TASTE API is running</h1>
  <p>Build the frontend with <code>npm run build</code> in <code>web/frontend/client</code>.</p>
</body></html>
"""
    )
