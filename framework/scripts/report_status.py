#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from llm_client import llm_available, llm_disabled_reason
from project_paths import build_paths, load_project_config
from pipeline_guard import current_environment_selection as guard_current_environment_selection
from paper_common import get_active_paper_state


def find_cli_binary(cfg: dict, name: str) -> str:
    import glob
    import os
    import subprocess
    hints = [os.environ.get(f"{name.upper()}_BIN", "")]
    agent_cfg = cfg.get("coding_agent", {}) if isinstance(cfg, dict) else {}
    hints.append(str(agent_cfg.get(f"{name}_path_hint", "") or ""))
    found = shutil.which(name)
    if found:
        hints.append(found)
    try:
        proc = subprocess.run(["bash", "-ic", "printf %s \"$PATH\""], text=True, capture_output=True, timeout=10)
        if proc.returncode == 0:
            for part in proc.stdout.split(":"):
                if part:
                    hints.append(str(Path(part) / name))
    except Exception:
        pass
    root = Path(__file__).resolve().parents[1]
    hints.extend([
        str(root.parent / ".nvm" / "versions" / "node" / "*" / "bin" / name),
        str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / name),
    ])
    expanded = []
    for item in hints:
        if item and "*" in item:
            expanded.extend(glob.glob(item))
        elif item:
            expanded.append(item)
    seen = set()
    for item in expanded:
        if item in seen or not Path(item).exists():
            continue
        seen.add(item)
        env = os.environ.copy()
        env["PATH"] = str(Path(item).parent) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run([item, "--version"], text=True, capture_output=True, env=env)
        if proc.returncode == 0:
            return item
    return ""


def find_claude(cfg: dict) -> str:
    return find_cli_binary(cfg, "claude")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def count_dirs(path: Path) -> int:
    return len([entry for entry in path.iterdir() if entry.is_dir()]) if path.exists() else 0


def _repo_identity(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("repo_path") or row.get("name") or row.get("url") or "").strip()


