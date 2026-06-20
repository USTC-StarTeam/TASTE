#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from experiment_contracts import align_reference_best_control, experiment_rows, llm_semantic_promotion_guard, scientific_progress_gate
from paper_common import ensure_paper_dirs, get_active_paper_state, load_json, read_text, slugify, update_pipeline_state, venue_submission_policy, write_json, write_text
from project_paths import build_paths, load_project_config
from audit_reference_reproduction import build_reference_reproduction_gate
from audit_experiment_runtime_integrity import build_runtime_integrity_audit
from paper_self_review import validate_paper_self_review_receipt


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def pass_check(name: str, ok: bool, detail: str, evidence: list[str], *, severity: str = "block") -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "status": "pass" if ok else severity,
        "severity": "pass" if ok else severity,
        "detail": detail,
        "evidence": evidence,
    }


def module_status(checks: list[dict[str, Any]]) -> str:
    if any(row.get("severity") == "block" or row.get("status") == "block" for row in checks):
        return "blocked"
    if any(row.get("severity") == "warn" or row.get("status") == "warn" for row in checks):
        return "warn"
    return "pass"


def real_ready_datasets(paths) -> set[str]:
    registry = load_json(paths.state / "dataset_registry.json", [])
    ready = {
        str(row.get("name") or row.get("dataset"))
        for row in registry
        if isinstance(row, dict)
        and row.get("claim_ready")
        and (row.get("loader_probe_success") or row.get("probe_success"))
        and not str(row.get("name") or row.get("dataset") or "").startswith("synthetic")
    } if isinstance(registry, list) else set()
    req = load_json(paths.state / "repo_data_requirements.json", {})
    if not ready and isinstance(req, dict):
        ready.update(str(name) for name in req.get("ready_datasets", []) or [] if str(name).strip())
    return {name for name in ready if name}




def _norm_path(value: Any) -> str:
    return str(value or '').rstrip('/')


def _payload_run_id(payload: Any) -> str:
    return str(payload.get('run_id') or payload.get('source_run_id') or payload.get('find_run_id') or '').strip() if isinstance(payload, dict) else ''


def _current_find_run_id(paths) -> str:
    for path in [
        paths.planning / 'finding' / 'find_progress.json',
        paths.state / 'current_find_research_plan.json',
        paths.state / 'literature_tool_packet.json',
    ]:
        run_id = _payload_run_id(load_json(path, {}))
        if run_id:
            return run_id
    return ''


def _current_selection_context(paths, active_repo: dict[str, Any]) -> dict[str, Any]:
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    selected = selection.get('selected', {}) if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
    impl = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
    impl_repo = impl.get('repo', {}) if isinstance(impl, dict) and isinstance(impl.get('repo'), dict) else {}
    impl_selected = impl.get('selected_base', {}) if isinstance(impl, dict) and isinstance(impl.get('selected_base'), dict) else {}
    current_run = _current_find_run_id(paths)
    selected_run = str((selection.get('fresh_find_run_id') if isinstance(selection, dict) else '') or selected.get('fresh_find_run_id') or '').strip()
    stage = str((selection.get('selection_stage') if isinstance(selection, dict) else '') or (selection.get('selected_by_stage') if isinstance(selection, dict) else '') or '').strip()
    selected_is_current = bool(selected and stage == 'environment_claude_code' and (not current_run or not selected_run or selected_run == current_run))

    source = 'active_repo_fallback'
    repo_path = ''
    repo_name = ''
    title = ''
    if selected_is_current:
        source = 'evidence_ready_repo_selection'
        for value in [selected.get('repo_path'), selected.get('local_path'), impl_repo.get('repo_path'), impl_repo.get('local_path')]:
            if str(value or '').strip():
                repo_path = _norm_path(value)
                break
        repo_name = str(selected.get('name') or selected.get('repo') or selected.get('url') or impl_repo.get('name') or impl_repo.get('repo') or '').strip()
        title = str(selected.get('literature_base_title') or selected.get('selected_base_title') or selected.get('title') or impl_selected.get('literature_base_title') or impl_selected.get('selected_base_title') or impl_selected.get('title') or '').strip()
    else:
        for value in [active_repo.get('repo_path') if isinstance(active_repo, dict) else '', active_repo.get('local_path') if isinstance(active_repo, dict) else '', impl_repo.get('repo_path'), impl_repo.get('local_path')]:
            if str(value or '').strip():
                repo_path = _norm_path(value)
                break
        repo_name = str((active_repo.get('name') if isinstance(active_repo, dict) else '') or impl_repo.get('name') or impl_repo.get('repo') or '').strip()
        title = str((active_repo.get('selected_base_title') if isinstance(active_repo, dict) else '') or impl_selected.get('literature_base_title') or impl_selected.get('selected_base_title') or impl_selected.get('title') or '').strip()
    return {'repo_path': repo_path, 'repo_name': repo_name, 'title': title, 'source': source, 'current_find_run_id': current_run, 'selected_fresh_find_run_id': selected_run}


