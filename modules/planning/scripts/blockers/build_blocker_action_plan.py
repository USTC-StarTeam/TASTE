#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

def _repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir():
            return candidate
    return current.parents[3]


ROOT = _repo_root(Path(__file__).resolve())
FRAMEWORK_SCRIPTS = ROOT / "framework" / "scripts"
if str(FRAMEWORK_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_SCRIPTS))
from taste_pythonpath import ensure_taste_pythonpath, resolve_script_path, taste_pythonpath_string

ensure_taste_pythonpath(ROOT)

from project_paths import CLAUDE_SKILL_ROOT, build_paths, management_python


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def one_line(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def slug(value: str, limit: int = 72) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-").lower()
    return (text or "blocker")[:limit]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def skill_path(name: str, skills: dict[str, str]) -> str:
    return skills.get(name) or str(CLAUDE_SKILL_ROOT / name / "SKILL.md")


def load_skills() -> dict[str, str]:
    base = CLAUDE_SKILL_ROOT
    return {path.parent.name: str(path) for path in base.glob("*/SKILL.md")} if base.exists() else {}


def module_command(project: str, stage: str, action: str, *extra: str) -> str:
    parts = [
        management_python(),
        "framework/scripts/run_module.py",
        stage,
        "--action",
        action,
        "--project",
        project,
    ]
    parts.extend(str(item) for item in extra if str(item).strip())
    return " ".join(shlex.quote(str(part)) for part in parts)


PUBLIC_SCRIPT_ACTIONS: dict[tuple[str, str], str] = {
    ("environment", "select_fresh_research_base.py"): "fresh_base_selection",
    ("environment", "probe_fresh_base_data_acquisition.py"): "fresh_base_data_probe",
    ("environment", "run_safe_unblock.py"): "safe_unblock",
    ("experimenting", "audit_reference_reproduction.py"): "reference_reproduction",
    ("writing", "audit_paper_evidence.py"): "audit_evidence",
    ("writing", "audit_submission_readiness.py"): "submission_readiness",
    ("writing", "audit_paper_normality.py"): "audit_normality",
    ("writing", "audit_paper_figures.py"): "audit_figures",
    ("writing", "repair_paper_figures_loop.py"): "repair_figures",
    ("writing", "repair_paper_preview_loop.py"): "repair_preview",
    ("writing", "run_paper_pipeline.py"): "run",
    ("planning", "build_blocker_action_plan.py"): "blocker_action",
}


def current_find_refresh_command(project: str) -> str:
    parts = [
        management_python(),
        "framework/scripts/run_frontend.py",
        "--project",
        project,
        "--deep-survey",
    ]
    return " ".join(shlex.quote(str(part)) for part in parts)


def current_find_bridge_command(project: str, venue: str = "") -> str:
    extra = ["--venue", venue] if venue else []
    return module_command(project, "reading", "current_find_research_plan", *extra)


def command(project: str, script_or_venue: str, *extra: str) -> str:
    script_or_venue = str(script_or_venue or "").strip()
    if script_or_venue.endswith(".py") or not extra:
        script = script_or_venue
        tail = list(extra)
    else:
        venue = script_or_venue
        script = str(extra[0] or "").strip()
        tail = (["--venue", venue] if venue else []) + [str(item) for item in extra[1:]]
    script_path = resolve_script_path(script, ROOT)
    try:
        rel = script_path.relative_to(ROOT)
    except ValueError:
        rel = script_path
    rel_parts = rel.parts if isinstance(rel, Path) else ()
    if len(rel_parts) >= 4 and rel_parts[0] == "modules" and rel_parts[2] == "scripts" and str(rel_parts[-1]).endswith(".py"):
        stage = rel_parts[1]
        action = PUBLIC_SCRIPT_ACTIONS.get((stage, str(rel_parts[-1])), Path(rel_parts[-1]).stem)
        parts = [management_python(), "framework/scripts/run_module.py", stage, "--action", action, "--project", project]
    else:
        parts = [management_python(), str(rel), "--project", project]
    parts.extend(str(item) for item in tail if str(item).strip())
    return " ".join(parts)


def enabled_literature_source_count(selection: Any) -> int:
    if not isinstance(selection, dict):
        return 0
    count = len(selection.get("venue_ids") or []) if isinstance(selection.get("venue_ids"), list) else 0
    for key in [
        "include_arxiv",
        "include_biorxiv",
        "include_nature",
        "include_science",
        "include_huggingface",
        "include_github",
    ]:
        if selection.get(key):
            count += 1
    return count


def looks_like_llm_quota_blocker(value: Any) -> bool:
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


def latest_targeted_literature_tool_status(paths) -> dict[str, Any]:
    current_plan = load_json(paths.state / "current_find_research_plan.json", {})
    status = current_plan.get("targeted_search_tool_status") if isinstance(current_plan, dict) and isinstance(current_plan.get("targeted_search_tool_status"), dict) else {}
    progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    current_run = str(progress.get("run_id") or "").strip() if isinstance(progress, dict) else ""
    status_run = str(status.get("current_find_run_id") or status.get("run_id") or status.get("find_run_id") or "").strip() if isinstance(status, dict) else ""
    if current_run and status_run and status_run != current_run:
        status = {}
    latest = load_json(paths.state / "taste_targeted_queries.json", {})
    if isinstance(latest, dict):
        latest_status = str(latest.get("status") or "")
        latest_has_failure = bool(latest.get("failure_summary") or latest.get("return_codes"))
        latest_run = str(latest.get("current_find_run_id") or latest.get("run_id") or latest.get("find_run_id") or "").strip()
        if current_run and latest_run and latest_run != current_run:
            return status
        if latest_has_failure or latest_status.startswith("failed"):
            merged = dict(status)
            for key in ["status", "venue", "packet_return_code", "return_codes", "failure_summary", "guardrail", "record_only_requested", "new_find_allowed", "current_find_run_id"]:
                if key in latest:
                    merged[key] = latest.get(key)
            status = merged
    return status


def literature_recommendation_gate_status(paths) -> dict[str, Any]:
    progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    frontend = load_json(paths.state / "finding_frontend.json", {})

    def payload_run_id(payload: Any) -> str:
        return str(payload.get("run_id") or payload.get("source_run_id") or payload.get("find_run_id") or "").strip() if isinstance(payload, dict) else ""

    current_run_id = payload_run_id(progress) or payload_run_id(frontend) or payload_run_id(packet)

    def same_current(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        source_run_id = payload_run_id(payload)
        return bool(not current_run_id or not source_run_id or source_run_id == current_run_id)

    sources = [item for item in [progress, frontend, packet] if isinstance(item, dict) and same_current(item)]
    selection: dict[str, Any] = {}
    for item in sources:
        candidate = item.get("selection")
        if isinstance(candidate, dict) and candidate:
            selection = candidate
            break

    progress_status = str(progress.get("status") or progress.get("phase") or "").lower() if isinstance(progress, dict) else ""
    targeted_tool_status = latest_targeted_literature_tool_status(paths)
    blocked_reason = str(
        (progress.get("blocked_reason") or progress.get("error") or "") if isinstance(progress, dict) else ""
    ) or str(targeted_tool_status.get("failure_summary") or targeted_tool_status.get("error") or "")
    llm_blocked = (
        "blocked_llm" in progress_status
        or "quota" in progress_status
        or looks_like_llm_quota_blocker(blocked_reason)
        or looks_like_llm_quota_blocker(targeted_tool_status)
    )

    packet = packet if same_current(packet) else {}
    packet_summary = packet.get("summary", {}) if isinstance(packet.get("summary"), dict) else {}
    packet_layer = packet.get("candidate_layer_summary", {}) if isinstance(packet.get("candidate_layer_summary"), dict) else {}
    packet_counts = packet_layer.get("pool_counts", {}) if isinstance(packet_layer.get("pool_counts"), dict) else {}

    progress_actual = safe_int(progress.get("strong_recommendation_count"), 0) if isinstance(progress, dict) else 0
    actual = progress_actual if llm_blocked or current_run_id else max(
        progress_actual,
        safe_int(packet_summary.get("strong_paper_anchors"), 0),
        safe_int(packet_counts.get("strong_papers"), 0),
        safe_int(packet_counts.get("claim_ready_strong_papers"), 0),
        safe_int(packet_counts.get("strong_recommendations"), 0),
    )
    source_count = enabled_literature_source_count(selection)
    target = max(
        safe_int(progress.get("recommendation_target_count"), 0) if isinstance(progress, dict) else 0,
        safe_int(packet_summary.get("recommendation_target_count"), 0),
        safe_int(packet_counts.get("recommendation_target_count"), 0),
    ) or (source_count * 5 if source_count else 0)
    progress_shortfall = safe_int(progress.get("recommendation_shortfall"), 0) if isinstance(progress, dict) else 0
    shortfall = progress_shortfall if llm_blocked or current_run_id else max(
        progress_shortfall,
        safe_int(packet_summary.get("recommendation_shortfall"), 0),
        safe_int(packet_counts.get("recommendation_shortfall"), 0),
        max(0, target - actual) if target else 0,
    )
    return {
        "run_id": current_run_id,
        "actual": actual,
        "target": target,
        "shortfall": shortfall,
        "source_count": source_count,
        "selection": selection,
        "blocking": bool(target and shortfall > 0),
        "llm_blocked": llm_blocked,
        "blocked_reason": blocked_reason,
    }

def fresh_base_data_required(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    if str(plan.get("status") or "") == "blocked_fresh_base_data_required":
        return True
    for key in ["fresh_base_data_acquisition", "data_acquisition"]:
        data = plan.get(key)
        if isinstance(data, dict) and str(data.get("decision") or "") == "blocked_external_data_required":
            return True
    blocked = plan.get("blocked_datasets", [])
    blockers = plan.get("blocker_reasons", []) if isinstance(plan.get("blocker_reasons"), list) else []
    joined = "\n".join(str(item).lower() for item in blockers)
    return bool(blocked) and any(term in joined for term in ["dataset", "loader", "google drive", "required file", "required_files", "dataset_contract", "missing_required_files"])



def fresh_base_state_names(paths, suffix: str) -> list[str]:
    names = [f"fresh_base_{suffix}.json"]
    index = load_json(paths.state / "fresh_base_reference_reproduction_index.json", {})
    if isinstance(index, dict):
        for row in index.get("entries", []) if isinstance(index.get("entries", []), list) else []:
            if not isinstance(row, dict):
                continue
            value = str(row.get("state_audit_path") or "").strip()
            if value and value.endswith(f"{suffix}.json"):
                names.append(Path(value).name)
    return list(dict.fromkeys(names))


def _current_impl_repo_path(paths) -> str:
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if isinstance(selection, dict):
        selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
        gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(selected.get(key) or "").strip()
                if value:
                    return value
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(active.get(key) or "").strip()
                if value:
                    return value
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def _artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = _current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path", "path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def fresh_base_loader_contract_passed(paths) -> bool:
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    real_probe = load_json(paths.state / "real_dataset_probe.json", {})
    if isinstance(impl, dict) and impl.get("status") == "implementation_ready_for_reference_probe":
        ready = impl.get("ready_datasets", []) if isinstance(impl.get("ready_datasets"), list) else []
        blockers = impl.get("blocker_reasons", []) if isinstance(impl.get("blocker_reasons"), list) else []
        repo = impl.get("repo", {}) if isinstance(impl.get("repo"), dict) else {}
        repo_path = str(repo.get("repo_path") or "").strip()
        probe_repo_path = str(real_probe.get("repo_path") or "").strip() if isinstance(real_probe, dict) else ""
        probe_rows = real_probe.get("probes", []) if isinstance(real_probe, dict) and isinstance(real_probe.get("probes"), list) else []
        has_loader_success = any(isinstance(row, dict) and row.get("claim_ready") and row.get("loader_probe_success") for row in probe_rows)
        if ready and not blockers and (not repo_path or not probe_repo_path or repo_path == probe_repo_path) and has_loader_success:
            return True
    data = load_json(paths.state / "fresh_base_data_acquisition.json", {})
    loader = load_json(paths.state / "real_dataset_probe.json", {})
    if not isinstance(data, dict) or not isinstance(loader, dict):
        return False
    ready = loader.get("ready_datasets", []) if isinstance(loader.get("ready_datasets"), list) else []
    return bool(
        ready
        and data.get("status") == "ready"
        and data.get("decision") == "ready_for_loader_probe"
        and loader.get("decision") == "loader_contract_passed"
    )


def fresh_base_reference_protocol_payload(paths) -> dict[str, Any]:
    for name in fresh_base_state_names(paths, "reference_protocol_probe"):
        payload = load_json(paths.state / name, {})
        if _artifact_matches_current_repo(paths, payload):
            return payload
    return {}


def fresh_base_reference_smoke_payload(paths) -> dict[str, Any]:
    for name in fresh_base_state_names(paths, "reference_smoke"):
        payload = load_json(paths.state / name, {})
        if _artifact_matches_current_repo(paths, payload):
            return payload
    return {}


def fresh_base_reference_protocol_passed(paths) -> bool:
    probe = fresh_base_reference_protocol_payload(paths)
    return bool(
        isinstance(probe, dict)
        and probe.get("status") == "reference_protocol_probe_passed"
        and probe.get("decision") == "ready_for_bounded_reference_smoke"
    )


def fresh_base_reference_smoke_passed(paths) -> bool:
    smoke = fresh_base_reference_smoke_payload(paths)
    return bool(
        isinstance(smoke, dict)
        and smoke.get("status") == "reference_smoke_passed"
        and smoke.get("decision") == "ready_for_reference_reproduction_audit"
    )


def fresh_base_reference_smoke_required(paths) -> bool:
    return fresh_base_reference_protocol_passed(paths) and not fresh_base_reference_smoke_passed(paths)

def classify(text: str, check_id: str = "") -> str:
    hay = f"{check_id} {text}".lower()
    if "framework_content_coupling" in hay or "framework_content_coupling" in hay or "framework code must not hard-code" in hay:
        return "framework_content_coupling"
    if "obsolete_baseline_cleanup" in hay or "do_not_delete_or_archive_project_files" in hay:
        return "obsolete_baseline_cleanup"
    if (
        "base_switch_gate" in hay
        or "base-switch gate未授权" in hay
        or "base switch gate" in hay
        or "deterministic base-switch gate" in hay
        or "base-switch gate did not authorize" in hay
        or "state/base_switch_gate.json" in hay
    ):
        return "base_switch_gate"
    if (
        "selected_base_viability_gate" in hay
        or "base_switch_gate_required" in hay
        or "blind experiment cycling" in hay
    ):
        return "selected_base_viability_gate"
    if (
        "blocked before mandatory llm abstract scoring" in hay
        or "llm api" in hay and ("quota" in hay or "额度" in hay or "429" in hay)
        or "blocked_llm_quota_exhausted" in hay
        or "literature_llm_quota_exhausted" in hay
    ):
        return "literature_llm_quota_exhausted"
    if (
        "literature_strong_recommendation_gate" in hay
        or "strong_recommendations=" in hay
        or "strong recommendations are below" in hay
        or "recommendation_shortfall" in hay
        or "recommendation shortfall" in hay
        or ("strong recommendation" in hay and "shortfall" in hay)
        or ("强推荐" in hay and ("短缺" in hay or "不足" in hay))
    ):
        return "literature_recommendation_gate"
    if "reviewer nomination" in hay or "easychair" in hay:
        return "manual_submission_action"
    if (
        "literature_base_audit_required" in hay
        or "fresh find produced literature base candidates" in hay
        or "fresh find base candidates" in hay
        or "fresh literature base candidates" in hay
        or "fresh_find_base_candidates_not_audited" in hay
        or "pending_fresh_literature_base_audit" in hay
    ):
        return "fresh_literature_base_audit"
    if (
        "fresh_base_reference_reproduction_required" in hay
        or "paper-level reference reproduction audit" in hay
        or "reference reproduction audit" in hay
    ):
        return "fresh_base_reference_reproduction"
    if (
        "fresh_base_reference_smoke_required" in hay
        or "bounded reference smoke" in hay
        or "reference smoke" in hay
        or "bounded no-training" in hay
    ):
        return "fresh_base_reference_smoke"
    if (
        "fresh_base_reference_probe_required" in hay
        or "reference protocol" in hay
        or "reference-protocol" in hay
        or "environment manifest" in hay
        or "env manifest" in hay
        or "ready datasets=" in hay
    ):
        return "fresh_base_reference_probe"
    if (
        "blocked_fresh_base_data_required" in hay
        or "fresh_base_data_required" in hay
        or "official data/loader" in hay
        or "official data files" in hay
        or "loader contract" in hay
        or "dataset_contract" in hay
        or "missing_required_files" in hay
        or "required file" in hay
        or "official data" in hay
    ):
        return "fresh_base_data_contract"
    if (
        "fresh_base_implementation_required" in hay
        or "fresh_find_paper_base_requires_code_or_implementation" in hay
        or "implementation route is not evidence-ready" in hay
        or "fresh paper base" in hay
        or "environment-stage claude code selected a paper/method anchor" in hay
        or "代码/实现/数据协议" in hay
    ):
        return "fresh_base_implementation"
    if (
        "no_viable_base_switch_route" in hay
        or "base-switch search is exhausted" in hay
        or "no evidence-ready alternative" in hay
        or "base switch exhausted" in hay
    ):
        return "terminal_reference_base_block"
    if (
        "switch_base" in hay
        or "switch base" in hay
        or "paper_target_incomparable" in hay
        or "data split" in hay and "incomparable" in hay
        or "repo/literature backtracking" in hay
        or "换基底" in hay
    ):
        return "reference_base_switch"
    if (
        "reference_reproduction" in hay
        or "reference reproduction" in hay
        or "repair_reproduction" in hay
        or "reproduce" in hay
        or "reproduction gate" in hay
        or "paper/table target evidence" in hay
        or "paper target" in hay
        or "论文目标" in hay
    ):
        return "reference_reproduction_repair"
    if "iteration trajectory" in hay or "experiment_iteration" in hay or "loss" in hay or "reflection" in hay:
        return "experiment_trajectory_repair"
    if "samefileerror" in hay:
        return "recoverable_runtime_exception"
    if "body_pages" in hay or "body page" in hay or "body_page" in hay:
        return "venue_body_page_policy"
    if "reference_page" in hay or "estimated_reference_pages" in hay:
        return "venue_reference_page_policy"
    if "citation" in hay or "references" in hay:
        return "citation_coverage"
    if "paper_normality" in hay or "venue_hard" in hay or "format" in hay or "acmart" in hay or "sigconf" in hay:
        return "paper_venue_normality"
    if "figure" in hay:
        return "paper_figure_quality"
    if "paper_orchestra" in hay or "blocked_sections" in hay or "section" in hay:
        return "paper_section_state"
    if "reference_reproduction_gate_pass" in hay:
        if "fresh find selected" in hay or "fresh paper base" in hay or "fresh base" in hay:
            return "fresh_base_implementation"
        return "reference_reproduction_repair"
    if (
        "hold-markdown-only" in hay
        or "paper_evidence_audit" in hay
        or "real_audit_ready" in hay
        or "real-data" in hay
        or "real data" in hay
        or "scientific progress gate" in hay
        or "scientific_progress_gate" in hay
        or "does not beat" in hay
        or "control" in hay
        or "ablation" in hay
        or "baseline" in hay
        or "ndcg" in hay
        or "metric" in hay
        or "evaluation pipeline" in hay
        or "runtime integrity" in hay
        or "shared worktree" in hay
        or "contaminated" in hay
        or "isolated worktree" in hay
    ):
        return "experiment_evidence_repair"
    if "evidence" in hay or "claim" in hay or "assurance" in hay or "unsupported" in hay or "weak" in hay:
        return "evidence_or_claim_assurance"
    if "experiment" in hay or "hypothesis" in hay or "metric" in hay or "bad-case" in hay or "counterexample" in hay:
        return "experiment_trajectory_repair"
    if "repo" in hay or "dataset" in hay or "data" in hay or "conda" in hay or "env" in hay:
        return "repo_data_env_repair"
    if "pdf did not change" in hay or "pdf" in hay:
        return "paper_artifact_refresh"
    return "general_blocker"


def _current_route_tokens(paths) -> set[str]:
    tokens: set[str] = set()
    for rel in ["evidence_ready_repo_selection.json", "active_repo.json", "fresh_base_implementation_plan.json"]:
        payload = load_json(paths.state / rel, {})
        candidates = [payload] if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            for key in ["selected", "repo", "active_repo"]:
                if isinstance(payload.get(key), dict):
                    candidates.append(payload[key])
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ["name", "repo", "repo_name", "repo_path", "local_path", "url"]:
                value = str(item.get(key) or "").strip().lower()
                if value:
                    tokens.add(value)
                    tokens.add(value.rsplit("/", 1)[-1])
                    tokens.add(value.rsplit("/", 1)[-1].replace("_", "-"))
                    tokens.add(value.rsplit("/", 1)[-1].replace("-", "_"))
    return {token for token in tokens if len(token) >= 4}


def _mentions_non_current_route(paths, text: Any) -> bool:
    raw = str(text or "").lower()
    if not raw:
        return False
    current_tokens = _current_route_tokens(paths)
    for marker in ["legacy/control", "historical route", "alternative route", "base switch", "not the main route"]:
        if marker in raw:
            return True
    candidate_markers = ["repo", "github.com", "train.py --data", "reference smoke", "reference reproduction"]
    if any(marker in raw for marker in candidate_markers) and current_tokens and not any(token and token in raw for token in current_tokens):
        return True
    return False


def _pid_cwd(pid: Any) -> str:
    try:
        value = int(pid)
    except Exception:
        return ""
    if value <= 0:
        return ""
    try:
        return str(Path(f"/proc/{value}/cwd").resolve())
    except Exception:
        return ""


def _process_belongs_to_project(paths, project: str, pid: Any, cmd: Any) -> bool:
    raw = str(cmd or "").lower()
    cwd = _pid_cwd(pid).lower()
    root_text = str(ROOT).lower()
    project_root = str(getattr(paths, "root", "") or "").lower()
    project_token = str(project or "").strip().lower()
    project_path_token = f"/projects/{project_token}/" if project_token else ""
    owned_needles = [root_text, project_root, project_path_token]
    if project_token:
        owned_needles.append(f"--project {project_token}")
        owned_needles.append(f"--project={project_token}")
    if any(needle and needle in raw for needle in owned_needles):
        return True
    if cwd:
        for base in [project_root, root_text]:
            if base and (cwd == base or cwd.startswith(base.rstrip("/") + "/")):
                return True
    return False


def _cmd_launches_alternative_route(paths, project: str, pid: Any, cmd: Any) -> bool:
    raw = str(cmd or "").lower()
    if not raw or not any(marker in raw for marker in ["python", "train", "main.py", "finetune", "--data"]):
        return False
    if not _process_belongs_to_project(paths, project, pid, cmd):
        return False
    current_tokens = _current_route_tokens(paths)
    if not current_tokens:
        return False
    if any(token and token in raw for token in current_tokens):
        return False
    return any(marker in raw for marker in ["/repos/selected/", "github.com", "train.py", "main.py", "finetune"])


def action_template(kind: str, project: str, venue: str, skills: dict[str, str]) -> dict[str, Any]:
    if kind == "framework_content_coupling":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("evidence-gate", skills),
            "route": "framework_content_coupling",
            "repair_strategy": (
                "framework/control code must be project-content agnostic. Move content-specific entrypoints or control branches into the project workspace, "
                "or replace them with state-driven generic adapters. Do not delete project-local scientific evidence manually."
            ),
            "recommended_commands": [
                command(project, venue, "audit_framework_content_coupling.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/framework_content_coupling_audit.json status=pass, or remaining findings are explicitly project-local and outside framework roots.",
                "No framework script/control branch is named after or branches on a project-specific paper, repo, method, dataset, or route.",
            ],
        }
    if kind == "obsolete_baseline_cleanup":
        return {
            "priority": "P1",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("evidence-gate", skills),
            "route": "obsolete_baseline_cleanup",
            "repair_strategy": (
                "Baseline/route cleanup is a project-context decision owned by the project Claude Code session. "
                "TASTE may enumerate candidates and enforce protected paths, but it must not infer deletion from names alone. "
                "Project Claude Code must inspect current-route evidence and candidate paths, then either write state/obsolete_baseline_cleanup_review.json with reviewed_candidate_count and candidate_fingerprint when keeping files, or write state/obsolete_baseline_cleanup_authorization.json with exact approved paths. If cleanup is authorized, project Claude Code must execute it itself and write state/obsolete_baseline_cleanup_execution.json; TASTE only audits the receipt."
            ),
            "recommended_commands": [
                command(project, "", "audit_obsolete_baseline_cleanup.py"),
                "Queue project Claude Code guidance: inspect state/obsolete_baseline_cleanup_plan.json plus current-route evidence. If files must be kept, write state/obsolete_baseline_cleanup_review.json with cleanup_authorized=false, current_route_reviewed=true, protected_current_route=true, reviewed_candidate_count, candidate_fingerprint copied from the plan, and rationale. If cleanup is scientifically/project-operationally required, write state/obsolete_baseline_cleanup_authorization.json with status=authorized_by_project_claude_review, cleanup_authorized=true, current_route_reviewed=true, protected_current_route=true, approved_candidate_paths, protected_paths, and rationale.",
                command(project, "", "audit_obsolete_baseline_cleanup.py"),
                "After audit reports pending_project_claude_cleanup_execution, queue project Claude Code cleanup execution guidance. Project Claude Code must touch only approved exact paths, protect current-route/shared evidence, and write state/obsolete_baseline_cleanup_execution.json with status=completed_by_project_claude, cleanup_executed=true, applied_paths, remaining_candidate_paths, protected_paths, and rationale. The workflow must not archive/delete project files itself.",
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "project_claude_required_output": {
                "path": "state/obsolete_baseline_cleanup_authorization.json",
                "required_fields": [
                    "status=authorized_by_project_claude_review",
                    "cleanup_authorized=true",
                    "current_route_reviewed=true",
                    "protected_current_route=true",
                    "approved_candidate_paths",
                    "protected_paths",
                    "rationale"
                ],
                "policy": "Without this project-scoped Claude Code authorization, The workflow must keep cleanup blocked and must not archive or delete project files."
            },
            "success_checks": [
                "state/obsolete_baseline_cleanup_plan.json is reviewed_no_cleanup_required only when the project Claude review fingerprint matches the current candidate set, or cleanup_authorized=true only when exact candidate paths are approved.",
                "If authorization is present, project Claude Code executes cleanup itself for only approved exact paths, writes state/obsolete_baseline_cleanup_execution.json, and TASTE audits that no current selected route or shared evidence was removed.",
                "No project files are deleted by name matching or by unaudited manual cleanup.",
            ],
        }
    if kind == "selected_base_viability_gate":
        return {
            "priority": "P1",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "selected_base_viability_gate",
            "repair_strategy": (
                "The selected-base full reference reproduction passed, but no current selected-repo candidate satisfying the project target evidence contract is audit-ready yet. "
                "If state/selected_base_viability_gate.json decision=base_switch_gate_required, pause candidate/alternative main-route launches, run the deterministic base-switch gate, and keep active_repo/evidence_ready_repo_selection unchanged until authorization executes. Current selected-base evidence repair may continue through launcher route_scope=selected_base_current_route; bounded candidate gate-evidence collection may continue through route_scope=base_switch_evidence_collection. Candidate routes remain proposal-only unless every provenance, loader/data, protocol, smoke, full-reference, and artifact-local audit check passes under the dedicated gate. "
                "If decision=continue_experiment_evidence_repair, current-route real-data experiment repair may continue only through launcher/audit contracts before any paper or claim promotion."
            ),
            "recommended_commands": [
                command(project, venue, "audit_selected_base_viability.py"),
                command(project, venue, "audit_deterministic_base_switch_gate.py"),
                command(project, "guard_selected_base_route.py"),
                command(project, venue, "audit_paper_evidence.py"),
                command(project, venue, "audit_submission_readiness.py"),
                command(project, venue, "build_blocker_action_plan.py"),
                "Do not POST /api/jobs/project to execute an alternative main route until a dedicated deterministic base-switch gate artifact explicitly passes loader/import, data contract, reference protocol, smoke, and full reproduction for that alternative.",
            ],
            "success_checks": [
                "active_repo/evidence_ready_repo_selection still point at the trusted current selected base.",
                "state/base_switch_execution.json is absent/proposal-only/not_executed before gate pass, or authorized_by_deterministic_base_switch_gate only after execute_authorized_base_switch.py runs.",
                "When decision=base_switch_gate_required, state/base_switch_gate.json is refreshed and remains not_authorized unless all deterministic candidate checks pass; current route stays authoritative until execute_authorized_base_switch.py. Candidate main-route launches stay blocked by state/experiment_launch_gate.json; bounded gate-evidence collection must declare route_scope=base_switch_evidence_collection and current-route repair must declare route_scope=selected_base_current_route.",
                "When decision=continue_experiment_evidence_repair, any current-route project-target candidate experiment is recorded with artifact-local audit, or the blocker remains truthful.",
                "state/scientific_progress_gate.json and state/submission_readiness.json remain blocked/hold-markdown-only until an authorized route has promotable evidence.",
            ],
        }
    if kind == "base_switch_gate":
        return {
            "priority": "P2",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "base_switch_gate",
            "repair_strategy": (
                "Keep alternative routes as non-authoritative proposals. This gate does not execute a route switch and must not edit active_repo/evidence_ready_repo_selection. "
                "When selected-base viability requires base-switch gating, refresh this deterministic gate and keep new experiment launches paused until the gate state changes."
            ),
            "recommended_commands": [
                command(project, venue, "audit_selected_base_viability.py"),
                command(project, venue, "audit_deterministic_base_switch_gate.py"),
                command(project, "guard_selected_base_route.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/base_switch_gate.json records candidate routes as blocked/not_authorized until all deterministic checks pass, then pass/authorize_base_switch.",
                "state/base_switch_execution.json is absent/proposal-only/not_executed before gate pass, or authorized_by_deterministic_base_switch_gate after controlled execution.",
                "active_repo.json and evidence_ready_repo_selection.json still point to the trusted selected-base.",
                "Candidate route provenance can be inspected now; bounded candidate gate-evidence collection may launch only with route_scope=base_switch_evidence_collection, while candidate main-route experiments remain blocked until authorization executes."
            ],
        }
    if kind == "literature_recommendation_gate":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("evidence-gate", skills),
            "route": "literature_recommendation_gate",
            "repair_strategy": (
                "The current Find packet is below the strict strong-recommendation target. "
                "Repair only the literature layer: inspect current Find artifacts, run controlled targeted follow-up Find when needed, "
                "rebuild/sync the literature packet and audits, and keep submission_ready=false. "
                "Do not promote weak papers, select/promote a base, run experiments, repair citations/figures, run the paper pipeline, or claim the paper is ready."
            ),
            "recommended_commands": [
                current_find_refresh_command(project),
                current_find_bridge_command(project, venue),
                command(project, venue, "audit_submission_readiness.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "planning/finding/find_progress.json reports strong_recommendation_count >= recommendation_target_count, or preserves the shortfall with exact source/query blockers.",
                "state/taste_targeted_queries.json records targeted queries when record-only mode is requested; otherwise the controlled targeted Find refreshes planning/finding outputs without promoting weak papers.",
                "state/submission_readiness.json keeps submission_ready=false while the literature shortfall remains.",
                "No base-selection, experiment, paper/citation/figure, paper-pipeline, or claim-promotion command is recommended or executed for this blocker.",
            ],
        }
    if kind == "fresh_literature_base_audit":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "fresh_literature_base_audit",
            "repair_strategy": (
                "Fresh Find has produced candidate base works that have not been repo/data/env audited. "
                "Do not continue any historical route as the main route and do not declare base-switch exhaustion. "
                "Resolve code/repo/data/protocol evidence for the top fresh literature candidates, then update "
                "evidence_ready_repo_selection.json or repo_selection_blocker.json with the fresh_find_run_id."
            ),
            "recommended_commands": [
                current_find_bridge_command(project, venue),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/literature_base_candidate_assessment.json no longer has status=blocked_pending_literature_base_audit, or records a completed fresh_find_run_id audit with exact rejected/accepted evidence.",
                "state/reference_reproduction_gate.json must not decide no_viable_base_switch_route while fresh_literature_base_audit_required=true.",
                "No historical-route main experiment is launched until the fresh base audit is complete.",
            ],
        }
    if kind == "fresh_base_data_contract":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "fresh_base_data_contract",
            "repair_strategy": (
                "Environment-stage Claude Code selected the current paper/method anchor, but its data files and loader contract are not evidence-ready. "
                "Do not run historical/control repos as the main route, do not launch training, and do not write/promote paper claims. "
                "Resolve the repo-specific real dataset files declared in dataset_contract.required_files_per_dataset and pass loader/import probes first."
            ),
            "recommended_commands": [
                command(project, "probe_fresh_base_data_acquisition.py", "--attempt-download", "--timeout-sec", "45"),
                module_command(project, "environment", "fresh_base_plan"),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/fresh_base_implementation_plan.json status becomes implementation_ready_for_reference_probe only after at least one real dataset satisfies the repo-specific dataset_contract.required_files_per_dataset.",
                "state/fresh_base_data_acquisition.json no longer has decision=blocked_external_data_required for all candidate datasets.",
                "No training, Claude implementation, paper writing, or claim promotion runs while blocked_fresh_base_data_required is active.",
            ],
        }
    if kind == "fresh_base_reference_probe":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "fresh_base_reference_probe",
            "repair_strategy": (
                "The selected base data and loader/import probes have passed, but the reference protocol and environment manifest are not audited yet. "
                "Do not run full training, historical/control main-route fallback, paper writing, or claim promotion. "
                "Create/record the minimal environment manifest and run bounded read-only reference-protocol probes for the ready selected-base datasets."
            ),
            "recommended_commands": [
                module_command(project, "environment", "fresh_base_plan"),
                command(project, "probe_selected_base_reference.py", "--mode", "protocol", "--timeout-sec", "240"),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "the selected-base loader contract probe remains passed with ready datasets.",
                "A minimal selected-base environment manifest/protocol probe is recorded before any training or claim promotion.",
                "Historical/control routes remain legacy/control only in active_repo.json and compact API.",
            ],
        }
    if kind == "fresh_base_reference_smoke":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "fresh_base_reference_smoke",
            "repair_strategy": (
                "The selected-base reference protocol/env manifest probes have passed, but the bounded no-training reference smoke has not passed yet. "
                "Run the selected-base reference smoke through TASTE safe-unblock. Do not run full training, historical/control main-route fallback, paper writing, or claim promotion."
            ),
            "recommended_commands": [
                command(project, "probe_selected_base_reference.py", "--mode", "protocol", "--timeout-sec", "240"),
                command(project, "probe_selected_base_reference.py", "--mode", "smoke", "--timeout-sec", "240"),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/fresh_base_reference_protocol_probe.json status=reference_protocol_probe_passed.",
                "state/fresh_base_reference_smoke.json status=reference_smoke_passed for the current selected repo.",
                "Paper writing and claim promotion remain blocked after smoke until reference reproduction and scientific progress gates pass.",
            ],
        }
    if kind == "fresh_base_reference_reproduction":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "fresh_base_reference_reproduction",
            "repair_strategy": (
                "The selected-base bounded smoke and bounded audit have established that data/loader/protocol are runnable. "
                "Next research action is a wrapper-managed full selected-base reference reproduction or a truthful compute/protocol failure record; do not repeat the bounded audit as the main next step."
            ),
            "recommended_commands": [
                command(project, "run_safe_unblock.py", "--venue", venue or "ICLR"),
                command(project, "run_selected_base_reference_reproduction_audit.py", "--mode", "full", "--epoch", "30", "--timeout-sec", "93600", "--execute"),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/fresh_base_reference_smoke.json remains status=reference_smoke_passed for the current selected repo.",
                "state/fresh_base_reference_full_reproduction_job.json records the running/completed wrapper-managed full job when launched by safe-unblock.",
                "state/fresh_base_reference_reproduction_audit.json mode=full records exact official command/config/log/hash/metrics before downstream gates can pass.",
                "reference_reproduction_gate remains blocked until paper-level reproduction evidence passes; historical routes are legacy/control only.",
            ],
        }
    if kind == "fresh_base_implementation":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "fresh_base_implementation",
            "repair_strategy": (
                "Environment-stage Claude Code selected a paper/method anchor, but no evidence-ready implementation route exists yet. "
                "Do not run any historical route as the main route and do not write/promote paper claims. "
                "Resolve official code/artifacts, or implement the smallest reproducible fresh-base route with real data/protocol evidence under TASTE audit."
            ),
            "recommended_commands": [
                current_find_bridge_command(project, venue),
                command(project, "select_fresh_research_base.py"),
                module_command(project, "environment", "fresh_base_plan"),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/fresh_research_base.json records the selected fresh paper base and implementation route status.",
                "state/fresh_base_implementation_plan.json records official/candidate code, entrypoints, dataset contract, local data gaps, and whether the fresh base is ready for reference probing.",
                "state/active_repo.json is not treated as the main route unless it matches the fresh base or the user explicitly confirms an imperfect legacy route.",
                "No paper writing or claim promotion runs before the fresh base has code/data/protocol evidence and reference reproduction gate passes.",
            ],
        }
    if kind == "terminal_reference_base_block":
        return {
            "priority": "P0",
            "autonomy": "manual_or_new_evidence_required",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "terminal_reference_base_block",
            "repair_strategy": (
                "Reference reproduction is still blocked and the evidence-ready base-switch search is exhausted. "
                "Do not keep cycling Claude through the same base-switch prompt, do not tune the blocked base as the main route, "
                "and do not write/promote paper claims. Continue only after new external evidence changes the search space: "
                "a compatible paper protocol/data split, stronger compute for paper-config reproduction, or a genuinely runnable alternative base."
            ),
            "recommended_commands": [
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "audit_paper_evidence.py"),
                command(project, venue, "audit_submission_readiness.py"),
                command(project, venue, "build_research_trajectory_system.py"),
            ],
            "success_checks": [
                "state/reference_reproduction_gate.json remains blocked unless the paper-level reproduction target is actually met.",
                "state/reference_reproduction_gate.json decision is no_viable_base_switch_route only when repo_selection_blocker documents zero evidence-ready alternatives.",
                "submission_readiness remains blocked and paper/claim promotion stays suppressed.",
            ],
        }
    if kind == "reference_base_switch":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_switch",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "reference_base_switch",
            "repair_strategy": (
                "The active reference work cannot be paper-protocol reproduced with the available repo/data split. "
                "Stop tuning this base as the main route, run evidence-ready repo/literature backtracking, "
                "and select a better base only if code, environment, loader-ready data, paper target, and compute budget are all auditable."
            ),
            "recommended_commands": [
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, "run_environment_stage.py", *((["--venue", venue] if venue else []) + ["--real-bootstrap-env", "--repo-search-rounds", "3", "--skip-reference-repair"])),
                command(project, venue, "build_research_trajectory_system.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/evidence_ready_repo_selection.json records a Claude-accepted evidence-ready/transformable route, or state/repo_selection_blocker.json records why no better base is available.",
                "The new active base has paper target evidence, runnable code/env, loader-ready real data, and a feasible reproduction plan before novel experiments continue.",
            ],
        }
    if kind == "reference_reproduction_repair":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_switch",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "reference_reproduction_repair",
            "repair_strategy": (
                "Before novel experiments or paper writing, reproduce the active reference work to its paper-level target, "
                "record the target source, runtime cost, logs and audit artifacts, then decide continue_base or switch_base. "
                "If the target is missing or compute is infeasible, perform repo/literature backtracking instead of tuning blindly."
            ),
            "recommended_commands": [
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_research_trajectory_system.py"),
                command(project, "run_autonomous_research.py", *((["--venue", venue] if venue else []) + ["--iterations", "1", "--execute-plan", "--prepare-env", "--real-bootstrap-env", "--skip-discovery", "--skip-paper", "--max-launches", "1"])),
                command(project, venue, "audit_reference_reproduction.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "state/reference_reproduction_gate.json is pass with decision=continue_base, or records decision=switch_base with evidence.",
                "The experiment registry contains an audit-ready reference reproduction with metrics, logs, runtime, and target-source evidence.",
            ],
        }
    if kind == "experiment_evidence_repair":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "experiment_evidence_repair",
            "repair_strategy": (
                "参考复现已通过；当前最高优先级是让 project agent 在当前主线下自主设计、运行并审计真实项目目标候选实验。"
                "没有 audit-ready 可推广候选前，论文/claim promotion 和自动切基底都保持阻塞。"
            ),
            "human_summary": (
                "参考复现已通过；当前缺少当前主线下可审计、可推广的项目目标候选实验。"
                "下一步由 project agent 继续真实实验迭代，论文/claim 暂停。"
            ),
            "recommended_commands": [
                command(
                    project,
                    "run_autonomous_research.py",
                    *((["--venue", venue] if venue else []) + [
                        "--iterations",
                        "1",
                        "--execute-plan",
                        "--prepare-env",
                        "--real-bootstrap-env",
                        "--skip-discovery",
                        "--skip-paper",
                        "--max-launches",
                        "1",
                    ]),
                ),
                command(project, venue, "run_research_trajectory_supervisor.py", "--rounds", "1"),
                command(project, venue, "audit_paper_evidence.py"),
                command(project, venue, "build_research_trajectory_system.py"),
                command(project, venue, "build_blocker_action_plan.py"),
            ],
            "success_checks": [
                "A new or repaired audit-ready real-data experiment entry is recorded, or a truthful prune/switch decision is recorded.",
                "reports/paper_evidence_audit.md no longer recommends hold-markdown-only for promoted claims.",
                "research_assurance_layer and research_evidence_manifest no longer block promoted scientific claims.",
            ],
        }
    if kind == "manual_submission_action":
        return {
            "priority": "P0",
            "autonomy": "manual_required",
            "skill_contract": "",
            "route": "human_submission_admin",
            "repair_strategy": "Keep the gate blocked until the required external submission action is recorded in state/submission_actions.json.",
            "recommended_commands": [command(project, venue, "audit_submission_readiness.py")],
            "success_checks": [
                "state/submission_actions.json records reviewer_nomination_done=true with timestamp/evidence.",
                "audit_submission_readiness no longer reports the reviewer nomination blocker.",
            ],
        }
    if kind in {"venue_body_page_policy", "venue_reference_page_policy", "paper_venue_normality", "paper_artifact_refresh"}:
        return {
            "priority": "P2",
            "autonomy": "autonomous",
            "skill_contract": skill_path("writing", skills),
            "route": "paper_production_repair",
            "repair_strategy": "Use writing to satisfy current venue hard requirements first, then compile and audit. Diagnose figure/table footprint and bibliography/reference-page footprint before reducing prose; do not invent empirical claims to fill pages.",
            "recommended_commands": [
                command(project, venue, "run_paper_pipeline.py", "--refresh-current-paper"),
                command(project, venue, "repair_paper_preview_loop.py", "--refresh-current-paper", "--max-rounds", "5"),
                command(project, venue, "audit_paper_normality.py"),
                command(project, venue, "audit_submission_readiness.py"),
            ],
            "success_checks": [
                "paper_normality_audit.status is pass for the selected venue policy.",
                "body/reference/total page checks satisfy the venue policy.",
                "The latest PDF hash changes or the gate clears with explicit evidence.",
            ],
        }
    if kind == "citation_coverage":
        return {
            "priority": "P1",
            "autonomy": "autonomous",
            "skill_contract": skill_path("writing", skills),
            "route": "citation_repair",
            "repair_strategy": "Repair verified citation metadata and revise coverage without padding irrelevant references.",
            "recommended_commands": [
                command(project, venue, "repair_paper_orchestra_citations.py"),
                command(project, venue, "revise_paper_citation_coverage.py"),
                command(project, venue, "audit_paper_normality.py"),
                command(project, venue, "audit_submission_readiness.py"),
            ],
            "success_checks": [
                "Verified citation candidates meet the venue/project threshold.",
                "Reference-page budget still passes after citation repair.",
            ],
        }
    if kind == "paper_figure_quality":
        return {
            "priority": "P1",
            "autonomy": "autonomous",
            "skill_contract": skill_path("writing", skills),
            "route": "figure_repair",
            "repair_strategy": "Redraw, resize, remove, or demote weak figures according to the figure audit; for page pressure, repair float/table footprint before changing prose; keep figures evidence-backed.",
            "recommended_commands": [
                command(project, venue, "repair_paper_figures_loop.py", "--max-rounds", "3"),
                command(project, venue, "audit_paper_figures.py"),
                command(project, venue, "repair_paper_preview_loop.py", "--refresh-current-paper", "--max-rounds", "3"),
            ],
            "success_checks": ["paper_figure_quality_audit.status is pass.", "No synthetic/probe visual is promoted as a main result."],
        }
    if kind == "paper_section_state":
        return {
            "priority": "P0",
            "autonomy": "autonomous",
            "skill_contract": skill_path("writing", skills),
            "route": "section_state_repair",
            "repair_strategy": "Repair blocked/revision sections from evidence ledgers, or explicitly narrow/downgrade unsupported claims.",
            "recommended_commands": [
                command(project, venue, "run_paper_orchestra_bridge.py", "--refresh-current-paper"),
                command(project, venue, "build_paper_orchestra_state.py"),
                command(project, venue, "audit_paper_orchestra.py"),
                command(project, venue, "audit_submission_readiness.py"),
            ],
            "success_checks": ["No paper section remains blocked without a concrete missing-resource reason.", "paper_orchestra_audit no longer holds the paper for section blockers."],
        }
    if kind == "evidence_or_claim_assurance":
        return {
            "priority": "P0",
            "autonomy": "autonomous_or_truthful_block",
            "skill_contract": skill_path("evidence-gate", skills),
            "route": "evidence_assurance_repair",
            "repair_strategy": "Produce local experiment/citation/artifact evidence for the exact claim, or keep/downgrade/prune the claim. Never weaken the gate.",
            "recommended_commands": [
                command(project, venue, "audit_paper_evidence.py"),
                command(project, venue, "build_research_trajectory_system.py"),
                command(project, venue, "run_research_trajectory_supervisor.py", "--rounds", "1"),
                command(project, venue, "audit_submission_readiness.py"),
            ],
            "success_checks": [
                "research_assurance_layer.status and research_evidence_manifest.status pass or contain only justified non-promoted blockers.",
                "weak_or_unsupported_claims is empty for promoted claims.",
            ],
        }
    if kind in {"experiment_trajectory_repair", "repo_data_env_repair"}:
        return {
            "priority": "P1" if kind == "experiment_trajectory_repair" else "P0",
            "autonomy": "autonomous_or_missing_resource",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "experiment_loop_repair",
            "repair_strategy": "Use the recoverable experiment loop: inspect repo/env/data, run the smallest evidence-producing trial, then repair/prune/switch with memory updates.",
            "recommended_commands": [
                command(project, "analyze_experiment_failures.py", "--all-failed"),
                command(project, venue, "run_research_trajectory_supervisor.py", "--rounds", "1"),
                command(project, venue, "build_research_trajectory_system.py"),
            ],
            "success_checks": [
                "A new audit-ready experiment entry or evidence-backed prune/switch decision is recorded.",
                "failed_hypothesis_graph, research_memory, and trajectory_checkpoints are updated.",
            ],
        }
    if kind == "recoverable_runtime_exception":
        return {
            "priority": "P0",
            "autonomy": "autonomous",
            "skill_contract": skill_path("experiment-loop", skills),
            "route": "pipeline_runtime_repair",
            "repair_strategy": "Fix the deterministic pipeline exception first, then rerun the failed stage and its audit.",
            "recommended_commands": [
                command(project, venue, "repair_paper_preview_loop.py", "--refresh-current-paper", "--max-rounds", "1"),
                command(project, venue, "audit_submission_readiness.py"),
            ],
            "success_checks": ["The same exception no longer appears in stage_failures.", "The failed stage returns 0 or a scientific gate blocker instead of a Python exception."],
        }
    return {
        "priority": "P2",
        "autonomy": "autonomous_or_truthful_block",
        "skill_contract": skill_path("evidence-gate", skills),
        "route": "general_blocker_triage",
        "repair_strategy": "Inspect the named evidence files, choose the smallest evidence-producing repair, then rerun the relevant audit.",
        "recommended_commands": [command(project, venue, "build_research_trajectory_system.py"), command(project, venue, "audit_submission_readiness.py")],
        "success_checks": ["The blocker disappears from its source audit or is preserved with a concrete missing-resource reason."],
    }


