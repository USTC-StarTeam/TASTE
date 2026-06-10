from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import re
import signal
import subprocess
import sys
import time
import threading
import traceback
from concurrent.futures import TimeoutError as FutureTimeoutError, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auto_research.auto_find.catalog import catalog_by_id, load_catalog
from auto_research.auto_find.pipeline import FIND_RECOMMENDATION_POLICY, SCORING_POLICY_VERSION, _attach_abstract_language_fields, _critique_candidates, _llm_live_gate, _recommendation_quality_audit, _recommended, _screened_ranking, _triage_candidates, run_find
from auto_research.auto_find.sources import fetch_venue_sample
from auto_research.auto_idea.pipeline import patch_idea, run_idea
from auto_research.auto_plan.pipeline import finish_plan, polish_plan, run_plan
from auto_research.auto_read.pipeline import run_read
from auto_research.emailer import send_run_email
from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient
from auto_research.markdown import paper_markdown
from auto_research.models import AppConfig, EmailJobRequest, FindRequest, IdeaPatch, IdeaRequest, PlanPolishRequest, PlanRequest, ReadRequest, VenueHealthRequest
from auto_research.source_selection import canonical_source_selection, normalize_source_selection, save_canonical_source_selection, project_config_path
from auto_research.paths import CONFIG_PATH, RUNS_DIR, STATE_DIR, ensure_directories
from auto_research.storage import delete_run, list_runs, read_json, redacted_config, run_dir, write_json
from auto_research.web.project_bridge import action_gate_blocker, job_stage, create_project_config, detect_runtime_config, list_projects as list_projects, project_summary, run_action, runtime_status, update_project_config, update_runtime_config
from paper_common import get_active_paper_state


ensure_directories()

