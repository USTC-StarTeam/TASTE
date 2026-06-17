#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
from collections import defaultdict
from pathlib import Path

from experiment_contracts import PRUNE_RECOMMENDATIONS, row_promotion_blockers
from project_paths import build_paths, load_project_config, management_python


# ---- review board tool ----
def _aris_load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default

def _aris_numeric(value):
    try:
        return float(value)
    except Exception:
        return None

def _aris_method_rows(experiments: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in experiments:
        method = str(row.get('method') or row.get('method_slug') or row.get('experiment_id') or 'unknown')
        grouped[method].append(row)
    return grouped

def _aris_verdict_for(rows: list[dict]) -> dict:
    completed = [r for r in rows if str(r.get('status', '')).lower() in {'completed', 'success'}]
    blocker_map = {id(r): row_promotion_blockers(r) for r in completed}
    excluded = [r for r in completed if blocker_map.get(id(r))]
    audit_ready = [r for r in completed if r.get('audit_ready') and r not in excluded]
    metrics = [_aris_numeric(r.get('metric_value')) for r in audit_ready]
    metrics = [m for m in metrics if m is not None]
    tail_values = []
    for row in audit_ready:
        payload = row.get('metrics') if isinstance(row.get('metrics'), dict) else {}
        val = _aris_numeric(payload.get('tail_ndcg_at_10') or payload.get('tail_metric') or payload.get('worst_slice_metric'))
        if val is not None:
            tail_values.append(val)
    claim_support = sum((1 for r in audit_ready if str(r.get('claim_verdict', '')).lower() in {'support', 'supported', 'pass', 'partially_supported'}))
    counterexamples = sum((1 for r in audit_ready if 'missing_counterexample_outcome' not in blocker_map.get(id(r), [])))
    bad_cases = sum((1 for r in audit_ready if 'missing_bad_case_slices' not in blocker_map.get(id(r), [])))
    synthetic_only = all((str(r.get('dataset', '')).startswith('synthetic') for r in audit_ready)) if audit_ready else True
    best = max(metrics) if metrics else None
    weakest_tail = min(tail_values) if tail_values else None
    issues = []
    if not completed:
        issues.append('no completed run')
    if not audit_ready:
        issues.append('no audit-ready run')
    for row in excluded[:3]:
        issues.append(f"non-promotable evidence status: {row.get('experiment_id') or row.get('name')} ({', '.join(blocker_map.get(id(row), [])[:3])})")
    if bad_cases == 0:
        issues.append('no bad-case evidence')
    if counterexamples == 0:
        issues.append('no counterexample pressure')
    if synthetic_only:
        issues.append('synthetic-only evidence')
    if claim_support == 0:
        issues.append('no supporting claim verdict')
    if not issues and len(audit_ready) >= 2:
        recommendation = 'deepen'
    elif audit_ready and bad_cases and claim_support:
        recommendation = 'compare_or_repair'
    elif completed:
        recommendation = 'repair_or_prune'
    else:
        recommendation = 'block'
    return {'completed': len(completed), 'audit_ready': len(audit_ready), 'best_metric': best, 'weakest_tail_metric': weakest_tail, 'claim_support_count': claim_support, 'bad_case_count': bad_cases, 'counterexample_count': counterexamples, 'synthetic_only': synthetic_only, 'issues': issues, 'recommendation': recommendation}

def run_review_board(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    experiments = _aris_load_json(paths.state / 'experiment_registry.json', [])
    if not isinstance(experiments, list):
        experiments = []
    pruned = _aris_load_json(paths.state / 'pruned_methods.json', {})
    if not isinstance(pruned, dict):
        pruned = {}
    pruned_set = set(pruned.get('methods', []))
    board = []
    for method, rows in sorted(_aris_method_rows(experiments).items()):
        entry = {'method': method, **_aris_verdict_for(rows)}
        if method in pruned_set:
            pruned_info = pruned.get('reasons', {}).get(method, 'manual prune decision')
            entry['recommendation'] = 'pruned'
            if 'issues' in entry:
                entry['issues'] = [f'pruned: {pruned_info}'] + [i for i in entry['issues'] if i != 'no supporting claim verdict']
        board.append(entry)
    blockers = []
    if not board:
        blockers.append('No experiments exist.')
    if not any((row['recommendation'] in {'deepen', 'compare_or_repair'} for row in board)):
        blockers.append('No method is ready for confident deepening.')
    if not any((not row['synthetic_only'] for row in board)):
        blockers.append('No method has real-dataset evidence; paper promotion must remain blocked.')
    payload = {'project': args.project, 'reviewers': ['executor', 'bad_case_critic', 'claim_skeptic', 'prune_chair'], 'methods': board, 'blockers': blockers}
    (paths.state / 'aris_review_board.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# ARIS-Style Review Board\n\n']
    lines.append('Reviewers: executor, bad_case_critic, claim_skeptic, prune_chair.\n\n')
    for row in board:
        lines.append(f"## {row['method']}\n\n")
        for key in ['recommendation', 'completed', 'audit_ready', 'best_metric', 'weakest_tail_metric', 'claim_support_count', 'bad_case_count', 'counterexample_count', 'synthetic_only']:
            lines.append(f'- {key}: {row.get(key)}\n')
        lines.append(f"- issues: {(', '.join(row['issues']) if row['issues'] else 'none')}\n\n")
    lines.append('## Global Blockers\n\n')
    if blockers:
        for item in blockers:
            lines.append(f'- {item}\n')
    else:
        lines.append('- No global blocker detected.\n')
    out = paths.reports / 'aris_review_board.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


# ---- blocker resolution packet tool ----
def _blocker_resolution_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def _blocker_resolution_save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def run_blocker_resolution(argv=None):
    parser = argparse.ArgumentParser(description='Build an explicit evidence-safe resolution packet for the current blocker.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    active = _blocker_resolution_load_json(paths.state / 'active_repo.json', {})
    req = _blocker_resolution_load_json(paths.state / 'repo_data_requirements.json', {})
    policy = _blocker_resolution_load_json(paths.state / 'data_unavailability_policy.json', {})
    pool = _blocker_resolution_load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    acquisition = _blocker_resolution_load_json(paths.state / 'data_acquisition_history.json', {})
    audit = (paths.reports / 'paper_evidence_audit.md').read_text(encoding='utf-8') if (paths.reports / 'paper_evidence_audit.md').exists() else ''
    placement = policy.get('exact_user_data_placement_requests', []) if isinstance(policy, dict) else []
    evidence_ready = pool.get('evidence_ready_candidates', []) if isinstance(pool, dict) else []
    audited = pool.get('audited_candidates', []) if isinstance(pool, dict) else []
    attempts = acquisition.get('attempts', []) if isinstance(acquisition, dict) else []
    ready_datasets = req.get('ready_datasets', []) if isinstance(req.get('ready_datasets', []), list) else []
    missing_datasets = req.get('blocked_datasets', []) if isinstance(req.get('blocked_datasets', []), list) else []
    hard_real_data_blocker = not bool(ready_datasets)
    allowed_paths = ['place_active_repo_data_then_verify_loader', 'continue_pool_audit_until_evidence_ready_candidate_exists', 'expand_discovery_with_data_ready_repo_queries']
    blocked_actions = ['do_not_write_or_strengthen_final_claims', 'do_not_run_synthetic_results_as_paper_evidence', 'do_not_switch_to_needs_audit_repo', 'do_not_mark_download_attempt_as_data_ready']
    repo_path = active.get('repo_path', '<active_repo>')
    venue_suffix = f' --venue \"{args.venue}\"' if args.venue else ''
    verification_commands = [
        f"{management_python()} framework/scripts/run_module.py environment --action data_requirements --project {args.project} --repo-path {repo_path}",
        f"{management_python()} framework/scripts/run_module.py environment --action probe_repo --project {args.project} --repo-path {repo_path} --env-name <active_env>",
        f"{management_python()} framework/scripts/run_module.py writing --action audit_evidence --project {args.project}{venue_suffix}",
        f"{management_python()} framework/scripts/report_status.py --project {args.project}{venue_suffix}",
    ]
    payload = {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'project': args.project, 'venue_snapshot': args.venue, 'blocker_type': 'active_repo_real_data_missing' if hard_real_data_blocker else 'none', 'active_repo': active, 'ready_datasets': ready_datasets, 'blocked_datasets': missing_datasets if hard_real_data_blocker else [], 'non_ready_optional_datasets': missing_datasets if not hard_real_data_blocker else [], 'required_files_per_dataset': req.get('contract', {}).get('required_files_per_dataset', []), 'data_policy_decision': policy.get('decision', ''), 'data_acquisition_attempts': len([row for row in attempts if not row.get('dry_run')]), 'recorded_attempts_including_dry_run': len(attempts), 'evidence_ready_candidate_count': len(evidence_ready), 'audited_candidate_count': len(audited), 'allowed_resolution_paths': allowed_paths, 'blocked_actions': blocked_actions, 'exact_user_data_placement_requests': placement, 'paper_gate_summary': 'hold-markdown-only' if 'promotion_gate_recommendation: hold-markdown-only' in audit else 'unknown_or_missing', 'verification_commands': verification_commands, 'completion_condition': 'At least one real dataset must be present, loader-probed, and used in an audit-ready active-repo experiment before paper claims can advance.' if hard_real_data_blocker else 'Active repo has ready real dataset evidence; do not treat other missing optional datasets as the active blocker. Paper promotion remains controlled by the evidence audit gate.'}
    _blocker_resolution_save_json(paths.state / 'blocker_resolution_packet.json', payload)
    lines = ['# Blocker Resolution Packet\n\n']
    for key in ['generated_at', 'project', 'venue_snapshot', 'blocker_type', 'ready_datasets', 'data_policy_decision', 'data_acquisition_attempts', 'recorded_attempts_including_dry_run', 'evidence_ready_candidate_count', 'audited_candidate_count', 'paper_gate_summary', 'completion_condition']:
        lines.append(f'- {key}: {payload.get(key)}\n')
    lines.append('\n## Non-Ready Optional Datasets\n')
    for item in payload.get('non_ready_optional_datasets', []):
        lines.append(f'- {item}\n')
    if not payload.get('non_ready_optional_datasets'):
        lines.append('- none\n')
    lines.append('\n## Exact User Data Placement Requests\n')
    if placement:
        for item in placement:
            lines.append(f"- {item.get('dataset')}: place {', '.join(item.get('required_files', []))} under `{item.get('place_required_files_under')}`\n")
    else:
        lines.append('- none\n')
    lines.append('\n## Allowed Resolution Paths\n')
    for item in allowed_paths:
        lines.append(f'- {item}\n')
    lines.append('\n## Blocked Actions\n')
    for item in blocked_actions:
        lines.append(f'- {item}\n')
    lines.append('\n## Verification Commands\n')
    for cmd in payload['verification_commands']:
        lines.append(f'- `{cmd}`\n')
    out = paths.reports / 'blocker_resolution_packet.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


# ---- method frontier tool ----
def _method_frontier_load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def run_method_frontier(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    next_actions = _method_frontier_load_json(paths.state / 'next_actions.json', {'method_summaries': []})
    methods = next_actions.get('method_summaries', []) if isinstance(next_actions, dict) else []
    ranked = sorted(methods, key=lambda row: (-row.get('decision_score', 0.0), row.get('priority', 0)))
    pruned_recommendations = {'compare_then_prune_or_pause', 'pause_or_prune'}
    active_ranked = [row for row in ranked if row.get('recommendation') not in pruned_recommendations]
    elite = active_ranked[:2]
    frontier = {'elite_methods': elite, 'repair_queue': [row for row in ranked if row.get('audit_incomplete_runs', 0) > 0], 'slice_focus_queue': [row for row in ranked if row.get('bad_case_slice_count', 0) > 0 and (not row.get('deepen_ready'))], 'prune_queue': [row for row in ranked if row.get('recommendation') in {'compare_then_prune_or_pause', 'pause_or_prune'}], 'explore_queue': [row for row in active_ranked if row.get('completed', 0) + row.get('failed', 0) == 0]}
    (paths.state / 'method_frontier.json').write_text(json.dumps(frontier, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Method Frontier\n\n']
    lines.append('## Elite Pool\n')
    if elite:
        for row in elite:
            lines.append(f"- {row.get('method')}: decision_score={row.get('decision_score')} deepen_ready={row.get('deepen_ready')}\n")
    else:
        lines.append('- none yet\n')
    lines.append('\n## Repair Queue\n')
    for row in frontier['repair_queue']:
        lines.append(f"- {row.get('method')}: audit_incomplete_runs={row.get('audit_incomplete_runs', 0)}\n")
    lines.append('\n## Slice Focus Queue\n')
    for row in frontier['slice_focus_queue']:
        lines.append(f"- {row.get('method')}: bad_case_slice_count={row.get('bad_case_slice_count', 0)}\n")
    lines.append('\n## Prune Queue\n')
    for row in frontier['prune_queue']:
        lines.append(f"- {row.get('method')}: recommendation={row.get('recommendation')}\n")
    lines.append('\n## Explore Queue\n')
    for row in frontier['explore_queue']:
        lines.append(f"- {row.get('method')}\n")
    out = paths.planning / 'method_frontier.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


# ---- workflow blueprint tool ----
def _workflow_load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def run_workflow(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    paper_state = _workflow_load_json(paths.root / 'paper' / 'metadata' / 'paper_pipeline.json', {})
    active_venues = list(paper_state.get('venues', {}).keys()) if isinstance(paper_state, dict) else []
    blueprint = {'project': args.project, 'topic': cfg.get('topic', ''), 'modules': [{'name': 'request_intake', 'purpose': 'Turn user natural-language goals into logged project requests and loop kickoff state.', 'inputs': ['user prompt', 'project config'], 'outputs': ['state/natural_language_requests.json', 'state/loop_history.json'], 'feeds_into': ['literature_discovery', 'initialization_and_arena']}, {'name': 'literature_discovery', 'purpose': 'Create an adaptive recent-literature plan, collect papers, compile wiki, and surface novelty deltas, contradictions, and gap signals; if no qualified papers pass the gate, trigger repo-first backtracking rather than importing weak work.', 'inputs': ['queries', 'raw/papers', 'manual imports'], 'outputs': ['planning/literature_review_plan.md', 'wiki/', 'reports/shared_research.md', 'gaps/research_gaps.md', 'state/ingest_ranking.json'], 'feeds_into': ['initialization_and_arena', 'paper_drafting']}, {'name': 'initialization_and_arena', 'purpose': 'Rank repos and datasets, inspect machine/runtime, and build the hypothesis arena.', 'inputs': ['repo candidates', 'dataset registry', 'machine profile', 'paper quality'], 'outputs': ['planning/init_brief.md', 'planning/hypothesis_arena.md', 'reports/repo_candidates.md', 'reports/dataset_registry.md'], 'feeds_into': ['parallel_experiment_planning']}, {'name': 'parallel_experiment_planning', 'purpose': 'Generate parallel methods with explicit novelty, claim, counterexample, and prune contracts.', 'inputs': ['hypothesis arena', 'repo choice', 'dataset choice', 'benchmark', 'metric'], 'outputs': ['state/parallel_plan.json', 'planning/parallel_experiment_plan.md'], 'feeds_into': ['execution_and_audit']}, {'name': 'execution_and_audit', 'purpose': 'Run methods, require auditable outputs, and record machine-readable evidence.', 'inputs': ['parallel plan', 'repo env', 'artifact contract'], 'outputs': ['state/experiment_registry.json', 'experiments/experiment_log.md', 'artifacts/*/audit.json'], 'feeds_into': ['failure_reflection_and_pruning', 'paper_evidence_gate']}, {'name': 'failure_reflection_and_pruning', 'purpose': 'Diagnose failures, build method frontier, rank next actions, and decide continue/repair/prune.', 'inputs': ['experiment registry', 'failure analyses', 'claim ledger', 'paper evidence audit'], 'outputs': ['planning/next_actions.md', 'planning/method_frontier.md', 'reports/iteration_reflection.md'], 'feeds_into': ['parallel_experiment_planning', 'paper_drafting']}, {'name': 'paper_evidence_gate', 'purpose': 'Prevent paper promotion unless claims, bad cases, and counterexamples are sufficiently supported.', 'inputs': ['experiment registry', 'claim ledger', 'method frontier', 'paper review state'], 'outputs': ['reports/paper_evidence_audit.md', 'reports/research_manifest.md'], 'feeds_into': ['paper_drafting', 'venue_latex_stage']}, {'name': 'paper_drafting', 'purpose': 'Draft, review, revise, and re-review the paper in Markdown before any template promotion.', 'inputs': ['shared research', 'claim ledger', 'paper evidence audit', 'research manifest'], 'outputs': ['paper/drafts/*.md', 'paper/reviews/*'], 'feeds_into': ['venue_latex_stage']}, {'name': 'venue_latex_stage', 'purpose': 'Fetch venue template, render LaTeX, and compile PDF only after evidence and review gates clear.', 'inputs': ['paper revision', 'target venue', 'template/runtime availability'], 'outputs': ['paper/rendered/*', 'paper/compiled/*'], 'feeds_into': ['request_intake']}], 'active_venues': active_venues}
    (paths.state / 'workflow_blueprint.json').write_text(json.dumps(blueprint, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Workflow Blueprint\n\n']
    lines.append(f'- project: {args.project}\n')
    lines.append(f"- topic: {cfg.get('topic', '')}\n")
    lines.append(f"- active_venues: {(', '.join(active_venues) if active_venues else 'none')}\n\n")
    for module in blueprint['modules']:
        lines.append(f"## {module['name']}\n\n")
        lines.append(f"- purpose: {module['purpose']}\n")
        lines.append(f"- inputs: {', '.join(module['inputs'])}\n")
        lines.append(f"- outputs: {', '.join(module['outputs'])}\n")
        lines.append(f"- feeds_into: {', '.join(module['feeds_into'])}\n\n")
    out = paths.planning / 'workflow_blueprint.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


# ---- experiment planning tool ----
TRIAL_FOCUS_LIBRARY = ['baseline reproduction on the selected repo and dataset', 'targeted hyperparameter sweep aimed at the current weakest slice', 'module-coordination or implementation sanity check with the same benchmark', 'focused repair run after inspecting bad cases']

SHELL_META_TOKENS = ('|', '&', ';', '<', '>', '(', ')', '$', '`', '\n')

def _experiments_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

def _experiments_load_mapping(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def _experiments_slugify(value: str) -> str:
    slug = re.sub('[^a-zA-Z0-9]+', '_', value.strip().lower()).strip('_')
    return slug or 'item'

def _experiments_discover_repo(repo_rows: list[dict], repo_name: str | None, repo_path: str | None) -> dict:
    if repo_path:
        target = Path(repo_path)
        match = next((row for row in repo_rows if row.get('local_path') == str(target)), None)
        return match or {'name': target.name, 'url': 'local', 'local_path': str(target), 'summary': 'manually provided repo path', 'score': 0, 'notes': 'selected from --repo-path'}
    if repo_name:
        for row in repo_rows:
            if row.get('name') == repo_name:
                return row
        raise SystemExit(f'No repo candidate named {repo_name}')
    for row in repo_rows:
        if row.get('local_path'):
            return row
    return repo_rows[0] if repo_rows else {}

def _experiments_discover_dataset(dataset_rows: list[dict], dataset_name: str) -> dict:
    for row in dataset_rows:
        if row.get('name') == dataset_name:
            return row
    return {'name': dataset_name, 'available': False, 'notes': 'not registered yet'}

def _experiments_load_machine_summary(paths) -> tuple[dict, int]:
    machine_path = paths.reports / 'machine_profile.json'
    if not machine_path.exists():
        return ({}, 0)
    machine = json.loads(machine_path.read_text(encoding='utf-8'))
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    gpus = accelerator.get('gpus', []) if isinstance(accelerator, dict) else []
    return (machine, len(gpus))

def _experiments_command_needs_shell(command: str) -> bool:
    return any((token in (command or '') for token in SHELL_META_TOKENS))

def _experiments_infer_command_spec(repo_path: str | None, explicit_template: str | None, python_executable: str) -> tuple[str, list[str], str, str]:
    if explicit_template:
        normalized_template = explicit_template.strip()
        argv = []
        kind = 'shell'
        if not _experiments_command_needs_shell(normalized_template):
            try:
                argv = shlex.split(normalized_template)
                if argv and argv[0] == 'python':
                    argv[0] = python_executable
                    normalized_template = shlex.join(argv)
                kind = 'argv'
            except ValueError:
                argv = []
        return (normalized_template, argv, 'user-provided', kind)
    if not repo_path:
        return ('', [], 'missing-repo-path', 'shell')
    repo = Path(repo_path)
    for candidate in ('train.py', 'main.py', 'run.py'):
        if (repo / candidate).exists():
            return (f'{python_executable} {candidate}', [python_executable, candidate], f'auto-detected:{candidate}', 'argv')
    return ('', [], 'no-entrypoint-detected', 'shell')

def _experiments_infer_env_name(project: str, repo: dict, explicit_env: str | None) -> str:
    if explicit_env:
        return explicit_env
    repo_name = repo.get('name') or Path(str(repo.get('local_path', project))).name
    return f'{_experiments_slugify(project)}_{_experiments_slugify(str(repo_name))}'

def _experiments_build_trial_focuses(count: int) -> list[str]:
    if count <= len(TRIAL_FOCUS_LIBRARY):
        return TRIAL_FOCUS_LIBRARY[:count]
    focuses = TRIAL_FOCUS_LIBRARY[:]
    while len(focuses) < count:
        focuses.append(f'targeted follow-up attempt {len(focuses) + 1} after cross-method comparison')
    return focuses

def _experiments_render_template(template: str, context: dict[str, object]) -> str:
    if not template:
        return ''
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace('{' + key + '}', str(value))
    return rendered

def _experiments_load_quality_focus(paths) -> dict:
    paper_quality = _experiments_load_mapping(paths.state / 'paper_quality.json')
    papers = paper_quality.get('papers', []) if isinstance(paper_quality, dict) else []
    best = next((row for row in papers if row.get('top_tier_readiness') == 'promising'), papers[0] if papers else None)
    if not best:
        return {'novelty_target': 'Define a precise delta over a strong baseline before scaling experiments.', 'claim_target': 'State what benchmark movement would support the core claim.', 'counterexample_target': 'List at least one slice or stress test that could falsify the claim.'}
    return {'paper_id': best.get('paper_id', ''), 'novelty_target': f"Clarify how the method is meaningfully different from the nearest-neighbor work around {best.get('paper_id', '')}.", 'claim_target': 'Tie every method to a claim that can be supported or weakened by the planned benchmark metric.', 'counterexample_target': 'Identify the slices or failure settings most likely to break the central claim early.'}

def _experiments_infer_method_role(method: str, cfg: dict) -> str:
    policy = cfg.get('experiment', {}).get('method_role_policy', {}) if isinstance(cfg, dict) else {}
    roles = policy.get('method_roles', {}) if isinstance(policy, dict) else {}
    if isinstance(roles, dict) and method in roles:
        return str(roles[method])
    prompt = (cfg.get('topic', '') + ' ' + cfg.get('user_prompt', '')).lower() if isinstance(cfg, dict) else ''
    lowered = method.lower()
    generic_control = ('baseline', 'control', 'ablation', 'reference', 'reproduction')
    generic_candidate = ('proposed', 'candidate', 'variant', 'ours', 'intervention', 'treatment')
    if any((token in lowered for token in generic_control)):
        return 'control'
    if any((token in lowered for token in generic_candidate)):
        return 'candidate'
    return 'unknown'

def _experiments_build_method_contract(method: str, dataset_name: str, benchmark: str, metric: str, quality_focus: dict) -> dict:
    return {'novelty_hypothesis': f'{method} should create a non-trivial delta over the strongest baseline on {benchmark}, not just minor tuning noise.', 'claim_to_test': f'If {method} is valid, it should improve {metric} on {dataset_name} under the same benchmark protocol.', 'support_threshold': f'Need reproducible improvement on {metric} plus evidence on difficult slices, not only one global average.', 'counterexample_test': quality_focus.get('counterexample_target', 'Test the weakest slice first.'), 'bad_case_slices': ['worst-performing slice from the first executable baseline', 'out-of-distribution or long-tail slice if available', 'high-latency or high-context examples if relevant to the task'], 'continue_rule': 'Continue only if the method shows either a meaningful aggregate gain or a clearly better failure profile on hard slices.', 'prune_rule': 'Prune or pause after repeated weak results if the novelty story is weak and the error profile is not more repairable than stronger alternatives.'}

def run_experiments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--methods', nargs='+', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--benchmark', required=True)
    parser.add_argument('--metric', required=True)
    parser.add_argument('--repo-name')
    parser.add_argument('--repo-path')
    parser.add_argument('--env-name')
    parser.add_argument('--command-template')
    parser.add_argument('--metrics-path-template', default='{artifact_dir}/metrics.json')
    parser.add_argument('--bad-case-path-template', default='{artifact_dir}/bad_cases.json')
    parser.add_argument('--audit-path-template', default='{artifact_dir}/audit.json')
    parser.add_argument('--max-methods', type=int)
    parser.add_argument('--max-trials-per-method', type=int)
    parser.add_argument('--gpu-per-trial', type=int)
    parser.add_argument('--seed-start', type=int, default=1)
    args = parser.parse_args(argv)
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo_rows = _experiments_load_json(paths.state / 'repo_candidates.json')
    dataset_rows = _experiments_load_json(paths.state / 'dataset_registry.json')
    repo = _experiments_discover_repo(repo_rows, args.repo_name, args.repo_path)
    dataset = _experiments_discover_dataset(dataset_rows, args.dataset)
    machine, visible_gpus = _experiments_load_machine_summary(paths)
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    quality_focus = _experiments_load_quality_focus(paths)
    max_methods = args.max_methods or cfg.get('parallel_experiments', {}).get('max_concurrent_methods', 3)
    selected_methods = args.methods[:max_methods]
    planned_trials = args.max_trials_per_method or cfg.get('parallel_experiments', {}).get('max_concurrent_trials_per_method', 2)
    gpu_per_trial = args.gpu_per_trial if args.gpu_per_trial is not None else 1 if visible_gpus > 0 else 0
    resource_bound_parallel = max(1, visible_gpus // max(gpu_per_trial, 1)) if visible_gpus and gpu_per_trial else max(1, len(selected_methods))
    max_parallel_trials = min(max(1, len(selected_methods)), resource_bound_parallel, max_methods)
    python_executable = cfg.get('python_executable', 'python3')
    command_template, command_argv_template, command_source, command_kind = _experiments_infer_command_spec(repo.get('local_path'), args.command_template, python_executable)
    env_name = _experiments_infer_env_name(args.project, repo, args.env_name)
    methods = []
    for rank, method in enumerate(selected_methods, start=1):
        method_slug = _experiments_slugify(method)
        method_role = _experiments_infer_method_role(method, cfg)
        trial_focuses = _experiments_build_trial_focuses(planned_trials)
        contract = _experiments_build_method_contract(method, args.dataset, args.benchmark, args.metric, quality_focus)
        trials = []
        for index, focus in enumerate(trial_focuses, start=1):
            artifact_dir = paths.artifacts / method_slug / f'trial_{index:02d}'
            context = {'project': args.project, 'project_root': str(paths.root.resolve()), 'repo_path': repo.get('local_path', ''), 'repo_name': repo.get('name', ''), 'method': method, 'method_slug': method_slug, 'dataset': args.dataset, 'benchmark': args.benchmark, 'metric': args.metric, 'trial': index, 'seed': args.seed_start + index - 1, 'artifact_dir': str(artifact_dir.resolve())}
            command = _experiments_render_template(command_template, context)
            command_argv = [_experiments_render_template(item, context) for item in command_argv_template] if command_argv_template else []
            trials.append({'experiment_id': f'{method_slug}_trial_{index:02d}', 'trial_index': index, 'seed': context['seed'], 'focus': focus, 'artifact_dir': str(artifact_dir.resolve()), 'command': command, 'command_argv': command_argv, 'command_kind': command_kind, 'command_source': command_source, 'metrics_path': _experiments_render_template(args.metrics_path_template, context), 'bad_case_path': _experiments_render_template(args.bad_case_path_template, context), 'audit_path': _experiments_render_template(args.audit_path_template, context), 'status': 'planned', 'result_summary': 'pending', 'method_role': method_role, 'comparison_role': method_role})
        methods.append({'method': method, 'method_slug': method_slug, 'method_role': method_role, 'comparison_role': method_role, 'priority': rank, 'status': 'planned', 'decision': 'open', 'repo_name': repo.get('name', ''), 'repo_path': repo.get('local_path', ''), 'env_name': env_name, 'dataset': args.dataset, 'benchmark': args.benchmark, 'metric': args.metric, 'gpu_per_trial': gpu_per_trial, 'launch_ready': bool((command_template or command_argv_template) and repo.get('local_path')), 'command_template': command_template, 'command_argv_template': command_argv_template, 'command_kind': command_kind, 'command_source': command_source, 'planned_trials': planned_trials, 'claim_contract': contract, 'trials': trials, 'notes': ''})
    plan = {'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'project': args.project, 'dataset': args.dataset, 'benchmark': args.benchmark, 'metric': args.metric, 'selected_repo': {'name': repo.get('name', ''), 'url': repo.get('url', ''), 'local_path': repo.get('local_path', ''), 'score': repo.get('score', 0), 'notes': repo.get('notes', '')}, 'selected_dataset': dataset, 'environment': {'env_name': env_name, 'python_version': cfg.get('environment', {}).get('python_version', ''), 'gpu_backend': accelerator.get('backend', 'unknown') if isinstance(accelerator, dict) else 'unknown', 'cuda_version': accelerator.get('cuda_version', '') if isinstance(accelerator, dict) else ''}, 'resource_plan': {'visible_gpu_count': visible_gpus, 'gpu_per_trial': gpu_per_trial, 'max_parallel_trials': max_parallel_trials, 'max_methods_considered': max_methods, 'resource_policy': cfg.get('parallel_experiments', {}).get('resource_policy', 'fit-to-visible-gpus')}, 'decision_policy': {'min_followup_attempts': cfg.get('failure_analysis', {}).get('min_followup_attempts', 2), 'early_drop_patience': cfg.get('failure_analysis', {}).get('early_drop_patience', 2), 'max_total_attempts_per_method': cfg.get('failure_analysis', {}).get('max_total_attempts_per_method', 6), 'require_bad_case_evidence_before_scaling': True, 'require_claim_check_before_deepen': True}, 'quality_focus': quality_focus, 'methods': methods}
    out_json = paths.state / 'parallel_plan.json'
    out_json.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Parallel Experiment Plan\n\n', f"- created_at: {plan['created_at']}\n", f"- repo: {plan['selected_repo']['name']} ({plan['selected_repo']['local_path'] or 'no local path'})\n", f'- dataset: {args.dataset}\n', f'- benchmark: {args.benchmark}\n', f'- metric: {args.metric}\n', f'- env_name: {env_name}\n', f'- visible_gpu_count: {visible_gpus}\n', f'- max_parallel_trials: {max_parallel_trials}\n', f'- command_source: {command_source}\n', f'- command_kind: {command_kind}\n\n', '## Readiness Checks\n', f'- repo_registered: {bool(repo)}\n', f"- repo_path_available: {bool(plan['selected_repo']['local_path'])}\n", f"- dataset_registered: {dataset.get('name', '') == args.dataset}\n", f"- dataset_available: {dataset.get('available', False)}\n", f"- launch_ready_methods: {sum((1 for row in methods if row.get('launch_ready')))}/{len(methods)}\n\n", '## Research Quality Contract\n', f"- novelty_target: {quality_focus.get('novelty_target', '')}\n", f"- claim_target: {quality_focus.get('claim_target', '')}\n", f"- counterexample_target: {quality_focus.get('counterexample_target', '')}\n\n", '| Priority | Method | Launch Ready | GPU/Trial | Planned Trials | Command Kind | Command Source | Trial Focuses | Audit Contract |\n', '| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n']
    for row in methods:
        focuses = '; '.join((trial['focus'] for trial in row['trials']))
        lines.append(f"| {row['priority']} | {row['method']} | {row['launch_ready']} | {row['gpu_per_trial']} | {row['planned_trials']} | {row['command_kind']} | {row['command_source']} | {focuses} | metrics + bad_cases + audit.json |\n")
        contract = row['claim_contract']
        lines.extend([f"\n### {row['method']} claim contract\n", f"- novelty_hypothesis: {contract['novelty_hypothesis']}\n", f"- claim_to_test: {contract['claim_to_test']}\n", f"- support_threshold: {contract['support_threshold']}\n", f"- counterexample_test: {contract['counterexample_test']}\n", f"- continue_rule: {contract['continue_rule']}\n", f"- prune_rule: {contract['prune_rule']}\n"])
    (paths.planning / 'parallel_experiment_plan.md').write_text(''.join(lines), encoding='utf-8')
    print(paths.planning / 'parallel_experiment_plan.md')


# ---- iteration reflection tool ----
def _reflect_read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''

def _reflect_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def _reflect_ratio(num: int, den: int) -> float:
    return 0.0 if den == 0 else round(num / den, 4)

def run_reflect(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    lint = _reflect_read(paths.reports / 'lint_report.md')
    health = _reflect_read(paths.reports / 'healthcheck.md')
    init_brief = _reflect_read(paths.planning / 'init_brief.md')
    quality = _reflect_load_json(paths.state / 'paper_quality.json')
    quality_md = _reflect_read(paths.planning / 'paper_quality.md')
    machine = _reflect_read(paths.reports / 'machine_profile.md')
    parallel_plan = _reflect_read(paths.planning / 'parallel_experiment_plan.md')
    next_actions = _reflect_read(paths.planning / 'next_actions.md')
    next_actions_json = _reflect_load_json(paths.state / 'next_actions.json')
    experiments = _reflect_load_json(paths.state / 'experiment_registry.json')
    if not isinstance(experiments, list):
        experiments = []
    failure_analyses = [_reflect_load_json(path) for path in sorted(paths.state.glob('failure_analysis_*.json'))]
    lint_json = _reflect_load_json(paths.state / 'lint_report.json')
    manifest = _reflect_load_json(paths.state / 'research_manifest.json')
    completed = [row for row in experiments if str(row.get('status', '')).lower() in {'completed', 'success'}]
    failed = [row for row in experiments if str(row.get('status', '')).lower() in {'failed', 'error', 'incomplete_audit'}]
    analyzed_failed = [row for row in failed if row.get('failure_analysis_path')]
    bad_case_runs = [row for row in experiments if row.get('bad_case_slices')]
    claim_checked = [row for row in experiments if row.get('claim_verdict')]
    counterexample_checked = [row for row in experiments if row.get('counterexample_outcome')]
    audit_ready = [row for row in experiments if row.get('audit_ready')]
    prune_ready = [item for item in failure_analyses if item.get('recommendation') in PRUNE_RECOMMENDATIONS]
    deepen_ready_count = manifest.get('methods_deepen_ready_count', 0) if isinstance(manifest, dict) else 0
    method_summaries = next_actions_json.get('method_summaries', []) if isinstance(next_actions_json, dict) else []
    active_methods = len(method_summaries)
    claim_ratio = _reflect_ratio(sum((1 for row in method_summaries if row.get('claim_strength_score', 0) != 0)), active_methods)
    bad_case_ratio = _reflect_ratio(sum((1 for row in method_summaries if row.get('bad_case_slice_count', 0) > 0)), active_methods)
    counterexample_ratio = _reflect_ratio(sum((1 for row in method_summaries if row.get('counterexample_score', 0) != 0)), active_methods)
    audit_ratio = _reflect_ratio(len(audit_ready), len(experiments))
    prune_ratio = _reflect_ratio(len(prune_ready), active_methods)
    closure_signals = {'planning_ready': 1.0 if parallel_plan.strip() else 0.0, 'execution_ready': 1.0 if any((row.get('command') for row in experiments)) else 0.0, 'execution_evidence': 1.0 if completed or failed else 0.0, 'failure_diagnosis': _reflect_ratio(len(analyzed_failed), len(failed)) if failed else 0.0, 'claim_coverage': claim_ratio, 'bad_case_coverage': bad_case_ratio, 'counterexample_coverage': counterexample_ratio, 'audit_contract_coverage': audit_ratio, 'prune_discipline': prune_ratio, 'deepen_gate_readiness': _reflect_ratio(deepen_ready_count, active_methods) if active_methods else 0.0}
    score = round(sum(closure_signals.values()), 2)
    paper_rows = quality.get('papers', []) if isinstance(quality, dict) else []
    promising_papers = [row for row in paper_rows if row.get('top_tier_readiness') == 'promising']
    weak_papers = [row for row in paper_rows if row.get('top_tier_readiness') == 'weak']
    lines = ['# Iteration Reflection\n\n', f'- closure_score: {score}/{len(closure_signals)}\n', f'- experiments_completed: {len(completed)}\n', f'- experiments_failed_or_incomplete: {len(failed)}\n', f'- failed_with_analysis: {len(analyzed_failed)}\n', f'- bad_case_runs: {len(bad_case_runs)}\n', f'- claim_checked_runs: {len(claim_checked)}\n', f'- counterexample_checked_runs: {len(counterexample_checked)}\n', f'- audit_ready_runs: {len(audit_ready)}\n', f'- prune_candidates: {len(prune_ready)}\n\n', '## Loop Health\n']
    for key, value in closure_signals.items():
        lines.append(f'- {key}: {value}\n')
    lines.extend(['\n## Decision Quality\n', f'- active_methods: {active_methods}\n', f'- methods_deepen_ready_count: {deepen_ready_count}\n', f'- methods_with_claim_signal_ratio: {claim_ratio}\n', f'- methods_with_bad_case_ratio: {bad_case_ratio}\n', f'- methods_with_counterexample_ratio: {counterexample_ratio}\n', f'- methods_with_prune_signal_ratio: {prune_ratio}\n', '\n## Research Quality Scorecard\n', f'- promising_paper_signals: {len(promising_papers)}\n', f'- weak_paper_signals: {len(weak_papers)}\n', f"- lint_quality_pressure: {(lint_json.get('quality_pressure_count', 0) if isinstance(lint_json, dict) else 0)}\n", '\n## What improved this round\n', '- Experiment planning and execution are now expected to emit audit-contract artifacts, not only stdout logs.\n', '- Next-step planning now ranks methods using claim strength, novelty pressure, counterexample outcomes, and bad-case slice evidence.\n', '- Reflection now scores evidence density and pruning discipline rather than only checking whether files exist.\n', '\n## What remains weak\n'])
    if not completed and (not failed):
        lines.append('- The loop still lacks real experiment evidence; it has not yet entered the run-diagnose-decide stage.\n')
    if failed and len(analyzed_failed) < len(failed):
        lines.append('- Some failed or audit-incomplete runs still do not have matching failure analyses.\n')
    if audit_ratio < 1.0:
        lines.append('- Not every run passes the audit contract yet, so some evidence is still untrustworthy.\n')
    if bad_case_ratio == 0:
        lines.append('- Bad-case extraction is still not consistently attached to active methods.\n')
    if claim_ratio == 0:
        lines.append('- Claim verdicts are still too sparse to support strong scientific pruning or promotion.\n')
    if deepen_ready_count == 0 and active_methods:
        lines.append('- No method is currently strong enough to justify deeper scaling.\n')
    lines.extend(['\n## Machine Constraints\n\n', machine or 'Machine profile not generated yet.\n', '\n\n', '## Signals from Lint\n\n', lint or 'No lint report generated yet.\n', '\n\n', '## Signals from Healthcheck\n\n', health or 'No healthcheck generated yet.\n', '\n\n', '## Initialization Pressure Points\n\n', init_brief or 'No initialization brief generated yet.\n', '\n\n', '## Paper Quality and Taste Checks\n\n', quality_md or 'No paper quality report generated yet.\n', '\n\n', '## Parallel Method Plan\n\n', parallel_plan or 'No parallel plan created yet.\n', '\n\n', '## Priority Next Actions\n\n', next_actions or 'No next-actions file created yet.\n'])
    out = paths.reports / 'iteration_reflection.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


TOOL_ACTIONS = {
    "blocker_resolution": run_blocker_resolution,
    "experiments": run_experiments,
    "method_frontier": run_method_frontier,
    "reflect": run_reflect,
    "review_board": run_review_board,
    "workflow": run_workflow,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Planning module private report/packet/experiment tools.")
    parser.add_argument("--tool-action", required=True, choices=sorted(TOOL_ACTIONS))
    ns, rest = parser.parse_known_args(argv)
    TOOL_ACTIONS[ns.tool_action](rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
