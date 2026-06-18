#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiment_contracts import align_reference_best_control, evidence_gate, experiment_rows, llm_semantic_promotion_guard, scientific_progress_gate, row_matches_method
from paper_common import get_active_paper_state, slugify
from paper_self_review import validate_paper_self_review_receipt
from audit_experiment_runtime_integrity import build_runtime_integrity_audit
from project_paths import build_paths, load_project_config
from audit_reference_reproduction import build_reference_reproduction_gate

PRUNE_RECOMMENDATIONS = {'compare_then_prune_or_pause', 'pause_or_prune'}


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


def _current_claim_guard_issues(claim_ledger: dict[str, Any], experiments: list[dict[str, Any]], current: dict[str, Any]) -> list[str]:
    repo_path = _norm_path(current.get('repo_path'))
    repo_name = str(current.get('repo_name') or '')
    title = str(current.get('title') or '')
    if not repo_path:
        return ['Current selected-base repo path is unavailable; claim ledger cannot be matched to the active route.']
    rows_by_id = _experiment_index(experiments)
    claims = claim_ledger.get('claims', []) if isinstance(claim_ledger, dict) and isinstance(claim_ledger.get('claims'), list) else []
    if not claims:
        return ['Claim ledger is empty for the current selected-base route.']
    stale_markers = []
    current_markers = [repo_name, title, repo_path.split('/')[-1]]
    current_markers = [m.lower() for m in current_markers if str(m or '').strip()]
    issues: list[str] = []
    current_supported = 0
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_type = str(claim.get('claim_type') or claim.get('id') or 'claim')
        status = str(claim.get('status') or '').lower()
        if status in {'weak', 'unsupported'}:
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
            current_supported += 1
            continue
        if run_ids:
            issues.append(f"Claim `{claim_type}` has no supporting run from the current selected repo; supporting_runs={run_ids[:6]}.")
        elif has_stale_marker or not has_current_marker:
            issues.append(f"Claim `{claim_type}` is not grounded in the current selected-base route ({repo_name or repo_path}).")
    if current_supported == 0:
        issues.append(f"Claim ledger has no supported claim backed by current selected repo experiments: {repo_name or repo_path}.")
    return issues


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper_state = get_active_paper_state(args.project, venue=args.venue)
    venue = args.venue or str(paper_state.get('venue') or cfg.get('target_venue') or cfg.get('venue') or '')
    venue_slug = slugify(venue) if venue else ''
    output_dir = paths.root / 'paper' / 'output' / venue_slug if venue_slug else paths.root / 'paper' / 'output'
    current_pdf = Path(str(paper_state.get('latest_preview_pdf') or paper_state.get('blocked_preview_pdf') or paper_state.get('pdf_path') or output_dir / 'paper.pdf'))
    current_tex = Path(str(paper_state.get('latest_preview_tex') or paper_state.get('blocked_preview_tex') or paper_state.get('rendered_tex') or output_dir / 'paper.tex'))
    current_refs = output_dir / 'refs.bib'
    self_review = validate_paper_self_review_receipt(
        paths.root,
        venue,
        current_pdf=current_pdf if current_pdf.exists() else None,
        current_tex=current_tex if current_tex.exists() else None,
        current_refs=current_refs if current_refs.exists() else None,
    ) if venue else {'evidence_blockers': [], 'evidence_blocker_count': 0, 'submission_evidence_ready': False}
    self_review_evidence_blockers = self_review.get('evidence_blockers', []) if isinstance(self_review.get('evidence_blockers', []), list) else []
    claim_ledger = load_json(paths.state / 'claim_ledger.json', {'claims': []})
    experiment_payload = load_json(paths.state / 'experiment_registry.json', [])
    experiments = experiment_rows(experiment_payload)
    dataset_registry = load_json(paths.state / 'dataset_registry.json', [])
    active_repo = load_json(paths.state / 'active_repo.json', {})
    repo_data_requirements = load_json(paths.state / 'repo_data_requirements.json', {})
    data_policy = load_json(paths.state / 'data_unavailability_policy.json', {})
    try:
        runtime_integrity = build_runtime_integrity_audit(args.project)
    except Exception as exc:
        runtime_integrity = load_json(paths.state / 'experiment_runtime_integrity.json', {})
        if not isinstance(runtime_integrity, dict):
            runtime_integrity = {}
        runtime_integrity = dict(runtime_integrity)
        runtime_issues = runtime_integrity.get('issues') if isinstance(runtime_integrity.get('issues'), list) else []
        runtime_issues.append({
            'severity': 'block',
            'issue': f'runtime integrity refresh failed: {exc}',
            'evidence': [str(paths.state / 'experiment_runtime_integrity.json')],
        })
        runtime_integrity.update({'status': 'blocked', 'current': False, 'issues': runtime_issues})
    next_actions = load_json(paths.state / 'next_actions.json', {'method_summaries': []})
    methods = next_actions.get('method_summaries', []) if isinstance(next_actions, dict) else []
    issues = []
    notes = []

    active_repo_path = str(active_repo.get('repo_path', '')) if isinstance(active_repo, dict) else ''
    current_context = _current_selection_context(paths, active_repo if isinstance(active_repo, dict) else {})
    if current_context.get('repo_path'):
        active_repo_path = current_context['repo_path']
    current_experiments = _rows_for_current_repo(experiments, active_repo_path)
    completed = sum(1 for row in current_experiments if str(row.get('status', '')).lower() in {'completed', 'success'})
    audit_ready = sum(1 for row in current_experiments if row.get('audit_ready'))
    bad_case_runs = sum(1 for row in current_experiments if row.get('bad_case_slices'))
    claim_runs = sum(1 for row in current_experiments if row.get('claim_verdict'))
    counterexample_runs = sum(1 for row in current_experiments if row.get('counterexample_outcome'))
    pruned_methods = [row.get('method', '') for row in methods if row.get('recommendation') in PRUNE_RECOMMENDATIONS]
    claim_ready_datasets = {
        row.get('name') for row in dataset_registry
        if row.get('claim_ready') and (row.get('loader_probe_success') or row.get('probe_success')) and not str(row.get('name', '')).startswith('synthetic_')
    }
    if isinstance(repo_data_requirements, dict) and repo_data_requirements.get('ready_datasets') and claim_ready_datasets:
        real_datasets = set(repo_data_requirements.get('ready_datasets', [])) & claim_ready_datasets
    elif isinstance(repo_data_requirements, dict) and repo_data_requirements.get('ready_datasets'):
        real_datasets = set(repo_data_requirements.get('ready_datasets', []))
    else:
        real_datasets = claim_ready_datasets
    reproduction_gate = build_reference_reproduction_gate(args.project)
    (paths.state / 'reference_reproduction_gate.json').write_text(json.dumps(reproduction_gate, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    real_completed = sum(1 for row in current_experiments if str(row.get('status', '')).lower() in {'completed', 'success'} and row.get('dataset') in real_datasets)
    margin = float(cfg.get('decision_policy', {}).get('minimum_meaningful_gain', 0.005) or 0.005) if isinstance(cfg, dict) else 0.005
    current_reference_dataset = str((reproduction_gate.get('best_reproduction', {}) if isinstance(reproduction_gate.get('best_reproduction'), dict) else {}).get('dataset') or '').strip()
    progress_gate = scientific_progress_gate(
        experiments,
        ready_real_datasets=real_datasets,
        active_repo_path=active_repo_path,
        active_dataset=current_reference_dataset,
        margin=margin,
        method_role_policy=cfg.get('experiment', {}).get('method_role_policy', {}) if isinstance(cfg, dict) else {},
    )
    llm_semantic_guard = llm_semantic_promotion_guard(paths, experiments, active_repo_path, claim_ledger)
    if llm_semantic_guard.get('status') != 'pass' and llm_semantic_guard.get('project_requires_llm_semantics'):
        blockers = list(progress_gate.get('blockers', [])) if isinstance(progress_gate.get('blockers', []), list) else []
        blockers.append(
            'LLM semantic evidence guard is blocked; no candidate can count as promotable scientific progress until real item-text/API embedding evidence passes. '
            + '; '.join(str(item) for item in llm_semantic_guard.get('issues', [])[:3])
        )
        progress_gate['status'] = 'blocked'
        progress_gate['comparison_pass'] = False
        progress_gate['best_candidate'] = {}
        progress_gate['llm_semantic_evidence_guard'] = 'blocked'
        progress_gate['blockers'] = blockers
    progress_gate = align_reference_best_control(progress_gate, reproduction_gate)
    (paths.state / 'scientific_progress_gate.json').write_text(json.dumps(progress_gate, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    if completed == 0:
        issues.append('No completed experiment exists, so no paper claim can be trusted.')
    if audit_ready == 0:
        issues.append('No experiment has a valid audit-ready artifact bundle.')
    if bad_case_runs == 0:
        issues.append('No experiment exported useful bad-case slice evidence.')
    if claim_runs == 0:
        issues.append('No experiment carries a claim verdict.')
    if counterexample_runs == 0:
        issues.append('No experiment reports a counterexample outcome.')
    blocked_datasets = repo_data_requirements.get('blocked_datasets', []) if isinstance(repo_data_requirements, dict) else []
    policy_decision = data_policy.get('decision', '') if isinstance(data_policy, dict) else ''
    if blocked_datasets and policy_decision != 'continue_normal_loop_with_claim_ready_data':
        suffix = f" Data policy decision: {policy_decision}." if policy_decision else ''
        issues.append(f"Active repo real datasets are missing locally ({', '.join(blocked_datasets[:6])}); real experiments and paper claims remain blocked.{suffix}")
    elif blocked_datasets and policy_decision == 'continue_normal_loop_with_claim_ready_data':
        claim_ready = data_policy.get('claim_ready_datasets', []) if isinstance(data_policy, dict) and isinstance(data_policy.get('claim_ready_datasets'), list) else []
        scoped = ', '.join(str(item) for item in claim_ready[:6]) or 'claim-ready datasets'
        notes.append(f"Data policy ({policy_decision}): blocked datasets remain unavailable; claims are scoped to {scoped} only.")
    if real_completed == 0:
        issues.append('No completed experiment on an audited real dataset for the active repo exists; synthetic smoke runs cannot support final paper claims.')
    runtime_status = str(runtime_integrity.get('status') or 'missing') if isinstance(runtime_integrity, dict) else 'missing'
    runtime_current = runtime_integrity.get('current') if isinstance(runtime_integrity, dict) else False
    if runtime_status == 'blocked' or runtime_current is not True:
        runtime_issues = runtime_integrity.get('issues', []) if isinstance(runtime_integrity, dict) and isinstance(runtime_integrity.get('issues'), list) else []
        if runtime_issues:
            for blocker in runtime_issues:
                detail = blocker.get('issue') if isinstance(blocker, dict) else str(blocker)
                issues.append(f"Experiment runtime integrity: {detail}")
        else:
            issues.append(
                "Experiment runtime integrity is not current/passable: "
                f"status={runtime_status}; current={runtime_current}; generated_at={runtime_integrity.get('generated_at', '') if isinstance(runtime_integrity, dict) else ''}"
            )
    if progress_gate.get('status') != 'pass':
        for blocker in progress_gate.get('blockers', []):
            issues.append(f"Scientific progress gate: {blocker}")
    if llm_semantic_guard.get('status') != 'pass':
        for blocker in llm_semantic_guard.get('issues', []):
            issues.append(f"LLM semantic evidence guard: {blocker}")
    if reproduction_gate.get('status') != 'pass':
        for blocker in reproduction_gate.get('blockers', []):
            issues.append(f"Reference reproduction gate: {blocker}")
    if len(methods) >= 2 and not pruned_methods:
        issues.append('No active method has been pruned or paused yet, so the loop still lacks visible resource discipline.')

    promotable_claim_statuses = {'supported', 'partially_supported', 'partial', 'promising'}
    for claim in claim_ledger.get('claims', []):
        claim_status = str(claim.get('status') or '').lower()
        if claim_status not in promotable_claim_statuses:
            issues.append(f"Claim `{claim.get('claim_type')}` status={claim_status or 'missing'} is not promotable.")
        elif claim_status == 'mixed':
            notes.append(f"Claim `{claim.get('claim_type')}` is contested by weakening runs.")
    for issue in _current_claim_guard_issues(claim_ledger, experiments, current_context):
        issues.append('Current selected-base claim guard: ' + issue)

    for row in methods:
        if row.get('recommendation') in PRUNE_RECOMMENDATIONS:
            continue
        # Skip methods from repos other than the active repo — they are not
        # the primary paper claim and should not block template promotion.
        row_repo = row.get('repo_path') or ''
        if active_repo_path and row_repo and row_repo != active_repo_path:
            continue
        method_rows = [exp for exp in experiments if row_matches_method(exp, row)]
        gate = evidence_gate(method_rows, row.get('recommendation', ''))
        if gate.get('deepen_ready') and gate.get('counterexample_score', 0) <= 0:
            issues.append(f"Method `{row.get('method')}` looks deepen-ready but still lacks a strong positive counterexample result.")
        if row.get('best_metric') is not None and row.get('bad_case_slice_count', 0) == 0:
            issues.append(f"Method `{row.get('method')}` has global metric evidence but no bad-case slice evidence.")
        if row.get('best_metric') is not None and row.get('novelty_score', 0) <= 0:
            issues.append(f"Method `{row.get('method')}` improved metrics without convincing novelty signal.")
        if row.get('claim_strength_score', 0) < 0:
            issues.append(f"Method `{row.get('method')}` currently weakens its own claim story.")

    if paper_state.get('paper_review_verdict') == 'blocked':
        notes.append('Internal review is still blocking template promotion.')

    for blocker in self_review_evidence_blockers:
        if isinstance(blocker, dict):
            issues.append(f"Paper self-review evidence blocker: {blocker.get('category', blocker.get('id', 'self_review_evidence'))}: {blocker.get('detail') or blocker.get('issue', '')}")
        else:
            issues.append(f"Paper self-review evidence blocker: {blocker}")
    if self_review.get('preview_only_ready'):
        notes.append('Paper self-review preview checks passed, but unresolved scientific-evidence blockers keep the manuscript preview-only.')

    promotion_gate = 'hold-markdown-only' if issues else 'allow-template'
    out_md = paths.reports / 'paper_evidence_audit.md'
    lines = [
        '# Paper Evidence Audit\n\n',
        f'- completed_experiments: {completed}\n',
        f'- audit_ready_experiments: {audit_ready}\n',
        f'- bad_case_runs: {bad_case_runs}\n',
        f'- claim_verdict_runs: {claim_runs}\n',
        f'- counterexample_runs: {counterexample_runs}\n',
        f'- completed_real_dataset_experiments: {real_completed}\n',
        f"- current_selected_repo: {current_context.get('repo_name') or current_context.get('repo_path') or 'unknown'}\n",
        f"- current_selected_repo_path: {active_repo_path or 'unknown'}\n",
        f"- reference_reproduction_gate: {reproduction_gate.get('status', '')}\n",
        f"- reference_reproduction_decision: {reproduction_gate.get('decision', '')}\n",
        f"- scientific_progress_gate: {progress_gate.get('status', '')}\n",
        f"- best_candidate: {progress_gate.get('best_candidate', {})}\n",
        f"- best_control: {progress_gate.get('best_control', {})}\n",
        f"- pruned_methods: {', '.join(pruned_methods) if pruned_methods else 'none'}\n",
        f"- llm_semantic_evidence_guard: {llm_semantic_guard.get('status', '')}\n",
        f"- real_llm_embedding_evidence: {llm_semantic_guard.get('has_real_llm_embedding_evidence', False)}\n",
        f"- experiment_runtime_integrity: {runtime_status}\n",
        f"- experiment_runtime_integrity_current: {runtime_current}\n",
        f"- experiment_runtime_integrity_generated_at: {runtime_integrity.get('generated_at', '') if isinstance(runtime_integrity, dict) else ''}\n",
        f"- paper_self_review_evidence_blockers: {len(self_review_evidence_blockers)}\n",
        f"- paper_self_review_preview_only_ready: {bool(self_review.get('preview_only_ready'))}\n",
        f'- promotion_gate_recommendation: {promotion_gate}\n\n',
    ]
    if issues:
        lines.append('## Issues\n')
        for issue in issues:
            lines.append(f'- {issue}\n')
    else:
        lines.append('No hard evidence issue detected.\n')
    lines.append('\n## Notes\n')
    if notes:
        for note in notes:
            lines.append(f'- {note}\n')
    else:
        lines.append('- No additional notes.\n')
    out_md.write_text(''.join(lines), encoding='utf-8')
    out_json = paths.state / 'paper_evidence_audit.json'
    out_json.write_text(json.dumps({
        'project': args.project,
        'status': 'pass' if not issues else 'blocked',
        'promotion_gate_recommendation': promotion_gate,
        'issues': issues,
        'notes': notes,
        'completed_experiments': completed,
        'audit_ready_experiments': audit_ready,
        'completed_real_dataset_experiments': real_completed,
        'current_selected_repo': current_context.get('repo_name') or current_context.get('repo_path') or 'unknown',
        'current_selected_repo_path': active_repo_path or 'unknown',
        'reference_reproduction_gate': reproduction_gate.get('status', ''),
        'reference_reproduction_decision': reproduction_gate.get('decision', ''),
        'scientific_progress_gate': progress_gate.get('status', ''),
        'llm_semantic_evidence_guard': llm_semantic_guard,
        'experiment_runtime_integrity': runtime_status,
        'experiment_runtime_integrity_current': runtime_current,
        'experiment_runtime_integrity_generated_at': runtime_integrity.get('generated_at', '') if isinstance(runtime_integrity, dict) else '',
        'experiment_runtime_integrity_issues': runtime_integrity.get('issues', []) if isinstance(runtime_integrity, dict) else [],
        'paper_self_review_status': self_review.get('status'),
        'paper_self_review_ready': bool(self_review.get('ready')),
        'paper_self_review_receipt': self_review.get('path', ''),
        'paper_self_review_evidence_blockers': self_review_evidence_blockers,
        'paper_self_review_evidence_blocker_count': len(self_review_evidence_blockers),
        'paper_self_review_preview_only_ready': bool(self_review.get('preview_only_ready')),
        'paper_self_review_submission_evidence_ready': bool(self_review.get('submission_evidence_ready')),
    }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(out_md)
    return 0 if not issues else 2


if __name__ == '__main__':
    raise SystemExit(main())