def _rows_for_current_repo(rows: list[dict[str, Any]], active_repo_path: str) -> list[dict[str, Any]]:
    if not active_repo_path:
        return rows
    active_norm = _norm_path(active_repo_path)
    return [row for row in rows if isinstance(row, dict) and _norm_path(row.get('repo_path')) == active_norm]


def _experiment_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ['experiment_id', 'name', 'id']:
            value = str(row.get(key) or '').strip()
            if value:
                index[value] = row
    return index


def _current_claim_guard(claim_ledger: dict[str, Any], experiments: list[dict[str, Any]], current: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    repo_path = _norm_path(current.get('repo_path'))
    repo_name = str(current.get('repo_name') or '')
    title = str(current.get('title') or '')
    if not repo_path:
        return [], ['Current selected-base repo path is unavailable; claim ledger cannot be matched to the active route.']
    rows_by_id = _experiment_index(experiments)
    claims = claim_ledger.get('claims', []) if isinstance(claim_ledger, dict) and isinstance(claim_ledger.get('claims'), list) else []
    if not claims:
        return [], ['Claim ledger is empty for the current selected-base route.']
    stale_markers = []
    current_markers = [repo_name, title, repo_path.split('/')[-1]]
    current_markers = [m.lower() for m in current_markers if str(m or '').strip()]
    current_supported: list[dict[str, Any]] = []
    issues: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_type = str(claim.get('claim_type') or claim.get('id') or 'claim')
        status = str(claim.get('status') or '').lower()
        if status in {'weak', 'unsupported'}:
            issues.append(f"Claim `{claim_type}` status={status}.")
            continue
        explicit_repo = _norm_path(claim.get('repo_path') or claim.get('active_repo_path') or claim.get('local_path'))
        if explicit_repo and explicit_repo != repo_path:
            issues.append(f"Claim `{claim_type}` is tied to non-current repo_path={explicit_repo}; current selected repo is {repo_path}.")
            continue
        run_ids = [str(x or '').strip() for x in (claim.get('supporting_runs') or claim.get('supported_by') or []) if str(x or '').strip()]
        matched_current_runs = [rid for rid in run_ids if _norm_path((rows_by_id.get(rid) or {}).get('repo_path')) == repo_path]
        blob = ' '.join(str(claim.get(key) or '') for key in ['text', 'claim', 'summary', 'repo', 'base_repo', 'method'])
        blob = (blob + ' ' + ' '.join(run_ids)).lower()
        has_stale_marker = any(marker in blob for marker in stale_markers)
        has_current_marker = any(marker in blob for marker in current_markers)
        if matched_current_runs:
            current_supported.append(claim)
        elif run_ids:
            issues.append(f"Claim `{claim_type}` has no supporting run from the current selected repo; supporting_runs={run_ids[:6]}.")
        elif has_stale_marker or not has_current_marker:
            issues.append(f"Claim `{claim_type}` is not grounded in the current selected-base route ({repo_name or repo_path}).")
    if not current_supported:
        issues.append(f"Claim ledger has no supported claim backed by current selected repo experiments: {repo_name or repo_path}.")
    return current_supported, issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether a workflow paper is truly submission-ready.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    os.environ["PROJECT_ID"] = args.project

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)
    state = get_active_paper_state(args.project, venue=args.venue)
    venue = args.venue or state.get("venue", "")
    venue_slug = slugify(venue) if venue else ""
    paper_orchestra = load_json(paths.state / "paper_orchestra_state.json", {})
    paper_orchestra_audit = load_json(paths.state / "paper_orchestra_audit.json", {})
    claim_ledger = load_json(paths.state / "claim_ledger.json", {"claims": []})
    paper_evidence_json = load_json(paths.state / "paper_evidence_audit.json", {})
    experiments = load_json(paths.state / "experiment_registry.json", [])
    active_repo = load_json(paths.state / "active_repo.json", {})
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    manifest = load_json(paths.state / "research_evidence_manifest.json", {})
    trajectory = load_json(paths.state / "research_trajectory_system.json", {})
    figure_quality = load_json(paths.state / "paper_figure_quality_audit.json", {})
    normality_audit = load_json(paths.state / "paper_normality_audit.json", {})
    submission_actions = load_json(paths.state / "submission_actions.json", {})
    try:
        runtime_integrity = build_runtime_integrity_audit(args.project)
    except Exception as exc:
        runtime_integrity = load_json(paths.state / "experiment_runtime_integrity.json", {})
        if not isinstance(runtime_integrity, dict):
            runtime_integrity = {}
        runtime_integrity = dict(runtime_integrity)
        issues = runtime_integrity.get("issues") if isinstance(runtime_integrity.get("issues"), list) else []
        issues.append({"severity": "block", "issue": f"runtime integrity refresh failed: {exc}", "evidence": [str(paths.state / "experiment_runtime_integrity.json")]})
        runtime_integrity["issues"] = issues
        runtime_integrity["status"] = "blocked"
        runtime_integrity["current"] = False
    iteration_audit = load_json(paths.state / "experiment_iteration_audit.json", {})
    find_progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    literature_packet = load_json(paths.state / "literature_tool_packet.json", {})
    packet_summary = literature_packet.get("summary", {}) if isinstance(literature_packet, dict) and isinstance(literature_packet.get("summary"), dict) else {}
    literature_actual = int(find_progress.get("strong_recommendation_count") or packet_summary.get("strong_paper_anchors") or 0) if isinstance(find_progress, dict) else int(packet_summary.get("strong_paper_anchors") or 0)
    literature_target = int(find_progress.get("recommendation_target_count") or packet_summary.get("recommendation_target_count") or 0) if isinstance(find_progress, dict) else int(packet_summary.get("recommendation_target_count") or 0)
    literature_shortfall = int(find_progress.get("recommendation_shortfall") or max(0, literature_target - literature_actual) if literature_target else packet_summary.get("recommendation_shortfall") or 0) if isinstance(find_progress, dict) else int(packet_summary.get("recommendation_shortfall") or 0)
    readiness_inputs = {
        "paper_orchestra_state": str(paths.state / "paper_orchestra_state.json"),
        "paper_orchestra_audit": str(paths.state / "paper_orchestra_audit.json"),
        "paper_evidence_audit": str(paths.reports / "paper_evidence_audit.md"),
        "paper_evidence_audit_json": str(paths.state / "paper_evidence_audit.json"),
        "claim_ledger": str(paths.state / "claim_ledger.json"),
        "experiment_registry": str(paths.state / "experiment_registry.json"),
        "research_assurance_layer": str(paths.state / "research_assurance_layer.json"),
        "research_evidence_manifest": str(paths.state / "research_evidence_manifest.json"),
        "research_trajectory_system": str(paths.state / "research_trajectory_system.json"),
        "paper_figure_quality_audit": str(paths.state / "paper_figure_quality_audit.json"),
        "paper_normality_audit": str(paths.state / "paper_normality_audit.json"),
        "submission_actions": str(paths.state / "submission_actions.json"),
        "pipeline_state": str(paper["pipeline_state"]),
        "paper_self_review_receipt": str(paths.state / "paper_preview_self_review.json"),
    }

    exp_rows = experiment_rows(experiments)
    ready_real = real_ready_datasets(paths)
    current_context = _current_selection_context(paths, active_repo if isinstance(active_repo, dict) else {})
    active_repo_path = str(current_context.get("repo_path") or (active_repo.get("repo_path") if isinstance(active_repo, dict) else "") or "")
    current_exp_rows = _rows_for_current_repo(exp_rows, active_repo_path)
    completed = [row for row in current_exp_rows if isinstance(row, dict) and str(row.get("status", "")).lower() in {"completed", "success"}]
    audit_ready = [row for row in completed if row.get("audit_ready")]
    real_audit_ready = [row for row in audit_ready if str(row.get("dataset")) in ready_real and not str(row.get("dataset", "")).startswith("synthetic")]
    margin = float(cfg.get("decision_policy", {}).get("minimum_meaningful_gain", 0.005) or 0.005) if isinstance(cfg, dict) else 0.005
    reproduction_gate = build_reference_reproduction_gate(args.project)
    current_reference_dataset = str((reproduction_gate.get("best_reproduction", {}) if isinstance(reproduction_gate.get("best_reproduction"), dict) else {}).get("dataset") or "").strip()
    progress_gate = scientific_progress_gate(
        exp_rows,
        ready_real_datasets=ready_real,
        active_repo_path=active_repo_path,
        active_dataset=current_reference_dataset,
        margin=margin,
        method_role_policy=cfg.get("experiment", {}).get("method_role_policy", {}) if isinstance(cfg, dict) else {},
    )
    llm_semantic_guard = llm_semantic_promotion_guard(paths, exp_rows, active_repo_path, claim_ledger)
    if llm_semantic_guard.get("status") != "pass" and llm_semantic_guard.get("project_requires_llm_semantics"):
        blockers = list(progress_gate.get("blockers", [])) if isinstance(progress_gate.get("blockers", []), list) else []
        blockers.append(
            "LLM semantic evidence guard is blocked; no candidate can count as promotable scientific progress until real item-text/API embedding evidence passes. "
            + "; ".join(str(item) for item in llm_semantic_guard.get("issues", [])[:3])
        )
        progress_gate["status"] = "blocked"
        progress_gate["comparison_pass"] = False
        progress_gate["best_candidate"] = {}
        progress_gate["llm_semantic_evidence_guard"] = "blocked"
        progress_gate["blockers"] = blockers
    progress_gate = align_reference_best_control(progress_gate, reproduction_gate)
    write_json(paths.state / "scientific_progress_gate.json", progress_gate)
    write_json(paths.state / "reference_reproduction_gate.json", reproduction_gate)
    claim_rows = claim_ledger.get("claims", []) if isinstance(claim_ledger.get("claims", []), list) else []
    current_claim_rows, current_claim_issues = _current_claim_guard(claim_ledger, exp_rows, current_context)
    promotable_claim_statuses = {'supported', 'partially_supported', 'partial', 'promising'}
    weak_claims = [row for row in claim_rows if str(row.get("status", "")).lower() not in promotable_claim_statuses]
    section_rows = paper_orchestra.get("sections", []) if isinstance(paper_orchestra.get("sections", []), list) else []
    blocked_sections = [row for row in section_rows if row.get("status") == "blocked"]
    revision_sections = [row for row in section_rows if row.get("status") == "needs_revision"]
    citation_count = int(paper_orchestra.get("citations", {}).get("candidate_count", 0) or 0) if isinstance(paper_orchestra.get("citations", {}), dict) else 0
    verified_citation_count = int(paper_orchestra.get("citations", {}).get("verified_candidate_count", 0) or 0) if isinstance(paper_orchestra.get("citations", {}), dict) else 0
    output_dir = paper["output_dir"] / venue_slug if venue_slug else paper["output_dir"]
    pdf_path = Path(state.get("latest_preview_pdf") or state.get("blocked_preview_pdf") or state.get("pdf_path") or output_dir / "paper.pdf")
    tex_path = Path(state.get("latest_preview_tex") or state.get("blocked_preview_tex") or state.get("rendered_tex") or output_dir / "paper.tex")
    refs_path = output_dir / "refs.bib"
    paper_self_review = validate_paper_self_review_receipt(
        paths.root,
        venue,
        current_pdf=pdf_path if pdf_path.exists() else None,
        current_tex=tex_path if tex_path.exists() else None,
        current_refs=refs_path if refs_path.exists() else None,
    ) if venue else {"evidence_blockers": [], "evidence_blocker_count": 0, "submission_evidence_ready": False}
    direct_self_review_evidence_blockers = paper_self_review.get("evidence_blockers", []) if isinstance(paper_self_review.get("evidence_blockers", []), list) else []
    evidence_audit_self_review_blockers = paper_evidence_json.get("paper_self_review_evidence_blockers", []) if isinstance(paper_evidence_json, dict) and isinstance(paper_evidence_json.get("paper_self_review_evidence_blockers", []), list) else []
    self_review_evidence_blockers = direct_self_review_evidence_blockers or evidence_audit_self_review_blockers
    paper_evidence = read_text(paths.reports / "paper_evidence_audit.md")
    venue_policy = venue_submission_policy(venue, project=args.project) if venue else {}
    normality_metrics = normality_audit.get("metrics", {}) if isinstance(normality_audit.get("metrics", {}), dict) else {}
    body_pages = int(normality_metrics.get("body_pages") or state.get("paper_normality_body_pages") or 0)
    total_pages = int(normality_metrics.get("pages") or state.get("paper_normality_pages") or 0)
    reference_pages = int(normality_metrics.get("estimated_reference_pages") or state.get("paper_normality_estimated_reference_pages") or 0)
    body_min = int(venue_policy.get("body_page_min") or 0)
    body_max = int(venue_policy.get("body_page_max") or 0)
    # Load capability audit directly (avoids ordering issue where the trajectory
    # system summary hasn't been updated yet when submission_readiness runs).
    cap_audit_path = paths.state / "research_trajectory_capability_audit.json"
    capability_audit = load_json(cap_audit_path, {})
    trajectory_capability_status = capability_audit.get("capability_status", "") if isinstance(capability_audit, dict) else ""
    total_max = int(venue_policy.get("total_page_max") or 0)
    reference_max = int(venue_policy.get("reference_page_max") or 0)
    venue_failed = [
        row for row in (normality_audit.get("failed_checks", []) if isinstance(normality_audit.get("failed_checks", []), list) else [])
        if isinstance(row, dict) and str(row.get("id") or "").startswith(("venue_", "body_page", "total_page", "reference_page", "anonymous"))
    ]
    reviewer_nomination_detail = str(venue_policy.get("reviewer_nomination_detail") or "Reviewer nomination is required by the target venue; record completion in state/submission_actions.json")
    reviewer_nomination_done = bool(
        isinstance(submission_actions, dict)
        and (
            submission_actions.get("cikm_author_reviewer_nominated")
            or submission_actions.get("author_reviewer_nominated")
            or (
                isinstance(submission_actions.get("venue_actions", {}), dict)
                and submission_actions.get("venue_actions", {}).get(slugify(venue), {}).get("author_reviewer_nominated")
            )
        )
    )
    body_requirement_detail = (
        f"required={body_min}-{body_max}"
        if body_min or body_max
        else "no hard body-page range recorded"
    )
    total_requirement_detail = f"required<= {total_max}" if total_max else "no hard total-page cap recorded"
    reference_requirement_detail = f"required<= {reference_max}" if reference_max else "no hard reference-page cap recorded"

    checks = [
        pass_check(
            "literature_strong_recommendation_gate",
            literature_shortfall <= 0,
            f"strong_recommendations={literature_actual}/{literature_target}; shortfall={literature_shortfall}; source=planning/finding/find_progress.json",
            [str(paths.planning / "finding" / "find_progress.json"), str(paths.state / "literature_tool_packet.json")],
        ),
        pass_check(
            "venue_policy_known",
            (not venue) or venue_policy.get("status") == "known",
            f"venue_policy_status={venue_policy.get('status', '')}; source={venue_policy.get('source_url', '')}",
            [readiness_inputs["paper_normality_audit"]],
        ),
        pass_check(
            "venue_hard_requirements_pass",
            not venue_failed,
            f"paper_normality_status={normality_audit.get('status', '')}; venue_failed_checks={len(venue_failed)}",
            [readiness_inputs["paper_normality_audit"]],
        ),
        pass_check(
            "paper_quality_normality_pass",
            normality_audit.get("status") == "pass",
            f"paper_normality_status={normality_audit.get('status', '')}; quality_or_shape_failed_checks={len(normality_audit.get('failed_checks', []) if isinstance(normality_audit.get('failed_checks', []), list) else [])}",
            [readiness_inputs["paper_normality_audit"]],
        ),
        pass_check(
            "venue_body_page_target",
            (not body_min and not body_max) or (body_min <= body_pages <= body_max),
            f"body_pages={body_pages}; {body_requirement_detail}; total_pages={total_pages}; estimated_reference_pages={reference_pages}",
            [readiness_inputs["paper_normality_audit"]],
        ),
        pass_check(
            "venue_total_page_limit",
            (not total_max) or total_pages <= total_max,
            f"total_pages={total_pages}; {total_requirement_detail}",
            [readiness_inputs["paper_normality_audit"]],
        ),
        pass_check(
            "venue_reference_page_limit",
            (not reference_max) or reference_pages <= reference_max,
            f"estimated_reference_pages={reference_pages}; {reference_requirement_detail}",
            [readiness_inputs["paper_normality_audit"]],
        ),
        pass_check(
            "venue_author_reviewer_nomination",
            (not venue_policy.get("reviewer_nomination_required")) or reviewer_nomination_done,
            reviewer_nomination_detail,
            [readiness_inputs["submission_actions"]],
        ),
        pass_check(
            "paper_orchestra_state_pass",
            paper_orchestra.get("status") == "pass",
            f"paper_orchestra_state.status={paper_orchestra.get('status', '')}",
            [readiness_inputs["paper_orchestra_state"]],
        ),
        pass_check(
            "paper_orchestra_audit_pass",
            paper_orchestra_audit.get("status") in ("pass", "submission_ready"),
            f"paper_orchestra_audit.status={paper_orchestra_audit.get('status', '')}",
            [readiness_inputs["paper_orchestra_audit"]],
        ),
        pass_check(
            "no_blocked_sections",
            not blocked_sections and not revision_sections,
            f"blocked_sections={len(blocked_sections)} revision_sections={len(revision_sections)}",
            [readiness_inputs["paper_orchestra_state"]],
        ),
        pass_check(
            "evidence_gate_allows_template",
            "promotion_gate_recommendation: hold-markdown-only" not in paper_evidence,
            "paper_evidence_audit must not recommend hold-markdown-only",
            [readiness_inputs["paper_evidence_audit"]],
        ),
        pass_check(
            "paper_evidence_audit_json_pass",
            isinstance(paper_evidence_json, dict) and paper_evidence_json.get("status") == "pass" and paper_evidence_json.get("promotion_gate_recommendation") == "allow-template",
            f"paper_evidence_audit_json.status={paper_evidence_json.get('status', '') if isinstance(paper_evidence_json, dict) else 'missing'}; gate={paper_evidence_json.get('promotion_gate_recommendation', '') if isinstance(paper_evidence_json, dict) else 'missing'}",
            [readiness_inputs["paper_evidence_audit_json"]],
        ),
        pass_check(
            "paper_self_review_evidence_ready",
            bool(paper_self_review.get("ready")) and not self_review_evidence_blockers,
            f"self_review_status={paper_self_review.get('status')}; preview_ready={paper_self_review.get('ready')}; evidence_blockers={len(self_review_evidence_blockers)}; preview_only_ready={paper_self_review.get('preview_only_ready')}",
            [readiness_inputs["paper_self_review_receipt"], readiness_inputs["paper_evidence_audit_json"]],
        ),
        pass_check(
            "llm_semantic_embedding_evidence_guard",
            llm_semantic_guard.get("status") == "pass",
            f"status={llm_semantic_guard.get('status')}; real_llm_embedding_evidence={llm_semantic_guard.get('has_real_llm_embedding_evidence')}; issues={llm_semantic_guard.get('issues', [])[:4]}",
            [readiness_inputs["experiment_registry"], readiness_inputs["claim_ledger"], str(paths.state / "guidance_queue.json"), str(Path(active_repo_path) / "generate_semantic_embeddings.py") if active_repo_path else ""],
        ),
        pass_check(
            "claim_ledger_supported",
            bool(current_claim_rows) and not weak_claims and not current_claim_issues,
            f"claims={len(claim_rows)} current_selected_repo_supported={len(current_claim_rows)} weak_or_unsupported={len(weak_claims)} current_claim_issues={current_claim_issues[:3]}",
            [readiness_inputs["claim_ledger"], str(paths.state / "active_repo.json"), str(paths.state / "evidence_ready_repo_selection.json")],
        ),
        pass_check(
            "real_audit_ready_experiment",
            len(real_audit_ready) > 0,
            f"real_audit_ready={len(real_audit_ready)} ready_real_datasets={sorted(ready_real)}",
            [readiness_inputs["experiment_registry"]],
        ),
        pass_check(
            "scientific_progress_gate_pass",
            progress_gate.get("status") == "pass",
            (
                f"status={progress_gate.get('status')}; metric={progress_gate.get('metric_name')}; "
                f"candidate={progress_gate.get('best_candidate')}; control={progress_gate.get('best_control')}; "
                f"blockers={progress_gate.get('blockers')}"
            ),
            [readiness_inputs["experiment_registry"], str(paths.state / "scientific_progress_gate.json")],
        ),
        pass_check(
            "reference_reproduction_gate_pass",
            reproduction_gate.get("status") == "pass",
            (
                f"status={reproduction_gate.get('status')}; decision={reproduction_gate.get('decision')}; "
                f"best_reproduction={reproduction_gate.get('best_reproduction')}; blockers={reproduction_gate.get('blockers')}"
            ),
            [readiness_inputs["experiment_registry"], str(paths.state / "reference_reproduction_gate.json")],
        ),
        pass_check(
            "experiment_runtime_integrity_pass",
            runtime_integrity.get("status", "pass") != "blocked" and runtime_integrity.get("current") is True,
            f"status={runtime_integrity.get('status', 'pass')}; current={runtime_integrity.get('current')}; generated_at={runtime_integrity.get('generated_at', '')}; issues={runtime_integrity.get('issues', [])}",
            [str(paths.state / "experiment_runtime_integrity.json"), str(paths.state / "experiment_run_watchdog.json"), str(paths.state / "experiment_run_manifest.json")],
        ),
        pass_check(
            "experiment_iteration_trajectory_complete",
            iteration_audit.get("status") == "pass",
            f"status={iteration_audit.get('status', 'not-run')}; blockers={iteration_audit.get('blockers', [])}; warnings={iteration_audit.get('warnings', [])}",
            [str(paths.state / "experiment_iteration_audit.json")],
        ),
        pass_check(
            "claim_verdicts_available",
            any(row.get("claim_verdict") for row in audit_ready),
            f"audit_ready_runs={len(audit_ready)}",
            [readiness_inputs["experiment_registry"]],
        ),
        pass_check(
            "bad_cases_available",
            any(row.get("bad_case_path") or row.get("bad_case_slices") for row in audit_ready),
            f"audit_ready_runs={len(audit_ready)}",
            [readiness_inputs["experiment_registry"]],
        ),
        pass_check(
            "counterexamples_available",
            any(row.get("counterexample_outcome") for row in audit_ready),
            f"audit_ready_runs={len(audit_ready)}",
            [readiness_inputs["experiment_registry"]],
        ),
        pass_check(
            "citations_available",
            citation_count > 0 and verified_citation_count > 0,
            f"citation_candidates={citation_count} verified={verified_citation_count}",
            [readiness_inputs["paper_orchestra_state"]],
        ),
        pass_check(
            "paper_figures_conference_quality",
            figure_quality.get("status") == "pass" and bool(figure_quality.get("figure_quality_ready")),
            f"figure_quality_status={figure_quality.get('status', 'not-run')} blocked_figures={figure_quality.get('blocked_count', '')}",
            [readiness_inputs["paper_figure_quality_audit"]],
        ),
        pass_check(
            "assurance_layer_pass",
            assurance.get("status") == "pass",
            f"assurance_status={assurance.get('status', '')}",
            [readiness_inputs["research_assurance_layer"]],
            severity="warn" if assurance.get("status") in {"warn", "pass"} else "block",
        ),
        pass_check(
            "evidence_manifest_pass",
            manifest.get("status") == "pass" or (manifest.get("status") == "warn" and not manifest.get("weak_or_unsupported_claims", [])),
            f"evidence_manifest_status={manifest.get('status', '')}" + ("; weak_or_unsupported_claims=0 — warn is non-claim infrastructure only" if manifest.get("status") == "warn" and not manifest.get("weak_or_unsupported_claims", []) else ""),
            [readiness_inputs["research_evidence_manifest"]],
            severity="warn" if manifest.get("status") in {"warn", "pass"} else "block",
        ),
        # Read capability audit directly (avoids ordering issue where the trajectory
        # system summary hasn't been updated yet when submission_readiness runs).
        pass_check(
            "trajectory_operational",
            trajectory_capability_status == "pass",
            f"trajectory_capability={trajectory_capability_status}",
            [str(cap_audit_path)],
            severity="warn",
        ),
        pass_check(
            "tex_rendered",
            tex_path.exists() or bool(state.get("render_ready")),
            f"tex_path={tex_path}",
            [str(tex_path), readiness_inputs["pipeline_state"]],
            severity="warn",
        ),
        pass_check(
            "pdf_compiled",
            pdf_path.exists() and bool(state.get("pdf_ready")),
            f"pdf_path={pdf_path} pdf_ready={state.get('pdf_ready')}",
            [str(pdf_path), readiness_inputs["pipeline_state"]],
            severity="warn",
        ),
    ]

    blocker_checks = [row for row in checks if row.get("severity") == "block"]
    warning_checks = [row for row in checks if row.get("severity") == "warn"]
    status = "submission_ready" if not blocker_checks and not warning_checks else "blocked" if blocker_checks else "needs_final_packaging"
    payload = {
        "project": args.project,
        "venue": venue,
        "updated_at": now_iso(),
        "status": status,
        "submission_ready": status == "submission_ready",
        "promotion_gate": "allow-template" if status in {"submission_ready", "needs_final_packaging"} else "hold-markdown-only",
        "checks": checks,
        "failed_checks": [row for row in checks if row.get("status") != "pass"],
        "blockers": [row.get("detail") for row in blocker_checks],
        "warnings": [row.get("detail") for row in warning_checks],
        "metrics": {
            "completed_experiments": len(completed),
            "audit_ready_experiments": len(audit_ready),
            "real_audit_ready_experiments": len(real_audit_ready),
            "scientific_progress_gate": progress_gate.get("status", ""),
            "reference_reproduction_gate": reproduction_gate.get("status", ""),
            "reference_reproduction_decision": reproduction_gate.get("decision", ""),
            "reference_reproduction_blockers": reproduction_gate.get("blockers", []),
            "scientific_progress_best_candidate": progress_gate.get("best_candidate", {}),
            "scientific_progress_best_control": progress_gate.get("best_control", {}),
            "scientific_progress_blockers": progress_gate.get("blockers", []),
            "paper_evidence_audit_json_status": paper_evidence_json.get("status", "missing") if isinstance(paper_evidence_json, dict) else "missing",
            "paper_self_review_status": paper_self_review.get("status"),
            "paper_self_review_ready": bool(paper_self_review.get("ready")),
            "paper_self_review_receipt": paper_self_review.get("path", ""),
            "paper_self_review_evidence_blocker_count": len(self_review_evidence_blockers),
            "paper_self_review_evidence_blockers": self_review_evidence_blockers,
            "paper_self_review_preview_only_ready": bool(paper_self_review.get("preview_only_ready")),
            "paper_self_review_submission_evidence_ready": bool(paper_self_review.get("submission_evidence_ready")),
            "llm_semantic_evidence_guard": llm_semantic_guard.get("status", ""),
            "llm_semantic_evidence_issues": llm_semantic_guard.get("issues", []),
            "real_llm_embedding_evidence": llm_semantic_guard.get("real_llm_embedding_evidence", []),
            "experiment_runtime_integrity": runtime_integrity.get("status", "pass") if isinstance(runtime_integrity, dict) else "unknown",
            "experiment_runtime_integrity_current": runtime_integrity.get("current") if isinstance(runtime_integrity, dict) else False,
            "experiment_runtime_integrity_generated_at": runtime_integrity.get("generated_at", "") if isinstance(runtime_integrity, dict) else "",
            "experiment_runtime_integrity_issues": runtime_integrity.get("issues", []) if isinstance(runtime_integrity, dict) else [],
            "experiment_iteration_audit": iteration_audit.get("status", "not-run") if isinstance(iteration_audit, dict) else "not-run",
            "experiment_iteration_blockers": iteration_audit.get("blockers", []) if isinstance(iteration_audit, dict) else [],
            "claim_count": len(claim_rows),
            "current_selected_repo_claim_count": len(current_claim_rows),
            "current_selected_repo_claim_issues": current_claim_issues,
            "current_selected_repo": current_context.get("repo_name") or current_context.get("repo_path") or "",
            "current_selected_repo_path": active_repo_path,
            "weak_or_unsupported_claims": len(weak_claims),
            "blocked_sections": len(blocked_sections),
            "revision_sections": len(revision_sections),
            "citation_candidates": citation_count,
            "verified_citation_candidates": verified_citation_count,
            "paper_figure_quality_status": figure_quality.get("status", "") if isinstance(figure_quality, dict) else "",
            "paper_figure_count": figure_quality.get("figure_count", "") if isinstance(figure_quality, dict) else "",
            "paper_figure_blocker_count": figure_quality.get("blocked_count", "") if isinstance(figure_quality, dict) else "",
            "literature_strong_recommendations": literature_actual,
            "literature_recommendation_target": literature_target,
            "literature_recommendation_shortfall": literature_shortfall,
            "venue_policy_status": venue_policy.get("status", "") if isinstance(venue_policy, dict) else "",
            "venue_policy_source": venue_policy.get("source_url", "") if isinstance(venue_policy, dict) else "",
            "venue_body_pages": body_pages,
            "venue_body_page_min": body_min,
            "venue_body_page_max": body_max,
            "venue_total_pages": total_pages,
            "venue_total_page_max": total_max,
            "venue_reference_pages": reference_pages,
            "venue_reference_page_max": reference_max,
            "venue_failed_hard_checks": len(venue_failed),
            "venue_reviewer_nomination_done": reviewer_nomination_done,
        },
        "paper_self_review_evidence_blockers": self_review_evidence_blockers,
        "paper_self_review_evidence_blocker_count": len(self_review_evidence_blockers),
        "paper_self_review_preview_only_ready": bool(paper_self_review.get("preview_only_ready")),
        "principle": "Submission readiness is a scientific/evidence/design gate. Do not claim ready until every claim, citation, artifact, venue, figure-quality, and reproducibility blocker clears.",
    }
    write_json(paths.state / "submission_readiness.json", payload)
    lines = ["# Submission Readiness Audit\n\n"]
    for key in ["updated_at", "status", "submission_ready", "promotion_gate"]:
        lines.append(f"- {key}: {payload.get(key)}\n")
    lines.append("\n## Metrics\n\n")
    for key, value in payload["metrics"].items():
        lines.append(f"- {key}: {value}\n")
    lines.append("\n## Checks\n\n")
    for row in checks:
        lines.append(f"- [{row.get('severity')}] {row.get('name')}: {row.get('detail')} | evidence={row.get('evidence')}\n")
    lines.append("\n## Blockers\n\n")
    if blocker_checks:
        for row in blocker_checks:
            lines.append(f"- {row.get('name')}: {row.get('detail')}\n")
    else:
        lines.append("- No blocker.\n")
    out_md = paths.reports / "submission_readiness.md"
    write_text(out_md, "".join(lines))
    update_pipeline_state(
        args.project,
        {
            "submission_readiness": status,
            "submission_ready": status == "submission_ready",
            "submission_readiness_report": str(out_md),
            "submission_readiness_json": str(paths.state / "submission_readiness.json"),
            "promotion_gate": payload["promotion_gate"],
            "venue_submission_policy": venue_policy,
            "venue_submission_policy_status": venue_policy.get("status", "") if isinstance(venue_policy, dict) else "",
            "venue_submission_hard_gate_status": "pass" if not venue_failed else "blocked",
            "venue_submission_body_pages": body_pages,
            "venue_submission_total_pages": total_pages,
            "venue_submission_reference_pages": reference_pages,
            "venue_reviewer_nomination_done": reviewer_nomination_done,
            "paper_self_review_evidence_blockers": self_review_evidence_blockers,
            "paper_self_review_evidence_blocker_count": len(self_review_evidence_blockers),
            "paper_self_review_preview_only_ready": bool(paper_self_review.get("preview_only_ready")),
            "paper_self_review_submission_evidence_ready": bool(paper_self_review.get("submission_evidence_ready")),
        },
        venue=venue,
        promote_to_top=True,
    )
    print(out_md)
    return 0 if status != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