def _blocker_matches_active_repo(blocker_packet: dict, active_repo: dict) -> bool:
    if not isinstance(blocker_packet, dict) or not blocker_packet:
        return False
    if not isinstance(active_repo, dict) or not active_repo:
        return False
    blocker_repo = blocker_packet.get("active_repo", {})
    return bool(_repo_identity(blocker_repo) and _repo_identity(blocker_repo) == _repo_identity(active_repo))


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _state_run_id(row: dict) -> str:
    row = _as_dict(row)
    for key in ("fresh_find_run_id", "current_find_run_id", "run_id", "taste_run_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _accepted_repo_selection(row: dict) -> bool:
    row = _as_dict(row)
    selected = _as_dict(row.get("selected"))
    gate = str(row.get("selection_gate") or "").strip()
    action = str(row.get("current_action") or row.get("status") or "").strip().lower()
    return bool(
        selected
        and (
            gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate"))
            or action in {"complete", "completed", "selected", "done"}
        )
    )


def _repo_selection_blocker_is_current(blocker: dict, selection: dict) -> bool:
    blocker = _as_dict(blocker)
    if not blocker or not str(blocker.get("reason") or blocker.get("summary") or "").strip():
        return False
    if not _accepted_repo_selection(selection):
        return True
    blocker_run = _state_run_id(blocker)
    selection_run = _state_run_id(selection)
    if blocker_run and selection_run and blocker_run != selection_run:
        return False
    return False


def _pending_candidate_blocked(selection: dict) -> bool:
    selection = _as_dict(selection)
    return str(selection.get("selection_gate") or "").strip() == "blocked_pending_data_loader_for_claude_best_candidate"


def _candidate_base_switch_blocked(selection: dict) -> bool:
    selection = _as_dict(selection)
    return str(selection.get("selection_gate") or "").strip() == "blocked_candidate_base_switch_gate_required"


def _candidate_subject(row: dict) -> str:
    row = _as_dict(row)
    return str(
        row.get("name")
        or row.get("repo")
        or row.get("title")
        or row.get("repo_path")
        or ""
    ).strip()


def _repo_selection_public_status(selection: dict, current_env: dict | None = None) -> str:
    selection = _as_dict(selection)
    if _pending_candidate_blocked(selection):
        return "pending_candidate_blocked"
    if _candidate_base_switch_blocked(selection):
        return "pending_candidate_base_switch_gate_blocked"
    env = _as_dict(current_env)
    if env and not env.get("valid"):
        reason = str(env.get("reason") or "").strip()
        if reason:
            return reason
    if _accepted_repo_selection(selection):
        return "selected"
    status = str(selection.get("status") or selection.get("current_action") or "").strip()
    return status or "not-run"


def _environment_selection_status(selection: dict, current_find_plan: dict, current_env: dict | None = None) -> str:
    env = _as_dict(current_env)
    if env.get("valid"):
        return "selected"
    if env:
        reason = str(env.get("reason") or "").strip()
        if reason:
            return reason
    plan = _as_dict(current_find_plan)
    status = str(plan.get("base_selection_status") or plan.get("next_required_action") or "").strip()
    return status or "not-run"


def _public_summary(row: dict) -> str:
    row = _as_dict(row)
    return str(row.get("summary_zh") or row.get("summary") or row.get("human_summary") or "").strip()


def _failed_check_ids(gate: dict) -> list[str]:
    gate = _as_dict(gate)
    failed = gate.get("failed_checks") if isinstance(gate.get("failed_checks"), list) else []
    if not failed and isinstance(gate.get("checks"), list):
        failed = [row for row in gate.get("checks", []) if isinstance(row, dict) and row.get("status") != "pass"]
    return [str(row.get("id") or "").strip() for row in failed if isinstance(row, dict) and str(row.get("id") or "").strip()]


def _route_has_identity(route: dict) -> bool:
    route = _as_dict(route)
    return any(str(route.get(key) or "").strip() for key in ["repo", "title", "repo_path", "proposed_path_hint"])


def _selected_base_viability_public_status(gate: dict, base_switch_gate: dict | None = None) -> dict:
    gate = _as_dict(gate)
    base_switch = _as_dict(base_switch_gate)
    status = str(gate.get("status") or "").strip().lower()
    decision = str(gate.get("decision") or "").strip().lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return {}
    semantic = _as_dict(gate.get("semantic_data_provenance_review"))
    text_meta = _as_dict(semantic.get("text_metadata_provenance"))
    dataset = str(text_meta.get("dataset") or gate.get("dataset") or gate.get("selected_dataset") or "").strip()
    text_evidence_value = text_meta.get("has_text_metadata_evidence")
    semantic_required = bool(
        semantic.get("deterministic_gate_required")
        or (
            str(semantic.get("status") or "").strip().lower() == "blocked"
            and bool(semantic.get("project_requires_llm_semantics"))
            and not bool(semantic.get("has_real_llm_embedding_evidence"))
            and text_evidence_value is False
        )
    )
    issue = str(gate.get("issue") or "").strip()
    base_switch_status = str(base_switch.get("status") or "").strip().lower()
    base_switch_decision = str(base_switch.get("decision") or "").strip().lower()
    failed_ids = _failed_check_ids(base_switch)
    candidate_present = _route_has_identity(_as_dict(base_switch.get("candidate_route")))
    base_switch_not_authorized = base_switch_status == "blocked" and base_switch_decision == "base_switch_not_authorized"
    base_switch_fields = {
        "base_switch_gate_status": base_switch_status,
        "base_switch_gate_decision": base_switch_decision,
        "base_switch_candidate_route_present": candidate_present,
        "base_switch_failed_checks": failed_ids[:10],
    }
    if semantic_required:
        summary = issue or (
            "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 数据路线缺少 LLM/text-semantic "
            "实验所需的可审计文本/元数据 provenance；继续运行纯行为或损失级候选实验无法清除此门控。"
        )
        next_action = (
            "运行 deterministic base-switch / semantic-provenance gate；候选路线保持 proposal-only，或补齐当前路线保存 ID "
            "映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe。通过前不继续纯行为级候选实验、不写论文、不提升结论。"
        )
        if base_switch_not_authorized:
            missing_candidate = "candidate_route_proposal_exists" in failed_ids or not candidate_present
            if missing_candidate:
                summary = (
                    "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 数据路线缺少 LLM/text-semantic "
                    "文本/元数据 provenance；确定性 base-switch gate 已执行但未授权，因为还没有独立、可审计、可追溯到当前 "
                    "Find/read 的候选路线 proposal。继续运行纯行为或损失级候选实验无法清除此门控。"
                )
                next_action = (
                    "补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe，"
                    "或生成可追溯到当前 Find/read 的 candidate base-switch proposal，并完成 loader/data/protocol/smoke/"
                    "full-reference/artifact-local audits。通过前不继续纯行为级候选实验、不写论文、不提升结论。"
                )
            else:
                failed_text = "、".join(failed_ids[:5]) or "候选路线证据"
                summary = (
                    "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 数据路线仍缺少 LLM/text-semantic "
                    "文本/元数据 provenance；确定性 base-switch gate 已执行且未授权；"
                    f"候选路线仍有未通过检查：{failed_text}。"
                )
                next_action = (
                    "补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text "
                    "embedding probe；或补齐上列候选路线未通过检查后刷新 deterministic base-switch gate。"
                    "gate 通过前不切换基底、不写论文、不提升结论。"
                )
        return {
            "category": "semantic_data_provenance_required",
            "status": status,
            "decision": decision,
            "summary": summary,
            "next_action": next_action,
            "semantic_status": str(semantic.get("status") or "").strip(),
            "semantic_dataset": dataset,
            "semantic_has_text_metadata_evidence": text_evidence_value,
            "semantic_has_real_llm_embedding_evidence": bool(semantic.get("has_real_llm_embedding_evidence")),
            **base_switch_fields,
        }
    summary = issue or (
        "selected_base_viability_gate: 参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果。"
    )
    return {
        "category": "experiment_evidence_audit",
        "status": status,
        "decision": decision,
        "summary": summary,
        "next_action": "等待项目代理读取当前缺口证据，并给出下一轮实验或修复动作。",
        "semantic_status": str(semantic.get("status") or "").strip(),
        "semantic_dataset": dataset,
        "semantic_has_text_metadata_evidence": text_evidence_value,
        "semantic_has_real_llm_embedding_evidence": bool(semantic.get("has_real_llm_embedding_evidence")),
        **base_switch_fields,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    report = paths.reports / "status.md"
    repo_rows = load_json(paths.state / "repo_candidates.json")
    ds_rows = load_json(paths.state / "dataset_registry.json")
    exp_rows = load_json(paths.state / "experiment_registry.json")
    requests = load_json(paths.state / "natural_language_requests.json")
    plan = load_json(paths.state / "parallel_plan.json")
    ideas = load_json(paths.state / "idea_candidates.json")
    paper_quality = load_json(paths.state / "paper_quality.json")
    active_repo = load_json(paths.state / "active_repo.json") if (paths.state / "active_repo.json").exists() else {}
    repo_data_requirements = load_json(paths.state / "repo_data_requirements.json") if (paths.state / "repo_data_requirements.json").exists() else {}
    data_policy = load_json(paths.state / "data_unavailability_policy.json") if (paths.state / "data_unavailability_policy.json").exists() else {}
    blocker_packet = load_json(paths.state / "blocker_resolution_packet.json") if (paths.state / "blocker_resolution_packet.json").exists() else {}
    repo_selection = load_json(paths.state / "evidence_ready_repo_selection.json") if (paths.state / "evidence_ready_repo_selection.json").exists() else {}
    repo_selection_blocker = load_json(paths.state / "repo_selection_blocker.json") if (paths.state / "repo_selection_blocker.json").exists() else {}
    current_find_plan = load_json(paths.state / "current_find_research_plan.json") if (paths.state / "current_find_research_plan.json").exists() else {}
    finding_frontend = load_json(paths.state / "finding_frontend.json") if (paths.state / "finding_frontend.json").exists() else {}
    full_cycle = load_json(paths.state / "full_research_cycle.json") if (paths.state / "full_research_cycle.json").exists() else {}
    scientific_progress_gate = load_json(paths.state / "scientific_progress_gate.json") if (paths.state / "scientific_progress_gate.json").exists() else {}
    selected_base_viability_gate = load_json(paths.state / "selected_base_viability_gate.json") if (paths.state / "selected_base_viability_gate.json").exists() else {}
    base_switch_gate = load_json(paths.state / "base_switch_gate.json") if (paths.state / "base_switch_gate.json").exists() else {}
    current_environment = guard_current_environment_selection(paths)
    current_environment_valid = bool(_as_dict(current_environment).get("valid"))
    selected_repo = repo_selection.get('selected', {}) if isinstance(repo_selection, dict) else {}
    claude_decision = {}
    claude_decision_scope = "none"
    claude_decision_subject = ""
    pending_candidate = _as_dict(repo_selection.get('pending_environment_candidate')) if isinstance(repo_selection, dict) else {}
    if isinstance(repo_selection, dict) and isinstance(repo_selection.get('claude_topic_decision'), dict):
        claude_decision = repo_selection.get('claude_topic_decision', {})
        if _pending_candidate_blocked(repo_selection) or _candidate_base_switch_blocked(repo_selection):
            claude_decision_scope = "pending_candidate_not_authoritative"
            claude_decision_subject = _candidate_subject(pending_candidate) or _candidate_subject(repo_selection)
        elif _accepted_repo_selection(repo_selection):
            if current_environment_valid:
                claude_decision_scope = "accepted_environment_selection"
            else:
                claude_decision_scope = "stale_environment_selection"
            claude_decision_subject = _candidate_subject(selected_repo) or _candidate_subject(repo_selection)
        else:
            claude_decision_scope = "repo_selection_candidate"
            claude_decision_subject = _candidate_subject(pending_candidate) or _candidate_subject(selected_repo) or _candidate_subject(repo_selection)
    elif isinstance(active_repo, dict) and isinstance(active_repo.get('claude_topic_fit_decision'), dict):
        claude_decision = active_repo.get('claude_topic_fit_decision', {})
        claude_decision_scope = "active_repo"
        claude_decision_subject = _candidate_subject(active_repo)
    claude_accepted_repo_ready = current_environment_valid and bool(selected_repo) and (
        str(repo_selection.get('selection_gate', '') if isinstance(repo_selection, dict) else '').startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate'))
        or bool(claude_decision.get('accept_as_current_best'))
    )
    current_repo = active_repo if isinstance(active_repo, dict) else {}
    if claude_accepted_repo_ready and isinstance(selected_repo, dict) and selected_repo:
        current_repo = {**current_repo, **selected_repo}
        current_repo.setdefault('selection_source', 'evidence_ready_repo_selection')
    current_repo_path = str(current_repo.get('repo_path') or current_repo.get('local_path') or '') if isinstance(current_repo, dict) else ''
    real_probe = load_json(paths.state / "real_dataset_probe.json") if (paths.state / "real_dataset_probe.json").exists() else {}
    bootstrap = load_json(paths.state / "repo_env_bootstrap.json") if (paths.state / "repo_env_bootstrap.json").exists() else {}
    machine = load_json(paths.reports / "machine_profile.json")
    paper_state = get_active_paper_state(args.project, venue=args.venue)
    methods = plan.get("methods", []) if isinstance(plan, dict) else plan
    completed = [row for row in exp_rows if str(row.get("status", "")).lower() in {"completed", "success", "repaired"}]
    failed = [row for row in exp_rows if str(row.get("status", "")).lower() in {"failed", "error", "incomplete_audit"}]
    analyzed_failed = [row for row in failed if row.get("failure_analysis_path")]
    deps = machine.get("dependencies", {}) if isinstance(machine, dict) else {}
    idea_summary = ideas.get("summary", {}) if isinstance(ideas, dict) else {}
    paper_summary = paper_quality.get("summary", {}) if isinstance(paper_quality, dict) else {}
    coding_cfg = cfg.get("coding_agent", {}) if isinstance(cfg, dict) else {}
    coding_backend = "claude"
    coding_state_files = sorted(paths.state.glob("coding_agent_*.json"), key=lambda p: p.stat().st_mtime)
    successful_repairs = 0
    last_coding_backend = ""
    for state_file in coding_state_files:
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("repair_success"):
            successful_repairs += 1
        if payload.get("backend"):
            last_coding_backend = payload.get("backend", "")
    llm_ready = llm_available(cfg)
    llm_reason = "" if llm_ready else llm_disabled_reason(cfg)
    claude_available = bool(find_claude(cfg))
    active_env = bootstrap.get('env_name', '') if isinstance(bootstrap, dict) else ''
    active_env_status = bootstrap.get('status', '') if isinstance(bootstrap, dict) else ''
    active_env_installed = active_env_status == 'completed'
    blocked_datasets = list(repo_data_requirements.get('blocked_datasets', []) if isinstance(repo_data_requirements, dict) else [])
    ready_datasets = list(repo_data_requirements.get('ready_datasets', []) if isinstance(repo_data_requirements, dict) else [])
    if isinstance(real_probe, dict) and str(real_probe.get('repo_path') or '') == current_repo_path:
        for probe in real_probe.get('probes', []) or []:
            if isinstance(probe, dict) and probe.get('claim_ready') and probe.get('loader_probe_success'):
                dataset = str(probe.get('dataset') or '').strip()
                if dataset and dataset not in ready_datasets:
                    ready_datasets.append(dataset)
    blocked_datasets = [item for item in blocked_datasets if item not in set(ready_datasets)]
    blocker_current = _blocker_matches_active_repo(blocker_packet, current_repo)
    stale_blocker = bool(isinstance(blocker_packet, dict) and blocker_packet and not blocker_current)
    data_blocker_cleared = bool(ready_datasets)
    blocker_type = ""
    blocker_evidence_ready_count = ""
    blocker_completion_condition = ""
    if isinstance(blocker_packet, dict) and blocker_current and not data_blocker_cleared:
        blocker_type = str(blocker_packet.get('blocker_type', '') or '')
        blocker_evidence_ready_count = blocker_packet.get('evidence_ready_candidate_count', '')
        blocker_completion_condition = str(blocker_packet.get('completion_condition', '') or '')
    repo_selection_gate = repo_selection.get('selection_gate', '') if isinstance(repo_selection, dict) else ''
    repo_selection_status = _repo_selection_public_status(repo_selection, current_environment)
    pending_candidate_blocked = _pending_candidate_blocked(repo_selection)
    candidate_base_switch_blocked = _candidate_base_switch_blocked(repo_selection)
    current_active_route_ready = bool(current_repo_path and ready_datasets)
    repo_selection_blocker_current = _repo_selection_blocker_is_current(repo_selection_blocker, repo_selection)
    repo_selection_blocker_stale = bool(isinstance(repo_selection_blocker, dict) and repo_selection_blocker and not repo_selection_blocker_current)
    repo_selection_block_reason = repo_selection_blocker.get('reason', '') if repo_selection_blocker_current and isinstance(repo_selection_blocker, dict) else ''
    if pending_candidate_blocked or candidate_base_switch_blocked:
        repo_selection_blocker_stale = bool(repo_selection_blocker)
        repo_selection_blocker_current = False
        default_reason = 'Pending candidate route is blocked until loader/data/protocol audits pass; active_repo remains non-authoritative for the current selected plan.'
        if candidate_base_switch_blocked:
            default_reason = 'Candidate loader/data evidence passed, but deterministic base-switch reference protocol/smoke/full reproduction/artifact-local audit gates remain blocked; active_repo is unchanged.'
        repo_selection_block_reason = str(repo_selection.get('blocker') or default_reason)
    current_find_run_id = _state_run_id(current_find_plan) or _state_run_id(finding_frontend) or _state_run_id(full_cycle)
    current_find_status = str(_as_dict(current_find_plan).get('status') or _as_dict(finding_frontend).get('status') or '').strip()
    environment_selection_status = _environment_selection_status(repo_selection, current_find_plan, current_environment)
    full_cycle_status = str(_as_dict(full_cycle).get('status') or '').strip() or 'not-run'
    full_cycle_summary = _public_summary(full_cycle)
    scientific_progress_status = str(_as_dict(scientific_progress_gate).get('status') or '').strip() or 'not-run'
    scientific_progress_summary = _public_summary(scientific_progress_gate)
    selected_base_viability_status = _selected_base_viability_public_status(selected_base_viability_gate, base_switch_gate)
    if selected_base_viability_status:
        full_cycle_summary = selected_base_viability_status.get('summary') or full_cycle_summary
        scientific_progress_summary = selected_base_viability_status.get('summary') or scientific_progress_summary
    claude_rationale = claude_decision.get('rationale', '') if isinstance(claude_decision, dict) else ''
    if claude_rationale and claude_decision_scope == "pending_candidate_not_authoritative":
        claude_rationale = f"pending candidate only; active_repo unchanged: {claude_rationale}"
    lines = [
        "# Workflow Status\n\n",
        f"- project: {cfg.get('name', args.project)}\n",
        f"- topic: {cfg.get('topic', '')}\n",
        f"- conda_env: {active_env or cfg.get('conda_env', '')}\n",
        f"- configured_conda_env: {cfg.get('conda_env', '')}\n",
        f"- active_repo: {current_repo.get('name', '') if isinstance(current_repo, dict) else ''}\n",
        f"- active_repo_path: {current_repo_path}\n",
        f"- active_repo_env: {active_env}\n",
        f"- active_repo_env_status: {active_env_status or 'unknown'}\n",
        f"- active_repo_env_installed: {active_env_installed}\n",
        f"- active_repo_ready_datasets: {', '.join(ready_datasets) if ready_datasets else 'none'}\n",
        f"- active_repo_blocked_datasets: {', '.join(blocked_datasets) if blocked_datasets else 'none'}\n",
        f"- claude_accepted_transformable_repo: {claude_accepted_repo_ready}\n",
        f"- claude_repo_decision_scope: {claude_decision_scope}\n",
        f"- claude_repo_decision_subject: {claude_decision_subject}\n",
        f"- claude_repo_decision: {claude_decision.get('decision', '') if isinstance(claude_decision, dict) else ''}\n",
        f"- claude_repo_confidence: {claude_decision.get('confidence', '') if isinstance(claude_decision, dict) else ''}\n",
        f"- claude_repo_rationale: {claude_rationale}\n",
        f"- current_find_run_id: {current_find_run_id}\n",
        f"- current_find_downstream_status: {current_find_status or 'unknown'}\n",
        f"- environment_base_selection_status: {environment_selection_status}\n",
        f"- full_cycle_status: {full_cycle_status}\n",
        f"- full_cycle_summary: {full_cycle_summary}\n",
        f"- scientific_progress_gate_status: {scientific_progress_status}\n",
        f"- scientific_progress_gate_summary: {scientific_progress_summary}\n",
        f"- selected_base_viability_gate_status: {selected_base_viability_status.get('status', 'not-run') if selected_base_viability_status else str(_as_dict(selected_base_viability_gate).get('status') or 'not-run')}\n",
        f"- selected_base_viability_gate_decision: {selected_base_viability_status.get('decision', '') if selected_base_viability_status else str(_as_dict(selected_base_viability_gate).get('decision') or '')}\n",
        f"- current_blocker_category: {selected_base_viability_status.get('category', '') if selected_base_viability_status else ''}\n",
        f"- current_blocker_summary: {selected_base_viability_status.get('summary', '') if selected_base_viability_status else ''}\n",
        f"- current_blocker_next_action: {selected_base_viability_status.get('next_action', '') if selected_base_viability_status else ''}\n",
        f"- semantic_data_provenance_status: {selected_base_viability_status.get('semantic_status', '') if selected_base_viability_status else ''}\n",
        f"- semantic_data_provenance_dataset: {selected_base_viability_status.get('semantic_dataset', '') if selected_base_viability_status else ''}\n",
        f"- semantic_data_provenance_has_text_metadata_evidence: {selected_base_viability_status.get('semantic_has_text_metadata_evidence', '') if selected_base_viability_status else ''}\n",
        f"- semantic_data_provenance_has_real_llm_embedding_evidence: {selected_base_viability_status.get('semantic_has_real_llm_embedding_evidence', '') if selected_base_viability_status else ''}\n",
        f"- base_switch_gate_status: {selected_base_viability_status.get('base_switch_gate_status', '') if selected_base_viability_status else str(_as_dict(base_switch_gate).get('status') or '')}\n",
        f"- base_switch_gate_decision: {selected_base_viability_status.get('base_switch_gate_decision', '') if selected_base_viability_status else str(_as_dict(base_switch_gate).get('decision') or '')}\n",
        f"- base_switch_candidate_route_present: {selected_base_viability_status.get('base_switch_candidate_route_present', '') if selected_base_viability_status else ''}\n",
        f"- base_switch_failed_checks: {', '.join(selected_base_viability_status.get('base_switch_failed_checks', [])) if selected_base_viability_status else ''}\n",
        f"- repo_selection_status: {repo_selection_status}\n",
        f"- repo_selection_gate: {repo_selection_gate or 'not-run'}\n",
        f"- repo_selection_block_reason: {repo_selection_block_reason or 'none'}\n",
        f"- repo_selection_blocker_stale_ignored: {repo_selection_blocker_stale}\n",
        f"- data_unavailability_decision: {data_policy.get('decision', '') if isinstance(data_policy, dict) else ''}\n",
        f"- blocker_type: {blocker_type}\n",
        f"- evidence_ready_candidate_count: {blocker_evidence_ready_count}\n",
        f"- blocker_completion_condition: {blocker_completion_condition}\n",
        f"- stale_blocker_packet_ignored: {stale_blocker}\n",
        f"- user_prompt: {cfg.get('user_prompt', '')}\n",
        f"- active_paper_venue: {paper_state.get('venue', '') if isinstance(paper_state, dict) else ''}\n",
        f"- configured_coding_backend: {coding_backend}\n",
        f"- llm_backend_ready: {llm_ready}\n",
        f"- llm_backend_reason: {llm_reason or 'ready'}\n",
        f"- claude_backend_available: {claude_available}\n",
        f"- coding_agent_runs: {len(coding_state_files)}\n",
        f"- coding_agent_successful_repairs: {successful_repairs}\n",
        f"- coding_agent_last_backend: {last_coding_backend or 'none'}\n",
        f"- discovery snapshots: {len(list(paths.discover.glob('*.json')))}\n",
        f"- ingested paper folders: {count_dirs(paths.raw_papers)}\n",
        f"- recent_high_priority_papers: {paper_summary.get('recent_high_priority_count', 0)}\n",
        f"- recent_candidate_papers: {paper_summary.get('recent_candidate_count', 0)}\n",
        f"- idea_candidates: {idea_summary.get('idea_count', 0)}\n",
        f"- pursue_ready_ideas: {idea_summary.get('pursue_count', 0)}\n",
        f"- wiki paper pages: {len(list(paths.wiki_papers.glob('*.md')))}\n",
        f"- wiki concept pages: {len(list(paths.wiki_concepts.glob('*.md')))}\n",
        f"- wiki entity pages: {len(list(paths.wiki_entities.glob('*.md')))}\n",
        f"- comparison pages: {len(list(paths.wiki_comparisons.glob('*.md')))}\n",
        f"- repo candidates: {len(repo_rows)}\n",
        f"- datasets tracked: {len(ds_rows)}\n",
        f"- methods in parallel plan: {len(methods)}\n",
        f"- experiments logged: {len(exp_rows)}\n",
        f"- experiments completed: {len(completed)}\n",
        f"- experiments failed_or_incomplete: {len(failed)}\n",
        f"- failed runs with analysis: {len(analyzed_failed)}\n",
        f"- natural-language requests logged: {len(requests) if isinstance(requests, list) else 0}\n",
        f"- environment bootstrap prepared: {(paths.state / 'repo_env_bootstrap.json').exists()}\n",
        f"- next actions generated: {(paths.state / 'next_actions.json').exists()}\n",
        f"- evolution memory ready: {(paths.state / 'evolution_memory.json').exists()}\n",
        f"- workflow blueprint ready: {(paths.planning / 'workflow_blueprint.md').exists()}\n",
        f"- workflow connectivity audit ready: {(paths.reports / 'workflow_connectivity.md').exists()}\n",
        f"- work status ready: {paths.work_status.exists()}\n",
        f"- machine profile ready: {(paths.reports / 'machine_profile.json').exists()}\n",
        f"- dependency install plan ready: {(paths.reports / 'dependency_install_plan.md').exists()}\n",
        f"- core runtime ready: {deps.get('ready_for_core_loop', False) if isinstance(deps, dict) else False}\n",
        f"- latex runtime ready: {deps.get('ready_for_latex', False) if isinstance(deps, dict) else False}\n",
        f"- required runtime gaps: {', '.join(deps.get('required_missing', [])) if isinstance(deps, dict) and deps.get('required_missing') else 'none'}\n",
        f"- recommended runtime gaps: {', '.join(deps.get('recommended_missing', [])) if isinstance(deps, dict) and deps.get('recommended_missing') else 'none'}\n",
        f"- paper draft ready: {paper_state.get('draft_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- paper review packet ready: {paper_state.get('review_packet_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- internal paper reviews ready: {paper_state.get('internal_reviews_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- aggregated review ready: {paper_state.get('paper_reviews_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- aggregated review verdict: {paper_state.get('paper_review_verdict', '') if isinstance(paper_state, dict) else ''}\n",
        f"- author response ready: {paper_state.get('author_response_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- re-review ready: {paper_state.get('re_review_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- re-review verdict: {paper_state.get('re_review_verdict', '') if isinstance(paper_state, dict) else ''}\n",
        f"- promotion gate: {paper_state.get('promotion_gate', '') if isinstance(paper_state, dict) else ''}\n",
        f"- revised draft ready: {paper_state.get('paper_revision_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- venue template fetched: {paper_state.get('template_fetched', False) if isinstance(paper_state, dict) else False}\n",
        f"- venue template format ready: {paper_state.get('venue_template_format_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- venue template format status: {paper_state.get('paper_venue_format_status', '') if isinstance(paper_state, dict) else ''}\n",
        f"- template fetch error: {paper_state.get('template_fetch_error', '') if isinstance(paper_state, dict) else ''}\n",
        f"- render ready: {paper_state.get('render_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- pdf ready: {paper_state.get('pdf_ready', False) if isinstance(paper_state, dict) else False}\n",
        f"- draft hypotheses present: {'status: draft' in (paths.wiki_gaps / 'hypotheses.md').read_text(encoding='utf-8') if (paths.wiki_gaps / 'hypotheses.md').exists() else False}\n",
        f"- ingested ids tracked: {len(load_json(paths.state / 'ingested_ids.json'))}\n",
        f"- compiled ids tracked: {len(load_json(paths.state / 'compiled_ids.json'))}\n",
        f"- loop runs tracked: {len(load_json(paths.state / 'loop_history.json'))}\n",
        "- standalone_runner: framework/scripts/run_autonomous_research.py\n",
    ]
    report.write_text("".join(lines), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