def canonical_issue(text: str) -> str:
    value = str(text or "").lower()
    value = re.sub(r"\b(name|id):\s*", "", value)
    value = re.sub(r"paper_normality_status=blocked;\s*", "", value)
    value = re.sub(r";?\s*venue_failed_checks=\d+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:180]


def add_action(actions: list[dict[str, Any]], seen: set[str], *, project: str, venue: str, skills: dict[str, str], source: str, issue: str, evidence: Any = None, check_id: str = "", severity: str = "block") -> None:
    issue_text = one_line(issue)
    if not issue_text:
        return
    kind = classify(issue_text, check_id)
    if kind == "obsolete_baseline_cleanup":
        cleanup_plan = load_json(build_paths(project).state / "obsolete_baseline_cleanup_plan.json", {})
        cleanup_status = str(cleanup_plan.get("status") or "") if isinstance(cleanup_plan, dict) else ""
        if cleanup_status in {"reviewed_no_cleanup_required", "project_cleanup_completed"}:
            return
    template = action_template(kind, project, venue, skills)
    key = f"{kind}:{canonical_issue(issue_text)}"
    if key in seen:
        return
    seen.add(key)
    action_id = f"{kind}-{len(actions) + 1:03d}-{slug(check_id or issue_text, 36)}"
    public_issue = str(template.get("human_summary") or issue_text)
    action = {
        "id": action_id,
        "source": source,
        "source_check_id": check_id,
        "category": kind,
        "severity": severity or "block",
        "issue": public_issue,
        "raw_issue": issue_text if public_issue != issue_text else "",
        "evidence": evidence if isinstance(evidence, list) else ([evidence] if evidence else []),
        **template,
    }
    # Warn-severity actions should not be P0 — downgrade to P1
    if action.get("severity") == "warn" and action.get("priority") in {"P0", "P1"}:
        action["priority"] = "P2"
    actions.append(action)


def stale_action_after_reference_pass(row: dict[str, Any], reference_gate_passed: bool) -> bool:
    if not reference_gate_passed or not isinstance(row, dict):
        return False
    hay = " ".join(str(row.get(key) or "") for key in ["route", "category", "source", "source_check_id", "issue", "repair_strategy"]).lower()
    stale_markers = [
        "paper-level full reference reproduction is still required",
        "full reference reproduction is still required",
        "bounded reference smoke/audit passed",
        "bounded reference smoke passed",
        "fresh_base_reference_reproduction_required",
        "reference-reproduction-gate",
        "reference_reproduction_gate",
        "selected-base-safe-unblock failed",
        "run wrapper-managed full reference reproduction",
    ]
    if any(marker in hay for marker in stale_markers):
        if "selected_base_viability_gate" in hay or "base_switch_gate_required" in hay:
            return False
        return True
    return False


def priority_key(row: dict[str, Any]) -> tuple[int, int, str]:
    pmap = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    amap = {"autonomous": 0, "autonomous_or_truthful_block": 1, "autonomous_or_missing_resource": 1, "observe_running_worker": 1, "manual_or_new_evidence_required": 2, "manual_required": 3}
    route_order = {
        "literature_recommendation_gate": 0,
        "fresh_literature_base_audit": 1,
        "fresh_base_data_contract": 0,
        "fresh_base_reference_probe": 0,
        "fresh_base_reference_smoke": 0,
        "fresh_base_reference_reproduction": 0,
        "fresh_base_implementation": 1,
        "terminal_reference_base_block": 2,
        "selected_base_viability_gate": 1,
        "base_switch_gate": 2,
        "reference_base_switch": 3,
        "reference_reproduction_repair": 3,
        "experiment_evidence_repair": 3,
        "experiment_loop_repair": 3,
        "evidence_assurance_repair": 4,
        "pipeline_runtime_repair": 5,
        "repo_data_env_repair": 6,
        "paper_production_repair": 7,
        "section_state_repair": 8,
        "figure_repair": 9,
        "citation_repair": 10,
        "human_submission_admin": 11,
    }
    return (
        pmap.get(str(row.get("priority") or "P9"), 9),
        route_order.get(str(row.get("route") or ""), 6),
        amap.get(str(row.get("autonomy") or ""), 2),
        str(row.get("id") or ""),
    )


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        import os
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def active_full_research_cycle_worker(paths, project: str) -> dict[str, Any]:
    tick = load_json(paths.state / "supervision_tick.json", {})
    for source in [tick.get("full_cycle_job") if isinstance(tick, dict) else {}, load_json(paths.state / "full_cycle_job.json", {})]:
        if not isinstance(source, dict):
            continue
        pid = source.get("pid")
        if str(source.get("status") or "").lower() == "running" and _pid_alive(pid):
            return {**source, "pid": pid, "process_alive": True}
    try:
        import subprocess
        proc = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,stat=,pcpu=,pmem=,cmd="], cwd=ROOT, text=True, capture_output=True, timeout=20)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        cmd = parts[6]
        if any(skip in cmd for skip in [" grep ", " rg ", " ps -", "sed -n"]):
            continue
        if _cmd_launches_alternative_route(paths, project, parts[0], cmd):
            return {"status": "running", "pid": parts[0], "ppid": parts[1], "elapsed_sec": int(parts[2]), "stat": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": cmd, "kind": "active_child_worker", "process_alive": True}
        if (
            project
            and project in cmd
            and _process_belongs_to_project(paths, project, parts[0], cmd)
            and ("run_full_research_cycle.py" in cmd or "run_paper_pipeline.py" in cmd or "claude_project_session.py" in cmd)
        ):
            return {"status": "running", "pid": parts[0], "ppid": parts[1], "elapsed_sec": int(parts[2]), "stat": parts[3], "pcpu": parts[4], "pmem": parts[5], "cmd": cmd, "kind": "project_worker", "process_alive": True}
    return {}


def live_worker_issue(worker: dict[str, Any]) -> str:
    pid = worker.get("pid", "")
    elapsed = worker.get("elapsed_sec") or worker.get("elapsed") or ""
    cmd = one_line(worker.get("cmd", ""), 420)
    return f"active_full_research_cycle_worker: 当前 TASTE/full research cycle worker 正在运行，PID={pid}" + (f"，已运行 {elapsed}s" if elapsed != "" else "") + (f"；cmd={cmd}" if cmd else "") + "。等待训练产出指标、审计文件和下一步门控结论；不要启动第二条 Find、pair_compare 或重复训练。"


def experiment_evidence_gate_blocked(paths) -> bool:
    evidence_audit = ""
    try:
        evidence_audit = (paths.reports / "paper_evidence_audit.md").read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        evidence_audit = ""
    submission = load_json(paths.state / "submission_readiness.json", {})
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    manifest = load_json(paths.state / "research_evidence_manifest.json", {})
    failed = submission.get("failed_checks", []) if isinstance(submission.get("failed_checks", []), list) else []
    evidence_failed = any(
        isinstance(row, dict)
        and str(row.get("id") or row.get("name") or "").lower()
        in {
            "evidence_gate_allows_template",
            "real_audit_ready_experiment",
            "scientific_progress_gate_pass",
            "claim_verdicts_available",
            "bad_cases_available",
            "counterexamples_available",
            "assurance_layer_pass",
            "experiment_runtime_integrity_pass",
        }
        and row.get("severity") == "block"
        for row in failed
    )
    return bool(
        "promotion_gate_recommendation: hold-markdown-only" in evidence_audit
        or "hold-markdown-only" in evidence_audit
        or evidence_failed
        or (isinstance(assurance, dict) and assurance.get("status") == "blocked")
        or (isinstance(manifest, dict) and manifest.get("status") == "blocked")
    )




def stale_full_cycle_blocker(paths, issue: str, category: str = "", severity: str = "") -> bool:
    """Drop obsolete full-cycle blocker snapshots after the live gates moved on.

    full_research_cycle.latest_blockers is a historical controller snapshot. The
    action plan should route current gates, not resurrect stale P0 blockers after
    scientific_progress_gate/submission/evidence audits have been refreshed.
    """
    issue_l = str(issue or "").lower()
    category_l = str(category or "").lower()
    severity_l = str(severity or "").lower()
    science = load_json(paths.state / "scientific_progress_gate.json", {})
    submission = load_json(paths.state / "submission_readiness.json", {})
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    manifest = load_json(paths.state / "research_evidence_manifest.json", {})
    try:
        evidence_audit = (paths.reports / "paper_evidence_audit.md").read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        evidence_audit = ""

    science_passed = isinstance(science, dict) and science.get("status") == "pass" and not science.get("blockers")
    submission_blockers = submission.get("blockers", []) if isinstance(submission, dict) and isinstance(submission.get("blockers", []), list) else []
    submission_failed = submission.get("failed_checks", []) if isinstance(submission, dict) and isinstance(submission.get("failed_checks", []), list) else []
    current_failed_block_ids = {
        str((row.get("id") or row.get("name") or "")).lower()
        for row in submission_failed
        if isinstance(row, dict) and str(row.get("severity") or row.get("status") or "").lower() == "block"
    }

    if deterministic_base_switch_executed(paths):
        stale_switch_markers = [
            "base_switch_gate_required",
            "invalid_unapproved_switch",
            "selected_base_viability_gate",
            "deterministic base-switch approval chain",
            "current selected repo",
            "selected-base viability gate blocked",
            "base_switch_execution status=invalid_unapproved_switch",
        ]
        stale_switch_categories = {
            "selected_base_viability_gate",
            "base_switch_execution",
            "base_switch_gate",
        }
        if category_l in stale_switch_categories or any(marker in issue_l for marker in stale_switch_markers):
            return True

    reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    reference_gate_passed = isinstance(reference_gate, dict) and reference_gate.get("status") == "pass" and reference_gate.get("decision") == "continue_base"
    if reference_gate_passed and (
        category_l in {"reference_reproduction_gate", "literature_to_base_route", "environment_anchor_selection_required"}
        or "current find has produced a base-work candidate pool" in issue_l
        or "environment-stage claude code anchor selection exists for this run" in issue_l
        or "previous active_repo/reference-reproduction state must not satisfy" in issue_l
        or "fresh literature survey is available, but the active reference base is still blocked" in issue_l
        or "active reference base is still blocked" in issue_l
        or "reference base is still blocked" in issue_l
        or "paper-level full reference reproduction is still required" in issue_l
        or "full reference reproduction is still required" in issue_l
        or "bounded reference smoke/audit passed" in issue_l
        or "bounded reference smoke passed" in issue_l
        or ("reference reproduction gate" in issue_l and "still required" in issue_l)
        or ("reference-reproduction-gate" in issue_l and "failed rc=2" in issue_l)
    ):
        return True
    if science_passed and (
        "no audit-ready real-data candidate/proposed-method run exists" in issue_l
        or "scientific_progress_gate blocked" in issue_l
        or "scientific_progress_gate_pass: status=blocked" in issue_l
        or ("scientific_progress_gate" in issue_l and "candidate={}" in issue_l)
    ):
        return True
    if "hold-markdown-only" in issue_l and "promotion_gate_recommendation: allow-template" in evidence_audit:
        return True
    if "evidence_gate_allows_template" in issue_l and "evidence_gate_allows_template" not in current_failed_block_ids:
        return True
    if "assurance_status=blocked" in issue_l and isinstance(assurance, dict) and assurance.get("status") in {"warn", "pass"}:
        return True
    if "evidence_manifest" in issue_l and isinstance(manifest, dict) and manifest.get("status") in {"warn", "pass"} and severity_l != "block":
        return True
    if category_l == "submission_readiness" and not submission_blockers and severity_l != "block":
        return True
    return False

def enforce_experiment_first(actions: list[dict[str, Any]], paths, project: str, venue: str, skills: dict[str, str]) -> None:
    if not experiment_evidence_gate_blocked(paths):
        return
    has_experiment_gate = any(row.get("route") == "experiment_evidence_repair" for row in actions)
    if not has_experiment_gate:
        add_action(
            actions,
            set(),
            project=project,
            venue=venue,
            skills=skills,
            source="reports/paper_evidence_audit.md",
            check_id="experiment_evidence_gate",
            issue="Experiment evidence gate is blocked; run real-data evaluation/baseline repair before paper production.",
            evidence=["reports/paper_evidence_audit.md", "state/experiment_registry.json", "state/scientific_progress_gate.json", "state/submission_readiness.json"],
            severity="block",
        )
    for row in actions:
        if row.get("route") in {"paper_production_repair", "section_state_repair", "figure_repair", "citation_repair"}:
            row["priority"] = "P2"
            row["blocked_by_experiment_evidence_gate"] = True
            row["repair_strategy"] = (
                "Deferred until the experiment evidence gate clears. Paper repair must not run before real-data "
                "evaluation/baseline evidence is audit-ready."
            )


def deterministic_base_switch_executed(paths) -> bool:
    gate = load_json(paths.state / "base_switch_gate.json", {})
    execution = load_json(paths.state / "base_switch_execution.json", {})
    return bool(
        isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and gate.get("switch_authorized") is True
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )


def failed_check_ids(gate: dict[str, Any]) -> list[str]:
    gate = gate if isinstance(gate, dict) else {}
    failed = gate.get("failed_checks") if isinstance(gate.get("failed_checks"), list) else []
    if not failed and isinstance(gate.get("checks"), list):
        failed = [row for row in gate.get("checks", []) if isinstance(row, dict) and row.get("status") != "pass"]
    return [str(row.get("id") or "").strip() for row in failed if isinstance(row, dict) and str(row.get("id") or "").strip()]


def failed_check_summary(ids: list[str]) -> str:
    labels = {
        "candidate_loader_import_probe_passed": "loader/import",
        "candidate_data_contract_passed": "data contract",
        "candidate_reference_protocol_passed": "reference protocol/env manifest",
        "candidate_reference_smoke_passed": "bounded reference smoke",
        "candidate_full_reference_reproduction_passed": "full reference reproduction",
        "candidate_artifact_local_audit_ready": "artifact-local audit",
        "candidate_route_proposal_exists": "candidate proposal",
        "candidate_find_run_provenance_clear": "Find/read provenance",
    }
    out = [labels.get(item, item) for item in ids if item]
    return ", ".join(out) or "candidate gate evidence"


def route_has_identity(route: dict[str, Any]) -> bool:
    route = route if isinstance(route, dict) else {}
    return any(str(route.get(key) or "").strip() for key in ["repo", "title", "repo_path", "proposed_path_hint"])


def failed_base_switch_gate_guidance(paths) -> dict[str, Any]:
    gate = load_json(paths.state / "base_switch_gate.json", {})
    if not (
        isinstance(gate, dict)
        and gate.get("status") == "blocked"
        and gate.get("decision") == "base_switch_not_authorized"
        and not deterministic_base_switch_executed(paths)
    ):
        return {}
    failed_ids = failed_check_ids(gate)
    missing_summary = failed_check_summary(failed_ids)
    candidate = gate.get("candidate_route") if isinstance(gate.get("candidate_route"), dict) else {}
    candidate_present = route_has_identity(candidate)
    missing_candidate = "candidate_route_proposal_exists" in failed_ids or not candidate_present
    issue = (
        "base_switch_gate: deterministic base-switch gate already ran and did not authorize a switch because no distinct, "
        "auditable current-Find/read candidate route proposal exists. Provide artifact-local current-route text/metadata "
        "provenance plus a real LLM/text embedding probe, or generate a proposal-only candidate base-switch route and collect "
        "loader/data/protocol/smoke/full-reference/artifact-local audit evidence before rerunning the gate."
        if missing_candidate else
        f"base_switch_gate: deterministic base-switch gate already ran and did not authorize the candidate route because required {missing_summary} checks are still blocked."
    )
    return {
        "missing_candidate": missing_candidate,
        "candidate_present": candidate_present,
        "failed_check_ids": failed_ids,
        "failed_check_summary": missing_summary,
        "issue": issue,
    }


def apply_failed_base_switch_gate_guidance(actions: list[dict[str, Any]], paths, project: str, venue: str) -> None:
    guidance = failed_base_switch_gate_guidance(paths)
    if not guidance:
        return
    failed_text = ",".join(guidance.get("failed_check_ids", [])[:8]) or "unknown"
    missing_summary = str(guidance.get("failed_check_summary") or "candidate gate evidence")
    proposal_or_provenance = (
        "The deterministic base-switch gate has already run and is blocked/not_authorized. Re-running it unchanged will not clear the blocker. "
        "First either provide artifact-local current-route raw text/metadata provenance with preserved ID mapping plus a real LLM/text embedding probe, "
        f"or create a non-authoritative candidate base-switch proposal traceable to the current Find/read packet and collect the missing {missing_summary} evidence. "
        f"Current failed checks: {failed_text}."
    )
    commands = [
        command(project, venue, "audit_selected_base_viability.py"),
        "Queue project Claude Code guidance: inspect current Find/read evidence and either write current-route artifact-local text/metadata provenance plus a real LLM/text embedding probe receipt, or write state/selected_route_switch_proposal.json as proposal_only_not_authorized with proposed_route repo/title/repo_path or proposed_path_hint, current_find_run_id, provenance evidence, and required_gate=deterministic_base_switch_gate. Do not edit active_repo/evidence_ready_repo_selection and do not launch candidate main-route experiments.",
        command(project, venue, "audit_deterministic_base_switch_gate.py"),
        command(project, "guard_selected_base_route.py"),
        command(project, venue, "build_blocker_action_plan.py"),
    ]
    success_checks = [
        "state/base_switch_gate.json is not rerun to the same failed candidate_route={} state without new provenance or proposal evidence.",
        "Current-route repair evidence includes artifact-local raw text/metadata provenance with preserved ID mapping and a real LLM/text embedding probe, or state/selected_route_switch_proposal.json names a proposal-only candidate traceable to current Find/read.",
        f"Candidate routes remain non-authoritative until the failed {missing_summary} checks pass and execute_authorized_base_switch.py runs after a passed gate.",
        "active_repo.json and evidence_ready_repo_selection.json remain unchanged while the gate is blocked/not_authorized.",
    ]
    for row in actions:
        route = row.get("route")
        if route in {"selected_base_viability_gate", "base_switch_gate"}:
            row["issue"] = guidance["issue"] if route == "base_switch_gate" else one_line("selected_base_viability_gate: semantic provenance remains blocked after a failed deterministic base-switch gate; provide current-route provenance or a candidate base-switch proposal before rerunning the gate.")
            row["repair_strategy"] = proposal_or_provenance
            row["recommended_commands"] = list(commands)
            row["success_checks"] = list(success_checks)
            row["base_switch_gate_status"] = "blocked/base_switch_not_authorized"
            row["base_switch_failed_checks"] = guidance.get("failed_check_ids", [])[:10]
            row["base_switch_candidate_route_present"] = guidance.get("candidate_present")
            row["priority"] = "P0"
            row["selected_base_gate_required_mode"] = True
        elif row.get("blocked_by_selected_base_viability_gate") or route in {"experiment_evidence_repair", "experiment_loop_repair", "evidence_assurance_repair", "paper_production_repair", "section_state_repair", "figure_repair", "citation_repair", "paper_artifact_refresh"}:
            row["priority"] = "P2"
            row["blocked_by_selected_base_viability_gate"] = True
            deferred_issue = (
                "blocked_by_failed_base_switch_gate: downstream experiment, paper, and claim actions are deferred until current-route "
                "provenance/embedding evidence or a proposal-only candidate base-switch route changes the deterministic gate input."
            )
            if row.get("issue") and not row.get("deferred_original_issue"):
                row["deferred_original_issue"] = row.get("issue")
            row["issue"] = deferred_issue
            row["human_summary"] = deferred_issue
            row["repair_strategy"] = (
                "Deferred while the failed deterministic base-switch gate lacks new current-route provenance or a proposal-only candidate route. "
                "Do not rerun the same gate or launch candidate/alternative main-route work until provenance/proposal evidence changes. "
                "Current-route repair may use launcher route_scope=selected_base_current_route; bounded candidate evidence collection may use route_scope=base_switch_evidence_collection only after a proposal exists."
            )


def merge_unique_values(target: dict[str, Any], key: str, values: Any) -> None:
    existing = target.get(key)
    merged = list(existing) if isinstance(existing, list) else ([existing] if existing not in (None, "") else [])
    incoming = values if isinstance(values, list) else [values]
    for value in incoming:
        if value in (None, "") or value in merged:
            continue
        merged.append(value)
    if merged:
        target[key] = merged


def compact_failed_base_switch_gate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated live failed-base-switch actions while preserving audit provenance."""
    compacted: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str, tuple[str, ...]], dict[str, Any]] = {}
    for row in actions:
        route = str(row.get("route") or "")
        issue = str(row.get("issue") or "")
        failed_checks = row.get("base_switch_failed_checks") if isinstance(row.get("base_switch_failed_checks"), list) else []
        if route in {"selected_base_viability_gate", "base_switch_gate"} and row.get("base_switch_gate_status") == "blocked/base_switch_not_authorized":
            key = (
                route,
                issue,
                str(row.get("base_switch_gate_status") or ""),
                tuple(str(item) for item in failed_checks),
            )
        elif row.get("blocked_by_selected_base_viability_gate") and issue.startswith("blocked_by_failed_base_switch_gate:"):
            key = (
                "failed_base_switch_downstream_deferral",
                issue,
                str(row.get("base_switch_gate_status") or "blocked/base_switch_not_authorized"),
                (),
            )
        else:
            compacted.append(row)
            continue

        kept = seen.get(key)
        if kept is not None:
            merge_unique_values(kept, "merged_action_ids", [kept.get("id"), row.get("id")])
            merge_unique_values(kept, "merged_source_check_ids", [kept.get("source_check_id"), row.get("source_check_id")])
            merge_unique_values(kept, "merged_sources", [kept.get("source"), row.get("source")])
            merge_unique_values(kept, "merged_routes", [kept.get("route"), row.get("route")])
            merge_unique_values(kept, "deferred_original_issues", [kept.get("deferred_original_issue"), row.get("deferred_original_issue")])
            merge_unique_values(kept, "evidence", row.get("evidence") if isinstance(row.get("evidence"), list) else [row.get("evidence")])
            continue
        seen[key] = row
        if key[0] == "failed_base_switch_downstream_deferral":
            row.setdefault("base_switch_gate_status", "blocked/base_switch_not_authorized")
            merge_unique_values(row, "merged_routes", [row.get("route")])
            merge_unique_values(row, "deferred_original_issues", [row.get("deferred_original_issue")])
        compacted.append(row)
    return compacted


def enforce_selected_base_viability_gate(actions: list[dict[str, Any]], seen: set[str], paths, project: str, venue: str, skills: dict[str, str]) -> None:
    if deterministic_base_switch_executed(paths):
        return
    gate = load_json(paths.state / "selected_base_viability_gate.json", {})
    base_switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    if not (isinstance(gate, dict) and gate.get("status") == "blocked" and gate.get("decision") == "base_switch_gate_required"):
        return
    if not any(row.get("route") == "selected_base_viability_gate" for row in actions):
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/selected_base_viability_gate.json",
            check_id="selected_base_viability_gate",
            issue=str(gate.get("issue") or "selected_base_viability_gate: base-switch gate required before further autonomous experiment launches."),
            evidence=gate.get("evidence", ["state/selected_base_viability_gate.json"]),
            severity="warn",
        )
    if not any(row.get("route") == "base_switch_gate" for row in actions):
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/base_switch_gate.json",
            check_id="base_switch_gate",
            issue=str(
                (base_switch_gate.get("summary") if isinstance(base_switch_gate, dict) else "")
                or "base_switch_gate未授权：候选路线只能作为 proposal；必须完成 provenance/loader/data/protocol/smoke/full reproduction/artifact-local audit 后才能切换。"
            ),
            evidence=["state/base_switch_gate.json", "reports/base_switch_gate.md", "state/base_switch_execution.json"],
            severity="warn",
        )
    for row in actions:
        if row.get("route") in {"experiment_evidence_repair", "experiment_loop_repair", "evidence_assurance_repair"}:
            row["current_selected_route_repair_allowed"] = False
            row["blocked_by_selected_base_viability_gate"] = True
            row["repair_strategy"] = (
                "selected_base_viability_gate.decision=base_switch_gate_required；暂停候选/替代路线实验；当前 selected-base 修复实验必须通过 launcher route_scope=selected_base_current_route。"
                "先刷新 deterministic base-switch gate、保持候选路线 proposal-only，并等待 gate 状态变化。"
            )


def enforce_literature_recommendation_gate(
    actions: list[dict[str, Any]],
    seen: set[str],
    paths,
    project: str,
    venue: str,
    skills: dict[str, str],
) -> list[dict[str, Any]]:
    gate = literature_recommendation_gate_status(paths)
    if not gate.get("blocking"):
        return actions
    if gate.get("llm_blocked"):
        issue = (
            f"Current Find {gate.get('run_id')} is blocked before mandatory LLM abstract scoring: "
            f"{gate.get('blocked_reason') or 'LLM API quota/configuration is unavailable'}. "
            f"Strong recommendations remain {gate.get('actual')}/{gate.get('target')}; shortfall={gate.get('shortfall')}; source_count={gate.get('source_count')}. "
            "Repair the LLM API configuration/quota first, then rerun complete Find from the TASTE entrypoint. "
            "Do not run targeted Find, experiments, base promotion, paper repair, or claim promotion while LLM scoring is unavailable."
        )
    else:
        issue = (
            f"Find recommended papers are below the project target: {gate.get('actual')}/{gate.get('target')}; "
            f"shortfall={gate.get('shortfall')}; source_count={gate.get('source_count')}. "
            "Only current Find title+abstract scoring/packet generation may be repaired; paper writing, citation/figure repair, experiments, base promotion, and claim promotion are blocked."
        )
    literature_route = "literature_llm_quota_exhausted" if gate.get("llm_blocked") else "literature_recommendation_gate"
    literature_check_id = "literature_llm_quota_exhausted" if gate.get("llm_blocked") else "literature_recommendation_count_gate"
    if not any(row.get("route") == literature_route for row in actions):
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="planning/finding/find_progress.json",
            check_id=literature_check_id,
            issue=issue,
            evidence=[str(paths.planning / "finding" / "find_progress.json"), str(paths.state / "literature_tool_packet.json")],
            severity="block",
        )
    allowed_routes = {"literature_llm_quota_exhausted", "literature_recommendation_gate", "active_full_research_cycle_worker"}
    filtered: list[dict[str, Any]] = []
    kept_literature_gate = False
    kept_worker = False
    for row in actions:
        route = str(row.get("route") or "")
        if route not in allowed_routes:
            continue
        if route in {"literature_llm_quota_exhausted", "literature_recommendation_gate"}:
            if kept_literature_gate:
                continue
            kept_literature_gate = True
            row["id"] = "literature_llm_quota_exhausted-001-current-find-llm-scoring" if gate.get("llm_blocked") else "literature_recommendation_gate-001-current-find-strong-shortfall"
            row["route"] = "literature_llm_quota_exhausted" if gate.get("llm_blocked") else "literature_recommendation_gate"
            row["category"] = "literature_llm_quota_exhausted" if gate.get("llm_blocked") else "literature_recommendation_gate"
            row["source"] = "planning/finding/find_progress.json"
            row["source_check_id"] = "literature_llm_quota_exhausted" if gate.get("llm_blocked") else "literature_strong_recommendation_gate"
            row["issue"] = issue
            row["priority"] = "P0"
            row["evidence"] = [str(paths.planning / "finding" / "find_progress.json"), str(paths.state / "literature_tool_packet.json")]
            row["literature_gate"] = {k: gate.get(k) for k in ["run_id", "actual", "target", "shortfall", "source_count", "llm_blocked", "blocked_reason"]}
            if gate.get("llm_blocked"):
                row["repair_strategy"] = "Restore a usable LLM API key/base/model/quota through the web config, verify the Find-shaped LLM probe, then rerun a complete Find/full-cycle discovery. Do not use weak/no-abstract papers or targeted Find to bypass mandatory LLM scoring."
                row["recommended_commands"] = [
                    command(project, "check_llm_ready.py", "--live"),
                    command(project, venue, "build_blocker_action_plan.py"),
                    "After /api/config/llm-probe succeeds, restart the complete full-cycle discovery from the web/TASTE entrypoint.",
                ]
                row["success_checks"] = [
                    "POST /api/config/llm-probe returns ok=true for the saved LLM config.",
                    "framework/scripts/check_llm_ready.py --project " + project + " --live exits 0.",
                    "A new complete Find run reaches mandatory LLM abstract scoring and writes current-run find_results/article outputs with real abstracts.",
                    "No targeted Find, base-selection, experiment, paper/citation/figure, paper-pipeline, or claim-promotion command is recommended or executed while LLM scoring is unavailable.",
                ]
            else:
                row["recommended_commands"] = [
                    current_find_refresh_command(project),
                    current_find_bridge_command(project, venue),
                    command(project, venue, "audit_submission_readiness.py"),
                    command(project, venue, "build_blocker_action_plan.py"),
                ]
                row["success_checks"] = [
                    "planning/finding/find_progress.json reports strong_recommendation_count >= recommendation_target_count, or preserves the shortfall with exact source/query blockers.",
                    "state/taste_targeted_queries.json records targeted queries when record-only mode is requested; otherwise the controlled targeted Find refreshes planning/finding outputs without promoting weak papers.",
                    "state/submission_readiness.json keeps submission_ready=false while the literature shortfall remains.",
                    "No base-selection, experiment, paper/citation/figure, paper-pipeline, or claim-promotion command is recommended or executed for this blocker.",
                ]
        if route == "active_full_research_cycle_worker":
            if kept_worker:
                continue
            kept_worker = True
            row["priority"] = "P0"
            row["blocked_by_literature_recommendation_gate"] = True
            row["repair_strategy"] = "Only supervise the existing worker long enough to stop/refresh under the literature gate; do not launch downstream paper/experiment repair."
            row["recommended_commands"] = [command(project, venue, "run_supervision_tick.py", "--supervise")]
        filtered.append(row)
    return filtered


def build(project: str, venue: str) -> dict[str, Any]:
    paths = build_paths(project)
    skills = load_skills()
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    reference_gate_passed = isinstance(reference_gate, dict) and reference_gate.get("status") == "pass" and reference_gate.get("decision") == "continue_base"
    current_reference_terminal = isinstance(reference_gate, dict) and reference_gate.get("decision") == "no_viable_base_switch_route"
    current_fresh_base_implementation = isinstance(reference_gate, dict) and reference_gate.get("decision") in {"fresh_base_implementation_required", "fresh_base_reference_probe_required", "fresh_base_reference_smoke_required", "fresh_base_reference_reproduction_required"}
    current_fresh_base_reference_probe = isinstance(reference_gate, dict) and reference_gate.get("decision") == "fresh_base_reference_probe_required"
    current_fresh_base_reference_smoke = isinstance(reference_gate, dict) and reference_gate.get("decision") == "fresh_base_reference_smoke_required"
    current_fresh_base_reference_reproduction = isinstance(reference_gate, dict) and reference_gate.get("decision") == "fresh_base_reference_reproduction_required"
    current_fresh_literature_audit = isinstance(reference_gate, dict) and reference_gate.get("decision") == "literature_base_audit_required"

    submission = load_json(paths.state / "submission_readiness.json", {})
    for row in submission.get("failed_checks", []) if isinstance(submission.get("failed_checks", []), list) else []:
        if isinstance(row, dict):
            check_id = str(row.get("id") or row.get("name") or "")
            detail = str(row.get("detail") or row.get("name") or row.get("id") or "")
            if current_fresh_base_implementation and check_id == "reference_reproduction_gate_pass":
                continue
            add_action(
                actions,
                seen,
                project=project,
                venue=venue,
                skills=skills,
                source="state/submission_readiness.json",
                check_id=check_id,
                issue=detail,
                evidence=row.get("evidence", []),
                severity=str(row.get("severity") or row.get("status") or "block"),
            )
    for text in submission.get("blockers", []) if isinstance(submission.get("blockers", []), list) else []:
        text_s = str(text)
        if current_fresh_base_implementation and (
            "best_reproduction=" in text_s
            or "legacy" in text_s.lower()
            or "reference_reproduction_gate" in text_s
        ):
            continue
        add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/submission_readiness.json", issue=text_s, evidence=["state/submission_readiness.json"])

    for filename, source_name in [
        ("reference_reproduction_gate.json", "state/reference_reproduction_gate.json"),
        ("experiment_iteration_audit.json", "state/experiment_iteration_audit.json"),
    ]:
        payload = load_json(paths.state / filename, {})
        for blocker in payload.get("blockers", []) if isinstance(payload.get("blockers", []), list) else []:
            add_action(actions, seen, project=project, venue=venue, skills=skills, source=source_name, check_id=filename, issue=str(blocker), evidence=[source_name])

    normality = load_json(paths.state / "paper_normality_audit.json", {})
    for row in normality.get("checks", []) if isinstance(normality.get("checks", []), list) else []:
        if isinstance(row, dict) and str(row.get("status")) == "block":
            add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/paper_normality_audit.json", check_id=str(row.get("id") or ""), issue=str(row.get("detail") or row.get("id") or ""), evidence=["state/paper_normality_audit.json"])

    figure = load_json(paths.state / "paper_figure_quality_audit.json", {})
    if figure.get("status") == "blocked" or int(figure.get("blocked_count") or 0) > 0:
        add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/paper_figure_quality_audit.json", check_id="paper_figure_quality", issue=f"paper figure quality blocked_count={figure.get('blocked_count')}", evidence=["state/paper_figure_quality_audit.json"])

    orchestra = load_json(paths.state / "paper_orchestra_state.json", {})
    if orchestra.get("status") == "hold" or orchestra.get("promotion_gate_recommendation") == "hold-markdown-only":
        add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/paper_orchestra_state.json", check_id="paper_orchestra_state", issue=f"paper_orchestra_state.status={orchestra.get('status')}; promotion_gate={orchestra.get('promotion_gate_recommendation')}", evidence=["state/paper_orchestra_state.json"])
    for section in orchestra.get("sections", []) if isinstance(orchestra.get("sections", []), list) else []:
        if isinstance(section, dict) and str(section.get("status") or "").lower() in {"blocked", "revision"}:
            blockers = section.get("blockers", []) if isinstance(section.get("blockers", []), list) else []
            add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/paper_orchestra_state.json", check_id=f"section:{section.get('id')}", issue=f"section {section.get('id')} status={section.get('status')}; blockers={'; '.join(str(x) for x in blockers[:4])}", evidence=["state/paper_orchestra_state.json"])

    framework_coupling = load_json(paths.state / "framework_content_coupling_audit.json", {})
    if isinstance(framework_coupling, dict) and framework_coupling.get("status") == "blocked":
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/framework_content_coupling_audit.json",
            check_id="framework_content_coupling",
            issue=(
                f"framework_content_coupling: finding_count={framework_coupling.get('finding_count')}; "
                "framework code still contains project-specific research content coupling."
            ),
            evidence=["state/framework_content_coupling_audit.json", "reports/framework_content_coupling_audit.md"],
            severity="block",
        )

    obsolete_cleanup = load_json(paths.state / "obsolete_baseline_cleanup_plan.json", {})
    if isinstance(obsolete_cleanup, dict) and obsolete_cleanup.get("status") in {"blocked_not_authorized", "blocked_pending_project_review"}:
        candidate_count = len(obsolete_cleanup.get("blocked_candidate_paths", []) or [])
        issue = (
            "obsolete_baseline_cleanup: project Claude Code must review current-route protections and "
            f"{candidate_count} candidate cleanup paths, then write either a keep review with candidate_fingerprint "
            "or state/obsolete_baseline_cleanup_authorization.json with exact approved paths; project Claude Code must execute any authorized cleanup itself and TASTE only audits the receipt."
        )
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/obsolete_baseline_cleanup_plan.json",
            check_id="obsolete_baseline_cleanup",
            issue=issue,
            evidence=["state/obsolete_baseline_cleanup_plan.json", "state/base_switch_gate.json", "state/base_switch_execution.json"],
            severity="warn",
        )

    for filename, source_name in [
        ("research_assurance_layer.json", "state/research_assurance_layer.json"),
        ("research_evidence_manifest.json", "state/research_evidence_manifest.json"),
        ("experiment_runtime_integrity.json", "state/experiment_runtime_integrity.json"),
    ]:
        payload = load_json(paths.state / filename, {})
        if payload.get("status") == "blocked":
            add_action(actions, seen, project=project, venue=venue, skills=skills, source=source_name, check_id=payload.get("status", ""), issue=f"{source_name} status={payload.get('status')}", evidence=[source_name])
        for row in payload.get("issues", []) if isinstance(payload.get("issues", []), list) else []:
            if isinstance(row, dict) and row.get("severity") == "block":
                add_action(actions, seen, project=project, venue=venue, skills=skills, source=source_name, check_id=str(row.get("id") or row.get("issue") or ""), issue=str(row.get("issue") or ""), evidence=row.get("evidence") or [source_name])
        for row in payload.get("weak_or_unsupported_claims", []) if isinstance(payload.get("weak_or_unsupported_claims", []), list) else []:
            if isinstance(row, dict):
                add_action(actions, seen, project=project, venue=venue, skills=skills, source=source_name, check_id=str(row.get("id") or "weak_claim"), issue=f"weak_or_unsupported_claim={row.get('id')}; status={row.get('claim_status')}; support_count={row.get('support_count')}", evidence=[source_name])

    full_cycle = load_json(paths.state / "full_research_cycle.json", {})
    selected_base_viability = load_json(paths.state / "selected_base_viability_gate.json", {})
    base_switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    switch_executed = deterministic_base_switch_executed(paths)
    if isinstance(base_switch_gate, dict) and base_switch_gate.get("status") == "blocked" and not switch_executed:
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/base_switch_gate.json",
            check_id="base_switch_gate",
            issue=str(base_switch_gate.get("summary") or "base_switch_gate blocked"),
            evidence=base_switch_gate.get("evidence", ["state/base_switch_gate.json"]),
            severity="block",
        )
    if isinstance(selected_base_viability, dict) and selected_base_viability.get("status") == "blocked" and not switch_executed:
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/selected_base_viability_gate.json",
            check_id="selected_base_viability_gate",
            issue=str(selected_base_viability.get("issue") or "selected_base_viability_gate blocked"),
            evidence=selected_base_viability.get("evidence", ["state/selected_base_viability_gate.json"]),
            severity=str(selected_base_viability.get("severity") or "block"),
        )
    live_worker = active_full_research_cycle_worker(paths, project) if reference_gate_passed else {}
    if live_worker:
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/supervision_tick.json",
            check_id="active_full_research_cycle_worker",
            issue=live_worker_issue(live_worker),
            evidence=["state/supervision_tick.json", "state/full_research_cycle.json"],
            severity="running",
        )
    if current_fresh_base_implementation:
        fresh_impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
        selected = {}
        base_switch = reference_gate.get("base_switch", {}) if isinstance(reference_gate.get("base_switch"), dict) else {}
        if isinstance(base_switch.get("fresh_paper_base"), dict):
            selected = base_switch.get("fresh_paper_base", {})
        if not selected:
            fresh_state = load_json(paths.state / "fresh_research_base.json", {})
            selected = fresh_state.get("selected", {}) if isinstance(fresh_state, dict) and isinstance(fresh_state.get("selected"), dict) else {}
        title = selected.get("title") or "fresh Find selected paper/method base"
        impl_status = fresh_impl.get("status", "") if isinstance(fresh_impl, dict) else ""
        impl_blockers = fresh_impl.get("blocker_reasons", []) if isinstance(fresh_impl, dict) and isinstance(fresh_impl.get("blocker_reasons", []), list) else []
        loader_contract_passed = fresh_base_loader_contract_passed(paths)
        data_required = fresh_base_data_required(fresh_impl) and not loader_contract_passed
        reference_smoke_required = current_fresh_base_reference_smoke or (loader_contract_passed and impl_status == "implementation_ready_for_reference_probe" and fresh_base_reference_smoke_required(paths))
        reference_reproduction_required = current_fresh_base_reference_reproduction or (loader_contract_passed and impl_status == "implementation_ready_for_reference_probe" and fresh_base_reference_smoke_passed(paths))
        reference_probe_required = current_fresh_base_reference_probe or (loader_contract_passed and impl_status == "implementation_ready_for_reference_probe" and not reference_smoke_required and not reference_reproduction_required)
        add_action(
            actions,
            seen,
            project=project,
            venue=venue,
            skills=skills,
            source="state/reference_reproduction_gate.json",
            check_id="fresh_base_data_required" if data_required else "fresh_base_reference_probe_required" if reference_probe_required else "fresh_base_reference_smoke_required" if reference_smoke_required else "fresh_base_reference_reproduction_required" if reference_reproduction_required else "fresh_base_implementation_required",
            issue=(
                (
                    f"fresh_base_data_required: Environment-stage Claude Code selected {title}; resolve official real data files and pass loader/import probes before Claude implementation, experiments, or paper writing; legacy routes are control only."
                )
                if data_required else (
                    f"fresh_base_reference_probe_required: selected-base data/loader passed for {title}; "
                    "create the environment manifest and run bounded read-only reference-protocol probes before training, paper writing, or claim promotion; "
                    "Historical routes are legacy/control only and must not be the main route."
                )
                if reference_probe_required else (
                    f"fresh_base_reference_smoke_required: selected-base reference protocol passed for {title}; "
                    "run bounded no-training reference smoke/audit before full training, paper writing, or claim promotion; "
                    "Historical routes are legacy/control only and must not be the main route."
                )
                if reference_smoke_required else (
                    f"fresh_base_reference_reproduction_required: selected-base bounded reference smoke passed for {title}; "
                    "run wrapper-managed full reference reproduction before candidate experiments, paper writing, or claim promotion; "
                    "Historical routes are legacy/control only and must not be the main route."
                )
                if reference_reproduction_required else (
                    f"fresh_base_implementation_required: Environment-stage Claude Code selected {title}; "
                    "continue official-code/artifact search or implement the smallest reproducible fresh-base route; "
                    "Historical routes are legacy/control only and must not be the main route."
                )
                + (f" implementation_plan_status={impl_status}; blockers={one_line('; '.join(str(item) for item in impl_blockers[:4]), 360)}." if impl_status or impl_blockers else "")
            ),
            evidence=["state/fresh_research_base.json", "state/current_find_research_plan.json", "state/fresh_base_implementation_plan.json", "state/fresh_base_data_acquisition.json", "state/literature_tool_packet.json", "planning/finding/read_results.json", "planning/finding/ideas.json", "planning/finding/plans.json", "state/reference_reproduction_gate.json"],
            severity="block",
        )
    literature_audit = load_json(paths.state / "literature_base_audit.json", {})
    fresh_literature_audit_complete = bool(isinstance(literature_audit, dict) and literature_audit.get("audit_complete"))
    if fresh_literature_audit_complete:
        actions = [
            row for row in actions
            if not (
                row.get("route") == "fresh_literature_base_audit"
                and (
                    "not been repo/data/env audited" in str(row.get("issue") or "")
                    or "审计尚未完成" in str(row.get("issue") or "")
                    or "pending_fresh_literature_base_audit" in str(row.get("issue") or "")
                )
            )
        ]
    for row in full_cycle.get("latest_blockers", []) if isinstance(full_cycle.get("latest_blockers", []), list) else []:
        if isinstance(row, dict):
            issue = str(row.get("issue") or "")
            if stale_full_cycle_blocker(paths, issue, str(row.get("category") or ""), str(row.get("severity") or "block")):
                continue
            if live_worker and (
                "paper-preview-repair" in issue
                or "paper preview" in issue.lower()
                or "pdf" in issue.lower()
                or "paper_refresh" in issue
                or "paper_pipeline" in issue
            ):
                continue
            if reference_gate_passed and str(row.get("category") or "").startswith("fresh_base_"):
                continue
            if reference_gate_passed and (
                "fresh base" in issue.lower()
                or "alternative-route bounded reference" in issue
                or "paper-level full reference reproduction is still required" in issue.lower()
                or "bounded reference smoke/audit passed" in issue.lower()
                or "full reference reproduction is still required" in issue.lower()
            ):
                continue
            if (current_fresh_literature_audit or current_fresh_base_implementation) and (
                "Base-switch search is exhausted" in issue
                or "no_viable_base_switch_route" in issue
                or "no evidence-ready alternative" in issue
                or "paper_refresh" in issue
            ):
                continue
            # Ignore stale controller snapshots from an older switch_base decision once
            # the current audited gate records terminal base-switch exhaustion.
            if current_reference_terminal and "decision=switch_base" in issue:
                continue
            # `paper_orchestra_audit.status=submission_ready` is a passing audit value;
            # older full-cycle snapshots used it as a blocker before the readiness audit
            # normalized the check. Do not turn that stale snapshot back into a repair.
            if "paper_orchestra_audit.status=submission_ready" in issue:
                continue
            # Skip stale submission-readiness snapshots after the live readiness gate passes.
            if isinstance(submission, dict) and submission.get("submission_ready") and submission.get("status") == "submission_ready":
                if str(row.get("category") or "") == "submission_readiness":
                    continue
            # Skip stale paper_evidence_audit blockers when the audit now allows template
            if "paper_evidence_audit recommends hold-markdown-only" in issue or "paper_evidence_audit must not recommend hold-markdown-only" in issue:
                current_audit = (paths.reports / "paper_evidence_audit.md").read_text(encoding="utf-8", errors="replace").lower() if (paths.reports / "paper_evidence_audit.md").exists() else ""
                if "promotion_gate_recommendation: allow-template" in current_audit:
                    continue
            # Skip stale assurance_status=blocked when assurance is now warn or pass
            if "assurance_status=blocked" in issue:
                current_audit = (paths.reports / "paper_evidence_audit.md").read_text(encoding="utf-8", errors="replace").lower() if (paths.reports / "paper_evidence_audit.md").exists() else ""
                if "promotion_gate_recommendation: allow-template" in current_audit:
                    continue
            # Skip stale assurance_status=blocked when assurance is now warn or pass
            if "assurance_status=blocked" in issue:
                current_assurance = load_json(paths.state / "research_assurance_layer.json", {})
                if isinstance(current_assurance, dict) and current_assurance.get("status") in {"warn", "pass"}:
                    continue
            # Skip stale evidence_manifest issues when manifest is no longer blocked
            if "evidence_manifest" in issue:
                current_manifest = load_json(paths.state / "research_evidence_manifest.json", {})
                if isinstance(current_manifest, dict) and current_manifest.get("status") in {"pass", "warn"}:
                    continue
            add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/full_research_cycle.json", check_id=str(row.get("category") or ""), issue=issue, evidence=row.get("evidence", []), severity=str(row.get("severity") or "block"))
    for row in full_cycle.get("stage_failures", [])[-10:] if isinstance(full_cycle.get("stage_failures", []), list) else []:
        if isinstance(row, dict):
            stage = str(row.get("stage") or "stage_failure")
            issue_text = f"{stage} failed rc={row.get('return_code')}; {one_line(row.get('tail'), 420)}"
            if switch_executed:
                stale_stage_text = f"{stage} {issue_text}".lower()
                if any(
                    marker in stale_stage_text
                    for marker in [
                        "selected-base-viability",
                        "selected_base_viability",
                        "base_switch_gate_required",
                        "invalid_unapproved_switch",
                    ]
                ):
                    continue
            if live_worker and ("paper-preview-repair" in stage or "paper" in stage.lower() or "pdf" in issue_text.lower()):
                continue
            if reference_gate_passed and (
                "reference-reproduction-gate" in stage.lower()
                or "reference_reproduction_gate" in issue_text.lower()
                or "reference reproduction" in issue_text.lower()
            ):
                continue
            if "framework-content-coupling" in stage.lower() or "framework_content_coupling" in issue_text.lower():
                current_coupling = load_json(paths.state / "framework_content_coupling_audit.json", {})
                if isinstance(current_coupling, dict) and current_coupling.get("status") in {"pass", "warn"}:
                    continue
            add_action(actions, seen, project=project, venue=venue, skills=skills, source="state/full_research_cycle.json", check_id=stage, issue=issue_text, evidence=["state/full_research_cycle.json"], severity="block")

    if live_worker:
        for row in actions:
            if row.get("source_check_id") == "active_full_research_cycle_worker":
                row["priority"] = "P0"
                row["route"] = "active_full_research_cycle_worker"
                row["autonomy"] = "observe_running_worker"
                row["repair_strategy"] = "只监督当前活训练进程并刷新 gates；不要把旧 paper-preview/full-cycle 快照当成当前阻塞。"
                row["recommended_commands"] = [
                    command(project, "run_supervision_tick.py", "--venue", venue or "ICLR", "--supervise"),
                    command(project, venue, "audit_reference_reproduction.py"),
                ]
                row["success_checks"] = [
                    "state/supervision_tick.json full_cycle_job.status=running 且 PID 存活，或训练结束后由下一次 tick 进入下一门控。",
                    "state/full_research_cycle.json 顶层 status 不得继续显示 completed/final paper passed 的旧状态。",
                ]
            elif row.get("route") in {"paper_production_repair", "section_state_repair", "figure_repair", "citation_repair", "paper_artifact_refresh", "experiment_loop_repair"}:
                row["priority"] = "P2"
                row["blocked_by_active_full_research_cycle_worker"] = True

    if current_fresh_base_implementation:
        fresh_data_required = fresh_base_data_required(load_json(paths.state / "fresh_base_implementation_plan.json", {}))
        for row in actions:
            route = row.get("route")
            issue = str(row.get("issue") or "")
            if route == "fresh_base_data_contract":
                if fresh_base_loader_contract_passed(paths):
                    row["priority"] = "P2"
                    row["blocked_by_fresh_base_reference_probe"] = True
                    row["repair_strategy"] = "Superseded because selected-base data and loader/import probes have passed; continue with reference protocol/env manifest probe."
                else:
                    row["priority"] = "P0"
                continue
            if route in {"fresh_base_reference_probe", "fresh_base_reference_smoke", "fresh_base_reference_reproduction"}:
                row["priority"] = "P0"
                continue
            if route == "fresh_base_implementation":
                row["priority"] = "P1" if fresh_data_required else "P0"
                if fresh_data_required:
                    row["blocked_by_fresh_base_data_contract"] = True
                    row["repair_strategy"] = "Deferred until selected-base official data files and loader contract are evidence-ready."
                continue
            if route in {"terminal_reference_base_block", "reference_base_switch", "reference_reproduction_repair", "experiment_evidence_repair", "experiment_loop_repair", "evidence_assurance_repair"} or _mentions_non_current_route(paths, issue):
                row["priority"] = "P2"
                row["blocked_by_fresh_base_implementation"] = True
                row["repair_strategy"] = (
                    "Deferred while fresh Find paper/method base implementation is unresolved. "
                    "Do not run non-current historical routes as the main research route; use them only as legacy/control after the current route is audited."
                )
    enforce_experiment_first(actions, paths, project, venue, skills)
    enforce_selected_base_viability_gate(actions, seen, paths, project, venue, skills)
    selected_base_viability = load_json(paths.state / "selected_base_viability_gate.json", {})
    base_switch_gate = load_json(paths.state / "base_switch_gate.json", {})
    if (
        isinstance(selected_base_viability, dict)
        and selected_base_viability.get("status") == "blocked"
        and selected_base_viability.get("decision") == "base_switch_gate_required"
        and not deterministic_base_switch_executed(paths)
    ):
        for row in actions:
            route = row.get("route")
            if route in {"selected_base_viability_gate", "base_switch_gate"}:
                row["priority"] = "P0"
                row["selected_base_gate_required_mode"] = True
            elif route in {"experiment_evidence_repair", "experiment_loop_repair", "evidence_assurance_repair", "paper_production_repair", "section_state_repair", "figure_repair", "citation_repair", "paper_artifact_refresh"}:
                row["priority"] = "P2"
                row["blocked_by_selected_base_viability_gate"] = True
                row["repair_strategy"] = (
                    "Deferred for candidate/alternative-route work because selected_base_viability_gate.decision=base_switch_gate_required. "
                    "Run audit_deterministic_base_switch_gate.py. Keep candidate routes proposal-only/not_authorized until checks pass; bounded candidate gate-evidence collection may use launcher route_scope=base_switch_evidence_collection, but paper/claim promotion remains blocked until downstream audits pass."
                )
        if not any(row.get("route") == "selected_base_viability_gate" for row in actions):
            add_action(
                actions,
                seen,
                project=project,
                venue=venue,
                skills=skills,
                source="state/selected_base_viability_gate.json",
                check_id="selected_base_viability_gate",
                issue=str(selected_base_viability.get("issue") or "selected_base_viability_gate requires deterministic base-switch gate."),
                evidence=selected_base_viability.get("evidence", ["state/selected_base_viability_gate.json"]),
                severity="block",
            )
            actions[-1]["priority"] = "P0"
            actions[-1]["selected_base_gate_required_mode"] = True
        if not any(row.get("route") == "base_switch_gate" for row in actions):
            add_action(
                actions,
                seen,
                project=project,
                venue=venue,
                skills=skills,
                source="state/base_switch_gate.json",
                check_id="base_switch_gate",
                issue=str(base_switch_gate.get("summary") or "base_switch_gate blocked/not_authorized; keep current selected base unchanged."),
                evidence=base_switch_gate.get("evidence", ["state/base_switch_gate.json"]),
                severity="block",
            )
            actions[-1]["priority"] = "P0"
            actions[-1]["selected_base_gate_required_mode"] = True
    actions = enforce_literature_recommendation_gate(actions, seen, paths, project, venue, skills)
    if current_fresh_base_implementation:
        for row in actions:
            if row.get("route") in {"experiment_evidence_repair", "evidence_assurance_repair"} or row.get("blocked_by_fresh_base_implementation"):
                row["priority"] = "P2"
                row["blocked_by_fresh_base_implementation"] = True
        if current_fresh_base_reference_probe or current_fresh_base_reference_smoke or current_fresh_base_reference_reproduction:
            actions = [row for row in actions if not (row.get("route") == "fresh_base_data_contract" and fresh_base_loader_contract_passed(paths))]
        if current_fresh_base_reference_smoke:
            actions = [row for row in actions if row.get("route") != "fresh_base_reference_probe"]
        if current_fresh_base_reference_reproduction:
            actions = [row for row in actions if row.get("route") not in {"fresh_base_reference_probe", "fresh_base_reference_smoke"}]
    if current_fresh_base_reference_reproduction:
        fresh_route_actions = [row for row in actions if row.get("route") == "fresh_base_reference_reproduction"]
        other_actions = [row for row in actions if row.get("route") != "fresh_base_reference_reproduction"]
        gate_actions = [row for row in fresh_route_actions if row.get("source") == "state/reference_reproduction_gate.json"]
        retained_fresh = gate_actions + [
            row for row in fresh_route_actions
            if row not in gate_actions and "bounded reference smoke passed" not in str(row.get("issue") or "").lower()
        ]
        actions = retained_fresh + other_actions
        for row in actions:
            if row.get("route") == "fresh_base_reference_reproduction" and row.get("source") != "state/reference_reproduction_gate.json":
                row["priority"] = "P1"
                row["blocked_by_fresh_base_reference_reproduction"] = True
    if reference_gate_passed:
        actions = [row for row in actions if not stale_action_after_reference_pass(row, reference_gate_passed)]
        selected_issue = (
            "参考复现已通过；当前缺少当前主线下可审计、可推广的项目目标候选实验。"
            "下一步由 project agent 继续真实实验迭代，论文/claim 暂停。"
        )
        if not any(row.get("route") == "experiment_evidence_repair" for row in actions):
            add_action(
                actions,
                seen,
                project=project,
                venue=venue,
                skills=skills,
                source="state/scientific_progress_gate.json",
                check_id="selected_base_experiment_evidence_required",
                issue=selected_issue,
                evidence=["state/reference_reproduction_gate.json", "state/scientific_progress_gate.json", "state/experiment_registry.json"],
                severity="block",
            )
    apply_failed_base_switch_gate_guidance(actions, paths, project, venue)
    actions = compact_failed_base_switch_gate_actions(actions)
    actions.sort(key=priority_key)
    for row in actions:
        cmds = row.get("recommended_commands", []) if isinstance(row.get("recommended_commands", []), list) else []
        filtered_cmds = [cmd for cmd in cmds if "run_research_trajectory_supervisor.py" not in str(cmd)]
        if len(filtered_cmds) != len(cmds):
            row["recommended_commands"] = filtered_cmds
            row.setdefault("wrapper_managed_notes", []).append("trajectory supervisor invocation is managed by wrapper/full-cycle, not by project Claude workers")
    manual = [row for row in actions if row.get("autonomy") == "manual_required"]
    autonomous = [row for row in actions if row.get("autonomy") != "manual_required"]
    payload = {
        "project": project,
        "venue": venue,
        "updated_at": now_iso(),
        "status": "blocked" if actions else "pass",
        "wrapper_managed_actions": [
            {
                "id": "trajectory_supervisor",
                "script": "framework/scripts/run_research_trajectory_supervisor.py",
                "policy": "wrapper/web/full-cycle owns trajectory supervisor invocation; project Claude workers must not spawn nested supervisors.",
            }
        ],
        "top_action": actions[0].get("issue") if actions else "",
        "top_route": actions[0].get("route") if actions else "",
        "summary": {
            "action_count": len(actions),
            "autonomous_action_count": len(autonomous),
            "manual_action_count": len(manual),
            "p0_action_count": sum(1 for row in actions if row.get("priority") == "P0"),
            "top_action": actions[0].get("issue") if actions else "",
            "top_route": actions[0].get("route") if actions else "",
        },
        "policy": {
            "deterministic_layer": "Classify blockers, assign priority/autonomy, provide commands and success checks.",
            "skill_layer": "Claude executes open-ended repairs through native skill contracts.",
            "gate_layer": "Hard scripts re-run audits and decide whether the blocker is cleared.",
        },
        "actions": actions[:80],
    }
    return payload


def write_report(paths, payload: dict[str, Any]) -> Path:
    lines = [
        "# Blocker Action Plan\n\n",
        f"- project: {payload.get('project', '')}\n",
        f"- venue: {payload.get('venue', '')}\n",
        f"- status: {payload.get('status', '')}\n",
        f"- updated_at: {payload.get('updated_at', '')}\n",
    ]
    summary = payload.get("summary", {}) if isinstance(payload.get("summary", {}), dict) else {}
    for key in ["action_count", "autonomous_action_count", "manual_action_count", "p0_action_count", "top_route", "top_action"]:
        lines.append(f"- {key}: {summary.get(key, '')}\n")
    lines.append("\n## Implementation Split\n")
    policy = payload.get("policy", {}) if isinstance(payload.get("policy", {}), dict) else {}
    for key in ["deterministic_layer", "skill_layer", "gate_layer"]:
        lines.append(f"- {key}: {policy.get(key, '')}\n")
    lines.append("\n## Actions\n")
    for row in payload.get("actions", [])[:40]:
        lines.append(f"- [{row.get('priority')}] {row.get('id')} | {row.get('autonomy')} | {row.get('route')}: {row.get('issue')}\n")
        lines.append(f"  skill: {row.get('skill_contract', '') or 'manual'}\n")
        lines.append(f"  strategy: {row.get('repair_strategy', '')}\n")
        cmds = row.get("recommended_commands", []) if isinstance(row.get("recommended_commands", []), list) else []
        if cmds:
            lines.append(f"  verify: `{cmds[-1]}`\n")
    out = paths.reports / "blocker_action_plan.md"
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic blocker-to-action routing plan for TASTE autonomous research.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    paths = build_paths(args.project)
    payload = build(args.project, args.venue)
    save_json(paths.state / "blocker_action_plan.json", payload)
    report = write_report(paths, payload)
    print(report)
    return 0 if payload.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
