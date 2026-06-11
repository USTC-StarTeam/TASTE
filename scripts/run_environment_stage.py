#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config
from run_project import current_find_execution_contract

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


def current_selected_execution_ids(paths) -> tuple[str, str]:
    try:
        contract = current_find_execution_contract(paths)
    except Exception:
        return '', ''
    if not isinstance(contract, dict):
        return '', ''
    return str(contract.get('selected_plan_id') or '').strip(), str(contract.get('selected_idea_id') or '').strip()


def current_selected_plan_id(paths) -> str:
    selected_plan_id, _ = current_selected_execution_ids(paths)
    return selected_plan_id


def current_env_selection_valid(paths) -> bool:
    run_id = current_find_run_id(paths)
    selected_plan_id = current_selected_plan_id(paths)
    if not selected_plan_id:
        return False
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if not isinstance(selection, dict):
        return False
    selected = selection.get('selected') if isinstance(selection.get('selected'), dict) else {}
    selected_run = str(selection.get('fresh_find_run_id') or selected.get('fresh_find_run_id') or '').strip()
    selection_plan_id = str(selection.get('selected_plan_id') or selected.get('selected_plan_id') or '').strip()
    stage = str(selection.get('selection_stage') or selection.get('selected_by_stage') or selected.get('selection_stage') or '').strip()
    accepted = bool(str(selection.get('selection_gate') or '').startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate')) or (isinstance(selection.get('claude_topic_decision'), dict) and selection['claude_topic_decision'].get('accept_as_current_best')))
    return bool(run_id and selected_plan_id and selection_plan_id == selected_plan_id and selected and selected_run == run_id and stage == 'environment_claude_code' and accepted)


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
        round_selector = list(selector)
        if round_index > 1 and '--candidate-source' in round_selector:
            source_index = round_selector.index('--candidate-source')
            del round_selector[source_index:source_index + 2]
        selector_timeout = int(os.environ.get('ENV_REPO_SELECTOR_TIMEOUT_SEC', '900') or '900')
        rc = run_optional(round_selector, ROOT, timeout=selector_timeout)
        selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
        selected = selection.get('selected') if isinstance(selection, dict) and isinstance(selection.get('selected'), dict) else {}
        repo = str(selected.get('repo_path') or selected.get('local_path') or '').strip()
        if rc == 0 and repo and Path(repo).exists() and current_env_selection_valid(paths):
            return repo
        expand_repo_search(project, round_index, fresh_find_run_id=run_id)
    blocker_reason = 'Current Find environment-stage selection did not find an evidence-ready repo; old active_repo remains legacy/control only.'
    write_repo_selection_blocker(paths, blocker_reason, selection=load_json(paths.state / 'evidence_ready_repo_selection.json', {}))
    write_fresh_base_implementation_blocker(paths, run_id, blocker_reason)
    raise SystemExit(2)

def run(cmd: list[str], cwd: Path, timeout: int | None = None) -> int:
    print('$ ' + ' '.join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f'command timed out after {timeout}s: {" ".join(cmd)}', flush=True)
        raise SystemExit(124)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.returncode


def run_optional(cmd: list[str], cwd: Path, timeout: int | None = None) -> int:
    print('$ ' + ' '.join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f'optional command timed out after {timeout}s: {" ".join(cmd)}', flush=True)
        return 124
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


def strategy_env_name(strategy: dict, fallback: str, *, explicit_env_name: str = '') -> str:
    if explicit_env_name:
        return explicit_env_name
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




def _query_placeholder_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _query_looks_like_project_id(value: str, project: str) -> bool:
    text = str(value or '').strip()
    if not text:
        return True
    project_key = _query_placeholder_key(project)
    text_key = _query_placeholder_key(text)
    if not project_key:
        return False
    if text_key == project_key:
        return True
    # Generated smoke/default prompts can repeat the project id; those are still
    # placeholders and should never become repo-search queries.
    return bool(text_key and not text_key.replace(project_key, ''))


def _append_query(queries: list[str], value: object, project: str) -> None:
    text = ' '.join(str(value or '').split()).strip()
    lowered = text.lower()
    if not text or _query_looks_like_project_id(text, project):
        return
    if any(marker in lowered for marker in ['no repo selected', 'no repo has been selected', 'no audited repos exist', 'zero audited candidates', 'zero evidence-ready', 'initial search phase', 'future memory', 'none satisfied', 'none data-ready', 'no data-ready', 'current search found', 'needs-more-search', 'if no repo is good enough', 'after auditing', 'after reviewing', 'selected_active_repo=none']):
        return
    if lowered.startswith('topic ') and _query_looks_like_project_id(lowered.removeprefix('topic '), project):
        return
    generic_find_markers = [
        'ideas that directly help the current research loop',
        'the workflow should prioritize papers and ideas that directly help the current research loop',
    ]
    project_key = _query_placeholder_key(project)
    text_key = _query_placeholder_key(text)
    if any(marker in lowered for marker in generic_find_markers) and (
        lowered in generic_find_markers
        or 'research goal:' in lowered
        or bool(project_key and project_key in text_key)
    ):
        return
    # Search backends work better with concise, evidence-bearing phrases than
    # long generated paragraphs. Keep titles intact, trim only oversized text.
    if len(text) > 180:
        text = text[:180].rsplit(' ', 1)[0].strip() or text[:180]
    if text:
        queries.append(text)


def _quoted_search_terms(text: object) -> list[str]:
    raw = str(text or '')
    terms = re.findall(r"['\"]([^'\"]{4,120})['\"]", raw)
    out: list[str] = []
    for term in terms:
        cleaned = ' '.join(term.split()).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _stewardship_short_phrases(text: object) -> list[str]:
    raw = ' '.join(str(text or '').replace('；', ',').replace('，', ',').split())
    if not raw:
        return []
    first_sentence = re.split(r'[。.!?]\s+', raw, maxsplit=1)[0]
    first_sentence = re.sub(r'^(?:Search|Look)\s+for\s+(?:repositories|repos)\s+(?:explicitly\s+)?(?:related\s+to\s+)?', '', first_sentence, flags=re.I)
    first_sentence = re.sub(r'^Prioritize\s+(?:repositories|repos)\s+(?:with\s+)?', '', first_sentence, flags=re.I)
    pieces = re.split(r',|\bor\b|\band\b|/|、|\s+for\s+', first_sentence)
    phrases: list[str] = []
    for piece in pieces:
        cleaned = ' '.join(piece.split()).strip(' .;:')
        cleaned = re.sub(r'^(?:with|for|the|a|an|or)\s+', '', cleaned, flags=re.I)
        cleaned = re.sub(r'^(?:evaluating|evaluate|evaluation\s+of)\s+', '', cleaned, flags=re.I)
        if 4 <= len(cleaned) <= 90 and not re.search(r'\b(no|none|without|lack|lacking|missing)\b', cleaned, flags=re.I):
            phrases.append(cleaned)
    out: list[str] = []
    for phrase in phrases:
        if phrase and phrase.lower() not in {item.lower() for item in out}:
            out.append(phrase)
    return out



def _stewardship_memory_is_search_guidance(text: object) -> bool:
    lowered = str(text or '').lower().strip()
    if not lowered:
        return False
    if lowered.startswith(('after auditing', 'after reviewing', 'after the audit')) or any(marker in lowered for marker in ['none satisfied', 'none data-ready', 'current search found', 'needs-more-search', 'no repo selected', 'future memory']):
        return False
    if any(marker in lowered for marker in ['no repo has been selected', 'no audited repos exist', 'zero audited candidates', 'zero evidence-ready', 'initial search phase']):
        return False
    if any(marker in lowered for marker in ['search for', 'priority search', 'target repos', 'target repositories', 'next search', 'continue searching for']):
        return True
    return False


def _current_find_query_context(paths, project: str) -> list[str]:
    queries: list[str] = []

    def selected_row(rows: object, selected_id: str, id_keys: tuple[str, ...]) -> dict:
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get('selected_for_execution') or row.get('execute_next'):
                return row
            selection = row.get('execution_selection')
            if isinstance(selection, dict) and selection.get('selected'):
                return row
            if selected_id and any(str(row.get(key) or '').strip() == selected_id for key in id_keys):
                return row
        return {}

    plans_payload = load_json(paths.planning / 'finding' / 'plans.json', {})
    if isinstance(plans_payload, dict):
        selected_plan = selected_row(plans_payload.get('plans'), str(plans_payload.get('selected_plan_id') or '').strip(), ('plan_id', 'id'))
        if selected_plan:
            for key in ('title', 'objective', 'summary', 'research_question', 'description'):
                _append_query(queries, selected_plan.get(key), project)

    ideas_payload = load_json(paths.planning / 'finding' / 'ideas.json', {})
    if isinstance(ideas_payload, dict):
        selected_idea = selected_row(ideas_payload.get('ideas'), str(ideas_payload.get('selected_idea_id') or '').strip(), ('id', 'idea_id'))
        if selected_idea:
            for key in ('title', 'objective', 'summary', 'hypothesis', 'method'):
                _append_query(queries, selected_idea.get(key), project)

    find_results = load_json(paths.planning / 'finding' / 'find_results.json', {})
    if isinstance(find_results, dict):
        paper_rows: list[dict] = []
        for key in ('articles', 'read_candidates', 'strong_recommendations', 'recommended_papers', 'papers'):
            rows = find_results.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    paper_rows.append(row)
        seen_titles: set[str] = set()
        for row in paper_rows[:12]:
            title = str(row.get('title') or '').strip()
            title_key = title.lower()
            if title and title_key not in seen_titles:
                seen_titles.add(title_key)
                _append_query(queries, f'{title} code dataset', project)

        stage0 = find_results.get('stage0_profile') if isinstance(find_results.get('stage0_profile'), dict) else {}
        profile = stage0.get('profile') if isinstance(stage0.get('profile'), dict) else {}
        explicit = profile.get('explicit_profile') if isinstance(profile.get('explicit_profile'), dict) else {}
        summary = explicit.get('research_interest_summary')
        for phrase in _stewardship_short_phrases(summary):
            _append_query(queries, phrase, project)
        _append_query(queries, summary, project)
        retrieval_text = stage0.get('retrieval_text')
        for phrase in _stewardship_short_phrases(retrieval_text):
            _append_query(queries, phrase, project)
        _append_query(queries, retrieval_text, project)
    return queries


def _stewardship_query_context(paths, project: str) -> list[str]:
    queries: list[str] = []
    sources = [
        load_json(paths.reports / 'repo_topic_fit_decision.json', {}),
        (load_json(paths.state / 'evidence_ready_repo_selection.json', {}) or {}).get('claude_topic_decision', {}),
    ]
    for payload in sources:
        if not isinstance(payload, dict):
            continue
        memory = payload.get('stewardship_memory')
        if _stewardship_memory_is_search_guidance(memory):
            for term in _quoted_search_terms(memory):
                _append_query(queries, term, project)
            for phrase in _stewardship_short_phrases(memory):
                _append_query(queries, phrase, project)
            first_sentence = re.split(r'[。.!?]\s+', str(memory or '').strip(), maxsplit=1)[0]
            _append_query(queries, first_sentence, project)
        for key in ['data_action_reason', 'repo_action_reason', 'rationale']:
            for term in _quoted_search_terms(payload.get(key)):
                _append_query(queries, term, project)
    return queries


def project_search_queries(project: str) -> list[str]:
    cfg = load_project_config(project)
    paths = build_paths(project)
    explicit_queries: list[str] = []
    config_context: list[str] = []
    if isinstance(cfg, dict):
        for key in ('queries', 'github_queries', 'repo_search_queries', 'literature_queries'):
            values = cfg.get(key)
            if isinstance(values, list):
                for value in values:
                    _append_query(explicit_queries, value, project)
        for key in ('topic', 'research_interest', 'user_prompt'):
            _append_query(config_context, cfg.get(key), project)
    queries: list[str] = []
    queries.extend(explicit_queries)
    queries.extend(_stewardship_query_context(paths, project))
    # If the saved config topic is only a project id, the current Find/Plan
    # outputs are the authoritative research context for environment search.
    queries.extend(_current_find_query_context(paths, project))
    queries.extend(config_context)
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        key = query.lower()
        if key and key not in seen and not _query_looks_like_project_id(query, project):
            seen.add(key)
            out.append(query)
    return out or ['reproducible scientific code with real dataset']

def expand_repo_search(project: str, round_index: int, limit: int = 6, fresh_find_run_id: str = '') -> None:
    queries = project_search_queries(project)
    query = queries[(round_index - 1) % len(queries)]
    print(f"TASTE autonomous repo-search round {round_index}: {query}", flush=True)
    print("Environment repo search ignores Find-only source toggles; repo/data audit needs code evidence.", flush=True)
    github_cmd = [sys.executable, "scripts/discover_github_repos.py", "--project", project, "--query", query, "--limit", str(limit), "--sort", "stars", "--order", "desc", "--ignore-source-selection", "--candidate-source", "environment_expanded_github_search"]
    if fresh_find_run_id:
        github_cmd.extend(["--fresh-find-run-id", fresh_find_run_id])
    run_optional(github_cmd, ROOT)
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
    selected = selection.get('selected', {}) if isinstance(selection.get('selected'), dict) else {}
    current_plan_id, current_idea_id = current_selected_execution_ids(paths)
    run_id = str(selection.get('fresh_find_run_id') or current_find_run_id(paths) or '').strip()
    selected_plan_id = str(selection.get('selected_plan_id') or selected.get('selected_plan_id') or current_plan_id or '').strip()
    selected_idea_id = str(selection.get('selected_idea_id') or selected.get('selected_idea_id') or current_idea_id or '').strip()
    payload = {
        'status': 'blocked',
        'blocker_type': 'environment_repo_selection_blocked',
        'fresh_find_run_id': run_id,
        'selected_plan_id': selected_plan_id,
        'selected_idea_id': selected_idea_id,
        'reason': reason,
        'selection_gate': selection.get('selection_gate', ''),
        'selection_stage': selection.get('selection_stage', ''),
        'selected': selected,
        'rejected_selected': selection.get('rejected_selected', {}),
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (paths.state / 'repo_selection_blocker.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def write_fresh_base_implementation_blocker(paths, run_id: str, reason: str) -> None:
    selected_plan_id, selected_idea_id = current_selected_execution_ids(paths)
    payload = {
        'status': 'blocked_environment_repo_selection_required',
        'fresh_find_run_id': run_id,
        'selected_plan_id': selected_plan_id,
        'selected_idea_id': selected_idea_id,
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
        selector_timeout = int(os.environ.get('ENV_REPO_SELECTOR_TIMEOUT_SEC', '900') or '900')
        rc = run_optional(selector, ROOT, timeout=selector_timeout)
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
    explicit_env_name = str(args.env_name or cfg.get('conda_env', '') or '').strip()
    env_name = explicit_env_name or f"{args.project}_env".replace('-', '_')
    if args.repo_path:
        repo = infer_repo(paths, args.repo_path)
    elif current_env_selection_valid(paths):
        repo = infer_repo(paths, '')
    else:
        repo = select_current_run_environment_repo(args.project, paths, env_name, max_rounds=args.repo_search_rounds)
    env_name = explicit_env_name or f"{args.project}_{Path(repo).name}".replace('-', '_')
    print(f'TASTE environment stage: project={args.project}', flush=True)
    print(f'selected repo={repo}', flush=True)
    print(f'conda env={env_name}', flush=True)
    run([sys.executable, 'scripts/setup_git_guardrails.py', '--project', args.project, '--repo-path', repo], ROOT)
    refresh_repo_data(args.project, repo, env_name)
    if current_env_selection_valid(paths):
        repo = maybe_switch_to_evidence_ready_repo(args.project, paths, env_name, repo, max_rounds=args.repo_search_rounds)
    strategy = load_repo_env_strategy(paths)
    recommended_env_name = str(strategy.get('recommended_env_name') or '').strip() if isinstance(strategy, dict) else ''
    env_name = strategy_env_name(strategy, env_name, explicit_env_name=explicit_env_name)
    if strategy:
        if explicit_env_name and recommended_env_name and recommended_env_name != explicit_env_name:
            print(
                f"Explicit environment name {explicit_env_name} overrides Claude recommended_env_name={recommended_env_name}; "
                "environment stage will repair/reuse the configured environment instead of switching names.",
                flush=True,
            )
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
