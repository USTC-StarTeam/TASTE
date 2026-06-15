from __future__ import annotations

import json
import difflib
import datetime as dt
import os
import re
import time
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path(__file__).resolve().parents[4]).expanduser().resolve()
FRAMEWORK_SCRIPTS = ROOT / "framework" / "scripts"
if str(FRAMEWORK_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_SCRIPTS))
from taste_pythonpath import ensure_taste_pythonpath, script_resolver
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection, filter_papers_by_source_selection, filter_source_status_by_selection, normalize_source_selection
from auto_research.paths import CONFIG_PATH, LOCAL_DATABASE_DIR, RUNS_DIR
from auto_research.jobs import JobCancelled
from agent_state import append_agent_log, list_agents, queue_guidance, refresh_process_flags, upsert_agent
from runtime_env import detect_project_runtime, interactive_env, project_runtime_config, runtime_diagnostics, update_project_runtime
from project_paths import management_python
from project_config import create_project_settings, project_source_selection, project_target_venue, update_project_settings
from run_project import current_find_execution_contract
from paper_common import get_active_paper_state

PROJECTS = ROOT / "projects"
SCRIPTS = script_resolver(ROOT)

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
ProgressFn = Callable[[str, int, int, str], None]

SAFE_PROJECT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
_PROCESS_ROWS_CACHE: dict[str, Any] = {"expires_at": 0.0, "rows": []}
_PROJECT_SUMMARY_CACHE: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}
_EXPERIMENT_SYNC_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RUNTIME_DIAGNOSTICS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
COMPACT_PROJECT_SUMMARY_TTL_SEC = float(os.environ.get("COMPACT_PROJECT_SUMMARY_TTL_SEC", "2.0") or 2.0)
PROCESS_ROWS_TTL_SEC = float(os.environ.get("PROCESS_ROWS_TTL_SEC", "1.0") or 1.0)
EXPERIMENT_SYNC_TTL_SEC = float(os.environ.get("EXPERIMENT_SYNC_TTL_SEC", "30.0") or 30.0)
RUNTIME_DIAGNOSTICS_TTL_SEC = float(os.environ.get("RUNTIME_DIAGNOSTICS_TTL_SEC", "300.0") or 300.0)


def _safe_project(name: str) -> str:
    project = str(name or "").strip()
    if not project or any(ch not in SAFE_PROJECT_CHARS for ch in project):
        raise ValueError("Invalid project name. Use only letters, numbers, dash, underscore, and dot.")
    path = (PROJECTS / project).resolve()
    if PROJECTS.resolve() not in path.parents or not path.exists():
        raise ValueError(f"Project not found: {project}")
    return project




def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _current_find_selected_execution_summary(root: Path) -> dict[str, Any]:
    paths = type("CurrentFindExecutionPaths", (), {"state": root / "state", "planning": root / "planning"})()
    try:
        contract = current_find_execution_contract(paths)
    except Exception as exc:
        return {
            "required": False,
            "status": "contract_error",
            "selected_plan_id": "",
            "selected_idea_id": "",
            "reason": str(exc),
            "source": "current_find_execution_contract",
        }
    return contract if isinstance(contract, dict) else {}



def _current_find_selected_plan_gate_public(pipeline: Any) -> dict[str, Any]:
    current = pipeline if isinstance(pipeline, dict) else {}
    selected = current.get("selected_execution") if isinstance(current.get("selected_execution"), dict) else {}

    def to_int(value: Any) -> int:
        try:
            if value in (None, ""):
                return 0
            return int(value)
        except (TypeError, ValueError):
            return 0

    status = str(current.get("status") or selected.get("status") or "").strip()
    issue = str(
        current.get("selected_execution_issue")
        or selected.get("selection_issue")
        or current.get("failure_type")
        or ""
    ).strip()
    selected_plan_id = str(current.get("selected_plan_id") or selected.get("selected_plan_id") or "").strip()
    candidate_counts = current.get("candidate_counts") if isinstance(current.get("candidate_counts"), dict) else selected.get("candidate_counts") if isinstance(selected.get("candidate_counts"), dict) else {}
    ideas = to_int(current.get("ideas") or current.get("idea_count") or candidate_counts.get("ideas"))
    plans = to_int(current.get("plans") or current.get("plan_count") or candidate_counts.get("plans"))
    content_ready = bool(current.get("content_ready") or current.get("read_idea_plan_ready"))
    blocking_statuses = {"blocked_missing_selected_plan", "blocked_ambiguous_selected_plan"}
    blocking_issues = {
        "missing_selected_plan",
        "ambiguous_selected_plan",
        "selected_plan_id_missing",
        "selected_plan_missing_matching_idea",
    }
    blocked = bool(
        content_ready
        and (ideas or plans)
        and (
            status in blocking_statuses
            or issue in blocking_issues
            or (not selected_plan_id and bool(selected.get("required")))
        )
    )
    public_status = "blocked_ambiguous_selected_plan" if (status == "blocked_ambiguous_selected_plan" or issue == "ambiguous_selected_plan") else "blocked_missing_selected_plan"
    summary = "当前 Find 推荐、全文精读、idea 和 plan 已就绪，但主控 Claude Code 还没有从候选 plan 中选出唯一 selected_plan_id；环境、实验、论文和结论提升保持阻断。"
    next_action = "重新运行当前 Find 的 Claude 接管/选择阶段：主控 Claude Code 必须基于完整精读结果比较所有候选 idea/plan，只写入一个 selected_plan_id 或只标记一个 selected_for_execution/execute_next；其他候选保留为 backlog。"
    return {
        "blocked": blocked,
        "status": public_status,
        "category": "current_find_selected_plan_gate",
        "title": "等待唯一执行计划",
        "summary": summary,
        "next_action": next_action,
        "selection_issue": issue or "missing_selected_plan",
        "selected_plan_id": selected_plan_id,
        "candidate_counts": {"ideas": ideas, "plans": plans},
    }

def _venue_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-") or "venue"


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


def _display_venue(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    lowered = text.lower()
    acronym_prefixes = ('iclr', 'icml', 'kdd', 'cikm', 'acl', 'emnlp', 'aaai', 'cvpr', 'eccv', 'neurips')
    return text.upper() if lowered.startswith(acronym_prefixes) else text


def _project_configured_venue(cfg: dict[str, Any] | None, fallback: str = '') -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    paper = cfg.get('paper') if isinstance(cfg.get('paper'), dict) else {}
    return str(cfg.get('target_venue') or cfg.get('venue') or paper.get('target_venue') or fallback or '').strip()


def _paper_receipt_venue_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _project_configured_venue_slug(root: Path) -> str:
    cfg = _read_json(root / "project.json", {})
    cfg = cfg if isinstance(cfg, dict) else {}
    paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    venue = cfg.get("target_venue") or cfg.get("venue") or paper.get("target_venue") or paper.get("venue") or paper.get("venue_slug") or ""
    return _paper_receipt_venue_slug(venue)


def _paper_receipt_stale_for_current_venue(root: Path, receipt: Any) -> bool:
    current_slug = _project_configured_venue_slug(root)
    if not current_slug:
        return False
    row = receipt if isinstance(receipt, dict) else {}
    for key in ["target_venue", "venue", "venue_slug", "active_venue"]:
        value = str(row.get(key) or "").strip()
        if value:
            slug = _paper_receipt_venue_slug(value)
            if slug and slug != current_slug:
                return True
    text = "\n".join([
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


def _paper_preferences_for_venue(paper: dict[str, Any] | None, venue: str) -> dict[str, Any]:
    out = dict(paper or {}) if isinstance(paper, dict) else {}
    normalized = _display_venue(venue)
    if not normalized:
        return out
    lowered = normalized.lower()
    out["target_venue"] = normalized
    out["venue_slug"] = _venue_slug(normalized)
    if lowered.startswith("iclr"):
        out["template_family"] = "iclr"
        out["template_source_url"] = "https://github.com/ICLR/Master-Template"
    elif "nature" in lowered:
        out["template_family"] = "springer-nature"
        out["template_source_url"] = "https://www.springernature.com/gp/authors/campaigns/latex-author-support"
    elif out.get("template_family") in {"iclr", "springer-nature"}:
        out.pop("template_family", None)
        out.pop("template_source_url", None)
    return out


def _active_paper_state(root: Path, project: str, cfg: dict[str, Any] | None = None, venue: str = '') -> dict[str, Any]:
    target = venue or _project_configured_venue(cfg)
    try:
        state = get_active_paper_state(project, venue=target)
    except Exception:
        state = _read_json(root / 'paper' / 'metadata' / 'paper_pipeline.json', {})
    return state if isinstance(state, dict) else {}


def _paper_template_fetched(paper_state: dict[str, Any] | None) -> bool:
    if not isinstance(paper_state, dict):
        return False
    if paper_state.get("template_fetched") is True:
        return True
    template_source = paper_state.get("template_source") if isinstance(paper_state.get("template_source"), dict) else {}
    source_status = str(template_source.get("status") or "").strip().lower()
    if source_status in {"ok", "pass", "ok-official-template"}:
        return True
    if bool(paper_state.get("venue_template_format_ready")):
        return True
    return str(paper_state.get("paper_venue_format_status") or "").strip().lower() == "pass"


def _venue_requirements_summary(root: Path, venue: str, paper_state: dict[str, Any] | None = None) -> dict[str, Any]:
    paper_state = paper_state if isinstance(paper_state, dict) else {}
    slug = _venue_slug(venue or paper_state.get("venue") or "")
    req_path = root / "paper" / "venues" / slug / "venue_requirements.json"
    req = _read_json(req_path, {})
    if not isinstance(req, dict) or not req:
        policy = paper_state.get("venue_submission_policy") if isinstance(paper_state.get("venue_submission_policy"), dict) else {}
        return {
            "status": "missing",
            "venue": str(venue or paper_state.get("venue") or ""),
            "path": str(req_path),
            "summary": "目标 venue 官方模板和投稿要求尚未解析；writing 不应猜测页数、模板或引用要求。",
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
        "summary": "；".join(bits) if bits else "目标 venue 官方要求已解析。",
    }


def _runtime_diagnostics_light(project: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    runtime = project_runtime_config(project, cfg)
    try:
        path_head = interactive_env(project, cfg).get("PATH", "").split(os.pathsep)[:12]
    except Exception:
        path_head = []

    def check_path(path_value: Any, missing_reason: str) -> dict[str, Any]:
        path = str(path_value or "").strip()
        ok = bool(path and Path(path).expanduser().exists())
        return {"path": path, "ok": ok, "version": "", "reason": "ok" if ok else missing_reason}

    node_bin = Path(str(runtime.get("node_bin") or "")).expanduser() if runtime.get("node_bin") else Path("")
    conda_base = Path(str(runtime.get("conda_base") or "")).expanduser() if runtime.get("conda_base") else Path("")
    management_python = runtime.get("management_python") or runtime.get("python_executable")
    experiment_python = runtime.get("experiment_python")
    management_check = check_path(management_python, "management Python does not exist")
    experiment_check = check_path(experiment_python, "experiment Python does not exist")
    checks = {
        "node": check_path(node_bin / "node" if str(node_bin) else "", "node not found in configured node_bin"),
        "npm": check_path(node_bin / "npm" if str(node_bin) else "", "npm not found in configured node_bin"),
        "claude": check_path(runtime.get("claude_path"), "claude_path does not exist"),
        "management_python": management_check,
        "experiment_python": experiment_check,
        "python": management_check,
        "conda": check_path(conda_base / "bin" / "conda" if str(conda_base) else "", "conda not found under conda_base"),
        "conda_base": check_path(conda_base / "etc" / "profile.d" / "conda.sh" if str(conda_base) else "", "conda.sh not found under conda base"),
    }
    return {"project": project, "runtime": runtime, "checks": checks, "path_head": path_head, "status": "ok" if all(row.get("ok") for row in checks.values()) else "needs_attention", "diagnostic_mode": "compact_cached_paths"}


def _cached_runtime_diagnostics(project: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.monotonic()
    cached = _RUNTIME_DIAGNOSTICS_CACHE.get(project)
    if cached and cached[0] > now:
        return cached[1]
    return _runtime_diagnostics_light(project, cfg)

def _public_project_identity_config(project: str, cfg: dict[str, Any] | None = None, topic: str = "") -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "name": cfg.get("name") or project,
        "topic": topic or cfg.get("topic") or "",
    }


def _public_run_preferences(project: str, root: Path, cfg: dict[str, Any] | None = None, runtime_public: dict[str, Any] | None = None, selection: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    venue = _display_venue(cfg.get("target_venue") or cfg.get("venue") or paper.get("target_venue") or "")
    paper = _paper_preferences_for_venue(paper, venue)
    out = {
        "user_prompt": cfg.get("user_prompt", ""),
        "title": cfg.get("title", ""),
        "target_venue": venue,
        "venue": venue,
        "research_interest": cfg.get("research_interest", ""),
        "researcher_profile": cfg.get("researcher_profile", ""),
        "conda_env": cfg.get("conda_env", ""),
        "coding_agent": {"backend": ((cfg.get("coding_agent") or {}) if isinstance(cfg.get("coding_agent"), dict) else {}).get("backend", "")},
        "paper": paper,
        "default_find_selection": selection if isinstance(selection, dict) else (_current_project_source_selection(project, root) if project else canonical_source_selection()),
    }
    if isinstance(runtime_public, dict):
        out["runtime"] = runtime_public
    return out


def _project_summary_public_identity(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    prefs = payload.get("run_preferences") if isinstance(payload.get("run_preferences"), dict) else {}
    if not prefs:
        prefs = {
            key: cfg.get(key, "")
            for key in ["user_prompt", "title", "target_venue", "venue", "research_interest", "researcher_profile", "conda_env", "paper", "default_find_selection", "coding_agent", "runtime"]
            if key in cfg
        }
    identity = _public_project_identity_config(str(payload.get("project") or cfg.get("name") or ""), cfg, str(payload.get("topic") or cfg.get("topic") or ""))
    paper_prefs = prefs.get("paper") if isinstance(prefs.get("paper"), dict) else {}
    venue = _display_venue(
        prefs.get("target_venue")
        or prefs.get("venue")
        or paper_prefs.get("target_venue")
        or payload.get("target_venue")
        or payload.get("venue")
        or cfg.get("target_venue")
        or cfg.get("venue")
        or ""
    )
    if venue:
        prefs = {**prefs, "target_venue": venue, "venue": venue}
    payload["config"] = identity
    payload["topic"] = identity.get("topic", "")
    payload["run_preferences"] = prefs
    if venue:
        payload["target_venue"] = venue
        payload["venue"] = venue
    else:
        payload.pop("target_venue", None)
        payload.pop("venue", None)
    return payload

def _cleruntime_caches(project: str = "") -> None:
    if project:
        _RUNTIME_DIAGNOSTICS_CACHE.pop(project, None)
        _EXPERIMENT_SYNC_CACHE.pop(project, None)
        for key in list(_PROJECT_SUMMARY_CACHE):
            if key[0] == project:
                _PROJECT_SUMMARY_CACHE.pop(key, None)
    else:
        _RUNTIME_DIAGNOSTICS_CACHE.clear()
        _EXPERIMENT_SYNC_CACHE.clear()
        _PROJECT_SUMMARY_CACHE.clear()


def _source_selection_matches_current(project_id: str, selection: Any) -> bool:
    if not project_id or not isinstance(selection, dict):
        return False
    current = project_source_selection(project_id)
    normalized = normalize_source_selection(selection)
    return normalized == current


def _latest_find_run_id_from_runs() -> str:
    runs_dir = RUNS_DIR
    try:
        candidates = [path for path in runs_dir.glob("find_*") if path.is_dir()]
    except Exception:
        return ""
    if not candidates:
        return ""
    return sorted(candidates, key=lambda path: path.name)[-1].name


def _current_find_run_id_from_state(root: Path) -> str:
    # Prefer small state/progress files. find_results.json can be multi-MB and
    # compact project summaries call this helper several times per refresh.
    for rel in [
        ("planning", "finding", "find_progress.json"),
        ("state", "current_find_recommendation_projection.json"),
        ("state", "current_find_research_plan.json"),
        ("state", "literature_tool_packet.json"),
        ("state", "finding_frontend.json"),
        ("planning", "finding", "find_results.json"),
    ]:
        payload = _read_json(root.joinpath(*rel), {})
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or "").strip()
        if run_id:
            return run_id
    return ""


def _payload_run_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("run_id") or value.get("source_run_id") or value.get("find_run_id") or value.get("current_find_run_id") or "").strip()


def _payload_matches_current_run(value: Any, current_run_id: str, *, allow_unversioned: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    expected = str(current_run_id or "").strip()
    actual = _payload_run_id(value)
    if not expected:
        return bool(actual) or allow_unversioned
    if not actual:
        return allow_unversioned
    return actual == expected


def _current_find_recommendation_projection(root: Path, run_id: str = "") -> dict[str, Any]:
    projection = _read_json(root / "state" / "current_find_recommendation_projection.json", {})
    if not isinstance(projection, dict):
        return {}
    expected = str(run_id or "").strip()
    actual = _payload_run_id(projection)
    if expected and actual != expected:
        return {}
    if expected and not actual:
        return {}
    return projection

def _current_find_results_light(root: Path, project_id: str = "") -> dict[str, Any]:
    """Return compact current Find metadata without hydrating large find_results.json."""
    progress = _read_json(root / "planning" / "finding" / "find_progress.json", {})
    plan = _read_json(root / "state" / "current_find_research_plan.json", {})
    packet = _read_json(root / "state" / "literature_tool_packet.json", {})
    frontend = _read_json(root / "state" / "finding_frontend.json", {})
    current_run_id = _current_find_run_id_from_state(root)
    def _same_current_run(source: Any, *, allow_unversioned: bool = False) -> bool:
        return _payload_matches_current_run(source, current_run_id, allow_unversioned=allow_unversioned)

    payload: dict[str, Any] = {}
    if current_run_id:
        payload["run_id"] = current_run_id
        payload["source_run_id"] = current_run_id
    skipped_keys = {"readings", "ideas", "plans", "strong_papers", "audit_only_candidates", "critique_candidates", "repo_candidates", "run_id", "source_run_id", "find_run_id"}
    for source in [progress, packet, plan]:
        if isinstance(source, dict) and _same_current_run(source):
            for key, value in source.items():
                if key not in skipped_keys and key not in payload:
                    payload[key] = value
            run_id = _payload_run_id(source)
            if run_id and not payload.get("run_id"):
                payload["run_id"] = run_id
                payload["source_run_id"] = run_id
    if isinstance(progress, dict):
        counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
        payload.setdefault("counts", counts)
        payload.setdefault("strong_recommendation_count", progress.get("strong_recommendation_count", 0))
        payload["recommendation_target_count"] = progress.get("recommendation_target_count", payload.get("recommendation_target_count", 0))
        payload["recommendation_shortfall"] = progress.get("recommendation_shortfall", payload.get("recommendation_shortfall", 0))
        if isinstance(progress.get("source_status"), list):
            payload.setdefault("source_status", progress.get("source_status"))
        if isinstance(progress.get("venue_health_report"), list):
            payload.setdefault("venue_health_report", progress.get("venue_health_report"))
        if isinstance(progress.get("selection"), dict):
            payload.setdefault("selection", normalize_source_selection(progress.get("selection")))
    if isinstance(frontend, dict) and _same_current_run(frontend):
        survey_stats = frontend.get("survey_stats") if isinstance(frontend.get("survey_stats"), dict) else {}
        if survey_stats:
            payload.setdefault("survey_stats", survey_stats)
            counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
            for key in [
                "raw_title_index_papers",
                "venue_total_papers_available",
                "venue_corpus_audited_papers",
                "venue_category_selected_papers",
                "category_corpus_audited_papers",
                "category_filtered_papers",
                "tfidf_screened_papers",
                "venue_title_filter_input_papers",
                "title_score_input_papers",
                "llm_title_scored_papers",
                "venue_final_title_candidates",
                "abstract_scored_papers",
                "recommended_papers",
                "venue_detail_fetched_candidates",
                "venue_evaluated_candidates",
                "llm_scored_candidates",
                "abstract_fetch_failed_candidates",
                "category_scan_reports",
                "title_filter_reports",
            ]:
                if survey_stats.get(key) not in (None, "") and counts.get(key) in (None, "", 0):
                    counts[key] = survey_stats.get(key)
            payload["counts"] = counts
    projection = _current_find_recommendation_projection(root, current_run_id)
    if projection:
        raw_recommendation_rows = projection.get("strong_recommendations") if isinstance(projection.get("strong_recommendations"), list) else projection.get("recommendations") if isinstance(projection.get("recommendations"), list) else projection.get("articles") if isinstance(projection.get("articles"), list) else []
        recommendation_rows = _human_recommendation_literature_rows(raw_recommendation_rows)
        raw_read_rows = projection.get("read_candidates") if isinstance(projection.get("read_candidates"), list) else []
        read_rows = _human_readable_literature_rows(raw_read_rows) or recommendation_rows
        if recommendation_rows:
            payload["strong_recommendations"] = recommendation_rows
            payload["articles"] = recommendation_rows
        if read_rows:
            payload["read_candidates"] = read_rows
        projection_counts = projection.get("counts") if isinstance(projection.get("counts"), dict) else {}
        projection_survey_stats = projection.get("survey_stats") if isinstance(projection.get("survey_stats"), dict) else {}
        if projection_survey_stats:
            payload["survey_stats"] = projection_survey_stats
        if projection_counts or projection_survey_stats:
            counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
            counts.update(projection_counts)
            counts.update(projection_survey_stats)
            if raw_recommendation_rows or raw_read_rows:
                counts["strong_recommendations"] = len(recommendation_rows)
                counts["recommended"] = len(recommendation_rows)
                counts["strict_strong_anchor_count"] = len(recommendation_rows)
                counts["read_candidates"] = len(read_rows)
            payload["counts"] = counts
        for key in ["recommendation_target_count", "recommendation_quality", "coverage_explanation_i18n", "semantics"]:
            if projection.get(key) not in (None, "", []):
                payload[key] = projection.get(key)
        target = _as_int(payload.get("recommendation_target_count") or projection_counts.get("recommendation_target_count"), 0)
        if raw_recommendation_rows or raw_read_rows:
            payload["strict_strong_anchor_count"] = len(recommendation_rows)
            payload["recommendation_shortfall"] = max(0, target - len(recommendation_rows)) if target else 0
        elif projection.get("recommendation_shortfall") not in (None, "", []):
            payload["recommendation_shortfall"] = projection.get("recommendation_shortfall")
    if isinstance(packet, dict) and _same_current_run(packet):
        coverage = packet.get("coverage") if isinstance(packet.get("coverage"), dict) else {}
        payload.setdefault("coverage", coverage)
        if isinstance(coverage, dict):
            counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
            for key in [
                "raw_title_index_papers",
                "venue_total_papers_available",
                "venue_corpus_audited_papers",
                "venue_category_selected_papers",
                "venue_title_filter_input_papers",
                "venue_final_title_candidates",
                "venue_detail_fetched_candidates",
                "venue_evaluated_candidates",
                "llm_scored_candidates",
                "strong_recommendations",
            ]:
                if key == "strong_recommendations":
                    continue
                if coverage.get(key) not in (None, "") and counts.get(key) in (None, "", 0):
                    counts[key] = coverage.get(key)
            payload["counts"] = counts
        strong_papers = packet.get("strong_papers") if isinstance(packet.get("strong_papers"), list) else []
        filtered_strong_papers = _human_recommendation_literature_rows(strong_papers)
        if strong_papers:
            payload["strict_strong_anchors"] = filtered_strong_papers
            payload.setdefault("strict_strong_anchor_count", len(filtered_strong_papers))
        if filtered_strong_papers and not isinstance(payload.get("strong_recommendations"), list):
            payload.setdefault("strong_recommendations", filtered_strong_papers)
        if filtered_strong_papers and not isinstance(payload.get("articles"), list):
            payload.setdefault("articles", filtered_strong_papers)
    if isinstance(plan, dict) and _same_current_run(plan):
        payload.setdefault("read_candidates", plan.get("readings") if isinstance(plan.get("readings"), list) else [])
        payload.setdefault("ideas", plan.get("ideas") if isinstance(plan.get("ideas"), list) else [])
        payload.setdefault("plans", plan.get("plans") if isinstance(plan.get("plans"), list) else [])
    selection = payload.get("selection")
    if project_id and isinstance(selection, dict) and not _source_selection_matches_current(project_id, selection):
        return {
            "stale_for_current_selection": True,
            "run_id": payload.get("run_id", ""),
            "selection": normalize_source_selection(selection),
            "current_selection": project_source_selection(project_id),
        }
    return payload



def _pid_alive(pid: Any) -> bool:
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


def _command_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value or "")


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



def _is_current_find_read_worker_cmd(cmd: str) -> bool:
    lowered = str(cmd or "").lower()
    return (
        "ensure_current_find_research_plan.py" in lowered
        or ("claude_project_session.py" in lowered and "current-find" in lowered)
    )


def _process_has_current_find_ancestor(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    rows = _remote_process_rows()
    by_pid = {str(item.get("pid") or ""): item for item in rows if isinstance(item, dict)}
    current = str(row.get("pid") or "").strip()
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        item = by_pid.get(current)
        if not item:
            return False
        if _is_current_find_read_worker_cmd(str(item.get("cmd") or "")):
            return True
        current = str(item.get("ppid") or "").strip()
    return False


def _is_real_full_cycle_command(cmd: Any, *, kind: Any = "", stage: Any = "") -> bool:
    lowered = str(cmd or "").lower().replace("_", "-")
    kind_text = str(kind or "").lower().replace("_", "-")
    stage_text = str(stage or "").lower().replace("_", "-")
    if "run-full-research-cycle.py" in lowered or "run_full_research_cycle.py" in lowered:
        return True
    return kind_text == "full-cycle" and "claude" not in lowered and "current-find" not in lowered and stage_text.startswith("full-cycle")


def _full_cycle_job_is_live(job: Any) -> bool:
    src = job if isinstance(job, dict) else {}
    if not src:
        return False
    status = str(src.get("status") or "").strip().lower()
    pid = str(src.get("pid") or "").strip()
    cmd = _command_text(src.get("cmd") or src.get("command"))
    kind = str(src.get("kind") or "")
    stage = str(src.get("stage") or src.get("raw_stage") or "")
    if status not in {"queued", "running", "cancelling"} or src.get("process_alive") is not True or not pid or not _pid_alive(pid):
        return False
    if _is_current_find_read_worker_cmd(cmd) or str(kind).lower().replace("_", "-").startswith("current-find"):
        return False
    if _process_has_current_find_ancestor({"pid": pid}):
        return False
    return _is_real_full_cycle_command(cmd, kind=kind, stage=stage)


def _live_full_cycle_process(root: Path, project_id: str = "", pid: Any = "") -> dict[str, Any] | None:
    pid_text = str(pid or "").strip()
    candidates: list[dict[str, Any]] = []
    root_text = str(root)
    for row in _remote_process_rows():
        if not isinstance(row, dict) or row.get("kind") != "full_cycle":
            continue
        cmd = str(row.get("cmd") or "")
        if not _is_real_full_cycle_command(cmd, kind=row.get("kind"), stage=row.get("stage")):
            continue
        if _process_has_current_find_ancestor(row):
            continue
        if project_id and project_id not in cmd and root_text not in cmd:
            continue
        candidates.append(row)
    if pid_text:
        exact = next((row for row in candidates if str(row.get("pid") or "") == pid_text), None)
        if exact:
            return exact
    return candidates[0] if candidates else None


def _project_command_markers(project_id: str, root: Path) -> list[str]:
    markers = [project_id, str(root)]
    for rel in [
        "state/active_repo.json",
        "state/fresh_research_base.json",
        "state/fresh_base_implementation_plan.json",
        "state/current_find_research_plan.json",
        "project.json",
    ]:
        payload = _read_json(root / rel, {})
        if not isinstance(payload, dict):
            continue
        for key in ["repo_path", "local_path", "path", "selected_repo_path", "active_repo_path"]:
            value = str(payload.get(key) or "").strip()
            if value:
                markers.append(value)
        for key in ["repo", "repo_url", "url", "name", "title"]:
            value = str(payload.get(key) or "").strip()
            if value and len(value) >= 4:
                markers.append(value)
        for nested_key in ["repo", "active_repo", "selected", "fresh_paper_base"]:
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                for key in ["repo_path", "local_path", "path", "repo", "repo_url", "url", "name", "title"]:
                    value = str(nested.get(key) or "").strip()
                    if value and len(value) >= 4:
                        markers.append(value)
    seen: set[str] = set()
    result: list[str] = []
    for marker in markers:
        cleaned = " ".join(str(marker or "").split())
        if len(cleaned) < 4:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _cmd_matches_project(cmd: str, project_id: str, root: Path) -> bool:
    if not cmd:
        return False
    return any(marker and marker in cmd for marker in _project_command_markers(project_id, root))


def _active_project_worker_row(project_id: str, root: Path) -> dict[str, Any] | None:
    for row in _remote_process_rows():
        if not isinstance(row, dict):
            continue
        cmd = str(row.get("cmd") or "")
        kind = str(row.get("kind") or "")
        if _is_current_find_read_worker_cmd(cmd) or _process_has_current_find_ancestor(row):
            continue
        if kind not in {"frontend", "driver", "experiment_or_reproduction", "paper_pipeline", "paper_orchestra", "claude_session", "claude_cli"}:
            continue
        if _cmd_matches_project(cmd, project_id, root):
            return row
    return None


def _has_active_experiment_training(project_id: str, root: Path) -> bool:
    selected_repo_path = _current_selected_repo_path(root)
    for row in _remote_process_rows():
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "") != "experiment_or_reproduction":
            continue
        cmd = str(row.get("cmd") or "")
        if not _looks_like_experiment_training_cmd(cmd):
            continue
        if _cmd_matches_project(cmd, project_id, root):
            return True
        if selected_repo_path and _path_is_within(row.get("cwd"), selected_repo_path):
            return True
    return False


def _public_phase_for_full_cycle(stage: Any, project_id: str, root: Path) -> str:
    if _has_active_experiment_training(project_id, root):
        return "experiment"
    phase = _phase_from_stage(stage)
    return "find" if phase == "literature" else phase


def _read_tail_lines(path_value: Any, max_bytes: int = 24000) -> list[str]:
    path_text = str(path_value or '').strip()
    if not path_text:
        return []
    path = Path(path_text)
    try:
        if not path.exists() or not path.is_file():
            return []
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), 0)
            data = handle.read().decode('utf-8', errors='replace')
        return [line.strip() for line in data.splitlines() if line.strip()]
    except Exception:
        return []


def _public_claude_activity_from_lines(lines: list[str]) -> dict[str, Any]:
    actions: list[str] = []
    stage = ''
    for raw in lines[-160:]:
        line = str(raw or '').strip()
        if not line:
            continue
        if line.startswith('full-cycle: running') and 'claude_project_session.py' in line:
            stage_match = re.search(r'--stage\s+(\S+)', line)
            if stage_match:
                stage = stage_match.group(1)
            continue
        if line.startswith('Claude:'):
            body = line.split(':', 1)[1].strip()
            if body.startswith('调用工具:'):
                body = body.replace('调用工具:', '正在使用工具:', 1).strip()
            if body:
                actions.append(body)
            continue
        if line.startswith('claude:') and 'session initialized' not in line:
            body = line.split(':', 1)[1].strip()
            if body:
                actions.append('Claude 会话：' + body)
    cleaned: list[str] = []
    for item in actions[-8:]:
        item = _public_internal_names(_redact_secrets(item))
        item = re.sub(r'\s+', ' ', item).strip()
        if len(item) > 260:
            item = item[:257] + '...'
        if item and item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        return {}
    latest = cleaned[-1]
    return {
        'stage': stage,
        'summary': '项目 Claude Code 当前动作：' + latest,
        'recent': cleaned[-5:],
    }


def _full_cycle_claude_activity(root: Path, job: Any) -> dict[str, Any]:
    src = job if isinstance(job, dict) else {}
    paths: list[str] = []
    for key in ['log_path', 'stdout_path']:
        value = str(src.get(key) or '').strip()
        if value and value not in paths:
            paths.append(value)
    if not paths:
        state = _read_json(root / 'state' / 'full_research_cycle.json', {})
        current = state.get('current_running_stage') if isinstance(state, dict) else {}
        if isinstance(current, dict):
            text = str(current.get('stdout_tail') or '')
            activity = _public_claude_activity_from_lines(text.splitlines())
            if activity:
                return activity
    for path in paths:
        activity = _public_claude_activity_from_lines(_read_tail_lines(path))
        if activity:
            return activity
    return {}


def _normalize_full_cycle_job(root: Path, project_id: str, job: Any) -> dict[str, Any]:
    src = dict(job) if isinstance(job, dict) else {}
    if not src:
        live = _live_full_cycle_process(root, project_id)
        if not live:
            live = _active_project_worker_row(project_id, root)
        if not live:
            return {}
        normalized = {
            "project": project_id,
            "status": "running",
            "pid": live.get("pid"),
            "cmd": live.get("cmd", ""),
            "process_alive": True,
            "alive": True,
            "elapsed": live.get("elapsed", ""),
            "kind": live.get("kind", "full_cycle"),
        }
        activity = _full_cycle_claude_activity(root, normalized)
        if activity:
            normalized["claude_activity"] = activity
        return normalized
    cmd_text = _command_text(src.get("cmd") or src.get("command"))
    if cmd_text and not src.get("cmd"):
        src["cmd"] = cmd_text
    live = _live_full_cycle_process(root, project_id, src.get("pid"))
    if live:
        live_pid = str(live.get("pid") or "")
        src_pid = str(src.get("pid") or "")
        if str(src.get("status") or "").lower() == "stale" or (src_pid and live_pid and src_pid != live_pid):
            src = {"project": project_id, "venue": src.get("venue", "")}
        src.update({
            "project": src.get("project") or project_id,
            "status": "running",
            "pid": live.get("pid"),
            "cmd": live.get("cmd", ""),
            "command": live.get("cmd", ""),
            "process_alive": True,
            "alive": True,
            "elapsed": live.get("elapsed", ""),
            "kind": live.get("kind", "full_cycle"),
        })
        activity = _full_cycle_claude_activity(root, src)
        if activity:
            src["claude_activity"] = activity
        src.pop("stale_reason", None)
        src.pop("guardrail", None)
        return src
    child = _active_project_worker_row(project_id, root)
    if child and not _is_current_find_read_worker_cmd(str(child.get("cmd") or "")):
        src.update({
            "project": src.get("project") or project_id,
            "status": "running",
            "pid": child.get("pid"),
            "cmd": child.get("cmd", ""),
            "process_alive": True,
            "alive": True,
            "elapsed": child.get("elapsed", ""),
            "kind": child.get("kind", "active_child_worker"),
            "note": "full research cycle wrapper is not live, but a project child worker is still running",
        })
        activity = _full_cycle_claude_activity(root, src)
        if activity:
            src["claude_activity"] = activity
        return src
    if str(src.get("status") or "").lower() == "running":
        src["status"] = "stale"
        src["stale_reason"] = "no_matching_live_full_cycle_process"
    src["process_alive"] = False
    src["alive"] = False
    return src


def _clean_stale_active_worker_text(text: Any, fallback: str = "") -> str:
    cleaned = _public_internal_names(str(text or "").strip())
    fallback_text = _public_internal_names(str(fallback or "").strip())
    if not cleaned:
        return fallback_text
    lower = cleaned.lower()
    terminal_markers = ["没有正在运行", "已停止", "stale", "blocked", "not running", "no running"]
    live_markers = ["pid=", "historical_pid", "正在运行", "running", "active_full_research_cycle_worker", "worker"]
    if any(marker in lower or marker in cleaned for marker in live_markers) and not any(marker in lower or marker in cleaned for marker in terminal_markers):
        return fallback_text or "上一条完整科研循环已结束，当前未检测到活进程。"
    return cleaned


def _public_full_cycle_job(job: Any, *, target_venue: str = "") -> dict[str, Any]:
    """Project-summary job projection: preserve live state, hide stale audit commands."""
    src = dict(job) if isinstance(job, dict) else {}
    if not src:
        return {}
    status = str(src.get("status") or "").strip().lower()
    live = _full_cycle_job_is_live(src)
    allowed = [
        "project", "status", "pid", "process_alive", "alive", "elapsed", "elapsed_sec",
        "kind", "stage", "phase", "raw_stage", "started_at", "updated_at", "log_path",
        "stdout_path", "stale_reason", "note", "web_job_id", "child_pid", "child_stage",
    ]
    out = {key: src.get(key) for key in allowed if key in src and src.get(key) not in (None, "")}
    configured = _display_venue(target_venue)
    if configured:
        out["target_venue"] = configured
    if live:
        if configured:
            out["venue"] = configured
        if isinstance(src.get("claude_activity"), dict):
            out["claude_activity"] = src["claude_activity"]
    else:
        if status == "running":
            out["status"] = "stale"
        out["process_alive"] = False
        out["alive"] = False
        # cmd/command and job-specific venue are historical audit fields. They
        # remain in state files and logs, but must not override the current
        # project venue or appear in human-facing compact project state.
        out.pop("venue", None)
    return out

def _json_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("experiments"), list):
        return [row for row in payload.get("experiments", []) if isinstance(row, dict)]
    return []



def _venue_source_key(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("venue_id", "venue", "source")).lower()
    if "iclr" in text:
        return "iclr"
    if "neurips" in text or "nips" in text:
        return "neurips"
    if "icml" in text:
        return "icml"
    if "kdd" in text or "sigkdd" in text:
        return "kdd"
    return str(row.get("venue_id") or row.get("venue") or row.get("source") or "").strip().lower()


def _manifest_first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _derive_source_scope(adapter: str) -> str:
    key = str(adapter or "").lower()
    if key.startswith("icml_downloads"):
        return "official_icml_downloads_title_index"
    if key.startswith("openreview"):
        return "official_openreview_metadata"
    if key.startswith("dblp"):
        return "dblp_current_index_not_official_accepted_list"
    return ""


def _derive_official_title_index_verified(adapter: str, title_index_complete: bool, explicit: Any = None) -> Any:
    scope = _derive_source_scope(adapter)
    if scope in {"official_icml_downloads_title_index", "official_openreview_metadata"}:
        return bool(title_index_complete)
    if scope == "dblp_current_index_not_official_accepted_list":
        return False
    if explicit not in (None, ""):
        return bool(explicit)
    return bool(title_index_complete) if title_index_complete else None


def _derive_official_accepted_list_verified(adapter: str, title_index_complete: bool, explicit: Any = None) -> Any:
    scope = _derive_source_scope(adapter)
    if scope == "official_icml_downloads_title_index":
        return bool(title_index_complete)
    if scope == "official_openreview_metadata":
        return bool(title_index_complete)
    if scope == "dblp_current_index_not_official_accepted_list":
        return False
    if explicit not in (None, ""):
        return bool(explicit)
    return None


def _venue_source_public_limited(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if not row.get("ok"):
        return bool(row.get("limited") or row.get("metadata_completeness_limited"))
    adapter = str(row.get("adapter") or row.get("source_adapter") or "").lower()
    category_status = str(row.get("category_status") or "").lower()
    has_official_categories = bool(row.get("has_official_categories")) and category_status not in {"no_official_categories", "missing_categories", "no_or_partial_categories"}
    has_abstracts = bool(row.get("has_abstracts_in_title_index") or row.get("has_abstracts") or row.get("any_abstracts"))
    official_openreview_ready = (
        "openreview" in adapter
        and has_official_categories
        and has_abstracts
        and bool(row.get("source_verified") or row.get("official_title_index_verified") or str(row.get("source_scope") or "") == "official_openreview_metadata")
    )
    if official_openreview_ready:
        return False
    return bool(row.get("limited") or row.get("metadata_completeness_limited"))


def _read_local_venue_cache_manifest(venue_id: str, year: int) -> dict[str, Any]:
    venue_id = str(venue_id or "").strip()
    try:
        yeint = int(year)
    except Exception:
        return {}
    if not venue_id or yeint <= 0:
        return {}
    aliases = [venue_id]
    for suffix in ("_2026", "_2025", "_2024", "_2023", "_2022"):
        if venue_id.endswith(suffix):
            aliases.append(venue_id[: -len(suffix)])
    lowered = venue_id.lower()
    if "iclr" in lowered:
        aliases.extend(["openreview_iclr", "openreview_iclr_2026"])
    if "neurips" in lowered or "nips" in lowered:
        aliases.append("openreview_neurips")
    if "icml" in lowered:
        aliases.append("dblp_icml")
    if "kdd" in lowered or "sigkdd" in lowered:
        aliases.append("dblp_kdd")
    aliases = list(dict.fromkeys(item for item in aliases if item))
    local_root = LOCAL_DATABASE_DIR
    for alias in aliases:
        directory = local_root / alias / str(yeint)
        papers_path = directory / "papers.json"
        summary_path = directory / "category_summary.json"
        if not papers_path.exists() or not summary_path.exists():
            continue
        papers_payload = _read_json(papers_path, {})
        summary_payload = _read_json(summary_path, {})
        manifest_payload = _read_json(directory / "manifest.json", {})
        papers = papers_payload.get("papers") if isinstance(papers_payload, dict) and isinstance(papers_payload.get("papers"), list) else []
        category_rows = summary_payload.get("category_summary") if isinstance(summary_payload, dict) and isinstance(summary_payload.get("category_summary"), list) else []
        expected = _as_int(
            (manifest_payload.get("paper_count") if isinstance(manifest_payload, dict) else 0)
            or (papers_payload.get("paper_count") if isinstance(papers_payload, dict) else 0)
            or (summary_payload.get("paper_count") if isinstance(summary_payload, dict) else 0)
            or len(papers),
            0,
        )
        category_total = sum(_as_int((row if isinstance(row, dict) else {}).get("count"), 0) for row in category_rows)
        missing_titles = sum(1 for paper in papers if isinstance(paper, dict) and not str(paper.get("title") or "").strip())
        title_index_complete = bool(expected > 0 and len(papers) == expected and missing_titles == 0)
        category_counts_consistent = bool(category_rows and category_total == expected)
        audit = {}
        if isinstance(manifest_payload, dict):
            audit = manifest_payload.get("audit") if isinstance(manifest_payload.get("audit"), dict) else manifest_payload.get("metadata_completeness_audit") if isinstance(manifest_payload.get("metadata_completeness_audit"), dict) else {}
        if not audit and isinstance(papers_payload, dict):
            audit = papers_payload.get("metadata_completeness_audit") if isinstance(papers_payload.get("metadata_completeness_audit"), dict) else {}
        adapter = str(
            (manifest_payload.get("adapter") if isinstance(manifest_payload, dict) else "")
            or (manifest_payload.get("source_adapter") if isinstance(manifest_payload, dict) else "")
            or (papers_payload.get("source_adapter") if isinstance(papers_payload, dict) else "")
            or (summary_payload.get("source_adapter") if isinstance(summary_payload, dict) else "")
            or (papers_payload.get("source") if isinstance(papers_payload, dict) else "")
            or (summary_payload.get("source") if isinstance(summary_payload, dict) else "")
            or "local_database"
        )
        category_status = str(
            (manifest_payload.get("category_status") if isinstance(manifest_payload, dict) else "")
            or audit.get("category_status")
            or ("official_or_cached_categories" if category_counts_consistent else "no_official_categories")
        )
        has_official_categories = bool(
            (manifest_payload.get("has_official_categories") if isinstance(manifest_payload, dict) else False)
            or audit.get("has_official_categories")
            or (category_counts_consistent and category_status not in {"no_official_categories", "missing_categories", "no_or_partial_categories"})
        )
        missing_abstracts = sum(1 for paper in papers if isinstance(paper, dict) and not str(paper.get("abstract") or "").strip())
        has_abstracts = bool(papers) and missing_abstracts == 0
        any_abstracts = bool(papers) and missing_abstracts < len(papers)
        source_scope = str(_derive_source_scope(adapter) or _manifest_first(
            manifest_payload.get("source_scope") if isinstance(manifest_payload, dict) else "",
            audit.get("source_scope"),
        ) or "")
        official_title_index_verified = _derive_official_title_index_verified(
            adapter,
            title_index_complete,
            _manifest_first(
                manifest_payload.get("official_title_index_verified") if isinstance(manifest_payload, dict) else None,
                audit.get("official_title_index_verified"),
            ),
        )
        official_accepted_list_verified = _derive_official_accepted_list_verified(
            adapter,
            title_index_complete,
            _manifest_first(
                manifest_payload.get("official_accepted_list_verified") if isinstance(manifest_payload, dict) else None,
                audit.get("official_accepted_list_verified"),
            ),
        )
        metadata_complete = bool(title_index_complete and (has_abstracts or has_official_categories))
        if not papers:
            metadata_status = "missing"
        elif metadata_complete:
            metadata_status = "complete"
        elif title_index_complete:
            metadata_status = "title_index_only"
        else:
            metadata_status = "partial"
        basis_parts = [
            "Find title-index metadata cache audit: papers.json count and non-empty titles were checked. This audits Find metadata, not Read-stage PDF full text."
        ]
        if title_index_complete and not has_abstracts:
            basis_parts.append("The cache has a verified title corpus but no abstracts in the title index; selected papers still need metadata enrichment before final LLM scoring.")
        if not has_official_categories:
            basis_parts.append("No trusted official venue categories are available in this cache; The workflow must use title LLM screening instead of category pruning.")
        return {
            "venue_id": alias,
            "venue": str((manifest_payload.get("venue") if isinstance(manifest_payload, dict) else "") or (papers_payload.get("venue") if isinstance(papers_payload, dict) else "") or (summary_payload.get("venue") if isinstance(summary_payload, dict) else "") or venue_id),
            "year": yeint,
            "paper_count": expected or len(papers),
            "adapter": adapter,
            "source_adapter": adapter,
            "papers_path": str(papers_path),
            "category_summary_path": str(summary_path),
            "manifest_path": str(directory / "manifest.json") if (directory / "manifest.json").exists() else "",
            "title_index_completeness_status": "complete" if title_index_complete else "partial" if papers else "missing",
            "title_index_completeness_ok": title_index_complete,
            "metadata_completeness_status": metadata_status,
            "metadata_completeness_ok": metadata_complete,
            "metadata_completeness_limited": bool(papers and not metadata_complete),
            "metadata_completeness_basis": " ".join(part for part in basis_parts if part),
            "source_scope": source_scope,
            "official_title_index_verified": official_title_index_verified,
            "official_accepted_list_verified": official_accepted_list_verified,
            "source_verified": bool(_manifest_first(
                manifest_payload.get("source_verified") if isinstance(manifest_payload, dict) else None,
                audit.get("source_verified"),
                title_index_complete,
            )),
            "has_official_categories": has_official_categories,
            "has_abstracts": has_abstracts,
            "has_abstracts_in_title_index": has_abstracts,
            "any_abstracts": any_abstracts,
            "missing_abstract_count": missing_abstracts,
            "category_status": category_status,
            "category_count": len(category_rows),
            "category_total_count": category_total,
            "missing_title_count": missing_titles,
            "cache_verified": metadata_complete,
        }
    return {}



def _venue_display_name_from_catalog(venue_id: Any) -> str:
    text = str(venue_id or "").strip()
    if not text:
        return "venue"
    try:
        from auto_research.auto_find.catalog import catalog_by_id
        venue = catalog_by_id().get(text) or {}
    except Exception:
        venue = {}
    name = str(venue.get("name") or "").strip()
    if name:
        return name
    return text


def _current_verified_venue_metadata_rows(project_id: str, root: Path | None = None, selection: Any = None) -> list[dict[str, Any]]:
    root = root or (PROJECTS / project_id)
    source_selection = normalize_source_selection(selection) if isinstance(selection, dict) else _current_project_source_selection(project_id, root)
    venue_ids = source_selection.get("venue_ids") or []
    years = source_selection.get("years") or []
    rows: list[dict[str, Any]] = []
    for venue_id in venue_ids:
        requested_years = []
        effective_years = []
        fallback_reasons: list[str] = []
        manifest: dict[str, Any] = {}
        for year in years:
            try:
                yeint = int(year)
            except Exception:
                continue
            requested_years.append(yeint)
            manifest = _read_local_venue_cache_manifest(str(venue_id), yeint)
            if manifest and _as_int(manifest.get("paper_count"), 0) > 0:
                effective_years.append(yeint)
                break
            if manifest and effective_years:
                break
        venue_display_name = _venue_display_name_from_catalog(venue_id)
        if not manifest:
            rows.append({
                "source": venue_display_name,
                "source_kind": "venue",
                "venue_id": str(venue_id),
                "venue": venue_display_name,
                "ok": False,
                "limited": True,
                "count": 0,
                "message": "verified local venue metadata cache missing",
                "requested_years": requested_years,
                "effective_years": [],
                "raw_title_index_count": 0,
                "candidate_count": 0,
                "detail_fetched_count": 0,
                "metadata_completeness_status": "missing",
                "metadata_completeness_ok": False,
                "metadata_completeness_basis": "No reusable local venue metadata cache was found for the selected venue/year.",
                "source_scope": "",
                "official_title_index_verified": False,
                "official_accepted_list_verified": None,
                "source_verified": False,
                "category_status": "unknown",
                "has_official_categories": False,
                "has_abstracts": False,
                "has_abstracts_in_title_index": False,
                "any_abstracts": False,
                "missing_abstract_count": 0,
            })
            continue
        paper_count = _as_int(manifest.get("paper_count"), 0)
        category_status = str(manifest.get("category_status") or "unknown")
        has_categories = bool(manifest.get("has_official_categories"))
        selected_category_count = paper_count if has_categories else 0
        title_filter_input = selected_category_count if has_categories else paper_count
        parts = [
            f"adapter={manifest.get('adapter') or manifest.get('source_adapter') or 'local_database'}",
            "years=" + ",".join(str(year) for year in (effective_years or [manifest.get("year")])) if (effective_years or manifest.get("year")) else "",
            f"corpus={paper_count}",
            f"screen_input={title_filter_input}",
            f"metadata={manifest.get('metadata_completeness_status') or 'unknown'}",
            f"category={category_status}",
        ]
        venue_display_name = str(manifest.get("venue") or "").strip() or _venue_display_name_from_catalog(venue_id)
        rows.append({
            "source": venue_display_name,
            "source_kind": "venue",
            "venue_id": str(venue_id),
            "venue": venue_display_name,
            "ok": bool(paper_count > 0),
            "limited": bool(manifest.get("metadata_completeness_limited") or not manifest.get("metadata_completeness_ok")),
            "count": title_filter_input,
            "message": "; ".join(part for part in parts if part),
            "adapter": manifest.get("adapter") or manifest.get("source_adapter") or "local_database",
            "requested_years": requested_years,
            "effective_years": effective_years or ([manifest.get("year")] if manifest.get("year") else []),
            "raw_title_index_count": paper_count,
            "corpus_count": paper_count,
            "candidate_count": title_filter_input,
            "sample_count": paper_count,
            "selected_category_count": selected_category_count,
            "metadata_detail_count": paper_count,
            "detail_fetched_count": None,
            "title_index_completeness_status": manifest.get("title_index_completeness_status") or "unknown",
            "title_index_completeness_ok": bool(manifest.get("title_index_completeness_ok")),
            "metadata_completeness_status": manifest.get("metadata_completeness_status") or "unknown",
            "metadata_completeness_ok": bool(manifest.get("metadata_completeness_ok")),
            "metadata_completeness_limited": bool(manifest.get("metadata_completeness_limited")),
            "metadata_completeness_basis": manifest.get("metadata_completeness_basis") or "",
            "source_scope": manifest.get("source_scope") or _derive_source_scope(str(manifest.get("adapter") or manifest.get("source_adapter") or "")),
            "official_title_index_verified": manifest.get("official_title_index_verified"),
            "official_accepted_list_verified": manifest.get("official_accepted_list_verified"),
            "source_verified": bool(manifest.get("source_verified")),
            "category_status": category_status,
            "has_official_categories": has_categories,
            "has_abstracts": bool(manifest.get("has_abstracts")),
            "has_abstracts_in_title_index": bool(manifest.get("has_abstracts_in_title_index") or manifest.get("has_abstracts")),
            "any_abstracts": bool(manifest.get("any_abstracts") or manifest.get("has_abstracts")),
            "missing_abstract_count": manifest.get("missing_abstract_count") or 0,
            "category_count": manifest.get("category_count") or 0,
            "category_total_count": manifest.get("category_total_count") or 0,
            "cache_verified": bool(manifest.get("cache_verified") or manifest.get("metadata_completeness_ok")),
            "cache_papers_path": manifest.get("papers_path") or "",
            "cache_manifest_path": manifest.get("manifest_path") or "",
        })
    return rows


def _normalize_venue_metadata_status_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    adapter = str(normalized.get("adapter") or normalized.get("source_adapter") or "")
    category_status = str(normalized.get("category_status") or "").lower()
    has_categories = bool(normalized.get("has_official_categories")) and category_status not in {"no_official_categories", "missing_categories", "no_or_partial_categories"}
    has_abstracts = bool(normalized.get("has_abstracts_in_title_index") or normalized.get("has_abstracts"))
    if "has_abstracts_in_title_index" not in normalized:
        normalized["has_abstracts_in_title_index"] = has_abstracts
    if "has_abstracts" not in normalized:
        normalized["has_abstracts"] = has_abstracts
    normalized.setdefault("any_abstracts", bool(normalized.get("any_abstracts") or has_abstracts))
    normalized.setdefault("missing_abstract_count", 0)
    title_ok = bool(normalized.get("title_index_completeness_ok"))
    if "title_index_completeness_ok" not in normalized:
        title_ok = bool(normalized.get("raw_title_index_count") or normalized.get("corpus_count") or normalized.get("sample_count") or normalized.get("count") or normalized.get("ok"))
        normalized["title_index_completeness_ok"] = title_ok
        normalized.setdefault("title_index_completeness_status", "complete" if title_ok else "missing")
    source_scope = str(_derive_source_scope(adapter) or normalized.get("source_scope") or "")
    normalized["source_scope"] = source_scope
    if normalized.get("official_title_index_verified") in (None, ""):
        normalized["official_title_index_verified"] = _derive_official_title_index_verified(adapter, title_ok)
    if normalized.get("official_accepted_list_verified") in (None, ""):
        normalized["official_accepted_list_verified"] = _derive_official_accepted_list_verified(adapter, title_ok)
    normalized.setdefault("source_verified", bool(title_ok))
    metadata_ready = title_ok and (has_abstracts or has_categories)
    if not metadata_ready and title_ok:
        normalized["metadata_completeness_status"] = "title_index_only"
        normalized["metadata_completeness_ok"] = False
        normalized["metadata_completeness_limited"] = True
        if normalized.get("raw_title_index_count") or normalized.get("corpus_count") or normalized.get("sample_count") or normalized.get("count"):
            normalized["ok"] = True
        basis = str(normalized.get("metadata_completeness_basis") or "").strip()
        extra = []
        if not has_abstracts:
            extra.append("Title corpus is available, but abstracts are not present in the title index; selected papers require metadata enrichment before final LLM scoring.")
        if not has_categories:
            extra.append("No trusted official venue categories are available; title LLM screening must be used instead of category pruning.")
        normalized["metadata_completeness_basis"] = " ".join(part for part in [basis, *extra] if part).strip()
    else:
        normalized.setdefault("metadata_completeness_ok", metadata_ready)
        normalized.setdefault("metadata_completeness_limited", bool(normalized and not metadata_ready))
    normalized["limited"] = _venue_source_public_limited(normalized)
    return normalized


def _venue_status_has_run_evidence(row: dict[str, Any]) -> bool:
    if bool(row.get("ok")):
        return True
    count = _as_int(
        row.get("raw_title_index_count")
        or row.get("corpus_count")
        or row.get("sample_count")
        or row.get("candidate_count")
        or row.get("count"),
        0,
    )
    return count > 0 or bool(row.get("effective_years"))


def _venue_status_is_missing_cache(row: dict[str, Any]) -> bool:
    message = str(row.get("message") or row.get("metadata_completeness_basis") or "").lower()
    status = str(row.get("metadata_completeness_status") or "").lower()
    count = _as_int(
        row.get("raw_title_index_count")
        or row.get("corpus_count")
        or row.get("sample_count")
        or row.get("candidate_count")
        or row.get("count"),
        0,
    )
    return not row.get("ok") and count <= 0 and (status == "missing" or "metadata cache missing" in message)


def _merge_verified_venue_metadata_rows(current_rows: Any, verified_rows: Any) -> list[dict[str, Any]]:
    rows = [_normalize_venue_metadata_status_row(dict(row)) for row in _json_rows(current_rows)]
    verified = [_normalize_venue_metadata_status_row(dict(row)) for row in _json_rows(verified_rows)]
    if not verified:
        return rows
    by_key = {_venue_source_key(row): row for row in verified}
    merged: list[dict[str, Any]] = []
    used: set[str] = set()
    for row in rows:
        key = _venue_source_key(row)
        replacement = by_key.get(key)
        if replacement:
            used.add(key)
            if _venue_status_is_missing_cache(replacement) and _venue_status_has_run_evidence(row):
                merged.append(row)
                continue
            combined = {**row, **replacement}
            row_candidate = _as_int(row.get("candidate_count") or row.get("count"), 0)
            row_selected = _as_int(row.get("selected_category_count") or row.get("selected_category_papers"), 0)
            replacement_has_categories = bool(replacement.get("has_official_categories"))
            replacement_raw = _as_int(replacement.get("raw_title_index_count") or replacement.get("corpus_count"), 0)
            # If the run already records category-screened input for a categorized
            # venue, keep that stage count. Verified cache replaces only the full
            # corpus and metadata-completeness facts. For venues without official
            # categories, the complete title corpus is the title-screen input.
            if replacement_has_categories and row_candidate and (not replacement_raw or row_candidate <= replacement_raw):
                combined["candidate_count"] = row_candidate
                combined["count"] = row_candidate
                combined["selected_category_count"] = row_selected or row_candidate
            run_detail = row.get("detail_fetched_count") or row.get("detail_fetched") or row.get("fetched_count")
            if run_detail not in (None, "", 0):
                combined["detail_fetched_count"] = run_detail
            merged.append(_normalize_venue_metadata_status_row(combined))
        else:
            merged.append(row)
    for key, row in by_key.items():
        if key not in used:
            merged.append(row)
    return merged


def _venue_metadata_counts(rows: Any) -> dict[str, Any]:
    venue_rows = _json_rows(rows)
    raw_total = sum(_as_int(row.get("raw_title_index_count") or row.get("corpus_count") or row.get("sample_count"), 0) for row in venue_rows)
    selected_total = sum(_as_int(row.get("selected_category_count") or row.get("selected_category_papers"), 0) for row in venue_rows)
    title_input_total = sum(_as_int(row.get("candidate_count") or row.get("title_filter_input_papers") or row.get("count"), 0) for row in venue_rows)
    complete_count = sum(1 for row in venue_rows if row.get("metadata_completeness_ok"))
    no_official_count = sum(1 for row in venue_rows if str(row.get("category_status") or "") in {"no_official_categories", "no_or_partial_categories"})
    return {
        "raw_title_index_papers": raw_total,
        "venue_total_papers_available": raw_total,
        "venue_corpus_audited_papers": raw_total,
        "venue_category_selected_papers": selected_total,
        "category_selected_papers": selected_total,
        "venue_title_filter_input_papers": title_input_total,
        "metadata_complete_venue_count": complete_count,
        "metadata_venue_count": len(venue_rows),
        "venues_without_official_categories": no_official_count,
    }

def _venue_source_rows_from_health(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _json_rows(rows):
        source_name = row.get("venue") or row.get("venue_id") or "venue"
        try:
            count = int(row.get("candidate_count") or row.get("sample_count") or row.get("corpus_count") or 0)
        except Exception:
            count = 0
        parts: list[str] = []
        if row.get("adapter"):
            parts.append(f"adapter={row.get('adapter')}")
        effective_years = row.get("effective_years") or []
        if effective_years:
            parts.append("years=" + ",".join(str(year) for year in effective_years))
        if row.get("corpus_count") is not None:
            parts.append("corpus=" + str(row.get("corpus_count")))
        if row.get("candidate_count") is not None:
            parts.append("screen_input=" + str(row.get("candidate_count")))
        if row.get("sample_count") is not None:
            parts.append("fetched=" + str(row.get("sample_count")))
        if row.get("metadata_completeness_status"):
            parts.append("metadata=" + str(row.get("metadata_completeness_status")))
        if row.get("category_status"):
            parts.append("category=" + str(row.get("category_status")))
        if row.get("year_fallback_reason"):
            parts.append(str(row.get("year_fallback_reason")))
        if row.get("error"):
            parts.append(str(row.get("error")))
        adapter = str(row.get("adapter") or row.get("source_adapter") or "")
        source_scope = str(_derive_source_scope(adapter) or row.get("source_scope") or "")
        raw_title_index_count = row.get("corpus_count") or row.get("raw_title_index_count") or row.get("sample_count") or 0
        title_ok = bool(row.get("title_index_completeness_ok")) or bool(raw_title_index_count)
        out.append({
            "source": source_name,
            "source_kind": "venue",
            "venue_id": row.get("venue_id") or "",
            "venue": row.get("venue") or source_name,
            "ok": bool(row.get("ok")),
            "limited": _venue_source_public_limited(row),
            "count": count,
            "message": "; ".join(parts) or ("ok" if row.get("ok") else "No papers fetched."),
            "adapter": adapter,
            "source_adapter": adapter,
            "requested_years": row.get("requested_years") or [],
            "effective_years": effective_years,
            "raw_title_index_count": raw_title_index_count,
            "corpus_count": row.get("corpus_count") or raw_title_index_count,
            "candidate_count": row.get("candidate_count") or row.get("sample_count") or 0,
            "selected_category_count": row.get("selected_category_count") or row.get("selected_category_papers") or 0,
            "detail_fetched_count": row.get("detail_fetched_count") or row.get("detail_fetched") or row.get("fetched_count"),
            "metadata_completeness_status": row.get("metadata_completeness_status") or "",
            "metadata_completeness_ok": bool(row.get("metadata_completeness_ok")),
            "metadata_completeness_limited": bool(row.get("metadata_completeness_limited")),
            "metadata_completeness_basis": row.get("metadata_completeness_basis") or "",
            "title_index_completeness_status": row.get("title_index_completeness_status") or ("complete" if title_ok else ""),
            "title_index_completeness_ok": title_ok,
            "source_scope": source_scope,
            "official_title_index_verified": row.get("official_title_index_verified") if row.get("official_title_index_verified") not in (None, "") else _derive_official_title_index_verified(adapter, title_ok),
            "official_accepted_list_verified": row.get("official_accepted_list_verified") if row.get("official_accepted_list_verified") not in (None, "") else _derive_official_accepted_list_verified(adapter, title_ok),
            "source_verified": bool(row.get("source_verified") if row.get("source_verified") not in (None, "") else title_ok),
            "category_status": row.get("category_status") or "",
            "has_official_categories": bool(row.get("has_official_categories")),
            "has_abstracts": bool(row.get("has_abstracts")),
            "has_abstracts_in_title_index": bool(row.get("has_abstracts_in_title_index") or row.get("has_abstracts")),
            "any_abstracts": bool(row.get("any_abstracts") or row.get("has_abstracts")),
            "missing_abstract_count": row.get("missing_abstract_count") or 0,
        })
    return out


def _expand_source_status_rows(source_rows: Any, venue_health_rows: Any) -> list[dict[str, Any]]:
    rows = _json_rows(source_rows)
    venue_rows = _venue_source_rows_from_health(venue_health_rows)
    if venue_rows:
        non_aggregate = [
            row for row in rows
            if str(row.get("source") or "").strip().lower() not in {"venues", "venue summary", "venue_summary"}
            and str(row.get("source_kind") or "").strip().lower() != "venue_summary"
        ]
        return venue_rows + non_aggregate
    return rows


def _venue_id_equivalent_keys(venue_id: Any, year: int = 0) -> set[str]:
    text = str(venue_id or "").strip()
    if not text:
        return set()
    keys = {text}
    match = re.match(r"^(.+)_((?:19|20)\d{2})$", text)
    base = match.group(1) if match else text
    if match:
        keys.add(base)
        if not year:
            try:
                year = int(match.group(2))
            except ValueError:
                year = 0
    if year:
        keys.add(f"{base}_{year}")
        keys.add(f"{text}_{year}")
    return {key for key in keys if key}


def _current_health_check_source_status_rows(project_id: str, root: Path | None = None, selection: Any = None) -> list[dict[str, Any]]:
    project_root = root or (PROJECTS / project_id)
    payload = _read_json(project_root / "state" / "venue_health_status.json", {})
    if not isinstance(payload, dict):
        return []
    rows = _json_rows(payload.get("source_status"))
    if not rows:
        return []
    source_selection = normalize_source_selection(selection) if isinstance(selection, dict) else _current_project_source_selection(project_id, project_root)
    selected_years = {_as_int(item, 0) for item in (source_selection.get("years") or [])}
    selected_years.discard(0)
    selected_ids: set[str] = set()
    for item in source_selection.get("venue_ids") or []:
        venue_id = str(item or "").strip()
        if venue_id:
            selected_ids.update(_venue_id_equivalent_keys(venue_id))
            for year in selected_years:
                selected_ids.update(_venue_id_equivalent_keys(venue_id, year))
    selected_pairs: set[tuple[str, int]] = set()
    for item in source_selection.get("venue_years") or []:
        if not isinstance(item, dict):
            continue
        venue_id = str(item.get("venue_id") or item.get("venue") or item.get("id") or "").strip()
        raw_years = item.get("years") if isinstance(item.get("years"), list) else [item.get("year")]
        for raw_year in raw_years:
            year = _as_int(raw_year, 0)
            if venue_id and year:
                for key in _venue_id_equivalent_keys(venue_id, year):
                    selected_pairs.add((key, year))
    filtered: list[dict[str, Any]] = []
    for row in rows:
        venue_id = str(row.get("venue_id") or "").strip()
        year = _as_int(row.get("year"), 0)
        if not year:
            years = row.get("effective_years") or row.get("requested_years") or []
            if isinstance(years, list) and years:
                year = _as_int(years[0], 0)
        row_keys = _venue_id_equivalent_keys(venue_id, year)
        if selected_pairs and not any((key, year) in selected_pairs for key in row_keys):
            continue
        if selected_ids and row_keys and not (row_keys & selected_ids):
            continue
        if selected_years and year and year not in selected_years:
            continue
        filtered.append(dict(row))
    return filtered


def _first_non_empty_rows(*values: Any) -> list[dict[str, Any]]:
    for value in values:
        if isinstance(value, list):
            rows = [row for row in value if isinstance(row, dict)]
            if rows:
                return rows
    return []



def _count_json_value(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if value is None:
        return 0
    try:
        return int(value)
    except Exception:
        return 0


def _tail_lines(path: Path, limit: int = 80) -> list[str]:
    try:
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []


def _current_project_source_selection(project_id: str, root: Path | None = None) -> dict[str, Any]:
    project_root = root or (PROJECTS / project_id)
    if project_id:
        configured = project_source_selection(project_id)
        if configured.get("venue_ids"):
            return configured
    for rel in [
        project_root / "planning" / "finding" / "find_progress.json",
        project_root / "state" / "current_find_research_plan.json",
        project_root / "state" / "literature_tool_packet.json",
    ]:
        payload = _read_json(rel, {})
        if isinstance(payload, dict) and isinstance(payload.get("selection"), dict):
            return normalize_source_selection(payload.get("selection"))
    return canonical_source_selection()


def _looks_like_experiment_training_cmd(command: Any) -> bool:
    lowered = str(command or "").lower()
    if not lowered:
        return False
    if "finetune.py" in lowered or "finetune_llm.py" in lowered or "finetune_llm_seminit.py" in lowered:
        return True
    if "exp_text_init" in lowered or "exp_text_init_standard_train.py" in lowered:
        return True
    # Some selected bases run training/evaluation through main.py. Require an explicit
    # dataset flag so generic main.py utilities do not move TASTE back to experiment.
    if re.search(r"(?:^|\s)(?:\S*/)?main\.py\b", lowered) and re.search(r"(?:^|\s)--data(?:=|\s+)", lowered):
        return True
    pattern = r"(?:^|\s)(?:python\S*|conda)(?:\s+\S+){0,10}\s+(?:-u\s+)?(?:\S*/)?(?:finetune|train)[\w.-]*\.py\b"
    return bool(re.search(pattern, lowered))


def _process_cwd(pid: Any) -> str:
    try:
        return str(Path(f"/proc/{str(pid).strip()}/cwd").resolve())
    except Exception:
        return ""


def _path_is_within(path_value: Any, parent_value: Any) -> bool:
    text = str(path_value or "").strip()
    parent_text = str(parent_value or "").strip()
    if not text or not parent_text:
        return False
    try:
        path = Path(text).resolve()
        parent = Path(parent_text).resolve()
        return path == parent or parent in path.parents
    except Exception:
        parent_text = parent_text.rstrip("/")
        return text == parent_text or text.startswith(parent_text + "/")


def _current_selected_repo_path(root: Path) -> Path | None:
    for rel in [
        "state/evidence_ready_repo_selection.json",
        "state/active_repo.json",
        "state/fresh_research_base.json",
        "state/fresh_base_implementation_plan.json",
    ]:
        payload = _read_json(root / rel, {})
        candidates: list[Any] = []
        if isinstance(payload, dict):
            candidates.append(payload)
            for key in ["selected", "active_repo", "repo", "fresh_paper_base"]:
                if isinstance(payload.get(key), dict):
                    candidates.append(payload[key])
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ["repo_path", "local_path", "path", "selected_repo_path", "active_repo_path"]:
                value = str(item.get(key) or "").strip()
                if not value:
                    continue
                path = Path(value)
                if not path.is_absolute():
                    path = root / path
                if path.exists():
                    return path.resolve()
    fallback = root / "repos" / "selected"
    return fallback.resolve() if fallback.exists() else None


def _remote_process_rows() -> list[dict[str, Any]]:
    now = time.monotonic()
    cached_rows = _PROCESS_ROWS_CACHE.get("rows")
    if isinstance(cached_rows, list) and now < float(_PROCESS_ROWS_CACHE.get("expires_at") or 0.0):
        return [dict(row) for row in cached_rows if isinstance(row, dict)]
    patterns = [
        "run_pair_compare",
        "run_frontend",
        "run_driver",
        "run_full_research_cycle",
        "run_paper_pipeline",
        "run_paper_orchestra_bridge",
        "claude_project_session",
        "claude -p",
        "main.py",
        "main-py",
        "train.py --data",
        "finetune.py",
        "finetune_llm.py",
        "finetune_llm_seminit.py",
        "exp_text_init",
        "exp_text_init_standard_train.py",
    ]
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid,etime,stat,%cpu,%mem,cmd"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=3,
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        stat = str(parts[3] or "")
        if "Z" in stat.upper():
            continue
        cmd = parts[6]
        cmd_parts = cmd.split()
        exe_name = Path(cmd_parts[0]).name if cmd_parts else ""
        inspection_markers = [
            "| grep",
            "| rg",
            " ps -",
            " ps ",
            "sed -n",
            "tail -n",
            "curl -sS",
            "curl -ss",
            "api/jobs",
            "api/projects",
            "urllib.request",
            "json.load",
            "json.loads",
            "read_text",
            "state/full_research_cycle.json",
            "state/reference_reproduction_gate.json",
            "fresh_base_reference_full_reproduction_job.json",
        ]
        if exe_name in {"grep", "rg", "sed", "awk", "tail", "head", "curl", "ssh", "ps"}:
            continue
        lowered_cmd = cmd.lower()
        if exe_name in {"sh", "bash", "zsh"} and any(marker.lower() in lowered_cmd for marker in inspection_markers):
            continue
        if exe_name.startswith("python") and any(marker.lower() in lowered_cmd for marker in inspection_markers):
            continue
        if "python - <<" in lowered_cmd or "python3 - <<" in lowered_cmd or "python -c" in lowered_cmd or "python3 -c" in lowered_cmd:
            if any(marker.lower() in lowered_cmd for marker in inspection_markers):
                continue
        if not any(pattern in cmd for pattern in patterns):
            continue
        kind = "other"
        if any(part.endswith("run_pair_compare.py") or part == "run_pair_compare" for part in cmd_parts):
            kind = "pair_driver"
        elif any(part.endswith("run_frontend.py") or part == "run_frontend" for part in cmd_parts):
            kind = "frontend"
        elif any(part.endswith("run_driver.py") or part == "run_driver" for part in cmd_parts):
            kind = "driver"
        elif any(part.endswith("run_full_research_cycle.py") for part in cmd_parts):
            kind = "full_cycle"
        elif any(part.endswith("run_paper_pipeline.py") for part in cmd_parts):
            kind = "paper_pipeline"
        elif any(part.endswith("run_paper_orchestra_bridge.py") for part in cmd_parts):
            kind = "paper_orchestra"
        elif any(part.endswith("claude_project_session.py") for part in cmd_parts):
            kind = "claude_session"
        elif cmd_parts and cmd_parts[0].endswith("/claude") and "-p" in cmd_parts:
            kind = "claude_cli"
        elif _looks_like_experiment_training_cmd(cmd) or any(marker in cmd for marker in ["main.py", "main-py", "train.py --data"]):
            kind = "experiment_or_reproduction"
        else:
            continue
        rows.append({
            "pid": parts[0],
            "ppid": parts[1],
            "elapsed": parts[2],
            "stat": parts[3],
            "cpu": parts[4],
            "mem": parts[5],
            "kind": kind,
            "cmd": cmd[:500],
            "cwd": _process_cwd(parts[0]),
        })
    rows = rows[:80]
    _PROCESS_ROWS_CACHE["rows"] = [dict(row) for row in rows]
    _PROCESS_ROWS_CACHE["expires_at"] = time.monotonic() + PROCESS_ROWS_TTL_SEC
    return rows


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _find_summary_from_payload(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    layer = data.get("candidate_layer_summary") if isinstance(data.get("candidate_layer_summary"), dict) else {}
    pool_counts = layer.get("pool_counts") if isinstance(layer.get("pool_counts"), dict) else {}
    strong = max(
        _count_json_value(data.get("strong_recommendations")),
        _as_int(data.get("strong_recommendation_count")),
        _as_int(summary.get("strong_paper_anchors")),
        _as_int(pool_counts.get("strong_papers")),
        _as_int(pool_counts.get("strong_recommendations")),
    )
    target = max(
        _as_int(data.get("recommendation_target_count")),
        _as_int(summary.get("recommendation_target_count")),
        _as_int(pool_counts.get("recommendation_target_count")),
    )
    shortfall = max(
        _as_int(data.get("recommendation_shortfall")),
        _as_int(summary.get("recommendation_shortfall")),
        _as_int(pool_counts.get("recommendation_shortfall")),
        max(0, target - strong) if target else 0,
    )
    survey_stats = data.get("survey_stats") if isinstance(data.get("survey_stats"), dict) else {}
    def _rows(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []
    category_rows = _rows(data.get("category_scan_report"))
    title_rows = _rows(data.get("title_filter_report"))
    venue_health = _rows(data.get("venue_health_report"))
    raw_title_index_count = (
        _as_int(counts.get("raw_title_index_papers"))
        or _as_int(counts.get("raw_title_index"))
        or _as_int(survey_stats.get("raw_title_index_papers"))
        or _count_json_value(data.get("raw_title_index"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("corpus_count") or (row if isinstance(row, dict) else {}).get("sample_count")) for row in venue_health)
    )
    category_selected_count = (
        _as_int(counts.get("venue_category_selected_papers"))
        or _as_int(counts.get("category_selected_papers"))
        or _as_int(survey_stats.get("venue_category_selected_papers"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("selected_category_papers")) for row in category_rows)
    )
    title_filter_input_count = (
        _as_int(counts.get("venue_title_filter_input_papers"))
        or _as_int(survey_stats.get("venue_title_filter_input_papers"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("title_filter_input_papers")) for row in title_rows)
    )
    category_filtered_count = (
        _as_int(counts.get("category_filtered_papers"))
        or _as_int(survey_stats.get("category_filtered_papers"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("category_filtered_papers")) for row in title_rows)
        or title_filter_input_count
        or category_selected_count
        or raw_title_index_count
    )
    tfidf_screened_count = (
        _as_int(counts.get("tfidf_screened_papers"))
        or _as_int(survey_stats.get("tfidf_screened_papers"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("tfidf_screened_papers")) for row in title_rows)
        or title_filter_input_count
        or category_filtered_count
    )
    title_score_input_count = (
        _as_int(counts.get("title_score_input_papers"))
        or _as_int(survey_stats.get("title_score_input_papers"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("title_score_input_papers")) for row in title_rows)
    )
    llm_title_scored_count = (
        _as_int(counts.get("llm_title_scored_papers"))
        or _as_int(survey_stats.get("llm_title_scored_papers"))
        or sum(_as_int((row if isinstance(row, dict) else {}).get("llm_title_scored_papers")) for row in title_rows)
    )
    title_candidate_count = (
        _as_int(counts.get("title_candidates"))
        or _as_int(counts.get("venue_final_title_candidates"))
        or _as_int(survey_stats.get("venue_final_title_candidates"))
        or _count_json_value(data.get("title_candidates"))
    )
    venue_final_title_candidate_count = (
        _as_int(counts.get("venue_final_title_candidates"))
        or _as_int(survey_stats.get("venue_final_title_candidates"))
        or title_candidate_count
    )
    detail_fetched_count = (
        _as_int(counts.get("venue_detail_fetched_candidates"))
        or _as_int(counts.get("detail_fetched"))
        or _as_int(survey_stats.get("venue_detail_fetched_candidates"))
        or _count_json_value(data.get("detail_fetched"))
    )
    abstract_fetch_failed_count = _as_int(counts.get("abstract_fetch_failed_candidates")) or _as_int(survey_stats.get("abstract_fetch_failed_candidates"))
    final_llm_scoring_skipped_count = _as_int(counts.get("final_llm_scoring_skipped_candidates")) or _as_int(survey_stats.get("final_llm_scoring_skipped_candidates"))
    evaluated_count = _as_int(counts.get("evaluated_candidates")) or _count_json_value(data.get("evaluated_candidates"))
    llm_scored_count = (
        _as_int(counts.get("abstract_scored_papers"))
        or _as_int(counts.get("llm_scored_candidates"))
        or _as_int(survey_stats.get("abstract_scored_papers"))
        or _as_int(survey_stats.get("llm_scored_candidates"))
        or sum(1 for row in _rows(data.get("evaluated_candidates")) if isinstance(row, dict) and str(row.get("reason_source") or "") == "llm abstract evaluation")
    )
    return {
        "run_id": data.get("run_id"),
        "created_at": data.get("created_at"),
        "scoring_policy_version": data.get("scoring_policy_version"),
        "raw_title_index_papers": raw_title_index_count,
        "venue_total_papers_available": raw_title_index_count,
        "venue_corpus_audited_papers": raw_title_index_count,
        "venue_category_selected_papers": category_selected_count,
        "category_selected_papers": category_selected_count,
        "category_filtered_papers": category_filtered_count,
        "tfidf_screened_papers": tfidf_screened_count,
        "venue_title_filter_input_papers": title_filter_input_count,
        "title_score_input_papers": title_score_input_count,
        "llm_title_scored_papers": llm_title_scored_count,
        "title_candidates": title_candidate_count,
        "venue_final_title_candidates": venue_final_title_candidate_count,
        "detail_fetched": detail_fetched_count,
        "venue_detail_fetched_candidates": detail_fetched_count,
        "evaluated_candidates": evaluated_count,
        "abstract_scored_papers": llm_scored_count,
        "llm_scored_candidates": llm_scored_count,
        "abstract_fetch_failed_candidates": abstract_fetch_failed_count,
        "final_llm_scoring_skipped_candidates": final_llm_scoring_skipped_count,
        "screened_ranking": _count_json_value(data.get("screened_ranking")),
        "strong_recommendations": strong,
        "articles": _count_json_value(data.get("articles")) or strong,
        "read_candidates": _count_json_value(data.get("read_candidates")) or strong,
        "recommendation_target_count": target,
        "recommendation_shortfall": shortfall,
        "recommendation_gate_status": "shortfall" if shortfall > 0 else "pass" if target else "unknown",
    }


def _fresh_base_data_required(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    if str(plan.get("status") or "") == "blocked_fresh_base_data_required":
        return True
    for key in ["fresh_base_data_acquisition", "data_acquisition"]:
        data = plan.get(key)
        if isinstance(data, dict) and str(data.get("decision") or "") == "blocked_external_data_required":
            return True
    blocked_datasets = plan.get("blocked_datasets", [])
    blockers = plan.get("blocker_reasons", []) if isinstance(plan.get("blocker_reasons"), list) else []
    text = "\\n".join(str(item).lower() for item in blockers)
    return bool(blocked_datasets) and any(
        term in text
        for term in ["dataset", "loader", "google drive", "required file", "required_files", "dataset_contract", "missing_required_files"]
    )



def _current_find_run_id_for_project(root: Path) -> str:
    for rel in [
        root / "planning" / "finding" / "find_progress.json",
        root / "state" / "current_find_research_plan.json",
        root / "state" / "literature_tool_packet.json",
        root / "state" / "supervision_tick.json",
    ]:
        payload = _read_json(rel, {})
        if isinstance(payload, dict):
            run_id = str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or "").strip()
            if run_id:
                return run_id
    return ""



def title_key_for_current_find(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_recommended_title_keys(paths_or_root) -> set[str]:
    root = Path(paths_or_root)
    keys: set[str] = set()
    for rel in [root / "state" / "literature_tool_packet.json", root / "state" / "current_find_research_plan.json", root / "state" / "evidence_ready_repo_selection.json"]:
        payload = _read_json(rel, {})
        if not isinstance(payload, dict):
            continue
        for pool in ["strong_papers", "base_work_candidates", "articles", "strong_recommendations", "readings"]:
            rows = payload.get(pool)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    key = title_key_for_current_find(row.get("title") or row.get("paper_title") or row.get("literature_base_title") or row.get("selected_base_title"))
                    if key:
                        keys.add(key)
        selected = payload.get("selected") if isinstance(payload.get("selected"), dict) else {}
        if selected:
            key = title_key_for_current_find(selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or selected.get("name"))
            if key:
                keys.add(key)
    return keys


def selected_title_in_current_find(paths_or_root, selected: dict[str, Any], decision: dict[str, Any] | None = None) -> bool:
    decision = decision if isinstance(decision, dict) else {}
    title = selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or decision.get("selected_base_title") or selected.get("name") or ""
    key = title_key_for_current_find(title)
    if key and key in current_find_recommended_title_keys(paths_or_root):
        return True
    root = Path(paths_or_root.root) if hasattr(paths_or_root, "root") else Path(paths_or_root)
    audit = {}
    for audit_name in ["fresh_base_reference_full_reproduction_audit.json", "fresh_base_reference_reproduction_audit.json"]:
        candidate_audit = _read_json(root / "state" / audit_name, {})
        if _artifact_matches_current_repo(root, candidate_audit):
            audit = candidate_audit
            break
    audit_selected = audit.get("selected_base") if isinstance(audit, dict) and isinstance(audit.get("selected_base"), dict) else {}
    selected_repo = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    audit_repo = str(audit.get("repo_path") or audit.get("active_repo_path") or audit_selected.get("repo_path") or audit_selected.get("local_path") or "").strip() if isinstance(audit, dict) else ""
    audit_title = audit_selected.get("literature_base_title") or audit_selected.get("title") or audit.get("paper_title") or audit.get("base_title") or "" if isinstance(audit, dict) else ""
    audit_run = str(audit_selected.get("fresh_find_run_id") or "").strip()
    selected_run = str(selected.get("fresh_find_run_id") or "").strip()
    if selected_repo and audit_repo and selected_repo == audit_repo and (
        (audit_run and selected_run == audit_run)
        or (key and title_key_for_current_find(audit_title) == key)
    ):
        return True
    gate = _read_json(root / "state" / "base_switch_gate.json", {})
    execution = _read_json(root / "state" / "base_switch_execution.json", {})
    candidate = gate.get("candidate_route") if isinstance(gate, dict) and isinstance(gate.get("candidate_route"), dict) else {}
    candidate_repo = str(candidate.get("repo_path") or "").strip()
    return bool(
        selected_repo
        and candidate_repo
        and selected_repo == candidate_repo
        and isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and gate.get("switch_authorized") is True
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )


def _repo_path_from_mapping(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["repo_path", "local_path", "path"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _selected_base_viability_current_selection(root: Path, current_run: str = "") -> dict[str, Any]:
    """Prefer the still-active selected-base route over conflicting candidate switch artifacts."""
    gate = _read_json(root / "state" / "selected_base_viability_gate.json", {})
    if not isinstance(gate, dict):
        return {}
    status = str(gate.get("status") or "").lower()
    decision = str(gate.get("decision") or "").lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return {}
    repo_path = str(gate.get("current_selected_repo_path") or "").strip()
    repo_name = str(gate.get("current_selected_repo") or "").strip()
    title = str(gate.get("selected_base_title") or gate.get("literature_base_title") or repo_name or "").strip()
    if not (repo_path or repo_name or title):
        return {}

    impl = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    impl_repo = impl.get("repo") if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    impl_repo_path = _repo_path_from_mapping(impl_repo)
    guard = _read_json(root / "state" / "selected_base_route_guard.json", {})
    trusted = guard.get("trusted_audit") if isinstance(guard, dict) and isinstance(guard.get("trusted_audit"), dict) else {}
    guard_repo_path = _repo_path_from_mapping(trusted)
    audit = _read_json(root / "state" / "fresh_base_reference_reproduction_audit.json", {})
    audit_selected = audit.get("selected_base") if isinstance(audit, dict) and isinstance(audit.get("selected_base"), dict) else {}
    audit_repo_path = _repo_path_from_mapping(audit) or _repo_path_from_mapping(audit_selected)
    aligned_paths = {value for value in [impl_repo_path, guard_repo_path, audit_repo_path] if value}
    if repo_path and aligned_paths and repo_path not in aligned_paths:
        return {}

    selected_run = ""
    if isinstance(guard, dict):
        selected_run = str(guard.get("selected_base_find_run_id") or "").strip()
    selected_run = str(gate.get("fresh_find_run_id") or selected_run or "").strip()
    selected_plan_id = str(gate.get("selected_plan_id") or (guard.get("selected_base_selected_plan_id") if isinstance(guard, dict) else "") or "").strip()
    selected_idea_id = str(gate.get("selected_idea_id") or (guard.get("selected_base_selected_idea_id") if isinstance(guard, dict) else "") or "").strip()
    ready_datasets = []
    if isinstance(impl, dict) and isinstance(impl.get("ready_datasets"), list):
        ready_datasets = impl.get("ready_datasets") or []
    selected = {
        "name": repo_name,
        "repo": repo_name,
        "repo_name": repo_name,
        "repo_path": repo_path,
        "local_path": repo_path,
        "title": title,
        "literature_base_title": title,
        "selected_base_title": title,
        "fresh_find_run_id": selected_run,
        "selection_stage": "environment_claude_code",
        "selected_by_stage": "environment_claude_code",
        "selection_gate": "selected_base_viability_gate_current_route",
        "decision": "continue_current_selected_base_evidence_repair",
        "claim_ready_datasets": ready_datasets,
        "ready_datasets": ready_datasets,
        "route_consistency": {
            "status": "candidate_switch_conflicts_with_selected_base_gate",
            "source": "state/selected_base_viability_gate.json",
            "current_selected_repo_path": repo_path,
        },
    }
    if ready_datasets:
        selected["claim_ready_dataset"] = str(ready_datasets[0])
    if selected_plan_id:
        selected["selected_plan_id"] = selected_plan_id
    if selected_idea_id:
        selected["selected_idea_id"] = selected_idea_id
    return {
        "valid": True,
        "current_find_run_id": current_run,
        "fresh_find_run_id": selected_run,
        "selection_stage": "environment_claude_code",
        "accepted_by_claude": True,
        "selected": selected,
        "selection_gate": "selected_base_viability_gate_current_route",
        "raw_selection_gate": str(gate.get("selection_gate") or "selected_base_viability_gate_current_route"),
        "reason": "selected_base_viability_current_route",
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
        "candidate_switch_conflict": True,
    }

def _current_environment_selection(root: Path) -> dict[str, Any]:
    current_run = _current_find_run_id_for_project(root)
    selected_contract = _current_find_selected_execution_summary(root)
    current_selected_plan_id = str(selected_contract.get("selected_plan_id") or "").strip() if isinstance(selected_contract, dict) else ""
    current_selected_idea_id = str(selected_contract.get("selected_idea_id") or "").strip() if isinstance(selected_contract, dict) else ""
    selected_plan_required = bool((selected_contract or {}).get("required")) if isinstance(selected_contract, dict) else False

    def invalid_selection(reason: str, *, selected_run: str = "", selection_plan_id: str = "", selection_idea_id: str = "") -> dict[str, Any]:
        return {
            "valid": False,
            "current_find_run_id": current_run,
            "fresh_find_run_id": selected_run,
            "selected_plan_id": selection_plan_id,
            "selected_idea_id": selection_idea_id,
            "current_selected_plan_id": current_selected_plan_id,
            "current_selected_idea_id": current_selected_idea_id,
            "reason": reason,
        }

    def route_plan_id(row: Any) -> str:
        return str(row.get("selected_plan_id") or row.get("current_find_plan_id") or row.get("source_plan_id") or "").strip() if isinstance(row, dict) else ""

    def route_idea_id(row: Any) -> str:
        return str(row.get("selected_idea_id") or row.get("current_find_idea_id") or row.get("source_idea_id") or "").strip() if isinstance(row, dict) else ""

    def route_run_id(row: Any) -> str:
        return str(row.get("fresh_find_run_id") or row.get("current_find_run_id") or row.get("find_run_id") or row.get("run_id") or "").strip() if isinstance(row, dict) else ""

    def route_mismatch_reason(
        *,
        selected_run: str,
        selected_route_run: str,
        selection_plan_id: str,
        selected_route_plan_id: str,
    ) -> str:
        if current_run and (not selected_run or selected_run != current_run or not selected_route_run or selected_route_run != current_run):
            return "environment_selection_find_run_missing_or_stale"
        if selected_plan_required and (
            not selection_plan_id
            or selection_plan_id != current_selected_plan_id
            or not selected_route_plan_id
            or selected_route_plan_id != current_selected_plan_id
        ):
            return "environment_selection_selected_plan_missing_or_stale"
        return ""

    if selected_plan_required and not current_selected_plan_id:
        return invalid_selection("missing_current_find_selected_plan_id")
    viability_mismatch: dict[str, Any] = {}
    viability_current = _selected_base_viability_current_selection(root, current_run)
    if viability_current:
        viability_selected = viability_current.get("selected") if isinstance(viability_current.get("selected"), dict) else {}
        viability_plan_id = str(viability_current.get("selected_plan_id") or route_plan_id(viability_selected) or "").strip()
        viability_idea_id = str(viability_current.get("selected_idea_id") or route_idea_id(viability_selected) or "").strip()
        viability_run_id = str(viability_current.get("fresh_find_run_id") or route_run_id(viability_selected) or "").strip()
        viability_selected_run_id = route_run_id(viability_selected)
        viability_reason = route_mismatch_reason(
            selected_run=viability_run_id,
            selected_route_run=viability_selected_run_id,
            selection_plan_id=viability_plan_id,
            selected_route_plan_id=route_plan_id(viability_selected),
        ) if selected_plan_required else ""
        if not selected_plan_required and not viability_reason:
            return {**viability_current, "selected_plan_id": viability_plan_id, "selected_idea_id": viability_idea_id, "current_selected_plan_id": current_selected_plan_id, "current_selected_idea_id": current_selected_idea_id}
        viability_mismatch = invalid_selection(viability_reason, selected_run=viability_run_id, selection_plan_id=viability_plan_id, selection_idea_id=viability_idea_id)
    selection = _read_json(root / "state" / "evidence_ready_repo_selection.json", {})
    if not isinstance(selection, dict):
        return viability_mismatch or invalid_selection("missing_evidence_ready_repo_selection")
    execution = _read_json(root / "state" / "base_switch_execution.json", {})
    executed_route = execution.get("new_route", {}) if isinstance(execution, dict) and isinstance(execution.get("new_route"), dict) else {}
    authorized_switch = bool(
        executed_route
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
        and str(execution.get("decision") or "") == "route_switch_executed"
    )
    selected = selection.get("selected", {}) if isinstance(selection.get("selected"), dict) else {}
    if authorized_switch:
        selected = {**selected, **{key: value for key, value in executed_route.items() if value not in (None, "", [])}}
    selected_run = str(selection.get("fresh_find_run_id") or (route_run_id(executed_route) if authorized_switch else "") or "").strip()
    selection_plan_id = str(selection.get("selected_plan_id") or (route_plan_id(executed_route) if authorized_switch else "") or "").strip()
    selection_idea_id = str(selection.get("selected_idea_id") or (route_idea_id(executed_route) if authorized_switch else "") or current_selected_idea_id or "").strip()
    selected_route_run = str(selected.get("fresh_find_run_id") or "").strip()
    selected_route_plan_id = route_plan_id(selected)
    mismatch = route_mismatch_reason(
        selected_run=selected_run,
        selected_route_run=selected_route_run,
        selection_plan_id=selection_plan_id,
        selected_route_plan_id=selected_route_plan_id,
    ) if selected else ""
    if mismatch:
        return invalid_selection(mismatch, selected_run=selected_run, selection_plan_id=selection_plan_id or selected_route_plan_id, selection_idea_id=selection_idea_id)
    stage = str(selection.get("selection_stage") or selection.get("selected_by_stage") or selected.get("selection_stage") or selected.get("selected_by_stage") or "").strip()
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    raw_selection_gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "").strip()
    accepted = bool(
        selection.get("accepted_by_claude")
        or raw_selection_gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate"))
        or raw_selection_gate.startswith("accepted_by_deterministic_base_switch_gate")
        or selected.get("decision") == "selected_by_authorized_base_switch_gate"
        or decision.get("accept_as_current_best")
        or authorized_switch
    )
    public_selection_gate = raw_selection_gate
    if accepted and not raw_selection_gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
        public_selection_gate = raw_selection_gate or ("accepted_by_deterministic_base_switch_gate" if authorized_switch else "accepted_by_claude_topic_fit")
    in_current_find = selected_title_in_current_find(root, selected, decision) if selected else True
    valid = bool(selected and (stage == "environment_claude_code" or authorized_switch) and accepted and in_current_find)
    pending_candidate_blocked = raw_selection_gate == "blocked_pending_data_loader_for_claude_best_candidate"
    if not valid and not selected and pending_candidate_blocked:
        active_repo = _read_json(root / "state" / "active_repo.json", {})
        if isinstance(active_repo, dict):
            ready_datasets = active_repo.get("claim_ready_datasets") or active_repo.get("ready_datasets") or []
            if isinstance(ready_datasets, str):
                ready_datasets = [ready_datasets]
            claim_ready_dataset = str(active_repo.get("claim_ready_dataset") or active_repo.get("dataset") or "").strip()
            if claim_ready_dataset and claim_ready_dataset not in ready_datasets:
                ready_datasets = [claim_ready_dataset, *ready_datasets]
            ready_datasets = [str(item or "").strip() for item in ready_datasets if str(item or "").strip()]
            active_repo_path = str(active_repo.get("repo_path") or active_repo.get("local_path") or "").strip()
            if active_repo_path and ready_datasets:
                selected = {**active_repo}
                selected.setdefault("fresh_find_run_id", selected_run or current_run)
                selected.setdefault("selection_stage", "environment_claude_code")
                selected.setdefault("selected_by_stage", "environment_claude_code")
                selected.setdefault("claim_ready_dataset", ready_datasets[0])
                selected["claim_ready_datasets"] = ready_datasets
                selected_run = str(selected.get("fresh_find_run_id") or selected_run or current_run).strip()
                selection_plan_id = selection_plan_id or current_selected_plan_id
                selection_idea_id = selection_idea_id or current_selected_idea_id
                stage = "environment_claude_code"
                public_selection_gate = "current_active_route_pending_candidate_blocked"
                accepted = bool(active_repo.get("claude_topic_fit_decision") or active_repo.get("selection_stage") == "environment_claude_code")
                in_current_find = True
                valid = True
    current_candidate = selection.get("current_candidate") if isinstance(selection.get("current_candidate"), dict) else {}
    pending_candidate = selection.get("pending_environment_candidate") if isinstance(selection.get("pending_environment_candidate"), dict) else {}
    selection_status = str(selection.get("status") or "")
    selection_rationale_en = str(decision.get("rationale_en") or decision.get("rationale") or "").strip()
    selection_rationale_zh = str(decision.get("rationale_zh") or "").strip()
    repo_action_reason_en = str(decision.get("repo_action_reason_en") or decision.get("repo_action_reason") or "").strip()
    repo_action_reason_zh = str(decision.get("repo_action_reason_zh") or "").strip()
    data_action_reason_en = str(decision.get("data_action_reason_en") or decision.get("data_action_reason") or "").strip()
    data_action_reason_zh = str(decision.get("data_action_reason_zh") or "").strip()
    if valid:
        reason = "current_active_route_pending_candidate_blocked" if pending_candidate_blocked else "current_environment_base_selected"
    elif not selected:
        reason = "environment_repo_selection_blocked_current_run" if raw_selection_gate.startswith("continued_search") or selection_status.lower().startswith("blocked") else "environment_base_selection_pending_or_stale"
    elif not in_current_find:
        reason = "selected_base_not_in_current_find_recommendations"
    else:
        reason = "environment_base_selection_pending_or_stale"
    return {
        "valid": valid,
        "current_find_run_id": current_run,
        "fresh_find_run_id": selected_run,
        "selected_plan_id": selection_plan_id,
        "selected_idea_id": selection_idea_id,
        "current_selected_plan_id": current_selected_plan_id,
        "current_selected_idea_id": current_selected_idea_id,
        "selection_stage": stage or ("environment_claude_code" if authorized_switch else ""),
        "accepted_by_claude": accepted,
        "selected": selected,
        "selection_gate": public_selection_gate,
        "raw_selection_gate": raw_selection_gate,
        "selection_status": selection_status,
        "selection_decision": str(decision.get("decision") or ""),
        "selection_confidence": decision.get("confidence", ""),
        "selection_rationale": selection_rationale_en,
        "selection_rationale_en": selection_rationale_en,
        "selection_rationale_zh": selection_rationale_zh,
        "repo_action": str(decision.get("repo_action") or ""),
        "repo_action_reason": repo_action_reason_en,
        "repo_action_reason_en": repo_action_reason_en,
        "repo_action_reason_zh": repo_action_reason_zh,
        "env_action": str(decision.get("env_action") or ""),
        "data_action": str(decision.get("data_action") or ""),
        "data_action_reason": data_action_reason_en,
        "data_action_reason_en": data_action_reason_en,
        "data_action_reason_zh": data_action_reason_zh,
        "audited_count": selection.get("audited_count", 0),
        "evidence_ready_count": selection.get("evidence_ready_count", 0),
        "candidate_count": selection.get("candidate_count", 0),
        "current_candidate_index": selection.get("current_candidate_index", 0),
        "current_candidate_total": selection.get("current_candidate_total", 0),
        "current_action": str(selection.get("current_action") or ""),
        "current_candidate": current_candidate,
        "pending_candidate": pending_candidate,
        "progress_summary": str(selection.get("progress_summary") or ""),
        "elapsed_sec": selection.get("elapsed_sec", 0),
        "reason": reason,
    }


def _selected_base_gate_active(project_id: str, root: Path, cfg: dict[str, Any] | None = None) -> bool:
    """Current selected-base gates are project-generic; repo names are evidence, not route names."""
    env = _current_environment_selection(root)
    if env.get("valid"):
        return True
    gate = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    status = str(gate.get("decision") or "") if isinstance(gate, dict) else ""
    return status in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}

def _current_impl_repo_path(root: Path) -> str:
    env = _current_environment_selection(root)
    if env.get("valid"):
        selected = env.get("selected", {}) if isinstance(env.get("selected"), dict) else {}
        for key in ["repo_path", "local_path", "path"]:
            value = str(selected.get(key) or "").strip()
            if value:
                return value
    active = _read_json(root / "state" / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(active.get(key) or "").strip()
                if value:
                    return value
    impl = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def _artifact_match_repo_path(root: Path) -> str:
    # Non-recursive repo resolver for artifact filtering during summary projection.
    viability_current = _selected_base_viability_current_selection(root, _current_find_run_id_for_project(root))
    selected_current = viability_current.get("selected") if isinstance(viability_current.get("selected"), dict) else {}
    for key in ["repo_path", "local_path", "path"]:
        value = str(selected_current.get(key) or "").strip()
        if value:
            return value
    active = _read_json(root / "state" / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(active.get(key) or "").strip()
                if value:
                    return value
    selection = _read_json(root / "state" / "evidence_ready_repo_selection.json", {})
    selected = selection.get("selected", {}) if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    if selected:
        for key in ["repo_path", "local_path", "path"]:
            value = str(selected.get(key) or "").strip()
            if value:
                return value
    impl = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def _artifact_matches_current_repo(root: Path, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = _artifact_match_repo_path(root)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path", "path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def _state_payload_matches_current_repo(root: Path, payload: Any) -> bool:
    return _artifact_matches_current_repo(root, payload)


def _fresh_base_state_names(root: Path, suffix: str) -> list[str]:
    names = [f"fresh_base_{suffix}.json"]
    index = _read_json(root / "state" / "fresh_base_reference_reproduction_index.json", {})
    if isinstance(index, dict):
        for row in index.get("entries", []) if isinstance(index.get("entries", []), list) else []:
            if not isinstance(row, dict):
                continue
            value = str(row.get("state_audit_path") or "").strip()
            if value and value.endswith(f"{suffix}.json"):
                names.append(Path(value).name)
    return list(dict.fromkeys(names))


def _fresh_base_loader_probe(root: Path) -> dict[str, Any]:
    real_probe = _read_json(root / "state" / "real_dataset_probe.json", {})
    if isinstance(real_probe, dict) and real_probe:
        return real_probe
    for name in _fresh_base_state_names(root, "loader_contract_probe"):
        payload = _read_json(root / "state" / name, {})
        if _state_payload_matches_current_repo(root, payload):
            return payload
    return {}


def _reference_protocol_import_probe_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    verdict = str(out.get("verdict") or "").strip()
    results = out.get("results") if isinstance(out.get("results"), dict) else {}
    direct_import = results.get("direct_import") if isinstance(results.get("direct_import"), dict) else {}
    audit = results.get("dependency_audit") if isinstance(results.get("dependency_audit"), dict) else {}
    if not audit and isinstance(out.get("dependency_audit"), dict):
        audit = out.get("dependency_audit") or {}
    missing = audit.get("missing")
    if missing in (None, "") and isinstance(audit.get("missing_packages"), list):
        missing = len(audit.get("missing_packages") or [])
    total = audit.get("total_requirements") or audit.get("total")
    first_blocker = str(direct_import.get("blocker") or direct_import.get("error") or "").strip()
    import_failed = str(direct_import.get("status") or "") == "failed"
    imports = out.get("imports") if isinstance(out.get("imports"), dict) else {}
    if not first_blocker and imports:
        for item in imports.values():
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") == "failed":
                import_failed = True
                first_blocker = str(item.get("blocker") or item.get("error") or "").strip()
                break
    if verdict == "code_present_deps_missing" or import_failed:
        out.setdefault("status", "reference_protocol_probe_blocked")
        out.setdefault("decision", "dependency_install_required")
        summary = "reference protocol/import probe 已运行；代码结构存在，但当前环境依赖缺失"
        if missing not in (None, "") and total:
            summary += f"（缺失 {missing}/{total} 个 requirements）"
        if first_blocker:
            summary += f"，首个 import blocker: {first_blocker}"
        summary += "。"
        out.setdefault("human_summary", summary)
        out.setdefault("summary", summary)
    return out


def _fresh_base_protocol_probe(root: Path) -> dict[str, Any]:
    names = list(dict.fromkeys([*_fresh_base_state_names(root, "reference_protocol_probe"), "reference_protocol_import_probe.json"]))
    for name in names:
        payload = _read_json(root / "state" / name, {})
        if isinstance(payload, dict) and payload and _artifact_matches_current_repo(root, payload):
            if name == "reference_protocol_import_probe.json":
                return _reference_protocol_import_probe_public_payload(payload)
            return payload
    return {}


def _reference_protocol_probe_blocker_summary(protocol_probe: Any, base_display: str) -> str:
    if not isinstance(protocol_probe, dict) or not protocol_probe:
        return ""
    status = str(protocol_probe.get("status") or "").strip()
    decision = str(protocol_probe.get("decision") or "").strip()
    if status != "reference_protocol_probe_blocked" and decision != "dependency_install_required":
        return ""
    summary = str(protocol_probe.get("human_summary") or protocol_probe.get("summary") or "").strip()
    if summary:
        return f"{base_display} {summary}"
    return f"{base_display} reference protocol/import probe 已运行，但当前环境依赖缺失；需要先补齐依赖并重新验证 import。"


def _fresh_base_smoke_probe(root: Path) -> dict[str, Any]:
    for name in _fresh_base_state_names(root, "reference_smoke"):
        payload = _read_json(root / "state" / name, {})
        if _artifact_matches_current_repo(root, payload):
            return payload
    return {}


def _fresh_base_reference_audit(root: Path) -> dict[str, Any]:
    for name in [
        "fresh_base_reference_full_reproduction_audit.json",
        "fresh_base_reference_bounded_reproduction_audit.json",
        "fresh_base_reference_reproduction_audit.json",
    ]:
        payload = _read_json(root / "state" / name, {})
        if _artifact_matches_current_repo(root, payload):
            return payload
    return {}


def _fresh_base_reference_full_job(root: Path) -> dict[str, Any]:
    audit = _fresh_base_reference_audit(root)
    gate = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    paper_level_passed = bool(
        isinstance(audit, dict)
        and audit.get("mode") == "full"
        and audit.get("return_code") == 0
        and audit.get("audit_ready")
        and audit.get("paper_level_reproduction_passed")
    )
    for name in _fresh_base_state_names(root, "reference_full_reproduction_job"):
        payload = _read_json(root / "state" / name, {})
        if not _artifact_matches_current_repo(root, payload):
            continue
        out = dict(payload)
        pid = str(out.get("pid") or "").strip()
        alive = _pid_alive(pid) if pid else False
        status = str(out.get("status") or "").lower()
        if paper_level_passed or (isinstance(gate, dict) and gate.get("decision") == "continue_base" and not alive):
            out["status"] = "completed"
            out["decision"] = "ready_for_full_research_cycle"
            out["process_alive"] = False
            out["alive"] = False
        elif status == "running" and not alive:
            out["status"] = "stale"
            out["process_alive"] = False
            out["alive"] = False
        else:
            out["process_alive"] = alive
            out["alive"] = alive
        return out
    return {}


def _normalize_ready_dataset_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _payload_repo_paths(*payloads: Any) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        nested = payload.get("repo") if isinstance(payload.get("repo"), dict) else None
        candidates = [payload]
        if nested:
            candidates.append(nested)
        for row in candidates:
            for key in ("repo_path", "local_path", "path"):
                text = str(row.get(key) or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    paths.append(text)
    return paths


def _fresh_base_evidence_matches_route(
    impl: Any,
    real_probe: Any,
    selected: dict[str, Any] | None = None,
    active_repo: dict[str, Any] | None = None,
    repo: dict[str, Any] | None = None,
) -> bool:
    current_paths = _payload_repo_paths(selected or {}, active_repo or {}, repo or {})
    evidence_paths = _payload_repo_paths(impl if isinstance(impl, dict) else {}, real_probe if isinstance(real_probe, dict) else {})
    if len(set(evidence_paths)) > 1:
        return False
    if current_paths and evidence_paths and not any(path in current_paths for path in evidence_paths):
        return False
    return True


def _fresh_base_ready_datasets_from_evidence(
    root: Path,
    selected: dict[str, Any] | None = None,
    active_repo: dict[str, Any] | None = None,
    repo: dict[str, Any] | None = None,
    existing: Any = None,
) -> list[str]:
    ready = _normalize_ready_dataset_list(existing)
    impl = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    real_probe = _read_json(root / "state" / "real_dataset_probe.json", {})
    if _fresh_base_loader_contract_passed(root) and _fresh_base_evidence_matches_route(impl, real_probe, selected, active_repo, repo):
        if isinstance(real_probe, dict):
            ready.extend(_normalize_ready_dataset_list(real_probe.get("ready_datasets")))
        if isinstance(impl, dict):
            ready.extend(_normalize_ready_dataset_list(impl.get("ready_datasets")))
    return _normalize_ready_dataset_list(ready)


def _fresh_base_loader_contract_passed(root: Path) -> bool:
    impl = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    real_probe = _read_json(root / "state" / "real_dataset_probe.json", {})
    if isinstance(impl, dict):
        impl_status = str(impl.get("status") or "").strip()
        impl_ready = impl_status in {"implementation_ready", "implementation_ready_for_reference_probe"}
        impl_ready_datasets = _normalize_ready_dataset_list(impl.get("ready_datasets"))
        impl_blockers = _normalize_ready_dataset_list(impl.get("blocker_reasons"))
        real_ready_datasets = _normalize_ready_dataset_list(real_probe.get("ready_datasets")) if isinstance(real_probe, dict) else []
        real_blockers = _normalize_ready_dataset_list(real_probe.get("blocker_reasons")) if isinstance(real_probe, dict) else []
        probe_status = str(real_probe.get("status") or "").strip() if isinstance(real_probe, dict) else ""
        probe_decision = str(real_probe.get("decision") or "").strip() if isinstance(real_probe, dict) else ""
        probe_rows = real_probe.get("probes", []) if isinstance(real_probe, dict) and isinstance(real_probe.get("probes"), list) else []
        row_loader_success = any(isinstance(row, dict) and row.get("claim_ready") and row.get("loader_probe_success") for row in probe_rows)
        probe_passed = probe_status in {"passed", "real_dataset_probe_passed", "loader_probe_passed"} or probe_decision in {"loader_probe_complete", "loader_contract_passed", "ready_for_reference_probe"}
        if (
            impl_ready
            and (impl_ready_datasets or real_ready_datasets)
            and not (impl_blockers or real_blockers)
            and _fresh_base_evidence_matches_route(impl, real_probe)
            and (probe_passed or row_loader_success)
        ):
            return True
    data = _read_json(root / "state" / "fresh_base_data_acquisition.json", {})
    loader = _fresh_base_loader_probe(root)
    ready = loader.get("ready_datasets", []) if isinstance(loader, dict) and isinstance(loader.get("ready_datasets"), list) else []
    return bool(
        ready
        and isinstance(data, dict)
        and data.get("status") == "ready"
        and data.get("decision") == "ready_for_loader_probe"
        and isinstance(loader, dict)
        and loader.get("decision") == "loader_contract_passed"
    )

def _project_route_context(root: Path, project_id: str = "", cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or _read_json(root / "project.json", {})
    fresh = _read_json(root / "state" / "fresh_research_base.json", {})
    current = _read_json(root / "state" / "current_find_research_plan.json", {})
    plan = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    active = _read_json(root / "state" / "active_repo.json", {})
    selected: dict[str, Any] = {}
    if isinstance(fresh, dict) and isinstance(fresh.get("selected"), dict):
        selected.update(fresh.get("selected") or {})
    gate = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    if isinstance(gate, dict) and isinstance(gate.get("base_switch"), dict):
        maybe = gate["base_switch"].get("fresh_paper_base")
        if isinstance(maybe, dict):
            selected.update(maybe)
    repo = plan.get("repo", {}) if isinstance(plan, dict) and isinstance(plan.get("repo"), dict) else {}
    title = ""
    for value in [
        selected.get("title") if isinstance(selected, dict) else "",
        current.get("selected_base_title") if isinstance(current, dict) else "",
        active.get("selected_base_title") if isinstance(active, dict) else "",
        active.get("title") if isinstance(active, dict) else "",
        active.get("name") if isinstance(active, dict) else "",
        cfg.get("title") if isinstance(cfg, dict) else "",
        cfg.get("topic") if isinstance(cfg, dict) else "",
        project_id or root.name,
    ]:
        candidate = str(value or "").strip()
        if candidate:
            title = candidate
            break
    repo_name = ""
    for value in [
        repo.get("name") if isinstance(repo, dict) else "",
        repo.get("repo") if isinstance(repo, dict) else "",
        active.get("name") if isinstance(active, dict) else "",
        active.get("repo") if isinstance(active, dict) else "",
    ]:
        candidate = str(value or "").strip()
        if candidate:
            repo_name = candidate
            break
    repo_path = str(
        (repo.get("repo_path") if isinstance(repo, dict) else "")
        or (repo.get("local_path") if isinstance(repo, dict) else "")
        or (active.get("repo_path") if isinstance(active, dict) else "")
        or (active.get("local_path") if isinstance(active, dict) else "")
        or ""
    ).strip()
    venue = _display_venue((cfg.get("target_venue") or cfg.get("venue") or "") if isinstance(cfg, dict) else "")
    return {"title": title, "repo_name": repo_name, "repo_path": repo_path, "venue": venue, "selected": selected, "repo": repo}


def _selected_base_status_text(category: str, ctx: dict[str, Any], *, zh: bool = False) -> tuple[str, str, str]:
    title = str(ctx.get("title") or ("当前选定基底" if zh else "the selected base"))
    repo_name = str(ctx.get("repo_name") or ("当前选定仓库" if zh else "the selected repository"))
    legacy = " 历史路线只作为内部对照，不是当前主线。" if zh else " Previous routes are legacy/control only, not the current main route."
    if category in {"blocked_fresh_base_data_required", "fresh_base_data_required"}:
        if zh:
            return (
                f"环境阶段 Claude Code 已选择 {title}；{repo_name} 的真实数据/loader 合同尚未通过",
                "补齐当前基底需要的真实数据文件和 loader/import probe；通过前不训练、不写论文、不提升结论。" + legacy,
                "fresh paper base lacks loader-ready real data",
            )
        return (
            f"Environment-stage Claude Code selected {title}; real data/loader contract for {repo_name} is not evidence-ready.",
            "Resolve the selected base's real dataset files and loader/import probes before training, paper writing, or paper-conclusion promotion." + legacy,
            "fresh paper base lacks loader-ready real data",
        )
    if category in {"blocked_fresh_base_reference_probe_required", "fresh_base_reference_probe_required"}:
        if zh:
            return (
                f"{title} 的数据/loader 已就绪；参考协议/环境 manifest 探针仍未通过",
                "运行当前基底的有界只读参考协议/环境探针；通过前不训练、不写论文、不提升结论。" + legacy,
                "fresh base reference protocol/env manifest is not audited",
            )
        return (
            f"Data/loader is ready for {title}; reference protocol/env manifest probe is still required.",
            "Run bounded read-only reference-protocol/env probes for the selected base before training, paper writing, or paper-conclusion promotion." + legacy,
            "fresh base reference protocol/env manifest is not audited",
        )
    if category in {"blocked_fresh_base_reference_smoke_required", "fresh_base_reference_smoke_required"}:
        if zh:
            return (
                f"{title} 的参考协议探针已通过；有界 no-training reference smoke/audit 仍未通过",
                "运行当前基底的有界 no-training reference smoke/audit；通过前不完整训练、不写论文、不提升结论。" + legacy,
                "fresh base bounded reference smoke is not audited",
            )
        return (
            f"Reference protocol passed for {title}; bounded no-training reference smoke/audit is still required.",
            "Run bounded no-training reference smoke/audit for the selected base before full training, paper writing, or paper-conclusion promotion." + legacy,
            "fresh base bounded reference smoke is not audited",
        )
    if category in {"blocked_fresh_base_reference_reproduction_required", "fresh_base_reference_reproduction_required"}:
        if zh:
            return (
                f"{title} 的 bounded audit 已通过；论文级 full reference reproduction 仍需完成或监督",
                "继续监督当前基底的受审计 full reference reproduction；通过前不写论文、不提升结论。" + legacy,
                "fresh base reference reproduction is not audited",
            )
        return (
            f"Bounded audit passed for {title}; paper-level full reference reproduction is still required or running.",
            "Continue audited full reference reproduction for the selected base before paper writing or paper-conclusion promotion." + legacy,
            "fresh base reference reproduction is not audited",
        )
    if zh:
        return (
            f"环境阶段 Claude Code 已选择 {title}；仍需补齐代码/实现/数据协议",
            "继续找官方代码/产物，或在当前项目内实现该基底并建立真实数据/协议证据；通过前不训练、不写论文、不提升结论。" + legacy,
            "fresh paper base needs code/data/protocol implementation route",
        )
    return (
        f"Environment-stage Claude Code selected {title}; code/artifact search or implementation route is still required.",
        "Continue official-code/artifact search or implement the selected base with real data/protocol evidence before experiments or paper writing." + legacy,
        "fresh paper base needs code/data/protocol implementation route",
    )


def _empty_supervision_payload() -> dict[str, Any]:
    return {
        "status": "",
        "action": "",
        "action_rc": "",
        "generated_at": "",
        "issue_count": 0,
        "observation_count": 0,
        "observations": [],
        "repairs": [],
        "next_action": "",
        "full_reference_job": {},
        "full_cycle_job": {},
        "api": {},
    }


def _current_find_plan_summary(root: Path) -> dict[str, Any]:
    plan = _read_json(root / "state" / "current_find_research_plan.json", {})
    if not isinstance(plan, dict) or not plan:
        return {}
    ideas = plan.get("ideas", []) if isinstance(plan.get("ideas"), list) else []
    plans = plan.get("plans", []) if isinstance(plan.get("plans"), list) else []
    loop = plan.get("claude_code_autonomous_loop", []) if isinstance(plan.get("claude_code_autonomous_loop"), list) else []
    selected = plan.get("selected_base", {}) if isinstance(plan.get("selected_base"), dict) else {}
    return {
        "status": plan.get("status", ""),
        "run_id": plan.get("run_id", ""),
        "source": plan.get("source", ""),
        "readings": plan.get("current_find_reading_count", 0),
        "ideas": plan.get("current_find_idea_count", len(ideas)),
        "plans": plan.get("current_find_plan_count", len(plans)),
        "primary_route": plan.get("primary_route", ""),
        "fresh_base_status": plan.get("fresh_base_status", ""),
        "selected_base_title": selected.get("title", ""),
        "selected_base_venue": selected.get("venue", ""),
        "selected_base_year": selected.get("year", ""),
        "top_ideas": [
            {
                "idea_id": row.get("idea_id") or row.get("id") or "",
                "title": row.get("title", ""),
                "score": row.get("score") or row.get("idea_score") or "",
                "recommendation": row.get("recommendation", ""),
                "status": row.get("status", ""),
            }
            for row in ideas[:4]
            if isinstance(row, dict)
        ],
        "top_plans": [
            {
                "plan_id": row.get("plan_id", ""),
                "idea_id": row.get("idea_id", ""),
                "title": row.get("title", ""),
                "completed": bool(row.get("completed")),
            }
            for row in plans[:4]
            if isinstance(row, dict)
        ],
        "claude_code_autonomous_loop": loop[:6],
    }


def _taste_pair_status(project_root: Path) -> dict[str, Any]:
    compare_root = ROOT / "tmp" / "taste_compare"
    current_find = _current_find_results_light(project_root, project_root.name)
    comparison = _read_json(compare_root / "comparison.json", {})
    runs: list[dict[str, Any]] = []
    for index in [1, 2]:
        run_dir = compare_root / f"run{index}"
        find_payload = _read_json(run_dir / "find_results.json", {})
        exit_path = compare_root / f"run{index}.exit"
        log_path = compare_root / f"run{index}.log"
        exit_code = exit_path.read_text(encoding="utf-8", errors="replace").strip() if exit_path.exists() else ""
        log_tail = _tail_lines(log_path, 30)
        latest_line = next((line for line in reversed(log_tail) if line.strip()), "")
        exit_is_fresh = exit_path.exists() and (not log_path.exists() or exit_path.stat().st_mtime >= log_path.stat().st_mtime)
        finished_in_log = "finished finding pair run" in latest_line
        stage = "not_started"
        if log_tail and (not exit_code or not exit_is_fresh or not finished_in_log):
            stage = "running"
        elif exit_code == "0":
            stage = "completed"
        elif exit_code:
            stage = "failed"
        runs.append({
            "index": index,
            "status": stage,
            "exit_code": exit_code,
            "find": _find_summary_from_payload(find_payload),
            "log_path": str(log_path),
            "latest_log": latest_line,
            "log_tail": log_tail[-10:],
        })
    processes = _remote_process_rows()
    wrong_direction = [row for row in processes if row.get("kind") in {"full_cycle", "claude_session", "experiment_or_reproduction"}]
    current_counts = _find_summary_from_payload(current_find)
    strong_counts = [run.get("find", {}).get("strong_recommendations", 0) or run.get("find", {}).get("articles", 0) for run in runs]
    comparison_dict = comparison if isinstance(comparison, dict) else {}
    return {
        "compare_dir": str(compare_root),
        "driver_pid": _read_text(compare_root / "driver.pid", 100).strip(),
        "driver_log": str(compare_root / "driver.log"),
        "driver_log_tail": _tail_lines(compare_root / "driver.log", 20),
        "runs": runs,
        "comparison": comparison_dict,
        "current_find": current_counts,
        "processes": processes,
        "wrong_direction_processes": wrong_direction,
        "strong_min_required": 20,
        "strong_counts": strong_counts,
        "stable_enough": bool(comparison_dict.get("stable_enough")),
        "needs_attention": bool(wrong_direction) or any(int(count or 0) < 20 for count in strong_counts if count is not None) or (bool(comparison_dict) and not comparison_dict.get("stable_enough")),
    }

def update_runtime_config(project: str, patch: dict[str, Any]) -> dict[str, Any]:
    project = _safe_project(project)
    result = update_project_runtime(project, patch)
    _cleruntime_caches(project)
    return result


def create_project_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = create_project_settings(payload)
    project = str(cfg.get("name") or payload.get("id") or payload.get("name") or "").strip()
    return project_summary(project, compact=True)


def update_project_config(project: str, patch: dict[str, Any]) -> dict[str, Any]:
    project = _safe_project(project)
    update_project_settings(project, patch)
    _cleruntime_caches(project)
    return project_summary(project, compact=True)


def detect_runtime_config(project: str) -> dict[str, Any]:
    project = _safe_project(project)
    result = detect_project_runtime(project)
    _cleruntime_caches(project)
    return result


def runtime_status(project: str) -> dict[str, Any]:
    project = _safe_project(project)
    payload = runtime_diagnostics(project)
    _RUNTIME_DIAGNOSTICS_CACHE[project] = (time.monotonic() + RUNTIME_DIAGNOSTICS_TTL_SEC, payload)
    for key in list(_PROJECT_SUMMARY_CACHE):
        if key[0] == project:
            _PROJECT_SUMMARY_CACHE.pop(key, None)
    return payload


def _read_text(path: Path, max_chars: int = 60000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars] if path.exists() else ""
    except Exception as exc:
        return f"Failed to read {path}: {exc}"


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*[\"']?)[A-Za-z0-9._\-]+"),
]


def _redact_secrets(value: Any) -> str:
    text = str(value or "")
    for pattern in SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if match.lastindex:
                return match.group(1) + "[REDACTED]"
            return "[REDACTED]"
        text = pattern.sub(repl, text)
    return text


def _parse_utc_datetime(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _read_current_claude_instruction(root: Path, max_chars: int = 8000) -> str:
    text = _read_text(root / "state" / "claude_project_session_last_prompt.txt", max_chars + 2000).strip()
    if not text:
        return ""
    marker = "\n---\n"
    if marker in text:
        text = text.split(marker, 1)[0].strip()
    return text[:max_chars]


def _tail_text_preserve_lines(text: str, max_chars: int = 12000) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    tail = raw[-max_chars:]
    if "\n" in tail:
        tail = tail.split("\n", 1)[1]
    return tail.strip()


PUBLIC_INTERNAL_NAME_REPLACEMENTS = [
    ("native frontend skipped", "finding frontend skipped"),
    ("native frontend", "finding frontend"),
    ("planning/finding", "planning/finding"),
    ("state/finding", "state/finding"),
    ("finding_frontend", "finding_frontend"),
    ("finding", "finding"),
    ("run_frontend", "run_finding"),
    ("run-finding", "run-finding"),
    ("PaperOrchestra", "writing"),
    ("project agent", "research project agent"),
    ("Claude Code", "project agent"),
    ("claim-ready", "audit-ready"),
    ("claim_ready", "audit_ready"),
    ("paper_orchestra", "writing"),
    ("paper-orchestra", "writing"),
    ("deterministic base-switch gate", "deterministic base-switch gate"),
    ("deterministic base switch gate", "deterministic base-switch gate"),
    ("base-switch gate", "base-switch gate"),
    ("base_switch_gate", "base-switch gate"),
    ("base_switch_execution", "base-switch execution receipt"),
    ("selected_base_viability", "experiment_evidence_review"),
    ("selected-base", "current route"),
    ("current base", "current route"),
    ("environment_claude_code", "environment review"),
    ("legacy/control", "historical comparison"),
]


def _normalize_public_workspace_paths(text: str) -> str:
    """Rewrite stale sibling workspace paths in compact project summaries."""
    current_root = str(ROOT)
    parent = str(ROOT.parent)
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


def _public_internal_names(value: Any) -> str:
    text = str(value or "")
    protected = {
        "__MAIN_CLAUDE_CODE_ZH__": "主控 Claude Code",
        "__MAIN_CLAUDE_CODE_EN__": "main Claude Code",
    }
    text = re.sub(r"主控\s*Claude Code", "__MAIN_CLAUDE_CODE_ZH__", text, flags=re.IGNORECASE)
    text = re.sub(r"main\s+Claude Code", "__MAIN_CLAUDE_CODE_EN__", text, flags=re.IGNORECASE)
    for source, target in PUBLIC_INTERNAL_NAME_REPLACEMENTS:
        text = text.replace(source, target)
    text = re.sub(r"\bresearch\s+research\s+project\s+agent\b", "research project agent", text, flags=re.IGNORECASE)
    for token, replacement in protected.items():
        text = text.replace(token, replacement)
    return _normalize_public_workspace_paths(text)


def _public_status_summary_en(
    status: Any,
    *,
    base_title: str = "",
    active_experiment_training: bool = False,
    reference_full_job_live: bool = False,
    fresh_find_running: bool = False,
    recommendation_shortfall: Any = 0,
) -> str:
    status_text = str(status or "").strip() or "unknown"
    if fresh_find_running:
        return "A new Find run is running; previous recommendation counts are historical until the new artifacts land."
    try:
        shortfall = int(recommendation_shortfall or 0)
    except Exception:
        shortfall = 0
    if shortfall > 0:
        return "The Find recommendation gate has not passed; The workflow must repair literature search/scoring before experiments, paper writing, or paper-conclusion promotion."
    if active_experiment_training:
        return "Reference reproduction has passed and the current candidate experiment is running. Paper claims remain blocked until training, artifact-local audit, and downstream evidence gates complete."
    if reference_full_job_live:
        label = f" for {base_title}" if base_title else ""
        return f"Reference reproduction is running{label}; candidate experiments and paper conclusions remain blocked until the audit finishes."
    if status_text == "blocked_after_max_cycles":
        return "The full cycle has stopped after the configured cycles; there is no live full-cycle process. The next step is to continue auditable candidate-experiment evidence collection."
    if "blocked" in status_text:
        return "The current route is blocked by missing auditable candidate-experiment evidence. The workflow will keep the current base and must refresh evidence audits before promoting paper conclusions."
    if status_text == "running":
        return "The full cycle is running and the dashboard reflects the latest stage state."
    return f"Current status: {status_text}."


def _public_environment_selection_summary(env: Any) -> dict[str, Any]:
    src = env if isinstance(env, dict) else {}
    selected = src.get("selected") if isinstance(src.get("selected"), dict) else {}
    return {
        "valid": bool(src.get("valid")),
        "current_find_run_id": str(src.get("current_find_run_id") or ""),
        "fresh_find_run_id": str(src.get("fresh_find_run_id") or ""),
        "selected_plan_id": str(src.get("selected_plan_id") or selected.get("selected_plan_id") or ""),
        "selected_idea_id": str(src.get("selected_idea_id") or selected.get("selected_idea_id") or ""),
        "current_selected_plan_id": str(src.get("current_selected_plan_id") or ""),
        "current_selected_idea_id": str(src.get("current_selected_idea_id") or ""),
        "base_selection_status": str(src.get("base_selection_status") or ("selected" if src.get("valid") else "waiting_for_environment_review")),
        "selection_stage": _public_internal_names(src.get("selection_stage") or selected.get("selection_stage") or ""),
        "selection_gate": _public_internal_names(src.get("selection_gate") or src.get("raw_selection_gate") or ""),
        "selection_status": str(src.get("selection_status") or ""),
        "selection_decision": _public_internal_names(src.get("selection_decision") or ""),
        "selection_confidence": src.get("selection_confidence", ""),
        "selection_rationale": _public_internal_names(src.get("selection_rationale") or src.get("selection_rationale_en") or ""),
        "selection_rationale_en": _public_internal_names(src.get("selection_rationale_en") or src.get("selection_rationale") or ""),
        "selection_rationale_zh": _public_internal_names(src.get("selection_rationale_zh") or ""),
        "repo_action": _public_internal_names(src.get("repo_action") or ""),
        "repo_action_reason": _public_internal_names(src.get("repo_action_reason") or src.get("repo_action_reason_en") or ""),
        "repo_action_reason_en": _public_internal_names(src.get("repo_action_reason_en") or src.get("repo_action_reason") or ""),
        "repo_action_reason_zh": _public_internal_names(src.get("repo_action_reason_zh") or ""),
        "env_action": _public_internal_names(src.get("env_action") or ""),
        "data_action": _public_internal_names(src.get("data_action") or ""),
        "data_action_reason": _public_internal_names(src.get("data_action_reason") or src.get("data_action_reason_en") or ""),
        "data_action_reason_en": _public_internal_names(src.get("data_action_reason_en") or src.get("data_action_reason") or ""),
        "data_action_reason_zh": _public_internal_names(src.get("data_action_reason_zh") or ""),
        "audited_count": src.get("audited_count", 0),
        "evidence_ready_count": src.get("evidence_ready_count", 0),
        "candidate_count": src.get("candidate_count", 0),
        "current_candidate": src.get("current_candidate") if isinstance(src.get("current_candidate"), dict) else {},
        "current_action": str(src.get("current_action") or ""),
        "progress_summary": str(src.get("progress_summary") or ""),
        "accepted_by_claude": bool(src.get("accepted_by_claude") or selected.get("accept_as_current_best")),
        "reason": _public_internal_names(src.get("reason") or ""),
        "selected": {
            "name": selected.get("name") or selected.get("repo") or "",
            "title": selected.get("title") or selected.get("literature_base_title") or "",
            "url": selected.get("url") or selected.get("repo_url") or "",
            "repo_path": selected.get("repo_path") or selected.get("local_path") or "",
            "dataset": selected.get("dataset") or selected.get("claim_ready_dataset") or "",
        } if selected else {},
        "details_hidden": True,
    }


def _public_experiment_row(row: Any) -> dict[str, Any]:
    src = row if isinstance(row, dict) else {}
    out: dict[str, Any] = {}
    for key, value in src.items():
        if isinstance(value, str):
            text = _public_internal_names(value)
            text = text.replace("selected-base full reference reproduction remains the comparison control", "current reference reproduction remains the comparison control")
            text = text.replace("candidate_observation_only", "候选实验观察记录")
            out[key] = text
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif key in {"metrics", "parsed_metrics", "scores"} and isinstance(value, dict):
            out[key] = value
        elif key in {"loss_curve", "metric_rows"} and isinstance(value, list):
            out[key] = value[:20]
    return out


def _experiment_row_time_value(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    for key in ("finished_at", "timestamp", "updated_at", "started_at", "created_at", "时间"):
        value = row.get(key)
        if value in (None, ""):
            continue
        parsed = _parse_utc_datetime(value)
        if parsed:
            return parsed.timestamp()
        text = str(value).strip()
        match = re.search(r"(20\d{2})[-_/]?(\d{2})[-_/]?(\d{2})[_ T-]?(\d{2})?(\d{2})?(\d{2})?", text)
        if match:
            year, month, day, hour, minute, second = match.groups()
            try:
                return dt.datetime(
                    int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0),
                    tzinfo=dt.timezone.utc,
                ).timestamp()
            except Exception:
                pass
    artifact = str(row.get("artifact_path") or row.get("证据路径") or "").strip()
    match = re.search(r"(20\d{6})[_-](\d{6})", artifact)
    if match:
        try:
            return dt.datetime.strptime("_".join(match.groups()), "%Y%m%d_%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception:
            pass
    return 0.0


def _sorted_experiment_source_rows(rows: Any) -> list[dict[str, Any]]:
    source_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return sorted(source_rows, key=_experiment_row_time_value, reverse=True)


def _public_experiment_rows(rows: Any, limit: int = 12) -> list[dict[str, Any]]:
    source_rows = _sorted_experiment_source_rows(rows)
    return [_public_experiment_row(row) for row in source_rows[:limit]]


def _public_claude_response_text(text: Any, max_chars: int = 2400) -> str:
    raw = _public_internal_names(_redact_secrets(str(text or ""))).strip()
    if not raw:
        return ""
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        if stripped.startswith("```"):
            continue
        lines.append(stripped)
    compact = "\n".join(lines).strip() or raw
    return _tail_text_preserve_lines(compact, max_chars)


def _is_stale_allowlisted_tool_policy_block(last_result: dict[str, Any]) -> bool:
    status = str(last_result.get("status") or "").strip()
    if status != "blocked_tool_policy":
        return False
    guard = last_result.get("tool_policy_guard") if isinstance(last_result.get("tool_policy_guard"), dict) else {}
    command = str(guard.get("command") or "").strip().lower()
    if not command:
        return False
    dangerous_markers = ["loss.backward", "optimizer.step", "for epoch", "--artifact_dir", "nohup", "&"]
    if any(marker in command for marker in dangerous_markers):
        return False
    # This was an old false positive fixed in the Bash policy guard.  Keep the
    # audit file on disk, but never re-present it as the latest Claude reply.
    return bool(re.search(r"(?:^|\s)(?:\S*/)?finetune[\w.-]*\.py", command) and re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", command))


def _claude_reply_text_from_log_tail(lines: list[str]) -> str:
    """Return only Claude Code output lines from a live agent log tail.

    wrapper/supervisor prefixes such as objective/method contracts are
    internal control text, not a Claude reply.  Completed turns still use the
    clean last_result stdout path below, so this only affects live projection.
    """
    reply_lines: list[str] = []
    for raw in lines:
        line = str(raw or "").strip()
        if not line.startswith("Claude:"):
            continue
        reply_lines.append(line.split(":", 1)[1].lstrip())
    return _redact_secrets("\n".join(item for item in reply_lines if item).strip())


def _completed_claude_reply_from_stdout(stdout: str) -> str:
    raw = str(stdout or "")
    chunks = [match.group(1) for match in re.finditer(r"(?ms)^Claude:\s?(.*?)(?=^Claude:|^claude:|\Z)", raw)]
    text = "".join(chunks).strip()
    # Prefer the final assistant answer over earlier tool-progress chunks.
    parts = re.split(r"\n---\s*\n", text)
    if len(parts) > 1:
        text = "---\n" + parts[-1].lstrip()
    elif text.startswith("---"):
        text = text
    return _redact_secrets(text.strip())


def _live_claude_receipt(root: Path, last_finished: dt.datetime | None) -> dict[str, Any]:
    candidates: list[tuple[dt.datetime, dict[str, Any]]] = []
    agents_state = _read_json(root / "state" / "agents.json", {})
    agents = agents_state.get("agents") if isinstance(agents_state, dict) else []
    if isinstance(agents, list):
        for row in agents:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip()
            if status not in {"running", "queued", "cancelling"}:
                continue
            role = str(row.get("role") or "").strip()
            agent_id = str(row.get("id") or "").strip()
            stage_text = str(row.get("stage") or "").strip()
            command_text = _command_text(row.get("command") or "")
            tail = row.get("log_tail")
            raw_lines: list[str] = []
            if isinstance(tail, list):
                raw_lines = [str(item) for item in tail[-100:] if str(item).strip()]
            is_claude_agent = (
                role in {"claude-main", "claude-worker"}
                or "claude_project_session.py" in command_text
                or "claude -p" in command_text
            )
            if not is_claude_agent:
                continue
            pid = row.get("pid")
            # The synthetic main agent can expose live Claude progress via log_tail
            # without owning the Claude process PID. Do not discard that live
            # receipt solely because pid is absent.
            if status in {"running", "cancelling"} and pid and not _pid_alive(pid):
                continue
            updated = _parse_utc_datetime(row.get("updated_at")) or _parse_utc_datetime(row.get("created_at"))
            if last_finished and updated and updated <= last_finished:
                continue
            if not raw_lines:
                continue
            text = _tail_text_preserve_lines(_claude_reply_text_from_log_tail(raw_lines), 12000)
            if not text:
                continue
            receipt = {
                "status": status,
                "stage": row.get("stage") or "running-claude-code",
                "return_code": "",
                "started_at": row.get("created_at", ""),
                "finished_at": "",
                "session_id": "",
                "instruction": "",
                "response_markdown": _tail_text_preserve_lines(text, 12000),
                "response_source": "live_agent_log_tail",
            }
            candidates.append((updated or dt.datetime.now(dt.timezone.utc), receipt))
    full_cycle = _read_json(root / "state" / "full_research_cycle.json", {})
    current = full_cycle.get("current_running_stage") if isinstance(full_cycle, dict) and str(full_cycle.get("status") or "").lower() == "running" else {}
    if isinstance(current, dict) and _pid_alive(current.get("pid")):
        current_stage = str(current.get("stage") or "")
        current_cmd = _command_text(current.get("command") or current.get("cmd") or "")
        current_text = str(current.get("stdout_tail") or "")
        current_is_claude = "claude_project_session.py" in current_cmd or "claude -p" in current_cmd or current_stage.startswith("full-cycle-") and "Claude:" in current_text
        updated = _parse_utc_datetime(current.get("last_heartbeat_at")) or _parse_utc_datetime(current.get("started_at"))
        if current_is_claude and (not last_finished or not updated or updated > last_finished):
            text = _tail_text_preserve_lines(_redact_secrets(current_text.strip()), 12000)
            if text:
                receipt = {
                    "status": "running" if current.get("heartbeat") else str(current.get("status") or "running"),
                    "stage": current.get("stage") or "running-claude-code",
                    "return_code": "",
                    "started_at": current.get("started_at", ""),
                    "finished_at": "",
                    "session_id": "",
                    "instruction": "",
                    "response_markdown": _tail_text_preserve_lines(text, 12000),
                    "response_source": "live_claude_stdout_tail",
                }
                candidates.append((updated or dt.datetime.now(dt.timezone.utc), receipt))
    if not candidates:
        return {}
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def _base_switch_gate_authorizes_current_switch(root: Path) -> bool:
    gate = _read_json(root / "state" / "base_switch_gate.json", {})
    return bool(
        isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and gate.get("switch_authorized") is True
    )


def _claude_receipt_contradicts_current_route(root: Path, receipt: dict[str, Any]) -> bool:
    if _base_switch_gate_authorizes_current_switch(root):
        return False
    text = "\n".join([
        str(receipt.get("instruction") or ""),
        str(receipt.get("response_markdown") or ""),
    ]).lower()
    if not text:
        return False
    stale_markers = [
        "authorized_by_deterministic_base_switch_gate",
        "accepted_by_deterministic_base_switch_gate",
    ]
    return any(marker in text for marker in stale_markers)


def _latest_claude_receipt(root: Path, session: Any = None, last_result: Any = None, *, include_live: bool = True) -> dict[str, Any]:
    session = session if isinstance(session, dict) else _read_json(root / "state" / "claude_project_session.json", {})
    last_result = last_result if isinstance(last_result, dict) else _read_json(root / "state" / "claude_project_session_last_result.json", {})
    if not isinstance(session, dict):
        session = {}
    if not isinstance(last_result, dict):
        last_result = {}
    last_finished = _parse_utc_datetime(last_result.get("finished_at"))
    if include_live:
        live = _live_claude_receipt(root, last_finished)
        if live:
            return {} if _claude_receipt_contradicts_current_route(root, live) else live
    if _is_stale_allowlisted_tool_policy_block(last_result):
        return {}
    claude_json = last_result.get("claude_json") if isinstance(last_result.get("claude_json"), dict) else {}
    clean_result = str(claude_json.get("result") or "").strip()
    stdout_text = str(last_result.get("stdout") or "").strip()
    if clean_result:
        raw_reply = _redact_secrets(clean_result)
        response_source = "claude_json.result"
    else:
        raw_reply = _completed_claude_reply_from_stdout(stdout_text)
        response_source = "stdout.cleaned" if raw_reply else ""
    instruction = _redact_secrets(str(last_result.get("instruction") or session.get("last_instruction") or "").strip())
    receipt = {
        "status": last_result.get("status", ""),
        "stage": last_result.get("stage", ""),
        "return_code": last_result.get("return_code", ""),
        "started_at": last_result.get("started_at", ""),
        "finished_at": last_result.get("finished_at", ""),
        "session_id": last_result.get("session_id") or session.get("session_id") or "",
        "instruction": instruction,
        "response_markdown": raw_reply,
        "response_source": response_source,
    }
    if not (raw_reply or instruction):
        return {}
    return {} if _claude_receipt_contradicts_current_route(root, receipt) else receipt


CLAUDE_PANEL_STAGES = ("environment", "experiment", "paper")


def _safe_claude_session_key(value: Any = "") -> str:
    key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    return key.strip("._-")[:80] or "main"


def _claude_state_path(root: Path, stem: str, session_key: str = "main", suffix: str = ".json") -> Path:
    key = _safe_claude_session_key(session_key)
    if key == "main":
        return root / "state" / f"{stem}{suffix}"
    return root / "state" / f"{stem}_{key}{suffix}"


def _claude_report_path(root: Path, session_key: str = "main") -> Path:
    key = _safe_claude_session_key(session_key)
    if key == "main":
        return root / "reports" / "claude_project_session.md"
    return root / "reports" / f"claude_project_session_{key}.md"


def _claude_session_key_from_last_result_path(path: Path) -> str:
    stem = "claude_project_session_last_result"
    name = path.name
    if name == f"{stem}.json":
        return "main"
    if name.startswith(f"{stem}_") and name.endswith(".json"):
        return _safe_claude_session_key(name[len(stem) + 1:-5])
    return ""


def _claude_stage_session_keys(stage: Any) -> list[str]:
    panel = _safe_claude_session_key(stage).lower()
    if panel == "environment":
        return ["environment"]
    if panel == "experiment":
        return ["experiment"]
    if panel == "paper":
        return ["paper", "writing_revision", "writing_refinement", "paper_preview_repair"]
    return []


def _claude_stage_session_keys_for_root(root: Path, stage: Any) -> list[str]:
    panel = _safe_claude_session_key(stage).lower()
    keys = list(_claude_stage_session_keys(panel))
    if panel != "experiment":
        return keys
    state_dir = root / "state"
    if not state_dir.exists():
        return keys
    seen = set(keys)
    for path in sorted(state_dir.glob("claude_project_session_last_result*.json")):
        key = _claude_session_key_from_last_result_path(path)
        if not key or key == "main":
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _claude_stage_matches_panel(result_stage: Any, panel_stage: str) -> bool:
    stage = str(result_stage or "").strip().lower().replace("_", "-")
    panel = str(panel_stage or "").strip().lower()
    if not stage or panel not in CLAUDE_PANEL_STAGES:
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


def _claude_is_current_route_global_result(result: Any) -> bool:
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


def _read_claude_session_bundle(root: Path, session_key: str = "main") -> tuple[dict[str, Any], dict[str, Any]]:
    session = _read_json(_claude_state_path(root, "claude_project_session", session_key), {})
    last_result = _read_json(_claude_state_path(root, "claude_project_session_last_result", session_key), {})
    return session if isinstance(session, dict) else {}, last_result if isinstance(last_result, dict) else {}


def _stage_receipt_from_session_key(root: Path, session_key: str, panel_stage: str, *, fallback_from_session_key: str = "") -> dict[str, Any]:
    session, last_result = _read_claude_session_bundle(root, session_key)
    receipt = _latest_claude_receipt(root, session, last_result, include_live=False)
    if not receipt:
        return {}
    if fallback_from_session_key and not _claude_stage_matches_panel(receipt.get("stage"), panel_stage):
        return {}
    if panel_stage == "paper" and _paper_receipt_stale_for_current_venue(root, {**last_result, **receipt}):
        return {}
    receipt = dict(receipt)
    receipt["stage_session_key"] = _safe_claude_session_key(session_key)
    receipt["stage_local"] = not bool(fallback_from_session_key)
    if fallback_from_session_key:
        receipt["fallback_from_session_key"] = _safe_claude_session_key(fallback_from_session_key)
        receipt["fallback_reason"] = "historical_global_receipt_for_same_stage"
    return receipt


def _latest_claude_receipt_for_stage(root: Path, stage: Any) -> dict[str, Any]:
    panel_stage = _safe_claude_session_key(stage).lower()
    if panel_stage not in CLAUDE_PANEL_STAGES:
        return {}
    candidates: list[tuple[str, dict[str, Any]]] = []
    skipped_stale_for_venue = False
    if panel_stage == "experiment":
        main_session, main_last_result = _read_claude_session_bundle(root, "main")
        if main_last_result and _claude_is_current_route_global_result(main_last_result):
            receipt = _latest_claude_receipt(root, main_session, main_last_result, include_live=False)
            if receipt:
                row = dict(receipt)
                row["stage_session_key"] = "main"
                row["stage_local"] = False
                row["fallback_from_session_key"] = "main"
                row["fallback_reason"] = "current_route_global_receipt_for_experiment"
                candidates.append((str(row.get("finished_at") or row.get("started_at") or ""), row))
    for session_key in _claude_stage_session_keys_for_root(root, panel_stage):
        session, last_result = _read_claude_session_bundle(root, session_key)
        receipt = _latest_claude_receipt(root, session, last_result, include_live=False)
        if receipt and not _claude_stage_matches_panel(receipt.get("stage"), panel_stage):
            continue
        if receipt and panel_stage == "paper" and _paper_receipt_stale_for_current_venue(root, {**last_result, **receipt}):
            skipped_stale_for_venue = True
            continue
        if receipt:
            row = dict(receipt)
            row["stage_session_key"] = _safe_claude_session_key(session_key)
            row["stage_local"] = True
            candidates.append((str(row.get("finished_at") or row.get("started_at") or ""), row))
    if candidates:
        return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]
    fallback = _stage_receipt_from_session_key(root, "main", panel_stage, fallback_from_session_key="main")
    if fallback:
        return fallback
    if panel_stage == "paper" and skipped_stale_for_venue:
        current = _display_venue(_project_configured_venue(_read_json(root / "project.json", {}))) or "当前投稿目标"
        return {
            "status": "blocked",
            "stage": "paper",
            "response_markdown": f"当前投稿目标为 {current}；旧论文写作回执属于其他 venue，已隐藏。需要重新生成当前 venue 的项目代理写作/自审回执。",
            "response_source": "venue_filtered_placeholder",
            "stage_session_key": "paper",
            "stage_local": False,
            "fallback_reason": "stage_receipt_stale_for_current_venue",
        }
    return {}


def _public_claude_receipts_by_stage(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for stage in CLAUDE_PANEL_STAGES:
        public = _public_claude_receipt(_latest_claude_receipt_for_stage(root, stage))
        if public:
            out[stage] = public
    return out


def _public_claude_receipt(receipt: Any) -> dict[str, Any]:
    """Expose only compact latest Claude status, not raw project-content logs."""
    src = receipt if isinstance(receipt, dict) else {}
    raw_response = str(src.get("response_markdown") or "").strip()
    if not raw_response and not src:
        return {}
    out: dict[str, Any] = {}
    for key in ["status", "stage", "return_code", "started_at", "finished_at", "session_id", "response_source", "stage_session_key", "fallback_from_session_key", "fallback_reason"]:
        value = src.get(key)
        if isinstance(value, str):
            out[key] = _public_internal_names(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
    status = str(out.get("status") or "").strip() or "unknown"
    stage = str(out.get("stage") or "").strip() or "project"
    if status == "running":
        response = f"项目代理正在处理 {stage}；详细审计保留在远端日志/receipt 中，普通页面只展示处理摘要。"
    elif raw_response:
        response = f"项目代理最近一次处理已记录；阶段={stage}，状态={status}。详细审计保留在远端日志/receipt 中。"
    else:
        response = f"项目代理状态已记录；阶段={stage}，状态={status}。"
    stale_placeholder = (
        str(src.get("fallback_reason") or "") == "stage_receipt_stale_for_current_venue"
        and str(src.get("response_source") or "") == "venue_filtered_placeholder"
    )
    out["stage_local"] = bool(src.get("stage_local", False))
    out["response_markdown"] = response
    out["full_response_available"] = bool(raw_response) and not stale_placeholder
    out["response_chcount"] = 0 if stale_placeholder else len(raw_response)
    out["content_compacted"] = bool(raw_response) and not stale_placeholder
    out["raw_response_hidden"] = bool(raw_response) and not stale_placeholder
    out["public_projection"] = "latest_claude_status_only"
    return out

def _public_base_switch_gate(value: Any) -> dict[str, Any]:
    src = value if isinstance(value, dict) else {}
    out: dict[str, Any] = {}
    for key in ["status", "decision", "switch_authorized", "authorization_status", "updated_at"]:
        item = src.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[key] = item
    summary = "参考复现已通过；当前还缺少可审计、可写入论文的候选实验结果。完成本地审计前，系统不会更换当前基底，也不会提升论文结论。"
    out["summary_zh"] = summary
    out["summary_en"] = "Reference reproduction has passed, but the current route still lacks audited candidate-experiment evidence. The workflow will not change the current base or promote paper conclusions before local audit completes."
    out["human_summary"] = summary
    return out


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _compact_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _compact_text(value: Any, max_chars: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _reference_gate_for_current_route_display(
    ref_gate: dict[str, Any],
    status: str,
    ready_datasets: Any,
    protocol_probe: Any = None,
    base_display: str = "当前基底",
) -> dict[str, Any]:
    public = _public_gate_status_summary(ref_gate)
    ready = _normalize_ready_dataset_list(ready_datasets)
    decision = str(public.get("decision") or (ref_gate.get("decision") if isinstance(ref_gate, dict) else "") or "").strip()
    protocol_blocker_summary = _reference_protocol_probe_blocker_summary(protocol_probe, base_display)
    if protocol_blocker_summary or status == "blocked_fresh_base_reference_probe_required" or (ready and decision == "no_viable_base_switch_route"):
        message = protocol_blocker_summary or "当前基底真实数据/loader 已通过；等待参考协议/环境 manifest 探针。"
        return {
            "status": "blocked",
            "decision": "dependency_install_required" if protocol_blocker_summary else "fresh_base_reference_probe_required",
            "human_summary": message,
            "summary": message,
            "reason": message,
            "blockers": [],
            "warnings": [],
        }
    return public


def _public_environment_stage(
    *,
    status: str,
    env: dict[str, Any],
    selected: dict[str, Any],
    active_repo: dict[str, Any],
    repo_name: str,
    repo_url: str,
    repo_path: str,
    ref_gate: dict[str, Any],
    reference_full_job: dict[str, Any] | None = None,
    route_dataset: str = "",
    route_ready_datasets: list[Any] | None = None,
    protocol_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = env if isinstance(env, dict) else {}
    selected = selected if isinstance(selected, dict) else {}
    active_repo = active_repo if isinstance(active_repo, dict) else {}
    reference_full_job = reference_full_job if isinstance(reference_full_job, dict) else {}

    def scalar(src: Any, keys: list[str]) -> dict[str, Any]:
        row = src if isinstance(src, dict) else {}
        out: dict[str, Any] = {}
        for key in keys:
            value = row.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
        return out

    probe_summary = selected.get("probe_summary") if isinstance(selected.get("probe_summary"), dict) else {}
    claim_ready_probe = selected.get("claim_ready_probe") if isinstance(selected.get("claim_ready_probe"), dict) else {}
    route_ready = route_ready_datasets if isinstance(route_ready_datasets, list) else []
    route_ready = [str(item).strip() for item in route_ready if str(item).strip()]
    ready_datasets = route_ready or selected.get("claim_ready_datasets") or selected.get("ready_datasets") or active_repo.get("claim_ready_datasets") or active_repo.get("ready_datasets") or probe_summary.get("claim_ready_datasets") or []
    if isinstance(ready_datasets, str):
        ready_datasets = [ready_datasets]
    if not isinstance(ready_datasets, list):
        ready_datasets = []
    ready_datasets = [str(item).strip() for item in ready_datasets if str(item).strip()]
    dataset = str(
        route_dataset
        or selected.get("claim_ready_dataset")
        or selected.get("dataset")
        or active_repo.get("claim_ready_dataset")
        or active_repo.get("dataset")
        or claim_ready_probe.get("dataset")
        or (ready_datasets[0] if ready_datasets else "")
    ).strip()
    required_files_ok = claim_ready_probe.get("required_files_ok")
    loader_probe = claim_ready_probe.get("loader_probe") if isinstance(claim_ready_probe.get("loader_probe"), dict) else {}
    loader_status = "passed" if (route_ready or required_files_ok is True or loader_probe.get("return_code") == 0 or probe_summary.get("probe_return_code") == 0) else "pending"
    data_status = "real_data_loader_ready" if (dataset or ready_datasets) and loader_status == "passed" else "waiting_for_real_data_loader_evidence"
    repo_status = "selected" if repo_path else "waiting_for_repo_selection"
    ref_public = _reference_gate_for_current_route_display(ref_gate, status, ready_datasets, protocol_probe, repo_name or "当前基底")
    existing_reference_completed = bool((repo_name or repo_path) and str(reference_full_job.get("status") or "").lower() in {"completed", "done", "pass"})
    if not env.get("valid") and existing_reference_completed:
        ref_public = {
            **ref_public,
            "status": "completed",
            "decision": reference_full_job.get("decision") or "ready_for_full_research_cycle",
            "human_summary": "已有基底的参考复现已完成；当前 Find 的新基底选择尚未完成。",
            "summary": "已有基底的参考复现已完成；当前 Find 的新基底选择尚未完成。",
        }
    ref_status = str(ref_public.get("status") or ref_gate.get("status") or "").strip()
    ref_decision = str(ref_public.get("decision") or ref_gate.get("decision") or "").strip()
    progress_summary = str(env.get("progress_summary") or "").strip()
    selection_rationale = str(env.get("selection_rationale_zh") or env.get("selection_rationale") or "").strip()
    selection_gate_text = str(env.get("selection_gate") or env.get("raw_selection_gate") or "").strip()
    if env.get("valid"):
        module_summary = "当前基底已由环境阶段选定；本步骤只展示仓库、真实数据/loader、实验环境和参考复现状态。"
    elif selection_gate_text.startswith("continued_search") and selection_rationale:
        module_summary = "当前 Find 的新基底选择被 topic-fit 门控阻塞：" + _field_text(selection_rationale, 260)
    elif repo_name or repo_path or dataset or ready_datasets or reference_full_job:
        module_summary = "已有环境证据已保留展示；当前 Find 的新基底选择尚未完成。"
    elif status == "waiting_for_current_find_results":
        module_summary = "环境配置等待当前 Find 产物稳定后再选择基底。"
    elif progress_summary:
        module_summary = "环境配置正在审计候选仓库：" + _public_internal_names(progress_summary)
    else:
        module_summary = "环境配置等待基底选择和真实数据/loader 证据。"

    pending_candidate = env.get("pending_candidate") if isinstance(env.get("pending_candidate"), dict) else {}
    pending_candidate_public: dict[str, Any] = {}
    if pending_candidate:
        pending_candidate_public = {
            "name": pending_candidate.get("name") or pending_candidate.get("repo") or "",
            "title": pending_candidate.get("title") or pending_candidate.get("literature_base_title") or "",
            "url": pending_candidate.get("url") or pending_candidate.get("repo_url") or "",
            "repo_path": pending_candidate.get("repo_path") or pending_candidate.get("local_path") or "",
            "status": "non_authoritative_pending_loader_proposal",
            "selection_gate": env.get("selection_gate", ""),
        }

    selection_public = {
        "valid": bool(env.get("valid")),
        "current_find_run_id": env.get("current_find_run_id", ""),
        "fresh_find_run_id": env.get("fresh_find_run_id", ""),
        "selected_plan_id": env.get("selected_plan_id", ""),
        "selected_idea_id": env.get("selected_idea_id", ""),
        "current_selected_plan_id": env.get("current_selected_plan_id", ""),
        "current_selected_idea_id": env.get("current_selected_idea_id", ""),
        "selection_stage": _public_internal_names(env.get("selection_stage", "")),
        "selection_gate": _public_internal_names(env.get("selection_gate", "")),
        "selection_status": str(env.get("selection_status") or ""),
        "raw_selection_gate": _public_internal_names(env.get("raw_selection_gate", "")),
        "selection_decision": _public_internal_names(env.get("selection_decision", "")),
        "selection_confidence": env.get("selection_confidence", ""),
        "selection_rationale": _public_internal_names(env.get("selection_rationale") or env.get("selection_rationale_en") or ""),
        "selection_rationale_en": _public_internal_names(env.get("selection_rationale_en") or env.get("selection_rationale") or ""),
        "selection_rationale_zh": _public_internal_names(env.get("selection_rationale_zh") or ""),
        "repo_action": _public_internal_names(env.get("repo_action") or ""),
        "repo_action_reason": _public_internal_names(env.get("repo_action_reason") or env.get("repo_action_reason_en") or ""),
        "repo_action_reason_en": _public_internal_names(env.get("repo_action_reason_en") or env.get("repo_action_reason") or ""),
        "repo_action_reason_zh": _public_internal_names(env.get("repo_action_reason_zh") or ""),
        "env_action": _public_internal_names(env.get("env_action") or ""),
        "data_action": _public_internal_names(env.get("data_action") or ""),
        "data_action_reason": _public_internal_names(env.get("data_action_reason") or env.get("data_action_reason_en") or ""),
        "data_action_reason_en": _public_internal_names(env.get("data_action_reason_en") or env.get("data_action_reason") or ""),
        "data_action_reason_zh": _public_internal_names(env.get("data_action_reason_zh") or ""),
        "audited_count": env.get("audited_count", 0),
        "evidence_ready_count": env.get("evidence_ready_count", 0),
        "candidate_count": env.get("candidate_count", 0),
        "current_candidate": env.get("current_candidate") if isinstance(env.get("current_candidate"), dict) else {},
        "pending_candidate": pending_candidate_public,
        "current_action": str(env.get("current_action") or ""),
        "progress_summary": progress_summary,
        "accepted_by_claude": bool(env.get("accepted_by_claude")),
        "reason": env.get("reason", ""),
    }
    if selected:
        selection_public["selected_base"] = {
            "name": selected.get("name") or selected.get("repo") or repo_name,
            "title": selected.get("title") or selected.get("literature_base_title") or "",
            "url": selected.get("url") or selected.get("repo_url") or repo_url,
            "repo_path": repo_path,
            "dataset": dataset,
        }

    checks = [
        {"id": "repo", "label_zh": "仓库", "label_en": "Repo", "status": repo_status, "summary": repo_name or repo_path or "未选择"},
        {"id": "data_loader", "label_zh": "真实数据/loader", "label_en": "Real data / loader", "status": data_status, "summary": dataset or ", ".join(str(x) for x in ready_datasets[:3]) or "等待证据"},
        {"id": "reference_reproduction", "label_zh": "参考复现", "label_en": "Reference reproduction", "status": ref_status or "not_started", "summary": ref_public.get("human_summary") or ref_decision or ref_status or "等待门控"},
    ]

    return {
        "status": status,
        "summary": module_summary,
        "summary_zh": module_summary,
        "summary_i18n": {"zh": module_summary, "en": "Environment step shows only repo, real-data/loader, experiment env, and reference-reproduction status."},
        "module_summary": module_summary,
        "module_summary_zh": module_summary,
        "module_summary_i18n": {"zh": module_summary, "en": "Environment step shows only repo, real-data/loader, experiment env, and reference-reproduction status."},
        "repo_status": repo_status,
        "data_status": data_status,
        "loader_status": loader_status,
        "dataset": dataset,
        "ready_datasets": ready_datasets[:8],
        "repo_path": repo_path,
        "active_repo": {"name": repo_name, "repo": repo_url, "repo_path": repo_path, "local_path": repo_path},
        "pending_candidate": pending_candidate_public,
        "selection": selection_public,
        "reference_reproduction_gate": ref_public,
        "reference_full_job": scalar(reference_full_job, ["status", "decision", "pid", "process_alive", "log_path"]),
        "checks": checks,
        "details_hidden": True,
    }


def _compact_file_artifact(name: str, path: Path, kind: str, content: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "path": str(path),
        "content_truncated": False,
    }
    if path.exists():
        try:
            stat = path.stat()
            info.update({"size": stat.st_size, "mtime": stat.st_mtime})
        except OSError:
            pass
    if kind == "json":
        if isinstance(content, dict):
            info["content"] = {
                "summary": {key: _compact_scalar(value) for key, value in content.items() if _compact_scalar(value) is not None},
                "counts": {
                    key: len(value)
                    for key, value in content.items()
                    if isinstance(value, (list, dict))
                },
            }
        elif isinstance(content, list):
            info["content"] = {"count": len(content), "preview": content[:5]}
        else:
            info["content"] = content
        info["content_truncated"] = True
        return info
    text = str(content or "")
    info["content"] = _compact_text(text, 12000)
    info["content_truncated"] = len(text) > 12000
    return info


def _compact_paper_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id", "title", "venue", "venue_id", "year", "track", "presentation_type", "presentation_label", "presentation_labels", "quality_labels", "doi", "url", "pdf_url",
        "recommendation_score", "recommendation_score_v2", "score", "score_source",
        "fit_score", "llm_fit_score", "diversity_score",
        "abstract", "abstract_zh", "abstract_en",
        "reason", "reason_zh", "reason_en",
        "fit_explanation", "fit_explanation_zh", "fit_explanation_en",
    ]
    compact = {key: row.get(key) for key in keys if key in row}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    doi = str(row.get("doi") or metadata.get("doi") or "").strip()
    if doi:
        compact.setdefault("doi", doi)
    if not str(compact.get("url") or "").strip():
        for key in ["url", "doi_url", "publisher_url", "acm_abs_url", "dblp_record_url"]:
            value = str(row.get(key) or metadata.get(key) or "").strip()
            if value:
                compact["url"] = value
                break
    if not str(compact.get("pdf_url") or "").strip():
        for key in ["pdf_url", "acm_pdf_url", "acm_epdf_url", "open_access_pdf_url"]:
            value = str(row.get(key) or metadata.get(key) or "").strip()
            if value:
                compact["pdf_url"] = value
                break
    abstract = _clean_literature_abstract(row)
    if abstract and not str(compact.get("abstract") or "").strip():
        compact["abstract"] = abstract
        compact["abstract_en"] = abstract
    for score_key in ["score", "recommendation_score", "recommendation_score_v2", "fit_score", "llm_fit_score", "diversity_score"]:
        if score_key in compact:
            compact[score_key] = _display_score_value(row, score_key)
    if row.get("missing_abstract_guard"):
        compact["missing_abstract_guard"] = row.get("missing_abstract_guard")
    for key in ["title", "matched_topic_route", "source_supported_adaptive_route", "foundation_invalid_reason", "topic_evidence"]:
        if key in compact:
            compact[key] = _compact_text(compact[key], 220)
    for key in [
        "abstract", "abstract_zh", "abstract_en",
        "reason", "reason_zh", "reason_en",
        "fit_explanation", "fit_explanation_zh", "fit_explanation_en",
    ]:
        if key in compact:
            compact[key] = _compact_text(compact[key], 650)
    return compact


def _compact_rows(rows: Any, limit: int, row_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for item in rows[:limit]:
        if isinstance(item, dict):
            out.append(row_fn(item) if row_fn else dict(item))
    return out


def _gate_status_summary(value: Any) -> dict[str, Any]:
    src = value if isinstance(value, dict) else {}
    keys = [
        "status", "decision", "decision_reason", "human_summary", "summary",
        "reason", "current_goal", "metric_name", "margin", "comparison_pass",
        "experiment_count", "completed_experiment_count", "recent_experiment_count",
        "real_metric_runs", "audit_ready_real_metric_runs", "candidate_real_runs",
        "candidate_audit_ready_runs", "control_real_runs", "control_audit_ready_runs",
    ]
    out = {key: src.get(key) for key in keys if isinstance(src.get(key), (str, int, float, bool)) or src.get(key) is None}
    for row_key in ["best_candidate", "best_control", "best_audit_ready_control", "best_reproduction"]:
        row = src.get(row_key)
        if isinstance(row, dict):
            out[row_key] = {
                key: row.get(key)
                for key in ["experiment_id", "name", "method", "dataset", "metric_name", "metric_value", "audit_ready", "artifact_path", "audit_path"]
                if isinstance(row.get(key), (str, int, float, bool)) or row.get(key) is None
            }
    for list_key in ["blockers", "warnings"]:
        rows = src.get(list_key)
        if isinstance(rows, list):
            out[list_key] = rows[:3]
    return out


def _public_text_for_gate(raw: Any) -> str:
    if isinstance(raw, dict):
        text = str(raw.get("human_summary") or raw.get("summary") or raw.get("issue") or raw.get("reason") or raw.get("decision_reason") or raw.get("status") or "")
    else:
        text = str(raw or "")
    text = " ".join(text.split())
    lower = text.lower()
    if not text:
        return ""
    reference_pass_markers = ("reference reproduction passed", "reference_reproduction_gate pass", "paper_level_reproduction_passed", "completed_reference_reproduction")
    if any(marker in lower for marker in reference_pass_markers) or ("参考复现" in text and "已通过" in text):
        return "参考复现已通过；当前重点是补出当前主线下可审计、可写入论文的候选实验证据。论文写作和结论提升仍保持阻塞。"
    if "bounded audit passed" in lower or "paper-level full reference reproduction" in lower or "fresh_base_reference_reproduction_required" in lower:
        return "当前参考工作的数据、loader、协议和小规模 smoke 已通过；TASTE 正在跑论文级 full reference reproduction。结果落盘并通过审计前，不启动候选实验、论文写作或结论提升。"
    if "no audit-ready promotable" in lower or "non-promotable candidates" in lower or "promotable candidate" in lower or "current best candidate" in lower or "当前最佳候选" in text or "当前基线" in text:
        return "当前还没有可写入论文的、经过审计的候选方法实验；已有候选只能作为观察或历史对照。具体下一步由项目代理读取当前证据后决定。"
    if "reference reproduction" in lower or "reference_reproduction" in lower or "below target" in lower:
        return "参考复现相关状态需要审计确认；若 gate 已通过，下一步是补当前主线候选实验，而不是论文或结论提升。"
    if "hold-markdown-only" in lower or "submission" in lower or "paper_evidence" in lower:
        return "论文证据/投稿门控未通过；当前只能保留审计材料，不能进入论文或结论提升。"
    if "llm" in lower and ("quota" in lower or "api" in lower or "rate" in lower):
        return "LLM/API 配置或额度不可用，自动评分和后续判断需要先恢复。"
    if len(text) > 220 or text.count(';') >= 2 or text.count('/') >= 8:
        return "当前存在内部审计门控阻塞；网页只展示摘要，完整证据见对应 state/report 文件。"
    return _blocker_text_zh(text)


_PUBLIC_RUN_SUMMARY_ACTION_MARKERS = (
    "；当前最高优先级",
    "; current highest priority",
    "；下一步:",
    "；下一步：",
    "；阻塞:",
    "；阻塞：",
    "\n下一步:",
    "\n下一步：",
    "\n阻塞:",
    "\n阻塞：",
)

def _public_run_summary_without_action_plan(raw: Any) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    for marker in _PUBLIC_RUN_SUMMARY_ACTION_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    # If an old blocker/action-plan sentence was appended without a clear
    # Chinese marker, keep only the stable runtime identity in public summaries.
    lowered = text.lower()
    if "完整科研自循环" in text and ("deterministic base-switch gate" in lowered or "base_switch_gate" in lowered or "selected_base_viability" in lowered):
        parts = []
        for part in text.split("；"):
            keep = any(token in part for token in ("完整科研自循环", "阶段=", "PID=", "运行时长="))
            if keep:
                parts.append(part)
        if parts:
            text = "；".join(parts).strip()
    return text




def _full_cycle_summary_claims_live(summary: Any) -> bool:
    text = " ".join(str(summary or "").split())
    if not text:
        return False
    lowered = text.lower()
    if "没有正在运行" in text and "PID=" not in text:
        return False
    if text.startswith("完整科研自循环正在运行") or text.startswith("完整科研循环正在运行"):
        return True
    if "PID=" in text and "正在运行" in text and "没有正在运行" not in text:
        return True
    return "full-cycle running" in lowered or "full research cycle is running" in lowered


def _terminal_full_cycle_summary(full_cycle: Any, *, base_title: str = "") -> str:
    src = full_cycle if isinstance(full_cycle, dict) else {}
    status = str(src.get("status") or "stale_full_research_cycle_snapshot").strip() or "stale_full_research_cycle_snapshot"
    latest = src.get("latest_step") if isinstance(src.get("latest_step"), dict) else {}
    stage = str(latest.get("stage") or src.get("public_phase") or "full-cycle")
    phase = str(latest.get("phase") or _phase_from_stage(stage) or "full-cycle")
    current = src.get("current_blocker") if isinstance(src.get("current_blocker"), dict) else {}
    latest_blockers = src.get("latest_blockers") if isinstance(src.get("latest_blockers"), list) else []
    blocker = current or (latest_blockers[0] if latest_blockers and isinstance(latest_blockers[0], dict) else {})
    blocker_text = ""
    if blocker:
        blocker_text = " ".join(str(blocker.get("human_summary") or blocker.get("summary") or blocker.get("issue") or "").split())
    if not blocker_text:
        blocker_text = " ".join(str(src.get("current_goal") or "").split())
    if len(blocker_text) > 260:
        blocker_text = blocker_text[:257] + "..."
    if status == "blocked_after_max_cycles":
        summary = f"完整科研自循环已停止在最大轮次后；最后步骤={stage or 'full-cycle'}；阶段={phase or 'full-cycle'}；没有正在运行的 full-cycle。"
    elif status == "completed":
        summary = "完整科研自循环已完成；当前没有正在运行的 full-cycle。"
    elif status.startswith("blocked") or status in {"blocked", "stale_full_research_cycle_snapshot"}:
        summary = f"完整科研自循环已停止；当前状态={status}；没有正在运行的 full-cycle。"
    else:
        summary = f"完整科研自循环未检测到存活进程；当前状态={status}；没有正在运行的 full-cycle。"
    if base_title:
        summary += f"当前基底：{base_title}。"
    if blocker_text and blocker_text not in summary:
        summary += blocker_text
    return _public_run_summary_without_action_plan(summary)


def _sanitize_stale_full_cycle_summary(full_cycle: Any, full_job: Any = None, *, root: Path | None = None, base_title: str = "") -> dict[str, Any]:
    """Remove stale live-PID wording when the full-cycle is not actually live."""
    if not isinstance(full_cycle, dict):
        return {}
    out = dict(full_cycle)
    job = full_job if isinstance(full_job, dict) else out.get("full_cycle_job") if isinstance(out.get("full_cycle_job"), dict) else {}
    pid = str(job.get("pid") or "").strip()
    job_live = _full_cycle_job_is_live(job)
    status = str(out.get("status") or "").strip()
    stale_live_text = _full_cycle_summary_claims_live(out.get("summary")) or _full_cycle_summary_claims_live(out.get("summary_zh"))
    if status == "running" and not job_live:
        out["status"] = "stale_full_research_cycle_snapshot"
        stale_live_text = True
    if not job_live and stale_live_text:
        replacement = _terminal_full_cycle_summary(out, base_title=base_title)
        out["summary"] = replacement
        out["summary_zh"] = replacement
        if isinstance(out.get("full_cycle_job"), dict):
            out["full_cycle_job"] = {**out["full_cycle_job"], "process_alive": False, "alive": False}
        if root is not None:
            state_path = root / "state" / "full_research_cycle.json"
            raw = _read_json(state_path, {})
            if isinstance(raw, dict):
                raw_status = str(raw.get("status") or "").strip()
                raw["summary"] = replacement
                raw["summary_zh"] = replacement
                if raw_status == "running" and not job_live:
                    raw["status"] = "stale_full_research_cycle_snapshot"
                if isinstance(raw.get("full_cycle_job"), dict):
                    raw["full_cycle_job"] = {**raw["full_cycle_job"], "process_alive": False, "alive": False}
                try:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    state_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
    return out

def _public_gate_status_summary(value: Any) -> dict[str, Any]:
    out = _gate_status_summary(value)
    src = value if isinstance(value, dict) else {}
    raw_blockers = src.get("blockers") if isinstance(src.get("blockers"), list) else []
    status = str(src.get("status") or "").strip().lower()
    decision = str(src.get("decision") or "").strip().lower()

    # Public compact state is for humans. Keep run IDs, artifact paths and raw
    # blocker strings in the underlying state/report files, not in dashboard cards.
    for private_key in [
        "best_candidate",
        "best_control",
        "best_audit_ready_control",
        "best_reproduction",
        "comparisons",
    ]:
        out.pop(private_key, None)

    summary = _public_text_for_gate(src)
    if not summary and raw_blockers:
        summary = _public_text_for_gate(raw_blockers[0])
    if summary == status and raw_blockers:
        summary = _public_text_for_gate(raw_blockers[0])
    if (not summary or summary == "blocked") and status == "blocked":
        raw_text = " ".join(str(item).lower() for item in raw_blockers[:2])
        if "reference" in decision or "reference" in raw_text:
            summary = "参考复现门控还未通过；现有指标只能用于审计，不能作为论文结论。"
        else:
            summary = "当前科研门控阻塞；系统会按最高优先级继续处理，原始证据保留在 state/report 文件中。"
    if summary:
        out["human_summary"] = summary
        out["summary"] = summary
        out["reason"] = summary
    if raw_blockers:
        public_blockers = []
        for item in raw_blockers[:3]:
            public = _public_text_for_gate(item)
            if public and public not in public_blockers:
                public_blockers.append(public)
        if public_blockers:
            out["blockers"] = public_blockers

    return out


def _selected_base_viability_public_blocker(gate: Any, base_display: str = "", base_switch_gate: Any = None) -> dict[str, Any]:
    row = gate if isinstance(gate, dict) else {}
    status = str(row.get("status") or "").strip().lower()
    decision = str(row.get("decision") or "").strip().lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return {}

    semantic = row.get("semantic_data_provenance_review") if isinstance(row.get("semantic_data_provenance_review"), dict) else {}
    text_meta = semantic.get("text_metadata_provenance") if isinstance(semantic.get("text_metadata_provenance"), dict) else {}
    base_switch = base_switch_gate if isinstance(base_switch_gate, dict) else {}
    failed_rows = base_switch.get("failed_checks") if isinstance(base_switch.get("failed_checks"), list) else []
    if not failed_rows and isinstance(base_switch.get("checks"), list):
        failed_rows = [item for item in base_switch.get("checks", []) if isinstance(item, dict) and item.get("status") != "pass"]
    failed_check_ids = [str(item.get("id") or "").strip() for item in failed_rows if isinstance(item, dict) and str(item.get("id") or "").strip()]
    candidate_route = base_switch.get("candidate_route") if isinstance(base_switch.get("candidate_route"), dict) else {}
    candidate_route_present = any(str(candidate_route.get(key) or "").strip() for key in ["repo", "title", "repo_path", "proposed_path_hint"])
    base_switch_not_authorized = bool(
        str(base_switch.get("status") or "").strip().lower() == "blocked"
        and str(base_switch.get("decision") or "").strip().lower() == "base_switch_not_authorized"
    )
    base_label = str(
        base_display
        or row.get("selected_base_title")
        or row.get("current_selected_repo")
        or row.get("current_repo")
        or "当前基底"
    ).strip()
    dataset = str(text_meta.get("dataset") or row.get("dataset") or row.get("selected_dataset") or "").strip()
    dataset_clause = f"（数据集：{dataset}）" if dataset else ""
    semantic_required = bool(
        semantic.get("deterministic_gate_required")
        or (
            str(semantic.get("status") or "").strip().lower() == "blocked"
            and bool(semantic.get("project_requires_llm_semantics"))
            and not bool(semantic.get("has_real_llm_embedding_evidence"))
            and text_meta.get("has_text_metadata_evidence") is False
        )
    )
    semantic_public = {
        "status": str(semantic.get("status") or ""),
        "deterministic_gate_required": bool(semantic.get("deterministic_gate_required")),
        "project_requires_llm_semantics": bool(semantic.get("project_requires_llm_semantics")),
        "llm_semantic_guard_status": str(semantic.get("llm_semantic_guard_status") or ""),
        "has_real_llm_embedding_evidence": bool(semantic.get("has_real_llm_embedding_evidence")),
        "has_text_metadata_evidence": bool(text_meta.get("has_text_metadata_evidence")),
        "dataset": dataset,
        "base_switch_gate_status": str(base_switch.get("status") or ""),
        "base_switch_gate_decision": str(base_switch.get("decision") or ""),
        "base_switch_candidate_route_present": candidate_route_present,
        "base_switch_failed_checks": failed_check_ids[:10],
    }
    if semantic_required:
        summary = (
            f"{base_label} 参考复现已通过，但当前数据路线{dataset_clause}没有可审计的 LLM/text-semantic 文本/元数据 provenance；"
            "LLM semantic evidence gate 已阻塞。继续运行纯行为或损失级候选实验无法清除此门控，"
            "必须先通过 deterministic base-switch / semantic-provenance gate。"
        )
        next_action = (
            "运行 deterministic base-switch / semantic-provenance gate；候选路线保持 proposal-only，或补齐当前路线保存 ID 映射的"
            "原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe。通过前不继续纯行为级候选实验、不写论文、不提升结论。"
        )
        project_summary = (
            "完整科研自循环已停在 semantic-provenance/base-switch 门控；"
            f"{base_label} 参考复现已通过，但当前数据路线{dataset_clause}缺少 LLM/text-semantic 实验证据所需的可审计文本/元数据 provenance。"
        )
        if base_switch_not_authorized:
            missing_candidate = "candidate_route_proposal_exists" in failed_check_ids or not candidate_route_present
            if missing_candidate:
                summary = (
                    f"{base_label} 参考复现已通过，但当前数据路线{dataset_clause}没有可审计的 LLM/text-semantic 文本/元数据 provenance；"
                    "继续运行纯行为或损失级候选实验无法清除此门控。确定性切换门控已执行但未授权，"
                    "因为还没有独立、可审计、可追溯到当前 Find/read 的候选路线 proposal。"
                )
                next_action = (
                    "补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe；"
                    "或生成可追溯到当前 Find/read 的 candidate base-switch proposal，并完成 loader/data/protocol/smoke/full-reference/artifact-local audit 后再刷新 gate。"
                )
                project_summary = (
                    "完整科研自循环已停在 semantic-provenance/base-switch 门控；确定性 gate 已执行但未授权，"
                    "当前缺少可审计候选路线 proposal 或当前路线文本/元数据 provenance。"
                )
            else:
                failed_text = "、".join(failed_check_ids[:5]) or "候选路线证据"
                summary = (
                    f"缺少 LLM/text-semantic 数据 provenance：{base_label} 参考复现已通过，但当前数据路线{dataset_clause}仍没有可审计的文本/元数据 provenance；"
                    f"确定性切换门控已执行且未授权，候选路线仍有未通过检查：{failed_text}。"
                    "下一步只能补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe，"
                    "或补齐上列候选路线未通过检查后刷新 deterministic base-switch gate。"
                )
                next_action = (
                    "补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe；"
                    "或补齐上列候选路线未通过检查后刷新 deterministic base-switch gate。"
                    "gate 通过前不切换基底、不写论文、不提升结论。"
                )
                project_summary = "完整科研自循环已停在 semantic-provenance/base-switch gate 证据审计；候选路线存在但尚未获得确定性授权。"
        return {
            "category": "semantic_data_provenance_required",
            "title": "缺少 LLM/text-semantic 数据 provenance",
            "summary": summary,
            "next_action": next_action,
            "project_summary": project_summary,
            "scientific_progress_summary": summary,
            "semantic_data_provenance": semantic_public,
        }

    if decision == "base_switch_gate_required":
        summary = "参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果；在独立授权前不能更换当前基底或提升论文结论。"
        return {
            "category": "experiment_evidence_audit",
            "title": "缺少审计就绪候选实验证据",
            "summary": summary,
            "next_action": "等待项目代理读取当前缺口证据，并给出下一轮实验或修复动作。",
            "project_summary": "完整科研自循环已停在实验证据审计；参考复现已通过，但当前主线还缺少可审计、可写入论文的候选实验结果。",
            "scientific_progress_summary": summary,
            "semantic_data_provenance": semantic_public,
        }

    summary = "参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验证据；论文预览可以生成，但不能被标记为投稿通过。"
    return {
        "category": "experiment_evidence_audit",
        "title": "缺少当前主线候选实验证据",
        "summary": summary,
        "next_action": "等待项目代理读取候选实验证据缺口，并给出下一步实验动作。",
        "project_summary": "完整科研自循环已停在实验门控；参考复现已通过，但当前主线还缺少可审计、可写入论文的候选实验证据。",
        "scientific_progress_summary": summary,
        "semantic_data_provenance": semantic_public,
    }


def _public_experiment_module_summary(
    *,
    status: Any,
    reference_gate: Any,
    scientific_progress_gate: Any,
    experiment_iteration_audit: Any,
    experiment_rows: Any,
    record_rows: Any,
    completed_count: int,
    total_count: int,
    active_training: bool = False,
    reference_job_live: bool = False,
    fresh_find_running: bool = False,
    literature_gate_blocked: bool = False,
    recommendation_shortfall: int = 0,
    next_action: Any = "",
) -> dict[str, Any]:
    """Deterministic gate/count summary for the Experiment tab only."""
    ref = _public_gate_status_summary(reference_gate)
    science = _public_gate_status_summary(scientific_progress_gate)
    loop = _public_gate_status_summary(experiment_iteration_audit)
    ref_src = reference_gate if isinstance(reference_gate, dict) else {}
    science_src = scientific_progress_gate if isinstance(scientific_progress_gate, dict) else {}
    loop_src = experiment_iteration_audit if isinstance(experiment_iteration_audit, dict) else {}
    ref_status = str(ref.get("status") or ref_src.get("status") or "not_started")
    science_status = str(science.get("status") or science_src.get("status") or "not_started")
    loop_status = str(loop.get("status") or loop_src.get("status") or "not_started")
    rows_l = experiment_rows if isinstance(experiment_rows, list) else []
    record_l = record_rows if isinstance(record_rows, list) else []
    real_count = _completed_or_live_real_experiment_count(rows_l) or _completed_or_live_real_experiment_count(record_l)
    audit_ready_count = _audit_ready_completed_experiment_count(rows_l)
    if not audit_ready_count:
        audit_ready_count = len([
            row for row in record_l
            if isinstance(row, dict) and "通过" in str(row.get("审计状态") or "")
        ])
    total = int(total_count or len(rows_l) or len(record_l) or 0)
    completed = int(completed_count or audit_ready_count or 0)

    if fresh_find_running:
        zh = "实验模块等待当前 Find 产物落盘；本模块暂不启动新的复现、训练或候选实验。"
        en = "The Experiment module is waiting for the current Find artifacts; no new reproduction, training, or candidate run is started here."
        action_zh = "等待 Find 推荐、精读、想法和计划完成后，再进入环境与实验门控。"
        action_en = "Wait for Find recommendations, reading, ideas, and plans before environment and experiment gates."
    elif literature_gate_blocked or int(recommendation_shortfall or 0) > 0:
        zh = "实验模块暂停：当前 Find 推荐门控未过；本模块只保留既有实验审计记录，不启动新的训练或结论提升。"
        en = "The Experiment module is paused because the current Find recommendation gate has not passed; existing audit records remain, but no new training or paper-conclusion promotion starts."
        action_zh = "回到发现页修复本轮检索/评分包，门控通过后再继续实验。"
        action_en = "Repair the current Find retrieval/scoring packet on the Find page, then continue experiments after the gate passes."
    elif reference_job_live:
        zh = "实验模块正在等待参考复现任务完成；完成并刷新审计前，不启动候选实验。"
        en = "The Experiment module is waiting for the reference reproduction job to finish; candidate runs stay blocked until audit refreshes."
        action_zh = "等待参考复现日志、指标和审计文件落盘。"
        action_en = "Wait for reference-reproduction logs, metrics, and audit files to land."
    elif active_training:
        zh = "候选实验正在运行；训练完成并写入本地产物审计前，结果不能进入论文结论。"
        en = "A candidate experiment is running; results cannot support paper conclusions until training and artifact-local audit finish."
        action_zh = "等待训练完成，然后登记实验表、坏例/反例和审计结果。"
        action_en = "Wait for training to finish, then record metrics, bad cases/counterexamples, and audit results."
    else:
        def status_label(value: str, *, english: bool = False) -> str:
            normalized = _public_internal_names(value).replace("_", " ").strip().lower()
            zh_labels = {
                "pass": "已通过",
                "passed": "已通过",
                "blocked": "仍需补证",
                "blocked after max cycles": "已暂停，等待下一轮自动处理",
                "completed": "已完成",
                "done": "已完成",
                "running": "运行中",
                "not started": "尚未开始",
                "pending": "待处理",
            }
            en_labels = {
                "pass": "passed",
                "passed": "passed",
                "blocked": "needs more evidence",
                "blocked after max cycles": "paused after configured cycles",
                "completed": "completed",
                "done": "completed",
                "running": "running",
                "not started": "not started",
                "pending": "pending",
            }
            labels = en_labels if english else zh_labels
            return labels.get(normalized, normalized or ("unknown" if english else "未知"))

        ref_text = "参考复现已通过" if ref_status == "pass" else f"参考复现状态：{status_label(ref_status)}"
        if total:
            exp_text = f"当前主线共有 {total} 条实验/复现审计记录，{completed} 条已完成或审计就绪"
        elif real_count:
            exp_text = f"当前主线已有 {real_count} 条真实数据实验记录"
        else:
            exp_text = "当前主线还没有新的审计就绪候选实验记录"
        zh = f"{ref_text}；{exp_text}。科学进展：{status_label(science_status)}；实验循环：{status_label(loop_status)}。"
        en = f"Reference reproduction: {status_label(ref_status, english=True)}; current-route experiment/reproduction audit records: {completed}/{total or real_count or 0}. Scientific progress: {status_label(science_status, english=True)}; experiment loop: {status_label(loop_status, english=True)}."
        action_zh = "等待项目代理读取证据并给出具体下一步。"
        action_en = "Waiting for the project agent to read the evidence and choose the concrete next step."
    return {
        "source": "deterministic_gate_audit",
        "source_label": "来源：确定性门控审计（状态和计数由项目 artifact 计算，不是项目代理自由文本）",
        "source_label_en": "Source: deterministic gate audit (status and counts are computed from project artifacts, not free-form project-agent text)",
        "summary_source": "deterministic_gate_audit",
        "summary": zh,
        "summary_zh": zh,
        "summary_en": en,
        "summary_i18n": {"zh": zh, "en": en},
        "module_summary": zh,
        "module_summary_zh": zh,
        "module_summary_en": en,
        "module_summary_i18n": {"zh": zh, "en": en},
        "next_action": action_zh,
        "next_action_i18n": {"zh": action_zh, "en": action_en},
        "reference_status": ref_status,
        "scientific_progress_status": science_status,
        "experiment_loop_status": loop_status,
        "audit_ready_completed_experiment_count": audit_ready_count,
        "real_experiment_count": real_count,
    }


def _compact_literature_survey(survey: Any) -> Any:
    if not isinstance(survey, dict):
        return survey
    out = dict(survey)
    # Compact project polling must stay status/count oriented. Full paper rows
    # are exposed through article.md and the artifact endpoints, not this API.
    for key in [
        "strong_recommendations",
        "screened_ranking_audit_only",
        "survey_candidates",
        "read_candidates",
        "audit_candidates",
        "readings",
    ]:
        if isinstance(survey.get(key), list):
            out[f"{key}_count"] = len(survey.get(key) or [])
        out[key] = []
    return out


def _stage_literature_summary(survey: Any) -> dict[str, Any]:
    src = survey if isinstance(survey, dict) else {}
    return {
        "status": src.get("status", ""),
        "status_zh": src.get("status_zh", ""),
        "status_en": src.get("status_en", ""),
        "counts": src.get("counts", {}) if isinstance(src.get("counts"), dict) else {},
        "source_status": _compact_rows(src.get("source_status"), 8),
        "current_find_pipeline": src.get("current_find_pipeline", {}) if isinstance(src.get("current_find_pipeline"), dict) else {},
        "files": src.get("files", {}) if isinstance(src.get("files"), dict) else {},
        "note": src.get("note", ""),
        "note_zh": src.get("note_zh", ""),
        "note_en": src.get("note_en", ""),
    }


def _compact_trajectory_system(trajectory: Any) -> Any:
    if not isinstance(trajectory, dict):
        return trajectory
    keep_keys = [
        "updated_at",
        "phase",
        "phase_i18n",
        "assurance_status",
        "assurance_status_i18n",
        "landscape_nodes",
        "novelty_nodes",
        "failed_hypotheses",
        "unexplored_niches",
        "assurance_issue_count",
        "evo_phase_count",
        "recoverable_exception_count",
        "skill_contract_count",
        "third_party_stack_status",
        "third_party_stack_status_i18n",
        "third_party_source_count",
        "third_party_selected_module_count",
        "third_party_synced_skill_count",
        "direction_memory_entries",
        "evidence_integrity_status",
        "evidence_integrity_status_i18n",
        "evidence_integrity_issue_count",
        "evidence_integrity_score",
        "optimization_queue_size",
        "highest_optimization_priority",
        "blocker_action_plan_summary",
        "trajectory_checkpoint_count",
        "trajectory_delta_status",
        "trajectory_delta_status_i18n",
        "evolutionary_index_items",
        "graph_history_entries",
        "evolutionary_memory_ledger_entries",
        "landscape_assessment_status",
        "landscape_assessment_status_i18n",
        "evidence_manifest_ref_count",
        "weak_or_unsupported_claim_count",
        "trajectory_supervisor_status",
        "trajectory_supervisor_rounds",
        "trajectory_supervisor_latest",
        "capability_audit_status",
        "capability_status",
        "capability_audit_status_i18n",
        "capability_status_i18n",
        "end_to_end_verification_status",
        "end_to_end_verification_capability_status",
        "end_to_end_verification_status_i18n",
        "end_to_end_verification_capability_status_i18n",
        "end_to_end_verification_failed_checks",
        "end_to_end_verification_warning_checks",
        "end_to_end_verification_total_checks",
        "integrated_method_contracts",
        "summary_i18n",
        "memory",
        "files",
    ]
    out = {key: trajectory.get(key) for key in keep_keys if key in trajectory}
    out["optimization_queue"] = _compact_rows(trajectory.get("optimization_queue"), 5)
    out["next_objectives"] = _compact_rows(trajectory.get("next_objectives"), 5)
    out["agent_roles"] = _compact_rows(trajectory.get("agent_roles"), 8)
    out["assurance_issues"] = _compact_rows(trajectory.get("assurance_issues"), 5)
    blocker_plan = trajectory.get("blocker_action_plan")
    if isinstance(blocker_plan, dict):
        out["blocker_action_plan"] = {
            "status": blocker_plan.get("status", ""),
            "summary": blocker_plan.get("summary", {}),
            "actions": _compact_rows(blocker_plan.get("actions"), 8),
        }
    return out


def _compact_full_cycle(full_cycle: Any) -> Any:
    if not isinstance(full_cycle, dict):
        return full_cycle
    out = dict(full_cycle)
    if str(out.get("status") or "").lower() == "running":
        out.pop("finished_at", None)
        out.pop("completed_at", None)
    out["cycles"] = _compact_rows(full_cycle.get("cycles"), 2)
    out["latest_gate"] = _compact_gate(full_cycle.get("latest_gate", {}))
    out["stage_failures"] = _compact_rows(full_cycle.get("stage_failures"), 5)
    out["runtime_blockers"] = _compact_rows(full_cycle.get("runtime_blockers"), 5)
    blocker_plan = full_cycle.get("blocker_action_plan")
    if isinstance(blocker_plan, dict):
        out["blocker_action_plan"] = {
            "status": blocker_plan.get("status", ""),
            "summary": blocker_plan.get("summary", {}),
            "actions": _compact_rows(blocker_plan.get("actions"), 5),
        }
    fresh_impl = full_cycle.get("fresh_base_implementation_plan")
    if isinstance(fresh_impl, dict):
        out["fresh_base_implementation_plan"] = {
            "status": fresh_impl.get("status", ""),
            "selected_base_title": fresh_impl.get("selected_base_title", ""),
            "repo": fresh_impl.get("repo", {}),
            "ready_datasets": fresh_impl.get("ready_datasets", []),
            "blocked_datasets": fresh_impl.get("blocked_datasets", []),
            "blocker_reasons": fresh_impl.get("blocker_reasons", []),
        }
    return out


def _compact_gate(gate: Any) -> Any:
    if not isinstance(gate, dict):
        return gate
    keep = {}
    for key, value in gate.items():
        if key in {"paper_status", "reference_reproduction_gate"}:
            keep[key] = value
        elif isinstance(value, (str, int, float, bool)) or value is None:
            keep[key] = value
        elif key.endswith("_info") and isinstance(value, dict):
            keep[key] = {subkey: subvalue for subkey, subvalue in value.items() if isinstance(subvalue, (str, int, float, bool)) or subvalue is None}
    return keep


def _compact_stage_status(stages: Any) -> Any:
    if not isinstance(stages, dict):
        return stages
    out = dict(stages)
    experiment = dict(out.get("experiment", {})) if isinstance(out.get("experiment"), dict) else {}
    if experiment:
        experiment["trajectory_system"] = _compact_trajectory_system(experiment.get("trajectory_system", {}))
        # Literature survey belongs to the Find/discovery surface. Keeping a copy
        # under the experiment stage makes the UI/API imply that title crawling or
        # LLM scoring is part of experiment iteration, which is misleading.
        experiment.pop("literature_survey", None)
        experiment["full_research_cycle"] = _compact_full_cycle(experiment.get("full_research_cycle", {}))
        experiment["experiments"] = _compact_rows(experiment.get("experiments"), 30)
        record = experiment.get("experiment_record", {})
        if isinstance(record, dict):
            record = dict(record)
            rows = record.get("rows", [])
            record["rows"] = rows[-50:] if isinstance(rows, list) else []
            experiment["experiment_record"] = record
        out["experiment"] = experiment
    environment = dict(out.get("environment", {})) if isinstance(out.get("environment"), dict) else {}
    if environment:
        for key, limit in [
            ("repo_details", 30),
            ("dataset_details", 30),
            ("ready_dataset_details", 20),
            ("pending_dataset_details", 20),
            ("blocked_dataset_details", 20),
        ]:
            if isinstance(environment.get(key), list):
                environment[key] = environment[key][:limit]
        out["environment"] = environment
    paper = dict(out.get("paper", {})) if isinstance(out.get("paper"), dict) else {}
    if paper:
        paper.pop("full_research_cycle", None)
        paper.pop("state", None)
        paper.pop("science_gate_preflight", None)
        for key in ["science_gate_preflight_blockers", "paper_figure_failed"]:
            if isinstance(paper.get(key), list):
                paper[key] = paper[key][:10]
        out["paper"] = paper
    return out


def _human_supervision_summary(root: Path, compact: dict[str, Any], raw_summary: dict[str, Any]) -> dict[str, Any]:
    full_cycle = compact.get("full_research_cycle", {}) if isinstance(compact.get("full_research_cycle"), dict) else {}
    current_blocker = compact.get("current_blocker", {}) if isinstance(compact.get("current_blocker"), dict) else {}
    fresh_base = compact.get("fresh_base", {}) if isinstance(compact.get("fresh_base"), dict) else {}

    def _count_value(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, list):
            return len(value)
        return 0

    fresh_impl = full_cycle.get("fresh_base_implementation_plan", {}) if isinstance(full_cycle.get("fresh_base_implementation_plan"), dict) else {}
    if not fresh_impl:
        fresh_impl = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    find_plan = _read_json(root / "state" / "current_find_research_plan.json", {})
    data_probe = _read_json(root / "state" / "fresh_base_data_acquisition.json", {})
    loader_probe = _read_json(root / "state" / "real_dataset_probe.json", {})
    if not isinstance(loader_probe, dict) or not loader_probe:
        loader_probe = _fresh_base_loader_probe(root)
    protocol_probe = _fresh_base_protocol_probe(root)
    smoke_probe = _fresh_base_smoke_probe(root)
    reference_audit = _fresh_base_reference_audit(root)
    reference_full_job = _fresh_base_reference_full_job(root)
    active_repo = _read_json(root / "state" / "active_repo.json", {})
    paper_meta = _read_json(root / "paper" / "metadata" / "paper_metadata.json", {})
    cfg = raw_summary.get("config", {}) if isinstance(raw_summary.get("config"), dict) else _read_json(root / "project.json", {})
    project_id = str(raw_summary.get("project") or cfg.get("name") or root.name)
    paper_state = _active_paper_state(root, project_id, cfg if isinstance(cfg, dict) else {})
    supervision_tick = _read_json(root / "state" / "supervision_tick.json", {})
    selected_base_viability = _read_json(root / "state" / "selected_base_viability_gate.json", {})
    base_switch_execution = _read_json(root / "state" / "base_switch_execution.json", {})
    full_cycle_job = _normalize_full_cycle_job(root, project_id, supervision_tick.get("full_cycle_job", {}) if isinstance(supervision_tick, dict) else {})
    reference_gate = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    fresh_gate_decision = str(reference_gate.get("decision") or "") if isinstance(reference_gate, dict) else ""
    fresh_gate_status = str(full_cycle.get("status") or compact.get("full_status") or compact.get("status") or "")
    has_current_fresh_base_gate = bool(
        fresh_gate_decision in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}
        or fresh_gate_status in {"blocked_fresh_base_implementation_required", "blocked_fresh_base_data_required", "blocked_fresh_base_reference_probe_required", "blocked_fresh_base_reference_smoke_required", "blocked_fresh_base_reference_reproduction_required"}
        or (isinstance(fresh_impl, dict) and fresh_impl.get("status") in {"implementation_ready", "implementation_ready_for_reference_probe", "blocked_fresh_base_data_required", "blocked_fresh_base_implementation_required"})
    )
    if not has_current_fresh_base_gate and not _selected_base_gate_active(project_id, root, cfg if isinstance(cfg, dict) else {}):
        active_repo = _read_json(root / "state" / "active_repo.json", {})
        env_selection = _current_environment_selection(root)
        selected_base = env_selection.get("selected", {}) if isinstance(env_selection.get("selected"), dict) else {}
        title = str(cfg.get("title") or cfg.get("topic") or project_id) if isinstance(cfg, dict) else project_id
        venue = _display_venue(cfg.get("target_venue") or cfg.get("venue") or "") if isinstance(cfg, dict) else ""
        repo_path = ""
        route_dataset = ""
        route_ready_datasets = []
        repo_name = ""
        repo_url = ""
        base_title = "环境阶段正在选择当前基底"
        if env_selection.get("valid"):
            base_title = str(selected_base.get("title") or selected_base.get("literature_base_title") or title)
            repo_path = str(selected_base.get("repo_path") or selected_base.get("local_path") or "")
            repo_name = str(selected_base.get("name") or selected_base.get("repo") or selected_base.get("repo_name") or "")
            repo_url = str(selected_base.get("url") or selected_base.get("repo_url") or "")
        read_results = _read_json(root / "planning" / "finding" / "read_results.json", {})
        ideas_results = _read_json(root / "planning" / "finding" / "ideas.json", {})
        plans_results = _read_json(root / "planning" / "finding" / "plans.json", {})
        read_count = _count_value(read_results.get("readings") if isinstance(read_results, dict) else None)
        idea_count = _count_value(ideas_results.get("ideas") if isinstance(ideas_results, dict) else None)
        plan_count = _count_value(plans_results.get("plans") if isinstance(plans_results, dict) else None)
        status = str(full_cycle.get("status") or compact.get("full_status") or compact.get("status") or "not_started")
        if full_cycle_job.get("status") == "running":
            status = "running"
        elif status == "not_started" and (read_count or idea_count or plan_count):
            status = "running_or_ready"
        blocker_title = "等待环境阶段 Claude Code 选择当前基底" if not repo_name and not repo_path else "继续推进当前项目门控"
        blocker_summary = _public_blocker_summary(current_blocker, str(full_cycle.get("summary_zh") or full_cycle.get("summary") or "当前 Find 已产出候选/计划后，必须由环境阶段 Claude Code 选择当前基底并验证 repo/data/protocol；旧 active_repo 不会作为当前主线显示。"))
        next_action = _public_blocker_next_action(current_blocker, str(current_blocker.get("next_action") or compact.get("next_action") or full_cycle.get("current_goal") or "按当前项目主题继续运行完整科研循环，直到 repo、数据、实验和论文门控给出真实状态。"))
        summary_zh = str(full_cycle.get("summary_zh") or full_cycle.get("summary") or f"项目：{title}；投稿目标：{venue or '未设置'}；状态：{status}。当前基底：{base_title}。Find/实验/论文状态来自该项目目录，不继承其他项目或旧 active_repo。")
        return {
            "status": status,
            "target_venue": venue,
            "summary": summary_zh,
            "summary_i18n": {"zh": summary_zh, "en": summary_zh},
            "main_route": {
                "base_title": base_title,
                "base_venue": "",
                "base_year": "",
                "repo_name": repo_name,
                "repo_url": repo_url,
                "repo_path": repo_path,
                "find_run_id": env_selection.get("current_find_run_id", ""),
                "base_selection_status": "selected" if env_selection.get("valid") else "waiting_for_environment_claude_code",
                "readings": read_count,
                "ideas": idea_count,
                "plans": plan_count,
            },
            "blocker": {
                "category": str(current_blocker.get("category") or ""),
                "title": blocker_title,
                "summary": blocker_summary,
                "data_status": "",
                "data_decision": "",
                "loader_probe_status": "",
                "loader_probe_decision": "",
                "reference_protocol_status": "",
                "reference_smoke_status": "",
                "reference_audit_status": "",
                "reference_audit_decision": "",
                "reference_audit_artifact": "",
                "reference_audit_metrics": {},
                "reference_audit_ready": False,
                "reference_full_job_status": "",
                "reference_full_job_pid": "",
                "reference_full_job_log": "",
                "reference_full_job_decision": "",
                "paper_level_reproduction_passed": False,
                "missing_files": [],
                "ready_datasets": [],
                "blocked_datasets": [],
                "next_action": next_action,
            },
            "legacy_control": {
                "policy": "历史仓库、实验和参考复现只保留为审计记录；当前主线以本轮 Find 后的环境审查选择为准。",
                "details_hidden": True,
            },
            "display_policy": {
                "hide_legacy_repo_cards": False,
                "hide_literature_candidate_pool": False,
                "hide_legacy_route_metrics_on_main": True,
            },
            "supervision": {**_empty_supervision_payload(), "full_cycle_job": full_cycle_job},
        }


    repo = fresh_impl.get("repo", {}) if isinstance(fresh_impl, dict) and isinstance(fresh_impl.get("repo"), dict) else {}
    env_selection = _current_environment_selection(root)
    selected_current = env_selection.get("selected", {}) if env_selection.get("valid") and isinstance(env_selection.get("selected"), dict) else {}
    title = str(selected_current.get("title") or selected_current.get("literature_base_title") or selected_current.get("selected_base_title") or selected_current.get("name") or fresh_base.get("title") or find_plan.get("selected_base_title") or cfg.get("title") or cfg.get("topic") or "")
    repo_path = str(selected_current.get("repo_path") or selected_current.get("local_path") or repo.get("repo_path") or repo.get("local_path") or "")
    route_ready_datasets = _fresh_base_ready_datasets_from_evidence(root, selected_current, active_repo, repo, selected_current.get("claim_ready_datasets") or selected_current.get("ready_datasets") or [])
    route_dataset = str(selected_current.get("dataset") or selected_current.get("claim_ready_dataset") or (route_ready_datasets[0] if route_ready_datasets else "") or "")
    required_files = loader_probe.get("required_files_per_dataset") if isinstance(loader_probe, dict) else None
    if not isinstance(required_files, list) or not required_files:
        required_files = data_probe.get("required_files_per_dataset") if isinstance(data_probe, dict) else None
    if not isinstance(required_files, list) or not required_files:
        impl_evidence = fresh_impl.get("implementation_evidence", {}) if isinstance(fresh_impl, dict) and isinstance(fresh_impl.get("implementation_evidence"), dict) else {}
        contract = impl_evidence.get("dataset_contract", {}) if isinstance(impl_evidence.get("dataset_contract"), dict) else {}
        required_files = contract.get("required_files_per_dataset") if isinstance(contract.get("required_files_per_dataset"), list) else []
    if not isinstance(required_files, list):
        required_files = []
    blocked_datasets = loader_probe.get("blocked_datasets") if isinstance(loader_probe, dict) else None
    if not isinstance(blocked_datasets, list) or not blocked_datasets:
        blocked_datasets = fresh_impl.get("blocked_datasets") if isinstance(fresh_impl, dict) else []
    if not isinstance(blocked_datasets, list):
        blocked_datasets = []
    ready_datasets = loader_probe.get("ready_datasets") if isinstance(loader_probe, dict) else []
    if not isinstance(ready_datasets, list):
        ready_datasets = []
    loader_contract_passed = _fresh_base_loader_contract_passed(root)
    if loader_contract_passed and not ready_datasets and isinstance(fresh_impl, dict) and isinstance(fresh_impl.get("ready_datasets"), list):
        ready_datasets = fresh_impl.get("ready_datasets") or []
    read_results = _read_json(root / "planning" / "finding" / "read_results.json", {})
    ideas_results = _read_json(root / "planning" / "finding" / "ideas.json", {})
    plans_results = _read_json(root / "planning" / "finding" / "plans.json", {})
    read_count = (
        _count_value(find_plan.get("current_find_reading_count"))
        or _count_value(find_plan.get("readings"))
        or _count_value(read_results.get("readings") if isinstance(read_results, dict) else None)
        or _count_value(compact.get("readings"))
    )
    idea_count = (
        _count_value(find_plan.get("current_find_idea_count"))
        or _count_value(find_plan.get("ideas"))
        or _count_value(ideas_results.get("ideas") if isinstance(ideas_results, dict) else None)
        or _count_value(compact.get("ideas"))
    )
    plan_count = (
        _count_value(find_plan.get("current_find_plan_count"))
        or _count_value(find_plan.get("plans"))
        or _count_value(plans_results.get("plans") if isinstance(plans_results, dict) else None)
        or _count_value(compact.get("plans"))
    )
    cfg_paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    venue = _display_venue(cfg.get("target_venue") or cfg.get("venue") or cfg_paper.get("target_venue") or paper_state.get("venue") or paper_state.get("active_venue") or paper_meta.get("target_venue") or "")
    status = str(full_cycle.get("status") or compact.get("full_status") or compact.get("status") or "")
    blocker_category = str(current_blocker.get("category") or "")
    protocol_passed = bool(isinstance(protocol_probe, dict) and protocol_probe.get("status") == "reference_protocol_probe_passed" and protocol_probe.get("decision") == "ready_for_bounded_reference_smoke")
    smoke_passed = bool(isinstance(smoke_probe, dict) and smoke_probe.get("status") == "reference_smoke_passed" and smoke_probe.get("decision") == "ready_for_reference_reproduction_audit")
    if loader_contract_passed and status == "blocked_fresh_base_data_required":
        status = "blocked_fresh_base_reference_probe_required"
    if loader_contract_passed and blocker_category == "fresh_base_data_required":
        blocker_category = "fresh_base_reference_probe_required"
    paper_level_reproduction_passed = bool(
        isinstance(reference_audit, dict)
        and reference_audit.get("mode") == "full"
        and reference_audit.get("return_code") == 0
        and reference_audit.get("audit_ready")
        and reference_audit.get("paper_level_reproduction_passed")
    )
    fresh_smoke_blocked = status == "blocked_fresh_base_reference_smoke_required" or blocker_category == "fresh_base_reference_smoke_required" or (protocol_passed and not smoke_passed and loader_contract_passed)
    fresh_reproduction_blocked = not paper_level_reproduction_passed and (status == "blocked_fresh_base_reference_reproduction_required" or blocker_category == "fresh_base_reference_reproduction_required" or smoke_passed)
    fresh_reference_blocked = (status == "blocked_fresh_base_reference_probe_required" or blocker_category == "fresh_base_reference_probe_required") and not protocol_passed
    fresh_data_blocked = (status == "blocked_fresh_base_data_required" or blocker_category == "fresh_base_data_required") and not loader_contract_passed
    legacy_path = str(active_repo.get("repo_path") or active_repo.get("local_path") or "") if isinstance(active_repo, dict) else ""
    legacy_name = str(active_repo.get("name") or active_repo.get("repo") or "legacy/control") if isinstance(active_repo, dict) else "legacy/control"
    base_display = title or repo.get("name") or selected_current.get("name") or selected_current.get("repo") or "当前环境阶段选出的基底"
    reference_smoke_script = "modules/environment/scripts/probe_selected_base_reference.py --mode smoke"
    reference_wrapper_label = f"{base_display} full reference reproduction"
    selected_base_viability_blocked = bool(isinstance(selected_base_viability, dict) and selected_base_viability.get("status") == "blocked" and selected_base_viability.get("decision") in {"base_switch_gate_required", "continue_experiment_evidence_repair"})
    selected_base_viability_public = _selected_base_viability_public_blocker(selected_base_viability, base_display, base_switch_gate)
    if selected_base_viability_blocked:
        blocker_title = selected_base_viability_public.get("title") or "缺少当前主线候选实验证据"
        blocker_summary = selected_base_viability_public.get("summary") or _public_blocker_summary(selected_base_viability, f"{base_display} 参考复现已通过；当前还没有可写入论文的审计就绪候选实验。旧路线只作为历史对照，不是当前参考工作。")
        next_action = selected_base_viability_public.get("next_action") or f"保持 {base_display} 为当前基底；由 project agent 在同一数据和评测协议下设计、运行并审计真实候选实验。没有审计就绪的提升证据前，不进入论文或结论提升。"
        summary_zh = (
            f"主线：{base_display}；投稿目标：{venue}；参考复现已通过。"
            f" 当前阻塞是{blocker_title}；论文和结论提升暂停。"
            f" 当前 Find 已精读 {read_count} 篇、形成 {idea_count} 个 idea 和 {plan_count} 个 plan。"
        )
    elif fresh_reproduction_blocked:
        audit_ready = bool(isinstance(reference_audit, dict) and reference_audit.get("return_code") == 0 and reference_audit.get("audit_ready"))
        audit_metrics = reference_audit.get("metrics", {}) if isinstance(reference_audit, dict) and isinstance(reference_audit.get("metrics"), dict) else {}
        blocker_title = f"{base_display} 论文级 full reference reproduction 未完成" if audit_ready else f"{base_display} reference reproduction audit 未完成"
        full_job_running = bool(isinstance(reference_full_job, dict) and reference_full_job.get("status") == "running" and not paper_level_reproduction_passed)
        full_job_note = f" 当前 full reference reproduction 已由 wrapper 启动，PID={reference_full_job.get('pid')}。" if full_job_running else ""
        blocker_summary = (
            f"{base_display} bounded reference audit 已由 wrapper 跑通并记录指标；TASTE 正在推进论文级 full reference reproduction。未完成前不写论文、不提升结论。" + full_job_note
            if audit_ready
            else f"{base_display} 有界 reference smoke 已通过；当前阻塞在论文级参考复现审计，未通过前不写论文、不提升结论。"
        )
        next_action = (
            "监督 wrapper 的 full reference reproduction；若失败，记录算力/协议原因并回到 fresh-base 数据/工作选择；禁止历史路线回主线。"
            if full_job_running else
            "继续由 wrapper 运行论文级 full reference reproduction，或在 full reproduction 不可行时记录算力/协议原因并回到 fresh-base 数据/工作选择；禁止历史路线回主线。"
            if audit_ready
            else f"运行受审计的 {base_display} reference reproduction wrapper，记录 command/config/log/hash/runtime/metrics；通过前不进入论文或结论提升。"
        )
        summary_zh = (
            f"主线：{base_display}；投稿目标：{venue}；状态：阻塞在当前基底论文级 full reference reproduction。"
            f" 当前 Find 已精读 {read_count} 篇、形成 {idea_count} 个 idea 和 {plan_count} 个 plan。"
            f" 已通过 bounded smoke 的数据集：{smoke_probe.get('selected_dataset') if isinstance(smoke_probe, dict) else ''}。"
            + (f" bounded audit 指标：NDCG@10={audit_metrics.get('ndcg_at_10')}。" if audit_ready else "")
            + (f" full reproduction 正在运行：PID={reference_full_job.get('pid')}。" if isinstance(reference_full_job, dict) and reference_full_job.get("status") == "running" else "")
            + " 旧路线仅保留为内部历史对照，不是当前主线。"
        )
    elif fresh_smoke_blocked:
        blocker_title = f"{base_display} 有界 reference smoke 未完成"
        blocker_summary = f"{base_display} 参考协议/环境 manifest 只读探针已通过；当前只阻塞在有界 no-training reference smoke/audit。"
        next_action = f"自动运行 {reference_smoke_script}，只做真实数据加载、模型初始化和 no-grad 前向探针；通过前不训练、不写论文、不提升结论。"
        summary_zh = (
            f"主线：{base_display}；投稿目标：{venue}；状态：阻塞在当前基底有界 reference smoke。"
            f" 当前 Find 已精读 {read_count} 篇、形成 {idea_count} 个 idea 和 {plan_count} 个 plan。"
            f" 已通过数据/loader 的数据集：{', '.join(str(x) for x in ready_datasets) or '无'}。"
            " 旧路线仅保留为内部历史对照，不是当前主线。"
        )
    elif fresh_reference_blocked:
        blocker_title = f"{base_display} 参考协议/环境 manifest 未完成"
        protocol_blocker_summary = _reference_protocol_probe_blocker_summary(protocol_probe, base_display)
        blocker_summary = protocol_blocker_summary or f"{base_display} 数据和 loader/import probe 已通过；当前只阻塞在最小环境 manifest、参考协议只读探针和 reference reproduction 审计。"
        next_action = "使用当前配置的实验环境补齐缺失依赖后重新运行 reference-protocol/import probe；通过前不训练、不写论文、不提升结论。" if protocol_blocker_summary else f"记录 {base_display} 最小环境 manifest，并对 ready 数据集运行有界只读 reference-protocol/import probe；通过前不训练、不写论文、不提升结论。"
        summary_zh = (
            f"主线：{base_display}；投稿目标：{venue}；状态：阻塞在当前基底参考协议/环境 manifest 只读探针。"
            f" 当前 Find 已精读 {read_count} 篇、形成 {idea_count} 个 idea 和 {plan_count} 个 plan。"
            f" 已通过数据/loader 的数据集：{', '.join(str(x) for x in ready_datasets) or '无'}。"
            " 旧路线仅保留为内部历史对照，不是当前主线。"
        )
    elif fresh_data_blocked:
        blocker_title = f"{base_display} 数据/loader 合同未满足"
        blocker_summary = f"缺少 {base_display} loader-ready 真实数据文件；在真实数据和 loader/import probe 通过前，系统不会训练、写论文或提升结论。"
        next_action = f"获取 {base_display} 所需官方/合法数据，放置到 repo 期望的数据目录，并运行只读 loader/import probe。"
        summary_zh = (
            f"主线：{base_display}；投稿目标：{venue}；状态：阻塞在当前基底数据/loader 合同。"
            f" 已使用当前 Find 精读 {read_count} 篇、形成 {idea_count} 个 idea 和 {plan_count} 个 plan。"
            " 旧路线仅保留为内部历史对照，不是当前主线。"
        )
    else:
        blocker_title = "主线仍需处理阻塞"
        blocker_summary = _public_blocker_summary(current_blocker, str(full_cycle.get("current_goal") or ""))
        next_action = _public_blocker_next_action(current_blocker, str(current_blocker.get("next_action") or compact.get("next_action") or "继续处理当前最高优先级阻塞。"))
        summary_zh = str(full_cycle.get("summary_zh") or full_cycle.get("summary") or "")
    return {
        "status": status or "not_started",
        "target_venue": venue,
        "summary": summary_zh,
        "summary_i18n": {"zh": summary_zh, "en": summary_zh},
        "main_route": {
            "base_title": title,
            "base_venue": fresh_base.get("venue") or find_plan.get("selected_base_venue") or "",
            "base_year": fresh_base.get("year") or find_plan.get("selected_base_year") or "",
            "repo_name": repo.get("name") or selected_current.get("name") or selected_current.get("repo") or "",
            "repo_url": repo.get("url") or selected_current.get("url") or selected_current.get("repo_url") or "",
            "repo_path": repo_path,
            "dataset": route_dataset,
            "ready_datasets": route_ready_datasets[:8],
            "find_run_id": find_plan.get("run_id") or find_plan.get("find_run_id") or env_selection.get("current_find_run_id") or "",
            "readings": completed_read_count,
            "read_artifacts": raw_read_count,
            "raw_reading_count": raw_read_count,
            "full_text_reading_count": full_text_read_count,
            "pending_full_text_reading_count": pending_full_text_read_count,
            "ideas": idea_count,
            "plans": plan_count,
        },
        "blocker": {
            "category": selected_base_viability_public.get("category", "experiment_evidence_audit") if selected_base_viability_blocked else "fresh_base_reference_reproduction_required" if fresh_reproduction_blocked else "fresh_base_reference_smoke_required" if fresh_smoke_blocked else "fresh_base_reference_probe_required" if fresh_reference_blocked else "fresh_base_data_required" if fresh_data_blocked else _public_internal_names(blocker_category),
            "title": blocker_title,
            "summary": blocker_summary,
            "data_status": data_probe.get("status") if isinstance(data_probe, dict) else "",
            "data_decision": data_probe.get("decision") if isinstance(data_probe, dict) else "",
            "loader_probe_status": loader_probe.get("status") if isinstance(loader_probe, dict) else "",
            "loader_probe_decision": loader_probe.get("decision") if isinstance(loader_probe, dict) else "",
            "reference_protocol_status": protocol_probe.get("status") if isinstance(protocol_probe, dict) else "",
            "reference_smoke_status": smoke_probe.get("status") if isinstance(smoke_probe, dict) else "",
            "reference_audit_status": reference_audit.get("status") if isinstance(reference_audit, dict) else "",
            "reference_audit_decision": reference_audit.get("decision") if isinstance(reference_audit, dict) else "",
            "reference_audit_artifact": reference_audit.get("artifact_dir") if isinstance(reference_audit, dict) else "",
            "reference_audit_metrics": reference_audit.get("metrics") if isinstance(reference_audit, dict) else {},
            "reference_audit_ready": bool(isinstance(reference_audit, dict) and reference_audit.get("audit_ready")),
            "reference_full_job_status": "completed" if paper_level_reproduction_passed else (reference_full_job.get("status") if isinstance(reference_full_job, dict) else ""),
            "reference_full_job_pid": reference_full_job.get("pid") if isinstance(reference_full_job, dict) else "",
            "reference_full_job_log": reference_full_job.get("log_path") if isinstance(reference_full_job, dict) else "",
            "reference_full_job_decision": "ready_for_full_research_cycle" if paper_level_reproduction_passed else (reference_full_job.get("decision") if isinstance(reference_full_job, dict) else ""),
            "paper_level_reproduction_passed": paper_level_reproduction_passed,
            "missing_files": [] if loader_contract_passed else required_files,
            "ready_datasets": route_ready_datasets[:8] or ready_datasets,
            "blocked_datasets": blocked_datasets,
            "next_action": next_action,
        },
        "legacy_control": {
            "policy": "旧路线仅作为内部历史对照；不得作为当前主线训练、实验或论文证据。",
            "details_hidden": True,
        },
        "display_policy": {
            "hide_legacy_repo_cards": True,
            "hide_literature_candidate_pool": True,
            "hide_legacy_route_metrics_on_main": True,
        },
        "supervision": {
            "status": ("stale_full_research_cycle_snapshot" if full_cycle_job.get("status") == "stale" and "running" in str(supervision_tick.get("status", "")) else supervision_tick.get("status", "")) if isinstance(supervision_tick, dict) else "",
            "action": supervision_tick.get("action", "") if isinstance(supervision_tick, dict) else "",
            "action_rc": supervision_tick.get("action_rc", "") if isinstance(supervision_tick, dict) else "",
            "generated_at": supervision_tick.get("generated_at", "") if isinstance(supervision_tick, dict) else "",
            "issue_count": len(supervision_tick.get("issues", [])) if isinstance(supervision_tick, dict) and isinstance(supervision_tick.get("issues"), list) else 0,
            "observation_count": len(supervision_tick.get("observations", [])) if isinstance(supervision_tick, dict) and isinstance(supervision_tick.get("observations"), list) else 0,
            "observations": supervision_tick.get("observations", [])[:5] if isinstance(supervision_tick, dict) and isinstance(supervision_tick.get("observations"), list) else [],
            "repairs": supervision_tick.get("repairs", [])[:5] if isinstance(supervision_tick, dict) and isinstance(supervision_tick.get("repairs"), list) else [],
            "next_action": _clean_stale_active_worker_text(supervision_tick.get("next_action", next_action), next_action or "上一条完整科研循环已结束；当前未检测到活进程。可以通过统一 TASTE 入口启动下一轮完整科研流程。") if isinstance(supervision_tick, dict) and full_cycle_job.get("status") == "stale" else (supervision_tick.get("next_action", next_action) if isinstance(supervision_tick, dict) else next_action),
            "full_reference_job": reference_full_job if isinstance(reference_full_job, dict) and reference_full_job else (supervision_tick.get("full_reference_job", {}) if isinstance(supervision_tick, dict) and isinstance(supervision_tick.get("full_reference_job"), dict) else {}),
            "full_cycle_job": full_cycle_job,
            "api": supervision_tick.get("api", {}) if isinstance(supervision_tick, dict) and isinstance(supervision_tick.get("api"), dict) else {},
        },
    }



def _compact_action_rows(rows: Any, limit: int = 4) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for raw_row in rows[:limit]:
        if not isinstance(raw_row, dict):
            continue
        row = _public_blocker_row(raw_row)
        item: dict[str, Any] = {}
        for key in ["id", "category", "route", "priority", "severity", "source", "source_check_id", "autonomy"]:
            value = row.get(key)
            if value not in (None, "", []):
                item[key] = value
        for key in ["issue", "summary", "human_summary", "repair_strategy", "next_action"]:
            value = row.get(key)
            if value not in (None, "", []):
                text = " ".join(str(value).split())
                item[key] = text[:320] + ("..." if len(text) > 320 else "")
        # Raw machine audit text remains available in state/report artifacts;
        # compact project polling should stay human-readable.
        commands = row.get("recommended_commands")
        if isinstance(commands, list):
            item["recommended_commands"] = [str(command) for command in commands[:3]]
        out.append(item)
    return out


def _compact_project_summary(summary: dict[str, Any]) -> dict[str, Any]:
    project_id = str(summary.get("id") or summary.get("name") or summary.get("project") or "")
    project_root = Path(summary.get("path")) if summary.get("path") else PROJECTS / project_id
    cfg = dict(summary.get("config", {})) if isinstance(summary.get("config"), dict) else {}

    def scalmap(value: Any, keys: list[str]) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        return {key: src.get(key, "") for key in keys if isinstance(src.get(key, ""), (str, int, float, bool)) or src.get(key) is None}

    def gate_summary(value: Any) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        out = scalmap(src, ["status", "decision", "human_summary", "summary", "reason", "current_goal", "paper_status"])

        def compact_gate_row(row: Any) -> dict[str, Any]:
            if not isinstance(row, dict):
                return {}
            compact = scalmap(row, [
                "timestamp", "experiment_id", "name", "method", "dataset",
                "metric_name", "metric_value", "audit_ready", "artifact_path", "audit_path",
                "source", "paper_level", "target_value",
            ])
            metrics = row.get("metrics")
            if isinstance(metrics, dict):
                compact["metrics"] = {str(k): v for k, v in list(metrics.items())[:8] if isinstance(v, (str, int, float, bool)) or v is None}
            return compact

        blockers = src.get("blockers") or src.get("issues") or []
        if isinstance(blockers, list):
            public_blockers: list[str] = []
            for item in blockers[:3]:
                public = _public_text_for_gate(item)
                if public and public not in public_blockers:
                    public_blockers.append(public)
            out["blockers"] = public_blockers
        warnings = src.get("warnings")
        if isinstance(warnings, list):
            out["warnings"] = warnings[:3]
        checks = src.get("checks")
        if isinstance(checks, list):
            out["checks"] = [compact_gate_row(row) or scalmap(row, ["step", "id", "name", "status", "detail"]) for row in checks[:12] if isinstance(row, dict)]
        comparisons = src.get("comparisons")
        if isinstance(comparisons, list):
            out["comparisons"] = [
                {
                    "target": compact_gate_row(row.get("target")) if isinstance(row, dict) else {},
                    "best_reproduction": compact_gate_row(row.get("best_reproduction")) if isinstance(row, dict) else {},
                    "status": row.get("status", "") if isinstance(row, dict) else "",
                }
                for row in comparisons[:4]
                if isinstance(row, dict)
            ]
        for key in ["best_candidate", "best_control", "best_audit_ready_control", "best_reproduction"]:
            if isinstance(src.get(key), dict):
                out[key] = compact_gate_row(src[key])
        compute = src.get("compute_feasibility")
        if isinstance(compute, dict):
            compact_compute = scalmap(compute, ["status", "estimated_full_reproduction_hours", "max_full_reproduction_hours", "min_iteration_budget"])
            machine = compute.get("machine")
            if isinstance(machine, dict):
                compact_compute["machine"] = scalmap(machine, ["gpu_count", "profile_exists"])
            out["compute_feasibility"] = compact_compute
        public_summary = _public_text_for_gate(src)
        if public_summary:
            out["human_summary"] = public_summary
            out["summary"] = public_summary
            out["reason"] = public_summary
        return out

    def agent_summary(value: Any) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        main = src.get("main") if isinstance(src.get("main"), dict) else {}
        agents = [row for row in (src.get("agents") if isinstance(src.get("agents"), list) else []) if isinstance(row, dict)]

        def compact_agent(row: Any) -> dict[str, Any]:
            compact = scalmap(row if isinstance(row, dict) else {}, [
                "id", "name", "role", "stage", "status", "goal", "current_step",
                "process_alive", "pid", "ppid", "elapsed", "kind", "cmd", "log_path",
            ])
            queued = row.get("queued_guidance") if isinstance(row, dict) else None
            if isinstance(queued, list):
                compact["queued_guidance"] = queued[:5]
            tail = row.get("log_tail") if isinstance(row, dict) else None
            if isinstance(tail, list):
                compact["log_tail"] = tail[-8:]
            return compact

        process_names = {
            "full_cycle": ("完整科研自循环", "full-cycle"),
            "paper_pipeline": ("论文流水线", "paper-pipeline"),
            "paper_orchestra": ("writing 模块", "writing"),
            "claude_session": ("Claude 论文/科研会话", "claude-session"),
            "claude_cli": ("Claude Code 执行器", "claude-cli"),
            "frontend": ("finding 文献刷新入口", "find-refresh"),
            "driver": ("finding/read/idea/plan", "find-driver"),
            "experiment_or_reproduction": ("实验/复现训练", "experiment-training"),
        }
        process_agents: list[dict[str, Any]] = []
        for row in _remote_process_rows():
            if not isinstance(row, dict):
                continue
            cmd = str(row.get("cmd") or "")
            kind = str(row.get("kind") or "")
            if kind not in process_names:
                continue
            if project_id and project_id not in cmd and str(project_root) not in cmd:
                continue
            name, stage = process_names[kind]
            pid = str(row.get("pid") or "")
            process_agents.append({
                "id": f"process-{kind}-{pid}",
                "name": name,
                "role": "main" if kind == "full_cycle" else "worker",
                "stage": stage,
                "status": "running",
                "goal": "reflect live process state from the remote host",
                "current_step": "{}: pid {}, elapsed {}".format(stage, pid, row.get("elapsed") or "").strip(),
                "process_alive": True,
                "pid": pid,
                "ppid": row.get("ppid", ""),
                "elapsed": row.get("elapsed", ""),
                "kind": kind,
                "cmd": cmd,
            })
        full_cycle_agent = next((row for row in process_agents if row.get("kind") == "full_cycle"), None)
        primary_live_agent = full_cycle_agent or (process_agents[0] if process_agents else None)
        if primary_live_agent and (not main or str(main.get("status") or "") in {"", "idle", "done", "completed"}):
            main_name = "完整科研自循环" if primary_live_agent.get("kind") == "full_cycle" else primary_live_agent.get("name", "实时任务")
            main = {**main, **primary_live_agent, "id": "main", "name": main_name, "role": "main"}
        seen: set[str] = set()
        merged_agents: list[dict[str, Any]] = []
        for row in [main, *agents, *process_agents]:
            if not isinstance(row, dict) or not row:
                continue
            key = str(row.get("id") or row.get("pid") or len(merged_agents))
            if key in seen:
                continue
            seen.add(key)
            merged_agents.append(row)
        running_agents = [row for row in merged_agents if row.get("status") in {"queued", "running", "cancelling"}]
        main_out = compact_agent(main if main else (running_agents[0] if running_agents else {}))
        return {
            "main": main_out,
            "agents": [compact_agent(row) for row in merged_agents[:12]],
            "running": [compact_agent(row) for row in running_agents[:8]],
        }

    def compact_experiment_record(value: Any) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        rows = src.get("rows") if isinstance(src.get("rows"), list) else []
        return {
            "updated_at": src.get("updated_at", ""),
            "row_count": src.get("row_count", len(rows)),
            "columns": src.get("columns", []) if isinstance(src.get("columns", []), list) else [],
            "rows": rows[-12:],
            "csv_path": src.get("csv_path", ""),
            "csv_url": src.get("csv_url", ""),
            "report_path": src.get("report_path", ""),
            "report_url": src.get("report_url", ""),
            "json_path": src.get("json_path", ""),
            "json_url": src.get("json_url", ""),
            "source": src.get("source", ""),
            "refresh_error": src.get("refresh_error", ""),
        }

    def compact_paper_stage(value: Any) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        scalkeys = [
            "status", "summary", "summary_zh", "summary_en", "venue",
            "paper_generation_skipped", "paper_generation_skipped_reason",
            "paper_normality_status", "paper_venue_format_status", "paper_figure_quality_status",
            "paper_citation_render_status", "paper_citation_render_ready", "paper_self_review_status", "paper_self_review_ready",
            "paper_self_review_evidence_blocker_count", "paper_self_review_preview_only_ready", "paper_self_review_submission_evidence_ready",
            "pdf_ready", "pdf_path", "pdf_url", "tex_path", "tex_url",
            "blocked_pdf_path", "blocked_pdf_url", "blocked_tex_path", "blocked_tex_url",
            "blocked_preview_available", "latest_generated_pdf_path", "latest_generated_pdf_url",
            "latest_generated_tex_path", "latest_generated_tex_url", "latest_generated_is_accepted_preview",
            "conference_preview_ready", "conference_preview_pages", "conference_preview_body_pages",
            "conference_preview_reference_pages", "conference_preview_body_page_limit", "normal_preview_ready", "paper_normality_pages",
            "paper_normality_body_pages", "paper_normality_estimated_reference_pages",
            "paper_normality_citation_count", "venue_submission_policy_status",
            "conference_preview_blocker_summary", "paper_layout_summary",
            "template_fetched", "venue_requirements_status", "venue_requirements_path", "venue_requirements_public_summary",
            "paper_figure_quality_ready", "paper_figure_count", "paper_figure_blocker_count",
            "paper_figure_warning_count", "paper_table_count", "paper_figure_repair_loop_status",
            "paper_figure_repair_rounds", "paper_preview_repair_loop_status", "paper_preview_repair_rounds", "paper_self_review_receipt",
            "paper_self_review_evidence_blockers", "paper_self_review_independent_findings_count", "paper_self_review_repairs_count",
            "paper_stage_status", "writing_status", "writing_workspace",
            "raw_pdf_path", "raw_pdf_url", "raw_tex_path", "raw_tex_url",
        ]
        out = scalmap(src, scalkeys)
        if not bool(src.get("conference_preview_ready")):
            out["paper_preview_repair_loop_status"] = "blocked"
        for key in ["venue_submission_policy", "venue_requirements_summary", "latest_generated_pdf_info"]:
            if isinstance(src.get(key), dict):
                out[key] = src[key]
        for key in ["science_gate_preflight_blockers", "paper_layout_footprint_warnings", "paper_figure_failed", "paper_table_failed", "venue_desk_reject_risks"]:
            if isinstance(src.get(key), list):
                out[key] = src[key][:10]
        # Low-level repair diagnostics are for the project agent/Claude Code artifacts.
        # The web-facing projection keeps statuses and counts, but not machine blocker rows.
        out["paper_citation_render_blockers"] = []
        out["paper_self_review_blockers"] = []
        out["paper_self_review_evidence_blockers"] = []
        out["conference_preview_blockers"] = []
        return out

    def runtime_summary(value: Any) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        runtime = src.get("runtime") if isinstance(src.get("runtime"), dict) else {}
        checks_src = src.get("checks") if isinstance(src.get("checks"), dict) else {}
        checks: dict[str, Any] = {}
        for name, check in checks_src.items():
            if isinstance(check, dict):
                checks[str(name)] = scalmap(check, ["path", "ok", "version", "reason"])
        required = ["node", "npm", "claude", "python", "conda", "conda_base"]
        status = str(src.get("status") or "")
        if not status and checks_src:
            status = "ready" if all(bool((checks_src.get(name) or {}).get("ok")) for name in required if name in checks_src) else "needs_attention"
        runtime_public = scalmap(runtime, ["source_bashrc", "bashrc_path", "node_bin", "claude_path", "conda_base", "python_executable"])
        return {
            "project": src.get("project", project_id),
            "status": status,
            "runtime": runtime_public,
            "checks": checks,
            "path_head": [str(item) for item in (src.get("path_head") if isinstance(src.get("path_head"), list) else [])[:12]],
            "python_executable": str(runtime.get("python_executable") or src.get("python_executable") or ""),
            "conda_env": str(cfg.get("conda_env") or src.get("conda_env") or ""),
        }

    def claude_summary(value: Any) -> dict[str, Any]:
        src = value if isinstance(value, dict) else {}
        session = src.get("session") if isinstance(src.get("session"), dict) else {}
        last_result = src.get("last_result") if isinstance(src.get("last_result"), dict) else {}
        current_repo = _current_impl_repo_path(project_root)
        session_repo = str(session.get("repo_path") or "").strip() if isinstance(session, dict) else ""
        stale_session = bool(current_repo and session_repo and session_repo != current_repo)
        public_session = scalmap(session, ["status", "stage", "pid", "started_at", "finished_at", "repo_path", "session_id"])
        if stale_session:
            # Keep the old session file for audit, but do not present a legacy
            # route session as the current project Claude state.
            public_session = {
                "status": "historical",
                "stale_reason": "previous Claude session belongs to a legacy/control route",
            }
        latest_receipt = _public_claude_receipt(_latest_claude_receipt(project_root, session, last_result))
        latest_receipt_by_stage = _public_claude_receipts_by_stage(project_root)
        if stale_session and _claude_receipt_contradicts_current_route(project_root, latest_receipt):
            latest_receipt = {}
        return {
            "status": "historical" if stale_session and not latest_receipt else src.get("status", ""),
            "session": public_session,
            "last_result": scalmap(last_result, ["status", "stage", "return_code", "backend", "started_at", "finished_at", "session_id"]),
            "latest_receipt": latest_receipt,
            "latest_receipt_by_stage": latest_receipt_by_stage,
        }

    raw_stages = summary.get("stages", {}) if isinstance(summary.get("stages"), dict) else {}
    env_raw = raw_stages.get("environment", {}) if isinstance(raw_stages.get("environment"), dict) else {}
    exp_raw = raw_stages.get("experiment", {}) if isinstance(raw_stages.get("experiment"), dict) else {}
    paper_raw = raw_stages.get("paper", {}) if isinstance(raw_stages.get("paper"), dict) else {}
    raw_full = summary.get("full_research_cycle", {}) if isinstance(summary.get("full_research_cycle"), dict) else {}
    active_repo_state = _read_json(project_root / "state" / "active_repo.json", {})
    env_selection_for_stage = _current_environment_selection(project_root)
    if env_selection_for_stage.get("valid") and isinstance(active_repo_state, dict) and str(active_repo_state.get("role") or "") == "main_fresh_base":
        env_raw = dict(env_raw)
        repo_path = str(active_repo_state.get("repo_path") or active_repo_state.get("local_path") or "")
        env_raw["repo_path"] = repo_path
        env_raw["active_repo"] = {
            "name": active_repo_state.get("name") or active_repo_state.get("repo") or "",
            "repo": active_repo_state.get("url") or active_repo_state.get("repo_url") or active_repo_state.get("repo") or "",
            "repo_path": repo_path,
            "local_path": repo_path,
        }
    blocker_action_plan = raw_full.get("blocker_action_plan", {}) if isinstance(raw_full.get("blocker_action_plan"), dict) else {}
    blocker_action_summary = blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan.get("summary"), dict) else {}
    live_worker_for_project = bool(_live_full_cycle_process(project_root, project_id) or _active_project_worker_row(project_id, project_root))
    blocker_actions = _compact_action_rows(blocker_action_plan.get("actions"), 4)
    latest_blockers = _compact_action_rows(raw_full.get("latest_blockers"), 4)
    if not live_worker_for_project:
        blocker_actions = [row for row in blocker_actions if not _is_active_full_cycle_worker_marker(row)]
        latest_blockers = [row for row in latest_blockers if not _is_active_full_cycle_worker_marker(row)]
        if str(blocker_action_summary.get("top_route") or "") == "active_full_research_cycle_worker":
            blocker_action_summary = {**blocker_action_summary, "top_route": "", "top_action": ""}
    blockers = latest_blockers or blocker_actions[:3]
    full_current_blocker = raw_full.get("current_blocker") if isinstance(raw_full.get("current_blocker"), dict) else {}
    if isinstance(full_current_blocker, dict) and str(full_current_blocker.get("category") or "").startswith("fresh_base_"):
        current = _compact_action_rows([full_current_blocker], 1)
        if current:
            blockers = current + [row for row in blockers if row.get("category") != current[0].get("category")]

    full_cycle = scalmap(raw_full, ["status", "summary", "summary_zh", "summary_en", "current_goal", "latest_step", "continuation_required", "continuation_reason", "updated_at", "started_at", "finished_at", "data_status", "data_decision", "loader_status", "loader_decision"])
    if str(full_cycle.get("status") or "").lower() == "running":
        full_cycle.pop("finished_at", None)
        full_cycle.pop("completed_at", None)
    fresh_impl = raw_full.get("fresh_base_implementation_plan") if isinstance(raw_full.get("fresh_base_implementation_plan"), dict) else {}
    full_cycle["fresh_base_implementation_plan"] = scalmap(fresh_impl, ["status", "selected_base_title", "reason"])
    full_cycle["blocker_action_plan"] = {"summary": blocker_action_summary, "actions": blocker_actions}
    full_cycle["latest_blockers"] = blockers

    run_preferences = _public_run_preferences(project_id, project_root, cfg)
    cfg_public = _public_project_identity_config(project_id, cfg)
    stages = {
        "environment": {
            **scalmap(env_raw, ["status", "summary", "summary_zh", "summary_en", "locked", "repo_path", "block_reason"]),
            "active_repo": scalmap(env_raw.get("active_repo") if isinstance(env_raw.get("active_repo"), dict) else {}, ["name", "repo", "repo_path", "local_path"]),
            "pending_candidate": scalmap(env_raw.get("pending_candidate") if isinstance(env_raw.get("pending_candidate"), dict) else {}, ["name", "title", "url", "repo_path", "status", "selection_gate"]),
        },
        "experiment": {
            **scalmap(exp_raw, ["status", "summary", "summary_zh", "summary_en", "last_backend"]),
            "experiment_count": exp_raw.get("experiment_count", len(exp_raw.get("experiments")) if isinstance(exp_raw.get("experiments"), list) else 0),
            "completed_experiment_count": exp_raw.get("completed_experiment_count", len([row for row in exp_raw.get("experiments", []) if isinstance(row, dict) and str(row.get("status", "")).lower() in {"completed", "success", "repaired"}]) if isinstance(exp_raw.get("experiments"), list) else 0),
            "show_experiment_summary_count": bool(exp_raw.get("show_experiment_summary_count", False)),
            "show_synthetic_smoke_warning": bool(exp_raw.get("show_synthetic_smoke_warning", False)),
            "audit_ready_completed_experiment_count": exp_raw.get("audit_ready_completed_experiment_count", 0),
            "running_experiment_count": exp_raw.get("running_experiment_count", 0),
            "recent_experiments": _compact_rows(exp_raw.get("experiments"), 12),
            "experiments": _compact_rows(exp_raw.get("experiments"), 12),
            "experiment_record": compact_experiment_record(exp_raw.get("experiment_record")),
            "reference_reproduction_gate": gate_summary(exp_raw.get("reference_reproduction_gate")),
            "scientific_progress_gate": gate_summary(exp_raw.get("scientific_progress_gate")),
            "experiment_iteration_audit": gate_summary(exp_raw.get("experiment_iteration_audit")),
            "full_research_cycle": full_cycle,
        },
        "paper": compact_paper_stage(paper_raw),
    }

    out = {
        "project": project_id,
        "config": cfg_public,
        "run_preferences": run_preferences,
        "path": str(project_root),
        "stages": stages,
        "full_research_cycle": full_cycle,
        "blockers": blockers,
        "blocker_count": len(blockers),
        "current_blocker": blockers[0] if blockers else {},
        "next_actions": blocker_actions,
        "next_action": blocker_actions[0].get("issue", "") if blocker_actions else str(full_cycle.get("current_goal") or full_cycle.get("continuation_reason") or ""),
        "blocker_action_plan_summary": blocker_action_summary,
        "runtime": runtime_summary(summary.get("runtime")),
        "agent_state": agent_summary(summary.get("agent_state")),
        "claude_status": claude_summary(summary.get("claude_status")),
        "artifacts": [],
        "compact": True,
    }
    out["status"] = full_cycle.get("status", "")
    out["paper_status"] = stages["paper"].get("status", "")
    out["paper_summary"] = stages["paper"].get("summary", "")
    out["paper_generation_skipped"] = stages["paper"].get("paper_generation_skipped", False)
    out["latest_generated_pdf_url"] = stages["paper"].get("latest_generated_pdf_url", "")
    out["blocked_pdf_url"] = stages["paper"].get("blocked_pdf_url", "")
    out["human_supervision"] = _human_supervision_summary(project_root, out, {"config": cfg_public, "path": str(project_root), "project": project_id})
    human = out["human_supervision"] if isinstance(out.get("human_supervision"), dict) else {}
    main_route = human.get("main_route", {}) if isinstance(human.get("main_route"), dict) else {}
    blocker = human.get("blocker", {}) if isinstance(human.get("blocker"), dict) else {}
    fresh_blocker_category = str(blocker.get("category") or "")
    fresh_reproduction_blocked = fresh_blocker_category == "fresh_base_reference_reproduction_required" or human.get("status") == "blocked_fresh_base_reference_reproduction_required"
    fresh_smoke_blocked = fresh_blocker_category == "fresh_base_reference_smoke_required" or human.get("status") == "blocked_fresh_base_reference_smoke_required"
    fresh_reference_blocked = fresh_blocker_category == "fresh_base_reference_probe_required" or human.get("status") == "blocked_fresh_base_reference_probe_required"
    fresh_data_blocked = fresh_blocker_category == "fresh_base_data_required" or human.get("status") == "blocked_fresh_base_data_required"
    if (fresh_data_blocked or fresh_reference_blocked or fresh_smoke_blocked or fresh_reproduction_blocked):
        selected_base_display = str(main_route.get("base_title") or "当前基底")
        concise_status = "blocked_fresh_base_reference_reproduction_required" if fresh_reproduction_blocked else "blocked_fresh_base_reference_smoke_required" if fresh_smoke_blocked else "blocked_fresh_base_reference_probe_required" if fresh_reference_blocked else "blocked_fresh_base_data_required"
        concise_reason = "fresh base reference smoke passed; paper-level reference reproduction required" if fresh_reproduction_blocked else "fresh base reference protocol passed; bounded reference smoke required" if fresh_smoke_blocked else "fresh base data/loader ready; reference protocol/env manifest probe required" if fresh_reference_blocked else f"resolve {selected_base_display} data files and loader contract"
        concise_gate_decision = "fresh_base_reference_reproduction_required" if fresh_reproduction_blocked else "fresh_base_reference_smoke_required" if fresh_smoke_blocked else "fresh_base_reference_probe_required" if fresh_reference_blocked else "fresh_base_data_required"
        concise_full_cycle = {
            "status": human.get("status") or concise_status,
            "summary": human.get("summary", ""),
            "summary_zh": human.get("summary", ""),
            "summary_en": human.get("summary_i18n", {}).get("en", human.get("summary", "")) if isinstance(human.get("summary_i18n"), dict) else human.get("summary", ""),
            "current_goal": blocker.get("next_action", ""),
            "continuation_required": True,
            "continuation_reason": concise_reason,
            "fresh_base_implementation_plan": {
                "status": human.get("status") or concise_status,
                "selected_base_title": main_route.get("base_title", ""),
                "reason": blocker.get("summary", ""),
            },
            "blocker_action_plan": {
                "summary": blocker_action_summary,
                "actions": blocker_actions,
            },
            "latest_blockers": [{
                "category": blocker.get("category", "fresh_base_data_required"),
                "severity": "block",
                "issue": blocker.get("summary", ""),
                "next_action": blocker.get("next_action", ""),
            }],
        }
        full_cycle = concise_full_cycle
        stages = {
            "environment": {
                "status": "blocked",
                "summary": human.get("summary", ""),
                "summary_zh": human.get("summary", ""),
                "summary_en": concise_full_cycle.get("summary_en", ""),
                "locked": True,
                "repo_path": main_route.get("repo_path", ""),
                "block_reason": blocker.get("summary", ""),
                "active_repo": {
                    "name": main_route.get("repo_name", ""),
                    "repo": main_route.get("repo_url", ""),
                    "repo_path": main_route.get("repo_path", ""),
                    "local_path": main_route.get("repo_path", ""),
                },
            },
            "experiment": {
                "status": "blocked",
                "summary": human.get("summary", ""),
                "summary_zh": human.get("summary", ""),
                "summary_en": concise_full_cycle.get("summary_en", ""),
                "last_backend": exp_raw.get("last_backend", "") if isinstance(exp_raw, dict) else "",
                "reference_reproduction_gate": {
                    "status": "blocked",
                    "decision": concise_gate_decision,
                    "human_summary": blocker.get("summary", ""),
                    "blockers": [blocker.get("summary", "")],
                },
                "scientific_progress_gate": {"status": "blocked", "decision": "blocked_until_fresh_base_reference_reproduction" if fresh_reproduction_blocked else "blocked_until_fresh_base_reference_smoke" if fresh_smoke_blocked else "blocked_until_fresh_base_reference_probe" if fresh_reference_blocked else "blocked_until_fresh_base_data_ready", "human_summary": blocker.get("summary", "")},
                "experiment_iteration_audit": {"status": "blocked", "human_summary": f"{selected_base_display} 参考复现审计通过前不启动主线实验。" if fresh_reproduction_blocked else f"{selected_base_display} 有界 reference smoke 通过前不启动主线实验。" if fresh_smoke_blocked else f"{selected_base_display} 参考协议/环境 manifest 只读探针通过前不启动主线实验。" if fresh_reference_blocked else f"{selected_base_display} 数据/loader 合同通过前不启动主线实验。"},
                "full_research_cycle": concise_full_cycle,
            },
            "paper": {
                **compact_paper_stage(paper_raw),
                "status": "blocked_before_paper_generation",
                "summary": f"{selected_base_display} 参考复现审计通过前不启动论文写作。" if fresh_reproduction_blocked else f"{selected_base_display} 有界 reference smoke 通过前不启动论文写作。" if fresh_smoke_blocked else f"{selected_base_display} 参考协议/环境 manifest 只读探针通过前不启动论文写作。" if fresh_reference_blocked else f"{selected_base_display} 数据/loader 合同通过前不启动论文写作。",
                "summary_zh": f"{selected_base_display} 参考复现审计通过前不启动论文写作。" if fresh_reproduction_blocked else f"{selected_base_display} 有界 reference smoke 通过前不启动论文写作。" if fresh_smoke_blocked else f"{selected_base_display} 参考协议/环境 manifest 只读探针通过前不启动论文写作。" if fresh_reference_blocked else f"{selected_base_display} 数据/loader 合同通过前不启动论文写作。",
                "summary_en": "Paper writing is blocked until the selected-base reference reproduction audit passes." if fresh_reproduction_blocked else "Paper writing is blocked until the selected-base bounded reference smoke passes." if fresh_smoke_blocked else "Paper writing is blocked until the selected-base reference protocol/env manifest probe passes." if fresh_reference_blocked else "Paper writing is blocked until the selected-base data/loader contract passes.",
                "venue": human.get("target_venue") or run_preferences.get("target_venue") or run_preferences.get("venue") or "",
                "paper_generation_skipped": True,
                "paper_generation_skipped_reason": "fresh base reference reproduction is not audited" if fresh_reproduction_blocked else "fresh base bounded reference smoke is not audited" if fresh_smoke_blocked else "fresh base reference protocol/env manifest is not audited" if fresh_reference_blocked else "fresh paper base lacks loader-ready real data",
                "paper_normality_status": paper_raw.get("paper_normality_status") or "blocked",
                "paper_venue_format_status": paper_raw.get("paper_venue_format_status") or "configured",
                "paper_figure_quality_status": paper_raw.get("paper_figure_quality_status") or "blocked",
            },
        }
        out["stages"] = stages
        out["full_research_cycle"] = full_cycle
        out["blockers"] = concise_full_cycle["latest_blockers"]
        out["blocker_count"] = 1
        out["current_blocker"] = concise_full_cycle["latest_blockers"][0]
        out["next_actions"] = blocker_actions
        out["next_action"] = blocker.get("next_action", "")
        full_job_status = str(blocker.get("reference_full_job_status") or "")
        if full_job_status == "running" and not blocker.get("paper_level_reproduction_passed"):
            full_job_pid = blocker.get("reference_full_job_pid", "")
            full_job_log = blocker.get("reference_full_job_log", "")
            job_agent = {
                "id": "selected-base-full-reference-reproduction",
                "name": reference_wrapper_label,
                "role": "reference-reproduction-wrapper",
                "stage": "fresh-base-reference-reproduction",
                "status": "running",
                "goal": f"run audited {base_display} full reference reproduction before experiments or paper writing",
                "current_step": f"{base_display} full reference reproduction wrapper 正在运行，PID={full_job_pid}。",
                "process_alive": True,
                "pid": full_job_pid,
                "log_path": full_job_log,
            }
            existing_agent = out.get("agent_state") if isinstance(out.get("agent_state"), dict) else {}
            existing_running = existing_agent.get("running") if isinstance(existing_agent.get("running"), list) else []
            out["agent_state"] = {
                **existing_agent,
                "main": job_agent,
                "running": [job_agent, *[row for row in existing_running if isinstance(row, dict) and row.get("id") != job_agent["id"]]][:5],
            }
    literature_source = summary.get("literature_survey")
    if not isinstance(literature_source, dict):
        state_source = summary.get("state", {}) if isinstance(summary.get("state"), dict) else {}
        literature_source = state_source.get("literature_survey")
    if not isinstance(literature_source, dict):
        stage_source = exp_raw.get("literature_survey", {}) if isinstance(exp_raw, dict) else {}
        literature_source = stage_source
    if not isinstance(literature_source, dict):
        literature_source = _taste_literature_summary(project_root)
    literature_survey = _compact_literature_survey(literature_source)
    if not isinstance(literature_survey, dict):
        literature_survey = {}
    current_find_pipeline = _current_find_pipeline_summary(project_root)
    projection = _current_find_recommendation_projection(project_root, str(current_find_pipeline.get("run_id") or ""))
    literature_survey["current_find_pipeline"] = current_find_pipeline
    counts = literature_survey.get("counts") if isinstance(literature_survey.get("counts"), dict) else {}
    if projection:
        projection_counts = projection.get("counts") if isinstance(projection.get("counts"), dict) else {}
        raw_projection_rows = projection.get("strong_recommendations") if isinstance(projection.get("strong_recommendations"), list) else projection.get("recommendations") if isinstance(projection.get("recommendations"), list) else projection.get("articles") if isinstance(projection.get("articles"), list) else []
        projected_count = len(_human_recommendation_literature_rows(raw_projection_rows)) if raw_projection_rows else _as_int(projection.get("strict_strong_anchor_count"), 0) or _as_int(projection_counts.get("recommended"), 0)
        projected_target = _as_int(projection.get("recommendation_target_count"), 0) or _as_int(projection_counts.get("recommendation_target_count"), 0)
        projected_shortfall = max(0, projected_target - projected_count) if raw_projection_rows and projected_target else _as_int(projection.get("recommendation_shortfall"), 0) if projection.get("recommendation_shortfall") not in (None, "") else max(0, projected_target - projected_count) if projected_target else 0
        literature_survey.update({
            "run_id": projection.get("run_id") or literature_survey.get("run_id"),
            "strict_strong_anchor_count": projected_count,
            "recommendation_target_count": projected_target,
            "recommendation_shortfall": projected_shortfall,
            "status": "recommendation_shortfall" if projected_shortfall else "current_find_packet_ready",
        })
        counts = {**counts, **projection_counts, "strong_recommendations": projected_count, "recommended": projected_count, "strict_strong_anchor_count": projected_count, "recommendation_target_count": projected_target, "recommendation_shortfall": projected_shortfall}
    counts = {
        **counts,
        "raw_title_index_papers": counts.get("raw_title_index_papers", counts.get("raw_title_index", 0)),
        "venue_category_selected_papers": counts.get("venue_category_selected_papers", counts.get("category_selected_papers", 0)),
        "category_selected_papers": counts.get("category_selected_papers", counts.get("venue_category_selected_papers", 0)),
        "venue_title_filter_input_papers": counts.get("venue_title_filter_input_papers", 0),
        "llm_scored_candidates": counts.get("llm_scored_candidates", counts.get("evaluated_candidates", 0)),
        "readings": current_find_pipeline.get("reading_count", counts.get("readings", 0)),
        "read_artifacts": current_find_pipeline.get("read_artifact_count", counts.get("read_artifacts", main_route.get("readings", 0))),
        "full_text_reading_count": current_find_pipeline.get("full_text_reading_count", counts.get("full_text_reading_count", 0)),
        "pending_full_text_reading_count": current_find_pipeline.get("pending_full_text_reading_count", counts.get("pending_full_text_reading_count", 0)),
        "ideas": main_route.get("ideas", counts.get("ideas", 0)),
        "plans": main_route.get("plans", counts.get("plans", 0)),
    }
    literature_survey["counts"] = counts
    literature_survey.setdefault("status", "current_find_packet_ready")
    literature_survey["display_hidden_on_blocked_route"] = False
    out["literature_survey"] = literature_survey
    if isinstance(out.get("stages", {}).get("experiment"), dict):
        out["stages"]["experiment"].pop("literature_survey", None)
    out["topic"] = cfg_public.get("topic", "")
    current_config_venue = run_preferences.get("target_venue") or run_preferences.get("venue") or human.get("target_venue") or ""
    out["run_preferences"] = {**run_preferences, "target_venue": current_config_venue, "venue": current_config_venue}
    out["summary"] = human.get("summary") or full_cycle.get("summary_zh") or full_cycle.get("summary") or cfg_public.get("topic", "")
    out["status"] = human.get("status") or full_cycle.get("status", "")
    out["supervision"] = human.get("supervision", {}) if isinstance(human.get("supervision"), dict) else {}
    if isinstance(out.get("supervision"), dict):
        normalized_job = _normalize_full_cycle_job(project_root, project_id, out["supervision"].get("full_cycle_job", {}))
        out["supervision"]["full_cycle_job"] = _public_full_cycle_job(normalized_job, target_venue=current_config_venue)
        if isinstance(out.get("human_supervision"), dict) and isinstance(out["human_supervision"].get("supervision"), dict):
            out["human_supervision"]["supervision"]["full_cycle_job"] = out["supervision"]["full_cycle_job"]
    live_job = out.get("supervision", {}).get("full_cycle_job", {}) if isinstance(out.get("supervision"), dict) else {}
    if _full_cycle_job_is_live(live_job):
        pid = str(live_job.get("pid") or "")
        elapsed = str(live_job.get("elapsed") or live_job.get("elapsed_sec") or "")
        cmd = str(live_job.get("cmd") or "")
        if live_job.get("kind") in {"experiment_or_reproduction", "active_child_worker"}:
            live_summary = f"实验训练正在运行；PID={pid}" + (f"；运行时长={elapsed}" if elapsed else "") + (f"；命令={cmd}" if cmd else "")
        else:
            live_summary = f"完整科研自循环正在运行；PID={pid}" + (f"；运行时长={elapsed}" if elapsed else "") + (f"；命令={cmd}" if cmd else "")
        out["status"] = "running"
        out["summary"] = live_summary
        running_full_cycle = {**full_cycle, "status": "running", "summary": live_summary, "summary_zh": live_summary, "current_goal": "等待当前运行任务产出指标、审计文件和下一步门控结论。"}
        running_full_cycle.pop("finished_at", None)
        running_full_cycle.pop("completed_at", None)
        out["full_research_cycle"] = running_full_cycle
        out["stages"]["experiment"] = {**out["stages"].get("experiment", {}), "status": "running", "summary": live_summary, "summary_zh": live_summary, "full_research_cycle": out["full_research_cycle"]}
        if isinstance(out.get("human_supervision"), dict):
            out["human_supervision"] = {**out["human_supervision"], "status": "running", "summary": live_summary, "summary_i18n": {"zh": live_summary, "en": live_summary}}
        if isinstance(out.get("stages", {}).get("environment"), dict):
            out["stages"]["environment"] = {**out["stages"].get("environment", {}), "status": "ready"}
    full_cycle = out.get("full_research_cycle", full_cycle) if isinstance(out.get("full_research_cycle"), dict) else full_cycle
    human = out.get("human_supervision", human) if isinstance(out.get("human_supervision"), dict) else human
    stages = out.get("stages", stages) if isinstance(out.get("stages"), dict) else stages
    state_experiments = stages["experiment"].get("experiments", [])
    if not isinstance(state_experiments, list):
        state_experiments = []
    state_experiment_record = dict(stages["experiment"].get("experiment_record", {})) if isinstance(stages["experiment"].get("experiment_record", {}), dict) else {}
    if "rows" in state_experiment_record:
        state_experiment_record["rows"] = []
    state_experiment_count = stages["experiment"].get("experiment_count", len(state_experiments))
    state_completed_experiment_count = stages["experiment"].get("completed_experiment_count")
    if state_completed_experiment_count is None:
        state_completed_experiment_count = len([row for row in state_experiments if isinstance(row, dict) and str(row.get("status", "")).lower() in {"completed", "success", "repaired"}])
    out["state"] = {
        "experiment_count": state_experiment_count,
        "completed_experiment_count": state_completed_experiment_count,
        "show_experiment_summary_count": bool(stages["experiment"].get("show_experiment_summary_count", False)),
        "show_synthetic_smoke_warning": bool(stages["experiment"].get("show_synthetic_smoke_warning", False)),
        "audit_ready_completed_experiment_count": stages["experiment"].get("audit_ready_completed_experiment_count", 0),
        "running_experiment_count": stages["experiment"].get("running_experiment_count", 0),
        "recent_experiments": stages["experiment"].get("recent_experiments", state_experiments[-8:]),
        "experiment_record": state_experiment_record,
        "reference_reproduction_gate": stages["experiment"].get("reference_reproduction_gate", {}),
        "scientific_progress_gate": stages["experiment"].get("scientific_progress_gate", {}),
        "experiment_iteration_audit": stages["experiment"].get("experiment_iteration_audit", {}),
        "full_research_cycle": full_cycle,
        "human_supervision": human,
    }
    out["literature_status"] = out["literature_survey"]["status"]
    out["current_find_pipeline"] = current_find_pipeline
    out["readings"] = current_find_pipeline.get("reading_count", current_find_pipeline.get("readings") or 0)
    out["read_artifacts"] = current_find_pipeline.get("read_artifact_count", main_route.get("readings", 0))
    out["ideas"] = current_find_pipeline.get("ideas") or main_route.get("ideas", 0)
    out["plans"] = current_find_pipeline.get("plans") or main_route.get("plans", 0)
    out["fresh_base"] = {"title": main_route.get("base_title", ""), "venue": main_route.get("base_venue", ""), "year": main_route.get("base_year", ""), "repo_name": main_route.get("repo_name", ""), "repo_path": main_route.get("repo_path", ""), "dataset": main_route.get("dataset", ""), "ready_datasets": main_route.get("ready_datasets", [])}
    out["data_status"] = blocker.get("data_status", "")
    out["data_decision"] = blocker.get("data_decision", "")
    guidance_queue = _read_json(project_root / "state" / "guidance_queue.json", [])
    if isinstance(guidance_queue, list):
        out["queued_guidance"] = [
            {key: row.get(key) for key in ["id", "stage", "target_agent_id", "source", "message", "status", "created_at"] if key in row}
            for row in guidance_queue
            if isinstance(row, dict) and str(row.get("status") or "queued") == "queued"
        ][-5:]

    def _public_running_summary(text: Any) -> str:
        value = str(text or "")
        full_job = out.get("supervision", {}).get("full_cycle_job", {}) if isinstance(out.get("supervision"), dict) else {}
        if not _full_cycle_job_is_live(full_job):
            if _full_cycle_summary_claims_live(value) or "full-cycle-" in value:
                return _terminal_full_cycle_summary(full_cycle, base_title=str(main_route.get("base_title") or ""))
            return _public_run_summary_without_action_plan(value)
        if "当前步骤=" not in value and "full-cycle-" not in value:
            return _public_run_summary_without_action_plan(value)
        pid = str(full_job.get("pid") or "")
        elapsed = str(full_job.get("elapsed") or full_job.get("elapsed_sec") or "")
        phase = _public_phase_for_full_cycle(full_job.get("stage"), project_id, project_root)
        summary = f"完整科研自循环正在运行；阶段={phase}"
        if pid:
            summary += f"；PID={pid}"
        if elapsed:
            summary += f"；运行时长={elapsed}"
        return summary


    public_summary = _public_running_summary(out.get("summary") or full_cycle.get("summary_zh") or full_cycle.get("summary"))
    if public_summary:
        out["summary"] = public_summary
        if isinstance(out.get("full_research_cycle"), dict):
            out["full_research_cycle"] = {**out["full_research_cycle"], "summary": public_summary, "summary_zh": public_summary}
        if isinstance(out.get("human_supervision"), dict):
            out["human_supervision"] = {**out["human_supervision"], "summary": public_summary, "summary_i18n": {"zh": public_summary, "en": public_summary}}
        if isinstance(out.get("stages", {}).get("experiment"), dict):
            out["stages"]["experiment"] = {**out["stages"]["experiment"], "summary": public_summary, "summary_zh": public_summary}
        if isinstance(out.get("state"), dict):
            state = dict(out["state"])
            if isinstance(state.get("full_research_cycle"), dict):
                state["full_research_cycle"] = {**state["full_research_cycle"], "summary": public_summary, "summary_zh": public_summary}
            if isinstance(state.get("human_supervision"), dict):
                state["human_supervision"] = {**state["human_supervision"], "summary": public_summary, "summary_i18n": {"zh": public_summary, "en": public_summary}}
            out["state"] = state
    out["payload_bytes"] = _json_size(out)
    return out


def _light_find_survey_from_progress(project_dir: Path) -> dict[str, Any]:
    progress = _read_json(project_dir / "planning" / "finding" / "find_progress.json", {})
    projection = _read_json(project_dir / "state" / "current_find_recommendation_projection.json", {})
    if not isinstance(progress, dict):
        progress = {}
    if not isinstance(projection, dict):
        projection = {}
    if not progress and not projection:
        return {}
    source_status = progress.get("source_status") if isinstance(progress.get("source_status"), list) else []
    selection = progress.get("selection") if isinstance(progress.get("selection"), dict) else _current_project_source_selection(project_dir.name, project_dir)
    verified_rows = _current_verified_venue_metadata_rows(project_dir.name, project_dir, selection)
    health_check_source_status = _current_health_check_source_status_rows(project_dir.name, project_dir, selection)
    source_status = _merge_verified_venue_metadata_rows(source_status, verified_rows)
    venue_counts = _venue_metadata_counts(source_status or verified_rows)
    progress_counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
    projection_counts = projection.get("counts") if isinstance(projection.get("counts"), dict) else {}
    projection_survey_stats = projection.get("survey_stats") if isinstance(projection.get("survey_stats"), dict) else {}
    counts = {
        **{key: value for key, value in venue_counts.items() if value not in (None, "")},
        **progress_counts,
        **projection_counts,
        **projection_survey_stats,
    }
    run_id = str(projection.get("run_id") or projection.get("source_run_id") or progress.get("run_id") or progress.get("source_run_id") or "").strip()
    projection_rows = _human_recommendation_literature_rows(projection.get("strong_recommendations") or projection.get("recommendations") or projection.get("articles") or [])
    if projection.get("strong_recommendations") or projection.get("recommendations") or projection.get("articles"):
        strong_count = len(projection_rows)
    else:
        strong_count = int(projection.get("strict_strong_anchor_count") or projection_counts.get("recommended") or progress.get("strong_recommendation_count") or counts.get("strong_recommendations") or 0)
    target_count = int(projection.get("recommendation_target_count") or projection_counts.get("recommendation_target_count") or progress.get("recommendation_target_count") or counts.get("recommendation_target_count") or 0)
    shortfall = int(projection.get("recommendation_shortfall") if projection.get("recommendation_shortfall") not in (None, "") else progress.get("recommendation_shortfall") if progress.get("recommendation_shortfall") not in (None, "") and not projection else (max(0, target_count - strong_count) if target_count else 0))
    return {
        "run_id": run_id,
        "status": "recommendation_shortfall" if shortfall else "current_find_packet_ready" if run_id else "missing_find_packet",
        "source_status": source_status[:20],
        "health_check_source_status": health_check_source_status[:20],
        "selection": selection,
        "venue_sources": source_status[:20],
        "counts": {
            "raw_title_index_papers": counts.get("raw_title_index_papers") or counts.get("raw_title_index") or counts.get("venue_total_papers_available") or 0,
            "venue_total_papers_available": counts.get("venue_total_papers_available") or counts.get("raw_title_index_papers") or counts.get("raw_title_index") or 0,
            "venue_corpus_audited_papers": counts.get("venue_corpus_audited_papers") or counts.get("raw_title_index_papers") or counts.get("raw_title_index") or 0,
            "venue_category_selected_papers": counts.get("venue_category_selected_papers") or counts.get("category_selected_papers") or 0,
            "category_selected_papers": counts.get("category_selected_papers") or counts.get("venue_category_selected_papers") or 0,
            "category_filtered_papers": counts.get("category_filtered_papers") or counts.get("venue_title_filter_input_papers") or 0,
            "tfidf_screened_papers": counts.get("tfidf_screened_papers") or counts.get("category_filtered_papers") or counts.get("venue_title_filter_input_papers") or 0,
            "venue_title_filter_input_papers": counts.get("venue_title_filter_input_papers") or 0,
            "title_score_input_papers": counts.get("title_score_input_papers") or 0,
            "llm_title_scored_papers": counts.get("llm_title_scored_papers") or 0,
            "title_candidates": counts.get("title_candidates") or counts.get("venue_final_title_candidates") or 0,
            "venue_final_title_candidates": counts.get("venue_final_title_candidates") or counts.get("title_candidates") or 0,
            "traceable_candidates": counts.get("traceable_candidates") or counts.get("title_candidates") or counts.get("venue_final_title_candidates") or 0,
            "detail_fetched": counts.get("detail_fetched") or counts.get("venue_detail_fetched_candidates") or 0,
            "venue_detail_fetched_candidates": counts.get("venue_detail_fetched_candidates") or counts.get("detail_fetched") or 0,
            "evaluated_candidates": counts.get("evaluated_candidates") or 0,
            "abstract_scored_papers": counts.get("abstract_scored_papers") or counts.get("llm_scored_candidates") or 0,
            "llm_scored_candidates": counts.get("llm_scored_candidates") or counts.get("abstract_scored_papers") or 0,
            "abstract_fetch_failed_candidates": counts.get("abstract_fetch_failed_candidates") or 0,
            "final_llm_scoring_skipped_candidates": counts.get("final_llm_scoring_skipped_candidates") or 0,
            "strong_recommendations": strong_count,
            "recommended": strong_count,
            "articles": strong_count,
            "read_candidates": strong_count,
            "recommendation_target_count": target_count,
            "recommendation_shortfall": shortfall,
        },
    }


def _project_activity_mtime(project_dir: Path) -> float:
    latest = 0.0

    def track(path: Path) -> None:
        nonlocal latest
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass

    for path in [
        project_dir / "project.json",
        project_dir / "state" / "current_find_progress.json",
        project_dir / "state" / "full_research_cycle.json",
        project_dir / "state" / "claude_project_session_last_result.json",
        project_dir / "state" / "supervision_tick.json",
        project_dir / "paper" / "metadata" / "paper_pipeline.json",
    ]:
        track(path)

    for directory in [project_dir / "state", project_dir / "runs", project_dir / "reports", project_dir / "artifacts", project_dir / "paper"]:
        if not directory.exists():
            continue
        track(directory)
        try:
            children = list(directory.iterdir())
        except OSError:
            children = []
        for child in children:
            track(child)
    return latest


def list_projects() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not PROJECTS.exists():
        return rows
    for project_dir in sorted(p for p in PROJECTS.iterdir() if p.is_dir()):
        cfg_path = project_dir / "project.json"
        if not cfg_path.exists():
            continue
        cfg = _read_json(cfg_path, {})
        activity_mtime = _project_activity_mtime(project_dir)
        rows.append({
            "name": cfg.get("name", project_dir.name),
            "id": project_dir.name,
            "topic": cfg.get("topic", ""),
            "conda_env": cfg.get("conda_env", ""),
            "path": str(project_dir),
            "updated_at": dt.datetime.fromtimestamp(activity_mtime, dt.timezone.utc).isoformat() if activity_mtime else "",
            "literature_survey_preview": _light_find_survey_from_progress(project_dir),
            "_activity_mtime": activity_mtime,
        })
    rows.sort(key=lambda row: (-float(row.get("_activity_mtime") or 0.0), str(row.get("id") or "").lower()))
    for row in rows:
        row.pop("_activity_mtime", None)
    return rows




def _items(values: Any) -> list[Any]:
    return values if isinstance(values, list) else []


def _text_list(values: Any, empty: str = "暂无") -> str:
    items = [str(item) for item in _items(values) if str(item).strip()]
    return ", ".join(items) if items else empty


def _text_list_en(values: Any, empty: str = "none") -> str:
    items = [str(item) for item in _items(values) if str(item).strip()]
    return ", ".join(items) if items else empty


DATA_FILE_PURPOSE_ZH = {
    "train_3.txt": "训练交互记录",
    "test_3.txt": "测试交互记录",
    "trust_3.txt": "社交信任关系",
    "all_data.pkl": "打包后的 POI 轨迹数据",
    "dist_mat.npy": "地点距离矩阵",
}

DATA_FILE_PURPOSE_EN = {
    "train_3.txt": "training interactions",
    "test_3.txt": "test interactions",
    "trust_3.txt": "social trust graph",
    "all_data.pkl": "packed POI trajectory data",
    "dist_mat.npy": "POI distance matrix",
}


def _dataset_contract_label(files: list[Any], lang: str = "zh") -> str:
    names = [str(item).strip() for item in files if str(item).strip()]
    if not names:
        return "当前 repo 的数据格式" if lang == "zh" else "the active repo data format"
    mapping = DATA_FILE_PURPOSE_ZH if lang == "zh" else DATA_FILE_PURPOSE_EN
    purposes = [mapping.get(name, "") for name in names]
    if set(names) == {"all_data.pkl", "dist_mat.npy"}:
        return "当前 repo 的轨迹/地点数据包格式" if lang == "zh" else "the active repo trajectory/POI data bundle"
    readable = [purpose or name for name, purpose in zip(names, purposes)]
    return _text_list(readable) if lang == "zh" else _text_list_en(readable)


def _dataset_missing_label(files: list[Any], lang: str = "zh") -> str:
    names = [str(item).strip() for item in files if str(item).strip()]
    if not names:
        return ""
    mapping = DATA_FILE_PURPOSE_ZH if lang == "zh" else DATA_FILE_PURPOSE_EN
    readable = [mapping.get(name, name) for name in names]
    return _text_list(readable) if lang == "zh" else _text_list_en(readable)


def _dataset_loader_note(row: dict[str, Any], required: list[Any], missing: list[Any], lang: str = "zh") -> str:
    contract = _dataset_contract_label(required, lang)
    missing_label = _dataset_missing_label(missing, lang)
    if lang == "zh":
        if missing_label:
            return f"{contract}不完整，缺少：{missing_label}。"
        if row.get("loader_probe_success"):
            return f"{contract}已通过当前 repo loader。"
        if row.get("generic_probe_success") or row.get("generic_probe", {}).get("success"):
            return f"只发现了部分数据文件，但没有通过当前 repo loader；不能作为论文证据。"
        return f"{contract}未通过当前 repo loader。"
    if missing_label:
        return f"{contract} is incomplete; missing: {missing_label}."
    if row.get("loader_probe_success"):
        return f"{contract} passed the active repo loader."
    if row.get("generic_probe_success") or row.get("generic_probe", {}).get("success"):
        return "Some data files were found, but the active repo loader did not pass; this is not paper evidence."
    return f"{contract} did not pass the active repo loader."


def _set_i18n(row: dict[str, Any], key: str, zh: str, en: str) -> None:
    row[key] = zh
    row[f"{key}_zh"] = zh
    row[f"{key}_en"] = en
    row[f"{key}_i18n"] = {"zh": zh, "en": en}


def _set_list_i18n(row: dict[str, Any], key: str, zh: list[str], en: list[str]) -> None:
    row[key] = zh
    row[f"{key}_zh"] = zh
    row[f"{key}_en"] = en
    row[f"{key}_i18n"] = {"zh": zh, "en": en}


DATASET_STATUS_EN = {
    "可用于真实实验": "Ready for real experiments",
    "登记矛盾，不能作为可用数据": "Registry conflict: not usable",
    "登记矛盾，缺少 loader 成功证据": "Registry conflict: loader evidence missing",
    "仅流程自测": "Smoke-test only",
    "缺少 active repo 真实数据": "Missing real data for the active repo",
    "有文件但不匹配当前 repo": "Files exist but do not match the active repo",
    "当前 repo 数据不完整": "Incomplete data for the active repo",
    "候选数据不完整且不属当前 repo": "Incomplete candidate data outside the active repo",
    "非当前 repo 数据线索": "Data lead for a different repo",
    "待补证据": "Evidence pending",
    "未找到可审计数据": "No auditable data found",
    "未通过 claim-ready 检查": "Claim-ready check failed",
}


REPO_STATUS_EN = {
    "当前路线：代码和真实数据已通过": "Current route: code and real data passed",
    "当前路线：代码环境可用，但数据未过门": "Current route: code environment is usable, but data gate is not cleared",
    "当前路线需要复核": "Current route needs review",
    "候选 repo，尚未本地验证": "Candidate repo, not locally verified yet",
    "可继续审计的候选": "Candidate worth further audit",
}


KNOWN_TEXT_ZH = {
    "verified_datasets": "已验证真实数据集",
    "missing_llm": "缺少 LLM 组件，需在实验迭代中补齐",
    "runnable_entrypoint": "存在可运行入口",
    "no_evidence_ready_competitor": "没有更强的 evidence-ready 竞品仓库",
}


def _zh_or_known(text: Any) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if value in KNOWN_TEXT_ZH:
        return KNOWN_TEXT_ZH[value]
    if any("\u4e00" <= ch <= "\u9fff" for ch in value):
        return value
    return "底层诊断为英文旧格式；请重新运行环境配置，让 project agent 生成中文结构化说明。"


def _claude_decision_i18n(decision: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(decision, dict):
        return {}
    out = dict(decision)
    rationale = str(out.get("rationale") or "").strip()
    rationale_en = str(out.get("rationale_en") or rationale).strip()
    rationale_zh = str(out.get("rationale_zh") or "").strip() or _zh_or_known(rationale)
    out["rationale"] = rationale_en or rationale
    out["rationale_en"] = rationale_en
    out["rationale_zh"] = rationale_zh
    out["rationale_i18n"] = {"zh": rationale_zh, "en": rationale_en}
    for key in ["repo_action_reason", "env_action_reason", "data_action_reason", "stewardship_memory"]:
        raw = str(out.get(key) or "").strip()
        en_value = str(out.get(f"{key}_en") or raw).strip()
        zh_value = str(out.get(f"{key}_zh") or "").strip() or _zh_or_known(raw)
        out[key] = en_value or raw
        out[f"{key}_en"] = en_value
        out[f"{key}_zh"] = zh_value
        out[f"{key}_i18n"] = {"zh": zh_value, "en": en_value}
    for key in ["required_modifications", "risks", "evidence"]:
        raw = out.get(key, [])
        values = raw if isinstance(raw, list) else [raw] if raw else []
        en_values = out.get(f"{key}_en", values)
        if not isinstance(en_values, list):
            en_values = [en_values] if en_values else []
        zh_values = out.get(f"{key}_zh", [])
        if not isinstance(zh_values, list):
            zh_values = [zh_values] if zh_values else []
        if not zh_values:
            zh_values = [_zh_or_known(item) for item in values if str(item).strip()]
        out[key] = [str(item) for item in en_values if str(item).strip()]
        out[f"{key}_en"] = [str(item) for item in en_values if str(item).strip()]
        out[f"{key}_zh"] = [str(item) for item in zh_values if str(item).strip()]
        out[f"{key}_i18n"] = {"zh": out[f"{key}_zh"], "en": out[f"{key}_en"]}
    return out


def _dataset_is_claim_ready(row: dict[str, Any]) -> bool:
    missing = _items(row.get("missing_required_files"))
    return bool(row.get("claim_ready")) and bool(row.get("loader_probe_success")) and not missing


def _dataset_evidence_en(
    row: dict[str, Any],
    required: list[Any],
    missing: list[Any],
    present: list[str],
    local_path: str,
    placement: str,
) -> list[str]:
    evidence: list[str] = []
    if required:
        evidence.append(f"Loader contract: {_dataset_contract_label(required, 'en')}")
    if missing:
        evidence.append(f"Incomplete data: {_dataset_missing_label(missing, 'en')} missing")
    if present and (not row.get("claim_ready", False) or len(set(present)) < len(required)):
        evidence.append("Partial local data was found, but the loader contract is still incomplete.")
    if row.get("loader_probe_success"):
        evidence.append("Active repo loader passed.")
    elif row.get("generic_probe_success") or row.get("generic_probe", {}).get("success"):
        evidence.append("Only generic file discovery passed; active repo loader did not pass.")
    if row.get("candidate_root_count"):
        evidence.append(f"The workflow checked {row.get('candidate_root_count')} candidate directories without finding complete loadable data.")
    if row.get("source_confidence_reasons"):
        evidence.append(f"Data-source limitations: {_text_list_en(row.get('source_confidence_reasons'))}")
    return evidence


def _dataset_is_synthetic_smoke(row: dict[str, Any], name: str) -> bool:
    fields = [
        name,
        row.get("task"),
        row.get("access"),
        row.get("format"),
        row.get("split"),
        row.get("notes"),
        row.get("source"),
        row.get("status"),
    ]
    text = " ".join(str(value or "").lower() for value in fields)
    synthetic = any(marker in text for marker in ["synthetic", "generated locally", "toy", "合成", "玩具"])
    smoke = any(marker in text for marker in ["smoke", "pipeline", "流程", "自测", "debug"])
    return bool(synthetic and smoke)


def _dataset_messages_en(
    row: dict[str, Any],
    name: str,
    status_label: str,
    active_repo: str,
    required: list[Any],
    missing: list[Any],
    claim_ready: bool,
) -> tuple[str, str, str]:
    missing_text = _text_list_en(missing)
    required_text = _text_list_en(required)
    status_en = DATASET_STATUS_EN.get(status_label, "Claim-ready check failed")
    if claim_ready:
        summary = f"{name} passed the active repo loader checks and can be used for real experiments and paper evidence."
        explanation = summary
        next_action = "Continue real experiments and write metrics, bad cases, and evidence audits into the experiment registry."
    elif row.get("claim_ready") and missing:
        summary = f"{name} has a registry conflict: claim_ready=true but the same audit still reports missing files ({missing_text}). TASTE downgraded it from usable data."
        explanation = "Claim-ready data must have complete real files, loader_probe_success=true for the current repo, and no missing_required_files. Conflicting evidence stays out of the usable-data section."
        next_action = "Re-run the repo/data probe so TASTE refreshes the dataset registry from the active repo loader contract before any formal experiment claim."
    elif row.get("claim_ready") and not row.get("loader_probe_success"):
        summary = f"{name} has a registry conflict: claim_ready=true but no loader_probe_success=true evidence for the current repo."
        explanation = "File presence or an earlier probe is not enough. The workflow needs a successful current-repo loader probe, reproducible split evidence, and no missing files before using the dataset as paper evidence."
        next_action = "Re-run the loader probe; if it fails, fix files, convert the format, or return to repo selection."
    elif _dataset_is_synthetic_smoke(row, name):
        summary = f"{name} is a generated smoke-test dataset; it cannot support real paper conclusions."
        explanation = "It has no audited real-world data distribution, so The workflow keeps it as a pipeline test instead of claim-ready evidence."
        next_action = "Keep searching for or placing real data. Synthetic smoke-test results may appear in debug logs only, not as main paper results."
    elif missing:
        summary = f"{name} is not claim-ready for the active repo {active_repo}. {_dataset_loader_note(row, required, missing, 'en')}"
        explanation = "The workflow keeps this as a tracked candidate data gap, not as real experiment or paper evidence, until the active loader contract passes."
        next_action = "If this dataset is needed later, complete the loader-specific data files and rerun the dataset probe; otherwise use loader-ready datasets."
    elif row.get("available") and not claim_ready:
        summary = f"{name} has some local or probe evidence, but it has not reached claim-ready status for the active repo."
        explanation = "The workflow needs a complete loader success and reproducible experiment evidence before promoting the dataset into the paper evidence chain."
        next_action = "Complete required files and re-run the loader probe; only claim_ready=true and loader_probe_success=true datasets are promoted."
    elif not row.get("available", row.get("registry_available", False)):
        summary = f"{name} has no auditable, loadable local data files yet."
        explanation = "The workflow found no complete local path and no successful loader probe, so it cannot treat this as usable data."
        next_action = "Add the data files and re-run dataset registry/probe before entering the paper evidence chain."
    else:
        summary = f"{name} has partial records but did not pass the current active repo loader probe."
        explanation = "The workflow needs real loader success, reproducible split/metric evidence, and auditable experiment artifacts before marking data as claim-ready."
        next_action = "Re-run the probe and inspect failure logs. If the format is incompatible, return to repo selection or data conversion."
    if status_en and status_en not in summary:
        return summary, explanation, next_action
    return summary, explanation, next_action


def _repo_evidence_en(row: dict[str, Any], url: str, support_signals: list[Any], missing_topics: list[Any]) -> list[str]:
    evidence: list[str] = []
    if url:
        evidence.append(f"Source: {url}")
    if row.get("score") != "":
        evidence.append(f"repo reuse score: {row.get('score')}; bucket: {row.get('bucket') or 'unbucketed'}")
    if support_signals:
        evidence.append(f"Local support signals: {_text_list_en(support_signals)}")
    if missing_topics:
        evidence.append(f"Topic gaps: {_text_list_en(missing_topics)}")
    if row.get("notes"):
        evidence.append(f"Candidate-pool note: {row.get('notes')}")
    return evidence


def _repo_messages_en(
    row: dict[str, Any],
    name: str,
    active: bool,
    execution_ready: bool,
    missing_topics: list[Any],
    blocked_dataset_count: int,
    ready_dataset_count: int,
) -> tuple[str, str, str]:
    if active and execution_ready and ready_dataset_count:
        summary = f"{name} is the active repo. The workflow has confirmed runnable code signals and {ready_dataset_count} real dataset(s) passing the active loader probe."
        data_note = f"{blocked_dataset_count} candidate dataset(s) are still blocked and will be recorded as gaps, not paper conclusions." if blocked_dataset_count else "No data blocker is currently recorded."
        topic_note = f"Topic gaps remain: {_text_list_en(missing_topics)}. Later experiments must address them on top of this runnable baseline." if missing_topics else "No obvious topic gap is currently recorded."
        explanation = f"{summary} {data_note} {topic_note}"
        next_action = "Enter real smoke tests and experiment iteration using only claim-ready datasets, while recording bad cases, metrics, and reproducible configs."
    elif active and execution_ready:
        summary = f"{name} is the active repo and its code/environment passed initial checks; the current blocker is the data gate."
        data_note = f"{blocked_dataset_count} dataset item(s) still lack claim-ready evidence." if blocked_dataset_count else "No data blocker is currently recorded."
        topic_note = f"Topic gaps remain: {_text_list_en(missing_topics)}. Method design must address them instead of claiming the repo already solves the full topic." if missing_topics else "No obvious topic gap is currently recorded."
        explanation = f"{summary} {data_note} {topic_note}"
        next_action = "Prioritize real data and loader success for the active repo, then let experiment iteration implement the project-specific method changes."
    elif active:
        summary = f"{name} is selected as the active repo, but the candidate pool does not yet show it as fully execution-ready."
        explanation = "This usually means the candidate pool is an early GitHub-search snapshot while active_repo reflects later local audit state. The workflow should trust active_repo and loader probes, then resync the candidate display."
        next_action = "Re-run repo audit to synchronize repo_candidates with active_repo execution state."
    elif not execution_ready:
        summary = f"{name} is a research/search-stage candidate repo and has not passed local execution audit yet."
        gap = f"Topic evidence is still missing for: {_text_list_en(missing_topics)}." if missing_topics else "Topic keywords look relevant, but README, installation, training entrypoints, and data access still need inspection."
        explanation = f"{summary} {gap} The workflow must verify installation, training/eval entrypoints, and obtainable data before switching routes."
        next_action = "If the active repo remains blocked, add this repo to the next audit round; do not mark it executable until audit passes."
    else:
        summary = f"{name} has reuse potential but is not the current main route."
        explanation = "It must be compared against the active repo on data availability, topic fit, and reproduction cost before switching."
        next_action = "Run local repo audit, dataset probe, and smoke tests before evidence gates decide whether to replace the active repo."
    return summary, explanation, next_action


def _repo_label(root: Path) -> str:
    active = _read_json(root / "state" / "active_repo.json", {})
    if isinstance(active, dict) and active.get("name"):
        name = str(active.get("name"))
        repo_path = str(active.get("repo_path") or "")
        if "/" in name:
            return name.split("/")[-1]
        return name
    return "当前 active repo"


def _dataset_block_reasons(root: Path) -> dict[str, Any]:
    req = _read_json(root / "state" / "repo_data_requirements.json", {})
    policy = _read_json(root / "state" / "data_unavailability_policy.json", {})
    registry = _read_json(root / "state" / "dataset_registry.json", [])
    reasons: dict[str, Any] = {}
    contract = req.get("contract", {}) if isinstance(req, dict) and isinstance(req.get("contract", {}), dict) else {}
    active_required = contract.get("required_files_per_dataset", []) if isinstance(contract, dict) else []
    expected_roots = contract.get("expected_roots", []) if isinstance(contract, dict) else []
    download_sources = req.get("download_sources", []) if isinstance(req, dict) else []

    if isinstance(req, dict):
        for row in req.get("local_statuses", []) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("dataset") or "").strip()
            if not name:
                continue
            missing: list[str] = []
            roots: list[dict[str, Any]] = []
            ready_root = str(row.get("ready_root") or "")
            for root_row in row.get("candidate_roots", []) or []:
                if not isinstance(root_row, dict):
                    continue
                files = root_row.get("missing_required_files", []) or []
                root_path = str(root_row.get("root") or "")
                root_exists = bool(root_row.get("exists", False))
                # Only aggregate missing files from the selected ready root, or
                # from existing/checked roots when no ready root was found.
                # Otherwise a ready dataset appears contradictory because
                # alternate non-existent candidate paths are also missing files.
                should_count_missing = (not ready_root and root_exists) or (ready_root and root_path == ready_root)
                if isinstance(files, list) and should_count_missing:
                    missing.extend(str(item) for item in files if item)
                roots.append({
                    "root": root_path,
                    "exists": root_exists,
                    "missing_required_files": files if isinstance(files, list) else [],
                    "present_required_files": root_row.get("present_required_files", []) if isinstance(root_row.get("present_required_files", []), list) else [],
                })
            status = str(row.get("status") or "")
            if status == "missing":
                reason = "active repo loader files are missing"
            elif status:
                reason = status
            else:
                reason = "dataset status was not recorded"
            reasons[name] = {
                "dataset": name,
                "status": status,
                "reason": reason,
                "available": bool(ready_root or status == "ready"),
                "claim_ready": bool(ready_root or status == "ready"),
                "probe_success": bool(ready_root or status == "ready"),
                "loader_probe_success": bool(ready_root or status == "ready"),
                "missing_required_files": sorted(set(missing)),
                "candidate_roots": roots[:4],
                "candidate_root_count": len(roots),
                "ready_root": ready_root,
                "active_repo_required_files": active_required,
                "expected_roots": expected_roots,
                "download_sources": download_sources,
            }

        for name in req.get("blocked_datasets", []) or []:
            dataset = str(name or "").strip()
            if dataset:
                reasons.setdefault(dataset, {
                    "dataset": dataset,
                    "status": "blocked",
                    "reason": "listed in blocked_datasets; inspect required files and data policy",
                    "active_repo_required_files": active_required,
                    "expected_roots": expected_roots,
                    "download_sources": download_sources,
                })
        for name in req.get("ready_datasets", []) or []:
            dataset = str(name or "").strip()
            if dataset:
                reasons.setdefault(dataset, {"dataset": dataset})
                reasons[dataset].update({
                    "dataset": dataset,
                    "status": "ready",
                    "reason": "ready dataset listed by repo_data_requirements",
                    "available": True,
                    "claim_ready": True,
                    "probe_success": True,
                    "loader_probe_success": True,
                    "missing_required_files": [],
                    "active_repo_required_files": active_required,
                    "expected_roots": expected_roots,
                })

    probe = _read_json(root / "state" / "real_dataset_probe.json", {})
    if isinstance(probe, dict):
        for row in probe.get("probes", []) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("dataset") or "").strip()
            if not name:
                continue
            loader = row.get("loader_probe", {}) if isinstance(row.get("loader_probe", {}), dict) else {}
            loader_success = bool(loader.get("success") or row.get("claim_ready"))
            if loader_success:
                reasons.setdefault(name, {"dataset": name})
                reasons[name].update({
                    "available": True,
                    "claim_ready": True,
                    "probe_success": True,
                    "loader_probe_success": True,
                    "generic_probe_success": bool(row.get("generic_probe_success") or row.get("generic_probe", {}).get("success")),
                    "missing_required_files": [],
                    "local_path": row.get("dataset_path", reasons[name].get("local_path", "")),
                    "reason": row.get("claim_ready_reason") or "real_dataset_probe loader succeeded",
                    "probe_timestamp": row.get("timestamp", ""),
                })
            else:
                reasons.setdefault(name, {"dataset": name})
                reasons[name].update({
                    "probe_success": False,
                    "loader_probe_success": False,
                    "generic_probe_success": bool(row.get("generic_probe_success") or row.get("generic_probe", {}).get("success")),
                })

    if isinstance(policy, dict):
        placements = policy.get("exact_user_data_placement_requests", []) or []
        placement = {
            str(row.get("dataset") or "").strip(): row
            for row in placements
            if isinstance(row, dict) and str(row.get("dataset") or "").strip()
        }
        for name, row in placement.items():
            reasons.setdefault(name, {"dataset": name})
            reasons[name].update({
                "place_required_files_under": row.get("place_required_files_under", ""),
                "required_files": row.get("required_files", []),
                "policy_decision": policy.get("decision", ""),
                "policy_rationale": policy.get("rationale", ""),
                "source_confidence_reasons": policy.get("source_confidence_reasons", []),
                "acquisition_plan_status": policy.get("acquisition_plan_status", ""),
                "acquisition_attempt_count": policy.get("acquisition_attempt_count", ""),
            })
            if not reasons[name].get("reason"):
                reasons[name]["reason"] = policy.get("rationale") or policy.get("decision") or "blocked by data availability policy"

    if isinstance(registry, list):
        for row in registry:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("dataset") or "").strip()
            if not name:
                continue
            reasons.setdefault(name, {"dataset": name})
            current_missing = reasons.get(name, {}).get("missing_required_files", [])
            registry_missing = row.get("missing_required_files", current_missing)
            existing_claim_ready = bool(reasons[name].get("claim_ready", False))
            registry_loader_success = bool(row.get("loader_probe_success", False))
            registry_claim_ready = bool(row.get("claim_ready", False)) and registry_loader_success
            merged_claim_ready = existing_claim_ready or registry_claim_ready
            existing_loader_success = bool(reasons[name].get("loader_probe_success", False))
            merged_loader_success = existing_loader_success or registry_loader_success or merged_claim_ready
            generic_probe_success = bool(row.get("generic_probe_success", False)) or (
                bool(row.get("probe_success", False)) and not registry_loader_success and not registry_claim_ready
            )
            reasons[name].update({
                "registry_available": bool(row.get("available", False)),
                "claim_ready": merged_claim_ready,
                "probe_success": merged_loader_success,
                "loader_probe_success": merged_loader_success,
                "generic_probe_success": bool(reasons[name].get("generic_probe_success", False)) or generic_probe_success,
                "notes": row.get("notes", ""),
                "local_path": row.get("local_path", ""),
                "format": row.get("format", ""),
                "split": row.get("split", ""),
                "missing_required_files": [] if merged_claim_ready else registry_missing if isinstance(registry_missing, list) else current_missing,
            })
    return reasons


def _dataset_human_fields(row: dict[str, Any], root: Path) -> dict[str, Any]:
    name = str(row.get("name") or row.get("dataset") or "数据集")
    required = _items(row.get("required_files")) or _items(row.get("active_repo_required_files"))
    missing = _items(row.get("missing_required_files"))
    present: list[str] = []
    for root_row in _items(row.get("candidate_roots")):
        if not isinstance(root_row, dict):
            continue
        files = root_row.get("present_required_files", []) or []
        if isinstance(files, list):
            present.extend(str(item) for item in files if str(item).strip())
    local_path = str(row.get("local_path") or "")
    placement = str(row.get("place_required_files_under") or "")
    active_repo = _repo_label(root)
    evidence: list[str] = []
    if required:
        evidence.append(f"loader 契约：{_dataset_contract_label(required, 'zh')}")
    if missing:
        evidence.append(f"数据不完整：缺少{_dataset_missing_label(missing, 'zh')}")
    if present and (not row.get("claim_ready", False) or len(set(present)) < len(required)):
        evidence.append("已找到部分本地数据，但 loader 契约仍不完整。")
    if row.get("loader_probe_success"):
        evidence.append("当前 repo loader 已通过。")
    elif row.get("generic_probe_success") or row.get("generic_probe", {}).get("success"):
        evidence.append("只通过了通用文件发现，当前 repo loader 没有通过。")
    if row.get("candidate_root_count"):
        evidence.append(f"已检查 {row.get('candidate_root_count')} 个候选目录，未找到完整可加载数据。")
    if row.get("source_confidence_reasons"):
        evidence.append(f"数据源限制：{_text_list(row.get('source_confidence_reasons'))}")
    evidence_en = _dataset_evidence_en(row, required, missing, present, local_path, placement)

    required_set = {str(item) for item in required}
    has_active_contract = bool(required_set)
    name_lower = name.lower()
    available = bool(row.get("available", row.get("registry_available", False)))
    claim_ready = _dataset_is_claim_ready(row)

    if claim_ready:
        status_label = "可用于真实实验"
        summary = f"{name} 已通过当前 repo 的 loader 检查，可以进入真实实验与论文证据链。"
        explanation = summary
        next_action = "继续运行真实实验，并把 metrics、bad-case 和 evidence audit 写入实验注册表。"
    elif row.get("claim_ready") and missing:
        status_label = "登记矛盾，不能作为可用数据"
        summary = f"{name} 的登记里出现 claim_ready=true，但同一审计记录仍显示缺少 {_text_list(missing)}，已将它降级为阻塞数据。"
        explanation = "claim-ready 必须同时满足真实文件齐全、当前 repo loader_probe_success=true、并且 missing_required_files 为空。只要这些证据互相矛盾，网页不会把它放进可用数据区，避免把坏数据带入论文证据链。"
        next_action = "重新运行 repo/data probe，以当前 active repo 的 loader 契约刷新 dataset registry；刷新前不要运行正式实验 claim。"
    elif row.get("claim_ready") and not row.get("loader_probe_success"):
        status_label = "登记矛盾，缺少 loader 成功证据"
        summary = f"{name} 的登记里出现 claim_ready=true，但没有当前 repo 的 loader_probe_success=true 证据，已将它降级为待补/阻塞数据。"
        explanation = "文件存在或早期 probe 通过不等于能被当前 active repo 加载。TASTE 需要看到 loader 成功、缺失文件为空、实验 split 可复现，才允许它进入真实实验与论文证据链。"
        next_action = "重新运行 loader probe；若失败，按报错补齐文件、转换格式或回到 repo 选择阶段。"
    elif _dataset_is_synthetic_smoke(row, name):
        status_label = "仅流程自测"
        summary = f"{name} 是生成的流程自测数据，只能证明代码链路能跑通，不能支撑论文里的真实实验结论。"
        explanation = "它没有经过审计的真实世界数据分布，因此 系统会把它保留为 smoke test，而不会把它升级为 claim-ready 数据。"
        next_action = "继续寻找或放置真实数据；合成流程自测结果只能写在调试记录里，不能写成论文主结果。"
    elif missing and has_active_contract:
        status_label = "当前 repo 数据不完整"
        needed = _text_list(required)
        checked = row.get("candidate_root_count") or len(_items(row.get("candidate_roots")))
        summary = f"{name} 是当前 active repo {active_repo} 的候选数据，但 loader 契约还不完整：需要 {needed}，缺少 {_text_list(missing)}。TASTE 检查了 {checked or '多个'} 个可能目录。"
        source_note = ""
        if row.get("source_confidence_reasons"):
            source_note = f" 数据源限制：{_text_list(row.get('source_confidence_reasons'))}。"
        explanation = summary + source_note + " 在 loader 成功前，它不会进入真实实验或论文证据链。"
        target = placement or local_path or "当前 repo 声明的数据目录"
        next_action = f"补齐当前 repo loader 需要的文件到 {target}，然后重新运行数据 probe；若长期无法补齐，TASTE 应回到 repo/data 选择阶段。"
    elif missing:
        status_label = "候选数据证据不足"
        summary = f"{name} 目前不是可用真实数据：{_dataset_loader_note(row, required, missing, 'zh')}"
        explanation = "文件线索存在不等于当前 repo loader 可加载。系统会保留这个数据缺口，但不会把它放进真实实验或论文证据链。"
        next_action = "补齐缺失文件并重新运行 loader probe；或让 TASTE 根据证据选择更合适的 repo/data 路线。"
    elif available and not claim_ready:
        if row.get("probe_success") or row.get("loader_probe_success"):
            status_label = "待补证据"
            summary = f"{name} 已有部分 probe / loader 迹象，但还没有达到 claim-ready。"
            explanation = "TASTE 看到了一些可读数据或局部加载迹象，但还缺少完整 loader 成功和可复现实验证据，因此暂不进入论文证据链。"
            next_action = "补齐 required files，并重新运行 loader probe；只有 claim_ready=true 且 loader_probe_success=true 时才会升级为可用数据。"
        else:
            status_label = "待补证据"
            summary = f"{name} 在登记层面可见，但还没有足够的 loader 证据，因此暂时不算可用数据。"
            explanation = "TASTE 目前只确认目录或文件线索存在，没有看到当前 repo 的真实 loader 成功记录，所以只能先记为待补证据。"
            next_action = "补齐 required files，并重新运行 loader probe；只有 claim_ready=true 且 loader_probe_success=true 时才会升级为可用数据。"
    elif not available:
        status_label = "未找到可审计数据"
        summary = f"{name} 目前没有可被 TASTE 证明存在且可加载的数据文件。"
        explanation = "TASTE 没有找到完整本地路径，也没有 loader probe 成功记录，因此不能把它当作可用数据。"
        next_action = "补齐数据文件后重新运行 dataset registry/probe；在此之前不要进入论文证据链。"
    else:
        status_label = "未通过 claim-ready 检查"
        summary = f"{name} 虽然有部分记录，但还没有通过当前 active repo 的 loader probe。"
        explanation = "TASTE 需要看到真实 loader 成功、split/metric 可复现、实验产物可审计，才会把数据升级为 claim-ready。"
        next_action = "重新运行 probe 并检查失败日志；如果格式不兼容，应回到 repo 选择或数据转换阶段。"

    summary_en, explanation_en, next_action_en = _dataset_messages_en(
        row,
        name,
        status_label,
        active_repo,
        required,
        missing,
        claim_ready,
    )
    _set_i18n(row, "status_label", status_label, DATASET_STATUS_EN.get(status_label, status_label))
    _set_i18n(row, "human_summary", summary, summary_en)
    _set_i18n(row, "blocking_explanation", explanation, explanation_en)
    _set_i18n(row, "next_action", next_action, next_action_en)
    _set_list_i18n(row, "evidence", evidence, evidence_en)
    return row


def _repo_human_fields(row: dict[str, Any], root: Path, blocked_dataset_count: int = 0, ready_dataset_count: int = 0) -> dict[str, Any]:
    name = str(row.get("name") or "候选 repo")
    active = bool(row.get("active"))
    execution_ready = bool(row.get("execution_ready", False))
    missing_topics = _items(row.get("missing_topic_groups"))
    support_signals = _items(row.get("support_signals"))
    url = str(row.get("url") or "")
    evidence: list[str] = []
    if url:
        evidence.append(f"来源：{url}")
    if row.get("score") != "":
        evidence.append(f"repo reuse score：{row.get('score')}；bucket：{row.get('bucket') or '未分组'}")
    if support_signals:
        evidence.append(f"本地支持信号：{_text_list(support_signals)}")
    if missing_topics:
        evidence.append(f"主题缺口：{_text_list(missing_topics)}")
    if row.get("notes"):
        evidence.append(f"候选池备注：{row.get('notes')}")
    evidence_en = _repo_evidence_en(row, url, support_signals, missing_topics)

    if active and execution_ready:
        if ready_dataset_count:
            status_label = "当前路线：代码和真实数据已通过"
            summary = f"{name} 是当前 active repo。TASTE 已确认它有训练入口、数据目录，并且已有 {ready_dataset_count} 个真实数据集通过当前 repo 的 loader probe。"
            data_note = f"另有 {blocked_dataset_count} 个候选数据集未通过文件/loader 检查，系统会把它们作为缺口记录，不会用于论文 claim。" if blocked_dataset_count else "当前没有记录到数据阻塞。"
            topic_note = f"它仍有 {_text_list(missing_topics)} 主题缺口；后续实验迭代必须在这个可跑通基线之上补齐项目目标创新，并用真实实验验证。" if missing_topics else "主题覆盖暂未发现明显缺口。"
            explanation = f"{summary} {data_note} {topic_note}"
            next_action = "进入真实 smoke test 和实验迭代：只使用 claim-ready 数据集产生证据，同时继续记录 bad-case、metrics 和可复现实验配置。"
        else:
            status_label = "当前路线：代码环境可用，但数据未过门"
            summary = f"{name} 是当前 active repo。TASTE 已把它作为主路线，并且代码/环境层面通过了初步检查；真正卡住的是数据门，而不是 conda 或 repo 安装。"
            data_note = f"当前仍有 {blocked_dataset_count} 个数据项没有通过 claim-ready 检查，所需文件/loader 证据尚未就位。" if blocked_dataset_count else "当前没有记录到数据阻塞。"
            topic_note = f"另外，它仍缺少明确的 {_text_list(missing_topics)} 项目目标组件，所以后续方法设计需要补上这部分创新，而不是直接宣称已有 repo 已解决完整项目目标。" if missing_topics else "主题覆盖暂未发现明显缺口。"
            explanation = f"{summary} {data_note} {topic_note}"
            next_action = "优先补齐 active repo 的真实数据并跑通 loader；数据通过后，再让实验迭代模块在该 repo 上实现项目目标所需的候选改动。"
    elif active:
        status_label = "当前路线需要复核"
        summary = f"{name} 被选为 active repo，但候选池记录还没有显示它完全 execution-ready。"
        explanation = "这通常意味着候选池是早期 GitHub 搜索快照，而 active_repo 是后续本地审计结果；需要以 active_repo 和实际 loader probe 为准。"
        next_action = "重新运行 repo audit，同步 repo_candidates 与 active_repo 的执行状态，避免网页显示 needs-check 与 active 冲突。"
    elif not execution_ready:
        status_label = "候选 repo，尚未本地验证"
        summary = f"{name} 目前只是调研/搜索阶段找到的候选 repo，还没有通过本地执行审计，不能直接拿来替换 active repo。"
        gap = f"它还缺少 {_text_list(missing_topics)} 主题证据。" if missing_topics else "主题关键词看起来相关，但还需要检查 README、安装方式、训练入口和数据下载。"
        explanation = f"{summary} {gap} TASTE 需要先确认它是否能安装、是否有训练/评估入口、是否有可获得的数据，再决定是否切换路线。"
        next_action = "如果 active repo 数据长期无法解决，可把它加入下一轮 repo audit；但在 audit 通过前，不要把它标成可执行路线。"
    else:
        status_label = "可继续审计的候选"
        summary = f"{name} 有一定可复用潜力，但还不是当前主路线。"
        explanation = "它需要和 active repo 在数据可得性、方法贴合度、复现实验成本上做对比后才能切换。"
        next_action = "运行 local repo audit、dataset probe 和 smoke test，再由 evidence gate 决定是否替换 active repo。"

    summary_en, explanation_en, next_action_en = _repo_messages_en(
        row,
        name,
        active,
        execution_ready,
        missing_topics,
        blocked_dataset_count,
        ready_dataset_count,
    )
    _set_i18n(row, "status_label", status_label, REPO_STATUS_EN.get(status_label, status_label))
    _set_i18n(row, "human_summary", summary, summary_en)
    _set_i18n(row, "blocking_explanation", explanation, explanation_en)
    _set_i18n(row, "next_action", next_action, next_action_en)
    _set_list_i18n(row, "evidence", evidence, evidence_en)
    return row


def _repo_details(root: Path) -> list[dict[str, Any]]:
    rows = _read_json(root / "state" / "repo_candidates.json", [])
    active = _read_json(root / "state" / "active_repo.json", {})
    data_req = _read_json(root / "state" / "repo_data_requirements.json", {})
    dataset_rows = _dataset_details(root)
    ready_dataset_count = len([row for row in dataset_rows if _dataset_is_claim_ready(row)])
    blocked_dataset_count = len([row for row in dataset_rows if not _dataset_is_claim_ready(row) and (row.get("missing_required_files") or row.get("available") is False or row.get("policy_decision"))])
    active_path = str(active.get("repo_path") or "") if isinstance(active, dict) else ""
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        local_path = str(row.get("local_path") or "")
        is_active = bool(active_path and local_path == active_path)
        missing = row.get("missing_topic_groups", [])
        signals = row.get("repo_support_signals", row.get("support_signals", []))
        execution_ready = bool(row.get("repo_execution_ready", row.get("execution_ready", False)))
        notes = row.get("notes", "")
        score = row.get("score", row.get("repo_reuse_score", ""))
        bucket = row.get("repo_selection_bucket", row.get("selection_bucket", ""))
        if is_active and isinstance(active, dict):
            execution_ready = bool(active.get("repo_execution_ready", execution_ready))
            active_signals = active.get("repo_support_signals", [])
            if isinstance(active_signals, list) and active_signals:
                signals = active_signals
            score = active.get("repo_reuse_score", score)
            bucket = active.get("selection_bucket", bucket)
        if not execution_ready and not notes:
            notes = "repo candidate is not execution-ready yet; inspect clone, dependencies, dataset loaders, and smoke-test logs"
        item = {
            "name": row.get("name", ""),
            "url": row.get("url", ""),
            "local_path": local_path,
            "score": score,
            "bucket": bucket,
            "execution_ready": execution_ready,
            "support_signals": signals if isinstance(signals, list) else [],
            "missing_topic_groups": missing if isinstance(missing, list) else [],
            "notes": notes,
            "active": is_active,
        }
        out.append(_repo_human_fields(item, root, blocked_dataset_count, ready_dataset_count))
    return out


def _dataset_details(root: Path) -> list[dict[str, Any]]:
    rows = _read_json(root / "state" / "dataset_registry.json", [])
    reasons = _dataset_block_reasons(root)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("dataset") or "").strip()
        if not name:
            continue
        merged = {**row, **reasons.get(name, {})}
        merged.setdefault("dataset", name)
        merged.setdefault("name", name)
        if not merged.get("reason"):
            if merged.get("available") and not merged.get("claim_ready", True):
                merged["reason"] = "available for probing but not claim-ready"
            elif not merged.get("available"):
                merged["reason"] = "dataset files unavailable or not audited"
            else:
                merged["reason"] = "ready"
        out.append(_dataset_human_fields(merged, root))
        seen.add(name)
    for name, row in reasons.items():
        if name not in seen:
            merged = {"name": name, "dataset": name, **row}
            if not merged.get("reason"):
                merged["reason"] = "dataset appears in requirements/policy but not in registry"
            out.append(_dataset_human_fields(merged, root))
    return out



TRAJECTORY_TEXT_ZH = {
    "assurance_blocked": "证据门控阻塞",
    "repair_or_explore": "修复或探索",
    "explore_or_deepen": "探索或深化",
    "blocked": "阻塞",
    "warn": "警告",
    "pass": "通过",
    "Resolve evidence blockers before claiming or promoting paper output.": "在声明结论或推广论文产物前，先解决证据阻塞。",
    "Use Claude Code on the highest-priority failed/repair node, then rerun the validation trial in the same trajectory.": "让 Claude Code 处理最高优先级的失败/修复节点，然后在同一轨迹里重新运行验证实验。",
    "Select one unexplored niche with loader-ready data and runnable repo evidence for the next bounded experiment.": "选择一个同时具备 loader-ready 数据和可运行 repo 证据的未探索 niche，作为下一轮有界实验。",
    "Resolve current-route provenance/embedding evidence or a proposal-only candidate base-switch route before exploring new niches.": "在探索新 niche 前，先补当前路线 provenance/embedding 证据，或提供 proposal-only candidate base-switch route。",
    "Resolve selected-base semantic provenance before exploring new niches or downstream experiments.": "在探索新 niche 或下游实验前，先解决 selected-base 语义 provenance 门控。",
    "Use Claude Code only on gate evidence repair nodes; do not launch behavior-only candidate experiments until the gate clears.": "Claude Code 只能处理 gate 证据修复节点；gate 通过前不要启动纯行为候选实验。",
    "Keep unexplored-niche experiments deferred until current-route provenance/base-switch gate input changes.": "当前路线 provenance/base-switch gate 输入改变前，继续暂缓未探索 niche 实验。",
    "Keep method deepening deferred until selected-base provenance/base-switch gates pass.": "selected-base provenance/base-switch gate 通过前，继续暂缓方法深化。",
    "Deepen only elite methods that pass ARIS review and assurance checks.": "只深化通过 TASTE 证据保障检查的精英方法。",
    "Deepen only elite methods that pass TASTE evidence-assurance checks.": "只深化通过 TASTE 证据保障检查的精英方法。",
    "Use EvoScientist cycle trace and recoverable memory before starting the next experiment.": "开始下一轮实验前，先使用 TASTE 可恢复周期轨迹和长期记忆。",
    "Use TASTE recoverable cycle trace and long-horizon memory before starting the next experiment.": "开始下一轮实验前，先使用 TASTE 可恢复周期轨迹和长期记忆。",
    "Route Claude Code through local TASTE skills for experiment-loop, evidence-gate, and writing work.": "让 Claude Code 按本地 TASTE skills 执行实验循环、证据门控和论文生产。",
    "Route Claude Code through local TASTE skills for experiment-loop, evidence-gate, and paper-production work.": "让 Claude Code 按本地 TASTE skills 执行实验循环、证据门控和论文生产。",
    "landscape_cartographer": "研究版图绘制员",
    "evolutionary_memory_curator": "进化记忆维护员",
    "aris_assurance_reviewer": "TASTE 证据保障员",
    "evoscientist_executor": "TASTE 轨迹实验执行员",
    "paperorchestra_scribe": "TASTE 论文生产控制员",
    "evidence_assurance_reviewer": "TASTE 证据保障员",
    "trajectory_experiment_executor": "TASTE 轨迹实验执行员",
    "paper_production_controller": "TASTE 论文生产控制员",
    "skill_bound_claude_executor": "技能约束的 Claude 执行员",
    "Maintain research landscape, novelty map, failed hypothesis graph, and unexplored niche graph.": "维护 research landscape、novelty map、failed hypothesis graph 与 unexplored niche graph。",
    "Persist ideation, experimentation, assurance, and trajectory memory to disk.": "把 ideation、experimentation、assurance 与 trajectory memory 持久化落盘。",
    "Use local Claude skills as executable contracts for experiment-loop, evidence-gate, and writing work.": "把本地 Claude skills 作为实验循环、证据门控和论文生产的可执行约束。",
    "Use local Claude skills as executable contracts for experiment-loop, evidence-gate, and paper-production work.": "把本地 Claude skills 作为实验循环、证据门控和论文生产的可执行约束。",
    "Reject unsupported claims and require local evidence paths.": "拒绝无证据支撑的结论，并要求本地证据路径。",
    "Run plan -> execute -> evaluate -> repair loops with bounded retries.": "执行 plan -> execute -> evaluate -> repair 的有界重试循环。",
    "Only write claims after evidence gates pass; otherwise keep paper at draft/preview.": "只有证据门控通过后才写入结论；否则论文保持草稿/预览状态。",
    "No audit-ready experiment on loader-ready real data exists.": "尚不存在基于真实可加载数据且审计就绪的实验。",
    "No audit-ready experiment exists.": "尚不存在审计就绪实验。",
    "Paper evidence audit recommends hold-markdown-only.": "论文证据审计建议保持草稿状态，暂不推广最终论文产物。",
    "ready": "就绪",
    "waiting_for_queue": "等待队列",
    "completed": "已完成",
    "dry_run_recorded": "已记录干跑轮次",
    "queue_exhausted": "队列已耗尽",
    "stopped_passed": "已通过并停止",
    "refresh_failed": "刷新失败",
    "claude_failed": "Claude 执行失败",
    "memory_extended": "记忆已延展",
    "operational_but_research_gate_blocked": "能力已具备，但研究证据门控仍阻塞",
    "operational_with_warnings": "能力可运行但有警告",
    "operational": "可运行",
    "capability_blocked": "能力阻塞",
    "research_direction_management": "研究方向管理",
    "evolutionary_memory": "进化记忆",
    "research_assurance_layer": "研究证据保障层",
    "trajectory_system": "轨迹系统",
    "skill_and_code_bindings": "skills/代码绑定",
    "trajectory_supervisor": "轨迹主控",
    "refresh_state": "刷新状态",
    "select_queue_item": "选择队列项",
    "delegate_to_claude_code": "委派给 Claude Code",
    "validate_and_rebuild": "验证并重建",
    "continue_or_stop": "继续或停止",
    "Continuously optimize the research trajectory through evidence-gated Claude Code calls, validation, memory updates, and checkpoint comparison.": "通过证据门控的 Claude Code 调用、验证、记忆更新和检查点对比，持续优化 TASTE 研究轨迹。",
    "Run build_research_trajectory_system.py and read evolutionary_memory_index before selecting work.": "选择任务前先运行 build_research_trajectory_system.py，并读取 evolutionary_memory_index。",
    "Select the highest-priority non-completed P0/P1/P2 trajectory queue item; prefer assurance/evidence blockers over new exploration.": "选择最高优先级且尚未完成的 P0/P1/P2 轨迹队列项；优先处理保障/证据阻塞，再做新探索。",
    "Call the persistent Claude Code project session with the queue objective, local skill contract, evidence inputs, success checks, and queued web guidance.": "携带队列目标、本地 skill 合约、证据输入、成功检查和网页排队引导，调用持久化 Claude Code 项目会话。",
    "Re-run audits/builders after Claude returns; compare checkpoint deltas and never treat text-only claims as scientific evidence.": "Claude 返回后重新运行审计/构建脚本，对比检查点变化，并且绝不把纯文本结论当作科学证据。",
    "Continue until gates pass, the queue is exhausted, bounded rounds are used, or a real missing-resource blocker is recorded.": "持续迭代，直到门控通过、队列耗尽、有界轮次用完，或记录真实缺资源阻塞。",
    "Issue is removed from research_assurance_layer or downgraded with new local evidence.": "问题已从 research_assurance_layer 移除，或已用新的本地证据降级。",
    "No paper conclusion is promoted before the gate passes.": "门控通过前，不推广任何论文结论。",
    "A rerun produces an audit-ready registry entry or a prune decision with evidence.": "重跑产生审计就绪注册记录，或产生带证据的剪枝决定。",
    "Recoverable exception memory is updated.": "可恢复异常记忆已更新。",
    "A concrete repo/data/metric plan is recorded before execution.": "执行前已记录具体 repo/data/metric 计划。",
    "The experiment result updates novelty, failed, and memory graphs.": "实验结果会更新新颖性、失败假设和记忆图。",
    "research_evidence_integrity.status becomes pass or remaining warnings are explicitly justified.": "research_evidence_integrity.status 变为 pass，或剩余警告已被明确说明。",
    "stagnant": "停滞",
    "active": "活跃",
    "Claim ledger contains weak or unsupported claims: headline_claim, scope_boundary, minimal_evidence_contract": "Claim ledger 仍包含弱证据或无支撑声明：headline_claim、scope_boundary、minimal_evidence_contract",
    "Landscape appears unchanged across repeated builds; prioritize literature/repo refresh or document why search is exhausted.": "研究版图连续多次构建未变化；应优先刷新文献/仓库搜索，或记录搜索已穷尽的理由。",
    "research_graph_history_file": "研究图谱历史文件存在",
    "research_graph_history_append": "研究图谱历史已追加",
    "landscape_assessment_file": "研究版图评估文件存在",
    "landscape_assessment_status": "研究版图评估状态存在",
    "evolutionary_memory_ledger_file": "进化记忆 ledger 文件存在",
    "evolutionary_memory_ledger_append": "进化记忆 ledger 已追加",
    "evidence_manifest_file": "证据 manifest 文件存在",
    "evidence_manifest_has_refs": "证据 manifest 包含引用",
    "weak_claims_are_blocked": "弱/无支撑 claim 会被门控阻塞",
    "protocol_requires_graph_history_and_manifest": "协议要求读取图谱历史和证据 manifest",
    "trajectory_builder_maintains_graph_history_and_manifest": "轨迹构建器维护图谱历史和证据 manifest",
    "This audit checks infrastructure and evidence discipline. A blocked research gate is a correct refusal to overclaim, not a reason to weaken evidence standards.": "该审计检查基础能力与证据纪律。研究证据门控被阻塞，表示 TASTE 正确拒绝过度声明，并不是降低证据标准的理由。",
    "research_landscape_file": "研究版图文件存在",
    "research_landscape_has_nodes": "研究版图包含节点",
    "novelty_map_file": "新颖性图文件存在",
    "novelty_map_maintained": "新颖性图已维护",
    "failed_hypothesis_graph_file": "失败假设图文件存在",
    "failed_hypothesis_graph_maintained": "失败假设图已维护",
    "unexplored_niche_graph_file": "未探索 niche 图文件存在",
    "unexplored_niche_graph_maintained": "未探索 niche 图已维护",
    "direction_memory_history": "方向记忆历史已落盘",
    "research_memory_file": "研究记忆文件存在",
    "ideation_memory_persisted": "想法记忆已落盘",
    "experimentation_memory_persisted": "实验记忆已落盘",
    "assurance_memory_persisted": "证据保障记忆已落盘",
    "trajectory_memory_persisted": "轨迹记忆已落盘",
    "evolutionary_index_file": "进化索引文件存在",
    "evolutionary_index_has_items": "进化索引包含条目",
    "evoscientist_cycle_summary": "TASTE 可恢复周期摘要存在",
    "recoverable_cycle_summary": "TASTE 可恢复周期摘要存在",
    "recoverable_memory_available": "可恢复记忆可用",
    "assurance_layer_file": "研究证据保障层文件存在",
    "assurance_layer_has_principles": "证据保障原则已记录",
    "assurance_issues_are_explicit": "证据问题显式记录",
    "evidence_integrity_file": "证据完整性文件存在",
    "evidence_integrity_checked_nodes": "证据完整性已检查节点",
    "paper_evidence_audit_present": "论文证据审计存在",
    "aris_review_board_present": "TASTE 证据评审板存在",
    "evidence_review_board_present": "TASTE 证据评审板存在",
    "execution_protocol_file": "轨迹执行协议文件存在",
    "execution_protocol_has_loop_steps": "轨迹执行协议包含循环步骤",
    "trajectory_supervisor_entrypoint": "轨迹主控入口存在",
    "optimization_plan_file": "轨迹优化计划文件存在",
    "optimization_queue_available": "轨迹优化队列可用",
    "trajectory_checkpoints_file": "轨迹检查点文件存在",
    "trajectory_checkpoints_append": "轨迹检查点可追加",
    "supervisor_state_file": "主控状态文件存在",
    "supervisor_has_round_history": "主控已有轮次历史",
    "skill_evidence-gate": "evidence-gate skill 存在",
    "skill_experiment-loop": "experiment-loop skill 存在",
    "skill_writing": "writing module 存在",
    "skill_contracts_exported": "skill 合约已导出",
    "ARIS:audit_paper_evidence.py": "TASTE 论文证据审计脚本存在",
    "ARIS:build_aris_review_board.py": "TASTE 证据评审板脚本存在",
    "EvoScientist:run_autoscientist_supervisor.py": "TASTE 自主科研主控脚本存在",
    "EvoScientist:run_evoscientist_style_cycle.py": "TASTE 可恢复周期脚本存在",
    "EvoScientist:run_research_trajectory_supervisor.py": "TASTE 研究轨迹主控脚本存在",
    "EvoScientist:update_evolution_memory.py": "TASTE 进化记忆更新脚本存在",
    "writing:audit_paper_evidence.py": "论文阶段证据审计脚本存在",
    "writing:build_paper_md.py": "论文阶段 Markdown 构建脚本存在",
    "writing:revise_paper_md.py": "论文阶段修订脚本存在",
    "writing:run_paper_pipeline.py": "论文阶段流水线脚本存在",
    "end_to_end_verification": "端到端验证",
    "persistent_state_files": "持久化状态文件",
    "research_direction_management_e2e": "研究方向管理端到端验证",
    "evolutionary_memory_e2e": "进化记忆端到端验证",
    "research_assurance_layer_e2e": "研究证据保障层端到端验证",
    "trajectory_system_e2e": "轨迹系统端到端验证",
    "skills_and_prompt_context_e2e": "skills 与提示上下文端到端验证",
    "third_party_research_stack": "内置方法契约",
    "third_party_stack_e2e": "方法契约端到端验证",
    "web_visibility_e2e": "网页可见性端到端验证",
    "required_state_files_exist": "必需状态文件存在",
    "landscape_nodes_available": "研究版图节点可用",
    "novelty_map_list": "新颖性图为列表结构",
    "failed_hypothesis_list": "失败假设图为列表结构",
    "unexplored_niche_list": "未探索 niche 图为列表结构",
    "graph_history_has_entries": "图谱历史包含条目",
    "graph_history_has_hash": "图谱历史包含快照 hash",
    "landscape_assessment_recorded": "研究版图评估已记录",
    "direction_memory_persisted": "方向记忆已持久化",
    "novelty_failed_niche_graphs_maintained": "新颖性/失败假设/未探索 niche 图已维护",
    "memory_ledger_has_entries": "记忆 ledger 包含条目",
    "memory_ledger_counts_memory_families": "记忆 ledger 覆盖所有记忆族",
    "evoscientist_cycle_has_phases": "TASTE 可恢复周期包含阶段",
    "weak_claims_block_assurance": "弱声明会阻塞证据保障",
    "missing_local_refs_not_promoted": "缺失本地引用不会被推广",
    "claim_ledger_weak_claims_blocked": "claim ledger 弱声明被阻塞",
    "execution_protocol_loop": "执行协议包含循环",
    "worker_contract_reads_long_horizon_state": "worker 合约读取长期状态",
    "checkpoint_history_available": "检查点历史可用",
    "dry_run_does_not_complete_queue": "干跑不会完成队列",
    "supervisor_multi_round_entrypoint": "主控支持多轮入口",
    "long_running_claude_timeout_supported": "Claude 长时间运行超时策略可用",
    "skill_contracts_exist": "skill 合约存在",
    "skill_contracts_rich": "skill 合约足够完整",
    "claude_prompt_reads_long_horizon_assets": "Claude prompt 读取长期资产",
    "coding_agent_reads_trajectory_assets": "代码 agent 读取轨迹资产",
    "api_exposes_verification": "API 暴露端到端验证",
    "ui_renders_verification": "前端渲染端到端验证",
    "third_party_stack_file": "内置方法契约状态文件存在",
    "third_party_stack_report": "内置方法契约报告存在",
    "third_party_stack_ready": "内置方法契约已就绪",
    "third_party_sources_cover_required_repos": "第三方来源覆盖必需仓库",
    "third_party_modules_selected": "第三方模块已选取",
    "third_party_skill_adapters_synced": "第三方 skill adapter 已同步",
    "third_party_skill_adapters_exported": "第三方 skill adapter 已导出",
    "third_party_prompt_context_bound": "第三方栈已绑定到 Claude prompt",
    "third_party_trajectory_builder_bound": "第三方栈已绑定到轨迹构建器",
    "third_party_web_api_bound": "第三方栈已绑定到 API",
    "third_party_web_ui_bound": "第三方栈已绑定到前端",
}

CAPABILITY_METRIC_ZH = {
    "landscape_nodes": "研究版图节点",
    "novelty_nodes": "新颖性节点",
    "failed_hypotheses": "失败假设",
    "unexplored_niches": "未探索 niche",
    "direction_memory_entries": "方向记忆条目",
    "ideation_entries": "想法记忆条目",
    "experimentation_entries": "实验记忆条目",
    "assurance_entries": "证据保障记忆条目",
    "trajectory_entries": "轨迹记忆条目",
    "evolutionary_index_items": "进化索引条目",
    "graph_history_entries": "图谱历史条目",
    "evolutionary_memory_ledger_entries": "进化记忆 ledger 条目",
    "evidence_manifest_ref_count": "证据 manifest 引用数",
    "weak_or_unsupported_claim_count": "弱/无支撑 claim 数",
    "recoverable_exception_entries": "可恢复异常条目",
    "assurance_issues": "证据保障问题",
    "integrity_issues": "证据完整性问题",
    "checked_nodes": "已检查节点",
    "loop_steps": "循环步骤",
    "optimization_queue_size": "优化队列大小",
    "checkpoint_count": "检查点数量",
    "supervisor_rounds": "主控轮次",
    "latest_supervisor_status": "最近主控状态",
    "required_skills": "必需 skills",
    "available_required_skills": "可用必需 skills",
    "exported_skill_contracts": "已导出 skill 合约",
    "required_files": "必需文件数",
    "missing_files": "缺失文件数",
    "missing_local_refs": "缺失本地引用数",
    "evo_phase_count": "可恢复周期阶段数",
    "latest_supervisor_status": "最近主控状态",
    "skill_contract_chars": "skill 合约字符数",
    "verification_modules": "验证模块数",
    "verification_total_checks": "验证检查总数",
    "verification_failed_checks": "失败检查数",
    "verification_warning_checks": "警告检查数",
    "verification_overall_status": "验证总体状态",
    "total_checks": "检查总数",
    "passed_checks": "通过检查数",
    "warning_checks": "警告检查数",
    "failed_checks": "失败检查数",
    "modules_checked": "已检查模块数",
    "source_count": "来源数量",
    "available_source_count": "可用来源数量",
    "selected_module_count": "选中模块数量",
    "missing_module_count": "缺失模块数量",
    "synced_skill_count": "同步 skill 数量",
    "adapter_count": "适配器数量",
    "external_module_count": "外部模块数量",
}


def _trajectory_i18n_text(value: Any, lang: str = "zh") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if lang == "zh":
        if text in TRAJECTORY_TEXT_ZH:
            return TRAJECTORY_TEXT_ZH[text]
        template_prefixes = [
            ("Resolve or truthfully document evidence issue: ", "解决或如实记录证据问题："),
            ("Repair, retry, or prune failed hypothesis: ", "修复、重试或剪枝失败假设："),
            ("Turn unexplored niche into one bounded experiment: ", "把未探索 niche 转成一次有界实验："),
            ("Repair trajectory nodes that lack auditable evidence references or point at missing local files.", "修复缺少可审计证据引用、或指向缺失本地文件的轨迹节点。"),
            ("Refresh landscape and propose the next evidence-feasible trajectory branch.", "刷新研究版图，并提出下一条证据可行的轨迹分支。"),
        ]
        for prefix, zh_prefix in template_prefixes:
            if text == prefix:
                return zh_prefix.rstrip("：")
            if text.startswith(prefix):
                suffix = text[len(prefix):].strip()
                return zh_prefix + _trajectory_i18n_text(suffix, "zh")
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
            return text
    return text


def _trajectory_i18n_field(row: dict[str, Any], key: str) -> None:
    value = str(row.get(key) or "").strip()
    row[f"{key}_zh"] = _trajectory_i18n_text(value, "zh")
    row[f"{key}_en"] = value
    row[f"{key}_i18n"] = {"zh": row[f"{key}_zh"], "en": row[f"{key}_en"]}


def _trajectory_i18n_list_field(row: dict[str, Any], key: str) -> None:
    values = row.get(key, [])
    if not isinstance(values, list):
        values = [values] if values else []
    en_values = [str(item) for item in values if str(item).strip()]
    zh_values = [_trajectory_i18n_text(item, "zh") for item in en_values]
    row[f"{key}_zh"] = zh_values
    row[f"{key}_en"] = en_values
    row[f"{key}_i18n"] = {"zh": zh_values, "en": en_values}


def _trajectory_summary(root: Path) -> dict[str, Any]:
    payload = _read_json(root / "state" / "research_trajectory_system.json", {})
    memory = _read_json(root / "state" / "research_memory.json", {})
    if not isinstance(payload, dict) or not payload:
        return {}
    summary = payload.get("summary", {}) if isinstance(payload.get("summary", {}), dict) else {}
    controller = payload.get("trajectory_controller", {}) if isinstance(payload.get("trajectory_controller", {}), dict) else {}
    assurance = payload.get("assurance_layer", {}) if isinstance(payload.get("assurance_layer", {}), dict) else {}
    landscape = payload.get("research_landscape", {}) if isinstance(payload.get("research_landscape", {}), dict) else {}
    novelty = payload.get("novelty_map", {}) if isinstance(payload.get("novelty_map", {}), dict) else {}
    failed = payload.get("failed_hypothesis_graph", {}) if isinstance(payload.get("failed_hypothesis_graph", {}), dict) else {}
    niches = payload.get("unexplored_niche_graph", {}) if isinstance(payload.get("unexplored_niche_graph", {}), dict) else {}
    evo_cycle = payload.get("recoverable_cycle", payload.get("evoscientist_cycle", {})) if isinstance(payload.get("recoverable_cycle", payload.get("evoscientist_cycle", {})), dict) else {}
    skill_contracts = payload.get("skill_contracts", []) if isinstance(payload.get("skill_contracts", []), list) else []
    direction_memory = payload.get("direction_memory", {}) if isinstance(payload.get("direction_memory", {}), dict) else {}
    evidence_integrity = payload.get("research_evidence_integrity", {}) if isinstance(payload.get("research_evidence_integrity", {}), dict) else _read_json(root / "state" / "research_evidence_integrity.json", {})
    evidence_manifest = payload.get("research_evidence_manifest", {}) if isinstance(payload.get("research_evidence_manifest", {}), dict) else _read_json(root / "state" / "research_evidence_manifest.json", {})
    graph_history = payload.get("research_graph_history", {}) if isinstance(payload.get("research_graph_history", {}), dict) else _read_json(root / "state" / "research_graph_history.json", {})
    landscape_assessment = payload.get("research_landscape_assessment", {}) if isinstance(payload.get("research_landscape_assessment", {}), dict) else _read_json(root / "state" / "research_landscape_assessment.json", {})
    memory_ledger = payload.get("evolutionary_memory_ledger", {}) if isinstance(payload.get("evolutionary_memory_ledger", {}), dict) else _read_json(root / "state" / "evolutionary_memory_ledger.json", {})
    optimization_plan = payload.get("trajectory_optimization_plan", {}) if isinstance(payload.get("trajectory_optimization_plan", {}), dict) else _read_json(root / "state" / "trajectory_optimization_plan.json", {})
    trajectory_checkpoints = payload.get("trajectory_checkpoints", {}) if isinstance(payload.get("trajectory_checkpoints", {}), dict) else _read_json(root / "state" / "trajectory_checkpoints.json", {})
    evolutionary_index = payload.get("evolutionary_memory_index", {}) if isinstance(payload.get("evolutionary_memory_index", {}), dict) else _read_json(root / "state" / "evolutionary_memory_index.json", {})
    execution_protocol = payload.get("trajectory_execution_protocol", {}) if isinstance(payload.get("trajectory_execution_protocol", {}), dict) else _read_json(root / "state" / "trajectory_execution_protocol.json", {})
    supervisor_state = _read_json(root / "state" / "trajectory_supervisor_state.json", {})
    capability_audit = payload.get("research_trajectory_capability_audit", {}) if isinstance(payload.get("research_trajectory_capability_audit", {}), dict) else _read_json(root / "state" / "research_trajectory_capability_audit.json", {})
    end_to_end_verification = payload.get("research_trajectory_end_to_end_verification", {}) if isinstance(payload.get("research_trajectory_end_to_end_verification", {}), dict) else _read_json(root / "state" / "research_trajectory_end_to_end_verification.json", {})
    third_party_stack = payload.get("third_party_research_stack", {}) if isinstance(payload.get("third_party_research_stack", {}), dict) else _read_json(root / "state" / "third_party_research_stack.json", {})
    blocker_action_plan = payload.get("blocker_action_plan", {}) if isinstance(payload.get("blocker_action_plan", {}), dict) else _read_json(root / "state" / "blocker_action_plan.json", {})
    if isinstance(supervisor_state, dict) and isinstance(supervisor_state.get("latest", {}), dict):
        latest = dict(supervisor_state.get("latest", {}))
        latest["status_i18n"] = {"zh": _trajectory_i18n_text(latest.get("status", ""), "zh"), "en": str(latest.get("status", "")).replace("_", " ")}
        latest["checkpoint_delta_i18n"] = {"zh": _trajectory_i18n_text(latest.get("checkpoint_delta", ""), "zh"), "en": str(latest.get("checkpoint_delta", "")).replace("_", " ")}
        if isinstance(latest.get("queue_item", {}), dict):
            queue_item = dict(latest.get("queue_item", {}))
            _trajectory_i18n_field(queue_item, "objective")
            _trajectory_i18n_field(queue_item, "owner_role")
            _trajectory_i18n_list_field(queue_item, "success_checks")
            latest["queue_item"] = queue_item
        supervisor_state = dict(supervisor_state)
        supervisor_state["latest"] = latest
    memory_summary = summary.get("memory", {}) if isinstance(summary.get("memory", {}), dict) else {}
    landscape_assessment = dict(landscape_assessment) if isinstance(landscape_assessment, dict) else {}
    landscape_assessment["status_i18n"] = {"zh": _trajectory_i18n_text(landscape_assessment.get("status", ""), "zh"), "en": str(landscape_assessment.get("status", "")).replace("_", " ")}
    landscape_assessment["risk_notes_i18n"] = {"zh": [_trajectory_i18n_text(item, "zh") for item in landscape_assessment.get("risk_notes", []) if str(item).strip()] if isinstance(landscape_assessment.get("risk_notes", []), list) else [], "en": [str(item) for item in landscape_assessment.get("risk_notes", []) if str(item).strip()] if isinstance(landscape_assessment.get("risk_notes", []), list) else []}
    protocol = dict(execution_protocol) if isinstance(execution_protocol, dict) else {}
    protocol["status_i18n"] = {"zh": _trajectory_i18n_text(protocol.get("status", ""), "zh"), "en": str(protocol.get("status", "")).replace("_", " ")}
    main_agent = dict(protocol.get("main_agent", {})) if isinstance(protocol.get("main_agent", {}), dict) else {}
    if main_agent:
        _trajectory_i18n_field(main_agent, "role")
        _trajectory_i18n_field(main_agent, "goal")
        protocol["main_agent"] = main_agent
    protocol_steps = []
    for step in protocol.get("loop_steps", []) if isinstance(protocol.get("loop_steps", []), list) else []:
        if not isinstance(step, dict):
            continue
        row = dict(step)
        _trajectory_i18n_field(row, "step")
        _trajectory_i18n_field(row, "action")
        protocol_steps.append(row)
    protocol["loop_steps"] = protocol_steps
    capability = dict(capability_audit) if isinstance(capability_audit, dict) else {}
    capability["overall_status_i18n"] = {"zh": _trajectory_i18n_text(capability.get("overall_status", ""), "zh"), "en": str(capability.get("overall_status", "")).replace("_", " ")}
    capability["capability_status_i18n"] = {"zh": _trajectory_i18n_text(capability.get("capability_status", ""), "zh"), "en": str(capability.get("capability_status", "")).replace("_", " ")}
    _trajectory_i18n_field(capability, "principle")
    module_rows = []
    for module in capability.get("modules", []) if isinstance(capability.get("modules", []), list) else []:
        if not isinstance(module, dict):
            continue
        row = dict(module)
        row["id"] = str(row.get("id") or row.get("module") or "")
        _trajectory_i18n_field(row, "module")
        row["status_i18n"] = {"zh": _trajectory_i18n_text(row.get("status", ""), "zh"), "en": str(row.get("status", "")).replace("_", " ")}
        metric_rows = []
        metrics = row.get("metrics", {}) if isinstance(row.get("metrics", {}), dict) else {}
        for key, value in metrics.items():
            key_text = str(key)
            metric_rows.append({
                "key": key_text,
                "label_i18n": {"zh": CAPABILITY_METRIC_ZH.get(key_text, _trajectory_i18n_text(key_text, "zh")), "en": key_text.replace("_", " ")},
                "value": value,
                "value_i18n": {"zh": _trajectory_i18n_text(value, "zh"), "en": str(value).replace("_", " ")},
            })
        row["metric_rows"] = metric_rows
        check_rows = []
        for check_row in row.get("checks", []) if isinstance(row.get("checks", []), list) else []:
            if not isinstance(check_row, dict):
                continue
            check_copy = dict(check_row)
            check_copy["id"] = str(check_copy.get("id") or check_copy.get("name") or "")
            _trajectory_i18n_field(check_copy, "name")
            check_copy["status_i18n"] = {"zh": _trajectory_i18n_text(check_copy.get("status", ""), "zh"), "en": str(check_copy.get("status", "")).replace("_", " ")}
            check_copy["severity_i18n"] = {"zh": _trajectory_i18n_text(check_copy.get("severity", ""), "zh"), "en": str(check_copy.get("severity", "")).replace("_", " ")}
            check_rows.append(check_copy)
        row["checks"] = check_rows
        module_rows.append(row)
    capability["modules"] = module_rows
    verification = dict(end_to_end_verification) if isinstance(end_to_end_verification, dict) else {}
    verification["overall_status_i18n"] = {"zh": _trajectory_i18n_text(verification.get("overall_status", ""), "zh"), "en": str(verification.get("overall_status", "")).replace("_", " ")}
    verification["capability_status_i18n"] = {"zh": _trajectory_i18n_text(verification.get("capability_status", ""), "zh"), "en": str(verification.get("capability_status", "")).replace("_", " ")}
    _trajectory_i18n_field(verification, "principle")
    verification_modules = []
    for module in verification.get("modules", []) if isinstance(verification.get("modules", []), list) else []:
        if not isinstance(module, dict):
            continue
        row = dict(module)
        row["id"] = str(row.get("id") or row.get("module") or "")
        _trajectory_i18n_field(row, "module")
        row["status_i18n"] = {"zh": _trajectory_i18n_text(row.get("status", ""), "zh"), "en": str(row.get("status", "")).replace("_", " ")}
        metric_rows = []
        metrics = row.get("metrics", {}) if isinstance(row.get("metrics", {}), dict) else {}
        for key, value in metrics.items():
            key_text = str(key)
            metric_rows.append({
                "key": key_text,
                "label_i18n": {"zh": CAPABILITY_METRIC_ZH.get(key_text, _trajectory_i18n_text(key_text, "zh")), "en": key_text.replace("_", " ")},
                "value": value,
                "value_i18n": {"zh": _trajectory_i18n_text(value, "zh"), "en": str(value).replace("_", " ")},
            })
        row["metric_rows"] = metric_rows
        check_rows = []
        for check_row in row.get("checks", []) if isinstance(row.get("checks", []), list) else []:
            if not isinstance(check_row, dict):
                continue
            check_copy = dict(check_row)
            check_copy["id"] = str(check_copy.get("id") or check_copy.get("name") or "")
            _trajectory_i18n_field(check_copy, "name")
            check_copy["status_i18n"] = {"zh": _trajectory_i18n_text(check_copy.get("status", ""), "zh"), "en": str(check_copy.get("status", "")).replace("_", " ")}
            check_copy["severity_i18n"] = {"zh": _trajectory_i18n_text(check_copy.get("severity", ""), "zh"), "en": str(check_copy.get("severity", "")).replace("_", " ")}
            check_rows.append(check_copy)
        row["checks"] = check_rows
        verification_modules.append(row)
    verification["modules"] = verification_modules
    third_party = dict(third_party_stack) if isinstance(third_party_stack, dict) else {}
    third_party_summary = third_party.get("summary", {}) if isinstance(third_party.get("summary", {}), dict) else {}
    third_party["status_i18n"] = {"zh": _trajectory_i18n_text(third_party.get("status", ""), "zh"), "en": str(third_party.get("status", "")).replace("_", " ")}
    source_rows = []
    for source in third_party.get("sources", []) if isinstance(third_party.get("sources", []), list) else []:
        if not isinstance(source, dict):
            continue
        source_copy = dict(source)
        source_copy["status_i18n"] = {"zh": "可用" if source_copy.get("available") else "缺失", "en": "available" if source_copy.get("available") else "missing"}
        source_rows.append(source_copy)
    third_party["sources"] = source_rows
    third_party["synced_skill_adapters"] = [dict(row) for row in third_party.get("synced_skill_adapters", []) if isinstance(row, dict)] if isinstance(third_party.get("synced_skill_adapters", []), list) else []
    blocker_action_plan = dict(blocker_action_plan) if isinstance(blocker_action_plan, dict) else {}
    blocker_action_rows = [dict(row) for row in blocker_action_plan.get("actions", [])[:12] if isinstance(row, dict)] if isinstance(blocker_action_plan.get("actions", []), list) else []
    for row in blocker_action_rows:
        _trajectory_i18n_field(row, "issue")
        _trajectory_i18n_field(row, "repair_strategy")
        _trajectory_i18n_list_field(row, "success_checks")
    blocker_action_plan["actions"] = blocker_action_rows
    if isinstance(memory, dict):
        memory_summary = {
            **memory_summary,
            "ideation_entries": len(memory.get("ideation_memory", [])) if isinstance(memory.get("ideation_memory", []), list) else memory_summary.get("ideation_entries", 0),
            "experimentation_entries": len(memory.get("experimentation_memory", [])) if isinstance(memory.get("experimentation_memory", []), list) else memory_summary.get("experimentation_entries", 0),
            "assurance_entries": len(memory.get("assurance_memory", [])) if isinstance(memory.get("assurance_memory", []), list) else memory_summary.get("assurance_entries", 0),
            "trajectory_entries": len(memory.get("trajectory_memory", [])) if isinstance(memory.get("trajectory_memory", []), list) else memory_summary.get("trajectory_entries", 0),
            "direction_entries": summary.get("direction_memory_entries", direction_memory.get("entries", memory_summary.get("direction_entries", 0))),
        }
    objectives = []
    for item in controller.get("next_objectives", summary.get("next_objectives", [])) or []:
        row = {"text": str(item)}
        _trajectory_i18n_field(row, "text")
        objectives.append(row)
    roles = []
    for item in controller.get("agent_roles", []) or []:
        if not isinstance(item, dict):
            continue
        row = {"role": str(item.get("role") or ""), "responsibility": str(item.get("responsibility") or "")}
        _trajectory_i18n_field(row, "role")
        _trajectory_i18n_field(row, "responsibility")
        roles.append(row)
    queue_rows = []
    for item in optimization_plan.get("queue", []) if isinstance(optimization_plan.get("queue", []), list) else []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        _trajectory_i18n_field(row, "objective")
        _trajectory_i18n_field(row, "owner_role")
        _trajectory_i18n_list_field(row, "success_checks")
        queue_rows.append(row)
    issues = []
    for item in assurance.get("issues", []) or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        _trajectory_i18n_field(row, "issue")
        issues.append(row)
    method_bindings = []
    for binding in third_party.get("capability_bindings", []) if isinstance(third_party.get("capability_bindings", []), list) else []:
        if not isinstance(binding, dict):
            continue
        method_bindings.append({
            "capability": binding.get("capability", ""),
            "uses": binding.get("uses", []),
            "status": binding.get("status", "active"),
        })
    method_summary_zh = "已内置到 主流程" if third_party.get("status") == "ready" else "等待同步方法契约"
    method_summary_en = "fused into research workflow" if third_party.get("status") == "ready" else "waiting for method-contract sync"
    phase = str(summary.get("phase") or controller.get("phase") or "")
    assurance_status = str(summary.get("assurance_status") or assurance.get("status") or "")
    result = {
        "updated_at": payload.get("updated_at", ""),
        "phase": phase,
        "phase_i18n": {"zh": _trajectory_i18n_text(phase, "zh"), "en": phase.replace("_", " ")},
        "assurance_status": assurance_status,
        "assurance_status_i18n": {"zh": _trajectory_i18n_text(assurance_status, "zh"), "en": assurance_status.replace("_", " ")},
        "landscape_nodes": summary.get("landscape_nodes", sum(len(v) for v in landscape.get("nodes", {}).values()) if isinstance(landscape.get("nodes"), dict) else 0),
        "novelty_nodes": summary.get("novelty_nodes", len(novelty.get("nodes", [])) if isinstance(novelty.get("nodes", []), list) else 0),
        "failed_hypotheses": summary.get("failed_hypotheses", len(failed.get("nodes", [])) if isinstance(failed.get("nodes", []), list) else 0),
        "unexplored_niches": summary.get("unexplored_niches", len(niches.get("nodes", [])) if isinstance(niches.get("nodes", []), list) else 0),
        "assurance_issue_count": summary.get("assurance_issue_count", len(issues)),
        "evo_phase_count": summary.get("evo_phase_count", evo_cycle.get("phase_count", 0)),
        "recoverable_exception_count": summary.get("recoverable_exception_count", evo_cycle.get("recoverable_exception_count", 0)),
        "skill_contract_count": summary.get("skill_contract_count", len(skill_contracts)),
        "third_party_stack_status": summary.get("third_party_stack_status", third_party.get("status", "")),
        "third_party_stack_status_i18n": third_party.get("status_i18n", {}),
        "third_party_source_count": summary.get("third_party_source_count", third_party_summary.get("source_count", 0)),
        "third_party_selected_module_count": summary.get("third_party_selected_module_count", third_party_summary.get("selected_module_count", 0)),
        "third_party_synced_skill_count": summary.get("third_party_synced_skill_count", third_party_summary.get("synced_skill_count", 0)),
        "direction_memory_entries": summary.get("direction_memory_entries", direction_memory.get("entries", 0)),
        "evidence_integrity_status": summary.get("evidence_integrity_status", evidence_integrity.get("status", "")),
        "evidence_integrity_status_i18n": {"zh": _trajectory_i18n_text(summary.get("evidence_integrity_status", evidence_integrity.get("status", "")), "zh"), "en": str(summary.get("evidence_integrity_status", evidence_integrity.get("status", ""))).replace("_", " ")},
        "evidence_integrity_issue_count": summary.get("evidence_integrity_issue_count", len(evidence_integrity.get("issues", [])) if isinstance(evidence_integrity.get("issues", []), list) else 0),
        "evidence_integrity_score": summary.get("evidence_integrity_score", evidence_integrity.get("score", 0)),
        "optimization_queue_size": summary.get("optimization_queue_size", optimization_plan.get("queue_size", len(queue_rows))),
        "highest_optimization_priority": summary.get("highest_optimization_priority", optimization_plan.get("highest_priority", "")),
        "blocker_action_plan": blocker_action_plan,
        "blocker_action_plan_summary": blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) else {},
        "blocker_action_rows": blocker_action_rows,
        "trajectory_checkpoint_count": summary.get("trajectory_checkpoint_count", trajectory_checkpoints.get("checkpoint_count", 0)),
        "trajectory_delta_status": summary.get("trajectory_delta_status", (trajectory_checkpoints.get("latest", {}) if isinstance(trajectory_checkpoints.get("latest", {}), dict) else {}).get("delta_status", "")),
        "trajectory_delta_status_i18n": {"zh": _trajectory_i18n_text(summary.get("trajectory_delta_status", (trajectory_checkpoints.get("latest", {}) if isinstance(trajectory_checkpoints.get("latest", {}), dict) else {}).get("delta_status", "")), "zh"), "en": str(summary.get("trajectory_delta_status", (trajectory_checkpoints.get("latest", {}) if isinstance(trajectory_checkpoints.get("latest", {}), dict) else {}).get("delta_status", ""))).replace("_", " ")},
        "evolutionary_index_items": summary.get("evolutionary_index_items", evolutionary_index.get("indexed_item_count", 0)),
        "graph_history_entries": summary.get("graph_history_entries", graph_history.get("history_count", 0)),
        "evolutionary_memory_ledger_entries": summary.get("evolutionary_memory_ledger_entries", memory_ledger.get("history_count", 0)),
        "landscape_assessment_status": summary.get("landscape_assessment_status", landscape_assessment.get("status", "")),
        "landscape_assessment_status_i18n": {"zh": _trajectory_i18n_text(summary.get("landscape_assessment_status", landscape_assessment.get("status", "")), "zh"), "en": str(summary.get("landscape_assessment_status", landscape_assessment.get("status", ""))).replace("_", " ")},
        "evidence_manifest_ref_count": summary.get("evidence_manifest_ref_count", evidence_manifest.get("ref_count", 0)),
        "weak_or_unsupported_claim_count": summary.get("weak_or_unsupported_claim_count", len(evidence_manifest.get("weak_or_unsupported_claims", [])) if isinstance(evidence_manifest.get("weak_or_unsupported_claims", []), list) else 0),
        "research_graph_history": graph_history,
        "research_landscape_assessment": landscape_assessment,
        "research_evidence_manifest": evidence_manifest,
        "evolutionary_memory_ledger": memory_ledger,
        "recoverable_cycle": evo_cycle,
        "evoscientist_cycle": evo_cycle,
        "skill_contracts": skill_contracts,
        "direction_memory": direction_memory,
        "evidence_integrity": evidence_integrity,
        "optimization_plan": optimization_plan,
        "trajectory_checkpoints": trajectory_checkpoints,
        "evolutionary_memory_index": evolutionary_index,
        "trajectory_execution_protocol": protocol,
        "trajectory_supervisor_state": supervisor_state,
        "trajectory_supervisor_status": supervisor_state.get("status", "") if isinstance(supervisor_state, dict) else "",
        "trajectory_supervisor_rounds": len(supervisor_state.get("rounds", [])) if isinstance(supervisor_state, dict) and isinstance(supervisor_state.get("rounds", []), list) else 0,
        "trajectory_supervisor_latest": supervisor_state.get("latest", {}) if isinstance(supervisor_state, dict) else {},
        "research_trajectory_capability_audit": capability,
        "capability_audit_status": capability.get("overall_status", "") if isinstance(capability, dict) else "",
        "capability_status": capability.get("capability_status", "") if isinstance(capability, dict) else "",
        "capability_audit_status_i18n": capability.get("overall_status_i18n", {}) if isinstance(capability, dict) else {},
        "capability_status_i18n": capability.get("capability_status_i18n", {}) if isinstance(capability, dict) else {},
        "research_trajectory_end_to_end_verification": verification,
        "end_to_end_verification_status": verification.get("overall_status", "") if isinstance(verification, dict) else "",
        "end_to_end_verification_capability_status": verification.get("capability_status", "") if isinstance(verification, dict) else "",
        "end_to_end_verification_status_i18n": verification.get("overall_status_i18n", {}) if isinstance(verification, dict) else {},
        "end_to_end_verification_capability_status_i18n": verification.get("capability_status_i18n", {}) if isinstance(verification, dict) else {},
        "end_to_end_verification_failed_checks": verification.get("failed_checks", 0) if isinstance(verification, dict) else 0,
        "end_to_end_verification_warning_checks": verification.get("warning_checks", 0) if isinstance(verification, dict) else 0,
        "end_to_end_verification_total_checks": verification.get("total_checks", 0) if isinstance(verification, dict) else 0,
        "third_party_research_stack": third_party,
        "integrated_method_contracts": {
            "status": third_party.get("status", ""),
            "status_i18n": {"zh": method_summary_zh, "en": method_summary_en},
            "source_count": summary.get("third_party_source_count", third_party_summary.get("source_count", 0)),
            "binding_count": len(method_bindings),
            "skill_adapter_count": summary.get("third_party_synced_skill_count", third_party_summary.get("synced_skill_count", 0)),
            "bindings": method_bindings,
            "report": third_party.get("report", str(root / "reports" / "third_party_research_stack.md")),
        },
        "optimization_queue": queue_rows,
        "next_objectives": objectives,
        "agent_roles": roles,
        "assurance_issues": issues,
        "memory": memory_summary,
        "files": {
            "trajectory": str(root / "state" / "research_trajectory_system.json"),
            "memory": str(root / "state" / "research_memory.json"),
            "landscape": str(root / "state" / "research_landscape.json"),
            "novelty_map": str(root / "state" / "novelty_map.json"),
            "failed_hypothesis_graph": str(root / "state" / "failed_hypothesis_graph.json"),
            "unexplored_niche_graph": str(root / "state" / "unexplored_niche_graph.json"),
            "assurance_layer": str(root / "state" / "research_assurance_layer.json"),
            "direction_memory": str(root / "state" / "research_direction_memory.json"),
            "evidence_integrity": str(root / "state" / "research_evidence_integrity.json"),
            "evidence_manifest": str(root / "state" / "research_evidence_manifest.json"),
            "graph_history": str(root / "state" / "research_graph_history.json"),
            "landscape_assessment": str(root / "state" / "research_landscape_assessment.json"),
            "evolutionary_memory_ledger": str(root / "state" / "evolutionary_memory_ledger.json"),
            "optimization_plan": str(root / "state" / "trajectory_optimization_plan.json"),
            "blocker_action_plan": str(root / "state" / "blocker_action_plan.json"),
            "blocker_action_plan_report": str(root / "reports" / "blocker_action_plan.md"),
            "trajectory_checkpoints": str(root / "state" / "trajectory_checkpoints.json"),
            "evolutionary_memory_index": str(root / "state" / "evolutionary_memory_index.json"),
            "trajectory_execution_protocol": str(root / "state" / "trajectory_execution_protocol.json"),
            "trajectory_supervisor_state": str(root / "state" / "trajectory_supervisor_state.json"),
            "research_trajectory_capability_audit": str(root / "state" / "research_trajectory_capability_audit.json"),
            "research_trajectory_end_to_end_verification": str(root / "state" / "research_trajectory_end_to_end_verification.json"),
            "third_party_research_stack": str(root / "state" / "third_party_research_stack.json"),
            "third_party_research_stack_report": str(root / "reports" / "third_party_research_stack.md"),
            "recoverable_cycle": str(root / "state" / "recoverable_cycle_summary.json"),
            "skill_contracts": str(root / "state" / "research_skill_contracts.json"),
            "report": str(root / "reports" / "research_trajectory_system.md"),
        },
    }
    result["summary_i18n"] = {
        "zh": f"当前阶段：{result['phase_i18n']['zh']}；证据保障：{result['assurance_status_i18n']['zh']}；下一步队列 {result['optimization_queue_size']} 项；最近轨迹变化：{result['trajectory_delta_status_i18n']['zh']}；方法契约{method_summary_zh}。",
        "en": f"Current phase: {result['phase_i18n']['en']}; assurance: {result['assurance_status_i18n']['en']}; next-action queue has {result['optimization_queue_size']} item(s); latest trajectory delta: {result['trajectory_delta_status_i18n']['en']}; method contracts are {method_summary_en}.",
    }
    return result

def _latest_coding_backend(root: Path) -> str:
    files = sorted((root / "state").glob("coding_agent_*.json"), key=lambda item: item.stat().st_mtime)
    for path in reversed(files):
        payload = _read_json(path, {})
        if isinstance(payload, dict) and payload.get("backend"):
            return str(payload.get("backend"))
    return ""


def _active_repo_path(root: Path) -> str:
    selection = _read_json(root / "state" / "evidence_ready_repo_selection.json", {})
    if isinstance(selection, dict):
        selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
        decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
        base_switch_gate = _read_json(root / "state" / "base_switch_gate.json", {})
        base_switch_execution = _read_json(root / "state" / "base_switch_execution.json", {})
        deterministic_switch_accepted = bool(
            isinstance(base_switch_gate, dict)
            and base_switch_gate.get("status") == "pass"
            and base_switch_gate.get("decision") == "authorize_base_switch"
            and base_switch_gate.get("switch_authorized") is True
            and isinstance(base_switch_execution, dict)
            and str(base_switch_execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
        )
        accepted = str(selection.get("selection_gate") or "").startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")) or deterministic_switch_accepted or bool(decision.get("accept_as_current_best"))
        if accepted:
            for key in ["repo_path", "local_path", "path"]:
                value = str(selected.get(key) or "").strip()
                if value:
                    return value
    active = _read_json(root / "state" / "active_repo.json", {})
    if isinstance(active, dict) and active.get("repo_path"):
        return str(active.get("repo_path"))
    plan = _read_json(root / "state" / "parallel_plan.json", {})
    methods = plan.get("methods", []) if isinstance(plan, dict) else plan if isinstance(plan, list) else []
    for method in methods:
        if isinstance(method, dict) and method.get("repo_path"):
            return str(method.get("repo_path"))
    rows = _read_json(root / "state" / "repo_candidates.json", [])
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("local_path"):
            return str(row.get("local_path"))
    return ""


def _active_env_name(root: Path, cfg: dict[str, Any]) -> str:
    bootstrap = _read_json(root / "state" / "repo_env_bootstrap.json", {})
    if isinstance(bootstrap, dict) and bootstrap.get("env_name"):
        return str(bootstrap.get("env_name"))
    return str(cfg.get("conda_env", "")) if isinstance(cfg, dict) else ""


def _read_metric_payload(path_text: str) -> dict[str, Any]:
    if not path_text:
        return {}
    path = Path(path_text)
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def _scalmetrics(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            continue
        out[str(key)] = value
    return out


def _experiment_rows(root: Path) -> list[dict[str, Any]]:
    rows = _json_rows(_read_json(root / "state" / "experiment_registry.json", []))
    out: list[dict[str, Any]] = []
    for row in rows[-80:]:
        if not isinstance(row, dict):
            continue
        metrics_path = str(row.get("metrics_path", "") or "")
        if not metrics_path and row.get("artifact_path"):
            candidate_metrics = Path(str(row.get("artifact_path"))) / "metrics.json"
            if candidate_metrics.exists():
                metrics_path = str(candidate_metrics)
        metrics = _read_metric_payload(metrics_path)
        loss_curve = []
        for key in ["loss_curve", "loss", "losses", "train_loss", "history"]:
            value = metrics.get(key)
            if isinstance(value, list):
                loss_curve = value[:300]
                break
        row_metrics = _scalmetrics(row.get("metrics"))
        file_metrics = _scalmetrics(metrics)
        combined_metrics = {**file_metrics, **row_metrics}
        metric_name = str(row.get("metric_name") or row.get("metric") or "").strip()
        metric_value = row.get("metric_value")
        if metric_value is None or metric_value == "":
            metric_value = row.get("result") or ""
        if metric_name and metric_name not in combined_metrics and metric_value != "":
            combined_metrics[metric_name] = metric_value
        metric_rows = [
            {"key": key, "value": value}
            for key, value in combined_metrics.items()
            if str(key).strip() and key not in {"loss_curve", "losses", "history"}
        ]
        out.append({
            "experiment_id": row.get("experiment_id") or row.get("name") or "",
            "method": row.get("method") or row.get("method_slug") or "",
            "dataset": row.get("dataset", ""),
            "benchmark": row.get("benchmark", ""),
            "timestamp": row.get("timestamp", ""),
            "started_at": row.get("started_at", ""),
            "finished_at": row.get("finished_at", ""),
            "metric": metric_name,
            "metric_value": metric_value,
            "metric_rows": metric_rows,
            "status": row.get("status", ""),
            "audit_ready": bool(row.get("audit_ready", False)),
            "claim_verdict": row.get("claim_verdict", ""),
            "duration_sec": row.get("duration_sec", ""),
            "artifact_path": row.get("artifact_path", ""),
            "metrics_path": metrics_path,
            "bad_case_path": row.get("bad_case_path", ""),
            "audit_path": row.get("audit_path", ""),
            "notes": row.get("notes", ""),
            "metrics": metrics,
            "loss_curve": loss_curve,
        })
    return out


def _latest_pdf(root: Path) -> Path | None:
    pdfs = sorted((root / "paper" / "output").glob("**/*.pdf"), key=lambda item: item.stat().st_mtime)
    return pdfs[-1] if pdfs else None


def _file_info(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists() or not path.is_file():
        return {}
    try:
        stat = path.stat()
        return {
            "path": str(path),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mtime_iso": __import__("datetime").datetime.fromtimestamp(stat.st_mtime, __import__("datetime").timezone.utc).isoformat(),
        }
    except Exception:
        return {"path": str(path), "exists": True}


def _latest_paper_tex(root: Path) -> Path | None:
    texs = sorted((root / "paper" / "output").glob("**/paper.tex"), key=lambda item: item.stat().st_mtime)
    return texs[-1] if texs else None


def _venue_output_artifact(root: Path, paper_state: dict[str, Any], filename: str) -> Path | None:
    if not isinstance(paper_state, dict):
        return None
    venue_slug = str(paper_state.get("venue_slug") or paper_state.get("active_venue") or "").strip()
    if not venue_slug:
        return None
    path = root / "paper" / "output" / venue_slug / filename
    return path if path.exists() and path.is_file() else None


def _paper_asset_url(root: Path, path: Path | None) -> str:
    if not path:
        return ""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        return ""
    return f"/api/projects/{root.name}/files/{rel.as_posix()}"


def _project_file_url(root: Path, path: Path | None) -> str:
    if not path:
        return ""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        return ""
    return f"/api/projects/{root.name}/files/{rel.as_posix()}"


def _process_alive_for_experiment_sync(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    return Path(f"/proc/{value}").exists()


def _artifact_import_status(root: Path, artifact: Path) -> str:
    contract = _read_json(artifact / "run_contract.json", {})
    if isinstance(contract, dict):
        status = str(contract.get("registry_import_status") or "").strip()
        if status:
            return status
    if (artifact / "audit.json").exists():
        return "audit_exists"
    if (artifact / "failure_audit.json").exists():
        return "failure_audit_exists"
    registry = _json_rows(_read_json(root / "state" / "experiment_registry.json", []))
    artifact_text = str(artifact.resolve())
    for row in registry:
        if isinstance(row, dict) and str(row.get("artifact_path") or "") == artifact_text:
            return "registry_row_exists"
    return "missing"


def _sync_running_experiment_artifacts(root: Path, *, force: bool = False) -> dict[str, Any]:
    """Import TASTE-launcher training artifacts into the experiment registry/table.

    This is a control-plane refresh only: it parses run_contract/stdout logs and
    records completed, failed, or live progress rows. It never promotes paper
    claims or writes pass gates.
    """
    now = time.monotonic()
    cached = _EXPERIMENT_SYNC_CACHE.get(root.name)
    if not force and cached and cached[0] > now:
        return dict(cached[1])

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        _EXPERIMENT_SYNC_CACHE[root.name] = (time.monotonic() + EXPERIMENT_SYNC_TTL_SEC, payload)
        return payload

    artifact_dirs: list[Path] = []
    seen: set[str] = set()
    try:
        sidecars = list((root / "artifacts").glob("**/run_contract.json")) + list((root / "artifacts").glob("**/launcher.pid.json"))
    except Exception:
        sidecars = []
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - 14 * 24 * 3600
    for sidecar in sidecars:
        try:
            if sidecar.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        payload = _read_json(sidecar, {})
        if not isinstance(payload, dict):
            continue
        command_text = _command_text(payload.get("command") or payload.get("command_display"))
        if not _looks_like_experiment_training_cmd(command_text):
            continue
        artifact_value = payload.get("artifact_dir") or sidecar.parent
        artifact = Path(str(artifact_value)).expanduser()
        if not artifact.is_absolute():
            artifact = (root / artifact).resolve()
        try:
            artifact = artifact.resolve()
            artifact.relative_to((root / "artifacts").resolve())
        except Exception:
            continue
        if not artifact.exists() or not artifact.is_dir():
            continue
        if (artifact / "CONTAMINATED_DO_NOT_IMPORT.txt").exists() or (artifact / "FAILED_DO_NOT_IMPORT.txt").exists():
            continue
        import_status = _artifact_import_status(root, artifact)
        alive = _process_alive_for_experiment_sync(payload.get("pid"))
        contract_status = str(payload.get("status") or "").lower()
        needs_import = alive or import_status == "missing" or contract_status in {"running", "incomplete", "failed"}
        if not needs_import:
            continue
        key = str(artifact)
        if key in seen:
            continue
        seen.add(key)
        artifact_dirs.append(artifact)
    if not artifact_dirs:
        return finish({"status": "idle", "artifact_dirs": [], "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    commands: list[list[str]] = []
    importer = SCRIPTS / "import_experiment_artifacts.py"
    if importer.exists():
        for artifact in artifact_dirs:
            commands.append([sys.executable, str(importer), "--project", root.name, "--artifact-dir", str(artifact), "--allow-incomplete"])
    builder = SCRIPTS / "build_experiment_record_table.py"
    if builder.exists():
        commands.append([sys.executable, str(builder), "--project", root.name])
    audit = SCRIPTS / "audit_experiment_iteration.py"
    if audit.exists():
        commands.append([sys.executable, str(audit), "--project", root.name])
    results: list[dict[str, Any]] = []
    for command in commands:
        try:
            proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=90)
            results.append({
                "cmd": " ".join(str(part) for part in command),
                "return_code": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-1200:],
                "stderr_tail": (proc.stderr or "")[-1200:],
            })
        except Exception as exc:
            results.append({"cmd": " ".join(str(part) for part in command), "return_code": 125, "error": str(exc)})
    failed = [row for row in results if int(row.get("return_code") or 0) not in {0, 1, 2}]
    return finish({
        "status": "error" if failed else "synced",
        "artifact_dirs": [str(path) for path in artifact_dirs],
        "commands": results,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })


def _refresh_experiment_record_table(root: Path, *, sync_running: bool = True) -> dict[str, Any]:
    sync_result = _sync_running_experiment_artifacts(root) if sync_running else {"status": "compact_read_only", "artifact_dirs": [], "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    registry_path = root / "state" / "experiment_registry.json"
    record_path = root / "state" / "experiment_record_table.json"
    if not sync_running:
        payload = _read_json(record_path, {}) if record_path.exists() else {}
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["running_experiment_sync"] = sync_result
            return payload
        return {"running_experiment_sync": sync_result}
    if not registry_path.exists():
        return _read_json(record_path, {}) if record_path.exists() else {}
    needs_refresh = not record_path.exists()
    if not needs_refresh:
        try:
            needs_refresh = registry_path.stat().st_mtime > record_path.stat().st_mtime
        except OSError:
            needs_refresh = True
    if needs_refresh:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_experiment_record_table.py"), "--project", root.name],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            payload = _read_json(record_path, {}) if record_path.exists() else {}
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["refresh_error"] = (proc.stderr or proc.stdout or "experiment record refresh failed").strip()
                return payload
            return {"refresh_error": (proc.stderr or proc.stdout or "experiment record refresh failed").strip()}
    payload = _read_json(record_path, {})
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["running_experiment_sync"] = sync_result
        return payload
    return {"running_experiment_sync": sync_result}


def _experiment_record_table(root: Path, *, sync_running: bool = True) -> dict[str, Any]:
    payload = _refresh_experiment_record_table(root, sync_running=sync_running)
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    columns = payload.get("columns", []) if isinstance(payload, dict) else []
    csv_path = root / "experiments" / "experiment_records.csv"
    report_path = root / "experiments" / "实验记录.md"
    json_path = root / "state" / "experiment_record_table.json"
    return {
        "updated_at": payload.get("updated_at", "") if isinstance(payload, dict) else "",
        "row_count": payload.get("row_count", len(rows) if isinstance(rows, list) else 0) if isinstance(payload, dict) else 0,
        "columns": columns if isinstance(columns, list) else [],
        "rows": rows if isinstance(rows, list) else [],
        "csv_path": str(csv_path) if csv_path.exists() else "",
        "csv_url": _project_file_url(root, csv_path) if csv_path.exists() else "",
        "report_path": str(report_path) if report_path.exists() else "",
        "report_url": _project_file_url(root, report_path) if report_path.exists() else "",
        "json_path": str(json_path) if json_path.exists() else "",
        "json_url": _project_file_url(root, json_path) if json_path.exists() else "",
        "source": payload.get("source", str(root / "state" / "experiment_registry.json")) if isinstance(payload, dict) else "",
        "refresh_error": payload.get("refresh_error", "") if isinstance(payload, dict) else "",
        "running_experiment_sync": payload.get("running_experiment_sync", {}) if isinstance(payload, dict) else {},
    }


def _current_route_experiment_rows(rows: Any, repo_name: Any = "", repo_path: Any = "") -> list[dict[str, Any]]:
    """Return experiment/record rows that belong to the current selected base.

    Historical registries are still retained for audit/download, but compact UI
    counters should not mix old routes into the active selected-base summary.
    """
    source_rows = rows if isinstance(rows, list) else []
    tokens: set[str] = set()

    def add_token(value: Any) -> None:
        raw = str(value or "").strip().lower()
        if not raw:
            return
        for item in {raw, raw.replace("_", "-"), raw.replace("-", "_")}:
            if len(item) >= 3:
                tokens.add(item)

    repo_name_text = str(repo_name or "").strip()
    add_token(repo_name_text)
    if "/" in repo_name_text:
        add_token(repo_name_text.rsplit("/", 1)[-1])
    repo_path_text = str(repo_path or "").strip()
    add_token(repo_path_text)
    if repo_path_text:
        add_token(Path(repo_path_text).name)
    if not tokens:
        return []

    route_keys = [
        "repo", "repo_path", "artifact_path", "audit_path", "metrics_path", "command",
        "experiment_id", "name", "method", "method_slug", "notes",
        "仓库", "证据路径", "关键配置/命令", "实验ID", "方法/变体",
    ]
    out: list[dict[str, Any]] = []
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        parts: list[str] = []
        for key in route_keys:
            value = row.get(key)
            if isinstance(value, (str, int, float, bool)):
                parts.append(str(value))
        haystack = " ".join(parts).lower()
        if any(token and token in haystack for token in tokens):
            out.append(row)
    return out


def _completed_experiment_count(rows: Any) -> int:
    return len([
        row for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and str(row.get("status") or "").lower() in {"completed", "success", "repaired"}
    ])


def _audit_ready_completed_experiment_count(rows: Any) -> int:
    return len([
        row for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and str(row.get("status") or "").lower() in {"completed", "success", "repaired"}
        and bool(row.get("audit_ready"))
    ])


def _row_dataset_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("dataset") or row.get("数据集") or row.get("benchmark") or "").strip()


def _has_synthetic_dataset(rows: Any) -> bool:
    for row in rows if isinstance(rows, list) else []:
        name = _row_dataset_name(row).lower()
        if name.startswith("synthetic") or "synthetic" in name or "合成" in name:
            return True
    return False


def _row_is_real_dataset(row: Any) -> bool:
    name = _row_dataset_name(row).lower()
    return bool(name and not (name.startswith("synthetic") or "synthetic" in name or "合成" in name))


def _completed_or_live_real_experiment_count(rows: Any) -> int:
    count = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not _row_is_real_dataset(row):
            continue
        status = str(row.get("status") or "").lower()
        if status in {"completed", "success", "repaired", "running", "in_progress", "launched"}:
            count += 1
    return count


def _synthetic_smoke_warning_required(rows: Any, record_rows: Any) -> bool:
    rows_l = rows if isinstance(rows, list) else []
    record_l = record_rows if isinstance(record_rows, list) else []
    # The warning is a current-summary flag, not a history flag. Historical
    # synthetic smoke rows stay in the audit table, but they should not make the
    # active experiment summary look synthetic once real selected-route runs exist.
    has_synthetic = _has_synthetic_dataset(rows_l) or _has_synthetic_dataset(record_l)
    has_real_current = bool(_completed_or_live_real_experiment_count(rows_l) or _completed_or_live_real_experiment_count(record_l))
    return bool(has_synthetic and not has_real_current)


def _experiment_summary_display_flags(rows: Any, record_rows: Any) -> dict[str, Any]:
    rows_l = rows if isinstance(rows, list) else []
    record_l = record_rows if isinstance(record_rows, list) else []
    audit_ready_completed = _audit_ready_completed_experiment_count(rows_l)
    running_count = len([row for row in rows_l if isinstance(row, dict) and str(row.get("status") or "").lower() in {"running", "in_progress", "launched"}])
    return {
        "show_experiment_summary_count": bool(audit_ready_completed or running_count),
        "show_synthetic_smoke_warning": _synthetic_smoke_warning_required(rows_l, record_l),
        "audit_ready_completed_experiment_count": audit_ready_completed,
        "running_experiment_count": running_count,
    }


def _paper_path_matches_current_venue(root: Path, paper_state: dict[str, Any], path: Path) -> bool:
    slug = str((paper_state or {}).get("venue_slug") or (paper_state or {}).get("active_venue") or "").strip().lower()
    if not slug:
        return True
    try:
        rel_parts = path.resolve().relative_to(root.resolve()).parts
    except Exception:
        return False
    if len(rel_parts) >= 3 and rel_parts[0] == "paper" and rel_parts[1] in {"output", "orchestra"}:
        return rel_parts[2].lower() == slug
    return True


def _paper_state_path(root: Path, paper_state: dict[str, Any], *keys: str) -> Path | None:
    for key in keys:
        value = str(paper_state.get(key) or "").strip() if isinstance(paper_state, dict) else ""
        if not value:
            continue
        path = Path(value)
        if path.exists() and path.is_file():
            try:
                path.resolve().relative_to(root.resolve())
            except Exception:
                continue
            if not _paper_path_matches_current_venue(root, paper_state, path):
                continue
            return path
    return None


def _paper_regeneration_requested(paper_state: dict[str, Any]) -> bool:
    if not isinstance(paper_state, dict):
        return False
    return bool(
        paper_state.get("paper_current_regeneration_requested")
    )


def _paper_regeneration_running(paper_state: dict[str, Any]) -> bool:
    if not isinstance(paper_state, dict):
        return False
    return bool(
        _paper_regeneration_requested(paper_state)
        and str(paper_state.get("paper_orchestra_bridge_status") or "") in {"running", "prepared"}
        and not paper_state.get("paper_orchestra_pdf_generated")
    )


def _paper_regeneration_failed(paper_state: dict[str, Any]) -> bool:
    if not isinstance(paper_state, dict):
        return False
    status = str(paper_state.get("paper_orchestra_bridge_status") or "")
    return bool(
        _paper_regeneration_requested(paper_state)
        and status not in {"generated", "running", "prepared"}
        and not paper_state.get("paper_orchestra_pdf_generated")
    )


def _live_science_gate_status(root: Path) -> dict[str, Any]:
    reference_gate = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    progress_gate = _read_json(root / "state" / "scientific_progress_gate.json", {})
    iteration_audit = _read_json(root / "state" / "experiment_iteration_audit.json", {})
    blockers: list[str] = []

    reference_ready = bool(
        isinstance(reference_gate, dict)
        and reference_gate.get("status") == "pass"
        and reference_gate.get("decision") == "continue_base"
    )
    if not reference_ready:
        gate_blockers = reference_gate.get("blockers", []) if isinstance(reference_gate, dict) and isinstance(reference_gate.get("blockers", []), list) else []
        status = reference_gate.get("status") if isinstance(reference_gate, dict) else "missing"
        decision = reference_gate.get("decision") if isinstance(reference_gate, dict) else "missing"
        detail = "; ".join(str(item) for item in gate_blockers[:3]) or f"status={status}; decision={decision}"
        blockers.append("reference reproduction gate blocked: " + detail)

    progress_ready = bool(isinstance(progress_gate, dict) and progress_gate.get("status") == "pass")
    if not progress_ready:
        gate_blockers = progress_gate.get("blockers", []) if isinstance(progress_gate, dict) and isinstance(progress_gate.get("blockers", []), list) else []
        status = progress_gate.get("status") if isinstance(progress_gate, dict) else "missing"
        detail = "; ".join(str(item) for item in gate_blockers[:3]) or f"status={status}"
        blockers.append("scientific progress gate blocked: " + detail)

    iteration_ready = bool(isinstance(iteration_audit, dict) and iteration_audit.get("status") == "pass")
    if not iteration_ready:
        audit_blockers = iteration_audit.get("blockers", []) if isinstance(iteration_audit, dict) and isinstance(iteration_audit.get("blockers", []), list) else []
        audit_warnings = iteration_audit.get("warnings", []) if isinstance(iteration_audit, dict) and isinstance(iteration_audit.get("warnings", []), list) else []
        status = iteration_audit.get("status") if isinstance(iteration_audit, dict) else "missing"
        detail = "; ".join(str(item) for item in (audit_blockers + audit_warnings)[:3]) or f"status={status}"
        blockers.append("experiment trajectory audit blocked: " + detail)

    return {
        "ok": bool(reference_ready and progress_ready and iteration_ready),
        "blockers": blockers,
        "reference_reproduction_gate": reference_gate if isinstance(reference_gate, dict) else {},
        "scientific_progress_gate": progress_gate if isinstance(progress_gate, dict) else {},
        "experiment_iteration_audit": iteration_audit if isinstance(iteration_audit, dict) else {},
    }


def _submission_state_stale_for_current_venue(root: Path, submission: dict[str, Any]) -> bool:
    cfg = _read_json(root / "project.json", {})
    cfg_paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    current_venue = str(cfg.get("target_venue") or cfg.get("venue") or cfg_paper.get("target_venue") or "").strip() if isinstance(cfg, dict) else ""
    current_slug = _venue_slug(current_venue) if current_venue else ""
    if not current_slug:
        return False
    state_venue = str(submission.get("target_venue") or submission.get("venue") or submission.get("venue_slug") or "").strip()
    state_slug = _venue_slug(state_venue) if state_venue else ""
    if state_slug and state_slug != current_slug:
        return True
    watched = {
        "checks": submission.get("checks"),
        "failed_checks": submission.get("failed_checks"),
        "blockers": submission.get("blockers"),
        "metrics": submission.get("metrics"),
        "issues": submission.get("issues"),
        "warnings": submission.get("warnings"),
        "paper_self_review_blockers": submission.get("paper_self_review_blockers"),
        "paper_self_review_evidence_blockers": submission.get("paper_self_review_evidence_blockers"),
    }
    text = json.dumps(watched, ensure_ascii=False).lower()
    for match in re.finditer(r"paper/(?:output|writing|venues|orchestra)/([a-z0-9-]+)", text):
        found = match.group(1).strip("-")
        if found and found != current_slug:
            return True
    if current_slug not in {"nature", "springer-nature"} and any(marker in text for marker in ["springernature.com", "sn-jnl", "sn-nature", "nature article", "nature expects"]):
        return True
    if current_slug != "iclr" and any(marker in text for marker in ["github.com/iclr/master-template", "iclr2026_conference"]):
        return True
    return False


def _venue_refresh_submission_blocker(root: Path) -> dict[str, Any]:
    cfg = _read_json(root / "project.json", {})
    cfg_paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    venue = str(cfg.get("target_venue") or cfg.get("venue") or cfg_paper.get("target_venue") or "target venue").strip() if isinstance(cfg, dict) else "target venue"
    return _public_blocker_row({
        "category": "submission_readiness",
        "severity": "block",
        "issue": f"Target venue is now {venue}; paper evidence and submission-readiness audits must be rebuilt for this venue before showing readiness details.",
        "raw_issue": "venue_readiness_refresh_required",
        "evidence": [str(root / "project.json"), str(root / "state" / "submission_readiness.json")],
    })


def _current_submission_blockers(root: Path) -> list[dict[str, Any]]:
    submission = _read_json(root / "state" / "submission_readiness.json", {})
    if not isinstance(submission, dict):
        return []
    if _submission_state_stale_for_current_venue(root, submission):
        return [_venue_refresh_submission_blocker(root)]
    failed = submission.get("failed_checks", []) if isinstance(submission.get("failed_checks", []), list) else []
    if not failed:
        checks = submission.get("checks", []) if isinstance(submission.get("checks", []), list) else []
        failed = [
            row for row in checks
            if isinstance(row, dict)
            and str(row.get("status") or row.get("severity") or "").strip().lower() in {"block", "blocked", "fail", "failed", "error"}
        ]
    blockers: list[dict[str, Any]] = []
    for row in failed[:20]:
        if not isinstance(row, dict):
            continue
        label = str(row.get("name") or row.get("id") or "submission_readiness").strip()
        detail = str(row.get("detail") or "").strip()
        raw_issue = f"{label}: {detail}" if detail else label
        blockers.append(_public_blocker_row({
            "category": "submission_readiness",
            "severity": row.get("severity") or row.get("status") or "block",
            "issue": raw_issue,
            "raw_issue": raw_issue,
            "evidence": row.get("evidence", []),
        }))
    if not blockers and submission.get("status") and not submission.get("submission_ready"):
        raw_issue = f"submission_readiness.status={submission.get('status')}"
        blockers.append(_public_blocker_row({
            "category": "submission_readiness",
            "severity": "block",
            "issue": raw_issue,
            "raw_issue": raw_issue,
            "evidence": [str(root / "state" / "submission_readiness.json")],
        }))
    return blockers


def _fresh_literature_audit_issue(root: Path, reference_gate: dict[str, Any]) -> str:
    audit = _read_json(root / "state" / "literature_base_audit.json", {})
    assessment = _read_json(root / "state" / "literature_base_candidate_assessment.json", {})
    total = ""
    done = ""
    remaining = ""
    if isinstance(audit, dict):
        total = str(audit.get("total_audit_required_count") or "")
        done = str(audit.get("candidate_count") or "")
        remaining = str(audit.get("remaining_candidate_count") or "")
    if not total and isinstance(assessment, dict):
        total = str(assessment.get("last_audit_total_required_count") or assessment.get("last_audit_total_candidate_count") or "")
        done = str(assessment.get("last_audit_candidate_count") or "")
        remaining = str(assessment.get("last_audit_remaining_candidate_count") or "")
    run_id = ""
    if isinstance(reference_gate, dict):
        run_id = str(reference_gate.get("fresh_find_run_id") or "")
    if not run_id and isinstance(audit, dict):
        run_id = str(audit.get("fresh_find_run_id") or "")
    if not run_id and isinstance(assessment, dict):
        run_id = str(assessment.get("fresh_find_run_id") or "")
    progress = f"{done}/{total}" if done and total else f"remaining={remaining}" if remaining else "pending"
    return (
        f"Find candidate pool is not fully repo/data/env/topic-fit audited yet ({progress}; remaining={remaining or 'unknown'}; "
        f"fresh_find_run_id={run_id or 'unknown'}). The workflow must choose the next base from audited fresh literature candidates before keeping or returning to any legacy route."
    )


def _blocker_text_zh(text: str) -> str:
    if text.startswith("Find candidate pool is not fully repo/data/env/topic-fit audited yet"):
        return text.replace(
            "Find candidate pool is not fully repo/data/env/topic-fit audited yet",
            "fresh Find 基底候选尚未完成 repo/data/env/topic-fit 审计",
        ).replace(
            "The workflow must choose the next base from audited fresh literature candidates before keeping or returning to any legacy route.",
            "流程必须先从审计通过的 fresh 文献候选中选择后续基底，不能惯性保留或回到历史路线。"
        )
    if text in {"Latest forced paper refresh did not produce a new current TeX/PDF; previous PDF preview is invalid for this cycle.", "Latest current-paper refresh did not produce a new current TeX/PDF; previous PDF preview is invalid for this cycle."}:
        return "最近一次当前论文重新生成没有生成新的当前 TeX/PDF，上一版 PDF 预览已被判定为本轮失效。"
    return text


def _public_blocker_summary(blocker: Any, fallback: str = "") -> str:
    row = blocker if isinstance(blocker, dict) else {}
    raw_text = str(row.get("summary") or row.get("human_summary") or row.get("issue") or fallback or "").strip()
    if not raw_text:
        return ""
    text = " ".join(raw_text.split())
    lower = text.lower()
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason and reason not in reasons:
            reasons.append(reason)

    reference_passed_context = any(marker in lower for marker in ["reference_reproduction_gate_pass", "reference reproduction passed", "paper_level_reproduction_passed", "completed_reference_reproduction"]) or ("参考复现" in text and "已通过" in text)
    if any(marker in lower for marker in ["hold-markdown-only", "paper_evidence_audit", "submission_readiness", "evidence_gate_allows_template", "paper_evidence", "submission readiness"]):
        add("论文证据/投稿门控未通过，当前只能保留审计材料，不能进入论文或结论提升。")
    if "semantic_data_provenance" in lower or "semantic-provenance" in lower or ("llm" in lower and "provenance" in lower):
        add("当前数据路线缺少 LLM/text-semantic 实验所需的可审计文本/元数据 provenance；继续纯行为或损失级候选实验无法清除此门控。")
    if any(marker in lower for marker in ["reference_reproduction_gate", "reference reproduction", "below target", "ndcg_at_10", "reference_reproduction_gate_pass"]):
        if reference_passed_context:
            add("参考复现已通过；当前不是复现问题，而是还缺当前主线下可审计的候选实验。")
        else:
            add("参考复现相关状态需要先审计确认；未通过时不能作为论文证据。")
    if any(marker in lower for marker in ["scientific_progress_gate", "no audit-ready promotable", "promotable candidate", "audit-ready"]):
        add("还没有审计就绪的可提升候选实验；具体下一步由项目代理读取当前证据后决定。")
    if any(marker in lower for marker in ["fresh find base audit", "no evidence-ready fresh base", "no_viable_base_switch_route", "fresh_literature", "silently return to legacy", "legacy main-route fallback"]):
        add("fresh Find 基底审计没有找到可直接作为证据的新基底，TASTE 不能静默回到旧路线。")
    if any(marker in lower for marker in ["baseline", "crashed", "crash", "float object is not subscriptable", "crashed_or_incomplete"]):
        add("当前基线训练崩溃或不完整，不能和候选训练结果做提升对比。")
    if any(marker in lower for marker in ["conference_preview_ready=false", "accepted_preview", "pdf preview", "paper preview"]):
        add("论文预览尚未通过，不是可投稿版本。")
    if any(marker in lower for marker in ["quota", "api key", "rate limit"]) or ("llm api" in lower and "candidate" not in lower and "text" not in lower):
        add("LLM/API 配置或额度阻塞，Find 评分和后续自动判断不能可靠继续。")
    if any(marker in lower for marker in ["recommendation_shortfall", "shortfall", "推荐文章", "strong recommendation"]):
        add("Find 推荐门控未满足，不能用弱论文补足推荐数量。")

    if reasons:
        return "当前不能进入论文或结论提升阶段。原因：" + "；".join(reasons[:4])
    if len(text) > 220 or text.count(";") >= 2 or text.count("/") >= 8:
        return "当前存在项目门控阻塞，原始审计细节较长；请以门控文件和下方产物路径为准，系统会继续按最高优先级阻塞推进。"
    return _blocker_text_zh(text)


def _public_blocker_next_action(blocker: Any, fallback: str = "") -> str:
    row = blocker if isinstance(blocker, dict) else {}
    raw = str(row.get("next_action") or fallback or "").strip()
    category = str(row.get("category") or "").lower()
    issue = str(row.get("raw_issue") or row.get("issue") or row.get("summary") or "").lower()
    combined = category + " " + issue + " " + raw.lower()
    if any(marker in combined for marker in ["reference_reproduction", "below target", "no_viable_base_switch_route"]):
        return "先修复当前基底/路线的参考复现门控，再刷新 scientific_progress、paper_evidence、submission_readiness 和 blocker_action_plan。"
    if any(marker in combined for marker in ["scientific_progress", "promotable", "audit-ready"]):
        return "继续由 project agent 产生并审计真实候选实验；没有审计就绪的提升证据前保持阻塞。"
    if any(marker in combined for marker in ["hold-markdown-only", "submission", "paper_evidence"]):
        return "保持只写草稿状态；先补齐参考复现、可比基线、结论台账和证据清单，再允许论文阶段。"
    if raw:
        return _blocker_text_zh(raw)
    return "继续处理当前最高优先级科研门控；禁止手写通过状态或结论提升。"


def _public_blocker_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    raw_issue = str(row.get("raw_issue") or row.get("issue") or "").strip()
    public = _public_blocker_summary(row)
    out = dict(row)
    if raw_issue and raw_issue != public:
        out["raw_issue"] = raw_issue
    if public:
        out["issue"] = public
        out["summary"] = public
        out["human_summary"] = public
    out["next_action"] = _public_blocker_next_action({**out, "raw_issue": raw_issue}, str(row.get("next_action") or ""))
    return out


def _full_cycle_summary(root: Path) -> dict[str, Any]:
    payload = _read_json(root / "state" / "full_research_cycle.json", {})
    if not isinstance(payload, dict) or not payload:
        return {}
    latest_gate = payload.get("latest_gate", {}) if isinstance(payload.get("latest_gate", {}), dict) else {}
    latest_blockers = payload.get("latest_blockers", []) if isinstance(payload.get("latest_blockers", []), list) else []
    cycles = payload.get("cycles", []) if isinstance(payload.get("cycles", []), list) else []
    running_stage = payload.get("current_running_stage", {}) if isinstance(payload.get("current_running_stage", {}), dict) else {}
    stage_failures = payload.get("stage_failures", []) if isinstance(payload.get("stage_failures", []), list) else []
    runtime_blockers = payload.get("runtime_blockers", []) if isinstance(payload.get("runtime_blockers", []), list) else []
    blocker_action_plan = _read_json(root / "state" / "blocker_action_plan.json", {})
    blocker_action_summary = blocker_action_plan.get("summary", {}) if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("summary", {}), dict) else {}
    blocker_action_rows = blocker_action_plan.get("actions", [])[:8] if isinstance(blocker_action_plan, dict) and isinstance(blocker_action_plan.get("actions", []), list) else []
    reference_gate_live = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    fresh_research_base = _read_json(root / "state" / "fresh_research_base.json", {})
    fresh_base_impl_plan = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    status = str(payload.get("status") or "not_started")
    literature_base_audit_pending = isinstance(reference_gate_live, dict) and reference_gate_live.get("decision") == "literature_base_audit_required"
    fresh_literature_audit_exhausted = (
        isinstance(reference_gate_live, dict)
        and reference_gate_live.get("decision") == "no_viable_base_switch_route"
        and isinstance(reference_gate_live.get("base_switch"), dict)
        and reference_gate_live.get("base_switch", {}).get("fresh_literature_base_audit_complete")
    )
    fresh_base_decision = reference_gate_live.get("decision") if isinstance(reference_gate_live, dict) else ""
    fresh_base_implementation_required = fresh_base_decision in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}
    fresh_base_reference_probe_required = fresh_base_decision == "fresh_base_reference_probe_required"
    fresh_base_reference_smoke_required = fresh_base_decision == "fresh_base_reference_smoke_required"
    fresh_base_reference_reproduction_required = fresh_base_decision == "fresh_base_reference_reproduction_required"
    fresh_base_data_required = fresh_base_decision == "fresh_base_implementation_required" and _fresh_base_data_required(fresh_base_impl_plan)
    current_find_plan = _current_find_plan_summary(root)
    cfg = _read_json(root / "project.json", {})
    project_id = str(cfg.get("name") or root.name) if isinstance(cfg, dict) else root.name
    selected_base_gate_active = _selected_base_gate_active(project_id, root, cfg if isinstance(cfg, dict) else {})
    route_ctx = _project_route_context(root, project_id, cfg if isinstance(cfg, dict) else {})
    if isinstance(reference_gate_live, dict) and reference_gate_live.get("decision") == "literature_base_audit_required":
        status = "blocked_literature_base_audit_required"
        payload = dict(payload)
        payload.update({
            "status": status,
            "current_goal": "fresh Find base candidates must be audited before choosing the current main base route",
            "continuation_required": True,
            "continuation_reason": "repo/data/env audit pending for fresh literature base candidates",
            "paper_pipeline_skipped": True,
            "paper_pipeline_skipped_reason": "fresh literature base candidates are not yet repo/data/env audited",
            "reference_base_switch_required": True,
            "reference_base_switch_exhausted": False,
        })
        latest_blockers = [{
            "category": "fresh_literature_base_audit",
            "severity": "block",
            "issue": _fresh_literature_audit_issue(root, reference_gate_live),
            "evidence": [str(root / "state" / "literature_base_candidate_assessment.json"), str(root / "state" / "literature_base_audit.json"), str(root / "state" / "reference_reproduction_gate.json")],
            "next_action": "Continue fresh literature base audit; do not run legacy-route experiments or the full paper pipeline while this gate is pending.",
        }]
        latest_gate = dict(latest_gate)
        latest_gate["reference_reproduction_gate"] = reference_gate_live
    elif fresh_base_implementation_required:
        status = "blocked_fresh_base_reference_reproduction_required" if fresh_base_reference_reproduction_required else "blocked_fresh_base_reference_smoke_required" if fresh_base_reference_smoke_required else "blocked_fresh_base_reference_probe_required" if fresh_base_reference_probe_required else "blocked_fresh_base_data_required" if fresh_base_data_required else "blocked_fresh_base_implementation_required"
        payload = dict(payload)
        base_switch = reference_gate_live.get("base_switch", {}) if isinstance(reference_gate_live.get("base_switch"), dict) else {}
        fresh_base = base_switch.get("fresh_paper_base", {}) if isinstance(base_switch.get("fresh_paper_base"), dict) else {}
        if not fresh_base and isinstance(fresh_research_base, dict):
            fresh_base = fresh_research_base.get("selected", {}) if isinstance(fresh_research_base.get("selected"), dict) else {}
        title = str(fresh_base.get("title") or "environment-stage selected anchor")
        impl_status = str(fresh_base_impl_plan.get("status") or "") if isinstance(fresh_base_impl_plan, dict) else ""
        impl_repo = fresh_base_impl_plan.get("repo", {}) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("repo"), dict) else {}
        impl_blockers = fresh_base_impl_plan.get("blocker_reasons", []) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("blocker_reasons"), list) else []
        blocked_datasets = fresh_base_impl_plan.get("blocked_datasets", []) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("blocked_datasets"), list) else []
        reference_full_job = _fresh_base_reference_full_job(root)
        full_job_running = bool(isinstance(reference_full_job, dict) and reference_full_job.get("status") == "running")
        full_job_note = f" full reference reproduction 正在由 wrapper 运行，PID={reference_full_job.get('pid')}。" if full_job_running else ""
        block_category = "blocked_fresh_base_reference_reproduction_required" if fresh_base_reference_reproduction_required else "blocked_fresh_base_reference_smoke_required" if fresh_base_reference_smoke_required else "blocked_fresh_base_reference_probe_required" if fresh_base_reference_probe_required else "blocked_fresh_base_data_required" if fresh_base_data_required else "blocked_fresh_base_implementation_required"
        goal_zh, next_action_zh, skipped_reason = _selected_base_status_text(block_category, route_ctx, zh=True)
        goal_en, next_action_en, _ = _selected_base_status_text(block_category, route_ctx, zh=False)
        payload.update({
            "status": status,
            "current_goal": goal_zh + full_job_note,
            "fresh_research_base": fresh_base,
            "fresh_base_implementation_plan": {
                "status": impl_status,
                "repo": impl_repo,
                "ready_datasets": fresh_base_impl_plan.get("ready_datasets", []) if isinstance(fresh_base_impl_plan, dict) else [],
                "blocked_datasets": blocked_datasets,
                "blocker_reasons": impl_blockers,
            },
            "continuation_required": True,
            "continuation_reason": next_action_en,
            "paper_pipeline_skipped": True,
            "paper_pipeline_skipped_reason": skipped_reason,
            "reference_base_switch_required": True,
            "reference_base_switch_exhausted": False,
        })
        latest_blockers = [{
            "category": "fresh_base_reference_reproduction_required" if fresh_base_reference_reproduction_required else "fresh_base_reference_smoke_required" if fresh_base_reference_smoke_required else "fresh_base_reference_probe_required" if fresh_base_reference_probe_required else "fresh_base_data_required" if fresh_base_data_required else "fresh_base_implementation_required",
            "severity": "block",
            "issue": goal_zh + full_job_note + " " + next_action_zh,
            "evidence": [str(root / "state" / "fresh_research_base.json"), str(root / "state" / "fresh_base_implementation_plan.json"), str(root / "state" / "fresh_base_data_acquisition.json"), str(root / "state" / "literature_tool_packet.json"), str(root / "planning" / "finding" / "find_results.json"), str(root / "planning" / "finding" / "read_results.json"), str(root / "planning" / "finding" / "ideas.json"), str(root / "planning" / "finding" / "plans.json"), str(root / "state" / "reference_reproduction_gate.json")],
            "next_action": next_action_zh,
            "fresh_base_implementation_status": impl_status,
            "fresh_base_repo": impl_repo,
            "fresh_base_blocked_datasets": blocked_datasets,
            "fresh_base_blockers": impl_blockers[:8],
            "current_find_research_plan": current_find_plan,
        }]
        latest_gate = dict(latest_gate)
        latest_gate["reference_reproduction_gate"] = reference_gate_live
    elif fresh_literature_audit_exhausted:
        status = "blocked_no_viable_base_switch_route"
        payload = dict(payload)
        base_switch = reference_gate_live.get("base_switch", {}) if isinstance(reference_gate_live.get("base_switch"), dict) else {}
        issue = "; ".join(str(item) for item in (reference_gate_live.get("blockers") or [])[:4])
        payload.update({
            "status": status,
            "current_goal": "fresh Find base audit completed without an evidence-ready new base",
            "continuation_required": True,
            "continuation_reason": "fresh literature audit complete without evidence-ready new base",
            "paper_pipeline_skipped": True,
            "paper_pipeline_skipped_reason": "reference/base gate is blocked",
            "reference_base_switch_required": True,
            "reference_base_switch_exhausted": True,
        })
        latest_blockers = [{
            "category": "fresh_literature_base_audit_exhausted",
            "severity": "block",
            "issue": issue or (
                "Fresh Find base audit is complete but found no evidence-ready fresh base: "
                f"fresh_find_run_id={base_switch.get('fresh_find_run_id') or 'unknown'}, "
                f"{base_switch.get('total_candidates_evaluated') or 0} candidates audited, "
                f"{base_switch.get('repo_candidates_discovered_count') or 0} fresh repo candidates discovered."
            ),
            "evidence": [str(root / "state" / "literature_base_audit.json"), str(root / "state" / "evidence_ready_repo_selection.json"), str(root / "state" / "reference_reproduction_gate.json")],
            "next_action": "Continue external fresh-base search or get explicit user confirmation before using an imperfect base.",
        }]
        latest_gate = dict(latest_gate)
        latest_gate["reference_reproduction_gate"] = reference_gate_live
    current_cycle = payload.get("current_cycle") or len(cycles)
    max_cycles = payload.get("max_cycles") or ""
    paper_state = _active_paper_state(root, project_id, cfg if isinstance(cfg, dict) else {})
    live_science = _live_science_gate_status(root)
    live_submission = _read_json(root / "state" / "submission_readiness.json", {})
    live_submission_ready = bool(
        isinstance(live_submission, dict)
        and live_submission.get("submission_ready")
        and live_submission.get("status") == "submission_ready"
    )
    live_submission_failed = live_submission.get("failed_checks", []) if isinstance(live_submission, dict) and isinstance(live_submission.get("failed_checks", []), list) else []
    paper_regeneration_running = _paper_regeneration_running(paper_state if isinstance(paper_state, dict) else {})
    paper_regeneration_failed = _paper_regeneration_failed(paper_state if isinstance(paper_state, dict) else {})
    paper_stage_status_state = str(
        paper_state.get("paper_stage_status") or paper_state.get("paper_orchestra_bridge_status") or ""
    ) if isinstance(paper_state, dict) else ""
    stale_science_skip = bool(
        isinstance(paper_state, dict)
        and paper_state.get("paper_generation_skipped")
        and live_science.get("ok")
    )
    paper_blocked_before_generation = bool(
        isinstance(paper_state, dict)
        and not stale_science_skip
        and (paper_state.get("paper_generation_skipped") or paper_stage_status_state == "blocked_before_paper_generation")
    )
    latest_generated_pdf_path = None
    latest_generated_tex_path = None
    if isinstance(paper_state, dict) and not paper_regeneration_failed:
        latest_generated_pdf_path = (
            _paper_state_path(root, paper_state, "blocked_preview_pdf", "latest_preview_pdf", "conference_preview_pdf", "pdf_path", "paper_orchestra_final_pdf")
            or _venue_output_artifact(root, paper_state, "paper.pdf")
        )
        latest_generated_tex_path = (
            _paper_state_path(root, paper_state, "blocked_preview_tex", "latest_preview_tex", "rendered_tex", "tex_path", "paper_orchestra_final_tex")
            or _venue_output_artifact(root, paper_state, "paper.tex")
        )
    latest_pdf = ""
    latest_gate_pdf = "" if paper_regeneration_failed else str(latest_gate.get("latest_pdf") or "").strip()
    if latest_gate_pdf:
        candidate_path = Path(latest_gate_pdf)
        if candidate_path.exists() and _paper_path_matches_current_venue(root, paper_state, candidate_path):
            latest_pdf = latest_gate_pdf
        else:
            latest_gate = dict(latest_gate)
            latest_gate["latest_pdf"] = ""
            latest_gate["latest_pdf_info"] = {}
    if not latest_pdf and isinstance(paper_state, dict) and not paper_regeneration_failed:
        candidate = (
            _paper_state_path(root, paper_state, "conference_preview_pdf", "pdf_path", "blocked_preview_pdf", "latest_preview_pdf", "paper_orchestra_final_pdf")
            or _venue_output_artifact(root, paper_state, "paper.pdf")
        )
        latest_pdf = str(candidate) if candidate else ""
    latest_pdf_path = Path(latest_pdf) if latest_pdf else None
    latest_pdf_url = _paper_asset_url(root, latest_pdf_path) if latest_pdf_path and latest_pdf_path.exists() else ""
    latest_pdf_info = {}
    if not paper_regeneration_failed and latest_pdf_path and latest_pdf_path.exists():
        gate_pdf_info = latest_gate.get("latest_pdf_info", {}) if isinstance(latest_gate.get("latest_pdf_info", {}), dict) else {}
        info_path = str(gate_pdf_info.get("path") or "").strip()
        try:
            info_matches_pdf = not info_path or Path(info_path).resolve() == latest_pdf_path.resolve()
        except Exception:
            info_matches_pdf = False
        if gate_pdf_info and info_matches_pdf:
            latest_pdf_info = gate_pdf_info
        if not latest_pdf_info:
            try:
                stat = latest_pdf_path.stat()
                latest_pdf_info = {
                    "path": str(latest_pdf_path),
                    "exists": True,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "mtime_iso": __import__("datetime").datetime.fromtimestamp(stat.st_mtime, __import__("datetime").timezone.utc).isoformat(),
                }
            except Exception:
                latest_pdf_info = {"path": latest_pdf, "exists": True}
    if isinstance(paper_state, dict):
        current_paper_status = {
            "conference_preview_ready": bool(paper_state.get("conference_preview_ready")),
            "paper_normality_status": paper_state.get("paper_normality_status", ""),
            "paper_normality_pages": paper_state.get("paper_normality_pages", ""),
            "paper_normality_body_pages": paper_state.get("paper_normality_body_pages", ""),
            "paper_normality_estimated_reference_pages": paper_state.get("paper_normality_estimated_reference_pages", ""),
            "paper_normality_citation_count": paper_state.get("paper_normality_citation_count", ""),
            "venue_submission_policy_status": paper_state.get("venue_submission_policy_status", ""),
            "venue_submission_policy": paper_state.get("venue_submission_policy", {}),
            "paper_venue_format_status": paper_state.get("paper_venue_format_status", ""),
            "paper_figure_quality_status": paper_state.get("paper_figure_quality_status", ""),
            "paper_figure_quality_ready": bool(paper_state.get("paper_figure_quality_ready")),
            "paper_figure_blocker_count": paper_state.get("paper_figure_blocker_count", ""),
            "paper_figure_warning_count": paper_state.get("paper_figure_warning_count", ""),
            "paper_figure_failed": paper_state.get("paper_figure_failed", []),
            "paper_preview_repair_loop_status": paper_state.get("paper_preview_repair_loop_status", ""),
            "promotion_gate": (live_submission.get("promotion_gate") if isinstance(live_submission, dict) and live_submission.get("promotion_gate") else paper_state.get("promotion_gate", "")),
            "paper_review_verdict": "submission_ready" if live_submission_ready else paper_state.get("paper_review_verdict", ""),
        }
        latest_gate = dict(latest_gate)
        previous_paper_status = latest_gate.get("paper_status", {}) if isinstance(latest_gate.get("paper_status", {}), dict) else {}
        latest_gate["paper_status"] = {**previous_paper_status, **current_paper_status}
        paper_preview_ready = bool(
            paper_state.get("conference_preview_ready")
            and (paper_state.get("normal_preview_ready") or paper_state.get("paper_normality_ready"))
            and (paper_state.get("venue_template_format_ready") or paper_state.get("paper_venue_format_status") == "pass")
            and (paper_state.get("paper_figure_quality_ready") or paper_state.get("paper_figure_quality_status") == "pass")
        )
        if not paper_preview_ready or not paper_state.get("pdf_ready"):
            latest_gate = dict(latest_gate)
            latest_gate["accepted_preview"] = False
            if not paper_state.get("pdf_ready"):
                latest_gate["latest_pdf"] = ""
                latest_gate["latest_pdf_info"] = {}
                latest_pdf = ""
                latest_pdf_path = None
                latest_pdf_url = ""
                latest_pdf_info = {}
    if live_submission_ready:
        latest_gate = dict(latest_gate)
        latest_gate["submission_ready"] = True
        latest_gate["submission_readiness"] = {
            **(latest_gate.get("submission_readiness", {}) if isinstance(latest_gate.get("submission_readiness", {}), dict) else {}),
            "status": live_submission.get("status", "") if isinstance(live_submission, dict) else "",
            "failed_checks": 0,
            "blockers": [],
            "warnings": live_submission.get("warnings", [])[:20] if isinstance(live_submission, dict) and isinstance(live_submission.get("warnings", []), list) else [],
            "metrics": live_submission.get("metrics", {}) if isinstance(live_submission, dict) and isinstance(live_submission.get("metrics", {}), dict) else {},
        }
    if live_science.get("ok"):
        latest_gate = dict(latest_gate)
        experiment_gates = latest_gate.get("experiment_gates", {}) if isinstance(latest_gate.get("experiment_gates", {}), dict) else {}
        latest_gate["experiment_gates"] = {
            **experiment_gates,
            "reference_reproduction_status": "pass",
            "reference_reproduction_decision": "continue_base",
            "reference_reproduction_blockers": [],
            "scientific_progress_status": "pass",
            "scientific_progress_blockers": [],
            "iteration_audit_status": "pass",
            "iteration_audit_blockers": [],
            "paper_blocked_until_experiment_gate_passes": False,
        }
    if latest_gate.get("accepted_preview") and latest_gate.get("submission_ready") and live_science.get("ok"):
        latest_gate = dict(latest_gate)
        latest_gate["complete"] = True
    pdf_changed = latest_gate.get("pdf_changed_this_cycle")
    if pdf_changed is None:
        pdf_changed = payload.get("pdf_changed_this_cycle")
    continuation_required = bool(
        payload.get("continuation_required")
        or payload.get("paper_iteration_required")
        or (status == "blocked_after_max_cycles" and not latest_gate.get("complete"))
    )
    current_blockers = _current_submission_blockers(root)
    if current_blockers and not literature_base_audit_pending and not fresh_literature_audit_exhausted and not fresh_base_implementation_required:
        latest_blockers = current_blockers
    if live_submission_ready and not live_submission_failed:
        latest_blockers = [
            row for row in latest_blockers
            if not (
                isinstance(row, dict)
                and (
                    str(row.get("category") or "") == "submission_readiness"
                    or "paper_orchestra_audit.status=submission_ready" in str(row.get("issue") or "")
                )
            )
        ]
    if live_science.get("ok"):
        latest_blockers = [
            row for row in latest_blockers
            if not (
                isinstance(row, dict)
                and (
                    "reference reproduction gate blocked" in str(row.get("issue") or "")
                    or "scientific progress gate blocked" in str(row.get("issue") or "")
                    or str(row.get("category") or "") in {"reference_reproduction_gate", "scientific_progress_gate"}
                )
            )
        ]
    if paper_regeneration_running and not literature_base_audit_pending and not fresh_literature_audit_exhausted and not fresh_base_implementation_required:
        latest_blockers = [
            row for row in latest_blockers
            if not (isinstance(row, dict) and str(row.get("category") or "") == "paper_refresh")
        ]
    if paper_regeneration_failed and not literature_base_audit_pending and not fresh_literature_audit_exhausted and not fresh_base_implementation_required:
        latest_gate = dict(latest_gate)
        latest_gate["accepted_preview"] = False
        latest_gate["latest_pdf"] = ""
        latest_gate["latest_pdf_info"] = {}
        latest_gate["current_paper_regeneration_invalidated_previous_pdf"] = True
        latest_blockers = [
            {
                "category": "paper_refresh",
                "severity": "block",
                "issue": "Latest current-paper refresh did not produce a new current TeX/PDF; previous PDF preview is invalid for this cycle.",
            },
            *latest_blockers,
        ]
    latest_blocker = latest_blockers[0] if latest_blockers and isinstance(latest_blockers[0], dict) else {}
    latest_blocker_text = str(latest_blocker.get("raw_issue") or latest_blocker.get("issue") or "").strip()
    latest_blocker_text_zh = _public_blocker_summary(latest_blocker) or _blocker_text_zh(latest_blocker_text)
    status_zh = {
        "running": "运行中",
        "repairing": "正在把阻塞回灌给 project agent",
        "completed": "已通过本地论文与证据门控",
        "blocked_after_max_cycles": "达到本次配置轮数后仍阻塞",
        "blocked_fresh_base_implementation_required": "环境阶段锚点已选出，等待代码/实现/数据协议",
        "blocked_fresh_base_data_required": "环境阶段锚点已选出，等待当前基底真实数据/loader 合同",
        "blocked_fresh_base_reference_probe_required": "当前基底数据/loader 已通过，等待参考协议/环境 manifest 探针",
        "blocked_fresh_base_reference_smoke_required": "当前基底协议探针已通过，等待有界 reference smoke",
        "blocked_fresh_base_reference_reproduction_required": "当前基底 bounded audit 已通过，正在/等待 full 参考复现",
        "blocked_no_viable_base_switch_route": "基底路线阻塞",
        "error": "运行错误",
    }.get(status, status)
    accepted_preview = bool(latest_gate.get("accepted_preview"))
    submission_ready = bool(latest_gate.get("submission_ready"))
    if fresh_base_implementation_required:
        base = payload.get("fresh_research_base", {}) if isinstance(payload.get("fresh_research_base"), dict) else fresh_research_base.get("selected", {}) if isinstance(fresh_research_base, dict) else {}
        title = str(base.get("title") or "环境阶段锚点")
        impl_status = str(fresh_base_impl_plan.get("status") or "") if isinstance(fresh_base_impl_plan, dict) else ""
        impl_repo = fresh_base_impl_plan.get("repo", {}) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("repo"), dict) else {}
        impl_blockers = fresh_base_impl_plan.get("blocker_reasons", []) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("blocker_reasons"), list) else []
        repo_name = str(impl_repo.get("name") or impl_repo.get("repo_path") or "").strip()
        block_category = "blocked_fresh_base_reference_reproduction_required" if fresh_base_reference_reproduction_required else "blocked_fresh_base_reference_smoke_required" if fresh_base_reference_smoke_required else "blocked_fresh_base_reference_probe_required" if fresh_base_reference_probe_required else "blocked_fresh_base_data_required" if fresh_base_data_required else "blocked_fresh_base_implementation_required"
        _, route_next_action_zh, _ = _selected_base_status_text(block_category, route_ctx, zh=True)
        summary_zh = (
            f"完整科研自循环：Find 候选池已融入 TASTE，环境阶段 Claude Code 当前锚点为 {title}。"
            + ("已精读 {readings} 篇，形成 {ideas} 个 idea 和 {plans} 个可执行 plan。".format(readings=current_find_plan.get("readings", 0), ideas=current_find_plan.get("ideas", 0), plans=current_find_plan.get("plans", 0)) if current_find_plan else "")
            + route_next_action_zh
            + full_job_note
        )
        if repo_name:
            summary_zh += f" fresh-base 仓库/候选：{repo_name}。"
        if impl_status:
            summary_zh += f" 实现计划状态：{impl_status}。"
        if impl_blockers:
            summary_zh += " 具体缺口：" + "；".join(str(item) for item in impl_blockers[:3]) + "。"
    else:
        summary_zh = (
            f"完整科研自循环：{status_zh}；轮次 {current_cycle}/{max_cycles or '?'}；"
            f"合格 PDF 预览={'通过' if accepted_preview else '未通过'}；投稿证据门控={'通过' if submission_ready else '未通过'}。"
        )
    if latest_blocker_text_zh:
        summary_zh += f" 当前主要阻塞：{latest_blocker_text_zh}"
    if running_stage.get("stage"):
        line_count = running_stage.get("line_count")
        if isinstance(line_count, int) and line_count > 0:
            summary_zh += f" 当前阶段：{running_stage.get('stage')}，日志已同步 {line_count} 行。"
        else:
            summary_zh += f" 当前阶段：{running_stage.get('stage')}，日志同步中。"
    if continuation_required:
        summary_zh += " 需要继续迭代；当前输出不能视为合格论文。"
    if pdf_changed is False:
        summary_zh += " 本轮 PDF 未发生变化。"
    if paper_regeneration_failed:
        summary_zh += " 最近一次当前论文重新生成未生成新的当前 PDF，旧 PDF 预览已被判定为失效。"
    if literature_base_audit_pending:
        summary_zh = (
            "完整科研自循环：已暂停在 fresh Find 基底审计。"
            "Find 已融入 TASTE 的 literature_tool_packet/reference gate；当前必须审完 fresh strong/base 候选的 repo、数据、环境和 topic-fit，"
            "在通过前不能继续历史路线复现、实验迭代或论文写作。"
        )
        if latest_blocker_text_zh:
            summary_zh += f" 当前主要阻塞：{latest_blocker_text_zh}"
    if fresh_literature_audit_exhausted:
        summary_zh = (
            "完整科研自循环：fresh Find 基底审计已完成但没有 evidence-ready 新基底。"
            "Find 已融入 TASTE，候选已审计；TASTE 已阻止静默回到历史路线。"
            "当前不能继续实验或论文写作，除非继续搜索到新证据，或用户明确确认使用某个有缺陷但可改造的基底。"
        )
        if latest_blocker_text_zh:
            summary_zh += f" 当前主要阻塞：{latest_blocker_text_zh}"
    summary_en = (
        f"Full research cycle: {status.replace('_', ' ')}; cycle {current_cycle}/{max_cycles or '?'}; "
        f"accepted PDF preview={'pass' if accepted_preview else 'not passed'}; submission evidence gate={'pass' if submission_ready else 'not passed'}."
    )
    if latest_blocker_text:
        summary_en += f" Main blocker: {latest_blocker_text}"
    if running_stage.get("stage"):
        line_count = running_stage.get("line_count")
        if isinstance(line_count, int) and line_count > 0:
            summary_en += f" Current stage: {running_stage.get('stage')}; {line_count} log lines synchronized."
        else:
            summary_en += f" Current stage: {running_stage.get('stage')}; log synchronization is in progress."
    if continuation_required:
        summary_en += " Continuation is required; current output is not a qualified paper."
    if pdf_changed is False:
        summary_en += " The PDF did not change in this cycle."
    if paper_regeneration_failed:
        summary_en += " The latest current-paper regeneration produced no new current PDF, so the previous PDF preview is treated as stale."
    if literature_base_audit_pending:
        summary_en = (
            "Full research cycle is paused at fresh Find base audit. Find has been integrated into the TASTE literature_tool_packet/reference gate; "
            "The workflow must finish repo/data/env/topic-fit audit for fresh strong/base candidates before legacy-route reproduction, experiment iteration, or paper writing can continue."
        )
        if latest_blocker_text:
            summary_en += f" Main blocker: {latest_blocker_text}"
    if fresh_literature_audit_exhausted:
        summary_en = (
            "Full research cycle is blocked after completing the fresh Find base audit without an evidence-ready new base. "
            "Find has been integrated into The workflow and candidates were audited; The workflow is prevented from silently returning to a legacy route. "
            "Experiment and paper writing remain blocked until new evidence is added or the user explicitly confirms an imperfect but transformable base."
        )
        if latest_blocker_text:
            summary_en += f" Main blocker: {latest_blocker_text}"
    running_or_live = status.lower() == "running" or bool(_live_full_cycle_process(root, project_id))
    finished_at = "" if running_or_live else str(payload.get("finished_at") or payload.get("completed_at") or "")
    result = {
        "status": status,
        "status_i18n": {"zh": status_zh, "en": status.replace("_", " ")},
        "summary": summary_zh,
        "summary_zh": summary_zh,
        "summary_en": summary_en,
        "summary_i18n": {"zh": summary_zh, "en": summary_en},
        "current_cycle": current_cycle,
        "max_cycles": max_cycles,
        "cycle_count": len(cycles),
        "current_goal": str(payload.get("current_goal") or ""),
        "latest_step": payload.get("latest_step", {}) if isinstance(payload.get("latest_step", {}), dict) else {},
        "fresh_research_base": payload.get("fresh_research_base", fresh_research_base.get("selected", {}) if isinstance(fresh_research_base, dict) else {}),
        "fresh_base_implementation_plan": fresh_base_impl_plan if isinstance(fresh_base_impl_plan, dict) else {},
        "current_find_research_plan": current_find_plan,
        "current_running_stage": running_stage,
        "stage_failures": stage_failures[-8:],
        "runtime_blockers": runtime_blockers[-8:],
        "latest_gate": latest_gate,
        "accepted_preview": accepted_preview,
        "submission_ready": submission_ready,
        "latest_pdf_path": latest_pdf,
        "latest_pdf_url": latest_pdf_url,
        "latest_pdf_info": latest_pdf_info,
        "latest_generated_pdf_path": str(latest_generated_pdf_path) if latest_generated_pdf_path else "",
        "latest_generated_pdf_url": _paper_asset_url(root, latest_generated_pdf_path),
        "latest_generated_pdf_info": _file_info(latest_generated_pdf_path),
        "latest_generated_tex_path": str(latest_generated_tex_path) if latest_generated_tex_path else "",
        "latest_generated_tex_url": _paper_asset_url(root, latest_generated_tex_path),
        "latest_generated_is_accepted_preview": bool(accepted_preview and latest_pdf_url and latest_generated_pdf_path and latest_pdf_path and latest_generated_pdf_path.resolve() == latest_pdf_path.resolve()),
        "pdf_changed_this_cycle": pdf_changed,
        "paper_iteration_required": bool(payload.get("paper_iteration_required") or (status == "blocked_after_max_cycles" and not latest_gate.get("complete"))),
        "continuation_required": continuation_required,
        "continuation_reason": str(payload.get("continuation_reason") or ""),
        "latest_blockers": latest_blockers[:12],
        "latest_blocker_count": len(latest_blockers),
        "blocker_action_plan": {
            "status": blocker_action_plan.get("status", "") if isinstance(blocker_action_plan, dict) else "",
            "summary": blocker_action_summary,
            "actions": blocker_action_rows,
        },
        "blocker_action_plan_path": str(root / "state" / "blocker_action_plan.json"),
        "blocker_action_plan_report": str(root / "reports" / "blocker_action_plan.md"),
        "cycles": cycles[-5:],
        "updated_at": str(payload.get("updated_at") or ""),
        "started_at": str(payload.get("started_at") or ""),
        "finished_at": finished_at,
        "files": {
            "state": str(root / "state" / "full_research_cycle.json"),
            "report": str(root / "reports" / "full_research_cycle.md"),
        },
    }
    return result


def _stage_status(root: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    bootstrap = _read_json(root / "state" / "repo_env_bootstrap.json", {})
    data_req = _read_json(root / "state" / "repo_data_requirements.json", {})
    repo_selection = _read_json(root / "state" / "evidence_ready_repo_selection.json", {})
    repo_env_strategy = _read_json(root / "state" / "repo_env_strategy.json", {})
    repo_selection_blocker = _read_json(root / "state" / "repo_selection_blocker.json", {})
    paper_state = _active_paper_state(root, str(cfg.get("name") or root.name) if isinstance(cfg, dict) else root.name, cfg if isinstance(cfg, dict) else {})
    configured_paper_venue = _display_venue(_project_configured_venue(cfg if isinstance(cfg, dict) else {}))
    experiments = _experiment_rows(root)
    experiment_record = _experiment_record_table(root)
    trajectory_system = _trajectory_summary(root)
    full_cycle = _full_cycle_summary(root)
    reference_reproduction = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    fresh_research_base = _read_json(root / "state" / "fresh_research_base.json", {})
    fresh_base_impl_plan = _read_json(root / "state" / "fresh_base_implementation_plan.json", {})
    experiment_iteration_audit = _read_json(root / "state" / "experiment_iteration_audit.json", {})
    scientific_progress_gate = _read_json(root / "state" / "scientific_progress_gate.json", {})
    literature_summary = _taste_literature_summary(root)
    completed = [row for row in experiments if str(row.get("status", "")).lower() in {"completed", "success", "repaired"}]
    env_status = str(bootstrap.get("status", "")) if isinstance(bootstrap, dict) else ""
    env_name = _active_env_name(root, cfg)
    env_ok = env_status == "completed"
    if env_status == "prepared" and env_name:
        conda_base = cfg.get("environment", {}).get("conda_base_hint", "") if isinstance(cfg.get("environment", {}), dict) else ""
        for base in [Path(conda_base), ROOT.parent / "miniforge", Path.home() / "miniforge3"]:
            if base and (base / "envs" / env_name).exists():
                env_ok = True
                break
    ready_datasets = data_req.get("ready_datasets", []) if isinstance(data_req, dict) else []
    blocked_datasets = data_req.get("blocked_datasets", []) if isinstance(data_req, dict) else []
    repo_details = _repo_details(root)
    dataset_details = _dataset_details(root)
    ready_names = set()
    ready_dataset_details = [
        row for row in dataset_details
        if _dataset_is_claim_ready(row)
    ]
    ready_names.update(str(row.get("dataset") or row.get("name") or "") for row in ready_dataset_details)
    ready_datasets = sorted(ready_names)
    pending_dataset_details = [
        row for row in dataset_details
        if not _dataset_is_claim_ready(row)
        and not row.get("missing_required_files")
        and (
            row.get("available")
            or row.get("probe_success")
            or row.get("loader_probe_success")
            or row.get("generic_probe_success")
            or str(row.get("status_label") or "") in {"待补证据", "有文件但不匹配当前 repo"}
        )
    ]
    blocked_dataset_details = [
        row for row in dataset_details
        if str(row.get("dataset") or row.get("name") or "") not in ready_names
        and not _dataset_is_claim_ready(row)
        and (
            row.get("missing_required_files")
            or (row.get("available") is False)
            or row.get("policy_decision")
        )
    ]
    active_repo = next((row for row in repo_details if row.get("active")), repo_details[0] if repo_details else {})
    block_reasons: list[str] = []
    data_gap_notes: list[str] = []
    claude_decision = {}
    if isinstance(repo_selection, dict) and isinstance(repo_selection.get("claude_topic_decision"), dict):
        claude_decision = repo_selection.get("claude_topic_decision", {})
    active_payload = _read_json(root / "state" / "active_repo.json", {})
    if not claude_decision and isinstance(active_payload, dict) and isinstance(active_payload.get("claude_topic_fit_decision"), dict):
        claude_decision = active_payload.get("claude_topic_fit_decision", {})
    if isinstance(repo_env_strategy, dict) and repo_env_strategy:
        claude_decision = dict(claude_decision) if isinstance(claude_decision, dict) else {}
        claude_decision.update({key: value for key, value in repo_env_strategy.items() if key not in {"active_repo_before", "selected_repo", "guardrails"}})
    claude_accepted_repo_ready = bool(isinstance(repo_selection, dict) and repo_selection.get("selected")) and (
        bool(claude_decision.get("accept_as_current_best"))
        or str(repo_selection.get("selection_gate") or "").startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate"))
    )
    repo_selection_gate = str(repo_selection.get("selection_gate") or "") if isinstance(repo_selection, dict) else ""
    if isinstance(repo_selection_blocker, dict) and repo_selection_blocker.get("reason"):
        block_reasons.append(str(repo_selection_blocker.get("reason")))
    elif repo_selection_gate.startswith("continued_search_required"):
        block_reasons.append("Claude Code has not accepted any audited repo as the current best evidence-ready and transformable route yet; The workflow will keep searching/auditing instead of treating the fallback as final.")
    if blocked_dataset_details:
        ready_hint = f"当前真实实验路线使用已通过 loader 的数据：{_text_list(ready_datasets)}。" if ready_datasets else "当前还没有可进入真实实验的数据。"
        ready_hint_en = f"Current real experiments should use loader-ready data: {_text_list_en(ready_datasets)}." if ready_datasets else "No loader-ready data is available yet."
        data_gap_notes.append(f"{len(blocked_dataset_details)} candidate dataset(s) are tracked as data gaps outside the current evidence chain.")
        data_gap_notes_zh = [f"仍有 {len(blocked_dataset_details)} 个候选数据集未通过当前 repo loader，TASTE 只把它们记录为数据缺口，不会放进真实实验或论文证据链。{ready_hint}"]
        data_gap_notes_en = [f"{len(blocked_dataset_details)} candidate dataset(s) are tracked as data gaps outside the current evidence chain. {ready_hint_en}"]
        top_blockers: list[str] = []
        top_blockers_en: list[str] = []
        for row in blocked_dataset_details[:3]:
            if not isinstance(row, dict):
                continue
            ds_name = str(row.get("dataset") or row.get("name") or "dataset").strip() or "dataset"
            summary = str(row.get("human_summary") or row.get("blocking_explanation") or row.get("reason") or "").strip()
            summary_en = str(row.get("human_summary_en") or row.get("blocking_explanation_en") or row.get("reason") or "").strip()
            missing = row.get("missing_required_files") or []
            missing_text = f"缺少{_dataset_missing_label(missing, 'zh')}" if isinstance(missing, list) and missing else ""
            missing_text_en = f"missing {_dataset_missing_label(missing, 'en')}" if isinstance(missing, list) and missing else ""
            zh_reason = summary or missing_text
            en_reason = summary_en or missing_text_en
            if zh_reason:
                top_blockers.append(f"{ds_name}：{zh_reason}")
            if en_reason:
                top_blockers_en.append(f"{ds_name}: {en_reason}")
        if top_blockers:
            data_gap_notes.append("数据缺口示例：" + "；".join(top_blockers))
            data_gap_notes_zh.append("数据缺口示例：" + "；".join(top_blockers))
        if top_blockers_en:
            data_gap_notes_en.append("Data-gap examples: " + "; ".join(top_blockers_en))
    else:
        data_gap_notes_zh = []
        data_gap_notes_en = []
    if not ready_dataset_details:
        block_reasons.append("No claim-ready real dataset is available for the active repo; experiments cannot produce paper evidence yet.")
    block_reasons_zh: list[str] = []
    for reason in block_reasons:
        if reason == "No claim-ready real dataset is available for the active repo; experiments cannot produce paper evidence yet.":
            block_reasons_zh.append("当前 active repo 没有 claim-ready 的真实数据集；实验还不能产出论文证据。")
        elif reason == "Claude Code has not accepted any audited repo as the current best evidence-ready and transformable route yet; The workflow will keep searching/auditing instead of treating the fallback as final.":
            block_reasons_zh.append("Claude Code 尚未接受任何已审计仓库作为当前最好的 evidence-ready 且可改造路线；系统会继续搜索/审计，而不会把 fallback 当成最终路线。")
        else:
            block_reasons_zh.append(_zh_or_known(reason) or reason)
    if active_repo and not active_repo.get("execution_ready", False):
        reason = str(active_repo.get("notes") or "active repo is not execution-ready")
        block_reasons.append(reason)
        block_reasons_zh.append(_zh_or_known(reason) or "当前 active repo 尚未通过 execution-ready 检查。")
    if isinstance(bootstrap, dict) and bootstrap.get("error"):
        reason = str(bootstrap.get("error"))
        block_reasons.append(reason)
        block_reasons_zh.append(_zh_or_known(reason) or reason)
    block_reason = "; ".join(dict.fromkeys(reason for reason in block_reasons if reason))
    block_reason_zh = "；".join(dict.fromkeys(reason for reason in block_reasons_zh if reason))
    data_gap_warning = "; ".join(dict.fromkeys(reason for reason in data_gap_notes if reason))
    data_gap_warning_zh = "；".join(dict.fromkeys(reason for reason in data_gap_notes_zh if reason))
    data_gap_warning_en = "; ".join(dict.fromkeys(reason for reason in data_gap_notes_en if reason))
    env_stage_blocked = bool(block_reason) or (isinstance(repo_selection_blocker, dict) and repo_selection_blocker.get("status") == "blocked")
    reference_status = str(reference_reproduction.get("status") or "") if isinstance(reference_reproduction, dict) else ""
    reference_decision = str(reference_reproduction.get("decision") or "") if isinstance(reference_reproduction, dict) else ""
    reference_ready = reference_status == "pass" and reference_decision == "continue_base"
    if reference_ready:
        # Passed reference gates make old repo-selection blockers and fresh-base
        # candidates audit history; they must not keep the live UI blocked.
        env_stage_blocked = False
        block_reason = ""
        block_reason_zh = ""
    reference_blockers = reference_reproduction.get("blockers", []) if isinstance(reference_reproduction, dict) and isinstance(reference_reproduction.get("blockers", []), list) else []
    fresh_base_selected_raw = isinstance(fresh_research_base, dict) and isinstance(fresh_research_base.get("selected"), dict) and bool(fresh_research_base.get("selected"))
    fresh_base_selected = fresh_base_selected_raw and not reference_ready
    fresh_base_title = str((fresh_research_base.get("selected", {}) if isinstance(fresh_research_base, dict) and isinstance(fresh_research_base.get("selected"), dict) else {}).get("title") or "")
    if reference_ready:
        reference_summary_zh = "参考工作已按论文级门控通过，可进入正式创新实验。"
        reference_summary_en = "The reference work passed the paper-level reproduction gate, so formal novel experiments may proceed."
    elif reference_decision == "fresh_base_implementation_required" or fresh_base_selected:
        impl_status = str(fresh_base_impl_plan.get("status") or "") if isinstance(fresh_base_impl_plan, dict) else ""
        impl_repo = fresh_base_impl_plan.get("repo", {}) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("repo"), dict) else {}
        impl_blockers = fresh_base_impl_plan.get("blocker_reasons", []) if isinstance(fresh_base_impl_plan, dict) and isinstance(fresh_base_impl_plan.get("blocker_reasons"), list) else []
        repo_name = str(impl_repo.get("name") or impl_repo.get("repo_path") or "").strip()
        reference_summary_zh = f"环境阶段 Claude Code 已选择论文/方法锚点：{fresh_base_title or '见 fresh_research_base.json'}；当前要补代码/实现/数据协议，历史路线仅作为内部对照。"
        reference_summary_en = f"Environment-stage Claude Code selected a paper/method anchor: {fresh_base_title or 'see fresh_research_base.json'}; The workflow must resolve code/implementation/data protocol, with previous routes as legacy/control only."
        if repo_name:
            reference_summary_zh += f" fresh-base 仓库/候选：{repo_name}。"
            reference_summary_en += f" Fresh-base repo/candidate: {repo_name}."
        if impl_status:
            reference_summary_zh += f" 实现计划状态：{impl_status}。"
            reference_summary_en += f" Implementation-plan status: {impl_status}."
        if impl_blockers:
            reference_summary_zh += " 主要缺口：" + "；".join(str(item) for item in impl_blockers[:3]) + "。"
            reference_summary_en += " Main gaps: " + "; ".join(str(item) for item in impl_blockers[:3]) + "."
    else:
        reference_summary_zh = "环境可运行不等于科研可开始：参考工作尚未按论文协议复现通过，流程必须继续复现或有证据地更换基底。"
        reference_summary_en = "A runnable environment is not enough to start formal science: the reference work has not passed paper-protocol reproduction, so The workflow must continue reproduction or switch base with evidence."
    live_science = _live_science_gate_status(root)
    submission_readiness = _read_json(root / "state" / "submission_readiness.json", {})
    paper_orchestra_state = _read_json(root / "state" / "paper_orchestra_state.json", {})
    live_submission_ready = bool(
        isinstance(submission_readiness, dict)
        and submission_readiness.get("submission_ready")
        and submission_readiness.get("status") == "submission_ready"
    )
    paper_verdict = paper_state.get("paper_review_verdict", "") if isinstance(paper_state, dict) else ""
    promotion_gate = paper_state.get("promotion_gate", "") if isinstance(paper_state, dict) else ""
    if live_submission_ready:
        promotion_gate = str(
            (submission_readiness.get("promotion_gate") if isinstance(submission_readiness, dict) else "")
            or (paper_orchestra_state.get("promotion_gate_recommendation") if isinstance(paper_orchestra_state, dict) else "")
            or promotion_gate
        )
        if paper_verdict in {"", "blocked", "major-revision", "reject"}:
            paper_verdict = "submission_ready"
    conference_preview_ready = bool(paper_state.get("conference_preview_ready")) if isinstance(paper_state, dict) else False
    conference_preview_pages = paper_state.get("conference_preview_pages", "") if isinstance(paper_state, dict) else ""
    conference_preview_body_pages = paper_state.get("conference_preview_body_pages") or paper_state.get("paper_normality_body_pages", "") if isinstance(paper_state, dict) else ""
    conference_preview_reference_pages = paper_state.get("conference_preview_reference_pages") or paper_state.get("paper_normality_estimated_reference_pages", "") if isinstance(paper_state, dict) else ""
    venue_requirements = _venue_requirements_summary(root, str(configured_paper_venue or paper_state.get("venue") or ""), paper_state if isinstance(paper_state, dict) else {}) if isinstance(paper_state, dict) else {}
    conference_preview_body_page_limit = paper_state.get("conference_preview_body_page_limit") or (paper_state.get("venue_submission_policy", {}) if isinstance(paper_state.get("venue_submission_policy", {}), dict) else {}).get("body_page_max") or (venue_requirements.get("body_page_max", "") if isinstance(venue_requirements, dict) else "") if isinstance(paper_state, dict) else ""
    normal_preview_ready = bool(paper_state.get("normal_preview_ready") or paper_state.get("paper_normality_ready")) if isinstance(paper_state, dict) else False
    paper_normality_status = str(paper_state.get("paper_normality_status") or "") if isinstance(paper_state, dict) else ""
    paper_venue_format_status = str(paper_state.get("paper_venue_format_status") or "") if isinstance(paper_state, dict) else ""
    venue_template_format_ready = bool(paper_state.get("venue_template_format_ready") or paper_venue_format_status == "pass") if isinstance(paper_state, dict) else False
    paper_figure_quality_status = str(paper_state.get("paper_figure_quality_status") or "") if isinstance(paper_state, dict) else ""
    paper_figure_quality_ready = bool(paper_state.get("paper_figure_quality_ready") or paper_figure_quality_status == "pass") if isinstance(paper_state, dict) else False
    paper_citation_render_status = str(paper_state.get("paper_citation_render_status") or "") if isinstance(paper_state, dict) else ""
    paper_citation_render_ready = bool(paper_state.get("paper_citation_render_ready") or paper_citation_render_status == "pass") if isinstance(paper_state, dict) else False
    paper_citation_render_blockers = paper_state.get("paper_citation_render_blockers", []) if isinstance(paper_state, dict) and isinstance(paper_state.get("paper_citation_render_blockers", []), list) else []
    paper_self_review_status = str(paper_state.get("paper_self_review_status") or "") if isinstance(paper_state, dict) else ""
    paper_self_review_ready = bool(paper_state.get("paper_self_review_ready")) if isinstance(paper_state, dict) else False
    paper_self_review_blockers = paper_state.get("paper_self_review_blockers", []) if isinstance(paper_state, dict) and isinstance(paper_state.get("paper_self_review_blockers", []), list) else []
    paper_self_review_evidence_blockers = paper_state.get("paper_self_review_evidence_blockers", []) if isinstance(paper_state, dict) and isinstance(paper_state.get("paper_self_review_evidence_blockers", []), list) else []
    conference_preview_blockers = paper_state.get("conference_preview_blockers", []) if isinstance(paper_state, dict) and isinstance(paper_state.get("conference_preview_blockers", []), list) else []
    raw_layout_warnings = paper_state.get("paper_layout_footprint_warnings", []) if isinstance(paper_state, dict) and isinstance(paper_state.get("paper_layout_footprint_warnings", []), list) else []
    paper_layout_footprint_warnings = [item for item in (_paper_public_layout_warning_text(value) for value in raw_layout_warnings) if item]
    preview_blocker_text = ""
    if conference_preview_blockers:
        first_blocker = conference_preview_blockers[0]
        if isinstance(first_blocker, dict):
            preview_blocker_text = _paper_public_blocker_text(first_blocker.get("public_detail") or first_blocker.get("detail") or first_blocker.get("id") or "")
        else:
            preview_blocker_text = _paper_public_blocker_text(first_blocker)
    paper_orchestra_bridge_status = str(paper_state.get("paper_orchestra_bridge_status") or "") if isinstance(paper_state, dict) else ""
    if live_submission_ready and isinstance(paper_orchestra_state, dict) and paper_orchestra_state.get("status") in {"pass", "submission_ready"}:
        paper_orchestra_bridge_status = str(paper_orchestra_state.get("status") or paper_orchestra_bridge_status)
    paper_regeneration_running = _paper_regeneration_running(paper_state)
    paper_regeneration_failed = _paper_regeneration_failed(paper_state)
    paper_generation_skipped = bool(paper_state.get("paper_generation_skipped")) if isinstance(paper_state, dict) else False
    stale_science_skip = bool(paper_generation_skipped and live_science.get("ok"))
    if stale_science_skip:
        paper_generation_skipped = False
    paper_blocked_before_generation = paper_generation_skipped or (paper_orchestra_bridge_status == "blocked_before_paper_generation" and not live_science.get("ok"))
    full_cycle_preview_accepted = bool(full_cycle.get("accepted_preview") or (full_cycle.get("latest_gate") if isinstance(full_cycle.get("latest_gate"), dict) else {}).get("accepted_preview"))
    paper_quality_ready = bool(
        not paper_blocked_before_generation
        and conference_preview_ready
        and normal_preview_ready
        and venue_template_format_ready
        and paper_figure_quality_ready
        and paper_citation_render_ready
        and paper_self_review_ready
    )
    display_pdf = _paper_state_path(root, paper_state, "conference_preview_pdf", "pdf_path") if (paper_quality_ready and full_cycle_preview_accepted) else None
    display_tex = _paper_state_path(root, paper_state, "rendered_tex") if (paper_quality_ready and full_cycle_preview_accepted) else None
    raw_orchestra_pdf = _paper_state_path(root, paper_state, "paper_orchestra_final_pdf")
    raw_orchestra_tex = _paper_state_path(root, paper_state, "paper_orchestra_final_tex")
    suppress_previous_output = bool(paper_regeneration_failed and not paper_regeneration_running)
    latest_output_pdf = (
        (None if suppress_previous_output else _paper_state_path(root, paper_state, "blocked_preview_pdf", "latest_preview_pdf"))
        or (None if suppress_previous_output else _venue_output_artifact(root, paper_state, "paper.pdf"))
    )
    latest_output_tex = (
        (None if suppress_previous_output else _paper_state_path(root, paper_state, "blocked_preview_tex", "latest_preview_tex", "conference_preview_tex"))
        or (None if suppress_previous_output else _venue_output_artifact(root, paper_state, "paper.tex"))
    )
    blocked_preview_pdf = None if display_pdf else (latest_output_pdf or raw_orchestra_pdf)
    blocked_preview_tex = None if display_tex else (latest_output_tex or raw_orchestra_tex)
    has_pdf = bool(display_pdf)
    generated_but_blocked = bool(blocked_preview_pdf or raw_orchestra_pdf or latest_output_pdf)
    science_preflight = paper_state.get("science_gate_preflight", {}) if isinstance(paper_state, dict) and isinstance(paper_state.get("science_gate_preflight", {}), dict) else {}
    science_preflight_blockers = science_preflight.get("blockers", []) if isinstance(science_preflight.get("blockers", []), list) else []
    if live_science.get("ok"):
        science_preflight = {
            "reference_reproduction_gate": live_science.get("reference_reproduction_gate", {}),
            "scientific_progress_gate": live_science.get("scientific_progress_gate", {}),
            "experiment_iteration_audit": live_science.get("experiment_iteration_audit", {}),
            "blockers": [],
            "stale_preflight_ignored": stale_science_skip,
        }
        science_preflight_blockers = []
    evidence_blocked = (
        bool(science_preflight_blockers)
        or (not live_submission_ready and (promotion_gate in {"hold-markdown-only", "needs-more-evidence", "blocked"} or paper_verdict in {"major-revision", "reject"}))
    )
    self_review_suffix_zh = " 论文自审 receipt 未通过，项目代理需独立读 PDF/TeX/BibTeX/log/venue contract 后修复并写 receipt。" if (paper_self_review_blockers or paper_self_review_status == "block") else ""
    self_review_suffix_en = " Paper self-review receipt is not cleared; the project agent must independently inspect PDF/TeX/BibTeX/logs/venue contract, repair the manuscript, and write the current-artifact receipt." if (paper_self_review_blockers or paper_self_review_status == "block") else ""
    self_review_evidence_suffix_zh = f" 论文自审发现 {len(paper_self_review_evidence_blockers)} 项未解决科研证据问题；PDF 只能作为检查预览，不能标记为投稿通过。" if paper_self_review_evidence_blockers else ""
    self_review_evidence_suffix_en = f" Paper self-review found {len(paper_self_review_evidence_blockers)} unresolved scientific-evidence issue(s); the PDF is inspection preview only, not submission-ready." if paper_self_review_evidence_blockers else ""
    if paper_regeneration_running:
        paper_status = "running"
        paper_summary_en = "TASTE paper-stage current paper regeneration is running; writing is generating current TeX/PDF artifacts."
        paper_summary_zh = "论文阶段正在重新生成当前稿件预览；writing 模块正在生成当前 TeX/PDF 产物。"
    elif paper_blocked_before_generation:
        paper_status = "evidence_gated_preview"
        skipped_reason = str(paper_state.get("paper_generation_skipped_reason") or "science gates are not cleared") if isinstance(paper_state, dict) else "science gates are not cleared"
        if science_preflight_blockers:
            paper_summary_en = "Submission/evidence gates are not cleared; writing may still generate a venue-formatted preview. Main gate notes: " + "; ".join(str(item) for item in science_preflight_blockers[:3])
            paper_summary_zh = "投稿/证据门控未通过；writing 仍可生成当前稿件预览。主要门控说明：" + "；".join(str(item) for item in science_preflight_blockers[:3])
        else:
            paper_summary_en = "Submission/evidence gates are not cleared; generated output is a venue-formatted preview: " + skipped_reason
            paper_summary_zh = "投稿/证据门控未通过；生成产物作为目标 venue 稿件预览展示：" + skipped_reason
    elif has_pdf and live_submission_ready:
        paper_status = "pdf_ready"
        preview_suffix_en = f"; pages={conference_preview_pages}" if conference_preview_pages else ""
        preview_suffix_zh = f"；页数={conference_preview_pages}" if conference_preview_pages else ""
        paper_summary_en = f"TASTE paper-stage PDF is current and submission-readiness gates pass{preview_suffix_en}; review={paper_verdict or 'submission_ready'}; gate={promotion_gate or 'allow-template'}"
        paper_summary_zh = f"论文阶段当前 PDF 已通过正常形态、证据和投稿 readiness 门控{preview_suffix_zh}；评审={paper_verdict or 'submission_ready'}；门控={promotion_gate or 'allow-template'}"
    elif has_pdf and evidence_blocked:
        paper_status = "preview_available"
        preview_suffix_en = f"; pages={conference_preview_pages}" if conference_preview_pages else ""
        preview_suffix_zh = f"；页数={conference_preview_pages}" if conference_preview_pages else ""
        paper_summary_en = f"writing generated a venue-formatted preview PDF{preview_suffix_en}. Submission/evidence gates are not marked ready yet; continue citation, figure-footprint, and venue-compliance repair from current artifacts." + self_review_evidence_suffix_en
        paper_summary_zh = f"writing 已生成当前稿件预览 PDF{preview_suffix_zh}。投稿/证据门控尚未标记通过；后续应基于当前产物继续补引用、调图表占地并核对目标格式。" + self_review_evidence_suffix_zh
    elif has_pdf:
        paper_status = "pdf_ready"
        paper_summary_en = f"TASTE paper-stage normal preview PDF is ready; review={paper_verdict or 'unknown'}; gate={promotion_gate or 'not-set'}"
        paper_summary_zh = f"论文阶段正常预览 PDF 已就绪；评审={paper_verdict or '未知'}；门控={promotion_gate or '未设置'}"
    elif generated_but_blocked:
        paper_status = "preview_available"
        if paper_normality_status == "pass" and paper_venue_format_status == "pass" and paper_figure_quality_status == "pass":
            paper_summary_en = "A current TASTE paper PDF exists and passes normality/template/figure checks. It is shown as a venue-formatted preview; final submission readiness still follows evidence gates." + self_review_suffix_en + self_review_evidence_suffix_en
            paper_summary_zh = "已有当前 TASTE 论文 PDF，且论文形态、目标模板和图表质量检查均为 pass。下方作为目标 venue 稿件预览展示；最终投稿准备度仍以证据门控为准。" + self_review_suffix_zh + self_review_evidence_suffix_zh
        else:
            blocker_suffix_en = f" Main blocker: {preview_blocker_text}." if preview_blocker_text else ""
            blocker_suffix_zh = f" 主要原因：{preview_blocker_text}。" if preview_blocker_text else ""
            layout_suffix_zh = " 图表审计已提示版面压力，版面处理应先诊断图表/表格占地。" if paper_layout_footprint_warnings else ""
            layout_suffix_en = " Layout diagnostics flagged figure/table footprint, so layout handling should diagnose floats and tables first." if paper_layout_footprint_warnings else ""
            paper_summary_en = f"A current TASTE paper PDF exists and is shown below as a venue-formatted preview. Remaining writing checks: paper-normality={paper_normality_status or 'not-passed'}, venue-template={paper_venue_format_status or 'not-checked'}, figure-quality={paper_figure_quality_status or 'not-checked'}." + blocker_suffix_en + layout_suffix_en + self_review_suffix_en + self_review_evidence_suffix_en + " writing should repair those checks from current artifacts."
            paper_summary_zh = f"已有当前 TASTE 论文 PDF，并会在下方作为目标 venue 稿件预览展示。剩余写作检查：论文形态={paper_normality_status or '未通过'}，目标模板={paper_venue_format_status or '未检查'}，图表质量={paper_figure_quality_status or '未检查'}。" + blocker_suffix_zh + layout_suffix_zh + self_review_suffix_zh + self_review_evidence_suffix_zh + " 若正文页数已符合目标要求，writing 的当前任务是处理图表占地、真实引用覆盖和模板细节。"
    elif evidence_blocked:
        paper_status = "blocked"
        if paper_regeneration_failed:
            paper_summary_en = "The latest current-paper regeneration did not produce a new current TeX/PDF, so The workflow is blocking the old PDF instead of showing it as current output."
            paper_summary_zh = "最近一次当前论文预览重新生成没有产出新的当前 TeX/PDF，TASTE 已阻止旧 PDF 继续作为当前成果展示。"
        else:
            paper_summary_en = "No current TASTE paper PDF preview exists yet. writing should first resolve venue requirements/template and generate TeX/PDF, then keep submission/evidence gates separate from preview generation."
            paper_summary_zh = "当前还没有 TASTE 论文 PDF 预览。writing 应先解析目标要求和模板并生成 TeX/PDF，再把投稿/证据门控与预览生成状态分开。"
    else:
        paper_status = "drafting" if paper_state else "not_started"
        paper_summary_en = f"TASTE paper-stage status={paper_orchestra_bridge_status or 'not-run'}; review={paper_verdict or 'unknown'}; gate={promotion_gate or 'not-set'}"
        paper_summary_zh = f"论文阶段状态={paper_orchestra_bridge_status or '未运行'}；评审={paper_verdict or '未知'}；门控={promotion_gate or '未设置'}"
    if reference_ready and env_ok:
        env_summary_zh = "conda 环境已创建并锁定；当前参考复现门控已通过，历史 fresh-base 候选不会再覆盖实时状态。"
        env_summary_en = "The conda environment is created and locked; the current reference reproduction gate passed, so historical fresh-base candidates no longer override live status."
    elif reference_decision == "fresh_base_implementation_required" or fresh_base_selected:
        env_summary_zh = f"环境阶段 Claude Code 已选择论文/方法锚点：{fresh_base_title or '见 fresh_research_base.json'}；旧 active repo 只作为 legacy/control，不能继续当主线。"
        env_summary_en = f"Environment-stage Claude Code selected a paper/method anchor: {fresh_base_title or 'see fresh_research_base.json'}; the old active repo is legacy/control only, not the main route."
    elif env_stage_blocked:
        env_summary_zh = "环境已锁定，但关键 repo/data/Claude 门控仍未通过；不能进入正式实验 claim。"
        env_summary_en = "Environment is locked, but key repo/data/Claude gates are still not cleared; formal experiment claims cannot start."
    elif env_ok and not reference_ready:
        env_summary_zh = "conda 环境已创建并锁定；参考工作复现门控仍控制能否进入正式创新实验。"
        env_summary_en = "The conda environment is created and locked; the reference-work reproduction gate still controls whether formal novel experiments may start."
    elif env_ok:
        env_summary_zh = "conda 环境已创建并锁定；Claude 已接受当前 repo 作为最佳可改造路线，且已有真实数据通过 loader；后续不会从网页重复安装或创建新环境"
        env_summary_en = "The conda environment is created and locked; Claude accepted the current repo as the best transformable route, and real data passed the loader. The web UI will not reinstall or recreate the environment."
    elif env_status == "prepared":
        env_summary_zh = "已生成环境计划，尚未真实验证"
        env_summary_en = "Environment plan generated but not yet verified for real execution."
    else:
        env_summary_zh = "等待 repo / conda / 数据可用性检查"
        env_summary_en = "Waiting for repo / conda / data availability checks."
    env_lock_reason_zh = "环境已创建并锁定；网页和 系统不会重复创建/安装，除非人工清理锁定状态" if env_ok else "尚未锁定；只允许首次创建环境"
    env_lock_reason_en = "Environment created and locked; the web UI and The workflow will not recreate or reinstall unless the lock is manually cleared." if env_ok else "Not locked yet; only first-time environment creation is allowed."
    experiment_summary_zh = f"已记录 {len(experiments)} 个实验，其中 {len(completed)} 个完成"
    experiment_summary_en = f"{len(experiments)} experiment(s) recorded; {len(completed)} completed."
    return {
        "environment": {
            "label": "环境配置",
            "label_i18n": {"zh": "环境配置", "en": "Environment"},
            "status": "blocked" if env_stage_blocked else "ready" if env_ok else "prepared" if env_status == "prepared" else "blocked" if bootstrap else "not_started",
            "summary": env_summary_zh,
            "summary_i18n": {"zh": env_summary_zh, "en": env_summary_en},
            "summary_zh": env_summary_zh,
            "summary_en": env_summary_en,
            "locked": bool(env_ok),
            "lock_reason": env_lock_reason_zh,
            "lock_reason_i18n": {"zh": env_lock_reason_zh, "en": env_lock_reason_en},
            "lock_reason_zh": env_lock_reason_zh,
            "lock_reason_en": env_lock_reason_en,
            "repo_path": _active_repo_path(root),
            "env_name": _active_env_name(root, cfg),
            "ready_datasets": ready_datasets,
            "blocked_datasets": blocked_datasets,
            "repo_details": repo_details,
            "dataset_details": dataset_details,
            "ready_dataset_details": ready_dataset_details,
            "pending_dataset_details": pending_dataset_details,
            "blocked_dataset_details": blocked_dataset_details,
            "active_repo": active_repo,
            "fresh_research_base": fresh_research_base if isinstance(fresh_research_base, dict) else {},
            "fresh_base_implementation_plan": fresh_base_impl_plan if isinstance(fresh_base_impl_plan, dict) else {},
            "legacy_active_repo_only": bool(reference_decision == "fresh_base_implementation_required" or fresh_base_selected),
            "block_reason": block_reason,
            "block_reason_i18n": {"zh": block_reason_zh or block_reason, "en": block_reason},
            "block_reason_zh": block_reason_zh or block_reason,
            "block_reason_en": block_reason,
            "data_gap_warning": data_gap_warning,
            "data_gap_warning_i18n": {"zh": data_gap_warning_zh or data_gap_warning, "en": data_gap_warning_en or data_gap_warning},
            "data_gap_warning_zh": data_gap_warning_zh or data_gap_warning,
            "data_gap_warning_en": data_gap_warning_en or data_gap_warning,
            "claude_accepted_transformable_repo": claude_accepted_repo_ready,
            "claude_topic_decision": _claude_decision_i18n(claude_decision) if isinstance(claude_decision, dict) else {},
            "repo_selection_gate": repo_selection_gate,
            "repo_selection_blocker": repo_selection_blocker if isinstance(repo_selection_blocker, dict) else {},
            "repo_search_iterations": _read_json(root / "state" / "repo_search_iteration_memory.json", []),
            "repo_env_strategy": repo_env_strategy if isinstance(repo_env_strategy, dict) else {},
            "bootstrap": bootstrap if isinstance(bootstrap, dict) else {},
            "reference_reproduction_gate": reference_reproduction if isinstance(reference_reproduction, dict) else {},
            "reference_reproduction_ready": reference_ready,
            "reference_reproduction_summary": reference_summary_zh,
            "reference_reproduction_summary_i18n": {"zh": reference_summary_zh, "en": reference_summary_en},
            "reference_reproduction_summary_zh": reference_summary_zh,
            "reference_reproduction_summary_en": reference_summary_en,
            "reference_reproduction_blocker": "; ".join(str(item) for item in reference_blockers[:3]),
        },
        "experiment": {
            "label": "实验迭代",
            "label_i18n": {"zh": "实验迭代", "en": "Experiment Loop"},
            "status": "running_or_ready" if completed else "not_started",
            "summary": experiment_summary_zh,
            "summary_i18n": {"zh": experiment_summary_zh, "en": experiment_summary_en},
            "summary_zh": experiment_summary_zh,
            "summary_en": experiment_summary_en,
            "coding_backend": "claude",
            "last_backend": _latest_coding_backend(root),
            "experiments": experiments,
            "experiment_record": experiment_record,
            "reference_reproduction_gate": reference_reproduction if isinstance(reference_reproduction, dict) else {},
            "scientific_progress_gate": scientific_progress_gate if isinstance(scientific_progress_gate, dict) else {},
            "experiment_iteration_audit": experiment_iteration_audit if isinstance(experiment_iteration_audit, dict) else {},
            "trajectory_system": trajectory_system,
            "full_research_cycle": full_cycle,
        },
        "paper": {
            "label": "论文撰写",
            "label_i18n": {"zh": "论文撰写", "en": "Paper Writing"},
            "status": paper_status,
            "summary": paper_summary_zh,
            "summary_i18n": {"zh": paper_summary_zh, "en": paper_summary_en},
            "summary_zh": paper_summary_zh,
            "summary_en": paper_summary_en,
            "venue": _display_venue(configured_paper_venue or paper_state.get("venue", "")) if isinstance(paper_state, dict) else configured_paper_venue,
            "pdf_ready": has_pdf,
            "pdf_path": str(display_pdf) if display_pdf else "",
            "pdf_url": _paper_asset_url(root, display_pdf),
            "tex_path": str(display_tex) if display_tex else "",
            "tex_url": _paper_asset_url(root, display_tex),
            "blocked_pdf_path": str(blocked_preview_pdf) if blocked_preview_pdf else "",
            "blocked_pdf_url": _paper_asset_url(root, blocked_preview_pdf),
            "blocked_tex_path": str(blocked_preview_tex) if blocked_preview_tex else "",
            "blocked_tex_url": _paper_asset_url(root, blocked_preview_tex),
            "blocked_preview_available": bool(blocked_preview_pdf),
            "latest_generated_pdf_path": str(blocked_preview_pdf or display_pdf or latest_output_pdf or raw_orchestra_pdf) if (blocked_preview_pdf or display_pdf or latest_output_pdf or raw_orchestra_pdf) else "",
            "latest_generated_pdf_url": _paper_asset_url(root, blocked_preview_pdf or display_pdf or latest_output_pdf or raw_orchestra_pdf),
            "latest_generated_pdf_info": _file_info(blocked_preview_pdf or display_pdf or latest_output_pdf or raw_orchestra_pdf),
            "latest_generated_tex_path": str(blocked_preview_tex or display_tex or latest_output_tex or raw_orchestra_tex) if (blocked_preview_tex or display_tex or latest_output_tex or raw_orchestra_tex) else "",
            "latest_generated_tex_url": _paper_asset_url(root, blocked_preview_tex or display_tex or latest_output_tex or raw_orchestra_tex),
            "latest_generated_is_accepted_preview": bool(display_pdf),
            "conference_preview_ready": conference_preview_ready,
            "conference_preview_pages": conference_preview_pages,
            "conference_preview_body_pages": conference_preview_body_pages,
            "conference_preview_reference_pages": conference_preview_reference_pages,
            "conference_preview_body_page_limit": conference_preview_body_page_limit,
            "conference_preview_report": paper_state.get("conference_preview_report", "") if isinstance(paper_state, dict) else "",
            "normal_preview_ready": normal_preview_ready,
            "paper_normality_status": paper_normality_status,
            "paper_normality_report": paper_state.get("paper_normality_report", "") if isinstance(paper_state, dict) else "",
            "paper_normality_pages": paper_state.get("paper_normality_pages", "") if isinstance(paper_state, dict) else "",
            "paper_normality_body_pages": paper_state.get("paper_normality_body_pages", "") if isinstance(paper_state, dict) else "",
            "paper_normality_estimated_reference_pages": paper_state.get("paper_normality_estimated_reference_pages", "") if isinstance(paper_state, dict) else "",
            "paper_normality_citation_count": paper_state.get("paper_normality_citation_count", "") if isinstance(paper_state, dict) else "",
            "venue_submission_policy_status": paper_state.get("venue_submission_policy_status", "") if isinstance(paper_state, dict) else "",
            "venue_submission_policy": paper_state.get("venue_submission_policy", {}) if isinstance(paper_state, dict) else {},
            "venue_requirements_summary": venue_requirements,
            "venue_requirements_public_summary": venue_requirements.get("summary", "") if isinstance(venue_requirements, dict) else "",
            "venue_requirements_status": paper_state.get("venue_requirements_status", "") or (venue_requirements.get("status", "") if isinstance(venue_requirements, dict) else "") if isinstance(paper_state, dict) else "",
            "venue_requirements_path": paper_state.get("venue_requirements_path", "") or (venue_requirements.get("path", "") if isinstance(venue_requirements, dict) else "") if isinstance(paper_state, dict) else "",
            "venue_desk_reject_risks": paper_state.get("venue_desk_reject_risks", []) if isinstance(paper_state, dict) else [],
            "paper_venue_format_status": paper_venue_format_status,
            "paper_venue_format_profile": paper_state.get("paper_venue_format_profile", {}) if isinstance(paper_state, dict) else {},
            "paper_venue_format_validation": paper_state.get("paper_venue_format_validation", {}) if isinstance(paper_state, dict) else {},
            "venue_template_format_ready": venue_template_format_ready,
            "paper_figure_quality_status": paper_figure_quality_status,
            "paper_figure_quality_ready": paper_figure_quality_ready,
            "paper_citation_render_status": paper_citation_render_status,
            "paper_citation_render_ready": paper_citation_render_ready,
            "paper_citation_render_blockers": _paper_public_blocker_rows(paper_citation_render_blockers),
            "paper_citation_render_diagnostics": paper_state.get("paper_citation_render_diagnostics", {}) if isinstance(paper_state, dict) else {},
            "paper_self_review_status": paper_self_review_status,
            "paper_self_review_ready": paper_self_review_ready,
            "paper_self_review_receipt": paper_state.get("paper_self_review_receipt", "") if isinstance(paper_state, dict) else "",
            "paper_self_review_blockers": _paper_public_blocker_rows(paper_self_review_blockers),
            "paper_self_review_independent_findings_count": paper_state.get("paper_self_review_independent_findings_count", 0) if isinstance(paper_state, dict) else 0,
            "paper_self_review_repairs_count": paper_state.get("paper_self_review_repairs_count", 0) if isinstance(paper_state, dict) else 0,
            "paper_figure_quality_report": paper_state.get("paper_figure_quality_report", "") if isinstance(paper_state, dict) else "",
            "paper_figure_quality_audit": paper_state.get("paper_figure_quality_audit", "") if isinstance(paper_state, dict) else "",
            "paper_figure_count": paper_state.get("paper_figure_count", "") if isinstance(paper_state, dict) else "",
            "paper_figure_blocker_count": paper_state.get("paper_figure_blocker_count", "") if isinstance(paper_state, dict) else "",
            "paper_figure_warning_count": paper_state.get("paper_figure_warning_count", "") if isinstance(paper_state, dict) else "",
            "paper_figure_failed": paper_state.get("paper_figure_failed", []) if isinstance(paper_state, dict) else [],
            "conference_preview_blockers": _paper_public_blocker_rows(conference_preview_blockers),
            "conference_preview_blocker_summary": preview_blocker_text,
            "paper_layout_footprint_warnings": paper_layout_footprint_warnings,
            "paper_layout_summary": paper_layout_footprint_warnings[0] if paper_layout_footprint_warnings else "",
            "paper_table_count": paper_state.get("paper_table_count", "") if isinstance(paper_state, dict) else "",
            "paper_table_failed": paper_state.get("paper_table_failed", []) if isinstance(paper_state, dict) else [],
            "paper_figure_repair_loop_status": paper_state.get("paper_figure_repair_loop_status", "") if isinstance(paper_state, dict) else "",
            "paper_figure_repair_loop_report": paper_state.get("paper_figure_repair_loop_report", "") if isinstance(paper_state, dict) else "",
            "paper_figure_repair_loop_json": paper_state.get("paper_figure_repair_loop_json", "") if isinstance(paper_state, dict) else "",
            "paper_figure_repair_rounds": paper_state.get("paper_figure_repair_rounds", "") if isinstance(paper_state, dict) else "",
            "paper_preview_repair_loop_status": ("blocked" if not conference_preview_ready else (paper_state.get("paper_preview_repair_loop_status", "") if isinstance(paper_state, dict) else "")),
            "paper_preview_repair_loop_report": paper_state.get("paper_preview_repair_loop_report", "") if isinstance(paper_state, dict) else "",
            "paper_preview_repair_loop_json": paper_state.get("paper_preview_repair_loop_json", "") if isinstance(paper_state, dict) else "",
            "paper_preview_repair_rounds": paper_state.get("paper_preview_repair_rounds", "") if isinstance(paper_state, dict) else "",
            "paper_stage_status": paper_orchestra_bridge_status,
            "paper_generation_skipped": False if generated_but_blocked else paper_generation_skipped,
            "paper_generation_skipped_reason": (paper_state.get("paper_generation_skipped_reason", "") if (paper_generation_skipped and not generated_but_blocked) and isinstance(paper_state, dict) else ""),
            "science_gate_preflight": science_preflight,
            "science_gate_preflight_blockers": science_preflight_blockers,
            "paper_stage_report": paper_state.get("paper_orchestra_bridge_report", "") if isinstance(paper_state, dict) else "",
            "paper_stage_workspace": paper_state.get("paper_orchestra_workspace", "") if isinstance(paper_state, dict) else "",
            "writing_status": paper_orchestra_bridge_status,
            "writing_report": paper_state.get("paper_orchestra_bridge_report", "") if isinstance(paper_state, dict) else "",
            "writing_workspace": paper_state.get("paper_orchestra_workspace", "") if isinstance(paper_state, dict) else "",
            "raw_pdf_path": str(raw_orchestra_pdf) if raw_orchestra_pdf else "",
            "raw_tex_path": str(raw_orchestra_tex) if raw_orchestra_tex else "",
            "template_fetched": _paper_template_fetched(paper_state),
            "full_research_cycle": full_cycle,
            "state": paper_state if isinstance(paper_state, dict) else {},
        },
    }


def _claude_session_status(root: Path) -> dict[str, Any]:
    session = _read_json(root / "state" / "claude_project_session.json", {})
    last = _read_json(root / "state" / "claude_project_session_last_result.json", {})
    transcript = _read_text(root / "reports" / "claude_project_session.md", 20000)
    if not isinstance(session, dict):
        session = {}
    if not isinstance(last, dict):
        last = {}
    session = {
        **session,
        "permission_mode": session.get("permission_mode") or "bypassPermissions",
        "autonomy_label": session.get("autonomy_label") or "yolo / unattended",
    }
    return {
        "enabled": bool(session or last),
        "session": session,
        "last_result": last,
        "transcript_tail": transcript[-12000:] if transcript else "",
        "permission_mode": "bypassPermissions",
        "autonomy_label": "yolo / unattended",
    }




def _claude_status_payload(root: Path) -> dict[str, Any]:
    def safe_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def scalar(src: Any, keys: list[str]) -> dict[str, Any]:
        row = src if isinstance(src, dict) else {}
        out: dict[str, Any] = {}
        for key in keys:
            value = row.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
        return out

    session = safe_dict(_read_json(root / "state" / "claude_project_session.json", {}))
    last_result = safe_dict(_read_json(root / "state" / "claude_project_session_last_result.json", {}))
    latest_receipt = _public_claude_receipt(_latest_claude_receipt(root, session, last_result))
    latest_receipt_by_stage = _public_claude_receipts_by_stage(root)
    latest_status = str(latest_receipt.get("status") or "").strip() if isinstance(latest_receipt, dict) else ""
    session_status = str(session.get("status") or "").strip()
    last_status = str(last_result.get("status") or "").strip()
    if latest_status in {"running", "queued", "cancelling"}:
        status = latest_status
    elif latest_receipt:
        status = latest_status or last_status or session_status
    else:
        status = "historical" if (session or last_result) else ""
    return {
        "enabled": bool(session or last_result or latest_receipt or latest_receipt_by_stage),
        "status": status,
        "session": scalar(session, ["status", "last_stage", "last_return_code", "last_success_at", "updated_at", "repo_path", "session_id"]),
        "last_result": scalar(last_result, ["status", "stage", "return_code", "started_at", "finished_at", "session_id"]),
        "latest_receipt": latest_receipt,
        "latest_receipt_by_stage": latest_receipt_by_stage,
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


_ABSTRACT_UI_CONTROL_RE = re.compile(
    r"(?:\s*(?:show\s+(?:more|less)|read\s+(?:more|less)|显示更多|显示较少|展开|收起)\s*[。.]?\s*)+$",
    re.IGNORECASE,
)


def _strip_abstract_ui_controls(value: object) -> str:
    return _ABSTRACT_UI_CONTROL_RE.sub("", " ".join(str(value or "").split())).strip()


def _clean_literature_abstract(row: dict[str, Any]) -> str:
    text = _strip_abstract_ui_controls(row.get("abstract") or row.get("abstract_en") or row.get("abstract_excerpt") or row.get("summary") or "")
    lowered = text.lower()
    if lowered in {"", "no abstract available", "no abstract available.", "abstract not available", "abstract not available."}:
        return ""
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    source = str(metadata.get("abstract_source") or row.get("abstract_source") or "").lower()
    if "tldr" in source:
        return ""
    tldr = metadata.get("tldr") or row.get("tldr")
    if isinstance(tldr, dict):
        tldr = tldr.get("text")
    if tldr and " ".join(text.split()).casefold() == " ".join(str(tldr).split()).casefold():
        return ""
    return text if len(text) >= 80 else ""


def _has_real_literature_abstract(row: dict[str, Any]) -> bool:
    return bool(isinstance(row, dict) and _clean_literature_abstract(row))


def _display_score_value(row: dict[str, Any], key: str) -> Any:
    raw = row.get(key)
    try:
        value = float(raw)
    except Exception:
        return raw
    if not (value == value):
        return raw
    if value <= 10.0:
        return round(value, 3)
    if key == "score":
        for fallback_key in ["recommendation_score", "llm_fit_score", "fit_score"]:
            try:
                fallback = float(row.get(fallback_key))
            except Exception:
                continue
            if 0.0 <= fallback <= 10.0:
                return round(fallback, 3)
        return 10.0
    return round(min(10.0, max(0.0, value)), 3)


def _human_find_recommendation_literature_row(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("retrieval_pool_only") or row.get("llm_final_scoring_skipped") or row.get("llm_retry_exhausted"):
        return False
    if not (row.get("title") or row.get("id")):
        return False
    if not _has_real_literature_abstract(row):
        return False
    reason_source = str(row.get("reason_source") or "").lower()
    score_source = str(row.get("score_source") or "").lower()
    final_scored = reason_source == "llm abstract evaluation" or score_source == "llm_title_abstract_score_only"
    if not final_scored:
        return False
    recommended = bool(
        row.get("find_recommendation")
        or row.get("recommended_by_llm_ranking")
        or row.get("_user_visible_recommendation")
    )
    has_fit = row.get("llm_fit_score") not in (None, "") or row.get("fit_score") not in (None, "")
    return recommended and has_fit


def _human_positive_literature_row(row: dict[str, Any]) -> bool:
    return _human_find_recommendation_literature_row(row)


def _human_positive_literature_rows(rows: Any) -> list[dict[str, Any]]:
    return [row for row in _json_rows(rows) if _human_positive_literature_row(row)]



def _human_recommendation_literature_rows(rows: Any) -> list[dict[str, Any]]:
    return [row for row in _json_rows(rows) if _human_find_recommendation_literature_row(row)]


def _public_find_recommendation_rows(rows: Any, limit: int = 20) -> list[dict[str, Any]]:
    public_rows: list[dict[str, Any]] = []
    for row in _human_recommendation_literature_rows(rows):
        item = _compact_paper_row(row)
        for key in ["score_source", "reason_source", "find_recommendation", "recommended_by_llm_ranking", "_user_visible_recommendation"]:
            item.pop(key, None)
        for key in ["hit_directions_zh", "hit_directions_en", "hit_directions"]:
            value = row.get(key)
            if value not in (None, "", []):
                item[key] = value
            if isinstance(item.get(key), list):
                item[key] = [_compact_text(value, 120) for value in item[key] if str(value or "").strip()][:4]
        item["public_recommendation"] = True
        item["recommendation_rank"] = len(public_rows) + 1
        public_rows.append(item)
        if len(public_rows) >= limit:
            break
    return public_rows


def _human_readable_literature_row(row: dict[str, Any]) -> bool:
    return _human_find_recommendation_literature_row(row)


def _audit_literature_rows(rows: Any) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for row in _json_rows(rows):
        if not isinstance(row, dict):
            continue
        if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong") or str(row.get("evidence_tier") or "").lower() in {"nethreshold_for_reading", "critique_or_boundary_case", "retrieval_only"}:
            audit.append(row)
    return audit


def _human_readable_literature_rows(rows: Any) -> list[dict[str, Any]]:
    return [row for row in _json_rows(rows) if _human_readable_literature_row(row)]


def _reading_content_view(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    nested = row.get("reading") if isinstance(row.get("reading"), dict) else None
    if not nested:
        return row
    clean = dict(nested)
    for key, value in row.items():
        if key == "reading":
            continue
        if value not in (None, "", []):
            clean.setdefault(key, value)
    return clean


def _title_identity_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201b", "'")
        .replace("`", "'")
        .replace("\u00b4", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return " ".join(text.split())


def _reading_declares_positive(row: dict[str, Any]) -> bool:
    row = _reading_content_view(row)
    text = " ".join(str(row.get(key) or "") for key in ["verdict", "support_role",  "role", "recommendation", "status"]).lower()
    return any(marker in text for marker in ["claim_ready", "claim-ready", "positive_anchor", "positive support", "supporting_evidence", "supporting evidence", "component_reference", "component reference"])


def _reading_declares_critique(row: dict[str, Any]) -> bool:
    row = _reading_content_view(row)
    text = " ".join(str(row.get(key) or "") for key in ["verdict", "support_role",  "role", "critique_reason", "limitations"]).lower()
    markers = [
        "critique", "boundary", "audit", "search_expansion", "search expansion",
        "negative", "misfit", "not_positive", "not positive",
        "foundation_borrowing", "foundation borrowing",
        "transferable_method_reference", "transferable method reference",
        "contrast_or_boundary_reference", "contrast or boundary reference",
        "recommended_reading_boundary", "recommended reading boundary",
        "boundary_audit", "boundary audit", "weak_or_boundary", "weak or boundary",
    ]
    return any(marker in text for marker in markers)


def _find_row_declares_borrowed_or_boundary(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"):
        return True
    text = " ".join(
        str(row.get(key) or "")
        for key in [
              "support_policy",
            "recommendation_note", "recommendation_note_zh", "reason", "reason_zh",
            "fit_explanation", "fit_explanation_zh",
        ]
    ).lower()
    markers = [
        "critique", "boundary", "audit", "search_expansion", "search expansion",
        "negative", "misfit", "not_positive", "not positive",
        "foundation_borrowing", "foundation borrowing", "transferable_method_reference",
        "transferable method reference", "contrast_or_boundary_reference",
        "contrast or boundary reference", "recommended_reading_boundary",
        "recommended reading boundary", "boundary_audit", "boundary audit",
        "weak_or_boundary", "weak or boundary", "critique_or_boundary_case",
        "nethreshold_for_reading", "retrieval_only", "audit_or_search_expansion_only",
    ]
    return any(marker in text for marker in markers)


FULL_TEXT_READ_POLICY_VERSION = "full_text_required_v5_detailed_deep_read"
FULL_TEXT_PENDING_MARKERS = ("pending", "metadata_only", "abstract_only", "abstract only", "unavailable", "failed", "blocked", "not_read", "not read", "needs_full_text", "no_pdf", "no pdf", "missing_pdf", "missing pdf", "bibliographic", "citation_only", "title_only", "dblp_abstract_only", "待补", "不可访问")
FULL_TEXT_MIN_CHARS = 1200
FULL_TEXT_READ_MARKERS = ("full_text_read", "full text read", "pdf_text_read", "pdf text read", "html_text_read", "paper_text_read", "text_extracted", "full_text_available", "全文已读", "正文已读")
FULL_TEXT_CONTENT_PLACEHOLDERS = (
    "中文摘要待补",
    "论文动机待补",
    "动机待补",
    "详细方法待补",
    "方法细节待补",
    "详细方法待中文精读补齐",
    "实验设置与结果待补",
    "实验设置待补",
    "实验设置与结果待中文精读补齐",
    "局限性待补",
    "当前记录未提供中文摘要",
    "当前自动 fallback",
    "全文未读取",
    "待补全文精读",
    "方法差异和优缺点待正文精读后确认",
    "不能仅凭题录或摘要确认",
    "全文文本证据已抓取；但项目代理还没有",
    "全文文本证据已抓取，但",
    "待正文精读",
    "当前可访问正文证据不足",
    "当前可访问证据不足",
)
READ_VISIBLE_BANNED_MARKERS = (
    "project_topic",
    "对实现的直接含义",
    "对 实现",
    "实现",
    "Guardrail",
    "实验与证据限制",
    "摘要级线索",

    "repo/data/env/experiment gate",
    "repo/data/env/experiment",
    "paper conclusions",
    "paper conclusion",
    "论文 claim",
    "paper-conclusion promotion",
)
DEEP_READ_FIELD_MIN_CHARS = {
    "abstract_zh": 260,
    "motivation_zh": 180,
    "method_details_zh": 650,
    "experiments_zh": 420,
    "limitations_zh": 220,
}
DEEP_READ_LIST_MIN_ITEMS = 2
DEEP_READ_LIST_ITEM_MIN_CHARS = 55
RECOMMENDATION_RATIONALE_COPY_SIMILARITY = 0.82


def _positive_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _full_text_status_blob(value: dict[str, Any]) -> str:
    keys = ["full_text_status", "read_status", "claude_read_status", "pdf_status", "source_status", "status", "evidence_status", "source", "evidence_type", "kind", "note"]
    return " ".join(str(value.get(key) or "") for key in keys).lower()


def _full_text_status_is_pending_or_metadata(value: dict[str, Any]) -> bool:
    blob = _full_text_status_blob(value)
    return bool(blob and any(marker in blob for marker in FULL_TEXT_PENDING_MARKERS))


def _full_text_status_is_read(value: dict[str, Any]) -> bool:
    blob = _full_text_status_blob(value)
    return bool(blob and any(marker in blob for marker in FULL_TEXT_READ_MARKERS))


def _full_text_status_is_deep_read_pending(value: dict[str, Any]) -> bool:
    blob = _full_text_status_blob(value)
    markers = (
        "pending_deep_read_synthesis",
        "ready_pending_deep_read",
        "full_text_packet_ready_pending",
        "pending_claude_rewrite",
    )
    return bool(blob and any(marker in blob for marker in markers))


def _has_full_text_locator(value: dict[str, Any]) -> bool:
    for key in ["text_path", "full_text_text_path", "pdf_url", "html_url", "full_text_url", "pdf_path"]:
        if str(value.get(key) or "").strip():
            return True
    return False


def _full_text_evidence_chars(value: dict[str, Any]) -> int:
    if not isinstance(value, dict) or _full_text_status_is_pending_or_metadata(value):
        return 0
    for key in ["pdf_text_chars", "full_text_chars", "text_chars", "source_text_chars", "body_text_chars", "chars", "character_count"]:
        chars = _positive_int(value.get(key))
        if chars >= FULL_TEXT_MIN_CHARS:
            return chars
    return 0


def _full_text_evidence_dicts(row: dict[str, Any]) -> list[dict[str, Any]]:
    evidences: list[dict[str, Any]] = []
    for key in ["source_evidence", "full_text_evidence", "pdf_evidence", "text_evidence"]:
        value = row.get(key)
        if isinstance(value, dict):
            evidences.append(value)
    for key in ["source_evidences", "full_text_evidences", "evidence_sources"]:
        value = row.get(key)
        if isinstance(value, list):
            evidences.extend(item for item in value if isinstance(item, dict))
    return evidences


def _reading_has_full_text_evidence(row: dict[str, Any]) -> bool:
    row = _reading_content_view(row)
    if not isinstance(row, dict):
        return False
    status_allows_evidence = (
        not _full_text_status_is_pending_or_metadata(row)
        or _full_text_status_is_deep_read_pending(row)
        or _full_text_status_is_read(row)
        or _has_full_text_locator(row)
    )
    if status_allows_evidence:
        for key in ["pdf_text_chars", "full_text_chars", "text_chars", "body_text_chars"]:
            if _positive_int(row.get(key)) >= FULL_TEXT_MIN_CHARS:
                return True
    if _positive_int(row.get("source_text_chars")) >= FULL_TEXT_MIN_CHARS and (_full_text_status_is_read(row) or _has_full_text_locator(row) or _full_text_status_is_deep_read_pending(row)):
        return True
    for evidence in _full_text_evidence_dicts(row):
        if _full_text_evidence_chars(evidence) >= FULL_TEXT_MIN_CHARS:
            return True
    return False


def _contains_cjk_text(value: Any) -> bool:
    return bool(re.search(r"[一-鿿]", str(value or "")))


def _field_text(value: Any, limit: int = 5000) -> str:
    if isinstance(value, list):
        value = "\n".join(str(item or "") for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = " ".join(str(value or "").replace("\r", "\n").split()).strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."

def _nonspace_len(text: Any) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _sentence_like_count(text: Any) -> int:
    clean = str(text or "").strip()
    if not clean:
        return 0
    parts = [part for part in re.split(r"[。！？!?；;]\s*", clean) if part.strip()]
    return max(1, len(parts))


SCIENTIFIC_CHINESE_NUMERAL_RE = re.compile(
    r"(?:百分之[零一二三四五六七八九十百千万点两]+|[零一二三四五六七八九十两]+点[零一二三四五六七八九十]+|[一二三四五六七八九十两]+乘以十的[负正]?[一二三四五六七八九十两]+次方|K为[一二三四五六七八九十两]+)"
)
SCIENTIFIC_CONTEXT_RE = re.compile(
    r"(?:NDCG|HR|Hit|Recall|Precision|AUC|MRR|DCG|p值|p-value|top-K|K为|命中率|提升|下降|达到|相对|指标|数据集|基线|消融|学习率|温度|批次|参数|GPU|GB|延迟|用户数量|物品数量|交互数量)",
    re.IGNORECASE,
)
PUBLIC_PLACEHOLDER_LEAK_RE = re.compile(r"(?:@@TASTE_|TASTE_INLINE|\[\[(?:LATEX|PRESERVE|TASTE)[^\]]*\]\])")


def _scientific_notation_style_gap(field: str, value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if PUBLIC_PLACEHOLDER_LEAK_RE.search(text):
        return "含有内部公式/占位符标记；必须重新生成，用户可见产物不能泄漏 TASTE/LATEX placeholder"
    if SCIENTIFIC_CHINESE_NUMERAL_RE.search(text) and SCIENTIFIC_CONTEXT_RE.search(text):
        return "科学数字、百分比、p 值、K 值、指标、模型规模、超参数和实验结果必须保留阿拉伯数字/原始符号，不得写成“零点/百分之/一乘以十”等中文数字"
    return ""


def _keyword_group_count(text: str, groups: list[tuple[str, ...]]) -> int:
    return sum(1 for group in groups if any(token in text for token in group))


def _similarity(a: Any, b: Any) -> float:
    left = re.sub(r"\s+", "", str(a or "").lower())
    right = re.sub(r"\s+", "", str(b or "").lower())
    if not left or not right:
        return 0.0
    if len(left) < 40 or len(right) < 40:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _looks_like_recommendation_rationale_copy(row: dict[str, Any], abstract_zh: str) -> bool:
    # Find may already contain the paper's original abstract. That abstract can
    # seed abstract_zh; recommendation rationales and workflow notes cannot.
    for key in [
        "recommendation_summary",
        "recommendation_note",
        "recommendation_note_zh",
        "reason",
        "reason_zh",
        "fit_explanation",
        "fit_explanation_zh",
        "relevance",
        "critique_reason",
        "reading_status_note_zh",
    ]:
        source = _field_text(row.get(key), 1800)
        if not source:
            continue
        if _similarity(abstract_zh, source) >= RECOMMENDATION_RATIONALE_COPY_SIMILARITY:
            return True
        left = re.sub(r"\s+", "", abstract_zh)
        right = re.sub(r"\s+", "", source)
        if len(left) >= 60 and len(right) >= 60 and (left in right or right in left):
            return True
    return False


def _deep_read_public_talk_markers() -> tuple[str, ...]:
    return READ_VISIBLE_BANNED_MARKERS + (
        "当前项目配置的主题轴",
        "当前用户可见推荐文章",
        "摘要级线索",
        "后续精读",
        "进入精读",
        "本地证据才能支撑",
        "repo/data/env",
        "environment gate",
        "experiment gate",
    )


def _deep_read_field_ok(value: Any, min_chars: int) -> bool:
    text = _field_text(value, max(min_chars * 8, 2600))
    if _nonspace_len(text) < min_chars:
        return False
    if not _contains_cjk_text(text):
        return False
    if _sentence_like_count(text) < 2 and min_chars >= 180:
        return False
    if any(marker in text for marker in FULL_TEXT_CONTENT_PLACEHOLDERS):
        return False
    if any(marker in text for marker in _deep_read_public_talk_markers()):
        return False
    if _scientific_notation_style_gap("field", text):
        return False
    return True


def _deep_read_list_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in values:
        text = _field_text(item, 1000)
        if text:
            out.append(text)
    return out


def _deep_read_list_ok(value: Any) -> bool:
    items = [item for item in _deep_read_list_values(value) if _deep_read_field_ok(item, DEEP_READ_LIST_ITEM_MIN_CHARS)]
    return len(items) >= DEEP_READ_LIST_MIN_ITEMS


def _field_specific_deep_read_gaps(row: dict[str, Any]) -> list[str]:
    row = _reading_content_view(row)
    gaps: list[str] = []
    abstract = _field_text(row.get("abstract_zh") or row.get("summary"), 2200)
    motivation = _field_text(row.get("motivation_zh") or row.get("problem"), 2200)
    method = _field_text(row.get("method_details_zh") or row.get("method"), 3200)
    experiments = _field_text(row.get("experiments_zh") or row.get("experiments"), 2800)
    limitations = _field_text(row.get("limitations_zh") or row.get("limitations"), 2200)
    if abstract:
        if _looks_like_recommendation_rationale_copy(row, abstract):
            gaps.append("abstract_zh: 原论文摘要必须呈现论文原摘要的中文内容，不能用推荐理由、主题命中、流程说明或 critique_reason 冒充")
        if _sentence_like_count(abstract) < 2 or _nonspace_len(abstract) < DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]:
            gaps.append("abstract_zh: 原论文摘要过短；必须用中文概括论文问题、方法和主要发现，不能只写一句推荐摘要")
    if motivation and (_sentence_like_count(motivation) < 2 or _nonspace_len(motivation) < DEEP_READ_FIELD_MIN_CHARS["motivation_zh"]):
        gaps.append("motivation_zh: 论文动机过短；必须说明论文要解决的具体矛盾、已有方法不足和任务背景")
    if method:
        method_groups = [
            ("模型", "框架", "架构", "模块", "网络", "专家", "门控"),
            ("训练", "优化", "损失", "目标", "学习", "偏好", "奖励"),
            ("推理", "采样", "反向", "去噪", "扩散", "生成", "解码"),
            ("输入", "输出", "表示", "token", "用户", "物品", "序列", "轨迹"),
        ]
        if _keyword_group_count(method, method_groups) < 3:
            gaps.append("method_details_zh: 详细方法缺少模型结构、训练目标、推理/采样流程、输入输出等关键机制信息")
        if _sentence_like_count(method) < 4:
            gaps.append("method_details_zh: 详细方法必须是多句中文 synthesis，不能只列缩写或一句公式摘要")
    if experiments:
        experiment_groups = [
            ("数据", "dataset", "数据集", "任务", "文本到图像", "视频", "推荐"),
            ("基线", "baseline", "对照", "比较", "方法"),
            ("指标", "metric", "Recall", "NDCG", "HR", "AUC", "准确", "胜率", "偏好"),
            ("结果", "提升", "显著", "消融", "ablation", "分析", "表"),
        ]
        if _keyword_group_count(experiments, experiment_groups) < 3:
            gaps.append("experiments_zh: 实验设置与结果必须覆盖数据/任务、基线或对照、指标、主要结果或消融")
        if _sentence_like_count(experiments) < 4:
            gaps.append("experiments_zh: 实验设置与结果必须是多句中文 synthesis，不能只列任务名或一句结论")
    if limitations and (_sentence_like_count(limitations) < 3 or _nonspace_len(limitations) < DEEP_READ_FIELD_MIN_CHARS["limitations_zh"]):
        gaps.append("limitations_zh: 局限性必须结合正文说明实验边界、适用范围和迁移风险，不能只写一句限制")
    for field_name, field_value in [
        ("abstract_zh", abstract),
        ("motivation_zh", motivation),
        ("method_details_zh", method),
        ("experiments_zh", experiments),
        ("limitations_zh", limitations),
    ]:
        notation_gap = _scientific_notation_style_gap(field_name, field_value)
        if notation_gap:
            gaps.append(f"{field_name}: {notation_gap}")
    if not _deep_read_list_ok(row.get("method_advantages_zh")):
        gaps.append("method_advantages_zh: 每篇论文必须写至少两条具体方法优点，每条为中文且不能是占位话术")
    if not _deep_read_list_ok(row.get("method_disadvantages_zh")):
        gaps.append("method_disadvantages_zh: 每篇论文必须写至少两条具体方法不足/局限，每条为中文且不能是占位话术")
    return gaps


def _reading_deep_read_content_gaps(row: dict[str, Any]) -> list[str]:
    row = _reading_content_view(row)
    if not isinstance(row, dict):
        return ["reading row is not an object"]
    checks = [
        ("abstract_zh", row.get("abstract_zh") or row.get("deep_read_abstract_zh") or row.get("summary"), DEEP_READ_FIELD_MIN_CHARS["abstract_zh"], "原论文摘要必须写入 abstract_zh 或 summary，且为中文；可直接使用/翻译 Find 捕获的论文原摘要，但推荐理由、主题命中和流程说明不能替代该字段"),
        ("motivation_zh", row.get("motivation_zh") or row.get("problem"), DEEP_READ_FIELD_MIN_CHARS["motivation_zh"], "论文动机必须写入 motivation_zh 或 problem，且为中文全文 synthesis；relevance 枚举值不能替代动机"),
        ("method_details_zh", row.get("method_details_zh") or row.get("method"), DEEP_READ_FIELD_MIN_CHARS["method_details_zh"], "详细方法必须写入 method_details_zh 或 method，且为中文全文 synthesis"),
        ("experiments_zh", row.get("experiments_zh") or row.get("experiments"), DEEP_READ_FIELD_MIN_CHARS["experiments_zh"], "实验设置与结果必须写入 experiments_zh 或 experiments，且为中文全文 synthesis"),
        ("limitations_zh", row.get("limitations_zh") or row.get("limitations"), DEEP_READ_FIELD_MIN_CHARS["limitations_zh"], "局限性必须写入 limitations_zh 或 limitations，且为中文全文 synthesis"),
    ]
    gaps: list[str] = []
    for field, value, min_chars, message in checks:
        if not _deep_read_field_ok(value, min_chars):
            gaps.append(f"{field}: {message}")
    gaps.extend(_field_specific_deep_read_gaps(row))
    return gaps


def _reading_has_full_text_content(row: dict[str, Any]) -> bool:
    return not _reading_deep_read_content_gaps(row)


def _identity_values(row: dict[str, Any]) -> set[str]:
    row = _reading_content_view(row)
    values: set[str] = set()
    title = _title_identity_key(row.get("title") or row.get("paper_title"))
    if title:
        values.add(f"title:{title}")
    for key in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url"]:
        value = str(row.get(key) or "").strip().lower()
        if value:
            values.add(f"{key}:{value}")
    return values


def _find_row_original_abstract(row: dict[str, Any]) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["abstract_zh", "summary_zh", "abstract_cn", "abstract_chinese", "abstract", "abstract_en", "summary"]:
        text = _field_text(row.get(key), 2400)
        if text and not any(marker in text for marker in FULL_TEXT_CONTENT_PLACEHOLDERS):
            return text
    return ""


def _merge_find_abstract_into_reading(row: dict[str, Any], find_row: dict[str, Any] | None) -> dict[str, Any]:
    clean = dict(_reading_content_view(row)) if isinstance(row, dict) else {}
    find_abstract = _find_row_original_abstract(find_row or {})
    if not find_abstract:
        return clean
    current = _field_text(clean.get("abstract_zh") or clean.get("summary"), 2400)
    current_len = _nonspace_len(current)
    find_len = _nonspace_len(find_abstract)
    use_find = (
        not current
        or not _deep_read_field_ok(current, DEEP_READ_FIELD_MIN_CHARS["abstract_zh"])
        or _looks_like_recommendation_rationale_copy(clean, current)
        or find_len >= max(current_len + 80, int(current_len * 1.15))
    )
    if use_find:
        clean["abstract_zh"] = find_abstract
        clean["summary"] = find_abstract
    existing_abstract_from_find = _field_text(clean.get("abstract_from_find"), 2400)
    if (not existing_abstract_from_find) or find_len >= max(_nonspace_len(existing_abstract_from_find) + 80, int(max(1, _nonspace_len(existing_abstract_from_find)) * 1.15)):
        clean["abstract_from_find"] = find_abstract
    existing_find_abstract_zh = _field_text(clean.get("find_abstract_zh"), 2400)
    if (not existing_find_abstract_zh) or find_len >= max(_nonspace_len(existing_find_abstract_zh) + 80, int(max(1, _nonspace_len(existing_find_abstract_zh)) * 1.15)):
        clean["find_abstract_zh"] = find_abstract
    return clean


def _current_find_validation_row_key(row: dict[str, Any]) -> str:
    identities = sorted(_identity_values(row))
    if identities:
        return identities[0]
    return _title_identity_key(row.get("title") or row.get("paper_title"))


def _current_find_validation_recommendation_rows(find_results: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool, role in [("strong_recommendations", "user_visible_recommendation"), ("articles", "user_visible_article")]:
        for index, raw in enumerate(_json_rows((find_results or {}).get(pool, [])), 1):
            if not isinstance(raw, dict):
                continue
            if not _human_find_recommendation_literature_row(raw):
                continue
            key = _current_find_validation_row_key(raw)
            if not key or key in seen:
                continue
            row = dict(raw)
            row.setdefault("taste_pool", pool)
            row.setdefault("taste_pool_role", role)
            row.setdefault("taste_pool_rank", index)
            row["recommended_for_deep_reading"] = True
            rows.append(row)
            seen.add(key)
    return rows


def _full_text_packet_index_for_current_find(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not isinstance(packet, dict):
        return index
    for entry in _json_rows(packet.get("papers", [])):
        if not isinstance(entry, dict):
            continue
        for identity in _identity_values(entry):
            index[identity] = entry
    return index


def _current_find_row_has_packet_body(row: dict[str, Any], packet_index: dict[str, dict[str, Any]]) -> bool:
    entry = next((packet_index[key] for key in _identity_values(row) if key in packet_index), {})
    return bool(entry and _has_full_text_locator(entry) and _full_text_evidence_chars(entry) >= FULL_TEXT_MIN_CHARS)


def _packet_entry_has_body_for_web(entry: dict[str, Any]) -> bool:
    return bool(isinstance(entry, dict) and _has_full_text_locator(entry) and _full_text_evidence_chars(entry) >= FULL_TEXT_MIN_CHARS)


def _read_stage_replacement_entry_for_web(original_row: dict[str, Any], full_text_packet: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(full_text_packet, dict):
        return {}
    original_title_key = _title_identity_key(original_row.get("title") or original_row.get("paper_title"))
    if not original_title_key:
        return {}
    for entry in _json_rows(full_text_packet.get("papers", [])):
        if not isinstance(entry, dict) or not entry.get("read_replacement"):
            continue
        replacement_for = entry.get("replacement_for_unavailable_recommendation")
        if isinstance(replacement_for, dict):
            replacement_for_title = replacement_for.get("title") or replacement_for.get("paper_title")
        else:
            replacement_for_title = replacement_for
        replaced_title_key = _title_identity_key(replacement_for_title or entry.get("replacement_for") or entry.get("replacement_for_title"))
        if replaced_title_key != original_title_key:
            continue
        if _packet_entry_has_body_for_web(entry):
            return entry
    return {}


def _reading_row_from_replacement_entry_for_web(original_row: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    row = dict(entry)
    row.setdefault("title", entry.get("title") or entry.get("paper_title") or "")
    row.setdefault("paper_title", row.get("title") or "")
    row.setdefault("url", entry.get("url") or entry.get("html_url") or entry.get("pdf_url") or "")
    row.setdefault("pdf_url", entry.get("pdf_url") or "")
    row.setdefault("taste_pool", entry.get("replacement_source_pool") or "read_stage_full_text_replacement")
    row.setdefault("taste_pool_rank", entry.get("replacement_source_rank") or original_row.get("taste_pool_rank") or 0)
    row["reading_packet_role"] = "read_stage_full_text_replacement"
    row["recommended_for_deep_reading"] = True
    row["replacement_for_unavailable_recommendation"] = entry.get("replacement_for_unavailable_recommendation") or {"title": original_row.get("title") or original_row.get("paper_title") or ""}
    row["read_replacement"] = True
    return row


def _current_find_reading_packet_rows_for_web(find_results: dict[str, Any], full_text_packet: dict[str, Any] | None, read_limit: int = 0) -> list[dict[str, Any]]:
    recommendations = _current_find_validation_recommendation_rows(find_results if isinstance(find_results, dict) else {})
    target = read_limit if read_limit and read_limit > 0 else len(recommendations)
    if not target:
        return recommendations
    packet = full_text_packet if isinstance(full_text_packet, dict) else {}
    packet_index = _full_text_packet_index_for_current_find(packet)
    rows: list[dict[str, Any]] = []
    for row in recommendations[:target]:
        clean = dict(row)
        if packet_index and _current_find_row_has_packet_body(row, packet_index):
            clean["reading_packet_role"] = "original_recommendation_with_full_text"
            rows.append(clean)
            continue
        replacement_entry = _read_stage_replacement_entry_for_web(row, packet)
        if replacement_entry:
            rows.append(_reading_row_from_replacement_entry_for_web(row, replacement_entry))
        else:
            clean["reading_packet_role"] = "unavailable_original_recommendation"
            rows.append(clean)
    return rows


def _current_find_reading_validation_view_for_web(find_results: dict[str, Any], full_text_packet: dict[str, Any] | None, read_limit: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    source = find_results if isinstance(find_results, dict) else {}
    original_rows = _current_find_validation_recommendation_rows(source)
    target = read_limit if read_limit and read_limit > 0 else len(original_rows)
    reading_rows = _current_find_reading_packet_rows_for_web(source, full_text_packet if isinstance(full_text_packet, dict) else {}, target)
    validation_find_results = dict(source)
    validation_find_results["strong_recommendations"] = [dict(row) for row in reading_rows]
    validation_find_results["articles"] = []
    unavailable_rows = [row for row in reading_rows if row.get("reading_packet_role") == "unavailable_original_recommendation"]
    replacement_rows = [row for row in reading_rows if row.get("reading_packet_role") == "read_stage_full_text_replacement"]
    validation_find_results["current_reading_packet"] = {
        "source": "current_find_reading_packet_read_stage_full_text_selection_guard",
        "paper_count": len(reading_rows),
        "replacement_count": len(replacement_rows),
        "replacement_titles": [str(row.get("title") or row.get("paper_title") or "").strip() for row in replacement_rows],
        "unavailable_original_recommendation_count": len(unavailable_rows),
        "unavailable_original_recommendation_titles": [str(row.get("title") or row.get("paper_title") or "").strip() for row in unavailable_rows],
        "policy": "Find Top-N remains immutable; Read coverage is measured on the full-text reading packet after same-run replacement selection.",
    }
    return original_rows, reading_rows, validation_find_results


def _current_find_reading_validation(find_results: dict[str, Any], readings: list[dict[str, Any]], read_limit: int = 10) -> dict[str, Any]:
    recommendation_ids: set[str] = set()
    recommendation_index: dict[str, dict[str, Any]] = {}
    recommendation_titles: list[str] = []
    seen_recommendation_titles: set[str] = set()
    for pool in ["strong_recommendations", "articles"]:
        for row in _json_rows(find_results.get(pool, [])):
            if not _human_find_recommendation_literature_row(row):
                continue
            identities = _identity_values(row)
            recommendation_ids.update(identities)
            for identity in identities:
                recommendation_index[identity] = row
            title_key = _title_identity_key(row.get("title"))
            if title_key and title_key not in seen_recommendation_titles:
                seen_recommendation_titles.add(title_key)
                recommendation_titles.append(str(row.get("title") or ""))

    positive_ids: set[str] = set()
    positive_titles: list[str] = []
    seen_titles: set[str] = set()
    for pool in ["strong_recommendations", "articles"]:
        for row in _json_rows(find_results.get(pool, [])):
            if not _human_positive_literature_row(row):
                continue
            positive_ids.update(_identity_values(row))
            title_key = _title_identity_key(row.get("title"))
            if title_key and title_key not in seen_titles:
                seen_titles.add(title_key)
                positive_titles.append(str(row.get("title") or ""))
    known_ids: set[str] = set()
    for pool in ["strong_recommendations", "articles", "read_candidates", "triage_candidates", "audit_candidates", "evaluated_candidates", "critique_candidates", "screened_ranking", "title_candidates", "retrieval_candidates", "arxiv_prefiltered"]:
        for row in _json_rows(find_results.get(pool, [])):
            known_ids.update(_identity_values(row))
    positive_readings: list[str] = []
    critique_readings: list[str] = []
    full_text_readings: list[str] = []
    full_text_evidence_titles: list[str] = []
    pending_full_text_readings: list[str] = []
    pending_deep_read_synthesis: list[str] = []
    pending_without_evidence: list[str] = []
    deep_read_content_gap_details: list[dict[str, Any]] = []
    invalid_positive: list[str] = []
    unlabeled_non_positive: list[str] = []
    unknown: list[str] = []
    extra_readings: list[str] = []
    present_positive: set[str] = set()
    present_recommendations: set[str] = set()
    reading_title_counts: dict[str, int] = {}
    for raw_row in readings:
        if not isinstance(raw_row, dict):
            continue
        row = _reading_content_view(raw_row)
        title = str(row.get("title") or row.get("paper_title") or "Untitled").strip()
        ids = _identity_values(row)
        is_recommended = bool(ids & recommendation_ids)
        is_positive = bool(ids & positive_ids)
        is_known = bool(ids & known_ids)
        find_row = next((recommendation_index[key] for key in ids if key in recommendation_index), {})
        if find_row:
            row = _merge_find_abstract_into_reading(row, find_row)
        title_key = _title_identity_key(title)
        if title_key:
            reading_title_counts[title_key] = reading_title_counts.get(title_key, 0) + 1
        if is_recommended and title_key:
            present_recommendations.add(title_key)
            has_evidence = _reading_has_full_text_evidence(row)
            content_gaps = _reading_deep_read_content_gaps(row)
            has_content = not content_gaps
            if has_evidence:
                full_text_evidence_titles.append(title)
            if has_evidence and has_content:
                full_text_readings.append(title)
            elif has_evidence:
                pending_deep_read_synthesis.append(title)
                if len(deep_read_content_gap_details) < 20:
                    deep_read_content_gap_details.append({"title": title, "missing_or_invalid_fields": content_gaps})
                pending_full_text_readings.append(title)
            else:
                pending_without_evidence.append(title)
                pending_full_text_readings.append(title)
        if not is_recommended:
            extra_readings.append(title)
        if is_positive:
            positive_readings.append(title)
            key = _title_identity_key(title)
            if key:
                present_positive.add(key)
        elif _reading_declares_positive(row):
            invalid_positive.append(title)
        elif _reading_declares_critique(row) or (not is_positive and _find_row_declares_borrowed_or_boundary(find_row)):
            critique_readings.append(title)
        else:
            unlabeled_non_positive.append(title)
        if not is_known:
            unknown.append(title)
    expected_titles = recommendation_titles or positive_titles
    missing_recommendations = [title for title in expected_titles if _title_identity_key(title) not in present_recommendations]
    for title in missing_recommendations:
        if title and title not in pending_full_text_readings:
            pending_without_evidence.append(title)
            pending_full_text_readings.append(title)
    missing_positive = [title for title in positive_titles if _title_identity_key(title) not in present_positive]
    duplicate_readings = [title for title in expected_titles if reading_title_counts.get(_title_identity_key(title), 0) > 1]
    expected_count = len(expected_titles)
    actual_count = len(readings)
    blockers: list[str] = []
    if expected_count and actual_count != expected_count:
        blockers.append("readings count must equal current Read-stage packet entries")
    if extra_readings:
        blockers.append("readings include papers outside the current Read-stage packet or allowed audit pools")
    if duplicate_readings:
        blockers.append("readings contain duplicate Read-stage packet entries")
    if invalid_positive:
        blockers.append("non-strong readings were labelled positive")
    if unlabeled_non_positive:
        blockers.append("non-strong readings lack critique/boundary/audit role")
    if unknown:
        blockers.append("readings include papers absent from current Find")
    if missing_recommendations:
        blockers.append("not all current Read-stage packet entries were read")
    if missing_positive:
        blockers.append("not all current strict positive anchors were read")
    if pending_full_text_readings:
        if pending_deep_read_synthesis:
            blockers.append("recommended readings have full-text packets but still need Claude Code deep-read synthesis in read_results/read.md")
            if deep_read_content_gap_details:
                blockers.append("recommended readings lack required Chinese deep-read JSON fields: abstract_zh, motivation_zh, method_details_zh/method, experiments_zh/experiments, limitations_zh/limitations")
        if pending_without_evidence:
            blockers.append("recommended readings still lack full-text evidence; Claude Code must read the full paper/PDF/page before marking deep reading complete")
    if len(readings) < min(read_limit, max(len(expected_titles), 1)):
        blockers.append("readings below current Find coverage requirement")
    return {
        "valid": not blockers,
        "policy_version": FULL_TEXT_READ_POLICY_VERSION,
        "expected_recommendation_count": expected_count,
        "actual_reading_count": actual_count,
        "full_text_reading_count": len(full_text_readings),
        "full_text_evidence_count": len(full_text_evidence_titles),
        "pending_deep_read_synthesis_count": len(pending_deep_read_synthesis),
        "pending_full_text_reading_count": len(pending_full_text_readings),
        "pending_without_evidence_count": len(pending_without_evidence),
        "full_text_reading_titles": full_text_readings[:12],
        "full_text_evidence_titles": full_text_evidence_titles[:12],
        "pending_deep_read_synthesis_titles": pending_deep_read_synthesis[:12],
        "deep_read_content_gap_details": deep_read_content_gap_details[:20],
        "pending_without_evidence_titles": pending_without_evidence[:12],
        "pending_full_text_reading_titles": pending_full_text_readings[:12],
        "expected_positive_count": len(positive_titles),
        "positive_anchor_count": len(positive_readings),
        "critique_or_boundary_count": len(critique_readings),
        "invalid_positive_count": len(invalid_positive),
        "unknown_count": len(unknown),
        "unlabeled_non_positive_count": len(unlabeled_non_positive),
        "expected_positive_titles": positive_titles,
        "positive_anchor_titles": positive_readings,
        "critique_or_boundary_titles": critique_readings,
        "invalid_positive_titles": invalid_positive[:12],
        "unknown_reading_titles": unknown[:12],
        "extra_reading_titles": extra_readings[:12],
        "duplicate_reading_titles": duplicate_readings[:12],
        "missing_recommendation_titles": missing_recommendations[:12],
        "unlabeled_non_positive_titles": unlabeled_non_positive[:12],
        "missing_positive_titles": missing_positive[:12],
        "blockers": blockers,
        "expected_recommendation_titles": expected_titles[:12],
        "policy": "Read results are validated on the Read-stage reading packet for the current Find run. Each packet entry needs title/author-verified full-text/PDF/page evidence or an eligible same-run replacement plus Chinese deep-read synthesis fields before it counts as completed deep reading; non-packet pools are audit/search-expansion and must not inflate deep-reading counts.",
    }


def _current_find_pipeline_summary(root: Path, find_results: dict[str, Any] | None = None) -> dict[str, Any]:
    taste_dir = root / "planning" / "finding"
    find_results = find_results if isinstance(find_results, dict) else _current_find_results_light(root, root.name)
    read_results = _read_json(taste_dir / "read_results.json", {})
    ideas_results = _read_json(taste_dir / "ideas.json", {})
    plans_results = _read_json(taste_dir / "plans.json", {})
    state_plan = _read_json(root / "state" / "current_find_research_plan.json", {})
    experiment_plan = _read_json(root / "state" / "experiment_plan.json", {})
    required_source = "claude_code_current_find_takeover"

    def payload_run_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or payload.get("current_find_run_id") or "").strip()

    state_run_id = payload_run_id(state_plan)
    find_run_id = payload_run_id(find_results)
    run_id = str(find_run_id or state_run_id or "")

    def same_current_run_payload(payload: Any) -> bool:
        payload_id = payload_run_id(payload)
        return bool(run_id and payload_id and payload_id == run_id)

    state_plan_matches = same_current_run_payload(state_plan)
    if not same_current_run_payload(read_results) and state_plan_matches and isinstance(state_plan.get("readings"), list):
        read_results = {"run_id": run_id, "source": state_plan.get("source") or required_source, "readings": state_plan.get("readings")}
    if not same_current_run_payload(read_results):
        read_results = {}
    if not same_current_run_payload(ideas_results) and state_plan_matches and isinstance(state_plan.get("ideas"), list):
        ideas_results = {"run_id": run_id, "source": state_plan.get("source") or required_source, "ideas": state_plan.get("ideas")}
    if not same_current_run_payload(ideas_results):
        ideas_results = {}
    if not same_current_run_payload(plans_results) and state_plan_matches and isinstance(state_plan.get("plans"), list):
        plans_results = {
            "run_id": run_id,
            "source": state_plan.get("source") or required_source,
            "plans": state_plan.get("plans"),
            "selected_idea_id": state_plan.get("selected_idea_id") or "",
            "selected_plan_id": state_plan.get("selected_plan_id") or "",
            "selected_idea": state_plan.get("selected_idea") or {},
            "selected_plan": state_plan.get("selected_plan") or {},
            "selected_by": state_plan.get("selected_by") or "",
            "execution_policy": state_plan.get("execution_policy") or {},
        }
    if not same_current_run_payload(plans_results):
        plans_results = {}
    readings = _json_rows(read_results.get("readings", [])) if isinstance(read_results, dict) else []
    ideas = _json_rows(ideas_results.get("ideas", [])) if isinstance(ideas_results, dict) else []
    plans = _json_rows(plans_results.get("plans", [])) if isinstance(plans_results, dict) else []
    read_source = str(read_results.get("source") or "") if isinstance(read_results, dict) else ""
    idea_source = str(ideas_results.get("source") or "") if isinstance(ideas_results, dict) else ""
    plan_source = str(plans_results.get("source") or "") if isinstance(plans_results, dict) else ""
    current_read_artifact = same_current_run_payload(read_results)
    current_idea_artifact = same_current_run_payload(ideas_results)
    current_plan_artifact = same_current_run_payload(plans_results)

    def execution_truthy(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "selected", "approved", "accept", "accepted", "pass", "passed", "ready", "done", "completed"}

    def row_selected_for_execution(row: dict[str, Any]) -> bool:
        if any(execution_truthy(row.get(key)) for key in ["selected_for_execution", "execute_next", "primary", "selected", "best_plan", "best_idea"]):
            return True
        selection = row.get("execution_selection") if isinstance(row.get("execution_selection"), dict) else {}
        return any(execution_truthy(selection.get(key)) for key in ["selected", "selected_for_execution", "execute_next", "primary"])

    def idea_approved_for_planning(row: dict[str, Any]) -> bool:
        status = str(row.get("status") or row.get("decision") or "").strip().lower()
        return status in {"approved", "accepted", "accept", "pass", "passed", "selected", "ready"} or row_selected_for_execution(row)

    persisted_selected_idea_id = str(
        (state_plan.get("selected_idea_id") if isinstance(state_plan, dict) else "")
        or (ideas_results.get("selected_idea_id") if isinstance(ideas_results, dict) else "")
        or (plans_results.get("selected_idea_id") if isinstance(plans_results, dict) else "")
        or ""
    ).strip()
    persisted_selected_plan_id = str(
        (state_plan.get("selected_plan_id") if isinstance(state_plan, dict) else "")
        or (plans_results.get("selected_plan_id") if isinstance(plans_results, dict) else "")
        or ""
    ).strip()
    approved_ideas = [
        row for row in ideas
        if isinstance(row, dict)
        and (
            idea_approved_for_planning(row)
            or (persisted_selected_idea_id and str(row.get("id") or row.get("idea_id") or "").strip() == persisted_selected_idea_id)
        )
    ]
    approved_idea_ids = {str(row.get("id") or row.get("idea_id") or "").strip() for row in approved_ideas if str(row.get("id") or row.get("idea_id") or "").strip()}
    plans_for_approved_ideas = [
        row for row in plans
        if isinstance(row, dict)
        and (
            not approved_idea_ids
            or str(row.get("idea_id") or "").strip() in approved_idea_ids
            or (persisted_selected_plan_id and str(row.get("plan_id") or row.get("id") or "").strip() == persisted_selected_plan_id)
        )
    ]
    ideas_ready = bool(current_idea_artifact and ideas and (approved_ideas or persisted_selected_plan_id))
    plans_ready = bool(current_plan_artifact and plans and (plans_for_approved_ideas or persisted_selected_plan_id))
    selected_execution = _current_find_selected_execution_summary(root) if readings else {}
    if not str(selected_execution.get("selected_plan_id") or "").strip() and persisted_selected_plan_id:
        selected_plan_row = state_plan.get("selected_plan") if isinstance(state_plan.get("selected_plan"), dict) else next((row for row in plans if str(row.get("plan_id") or row.get("id") or "").strip() == persisted_selected_plan_id), {})
        selected_idea_row = state_plan.get("selected_idea") if isinstance(state_plan.get("selected_idea"), dict) else next((row for row in ideas if str(row.get("id") or row.get("idea_id") or "").strip() == persisted_selected_idea_id), {})
        selected_execution = {
            "required": True,
            "status": "selected_plan_ready",
            "run_id": run_id,
            "selected_plan_id": persisted_selected_plan_id,
            "selected_idea_id": persisted_selected_idea_id,
            "selected_plan": selected_plan_row if isinstance(selected_plan_row, dict) else {},
            "selected_idea": selected_idea_row if isinstance(selected_idea_row, dict) else {},
            "selected_by": str(state_plan.get("selected_by") or "state_current_find_research_plan"),
            "selection_issue": "",
            "execution_policy": state_plan.get("execution_policy") if isinstance(state_plan.get("execution_policy"), dict) else {},
            "candidate_counts": {"ideas": len(ideas), "plans": len(plans)},
        }
    selected_plan_id = str(selected_execution.get("selected_plan_id") or "").strip()
    selected_idea_id = str(selected_execution.get("selected_idea_id") or "").strip()
    selected_execution_issue = str(selected_execution.get("selection_issue") or selected_execution.get("failure_type") or "").strip()
    full_text_packet = _read_json(taste_dir / "full_text_reading" / "full_text_packet.json", {})
    original_recommendation_rows, reading_packet_rows, validation_find_results = _current_find_reading_validation_view_for_web(find_results if isinstance(find_results, dict) else {}, full_text_packet, 0)
    computed_validation = _current_find_reading_validation(validation_find_results, readings, len(reading_packet_rows))
    unavailable_reading_rows = [row for row in reading_packet_rows if row.get("reading_packet_role") == "unavailable_original_recommendation"]
    replacement_reading_rows = [row for row in reading_packet_rows if row.get("reading_packet_role") == "read_stage_full_text_replacement"]
    computed_validation.update({
        "original_recommendation_count": len(original_recommendation_rows),
        "reading_replacement_count": len(replacement_reading_rows),
        "reading_replacement_titles": [str(row.get("title") or row.get("paper_title") or "").strip() for row in replacement_reading_rows],
        "unavailable_original_recommendation_count": len(unavailable_reading_rows),
        "unavailable_original_recommendation_titles": [str(row.get("title") or row.get("paper_title") or "").strip() for row in unavailable_reading_rows],
        "enforced_current_recommendation_count": len(reading_packet_rows),
    })
    stored_validation = state_plan.get("reading_validation") if state_plan_matches and isinstance(state_plan, dict) else {}
    standalone_validation = _read_json(root / "state" / "current_find_claude_reading_validation.json", {})

    def wrapper_validation_matches_current_run(candidate: Any, *, inherited_state_match: bool = False) -> bool:
        if not isinstance(candidate, dict):
            return False
        candidate_run_id = payload_run_id(candidate)
        if candidate_run_id:
            return bool(run_id and candidate_run_id == run_id)
        return inherited_state_match

    def wrapper_validation_priority(candidate: Any) -> tuple[int, str]:
        if not wrapper_validation_matches_current_run(candidate, inherited_state_match=(candidate is stored_validation and state_plan_matches)):
            return (0, "")
        generated_at = str(candidate.get("generated_at") or candidate.get("updated_at") or "")
        actual_count = _as_int(candidate.get("actual_reading_count"), 0)
        if candidate.get("valid") is True and actual_count == len(readings):
            return (3, generated_at)
        status_text = str(candidate.get("status") or "")
        preflight = str(candidate.get("preflight") or "")
        preflight_statuses = {
            "blocked_current_find_full_text_evidence_pending",
            "current_find_full_text_evidence_ready_pending_claude_deep_read",
        }
        if preflight == "before_current_find_claude_takeover" or status_text in preflight_statuses:
            if not readings or actual_count == len(readings):
                return (2, generated_at)
        return (0, "")

    wrapper_candidates = [stored_validation, standalone_validation]
    best_wrapper_validation = max(wrapper_candidates, key=wrapper_validation_priority, default={})
    best_wrapper_priority = wrapper_validation_priority(best_wrapper_validation)
    # Current read_results content must be judged by the current validator. A
    # persisted wrapper validation can describe pre-read full-text evidence, but
    # once read_results exists it must not override newly added quality gates.
    trusted_state_validation = bool(best_wrapper_validation is stored_validation and state_plan_matches and best_wrapper_priority[0] >= 3)
    if trusted_state_validation:
        validation = {**computed_validation, **best_wrapper_validation}
    elif best_wrapper_priority[0] and not readings and best_wrapper_validation.get("valid") is not True:
        validation = {**computed_validation, **best_wrapper_validation}
    else:
        validation = dict(computed_validation)
    content_ready = bool(
        run_id
        and current_read_artifact
        and current_idea_artifact
        and current_plan_artifact
        and validation.get("valid")
        and len(readings) == int(validation.get("expected_recommendation_count") or len(readings) or 0)
        and ideas_ready
        and plans_ready
    )
    execution_ready = bool(content_ready and selected_plan_id and not selected_execution_issue)
    takeover_ready = execution_ready
    blockers: list[str] = []
    if not current_read_artifact:
        blockers.append("read_results missing or stale for current Find run")
    if not current_idea_artifact:
        blockers.append("ideas missing or stale for current Find run")
    if not current_plan_artifact:
        blockers.append("plans missing or stale for current Find run")
    blockers.extend(str(item) for item in validation.get("blockers", []) if str(item).strip())
    expected_readings = int(validation.get("expected_recommendation_count") or validation.get("expected_positive_count") or 0)
    if expected_readings and len(readings) != expected_readings:
        blockers.append(f"readings must equal current recommendations: {len(readings)}/{expected_readings}")
    elif len(readings) < min(10, max(expected_readings, 1)):
        blockers.append(f"readings below required current-Find coverage: {len(readings)}")
    if not ideas:
        blockers.append("ideas missing for current Find run")
    elif not approved_ideas:
        blockers.append("no approved ideas for current Find run; approve at least one idea before planning")
    if not plans:
        blockers.append("plans missing for current Find run")
    elif approved_idea_ids and not plans_for_approved_ideas:
        blockers.append("plans do not match approved current-Find ideas")
    selection_blocked = bool(content_ready and len(plans) >= 1 and (not selected_plan_id or selected_execution_issue))
    if selection_blocked:
        if selected_execution_issue == "ambiguous_selected_plan":
            blockers.append("ambiguous selected_plan_id: multiple current-Find plans were explicitly selected; main Claude Code must choose exactly one plan before environment, experiment, paper, or claim execution")
        else:
            blockers.append("missing selected_plan_id: main Claude Code must choose exactly one current-Find plan before environment, experiment, paper, or claim execution")
    pending_without_evidence = int(validation.get("pending_without_evidence_count") or 0)
    pending_deep_read = int(validation.get("pending_deep_read_synthesis_count") or 0)
    pending_full_text = int(validation.get("pending_full_text_reading_count") or 0)
    full_text_evidence_count = int(validation.get("full_text_evidence_count") or 0)
    state_status = str(state_plan.get("status") or "") if isinstance(state_plan, dict) else ""
    state_next_action = str(state_plan.get("next_required_action") or "") if isinstance(state_plan, dict) else ""
    raw_recommendation_rows = _json_rows(
        find_results.get("strong_recommendations", [])
        or find_results.get("articles", [])
        or find_results.get("recommendations", [])
    ) if isinstance(find_results, dict) else []
    raw_read_candidate_rows = _json_rows(find_results.get("read_candidates", [])) if isinstance(find_results, dict) else []
    recommendation_rows = _human_recommendation_literature_rows(raw_recommendation_rows)
    read_candidate_rows = _human_readable_literature_rows(raw_read_candidate_rows) or recommendation_rows
    row_payload_has_recommendation_pool = bool(raw_recommendation_rows or raw_read_candidate_rows)
    find_counts = find_results.get("counts") if isinstance(find_results, dict) and isinstance(find_results.get("counts"), dict) else {}
    if row_payload_has_recommendation_pool:
        recommendation_count = len(recommendation_rows)
        read_candidate_count = len(read_candidate_rows)
    else:
        recommendation_count = _as_int(find_counts.get("strong_recommendations") or find_counts.get("recommended"), 0)
        read_candidate_count = _as_int(find_counts.get("read_candidates"), 0)
    recommendation_shortfall = _as_int(find_results.get("recommendation_shortfall"), -1) if isinstance(find_results, dict) else -1
    if recommendation_shortfall < 0:
        recommendation_shortfall = _as_int(find_counts.get("recommendation_shortfall"), -1)
    if recommendation_shortfall < 0:
        target_count = _as_int(find_results.get("recommendation_target_count"), 0) if isinstance(find_results, dict) else 0
        if not target_count:
            target_count = _as_int(find_counts.get("recommendation_target_count"), 0)
        recommendation_shortfall = max(0, target_count - recommendation_count) if target_count else 0
    quality = find_results.get("recommendation_quality") if isinstance(find_results, dict) and isinstance(find_results.get("recommendation_quality"), dict) else {}
    recommendation_quality_ok = (
        not quality
        or (
            str(quality.get("status") or "") in {"", "ok", "not_needed"}
            and _as_int(quality.get("missing_real_abstract_count"), 0) == 0
            and _as_int(quality.get("missing_chinese_abstract_count"), 0) == 0
            and _as_int(quality.get("english_abstract_fallback_count"), 0) == 0
        )
    )
    recommendation_gate_ready = bool(
        run_id
        and recommendation_count > 0
        and recommendation_shortfall == 0
        and recommendation_quality_ok
        and (read_candidate_count in {0, recommendation_count})
    )

    def full_text_packet_coverage(rows: list[dict[str, Any]]) -> tuple[int, list[str], list[str]]:
        packet = _read_json(taste_dir / "full_text_reading" / "full_text_packet.json", {})
        if not isinstance(packet, dict) or not _payload_matches_current_run(packet, run_id):
            return 0, [], [str(row.get("title") or row.get("paper_title") or "Untitled") for row in rows]
        index: dict[str, dict[str, Any]] = {}
        for entry in _json_rows(packet.get("papers", [])):
            if not isinstance(entry, dict):
                continue
            for identity in _identity_values(entry):
                index[identity] = entry
        covered: list[str] = []
        missing: list[str] = []
        for row in rows:
            title = str(row.get("title") or row.get("paper_title") or "Untitled").strip()
            entry = next((index[key] for key in _identity_values(row) if key in index), {})
            if entry and _has_full_text_locator(entry) and _full_text_evidence_chars(entry) >= FULL_TEXT_MIN_CHARS:
                covered.append(title)
            else:
                missing.append(title)
        return len(covered), covered, missing

    packet_coverage_rows = reading_packet_rows if reading_packet_rows else recommendation_rows
    packet_evidence_count, packet_evidence_titles, packet_missing_titles = full_text_packet_coverage(packet_coverage_rows)
    full_text_packet_ready = bool(packet_coverage_rows and packet_evidence_count >= len(packet_coverage_rows) and not packet_missing_titles)
    if full_text_packet_ready and not readings:
        validation = dict(validation)
        validation.update({
            "status": "current_find_full_text_evidence_ready_pending_claude_deep_read",
            "preflight": "before_current_find_claude_takeover",
            "full_text_evidence_count": packet_evidence_count,
            "full_text_evidence_titles": packet_evidence_titles[:12],
            "pending_without_evidence_count": 0,
            "pending_without_evidence_titles": [],
            "pending_full_text_reading_count": 0,
            "pending_full_text_reading_titles": [],
            "blockers": [
                str(item)
                for item in validation.get("blockers", [])
                if "full-text evidence" not in str(item).lower() and "全文证据" not in str(item)
            ],
        })
        pending_without_evidence = 0
        pending_full_text = 0
        full_text_evidence_count = packet_evidence_count
    read_gate_ready = bool(
        run_id
        and recommendation_count > 0
        and recommendation_shortfall == 0
        and (read_candidate_count in {0, recommendation_count})
    )
    pending_current_find_read = bool(
        not readings
        and read_gate_ready
        and (
            full_text_packet_ready
            or (
                state_plan_matches
                and recommendation_gate_ready
                and (state_status == "pending_current_find_read" or state_next_action == "run_read_for_current_find")
            )
        )
    )
    if pending_current_find_read:
        blockers = ["current Find recommendation gate passed; Read stage has not run yet"]
    if pending_current_find_read:
        failure_type = ""
        next_required_action = "run_read_for_current_find"
        status = "pending_current_find_read"
    elif content_ready and selection_blocked:
        failure_type = selected_execution_issue or "missing_selected_plan"
        next_required_action = "rerun_current_find_claude_takeover_select_single_best_plan"
        status = "blocked_ambiguous_selected_plan" if failure_type == "ambiguous_selected_plan" else "blocked_missing_selected_plan"
    elif takeover_ready:
        failure_type = ""
        next_required_action = "environment_base_selection_and_repo_data_protocol_audit"
        status = "claude_takeover_ready"
    elif pending_without_evidence or (expected_readings and pending_full_text and full_text_evidence_count < expected_readings):
        failure_type = "full_text_evidence_missing"
        next_required_action = "acquire_current_find_full_text_evidence"
        status = "blocked_current_find_full_text_evidence_pending"
    elif pending_deep_read or validation.get("deep_read_content_gap_details"):
        failure_type = "claude_deep_read_rewrite_required"
        next_required_action = "rerun_current_find_claude_takeover_repair_deep_read_synthesis"
        status = "blocked_current_find_deep_read_validation_pending"
    elif read_source != required_source or not readings:
        failure_type = "claude_artifacts_missing"
        next_required_action = "rerun_current_find_claude_takeover_repair"
        status = "blocked_or_refreshing_claude_takeover"
    elif not ideas_ready or not plans_ready:
        failure_type = "idea_plan_artifacts_incomplete"
        next_required_action = "run_or_approve_current_find_idea_plan"
        status = "blocked_current_find_idea_plan_incomplete"
    else:
        failure_type = "contract_validation_failed"
        next_required_action = "rerun_current_find_claude_takeover_repair"
        status = "blocked_claude_current_find_takeover_incomplete"
    idea_titles = [str(row.get("title") or "").strip() for row in ideas[:5] if isinstance(row, dict) and str(row.get("title") or "").strip()]
    plan_titles = [str(row.get("title") or row.get("plan_id") or "").strip() for row in plans[:5] if isinstance(row, dict) and str(row.get("title") or row.get("plan_id") or "").strip()]
    # Selected execution belongs to the completed current-Find Read/Idea/Plan
    # contract. While Read is pending or validation is blocked, stale plan
    # selections from older state files must not be projected into the web UI.
    public_selected_execution = selected_execution if content_ready else {}
    public_selected_execution_issue = selected_execution_issue if content_ready else ""
    public_selected_plan_id = selected_plan_id if content_ready else ""
    public_selected_idea_id = selected_idea_id if content_ready else ""
    public_execution_policy = (
        public_selected_execution.get("execution_policy")
        if isinstance(public_selected_execution.get("execution_policy"), dict)
        else {}
    )
    public_candidate_counts = (
        public_selected_execution.get("candidate_counts")
        if isinstance(public_selected_execution.get("candidate_counts"), dict)
        else {"ideas": len(ideas), "plans": len(plans)}
    )
    return {
        "run_id": run_id,
        "required_source": required_source,
        "status": status,
        "failure_type": failure_type,
        "next_required_action": next_required_action,
        "content_ready": content_ready,
        "read_idea_plan_ready": content_ready,
        "execution_ready": execution_ready,
        "takeover_ready": takeover_ready,
        "read_source": read_source,
        "idea_source": idea_source,
        "plan_source": plan_source,
        "selected_execution": public_selected_execution,
        "selected_execution_status": str(public_selected_execution.get("status") or ""),
        "selected_execution_issue": public_selected_execution_issue,
        "selected_plan_id": public_selected_plan_id,
        "selected_idea_id": public_selected_idea_id,
        "selected_by": str(public_selected_execution.get("selected_by") or ""),
        "execution_policy": public_execution_policy,
        "candidate_counts": public_candidate_counts,
        "read_idea_plan_ready": content_ready,
        "readings": int(validation.get("full_text_reading_count") or 0),
        "read_artifact_count": len(readings),
        "raw_reading_count": len(readings),
        "full_text_reading_count": int(validation.get("full_text_reading_count") or 0),
        "full_text_evidence_count": full_text_evidence_count,
        "pending_deep_read_synthesis_count": pending_deep_read,
        "pending_full_text_reading_count": pending_full_text,
        "pending_without_evidence_count": pending_without_evidence,
        "deep_read_content_gap_details": validation.get("deep_read_content_gap_details", []),
        "ideas": len(ideas),
        "plans": len(plans),
        "state_readings": state_plan.get("current_find_reading_count") if isinstance(state_plan, dict) else None,
        "state_ideas": state_plan.get("current_find_idea_count") if isinstance(state_plan, dict) else None,
        "state_plans": state_plan.get("current_find_plan_count") if isinstance(state_plan, dict) else None,
        "experiment_plan_source": experiment_plan.get("source") if isinstance(experiment_plan, dict) else "",
        "blockers": blockers,
        "reading_validation": validation,
        "positive_anchor_readings": validation.get("positive_anchor_count", 0),
        "critique_or_boundary_readings": validation.get("critique_or_boundary_count", 0),
        "invalid_positive_readings": validation.get("invalid_positive_count", 0),
        "full_text_reading_titles": validation.get("full_text_reading_titles", []),
        "pending_full_text_reading_titles": validation.get("pending_full_text_reading_titles", []),
        "expected_positive_titles": validation.get("expected_positive_titles", []),
        "invalid_positive_titles": validation.get("invalid_positive_titles", []),
        "idea_titles": idea_titles,
        "plan_titles": plan_titles,
        "summary_zh": (
            f"当前 Find 推荐门控已通过：推荐 {recommendation_count} 篇，等待 Read 精读；待补全文 {pending_full_text} 篇。"
            if pending_current_find_read
            else f"Claude Code 接管当前 Find 的 Read/Idea/Plan 产物已通过，但缺少唯一 selected_plan_id；后续环境、实验、论文和结论提升保持阻断，等待主控 Claude Code 或人类监督选择一个计划。"
            if content_ready and selection_blocked
            else f"Claude Code 接管当前 Find 已通过：全文精读 {validation.get('full_text_reading_count', 0)} 篇、内部审计/边界候选 {validation.get('critique_or_boundary_count', 0)} 个、idea {len(ideas)} 个、plan {len(plans)} 个；run_id={run_id}。"
            if takeover_ready
            else (
                f"当前 Find 后处理已执行，但全文证据 gate 阻塞：全文精读 {validation.get('full_text_reading_count', 0)} 篇、待补全文证据 {pending_full_text} 篇、idea {len(ideas)} 个、plan {len(plans)} 个。"
                if failure_type == "full_text_evidence_missing"
                else f"当前 Find 后处理未通过 Claude 接管 gate：全文精读 {validation.get('full_text_reading_count', 0)} 篇、待补全文 {pending_full_text} 篇、非法正锚点 {validation.get('invalid_positive_count', 0)} 篇、idea {len(ideas)} 个、plan {len(plans)} 个。"
            )
        ),
    }


def _taste_literature_summary(root: Path) -> dict[str, Any]:
    find_results = _current_find_results_light(root, root.name)
    read_results = _read_json(root / "planning" / "finding" / "read_results.json", {})
    frontend = _read_json(root / "state" / "finding_frontend.json", {})
    intermediates = _read_json(root / "state" / "taste_literature_intermediates.json", {})
    progress = _read_json(root / "planning" / "finding" / "find_progress.json", {})
    if not isinstance(find_results, dict):
        find_results = {}
    if not isinstance(read_results, dict):
        read_results = {}
    if not isinstance(frontend, dict):
        frontend = {}
    if not isinstance(intermediates, dict):
        intermediates = {}
    if not isinstance(progress, dict):
        progress = {}
    source_selection = _current_project_source_selection(root.name, root)
    survey_stats = {}
    pipeline_stage_count_keys = {
        "category_filtered_papers",
        "tfidf_screened_papers",
        "venue_title_filter_input_papers",
        "title_score_input_papers",
        "llm_title_scored_papers",
        "abstract_scored_papers",
        "llm_scored_candidates",
        "recommended_papers",
    }
    survey_count_keys = pipeline_stage_count_keys | {
        "raw_title_index_papers",
        "title_total_papers",
        "venue_total_papers_available",
        "venue_corpus_audited_papers",
        "category_corpus_audited_papers",
        "venue_category_selected_papers",
        "venue_final_title_candidates",
        "venue_detail_fetched_candidates",
        "venue_evaluated_candidates",
        "abstract_fetch_failed_candidates",
        "final_llm_scoring_skipped_candidates",
        "category_scan_reports",
        "title_filter_reports",
    }
    current_run_id = str(find_results.get("run_id") or find_results.get("source_run_id") or _current_find_run_id_from_state(root) or "").strip()
    # Old intermediate/frontend snapshots may be unversioned. Once the current
    # Find run is known, do not let stale snapshots override live Find counters
    # such as final title+abstract LLM scoring coverage.
    for owner in [intermediates, frontend, progress, find_results]:
        if not isinstance(owner, dict):
            continue
        if owner is not find_results and not _payload_matches_current_run(owner, current_run_id):
            continue
        payload = owner.get("survey_stats")
        if isinstance(payload, dict):
            survey_stats.update({key: value for key, value in payload.items() if value not in (None, "")})
        count_payload = owner.get("counts") if isinstance(owner.get("counts"), dict) else {}
        for key in survey_count_keys:
            value = count_payload.get(key)
            if value not in (None, ""):
                survey_stats[key] = value
        if survey_stats.get("venue_title_filter_input_papers") in (None, "") and count_payload.get("title_score_input_papers") not in (None, ""):
            survey_stats["venue_title_filter_input_papers"] = count_payload.get("title_score_input_papers")
        if survey_stats.get("venue_detail_fetched_candidates") in (None, "") and count_payload.get("detail_fetched") not in (None, ""):
            survey_stats["venue_detail_fetched_candidates"] = count_payload.get("detail_fetched")
        if survey_stats.get("venue_evaluated_candidates") in (None, "") and count_payload.get("evaluated_candidates") not in (None, ""):
            survey_stats["venue_evaluated_candidates"] = count_payload.get("evaluated_candidates")
    category_rows = _json_rows(find_results.get("category_scan_report", []))
    title_rows = _json_rows(find_results.get("title_filter_report", []))
    raw_title_index_count = len(_json_rows(find_results.get("raw_title_index", [])))
    venue_health_rows = _json_rows(find_results.get("venue_health_report", []))
    source_raw_title_index_count = sum(int(row.get("corpus_count") or row.get("sample_count") or row.get("raw_title_index_count") or 0) for row in venue_health_rows if isinstance(row, dict))
    if raw_title_index_count or source_raw_title_index_count:
        # Title index coverage is the full source crawl across all enabled venues.
        # Category reports may exist only for OpenReview-style sources, so they are
        # a subset and must not be labelled as full-corpus coverage.
        survey_stats["raw_title_index_papers"] = raw_title_index_count or source_raw_title_index_count
        survey_stats["venue_total_papers_available"] = raw_title_index_count or source_raw_title_index_count
        survey_stats["venue_corpus_audited_papers"] = raw_title_index_count or source_raw_title_index_count
    if category_rows:
        survey_stats["venue_category_selected_papers"] = sum(int(row.get("selected_category_papers") or 0) for row in category_rows)
        survey_stats["category_corpus_audited_papers"] = sum(int(row.get("corpus_audit_papers") or row.get("total_papers") or 0) for row in category_rows)
        survey_stats["full_venue_corpus_audit"] = any(bool(row.get("full_venue_corpus_audit")) for row in category_rows)
        survey_stats["category_scan_reports"] = len(category_rows)
    if title_rows:
        survey_stats["venue_title_filter_input_papers"] = sum(int(row.get("title_filter_input_papers") or 0) for row in title_rows)
        survey_stats["venue_final_title_candidates"] = sum(int(row.get("final_title_candidates") or 0) for row in title_rows)
        survey_stats["title_filter_reports"] = len(title_rows)
    projection = _current_find_recommendation_projection(root, str(find_results.get("run_id") or ""))
    projection_counts = projection.get("counts") if isinstance(projection.get("counts"), dict) else {}
    projection_recommendations = _json_rows(projection.get("strong_recommendations") or projection.get("recommendations") or projection.get("articles") or []) if projection else []
    projection_read_candidates = _json_rows(projection.get("read_candidates") or projection.get("strong_recommendations") or projection.get("recommendations") or projection.get("articles") or []) if projection else []
    projection_triage_candidates = _json_rows(projection.get("triage_candidates") or projection.get("audit_candidates") or []) if projection else []
    articles = filter_papers_by_source_selection(_json_rows(find_results.get("articles", [])), source_selection)
    if projection_recommendations:
        articles = filter_papers_by_source_selection(projection_recommendations, source_selection)
    evaluated = filter_papers_by_source_selection(_json_rows(find_results.get("evaluated_candidates", [])), source_selection)
    title_candidates = filter_papers_by_source_selection(_json_rows(find_results.get("title_candidates", [])), source_selection)
    retrieval_candidates = filter_papers_by_source_selection(_json_rows(find_results.get("retrieval_candidates", [])), source_selection)
    arxiv_prefiltered = filter_papers_by_source_selection(_json_rows(find_results.get("arxiv_prefiltered", [])), source_selection)
    raw_read_candidates = _first_non_empty_rows(
        projection_read_candidates,
        find_results.get("read_candidates", []),
        articles,
    )
    raw_triage_candidates = _first_non_empty_rows(
        projection_triage_candidates,
        find_results.get("triage_candidates", []),
        find_results.get("audit_candidates", []),
        evaluated,
        arxiv_prefiltered,
        title_candidates,
    )
    raw_survey_candidates = _first_non_empty_rows(
        retrieval_candidates,
        title_candidates,
        evaluated,
        arxiv_prefiltered,
        find_results.get("raw_title_index", []),
    )
    readings = _json_rows(read_results.get("readings", []))
    screened = filter_papers_by_source_selection(_json_rows(find_results.get("screened_ranking", [])), source_selection)
    articles = filter_papers_by_source_selection(_json_rows(find_results.get("strong_recommendations", [])), source_selection) or articles
    if projection_recommendations:
        articles = filter_papers_by_source_selection(projection_recommendations, source_selection) or articles
    strong_pool = _human_recommendation_literature_rows(articles)
    audit_candidates = _audit_literature_rows(raw_triage_candidates) + _audit_literature_rows(evaluated)
    read_candidates = strong_pool or _human_recommendation_literature_rows(raw_read_candidates)
    survey_candidates = strong_pool or read_candidates
    audit_only_pool = _audit_literature_rows(raw_triage_candidates) + _audit_literature_rows(evaluated)
    strong_count = len(strong_pool)
    strict_strong_anchor_count = strong_count
    recommendation_display_limit = max(strong_count, len(read_candidates), 1)
    candidate_count = len(raw_survey_candidates)
    read_candidate_count = len(read_candidates)
    raw_read_candidate_count = len(read_candidates)
    raw_triage_candidate_count = len(raw_triage_candidates)
    demoted_or_boundary_count = sum(
        1 for row in evaluated
        if isinstance(row, dict) and (row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"))
    )
    verified_venue_rows = _current_verified_venue_metadata_rows(root.name, root, source_selection)
    venue_health_rows = _merge_verified_venue_metadata_rows(venue_health_rows, verified_venue_rows)
    source_rows = filter_source_status_by_selection(_merge_verified_venue_metadata_rows(_expand_source_status_rows(find_results.get("source_status", []), venue_health_rows), verified_venue_rows), source_selection)
    venue_metadata_counts = _venue_metadata_counts(venue_health_rows or source_rows or verified_venue_rows)
    for key, value in venue_metadata_counts.items():
        if key in pipeline_stage_count_keys and survey_stats.get(key) not in (None, "", 0):
            continue
        if value not in (None, ""):
            survey_stats[key] = value
    limited_sources = [
        {
            "source": row.get("source") or row.get("venue") or "",
            "status": "limited" if row.get("limited") else "failed" if not row.get("ok", True) else "ok",
            "message": row.get("message") or row.get("error") or "",
            "count": row.get("count") or row.get("sample_count") or 0,
        }
        for row in source_rows
        if isinstance(row, dict) and (row.get("limited") or not row.get("ok", True))
    ]
    missing_venues = [
        {
            "venue": row.get("venue") or row.get("venue_id") or "",
            "years": row.get("years") or [],
            "reason": row.get("error") or row.get("suggested_fix") or "No usable title index.",
        }
        for row in venue_health_rows
        if isinstance(row, dict) and not row.get("ok", False)
    ]
    has_candidates = bool(candidate_count or read_candidate_count or len(evaluated) or len(arxiv_prefiltered))
    arxiv_enabled = bool(source_selection.get("include_arxiv"))
    biorxiv_enabled = bool(source_selection.get("include_biorxiv"))
    nature_enabled = bool(source_selection.get("include_nature"))
    science_enabled = bool(source_selection.get("include_science"))
    biorxiv_prefiltered = _json_rows(find_results.get("biorxiv_prefiltered", []))
    nature_prefiltered = _json_rows(find_results.get("nature_prefiltered", []))
    science_prefiltered = _json_rows(find_results.get("science_prefiltered", []))
    if strong_count:
        status = "strong_recommendations_ready"
        status_zh = "已有推荐文章"
        note_zh = "已有论文进入推荐列表；推荐池只来自最终题名+摘要 LLM 评分和真实摘要合同，推荐精读与推荐池一致。"
        note_en = "At least one paper entered the recommendation list; the pool comes only from final title+abstract LLM scoring with real abstracts, and recommended readings mirror it."
    elif has_candidates:
        status = "candidates_but_no_strong_recommendation"
        status_zh = "有调研候选，但推荐列表为空"
        note_zh = "finding 已完成调研并找到候选论文；推荐列表为空表示证据门控未通过，不是爬取失败。"
        note_en = "finding completed the survey and found candidates; no paper entered the recommendation list, so this is not a crawl failure."
    else:
        status = "no_candidates"
        status_zh = "暂无候选论文"
        note_zh = "未看到可用候选池；需要检查抓取源、会议配置或网络/API。"
        note_en = "No usable candidate pool is visible; check sources, venue config, or network/API access."
    return {
        "status": status,
        "status_zh": status_zh,
        "status_en": status.replace("_", " "),
        "note": note_zh,
        "note_i18n": {"zh": note_zh, "en": note_en},
        "note_zh": note_zh,
        "note_en": note_en,
        "survey_stats": survey_stats,
        "counts": {
            "raw_title_index_papers": survey_stats.get("raw_title_index_papers"),
            "title_total_papers": survey_stats.get("title_total_papers") or survey_stats.get("raw_title_index_papers"),
            "venue_total_papers_available": survey_stats.get("venue_total_papers_available"),
            "venue_corpus_audited_papers": survey_stats.get("venue_corpus_audited_papers") or survey_stats.get("venue_total_papers_available"),
            "category_corpus_audited_papers": survey_stats.get("category_corpus_audited_papers"),
            "category_filtered_papers": survey_stats.get("category_filtered_papers"),
            "venue_category_selected_papers": survey_stats.get("venue_category_selected_papers"),
            "tfidf_screened_papers": survey_stats.get("tfidf_screened_papers"),
            "venue_title_filter_input_papers": survey_stats.get("venue_title_filter_input_papers"),
            "title_score_input_papers": survey_stats.get("title_score_input_papers"),
            "llm_title_scored_papers": survey_stats.get("llm_title_scored_papers"),
            "venue_final_title_candidates": survey_stats.get("venue_final_title_candidates") or len(title_candidates),
            "venue_detail_fetched_candidates": survey_stats.get("venue_detail_fetched_candidates") or len(evaluated),
            "venue_evaluated_candidates": survey_stats.get("venue_evaluated_candidates"),
            "abstract_scored_papers": survey_stats.get("abstract_scored_papers") or survey_stats.get("llm_scored_candidates"),
            "llm_scored_candidates": survey_stats.get("llm_scored_candidates") or sum(1 for row in evaluated if isinstance(row, dict) and str(row.get("reason_source") or "") == "llm abstract evaluation"),
            "recommended_papers": survey_stats.get("recommended_papers"),
            "abstract_fetch_failed_candidates": survey_stats.get("abstract_fetch_failed_candidates"),
            "final_llm_scoring_skipped_candidates": survey_stats.get("final_llm_scoring_skipped_candidates"),
            "full_venue_corpus_audit": bool(survey_stats.get("full_venue_corpus_audit")),
            "category_scan_reports": survey_stats.get("category_scan_reports"),
            "title_filter_reports": survey_stats.get("title_filter_reports"),
            "arxiv_raw_count": (survey_stats.get("arxiv_raw_count") or len(_json_rows(find_results.get("arxiv_raw", [])))) if arxiv_enabled else 0,
            "arxiv_prefiltered_count": (survey_stats.get("arxiv_prefiltered_count") or len(arxiv_prefiltered)) if arxiv_enabled else 0,
            "arxiv_pages_fetched": survey_stats.get("arxiv_pages_fetched") if arxiv_enabled else 0,
            "arxiv_full_scan": survey_stats.get("arxiv_full_scan") if arxiv_enabled else False,
            "biorxiv_raw_count": len(_json_rows(find_results.get("biorxiv_raw", []))) if biorxiv_enabled else 0,
            "biorxiv_prefiltered_count": len(biorxiv_prefiltered) if biorxiv_enabled else 0,
            "nature_raw_count": len(_json_rows(find_results.get("nature_raw", []))) if nature_enabled else 0,
            "nature_prefiltered_count": len(nature_prefiltered) if nature_enabled else 0,
            "science_raw_count": len(_json_rows(find_results.get("science_raw", []))) if science_enabled else 0,
            "science_prefiltered_count": len(science_prefiltered) if science_enabled else 0,
            "strong_recommendations": strong_count,
            "strict_strong_anchor_count": strict_strong_anchor_count,
            "screened_ranking": len(audit_only_pool),
            "evaluated_candidates": len(evaluated),
            "survey_candidates": candidate_count,
            "read_candidates": read_candidate_count,
            "read_candidates_raw": raw_read_candidate_count,
            "read_candidates_readable": read_candidate_count,
            "triage_candidates": raw_triage_candidate_count,
            "demoted_or_boundary_candidates": demoted_or_boundary_count,
            "audit_candidates": len(audit_candidates),
            "readings": len(readings),
            "ideas": len(_json_rows(_read_json(root / "planning" / "finding" / "ideas.json", {}).get("ideas", []))),
            "plans": len(_json_rows(_read_json(root / "planning" / "finding" / "plans.json", {}).get("plans", []))),
            "article_output": len(articles),
            "base_work_candidates": len(_json_rows(_read_json(root / "state" / "literature_tool_packet.json", {}).get("base_work_candidates", []))),
            "critique_candidates": len(_json_rows(_read_json(root / "state" / "literature_tool_packet.json", {}).get("critique_candidates", []))),
        },
        "coverage_explanation_i18n": {
            "zh": (
                f"本轮先全量审计会议语料，再用类别信号和标题/摘要预筛降低 LLM 负担，只有筛后候选进入详情抓取与并行 LLM 评分。"
                f"推荐文章 {strong_count} 篇来自同一个用户可读推荐池；推荐精读 {read_candidate_count} 篇与推荐池一致。未取得真实摘要或未完成最终题名+摘要 LLM 评分的候选不会进入推荐列表，只保留在机器审计产物中。当前可追踪候选 {candidate_count or len(title_candidates) or len(evaluated)} 个，已抓详情/评分候选 {len(evaluated)} 个。"
            ),
            "en": (
                f"This run scans venue titles, then applies category/title filtering and final title+abstract LLM scoring. "
                f"The {strong_count} recommended papers are the single user-facing recommendation pool; {read_candidate_count} recommended readings mirror that pool. Rows without real abstracts or final title+abstract LLM scoring remain only in machine audit artifacts. {candidate_count or len(title_candidates) or len(evaluated)} traceable candidates and {len(evaluated)} detail-scored candidates remain available."
            ),
        },
        "source_status": source_rows[:20],
        "source_limitations": limited_sources[:8],
        "missing_venue_indexes": missing_venues[:8],
        "strong_recommendations": strong_pool[:recommendation_display_limit],
        "screened_ranking_audit_only": audit_only_pool[:20],
        "survey_candidates": survey_candidates[:20],
        "audit_candidates": audit_candidates[:20],
        "read_candidates": read_candidates[:recommendation_display_limit],
        "readings": readings[:recommendation_display_limit],
        "files": {
            "find_results": str(root / "planning" / "finding" / "find_results.json"),
            "read_results": str(root / "planning" / "finding" / "read_results.json"),
            "frontend_state": str(root / "state" / "finding_frontend.json"),
            "intermediates": str(root / "state" / "taste_literature_intermediates.json"),
        },
    }


def _agent_state(project: str) -> dict[str, Any]:
    refresh_process_flags(project)
    agents = list_agents(project)
    full_cycle = _full_cycle_summary(PROJECTS / project)
    main = next((row for row in agents if row.get("id") == "main"), None)
    if main is None:
        main = {
            "id": "main",
            "name": "主控 Agent",
            "role": "main",
            "stage": "idle",
            "status": "idle",
            "goal": "等待网页指令或自主科研任务",
            "current_step": "idle",
            "process_alive": False,
            "queued_guidance": [],
            "children": [],
            "log_tail": [],
        }
        agents = [main, *agents]
    if isinstance(full_cycle, dict) and full_cycle.get("status") == "blocked_literature_base_audit_required":
        main = dict(main)
        main["status"] = "blocked"
        main["stage"] = "literature-base-audit"
        main["current_step"] = str(full_cycle.get("current_goal") or "fresh Find base candidates require repo/data/env audit before any legacy-route continuation")[:240]
        main["process_alive"] = False
        agents = [main if row.get("id") == "main" else row for row in agents]
    elif isinstance(full_cycle, dict) and full_cycle.get("continuation_required") and main.get("status") in {"done", "completed"}:
        main = dict(main)
        main["status"] = "blocked"
        main["current_step"] = str(full_cycle.get("current_goal") or full_cycle.get("summary") or "blocked; continuation is required")[:240]
        agents = [main if row.get("id") == "main" else row for row in agents]
    return {
        "agents": agents,
        "main": main,
        "running": [row for row in agents if row.get("status") in {"queued", "running", "cancelling"}],
    }




def _fast_project_summary(project: str, root: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    def safe_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}
    def safe_list(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []
    def scalar(src: Any, keys: list[str]) -> dict[str, Any]:
        row = src if isinstance(src, dict) else {}
        return {key: row.get(key, "") for key in keys if isinstance(row.get(key, ""), (str, int, float, bool)) or row.get(key) is None}
    def artifact(name: str, path: Path, kind: str = "json") -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            st = path.stat()
            return {"name": name, "kind": kind, "path": str(path), "size_bytes": st.st_size, "updated_at": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()}
        except Exception:
            return None
    def alive(pid: Any) -> bool:
        return _pid_alive(pid)

    def runtime_payload() -> dict[str, Any]:
        try:
            payload = _cached_runtime_diagnostics(project, cfg)
        except Exception as exc:
            runtime_cfg = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
            payload = {"project": project, "runtime": runtime_cfg, "checks": {}, "path_head": [], "status": "needs_attention", "error": str(exc)}
        checks = safe_dict(payload.get("checks"))
        required = ["node", "npm", "claude", "python", "conda", "conda_base"]
        if not payload.get("status"):
            payload["status"] = "ready" if checks and all(bool(safe_dict(checks.get(name)).get("ok")) for name in required if name in checks) else "needs_attention"
        return payload

    def claude_status_payload() -> dict[str, Any]:
        session = safe_dict(_read_json(root / "state" / "claude_project_session.json", {}))
        last_result = safe_dict(_read_json(root / "state" / "claude_project_session_last_result.json", {}))
        latest_receipt = _public_claude_receipt(_latest_claude_receipt(root, session, last_result))
        latest_receipt_by_stage = _public_claude_receipts_by_stage(root)
        return {
            "enabled": bool(session or last_result or latest_receipt_by_stage),
            "status": session.get("status") or last_result.get("status", ""),
            "session": scalar(session, ["status", "last_stage", "last_return_code", "last_success_at", "updated_at", "repo_path", "session_id"]),
            "last_result": scalar(last_result, ["status", "stage", "return_code", "started_at", "finished_at", "session_id"]),
            "latest_receipt": latest_receipt,
            "latest_receipt_by_stage": latest_receipt_by_stage,
        }

    current_plan = safe_dict(_read_json(root / "state" / "current_find_research_plan.json", {}))
    lit_packet = safe_dict(_read_json(root / "state" / "literature_tool_packet.json", {}))
    find_results = safe_dict(_current_find_results_light(root, project))
    recommendation_projection = safe_dict(_current_find_recommendation_projection(root, str(find_results.get("run_id") or "")))
    projection_counts = safe_dict(recommendation_projection.get("counts"))
    projection_survey_stats = safe_dict(recommendation_projection.get("survey_stats"))
    pipeline_stage_count_keys = {
        "category_filtered_papers", "tfidf_screened_papers", "venue_title_filter_input_papers", "title_score_input_papers",
        "llm_title_scored_papers", "abstract_scored_papers", "llm_scored_candidates", "recommended_papers",
    }
    raw_projection_recommendation_rows = safe_list(recommendation_projection.get("strong_recommendations")) or safe_list(recommendation_projection.get("recommendations")) or safe_list(recommendation_projection.get("articles"))
    projection_recommendation_rows = _human_recommendation_literature_rows(raw_projection_recommendation_rows)
    raw_projection_read_rows = safe_list(recommendation_projection.get("read_candidates"))
    projection_read_rows = _human_readable_literature_rows(raw_projection_read_rows) or projection_recommendation_rows
    tick = safe_dict(_read_json(root / "state" / "supervision_tick.json", {}))
    full_cycle = safe_dict(_read_json(root / "state" / "full_research_cycle.json", {}))
    ref_gate = safe_dict(_read_json(root / "state" / "reference_reproduction_gate.json", {}))
    selected_base_viability = safe_dict(_read_json(root / "state" / "selected_base_viability_gate.json", {}))
    base_switch_gate = safe_dict(_read_json(root / "state" / "base_switch_gate.json", {}))
    base_switch_execution = safe_dict(_read_json(root / "state" / "base_switch_execution.json", {}))
    reference_full_job = safe_dict(_fresh_base_reference_full_job(root))
    protocol_probe = safe_dict(_fresh_base_protocol_probe(root))
    scientific_progress_gate = safe_dict(_read_json(root / "state" / "scientific_progress_gate.json", {}))
    experiment_iteration_audit = safe_dict(_read_json(root / "state" / "experiment_iteration_audit.json", {}))
    fresh_impl = safe_dict(_read_json(root / "state" / "fresh_base_implementation_plan.json", {}))
    paper_state = safe_dict(_active_paper_state(root, project, cfg, venue=str(cfg.get("target_venue") or cfg.get("venue") or tick.get("target_venue") or "")))
    submission_state = safe_dict(_read_json(root / "state" / "submission_readiness.json", {}))
    experiment_record = safe_dict(_experiment_record_table(root, sync_running=False))
    env = _current_environment_selection(root)
    existing_env_selection = safe_dict(_read_json(root / "state" / "evidence_ready_repo_selection.json", {}))
    existing_selected = safe_dict(existing_env_selection.get("selected"))
    existing_active_repo = safe_dict(_read_json(root / "state" / "active_repo.json", {}))
    selected = safe_dict(env.get("selected")) if env.get("valid") else {}
    repo = safe_dict(fresh_impl.get("repo")) if env.get("valid") else {}
    active_repo = existing_active_repo if env.get("valid") else {}
    # Current main-route identity is owned by the current-run environment selection.
    # fresh_base_implementation_plan may lag behind after a base switch, so it must
    # not override selected/active_repo identity fields in the compact API.
    if env.get("valid"):
        repo_run = str(fresh_impl.get("fresh_find_run_id") or "").strip()
        repo_path_text = str(repo.get("repo_path") or repo.get("local_path") or "").strip()
        selected_path_text = str(selected.get("repo_path") or selected.get("local_path") or active_repo.get("repo_path") or active_repo.get("local_path") or "").strip()
        selected_name_text = str(selected.get("name") or selected.get("repo") or active_repo.get("name") or active_repo.get("repo") or "").strip().lower()
        repo_name_text = str(repo.get("name") or repo.get("repo") or "").strip().lower()
        if (repo_run and repo_run != str(env.get("current_find_run_id") or "").strip()) or (repo_path_text and selected_path_text and repo_path_text != selected_path_text) or (repo_name_text and selected_name_text and repo_name_text != selected_name_text):
            repo = {}

    full_job_source = full_cycle.get("full_cycle_job") if isinstance(full_cycle.get("full_cycle_job"), dict) else {}
    if not full_job_source and isinstance(tick.get("full_cycle_job"), dict):
        full_job_source = tick.get("full_cycle_job")
    full_job = _normalize_full_cycle_job(root, project, full_job_source)
    if not isinstance(full_job, dict):
        full_job = {}
    full_cycle = _sanitize_stale_full_cycle_summary(full_cycle, full_job, root=root)
    pid = str(full_job.get("pid") or "")
    full_job_live = bool(
        pid
        and _is_real_full_cycle_command(full_job.get("cmd") or full_job.get("command"), kind=full_job.get("kind"), stage=full_job.get("stage") or full_job.get("raw_stage"))
        and (full_job.get("process_alive") is True or alive(pid))
        and alive(pid)
    )
    raw_status = str(full_cycle.get("status") or tick.get("status") or "not_started")
    status = "running" if full_job_live else raw_status
    if status == "completed" and full_job_live:
        status = "running"
    if not full_job_live and status == "running":
        status = "stale_full_research_cycle_snapshot"
    venue = _display_venue(cfg.get("target_venue") or cfg.get("venue") or tick.get("target_venue") or "")
    public_full_job = _public_full_cycle_job(full_job, target_venue=venue)
    topic = str(cfg.get("topic") or cfg.get("title") or project)
    current_find_run_id = _current_find_run_id_from_state(root)
    run_id = str(current_find_run_id or current_plan.get("run_id") or lit_packet.get("run_id") or lit_packet.get("source_run_id") or tick.get("find_run_id") or "")
    def same_current_run_payload(payload: Any) -> bool:
        if not isinstance(payload, dict) or not run_id:
            return False
        payload_run_id = str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or "").strip()
        return bool(payload_run_id and payload_run_id == run_id)

    current_downstream_artifact_ready = False

    def current_run_artifact(name: str, path: Path, kind: str = "json") -> dict[str, Any] | None:
        if path.name == "find_results.json" and run_id:
            return artifact(name, path, kind)
        if path.name in {"find_progress.json", "current_find_research_plan.json", "literature_tool_packet.json", "article.md"}:
            return artifact(name, path, kind)
        if path.name in {"read_results.json", "ideas.json", "plans.json"} and not current_downstream_artifact_ready:
            return None
        payload = _read_json(path, {})
        if not same_current_run_payload(payload):
            return None
        return artifact(name, path, kind)

    raw_read_results = safe_dict(_read_json(root / "planning" / "finding" / "read_results.json", {}))
    raw_ideas_results = safe_dict(_read_json(root / "planning" / "finding" / "ideas.json", {}))
    raw_plans_results = safe_dict(_read_json(root / "planning" / "finding" / "plans.json", {}))
    pipeline_contract = safe_dict(_current_find_pipeline_summary(root, find_results=find_results))
    current_reading_validation = safe_dict(_read_json(root / "state" / "current_find_claude_reading_validation.json", {}))
    validation_expected = int(current_reading_validation.get("expected_recommendation_count") or 0)
    validation_actual = int(current_reading_validation.get("actual_reading_count") or 0)
    validation_ready = bool(
        same_current_run_payload(current_reading_validation)
        and current_reading_validation.get("valid") is True
        and validation_expected > 0
        and validation_actual >= validation_expected
    )
    plan_ready = bool(same_current_run_payload(current_plan) and current_plan.get("read_idea_plan_ready") is True)
    downstream_readings = safe_list(raw_read_results.get("readings"))
    downstream_ready_source = str(raw_read_results.get("normalization_source") or "").strip()
    current_downstream_artifact_ready = bool(pipeline_contract.get("content_ready") or pipeline_contract.get("read_idea_plan_ready") or pipeline_contract.get("takeover_ready"))
    read_results = raw_read_results if current_downstream_artifact_ready else {}
    ideas_results = raw_ideas_results if current_downstream_artifact_ready else {}
    plans_results = raw_plans_results if current_downstream_artifact_ready else {}
    read_count = int(pipeline_contract.get("readings") or pipeline_contract.get("reading_count") or len(safe_list(read_results.get("readings"))) or (current_plan.get("current_find_reading_count") if same_current_run_payload(current_plan) else 0) or 0)
    idea_count = int(pipeline_contract.get("ideas") or pipeline_contract.get("idea_count") or len(safe_list(ideas_results.get("ideas"))) or (current_plan.get("current_find_idea_count") if same_current_run_payload(current_plan) else 0) or 0)
    plan_count = int(pipeline_contract.get("plans") or pipeline_contract.get("plan_count") or len(safe_list(plans_results.get("plans"))) or (current_plan.get("current_find_plan_count") if same_current_run_payload(current_plan) else 0) or 0)
    full_text_read_count = int(pipeline_contract.get("full_text_reading_count") or read_count or 0)
    pending_full_text_read_count = int(pipeline_contract.get("pending_full_text_reading_count") or 0)
    structured_read_count = len(safe_list(read_results.get("readings")))
    raw_read_count = int(pipeline_contract.get("read_artifact_count") or pipeline_contract.get("raw_reading_count") or structured_read_count or read_count or 0)
    raw_read_count = max(raw_read_count, structured_read_count)
    read_count = max(read_count, structured_read_count)
    lit_summary = safe_dict(lit_packet.get("summary"))
    tick_lit = safe_dict(tick.get("literature"))
    if tick_lit and not same_current_run_payload(tick_lit):
        tick_lit = {}
    if lit_packet and not same_current_run_payload(lit_packet):
        lit_packet = {}
        lit_summary = {}
    progress = safe_dict(_read_json(root / "planning" / "finding" / "find_progress.json", {}))
    plan_gate = safe_dict(current_plan.get("literature_gate")) if same_current_run_payload(current_plan) else {}
    progress_status = str(progress.get("status") or progress.get("phase") or "").lower()
    progress_blocked_llm = "blocked_llm" in progress_status or "quota" in progress_status
    progress_run = str(progress.get("run_id") or "")
    targeted_tool_status = safe_dict(current_plan.get("targeted_search_tool_status"))
    status_run = str(targeted_tool_status.get("current_find_run_id") or targeted_tool_status.get("run_id") or targeted_tool_status.get("find_run_id") or "").strip()
    if progress_run and status_run and status_run != progress_run:
        targeted_tool_status = {}
    latest_targeted_tool = safe_dict(_read_json(root / "state" / "taste_targeted_queries.json", {}))
    latest_status = str(latest_targeted_tool.get("status") or "")
    latest_has_failure = bool(latest_targeted_tool.get("failure_summary") or latest_targeted_tool.get("return_codes"))
    latest_run = str(latest_targeted_tool.get("current_find_run_id") or latest_targeted_tool.get("run_id") or latest_targeted_tool.get("find_run_id") or "")
    latest_matches_current_find = not (progress_run and latest_run and latest_run != progress_run)
    if latest_targeted_tool and latest_matches_current_find and (latest_has_failure or latest_status.startswith("failed")):
        targeted_tool_status = {
            **targeted_tool_status,
            "status": latest_targeted_tool.get("status", targeted_tool_status.get("status")),
            "venue": latest_targeted_tool.get("venue", targeted_tool_status.get("venue")),
            "packet_return_code": latest_targeted_tool.get("packet_return_code", targeted_tool_status.get("packet_return_code")),
            "return_codes": latest_targeted_tool.get("return_codes", targeted_tool_status.get("return_codes")),
            "failure_summary": latest_targeted_tool.get("failure_summary", targeted_tool_status.get("failure_summary")),
            "guardrail": latest_targeted_tool.get("guardrail", targeted_tool_status.get("guardrail")),
            "record_only_requested": latest_targeted_tool.get("record_only_requested", targeted_tool_status.get("record_only_requested")),
            "new_find_allowed": latest_targeted_tool.get("new_find_allowed", targeted_tool_status.get("new_find_allowed")),
        }
    targeted_llm_blocked = _looks_like_llm_quota_blocker(targeted_tool_status)
    current_find_recommendations_ready = bool(
        progress_run
        and str(progress.get("phase") or progress.get("status") or "").lower() == "complete"
        and _as_int(progress.get("strong_recommendation_count"), 0) >= max(1, _as_int(progress.get("recommendation_target_count"), 0))
        and _as_int(progress.get("recommendation_shortfall"), 0) == 0
    )
    if current_find_recommendations_ready and not progress_blocked_llm:
        targeted_llm_blocked = False
    llm_quota_blocked = progress_blocked_llm or targeted_llm_blocked
    llm_blocker_reason = str(
        progress.get("blocked_reason")
        or targeted_tool_status.get("failure_summary")
        or targeted_tool_status.get("error")
        or "LLM API 额度/配置不可用，Find 必需的摘要评分或补评分无法继续。"
    )
    if progress_blocked_llm:
        plan_gate = {}
        read_count = 0
        idea_count = 0
        plan_count = 0
    if raw_projection_recommendation_rows:
        strong_count = len(projection_recommendation_rows)
    else:
        strong_count = _as_int(recommendation_projection.get("strict_strong_anchor_count"), -1)
    if strong_count < 0:
        strong_count = _as_int(projection_counts.get("recommended"), -1)
    if strong_count < 0:
        strong_count = _as_int(projection_counts.get("strict_strong_anchor_count"), -1)
    if strong_count < 0:
        strong_count = _as_int(plan_gate.get("strong_recommendations"), -1)
    if strong_count < 0:
        strong_count = _as_int(progress.get("strong_recommendation_count"), -1)
    if strong_count < 0:
        strong_count = _as_int(lit_summary.get("strong_paper_anchors"), -1)
    if strong_count < 0:
        strong_count = _as_int(tick_lit.get("strong_recommendations"), 0)
    recommendation_target_count = _as_int(recommendation_projection.get("recommendation_target_count"), -1)
    if recommendation_target_count < 0:
        recommendation_target_count = _as_int(projection_counts.get("recommendation_target_count"), -1)
    if recommendation_target_count < 0:
        recommendation_target_count = _as_int(plan_gate.get("recommendation_target_count"), -1)
    if recommendation_target_count < 0:
        recommendation_target_count = _as_int(progress.get("recommendation_target_count"), -1)
    if recommendation_target_count < 0:
        recommendation_target_count = _as_int(lit_summary.get("recommendation_target_count"), 0)
    recommendation_shortfall = _as_int(recommendation_projection.get("recommendation_shortfall"), -1)
    if recommendation_shortfall < 0:
        recommendation_shortfall = _as_int(projection_counts.get("recommendation_shortfall"), -1)
    if recommendation_shortfall < 0:
        recommendation_shortfall = _as_int(plan_gate.get("recommendation_shortfall"), -1)
    if recommendation_shortfall < 0:
        recommendation_shortfall = _as_int(progress.get("recommendation_shortfall"), -1)
    if recommendation_shortfall < 0:
        recommendation_shortfall = _as_int(lit_summary.get("recommendation_shortfall"), -1)
    if recommendation_shortfall < 0:
        recommendation_shortfall = max(0, recommendation_target_count - strong_count) if recommendation_target_count else 0
    literature_gate_blocked = recommendation_shortfall > 0
    if literature_gate_blocked:
        blocked_selection = safe_dict(env.get("selected"))
        env = {
            **env,
            "valid": False,
            "blocked_by": "literature_llm_quota_exhausted" if llm_quota_blocked else "literature_recommendation_shortfall",
            "base_selection_status": "blocked_by_literature_gate",
            "blocked_selection": blocked_selection,
            "reason": "current Find repair/restart is blocked by unavailable LLM abstract scoring; environment base selection is audit-only until LLM scoring succeeds" if llm_quota_blocked else "current Find strong-recommendation gate is short; environment base selection is audit-only until the gate passes",
        }
        selected = {}
        repo = {}
        active_repo = {}
    progress_counts = safe_dict(progress.get("counts"))
    traceable_count = int(lit_summary.get("inspected_candidates") or progress_counts.get("title_candidates") or 0)
    evaluated_count = int(progress_counts.get("evaluated_candidates") or tick_lit.get("evaluated_candidates") or 0)
    detail_fetched_count = int(progress_counts.get("detail_fetched") or evaluated_count or 0)
    title_candidate_count = int(progress_counts.get("title_candidates") or traceable_count or 0)
    inspected_count = traceable_count
    articles_count = int(tick_lit.get("articles") or strong_count or 0)
    venue_health = tick_lit.get("venue_health_report") if isinstance(tick_lit.get("venue_health_report"), list) else progress.get("venue_health_report") if isinstance(progress.get("venue_health_report"), list) else safe_list(find_results.get("venue_health_report"))
    source_status = tick_lit.get("source_status") if isinstance(tick_lit.get("source_status"), list) else progress.get("source_status") if isinstance(progress.get("source_status"), list) else safe_list(find_results.get("source_status"))
    verified_venue_rows = _current_verified_venue_metadata_rows(project, root, _current_project_source_selection(project, root))
    venue_health = _merge_verified_venue_metadata_rows(venue_health, verified_venue_rows)
    source_status = _merge_verified_venue_metadata_rows(source_status, verified_venue_rows)
    raw_title_index_count = int(progress_counts.get("raw_title_index") or progress_counts.get("raw_title_index_papers") or progress_counts.get("venue_total_papers_available") or 0)
    if not raw_title_index_count:
        raw_title_index_count = len(safe_list(find_results.get("raw_title_index")))
    venue_metadata_counts = _venue_metadata_counts(source_status or venue_health or verified_venue_rows)
    if venue_metadata_counts.get("raw_title_index_papers"):
        raw_title_index_count = int(venue_metadata_counts.get("raw_title_index_papers") or 0)
    if not raw_title_index_count:
        raw_title_index_count = sum(int((row if isinstance(row, dict) else {}).get("raw_title_index_count") or (row if isinstance(row, dict) else {}).get("corpus_count") or (row if isinstance(row, dict) else {}).get("sample_count") or 0) for row in source_status or venue_health)
    title_filter_input_count = sum(int((row if isinstance(row, dict) else {}).get("title_filter_input_papers") or 0) for row in safe_list(find_results.get("title_filter_report")))
    if venue_metadata_counts.get("venue_title_filter_input_papers"):
        title_filter_input_count = int(venue_metadata_counts.get("venue_title_filter_input_papers") or 0)
    if not title_filter_input_count:
        title_filter_input_count = sum(int((row if isinstance(row, dict) else {}).get("candidate_count") or (row if isinstance(row, dict) else {}).get("count") or 0) for row in source_status)
    find_counts = safe_dict(find_results.get("counts"))
    progress_survey_stats = {key: value for key, value in progress_counts.items() if key in pipeline_stage_count_keys and value not in (None, "")}
    find_survey_stats = {**progress_survey_stats, **safe_dict(find_results.get("survey_stats")), **projection_survey_stats}
    find_coverage = safe_dict(find_results.get("coverage"))
    if find_survey_stats.get("venue_title_filter_input_papers"):
        title_filter_input_count = int(find_survey_stats.get("venue_title_filter_input_papers") or title_filter_input_count or 0)
    venue_final_title_candidate_count = int(
        progress_counts.get("venue_final_title_candidates")
        or find_counts.get("venue_final_title_candidates")
        or find_survey_stats.get("venue_final_title_candidates")
        or title_candidate_count
        or 0
    )
    if venue_final_title_candidate_count and not title_candidate_count:
        title_candidate_count = venue_final_title_candidate_count
    venue_detail_fetched_count = int(
        progress_counts.get("venue_detail_fetched_candidates")
        or find_counts.get("venue_detail_fetched_candidates")
        or find_survey_stats.get("venue_detail_fetched_candidates")
        or detail_fetched_count
        or 0
    )
    detail_fetched_count = venue_detail_fetched_count or detail_fetched_count
    abstract_fetch_failed_count = int(progress_counts.get("abstract_fetch_failed_candidates") or find_counts.get("abstract_fetch_failed_candidates") or find_survey_stats.get("abstract_fetch_failed_candidates") or 0)
    final_llm_scoring_skipped_count = int(progress_counts.get("final_llm_scoring_skipped_candidates") or find_counts.get("final_llm_scoring_skipped_candidates") or find_survey_stats.get("final_llm_scoring_skipped_candidates") or 0)
    llm_scored_count = int(
        progress_counts.get("llm_scored_candidates")
        or find_counts.get("llm_scored_candidates")
        or find_survey_stats.get("llm_scored_candidates")
        or tick_lit.get("llm_scored_candidates")
        or 0
    )
    category_selected_count = int(
        progress_counts.get("venue_category_selected_papers")
        or find_counts.get("venue_category_selected_papers")
        or find_coverage.get("venue_category_selected_papers")
        or progress_counts.get("category_selected")
        or 0
    )
    if venue_metadata_counts.get("venue_category_selected_papers") is not None:
        category_selected_count = int(venue_metadata_counts.get("venue_category_selected_papers") or category_selected_count or 0)
    if not category_selected_count:
        category_selected_count = sum(int((row if isinstance(row, dict) else {}).get("selected_category_papers") or 0) for row in safe_list(find_results.get("category_scan_report")))
    project_selection = _current_project_source_selection(project, root)
    selection = tick_lit.get("selection") if isinstance(tick_lit.get("selection"), dict) else progress.get("selection") if isinstance(progress.get("selection"), dict) else project_selection
    selection = normalize_source_selection(selection)
    find_result_recommendation_rows = _human_recommendation_literature_rows(safe_list(find_results.get("strong_recommendations")) or safe_list(find_results.get("articles")))
    strong_rows = projection_recommendation_rows or find_result_recommendation_rows
    find_result_read_rows = _human_readable_literature_rows(safe_list(find_results.get("read_candidates"))) or find_result_recommendation_rows
    read_rows = projection_read_rows or find_result_read_rows or strong_rows
    strict_strong_anchor_count = strong_count
    audit_rows = [
        row for row in safe_list(find_results.get("evaluated_candidates"))
        if isinstance(row, dict)
        and (
            row.get("not_positive_support")
            or row.get("weak_candidate_for_critique")
            or row.get("foundation_demoted_from_strong")
            or str(row.get("evidence_tier") or "").lower() in {"nethreshold_for_reading", "critique_or_boundary_case", "retrieval_only", "detail_fetch_failed"}
        )
    ][:8]
    survey_rows = safe_list(find_results.get("retrieval_candidates"))[:8] or safe_list(find_results.get("survey_candidates"))[:8]

    base_title = str(selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or active_repo.get("selected_base_title") or active_repo.get("name") or selected.get("name") or "环境阶段正在选择当前基底")
    if literature_gate_blocked:
        base_title = "Find 推荐门控未过，环境基底选择暂不生效"
    repo_name = str(selected.get("name") or selected.get("repo") or active_repo.get("name") or active_repo.get("repo") or repo.get("name") or "")
    repo_url = str(selected.get("url") or selected.get("repo_url") or active_repo.get("url") or active_repo.get("repo_url") or repo.get("url") or "")
    repo_path = str(selected.get("repo_path") or selected.get("local_path") or active_repo.get("repo_path") or active_repo.get("local_path") or repo.get("repo_path") or repo.get("local_path") or "")
    route_ready_datasets = _fresh_base_ready_datasets_from_evidence(root, selected, active_repo, repo, selected.get("claim_ready_datasets") or selected.get("ready_datasets") or active_repo.get("ready_datasets") or [])
    route_dataset = str(selected.get("dataset") or selected.get("claim_ready_dataset") or active_repo.get("dataset") or (route_ready_datasets[0] if route_ready_datasets else "") or "")
    route_loader_contract_passed = bool(route_ready_datasets and _fresh_base_loader_contract_passed(root))
    latest_step = full_cycle.get("latest_step") if isinstance(full_cycle.get("latest_step"), dict) else {}
    running_step = full_cycle.get("current_running_stage") if isinstance(full_cycle.get("current_running_stage"), dict) else {}
    latest_stage = str(running_step.get("stage") or latest_step.get("stage") or "")
    latest_phase = _phase_from_stage(latest_stage)
    log_path = str(full_job.get("log_path") or "")
    log_tail = ""
    line_source = running_step.get("line_count", latest_step.get("line_count", 0))
    line_count = int(line_source or 0) if str(line_source or "").isdigit() else 0
    if log_path:
        try:
            lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = next((line.strip() for line in reversed(lines) if line.strip()), "")
            line_count = line_count or len(lines)
            # A running stage from full_research_cycle.json is authoritative.
            # Later stages often read literature artifacts; those file names must
            # not move the project summary back to the Find/literature phase.
            if not latest_stage:
                for line in reversed(lines[-80:]):
                    text = line.lower()
                    if any(marker in text for marker in ["paper-pipeline", "paper-preview", "paper-figure", "conference-preview", "submission-readiness", "latex"]):
                        latest_stage = "paper"
                        latest_phase = "paper"
                        break
                    if any(marker in text for marker in ["run_autonomous_research.py", "autonomous-research", "experiment", "trajectory-supervisor", "reference-reproduction"]):
                        latest_stage = "experiment"
                        latest_phase = "experiment"
                        break
                    if any(marker in text for marker in ["run_frontend", "semantic scholar"]):
                        latest_stage = "literature"
                        latest_phase = "literature"
                        break
        except Exception:
            pass
    elapsed = str(full_job.get("elapsed") or full_job.get("elapsed_sec") or "")
    full_job_kind = str(full_job.get("kind") or "")
    full_job_cmd = _command_text(full_job.get("cmd") or full_job.get("command"))
    taste_workers = [
        row for row in _remote_process_rows()
        if isinstance(row, dict)
        and str(row.get("kind") or "") in {"frontend", "driver"}
        and _cmd_matches_project(str(row.get("cmd") or ""), project, root)
    ]
    # Claude may call run_literature_tool.py during read/idea/experiment repair,
    # and that wrapper can spawn a real TASTE frontend/driver child. That child
    # is an auxiliary literature subtask unless the full-cycle stage itself is a
    # Find/literature stage. Do not let it clear the selected current main route.
    auxiliary_taste_literature_running = bool(
        taste_workers
        and full_job_kind not in {"frontend", "driver"}
        and not _active_stage_is_fresh_find(latest_stage)
    )
    fresh_find_running = bool(
        status == "running"
        and (
            _active_stage_is_fresh_find(latest_stage)
            or full_job_kind in {"frontend", "driver"}
        )
    )
    live_find_progress_payload: dict[str, Any] = {}
    if fresh_find_running:
        live_find_run_id = _latest_find_run_id_from_runs()
        if live_find_run_id:
            run_id = live_find_run_id
        live_find_progress_payload = safe_dict(_read_json(RUNS_DIR / run_id / "find_progress.json", {})) if run_id else {}
        strong_count = 0
        recommendation_target_count = 0
        recommendation_shortfall = 0
        literature_gate_blocked = False
        read_count = 0
        idea_count = 0
        plan_count = 0
        historical_selection = safe_dict(env.get("blocked_selection") or env.get("selected"))
        env = {
            **env,
            "valid": False,
            "blocked_by": "fresh_find_running",
            "base_selection_status": "waiting_for_current_find_results",
            "blocked_selection": historical_selection,
            "reason": "fresh Find is running; environment base selection waits for the current run outputs",
        }
        selected = {}
        repo = {}
        active_repo = {}
        base_title = "新的 Find 正在运行，环境基底等待本轮结果"
        repo_name = ""
        repo_url = ""
        repo_path = ""
    if fresh_find_running and llm_quota_blocked:
        # The persisted project find_progress may still describe the previous
        # blocked run until the fresh TASTE run syncs its first project state.
        # A live fresh Find process is the current truth for the web summary.
        progress_blocked_llm = False
        targeted_llm_blocked = False
        llm_quota_blocked = False
    reference_full_job_live = bool(reference_full_job.get("process_alive") is True and reference_full_job.get("pid") and alive(reference_full_job.get("pid")))
    active_experiment_training = _has_active_experiment_training(project, root)
    base_switch_gate_required = bool(selected_base_viability.get("status") == "blocked" and selected_base_viability.get("decision") == "base_switch_gate_required")
    base_switch_authorized = bool(
        base_switch_gate.get("status") == "pass"
        and base_switch_gate.get("decision") == "authorize_base_switch"
        and base_switch_gate.get("switch_authorized") is True
    )
    base_switch_gate_unresolved = bool(base_switch_gate_required and not base_switch_authorized)
    selected_base_viability_blocked = bool(selected_base_viability.get("status") == "blocked" and selected_base_viability.get("decision") in {"base_switch_gate_required", "continue_experiment_evidence_repair"})
    selected_base_viability_public = _selected_base_viability_public_blocker(selected_base_viability, base_title, base_switch_gate)
    selected_base_blocker_category = selected_base_viability_public.get("category", "experiment_evidence_audit") if selected_base_viability_blocked else "experiment_evidence_audit"
    if llm_quota_blocked:
        status = "blocked_literature_llm_quota_exhausted"
    elif reference_full_job_live and not full_job_live:
        status = "running"
    elif not full_job_live and not env.get("valid") and not literature_gate_blocked and run_id:
        status = "blocked_environment_base_selection_required"
    elif not full_job_live and env.get("valid") and not route_ready_datasets and not literature_gate_blocked:
        status = "blocked_fresh_base_data_required"
    elif not full_job_live and env.get("valid") and route_loader_contract_passed and status in {"blocked_fresh_base_data_required", "blocked_fresh_base_implementation_required", "blocked_no_viable_base_switch_route"}:
        status = "blocked_fresh_base_reference_probe_required"
    if status == "running" and pid and full_job_live:
        public_phase = _public_phase_for_full_cycle(latest_stage or full_job.get("stage"), project, root)
        summary = f"完整科研自循环正在运行；阶段={public_phase}；PID={pid}" + (f"；运行时长={elapsed}" if elapsed else "")
        if fresh_find_running:
            summary += "；新的 Find/文献调研正在运行，旧推荐统计仅作历史参考，等待本轮 Find 产物替换。"
        elif base_switch_gate_unresolved:
            summary = _public_run_summary_without_action_plan(summary)
    elif base_switch_gate_unresolved:
        summary = selected_base_viability_public.get("project_summary") or "完整科研自循环已停在实验证据审计；参考复现已通过，但当前主线还缺少可审计、可写入论文的候选实验结果。"
    elif selected_base_viability_blocked and status != "blocked_environment_base_selection_required":
        summary = selected_base_viability_public.get("project_summary") or "完整科研自循环已停在实验门控；参考复现已通过，但当前主线还缺少可审计、可写入论文的候选实验证据。"
    elif status == "blocked_fresh_base_data_required":
        summary = f"环境阶段已选择当前候选基底：{base_title}；但真实数据/loader 尚未通过，不能进入实验或论文证据。"
    elif status == "blocked_fresh_base_reference_probe_required":
        protocol_blocker_summary = _reference_protocol_probe_blocker_summary(protocol_probe, base_title)
        summary = f"环境阶段已选择当前候选基底：{base_title}；" + (protocol_blocker_summary or "真实数据/loader 已通过，等待参考协议/环境 manifest 探针。")
    elif reference_full_job_live:
        ref_pid = str(reference_full_job.get("pid") or "")
        ref_title = base_title or "当前基底"
        summary = f"参考复现正在运行；阶段=experiment；PID={ref_pid}；当前基底：{ref_title}"
    elif status == "blocked_after_max_cycles":
        summary = f"完整科研自循环已停止在最大轮次后；最后步骤={latest_stage or 'full-cycle'}；阶段={latest_phase or 'full-cycle'}；没有正在运行的 full-cycle。"
    elif status == "blocked_environment_base_selection_required":
        summary = "当前 Find/Read/Idea/Plan 已准备；等待环境阶段基于当前 selected_plan_id 选择基底、验证 repo/data/protocol，旧环境和旧参考复现不作为当前结果。"
    elif str(full_job.get("status") or "").lower() == "stale":
        summary = f"完整科研自循环进程已停止；最后步骤={latest_stage or 'full-cycle'}；阶段={latest_phase or 'full-cycle'}；没有正在运行的 full-cycle。"
    elif llm_quota_blocked:
        summary = llm_blocker_reason
    elif recommendation_shortfall and not fresh_find_running:
        summary = f"当前 Find 推荐文章 {strong_count}/{recommendation_target_count}，短缺 {recommendation_shortfall}；文献门控阻塞，禁止环境基底、实验、论文和结论提升。"
    else:
        summary = str(full_cycle.get("summary_zh") or full_cycle.get("summary") or f"项目：{topic}；状态：{status}。")

    summary = _public_run_summary_without_action_plan(summary)
    literature_status = "fresh_find_running" if fresh_find_running else "blocked_llm_quota_exhausted" if llm_quota_blocked else "recommendation_shortfall" if recommendation_shortfall else "current_find_packet_ready" if run_id else "missing_find_packet"
    targeted_queries = [str(item).strip() for item in safe_list(current_plan.get("targeted_search_queries")) if str(item).strip()]
    targeted_query_count = int(current_plan.get("targeted_search_query_count") or len(targeted_queries) or 0)
    if latest_targeted_tool and latest_matches_current_find:
        if latest_targeted_tool.get("queries") and not targeted_queries:
            targeted_queries = [str(item).strip() for item in safe_list(latest_targeted_tool.get("queries")) if str(item).strip()]
            targeted_query_count = len(targeted_queries)
        latest_is_record_only = bool(latest_targeted_tool.get("record_only_requested"))
        latest_has_failure = bool(latest_targeted_tool.get("failure_summary") or latest_targeted_tool.get("return_codes"))
        latest_status = str(latest_targeted_tool.get("status") or "")
        should_override_tool_status = (not latest_is_record_only) or latest_has_failure or latest_status.startswith("failed")
        if should_override_tool_status and (latest_targeted_tool.get("status") or latest_targeted_tool.get("failure_summary") or latest_targeted_tool.get("return_codes")):
            targeted_tool_status = {
                **targeted_tool_status,
                "status": latest_targeted_tool.get("status", targeted_tool_status.get("status")),
                "venue": latest_targeted_tool.get("venue", targeted_tool_status.get("venue")),
                "packet_return_code": latest_targeted_tool.get("packet_return_code", targeted_tool_status.get("packet_return_code")),
                "return_codes": latest_targeted_tool.get("return_codes", targeted_tool_status.get("return_codes")),
                "failure_summary": latest_targeted_tool.get("failure_summary", targeted_tool_status.get("failure_summary")),
                "guardrail": latest_targeted_tool.get("guardrail", targeted_tool_status.get("guardrail")),
                "record_only_requested": latest_targeted_tool.get("record_only_requested", targeted_tool_status.get("record_only_requested")),
                "new_find_allowed": latest_targeted_tool.get("new_find_allowed", targeted_tool_status.get("new_find_allowed")),
            }
    if fresh_find_running or llm_quota_blocked:
        targeted_queries = []
        targeted_query_count = 0
        targeted_tool_status = {}
    pipeline_state = safe_dict(_current_find_pipeline_summary(root, find_results=find_results))
    pipeline_validation = safe_dict(pipeline_state.get("reading_validation"))
    pipeline_content_ready = bool(pipeline_state.get("content_ready") or pipeline_state.get("read_idea_plan_ready"))
    selected_execution = safe_dict(pipeline_state.get("selected_execution")) if pipeline_content_ready else {}
    plan_validation = safe_dict(current_plan.get("reading_validation"))
    raw_read_count = read_count
    full_text_read_count = _as_int(pipeline_state.get("full_text_reading_count"), 0)
    full_text_evidence_count = _as_int(pipeline_state.get("full_text_evidence_count"), 0)
    pending_full_text_read_count = _as_int(pipeline_state.get("pending_full_text_reading_count"), 0)
    completed_read_count = full_text_read_count if (full_text_read_count or pending_full_text_read_count or pipeline_validation) else read_count
    display_read_count = raw_read_count or read_count or completed_read_count
    recommended_reading_count = len(read_rows) or strong_count
    positive_anchor_readings = _as_int(
        current_plan.get("positive_anchor_readings")
        or plan_validation.get("positive_anchor_count")
        or pipeline_state.get("positive_anchor_readings"),
        0,
    )
    critique_or_boundary_readings = _as_int(
        current_plan.get("critique_or_boundary_readings")
        or plan_validation.get("critique_or_boundary_count")
        or pipeline_state.get("critique_or_boundary_readings"),
        0,
    )
    current_find_pipeline = {
        "run_id": run_id,
        "status": str(pipeline_state.get("status") or ""),
        "failure_type": str(pipeline_state.get("failure_type") or ""),
        "next_required_action": str(pipeline_state.get("next_required_action") or ""),
        "content_ready": bool(pipeline_state.get("content_ready") or pipeline_state.get("read_idea_plan_ready")),
        "read_idea_plan_ready": bool(pipeline_state.get("read_idea_plan_ready") or pipeline_state.get("content_ready")),
        "execution_ready": bool(pipeline_state.get("execution_ready")),
        "takeover_ready": bool(pipeline_state.get("takeover_ready")),
        "read_source": str(pipeline_state.get("read_source") or ""),
        "idea_source": str(pipeline_state.get("idea_source") or ""),
        "plan_source": str(pipeline_state.get("plan_source") or ""),
        "selected_execution": selected_execution,
        "selected_execution_status": str((pipeline_state.get("selected_execution_status") if pipeline_content_ready else "") or selected_execution.get("status") or ""),
        "selected_execution_issue": str((pipeline_state.get("selected_execution_issue") if pipeline_content_ready else "") or selected_execution.get("selection_issue") or ""),
        "selected_plan_id": str((pipeline_state.get("selected_plan_id") if pipeline_content_ready else "") or selected_execution.get("selected_plan_id") or ""),
        "selected_idea_id": str((pipeline_state.get("selected_idea_id") if pipeline_content_ready else "") or selected_execution.get("selected_idea_id") or ""),
        "selected_by": str((pipeline_state.get("selected_by") if pipeline_content_ready else "") or selected_execution.get("selected_by") or ""),
        "execution_policy": safe_dict((pipeline_state.get("execution_policy") if pipeline_content_ready else {}) or selected_execution.get("execution_policy")),
        "candidate_counts": safe_dict(pipeline_state.get("candidate_counts") or selected_execution.get("candidate_counts")),
        "read_idea_plan_ready": bool(pipeline_state.get("read_idea_plan_ready") or pipeline_state.get("content_ready")),
        "readings": completed_read_count,
        "read_artifact_count": raw_read_count,
        "raw_reading_count": raw_read_count,
        "displayed_count": display_read_count,
        "read_count": completed_read_count,
        "recommended_reading_count": recommended_reading_count,
        "full_text_reading_count": full_text_read_count,
        "full_text_evidence_count": full_text_evidence_count,
        "pending_full_text_reading_count": pending_full_text_read_count,
        "pending_without_evidence_count": _as_int(pipeline_state.get("pending_without_evidence_count"), 0),
        "ideas": idea_count,
        "idea_count": idea_count,
        "plans": plan_count,
        "plan_count": plan_count,
        "recommended_count": strong_count,
        "strong_count": strong_count,
        "strong_recommendations_count": strong_count,
        "positive_anchor_readings": positive_anchor_readings,
        "critique_or_boundary_readings": critique_or_boundary_readings,
        "full_text_reading_titles": safe_list(pipeline_validation.get("full_text_reading_titles") or pipeline_state.get("full_text_reading_titles"))[:12],
        "pending_full_text_reading_titles": safe_list(pipeline_validation.get("pending_full_text_reading_titles") or pipeline_state.get("pending_full_text_reading_titles"))[:12],
        "invalid_positive_readings": _as_int(pipeline_state.get("invalid_positive_readings") or pipeline_validation.get("invalid_positive_count"), 0),
        "expected_positive_titles": safe_list(pipeline_validation.get("expected_positive_titles") or pipeline_state.get("expected_positive_titles"))[:12],
        "positive_anchor_titles": safe_list(pipeline_validation.get("positive_anchor_titles") or pipeline_state.get("positive_anchor_titles"))[:12],
        "reading_validation_blockers": safe_list(pipeline_validation.get("blockers") or pipeline_state.get("blockers"))[:6],
        "targeted_search_query_count": targeted_query_count,
        "recommended_count": strong_count,
        "strong_count": strong_count,
        "strong_recommendations_count": strong_count,
        "reading_count": completed_read_count,
        "displayed_count": display_read_count,
        "read_count": completed_read_count,
        "recommended_reading_count": recommended_reading_count,
        "read_artifact_count": raw_read_count,
        "full_text_reading_count": full_text_read_count,
        "full_text_evidence_count": full_text_evidence_count,
        "pending_full_text_reading_count": pending_full_text_read_count,
        "pending_without_evidence_count": _as_int(pipeline_state.get("pending_without_evidence_count"), 0),
        "idea_count": idea_count,
        "plan_count": plan_count,
        "summary_zh": str(pipeline_state.get("summary_zh") or f"当前 Find 推荐 {strong_count} 篇，全文精读完成 {completed_read_count} 篇，待补全文 {pending_full_text_read_count} 篇，想法 {idea_count} 个，计划 {plan_count} 个。"),
        "summary_en": str(pipeline_state.get("summary_en") or f"Current Find has {strong_count} recommendations, {completed_read_count} full-text readings, {pending_full_text_read_count} pending full-text readings, {idea_count} ideas, and {plan_count} plans."),
    }
    selected_plan_gate = _current_find_selected_plan_gate_public(current_find_pipeline)
    health_check_source_status = _current_health_check_source_status_rows(project, root, project_selection)
    literature_survey = {
        "run_id": run_id,
        "status": literature_status,
        "recommendation_target_count": recommendation_target_count,
        "recommendation_shortfall": recommendation_shortfall,
        "recommendation_gate_status": "shortfall" if recommendation_shortfall else "pass" if recommendation_target_count else "unknown",
        "selection": selection,
        "source_status": source_status[:20],
        "health_check_source_status": health_check_source_status[:20],
        "venue_sources": venue_health[:20],
        "strong_recommendations": _public_find_recommendation_rows(strong_rows, 20),
        "strong_recommendations_count": strong_count,
        "read_candidates": _public_find_recommendation_rows(read_rows, 20),
        "read_candidates_count": len(read_rows),
        "audit_candidates": [],
        "audit_candidates_count": len(audit_rows),
        "survey_candidates": [],
        "survey_candidates_count": len(survey_rows),
        "targeted_search_query_count": targeted_query_count,
        "targeted_search_status": str(targeted_tool_status.get("status") or "") if isinstance(targeted_tool_status, dict) else "",
        "counts": {
            "raw_title_index_papers": raw_title_index_count,
            "venue_total_papers_available": raw_title_index_count,
            "venue_corpus_audited_papers": raw_title_index_count,
            "venue_category_selected_papers": category_selected_count,
            "category_selected_papers": category_selected_count,
            "category_filtered_papers": int(find_survey_stats.get("category_filtered_papers") or projection_counts.get("category_filtered_papers") or 0),
            "tfidf_screened_papers": int(find_survey_stats.get("tfidf_screened_papers") or projection_counts.get("tfidf_screened_papers") or 0),
            "venue_title_filter_input_papers": title_filter_input_count,
            "title_score_input_papers": int(find_survey_stats.get("title_score_input_papers") or projection_counts.get("title_score_input_papers") or 0),
            "llm_title_scored_papers": int(find_survey_stats.get("llm_title_scored_papers") or projection_counts.get("llm_title_scored_papers") or 0),
            "abstract_scored_papers": int(find_survey_stats.get("abstract_scored_papers") or projection_counts.get("abstract_scored_papers") or llm_scored_count or 0),
            "title_candidates": title_candidate_count,
            "venue_final_title_candidates": venue_final_title_candidate_count,
            "traceable_candidates": traceable_count or title_candidate_count or venue_final_title_candidate_count,
            "detail_fetched": detail_fetched_count,
            "venue_detail_fetched_candidates": venue_detail_fetched_count,
            "evaluated_candidates": evaluated_count,
            "llm_scored_candidates": llm_scored_count or evaluated_count,
            "abstract_fetch_failed_candidates": abstract_fetch_failed_count,
            "final_llm_scoring_skipped_candidates": final_llm_scoring_skipped_count,
            "screened_ranking": strong_count,
            "strong_recommendations": strong_count,
            "recommended": strong_count,
            "recommendation_target_count": recommendation_target_count,
            "recommendation_shortfall": recommendation_shortfall,
            "articles": articles_count,
            "read_candidates": len(read_rows),
            "read_candidates_raw": len(read_rows),
            "strict_strong_anchor_count": strict_strong_anchor_count,
            "readings": completed_read_count,
            "read_artifacts": raw_read_count,
            "raw_reading_count": raw_read_count,
            "full_text_reading_count": full_text_read_count,
            "pending_full_text_reading_count": pending_full_text_read_count,
            "ideas": idea_count,
            "plans": plan_count,
        },
        "current_find_pipeline": current_find_pipeline,
        "strict_strong_anchor_count": strict_strong_anchor_count,
    }
    if recommendation_projection and not fresh_find_running:
        if recommendation_projection.get("coverage_explanation_i18n"):
            literature_survey["coverage_explanation_i18n"] = recommendation_projection.get("coverage_explanation_i18n")
        if recommendation_projection.get("recommendation_quality"):
            literature_survey["recommendation_quality"] = recommendation_projection.get("recommendation_quality")
        literature_survey["strong_recommendations_count"] = strong_count
        literature_survey["read_candidates_count"] = len(read_rows)
        literature_survey["strict_strong_anchor_count"] = strict_strong_anchor_count
    if fresh_find_running:
        live_counts = safe_dict(live_find_progress_payload.get("counts"))
        live_progress = safe_dict(live_find_progress_payload.get("live_progress"))
        live_source_status = safe_list(live_find_progress_payload.get("source_status"))
        live_venue_health = safe_list(live_find_progress_payload.get("venue_health_report"))
        live_phase = str(live_progress.get("phase") or "")
        live_is_llm_scoring = live_phase.startswith("abstract_scoring")
        live_batch_current = _as_int(live_progress.get("current"), 0) if live_is_llm_scoring else 0
        live_batch_total = _as_int(live_progress.get("total"), 0) if live_is_llm_scoring else 0
        live_batch_percent = _as_int(live_progress.get("percent"), 0) if live_is_llm_scoring else 0
        literature_survey.update({
            "status": "fresh_find_running",
            "recommendation_gate_status": "running",
            "recommendation_target_count": 0,
            "recommendation_shortfall": 0,
            "source_status": live_source_status[:20],
            "venue_sources": live_venue_health[:20],
            "strong_recommendations": [],
            "read_candidates": [],
            "audit_candidates": [],
            "survey_candidates": [],
            "counts": {
                "raw_title_index_papers": live_counts.get("raw_title_index") or 0,
                "venue_total_papers_available": live_counts.get("raw_title_index") or 0,
                "venue_corpus_audited_papers": live_counts.get("raw_title_index") or 0,
                "title_candidates": live_counts.get("title_candidates") or live_counts.get("venue_final_title_candidates") or 0,
                "venue_final_title_candidates": live_counts.get("venue_final_title_candidates") or live_counts.get("title_candidates") or 0,
                "traceable_candidates": live_counts.get("title_candidates") or live_counts.get("venue_final_title_candidates") or 0,
                "detail_fetched": live_counts.get("detail_fetched") or live_counts.get("venue_detail_fetched_candidates") or 0,
                "venue_detail_fetched_candidates": live_counts.get("venue_detail_fetched_candidates") or live_counts.get("detail_fetched") or 0,
                "evaluated_candidates": live_counts.get("evaluated_candidates") or 0,
                "abstract_fetch_failed_candidates": live_counts.get("abstract_fetch_failed_candidates") or 0,
                "final_llm_scoring_skipped_candidates": live_counts.get("final_llm_scoring_skipped_candidates") or 0,
                "llm_scoring_batches_current": live_batch_current,
                "llm_scoring_batches_total": live_batch_total,
                "llm_scoring_percent": live_batch_percent,
                "strong_recommendations": 0,
                "recommendation_target_count": 0,
                "recommendation_shortfall": 0,
                "readings": 0,
                "ideas": 0,
                "plans": 0,
            },
            "current_find_pipeline": {"run_id": run_id, "status": "fresh_find_running", "takeover_ready": False, "live_progress": live_progress},
            "note": "Fresh Find is currently running; previous recommendation statistics are hidden from the main Find page until the new run completes.",
            "note_i18n": {"zh": "新的 Find 正在运行；旧推荐统计暂不作为当前结果展示，等待本轮产物落盘。", "en": "Fresh Find is running; previous recommendation statistics are hidden until the new run lands."},
        })
    current_find_status = str(current_find_pipeline.get("status") or "").strip()
    current_plan_status = str(current_plan.get("status") or "").strip() if isinstance(current_plan, dict) else ""
    current_find_state_declares_ready = bool(
        current_plan_status in {"ready", "claude_takeover_ready", "already_current_valid_claude_artifacts"}
        or current_plan.get("takeover_ready")
        or current_plan.get("claude_current_find_ready")
        or current_plan.get("execution_ready")
    ) if isinstance(current_plan, dict) else False
    current_find_blocks_downstream = bool(
        run_id
        and current_find_status
        and current_find_status != "claude_takeover_ready"
        and (current_find_status.startswith("blocked") or current_find_status.startswith("pending"))
        and not current_find_state_declares_ready
        and not fresh_find_running
        and not llm_quota_blocked
    )
    base_selection_status = current_find_status if current_find_blocks_downstream else selected_plan_gate["status"] if selected_plan_gate.get("blocked") else "waiting_for_current_find_results" if fresh_find_running else "blocked_by_literature_gate" if literature_gate_blocked else ("selected" if env.get("valid") else "waiting_for_environment_claude_code")
    current_route_ref_gate = ref_gate
    if not env.get("valid") and not literature_gate_blocked and not fresh_find_running and not selected_plan_gate.get("blocked"):
        current_route_ref_gate = {
            "status": "not_started",
            "decision": "blocked_until_environment_base_selection",
            "human_summary": "环境阶段尚未为当前 selected_plan_id 选择基底；旧参考复现只保留为历史审计，不作为当前主线结果。",
            "summary": "环境阶段尚未为当前 selected_plan_id 选择基底；旧参考复现只保留为历史审计，不作为当前主线结果。",
        }
    if selected_plan_gate.get("blocked"):
        status = selected_plan_gate["status"]
        summary = selected_plan_gate["summary"]
    elif current_find_blocks_downstream:
        status = current_find_status
        summary = str(current_find_pipeline.get("summary_zh") or summary)
    if env.get("valid"):
        selected_base_run_id = str(selected.get("fresh_find_run_id") or active_repo.get("fresh_find_run_id") or env.get("fresh_find_run_id") or "").strip()
    else:
        selected_base_run_id = str(env.get("fresh_find_run_id") or selected.get("fresh_find_run_id") or active_repo.get("fresh_find_run_id") or "").strip()
    current_public_find_run_id = str(env.get("current_find_run_id") or run_id or "").strip()
    main_route = {"base_title": base_title, "base_venue": selected.get("venue") or active_repo.get("selected_base_venue") or "", "base_year": selected.get("year") or active_repo.get("selected_base_year") or "", "repo_name": repo_name, "repo_url": repo_url, "repo_path": repo_path, "dataset": route_dataset, "ready_datasets": route_ready_datasets[:8], "find_run_id": current_public_find_run_id, "base_selection_find_run_id": selected_base_run_id if selected_base_run_id and selected_base_run_id != current_public_find_run_id else "", "base_selection_status": base_selection_status, "selection_stage": _public_internal_names(env.get("selection_stage", "")), "selection_gate": _public_internal_names(env.get("selection_gate", "")), "readings": completed_read_count, "read_artifacts": raw_read_count, "raw_reading_count": raw_read_count, "full_text_reading_count": full_text_read_count, "pending_full_text_reading_count": pending_full_text_read_count, "ideas": idea_count, "plans": plan_count}
    blocker_summary = str(ref_gate.get("human_summary") or ref_gate.get("decision_reason") or full_cycle.get("current_goal") or "")
    submission_blockers = _current_submission_blockers(root)
    running_experiment_summary = "参考复现已通过；当前主线候选实验正在运行。训练完成、写入本地审计记录并刷新门控前，论文写作、结论提升和自动切换基底仍保持阻塞。"
    running_experiment_next_action = "等待当前训练日志和指标落盘；随后由项目代理读取产物并写入审计/刷新门控。"
    base_switch_gate_summary = selected_base_viability_public.get("summary") or "参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果；在独立授权前不能更换当前基底或提升论文结论。"
    base_switch_gate_next_action = selected_base_viability_public.get("next_action") or "等待项目代理读取当前缺口证据，并给出下一轮实验或修复动作。"
    reference_probe_next_action = "使用当前配置的实验环境补齐缺失依赖后重新运行 reference-protocol/import probe；通过前不训练、不写论文、不提升结论。" if _reference_protocol_probe_blocker_summary(protocol_probe, base_title) else "记录当前基底最小环境 manifest，并对 ready 数据集运行有界只读 reference-protocol/import probe；通过前不训练、不写论文、不提升结论。"
    selected_base_label = "当前基底"
    selected_base_viability_next_action = selected_base_viability_public.get("next_action") or (base_switch_gate_next_action if base_switch_gate_unresolved else running_experiment_next_action if active_experiment_training else "等待项目代理读取候选实验证据缺口，并给出下一步实验动作。")
    submission_blocker_next_action = "参考复现和实验门控通过后，继续补齐结论台账、坏例/反例和可靠性证据；在投稿准备度通过前保持只写草稿，禁止论文定稿或结论提升。"
    if selected_plan_gate.get("blocked"):
        blocker_summary = selected_plan_gate["summary"]
    elif llm_quota_blocked:
        blocker_summary = llm_blocker_reason
    elif fresh_find_running:
        blocker_summary = "新的 Find/文献调研正在运行；本轮完成前，旧推荐统计只作为历史参考，不作为当前结论。"
    elif recommendation_shortfall:
        blocker_summary = f"当前 Find 推荐文章 {strong_count}/{recommendation_target_count}，短缺 {recommendation_shortfall}；流程必须补检索/补评分或目标化调研，不能把弱论文凑成推荐，也不能推进论文或结论提升。"
    elif selected_base_viability_blocked and active_experiment_training:
        blocker_summary = running_experiment_summary
    elif base_switch_gate_unresolved:
        blocker_summary = base_switch_gate_summary
    elif selected_base_viability_blocked:
        blocker_summary = selected_base_viability_public.get("summary") or "参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验证据；论文预览可以生成，但不能被标记为投稿通过。"
    elif reference_full_job_live:
        blocker_summary = "TASTE 正在跑当前参考工作的论文级 full reference reproduction；完成并刷新门控前，不启动候选实验、论文写作或结论提升。"
    elif status == "blocked_fresh_base_reference_probe_required":
        blocker_summary = _reference_protocol_probe_blocker_summary(protocol_probe, base_title) or f"{base_title} 数据和 loader/import probe 已通过；当前等待参考协议/环境 manifest 探针。"
    elif submission_blockers:
        blocker_summary = _public_blocker_summary(submission_blockers[0], "submission_readiness blocked")
    if not env.get("valid") and not literature_gate_blocked:
        blocker_summary = blocker_summary or "当前 Find/Read/Idea/Plan 已准备；必须由环境阶段 Claude Code 基于当前 run 选择基底，不能使用旧 active_repo。"
    public_blocker_row = _public_blocker_row({
        "category": selected_plan_gate["category"] if selected_plan_gate.get("blocked") else "literature_llm_quota_exhausted" if llm_quota_blocked else "fresh_find_running" if fresh_find_running else "literature_recommendation_shortfall" if literature_gate_blocked else "environment_anchor_selection_required" if status == "blocked_environment_base_selection_required" else selected_base_blocker_category if selected_base_viability_blocked else "fresh_base_reference_reproduction_running" if reference_full_job_live else "fresh_base_reference_probe_required" if status == "blocked_fresh_base_reference_probe_required" else "submission_readiness" if submission_blockers else str(ref_gate.get("decision") or ref_gate.get("status") or ""),
        "severity": "block",
        "issue": blocker_summary,
        "summary": blocker_summary,
        "human_summary": blocker_summary,
    })
    public_current_goal = selected_plan_gate["next_action"] if selected_plan_gate.get("blocked") else running_experiment_next_action if active_experiment_training else "环境阶段需要项目代理基于当前推荐文章、Read/Idea/Plan、repo/data/protocol 证据选择当前基底。" if status == "blocked_environment_base_selection_required" else selected_base_viability_next_action if selected_base_viability_blocked else reference_probe_next_action if status == "blocked_fresh_base_reference_probe_required" else _public_text_for_gate(full_cycle.get("current_goal") or "")
    public_continuation_required = False if (full_job_live or active_experiment_training) else bool(full_cycle.get("continuation_required"))
    full_cycle_compact = {"status": status, "summary": summary, "summary_zh": summary, "summary_en": _public_status_summary_en(status, base_title=base_title, active_experiment_training=active_experiment_training, reference_full_job_live=reference_full_job_live, fresh_find_running=fresh_find_running, recommendation_shortfall=recommendation_shortfall), "current_goal": _public_internal_names(public_current_goal), "continuation_required": public_continuation_required, "continuation_reason": _public_internal_names(str(full_cycle.get("continuation_reason") or "")), "updated_at": str(full_cycle.get("updated_at") or tick.get("generated_at") or ""), "started_at": str(full_cycle.get("started_at") or full_job.get("started_at") or ""), "latest_step": {**latest_step, "stage": latest_stage or latest_step.get("stage", ""), "phase": latest_phase, "line_count": line_count}, "latest_blockers": [public_blocker_row] if public_blocker_row.get("summary") else [], "experiment_evidence_policy": {"status": str(selected_base_viability.get("status") or base_switch_gate.get("status") or ""), "decision": _public_internal_names(selected_base_viability.get("decision") or base_switch_gate.get("decision") or ""), "authorized": bool(base_switch_authorized), "updated_at": str(selected_base_viability.get("updated_at") or base_switch_gate.get("updated_at") or "")}, "reference_full_job": scalar(reference_full_job, ["status", "decision", "pid", "process_alive", "log_path"]), "full_cycle_job": public_full_job}
    stale_full_job = str(full_job.get("status") or "").lower() == "stale" and not full_job_live
    literature_gate_next_action = "LLM API 额度/配置不可用；请在网页保存可用 API key/base/model 并验证通过后，重新启动完整 Find/科研循环。恢复前不启动实验、论文或结论提升。" if llm_quota_blocked else "新的 Find/文献调研正在运行；等待本轮推荐文章、精读、想法和计划产物落盘。" if fresh_find_running else f"当前 Find 推荐文章 {strong_count}/{recommendation_target_count}，短缺 {recommendation_shortfall}；修复标题+摘要评分或 packet 生成，不允许用未评分/无摘要论文凑数。短缺未清零前，不启动实验、论文或结论提升。" if recommendation_shortfall else "当前 Find 推荐已完成；环境阶段需要项目代理基于当前推荐、精读、idea、plan 和 repo/data/protocol 证据选择当前基底。"
    stale_next_action = literature_gate_next_action
    tick_status = str(tick.get("status", "")) if isinstance(tick, dict) else ""
    if not full_job_live and "running" in tick_status:
        supervision_status = status or "stale_full_research_cycle_snapshot"
    else:
        supervision_status = "stale_full_research_cycle_snapshot" if stale_full_job and "running" in tick_status else tick_status
    if literature_gate_blocked and not fresh_find_running:
        supervision_status = "blocked_literature_recommendation_gate"
    reference_running_next_action = "等待 full reference reproduction 完成；随后自动刷新参考复现、科学进展、论文证据、投稿准备度和阻塞行动计划门控。"
    blocker_next_action = selected_plan_gate["next_action"] if selected_plan_gate.get("blocked") else literature_gate_next_action if (fresh_find_running or literature_gate_blocked) else "环境阶段需要项目代理基于当前推荐文章、Read/Idea/Plan、repo/data/protocol 证据选择当前基底。" if status == "blocked_environment_base_selection_required" else selected_base_viability_next_action if selected_base_viability_blocked else reference_running_next_action if reference_full_job_live else reference_probe_next_action if status == "blocked_fresh_base_reference_probe_required" else submission_blocker_next_action if submission_blockers else _clean_stale_active_worker_text(tick.get("next_action", "") if isinstance(tick, dict) else "", stale_next_action) if stale_full_job else str(tick.get("next_action") or full_cycle.get("current_goal") or "")
    if public_blocker_row.get("summary"):
        public_blocker_row["next_action"] = _public_internal_names(blocker_next_action)
        public_blocker_row["source"] = "deterministic_gate_audit"
        public_blocker_row["source_label"] = "来源：确定性门控审计"
    live_reference_job = reference_full_job if reference_full_job else safe_dict(tick.get("full_reference_job"))
    supervision = {**_empty_supervision_payload(), "status": selected_plan_gate["status"] if selected_plan_gate.get("blocked") else "blocked_literature_llm_quota_exhausted" if llm_quota_blocked else "running" if fresh_find_running else "blocked_environment_base_selection_required" if status == "blocked_environment_base_selection_required" else supervision_status, "action": _public_internal_names(tick.get("action", "")), "generated_at": tick.get("generated_at", ""), "next_action": _public_internal_names(blocker_next_action), "full_reference_job": live_reference_job, "full_cycle_job": public_full_job, "environment_base_selection": _public_environment_selection_summary(env), "claude_current_find_state": safe_dict(tick.get("claude_current_find_state"))}
    blocker_category = selected_plan_gate["category"] if selected_plan_gate.get("blocked") else "literature_llm_quota_exhausted" if llm_quota_blocked else "fresh_find_running" if fresh_find_running else "literature_recommendation_shortfall" if literature_gate_blocked else "environment_anchor_selection_required" if status == "blocked_environment_base_selection_required" else selected_base_blocker_category if selected_base_viability_blocked else "fresh_base_reference_reproduction_running" if reference_full_job_live else "fresh_base_reference_probe_required" if status == "blocked_fresh_base_reference_probe_required" else "submission_readiness" if submission_blockers else str(ref_gate.get("decision") or ref_gate.get("status") or "")
    reference_probe_has_dependency_blocker = bool(_reference_protocol_probe_blocker_summary(protocol_probe, base_title))
    selected_base_viability_title = selected_base_viability_public.get("title") or ("缺少审计就绪候选实验证据" if base_switch_gate_unresolved else "缺少当前主线候选实验证据")
    blocker_title = selected_plan_gate["title"] if selected_plan_gate.get("blocked") else "LLM API 额度/配置不可用" if llm_quota_blocked else "Find 正在运行" if fresh_find_running else "Find 推荐门控阻塞" if literature_gate_blocked else "候选实验运行中" if selected_base_viability_blocked and active_experiment_training else selected_base_viability_title if selected_base_viability_blocked else "参考复现正在运行" if reference_full_job_live else "参考协议依赖缺失" if status == "blocked_fresh_base_reference_probe_required" and reference_probe_has_dependency_blocker else "等待参考协议/环境 manifest 探针" if status == "blocked_fresh_base_reference_probe_required" else "论文证据/投稿门控阻塞" if submission_blockers else ("当前项目门控状态" if env.get("valid") else "等待环境阶段 Claude Code 选择当前基底")
    legacy_control = {"policy": "历史仓库、实验和参考复现只保留为审计记录；当前主线以本轮 Find 后的环境审查选择为准。", "details_hidden": True}
    if literature_gate_blocked and env.get("blocked_selection"):
        legacy_control["blocked_environment_selection"] = env.get("blocked_selection")
    display_ref_gate = _reference_gate_for_current_route_display(current_route_ref_gate, status, route_ready_datasets, protocol_probe, base_title or "当前基底")
    human_gate_summary = {
        "source": "deterministic_gate_audit",
        "source_label": "来源：确定性门控审计（状态和计数由项目 artifact 计算，不是项目代理自由文本）",
        "source_label_en": "Source: deterministic gate audit (status and counts are computed from project artifacts, not free-form project-agent text)",
        "summary_source": "deterministic_gate_audit",
        "status": status,
        "title": blocker_title,
        "summary": blocker_summary,
        "next_action": blocker_next_action,
        "main_route_title": main_route.get("base_title", ""),
        "main_route_repo": main_route.get("repo_name", ""),
        "reference_reproduction": {
            "source": "reference_reproduction_gate",
            "status": display_ref_gate.get("status", ""),
            "decision": display_ref_gate.get("decision", ""),
            "summary": _public_text_for_gate(display_ref_gate.get("human_summary") or display_ref_gate.get("summary") or display_ref_gate),
        },
        "scientific_progress": {
            "source": "scientific_progress_gate",
            "status": selected_plan_gate["status"] if selected_plan_gate.get("blocked") else scientific_progress_gate.get("status", ""),
            "summary": selected_plan_gate["summary"] if selected_plan_gate.get("blocked") else base_switch_gate_summary if base_switch_gate_unresolved else running_experiment_summary if active_experiment_training else (_public_gate_status_summary(scientific_progress_gate).get("human_summary") or "当前还没有可写入论文的、经过审计的候选方法实验。"),
        },
        "experiment_loop": {
            "source": "experiment_iteration_audit",
            "status": selected_plan_gate["status"] if selected_plan_gate.get("blocked") else experiment_iteration_audit.get("status", ""),
            "summary": selected_plan_gate["next_action"] if selected_plan_gate.get("blocked") else _public_text_for_gate(experiment_iteration_audit.get("human_summary") or experiment_iteration_audit.get("summary") or experiment_iteration_audit) or "实验迭代轨迹完整。",
        },
        "display_note": "页面展示当前状态摘要；项目代理原文保留在项目代理回复/receipt 中，详细证据文件、日志和产物路径保留在底部任务栏和产物区。",
        "semantic_data_provenance": selected_base_viability_public.get("semantic_data_provenance", {}),
    }
    human_summary_en = _public_status_summary_en(status, base_title=base_title, active_experiment_training=active_experiment_training, reference_full_job_live=reference_full_job_live, fresh_find_running=fresh_find_running, recommendation_shortfall=recommendation_shortfall)
    blocker_summary_public = _public_internal_names(blocker_summary)
    blocker_next_action_public = _public_internal_names(blocker_next_action)
    human = {"status": status, "target_venue": venue, "summary": summary, "summary_source": "deterministic_gate_audit", "source_label": "来源：确定性门控审计", "summary_i18n": {"zh": summary, "en": human_summary_en}, "main_route": main_route, "blocker": {"category": _public_internal_names(blocker_category), "title": blocker_title, "summary": blocker_summary_public, "next_action": blocker_next_action_public, "source": "deterministic_gate_audit", "source_label": "来源：确定性门控审计"}, "gate_summary": human_gate_summary, "literature_repair": {"targeted_search_query_count": targeted_query_count, "targeted_search_status": str(targeted_tool_status.get("status") or "") if isinstance(targeted_tool_status, dict) else ""}, "legacy_control": legacy_control, "supervision": supervision}
    record_rows = safe_list(experiment_record.get("rows"))
    experiments = safe_list(_read_json(root / "state" / "experiment_registry.json", []))
    current_route_experiments_all = _current_route_experiment_rows(experiments, repo_name, repo_path)
    current_route_record_rows = _current_route_experiment_rows(record_rows, repo_name, repo_path)
    experiment_count = len(current_route_experiments_all) or int(len(current_route_record_rows) or 0)
    completed_count = _completed_experiment_count(current_route_experiments_all)
    if not completed_count and current_route_record_rows:
        completed_count = len([row for row in current_route_record_rows if isinstance(row, dict) and "通过" in str(row.get("审计状态") or "")])
    legacy_experiment_count = len(experiments) or int(experiment_record.get("row_count") or len(record_rows) or 0)
    legacy_completed_count = _completed_experiment_count(experiments)
    pdf_candidates = [root / "paper" / "output" / venue.lower() / "paper.pdf", root / "paper" / "orchestra" / venue.lower() / "workspace" / "paper.pdf"]
    pdf_path = next((path for path in pdf_candidates if path.exists()), None)
    experiment_record_compact = {"updated_at": experiment_record.get("updated_at", ""), "row_count": int(len(current_route_record_rows) or experiment_count), "columns": safe_list(experiment_record.get("columns")), "rows": _public_experiment_rows(current_route_record_rows, 12), "csv_path": str(root / "experiments" / "experiment_records.csv") if (root / "experiments" / "experiment_records.csv").exists() else "", "json_path": str(root / "state" / "experiment_record_table.json") if (root / "state" / "experiment_record_table.json").exists() else "", "source": "state/experiment_record_table.json"}
    downstream_waiting_on_find = bool(selected_plan_gate.get("blocked") or fresh_find_running or literature_gate_blocked)
    has_legacy_experiment_history = legacy_experiment_count > experiment_count or bool(record_rows and len(record_rows) > len(current_route_record_rows))
    legacy_experiment_audit = {"experiment_count": legacy_experiment_count, "completed_experiment_count": legacy_completed_count, "csv_path": experiment_record_compact.get("csv_path", ""), "note": "旧实验记录保留为 CSV/registry 审计；当前摘要只统计当前主线记录。"} if (downstream_waiting_on_find or has_legacy_experiment_history) else {}
    current_experiments = [] if downstream_waiting_on_find else _public_experiment_rows(current_route_experiments_all, 12) if current_route_experiments_all else []
    current_experiment_count = 0 if downstream_waiting_on_find else experiment_count
    current_completed_count = 0 if downstream_waiting_on_find else completed_count
    current_experiment_record = {**experiment_record_compact, "row_count": current_experiment_count, "rows": [] if downstream_waiting_on_find else experiment_record_compact.get("rows", [])}
    experiment_display_flags = _experiment_summary_display_flags(current_route_experiments_all if not downstream_waiting_on_find else [], current_route_record_rows if not downstream_waiting_on_find else [])
    experiment_count_label = "实验/复现审计记录"
    experiment_count_help = "这是当前主线下实验与参考复现记录的审计统计，不是完整科研流程完成进度；论文结论仍以科学进展、证据和投稿门控为准。"
    paper_blocked = bool(selected_plan_gate.get("blocked") or fresh_find_running or recommendation_shortfall or not (submission_state.get("submission_ready") and submission_state.get("status") == "submission_ready"))
    preview_pdf_path = pdf_path
    preview_pdf_url = _project_file_url(root, preview_pdf_path) if preview_pdf_path else ""
    preview_tex_path = root / "paper" / "output" / venue.lower() / "paper.tex"
    venue_policy = paper_state.get("venue_submission_policy") if isinstance(paper_state.get("venue_submission_policy"), dict) else {}
    preview_blockers = paper_state.get("conference_preview_blockers") if isinstance(paper_state.get("conference_preview_blockers"), list) else []
    raw_layout_warnings = paper_state.get("paper_layout_footprint_warnings") if isinstance(paper_state.get("paper_layout_footprint_warnings"), list) else []
    layout_warnings = [item for item in (_paper_public_layout_warning_text(value) for value in raw_layout_warnings) if item]
    first_preview_blocker = preview_blockers[0] if preview_blockers else ""
    preview_blocker_text = _paper_public_blocker_text(first_preview_blocker.get("public_detail") or first_preview_blocker.get("detail") or first_preview_blocker.get("id") or "") if isinstance(first_preview_blocker, dict) else _paper_public_blocker_text(first_preview_blocker or "")
    layout_text = str(layout_warnings[0]) if layout_warnings else ""
    citation_count = _paper_int(paper_state.get("paper_normality_citation_count"))
    citation_target = _paper_int(
        paper_state.get("paper_normality_reference_target")
        or paper_state.get("paper_reference_quality_target")
        or venue_policy.get("reference_quality_target")
        or venue_policy.get("reference_quality_target")
        or venue_policy.get("official_min_references")
        or venue_policy.get("min_references")
    )
    citation_target_source = str(paper_state.get("paper_normality_reference_target_source") or venue_policy.get("reference_target_source") or "").strip()
    body_pages = _paper_int(paper_state.get("conference_preview_body_pages") or paper_state.get("paper_normality_body_pages"))
    body_limit = _paper_int(venue_policy.get("body_page_max"))
    public_diagnostics: list[str] = []
    if body_pages and body_limit:
        labels = _paper_venue_labels(paper_state if isinstance(paper_state, dict) else {})
        public_diagnostics.append(f"正文页数 {body_pages}/{body_limit}，符合当前{labels['venue_zh']}正文页数要求。" if body_pages <= body_limit else f"正文页数 {body_pages}/{body_limit}，需先定位图表、表格和参考文献占页来源。")
    elif body_pages:
        public_diagnostics.append(f"正文页数 {body_pages}；目标 venue 正文页数上限尚未解析。")
    if citation_count and citation_target:
        public_diagnostics.append(f"{('官方引用要求' if citation_target_source == 'official' else '写作引用质量目标')} {citation_count}/{citation_target}。")
    elif citation_count:
        public_diagnostics.append(f"参考文献数量 {citation_count}；目标 venue 没有官方最少引用数时，系统会使用写作质量目标。")
    if layout_warnings:
        public_diagnostics.append(f"图表版面有 {len(layout_warnings)} 项提示，优先处理图表占地和单栏适配。")
    if preview_blocker_text:
        if "参考文献覆盖不足" in preview_blocker_text or "reference_count" in preview_blocker_text or "references/citation" in preview_blocker_text:
            public_diagnostics.append("当前 写作引用质量目标未达：参考文献覆盖不足，需要补充真实且相关的已验证引用。")
        else:
            public_diagnostics.append("预览仍需完善：" + preview_blocker_text)
    self_review_blockers = paper_state.get("paper_self_review_blockers", []) if isinstance(paper_state.get("paper_self_review_blockers", []), list) else []
    self_review_evidence_blockers = paper_state.get("paper_self_review_evidence_blockers", []) if isinstance(paper_state.get("paper_self_review_evidence_blockers", []), list) else []
    if self_review_blockers or str(paper_state.get("paper_self_review_status") or "").strip().lower() == "block":
        public_diagnostics.append("论文自审未通过；具体修复项已交由项目代理处理。")
    if self_review_evidence_blockers:
        public_diagnostics.append(f"论文自审发现 {len(self_review_evidence_blockers)} 项未解决科研证据问题；PDF 只能作为检查预览，不能标记为投稿通过。")
    if body_pages and body_limit and body_pages <= body_limit and (layout_warnings or (citation_count and citation_target and citation_count < citation_target)):
        public_diagnostics.append("正文页数已符合目标要求；当前重点是调整图表占地、补足真实引用并核对模板细节。")
    common_paper_fields = {
        "venue": venue,
        "target_venue": venue,
        "venue_slug": _venue_slug(venue),
        "template_family": paper_state.get("template_family") or (paper_state.get("venue_submission_policy", {}) if isinstance(paper_state.get("venue_submission_policy", {}), dict) else {}).get("template_family", ""),
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
        "paper_reference_quality_target": paper_state.get("paper_reference_quality_target") or venue_policy.get("reference_quality_target") or venue_policy.get("reference_quality_target") or "",
        "paper_reference_official_min": paper_state.get("paper_reference_official_min") or venue_policy.get("official_min_references") or "",
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
        "conference_preview_reference_pages": paper_state.get("conference_preview_reference_pages") or paper_state.get("paper_normality_estimated_reference_pages", ""),
        "conference_preview_blockers": [],
        "conference_preview_blocker_summary": preview_blocker_text,
        "paper_layout_footprint_warnings": layout_warnings[:10],
        "paper_layout_summary": layout_text,
        "paper_public_diagnostics": public_diagnostics,
        "venue_submission_policy": venue_policy,
        "venue_requirements_summary": _venue_requirements_summary(root, venue, paper_state),
        "venue_requirements_status": paper_state.get("venue_requirements_status", "") or _venue_requirements_summary(root, venue, paper_state).get("status", ""),
        "venue_requirements_path": paper_state.get("venue_requirements_path", "") or _venue_requirements_summary(root, venue, paper_state).get("path", ""),
        "venue_requirements_public_summary": _venue_requirements_summary(root, venue, paper_state).get("summary", ""),
        "template_fetched": _paper_template_fetched(paper_state),
    }
    if paper_blocked:
        paper_stage = {
            **common_paper_fields,
            "paper_preview_repair_loop_status": "blocked" if not paper_state.get("conference_preview_ready") else paper_state.get("paper_preview_repair_loop_status", ""),
            "paper_preview_repair_rounds": paper_state.get("paper_preview_repair_rounds", ""),
            "status": "preview_available" if preview_pdf_path else (selected_plan_gate["status"] if selected_plan_gate.get("blocked") else "fresh_find_running" if fresh_find_running else "blocked_by_literature_gate" if recommendation_shortfall else "needs_writing"),
            "summary": "",
            "summary_zh": "",
            "paper_generation_skipped": False,
            "paper_generation_skipped_reason": "投稿/证据门控未通过；当前 PDF 作为目标 venue 稿件预览展示，不标记为投稿通过" if preview_pdf_path else "no current preview PDF exists",
            "pdf_ready": bool(preview_pdf_path),
            "pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
            "pdf_url": preview_pdf_url,
            "blocked_preview_available": bool(preview_pdf_path),
            "blocked_pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
            "blocked_pdf_url": preview_pdf_url,
            "blocked_tex_path": str(preview_tex_path) if preview_tex_path.exists() else "",
            "blocked_tex_url": _project_file_url(root, preview_tex_path) if preview_tex_path.exists() else "",
            "latest_generated_pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
            "latest_generated_pdf_url": preview_pdf_url,
            "raw_pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
            "raw_pdf_url": preview_pdf_url,
        }
        paper_stage["summary"] = _paper_stage_public_message(paper_stage)
        paper_stage["summary_zh"] = paper_stage["summary"]
    else:
        paper_stage = {
            **common_paper_fields,
            "status": paper_state.get("status") or paper_state.get("paper_stage_status") or ("preview_available" if pdf_path else "not_started"),
            "summary": paper_state.get("summary") or paper_state.get("summary_zh") or "",
            "summary_zh": paper_state.get("summary_zh") or paper_state.get("summary") or "",
            "paper_generation_skipped": paper_state.get("paper_generation_skipped", False),
            "paper_generation_skipped_reason": paper_state.get("paper_generation_skipped_reason", ""),
            "pdf_ready": bool(pdf_path),
            "pdf_path": str(pdf_path) if pdf_path else "",
            "pdf_url": _project_file_url(root, pdf_path) if pdf_path else "",
            "raw_pdf_path": str(pdf_path) if pdf_path else "",
            "raw_pdf_url": _project_file_url(root, pdf_path) if pdf_path else "",
        }
        if not paper_stage["summary"]:
            paper_stage["summary"] = _paper_stage_public_message(paper_stage)
            paper_stage["summary_zh"] = paper_stage["summary"]
    environment_status = selected_plan_gate["status"] if selected_plan_gate.get("blocked") else "waiting_for_current_find_results" if fresh_find_running else "blocked_by_literature_gate" if literature_gate_blocked else ("selected" if env.get("valid") else "waiting_for_environment_base_selection")
    experiment_stage_status = selected_plan_gate["status"] if selected_plan_gate.get("blocked") else "fresh_find_running" if fresh_find_running else "blocked_by_literature_gate" if literature_gate_blocked else status
    environment_display_selected = selected or existing_selected
    environment_display_active_repo = active_repo or existing_active_repo
    environment_display_repo_name = repo_name or str(environment_display_selected.get("name") or environment_display_selected.get("repo") or environment_display_active_repo.get("name") or environment_display_active_repo.get("repo") or "")
    environment_display_repo_url = repo_url or str(environment_display_selected.get("url") or environment_display_selected.get("repo_url") or environment_display_active_repo.get("url") or environment_display_active_repo.get("repo_url") or "")
    environment_display_repo_path = repo_path or str(environment_display_selected.get("repo_path") or environment_display_selected.get("local_path") or environment_display_active_repo.get("repo_path") or environment_display_active_repo.get("local_path") or "")
    environment_stage = _public_environment_stage(status=environment_status, env=env, selected=environment_display_selected, active_repo=environment_display_active_repo, repo_name=environment_display_repo_name, repo_url=environment_display_repo_url, repo_path=environment_display_repo_path, ref_gate=current_route_ref_gate, reference_full_job=reference_full_job, route_dataset=route_dataset, route_ready_datasets=route_ready_datasets, protocol_probe=protocol_probe)
    experiment_module_summary = _public_experiment_module_summary(
        status=experiment_stage_status,
        reference_gate=current_route_ref_gate,
        scientific_progress_gate=scientific_progress_gate,
        experiment_iteration_audit=experiment_iteration_audit,
        experiment_rows=current_route_experiments_all if not downstream_waiting_on_find else [],
        record_rows=current_route_record_rows if not downstream_waiting_on_find else [],
        completed_count=current_completed_count,
        total_count=current_experiment_count,
        active_training=active_experiment_training,
        reference_job_live=reference_full_job_live,
        fresh_find_running=fresh_find_running,
        literature_gate_blocked=literature_gate_blocked,
        recommendation_shortfall=recommendation_shortfall,
        next_action=blocker_next_action,
    )
    read_matches_recommendations = bool(strong_count and display_read_count == strong_count)
    idea_score_values = []
    idea_rows = [row for row in safe_list(ideas_results.get("ideas")) if isinstance(row, dict)]
    plan_rows = [row for row in safe_list(plans_results.get("plans")) if isinstance(row, dict)]
    selected_execution_plan_id = str(current_find_pipeline.get("selected_plan_id") or selected_execution.get("selected_plan_id") or "").strip()
    selected_execution_idea_id = str(current_find_pipeline.get("selected_idea_id") or selected_execution.get("selected_idea_id") or "").strip()
    selected_execution_status = str(current_find_pipeline.get("selected_execution_status") or selected_execution.get("status") or "").strip()
    selected_execution_issue = str(current_find_pipeline.get("selected_execution_issue") or selected_execution.get("selection_issue") or "").strip()
    selected_execution_ready = bool(
        plan_count
        and selected_execution_plan_id
        and not selected_execution_issue
        and (
            selected_execution_status == "selected_plan_ready"
            or current_find_pipeline.get("execution_ready") is True
            or selected_execution.get("status") == "selected_plan_ready"
        )
    )
    selected_plan_row = next((row for row in plan_rows if str(row.get("plan_id") or row.get("id") or "").strip() == selected_execution_plan_id), {})
    selected_idea_row = next((row for row in idea_rows if str(row.get("id") or row.get("idea_id") or "").strip() == selected_execution_idea_id), {})
    selected_plan_title = str(
        selected_plan_row.get("title")
        or safe_dict(selected_execution.get("selected_plan")).get("title")
        or selected_execution_plan_id
    ).strip()
    selected_idea_title = str(
        selected_idea_row.get("title")
        or safe_dict(selected_execution.get("selected_idea")).get("title")
        or selected_execution_idea_id
    ).strip()
    for idea_row in idea_rows:
        score_value = idea_row.get("score") if idea_row.get("score") not in (None, "") else idea_row.get("idea_score")
        if score_value not in (None, ""):
            idea_score_values.append(score_value)
    approved_idea_count = 0
    selected_idea_counted = False
    for row in idea_rows:
        row_id = str(row.get("id") or row.get("idea_id") or "").strip()
        row_passed = str(row.get("status") or "").lower() in {"approved", "pass", "pursue"}
        row_selected = bool(selected_execution_idea_id and row_id == selected_execution_idea_id)
        if row_passed or row_selected:
            approved_idea_count += 1
            selected_idea_counted = selected_idea_counted or row_selected
    if selected_execution_ready and selected_execution_idea_id and not selected_idea_counted and selected_idea_row:
        approved_idea_count += 1
    approved_plan_count = 0
    selected_plan_counted = False
    for row in plan_rows:
        row_id = str(row.get("plan_id") or row.get("id") or "").strip()
        row_passed = str(row.get("status") or "").lower() in {"approved", "pass", "ready", "completed", "waiting_for_environment_base_selection"}
        row_selected = bool(selected_execution_plan_id and row_id == selected_execution_plan_id)
        if row_passed or row_selected:
            approved_plan_count += 1
            selected_plan_counted = selected_plan_counted or row_selected
    if selected_execution_ready and selected_execution_plan_id and not selected_plan_counted and selected_plan_row:
        approved_plan_count += 1
    plan_backlog_count = max(0, plan_count - 1) if selected_execution_ready else max(0, plan_count - approved_plan_count)
    find_module_summary = (
        "新的 Find 正在运行；等待本轮检索、详情抓取、评分和推荐产物落盘。"
        if fresh_find_running else
        f"当前 Find 已形成 {strong_count} 篇推荐论文；推荐列表与精读列表按同一数量收口。"
        if run_id else
        "尚未找到当前 Find 产物。"
    )
    if recommendation_shortfall and not fresh_find_running:
        find_module_summary = f"当前 Find 推荐 {strong_count}/{recommendation_target_count} 篇，仍需补齐高质量推荐后才能进入下游步骤。"
    pending_deep_read_count = _as_int(pipeline_state.get("pending_deep_read_synthesis_count") or pipeline_validation.get("pending_deep_read_synthesis_count"), 0)
    pending_without_evidence_count = _as_int(pipeline_state.get("pending_without_evidence_count") or pipeline_validation.get("pending_without_evidence_count"), 0)
    pending_full_text_titles = [str(item).strip() for item in safe_list(pipeline_state.get("pending_full_text_reading_titles") or pipeline_validation.get("pending_full_text_reading_titles")) if str(item).strip()]
    pending_deep_read_titles = [str(item).strip() for item in safe_list(pipeline_state.get("pending_deep_read_synthesis_titles") or pipeline_validation.get("pending_deep_read_synthesis_titles")) if str(item).strip()]
    pending_without_evidence_titles = [str(item).strip() for item in safe_list(pipeline_state.get("pending_without_evidence_titles") or pipeline_validation.get("pending_without_evidence_titles")) if str(item).strip()]
    pending_full_text_title_text = "；缺失全文证据：" + "；".join(pending_without_evidence_titles[:5]) if pending_without_evidence_titles else ""
    pending_deep_read_title_text = "；需重写：" + "；".join((pending_deep_read_titles or pending_full_text_titles)[:5]) if (pending_deep_read_titles or pending_full_text_titles) else ""
    read_module_summary = (
        "新的 Find 正在运行；精读等待推荐列表稳定。"
        if fresh_find_running else
        f"当前 Find 全文证据覆盖 {full_text_evidence_count}/{strong_count or recommended_reading_count or full_text_evidence_count} 篇，仍缺全文证据 {pending_without_evidence_count} 篇{pending_full_text_title_text}。补齐同篇 PDF/HTML 证据后才能生成全文精读。"
        if pending_without_evidence_count else
        f"当前 Find 全文证据覆盖 {full_text_evidence_count}/{strong_count or recommended_reading_count or full_text_evidence_count} 篇；其中 {pending_deep_read_count} 篇已有正文证据但精读合成未通过{pending_deep_read_title_text}。请重新运行精读生成合格中文 synthesis。"
        if pending_deep_read_count else
        f"当前精读展示 {display_read_count}/{strong_count or display_read_count} 篇；全文精读合格 {full_text_read_count} 篇，待补 {pending_full_text_read_count} 篇。"
        if read_count or full_text_read_count or pending_full_text_read_count else
        "当前 Find 尚未产出精读结果。"
    )
    idea_module_summary = (
        f"当前精读后形成 {idea_count} 个想法，其中 {len(idea_score_values)} 个带评分，{approved_idea_count} 个已通过。"
        if idea_count and not selected_execution_ready else
        f"当前精读后形成 {idea_count} 个想法；执行想法已选择：{selected_idea_title or selected_execution_idea_id}。其余想法保留候选池。"
        if idea_count and selected_execution_ready else
        "当前 Find 尚未形成精读后的想法。"
    )
    plan_module_summary = (
        f"当前已形成 {plan_count} 个计划；唯一执行计划已选择：{selected_plan_title or selected_execution_plan_id}。其余 {plan_backlog_count} 个为候选池，不驱动环境、实验或写作。"
        if selected_execution_ready else
        f"当前已形成 {plan_count} 个计划，{approved_plan_count} 个可等待环境审查后落到真实仓库、数据和协议。"
        if plan_count else
        "当前 Find 尚未形成实验计划。"
    )
    planning_stages = {
        "find": {
            "status": literature_status,
            "run_id": run_id,
            "summary": find_module_summary,
            "summary_zh": find_module_summary,
            "summary_en": "Current Find status is shown with recommendation, reading, idea, and plan counts.",
            "module_summary": find_module_summary,
            "module_summary_zh": find_module_summary,
            "counts": {
                "raw_title_index_papers": raw_title_index_count,
                "venue_total_papers_available": raw_title_index_count,
                "venue_corpus_audited_papers": raw_title_index_count,
                "category_selected_papers": category_selected_count,
                "venue_category_selected_papers": category_selected_count,
                "venue_title_filter_input_papers": title_filter_input_count,
                "title_candidates": title_candidate_count,
                "venue_final_title_candidates": venue_final_title_candidate_count,
                "traceable_candidates": title_candidate_count or venue_final_title_candidate_count,
                "detail_fetched": detail_fetched_count,
                "venue_detail_fetched_candidates": venue_detail_fetched_count,
                "evaluated_candidates": evaluated_count,
                "llm_scored_candidates": safe_dict(literature_survey.get("counts")).get("llm_scored_candidates") or evaluated_count,
                "abstract_fetch_failed_candidates": abstract_fetch_failed_count,
                "final_llm_scoring_skipped_candidates": final_llm_scoring_skipped_count,
                "recommended": strong_count,
                "strong_recommendations": strong_count,
                "articles": articles_count,
                "read_candidates": len(read_rows),
                "read_candidates_raw": len(read_rows),
                "strict_strong_anchor_count": strict_strong_anchor_count,
                "recommended_readings": len(read_rows),
                "recommendation_target_count": recommendation_target_count,
                "recommendation_shortfall": recommendation_shortfall,
            },
            "recommendation_quality": literature_survey.get("recommendation_quality", {}),
        },
        "read": {
            "status": "fresh_find_running" if fresh_find_running else "blocked" if str(pipeline_state.get("status") or "").startswith("blocked") or pending_full_text_read_count else "pass" if full_text_read_count >= strong_count and strong_count else "syncing" if strong_count else "not_started",
            "run_id": run_id,
            "summary": read_module_summary,
            "summary_zh": read_module_summary,
            "module_summary": read_module_summary,
            "recommended_count": strong_count,
            "reading_count": display_read_count,
            "read_artifact_count": raw_read_count,
            "full_text_reading_count": full_text_read_count,
            "full_text_evidence_count": full_text_evidence_count,
            "pending_full_text_reading_count": pending_full_text_read_count,
            "pending_full_text_reading_titles": pending_full_text_titles[:12],
            "read_matches_recommendations": display_read_count >= strong_count if strong_count else read_matches_recommendations,
        },
        "idea": {
            "status": "fresh_find_running" if fresh_find_running else "pass" if selected_execution_ready and idea_count else "pass" if idea_count >= 5 and idea_score_values else "needs_attention" if idea_count else "not_started",
            "run_id": run_id,
            "summary": idea_module_summary,
            "summary_zh": idea_module_summary,
            "module_summary": idea_module_summary,
            "idea_count": idea_count,
            "scored_idea_count": len(idea_score_values),
            "approved_idea_count": approved_idea_count,
            "selected_idea_id": str(selected_execution.get("selected_idea_id") or ""),
            "selected_plan_id": str(selected_execution.get("selected_plan_id") or ""),
            "selected_execution_status": str(selected_execution.get("status") or ""),
            "source": "current_find_reading_output" if idea_count else "",
        },
        "plan": {
            "status": selected_plan_gate["status"] if selected_plan_gate.get("blocked") else "fresh_find_running" if fresh_find_running else "pass" if selected_execution_ready else "pass" if plan_count >= 5 else "needs_attention" if plan_count else "not_started",
            "run_id": run_id,
            "summary": plan_module_summary,
            "summary_zh": plan_module_summary,
            "module_summary": plan_module_summary,
            "plan_count": plan_count,
            "approved_plan_count": approved_plan_count,
            "selected_execution": selected_execution,
            "selected_execution_status": str(selected_execution.get("status") or ""),
            "selected_plan_id": selected_execution_plan_id,
            "selected_idea_id": selected_execution_idea_id,
            "execution_policy": safe_dict(selected_execution.get("execution_policy")),
            "selected_execution_ready": selected_execution_ready,
            "candidate_backlog_plan_count": plan_backlog_count,
            "source": "current_find_reading_output" if plan_count else "",
        },
    }
    stages = {**planning_stages, "environment": environment_stage, "experiment": {"status": experiment_stage_status, **experiment_module_summary, "human_gate_summary": human_gate_summary, "experiment_count": current_experiment_count, "completed_experiment_count": current_completed_count, "experiment_count_label": experiment_count_label, "experiment_count_help": experiment_count_help, "recent_experiments": current_experiments, "experiments": current_experiments, "experiment_record": current_experiment_record, **experiment_display_flags, "legacy_experiment_audit": legacy_experiment_audit, "reference_reproduction_gate": scalar(display_ref_gate, ["status", "decision", "decision_reason", "human_summary"]), "scientific_progress_gate": _public_gate_status_summary(scientific_progress_gate), "experiment_iteration_audit": _public_gate_status_summary(experiment_iteration_audit), "full_research_cycle": full_cycle_compact}, "paper": paper_stage}
    artifacts = [item for item in [artifact("find_progress.json", root / "planning" / "finding" / "find_progress.json"), current_run_artifact("find_results.json", root / "planning" / "finding" / "find_results.json"), artifact("article.md", root / "planning" / "finding" / "article.md", "markdown") if run_id else None, current_run_artifact("read_results.json", root / "planning" / "finding" / "read_results.json"), current_run_artifact("ideas.json", root / "planning" / "finding" / "ideas.json"), current_run_artifact("plans.json", root / "planning" / "finding" / "plans.json"), current_run_artifact("current_find_research_plan.json", root / "state" / "current_find_research_plan.json"), artifact("evidence_ready_repo_selection.json", root / "state" / "evidence_ready_repo_selection.json") if not downstream_waiting_on_find else None, artifact("blocker_action_plan.json", root / "state" / "blocker_action_plan.json") if not downstream_waiting_on_find else None, artifact("reference_reproduction_gate.json", root / "state" / "reference_reproduction_gate.json"), artifact("scientific_progress_gate.json", root / "state" / "scientific_progress_gate.json") if not downstream_waiting_on_find else None, artifact("experiment_iteration_audit.json", root / "state" / "experiment_iteration_audit.json") if not downstream_waiting_on_find else None, artifact("full_research_cycle.json", root / "state" / "full_research_cycle.json"), artifact("experiment_records.csv", root / "experiments" / "experiment_records.csv", "text") if not downstream_waiting_on_find else None, artifact("experiment_record_table.json", root / "state" / "experiment_record_table.json") if not downstream_waiting_on_find else None, artifact("supervision_tick.json", root / "state" / "supervision_tick.json"), artifact("paper.pdf", pdf_path, "pdf") if (pdf_path and not paper_blocked) else None] if item]
    runtime = runtime_payload()
    claude_status = _claude_status_payload(root)
    guidance_queue = safe_list(_read_json(root / "state" / "guidance_queue.json", []))
    queued_guidance = [
        scalar(row, ["id", "stage", "target_agent_id", "source", "message", "status", "created_at"])
        for row in guidance_queue
        if isinstance(row, dict) and str(row.get("status") or "queued") == "queued"
    ][-5:]
    runtime_public = dict(runtime.get("runtime", {}) if isinstance(runtime.get("runtime"), dict) else {})
    runtime_public.pop("env_overrides", None)
    runtime_public.pop("extra_path", None)
    runtime_compact = dict(runtime) if isinstance(runtime, dict) else {}
    runtime_compact["runtime"] = runtime_public
    run_preferences = _public_run_preferences(project, root, cfg, runtime_public, project_selection)
    state_compact = {
        "full_research_cycle": full_cycle_compact,
        "human_gate_summary": human_gate_summary,
        "experiment_count": current_experiment_count,
        "completed_experiment_count": current_completed_count,
        "experiment_count_label": experiment_count_label,
        "experiment_count_help": experiment_count_help,
        "recent_experiments": current_experiments[:4] if current_experiments else [],
        "legacy_experiment_audit": legacy_experiment_audit,
        "experiment_record": {**current_experiment_record, "rows": []},
        **experiment_display_flags,
        "reference_reproduction_gate": scalar(display_ref_gate, ["status", "decision", "decision_reason", "human_summary"]),
        "scientific_progress_gate": _public_gate_status_summary(scientific_progress_gate),
        "experiment_iteration_audit": _public_gate_status_summary(experiment_iteration_audit),
        "submission_readiness": scalar(submission_state, ["status", "submission_ready", "promotion_gate"]),
        "paper_pdf_count": 1 if (pdf_path and not paper_blocked) else 0,
        "current_find_pipeline": literature_survey["current_find_pipeline"],
    }
    result = {
        "project": project,
        "topic": topic,
        "summary": summary,
        "status": status,
        "config": _public_project_identity_config(project, cfg, topic),
        "run_preferences": run_preferences,
        "path": str(root),
        "full_research_cycle": full_cycle_compact,
        "blockers": full_cycle_compact.get("latest_blockers", []),
        "current_blocker": public_blocker_row,
        "human_supervision": human,
        "human_gate_summary": human_gate_summary,
        "main_route": main_route,
        "stages": stages,
        "state": state_compact,
        "literature_survey": literature_survey,
        "current_find_pipeline": literature_survey["current_find_pipeline"],
        "readings": completed_read_count,
        "read_artifacts": raw_read_count,
        "full_text_reading_count": full_text_read_count,
        "pending_full_text_reading_count": pending_full_text_read_count,
        "ideas": idea_count,
        "plans": plan_count,
        "fresh_base": {
            "title": base_title,
            "venue": main_route.get("base_venue", ""),
            "year": main_route.get("base_year", ""),
            "repo_name": repo_name,
            "repo_path": repo_path,
            "dataset": main_route.get("dataset", ""),
            "ready_datasets": main_route.get("ready_datasets", []),
        },
        "supervision": human["supervision"],
        "queued_guidance": queued_guidance,
        "runtime": runtime_compact,
        "claude_status": claude_status,
        "artifacts": artifacts,
        "payload_bytes": 0,
    }
    current_config_venue = run_preferences.get("target_venue") or run_preferences.get("venue") or venue
    if current_config_venue:
        result["target_venue"] = current_config_venue
        result["venue"] = current_config_venue
    result["payload_bytes"] = _json_size(result)
    return result


def _lightweight_project_summary(project: str, root: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    def safe_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def safe_list(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def count_payload(payload: Any, key: str) -> int:
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return len(payload[key])
        if isinstance(payload, list):
            return len(payload)
        return 0

    def scalar(src: Any, keys: list[str]) -> dict[str, Any]:
        row = src if isinstance(src, dict) else {}
        out: dict[str, Any] = {}
        for key in keys:
            value = row.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
        return out

    def compact_rows(rows: Any, limit: int = 8) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in safe_list(rows)[-limit:]:
            if not isinstance(row, dict):
                continue
            item = scalar(row, [
                'timestamp', 'started_at', 'finished_at', 'experiment_id', 'name', 'repo',
                'repo_path', 'dataset', 'method', 'method_slug', 'status', 'metric_name',
                'metric_value', 'result', 'audit_ready', 'artifact_path', 'audit_path',
                'duration_sec', 'command', 'notes', 'claim_verdict', 'counterexample_outcome',
            ])
            if isinstance(row.get('metrics'), dict):
                item['metrics'] = {str(k): v for k, v in list(row['metrics'].items())[:8] if isinstance(v, (str, int, float, bool)) or v is None}
            out.append(item)
        return out

    def artifact(name: str, path: Path, kind: str = 'json') -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            stat = path.stat()
            return {'name': name, 'kind': kind, 'path': str(path), 'size_bytes': stat.st_size, 'updated_at': dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat()}
        except Exception:
            return None

    find_results = safe_dict(_current_find_results_light(root, project))
    raw_source_selection = find_results.get('selection')
    source_selection = normalize_source_selection(raw_source_selection) if isinstance(raw_source_selection, dict) else _current_project_source_selection(project, root)
    recommendation_projection = safe_dict(_current_find_recommendation_projection(root, str(find_results.get('run_id') or '')))
    projection_counts = safe_dict(recommendation_projection.get('counts'))
    projection_survey_stats = safe_dict(recommendation_projection.get('survey_stats'))
    pipeline_stage_count_keys = {
        'category_filtered_papers', 'tfidf_screened_papers', 'venue_title_filter_input_papers', 'title_score_input_papers',
        'llm_title_scored_papers', 'abstract_scored_papers', 'llm_scored_candidates', 'recommended_papers',
    }
    read_results = safe_dict(_read_json(root / 'planning' / 'finding' / 'read_results.json', {}))
    ideas_results = safe_dict(_read_json(root / 'planning' / 'finding' / 'ideas.json', {}))
    plans_results = safe_dict(_read_json(root / 'planning' / 'finding' / 'plans.json', {}))
    current_plan = safe_dict(_read_json(root / 'state' / 'current_find_research_plan.json', {}))
    fresh_impl = safe_dict(_read_json(root / 'state' / 'fresh_base_implementation_plan.json', {}))
    ref_gate = safe_dict(_read_json(root / 'state' / 'reference_reproduction_gate.json', {}))
    protocol_probe = safe_dict(_fresh_base_protocol_probe(root))
    selected_base_viability = safe_dict(_read_json(root / 'state' / 'selected_base_viability_gate.json', {}))
    base_switch_gate = safe_dict(_read_json(root / 'state' / 'base_switch_gate.json', {}))
    full_cycle_raw = safe_dict(_read_json(root / 'state' / 'full_research_cycle.json', {}))
    reference_full_job = safe_dict(_fresh_base_reference_full_job(root))
    supervision = safe_dict(_read_json(root / 'state' / 'supervision_tick.json', {}))
    tick_full_job = supervision.get('full_cycle_job') if isinstance(supervision.get('full_cycle_job'), dict) else {}
    full_job = _normalize_full_cycle_job(root, project, tick_full_job or _read_json(root / 'state' / 'full_cycle_job.json', {}))
    full_cycle_raw = _sanitize_stale_full_cycle_summary(full_cycle_raw, full_job, root=root)
    env = _current_environment_selection(root)
    selected = safe_dict(env.get('selected')) if env.get('valid') else {}
    repo = safe_dict(fresh_impl.get('repo')) if env.get('valid') else {}
    active_repo = safe_dict(_read_json(root / 'state' / 'active_repo.json', {})) if env.get('valid') else {}
    # The lightweight compact builder must follow the same current-route policy
    # as the full builder: current selection/active_repo wins, stale implementation
    # plans are history until rebuilt for the current run.
    if env.get('valid'):
        repo_run = str(fresh_impl.get('fresh_find_run_id') or '').strip()
        repo_path_text = str(repo.get('repo_path') or repo.get('local_path') or '').strip()
        selected_path_text = str(selected.get('repo_path') or selected.get('local_path') or active_repo.get('repo_path') or active_repo.get('local_path') or '').strip()
        selected_name_text = str(selected.get('name') or selected.get('repo') or active_repo.get('name') or active_repo.get('repo') or '').strip().lower()
        repo_name_text = str(repo.get('name') or repo.get('repo') or '').strip().lower()
        if (repo_run and repo_run != str(env.get('current_find_run_id') or '').strip()) or (repo_path_text and selected_path_text and repo_path_text != selected_path_text) or (repo_name_text and selected_name_text and repo_name_text != selected_name_text):
            repo = {}

    venue = _display_venue(cfg.get('target_venue') or cfg.get('venue') or '') if isinstance(cfg, dict) else ''
    topic = str(cfg.get('topic') or cfg.get('title') or project) if isinstance(cfg, dict) else project
    pipeline_contract = safe_dict(_current_find_pipeline_summary(root, find_results=find_results))
    read_count = int((pipeline_contract.get('readings') or pipeline_contract.get('reading_count') or count_payload(read_results, 'readings') or current_plan.get('current_find_reading_count') or 0))
    idea_count = int((pipeline_contract.get('ideas') or pipeline_contract.get('idea_count') or count_payload(ideas_results, 'ideas') or current_plan.get('current_find_idea_count') or 0))
    plan_count = int((pipeline_contract.get('plans') or pipeline_contract.get('plan_count') or count_payload(plans_results, 'plans') or current_plan.get('current_find_plan_count') or 0))
    structured_read_count = count_payload(read_results, 'readings')
    raw_read_count = int(pipeline_contract.get('read_artifact_count') or pipeline_contract.get('raw_reading_count') or structured_read_count or read_count or 0)
    raw_read_count = max(raw_read_count, structured_read_count)
    read_count = max(read_count, structured_read_count)
    full_text_read_count = int(pipeline_contract.get('full_text_reading_count') or read_count or 0)
    pending_full_text_read_count = int(pipeline_contract.get('pending_full_text_reading_count') or 0)

    base_title = str(selected.get('title') or selected.get('literature_base_title') or selected.get('selected_base_title') or active_repo.get('selected_base_title') or active_repo.get('name') or selected.get('name') or '环境阶段正在选择当前基底')
    repo_name = str(selected.get('name') or selected.get('repo') or active_repo.get('name') or active_repo.get('repo') or repo.get('name') or '')
    repo_url = str(selected.get('url') or selected.get('repo_url') or active_repo.get('url') or active_repo.get('repo_url') or repo.get('url') or '')
    repo_path = str(selected.get('repo_path') or selected.get('local_path') or active_repo.get('repo_path') or active_repo.get('local_path') or repo.get('repo_path') or repo.get('local_path') or '')
    route_ready_datasets = _fresh_base_ready_datasets_from_evidence(root, selected, active_repo, repo, selected.get('claim_ready_datasets') or selected.get('ready_datasets') or active_repo.get('ready_datasets') or [])
    route_dataset = str(selected.get('dataset') or selected.get('claim_ready_dataset') or active_repo.get('dataset') or (route_ready_datasets[0] if route_ready_datasets else '') or '')
    route_loader_contract_passed = bool(route_ready_datasets and _fresh_base_loader_contract_passed(root))

    experiment_registry = _read_json(root / 'state' / 'experiment_registry.json', [])
    experiments = safe_list(experiment_registry)
    experiment_record = safe_dict(_experiment_record_table(root, sync_running=False))
    record_rows = safe_list(experiment_record.get('rows'))
    current_route_experiments_all = _current_route_experiment_rows(experiments, repo_name, repo_path)
    current_route_record_rows = _current_route_experiment_rows(record_rows, repo_name, repo_path)
    experiment_count = len(current_route_experiments_all) or int(len(current_route_record_rows) or 0)
    completed_count = _completed_experiment_count(current_route_experiments_all)
    if not completed_count and current_route_record_rows:
        completed_count = len([row for row in current_route_record_rows if isinstance(row, dict) and '通过' in str(row.get('审计状态') or '')])
    experiment_display_flags = _experiment_summary_display_flags(current_route_experiments_all, current_route_record_rows)
    legacy_experiment_count = len(experiments) or int(experiment_record.get('row_count') or len(record_rows) or 0)
    legacy_completed_count = _completed_experiment_count(experiments)
    experiment_record_compact = {
        'updated_at': experiment_record.get('updated_at', ''),
        'row_count': int(len(current_route_record_rows) or experiment_count),
        'columns': safe_list(experiment_record.get('columns')),
        'rows': _public_experiment_rows(current_route_record_rows, 12),
        'csv_path': str(root / 'experiments' / 'experiment_records.csv') if (root / 'experiments' / 'experiment_records.csv').exists() else '',
        'csv_url': _project_file_url(root, root / 'experiments' / 'experiment_records.csv') if (root / 'experiments' / 'experiment_records.csv').exists() else '',
        'json_path': str(root / 'state' / 'experiment_record_table.json') if (root / 'state' / 'experiment_record_table.json').exists() else '',
        'json_url': _project_file_url(root, root / 'state' / 'experiment_record_table.json') if (root / 'state' / 'experiment_record_table.json').exists() else '',
        'source': 'state/experiment_record_table.json',
    }

    find_summary = _find_summary_from_payload(find_results)
    progress = safe_dict(_read_json(root / 'planning' / 'finding' / 'find_progress.json', {}))
    progress_counts = safe_dict(progress.get('counts')) if _payload_matches_current_run(progress, str(find_summary.get('run_id') or '')) else {}
    for key in pipeline_stage_count_keys:
        value = projection_survey_stats.get(key)
        if value in (None, ''):
            value = projection_counts.get(key)
        if value in (None, ''):
            value = progress_counts.get(key)
        if value not in (None, ''):
            find_summary[key] = value
    if recommendation_projection:
        raw_recommendation_rows = safe_list(recommendation_projection.get('strong_recommendations')) or safe_list(recommendation_projection.get('recommendations')) or safe_list(recommendation_projection.get('articles'))
        recommendation_rows = _human_recommendation_literature_rows(raw_recommendation_rows)
        raw_read_candidate_rows = safe_list(recommendation_projection.get('read_candidates'))
        read_candidate_rows = _human_readable_literature_rows(raw_read_candidate_rows) or recommendation_rows
        if raw_recommendation_rows:
            find_summary['strong_recommendations'] = len(recommendation_rows)
            find_summary['articles'] = len(recommendation_rows)
        if raw_read_candidate_rows or raw_recommendation_rows:
            current_recommendation_count = len(recommendation_rows) if raw_recommendation_rows else len(read_candidate_rows)
            find_summary['read_candidates'] = current_recommendation_count
            find_summary['read_candidates_raw'] = current_recommendation_count
        for key in ['recommendation_target_count']:
            value = recommendation_projection.get(key)
            if value not in (None, ''):
                find_summary[key] = value
            elif projection_counts.get(key) not in (None, ''):
                find_summary[key] = projection_counts.get(key)
        if raw_recommendation_rows:
            target = int(find_summary.get('recommendation_target_count') or 0)
            find_summary['strict_strong_anchor_count'] = len(recommendation_rows)
            find_summary['recommendation_shortfall'] = max(0, target - len(recommendation_rows)) if target else 0
        else:
            for key in ['recommendation_shortfall', 'strict_strong_anchor_count']:
                value = recommendation_projection.get(key)
                if value not in (None, ''):
                    find_summary[key] = value
                elif projection_counts.get(key) not in (None, ''):
                    find_summary[key] = projection_counts.get(key)
    recommendation_target_count = int(find_summary.get('recommendation_target_count') or 0)
    recommendation_shortfall = int(find_summary.get('recommendation_shortfall') or 0)
    verified_venue_rows = _current_verified_venue_metadata_rows(project, root, source_selection)
    venue_rows = _merge_verified_venue_metadata_rows(_venue_source_rows_from_health(find_results.get('venue_health_report')), verified_venue_rows)
    source_status = _merge_verified_venue_metadata_rows(_expand_source_status_rows(find_results.get('source_status'), find_results.get('venue_health_report')), verified_venue_rows)
    venue_metadata_counts = _venue_metadata_counts(venue_rows or verified_venue_rows)
    for key, value in venue_metadata_counts.items():
        if key in pipeline_stage_count_keys and find_summary.get(key) not in (None, '', 0):
            continue
        if value not in (None, '', 0) or key in {'venue_category_selected_papers', 'category_selected_papers'}:
            find_summary[key] = value
    pid = str(full_job.get('pid') or '') if isinstance(full_job, dict) else ''
    full_job_live = bool(
        isinstance(full_job, dict)
        and pid
        and full_job.get('process_alive') is True
        and _is_real_full_cycle_command(full_job.get('cmd') or full_job.get('command'), kind=full_job.get('kind'), stage=full_job.get('stage') or full_job.get('raw_stage'))
        and _pid_alive(pid)
    )
    reference_full_job_live = bool(reference_full_job.get('process_alive') is True and reference_full_job.get('pid') and _pid_alive(reference_full_job.get('pid')))
    raw_cycle_status = str(full_cycle_raw.get('status') or 'not_started')
    current_plan_status = str(current_plan.get('status') or '')
    current_find_ready_for_environment = bool(
        current_plan.get('claude_current_find_ready')
        or current_plan.get('read_idea_plan_ready')
        or current_plan.get('execution_ready')
        or current_plan_status in {
            'claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection',
            'claude_current_find_ready',
            'plan_selected',
            'ready',
        }
    )
    waiting_environment_base_selection = bool(current_find_ready_for_environment and not env.get('valid') and not (full_job_live or reference_full_job_live))
    status = 'running' if (full_job_live or reference_full_job_live) else ('stale_full_research_cycle_snapshot' if raw_cycle_status == 'running' else raw_cycle_status)
    if waiting_environment_base_selection:
        status = 'blocked_environment_base_selection_required'
    if not (full_job_live or reference_full_job_live) and env.get('valid') and not route_ready_datasets:
        status = 'blocked_fresh_base_data_required'
    elif not (full_job_live or reference_full_job_live) and env.get('valid') and route_loader_contract_passed and status in {'blocked_fresh_base_data_required', 'blocked_fresh_base_implementation_required', 'blocked_no_viable_base_switch_route'}:
        status = 'blocked_fresh_base_reference_probe_required'
    elapsed = str(full_job.get('elapsed') or full_job.get('elapsed_sec') or '') if isinstance(full_job, dict) else ''
    command = str(full_job.get('cmd') or '') if isinstance(full_job, dict) else ''
    full_job_kind = str(full_job.get('kind') or '') if isinstance(full_job, dict) else ''
    # active_child_worker can be Claude Code or run_literature_tool.py inside
    # read/idea/experiment stages; do not present it as a fresh Find unless the
    # full job itself is the TASTE frontend/driver.
    fresh_find_running = bool(
        status == 'running'
        and full_job_kind in {'frontend', 'driver'}
    )
    live_find_progress_payload: dict[str, Any] = {}
    live_run_id = ''
    if fresh_find_running:
        live_run_id = _latest_find_run_id_from_runs()
        if live_run_id:
            live_find_progress_payload = safe_dict(_read_json(RUNS_DIR / live_run_id / 'find_progress.json', {}))
        live_progress = safe_dict(live_find_progress_payload.get('live_progress'))
        live_counts = safe_dict(live_find_progress_payload.get('counts'))
        find_summary = {**find_summary, 'run_id': live_run_id or find_summary.get('run_id')}
        recommendation_target_count = 0
        recommendation_shortfall = 0
        read_count = 0
        idea_count = 0
        plan_count = 0
    else:
        live_progress = {}
        live_counts = {}
    current_route_ref_gate = ref_gate
    if not env.get('valid') and not recommendation_shortfall and not fresh_find_running:
        current_route_ref_gate = {
            'status': 'not_started',
            'decision': 'blocked_until_environment_base_selection',
            'human_summary': '环境阶段尚未为当前 selected_plan_id 选择基底；旧参考复现只保留为历史审计，不作为当前主线结果。',
            'summary': '环境阶段尚未为当前 selected_plan_id 选择基底；旧参考复现只保留为历史审计，不作为当前主线结果。',
        }
    health_check_source_status = _current_health_check_source_status_rows(project, root, source_selection)
    literature_survey = {
        **find_summary,
        'status': 'fresh_find_running' if fresh_find_running else 'recommendation_shortfall' if recommendation_shortfall else 'current_find_packet_ready' if find_results else 'missing_find_packet',
        'recommendation_target_count': recommendation_target_count,
        'recommendation_shortfall': recommendation_shortfall,
        'recommendation_gate_status': 'running' if fresh_find_running else 'shortfall' if recommendation_shortfall else 'pass' if recommendation_target_count else 'unknown',
        'selection': live_find_progress_payload.get('selection') if isinstance(live_find_progress_payload.get('selection'), dict) else find_results.get('selection') if isinstance(find_results.get('selection'), dict) else _current_project_source_selection(project, root),
        'source_status': (safe_list(live_find_progress_payload.get('source_status')) if fresh_find_running else source_status)[:20],
        'health_check_source_status': health_check_source_status[:20],
        'venue_sources': (safe_list(live_find_progress_payload.get('venue_health_report')) if fresh_find_running else venue_rows)[:20],
        'counts': {
            'raw_title_index_papers': live_counts.get('raw_title_index', live_counts.get('raw_title_index_papers', 0)) if fresh_find_running else find_summary.get('raw_title_index_papers', 0),
            'venue_total_papers_available': live_counts.get('raw_title_index', live_counts.get('venue_total_papers_available', 0)) if fresh_find_running else find_summary.get('venue_total_papers_available', find_summary.get('raw_title_index_papers', 0)),
            'venue_corpus_audited_papers': live_counts.get('raw_title_index', live_counts.get('venue_corpus_audited_papers', 0)) if fresh_find_running else find_summary.get('venue_corpus_audited_papers', find_summary.get('raw_title_index_papers', 0)),
            'title_candidates': live_counts.get('title_candidates', live_counts.get('venue_final_title_candidates', 0)) if fresh_find_running else find_summary.get('title_candidates', find_summary.get('venue_final_title_candidates', 0)),
            'venue_final_title_candidates': live_counts.get('venue_final_title_candidates', live_counts.get('title_candidates', 0)) if fresh_find_running else find_summary.get('venue_final_title_candidates', find_summary.get('title_candidates', 0)),
            'traceable_candidates': live_counts.get('title_candidates', live_counts.get('venue_final_title_candidates', 0)) if fresh_find_running else find_summary.get('title_candidates', find_summary.get('venue_final_title_candidates', 0)),
            'venue_category_selected_papers': live_counts.get('venue_category_selected_papers', 0) if fresh_find_running else find_summary.get('venue_category_selected_papers', find_summary.get('category_selected_papers', 0)),
            'category_selected_papers': live_counts.get('venue_category_selected_papers', 0) if fresh_find_running else find_summary.get('category_selected_papers', find_summary.get('venue_category_selected_papers', 0)),
            'category_filtered_papers': live_counts.get('category_filtered_papers', 0) if fresh_find_running else find_summary.get('category_filtered_papers', 0),
            'tfidf_screened_papers': live_counts.get('tfidf_screened_papers', 0) if fresh_find_running else find_summary.get('tfidf_screened_papers', 0),
            'venue_title_filter_input_papers': live_counts.get('venue_title_filter_input_papers', 0) if fresh_find_running else find_summary.get('venue_title_filter_input_papers', 0),
            'title_score_input_papers': live_counts.get('title_score_input_papers', 0) if fresh_find_running else find_summary.get('title_score_input_papers', 0),
            'llm_title_scored_papers': live_counts.get('llm_title_scored_papers', 0) if fresh_find_running else find_summary.get('llm_title_scored_papers', 0),
            'abstract_scored_papers': live_counts.get('abstract_scored_papers', live_counts.get('llm_scored_candidates', 0)) if fresh_find_running else find_summary.get('abstract_scored_papers', find_summary.get('llm_scored_candidates', 0)),
            'evaluated_candidates': live_counts.get('evaluated_candidates', 0) if fresh_find_running else find_summary.get('evaluated_candidates', 0),
            'llm_scored_candidates': live_counts.get('llm_scored_candidates', 0) if fresh_find_running else find_summary.get('llm_scored_candidates', find_summary.get('evaluated_candidates', 0)),
            'abstract_fetch_failed_candidates': live_counts.get('abstract_fetch_failed_candidates', 0) if fresh_find_running else find_summary.get('abstract_fetch_failed_candidates', 0),
            'final_llm_scoring_skipped_candidates': live_counts.get('final_llm_scoring_skipped_candidates', 0) if fresh_find_running else find_summary.get('final_llm_scoring_skipped_candidates', 0),
            'screened_ranking': 0 if fresh_find_running else find_summary.get('screened_ranking', 0),
            'strong_recommendations': 0 if fresh_find_running else find_summary.get('strong_recommendations', 0),
            'recommendation_target_count': recommendation_target_count,
            'recommendation_shortfall': recommendation_shortfall,
            'articles': 0 if fresh_find_running else find_summary.get('articles', 0),
            'read_candidates': 0 if fresh_find_running else find_summary.get('read_candidates', projection_counts.get('read_candidates', 0)),
            'read_candidates_raw': 0 if fresh_find_running else find_summary.get('read_candidates_raw', projection_counts.get('read_candidates_raw', 0)),
            'strict_strong_anchor_count': 0 if fresh_find_running else find_summary.get('strong_recommendations', 0),
            'readings': read_count,
            'ideas': idea_count,
            'plans': plan_count,
            'detail_fetched': live_counts.get('detail_fetched', live_counts.get('venue_detail_fetched_candidates', 0)) if fresh_find_running else find_summary.get('detail_fetched', find_summary.get('venue_detail_fetched_candidates', 0)),
            'venue_detail_fetched_candidates': live_counts.get('venue_detail_fetched_candidates', live_counts.get('detail_fetched', 0)) if fresh_find_running else find_summary.get('venue_detail_fetched_candidates', find_summary.get('detail_fetched', 0)),
            'llm_scoring_batches_current': live_progress.get('current', 0),
            'llm_scoring_batches_total': live_progress.get('total', 0),
            'llm_scoring_percent': live_progress.get('percent', 0),
        },
        'current_find_pipeline': {
            **pipeline_contract,
            'run_id': live_run_id or pipeline_contract.get('run_id') or find_summary.get('run_id'),
            'status': 'fresh_find_running' if fresh_find_running else pipeline_contract.get('status', ''),
            'readings': 0 if fresh_find_running else read_count,
            'reading_count': 0 if fresh_find_running else read_count,
            'read_count': 0 if fresh_find_running else read_count,
            'displayed_count': 0 if fresh_find_running else read_count,
            'read_artifact_count': 0 if fresh_find_running else raw_read_count,
            'full_text_reading_count': 0 if fresh_find_running else full_text_read_count,
            'pending_full_text_reading_count': 0 if fresh_find_running else pending_full_text_read_count,
            'ideas': 0 if fresh_find_running else idea_count,
            'idea_count': 0 if fresh_find_running else idea_count,
            'plans': 0 if fresh_find_running else plan_count,
            'plan_count': 0 if fresh_find_running else plan_count,
            'recommended_count': 0 if fresh_find_running else find_summary.get('strong_recommendations', 0),
            'strong_count': 0 if fresh_find_running else find_summary.get('strong_recommendations', 0),
            'strong_recommendations_count': 0 if fresh_find_running else find_summary.get('strong_recommendations', 0),
            'recommended_reading_count': 0 if fresh_find_running else find_summary.get('read_candidates', find_summary.get('strong_recommendations', 0)),
            'live_progress': live_progress,
        },
        'note_i18n': {'zh': '新的 Find 正在运行；旧推荐统计暂不作为当前结果展示，等待本轮产物落盘。'} if fresh_find_running else {},
    }
    if recommendation_projection and not fresh_find_running:
        if recommendation_projection.get('coverage_explanation_i18n'):
            literature_survey['coverage_explanation_i18n'] = recommendation_projection.get('coverage_explanation_i18n')
        if recommendation_projection.get('recommendation_quality'):
            literature_survey['recommendation_quality'] = recommendation_projection.get('recommendation_quality')
        literature_survey['strong_recommendations_count'] = find_summary.get('strong_recommendations', 0)
        literature_survey['read_candidates_count'] = find_summary.get('read_candidates') or find_summary.get('strong_recommendations', 0)
        literature_survey['strict_strong_anchor_count'] = find_summary.get('strong_recommendations', 0)

    if status == 'running' and pid and full_job_live:
        public_phase = _public_phase_for_full_cycle(full_job.get('stage') or full_cycle_raw.get('latest_step', {}).get('stage') if isinstance(full_cycle_raw.get('latest_step'), dict) else full_job.get('stage'), project, root)
        summary = f'完整科研自循环正在运行；阶段={public_phase}；PID={pid}' + (f'；运行时长={elapsed}' if elapsed else '')
    elif reference_full_job_live:
        ref_pid = str(reference_full_job.get('pid') or '')
        summary = f'参考复现正在运行；阶段=experiment；PID={ref_pid}；当前基底：{base_title}'
    elif status == 'blocked_after_max_cycles':
        summary = f'完整科研自循环已停止在最大轮次后；没有正在运行的 full-cycle。当前基底：{base_title}。'
    elif isinstance(full_job, dict) and str(full_job.get('status') or '').lower() == 'stale':
        summary = f'完整科研自循环进程已停止；没有正在运行的 full-cycle。当前基底：{base_title}。'
    elif status == 'blocked_fresh_base_data_required':
        summary = f'环境阶段已选择当前候选基底：{base_title}；但真实数据/loader 尚未通过，不能进入实验或论文证据。'
    elif status == 'blocked_fresh_base_reference_probe_required':
        protocol_blocker_summary = _reference_protocol_probe_blocker_summary(protocol_probe, base_title)
        summary = f'环境阶段已选择当前候选基底：{base_title}；' + (protocol_blocker_summary or '真实数据/loader 已通过，等待参考协议/环境 manifest 探针。')
    elif waiting_environment_base_selection:
        summary = '当前 Find/Read/Idea/Plan 已准备；等待环境阶段基于当前 selected_plan_id 选择基底、验证 repo/data/protocol，旧环境和旧参考复现不作为当前结果。'
    else:
        summary = str(full_cycle_raw.get('summary_zh') or full_cycle_raw.get('summary') or f'项目：{topic}；状态：{status}。当前基底：{base_title}。')
    summary = _public_run_summary_without_action_plan(summary)
    if fresh_find_running:
        lp = safe_dict(literature_survey.get('current_find_pipeline')).get('live_progress') or {}
        phase_text = str(lp.get('phase') or 'Find')
        percent_text = str(lp.get('percent') or 0)
        summary += f'；新的 Find 正在运行：{phase_text} {percent_text}%，旧推荐统计仅作历史参考。'
    elif recommendation_shortfall:
        summary += f'；文献推荐门控未过：{find_summary.get("strong_recommendations", 0)}/{recommendation_target_count}，短缺 {recommendation_shortfall}，禁止论文或结论提升。'

    blocker_category = str(ref_gate.get('decision') or ref_gate.get('status') or '')
    blocker_summary = str(ref_gate.get('human_summary') or ref_gate.get('decision_reason') or full_cycle_raw.get('current_goal') or '')
    next_action = str((supervision.get('next_action') if isinstance(supervision, dict) else '') or full_cycle_raw.get('current_goal') or '继续按当前项目状态推进下一步门控。')
    blocker_title = '当前项目门控状态' if env.get('valid') else '等待环境阶段 Claude Code 选择当前基底'
    submission_blockers = _current_submission_blockers(root)
    base_switch_gate_required = bool(selected_base_viability.get('status') == 'blocked' and selected_base_viability.get('decision') == 'base_switch_gate_required')
    base_switch_authorized = bool(
        base_switch_gate.get('status') == 'pass'
        and base_switch_gate.get('decision') == 'authorize_base_switch'
        and base_switch_gate.get('switch_authorized') is True
    )
    base_switch_gate_unresolved = bool(base_switch_gate_required and not base_switch_authorized)
    selected_base_viability_blocked = bool(selected_base_viability.get('status') == 'blocked' and selected_base_viability.get('decision') in {'base_switch_gate_required', 'continue_experiment_evidence_repair'})
    selected_base_viability_public = _selected_base_viability_public_blocker(selected_base_viability, base_title, base_switch_gate)
    selected_base_blocker_category = selected_base_viability_public.get('category', 'experiment_evidence_audit') if selected_base_viability_blocked else 'experiment_evidence_audit'
    if base_switch_gate_unresolved or (selected_base_viability_blocked and status != 'blocked_environment_base_selection_required'):
        summary = selected_base_viability_public.get('project_summary') or summary
    if fresh_find_running:
        blocker_category = 'fresh_find_running'
        blocker_title = 'Find 正在运行'
        blocker_summary = '新的 Find/文献调研正在运行；本轮完成前，旧推荐统计只作为历史参考，不作为当前结论。'
        next_action = '新的 Find/文献调研正在运行；等待本轮推荐文章、精读、想法和计划产物落盘。'
    elif recommendation_shortfall:
        blocker_category = 'literature_recommendation_shortfall'
        blocker_title = 'Find 推荐门控阻塞'
        blocker_summary = f'当前 Find 推荐文章 {find_summary.get("strong_recommendations", 0)}/{recommendation_target_count}，短缺 {recommendation_shortfall}；需要 TASTE 补检索/补评分或目标化调研，不能把弱论文补成推荐，也不能推进论文或结论提升。'
        next_action = f'当前 Find 推荐文章 {find_summary.get("strong_recommendations", 0)}/{recommendation_target_count}，短缺 {recommendation_shortfall}；通过 TASTE 统一 literature tool 补检索/补评分并刷新 packet。短缺未清零前，不启动实验、论文或结论提升。'
    elif base_switch_gate_unresolved:
        blocker_category = selected_base_blocker_category
        blocker_title = selected_base_viability_public.get('title') or '缺少审计就绪候选实验证据'
        blocker_summary = selected_base_viability_public.get('summary') or '参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果；完成前不会更换当前基底或提升论文结论。'
        next_action = selected_base_viability_public.get('next_action') or '等待项目代理读取当前缺口证据，并给出下一轮实验或修复动作。'
    elif selected_base_viability_blocked:
        blocker_category = selected_base_blocker_category
        blocker_title = selected_base_viability_public.get('title') or '缺少当前主线候选实验证据'
        blocker_summary = selected_base_viability_public.get('summary') or '参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果；论文预览可以生成，但不能被标记为投稿通过。'
        next_action = selected_base_viability_public.get('next_action') or '等待项目代理读取候选实验证据缺口，并给出下一步实验动作。'
    elif reference_full_job_live:
        blocker_category = 'fresh_base_reference_reproduction_running'
        blocker_title = '参考复现正在运行'
        blocker_summary = 'TASTE 正在跑当前参考工作的论文级 full reference reproduction；完成并刷新门控前，不启动候选实验、论文写作或结论提升。'
        next_action = '等待 full reference reproduction 完成；随后自动刷新参考复现、科学进展、论文证据、投稿准备度和阻塞行动计划门控。'
    elif status == 'blocked_fresh_base_reference_probe_required':
        blocker_category = 'fresh_base_reference_probe_required'
        protocol_blocker_summary = _reference_protocol_probe_blocker_summary(protocol_probe, base_title)
        blocker_title = '参考协议依赖缺失' if protocol_blocker_summary else '等待参考协议/环境 manifest 探针'
        blocker_summary = protocol_blocker_summary or f'{base_title} 数据和 loader/import probe 已通过；当前等待参考协议/环境 manifest 探针。'
        next_action = '使用当前配置的实验环境补齐缺失依赖后重新运行 reference-protocol/import probe；通过前不训练、不写论文、不提升结论。' if protocol_blocker_summary else '记录当前基底最小环境 manifest，并对 ready 数据集运行有界只读 reference-protocol/import probe；通过前不训练、不写论文、不提升结论。'
    elif submission_blockers:
        top_submission_blocker = submission_blockers[0]
        blocker_category = str(top_submission_blocker.get('category') or 'submission_readiness')
        blocker_title = '论文证据/投稿门控阻塞'
        blocker_summary = str(top_submission_blocker.get('issue') or 'submission_readiness blocked')
        next_action = '参考复现和实验门控通过后，继续补齐结论台账、坏例/反例和可靠性证据；在投稿准备度通过前保持只写草稿，禁止论文定稿或结论提升。'
    if waiting_environment_base_selection:
        blocker_category = 'environment_anchor_selection_required'
        blocker_title = '等待环境阶段项目代理选择当前基底'
        blocker_summary = '当前 Find/Read/Idea/Plan 已准备；必须由环境阶段项目代理基于当前 run 选择基底，不能使用旧 active_repo。'
        next_action = '环境阶段需要项目代理审计当前推荐文章、Read/Idea/Plan、repo/data/protocol 后选择当前基底。'
    elif not env.get('valid'):
        blocker_category = blocker_category or 'environment_anchor_selection_required'
        blocker_title = '等待环境阶段项目代理选择当前基底'
        blocker_summary = blocker_summary or '当前 Find/Read/Idea/Plan 已准备；必须由环境阶段项目代理基于当前 run 选择基底，不能使用旧 active_repo。'
        next_action = '环境阶段需要项目代理审计当前推荐文章、Read/Idea/Plan、repo/data/protocol 后选择当前基底。'

    main_route = {
        'base_title': base_title,
        'base_venue': selected.get('venue') or selected.get('selected_base_venue') or active_repo.get('selected_base_venue') or '',
        'base_year': selected.get('year') or selected.get('selected_base_year') or active_repo.get('selected_base_year') or '',
        'repo_name': repo_name,
        'repo_url': repo_url,
        'repo_path': repo_path,
        'dataset': route_dataset,
        'ready_datasets': route_ready_datasets[:8],
        'find_run_id': (live_run_id if fresh_find_running else env.get('current_find_run_id')) or find_summary.get('run_id') or '',
        'base_selection_status': 'waiting_for_current_find_results' if fresh_find_running else 'selected' if env.get('valid') else 'waiting_for_environment_claude_code',
        'selection_stage': _public_internal_names(env.get('selection_stage', '')),
        'selection_gate': _public_internal_names(env.get('selection_gate', '')),
        'readings': read_count,
        'read_artifacts': raw_read_count,
        'raw_reading_count': raw_read_count,
        'full_text_reading_count': full_text_read_count,
        'pending_full_text_reading_count': pending_full_text_read_count,
        'ideas': idea_count,
        'plans': plan_count,
    }
    public_full_job = _public_full_cycle_job(full_job, target_venue=venue)
    full_cycle_compact = {
        'status': status,
        'summary': summary,
        'summary_zh': summary,
        'summary_en': _public_status_summary_en(status, base_title=base_title, fresh_find_running=fresh_find_running, recommendation_shortfall=recommendation_shortfall, reference_full_job_live=reference_full_job_live),
        'current_goal': _public_text_for_gate(next_action if selected_base_viability_blocked else full_cycle_raw.get('current_goal') or next_action),
        'updated_at': str(full_cycle_raw.get('updated_at') or ''),
        'started_at': str(full_cycle_raw.get('started_at') or ''),
        'current_cycle': full_cycle_raw.get('current_cycle') or len(safe_list(full_cycle_raw.get('cycles'))),
        'max_cycles': full_cycle_raw.get('max_cycles') or '',
        'latest_step': full_cycle_raw.get('latest_step', ''),
        'fresh_base_implementation_plan': scalar(fresh_impl, ['status', 'reason']),
        'full_cycle_job': public_full_job,
        'reference_full_job': scalar(reference_full_job, ['status', 'decision', 'pid', 'process_alive', 'log_path']),
    }
    if status == 'running':
        full_cycle_compact.pop('finished_at', None)
        full_cycle_compact.pop('completed_at', None)

    display_ref_gate = _reference_gate_for_current_route_display(current_route_ref_gate, status, route_ready_datasets, protocol_probe, base_title or "当前基底")
    blocker = {
        'category': _public_internal_names(blocker_category),
        'title': blocker_title,
        'summary': _public_internal_names(blocker_summary),
        'next_action': _public_internal_names(next_action),
        'reference_gate_status': display_ref_gate.get('status', ''),
        'reference_gate_decision': display_ref_gate.get('decision', ''),
        'paper_level_reproduction_passed': bool(ref_gate.get('status') == 'pass' and ref_gate.get('decision') == 'continue_base'),
        'ready_datasets': route_ready_datasets[:8],
        'blocked_datasets': active_repo.get('blocked_datasets') or selected.get('blocked_datasets') or [],
        'reference_full_job_status': str((supervision.get('full_reference_job') or {}).get('status') or ''),
        'reference_full_job_pid': str((supervision.get('full_reference_job') or {}).get('pid') or ''),
        'reference_full_job_log': str((supervision.get('full_reference_job') or {}).get('log_path') or ''),
        'source': 'deterministic_gate_audit',
        'source_label': '来源：确定性门控审计',
        'semantic_data_provenance': selected_base_viability_public.get('semantic_data_provenance', {}),
    }
    stale_full_job = isinstance(full_job, dict) and str(full_job.get('status') or '').lower() == 'stale' and not full_job_live
    stale_next_action = next_action if recommendation_shortfall else '上一条完整科研循环已结束，当前未检测到活进程；可以通过统一 TASTE 入口继续下一轮完整科研流程。'
    supervision_status = str(supervision.get('status', '') if isinstance(supervision, dict) else '')
    if stale_full_job and 'running' in supervision_status:
        supervision_status = 'stale_full_research_cycle_snapshot'
    supervision_next_action = _clean_stale_active_worker_text(supervision.get('next_action', '') if isinstance(supervision, dict) else '', stale_next_action) if stale_full_job else next_action
    if stale_full_job and ('PID=' in next_action or '正在运行' in next_action or 'worker' in next_action):
        next_action = stale_next_action
        blocker['next_action'] = next_action
    human = {
        'status': status,
        'target_venue': venue,
        'summary': summary,
        'summary_source': 'deterministic_gate_audit',
        'source_label': '来源：确定性门控审计',
        'summary_i18n': {'zh': summary, 'en': _public_status_summary_en(status, base_title=base_title, fresh_find_running=fresh_find_running, recommendation_shortfall=recommendation_shortfall, reference_full_job_live=reference_full_job_live)},
        'main_route': main_route,
        'blocker': blocker,
        'legacy_control': {'policy': '历史仓库、实验和参考复现只保留为审计记录；当前主线以本轮 Find 后的环境审查选择为准。', 'details_hidden': True},
        'display_policy': {'hide_legacy_repo_cards': True, 'hide_literature_candidate_pool': False, 'hide_legacy_route_metrics_on_main': True},
        'supervision': {**_empty_supervision_payload(), 'status': supervision_status, 'action': _public_internal_names(supervision.get('action', '') if isinstance(supervision, dict) else ''), 'generated_at': supervision.get('generated_at', '') if isinstance(supervision, dict) else '', 'next_action': _public_internal_names(supervision_next_action), 'full_cycle_job': public_full_job, 'environment_base_selection': _public_environment_selection_summary(env)},
    }

    paper_raw = safe_dict(_active_paper_state(root, project, cfg, venue=venue))
    pdf_candidates = [root / 'paper' / 'output' / venue.lower() / 'paper.pdf', root / 'paper' / 'orchestra' / venue.lower() / 'workspace' / 'paper.pdf']
    pdf_path = next((path for path in pdf_candidates if path.exists()), None)
    preview_blockers = paper_raw.get('conference_preview_blockers', []) if isinstance(paper_raw.get('conference_preview_blockers', []), list) else []
    raw_layout_warnings = paper_raw.get('paper_layout_footprint_warnings', []) if isinstance(paper_raw.get('paper_layout_footprint_warnings', []), list) else []
    layout_warnings = [item for item in (_paper_public_layout_warning_text(value) for value in raw_layout_warnings) if item]
    preview_blocker_text = ''
    if preview_blockers:
        first = preview_blockers[0]
        preview_blocker_text = _paper_public_blocker_text(first.get('public_detail') or first.get('detail') or first.get('id') or '') if isinstance(first, dict) else _paper_public_blocker_text(first)
    layout_text = str(layout_warnings[0]) if layout_warnings else ''
    submission_state = safe_dict(_read_json(root / 'state' / 'submission_readiness.json', {}))
    paper_blocked = bool(fresh_find_running or recommendation_shortfall or not (submission_state.get('submission_ready') and submission_state.get('status') == 'submission_ready'))
    if paper_blocked:
        if pdf_path and preview_blocker_text:
            paper_summary = f'已有当前稿件预览，但预览门控未通过：{preview_blocker_text}。正式投稿状态仍由科研证据和投稿审计决定。'
            if layout_text:
                paper_summary += ' 图表审计已提示版面压力，应先处理图表占地和单栏适配。'
        else:
            paper_summary = blocker_summary or '当前文献/投稿门控未过；旧 PDF 只能作为历史产物审计，不能作为当前投稿稿或结论提升证据。'
        paper_stage = {
            'status': 'fresh_find_running' if fresh_find_running else 'blocked_by_literature_gate' if recommendation_shortfall else 'blocked',
            'summary': paper_summary,
            'summary_zh': paper_summary,
            'venue': venue,
            'target_venue': venue,
            'venue_slug': _venue_slug(venue),
            'paper_generation_skipped': False,
            'paper_generation_skipped_reason': '投稿/证据门控未通过；当前 PDF 作为目标 venue 稿件预览展示，不标记为投稿通过' if pdf_path else 'no current preview PDF exists',
            'paper_normality_status': paper_raw.get('paper_normality_status', ''),
            'paper_venue_format_status': paper_raw.get('paper_venue_format_status', ''),
            'paper_figure_quality_status': paper_raw.get('paper_figure_quality_status', ''),
            'paper_citation_render_status': paper_raw.get('paper_citation_render_status', ''),
            'paper_citation_render_ready': bool(paper_raw.get('paper_citation_render_ready') or paper_raw.get('paper_citation_render_status') == 'pass'),
            'paper_citation_render_blockers': [],
            'paper_self_review_status': paper_raw.get('paper_self_review_status', ''),
            'paper_self_review_ready': bool(paper_raw.get('paper_self_review_ready')),
            'paper_self_review_receipt': paper_raw.get('paper_self_review_receipt', ''),
            'paper_self_review_blockers': [],
            'paper_self_review_evidence_blockers': [],
            'paper_self_review_evidence_blocker_count': int(paper_raw.get('paper_self_review_evidence_blocker_count') or len(paper_raw.get('paper_self_review_evidence_blockers', []) if isinstance(paper_raw.get('paper_self_review_evidence_blockers', []), list) else []) or 0),
            'paper_self_review_preview_only_ready': bool(paper_raw.get('paper_self_review_preview_only_ready')),
            'paper_self_review_submission_evidence_ready': bool(paper_raw.get('paper_self_review_submission_evidence_ready')),
            'paper_self_review_independent_findings_count': paper_raw.get('paper_self_review_independent_findings_count', 0),
            'paper_self_review_repairs_count': paper_raw.get('paper_self_review_repairs_count', 0),
            'paper_normality_citation_count': paper_raw.get('paper_normality_citation_count', ''),
            'normal_preview_ready': bool(paper_raw.get('normal_preview_ready') or paper_raw.get('paper_normality_ready')),
            'paper_preview_repair_loop_status': 'blocked' if not paper_raw.get('conference_preview_ready', False) else paper_raw.get('paper_preview_repair_loop_status', ''),
            'paper_preview_repair_rounds': paper_raw.get('paper_preview_repair_rounds', ''),
            'conference_preview_ready': paper_raw.get('conference_preview_ready', False),
            'conference_preview_pages': paper_raw.get('conference_preview_pages', ''),
            'conference_preview_body_pages': paper_raw.get('conference_preview_body_pages', ''),
            'conference_preview_reference_pages': paper_raw.get('conference_preview_reference_pages', ''),
            'conference_preview_blockers': [],
            'conference_preview_blocker_summary': preview_blocker_text,
            'paper_layout_footprint_warnings': layout_warnings[:10],
            'paper_layout_summary': layout_text,
            'venue_submission_policy': paper_raw.get('venue_submission_policy', {}),
            'template_fetched': _paper_template_fetched(paper_raw),
            'venue_requirements_status': paper_raw.get('venue_requirements_status', ''),
            'venue_requirements_path': paper_raw.get('venue_requirements_path', ''),
            'pdf_path': str(pdf_path) if pdf_path else '',
            'pdf_url': _project_file_url(root, pdf_path) if pdf_path else '',
            'raw_pdf_path': str(pdf_path) if pdf_path else '',
            'raw_pdf_url': _project_file_url(root, pdf_path) if pdf_path else '',
        }
    else:
        paper_stage = {
            'status': paper_raw.get('status') or paper_raw.get('paper_stage_status') or ('preview_available' if pdf_path else 'not_started'),
            'summary': paper_raw.get('summary') or paper_raw.get('summary_zh') or ('已有 PDF 产物，仅当门控通过时才可作为投稿稿。' if pdf_path else '还没有当前门控认可的论文 PDF。'),
            'summary_zh': paper_raw.get('summary_zh') or paper_raw.get('summary') or '',
            'venue': venue,
            'target_venue': venue,
            'venue_slug': _venue_slug(venue),
            'paper_generation_skipped': paper_raw.get('paper_generation_skipped', False),
            'paper_generation_skipped_reason': paper_raw.get('paper_generation_skipped_reason', ''),
            'paper_normality_status': paper_raw.get('paper_normality_status', ''),
            'paper_venue_format_status': paper_raw.get('paper_venue_format_status', ''),
            'paper_figure_quality_status': paper_raw.get('paper_figure_quality_status', ''),
            'paper_normality_citation_count': paper_raw.get('paper_normality_citation_count', ''),
            'paper_preview_repair_loop_status': 'blocked' if not paper_raw.get('conference_preview_ready', False) else paper_raw.get('paper_preview_repair_loop_status', ''),
            'paper_preview_repair_rounds': paper_raw.get('paper_preview_repair_rounds', ''),
            'conference_preview_ready': paper_raw.get('conference_preview_ready', False),
            'conference_preview_pages': paper_raw.get('conference_preview_pages', ''),
            'conference_preview_body_pages': paper_raw.get('conference_preview_body_pages', ''),
            'conference_preview_reference_pages': paper_raw.get('conference_preview_reference_pages', ''),
            'conference_preview_blockers': [],
            'conference_preview_blocker_summary': preview_blocker_text,
            'paper_layout_footprint_warnings': layout_warnings[:10],
            'paper_layout_summary': layout_text,
            'venue_submission_policy': paper_raw.get('venue_submission_policy', {}),
            'template_fetched': _paper_template_fetched(paper_raw),
            'venue_requirements_status': paper_raw.get('venue_requirements_status', ''),
            'venue_requirements_path': paper_raw.get('venue_requirements_path', ''),
            'pdf_path': str(pdf_path) if pdf_path else '',
            'pdf_url': _project_file_url(root, pdf_path) if pdf_path else '',
            'raw_pdf_path': str(pdf_path) if pdf_path else '',
            'raw_pdf_url': _project_file_url(root, pdf_path) if pdf_path else '',
        }

    artifacts = [item for item in [
        artifact('find_results.json', root / 'planning' / 'finding' / 'find_results.json'),
        artifact('article.md', root / 'planning' / 'finding' / 'article.md', 'markdown'),
        artifact('read_results.json', root / 'planning' / 'finding' / 'read_results.json'),
        artifact('ideas.json', root / 'planning' / 'finding' / 'ideas.json'),
        artifact('plans.json', root / 'planning' / 'finding' / 'plans.json'),
        artifact('current_find_research_plan.json', root / 'state' / 'current_find_research_plan.json'),
        artifact('evidence_ready_repo_selection.json', root / 'state' / 'evidence_ready_repo_selection.json'),
        artifact('fresh_base_implementation_plan.json', root / 'state' / 'fresh_base_implementation_plan.json'),
        artifact('reference_reproduction_gate.json', root / 'state' / 'reference_reproduction_gate.json'),
        artifact('full_research_cycle.json', root / 'state' / 'full_research_cycle.json'),
        artifact('experiment_records.csv', root / 'experiments' / 'experiment_records.csv', 'text'),
        artifact('experiment_record_table.json', root / 'state' / 'experiment_record_table.json'),
        artifact('supervision_tick.json', root / 'state' / 'supervision_tick.json'),
        artifact('paper.pdf', pdf_path, 'pdf') if pdf_path else None,
    ] if item]

    environment_stage = _public_environment_stage(status='waiting_for_current_find_results' if fresh_find_running else 'selected' if env.get('valid') else 'waiting_for_environment_base_selection', env=env, selected=selected, active_repo=active_repo, repo_name=repo_name, repo_url=repo_url, repo_path=repo_path, ref_gate=ref_gate, reference_full_job=reference_full_job, route_dataset=route_dataset, route_ready_datasets=route_ready_datasets, protocol_probe=protocol_probe)
    scientific_progress_gate_light = _read_json(root / 'state' / 'scientific_progress_gate.json', {})
    experiment_iteration_audit_light = _read_json(root / 'state' / 'experiment_iteration_audit.json', {})
    experiment_module_summary = _public_experiment_module_summary(
        status=status,
        reference_gate=ref_gate,
        scientific_progress_gate=scientific_progress_gate_light,
        experiment_iteration_audit=experiment_iteration_audit_light,
        experiment_rows=current_route_experiments_all,
        record_rows=current_route_record_rows,
        completed_count=completed_count,
        total_count=experiment_count,
        active_training=False,
        reference_job_live=reference_full_job_live,
        fresh_find_running=fresh_find_running,
        literature_gate_blocked=False,
        recommendation_shortfall=recommendation_shortfall,
        next_action=next_action,
    )
    stages = {
        'environment': environment_stage,
        'experiment': {'status': status, **experiment_module_summary, 'experiment_count': experiment_count, 'completed_experiment_count': completed_count, 'experiment_count_label': '实验/复现记录', 'experiment_count_help': '这是当前主线下实验与参考复现记录的审计统计，不是完整科研流程完成进度；论文结论仍以科学进展、数据/loader、参考复现和投稿审计为准。', **experiment_display_flags, 'recent_experiments': _public_experiment_rows(current_route_experiments_all, 12), 'experiments': _public_experiment_rows(current_route_experiments_all, 12), 'experiment_record': experiment_record_compact, 'legacy_experiment_audit': {'experiment_count': legacy_experiment_count, 'completed_experiment_count': legacy_completed_count, 'csv_path': experiment_record_compact.get('csv_path', ''), 'note': '旧实验记录保留为 CSV/registry 审计；当前摘要只统计当前主线记录。'} if legacy_experiment_count > experiment_count else {}, 'reference_reproduction_gate': scalar(display_ref_gate, ['status', 'decision', 'decision_reason', 'human_summary']), 'scientific_progress_gate': _public_gate_status_summary(scientific_progress_gate_light), 'experiment_iteration_audit': _public_gate_status_summary(experiment_iteration_audit_light), 'full_research_cycle': full_cycle_compact},
        'paper': paper_stage,
    }

    claude_status = _claude_status_payload(root)
    result = {
        'project': project,
        'topic': topic,
        'summary': summary,
        'status': status,
        'config': _public_project_identity_config(project, cfg, topic),
        'run_preferences': _public_run_preferences(project, root, cfg, selection=_current_project_source_selection(project, root)),
        'path': str(root),
        'full_research_cycle': full_cycle_compact,
        'human_supervision': human,
        'current_blocker': blocker,
        'blocker': blocker,
        'main_route': main_route,
        'stages': stages,
        'state': {'full_research_cycle': full_cycle_compact, 'literature_survey': literature_survey, 'experiment_count': experiment_count, 'completed_experiment_count': completed_count, 'experiment_count_label': '实验/复现记录', 'experiment_count_help': '这是当前主线下实验与参考复现记录的审计统计，不是完整科研流程完成进度；论文结论仍以科学进展、证据和投稿审计为准。', **experiment_display_flags, 'recent_experiments': _public_experiment_rows(current_route_experiments_all, 8), 'legacy_experiment_audit': {'experiment_count': legacy_experiment_count, 'completed_experiment_count': legacy_completed_count, 'csv_path': experiment_record_compact.get('csv_path', ''), 'note': '旧实验记录保留为 CSV/registry 审计；当前摘要只统计当前主线记录。'} if legacy_experiment_count > experiment_count else {}, 'experiment_record': {**experiment_record_compact, 'rows': []}, 'paper_pdf_count': 1 if (pdf_path and not paper_blocked) else 0, 'human_supervision': human, 'submission_readiness': scalar(submission_state, ['status', 'submission_ready', 'promotion_gate']), 'claude_status': claude_status},
        'literature_survey': literature_survey,
        'current_find_pipeline': literature_survey['current_find_pipeline'],
        'readings': read_count,
        'read_artifacts': raw_read_count,
        'full_text_reading_count': full_text_read_count,
        'pending_full_text_reading_count': pending_full_text_read_count,
        'ideas': idea_count,
        'plans': plan_count,
        'fresh_base': {'title': base_title, 'venue': main_route.get('base_venue', ''), 'year': main_route.get('base_year', ''), 'repo_name': repo_name, 'repo_path': repo_path, 'dataset': route_dataset, 'ready_datasets': route_ready_datasets[:8]},
        'supervision': human['supervision'],
        'claude_status': claude_status,
        'artifacts': artifacts,
        'payload_bytes': 0,
    }
    result_venue = result.get('run_preferences', {}).get('target_venue') if isinstance(result.get('run_preferences'), dict) else ''
    result_venue = result_venue or (result.get('run_preferences', {}).get('venue') if isinstance(result.get('run_preferences'), dict) else '') or venue
    if result_venue:
        result['target_venue'] = result_venue
        result['venue'] = result_venue
    result['payload_bytes'] = _json_size(result)
    return result


def project_summary(project: str, *, compact: bool = True) -> dict[str, Any]:
    project = _safe_project(project)
    cache_key = (project, bool(compact))
    if compact:
        cached = _PROJECT_SUMMARY_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and now < cached[0]:
            return json.loads(json.dumps(cached[1], ensure_ascii=False, default=str))
    root = PROJECTS / project
    cfg = _read_json(root / "project.json", {})
    if isinstance(cfg, dict) and isinstance(cfg.get("llm"), dict):
        cfg = dict(cfg)
        llm_cfg = dict(cfg.get("llm") or {})
        if llm_cfg.get("api_key"):
            llm_cfg["api_key"] = "***"
        cfg["llm"] = llm_cfg
    if compact:
        try:
            compact_summary = _fast_project_summary(project, root, cfg if isinstance(cfg, dict) else {})
            _PROJECT_SUMMARY_CACHE[cache_key] = (time.monotonic() + COMPACT_PROJECT_SUMMARY_TTL_SEC, compact_summary)
            return json.loads(json.dumps(compact_summary, ensure_ascii=False, default=str))
        except Exception as exc:
            return {"project": project, "status": "render_error", "summary": f"compact summary failed: {exc}", "error": str(exc), "path": str(root), "artifacts": []}
    report_candidates = [
        ("status.md", root / "reports" / "status.md", "markdown"),
        ("data_availability_claude_audit.md", root / "reports" / "data_availability_claude_audit.md", "markdown"),
        ("blocker_resolution_packet.md", root / "reports" / "blocker_resolution_packet.md", "markdown"),
        ("工作状态.txt", ROOT / "工作状态.txt", "text"),
        ("init_brief.md", root / "obsidian" / "planning" / "init_brief.md", "markdown"),
        ("research_plan.md", root / "obsidian" / "planning" / "research_plan.md", "markdown"),
        ("next_actions.md", root / "obsidian" / "planning" / "next_actions.md", "markdown"),
        ("finding_frontend.md", root / "obsidian" / "planning" / "finding_frontend.md", "markdown"),
        ("taste_sync.md", root / "reports" / "taste_sync.md", "markdown"),
        ("finding_frontend.json", root / "state" / "finding_frontend.json", "json"),
        ("taste_sync.json", root / "state" / "taste_sync.json", "json"),
        ("taste_literature_intermediates.json", root / "state" / "taste_literature_intermediates.json", "json"),
        ("find_results.json", root / "planning" / "finding" / "find_results.json", "json"),
        ("article.md", root / "planning" / "finding" / "article.md", "markdown"),
        ("read_results.json", root / "planning" / "finding" / "read_results.json", "json"),
        ("ideas.json", root / "planning" / "finding" / "ideas.json", "json"),
        ("plans.json", root / "planning" / "finding" / "plans.json", "json"),
        ("paper_quality.md", root / "obsidian" / "planning" / "paper_quality.md", "markdown"),
        ("experiment_log.md", root / "experiments" / "experiment_log.md", "markdown"),
        ("experiment_records.csv", root / "experiments" / "experiment_records.csv", "text"),
        ("实验记录.md", root / "experiments" / "实验记录.md", "markdown"),
        ("benchmark_matrix.md", root / "benchmarks" / "benchmark_matrix.md", "markdown"),
        ("paper_draft.md", root / "paper" / "drafts" / "paper_draft.md", "markdown"),
        ("paper_revision.md", root / "paper" / "drafts" / "paper_revision.md", "markdown"),
        ("aggregated_review.md", root / "paper" / "reviews" / "aggregated_review.md", "markdown"),
        ("full_research_cycle.md", root / "reports" / "full_research_cycle.md", "markdown"),
        ("full_research_cycle.json", root / "state" / "full_research_cycle.json", "json"),
        ("fresh_research_base.json", root / "state" / "fresh_research_base.json", "json"),
        ("fresh_research_base.md", root / "reports" / "fresh_research_base.md", "markdown"),
        ("paper_pipeline.json", root / "paper" / "metadata" / "paper_pipeline.json", "json"),
        ("machine_profile.json", root / "reports" / "machine_profile.json", "json"),
        ("parallel_plan.json", root / "state" / "parallel_plan.json", "json"),
        ("idea_candidates.json", root / "state" / "idea_candidates.json", "json"),
        ("repo_candidates.json", root / "state" / "repo_candidates.json", "json"),
        ("dataset_registry.json", root / "state" / "dataset_registry.json", "json"),
        ("experiment_registry.json", root / "state" / "experiment_registry.json", "json"),
        ("experiment_record_table.json", root / "state" / "experiment_record_table.json", "json"),
    ]
    artifacts: list[dict[str, Any]] = []
    if not compact:
        for name, path, kind in report_candidates:
            if not path.exists():
                continue
            content: Any = _read_json(path, {}) if kind == "json" else _read_text(path)
            artifacts.append({"name": name, "kind": kind, "path": str(path), "content": content})
    stages = _stage_status(root, cfg if isinstance(cfg, dict) else {})
    public_projection: dict[str, Any] = {}
    try:
        public_projection = _fast_project_summary(project, root, cfg if isinstance(cfg, dict) else {})
    except Exception:
        public_projection = {}
    public_human_gate_summary = public_projection.get("human_gate_summary") if isinstance(public_projection.get("human_gate_summary"), dict) else {}
    public_human_supervision = public_projection.get("human_supervision") if isinstance(public_projection.get("human_supervision"), dict) else {}
    public_main_route = public_projection.get("main_route") if isinstance(public_projection.get("main_route"), dict) else {}
    public_supervision = public_projection.get("supervision") if isinstance(public_projection.get("supervision"), dict) else {}
    public_stages = public_projection.get("stages") if isinstance(public_projection.get("stages"), dict) else {}
    public_config = public_projection.get("config") if isinstance(public_projection.get("config"), dict) else {}
    public_run_preferences = public_projection.get("run_preferences") if isinstance(public_projection.get("run_preferences"), dict) else {}
    # Non-compact responses still include raw artifact contents below, but the
    # public top-level project state must match compact polling so UI panels do
    # not treat historical gate dumps or legacy routes as current TASTE status.
    if public_stages:
        stages = public_stages
    elif public_human_gate_summary and isinstance(stages.get("experiment"), dict):
        stages["experiment"]["human_gate_summary"] = public_human_gate_summary
    repo_details = stages.get("environment", {}).get("repo_details", []) if isinstance(stages, dict) else []
    dataset_details = stages.get("environment", {}).get("dataset_details", []) if isinstance(stages, dict) else []
    claude_status = _claude_session_status(root)
    runtime = runtime_diagnostics(project)
    literature_summary = _taste_literature_summary(root)
    public_literature_summary = public_projection.get("literature_survey") if isinstance(public_projection.get("literature_survey"), dict) else literature_summary
    public_full_cycle = public_projection.get("full_research_cycle") if isinstance(public_projection.get("full_research_cycle"), dict) else stages.get("experiment", {}).get("full_research_cycle", {})
    public_claude_status = public_projection.get("claude_status") if isinstance(public_projection.get("claude_status"), dict) else claude_status
    public_runtime = public_projection.get("runtime") if isinstance(public_projection.get("runtime"), dict) else runtime
    public_trajectory_system = public_projection.get("trajectory_system") if isinstance(public_projection.get("trajectory_system"), dict) else stages.get("experiment", {}).get("trajectory_system", {})
    state = {
        "discover_count": len(list((root / "discover").glob("*.json"))) if (root / "discover").exists() else 0,
        "repo_candidate_count": len(repo_details),
        "dataset_count": len(dataset_details),
        "experiment_count": len(_json_rows(_read_json(root / "state" / "experiment_registry.json", []))),
        "paper_pdf_count": len(list((root / "paper" / "output").glob("*.pdf"))) if (root / "paper" / "output").exists() else 0,
        "stages": stages,
        "experiments": stages["experiment"].get("experiments", []),
        "experiment_record": stages["experiment"].get("experiment_record", {}),
        "reference_reproduction_gate": stages["experiment"].get("reference_reproduction_gate", {}),
        "scientific_progress_gate": stages["experiment"].get("scientific_progress_gate", {}),
        "experiment_iteration_audit": stages["experiment"].get("experiment_iteration_audit", {}),
        "human_gate_summary": public_human_gate_summary,
        "literature_survey": public_literature_summary,
        "trajectory_system": public_trajectory_system,
        "full_research_cycle": public_full_cycle,
        "claude_status": public_claude_status,
        "runtime": public_runtime,
        "agent_state": _agent_state(project),
    }
    summary = {
        "project": project,
        "topic": cfg.get("topic", "") if isinstance(cfg, dict) else "",
        "summary": public_projection.get("summary") or (cfg.get("topic", "") if isinstance(cfg, dict) else ""),
        "status": public_projection.get("status") or (state.get("full_research_cycle", {}).get("status", "") if isinstance(state.get("full_research_cycle"), dict) else ""),
        "config": _public_project_identity_config(project, public_config or cfg),
        "run_preferences": public_run_preferences or _public_run_preferences(project, root, cfg if isinstance(cfg, dict) else {}),
        "path": str(root),
        "state": state,
        "stages": stages,
        "literature_survey": public_literature_summary,
        "human_gate_summary": public_human_gate_summary,
        "human_supervision": public_human_supervision,
        "main_route": public_main_route,
        "supervision": public_supervision,
        "trajectory_system": public_trajectory_system,
        "full_research_cycle": public_full_cycle,
        "claude_status": public_claude_status,
        "runtime": public_runtime,
        "agent_state": state["agent_state"],
        "artifacts": artifacts,
    }
    if compact:
        compact_summary = _compact_project_summary(summary)
        compact_full = compact_summary.get("full_research_cycle") if isinstance(compact_summary, dict) else None
        if isinstance(compact_full, dict) and str(compact_full.get("status") or "").lower() == "running":
            compact_full.pop("finished_at", None)
            compact_full.pop("completed_at", None)
            if isinstance(compact_summary.get("state"), dict) and isinstance(compact_summary["state"].get("full_research_cycle"), dict):
                compact_summary["state"]["full_research_cycle"].pop("finished_at", None)
                compact_summary["state"]["full_research_cycle"].pop("completed_at", None)
            if isinstance(compact_summary.get("stages"), dict) and isinstance(compact_summary["stages"].get("experiment"), dict) and isinstance(compact_summary["stages"]["experiment"].get("full_research_cycle"), dict):
                compact_summary["stages"]["experiment"]["full_research_cycle"].pop("finished_at", None)
                compact_summary["stages"]["experiment"]["full_research_cycle"].pop("completed_at", None)
        if isinstance(compact_summary.get("config"), dict):
            compact_summary.setdefault("topic", compact_summary["config"].get("topic", ""))
            compact_summary.setdefault("summary", compact_summary["config"].get("topic", ""))
            compact_summary.setdefault("status", compact_summary.get("full_research_cycle", {}).get("status", "") if isinstance(compact_summary.get("full_research_cycle"), dict) else "")
        compact_summary = _project_summary_public_identity(compact_summary)
        _PROJECT_SUMMARY_CACHE[cache_key] = (time.monotonic() + COMPACT_PROJECT_SUMMARY_TTL_SEC, compact_summary)
        return json.loads(json.dumps(compact_summary, ensure_ascii=False, default=str))
    return summary


def _append(cmd: list[str], flag: str, value: Any) -> None:
    text = "" if value is None else str(value).strip()
    if text:
        cmd.extend([flag, text])


def _fresh_base_data_is_blocked(project: str) -> bool:
    """Return True when current fresh-base hard gates block unsafe experiment/paper routes."""
    root = PROJECTS / project
    full = _read_json(root / "state" / "full_research_cycle.json", {})
    gate = _read_json(root / "state" / "reference_reproduction_gate.json", {})
    blocker_plan = _read_json(root / "state" / "blocker_action_plan.json", {})
    data = _read_json(root / "state" / "fresh_base_data_acquisition.json", {})
    loader = _read_json(root / "state" / "real_dataset_probe.json", {})
    if not isinstance(loader, dict) or not loader:
        loader = _fresh_base_loader_probe(root)
    protocol = _fresh_base_protocol_probe(root)
    smoke = _fresh_base_smoke_probe(root)
    blocker = full.get("current_blocker", {}) if isinstance(full, dict) and isinstance(full.get("current_blocker"), dict) else {}
    summary = blocker_plan.get("summary", {}) if isinstance(blocker_plan, dict) and isinstance(blocker_plan.get("summary"), dict) else {}
    markers = [
        str(full.get("status") or "") if isinstance(full, dict) else "",
        str(full.get("full_status") or "") if isinstance(full, dict) else "",
        str(gate.get("decision") or "") if isinstance(gate, dict) else "",
        str(data.get("decision") or "") if isinstance(data, dict) else "",
        str(loader.get("decision") or "") if isinstance(loader, dict) else "",
        str(protocol.get("decision") or "") if isinstance(protocol, dict) else "",
        str(smoke.get("decision") or "") if isinstance(smoke, dict) else "",
        str(blocker.get("category") or ""),
        str(summary.get("top_route") or ""),
    ]
    fresh_gate_markers = {
        "blocked_fresh_base_data_required",
        "blocked_fresh_base_reference_probe_required",
        "blocked_fresh_base_reference_smoke_required",
        "blocked_fresh_base_reference_reproduction_required",
        "blocked_fresh_base_implementation_required",
        "fresh_base_data_required",
        "fresh_base_reference_probe_required",
        "fresh_base_reference_smoke_required",
        "fresh_base_reference_reproduction_required",
        "fresh_base_implementation_required",
        "fresh_base_data_contract",
        "fresh_base_reference_probe",
        "fresh_base_reference_smoke",
        "fresh_base_reference_reproduction",
        "blocked_external_data_required",
    }
    return any(marker in fresh_gate_markers for marker in markers)


def _literature_recommendation_gate_is_blocked(project: str) -> bool:
    """Return True while the current Find packet is below the strict strong-recommendation target."""
    root = PROJECTS / project
    full = _read_json(root / "state" / "full_research_cycle.json", {})
    blocker_plan = _read_json(root / "state" / "blocker_action_plan.json", {})
    packet = _read_json(root / "state" / "literature_tool_packet.json", {})
    find_progress = _read_json(root / "planning" / "finding" / "find_progress.json", {})
    packet_summary = packet.get("summary", {}) if isinstance(packet, dict) and isinstance(packet.get("summary"), dict) else {}
    blocker_summary = blocker_plan.get("summary", {}) if isinstance(blocker_plan, dict) and isinstance(blocker_plan.get("summary"), dict) else {}

    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    shortfall = max(
        as_int(packet_summary.get("recommendation_shortfall")),
        as_int(find_progress.get("recommendation_shortfall") if isinstance(find_progress, dict) else 0),
    )
    markers = [
        str(full.get("status") or "") if isinstance(full, dict) else "",
        str(full.get("full_status") or "") if isinstance(full, dict) else "",
        str(blocker_plan.get("top_route") or "") if isinstance(blocker_plan, dict) else "",
        str(blocker_summary.get("top_route") or ""),
        str(packet_summary.get("recommendation_gate_status") or ""),
        str(find_progress.get("recommendation_gate_status") or "") if isinstance(find_progress, dict) else "",
    ]
    return bool(shortfall > 0 or any(marker in {"blocked_literature_recommendation_gate", "literature_recommendation_gate", "shortfall"} for marker in markers))


def _blocked_literature_gate_command(py: str, action: str) -> list[str]:
    message = (
        "blocked_literature_recommendation_gate: current Find recommended papers are below target; "
        f"{action} is blocked until the current title+abstract scoring packet passes."
    )
    return [py, "-c", f"print({json.dumps(message)}); raise SystemExit(2)"]

def _normalize_action(action: Any) -> str:
    normalized = str(action or "").strip()
    if normalized == "paper-orchestra":
        normalized = "paper"
    if normalized == "current_find_selection":
        normalized = "current-find-selection"
    return normalized


LITERATURE_GATE_BLOCKED_ACTIONS = {
    "literature-base-audit",
    "literature_base_audit",
    "environment",
    "experiment",
    "autonomous",
    "claude-message",
}
LITERATURE_GATE_CONDITIONAL_ACTIONS = {
    "full-cycle",
    "full_research_cycle",
}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _requests_fresh_full_cycle_find(action: str, payload: dict[str, Any]) -> bool:
    if action not in {"full-cycle", "full_research_cycle"}:
        return False
    if _truthy(payload.get("use_existing_literature_packet")):
        return False
    return any(_truthy(payload.get(key)) for key in ["fresh_start", "force_discovery", "restart_full_cycle", "human_approved_new_find"])


def _record_fresh_full_cycle_find_approval(project: str, payload: dict[str, Any], *, source: str) -> None:
    root = PROJECTS / project
    reason = str(payload.get("new_find_reason") or payload.get("approval_reason") or "explicit full-cycle restart requested fresh Find").strip()
    record = {
        "project": project,
        "approved": True,
        "source": source,
        "reason": reason,
        "action": _normalize_action(payload.get("action")),
        "fresh_start": _truthy(payload.get("fresh_start")),
        "force_discovery": _truthy(payload.get("force_discovery")),
        "restart_full_cycle": _truthy(payload.get("restart_full_cycle")),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "latest_new_find_restart_approval.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (state_dir / "new_find_restart_audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _live_full_cycle_guard(project: str) -> dict[str, Any]:
    root = PROJECTS / project
    state_dir = root / "state"

    def normalize(row: Any, source: str) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        pid = row.get("pid")
        if not _pid_alive(pid):
            return {}
        command = _command_text(row.get("command") or row.get("cmd") or row.get("args") or "")
        stage = str(row.get("stage") or row.get("kind") or row.get("status") or "")
        kind = str(row.get("kind") or "")
        is_full_cycle = _is_real_full_cycle_command(command, kind=kind, stage=stage)
        if not is_full_cycle or _process_has_current_find_ancestor(row):
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
            "cmd": command,
            "log_path": str(row.get("log_path") or row.get("stdout_path") or ""),
            "started_at": str(row.get("started_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "source": source,
            "web_job_id": str(row.get("web_job_id") or ""),
        }

    candidates: list[tuple[str, Any]] = []
    job = _read_json(state_dir / "full_cycle_job.json", {})
    candidates.append(("full_cycle_job.json", job))
    full = _read_json(state_dir / "full_research_cycle.json", {})
    if isinstance(full, dict):
        candidates.append(("full_research_cycle.full_cycle_job", full.get("full_cycle_job")))
        candidates.append(("full_research_cycle.current_running_stage", full.get("current_running_stage")))
    tick = _read_json(state_dir / "supervision_tick.json", {})
    if isinstance(tick, dict):
        candidates.append(("supervision_tick.full_cycle_job", tick.get("full_cycle_job")))

    for source, row in candidates:
        live = normalize(row, source)
        if live:
            return live

    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,stat=,etimes=,cmd="],
            text=True,
            capture_output=True,
            timeout=3,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    for line in str(proc.stdout or "").splitlines():
        if f"--project {project}" not in line and f"--project={project}" not in line:
            continue
        if "run_full_research_cycle.py" not in line:
            continue
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, stat, elapsed, cmd = parts
        if "Z" in stat.upper() or not _pid_alive(pid):
            continue
        return {
            "project": project,
            "pid": int(pid),
            "ppid": int(ppid) if str(ppid).isdigit() else ppid,
            "status": "running",
            "process_alive": True,
            "alive": True,
            "kind": "full_cycle",
            "stage": "full-cycle",
            "command": cmd,
            "cmd": cmd,
            "elapsed_sec": int(elapsed) if str(elapsed).isdigit() else elapsed,
            "source": "ps",
        }
    return {}


FULL_CYCLE_EXCLUSIVE_ACTIONS = {
    "full-cycle",
    "full_research_cycle",
    "autonomous",
    "environment",
    "experiment",
    "paper",
    "current-find-selection",
    "current_find_selection",
    "claude-message",
    "literature-base-audit",
    "literature_base_audit",
}


def action_gate_blocker(payload: dict[str, Any]) -> dict[str, Any] | None:
    action = _normalize_action(payload.get("action"))
    guarded_actions = LITERATURE_GATE_BLOCKED_ACTIONS | LITERATURE_GATE_CONDITIONAL_ACTIONS | FULL_CYCLE_EXCLUSIVE_ACTIONS
    if action not in guarded_actions:
        return None
    project = _safe_project(str(payload.get("project") or ""))
    if action in FULL_CYCLE_EXCLUSIVE_ACTIONS:
        live_full_cycle = _live_full_cycle_guard(project)
        if live_full_cycle:
            return {
                "error": "full_cycle_already_running",
                "status": "blocked_existing_full_cycle_running",
                "project": project,
                "action": action,
                "message": "A full research cycle is already running for this project; duplicate stage launch is blocked. Queue guidance to the active project agent instead.",
                "message_zh": "当前项目已有完整科研循环正在运行；已阻止重复启动新的阶段任务。需要人工介入时请使用项目代理指令队列。",
                "existing_full_cycle": live_full_cycle,
            }
    if not _literature_recommendation_gate_is_blocked(project):
        return None
    if _requests_fresh_full_cycle_find(action, payload):
        _record_fresh_full_cycle_find_approval(project, payload, source="api_jobs_gate")
        return None
    return {
        "error": "action_blocked_by_current_literature_gate",
        "status": "blocked_literature_recommendation_gate",
        "project": project,
        "action": action,
        "blocked_actions": sorted(guarded_actions),
        "allowed_actions": [
            f"{management_python()} modules/finding/scripts/build_literature_tool_packet.py --project {project}",
            f"{management_python()} modules/finding/scripts/run_literature_tool.py --project {project} --query \"<targeted literature gap query>\" --fast-mode",
            f"{management_python()} modules/writing/scripts/audit_submission_readiness.py --project {project}",
            f"{management_python()} modules/planning/scripts/build_blocker_action_plan.py --project {project}",
        ],
        "message": "Current Find strong-recommendation gate is short; downstream research actions are blocked until the current title+abstract scoring packet passes. Full-cycle is allowed only when the request explicitly approves a fresh Find restart.",
        "message_zh": "当前 Find 推荐门控未过；下游动作被锁定，只允许修复当前 Find 文献/评分包。完整重启只有在请求明确批准 fresh Find 时才允许。",
    }



def job_stage(payload: dict[str, Any]) -> str:
    action = _normalize_action(payload.get("action")) or "action"
    # When the full cycle is data-blocked, the command safely runs the
    # unblock probe, but the user-facing job remains the full-cycle route.
    # Safe-unblock is an internal step, not a separate top-level workflow.
    return action


VENUE_AUTHORITATIVE_ACTIONS = {"autonomous", "full-cycle", "full_research_cycle", "environment", "experiment", "paper", "healthcheck", "status"}


def _payload_with_project_config_venue(payload: dict[str, Any]) -> dict[str, Any]:
    action = _normalize_action(payload.get("action"))
    if action not in VENUE_AUTHORITATIVE_ACTIONS:
        return payload
    project = _safe_project(str(payload.get("project") or ""))
    payload_venue = str(payload.get("venue") or "").strip()
    configured = str(project_target_venue(project, default=payload_venue) or "").strip()
    if not configured or configured == payload_venue:
        return payload
    out = dict(payload)
    if payload_venue and not out.get("launch_venue"):
        out["launch_venue"] = payload_venue
    out["venue"] = configured
    return out



def _current_find_selection_command_mode(project: str) -> str:
    """Use selection-only mode only after current Read/Idea/Plan content exists."""
    try:
        pipeline = _current_find_pipeline_summary(PROJECTS / project)
    except Exception:
        return "--force"
    if not isinstance(pipeline, dict):
        return "--force"

    def to_int(value: Any) -> int:
        try:
            if value in (None, ""):
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    content_ready = bool(
        pipeline.get("content_ready")
        or pipeline.get("read_idea_plan_ready")
        or pipeline.get("claude_current_find_ready")
    )
    readings = to_int(pipeline.get("reading_count") or pipeline.get("readings"))
    ideas = to_int(pipeline.get("idea_count") or pipeline.get("ideas"))
    plans = to_int(pipeline.get("plan_count") or pipeline.get("plans"))
    if content_ready and readings > 0 and ideas > 0 and plans > 0:
        return "--force-selection"
    return "--force"

def build_command(payload: dict[str, Any]) -> tuple[str, list[str]]:
    payload = _payload_with_project_config_venue(payload)
    action = _normalize_action(payload.get("action"))
    project = _safe_project(str(payload.get("project") or ""))
    py = management_python()
    guarded_actions = LITERATURE_GATE_BLOCKED_ACTIONS | LITERATURE_GATE_CONDITIONAL_ACTIONS
    if action in guarded_actions and _literature_recommendation_gate_is_blocked(project) and not _requests_fresh_full_cycle_find(action, payload):
        return project, _blocked_literature_gate_command(py, action)
    if _requests_fresh_full_cycle_find(action, payload):
        _record_fresh_full_cycle_find_approval(project, payload, source="build_command")
    if action == "healthcheck":
        cmd = [py, str(SCRIPTS / "refresh_project_reports.py"), "--project", project]
    elif action == "status":
        cmd = [py, str(SCRIPTS / "refresh_project_reports.py"), "--project", project]
    elif action == "handoff":
        cmd = [py, str(SCRIPTS / "generate_handoff.py"), "--project", project]
    elif action == "init":
        cmd = [py, str(SCRIPTS / "run_loop.py"), "--project", project, "--iterations", "0"]
        _append(cmd, "--prompt", payload.get("prompt"))
        _append(cmd, "--topic", payload.get("topic"))
        _append(cmd, "--conda-env", payload.get("conda_env"))
    elif action == "autonomous":
        iterations = max(1, min(50, int(payload.get("iterations") or 1)))
        cmd = [py, str(SCRIPTS / "run_autonomous_research.py"), "--project", project, "--iterations", str(iterations)]
        for flag, key in [
            ("--prompt", "prompt"), ("--topic", "topic"), ("--venue", "venue"),
            ("--title", "title"), ("--benchmark", "benchmark"), ("--metric", "metric"),
            ("--dataset", "dataset"), ("--repo-name", "repo_name"), ("--repo-path", "repo_path"),
            ("--command-template", "command_template"), ("--conda-env", "conda_env"),
        ]:
            _append(cmd, flag, payload.get(key))
        if payload.get("max_results"):
            cmd.extend(["--max-results", str(max(1, int(payload["max_results"])))])
        for key, flag in [
            ("skip_llm", "--skip-llm"), ("skip_semantic_scholar", "--skip-semantic-scholar"),
            ("skip_github", "--skip-github"), ("skip_initialization", "--skip-initialization"),
            ("skip_discovery", "--skip-discovery"), ("execute_plan", "--execute-plan"),
            ("prepare_env", "--prepare-env"), ("real_bootstrap_env", "--real-bootstrap-env"),
            ("skip_paper", "--skip-paper"), ("skip_fetch", "--skip-fetch"),
            ("skip_compile", "--skip-compile"), ("force_template", "--generate-paper-preview"),
            ("auto_install_latex", "--auto-install-latex"),
        ]:
            if payload.get(key):
                cmd.append(flag)
    elif action in {"full-cycle", "full_research_cycle"}:
        venue = str(payload.get("venue") or "").strip()
        if not venue:
            raise ValueError("Full research cycle requires a venue.")
        if _requests_fresh_full_cycle_find(action, payload):
            _record_fresh_full_cycle_find_approval(project, payload, source="full_cycle_command")
        max_cycles = max(3, min(20, int(payload.get("max_cycles") or 3)))
        iterations_per_cycle = max(1, min(10, int(payload.get("iterations_per_cycle") or 1)))
        trajectory_rounds = max(1, min(10, int(payload.get("trajectory_rounds") or 1)))
        cmd = [
            py,
            str(SCRIPTS / "run_full_research_cycle.py"),
            "--project",
            project,
            "--venue",
            venue,
            "--max-cycles",
            str(max_cycles),
            "--iterations-per-cycle",
            str(iterations_per_cycle),
            "--trajectory-rounds",
            str(trajectory_rounds),
        ]
        for flag, key in [("--topic", "topic"), ("--title", "title")]:
            _append(cmd, flag, payload.get(key))
        if payload.get("max_launches"):
            cmd.extend(["--max-launches", str(max(1, int(payload["max_launches"])))])
        if payload.get("paper_repair_rounds"):
            cmd.extend(["--paper-repair-rounds", str(max(1, int(payload["paper_repair_rounds"])))])
        if payload.get("figure_repair_rounds"):
            cmd.extend(["--figure-repair-rounds", str(max(1, int(payload["figure_repair_rounds"])))])
        if payload.get("claude_timeout_sec"):
            cmd.extend(["--claude-timeout-sec", str(max(0, int(payload["claude_timeout_sec"])))])
        for flag, key in [
            ("--autonomous-timeout-sec", "autonomous_timeout_sec"),
            ("--trajectory-timeout-sec", "trajectory_timeout_sec"),
            ("--paper-timeout-sec", "paper_timeout_sec"),
        ]:
            if payload.get(key):
                cmd.extend([flag, str(max(0, int(payload[key])))])
        for key, flag in [
            ("skip_fetch", "--skip-fetch"),
            ("auto_install_latex", "--auto-install-latex"),
            ("fresh_start", "--fresh-start"),
            ("force_discovery", "--force-discovery"),
            ("use_existing_literature_packet", "--use-existing-literature-packet"),
        ]:
            if payload.get(key):
                cmd.append(flag)
        if (
            not payload.get("fresh_start")
            and not payload.get("force_discovery")
            and not payload.get("use_existing_literature_packet")
            and (PROJECTS / project / "state" / "literature_tool_packet.json").exists()
            and (PROJECTS / project / "planning" / "finding" / "find_results.json").exists()
        ):
            cmd.append("--use-existing-literature-packet")
    elif action == "current-find-selection":
        cmd = [py, str(SCRIPTS / "ensure_current_find_research_plan.py"), "--project", project, _current_find_selection_command_mode(project)]
    elif action in {"literature-base-audit", "literature_base_audit"}:
        limit = max(1, min(200, int(payload.get("limit") or 26)))
        repo_search_per_candidate = max(1, min(8, int(payload.get("repo_search_per_candidate") or 2)))
        repo_limit = max(1, min(10, int(payload.get("repo_limit") or 4)))
        probe_timeout_sec = max(30, min(900, int(payload.get("probe_timeout_sec") or 90)))
        cmd = [
            py,
            str(SCRIPTS / "run_literature_base_audit.py"),
            "--project",
            project,
            "--limit",
            str(limit),
            "--repo-search-per-candidate",
            str(repo_search_per_candidate),
            "--repo-limit",
            str(repo_limit),
            "--probe-timeout-sec",
            str(probe_timeout_sec),
        ]
    elif action == "environment":
        cmd = [py, str(SCRIPTS / "run_environment_stage.py"), "--project", project]
        _append(cmd, "--repo-path", payload.get("repo_path"))
        _append(cmd, "--env-name", payload.get("conda_env"))
        _append(cmd, "--venue", payload.get("venue"))
        if payload.get("real_bootstrap_env", True):
            cmd.append("--real-bootstrap-env")
        if payload.get("skip_reference_repair"):
            cmd.append("--skip-reference-repair")
    elif action == "experiment":
        if _literature_recommendation_gate_is_blocked(project):
            cmd = _blocked_literature_gate_command(py, action)
        elif _fresh_base_data_is_blocked(project):
            cmd = [py, "-c", "print('blocked_fresh_base_gate_required: current project fresh-base gates must pass before experiments.') ; raise SystemExit(2)"]
        else:
            iterations = max(1, min(50, int(payload.get("iterations") or 1)))
            cmd = [py, str(SCRIPTS / "run_autonomous_research.py"), "--project", project, "--iterations", str(iterations), "--execute-plan", "--prepare-env", "--real-bootstrap-env", "--skip-paper", "--skip-discovery"]
            for flag, key in [("--topic", "topic"), ("--venue", "venue")]:
                _append(cmd, flag, payload.get(key))
            if payload.get("max_launches"):
                cmd.extend(["--max-launches", str(max(1, int(payload["max_launches"])))])
    elif action == "paper":
        venue = str(payload.get("venue") or "").strip()
        if not venue:
            raise ValueError("Paper pipeline requires a venue.")
        cmd = [py, str(SCRIPTS / "run_paper_pipeline.py"), "--project", project, "--venue", venue]
        _append(cmd, "--title", payload.get("title"))
        _append(cmd, "--template-url", payload.get("template_url"))
        _append(cmd, "--template-archive-path", payload.get("template_archive_path"))
        # A web paper action means: generate or revise the current venue-formatted
        # manuscript preview from current TASTE evidence. Evidence/readiness gates stay
        # honest, but the user-facing button must not require a hidden force flag.
        payload = {
            **payload,
            "force_template": True,  # internal compatibility: generate preview even when evidence gates remain blocked
            "force_refresh": True,
            "refresh_current_venue": True if payload.get("refresh_current_venue") is None else payload.get("refresh_current_venue"),
        }
        for key, flag in [
            ("skip_fetch", "--skip-fetch"), ("skip_compile", "--skip-compile"),
            ("strict_template", "--strict-template"), ("force_template", "--generate-paper-preview"),
            ("force_refresh", "--refresh-current-paper"),
            ("refresh_current_venue", "--refresh-current-venue"),
            ("auto_install_latex", "--auto-install-latex"),
        ]:
            if payload.get(key):
                cmd.append(flag)
    elif action == "claude-message":
        message = str(payload.get("message") or payload.get("prompt") or "").strip()
        if not message:
            raise ValueError("Claude message requires non-empty message.")
        stage = str(payload.get("stage") or "manual")
        agent_id = str(payload.get("agent_id") or "main")
        cmd = [py, str(SCRIPTS / "claude_project_session.py"), "--project", project, "--stage", stage, "--message", message, "--agent-id", agent_id]
        if payload.get("timeout_sec"):
            timeout_value = int(payload["timeout_sec"])
            cmd.extend(["--timeout-sec", str(timeout_value if timeout_value <= 0 else max(30, timeout_value))])
    elif action == "agent-guidance":
        target_agent_id = str(payload.get("agent_id") or "main")
        message = str(payload.get("message") or payload.get("prompt") or "").strip()
        if not message:
            raise ValueError("Agent guidance requires non-empty message.")
        stage = str(payload.get("stage") or "project").strip() or "project"
        item = queue_guidance(project, target_agent_id, message, stage=stage, source="web")
        return project, [py, "-c", f"print({json.dumps('queued project guidance: ' + item['id'])})"]
    else:
        raise ValueError(f"Unknown research action: {action}")
    if action in {"healthcheck", "status"}:
        _append(cmd, "--venue", payload.get("venue"))
    return project, cmd



def _is_transient_progress_line(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return True
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
    ]
    if any(marker in lowered for marker in transient_markers):
        return True
    json_error_prefixes = ('"error"', '"code"', '"message"', '"type"')
    compact = lowered.strip().rstrip(",")
    if compact in {"{", "}", "};"} or compact.startswith(json_error_prefixes):
        return True
    return False


def _progress_text(text: str) -> str:
    value = str(text or "").strip()
    if _is_transient_progress_line(value):
        return ""
    stripped = value.strip()
    lowered = stripped.lower()
    if not stripped:
        return ""
    if stripped in {"{", "}", "},", "[", "]"}:
        return ""
    if stripped.startswith(("\"", "'")) and ("/home/" in stripped or stripped.rstrip(",").endswith(('.json"', '.md"', '.tex"', '.pdf"', '.log"'))):
        return ""
    if re.match(r'^"[A-Za-z0-9_ -]+"\s*:\s*"[^"\n]+\.(?:json|md|tex|pdf|log)"[,]?$', stripped):
        return ""
    if re.match(r'^(?:/|projects/|state/|reports/|paper/)[^\s]+\.(?:json|md|tex|pdf|log)$', stripped):
        return ""
    if any(marker in lowered for marker in ["paper pipeline generated", "paper pipeline stopped", "generated a compile report"]):
        return stripped[:180]
    if lowered.startswith("warning: --force-template") or lowered.startswith("warning: --generate-inspection-paper") or lowered.startswith("warning: --generate-paper-preview"):
        return "正在生成当前稿件预览；证据门控仍按真实状态保留，不标记为投稿通过。"
    if "run_paper_orchestra_bridge.py" in stripped:
        return "正在调用 writing 模块生成/修订论文稿。"
    if "claude_project_session.py" in stripped and "writing" in stripped:
        return "项目 Claude Code 正在执行 writing 阶段。"
    if "build_conference_preview_paper.py" in stripped:
        return "正在同步目标 venue PDF 预览。"
    if "compile_paper_pdf.py" in stripped or "latexmk" in lowered or "pdflatex" in lowered:
        return "正在编译论文 PDF。"
    if "audit_paper" in stripped or "submission_readiness" in stripped or "build_blocker_action_plan" in stripped:
        return "正在刷新论文审计与投稿准备度状态。"
    return stripped[:180]


def _paper_int(value: Any) -> int:
    try:
        if value in (None, ''):
            return 0
        return int(float(value))
    except Exception:
        return 0


def _paper_venue_labels(row: dict | None = None, venue: str = '') -> dict[str, str]:
    row = row if isinstance(row, dict) else {}
    policy = row.get('venue_submission_policy') if isinstance(row.get('venue_submission_policy'), dict) else {}
    venue_text = str(venue or row.get('venue') or row.get('target_venue') or row.get('venue_slug') or '')
    family = str(policy.get('template_family') or row.get('template_family') or '').lower()
    slug = venue_text.lower()
    is_journal = family == 'springer-nature' or 'nature' in slug or 'journal' in slug
    return {
        'venue_zh': '期刊' if is_journal else '会议',
        'preview_zh': '期刊稿预览' if is_journal else '会议格式论文预览',
        'current_preview_zh': '当前期刊稿预览' if is_journal else '当前会议格式论文预览',
        'requirement_zh': '期刊要求' if is_journal else '会议要求',
    }


def _paper_public_blocker_text(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    lowered = text.lower()
    if 'missing bib entries' in lowered or 'missing bibliography entries' in lowered or 'cited keys=' in lowered or 'latex_undefined_citations' in lowered or 'undefined citations' in lowered:
        return '引用/参考文献仍需修复；具体修复清单已交由项目代理处理。'
    if 'nature_numeric_style_textual_citations' in lowered or '\\citet' in text or '\\citeauthor' in text or '作者型引用命令' in text:
        return 'Nature 数字引用模板下检测到作者型引用命令；应改为正常叙述加数字引用，重新编译并确认 PDF 不再出现 `(author?)`。'
    if 'pdf_unresolved_citation_markers' in lowered or '未解析引用标记' in text or '[?]' in text or '??' in text:
        return '引用渲染失败：PDF 正文含 `(author?)`、`[?]` 或 `??` 等未解析引用标记，不能作为正常论文预览通过。'
    if 'natbib_author_undefined' in lowered or 'author undefined' in lowered or '(author?)' in lowered:
        return '引用渲染失败：natbib Author undefined 导致 PDF 出现 `(author?) [n]`，TASTE 写作必须修复引用命令、BibTeX 字段或模板兼容性后重新编译。'
    if 'references/citation' in lowered or 'reference_quality_target' in lowered or 'reference_count' in lowered:
        return '参考文献覆盖不足：当前引用数量还没有达到 写作引用质量目标，需要补充真实且相关的已验证引用。'
    if 'wide' in lowered and 'graphic' in lowered:
        return '图表版面需要调整：有宽图被压进单栏，优先处理图表占地和单栏适配。'
    return text


def _paper_public_layout_warning_text(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    lowered = text.lower()
    if 'wide' in lowered and 'graphic' in lowered and 'single-column' in lowered:
        return '图表版面提示：宽图被放入单栏，当前应优先改为跨栏图、重绘或缩减图表占地。'
    if 'large single-column figure footprint' in lowered:
        return '图表版面提示：单栏图占地偏大，正文页数紧张时应先调整图表尺寸或重绘。'
    return text


def _paper_self_review_evidence_public_projection(category: str, detail: str) -> dict[str, str]:
    marker = f"{category} {detail}".lower()
    if 'missing_empirical_validation' in marker or 'zero empirical result' in marker or 'untested architecture' in marker:
        return {
            'public_title': '缺少新方法实验验证',
            'public_detail': '结果区尚未给出当前候选方法相对当前参考基底的同协议本地指标，不能把架构描述当作论文实证结论。',
            'public_next_action': '补齐 candidate/backbone/ablation 的本地审计指标后，再允许论文提升方法 claim。',
        }
    if 'results_contains_untested_design_space' in marker or 'method design space' in marker or 'untested architectural variants' in marker:
        return {
            'public_title': 'Results 含未验证设计空间',
            'public_detail': '结果区包含多种架构设计维度，但没有相应实验对比；应移到方法/讨论，或由 TASTE 实验补齐真实比较。',
            'public_next_action': '让写作阶段收束 Results，只保留已有证据；未测设计不得伪装成实证发现。',
        }
    if 'evaluation_scope_mismatch' in marker or ('contribution' in marker and 'backbone' in marker and 'primary claimed contribution' in marker):
        return {
            'public_title': '贡献表述范围不匹配',
            'public_detail': '当前贡献把 backbone 复现协议写成主要贡献，但它只验证基底，不验证 proposed method。',
            'public_next_action': '把基底复现降级为前置校准，并等待当前候选方法实验证据后再更新贡献列表。',
        }
    if 'data_code_availability' in marker or 'data availability' in marker or 'code availability' in marker or 'missing_data_or_code_availability' in marker:
        return {
            'public_title': '数据/代码可用性缺少明确链接',
            'public_detail': 'Data/Code availability 仍是泛称，缺少可核验的 dataset、repo 或 artifact 链接；预览稿不能标为 submission-ready。',
            'public_next_action': '由写作阶段绑定真实数据、代码和本地 artifact 路径；匿名预览和正式投稿要求要分开。',
        }
    if 'citation' in marker or '(author?)' in marker or '[?]' in marker:
        return {
            'public_title': '引用渲染或参考文献仍需修复',
            'public_detail': '论文自审发现引用/参考文献问题；PDF 只能作为检查预览，不能作为投稿稿。',
            'public_next_action': '修复 TeX/BibTeX/模板兼容性并重新编译审计。',
        }
    normalized = re.sub(r'[_\\-]+', ' ', str(category or '')).strip()
    title = normalized[:80] if normalized and normalized.lower() != 'self review evidence' else '科研证据待补齐'
    return {
        'public_title': title or '科研证据待补齐',
        'public_detail': '论文自审发现未解决的科研证据问题；完整审稿原文保留在论文自审 artifact 中，不在网页主体展开。',
        'public_next_action': '让 writing/experiment 继续基于真实 artifact 修复，保持投稿准备度阻塞。',
    }


def _paper_public_self_review_evidence_rows(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, dict):
            category = str(row.get('category') or row.get('id') or 'self_review_evidence')
            raw_detail = str(row.get('detail') or row.get('issue') or row.get('summary') or category).strip()
            public = _paper_self_review_evidence_public_projection(category, raw_detail)
            public_detail = str(public.get('public_detail') or public.get('public_title') or category).strip()
            out.append({
                'id': str(row.get('id') or category),
                'category': category,
                'public_title': public.get('public_title') or category,
                'summary': public.get('public_title') or category,
                'detail': public_detail,
                'public_detail': public_detail,
                'public_summary': public.get('public_title') or category,
                'public_next_action': public.get('public_next_action') or '',
                'source': '论文开放式自审',
                'source_detail_available': bool(raw_detail),
                'raw_detail_chcount': len(raw_detail),
                'artifact_hint': 'state/paper_evidence_audit.json / state/submission_readiness.json',
                'preview_blocker': False,
                'submission_blocker': True,
            })
        else:
            raw_detail = str(row or '').strip()
            if raw_detail:
                public = _paper_self_review_evidence_public_projection('self_review_evidence', raw_detail)
                public_detail = str(public.get('public_detail') or public.get('public_title') or '科研证据待补齐').strip()
                out.append({
                    'id': 'self_review_evidence',
                    'category': 'self_review_evidence',
                    'public_title': public.get('public_title') or '科研证据待补齐',
                    'summary': public.get('public_title') or '科研证据待补齐',
                    'detail': public_detail,
                    'public_detail': public_detail,
                    'public_summary': public.get('public_title') or '科研证据待补齐',
                    'public_next_action': public.get('public_next_action') or '',
                    'source': '论文开放式自审',
                    'source_detail_available': True,
                    'raw_detail_chcount': len(raw_detail),
                    'artifact_hint': 'state/paper_evidence_audit.json / state/submission_readiness.json',
                    'preview_blocker': False,
                    'submission_blocker': True,
                })
    return out


def _paper_stage_public_message(paper_stage: Any) -> str:
    row = paper_stage if isinstance(paper_stage, dict) else {}
    policy = row.get('venue_submission_policy') if isinstance(row.get('venue_submission_policy'), dict) else {}
    diagnostics = row.get('paper_public_diagnostics') if isinstance(row.get('paper_public_diagnostics'), list) else []
    parts: list[str] = []
    if row.get('blocked_preview_available') or row.get('raw_pdf_path') or row.get('pdf_path'):
        parts.append(_paper_venue_labels(row).get('preview_zh', '会议格式论文预览') + '已生成')
    elif row.get('latest_generated_pdf_path'):
        parts.append(_paper_venue_labels(row).get('preview_zh', '会议格式论文预览') + '有最近产物')
    else:
        parts.append(_paper_venue_labels(row).get('preview_zh', '会议格式论文预览') + '尚未生成')
    body_pages = _paper_int(row.get('conference_preview_body_pages'))
    body_limit = _paper_int(row.get('conference_preview_body_page_limit') or policy.get('body_page_max'))
    if body_pages and body_limit:
        parts.append(f'正文页数 {body_pages}/{body_limit}')
    elif body_pages:
        parts.append(f'正文页数 {body_pages}')
    citation_count = _paper_int(row.get('paper_normality_citation_count'))
    citation_target = _paper_int(
        row.get('paper_normality_citation_target')
        or row.get('paper_reference_quality_target')
        or policy.get('reference_quality_target')
        or policy.get('reference_quality_target')
        or policy.get('official_min_references')
        or policy.get('min_references')
    )
    citation_target_source = str(row.get('paper_normality_reference_target_source') or policy.get('reference_target_source') or '').strip()
    if citation_count and citation_target:
        parts.append(f"{('官方引用要求' if citation_target_source == 'official' else '写作引用质量目标')} {citation_count}/{citation_target}")
    elif citation_count:
        parts.append(f'参考文献数量 {citation_count}')
    layout_count = len(row.get('paper_layout_footprint_warnings') or []) if isinstance(row.get('paper_layout_footprint_warnings'), list) else 0
    if layout_count:
        parts.append(f'图表版面提示 {layout_count} 项，优先处理图表占地')
    blocker = str(row.get('conference_preview_blocker_summary') or '').strip()
    if blocker and blocker not in '；'.join(parts):
        if '参考文献覆盖不足' in blocker or 'reference_count' in blocker or 'references/citation' in blocker:
            parts.append('写作质量目标未达：参考文献覆盖不足')
        else:
            parts.append('预览仍需完善：' + blocker)
    if body_pages and body_limit and body_pages <= body_limit and (layout_count or (citation_count and citation_target and citation_count < citation_target)):
        parts.append('正文页数已符合' + _paper_venue_labels(row).get('requirement_zh', '会议要求') + '，后续重点是图表占地、真实引用覆盖和模板细节')
    self_review_blockers = row.get('paper_self_review_blockers') if isinstance(row.get('paper_self_review_blockers'), list) else []
    self_review_status = str(row.get('paper_self_review_status') or '').strip().lower()
    if self_review_blockers or self_review_status == 'block':
        parts.append('论文自审未通过，具体修复项已交由项目代理处理')
    self_review_evidence_blockers = row.get('paper_self_review_evidence_blockers') if isinstance(row.get('paper_self_review_evidence_blockers'), list) else []
    if self_review_evidence_blockers:
        parts.append(f'论文自审发现 {len(self_review_evidence_blockers)} 项未解决科研证据问题，预览不能标记为投稿通过')
    if str(row.get('status') or '').startswith('blocked') or row.get('blocked_preview_available') or row.get('latest_generated_pdf_path'):
        parts.append('投稿/证据门控仍按真实状态保留，不标记为投稿通过')
    return '；'.join(parts) + '。'


def _paper_public_blocker_rows(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, dict):
            raw_detail = str(row.get('id', '')) + ': ' + str(row.get('public_detail') or row.get('detail') or '')
            public_detail = _paper_public_blocker_text(raw_detail)
            out.append({
                'id': row.get('id', ''),
                'status': row.get('status', ''),
                'public_detail': public_detail,
                'detail': public_detail,
            })
        else:
            public_detail = _paper_public_blocker_text(row)
            if public_detail:
                out.append({'id': 'preview_blocker', 'status': 'block', 'public_detail': public_detail, 'detail': public_detail})
    return out


def _paper_stage_public_fields(paper_stage: Any) -> dict[str, Any]:
    row = paper_stage if isinstance(paper_stage, dict) else {}
    keys = [
        'status', 'venue', 'summary', 'summary_zh', 'paper_generation_skipped',
        'paper_generation_skipped_reason', 'paper_normality_status',
        'paper_venue_format_status', 'paper_figure_quality_status',
        'paper_normality_citation_count', 'paper_normality_citation_target',
        'paper_normality_reference_target_source', 'paper_normality_pages',
        'paper_normality_body_pages', 'paper_normality_estimated_reference_pages',
        'paper_reference_quality_target',
        'paper_reference_official_min', 'paper_self_review_status', 'paper_self_review_ready',
        'paper_self_review_evidence_blocker_count', 'paper_self_review_preview_only_ready', 'paper_self_review_submission_evidence_ready',
        'conference_preview_ready', 'conference_preview_pages',
        'conference_preview_body_pages', 'conference_preview_body_page_limit',
        'conference_preview_reference_pages', 'conference_preview_blocker_summary',
        'paper_layout_summary', 'venue_requirements_status', 'venue_requirements_path',
        'venue_requirements_public_summary',
        'pdf_ready', 'pdf_path', 'pdf_url', 'blocked_preview_available',
        'blocked_pdf_path', 'blocked_pdf_url', 'blocked_tex_path', 'blocked_tex_url',
        'latest_generated_pdf_path', 'latest_generated_pdf_url', 'raw_pdf_path', 'raw_pdf_url',
    ]
    out = {key: row.get(key) for key in keys if key in row}
    if isinstance(row.get('venue_requirements_summary'), dict):
        out['venue_requirements_summary'] = row.get('venue_requirements_summary')
    if isinstance(row.get('paper_public_diagnostics'), list):
        out['paper_public_diagnostics'] = row.get('paper_public_diagnostics')[:8]
    if isinstance(row.get('paper_layout_footprint_warnings'), list):
        out['paper_layout_footprint_warnings'] = [item for item in (_paper_public_layout_warning_text(value) for value in row.get('paper_layout_footprint_warnings')) if item][:8]
    out['paper_citation_render_blockers'] = []
    out['paper_self_review_blockers'] = []
    out['paper_self_review_evidence_blockers'] = []
    out['conference_preview_blockers'] = []
    out['paper_summary'] = _paper_stage_public_message(row)
    return out


def _looks_like_llm_quota_blocker(value: Any) -> bool:
    if not value:
        return False
    text = json.dumps(value, ensure_ascii=False).lower() if isinstance(value, (dict, list)) else str(value).lower()
    quota_markers = [
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
    return any(marker in text for marker in quota_markers)


def _write_project_state_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _full_cycle_llm_readiness_block(project: str, env: dict[str, str], payload: dict[str, Any], agent_id: str, log: LogFn, progress: ProgressFn) -> dict[str, Any] | None:
    if _truthy(payload.get("use_existing_literature_packet")):
        return None
    state_dir = PROJECTS / project / "state"
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    probe_env = dict(env)
    project_cfg = _read_json(PROJECTS / project / "project.json", {})
    project_llm = project_cfg.get("llm", {}) if isinstance(project_cfg, dict) and isinstance(project_cfg.get("llm"), dict) else {}
    taste_cfg = _read_json(CONFIG_PATH, {})
    if not isinstance(taste_cfg, dict):
        taste_cfg = {}
    api_key_env = str(probe_env.get("LLM_API_KEY_ENV") or project_llm.get("api_key_env") or "OPENAI_API_KEY")
    api_key = (
        str(probe_env.get(api_key_env) or "")
        or str(probe_env.get("LLM_API_KEY") or "")
        or str(project_llm.get("api_key") or "")
        or str(taste_cfg.get("api_key") or "")
    )
    if api_key_env:
        probe_env["LLM_API_KEY_ENV"] = api_key_env
    if api_key:
        probe_env["LLM_API_KEY"] = api_key
        if api_key_env:
            probe_env[api_key_env] = api_key
    env_defaults = {
        "LLM_API_BASE": project_llm.get("api_base") or taste_cfg.get("api_base") or taste_cfg.get("base_url"),
        "LLM_MODEL": project_llm.get("model") or taste_cfg.get("model"),
        "LLM_PROVIDER": project_llm.get("provider") or taste_cfg.get("provider"),
        "LLM_API_MODE": project_llm.get("api_mode") or taste_cfg.get("api_mode"),
    }
    for env_key, value in env_defaults.items():
        if value and not probe_env.get(env_key):
            probe_env[env_key] = str(value)
    cmd = [management_python(), str(SCRIPTS / "check_llm_ready.py"), "--project", project, "--live"]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=probe_env, text=True, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired as exc:
        stdout_tail = str(exc.stdout or "")[-2000:]
        stderr_tail = str(exc.stderr or "")[-2000:]
        summary = "LLM readiness probe timed out before full-cycle startup"
        return_code = 124
    else:
        stdout_tail = str(proc.stdout or "")[-2000:]
        stderr_tail = str(proc.stderr or "")[-2000:]
        return_code = int(proc.returncode or 0)
        summary = (stderr_tail or stdout_tail or "LLM readiness failed before full-cycle startup").strip().splitlines()[-1] if return_code else ""
    if return_code == 0:
        return None
    readiness_path = PROJECTS / project / "reports" / "llm_readiness.md"
    readiness = _read_json(PROJECTS / project / "reports" / "llm_readiness.json", {})
    if isinstance(readiness, dict) and readiness.get("live_error"):
        summary = str(readiness.get("live_error"))
    blocker = {
        "category": "literature_llm_not_ready",
        "title": "LLM API 未通过启动前检查",
        "summary": summary[:1000],
        "next_action": "在网页保存并验证可用 API key/base/model 后，再重新启动完整 Find/full-cycle。",
        "readiness_report": str(readiness_path),
        "return_code": return_code,
    }
    full = _read_json(state_dir / "full_research_cycle.json", {})
    if not isinstance(full, dict):
        full = {}
    preflight_job = {
        "project": project,
        "venue": str(payload.get("venue") or ""),
        "status": "blocked_literature_llm_not_ready",
        "process_alive": False,
        "alive": False,
        "kind": "full_cycle",
        "stage": "llm-readiness-preflight",
        "web_job_id": str(payload.get("web_job_id") or ""),
        "started_at": started_at,
        "updated_at": started_at,
        "cmd": " ".join(str(part) for part in cmd),
        "command": " ".join(str(part) for part in cmd),
    }
    full.update({
        "project": project,
        "venue": str(payload.get("venue") or ""),
        "status": "blocked_literature_llm_not_ready",
        "summary": blocker["summary"],
        "summary_zh": blocker["summary"],
        "current_goal": blocker["next_action"],
        "updated_at": started_at,
        "latest_step": {
            "stage": "llm-readiness-preflight",
            "status": "blocked",
            "return_code": return_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        },
        "full_cycle_job": preflight_job,
    })
    _write_project_state_json(state_dir / "full_research_cycle.json", full)
    _write_project_state_json(state_dir / "full_cycle_job.json", preflight_job)
    tick = _read_json(state_dir / "supervision_tick.json", {})
    if not isinstance(tick, dict):
        tick = {}
    tick.update({
        "project": project,
        "venue": str(payload.get("venue") or ""),
        "status": "blocked_literature_llm_not_ready",
        "generated_at": started_at,
        "blocker": blocker,
        "next_action": blocker["next_action"],
        "full_cycle_job": preflight_job,
    })
    _write_project_state_json(state_dir / "supervision_tick.json", tick)
    progress("blocked", 0, 1, "Full-cycle blocked before launch: LLM readiness failed")
    message = f"Full-cycle blocked before launch by LLM readiness: {blocker['summary']}"
    log(message)
    append_agent_log(project, agent_id, message)
    upsert_agent(project, agent_id, status="blocked", current_step="llm readiness preflight blocked full-cycle")
    return {
        "project": project,
        "action": payload.get("action"),
        "agent_id": agent_id,
        "returncode": return_code,
        "status": "blocked_literature_llm_not_ready",
        "blocker": blocker,
        "readiness_report": str(readiness_path),
    }


def _start_detached_full_cycle(project: str, cmd: list[str], env: dict[str, str], payload: dict[str, Any], agent_id: str, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    state_dir = PROJECTS / project / "state"
    logs_dir = PROJECTS / project / "logs" / "supervision"
    logs_dir.mkdir(parents=True, exist_ok=True)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    log_path = logs_dir / ("full_research_cycle_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S") + ".log")
    with log_path.open("a", encoding="utf-8", buffering=1) as handle:
        handle.write("Detached full-cycle command: " + " ".join(str(part) for part in cmd) + "\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            close_fds=True,
        )
    venue = str(payload.get("venue") or "").strip()
    command = " ".join(str(part) for part in cmd)
    web_job_id = str(payload.get("web_job_id") or "").strip()
    job = {
        "project": project,
        "venue": venue,
        "status": "running",
        "pid": proc.pid,
        "cmd": command,
        "command": command,
        "process_alive": True,
        "alive": True,
        "kind": "full_cycle",
        "web_job_id": web_job_id,
        "log_path": str(log_path),
        "started_at": started_at,
        "updated_at": started_at,
        "fresh_start": _truthy(payload.get("fresh_start")),
        "force_discovery": _truthy(payload.get("force_discovery")),
        "use_existing_literature_packet": _truthy(payload.get("use_existing_literature_packet")),
    }
    full_path = state_dir / "full_research_cycle.json"
    full = _read_json(full_path, {})
    if not isinstance(full, dict):
        full = {}
    full.update({
        "project": project,
        "venue": venue,
        "status": "running",
        "current_goal": "完整科研循环已通过网页/API 启动；参考复现已过，当前进入主线候选实验迭代，论文和结论提升继续由门控控制。",
        "summary": "完整科研循环正在运行；当前阶段由 project agent 自主推进当前主线候选实验。",
        "summary_zh": "完整科研循环正在运行；当前阶段由 project agent 自主推进当前主线候选实验。",
        "updated_at": started_at,
        "started_at": started_at,
        "full_cycle_job": job,
    })
    full.pop("finished_at", None)
    full.pop("completed_at", None)
    _write_project_state_json(full_path, full)
    _write_project_state_json(state_dir / "full_cycle_job.json", job)
    agents_path = state_dir / "agents.json"
    agents_state = _read_json(agents_path, {})
    if isinstance(agents_state, dict):
        agents = agents_state.get("agents") if isinstance(agents_state.get("agents"), list) else []
        for row in agents:
            if isinstance(row, dict) and row.get("id") == agent_id:
                row["log_tail"] = []
                row["queued_guidance"] = row.get("queued_guidance", []) if isinstance(row.get("queued_guidance"), list) else []
                row["created_at"] = started_at
                row.pop("finished_at", None)
                row.pop("result", None)
        agents_state["agents"] = agents
        agents_state["updated_at"] = started_at
        _write_project_state_json(agents_path, agents_state)
    tick = _read_json(state_dir / "supervision_tick.json", {})
    if not isinstance(tick, dict):
        tick = {}
    tick.update({
        "project": project,
        "venue": venue,
        "status": "running",
        "generated_at": started_at,
        "full_cycle_job": job,
        "next_action": "Detached full-cycle is running; web restarts must not cancel it.",
    })
    _write_project_state_json(state_dir / "supervision_tick.json", tick)
    upsert_agent(project, agent_id, pid=proc.pid, status="running", current_step="detached full-cycle running", extra={"log_path": str(log_path), "clelog_tail": True})
    append_agent_log(project, agent_id, f"Detached full-cycle started pid={proc.pid} log={log_path}")
    progress("running", 0, 0, f"Detached full-cycle running; PID={proc.pid}; log={log_path}")
    log(f"Detached full-cycle started; pid={proc.pid}; log={log_path}")
    return {"project": project, "action": payload.get("action"), "agent_id": agent_id, "returncode": 0, "status": "running", "pid": proc.pid, "log_path": str(log_path), "web_job_id": web_job_id, "full_cycle_job": job}

def _post_action_refresh_research_gates(project: str, venue: str, env: dict[str, str], log: LogFn | None = None) -> list[dict[str, Any]]:
    """Refresh derived evidence gates after project-agent experiment/paper work.

    Claude may update low-level artifacts such as experiment_registry.json. The web
    status panels read deterministic derived gates, so refresh those gates here and
    never let a stale summary hide newly completed evidence.
    """
    venue = str(venue or "").strip()
    commands: list[list[str]] = [
        [management_python(), str(SCRIPTS / "audit_paper_evidence.py"), "--project", project],
        [management_python(), str(SCRIPTS / "audit_submission_readiness.py"), "--project", project],
        [management_python(), str(SCRIPTS / "build_aris_review_board.py"), "--project", project],
        [management_python(), str(SCRIPTS / "build_research_trajectory_system.py"), "--project", project, "--skip-helpers"],
    ]
    if venue:
        commands[0].extend(["--venue", venue])
        commands[1].extend(["--venue", venue])
        commands[3].extend(["--venue", venue])
    results: list[dict[str, Any]] = []
    for cmd in commands:
        name = Path(cmd[1]).name if len(cmd) > 1 else str(cmd[0])
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=180)
            result = {
                "script": name,
                "returncode": int(proc.returncode or 0),
                "stdout_tail": str(proc.stdout or "")[-1200:],
                "stderr_tail": str(proc.stderr or "")[-1200:],
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                "script": name,
                "returncode": 124,
                "stdout_tail": str(exc.stdout or "")[-1200:],
                "stderr_tail": str(exc.stderr or "")[-1200:],
                "timeout": True,
            }
        results.append(result)
        if log:
            status = "ok" if result.get("returncode") == 0 else "gated" if result.get("returncode") == 2 else f"rc={result.get('returncode')}"
            log(f"post-action gate refresh {name}: {status}")
            detail = (str(result.get("stderr_tail") or "") or str(result.get("stdout_tail") or "")).strip()
            if detail and result.get("returncode") != 0:
                log(detail[-500:])
    _PROJECT_SUMMARY_CACHE.clear()
    return results


def _requested_panel_stage(payload: dict[str, Any], action: str) -> tuple[str, str]:
    requested = str(payload.get("stage") or "").strip()
    raw = requested.lower().replace("_", "-")
    if raw in {"paperwrite", "paper-write", "paper-writing"} or raw.startswith("paper") or "writing" in raw:
        return requested, "paper"
    if raw in {"environment", "env"} or raw.startswith("environment") or "repo-env" in raw:
        return requested, "environment"
    if raw in {"experiment", "autonomous", "trajectory"} or raw.startswith("experiment") or "trajectory" in raw:
        return requested, "experiment"
    if action == "paper":
        return requested, "paper"
    if action == "environment":
        return requested, "environment"
    if action in {"experiment", "autonomous", "full-cycle", "full_research_cycle"}:
        return requested, "experiment"
    return requested, ""


def run_action(payload: dict[str, Any], log: LogFn, should_cancel: CancelFn, progress: ProgressFn) -> dict[str, Any]:
    payload = _payload_with_project_config_venue(payload)
    project, cmd = build_command(payload)
    action = str(payload.get("action") or "action")
    if action == "paper-orchestra":
        action = "paper"
    requested_stage, panel_stage = _requested_panel_stage(payload, action)
    if action == "agent-guidance":
        target_agent_id = str(payload.get("agent_id") or "main")
        agent_id = f"guidance_{uuid4().hex[:8]}"
    else:
        target_agent_id = ""
        agent_id = str(payload.get("agent_id") or ("main" if action in {"autonomous", "full-cycle", "full_research_cycle", "literature-base-audit", "literature_base_audit", "experiment", "environment", "claude-message"} else f"{action}_{uuid4().hex[:8]}"))
    role = "main" if agent_id == "main" or action in {"autonomous", "full-cycle", "full_research_cycle", "literature-base-audit", "literature_base_audit", "experiment", "environment", "paper"} else "worker"
    goal = str(payload.get("message") or payload.get("prompt") or payload.get("topic") or action)
    upsert_agent(
        project,
        agent_id,
        name="主控 Agent" if role == "main" else f"TASTE {action}",
        role=role,
        stage=action,
        status="running",
        goal=goal[:500],
        command=cmd,
        current_step=f"preparing {action}",
        parent_id=target_agent_id if action == "agent-guidance" else "",
    )
    progress("prepare", 0, 1, f"Preparing research action for {project}")
    log("Workflow command: " + " ".join(cmd))
    append_agent_log(project, agent_id, "Workflow command: " + " ".join(cmd))
    env = interactive_env(project)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["PROJECT_ID"] = project
    env["PROJECT_CONFIG"] = str(PROJECTS / project / "project.json")
    log("Runtime PATH head: " + " | ".join(env.get("PATH", "").split(os.pathsep)[:6]))
    append_agent_log(project, agent_id, "Runtime PATH head: " + " | ".join(env.get("PATH", "").split(os.pathsep)[:6]))
    if action == "agent-guidance":
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30)
        output = (proc.stdout or proc.stderr or "").strip()
        if output:
            log(output)
            append_agent_log(project, agent_id, output)
        status = "done" if proc.returncode == 0 else "error"
        upsert_agent(project, agent_id, status=status, current_step="queued project-level human supervision guidance")
        progress("queued", 1, 1, output or "queued project-level human supervision guidance")
        return {"project": project, "action": payload.get("action"), "agent_id": agent_id, "target_agent_id": target_agent_id, "requested_stage": requested_stage, "panel_stage": panel_stage, "returncode": proc.returncode, "status": status, "guidance_receipt": output}
    if action in {"full-cycle", "full_research_cycle"}:
        readiness_block = _full_cycle_llm_readiness_block(project, env, payload, agent_id, log, progress)
        if readiness_block is not None:
            return readiness_block
        return _start_detached_full_cycle(project, cmd, env, payload, agent_id, log, progress)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    upsert_agent(project, agent_id, pid=proc.pid, status="running", current_step=f"running {action}")
    lines = 0
    progress("running", 0, 0, f"Running {payload.get('action')} for {project}")
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_cancel():
                log("Cancellation requested; terminating workflow subprocess.")
                append_agent_log(project, agent_id, "Cancellation requested; terminating workflow subprocess.")
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                except Exception:
                    proc.terminate()
                upsert_agent(project, agent_id, status="cancelled", current_step="cancelled by user")
                raise JobCancelled("research action cancelled by user.")
            text = line.rstrip()
            if text:
                lines += 1
                log(text)
                append_agent_log(project, agent_id, text)
                lowered = text.lower()
                phase = action if action in {'environment', 'experiment', 'paper', 'full-cycle', 'full_research_cycle'} else 'running'
                if action == 'environment':
                    phase = 'environment'
                elif action == 'experiment':
                    if 'coding_agent' in lowered or 'experiment' in lowered or 'trial' in lowered or 'metrics.json' in lowered or 'bad_cases.json' in lowered:
                        phase = 'experiment'
                    elif 'repo_env_bootstrap' in lowered or 'conda env' in lowered:
                        phase = 'environment'
                    else:
                        phase = 'experiment'
                elif action == 'paper':
                    phase = 'paper'
                elif action in {"literature-base-audit", "literature_base_audit"}:
                    phase = "literature-base-audit"
                elif action in {'full-cycle', 'full_research_cycle'}:
                    environment_literature_markers = [
                        'sync_outputs', 'sync-outputs', 'literature-sync', 'literature-tool-packet', 'build_literature_tool_packet',
                        'fresh-research-base-selection', 'select_fresh_research_base', 'assess_literature_base_candidates',
                        'literature-base-candidate', 'literature-base-audit', 'method-stack-sync',
                    ]
                    fresh_find_markers = ['literature-survey', 'run_frontend', 'run-finding', 'run_driver', 'run-literature-tool']
                    if any(marker in lowered for marker in fresh_find_markers):
                        phase = 'literature'
                    elif any(marker in lowered for marker in environment_literature_markers):
                        phase = 'environment'
                    elif 'paper-pipeline' in lowered or 'paper-preview' in lowered or 'paper-figure' in lowered or 'conference-preview' in lowered or 'latex' in lowered or 'submission-readiness' in lowered:
                        phase = 'paper'
                    elif 'reference-reproduction' in lowered or 'experiment' in lowered or 'trial' in lowered or 'trajectory' in lowered:
                        phase = 'experiment'
                    elif 'repo_env_bootstrap' in lowered or 'conda' in lowered or 'environment' in lowered:
                        phase = 'environment'
                    else:
                        phase = 'full-cycle'
                else:
                    if 'repo_env_bootstrap' in lowered or 'conda' in lowered or 'environment' in lowered:
                        phase = 'environment'
                    elif 'coding_agent' in lowered or 'experiment' in lowered or 'trial' in lowered:
                        phase = 'experiment'
                    elif 'latex' in lowered or 'template' in lowered or '/paper/' in lowered:
                        phase = 'paper'
                progress_text = _progress_text(text)
                if progress_text and (lines % 5 == 0 or phase != "running"):
                    progress(phase, lines, 0, progress_text)
                    upsert_agent(project, agent_id, status="running", current_step=progress_text[:240])
        code = proc.wait()
        if should_cancel():
            upsert_agent(project, agent_id, status="cancelled", current_step="cancelled by user")
            raise JobCancelled("research action cancelled by user.")
    finally:
        if proc.poll() is None:
            proc.terminate()
    allowed_blocked_codes = {
        "literature-base-audit": {2},
        "literature_base_audit": {2},
        "full-cycle": {2},
        "full_research_cycle": {2},
        "environment": {2},
        "experiment": {2},
        "paper": {2},
        "current-find-selection": {2},
    }
    if code != 0 and code not in allowed_blocked_codes.get(action, set()):
        upsert_agent(project, agent_id, status="error", current_step=f"research action failed with exit code {code}")
        raise RuntimeError(f"research action failed with exit code {code}")
    if code in allowed_blocked_codes.get(action, set()):
        progress("blocked", 1, 1, f"TASTE {payload.get('action')} completed with unresolved evidence gate")
    else:
        progress("complete", 1, 1, f"TASTE {payload.get('action')} complete")
    post_refresh_results: list[dict[str, Any]] = []
    if action in {"experiment", "paper"} or (action == "claude-message" and panel_stage in {"experiment", "paper"}):
        progress(panel_stage or action, 1, 1, "刷新实验/论文派生审计状态")
        post_refresh_results = _post_action_refresh_research_gates(project, str(payload.get("venue") or project_target_venue(project) or ""), env, log)
    summary = project_summary(project)
    final_status = "done"
    final_step = f"TASTE {payload.get('action')} complete"
    project_state_root = PROJECTS / project
    paper_stage: dict[str, Any] = {}
    paper_result_fields: dict[str, Any] = {}
    if action == "paper" and isinstance(summary, dict):
        stages = summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
        candidate = stages.get("paper") if isinstance(stages.get("paper"), dict) else {}
        paper_stage = candidate if isinstance(candidate, dict) else {}
        paper_result_fields = _paper_stage_public_fields(paper_stage)
    if action in {"literature-base-audit", "literature_base_audit"}:
        literature_audit = _read_json(PROJECTS / project / "state" / "literature_base_audit.json", {})
        audit_status = str(literature_audit.get("status") or "") if isinstance(literature_audit, dict) else ""
        if audit_status and audit_status != "completed_selected_evidence_ready_base":
            final_status = "blocked"
            final_step = str(literature_audit.get("status") or literature_audit.get("selection_gate") or "fresh literature base audit still blocked")[:240]
    elif action == "paper" and paper_stage:
        final_step = _paper_stage_public_message(paper_stage)[:240]
        stage_status = str(paper_stage.get("status") or "").lower()
        if stage_status.startswith("blocked") or not paper_stage.get("conference_preview_ready"):
            final_status = "blocked"
            progress("blocked", 1, 1, final_step)
        else:
            progress("complete", 1, 1, final_step)
    elif action == "current-find-selection":
        current_plan = _read_json(PROJECTS / project / "state" / "current_find_research_plan.json", {})
        current_find_pipeline = _current_find_pipeline_summary(PROJECTS / project)
        selected_plan_id = str(current_plan.get("selected_plan_id") or current_find_pipeline.get("selected_plan_id") or "") if isinstance(current_plan, dict) else str(current_find_pipeline.get("selected_plan_id") or "")
        if selected_plan_id:
            final_status = "done"
            final_step = f"主控 Claude Code 已选择唯一执行计划：{selected_plan_id}"[:240]
            progress("complete", 1, 1, final_step)
        else:
            final_status = "blocked"
            pipeline_summary = str(current_find_pipeline.get("summary_zh") or "").strip() if isinstance(current_find_pipeline, dict) else ""
            next_action = str((current_plan.get("next_required_action") if isinstance(current_plan, dict) else "") or (current_find_pipeline.get("next_required_action") if isinstance(current_find_pipeline, dict) else "") or "仍缺少唯一 selected_plan_id")
            final_step = (pipeline_summary or next_action)[:240]
            progress("blocked", 1, 1, final_step)
    elif code in allowed_blocked_codes.get(action, set()):
        full_cycle = _read_json(PROJECTS / project / "state" / "full_research_cycle.json", {})
        reference_gate = _read_json(PROJECTS / project / "state" / "reference_reproduction_gate.json", {})
        final_status = "blocked"
        final_step = str(
            (full_cycle.get("current_goal") if isinstance(full_cycle, dict) else "")
            or (reference_gate.get("human_summary") if isinstance(reference_gate, dict) else "")
            or "TASTE stopped at an evidence gate"
        )[:240]
    if action in {"full-cycle", "full_research_cycle"}:
        cycle_state = summary.get("full_research_cycle", {}) if isinstance(summary, dict) else {}
        raw_cycle_state = _read_json(project_state_root / "state" / "full_research_cycle.json", {})
        cycle_status = str(
            (raw_cycle_state.get("status") if isinstance(raw_cycle_state, dict) else "")
            or (cycle_state.get("status") if isinstance(cycle_state, dict) else "")
        )
        if cycle_status and cycle_status not in {"completed", "done"}:
            final_status = "blocked" if cycle_status.startswith("blocked") or cycle_status in {"waiting_for_background_experiment"} else "done"
            final_step = str(
                (raw_cycle_state.get("current_goal") if isinstance(raw_cycle_state, dict) else "")
                or (cycle_state.get("current_goal") if isinstance(cycle_state, dict) else "")
                or (cycle_state.get("summary") if isinstance(cycle_state, dict) else "")
                or final_step
            )[:240]
            if final_status == "blocked":
                progress("blocked", 1, 1, final_step or f"TASTE {payload.get('action')} stopped at an evidence gate")
    upsert_agent(project, agent_id, status=final_status, current_step=final_step, extra={"finished_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
    result_payload = {"project": project, "action": payload.get("action"), "agent_id": agent_id, "requested_stage": requested_stage, "panel_stage": panel_stage, "returncode": code, "status": final_status, "summary": summary, "post_refresh": post_refresh_results}
    if action == "current-find-selection":
        current_plan_payload = _read_json(PROJECTS / project / "state" / "current_find_research_plan.json", {})
        current_pipeline_payload = _current_find_pipeline_summary(PROJECTS / project)
        current_message = str((current_pipeline_payload if isinstance(current_pipeline_payload, dict) else {}).get("summary_zh") or final_step or "")
        result_payload["current_find_research_plan"] = current_plan_payload if isinstance(current_plan_payload, dict) else {}
        result_payload["current_find_pipeline"] = current_pipeline_payload if isinstance(current_pipeline_payload, dict) else {}
        result_payload["blocker"] = {
            "category": "current_find_read_idea_plan_gate",
            "summary": current_message,
            "next_action": str((current_plan_payload if isinstance(current_plan_payload, dict) else {}).get("next_required_action") or (current_pipeline_payload if isinstance(current_pipeline_payload, dict) else {}).get("next_required_action") or ""),
        } if final_status == "blocked" else {}
    if paper_result_fields:
        result_payload.update(paper_result_fields)
        result_payload["paper_stage"] = paper_result_fields
    return result_payload