app = FastAPI(title="TASTE Local API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _prevent_stale_frontend_cache(request, call_next):
    response = await call_next(request)
    path = str(request.url.path or "")
    if path == "/" or path.startswith("/assets/") or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


CLIENT_DIST = Path(__file__).resolve().parent / "client" / "dist"
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PROJECT_IDS_ROOT = WORKSPACE_ROOT / "projects"
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


def load_config() -> AppConfig:
    data = read_json(CONFIG_PATH, {})
    config = AppConfig(**data) if data else AppConfig()
    canonical = canonical_source_selection(project_config_path=project_config_path())
    if config.default_find_selection != canonical:
        config = config.model_copy(update={"default_find_selection": canonical})
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
    project_path = project_config_path()
    if project_path is None:
        return
    project_config = read_json(project_path, {})
    if not isinstance(project_config, dict):
        return
    llm = dict(project_config.get("llm") or {}) if isinstance(project_config.get("llm"), dict) else {}
    llm["enabled"] = bool(str(config.api_key or "").strip() and str(config.model or "").strip() and str(config.provider or "").strip().lower() != "mock")
    llm["provider"] = str(config.provider or "openai_compatible")
    llm["api_base"] = str(config.base_url or "")
    llm["model"] = str(config.model or "")
    llm["api_key_env"] = str(llm.get("api_key_env") or os.environ.get("LLM_API_KEY_ENV") or "OPENAI_API_KEY")
    # Project state/API is frequently displayed to Claude and the browser. Keep
    # the editable secret in TASTE config; research jobs inject it into env at runtime.
    llm.pop("api_key", None)
    llm["timeout_sec"] = int(os.environ.get("LLM_TIMEOUT_SEC", llm.get("timeout_sec", 600) or 600))
    llm["max_tokens"] = int(os.environ.get("LLM_MAX_TOKENS", llm.get("max_tokens", 4000) or 4000))
    llm["temperature"] = float(config.temperature)
    llm["api_mode"] = str(os.environ.get("LLM_API_MODE", llm.get("api_mode", "chat_completions")) or "chat_completions")
    if project_config.get("llm") == llm:
        return
    project_config["llm"] = llm
    write_json(project_path, project_config)


def _current_project_research_preferences() -> dict[str, str]:
    project_path = project_config_path()
    if project_path is None:
        return {}
    project_config = read_json(project_path, {})
    if not isinstance(project_config, dict):
        return {}
    return {
        "research_interest": str(project_config.get("research_interest") or ""),
        "researcher_profile": str(project_config.get("researcher_profile") or ""),
    }


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
    return config.model_copy(update=updates) if updates else config


def _sync_project_research_preferences_from_config(config: AppConfig) -> None:
    project_path = project_config_path()
    if project_path is None:
        return
    project_config = read_json(project_path, {})
    if not isinstance(project_config, dict):
        return
    updates: dict[str, str] = {}
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


def save_config(config: AppConfig) -> AppConfig:
    canonical = save_canonical_source_selection(config.default_find_selection, project_config_path=project_config_path())
    config = config.model_copy(update={"default_find_selection": canonical})
    payload = config.model_dump()
    if read_json(CONFIG_PATH, {}) != payload:
        write_json(CONFIG_PATH, payload)
    _sync_project_llm_from_config(config)
    _sync_project_research_preferences_from_config(config)
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
    try:
        config_stat = CONFIG_PATH.stat() if CONFIG_PATH.exists() else None
    except OSError:
        config_stat = None
    project_llm: dict[str, Any] = {}
    project_path = project_config_path()
    if project_path is not None:
        project_config = read_json(project_path, {})
        if isinstance(project_config, dict) and isinstance(project_config.get("llm"), dict):
            project_llm = dict(project_config.get("llm") or {})
    api_key_value = str(data.get("api_key") or "")
    data["api_key_saved"] = bool(api_key_value.strip())
    data["api_key_suffix"] = api_key_value[-4:] if api_key_value else ""
    data["config_saved_at"] = datetime.fromtimestamp(config_stat.st_mtime, UTC).isoformat() if config_stat else ""
    data["config_path"] = ""
    data["project_llm_synced"] = bool(
        project_llm
        and str(project_llm.get("api_base") or "") == str(config.base_url or "")
        and str(project_llm.get("model") or "") == str(config.model or "")
    )
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
        points_to_article_artifact = "article.md" in lower
        pointer_verb = bool(re.search(r"(?:见|查看|打开|see|open|refer)", stripped, flags=re.I))
        duplicated_article_fields = bool(re.search(r"(?:摘要|推荐理由|完整|abstract|recommendation)", stripped, flags=re.I))
        legacy_metadata_line = bool(
            re.match(r"^(?:[-*]\s*)?(?:\*\*)?(?:id|url|pdf|fit(?:\s*分数)?|score|final\s*score|最终分数)(?:\*\*)?\s*[:：]\s*.*$", stripped, flags=re.I)
            or re.match(r"^(?:url|pdf)$", stripped, flags=re.I)
        )
        legacy_score_line = ("/" in stripped or "|" in stripped) and bool(re.search(r"(?:Fit|Score)\s*=", stripped, flags=re.I))
        if (points_to_article_artifact and pointer_verb and duplicated_article_fields) or legacy_metadata_line or legacy_score_line:
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
        sibling_root = re.escape(parent) + r"/[A-Z][A-Z0-9_]*(?=/(?:projects|runtime|modules)(?:/|$))"
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
        ("deterministic base-switch gate", "experiment evidence review"),
        ("deterministic base switch gate", "experiment evidence review"),
        ("base-switch gate", "experiment evidence review"),
        ("base_switch_gate", "experiment_evidence_review"),
        ("base_switch_execution", "experiment_evidence_receipt"),
        ("selected_base_viability_gate", "experiment_evidence_audit"),
        ("selected_base_viability", "experiment_evidence_audit"),
        ("environment_claude_code", "environment review"),
        ("claude_code_current_find_takeover", "current Find reading output"),
        ("waiting_for_environment_base_selection", "waiting_for_environment_review"),
        ("wait_for_environment_base_selection", "waiting_for_environment_review"),
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
    if _paper_preview_artifact_available(row) and status not in {"running", "queued", "cancelling", "error", "cancelled"}:
        return "preview_available"
    return status



def _is_full_cycle_job(stage: Any = "", job_id: Any = "", result: Any = None, logs: Any = None) -> bool:
    stage_text = str(stage or "").lower().replace("_", "-")
    job_id_text = str(job_id or "").lower().replace("_", "-")
    if job_id_text.startswith("current-find-worker") or "current-find" in stage_text or "current-find" in job_id_text:
        return False
    if isinstance(result, dict):
        kind_text = str(result.get("kind") or result.get("raw_stage") or "").lower().replace("_", "-")
        cmd_text = str(result.get("cmd") or result.get("command") or "").lower().replace("_", "-")
        if kind_text.startswith("current-find") or "ensure-current-find-research-plan.py" in cmd_text or ("claude-project-session.py" in cmd_text and "current-find" in cmd_text):
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
    lowered = str(cmd or "").lower()
    if "ensure_current_find_research_plan.py" in lowered:
        return "read", "current_find_read_idea_plan_wrapper", 2
    if "claude_project_session.py" in lowered and "current-find-claude-read-idea-plan" in lowered:
        return "read", "current_find_claude_read_idea_plan", 2
    if "claude_project_session.py" in lowered and "current-find" in lowered:
        return "read", "current_find_claude_session", 2
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


def _normalize_project_agent_panel_stage(value: Any) -> str:
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


def _panel_stage_from_project_agent_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ["panel_stage", "requested_stage", "stage"]:
        normalized = _normalize_project_agent_panel_stage(result.get(key))
        if normalized:
            return normalized
    return ""


def _initial_project_agent_job_result(payload: Any, stage: Any) -> dict[str, Any]:
    row = payload if isinstance(payload, dict) else {}
    action = str(row.get("action") or stage or "").strip()
    requested_stage = str(row.get("stage") or "").strip()
    panel_stage = _normalize_project_agent_panel_stage(requested_stage)
    if not panel_stage:
        panel_stage = _normalize_project_agent_panel_stage(action)
    if action not in {"claude-message", "agent-guidance"} and not panel_stage:
        return {}
    return {
        "project": str(row.get("project") or "").strip(),
        "action": action,
        "agent_id": str(row.get("agent_id") or "main").strip() or "main",
        "requested_stage": requested_stage,
        "panel_stage": panel_stage,
        "status": "running",
    }


def _is_project_agent_panel_job(stage: Any = "", job_id: Any = "", result: Any = None) -> bool:
    parts = [str(stage or ""), str(job_id or "")]
    if isinstance(result, dict):
        parts.extend(str(result.get(key) or "") for key in ["action", "kind", "raw_stage"])
    haystack = "\n".join(parts).lower().replace("_", "-")
    action = str(result.get("action") or "").strip().lower().replace("_", "-") if isinstance(result, dict) else ""
    return action in {"claude-message", "agent-guidance"} or any(marker in haystack for marker in [
        "claude-message",
        "claude-message",
        "project-agent-guidance",
        "agent-guidance",
    ])


def _is_paper_job(stage: Any = "", job_id: Any = "", result: Any = None, logs: Any = None) -> bool:
    if _is_project_agent_panel_job(stage, job_id, result):
        return _panel_stage_from_project_agent_result(result) == "paper"
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


def _project_for_web_job_id(job_id: Any) -> str:
    target = str(job_id or "").strip()
    if not target or not PROJECT_IDS_ROOT.exists():
        return ""
    state_files = [
        Path("state/full_cycle_job.json"),
        Path("state/full_research_cycle.json"),
        Path("paper/metadata/paper_pipeline.json"),
    ]
    for root in sorted(PROJECT_IDS_ROOT.iterdir()):
        if not root.is_dir():
            continue
        for rel_path in state_files:
            payload = _read_project_json(root / rel_path, {})
            if not isinstance(payload, dict):
                continue
            nested = payload.get("full_cycle_job") if isinstance(payload.get("full_cycle_job"), dict) else {}
            ids = [payload.get("web_job_id"), payload.get("job_id"), payload.get("id"), nested.get("web_job_id"), nested.get("job_id")]
            if any(str(value or "").strip() == target for value in ids):
                return root.name
    return ""


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
        panel_stage = _panel_stage_from_project_agent_result(result)
        if panel_stage:
            return panel_stage
        if _is_paper_job(live_job.get("stage"), live_job.get("job_id") or job_id, result, live_job.get("logs")):
            return "paper"
        phase = str(result.get("phase") or live_job.get("stage") or "").strip().lower()
        if phase in {"paper", "experiment", "environment", "literature"}:
            return phase
    if known_job is not None:
        known_result = known_job.result if isinstance(getattr(known_job, "result", None), dict) else {}
        panel_stage = _panel_stage_from_project_agent_result(known_result)
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


def _public_taste_stage(stage: Any) -> str:
    """Map internal research job labels to the seven public workflow stages."""
    raw = str(stage or '').strip()
    lowered = raw.lower().replace('_', '-')
    if lowered == 'plan-polish':
        return 'plan'
    if lowered == 'email':
        return 'paper'
    if lowered == 'paper' or lowered.startswith('paper-') or lowered.startswith('paper_'):
        return 'paper'
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
    fresh_find_markers = ['literature-survey', 'run-finding', 'run-driver', 'run-literature-tool', 'find-repair-current']
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        job = cls(str(data.get("job_id") or "job_unknown"), str(data.get("stage") or "unknown"))
        job.status = str(data.get("status") or "queued")
        job.created_at = str(data.get("created_at") or job.created_at)
        job.logs = [str(line) for line in data.get("logs", [])]
        job.result = data.get("result")
        job.internal = bool(data.get("internal"))
        job.display = str(data.get("display") or "")
        if job.stage == "safe-unblock" or job.job_id.startswith("safe-unblock_") or job.stage == "find-repair-current" or job.job_id.startswith("find-repair-current_"):
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
        if job.status in {"done", "error", "cancelled", "blocked"}:
            job.done.set()
        return job

    def log(self, message: str) -> None:
        line = str(message)
        if not self.run_id:
            match = re.search(r"Created run\s+(\S+)", line)
            if match:
                self.run_id = match.group(1)
        self.logs.append(line)
        _persist_jobs()

    def as_dict(self, *, compact: bool = False) -> dict:
        result_payload = _compact_job_result(self.result, self.stage, self.job_id, self.logs) if compact else self.result
        panel_stage = _panel_stage_from_project_agent_result(result_payload if isinstance(result_payload, dict) else self.result)
        paper_job = _is_paper_job(self.stage, self.job_id, result_payload if isinstance(result_payload, dict) else self.result, self.logs)
        public_stage = panel_stage or ("paper" if paper_job else _public_taste_stage(self.stage))
        progress_payload = self.progress
        if compact and paper_job and public_stage == "paper" and isinstance(result_payload, dict):
            progress_payload = dict(self.progress if isinstance(self.progress, dict) else {})
            paper_summary = str(result_payload.get("paper_summary") or "").strip()
            if paper_summary:
                progress_payload["message"] = paper_summary
                progress_payload["phase"] = str(result_payload.get("status") or progress_payload.get("phase") or "paper")
        log_stage = panel_stage or ("paper" if paper_job else self.stage)
        logs = _public_job_logs(log_stage, self.logs, progress_payload, result_payload if isinstance(result_payload, dict) else self.result, limit=80) if compact else self.logs
        if public_stage != self.stage and isinstance(self.result, dict):
            self.result.setdefault("raw_stage", self.stage)
        payload = {
            "job_id": self.job_id,
            "stage": public_stage,
            "status": _public_paper_status(self.status, result_payload if isinstance(result_payload, dict) else {}) if paper_job and public_stage == "paper" else self.status,
            "created_at": self.created_at,
            "logs": logs,
            "log_count": len(self.logs),
            "run_id": self.run_id,
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
        if self.status in {"done", "error", "cancelled", "blocked"}:
            return
        self.cancel_requested = True
        self.status = "cancelling"
        self.log("Cancellation requested.")
        _persist_jobs()

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
LIVE_JOBS_TTL_SEC = float(os.environ.get("LIVE_JOBS_TTL_SEC", "2.0") or 2.0)
_LIVE_JOBS_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": []}
RUN_ARTIFACTS_CACHE_TTL_SEC = float(os.environ.get("RUN_ARTIFACTS_CACHE_TTL_SEC", "10.0") or 10.0)
RUNS_CACHE_TTL_SEC = float(os.environ.get("RUNS_CACHE_TTL_SEC", "10.0") or 10.0)
_RUN_ARTIFACTS_CACHE: dict[str, Any] = {}
_RUNS_CACHE: dict[str, Any] = {"expires_at": 0.0, "fingerprint": None, "items": []}

MARKDOWN_ARTIFACT_NAMES = [
    "article.md", "screened_ranking.md", "read_candidates.md", "critique_candidates.md",
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
LIGHT_ARTIFACT_MARKDOWN_NAMES = ["article.md", "source_status.md", "read.md", "idea.md", "plan.md"]
LIGHT_ARTIFACT_JSON_NAMES = ["find_progress.json", "read_results.json", "ideas.json", "plans.json", "selection.json"]


def _runs_fingerprint() -> tuple[int, int, int]:
    try:
        root_stat = RUNS_DIR.stat()
        count = 0
        newest_manifest = 0
        for item in RUNS_DIR.iterdir():
            if not item.is_dir():
                continue
            count += 1
            try:
                newest_manifest = max(newest_manifest, (item / "manifest.json").stat().st_mtime_ns)
            except OSError:
                continue
        return (root_stat.st_mtime_ns, count, newest_manifest)
    except OSError:
        return (0, 0, 0)


def _run_stage_names_from_artifacts(path_value: Any, existing: Any = None) -> list[str]:
    stages = [str(item) for item in (existing if isinstance(existing, list) else []) if str(item or "").strip()]
    path = Path(str(path_value or ""))
    if not path.exists():
        return stages
    checks = [
        ("find", ["find_results.json", "article.md"]),
        ("read", ["read_results.json", "read.md"]),
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
        _RUN_ARTIFACTS_CACHE.pop(run_id, None)
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
    target = venue or _project_configured_venue(cfg)
    try:
        state = get_active_paper_state(project, venue=target)
    except Exception:
        state = _read_project_json(root / 'paper' / 'metadata' / 'paper_pipeline.json', {})
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


def _paper_public_self_review_evidence_projection(category: str, detail: str) -> dict[str, str]:
    marker = f"{category} {detail}".lower()
    if "missing_empirical_validation" in marker or "zero empirical" in marker or "untested architecture" in marker:
        return {
            "public_title": "缺少新方法实验验证",
            "public_summary": "当前论文预览还没有用同一数据、同一 seed、同一指标验证拟议新方法；已有数字只适合作为参考基底校准或初始化检查。",
            "public_next_action": "继续由项目代理执行候选方法、基线和关键消融实验，写入可审计指标后再刷新论文。",
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
        "public_next_action": "让项目代理根据审稿 receipt 继续修实验、证据和论文，再刷新审计。",
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
    pdf_candidates = [root / "paper" / "output" / venue / "paper.pdf"]
    pdf_path = next((item for item in pdf_candidates if item.exists()), None)
    tex_path = (root / "paper" / "output" / venue / "paper.tex")
    policy = paper_state.get("venue_submission_policy") if isinstance(paper_state.get("venue_submission_policy"), dict) else {}
    blockers = paper_state.get("conference_preview_blockers") if isinstance(paper_state.get("conference_preview_blockers"), list) else []
    self_review_blockers = paper_state.get("paper_self_review_blockers") if isinstance(paper_state.get("paper_self_review_blockers"), list) else []
    self_review_evidence_blockers = paper_state.get("paper_self_review_evidence_blockers") if isinstance(paper_state.get("paper_self_review_evidence_blockers"), list) else []
    raw_warnings = paper_state.get("paper_layout_footprint_warnings") if isinstance(paper_state.get("paper_layout_footprint_warnings"), list) else []
    warnings = [item for item in (_paper_public_layout_warning_text(value) for value in raw_warnings) if item]
    first = blockers[0] if blockers else ""
    raw_blocker_text = str(first.get("public_detail") or first.get("detail") or first.get("id") or "") if isinstance(first, dict) else str(first or "")
    blocker_text = _paper_public_blocker_text(raw_blocker_text)
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
        diagnostics.append("Claude Code 独立审稿 receipt 未通过；系统会继续阻塞预览，直到项目代理读 PDF/TeX/BibTeX/log/venue contract 后写入当前产物自审 receipt。")
    if self_review_evidence_blockers:
        diagnostics.append(f"Claude Code 独立审稿发现 {len(self_review_evidence_blockers)} 项未解决科研证据问题；PDF 只能作为检查预览，不能标记为投稿通过。")
    row = {
        "status": "preview_available" if pdf_path else ("needs_writing" if blockers or not paper_state.get("conference_preview_ready") else str(paper_state.get("status") or "preview_available")),
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
        "paper_citation_render_blockers": [
            {**item, "detail": _paper_public_blocker_text(str(item.get("id") or "") + ": " + str(item.get("public_detail") or item.get("detail") or "")), "public_detail": _paper_public_blocker_text(str(item.get("id") or "") + ": " + str(item.get("public_detail") or item.get("detail") or ""))}
            for item in (paper_state.get("paper_citation_render_blockers", []) if isinstance(paper_state.get("paper_citation_render_blockers", []), list) else [])[:8] if isinstance(item, dict)
        ],
        "paper_self_review_status": paper_state.get("paper_self_review_status", ""),
        "paper_self_review_ready": bool(paper_state.get("paper_self_review_ready")),
        "paper_self_review_receipt": paper_state.get("paper_self_review_receipt", ""),
        "paper_self_review_blockers": [
            {**item, "detail": _paper_public_blocker_text(str(item.get("id") or "") + ": " + str(item.get("public_detail") or item.get("detail") or "")), "public_detail": _paper_public_blocker_text(str(item.get("id") or "") + ": " + str(item.get("public_detail") or item.get("detail") or ""))}
            for item in self_review_blockers[:8] if isinstance(item, dict)
        ],
        "paper_self_review_evidence_blockers": [_paper_public_self_review_evidence_blocker(item) for item in self_review_evidence_blockers[:8]],
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
        "conference_preview_blockers": [
            {**item, "detail": _paper_public_blocker_text(item.get("public_detail") or item.get("detail") or item.get("id") or ""), "public_detail": _paper_public_blocker_text(item.get("public_detail") or item.get("detail") or item.get("id") or "")}
            for item in blockers[:8] if isinstance(item, dict)
        ],
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
        "blocked_preview_available": bool(pdf_path),
        "blocked_pdf_path": str(pdf_path) if pdf_path else "",
        "blocked_tex_path": str(tex_path) if tex_path.exists() else "",
        "latest_generated_pdf_path": str(pdf_path) if pdf_path else "",
        "raw_pdf_path": str(pdf_path) if pdf_path else "",
        "venue_submission_policy": policy,
    }
    row["summary"] = _paper_stage_job_message(row)
    return row


def _paper_stage_from_job_result(result: dict[str, Any]) -> dict[str, Any]:
    project = str(result.get("project") or "")
    snapshot = _paper_stage_from_project_snapshot(project) if project else {}
    direct = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else {}
    if snapshot:
        merged = dict(direct)
        merged.update(snapshot)
        return merged
    if direct:
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
    if row.get("blocked_preview_available") or row.get("raw_pdf_path") or row.get("pdf_path"):
        parts.append(_paper_venue_labels(row).get("preview_zh", "会议格式论文预览") + "已生成")
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
    if blocker:
        if "reference_count" in blocker or "reference_quality_target" in blocker or "references/citation" in blocker:
            parts.append("写作质量目标未达：参考文献覆盖不足")
        elif "参考文献覆盖不足" in blocker:
            parts.append("写作质量目标未达：参考文献覆盖不足")
        else:
            parts.append("预览仍需完善：" + blocker)
    self_review_blockers = row.get("paper_self_review_blockers") if isinstance(row.get("paper_self_review_blockers"), list) else []
    self_review_status = str(row.get("paper_self_review_status") or "").strip().lower()
    if self_review_blockers or self_review_status == "block":
        parts.append("Claude Code 自审未通过，项目代理需独立读 PDF/TeX/BibTeX/log/venue contract 后修复并写 receipt")
    self_review_evidence_blockers = row.get("paper_self_review_evidence_blockers") if isinstance(row.get("paper_self_review_evidence_blockers"), list) else []
    if self_review_evidence_blockers:
        parts.append(f"Claude Code 自审发现 {len(self_review_evidence_blockers)} 项未解决科研证据问题，预览不能标记为投稿通过")
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
            "blocked_tex_path", "latest_generated_pdf_path", "raw_pdf_path",
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
        "raw_pdf_path",
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


def _public_full_cycle_job_logs(logs: Any, progress: Any = None, result: Any = None, *, limit: int = 40) -> list[str]:
    raw = [str(line or "").strip() for line in (logs if isinstance(logs, list) else []) if str(line or "").strip()]
    progress = progress if isinstance(progress, dict) else {}
    result = result if isinstance(result, dict) else {}
    out: list[str] = []
    project = str(result.get("project") or "").strip()
    if project:
        out.append("project=" + project)
    status = str(result.get("status") or progress.get("phase") or "").strip()
    process_alive = result.get("process_alive")
    if process_alive is not None:
        out.append("process_alive=" + str(bool(process_alive)).lower())
    message = str(progress.get("message") or result.get("summary") or "").strip()
    if message:
        if result.get("process_alive") is not True and any(marker in message.lower() for marker in ["gate=", "候选路线", "独立授权", "base_switch", "selected_base", "deterministic"]):
            message = "历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。"
        elif "正在运行" in message and result.get("process_alive") is not True:
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
    for line in raw:
        low = line.lower()
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


def _public_project_agent_progress_message(stage: Any, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    command = _public_stage_command_message(stage, text)
    if command:
        return command
    lowered = text.lower()
    stage_label = _public_stage_label(stage)
    if lowered.startswith("running claude-message") or lowered.startswith("claude-message started"):
        return f"项目代理正在处理{stage_label}请求。"
    if lowered.startswith("claude: executable=") or lowered.startswith("claude: permission_mode") or lowered.startswith("claude: session_key="):
        return f"项目代理会话已启动，正在处理{stage_label}请求。"
    if "调用工具" in text or "tool use" in lowered or "read file=" in lowered or "edit file=" in lowered or "bash command=" in lowered:
        return f"项目代理正在读取/修改当前项目证据以处理{stage_label}门控。"
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

    if public_stage == "paper" or raw_stage.startswith("paper"):
        out: list[str] = []
        message_source = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else result
        result_summary = _paper_stage_job_message(message_source if isinstance(message_source, dict) else result).strip()
        message = _public_paper_progress_message(progress.get("message") or "")
        phase = str(progress.get("phase") or "").strip()
        if result_summary:
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
    directory = run_dir(run_id)
    result = dict(job.result)
    result["run_id"] = run_id
    result.setdefault("artifact_dir", str(directory))
    result.setdefault("artifact_paths", {
        "find_results": str(directory / "find_results.json"),
        "article": str(directory / "article.md"),
        "source_status": str(directory / "source_status.md"),
        "read_candidates": str(directory / "read_candidates.md"),
        "critique_candidates": str(directory / "critique_candidates.md"),
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
        "read_candidates", "triage_candidates", "audit_candidates", "critique_candidates", "articles",
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
        "id", "title", "venue", "venue_id", "year", "url", "pdf_url",
        "fit_score", "llm_fit_score", "diversity_score", "score", "recommendation_score", "recommendation_score_v2",
        "taste_pool", "taste_pool_role", "hit_directions", "hit_directions_zh", "hit_directions_en",
        "abstract", "abstract_zh", "abstract_en",
        "reason", "reason_zh", "reason_en", "fit_explanation", "fit_explanation_zh", "fit_explanation_en",
    ]
    out = {key: row.get(key) for key in keys if key in row}
    for key in ["abstract", "abstract_zh", "abstract_en", "reason", "reason_zh", "reason_en", "fit_explanation", "fit_explanation_zh", "fit_explanation_en", "recommendation_note", "recommendation_note_zh", "recommendation_note_en"]:
        if key in out:
            out[key] = _artifact_compact_text(out[key], 650)
    if "title" in out:
        out["title"] = _artifact_compact_text(out["title"], 220)
    return out


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
    "article.md", "read.md", "idea.md", "plan.md", "source_status.md",
    "find_progress.json", "find_results.json", "read_results.json", "ideas.json", "plans.json",
}


def _payload_run_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("run_id") or value.get("source_run_id") or value.get("find_run_id") or value.get("current_find_run_id") or "").strip()


def _project_current_find_run_id(project_root: Path) -> str:
    for rel in [
        "planning/finding/find_progress.json",
        "planning/finding/find_results.json",
        "state/current_find_research_plan.json",
        "state/current_find_claude_reading_validation.json",
        "planning/finding/read_results.json",
        "planning/finding/ideas.json",
        "planning/finding/plans.json",
    ]:
        payload = _read_project_json(project_root / rel, {})
        run_id = _payload_run_id(payload)
        if run_id:
            return run_id
    return ""


def _project_taste_artifact_path(project_root: Path | None, run_id: str, name: str) -> Path | None:
    if project_root is None or name not in PROJECT_ARTIFACT_NAMES:
        return None
    candidate = project_root / "planning" / "finding" / name
    if not candidate.exists():
        return None
    expected = str(run_id or "").strip()
    if not expected:
        return None
    if name.endswith(".json"):
        payload = _read_project_json(candidate, {})
        actual = _payload_run_id(payload)
        if actual and actual != expected:
            return None
        if name in {"read_results.json", "ideas.json", "plans.json", "find_results.json"} and not actual:
            return None
        return candidate
    current = _project_current_find_run_id(project_root)
    return candidate if current == expected else None


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


def _compact_large_find_results_artifact(directory: Path, run_id: str, size_bytes: int) -> dict[str, Any]:
    progress = read_json(directory / "find_progress.json", {})
    payload: dict[str, Any] = {
        "run_id": run_id,
        "content_truncated": True,
        "artifact_size_bytes": size_bytes,
        "truncation_reason": "find_results.json is large; API returns compact sidecar state plus current strong-paper rows so polling cannot block the web worker.",
    }
    if isinstance(progress, dict):
        for key in ["phase", "counts", "strong_recommendation_count", "recommendation_target_count", "recommendation_shortfall", "source_status", "venue_health_report", "selection", "live_progress", "updated_at", "generated_at"]:
            if key in progress:
                payload[key] = progress.get(key)
    project_root = _project_root_for_find_run(run_id)
    if project_root is not None:
        packet = _read_project_json(project_root / "state" / "literature_tool_packet.json", {})
        frontend_state = _read_project_json(project_root / "state" / "finding_frontend.json", {})
        current_plan = _read_project_json(project_root / "state" / "current_find_research_plan.json", {})
        projection = _current_find_recommendation_projection(project_root, run_id)
        if isinstance(frontend_state, dict) and isinstance(frontend_state.get("survey_stats"), dict):
            payload.setdefault("survey_stats", frontend_state.get("survey_stats"))
        if isinstance(projection, dict) and projection:
            recommendation_rows = projection.get("strong_recommendations") if isinstance(projection.get("strong_recommendations"), list) else projection.get("recommendations") if isinstance(projection.get("recommendations"), list) else projection.get("articles") if isinstance(projection.get("articles"), list) else []
            read_rows = projection.get("read_candidates") if isinstance(projection.get("read_candidates"), list) else recommendation_rows
            compact_recommendation_limit = max(len(recommendation_rows), len(read_rows), 1)
            if recommendation_rows:
                compact_rows = [_artifact_compact_paper_row(row) for row in recommendation_rows[:compact_recommendation_limit] if isinstance(row, dict)]
                payload["strong_recommendations"] = compact_rows
                payload["articles"] = compact_rows
            if read_rows:
                payload["read_candidates"] = [_artifact_compact_paper_row(row) for row in read_rows[:compact_recommendation_limit] if isinstance(row, dict)]
            triage_rows = projection.get("triage_candidates") if isinstance(projection.get("triage_candidates"), list) else projection.get("audit_candidates") if isinstance(projection.get("audit_candidates"), list) else []
            if triage_rows:
                payload["triage_candidates"] = [_artifact_compact_paper_row(row) for row in triage_rows[:50] if isinstance(row, dict)]
                payload["audit_candidates"] = payload["triage_candidates"]
            counts = projection.get("counts") if isinstance(projection.get("counts"), dict) else {}
            if counts:
                merged_counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
                merged_counts.update(counts)
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
        if isinstance(current_plan, dict) and not payload.get("read_candidates"):
            readings = current_plan.get("readings") if isinstance(current_plan.get("readings"), list) else []
            if readings:
                payload["read_candidates"] = [_artifact_compact_paper_row(row) for row in readings[:max(len(readings), 1)] if isinstance(row, dict)]
    return _strip_public_taste_marker(payload)


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
            "goal": "让项目 Claude Code/TASTE 补齐候选路线的来源、数据加载、协议、冒烟、完整参考复现和本地产物审计证据。",
            "next": top_action or "继续补齐候选路线取证，同时保持当前主线基底不变；任何实验结果进入论文前都必须先完成本地审计和下游门控刷新。",
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
            "goal": "保持当前基底不变；旧路线只作为历史对照，不作为当前主线证据。",
            "next": "继续当前主线的真实候选实验；产出本地审计记录和实验登记后，再刷新科学进展、论文证据、投稿准备度与阻塞行动计划门控。",
        }
    if issue_text.strip():
        return {
            "gate": "科研门控阻塞",
            "issue": issue_text.strip()[:500],
            "goal": "等待 project agent 根据项目状态继续处理。",
            "next": next_text.strip()[:500] or "继续运行 TASTE 安全检查点并刷新门控。",
        }
    return {
        "gate": str(cycle_status or "科研门控").replace("_", " "),
        "issue": "当前科研门控未通过；原始证据保留在 state/report 产物中。",
        "goal": "等待 project agent 根据项目状态继续处理。",
        "next": next_text.strip()[:500] or "继续运行 TASTE 安全检查点并刷新门控。",
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _find_adoption_gate_metrics(run_id: str, result_dict: Any) -> dict[str, Any]:
    payload = result_dict if isinstance(result_dict, dict) else {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    scoring_runtime = payload.get("scoring_runtime") if isinstance(payload.get("scoring_runtime"), dict) else {}
    progress_payload: dict[str, Any] = {}
    run_id = str(run_id or "").strip()
    if run_id:
        try:
            progress = read_json(run_dir(run_id) / "find_progress.json", {})
            if isinstance(progress, dict):
                progress_payload = progress
        except Exception:
            progress_payload = {}
    target_count = max(
        _as_int(payload.get("recommendation_target_count"), 0),
        _as_int(scoring_runtime.get("recommendation_target_count"), 0),
        _as_int(progress_payload.get("recommendation_target_count"), 0),
        _as_int(counts.get("recommendation_target_count"), 0),
    )
    shortfall_values = [
        _as_int(payload.get("recommendation_shortfall"), -1),
        _as_int(scoring_runtime.get("recommendation_shortfall"), -1),
        _as_int(progress_payload.get("recommendation_shortfall"), -1),
        _as_int(counts.get("recommendation_shortfall"), -1),
    ]
    shortfall = next((value for value in shortfall_values if value >= 0), -1)
    recommendations = payload.get("strong_recommendations") or payload.get("articles") or []
    strong_count = len(recommendations) if isinstance(recommendations, list) else 0
    strong_count = max(
        strong_count,
        _as_int(payload.get("strong_recommendation_count"), 0),
        _as_int(scoring_runtime.get("strict_strong_anchor_count"), 0),
        _as_int(scoring_runtime.get("recommendation_actual_count"), 0),
        _as_int(progress_payload.get("strong_recommendation_count"), 0),
        _as_int(progress_payload.get("strict_strong_anchor_count"), 0),
        _as_int(counts.get("strong_recommendations"), 0),
        _as_int(counts.get("recommended"), 0),
    )
    if shortfall < 0 and target_count:
        shortfall = max(0, target_count - strong_count)
    elif shortfall < 0:
        shortfall = 0
    result_status = str(payload.get("status") or progress_payload.get("phase") or "").lower()
    return {
        "strong_count": strong_count,
        "target_count": target_count,
        "shortfall": shortfall,
        "status": result_status,
        "progress_phase": str(progress_payload.get("phase") or ""),
    }


def _current_project_for_find_guard() -> tuple[str, Path] | None:
    try:
        cfg_path = project_config_path()
    except Exception:
        cfg_path = None
    if cfg_path:
        root = Path(cfg_path).resolve().parent
        cfg = _read_project_json(Path(cfg_path), {})
        project = str((cfg if isinstance(cfg, dict) else {}).get("id") or root.name).strip()
        if project and (root / "state").exists():
            return project, root
    try:
        projects = list_projects()
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



def _find_artifact_run_dir_for_project(root: Path, run_id: str) -> Path:
    run_id = str(run_id or "").strip()
    if run_id:
        try:
            directory = run_dir(run_id)
            if (directory / "find_results.json").exists():
                return directory
        except Exception:
            pass
    taste_find = root / "planning" / "finding" / "find_results.json"
    if taste_find.exists():
        return taste_find.parent
    raise FileNotFoundError(f"Current Find results not found for run_id={run_id}")


def _find_selection_source_count(selection: Any) -> int:
    if not isinstance(selection, dict):
        return 1
    pairs = []
    seen_pairs: set[tuple[str, int]] = set()
    for item in selection.get("venue_years") or []:
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
            if key not in seen_pairs:
                seen_pairs.add(key)
                pairs.append(key)
    venues = selection.get("venue_ids") or selection.get("venues") or []
    years = selection.get("years") or []
    if pairs:
        count = len(pairs)
    elif isinstance(venues, list):
        year_count = len(years) if isinstance(years, list) and years else 1
        count = len(venues) * max(1, year_count)
    else:
        count = 0
    for name in ("include_arxiv", "include_biorxiv", "include_huggingface", "include_github", "include_nature", "include_science"):
        if selection.get(name):
            count += 1
    return max(1, count)

def _strict_find_projection_config(config: AppConfig) -> AppConfig:
    updates: dict[str, Any] = {}
    if not str(config.provider or "").strip() or str(config.provider or "").lower() == "mock":
        updates["provider"] = "openai"
    if not str(config.api_key or "").strip():
        updates["api_key"] = "repair-current-local-gate"
    if not str(config.model or "").strip():
        updates["model"] = "repair-current-local-gate"
    return config.model_copy(update=updates) if updates else config


def _reproject_find_results_with_current_contract(find_results: dict[str, Any], config: AppConfig, source: str) -> dict[str, Any]:
    """Rebuild user-visible Find pools from evaluated candidates.

    This does not score or translate papers. It only applies the currently loaded
    Find recommendation contract so stale runs cannot keep audit-only rows in
    articles/read_candidates after the framework gate changes.
    """
    if not isinstance(find_results, dict):
        return {"status": "skipped_invalid_find_results"}
    evaluated = find_results.get("evaluated_candidates")
    if not isinstance(evaluated, list) or not evaluated:
        return {"status": "skipped_missing_evaluated_candidates"}
    gate_config = _strict_find_projection_config(config)
    source_count = _find_selection_source_count(find_results.get("selection"))
    previous = find_results.get("strong_recommendations") if isinstance(find_results.get("strong_recommendations"), list) else find_results.get("articles") if isinstance(find_results.get("articles"), list) else []
    for row in evaluated:
        if not isinstance(row, dict):
            continue
        for key in ("find_recommendation", "recommended_by_llm_ranking", "_user_visible_recommendation"):
            row.pop(key, None)
    recommendations = _recommended(evaluated, gate_config, source_count=source_count)
    recommendation_ids = {str(row.get("id") or row.get("url") or row.get("title") or "") for row in recommendations if isinstance(row, dict)}
    for row in evaluated:
        if isinstance(row, dict) and str(row.get("id") or row.get("url") or row.get("title") or "") not in recommendation_ids:
            row.pop("_user_visible_recommendation", None)
    for row in recommendations:
        if isinstance(row, dict):
            row["_user_visible_recommendation"] = True
    screened = _screened_ranking(evaluated, gate_config)
    triage = _triage_candidates(evaluated, gate_config)
    critique = _critique_candidates(evaluated, gate_config)
    quality = _recommendation_quality_audit(recommendations)
    scoring_runtime = find_results.setdefault("scoring_runtime", {})
    if not isinstance(scoring_runtime, dict):
        scoring_runtime = {}
        find_results["scoring_runtime"] = scoring_runtime
    target_count = int(scoring_runtime.get("recommendation_target_count") or find_results.get("recommendation_target_count") or max(1, source_count) * 5)
    shortfall = max(0, target_count - len(recommendations))
    find_results["scoring_policy_version"] = SCORING_POLICY_VERSION
    find_results["strong_recommendations"] = recommendations
    find_results["articles"] = recommendations
    find_results["read_candidates"] = recommendations
    find_results["screened_ranking"] = screened
    find_results["triage_candidates"] = triage
    find_results["audit_candidates"] = triage
    find_results["critique_candidates"] = critique
    find_results["recommendation_quality"] = quality
    find_results["strict_strong_anchor_count"] = len(recommendations)
    find_results["recommendation_target_count"] = target_count
    find_results["recommendation_shortfall"] = shortfall
    find_results["recommendation_policy"] = FIND_RECOMMENDATION_POLICY
    scoring_runtime.update({
        "recommendation_quality": quality,
        "recommendation_actual_count": len(recommendations),
        "strict_strong_anchor_count": len(recommendations),
        "recommendation_target_count": target_count,
        "recommendation_shortfall": shortfall,
        "recommendation_policy": find_results["recommendation_policy"],
        "scoring_policy_version": SCORING_POLICY_VERSION,
        "reprojected_with_current_contract_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "reprojected_with_current_contract_source": source,
    })
    return {
        "status": "reprojected",
        "source": source,
        "previous_recommendation_count": len(previous),
        "recommendation_count": len(recommendations),
        "recommendation_target_count": target_count,
        "recommendation_shortfall": shortfall,
        "source_count": source_count,
    }

def _sync_current_find_projection(root: Path, run_id: str, find_results: dict[str, Any], source: str) -> dict[str, Any]:
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    recommendations = find_results.get("strong_recommendations") if isinstance(find_results.get("strong_recommendations"), list) else find_results.get("articles") if isinstance(find_results.get("articles"), list) else []
    scoring_runtime = find_results.get("scoring_runtime") if isinstance(find_results.get("scoring_runtime"), dict) else {}
    progress = read_json(_find_artifact_run_dir_for_project(root, run_id) / "find_progress.json", {})
    if not isinstance(progress, dict):
        progress = {}
    missing_zh = []
    for index, row in enumerate(recommendations, 1):
        if isinstance(row, dict) and str(row.get("abstract") or row.get("abstract_en") or "").strip() and not str(row.get("abstract_zh") or "").strip():
            missing_zh.append({"rank": index, "id": str(row.get("id") or ""), "title": str(row.get("title") or "")})
    target_count = int(progress.get("recommendation_target_count") or scoring_runtime.get("recommendation_target_count") or len(recommendations) or 0)
    shortfall = max(0, target_count - len(recommendations)) if target_count else 0
    projection = {
        "run_id": run_id,
        "source_run_id": run_id,
        "source": source,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "strong_recommendations": recommendations,
        "articles": recommendations,
        "read_candidates": recommendations,
        "counts": {"recommended": len(recommendations), "read_candidates": len(recommendations), "strict_strong_anchor_count": len(recommendations)},
        "strict_strong_anchor_count": len(recommendations),
        "recommendation_target_count": target_count,
        "recommendation_shortfall": shortfall,
        "recommendation_quality": find_results.get("recommendation_quality") or scoring_runtime.get("recommendation_quality") or {},
        "abstract_translation_status": progress.get("abstract_translation_status") or scoring_runtime.get("abstract_translation_status") or "",
        "missing_recommendation_abstract_zh": missing_zh,
    }
    write_json(state_dir / "current_find_recommendation_projection.json", projection)
    return projection


def _current_find_artifact_row_title(row: Any) -> str:
    if not isinstance(row, dict):
        return str(row or "").strip()
    return str(row.get("title") or row.get("paper_title") or row.get("id") or row.get("paper_id") or "").strip()


def _current_find_artifact_row_key(row: Any) -> str:
    if not isinstance(row, dict):
        return str(row or "").strip()
    return str(row.get("id") or row.get("paper_id") or row.get("url") or row.get("pdf_url") or row.get("title") or row.get("paper_title") or "").strip()


def _current_find_read_reset_reason(root: Path, run_id: str, recommendations: list[dict[str, Any]]) -> str:
    taste_dir = root / "planning" / "finding"
    read_payload = _read_project_json(taste_dir / "read_results.json", {})
    if not isinstance(read_payload, dict):
        return "missing_or_invalid_current_find_read_results"
    if str(read_payload.get("run_id") or "").strip() != run_id:
        return "read_results_run_id_mismatch"
    if str(read_payload.get("source") or "").strip() == "pending_new_find_read" and str(read_payload.get("status") or "").strip() == "pending":
        return ""
    readings = read_payload.get("readings") if isinstance(read_payload.get("readings"), list) else []
    if len(readings) != len(recommendations):
        return "read_results_recommendation_count_mismatch"
    expected_keys = {_current_find_artifact_row_key(row) for row in recommendations if _current_find_artifact_row_key(row)}
    reading_keys = {_current_find_artifact_row_key(row) for row in readings if _current_find_artifact_row_key(row)}
    if expected_keys and reading_keys and expected_keys != reading_keys:
        return "read_results_recommendation_set_mismatch"
    expected_titles = {_current_find_artifact_row_title(row) for row in recommendations if _current_find_artifact_row_title(row)}
    reading_titles = {_current_find_artifact_row_title(row) for row in readings if _current_find_artifact_row_title(row)}
    if expected_titles and reading_titles and expected_titles != reading_titles:
        return "read_results_recommendation_title_mismatch"
    return ""


def _reset_current_find_downstream_artifacts(root: Path, run_id: str, recommendations: list[dict[str, Any]], source: str, reason: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    taste_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    safe_now = now.replace(":", "").replace("-", "").replace(".", "_")
    backup_root = state_dir / "current_find_artifact_backups" / f"repair_current_find_reset_{run_id}_{safe_now}"

    def backup(path: Path) -> None:
        if not path.exists():
            return
        try:
            rel = path.relative_to(root)
        except Exception:
            rel = Path(path.name)
        target = backup_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)

    def backup_tree(path: Path) -> None:
        if not path.exists() or not path.is_dir():
            return
        try:
            rel = path.relative_to(root)
        except Exception:
            rel = Path(path.name)
        target = backup_root / rel
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(path, target)

    reset_files: list[str] = []
    placeholders = {
        "read_results.json": {"run_id": run_id, "source": "pending_new_find_read", "status": "pending", "readings": [], "created_at": now, "reset_source": source, "reset_reason": reason},
        "ideas.json": {"run_id": run_id, "source": "pending_new_find_idea", "status": "pending", "ideas": [], "created_at": now, "reset_source": source, "reset_reason": reason},
        "plans.json": {"run_id": run_id, "source": "pending_new_find_plan", "status": "pending", "plans": [], "created_at": now, "reset_source": source, "reset_reason": reason},
    }
    for name, payload in placeholders.items():
        dst = taste_dir / name
        backup(dst)
        write_json(dst, payload)
        reset_files.append(name)
    markdown = {
        "read.md": f"# 精读等待执行\n\n当前 Find {run_id} 的推荐列表已按最新推荐合同更新；请通过网页 Read 按钮触发 project agent 对当前 20 篇推荐论文逐篇全文精读。\n",
        "idea.md": f"# 想法等待生成\n\n当前 Find {run_id} 的精读尚未完成；Idea 必须在 Read 产物通过后生成。\n",
        "plan.md": f"# 计划等待生成\n\n当前 Find {run_id} 的 Idea 尚未完成；Plan 必须在 Idea 通过后生成。\n",
    }
    for name, content in markdown.items():
        dst = taste_dir / name
        backup(dst)
        dst.write_text(content, encoding="utf-8")
        reset_files.append(name)
    full_text_dir = taste_dir / "full_text_reading"
    backup_tree(full_text_dir)
    if full_text_dir.exists():
        shutil.rmtree(full_text_dir)
        reset_files.append("full_text_reading/")
    full_text_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        full_text_dir / "full_text_packet.json",
        {"run_id": run_id, "source": "pending_new_find_full_text_evidence", "status": "pending", "papers": [], "created_at": now, "reset_source": source, "reset_reason": reason},
    )
    reset_files.append("full_text_reading/full_text_packet.json")
    validation = {
        "run_id": run_id,
        "valid": False,
        "status": "pending_current_find_read",
        "source": source,
        "generated_at": now,
        "expected_recommendation_count": len(recommendations),
        "actual_reading_count": 0,
        "full_text_reading_count": 0,
        "pending_full_text_reading_count": len(recommendations),
        "blockers": ["current Find recommendation packet changed; Read must process the current recommendation list before Idea or Plan can be current"],
        "reset_reason": reason,
    }
    backup(state_dir / "current_find_claude_reading_validation.json")
    write_json(state_dir / "current_find_claude_reading_validation.json", validation)
    plan_stub = {
        "run_id": run_id,
        "source_run_id": run_id,
        "source": source,
        "status": "pending_current_find_read",
        "created_at": now,
        "current_find_reading_count": 0,
        "current_find_idea_count": 0,
        "current_find_plan_count": 0,
        "recommended_count": len(recommendations),
        "literature_gate": {"status": "pass", "strong_recommendations": len(recommendations), "recommendation_target_count": len(recommendations), "recommendation_shortfall": 0},
        "reading_validation": validation,
        "next_required_action": "run_read_for_current_find",
        "selected_plan_id": "",
        "selected_idea_id": "",
        "reset_reason": reason,
    }
    backup(state_dir / "current_find_research_plan.json")
    write_json(state_dir / "current_find_research_plan.json", plan_stub)
    return {"status": "reset", "run_id": run_id, "source": source, "reason": reason, "reset_files": reset_files, "backup_dir": str(backup_root) if backup_root.exists() else ""}


def _write_current_find_markdown(directory: Path, find_results: dict[str, Any]) -> None:
    recommendations = find_results.get("strong_recommendations") if isinstance(find_results.get("strong_recommendations"), list) else find_results.get("articles") if isinstance(find_results.get("articles"), list) else []
    read_candidates = recommendations
    triage = find_results.get("triage_candidates") if isinstance(find_results.get("triage_candidates"), list) else []
    audit = find_results.get("audit_candidates") if isinstance(find_results.get("audit_candidates"), list) else triage
    critique = find_results.get("critique_candidates") if isinstance(find_results.get("critique_candidates"), list) else []
    (directory / "article.md").write_text(paper_markdown(recommendations, "Recommended Articles"), encoding="utf-8")
    (directory / "read_candidates.md").write_text(paper_markdown(read_candidates, "Read Candidates"), encoding="utf-8")
    if triage:
        (directory / "triage_candidates.md").write_text(paper_markdown(triage, "Triage Candidates"), encoding="utf-8")
    if audit:
        (directory / "audit_candidates.md").write_text(paper_markdown(audit, "Audit Candidates"), encoding="utf-8")
    if critique:
        (directory / "critique_candidates.md").write_text(paper_markdown(critique, "Critique Candidates"), encoding="utf-8")


def _repair_current_find_translations(log: Callable[[str], None], should_cancel: Callable[[], bool], progress: Callable[[str, int, int, str], None]) -> dict[str, Any]:
    current = _current_project_for_find_guard()
    if not current:
        return {"status": "blocked_no_current_project", "reason": "No active research project is configured."}
    project, root = current
    plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
    projection = _read_project_json(root / "state" / "current_find_recommendation_projection.json", {})
    run_id = str((plan if isinstance(plan, dict) else {}).get("run_id") or (projection if isinstance(projection, dict) else {}).get("run_id") or "").strip()
    if not run_id:
        find_payload = _read_project_json(root / "planning" / "finding" / "find_results.json", {})
        if isinstance(find_payload, dict):
            run_id = str(find_payload.get("run_id") or "").strip()
    directory = _find_artifact_run_dir_for_project(root, run_id)
    find_path = directory / "find_results.json"
    find_results = read_json(find_path, {})
    if not isinstance(find_results, dict):
        return {"status": "blocked_invalid_find_results", "project": project, "run_id": run_id, "path": str(find_path)}
    run_id = str(find_results.get("run_id") or run_id or directory.name).strip()
    reproject_receipt = _reproject_find_results_with_current_contract(find_results, load_config(), "api_jobs_find_repair_current")
    log(f"Current Find recommendation contract reprojection: {reproject_receipt}")
    recommendations = find_results.get("strong_recommendations") if isinstance(find_results.get("strong_recommendations"), list) else find_results.get("articles") if isinstance(find_results.get("articles"), list) else []
    for row in recommendations:
        if isinstance(row, dict):
            row["_user_visible_recommendation"] = True
    missing_before = [row for row in recommendations if isinstance(row, dict) and str(row.get("abstract") or row.get("abstract_en") or "").strip() and not str(row.get("abstract_zh") or "").strip()]
    log(f"Repairing current Find recommendation abstracts for {project}/{run_id}; missing Chinese abstracts before={len(missing_before)}")
    progress("abstract_translation_repair", 0, max(1, len(missing_before)), f"补推荐摘要翻译 {len(missing_before)} 篇")
    config = load_config()
    llm = LLMClient(config, "find")
    if missing_before and not llm.enabled:
        return {"status": "blocked_llm_not_configured", "project": project, "run_id": run_id, "missing_chinese_abstract_count": len(missing_before)}
    translation_result = _attach_abstract_language_fields(recommendations, llm, log, should_cancel, progress)
    missing_after = [row for row in recommendations if isinstance(row, dict) and str(row.get("abstract") or row.get("abstract_en") or "").strip() and not str(row.get("abstract_zh") or "").strip()]
    translation_status = "completed" if not missing_after else "partial"
    if isinstance(translation_result, dict) and str(translation_result.get("status") or "") in {"completed", "not_needed", "skipped"} and not missing_after:
        translation_status = str(translation_result.get("status") or translation_status)
    find_results["strong_recommendations"] = recommendations
    find_results["articles"] = recommendations
    find_results["read_candidates"] = recommendations
    scoring_runtime = find_results.setdefault("scoring_runtime", {})
    if isinstance(scoring_runtime, dict):
        scoring_runtime["strict_strong_anchor_count"] = len(recommendations)
        scoring_runtime["recommendation_actual_count"] = len(recommendations)
        scoring_runtime["abstract_translation_status"] = translation_status
    target_count = int(find_results.get("recommendation_target_count") or (scoring_runtime.get("recommendation_target_count") if isinstance(scoring_runtime, dict) else 0) or len(recommendations) or 0)
    shortfall = max(0, target_count - len(recommendations)) if target_count else 0
    find_results["strict_strong_anchor_count"] = len(recommendations)
    find_results["recommendation_target_count"] = target_count
    find_results["recommendation_shortfall"] = shortfall
    find_results["abstract_translation_status"] = translation_status
    if isinstance(scoring_runtime, dict):
        scoring_runtime["recommendation_target_count"] = target_count
        scoring_runtime["recommendation_shortfall"] = shortfall
        scoring_runtime["recommendation_policy"] = find_results.get("recommendation_policy") or FIND_RECOMMENDATION_POLICY
    quality = _recommendation_quality_audit(recommendations)
    quality["missing_chinese_abstract_count"] = len(missing_after)
    quality["english_abstract_fallback_count"] = len(missing_after)
    quality["missing_chinese_abstract_ids"] = [str(row.get("id") or row.get("title") or "") for row in missing_after[:50] if isinstance(row, dict)]
    if missing_after and quality.get("status") == "ok":
        quality["status"] = "ok_with_translation_todo"
    find_results["recommendation_quality"] = quality
    write_json(find_path, find_results)
    _write_current_find_markdown(directory, find_results)
    taste_dir = root / "planning" / "finding"
    taste_dir.mkdir(parents=True, exist_ok=True)
    for name in ["find_results.json", "article.md", "read_candidates.md", "triage_candidates.md", "audit_candidates.md", "critique_candidates.md"]:
        src = directory / name
        if src.exists():
            shutil.copyfile(src, taste_dir / name)
    latest_dir = RUNS_DIR.parent / "auto_find"
    latest_dir.mkdir(parents=True, exist_ok=True)
    for name in ["find_results.json", "article.md", "read_candidates.md", "triage_candidates.md", "audit_candidates.md", "critique_candidates.md"]:
        src = directory / name
        if src.exists():
            shutil.copyfile(src, latest_dir / name)
    progress_path = directory / "find_progress.json"
    progress_payload = read_json(progress_path, {})
    if isinstance(progress_payload, dict):
        progress_payload["abstract_translation_status"] = translation_status
        progress_payload["strict_strong_anchor_count"] = len(recommendations)
        progress_payload["strong_recommendation_count"] = len(recommendations)
        progress_payload["recommendation_target_count"] = target_count
        progress_payload["recommendation_shortfall"] = shortfall
        progress_payload["recommendation_gate_status"] = "pass" if shortfall == 0 else "recommendation_shortfall"
        progress_payload["recommendation_policy"] = find_results.get("recommendation_policy") or FIND_RECOMMENDATION_POLICY
        counts = progress_payload.get("counts") if isinstance(progress_payload.get("counts"), dict) else {}
        counts.update({
            "recommended": len(recommendations),
            "strong_recommendations": len(recommendations),
            "read_candidates": len(recommendations),
            "strict_strong_anchor_count": len(recommendations),
            "recommendation_target_count": target_count,
            "recommendation_shortfall": shortfall,
        })
        progress_payload["counts"] = counts
        write_json(progress_path, progress_payload)
        shutil.copyfile(progress_path, taste_dir / "find_progress.json")
    projection = _sync_current_find_projection(root, run_id, find_results, "api_jobs_find_repair_current")
    downstream_reason = _current_find_read_reset_reason(root, run_id, recommendations)
    downstream_reset = {}
    if downstream_reason:
        downstream_reset = _reset_current_find_downstream_artifacts(root, run_id, recommendations, "api_jobs_find_repair_current", downstream_reason)
        log(f"Current Find downstream artifacts reset after recommendation repair: {downstream_reset}")
    _clerun_caches(run_id)
    progress("abstract_translation_repair", len(missing_before), max(1, len(missing_before)), f"推荐摘要翻译修复完成，剩余 {len(missing_after)} 篇")
    return {
        "status": "done" if not missing_after else "partial",
        "raw_stage": "find-repair-current",
        "project": project,
        "run_id": run_id,
        "missing_before": len(missing_before),
        "missing_after": len(missing_after),
        "translation_status": translation_status,
        "strict_strong_anchor_count": len(recommendations),
        "recommended_count": len(recommendations),
        "projection_missing": len(projection.get("missing_recommendation_abstract_zh") or []),
        "reprojection": reproject_receipt,
        "downstream_reset": downstream_reset,
        "artifact_dir": str(directory),
    }

def _adopt_find_run_for_project(root: Path, project: str, run_id: str, *, source: str = "web_find_complete") -> dict[str, Any]:
    """Make a completed Web/API Find run the project-level current Find packet.

    Runtime runs are historical by default. A user-approved Web/API Find must be
    explicitly adopted into the project packet so Read/Idea/Plan cannot keep
    consuming stale project artifacts from the previous run.
    """
    run_id = str(run_id or "").strip()
    if not run_id:
        return {"status": "skipped", "reason": "missing_run_id"}
    directory = run_dir(run_id)
    find_results_path = directory / "find_results.json"
    if not find_results_path.exists():
        return {"status": "skipped", "reason": "missing_find_results", "run_id": run_id}
    find_results = read_json(find_results_path, {})
    if not isinstance(find_results, dict):
        return {"status": "skipped", "reason": "invalid_find_results", "run_id": run_id}
    reproject_receipt = _reproject_find_results_with_current_contract(find_results, load_config(), f"{source}_adoption")
    if reproject_receipt.get("status") == "reprojected":
        write_json(find_results_path, find_results)
        _write_current_find_markdown(directory, find_results)
    actual_run_id = str(find_results.get("run_id") or "").strip()
    if actual_run_id and actual_run_id != run_id:
        return {"status": "skipped", "reason": "run_id_mismatch", "run_id": run_id, "actual_run_id": actual_run_id}
    progress = read_json(directory / "find_progress.json", {})
    if not isinstance(progress, dict):
        progress = {}
    recommendations = find_results.get("strong_recommendations") if isinstance(find_results.get("strong_recommendations"), list) else find_results.get("articles") if isinstance(find_results.get("articles"), list) else []
    scoring_runtime = find_results.get("scoring_runtime") if isinstance(find_results.get("scoring_runtime"), dict) else {}
    target_count = int(find_results.get("recommendation_target_count") or progress.get("recommendation_target_count") or scoring_runtime.get("recommendation_target_count") or len(recommendations) or 0)
    shortfall = max(0, target_count - len(recommendations)) if target_count else int(find_results.get("recommendation_shortfall") or progress.get("recommendation_shortfall") or scoring_runtime.get("recommendation_shortfall") or 0)
    find_results["recommendation_target_count"] = target_count
    find_results["recommendation_actual_count"] = len(recommendations)
    find_results["strict_strong_anchor_count"] = len(recommendations)
    find_results["strong_recommendation_count"] = len(recommendations)
    find_results["recommendation_shortfall"] = shortfall
    find_results["abstract_translation_status"] = find_results.get("abstract_translation_status") or progress.get("abstract_translation_status") or scoring_runtime.get("abstract_translation_status") or ""
    if isinstance(scoring_runtime, dict):
        scoring_runtime["recommendation_target_count"] = target_count
        scoring_runtime["recommendation_actual_count"] = len(recommendations)
        scoring_runtime["strict_strong_anchor_count"] = len(recommendations)
        scoring_runtime["recommendation_shortfall"] = shortfall
        scoring_runtime["abstract_translation_status"] = find_results.get("abstract_translation_status") or scoring_runtime.get("abstract_translation_status") or ""
    write_json(find_results_path, find_results)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    taste_dir = root / "planning" / "finding"
    state_dir = root / "state"
    safe_now = now.replace(":", "").replace("-", "").replace(".", "_")
    backup_root = state_dir / "current_find_artifact_backups" / f"adopt_{run_id}_{safe_now}"
    taste_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    stale_reset: list[str] = []
    find_names = [
        "article.md", "source_status.md", "screened_ranking.md", "read_candidates.md", "triage_candidates.md", "audit_candidates.md", "critique_candidates.md",
        "find_results.json", "find_progress.json", "manifest.json", "selection.json", "venue_health_report.json", "category_scan_report.json", "title_filter_report.json",
        "arxiv_raw.json", "arxiv_prefiltered.json", "biorxiv_raw.json", "biorxiv_prefiltered.json", "nature_raw.json", "nature_prefiltered.json", "science_raw.json", "science_prefiltered.json",
        "hf.md", "github.md", "biorxiv.md", "nature.md", "science.md",
    ]
    downstream_placeholders = {
        "read_results.json": {"run_id": run_id, "source": "pending_new_find_read", "status": "pending", "readings": []},
        "ideas.json": {"run_id": run_id, "source": "pending_new_find_idea", "status": "pending", "ideas": []},
        "plans.json": {"run_id": run_id, "source": "pending_new_find_plan", "status": "pending", "plans": []},
    }
    downstream_markdown = {
        "read.md": f"# 精读等待执行\n\n当前 Find `{run_id}` 已完成；请通过网页 Read 按钮触发 project agent 对当前推荐列表逐篇精读。\n",
        "idea.md": f"# 想法等待生成\n\n当前 Find `{run_id}` 的精读尚未完成；Idea 必须在 Read 产物通过后生成。\n",
        "plan.md": f"# 计划等待生成\n\n当前 Find `{run_id}` 的 Idea 尚未完成；Plan 必须在 Idea 通过后生成。\n",
    }

    def backup(path: Path) -> None:
        if not path.exists():
            return
        rel = path.relative_to(root)
        target = backup_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)

    def backup_tree(path: Path) -> None:
        if not path.exists() or not path.is_dir():
            return
        rel = path.relative_to(root)
        target = backup_root / rel
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(path, target)

    for name in find_names:
        src = directory / name
        if not src.exists():
            continue
        dst = taste_dir / name
        backup(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(name)

    completed_downstream_copied: list[str] = []
    downstream_status = "pending_downstream_reset"
    read_src_payload = read_json(directory / "read_results.json", {})
    idea_src_payload = read_json(directory / "ideas.json", {})
    plan_src_payload = read_json(directory / "plans.json", {})

    def same_run_payload(payload: Any) -> bool:
        return isinstance(payload, dict) and str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or "").strip() == run_id

    def completed_payload(payload: Any, list_key: str) -> bool:
        return (
            same_run_payload(payload)
            and str(payload.get("source") or "").strip() == "claude_code_current_find_takeover"
            and isinstance(payload.get(list_key), list)
            and len(payload.get(list_key) or []) > 0
        )

    reading_validation = read_src_payload.get("reading_validation") if isinstance(read_src_payload, dict) and isinstance(read_src_payload.get("reading_validation"), dict) else {}
    validation_run_id = str(reading_validation.get("run_id") or "").strip()
    validation_matches = bool(not validation_run_id or validation_run_id == run_id)
    expected_readings = int(reading_validation.get("expected_recommendation_count") or len(recommendations) or 0) if isinstance(reading_validation, dict) else len(recommendations)
    completed_downstream_ready = bool(
        completed_payload(read_src_payload, "readings")
        and completed_payload(idea_src_payload, "ideas")
        and completed_payload(plan_src_payload, "plans")
        and isinstance(reading_validation, dict)
        and reading_validation.get("valid") is True
        and validation_matches
        and len(read_src_payload.get("readings") or []) >= max(1, expected_readings)
    )

    if completed_downstream_ready:
        downstream_status = "completed_downstream_adopted"
        for name in ["read_results.json", "ideas.json", "plans.json", "read.md", "idea.md", "plan.md"]:
            src = directory / name
            if not src.exists():
                continue
            dst = taste_dir / name
            backup(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            completed_downstream_copied.append(name)
        existing_full_text_dir = taste_dir / "full_text_reading"
        run_full_text_dir = directory / "full_text_reading"
        if run_full_text_dir.exists():
            backup_tree(existing_full_text_dir)
            if existing_full_text_dir.exists():
                shutil.rmtree(existing_full_text_dir)
            shutil.copytree(run_full_text_dir, existing_full_text_dir)
            completed_downstream_copied.append("full_text_reading/")
        else:
            packet = read_json(existing_full_text_dir / "full_text_packet.json", {})
            if not (isinstance(packet, dict) and str(packet.get("run_id") or "").strip() == run_id and isinstance(packet.get("papers"), list) and packet.get("papers")):
                downstream_status = "completed_downstream_adopted_without_full_text_packet"
        reading_validation = {**reading_validation, "run_id": run_id, "source": "claude_code_current_find_takeover", "synced_from": str(directory / "read_results.json"), "synced_at": now}
        backup(state_dir / "current_find_claude_reading_validation.json")
        write_json(state_dir / "current_find_claude_reading_validation.json", reading_validation)
    else:
        for name, payload in downstream_placeholders.items():
            dst = taste_dir / name
            backup(dst)
            write_json(dst, {**payload, "created_at": now, "adopted_find_run_id": run_id})
            stale_reset.append(name)
        for name, content in downstream_markdown.items():
            dst = taste_dir / name
            backup(dst)
            dst.write_text(content, encoding="utf-8")
            stale_reset.append(name)
        full_text_dir = taste_dir / "full_text_reading"
        backup_tree(full_text_dir)
        if full_text_dir.exists():
            shutil.rmtree(full_text_dir)
            stale_reset.append("full_text_reading/")
        full_text_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            full_text_dir / "full_text_packet.json",
            {"run_id": run_id, "source": "pending_new_find_full_text_evidence", "status": "pending", "papers": [], "created_at": now, "adopted_find_run_id": run_id},
        )
        stale_reset.append("full_text_reading/full_text_packet.json")

    missing_zh = []
    for index, row in enumerate(recommendations, 1):
        if not isinstance(row, dict):
            continue
        if str(row.get("abstract") or row.get("abstract_en") or "").strip() and not str(row.get("abstract_zh") or "").strip():
            missing_zh.append({"rank": index, "id": str(row.get("id") or ""), "title": str(row.get("title") or "")})
    projection = {
        "run_id": run_id,
        "source_run_id": run_id,
        "source": source,
        "created_at": now,
        "strong_recommendations": recommendations,
        "articles": recommendations,
        "read_candidates": recommendations,
        "counts": {"recommended": len(recommendations), "read_candidates": len(recommendations), "strict_strong_anchor_count": len(recommendations)},
        "strict_strong_anchor_count": len(recommendations),
        "recommendation_target_count": target_count,
        "recommendation_shortfall": shortfall,
        "recommendation_quality": find_results.get("recommendation_quality") or scoring_runtime.get("recommendation_quality") or {},
        "abstract_translation_status": progress.get("abstract_translation_status") or scoring_runtime.get("abstract_translation_status") or "",
        "missing_recommendation_abstract_zh": missing_zh,
    }
    write_json(state_dir / "current_find_recommendation_projection.json", projection)
    def rows_from(payload: Any, key: str) -> list[Any]:
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return payload.get(key) or []
        return []

    def first_nonempty(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return ""

    read_rows = rows_from(read_src_payload, "readings") if completed_downstream_ready else []
    idea_rows = rows_from(idea_src_payload, "ideas") if completed_downstream_ready else []
    plan_rows = rows_from(plan_src_payload, "plans") if completed_downstream_ready else []
    selected_idea_id = str(first_nonempty(
        plan_src_payload.get("selected_idea_id") if isinstance(plan_src_payload, dict) else "",
        idea_src_payload.get("selected_idea_id") if isinstance(idea_src_payload, dict) else "",
        read_src_payload.get("selected_idea_id") if isinstance(read_src_payload, dict) else "",
    ) or "")
    selected_plan_id = str(first_nonempty(
        plan_src_payload.get("selected_plan_id") if isinstance(plan_src_payload, dict) else "",
        idea_src_payload.get("selected_plan_id") if isinstance(idea_src_payload, dict) else "",
        read_src_payload.get("selected_plan_id") if isinstance(read_src_payload, dict) else "",
    ) or "")
    selected_idea = next((row for row in idea_rows if isinstance(row, dict) and str(row.get("id") or row.get("idea_id") or "") == selected_idea_id), {})
    selected_plan = next((row for row in plan_rows if isinstance(row, dict) and str(row.get("plan_id") or row.get("id") or "") == selected_plan_id), {})
    execution_policy = first_nonempty(
        plan_src_payload.get("execution_policy") if isinstance(plan_src_payload, dict) else {},
        idea_src_payload.get("execution_policy") if isinstance(idea_src_payload, dict) else {},
        read_src_payload.get("execution_policy") if isinstance(read_src_payload, dict) else {},
    )
    if completed_downstream_ready:
        plan_stub = {
            "run_id": run_id,
            "source_run_id": run_id,
            "source": "claude_code_current_find_takeover",
            "status": "claude_takeover_ready",
            "created_at": now,
            "synced_from_run_dir": str(directory),
            "current_find_reading_count": len(read_rows),
            "current_find_idea_count": len(idea_rows),
            "current_find_plan_count": len(plan_rows),
            "recommended_count": len(recommendations),
            "literature_gate": {
                "status": "pass" if recommendations and shortfall == 0 else "recommendation_shortfall",
                "strong_recommendations": len(recommendations),
                "recommendation_target_count": target_count,
                "recommendation_shortfall": shortfall,
            },
            "reading_validation": reading_validation,
            "full_text_reading_count": int(reading_validation.get("full_text_reading_count") or len(read_rows) or 0),
            "pending_full_text_reading_count": int(reading_validation.get("pending_full_text_reading_count") or 0),
            "next_required_action": "environment_base_selection_and_repo_data_protocol_audit",
            "selected_plan_id": selected_plan_id,
            "selected_idea_id": selected_idea_id,
            "selected_plan": selected_plan,
            "selected_idea": selected_idea,
            "execution_policy": execution_policy if isinstance(execution_policy, dict) else {},
            "artifacts": {
                "read_results": str(taste_dir / "read_results.json"),
                "ideas": str(taste_dir / "ideas.json"),
                "plans": str(taste_dir / "plans.json"),
                "read_md": str(taste_dir / "read.md"),
                "idea_md": str(taste_dir / "idea.md"),
                "plan_md": str(taste_dir / "plan.md"),
            },
        }
    else:
        plan_stub = {
            "run_id": run_id,
            "source_run_id": run_id,
            "source": "web_find_adoption",
            "status": "pending_current_find_read",
            "created_at": now,
            "current_find_reading_count": 0,
            "current_find_idea_count": 0,
            "current_find_plan_count": 0,
            "recommended_count": len(recommendations),
            "literature_gate": {
                "status": "pass" if recommendations and shortfall == 0 else "recommendation_shortfall",
                "strong_recommendations": len(recommendations),
                "recommendation_target_count": target_count,
                "recommendation_shortfall": shortfall,
            },
            "reading_validation": {
                "valid": False,
                "expected_recommendation_count": len(recommendations),
                "actual_reading_count": 0,
                "pending_full_text_reading_count": len(recommendations),
                "blockers": ["current Find adopted; Read stage has not processed this run yet"],
            },
            "next_required_action": "run_read_for_current_find",
            "selected_plan_id": "",
            "selected_idea_id": "",
        }
    backup(state_dir / "current_find_research_plan.json")
    write_json(state_dir / "current_find_research_plan.json", plan_stub)
    receipt = {
        "status": "adopted",
        "project": project,
        "run_id": run_id,
        "source": source,
        "created_at": now,
        "run_dir": str(directory),
        "project_taste_dir": str(taste_dir),
        "copied": copied,
        "downstream_status": downstream_status,
        "completed_downstream_copied": completed_downstream_copied,
        "stale_downstream_reset": stale_reset,
        "recommended_count": len(recommendations),
        "reprojection": reproject_receipt,
        "missing_recommendation_abstract_zh_count": len(missing_zh),
        "backup_dir": str(backup_root) if backup_root.exists() else "",
    }
    write_json(state_dir / "latest_find_adoption_receipt.json", receipt)
    with (state_dir / "find_adoption_audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    _clerun_caches(run_id)
    return receipt


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
        "message": "Current Find recommendation gate is short; use force_new_find/restart_full_cycle for an explicit fresh Find, or scripts/run_literature_tool.py for controlled targeted repair.",
        "message_zh": "当前 Find 推荐门控未过；显式重新 Find 请使用 force_new_find/restart_full_cycle，受控补检索请走 scripts/run_literature_tool.py。",
    }


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
        "message_zh": "完整科研流程正在运行；已阻止重复启动新的 Find/Read/Idea/Plan 阶段任务。需要人工介入时请使用项目代理指令队列。",
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



def _suppress_same_phase_descendant_workers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one taskbar row for a stage wrapper and fold its children into logs.

    A standalone environment/experiment/paper launch often starts a Python
    wrapper which then spawns selector/probe/Claude child processes. Showing each
    child as another project-worker makes the UI look like duplicate launches.
    """
    by_pid = {str(row.get('pid') or ''): row for row in rows if isinstance(row, dict) and str(row.get('pid') or '')}

    def has_same_phase_ancestor(row: dict[str, Any]) -> bool:
        phase = str(row.get('phase') or '').strip().lower()
        current = str(row.get('ppid') or '').strip()
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            parent = by_pid.get(current)
            if not parent:
                return False
            parent_phase = str(parent.get('phase') or '').strip().lower()
            parent_kind = str(parent.get('kind') or '').strip().lower()
            if phase and parent_phase == phase and parent_kind != 'full_cycle':
                return True
            current = str(parent.get('ppid') or '').strip()
        return False

    return [row for row in rows if isinstance(row, dict) and not has_same_phase_ancestor(row)]


def _active_project_child_processes(project: str, root: Path, phase_hint: str = "") -> list[dict[str, Any]]:
    markers = [str(project), str(root), str(root / "tmp" / "finding")]
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,stat=,etime=,%cpu=,%mem=,cmd="],
            text=True,
            capture_output=True,
            timeout=2,
        )
    except Exception:
        return []
    process_rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        if "Z" in str(parts[2]).upper():
            continue
        process_rows.append({"pid": parts[0], "ppid": parts[1], "stat": parts[2], "elapsed": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": parts[6], "cwd": _proc_cwd(parts[0])})
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
            kind = "current_find_claude_child"
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
        elif any(marker in lowered for marker in ["run_environment_stage.py", "run_literature_base_audit.py", "select_evidence_ready_repo.py", "repo_env_bootstrap", "run_selected_base_reference"]):
            kind = "environment_stage"
            phase = "environment"
            priority = 2
        elif _looks_like_experiment_training_cmd(cmd):
            kind = "experiment_training"
            phase = "experiment"
            priority = 3
        elif "run_paper_pipeline.py" in lowered:
            kind = "paper_pipeline"
            phase = "paper"
            priority = 4
        elif "repair_paper_preview_loop.py" in lowered or "repair_paper_figures_loop.py" in lowered:
            kind = "paper_repair_loop"
            phase = "paper"
            priority = 4
        elif "claude_project_session.py" in lowered and ("paper" in lowered or "writing" in lowered or "writing" in lowered):
            kind = "paper_claude_session"
            phase = "paper"
            priority = 5
        elif "run_full_research_cycle.py" in lowered:
            kind = "full_cycle"
            phase = "full-cycle"
            priority = 10
        if not kind:
            continue
        rows.append({"pid": proc_row.get("pid"), "ppid": proc_row.get("ppid"), "stat": proc_row.get("stat"), "elapsed": proc_row.get("elapsed"), "pcpu": proc_row.get("pcpu"), "pmem": proc_row.get("pmem"), "cmd": cmd, "cwd": cwd, "kind": kind, "phase": phase, "priority": priority})
    rows = _suppress_same_phase_descendant_workers(rows)
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


def _process_tree_rows(pid: Any) -> list[dict[str, Any]]:
    try:
        root_pid = str(int(pid))
    except Exception:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,stat=,etime=,%cpu=,%mem=,cmd="],
            text=True,
            capture_output=True,
            timeout=2,
        )
    except Exception:
        return []
    rows_by_pid: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        if "Z" in str(parts[2]).upper():
            continue
        row = {"pid": parts[0], "ppid": parts[1], "stat": parts[2], "elapsed": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": parts[6], "cwd": _proc_cwd(parts[0])}
        rows_by_pid[parts[0]] = row
        children.setdefault(parts[1], []).append(parts[0])
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
            result.append(row)
        stack.extend(children.get(current, []))
    return result


def _proc_cwd(pid: Any) -> str:
    try:
        return os.path.realpath(os.readlink(f"/proc/{int(pid)}/cwd"))
    except Exception:
        return ""


def _all_process_rows() -> list[dict[str, Any]]:
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
        if _is_inspection_or_wrapper_cmd(parts[6]):
            continue
        rows.append({"pid": parts[0], "ppid": parts[1], "stat": parts[2], "elapsed": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": parts[6], "cwd": _proc_cwd(parts[0])})
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
    if "python - <<" in lowered or "python3 - <<" in lowered or "python -c" in lowered or "python3 -c" in lowered:
        inspection_terms = [
            "curl -ss",
            "api/jobs",
            "api/ar/projects",
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
        label = _command_dataset_label(cmd)
        path = paths[0] if paths else ""
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
    interesting: list[tuple[int, dict[str, Any], str]] = []
    for row in process_rows:
        cmd = str(row.get("cmd") or "")
        lowered = cmd.lower()
        label = ""
        priority = 99
        if _looks_like_experiment_training_cmd(cmd):
            label = "实验训练进程"
            priority = 0
            if "llm_candidate" in lowered:
                priority = -3
            elif "conda run" in lowered:
                priority = -2
            elif "finetune_llm.py" in lowered and "nohup" not in lowered:
                priority = -1
        elif "claude_project_session.py" in lowered:
            label = "项目 Claude 会话"
            priority = 1
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
        if _looks_like_experiment_training_cmd(cmd):
            experiment_pids.append(str(row.get("pid") or ""))
        if not experiment_cmd and _looks_like_experiment_training_cmd(cmd):
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
        if _looks_like_experiment_training_cmd(row.get("cmd"))
    ]
    training_rows = [
        row
        for _priority, row, _label in interesting
        if _looks_like_experiment_training_cmd(row.get("cmd"))
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
                    or _is_recent_current_repo_training_log(path)
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
            logs.append("experiment_output_status=最近一次实验训练已结束；下方展示该已完成训练的真实日志尾部，等待 project agent 登记审计和刷新门控。")
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
    if "current-find-selection" in text or "current-find-claude-select-plan" in text or "current-find-read-idea-plan" in text or ("plan" in text and "literature-plan" not in text):
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
    blocked_markers = ["sync-outputs", "literature-tool-packet", "build-literature-tool-packet", "ensure-current-find", "current-find-read-idea-plan", "current-find-selection", "current-find-claude-select-plan", "blocker-action-plan", "build-blocker-action-plan"]
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
        ("ensure-current-find", "Claude 精读当前 Find 并生成 idea/plan"),
        ("current-find-selection", "主控 Claude 选择唯一执行计划"),
        ("current-find-claude-select-plan", "主控 Claude 选择唯一执行计划"),
        ("current-find-read-idea-plan", "Claude 精读当前 Find 并生成 idea/plan"),
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
        return "project agent 正在设计下一轮当前主线候选实验；未审计通过前不写论文结论。"
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
    directory = run_dir(run_id)
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


def _compact_log_lines(lines: Any, *, limit: int = 40) -> list[str]:
    raw_lines = [str(line) for line in (lines or [])]
    return _dedupe_recent_lines(raw_lines, limit=limit)


def _redact_public_log_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|authorization|bearer|token)(\s*[:=]\s*)[^\s,'\"]+", r"\1\2***", text)
    text = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})", "sk-***", text)
    return text


def _summarize_claude_taskbline(line: Any) -> str:
    raw_text = _redact_public_log_text(line).strip()
    raw_lowered = raw_text.lower()
    if "scientific progress gate" in raw_lowered and "still blocked" in raw_lowered:
        return "Claude Code：确认科学进展门控仍阻塞，正在整理负结果并刷新后续审计。"
    if "ndcg improved" in raw_lowered or "loss is flat" in raw_lowered or "wait for more data" in raw_lowered:
        return "Claude Code：正在监督当前候选实验；指标仍需完整日志和本地审计确认，暂不形成结论。"
    if "dead end" in raw_lowered:
        return "Claude Code：正在总结当前候选路线的负结果，并保持科学进展门控阻塞。"
    if "not real llm" in raw_lowered:
        return "Claude Code：正在审计候选结果是否满足项目目标证据要求，不能直接提升为论文结论。"
    if "scientific_progress" in raw_lowered or "progress_gate" in raw_lowered or "build_blocker" in raw_lowered:
        return "Claude Code：正在检查科学进展门控和 blocker 刷新脚本，准备审计当前候选实验。"
    if "project_config.py" in raw_lowered:
        return "Claude Code：正在读取 项目配置，确认当前主线和运行参数。"
    if "audit_paper_evidence.py" in raw_lowered or "audit_submission_readiness.py" in raw_lowered:
        return "Claude Code：正在准备刷新论文证据和投稿准备度门控，但不会提升结论。"
    if "experiment_registry" in raw_lowered or "artifact-local audit" in raw_lowered or "artifact local audit" in raw_lowered:
        return "Claude Code：正在检查候选实验是否已有本地审计记录和实验登记。"
    if "reranking evaluation" in raw_lowered:
        if "tensor dimension" in raw_lowered or "dimension issues" in raw_lowered or "similarity computation" in raw_lowered:
            return "Claude Code：正在修复当前候选评估里的张量维度/相似度计算问题。"
        if "ndcg" in raw_lowered and ("numpy array" in raw_lowered or "array" in raw_lowered or "calculate_hit" in raw_lowered):
            return "Claude Code：正在修复当前候选评估的 NDCG 数组提取问题。"
        if "missing `/ total`" in raw_lowered or "missing / total" in raw_lowered or "total division" in raw_lowered:
            return "Claude Code：正在修复当前候选评估的 NDCG 归一化计算。"
        if "shape mismatch" in raw_lowered or "wrong item_num" in raw_lowered:
            return "Claude Code：正在修复当前候选评估的数据维度与表示形状不匹配。"
        return "Claude Code：正在运行当前候选评估，检查是否能形成可审计候选实验。"
    if "ndcg value is a numpy array" in raw_lowered or "ndcg_purchase" in raw_lowered or "calculate_hit" in raw_lowered:
        return "Claude Code：正在修复当前候选评估的 NDCG 数组提取问题。"
    if "missing `/ total` division" in raw_lowered or "missing / total division" in raw_lowered:
        return "Claude Code：正在修复当前候选评估的 NDCG 归一化计算。"
    if "tensor dimension bug" in raw_lowered or "dimension issues" in raw_lowered or "simplify the similarity computation" in raw_lowered:
        return "Claude Code：正在修复当前候选评估里的张量维度/相似度计算问题。"
    text = _clean_claude_taskbline(line)
    if not text:
        return ""
    lowered = text.lower()
    if "sentence_transformers ok" in lowered or "sklearn ok" in lowered or "environment is ready" in lowered:
        return "Claude Code：实验环境依赖检查通过，正在准备候选实验。"
    if "waiting for claude code output" in lowered:
        return "Claude Code：正在运行，等待下一段输出。"
    if "shape mismatch" in lowered or "wrong item_num" in lowered or "item_num" in lowered and "wrong" in lowered:
        return "Claude Code：发现候选实验维度/数据配置不匹配，正在修正 item 数量与 embedding 形状。"
    if "no training instability" in lowered:
        return "Claude Code：已排除训练不稳定，正在尝试更直接的当前主线候选改动。"
    if "residual" in lowered:
        return "Claude Code：正在尝试当前主线候选表示改动，并等待本地审计确认。"
    if "orthogonal to the id embedding space" in lowered or "cross-space" in lowered or "misalignment" in lowered:
        return "Claude Code：发现候选表示空间不对齐，正在调整当前主线实现。"
    if "experiment" in lowered and ("embedding" in lowered or "init" in lowered):
        return "Claude Code：正在运行当前候选实验；该结果需本地审计，不能直接作为论文结论。"
    if "embedding initialization" in lowered:
        return "Claude Code：正在尝试当前候选初始化方案，等待真实实验和审计结果。"
    if "embedding pipeline" in lowered:
        return "Claude Code：正在检查当前主线的项目目标证据管线。"
    if "model isn't learning" in lowered or "train loss" in lowered or "0.693" in lowered:
        return "Claude Code：发现上一轮候选训练未学习，正在排查候选方法实现。"
    if "duplicate guard" in lowered or "kill it and re-run" in lowered or "re-run" in lowered:
        return "Claude Code：检测到重复训练守护，正在按 TASTE 控制重新启动候选实验。"
    if "experiment is running" in lowered or "check on the experiment" in lowered or "hasn't produced output" in lowered or "wait 3 more minutes" in lowered or "monitor progress" in lowered or "while waiting" in lowered:
        return "Claude Code：候选实验训练已启动，正在等待新的 epoch/指标日志输出。"
    if "std=" in lowered or "optimization" in lowered or "large-magnitude" in lowered or "magnitude" in lowered:
        return "Claude Code：正在排查文本 embedding 尺度与优化稳定性问题。"
    if "embeddings are ready" in lowered:
        return "Claude Code：候选表示已生成，正在检查是否适合当前训练。"
    if "run an experiment" in lowered:
        return "Claude Code：正在准备启动当前主线候选实验。"
    if text.endswith((" for", " and", " or", " to", " with", " while", "—")):
        return ""
    # The taskbar is a human status surface. Keep full raw Claude text in
    # claude_project_session_last_result.json and supervision logs, but do not
    # surface arbitrary English stream fragments as the latest task state.
    if re.search(r"[A-Za-z]{4,}", text) and not re.search(r"[一-鿿]", text):
        return "Claude Code：正在执行当前科研动作；原始流式输出保留在项目日志中。"
    return text


def _is_generic_claude_taskbsummary(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    generic = {
        "Claude Code：正在运行，等待下一段输出。",
        "Claude Code：正在执行当前科研动作；原始流式输出保留在项目日志中。",
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


def _is_live_claude_agent_row(row: dict[str, Any]) -> bool:
    """Return True only for an actual Claude session/worker, not the controller."""
    status = str(row.get("status") or "").strip().lower()
    if status not in {"running", "queued", "cancelling"}:
        return False
    role = str(row.get("role") or "").strip().lower()
    command = " ".join(str(part) for part in row.get("command", [])) if isinstance(row.get("command"), list) else str(row.get("command") or "")
    command_l = command.lower()
    is_claude = role in {"claude-main", "claude-worker"} or "claude_project_session" in command_l or "/claude" in command_l or command_l.startswith("claude ")
    if not is_claude:
        return False
    pid = row.get("pid") or row.get("claude_pid")
    if status in {"running", "cancelling"} and pid not in (None, "") and not _pid_alive_local(pid):
        return False
    return True


def _agent_matches_taskbscope(row: dict[str, Any], stage_scope: str = "") -> bool:
    scope = str(stage_scope or "").strip().lower()
    if not scope:
        return True
    stage = str(row.get("stage") or row.get("last_stage") or "").strip().lower()
    agent_id = str(row.get("id") or "").strip().lower()
    if scope == "experiment":
        if stage.startswith("writing") or "paper" in stage or agent_id.startswith(("writing", "paper_", "venue-intelligence")):
            return False
        return agent_id in {"main", ""} or any(token in stage for token in ["experiment", "reference", "environment", "autonomous", "trajectory", "blocker", "selected-base"])
    if scope == "paper":
        return stage.startswith("writing") or "paper" in stage or agent_id.startswith(("writing", "paper_", "venue-intelligence"))
    return True


def _latest_claude_agent_status_lines(root: Path, *, limit: int = 10, stage_scope: str = "") -> list[str]:
    last = _read_project_json(root / "state" / "claude_project_session_last_result.json", {})
    lines: list[str] = []
    session = _read_project_json(root / "state" / "claude_project_session.json", {})
    session_status = str(session.get("status") or "").strip().lower() if isinstance(session, dict) else ""
    session_pid = session.get("pid") or session.get("claude_pid") if isinstance(session, dict) else None
    active_session = bool(isinstance(session, dict) and session_status in {"running", "queued", "cancelling"})
    if active_session and session_status in {"running", "cancelling"} and session_pid not in (None, "") and not _pid_alive_local(session_pid):
        active_session = False
    if active_session:
        stage = str(session.get("stage") or session.get("last_stage") or "").strip()
        status = str(session.get("status") or "").strip()
        pid = str(session.get("pid") or session.get("claude_pid") or "").strip()
        updated = str(session.get("updated_at") or "").strip()
        bits = []
        if stage:
            bits.append(f"阶段={_humanize_stage(stage) or stage}")
        if status:
            bits.append(f"状态={_humanize_job_status(status)}")
        if pid:
            bits.append(f"PID={pid}")
        if updated:
            bits.append(f"更新={updated}")
        if bits:
            lines.append("claude_status=" + "；".join(bits))
        live_agent_lines: list[str] = []
        payload = _read_project_json(root / "state" / "agents.json", {})
        agents = payload.get("agents") if isinstance(payload, dict) else []
        if isinstance(agents, list):
            live_rows = [row for row in agents if isinstance(row, dict) and _is_live_claude_agent_row(row) and _agent_matches_taskbscope(row, stage_scope)]
            if live_rows:
                latest_live = sorted(live_rows, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]
                current_items = []
                if isinstance(latest_live.get("log_tail"), list):
                    # Prefer the newest live Claude stream entries. The current_step
                    # field can lag behind a long-running session and should not pin
                    # the taskbar to an early summary.
                    current_items.extend(reversed(latest_live.get("log_tail")))
                current_items.append(latest_live.get("current_step"))
                for raw in current_items:
                    cleaned = _summarize_claude_taskbline(raw)
                    if cleaned and not _is_generic_claude_taskbsummary(cleaned):
                        line = "claude_current=" + cleaned
                        if line not in live_agent_lines:
                            live_agent_lines.append(line)
                    if len(live_agent_lines) >= 3:
                        break
        prompt_path = str(session.get("message_file") or session.get("prompt_path") or "").strip()
        if live_agent_lines:
            # Keep claude_current rows chronological so the client, which reads
            # the last matching row as latest, shows the true current action.
            lines.extend(list(reversed(live_agent_lines))[: max(0, limit - len(lines))])
        elif prompt_path:
            lines.append("claude_current=项目 Claude Code 正在读取当前阶段提示并自主处理；完整提示和回复保留在项目产物中。")
        if lines:
            # The global taskbar should describe the live Claude worker only.
            # Completed Claude replies are shown in the dedicated recent-reply panel,
            # not replayed as current job output.
            return lines[:limit]

    payload = _read_project_json(root / "state" / "agents.json", {})
    agents = payload.get("agents") if isinstance(payload, dict) else []
    if isinstance(agents, list):
        live_candidates = []
        for row in agents:
            if not isinstance(row, dict):
                continue
            if not _is_live_claude_agent_row(row) or not _agent_matches_taskbscope(row, stage_scope):
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


def _latest_find_run_id_from_runs() -> str:
    runs_dir = Path(__file__).resolve().parents[1] / "runs"
    try:
        candidates = [path for path in runs_dir.glob("find_*") if path.is_dir()]
    except Exception:
        return ""
    if not candidates:
        return ""
    return sorted(candidates, key=lambda path: path.name)[-1].name


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
    cmd = str(worker.get("cmd") or "")
    elapsed = str(worker.get("elapsed") or "")
    run_id = ""
    for source in [current_plan, find_progress, full_cycle]:
        if isinstance(source, dict):
            run_id = str(source.get("run_id") or source.get("source_run_id") or source.get("current_find_run_id") or source.get("find_run_id") or "").strip()
            if run_id:
                break
    controller_note = "由当前完整科研循环管理；不是完整科研循环控制器。" if controller_alive else "不是完整科研循环控制器；控制器未存活或需恢复。"
    current_find_worker = kind.startswith("current_find") or phase == "read"
    if current_find_worker:
        worker_summary = f"当前 Find 精读/想法/计划 worker 正在运行；{controller_note}"
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
    detail_logs: list[str] = []
    detail_artifacts: list[str] = []
    if phase == "experiment":
        detail_logs, detail_artifacts = _active_detail_lines(root, pid, "experiment")
        for line in _compact_experiment_detail_lines(detail_logs, limit=36):
            logs.append(line[:900])
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
    if current_find_worker:
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
    if phase == "experiment":
        progress_current, progress_total, progress_percent = _experiment_epoch_progress(logs, command=cmd)
    message_bits = [f"{phase} worker running", f"PID={pid}"]
    if elapsed:
        message_bits.append(f"elapsed={elapsed}")
    if latest_status:
        message_bits.append(latest_status[:180])
    elif cmd:
        message_bits.append(cmd[:180])
    return {
        "job_id": f"experiment-worker_{project}_{pid}" if phase == "experiment" else (f"current-find-worker_{project}_{pid}" if current_find_worker else f"project-worker_{project}_{pid}"),
        "stage": phase,
        "status": "running",
        "created_at": str((full_cycle if isinstance(full_cycle, dict) else {}).get("started_at") or datetime.now(UTC).isoformat()),
        "logs": logs[-80:] if compact else logs,
        "log_count": len(logs),
        "run_id": run_id,
        "result": {
            "project": project,
            "pid": pid,
            "phase": phase,
            "raw_stage": kind,
            "command": cmd,
            "log_path": log_path,
            "artifacts": artifacts,
            "summary": worker_summary,
            "status": "running",
            "process_alive": True,
            "kind": kind,
            "not_full_cycle_controller": True,
        },
        "internal": False,
        "display": "",
        "error": "",
        "cancel_requested": False,
        "cancelled_at": "",
        "progress": {"phase": phase, "current": progress_current, "total": progress_total, "percent": progress_percent, "message": "；".join(message_bits)},
    }


def _live_jobs_from_projects(*, compact: bool = True) -> list[dict[str, Any]]:
    """Build live research job rows without calling project_summary.

    This endpoint is polled frequently by the UI. It must only read small state
    files and process metadata; the heavier project summary endpoint feeds the
    main TASTE panels separately.
    """
    now = time.monotonic()
    if compact:
        cached_items = _LIVE_JOBS_CACHE.get("items")
        if isinstance(cached_items, list) and now < float(_LIVE_JOBS_CACHE.get("expires_at") or 0.0):
            return [dict(item) for item in cached_items if isinstance(item, dict)]
    jobs: list[dict[str, Any]] = []
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
        find_progress = _read_project_json(root / "planning" / "finding" / "find_progress.json", {})
        literature_packet = _read_project_json(root / "state" / "literature_tool_packet.json", {})
        current_plan = _read_project_json(root / "state" / "current_find_research_plan.json", {})
        reference_live_job = _live_reference_reproduction_job(project, root, current_plan if isinstance(current_plan, dict) else {}, compact=compact)
        if reference_live_job and str(reference_live_job.get("status") or "").lower() in {"queued", "running", "cancelling"}:
            # A wrapper-managed selected-base full reproduction is a real research job
            # launched by the cycle. Surface it in the taskbar while it is alive;
            # do not synthesize completed history after it exits.
            reference_live_job["stage"] = "experiment"
            reference_live_job["job_id"] = str(reference_live_job.get("job_id") or f"reference-reproduction_{project}")
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
        # A Claude child can invoke scripts/run_literature_tool.py while the
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
        if fresh_find_running:
            run_id = _latest_find_run_id_from_runs()
        if not run_id:
            # The taskbar should point at the current project Find packet. Older
            # full-cycle snapshots and selected-base provenance may mention the
            # Find run that originally selected the base; that is audit history,
            # not the current Find/read/idea/plan surface.
            for source in [current_plan, find_progress, literature_packet, tick, full_cycle]:
                if isinstance(source, dict):
                    run_id = str(source.get("run_id") or source.get("source_run_id") or source.get("current_find_run_id") or source.get("find_run_id") or "").strip()
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
                        desired_goal = "等待当前训练日志和指标落盘；完成后由 project agent 写本地审计记录、登记实验表，并刷新科学进展、论文证据、投稿准备度和阻塞行动计划门控。"
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
        claude_status_lines = _latest_claude_agent_status_lines(root, limit=12, stage_scope=phase) if process_alive else []
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
                if cleaned_stage_line and cleaned_stage_line.startswith("Claude Code：") and cleaned_stage_line not in stage_lines:
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
            # Always keep a bounded raw tail for auditability.  The compact
            # Claude status lines say what TASTE thinks is happening; these raw
            # full-cycle lines show the actual recent command/tool/log context.
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
        _LIVE_JOBS_CACHE["items"] = [dict(item) for item in jobs]
        _LIVE_JOBS_CACHE["expires_at"] = time.monotonic() + LIVE_JOBS_TTL_SEC
    return jobs

def _humanize_stale_tail(text: Any, process_alive: bool) -> str:
    value = str(text or "")
    if process_alive:
        return value
    value = value.replace("full-cycle: running ", "full-cycle last command: ")
    value = value.replace("running ", "last command: ")
    return value





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

    message_bits = [
        f"selected-base full reproduction {'running' if process_alive else job_status}",
        f"PID={pid or '-'}",
    ]
    if elapsed:
        message_bits.append(f"elapsed={elapsed}")
    if paper_title:
        message_bits.append(paper_title[:96])
    if not process_alive and job_status == "blocked":
        message_bits.append("请以 reference reproduction audit/gate 为准")

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
            "phase": "running" if process_alive else job_status,
            "current": 0 if process_alive else 1,
            "total": 1,
            "percent": 0 if process_alive else 100,
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


def _reconcile_detached_launcher_jobs() -> None:
    """Keep web-created detached research jobs aligned with project state.

    /api/jobs/project starts full-cycle as a detached worker so the web server can
    restart without killing research. The web-created job id is written into
    state/full_cycle_job.json; this reconciler keeps that same job row
    running/blocked/done instead of showing an idle launcher plus a separate
    synthetic row.
    """
    changed = False
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
    for job in JOBS.values():
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
            changed = True
    if changed:
        _persist_jobs()


def _compact_job_for_list(item: dict[str, Any]) -> dict[str, Any]:
    """Small row for the frequently-polled jobs list."""
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    raw_stage = str(item.get("stage", ""))
    panel_stage = _panel_stage_from_project_agent_result(result)
    paper_job = _is_paper_job(raw_stage, item.get("job_id", ""), result, item.get("logs"))
    full_cycle_job = False if panel_stage else _is_full_cycle_job(raw_stage, item.get("job_id", ""), result, item.get("logs"))
    public_stage = panel_stage or ("paper" if paper_job else (_public_full_cycle_stage(raw_stage, item.get("progress"), result) if full_cycle_job else _public_taste_stage(raw_stage)))
    compact_result: dict[str, Any] = {}
    result_keys = ["run_id", "project", "topic", "target_venue", "action", "agent_id", "target_agent_id", "requested_stage", "panel_stage", "pid", "cmd", "kind", "log_path", "artifact_dir", "find_results_path", "find_results_size_bytes", "phase", "raw_stage", "summary", "status", "process_alive"]
    if full_cycle_job:
        result_keys = [key for key in result_keys if key != "cmd"]
    for key in result_keys:
        if key in result:
            compact_result[key] = result.get(key)
    if public_stage == "paper":
        paper_stage = _paper_stage_from_job_result(result)
        if paper_stage:
            paper_keys = [
                "paper_summary", "paper_stage", "venue", "target_venue", "venue_slug", "template_family", "paper_normality_status",
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
                "latest_generated_pdf_path", "raw_pdf_path", "pdf_path", "tex_path",
            ]
            paper_summary = _paper_stage_job_message(paper_stage) or str(paper_stage.get("summary") or result.get("paper_summary") or "")
            compact_result["paper_summary"] = paper_summary
            compact_result["paper_stage"] = {key: paper_stage.get(key) for key in paper_keys if key in paper_stage}
            for key in paper_keys:
                if key in paper_stage and key not in {"paper_summary", "paper_stage"}:
                    compact_result[key] = paper_stage.get(key)
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
    if panel_stage and isinstance(result, dict):
        result_status = str(result.get("status") or "").strip()
        if result_status:
            public_status = result_status
    if paper_job and public_stage == "paper" and isinstance(result, dict):
        progress_payload = item.get("progress") if isinstance(item.get("progress"), dict) else {}
        paper_row = result.get("paper_stage") if isinstance(result.get("paper_stage"), dict) else result
        paper_status = str(paper_row.get("status") or "").strip()
        if _paper_preview_artifact_available(paper_row) and public_status not in {"running", "queued", "cancelling", "error", "cancelled"}:
            public_status = "preview_available"
        elif paper_status:
            public_status = "needs_writing" if paper_status.startswith("blocked") or paper_status in {"normality_blocked", "preview_pdf_blocked"} else paper_status
    public_progress = dict(item.get("progress")) if isinstance(item.get("progress"), dict) else {}
    if public_stage in {"environment", "experiment"}:
        command_message = _public_stage_command_message(public_stage, public_progress.get("message"))
        if command_message:
            public_progress["message"] = command_message
        elif _is_project_agent_panel_job(raw_stage, item.get("job_id", ""), result):
            public_progress["message"] = _public_project_agent_progress_message(public_stage, public_progress.get("message"))
    if public_stage == "environment" and public_status == "blocked":
        project_id = str(compact_result.get("project") or result.get("project") or "").strip()
        if project_id:
            try:
                live_summary = project_summary(project_id)
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
                    or "真实数据/loader" in live_message
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
        if "真实数据/loader 已通过" in progress_message or "等待参考协议" in progress_message or "reference-protocol" in progress_message.lower():
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
        if "真实数据/loader 已通过" in progress_message or "等待参考协议" in progress_message or "reference-protocol" in progress_message.lower():
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
    payload = {
        "job_id": item.get("job_id", ""),
        "stage": public_stage,
        "status": public_status,
        "created_at": item.get("created_at", ""),
        "logs": _public_job_logs(panel_stage or ("paper" if paper_job else ("full-cycle" if full_cycle_job else raw_stage)), item.get("logs"), public_progress, result, limit=40),
        "log_count": item.get("log_count", 0),
        "run_id": item.get("run_id", ""),
        "result": compact_result,
        "internal": bool(item.get("internal")),
        "display": item.get("display", ""),
        "error": item.get("error", ""),
        "cancel_requested": bool(item.get("cancel_requested")),
        "cancelled_at": item.get("cancelled_at", ""),
        "progress": public_progress,
    }
    return _strip_public_taste_marker(payload)


def _persist_jobs() -> None:
    if not globals().get("JOBS_PATH"):
        return
    with JOBS_LOCK:
        items = sorted(
            [item for item in (job.as_dict(compact=True) for job in JOBS.values()) if not _job_is_hollow_route(item)],
            key=lambda item: item["created_at"],
            reverse=True,
        )
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


def _find_run_history_jobs_from_runs(existing_run_ids: set[str], *, limit: int = 300) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    try:
        runs = _cached_list_runs()
    except Exception:
        return jobs
    for row in runs:
        run_id = str((row if isinstance(row, dict) else {}).get("run_id") or "").strip()
        if not run_id.startswith("find_") or run_id in existing_run_ids:
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
        article_path = directory / "article.md"
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
        if status == "done" and not article_path.exists():
            status = "blocked"
        total = _as_int(live.get("total"), 0)
        current = _as_int(live.get("current"), 0)
        percent = _as_int(live.get("percent"), 100 if status == "done" else 0)
        message = str(live.get("message") or phase or "Find run history")
        result = {
            "run_id": run_id,
            "artifact_dir": str(directory),
            "find_results_path": str(directory / "find_results.json"),
            "artifact_paths": {
                "find_results": str(directory / "find_results.json"),
                "find_progress": str(directory / "find_progress.json"),
                "article": str(directory / "article.md"),
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
            if stage.startswith(("full-cycle", "environment", "experiment", "paper", "autonomous")):
                job.status = "blocked"
                job.error = ""
                job.set_progress("interrupted", 0, 1, "服务重启，旧 workflow 任务已停止；请以项目 state/full_research_cycle.json 和 evidence gate 为准。")
                job.log("Marked interrupted after server restart; displayed as blocked for TASTE gate visibility.")
            else:
                job.status = "error"
                job.error = job.error or "Server restarted before this job completed."
                job.set_progress("interrupted", 0, 1, job.error)
                job.log("Marked interrupted after server restart.")
        if job.status in {"done", "blocked"}:
            job.result = _fresh_find_result_for_job(job)
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


def start_job(stage: str, fn: Callable[[Callable[[str], None], Callable[[], bool], Callable[[str, int, int, str], None]], Any], job_id: str | None = None) -> JobState:
    job_id = job_id or f"{stage}_{uuid4().hex[:10]}"
    job = JobState(job_id, stage)
    JOBS[job_id] = job
    _persist_jobs()

    def runner() -> None:
        job.status = "running"
        _persist_jobs()
        job.log(f"{stage} started")
        job.set_progress("started", 0, 1, f"{stage} started")
        try:
            job.result = fn(job.log, job.should_cancel, job.set_progress)
            result_status = str(job.result.get("status") or "").lower() if isinstance(job.result, dict) else ""
            if job.cancel_requested:
                job.status = "cancelled"
            elif result_status.startswith("blocked"):
                job.status = "blocked"
            elif result_status == "running":
                job.status = "running"
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
            _persist_jobs()
            if job.status == "cancelled":
                job.cancelled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                job.set_progress("cancelled", 0, 1, f"{stage} cancelled")
            elif job.status == "blocked":
                blocked_message = ""
                if isinstance(job.result, dict):
                    blocker = job.result.get("blocker") if isinstance(job.result.get("blocker"), dict) else {}
                    blocked_message = _human_progress_message(blocker.get("summary") or job.result.get("summary") or blocker, fallback=f"{stage} stopped at an evidence gate")
                job.set_progress("blocked", 1, 1, blocked_message or f"{stage} stopped at an evidence gate")
            elif job.status == "running":
                current = job.progress if isinstance(job.progress, dict) else {}
                job.set_progress(str(current.get("phase") or "running"), int(current.get("current") or 0), int(current.get("total") or 0), str(current.get("message") or f"{stage} detached background worker running"))
            else:
                job.set_progress("complete", 1, 1, f"{stage} complete")
            job.log(f"{stage} {'cancelled' if job.status == 'cancelled' else 'blocked' if job.status == 'blocked' else 'running' if job.status == 'running' else 'complete'}")
            if job.status == "done":
                _auto_email_after_success(stage, job.result)
        except JobCancelled as exc:
            job.status = "cancelled"
            _persist_jobs()
            job.error = str(exc)
            job.cancelled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            job.set_progress("cancelled", 0, 1, str(exc))
            job.log(str(exc))
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.set_progress("error", 0, 1, str(exc))
            job.log(str(exc))
            job.log(traceback.format_exc())
        finally:
            job.done.set()
            _persist_jobs()

    threading.Thread(target=runner, daemon=True).start()
    return job


_load_persisted_jobs()


@app.get("/api/config")
def api_get_config() -> dict:
    return _public_config_response(load_config())


@app.post("/api/config")
def api_save_config(config: AppConfig) -> dict:
    merged = _request_config_with_persisted_secrets(config)
    return _public_config_response(save_config(merged))


@app.post("/api/config/llm-probe")
def api_llm_probe() -> dict:
    """Validate the saved LLM config with the same scoring-shaped probe used by Find."""
    cfg = load_config()
    llm = LLMClient(cfg, "find")
    try:
        result = _llm_live_gate(llm)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "summary": llm.summary(), "probe": "scoring_shape"}
    summary = result.get("summary") if isinstance(result, dict) and isinstance(result.get("summary"), dict) else llm.summary()
    return {
        "ok": bool(isinstance(result, dict) and result.get("ok")),
        "error": str(result.get("error") or result.get("reason") or "")[:800] if isinstance(result, dict) else "LLM probe failed",
        "probe": str(result.get("probe") or "scoring_shape") if isinstance(result, dict) else "scoring_shape",
        "summary": {key: summary.get(key) for key in ["role", "provider", "base_url", "model", "temperature", "enabled", "api_mode"]},
    }


@app.get("/api/config/meta")
def api_config_meta() -> dict:
    return {"saved": CONFIG_PATH.exists()}


@app.get("/api/frontend/version")
def api_frontend_version() -> dict:
    return _frontend_version()


@app.get("/api/catalog/venues")
def api_catalog() -> list[dict]:
    return sorted(
        catalog_by_id().values(),
        key=lambda item: (item["source"], item["field"], item["type"], item["rank"], item["name"], item["id"]),
    )


def _venue_health_timeout_sec() -> float:
    try:
        value = float(os.environ.get("VENUE_HEALTH_TIMEOUT_SEC", "8") or 8)
    except (TypeError, ValueError):
        value = 8.0
    return max(2.0, min(30.0, value))


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


def _fetch_venue_sample_with_timeout(venue: dict, venue_id: str, year: int, sample_limit: int) -> dict:
    timeout_sec = _venue_health_timeout_sec()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="venue-health")
    future = executor.submit(fetch_venue_sample, venue, year, sample_limit)
    try:
        return future.result(timeout=timeout_sec)
    except FutureTimeoutError:
        future.cancel()
        return _venue_health_failure(venue_id, year, f"Venue health check timed out after {timeout_sec:.0f}s.")
    except Exception as exc:
        return _venue_health_failure(venue_id, year, str(exc) or "Venue health check failed.", adapter="error")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@app.post("/api/catalog/venue-health")
def api_venue_health(request: VenueHealthRequest) -> dict:
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
    for venue_id, year in normalize_pairs():
        venue = catalog.get(venue_id)
        if not venue:
            results.append(_venue_health_failure(venue_id, year, "Unknown venue id.", adapter="unknown"))
            continue
        results.append(_fetch_venue_sample_with_timeout(venue, venue_id, year, sample_limit))
    return {"results": results}


@app.post("/api/jobs/find/repair-current")
def api_find_repair_current() -> dict:
    job = start_job("find-repair-current", _repair_current_find_translations)
    job.internal = True
    job.display = "hidden"
    _persist_jobs()
    return job.as_dict()


@app.post("/api/jobs/find")
def api_find(request: FindRequest) -> dict:
    blocker = _new_find_guard_blocker(request)
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    current = _current_project_for_find_guard()
    explicit_reason = _new_find_request_reason(request)
    if current and explicit_reason:
        project, root = current
        _record_new_find_restart_approval(root, project, source="api_jobs_find", reason=explicit_reason)
    # Starting a run must not mutate the persisted config. The UI saves config
    # explicitly via /api/config; API callers may pass temporary run overrides.
    config = _request_config_with_persisted_secrets(request.config)
    if not (str(config.research_interest or "").strip() or str(config.researcher_profile or "").strip()):
        return JSONResponse(
            status_code=400,
            content={
                "code": "research_profile_required",
                "message": "Research interest/profile is required before starting Find; otherwise final title+abstract LLM scoring is skipped.",
            },
        )
    selection = normalize_source_selection(request.selection.model_dump() if request.selection else canonical_source_selection(project_config_path=project_config_path()))
    selection_type = type(request.selection) if request.selection else FindRequest.model_fields["selection"].default_factory().__class__

    def run_find_and_adopt(log, should_cancel, progress):
        result = run_find(FindRequest(config=config, selection=selection_type(**selection)), log, should_cancel, progress)
        run_id = str((result if isinstance(result, dict) else {}).get("run_id") or "").strip()
        result_dict = result if isinstance(result, dict) else {}
        target = current or _current_project_for_find_guard()
        gate_metrics = _find_adoption_gate_metrics(run_id, result_dict)
        target_count = int(gate_metrics["target_count"])
        shortfall = int(gate_metrics["shortfall"])
        strong_count = int(gate_metrics["strong_count"])
        result_status = str(gate_metrics.get("status") or "").lower()
        adoption_allowed = bool(
            run_id
            and target
            and not should_cancel()
            and not result_status.startswith("blocked")
            and target_count > 0
            and strong_count >= target_count
            and shortfall == 0
        )
        if adoption_allowed:
            project, root = target
            receipt = _adopt_find_run_for_project(root, project, run_id, source="api_jobs_find_complete")
            if isinstance(result, dict):
                result = {**result, "project_adoption": receipt}
            log(f"Find run {run_id} adopted as current project Find for {project}: {receipt.get('status')}")
        elif run_id and target:
            log(
                f"Find run {run_id} not adopted: cancel_requested={should_cancel()} "
                f"strong={strong_count}/{target_count or '?'} shortfall={shortfall} status={result_status or 'complete'}"
            )
        return result

    job = start_job("find", run_find_and_adopt)
    return job.as_dict()


def _current_project_find_run_id(root: Path) -> str:
    payload = _read_project_json(root / "planning" / "finding" / "find_results.json", {})
    return str((payload if isinstance(payload, dict) else {}).get("run_id") or "").strip()


def _request_targets_current_project_find(request: ReadRequest, project: str, root: Path) -> bool:
    current_run_id = _current_project_find_run_id(root)
    requested_run_id = str(request.run_id or "").strip()
    return bool(current_run_id and (not requested_run_id or requested_run_id == current_run_id))


def _current_find_read_is_incomplete(root: Path, run_id: str, idea_count: int = 1) -> bool:
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
    if str((read_payload if isinstance(read_payload, dict) else {}).get("source") or "").strip() != "claude_code_current_find_takeover":
        return True
    readings = (read_payload if isinstance(read_payload, dict) else {}).get("readings")
    if not isinstance(readings, list) or not readings:
        return True
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
    return _current_find_read_is_incomplete(root, current_run_id)


def _current_find_downstream_gate_blocker(stage: str) -> dict[str, Any] | None:
    current = _current_project_for_find_guard()
    if not current:
        return None
    project, root = current
    run_id = _current_project_find_run_id(root)
    if not run_id or not _current_find_read_is_incomplete(root, run_id):
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
    JOBS[job.job_id] = job
    job.set_progress("blocked", 1, 1, message)
    job.log(f"{stage} blocked: {message}")
    job.done.set()
    _persist_jobs()
    return job


def _current_find_read_validation_requires_repair(root: Path, run_id: str) -> bool:
    validation = _read_project_json(root / "state" / "current_find_claude_reading_validation.json", {})
    if not isinstance(validation, dict) or validation.get("valid") is True:
        return False
    validation_run_id = str(validation.get("run_id") or "").strip()
    if validation_run_id and validation_run_id != run_id:
        return False
    try:
        pending_count = int(validation.get("pending_full_text_reading_count") or 0)
    except (TypeError, ValueError):
        pending_count = 0
    pending_titles = validation.get("pending_full_text_reading_titles")
    return bool(pending_count > 0 or (isinstance(pending_titles, list) and pending_titles))


def _run_current_find_claude_read_job(project: str, root: Path, request: ReadRequest, log, should_cancel, progress) -> dict:
    run_id = _current_project_find_run_id(root)
    if not run_id:
        raise RuntimeError("current project Find run is missing; run Find before Read")
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")
    progress("current_find_claude_takeover", 0, 1, "主控 Claude Code 正在接管当前 Find 的全文精读、idea 和 plan。")
    management_python = os.environ.get("MANAGEMENT_PYTHON") or sys.executable
    try:
        configured_idea_count = int(getattr(load_config(), "max_ideas", 0) or 0)
    except Exception:
        configured_idea_count = 0
    idea_count = max(1, configured_idea_count or AppConfig().max_ideas)
    repair_mode = _current_find_read_validation_requires_repair(root, run_id) or _current_find_read_is_incomplete(root, run_id, idea_count=idea_count)
    cmd = [
        management_python,
        str(WORKSPACE_ROOT / "scripts" / "ensure_current_find_research_plan.py"),
        "--project",
        project,
        "--read-limit",
        "0",
        "--idea-count",
        str(idea_count),
    ]
    if repair_mode:
        cmd.append("--force")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    env["PROJECT_ID"] = project
    env["DEFAULT_PROJECT_ID"] = project
    env.setdefault("MANAGEMENT_PYTHON", management_python)
    py_entries = [str(WORKSPACE_ROOT / "modules" / "taste"), str(WORKSPACE_ROOT), str(WORKSPACE_ROOT / "scripts")]
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        py_entries.extend(item for item in existing_pythonpath.split(os.pathsep) if item)
    seen_py: set[str] = set()
    env["PYTHONPATH"] = os.pathsep.join(item for item in py_entries if not (item in seen_py or seen_py.add(item)))
    log(("Delegating current Find Read/Idea/Plan repair to wrapper: " if repair_mode else "Delegating current Find Read/Idea/Plan to wrapper: ") + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=str(WORKSPACE_ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    output_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                output_lines.append(line)
                log(line[:1200])
            if should_cancel():
                proc.terminate()
                raise JobCancelled("Task cancelled by user.")
        rc = proc.wait(timeout=5)
    except JobCancelled:
        raise
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
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
    status = str((result_payload if isinstance(result_payload, dict) else {}).get("status") or (current_plan if isinstance(current_plan, dict) else {}).get("status") or "").strip()
    if rc != 0 and not status.startswith("blocked"):
        status = "blocked_current_find_claude_read_failed"
    elif not status:
        status = "current_find_claude_read_complete"
    summary = str((current_plan if isinstance(current_plan, dict) else {}).get("summary_zh") or (current_plan if isinstance(current_plan, dict) else {}).get("summary") or "").strip()
    progress("complete" if rc == 0 else "blocked", 1, 1, summary or status)
    return {
        "status": status,
        "project": project,
        "run_id": run_id,
        "source": "current_find_claude_read_idea_plan_wrapper",
        "repair_mode": repair_mode,
        "idea_count": idea_count,
        "return_code": rc,
        "readings": len((read_payload if isinstance(read_payload, dict) else {}).get("readings") or []),
        "ideas": len((idea_payload if isinstance(idea_payload, dict) else {}).get("ideas") or []),
        "plans": len((plan_payload if isinstance(plan_payload, dict) else {}).get("plans") or []),
        "current_find_research_plan": str(root / "state" / "current_find_research_plan.json"),
        "read_results": str(root / "planning" / "finding" / "read_results.json"),
        "wrapper_result": result_payload,
        "summary": summary,
    }


@app.post("/api/jobs/read")
def api_read(request: ReadRequest) -> dict:
    blocker = _taste_stage_live_full_cycle_blocker("read")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    current = _current_project_for_find_guard()
    if current:
        project, root = current
        if _read_request_should_use_current_find_wrapper(request, project, root):
            current_run_id = _current_project_find_run_id(root)
            job = start_job("read", lambda log, should_cancel, progress: _run_current_find_claude_read_job(project, root, request, log, should_cancel, progress))
            job.run_id = current_run_id
            job.result = {
                "project": project,
                "run_id": current_run_id,
                "source": "current_find_claude_read_idea_plan_wrapper",
                "status": "running",
            }
            _persist_jobs()
            return job.as_dict()
    config = load_config()
    job = start_job("read", lambda log, should_cancel, _progress: run_read(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/idea")
def api_idea(request: IdeaRequest) -> dict:
    blocker = _taste_stage_live_full_cycle_blocker("idea")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    downstream_blocker = _current_find_downstream_gate_blocker("idea")
    if downstream_blocker:
        return _current_find_downstream_blocked_job("idea", downstream_blocker).as_dict()
    config = load_config()
    job = start_job("idea", lambda log, should_cancel, _progress: run_idea(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/plan")
def api_plan(request: PlanRequest) -> dict:
    blocker = _taste_stage_live_full_cycle_blocker("plan")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    downstream_blocker = _current_find_downstream_gate_blocker("plan")
    if downstream_blocker:
        return _current_find_downstream_blocked_job("plan", downstream_blocker).as_dict()
    config = load_config()
    job = start_job("plan", lambda log, should_cancel, _progress: run_plan(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/plan-polish")
def api_plan_polish(request: PlanPolishRequest) -> dict:
    blocker = _taste_stage_live_full_cycle_blocker("plan-polish")
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    downstream_blocker = _current_find_downstream_gate_blocker("plan-polish")
    if downstream_blocker:
        return _current_find_downstream_blocked_job("plan-polish", downstream_blocker).as_dict()
    config = load_config()
    job = start_job("plan-polish", lambda log, should_cancel, _progress: polish_plan(request, config, log, should_cancel))
    return job.as_dict()


@app.post("/api/jobs/email")
def api_email(request: EmailJobRequest) -> dict:
    config = load_config()
    job = start_job("email", lambda log, should_cancel, _progress: send_run_email(request, config, log, should_cancel))
    return job.as_dict()


@app.get("/api/projects")
def api_projects() -> list[dict]:
    return _strip_public_taste_marker(list_projects())


@app.post("/api/projects")
def api_project_create(payload: dict[str, Any]) -> dict:
    try:
        return create_project_config(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/projects/{project}")
def api_project(project: str, compact: bool = Query(True)) -> dict:
    try:
        return _strip_public_taste_marker(project_summary(project, compact=compact))
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


PROJECT_STAGE_EXCLUSIVE_ACTIONS = {"environment", "experiment", "paper", "full-cycle", "full_research_cycle", "autonomous"}
PROJECT_STAGE_EXCLUSIVE_PHASES = {"environment", "experiment", "paper"}


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


def _claude_response_state_path(root: Path, stem: str, session_key: str = "main", suffix: str = ".json") -> Path:
    key = _safe_claude_response_session_key(session_key)
    if key == "main":
        return root / "state" / f"{stem}{suffix}"
    return root / "state" / f"{stem}_{key}{suffix}"


def _claude_response_report_path(root: Path, session_key: str = "main") -> Path:
    key = _safe_claude_response_session_key(session_key)
    if key == "main":
        return root / "reports" / "claude_project_session.md"
    return root / "reports" / f"claude_project_session_{key}.md"


def _claude_response_session_key_from_last_result_path(path: Path) -> str:
    stem = "claude_project_session_last_result"
    name = path.name
    if name == f"{stem}.json":
        return "main"
    if name.startswith(f"{stem}_") and name.endswith(".json"):
        return _safe_claude_response_session_key(name[len(stem) + 1:-5])
    return ""


def _claude_response_stage_keys(stage: Any) -> list[str]:
    panel = _safe_claude_response_session_key(stage).lower()
    if panel == "environment":
        return ["environment"]
    if panel == "experiment":
        return ["experiment"]
    if panel == "paper":
        return ["paper", "writing_revision", "writing_refinement", "paper_preview_repair"]
    return []


def _claude_response_stage_keys_for_root(root: Path, stage: Any) -> list[str]:
    panel = _safe_claude_response_session_key(stage).lower()
    keys = list(_claude_response_stage_keys(panel))
    if panel != "experiment":
        return keys
    state_dir = root / "state"
    if not state_dir.exists():
        return keys
    seen = set(keys)
    for path in sorted(state_dir.glob("claude_project_session_last_result*.json")):
        key = _claude_response_session_key_from_last_result_path(path)
        if not key or key == "main":
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _claude_response_stage_matches(result_stage: Any, panel_stage: str) -> bool:
    stage = str(result_stage or "").strip().lower().replace("_", "-")
    panel = str(panel_stage or "").strip().lower()
    if not stage or panel not in CLAUDE_RESPONSE_PANEL_STAGES:
        return False
    if panel == "environment":
        return stage == "environment" or stage.startswith("environment-") or "environment" in stage
    if panel == "experiment":
        return stage == "experiment" or stage.startswith("experiment-") or any(marker in stage for marker in [
            "experiment",
            "trajectory",
            "autonomous",
            "scientific-progress",
            "iteration",
            "training",
            "blocker",
            "evidence",
            "research",
            "selected-base",
            "safe-unblock",
        ])
    if panel == "paper":
        return stage == "paper" or stage.startswith("paper-") or "paper" in stage or "writing" in stage or "writing" in stage
    return False


def _claude_response_is_current_route_global_result(result: Any) -> bool:
    row = result if isinstance(result, dict) else {}
    haystack = "\\n".join([
        str(row.get("stage") or ""),
        str(row.get("session_key") or ""),
        str(row.get("instruction") or ""),
        str(row.get("prompt_path") or ""),
    ]).lower().replace("_", "-")
    return any(marker in haystack for marker in [
        "current-find-claude-read-idea-plan",
        "current-find-claude-select-plan",
        "current-find-selection",
        "current-find-read-idea-plan",
    ])


def _claude_response_time_value(payload: Any) -> str:
    row = payload if isinstance(payload, dict) else {}
    return str(row.get("finished_at") or row.get("started_at") or "")


def _latest_claude_stage_last_result(root: Path, stage: Any) -> tuple[dict[str, Any], str, str]:
    panel = _safe_claude_response_session_key(stage).lower()
    if panel not in CLAUDE_RESPONSE_PANEL_STAGES:
        result = _read_project_json(root / "state" / "claude_project_session_last_result.json", {})
        return (result if isinstance(result, dict) else {}), "main", ""
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    stale_for_venue = 0
    if panel == "experiment":
        current_route_result = _read_project_json(root / "state" / "claude_project_session_last_result.json", {})
        if isinstance(current_route_result, dict) and _claude_response_is_current_route_global_result(current_route_result):
            candidates.append((_claude_response_time_value(current_route_result), "main", current_route_result))
    for session_key in _claude_response_stage_keys_for_root(root, panel):
        result = _read_project_json(_claude_response_state_path(root, "claude_project_session_last_result", session_key), {})
        if isinstance(result, dict) and result:
            if not _claude_response_stage_matches(result.get("stage"), panel):
                continue
            response, _source = _extract_latest_claude_response(result)
            if panel == "paper" and _paper_receipt_stale_for_current_venue(root, result, response):
                stale_for_venue += 1
                continue
            candidates.append((_claude_response_time_value(result), session_key, result))
    if candidates:
        _stamp, session_key, result = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        fallback_reason = "current_route_global_receipt_for_experiment" if panel == "experiment" and session_key == "main" and _claude_response_is_current_route_global_result(result) else ""
        return result, session_key, fallback_reason
    result = _read_project_json(root / "state" / "claude_project_session_last_result.json", {})
    if isinstance(result, dict) and _claude_response_stage_matches(result.get("stage"), panel):
        response, _source = _extract_latest_claude_response(result)
        if panel == "paper" and _paper_receipt_stale_for_current_venue(root, result, response):
            return {}, panel, "stage_receipt_stale_for_current_venue"
        return result, "main", "historical_global_receipt_for_same_stage"
    return {}, panel, "stage_receipt_stale_for_current_venue" if stale_for_venue else "stage_receipt_not_found"


def _latest_claude_response_payload(root: Path, *, max_chars: int = 1000000, stage: str = "") -> dict[str, Any]:
    requested_stage = _safe_claude_response_session_key(stage).lower() if str(stage or "").strip() else ""
    if requested_stage:
        last_result, session_key, fallback_reason = _latest_claude_stage_last_result(root, requested_stage)
    else:
        last_result = _read_project_json(root / "state" / "claude_project_session_last_result.json", {})
        last_result = last_result if isinstance(last_result, dict) else {}
        session_key = "main"
        fallback_reason = ""
    last_result = last_result if isinstance(last_result, dict) else {}
    response, source = _extract_latest_claude_response(last_result)
    if not response:
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
        "fallback_from_session_key": "main" if fallback_reason in {"historical_global_receipt_for_same_stage", "current_route_global_receipt_for_experiment"} else "",
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
        root = _safe_project_root(project)
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
        return runtime_status(project)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/projects/{project}/runtime/detect")
def api_project_runtime_detect(project: str) -> dict:
    try:
        runtime = detect_runtime_config(project)
        return runtime_status(project) | {"runtime": runtime}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/projects/{project}/runtime")
def api_project_runtime_update(project: str, payload: dict[str, Any]) -> dict:
    try:
        runtime = update_runtime_config(project, payload)
        return runtime_status(project) | {"runtime": runtime}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/projects/{project}/config")
def api_project_config_update(project: str, payload: dict[str, Any]) -> dict:
    try:
        return update_project_config(project, payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/jobs/project")
def api_job(payload: dict[str, Any]) -> dict:
    try:
        blocker = action_gate_blocker(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if blocker:
        return JSONResponse(status_code=409, content=blocker)
    stage = job_stage(payload)
    stage_blocker = _project_stage_running_blocker(payload, stage)
    if stage_blocker:
        return JSONResponse(status_code=409, content=stage_blocker)
    job_id = f"{stage}_{uuid4().hex[:10]}"
    payload_with_job = {**payload, "web_job_id": job_id}
    initial_result = _initial_project_agent_job_result(payload_with_job, stage)
    job = start_job(stage, lambda log, should_cancel, progress: run_action(payload_with_job, log, should_cancel, progress), job_id=job_id)
    if initial_result:
        job.result = initial_result
        panel_stage = str(initial_result.get("panel_stage") or "").strip()
        if panel_stage:
            job.progress = {"phase": panel_stage, "current": 0, "total": 0, "percent": 0, "message": f"项目代理正在处理{_public_stage_label(panel_stage)}请求。"}
            job.progress_version += 1
        _persist_jobs()
    return job.as_dict()


@app.get("/api/projects/{project}/files/{file_path:path}")
def api_project_file(project: str, file_path: str):
    try:
        project_info = project_summary(project)
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
    artifact_roots = [root / "paper" / "output", root / "paper" / "venues", root / "experiments", root / "state"]
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
        return finish_plan(run_id, plan_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)




@app.get("/api/jobs")
def api_jobs(
    compact: bool = Query(True),
    limit: int = Query(300, ge=1, le=1000),
    include_history: bool = Query(True),
) -> list[dict]:
    _reconcile_detached_launcher_jobs()
    dynamic = _live_jobs_from_projects(compact=True)
    if compact:
        effective_limit = min(limit, 30 if include_history else 10)
        persisted = [_compact_job_for_list(job.as_dict(compact=True)) for job in JOBS.values()]
    else:
        effective_limit = limit
        persisted = [job.as_dict(compact=False) for job in JOBS.values()]
    hidden_taskbstages = set()
    dynamic_live_projects = {
        str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "")
        for item in dynamic
        if str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}
    }

    def _hide_persisted_job(item: dict[str, Any]) -> bool:
        if item.get("internal") or str(item.get("display") or "").lower() == "hidden":
            return True
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        stage = str(item.get("stage") or "")
        raw_stage = str(result.get("raw_stage") or stage)
        job_id = str(item.get("job_id") or "")
        if str(result.get("raw_stage") or "") == "find-repair-current" or job_id.startswith(("find-repair-current_", "find-repair-current-")):
            return True
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
                and str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("kind") or "") in {"current_find_claude_read_idea_plan", "current_find_claude_session", "current_find_claude_child"}
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

    def _dedupe_completed_paper_previews(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_key: dict[tuple[str, str], str] = {}
        for item in rows:
            stage = str(item.get("stage") or "")
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            status = str(item.get("status") or "").lower()
            is_paper_preview = _is_paper_job(stage, item.get("job_id", ""), result, item.get("logs")) and status in {"preview_available", "needs_writing", "completed", "done"}
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
            is_paper_preview = _is_paper_job(stage, item.get("job_id", ""), result, item.get("logs")) and status in {"preview_available", "needs_writing", "completed", "done"}
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
    run_history = _find_run_history_jobs_from_runs(existing_find_run_ids, limit=max(0, effective_limit - len(dynamic) - len(persisted_items))) if include_history else []
    items = _dedupe_job_items_for_api(dynamic + persisted_items + run_history)
    if not include_history:
        items = [item for item in items if str(item.get("status") or "").lower() in {"queued", "running", "cancelling"}]
    if compact:
        items = [_compact_job_for_list(item) for item in items]
    return [_strip_public_taste_marker(item) for item in items[:effective_limit]]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, compact: bool = Query(False)) -> dict:
    _reconcile_detached_launcher_jobs()
    if job_id:
        live_job = next((item for item in _live_jobs_from_projects(compact=compact) if str(item.get("job_id") or "") == job_id), None)
        if not live_job and job_id.startswith("full_cycle_"):
            project_id = job_id[len("full_cycle_"):]
            live_job = next((item for item in _live_jobs_from_projects(compact=compact) if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") == project_id), None)
        if live_job:
            return _strip_public_taste_marker(live_job)
        if job_id.startswith(("full-cycle_", "full-cycle-", "full_cycle_", "full_cycle-")):
            for item in _live_jobs_from_projects(compact=compact):
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                command_text = str(result.get("command") or result.get("cmd") or "")
                if "run_full_research_cycle.py" in command_text and str(item.get("status") or "").lower() in {"queued", "running", "cancelling", "blocked"}:
                    return _strip_public_taste_marker({**item, "job_id": job_id})
    job = JOBS.get(job_id)
    if not job:
        if job_id.startswith(("find-run-find_",)):
            run_id = job_id.removeprefix("find-run-")
            history = next((item for item in _find_run_history_jobs_from_runs(set(), limit=300) if str(item.get("run_id") or "") == run_id), None)
            if history:
                return _strip_public_taste_marker(_compact_job_for_list(history) if compact else history)
        return JSONResponse({"error": "job not found"}, status_code=404)
    return _strip_public_taste_marker(job.as_dict(compact=compact))


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str) -> dict:
    known_job = JOBS.get(job_id)
    if job_id:
        _LIVE_JOBS_CACHE.clear()
        live_items = _live_jobs_from_projects(compact=False)
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
            _LIVE_JOBS_CACHE.clear()
            _persist_jobs()
            return {**_strip_public_taste_marker(exact_job), "cancel_requested": True, "status": "cancelling", "termination": termination}
        project_id = _project_from_job_payload(job_id, live_job, known_job)
        phase_hint = _phase_hint_from_job(job_id, live_job, known_job)
        if not live_job and project_id and not worker_pid:
            live_job = next((item for item in live_items if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") == project_id), None)
            phase_hint = phase_hint or _phase_hint_from_job(job_id, live_job, known_job)
        if not project_id and job_id.startswith(("paper",)):
            phase_hint = "paper"
            recovered_children: list[tuple[str, dict[str, Any]]] = []
            for root in sorted(PROJECT_IDS_ROOT.iterdir()) if PROJECT_IDS_ROOT.exists() else []:
                if not root.is_dir():
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
            return {**_strip_public_taste_marker(live_job), "cancel_requested": True, "status": "cancelling", "termination": termination}
    job = known_job
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    job.request_cancel()
    return job.as_dict()


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        sent = 0
        sent_progress = -1
        while True:
            live_job = None
            if job_id:
                live_job = next((item for item in _live_jobs_from_projects(compact=True) if str(item.get("job_id") or "") == job_id), None)
                if not live_job and job_id.startswith("full_cycle_"):
                    project_id = job_id[len("full_cycle_"):]
                    live_job = next((item for item in _live_jobs_from_projects(compact=True) if str((item.get("result") if isinstance(item.get("result"), dict) else {}).get("project") or "") == project_id), None)
            if live_job:
                live_job = _strip_public_taste_marker(live_job)
                live_status = str(live_job.get("status") or "")
                live_stage = _public_taste_stage(live_job.get("stage"))
                if live_status in {"done", "error", "cancelled", "blocked"} or live_status.startswith("blocked"):
                    compact_live_job = _compact_job_for_list(live_job) if live_stage in {"environment", "experiment"} else live_job
                    for line in (compact_live_job.get("logs") or []):
                        await websocket.send_json({"type": "log", "message": str(line)})
                    await websocket.send_json({"type": "progress", "progress": _strip_public_taste_marker(compact_live_job.get("progress") or {})})
                    await websocket.send_json({"type": "complete", "job": compact_live_job})
                    return
                logs = [str(line) for line in (live_job.get("logs") or [])]
                new_logs = logs[sent:]
                if live_stage in {"environment", "experiment"}:
                    new_logs = _public_job_logs(live_job.get("stage"), new_logs, {}, {}, limit=6)
                for line in new_logs:
                    await websocket.send_json({"type": "log", "message": line})
                sent = len(logs)
                live_progress_payload = _strip_public_taste_marker(live_job.get("progress") or {})
                if live_stage in {"environment", "experiment"}:
                    compact_live_job = _compact_job_for_list(live_job)
                    live_progress_payload = _strip_public_taste_marker(compact_live_job.get("progress") or live_progress_payload)
                await websocket.send_json({"type": "progress", "progress": live_progress_payload})
                await asyncio.sleep(2.0)
                continue
            job = JOBS.get(job_id)
            if not job:
                await websocket.send_json({"type": "error", "message": "job not found"})
                return
            job_stage = _public_taste_stage(job.stage)
            if job.status in {"done", "error", "cancelled", "blocked"}:
                compact_job = _compact_job_for_list(job.as_dict(compact=True)) if job_stage in {"environment", "experiment"} else job.as_dict(compact=True)
                for line in (compact_job.get("logs") or []):
                    await websocket.send_json({"type": "log", "message": str(line)})
                await websocket.send_json({"type": "progress", "progress": _strip_public_taste_marker(compact_job.get("progress") or {})})
                await websocket.send_json({"type": "complete", "job": compact_job})
                return
            new_logs = _strip_public_taste_marker(job.logs[sent:])
            if job_stage in {"environment", "experiment"}:
                new_logs = _public_job_logs(job.stage, new_logs, {}, {}, limit=6)
            for line in new_logs:
                await websocket.send_json({"type": "log", "message": line})
            sent = len(job.logs)
            if job.progress_version != sent_progress:
                job_progress_payload = _strip_public_taste_marker(job.progress)
                if job_stage in {"environment", "experiment"}:
                    compact_job = _compact_job_for_list(job.as_dict(compact=True))
                    job_progress_payload = _strip_public_taste_marker(compact_job.get("progress") or job_progress_payload)
                await websocket.send_json({"type": "progress", "progress": job_progress_payload})
                sent_progress = job.progress_version
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


@app.get("/api/runs")
def api_runs() -> list[dict]:
    return _cached_list_runs()


@app.get("/api/runs/{run_id}/artifacts")
def api_artifacts(run_id: str, light: bool = Query(False)) -> dict:
    light_mode = bool(light) if isinstance(light, bool) else bool(getattr(light, "default", False))
    directory = run_dir(run_id)
    project_root = _project_root_for_find_run(run_id)

    def artifact_path(name: str) -> Path:
        return _project_taste_artifact_path(project_root, run_id, name) or (directory / name)

    markdown_names = LIGHT_ARTIFACT_MARKDOWN_NAMES if light_mode else MARKDOWN_ARTIFACT_NAMES
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
    cache_key = json.dumps([run_id, "light" if light_mode else "full", mtimes], separators=(",", ":"), ensure_ascii=False)
    cached = _RUN_ARTIFACTS_CACHE.get(run_id)
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
            artifacts.append({"name": name, "kind": "markdown", "content": _public_text(content), "path": str(path), "content_truncated": False, "size_bytes": size_bytes})
    for name in json_names:
        path = artifact_path(name)
        if path.exists():
            try:
                stat = path.stat()
            except OSError:
                stat = None
            if name == "find_results.json" and stat is not None and stat.st_size > LARGE_JSON_ARTIFACT_LIMIT_BYTES:
                content = _compact_large_find_results_artifact(directory, run_id, stat.st_size)
                artifacts.append({"name": name, "kind": "json", "content": content, "path": str(path), "content_truncated": True, "size_bytes": stat.st_size})
                continue
            content = read_json(path, {})
            if name == "config.json" and isinstance(content, dict):
                content = redacted_config(content)
            artifacts.append({"name": name, "kind": "json", "content": _strip_public_taste_marker(content), "path": str(path), "content_truncated": False, "size_bytes": stat.st_size if stat is not None else 0})
    payload = {"run_id": run_id, "artifacts": artifacts}
    _RUN_ARTIFACTS_CACHE[run_id] = {"key": cache_key, "expires_at": now + RUN_ARTIFACTS_CACHE_TTL_SEC, "payload": payload}
    return payload


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str) -> dict:
    delete_run(run_id)
    _clerun_caches(run_id)
    return {"status": "ok", "run_id": run_id}


@app.patch("/api/runs/{run_id}/ideas/{idea_id}")
def api_patch_idea(run_id: str, idea_id: str, patch: IdeaPatch) -> dict:
    return patch_idea(run_id, idea_id, patch)


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
  <p>Build the frontend with <code>npm run build</code> in <code>auto_research/web/client</code>.</p>
</body></html>
"""
    )
