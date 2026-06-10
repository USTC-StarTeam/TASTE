#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config

WORKSPACE_ROOT = ROOT / "modules" / "taste"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from auto_research.source_selection import canonical_source_selection


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def infer_repo(paths, explicit: str = '') -> str:
    if explicit:
        return explicit
    if current_env_selection_valid(paths):
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        selected = selection.get('selected', {}) if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
        for key in ('repo_path', 'local_path', 'path'):
            repo = str(selected.get(key) or '').strip()
            if repo and Path(repo).exists():
                return str(Path(repo).resolve())
        plan = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
        repo_obj = plan.get('repo', {}) if isinstance(plan, dict) and isinstance(plan.get('repo'), dict) else {}
        repo = str(repo_obj.get('repo_path') or repo_obj.get('local_path') or repo_obj.get('path') or '').strip()
        if repo and Path(repo).exists():
            return str(Path(repo).resolve())
    active = load_json(paths.state / 'active_repo.json', {})
    if isinstance(active, dict) and active.get('repo_path') and Path(active['repo_path']).exists():
        return str(active['repo_path'])
    plan = load_json(paths.state / 'parallel_plan.json', {})
    methods = plan.get('methods', []) if isinstance(plan, dict) else plan if isinstance(plan, list) else []
    for method in methods:
        repo = method.get('repo_path') if isinstance(method, dict) else ''
        if repo and Path(repo).exists():
            return str(repo)
    rows = load_json(paths.state / 'repo_candidates.json', [])
    for row in rows if isinstance(rows, list) else []:
        repo = row.get('local_path') if isinstance(row, dict) else ''
        if repo and Path(repo).exists():
            return str(repo)
    raise SystemExit('No local repo is available yet. Run TASTE initialization and repo candidate assessment first.')




def current_find_run_id(paths) -> str:
    progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    if isinstance(progress, dict) and str(progress.get('run_id') or '').strip():
        return str(progress.get('run_id') or '').strip()
    fresh = load_json(paths.state / 'fresh_research_base.json', {})
    return str(fresh.get('fresh_find_run_id') or '').strip() if isinstance(fresh, dict) else ''


def current_env_selection_valid(paths) -> bool:
    run_id = current_find_run_id(paths)
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if not isinstance(selection, dict):
        return False
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    selected_run = str(selection.get('fresh_find_run_id') or selected.get('fresh_find_run_id') or '').strip()
    stage = str(selection.get('selection_stage') or selection.get('selected_by_stage') or selected.get('selection_stage') or '').strip()
    accepted = bool(str(selection.get('selection_gate') or '').startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate')) or (isinstance(selection.get('claude_topic_decision'), dict) and selection['claude_topic_decision'].get('accept_as_current_best')))
    return bool(run_id and selected and selected_run == run_id and stage == 'environment_claude_code' and accepted)


def select_current_run_environment_repo(project: str, paths, env_name: str, max_rounds: int = 3) -> str:
    run_id = current_find_run_id(paths)
    if not run_id:
        raise SystemExit('Current Find run_id is missing; cannot perform environment-stage base selection.')
    # Build/refresh current-run candidate pool first; audit must consume the same Find run that the UI shows.
    run_optional([sys.executable, 'scripts/select_fresh_research_base.py', '--project', project], ROOT)
    run_optional([sys.executable, 'scripts/run_literature_base_audit.py', '--project', project, '--limit', '9', '--repo-search-per-candidate', '2', '--repo-limit', '5', '--probe-timeout-sec', '120', '--fresh-find-run-id', run_id], ROOT)
    selector = [
        sys.executable, 'scripts/select_evidence_ready_repo.py', '--project', project,
        '--env-name', env_name, '--limit', '12', '--timeout-sec', '180',
        '--allow-veto-fallback', '--write-active', '--use-claude-review',
        '--selection-stage', 'environment_claude_code',
        '--candidate-source', 'fresh_literature_github_search',
        '--fresh-find-run-id', run_id,
        '--exclude-active-repo',
    ]
    for round_index in range(1, max(1, max_rounds) + 1):
        print(f'TASTE current-run environment repo-selection iteration {round_index}/{max_rounds}', flush=True)
        rc = run_optional(selector, ROOT)
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        selected = selection.get('selected') if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
        repo = str(selected.get('repo_path') or selected.get('local_path') or '').strip()
        if rc == 0 and repo and Path(repo).exists() and current_env_selection_valid(paths):
            return repo
        expand_repo_search(project, round_index)
    blocker_reason = 'Current Find environment-stage selection did not find an evidence-ready repo; old active_repo remains legacy/control only.'
    write_repo_selection_blocker(paths, blocker_reason, selection=load_json(paths.state / 'evidence_ready_repo_selection.json', {}))
    write_fresh_base_implementation_blocker(paths, run_id, blocker_reason)
    raise SystemExit(2)

def run(cmd: list[str], cwd: Path) -> int:
    print('$ ' + ' '.join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=cwd, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.returncode


def run_optional(cmd: list[str], cwd: Path) -> int:
    print('$ ' + ' '.join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=cwd, text=True)
    if proc.returncode != 0:
        print(f'optional command failed rc={proc.returncode}: {" ".join(cmd)}', flush=True)
    return proc.returncode


def reference_gate_passed(paths) -> bool:
    gate = load_json(paths.state / 'reference_reproduction_gate.json', {})
    return bool(isinstance(gate, dict) and gate.get('status') == 'pass' and gate.get('decision') == 'continue_base')


def repair_reference_reproduction_if_needed(project: str, paths, venue: str = '') -> None:
    gate = load_json(paths.state / 'reference_reproduction_gate.json', {})
    if reference_gate_passed(paths):
        print('Reference reproduction gate already passed; environment stage will not start a repair run.', flush=True)
        return
    blockers = gate.get('blockers', []) if isinstance(gate, dict) and isinstance(gate.get('blockers', []), list) else []
    print('Reference reproduction is still blocked after environment setup; launching TASTE reference-reproduction repair before novel experiments.', flush=True)
    if blockers:
        print('Top reference blocker: ' + str(blockers[0])[:500], flush=True)
    cmd = [
        sys.executable,
        'scripts/run_autonomous_research.py',
        '--project',
        project,
        '--iterations',
        '1',
        '--execute-plan',
        '--prepare-env',
        '--real-bootstrap-env',
        '--skip-discovery',
        '--skip-paper',
        '--max-launches',
        '1',
    ]
    if venue:
        cmd.extend(['--venue', venue])
    run_optional(cmd, ROOT)


def conda_env_exists(cfg: dict, env_name: str) -> bool:
    if not env_name:
        return False
    candidates = []
    env_cfg = cfg.get('environment', {}) if isinstance(cfg, dict) else {}
    hint = str(env_cfg.get('conda_base_hint', '') or '') if isinstance(env_cfg, dict) else ''
    if hint:
        candidates.append(Path(hint) / 'envs' / env_name)
    candidates.extend([
        ROOT.parent / 'miniforge' / 'envs' / env_name,
        ROOT.parent / 'miniforge3' / 'envs' / env_name,
        Path.home() / 'miniforge3' / 'envs' / env_name,
        Path.home() / 'miniconda3' / 'envs' / env_name,
    ])
    return any(path.exists() for path in candidates)


def environment_is_locked(paths, cfg: dict, env_name: str, repo: str) -> bool:
    bootstrap = load_json(paths.state / 'repo_env_bootstrap.json', {})
    same_env = isinstance(bootstrap, dict) and str(bootstrap.get('env_name', '')) == env_name
    same_repo = isinstance(bootstrap, dict) and str(bootstrap.get('repo_path', '')) == str(repo)
    completed = same_env and same_repo and bootstrap.get('status') == 'completed'
    # A later prepare-only run must not unlock or downgrade a previously created env.
    return completed or conda_env_exists(cfg, env_name)


def has_claim_ready_probe(paths, repo: str) -> bool:
    probe = load_json(paths.state / 'real_dataset_probe.json', {})
    if not isinstance(probe, dict) or str(probe.get('repo_path', '')) != str(repo):
        return False
    for row in probe.get('probes', []) or []:
        if isinstance(row, dict) and row.get('claim_ready') and row.get('loader_probe', {}).get('success'):
            return True
    return False


def refresh_repo_data(project: str, repo: str, env_name: str) -> None:
    run([sys.executable, 'scripts/build_repo_data_requirements.py', '--project', project, '--repo-path', repo], ROOT)
    run([sys.executable, 'scripts/probe_repo_dataset.py', '--project', project, '--repo-path', repo, '--env-name', env_name, '--timeout-sec', '180'], ROOT)


def claude_accepts_current_route(paths) -> bool:
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    active = load_json(paths.state / 'active_repo.json', {})
    gate = str(selection.get('selection_gate', '')) if isinstance(selection, dict) else ''
    if gate.startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate')):
        return True
    decision = {}
    if isinstance(selection, dict) and isinstance(selection.get('claude_topic_decision'), dict):
        decision = selection.get('claude_topic_decision', {})
    elif isinstance(active, dict) and isinstance(active.get('claude_topic_fit_decision'), dict):
        decision = active.get('claude_topic_fit_decision', {})
    return bool(decision.get('accept_as_current_best'))


def load_repo_env_strategy(paths) -> dict:
    strategy = load_json(paths.state / 'repo_env_strategy.json', {})
    if isinstance(strategy, dict) and strategy:
        return strategy
    active = load_json(paths.state / 'active_repo.json', {})
    if isinstance(active, dict) and isinstance(active.get('claude_repo_env_strategy'), dict):
        return active.get('claude_repo_env_strategy', {})
    return {}


def strategy_env_name(strategy: dict, fallback: str) -> str:
    if not isinstance(strategy, dict):
        return fallback
    if str(strategy.get('env_action') or '') == 'create_new_project_env':
        proposed = str(strategy.get('recommended_env_name') or '').strip()
        if proposed:
            return proposed
    return fallback


def env_bootstrap_should_run(paths, cfg: dict, env_name: str, repo: str, strategy: dict) -> tuple[bool, str]:
    action = str(strategy.get('env_action') or '').strip()
    locked = environment_is_locked(paths, cfg, env_name, repo)
    if action == 'reuse_existing_env' and locked:
        return False, 'Claude strategy says to reuse the existing conda env; it is already present/locked.'
    if action == 'defer_until_repo_selected':
        return False, 'Claude strategy defers conda changes until a repo is accepted.'
    if action == 'repair_existing_env':
        return True, 'Claude strategy says to repair the current project env from local missing-dependency evidence.'
    if action == 'create_new_project_env':
        return True, 'Claude strategy says to create/use a new project-specific env; The workflow will not delete any old env.'
    if locked:
        return False, 'Environment is already present/locked and Claude did not request a repair/new project env.'
    return True, 'No locked env exists yet; first-time bootstrap is allowed.'




def project_search_queries(project: str) -> list[str]:
    cfg = load_project_config(project)
    queries: list[str] = []
    if isinstance(cfg, dict):
        for key in ("queries", "github_queries", "repo_search_queries", "literature_queries"):
            values = cfg.get(key)
            if isinstance(values, list):
                queries.extend(str(value).strip() for value in values if str(value).strip())
        for key in ("topic", "research_interest", "user_prompt"):
            value = str(cfg.get(key) or "").strip()
            if value:
                queries.append(value)
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        key = query.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(query)
    return out or ["reproducible scientific code with real dataset"]


def expand_repo_search(project: str, round_index: int, limit: int = 6) -> None:
    queries = project_search_queries(project)
    query = queries[(round_index - 1) % len(queries)]
    print(f"TASTE autonomous repo-search round {round_index}: {query}", flush=True)
    print("Environment repo search ignores Find-only source toggles; repo/data audit needs code evidence.", flush=True)
    run_optional([sys.executable, "scripts/discover_github_repos.py", "--project", project, "--query", query, "--limit", str(limit), "--sort", "stars", "--order", "desc", "--ignore-source-selection"], ROOT)
    run_optional([sys.executable, "scripts/discover_arxiv.py", "--project", project, "--query", query, "--max-results", "5", "--ignore-source-selection"], ROOT)
    run_optional([sys.executable, "scripts/ingest_discovery.py", "--project", project, "--limit", "12"], ROOT)
    run_optional([sys.executable, "scripts/assess_repo_candidates.py", "--project", project], ROOT)
    run_optional([sys.executable, "scripts/audit_repo_candidate_pool.py", "--project", project, "--limit", str(limit), "--include-watch", "--use-cursor"], ROOT)

def append_search_memory(paths, payload: dict) -> None:
    history_path = paths.state / 'repo_search_iteration_memory.json'
    history = load_json(history_path, [])
    if not isinstance(history, list):
        history = []
    history.append(payload)
    history_path.write_text(json.dumps(history[-30:], indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def write_repo_selection_blocker(paths, reason: str, *, selection: dict | None = None) -> None:
    selection = selection or {}
    run_id = str(selection.get('fresh_find_run_id') or current_find_run_id(paths) or '').strip()
    payload = {
        'status': 'blocked',
        'blocker_type': 'environment_repo_selection_blocked',
        'fresh_find_run_id': run_id,
        'reason': reason,
        'selection_gate': selection.get('selection_gate', ''),
        'selection_stage': selection.get('selection_stage', ''),
        'selected': selection.get('selected', {}),
        'rejected_selected': selection.get('rejected_selected', {}),
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (paths.state / 'repo_selection_blocker.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def write_fresh_base_implementation_blocker(paths, run_id: str, reason: str) -> None:
    payload = {
        'status': 'blocked_environment_repo_selection_required',
        'fresh_find_run_id': run_id,
        'reason': reason,
        'repo': {},
        'ready_datasets': [],
        'blocked_datasets': [],
        'blocker_reasons': [reason],
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'policy': 'Implementation plans are valid only for the current Find run after environment_claude_code selects an evidence-ready repo.',
    }
    (paths.state / 'fresh_base_implementation_plan.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def clerepo_selection_blocker(paths) -> None:
    blocker = paths.state / 'repo_selection_blocker.json'
    if blocker.exists():
        blocker.unlink()


def write_iteration_strategy(paths) -> None:
    report = paths.reports / 'repo_search_iteration_strategy.md'
    lines = [
        '# Repo Search Iteration Strategy\n\n',
        'TASTE now treats repo selection as an autonomous research loop rather than a one-shot data fallback.\n\n',
        '## Borrowed Patterns\n',
        '- ARIS pattern: executor evidence is reviewed adversarially; unsupported or off-topic success claims are rejected instead of carried forward.\n',
        '- EvoScientist pattern: failed validations become persistent memory, so the next search round uses prior rejections as search pressure rather than repeating them.\n',
        '- writing pattern: discovery and verification are decoupled; high-throughput candidate discovery feeds slower sequential verification and refinement.\n\n',
        '## Repo Gate\n',
        '- A repo must satisfy code entrypoint and real dataset loader success before it can be considered for experiments.\n',
        '- Topic fit is dynamic: Claude Code judges whether a repo is directly aligned or the best transformable base for the current project topic.\n',
        '- Claude Code can accept a runnable/data-ready repo with explicit required modifications, or reject it and trigger another search/audit round.\n',
    ]
    report.write_text(''.join(lines), encoding='utf-8')


def maybe_switch_to_evidence_ready_repo(project: str, paths, env_name: str, current_repo: str, max_rounds: int = 3) -> str:
    write_iteration_strategy(paths)
    if has_claim_ready_probe(paths, current_repo) and claude_accepts_current_route(paths):
        print('Active repo has claim-ready real data and Claude has accepted it as the current best aligned/transformable route; no repo switch needed.', flush=True)
        return current_repo
    print('Active repo lacks either claim-ready data evidence or Claude acceptance as the best transformable route; The workflow will iterate repo discovery/audit until a paired route is found or explicitly rejected.', flush=True)
    selector = [
        sys.executable, 'scripts/select_evidence_ready_repo.py', '--project', project,
        '--env-name', env_name, '--limit', '12', '--timeout-sec', '180',
        '--allow-veto-fallback', '--write-active', '--use-claude-review',
        '--selection-stage', 'environment_claude_code',
    ]
    for round_index in range(1, max(1, max_rounds) + 1):
        print(f'TASTE repo-selection iteration {round_index}/{max_rounds}', flush=True)
        rc = run_optional(selector, ROOT)
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        active = load_json(paths.state / 'active_repo.json', {})
        next_repo = str(active.get('repo_path', '') if isinstance(active, dict) else '')
        append_search_memory(paths, {
            'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
            'round': round_index,
            'selector_return_code': rc,
            'selection_gate': selection.get('selection_gate', ''),
            'selected': selection.get('selected', {}),
            'rejected_selected': selection.get('rejected_selected', {}),
            'audited_count': selection.get('audited_count', 0),
            'evidence_ready_count': selection.get('evidence_ready_count', 0),
            'repo_env_strategy': load_repo_env_strategy(paths),
        })
        if rc == 0 and next_repo and Path(next_repo).exists() and claude_accepts_current_route(paths):
            if next_repo != current_repo:
                print(f'TASTE switched active repo to Claude-accepted evidence-ready/transformable route: {next_repo}', flush=True)
            else:
                print('TASTE kept active repo after Claude accepted it as the current best transformable route.', flush=True)
            clerepo_selection_blocker(paths)
            return next_repo
        expand_repo_search(project, round_index)
    latest_selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    reason = 'Claude Code did not accept any audited repo as the current best evidence-ready and transformable route after autonomous search rounds; current repo remains blocked and must not be treated as final.'
    print(reason, flush=True)
    write_repo_selection_blocker(paths, reason, selection=latest_selection if isinstance(latest_selection, dict) else {})
    return current_repo


def main() -> int:
    parser = argparse.ArgumentParser(description='TASTE web stage 1: repo/data/env preparation with honest gates.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--env-name', default='')
    parser.add_argument('--real-bootstrap-env', action='store_true')
    parser.add_argument('--repo-search-rounds', type=int, default=3)
    parser.add_argument('--venue', default='')
    parser.add_argument('--skip-reference-repair', action='store_true')
    args = parser.parse_args()
    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    env_name = args.env_name or cfg.get('conda_env', '') or f"{args.project}_env".replace('-', '_')
    if args.repo_path:
        repo = infer_repo(paths, args.repo_path)
    elif current_env_selection_valid(paths):
        repo = infer_repo(paths, '')
    else:
        repo = select_current_run_environment_repo(args.project, paths, env_name, max_rounds=args.repo_search_rounds)
    env_name = args.env_name or cfg.get('conda_env', '') or f"{args.project}_{Path(repo).name}".replace('-', '_')
    print(f'TASTE environment stage: project={args.project}', flush=True)
    print(f'selected repo={repo}', flush=True)
    print(f'conda env={env_name}', flush=True)
    run([sys.executable, 'scripts/setup_git_guardrails.py', '--project', args.project, '--repo-path', repo], ROOT)
    refresh_repo_data(args.project, repo, env_name)
    if current_env_selection_valid(paths):
        repo = maybe_switch_to_evidence_ready_repo(args.project, paths, env_name, repo, max_rounds=args.repo_search_rounds)
    strategy = load_repo_env_strategy(paths)
    env_name = strategy_env_name(strategy, env_name)
    if strategy:
        print(
            f"Claude stewardship strategy: repo_action={strategy.get('repo_action', '')} "
            f"env_action={strategy.get('env_action', '')} data_action={strategy.get('data_action', '')} "
            f"env_name={env_name}",
            flush=True,
        )
    run([sys.executable, 'scripts/setup_git_guardrails.py', '--project', args.project, '--repo-path', repo], ROOT)
    refresh_repo_data(args.project, repo, env_name)
    run([sys.executable, 'scripts/build_fresh_base_implementation_plan.py', '--project', args.project], ROOT)
    should_bootstrap, bootstrap_reason = env_bootstrap_should_run(paths, cfg, env_name, repo, strategy)
    if not should_bootstrap:
        print(bootstrap_reason, flush=True)
    else:
        print(bootstrap_reason, flush=True)
        bootstrap = [sys.executable, 'scripts/bootstrap_repo_env.py', '--project', args.project, '--repo-path', repo, '--env-name', env_name, '--auto-install-missing']
        if args.real_bootstrap_env:
            bootstrap.append('--update-project-config')
        else:
            bootstrap.append('--prepare-only')
        run(bootstrap, ROOT)
    run([sys.executable, 'scripts/data_unavailability_policy.py', '--project', args.project], ROOT)
    audit_reference_cmd = [sys.executable, 'scripts/audit_reference_reproduction.py', '--project', args.project]
    if args.venue:
        audit_reference_cmd.extend(['--venue', args.venue])
    run_optional(audit_reference_cmd, ROOT)
    if not args.skip_reference_repair:
        repair_reference_reproduction_if_needed(args.project, paths, venue=args.venue)
        run_optional(audit_reference_cmd, ROOT)
    run_optional([sys.executable, 'scripts/audit_experiment_iteration.py', '--project', args.project], ROOT)
    run_optional([sys.executable, 'scripts/audit_paper_evidence.py', '--project', args.project], ROOT)
    run_optional([sys.executable, 'scripts/build_research_trajectory_system.py', '--project', args.project], ROOT)
    run_optional([sys.executable, 'scripts/build_blocker_action_plan.py', '--project', args.project], ROOT)
    run([sys.executable, 'scripts/report_status.py', '--project', args.project], ROOT)
    print('TASTE environment stage complete. Environment creation is one-time; reruns only refresh read-only repo/data/status checks once locked. Formal experiment claims still require reference_reproduction_gate=pass and scientific_progress_gate=pass.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
