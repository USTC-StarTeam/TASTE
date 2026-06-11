#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from llm_agent_core import llm_json, llm_text, safe_json_loads
from llm_client import llm_available, llm_disabled_reason
from project_paths import ROOT, build_paths, load_project_config
from pipeline_guard import guard_fresh_base_blocker_entry


def run(cmd: list[str], cwd: Path, timeout: int = 600, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')




class TimeoutBlock(Exception):
    pass


class time_limit:
    def __init__(self, seconds: int, label: str):
        self.seconds = seconds
        self.label = label
        self.previous = None

    def __enter__(self):
        if self.seconds <= 0 or not hasattr(signal, 'SIGALRM'):
            return self
        self.previous = signal.getsignal(signal.SIGALRM)
        def handler(_signum, _frame):
            raise TimeoutBlock(f'{self.label} exceeded {self.seconds}s')
        signal.signal(signal.SIGALRM, handler)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.seconds > 0 and hasattr(signal, 'SIGALRM'):
            signal.alarm(0)
            if self.previous is not None:
                signal.signal(signal.SIGALRM, self.previous)
        return False


def append_trace(paths, trace: dict, stage: str, **fields) -> None:
    row = {'stage': stage, **fields, 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}
    trace.setdefault('steps', []).append(row)
    save_json(paths.state / 'autoscientist_trace.json', trace)


def slugify(value: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '_', value.strip().lower()).strip('_') or 'item'


SHELL_META_TOKENS = ('|', '&', ';', '<', '>', '(', ')', '$', chr(96), chr(10))


def command_needs_shell(command: str) -> bool:
    return any(token in (command or '') for token in SHELL_META_TOKENS)


def repo_selection_floor(cfg: dict) -> float:
    try:
        return float(cfg.get('literature', {}).get('repo_candidate_floor', 8.0))
    except Exception:
        return 8.0


def repo_is_acceptable(row: dict, cfg: dict) -> tuple[bool, str]:
    score = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
    bucket = str(row.get('repo_selection_bucket', ''))
    if bucket == 'paused_by_veto':
        return False, 'paused_by_veto'
    if row.get('hard_topic_mismatch'):
        return False, 'hard_topic_mismatch:' + ','.join(row.get('hard_missing_topic_groups', []) or [])
    if score < repo_selection_floor(cfg):
        return False, f'score_below_floor:{score}<{repo_selection_floor(cfg)}'
    if bucket not in {'promising'}:
        return False, f'not_promising:{bucket or "unknown"}'
    return True, 'accepted'


def write_repo_selection_blocker(paths, rows: list[dict], cfg: dict, reasons: list[dict]) -> None:
    payload = {
        'status': 'blocked_no_acceptable_repo',
        'repo_candidate_floor': repo_selection_floor(cfg),
        'candidate_count': len(rows),
        'top_rejections': reasons[:12],
        'policy': 'AutoScientist must restart discovery/workflow feedback instead of executing weak or vetoed codebases.',
    }
    save_json(paths.state / 'repo_selection_blocker.json', payload)
    lines = [
        '# Repo Selection Blocked\n\n',
        f"- candidate_count: {len(rows)}\n",
        f"- repo_candidate_floor: {payload['repo_candidate_floor']}\n",
        '- policy: do not execute weak/watch/vetoed repos; restart discovery or improve search queries.\n\n',
        '## Top Rejections\n',
    ]
    for row in payload['top_rejections']:
        lines.append(f"- {row.get('name','')} | score={row.get('score')} | bucket={row.get('bucket')} | reason={row.get('reason')} | {row.get('url','')}\n")
    (paths.reports / 'repo_selection_blocked.md').write_text(''.join(lines), encoding='utf-8')

def preaudit_watch_repos(project: str, paths, cfg: dict, limit: int = 3) -> list[dict]:
    rows = load_json(paths.state / 'repo_candidates.json', [])
    overrides = load_json(paths.state / 'method_overrides.json', {'repos': {}})
    paused_repos = overrides.get('repos', {}) if isinstance(overrides, dict) else {}
    ranked = sorted(rows, key=lambda row: (-float(row.get('repo_reuse_score', row.get('score', 0)) or 0), row.get('name', '')))
    audited: list[dict] = []
    for row in ranked:
        if len(audited) >= limit:
            break
        if row.get('local_path') or row.get('repo_selection_bucket') == 'paused_by_veto':
            continue
        if row.get('hard_topic_mismatch'):
            continue
        url = str(row.get('url', ''))
        if not url.startswith('http'):
            continue
        target = paths.repos_selected / slugify(str(row.get('name') or 'candidate_repo'))
        if str(target) in paused_repos:
            continue
        try:
            local_path = clone_or_update_repo(paths, row)
        except SystemExit as exc:
            audited.append({'name': row.get('name', ''), 'status': 'clone_failed', 'reason': str(exc)[:500]})
            continue
        rc = run_project_cmd([
            'scripts/audit_local_repo.py', '--project', project, '--repo-path', str(local_path),
            '--name', str(row.get('name', local_path.name)), '--url', url,
            '--summary', str(row.get('summary', '')), '--task-fit',
            '--stars', str(row.get('stars', 0) or 0), '--forks', str(row.get('forks', 0) or 0),
            '--last-pushed-at', str(row.get('last_pushed_at', '')),
            '--topics', ','.join(row.get('topics', []) or []),
            '--language', str(row.get('language', '')),
        ], timeout=180)
        audited.append({'name': row.get('name', ''), 'status': 'audited' if rc == 0 else 'audit_failed', 'return_code': rc, 'local_path': str(local_path)})
    save_json(paths.state / 'repo_preaudit.json', {'audited': audited, 'limit': limit, 'policy': 'clone top non-vetoed watch candidates before accepting/rejecting repo selection'})
    return audited

def choose_repo(paths, cfg: dict) -> dict:
    rows = load_json(paths.state / 'repo_candidates.json', [])
    overrides = load_json(paths.state / 'method_overrides.json', {'repos': {}})
    paused_repos = overrides.get('repos', {}) if isinstance(overrides, dict) else {}
    filtered = []
    rejections = []
    for row in rows:
        local_path = str(row.get('local_path', ''))
        if local_path and paused_repos.get(local_path, {}).get('status') in {'paused_or_abandoned', 'abandoned'}:
            reason = 'repo_paused_by_research_veto'
        else:
            ok, reason = repo_is_acceptable(row, cfg)
            if ok:
                filtered.append(row)
                continue
        rejections.append({
            'name': row.get('name', ''),
            'url': row.get('url', ''),
            'score': row.get('repo_reuse_score', row.get('score', 0)),
            'bucket': row.get('repo_selection_bucket', ''),
            'reason': reason,
        })
    if not filtered and not getattr(paths, '_preaudit_attempted', False):
        setattr(paths, '_preaudit_attempted', True)
        preaudit_watch_repos(str(getattr(paths, 'project', '')), paths, cfg, limit=int(cfg.get('repo_selection', {}).get('preaudit_top_k', 3) or 3))
        return choose_repo(paths, cfg)
    if not filtered:
        write_repo_selection_blocker(paths, rows, cfg, rejections)
        raise SystemExit('No acceptable non-vetoed repo candidate met the evidence floor; discovery must restart before experiments.')
    def key(row):
        return (-float(row.get('repo_reuse_score', row.get('score', 0)) or 0), row.get('activity_age_days') if isinstance(row.get('activity_age_days'), int) else 10**9, row.get('name', ''))
    return sorted(filtered, key=key)[0]


def reload_repo_candidate(paths, name: str, repo_path: Path, fallback: dict) -> dict:
    rows = load_json(paths.state / 'repo_candidates.json', [])
    for row in rows if isinstance(rows, list) else []:
        if row.get('name') == name or str(row.get('local_path', '')) == str(repo_path):
            return row
    return fallback

def clone_or_update_repo(paths, repo: dict) -> Path:
    url = repo.get('url', '')
    name = repo.get('name', 'selected_repo')
    target = paths.repos_selected / slugify(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target / '.git').exists():
        # Existing shallow clones are usable; avoid blocking the loop on slow GitHub fetches.
        if os.environ.get('REFRESH_EXISTING_REPOS', '').lower() in {'1', 'true', 'yes'}:
            run(['git', 'fetch', '--all', '--prune'], target, timeout=45)
        return target
    if target.exists():
        shutil.rmtree(target)
    if not url or not str(url).startswith('http'):
        raise SystemExit(f'Selected repo has no cloneable URL: {name} {url}')
    proc = run(['git', 'clone', '--depth', '1', url, str(target)], ROOT, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or f'git clone failed for {url}')
    return target


def read_repo_files(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    candidates = ['README.md', 'readme.md', 'requirements.txt', 'environment.yml', 'environment.yaml', 'setup.py', 'pyproject.toml']
    for rel in candidates:
        path = repo / rel
        if path.exists() and path.is_file():
            out[rel] = path.read_text(encoding='utf-8', errors='ignore')[:12000]
    for path in sorted(repo.rglob('*.py'))[:12]:
        if '.git' in path.parts:
            continue
        try:
            out[str(path.relative_to(repo))] = path.read_text(encoding='utf-8', errors='ignore')[:6000]
        except Exception:
            pass
    return out


def llm_research_plan(project: str, repo: dict, repo_context: dict[str, str], cfg: dict) -> dict:
    fallback = {
        'idea': 'Repo-first reproduction and controlled improvement for the configured research topic.',
        'dataset': 'generated_smoke_benchmark',
        'benchmark': 'generated_pipeline_smoke',
        'metric': 'ndcg_at_10',
        'methods': [],
        'experiment_command': 'python3 autoscientist_experiment.py --variant {method_slug} --artifact-dir {artifact_dir} --seed {seed}',
        'paper_claim': 'Only a smoke-test claim until real datasets and baselines are available.',
    }
    if not llm_available(cfg):
        fallback['llm_unavailable'] = llm_disabled_reason(cfg)
        return fallback
    prompt = {
        'project': project,
        'topic': cfg.get('topic', ''),
        'repo_candidate': repo,
        'repo_context': repo_context,
        'instruction': 'Create an executable autonomous research plan. Use the repo as reference when possible, but if real data is unavailable choose a minimal reproducible smoke benchmark and require honest claims. Return JSON keys: idea,dataset,benchmark,metric,methods,experiment_command,paper_claim,risks,prune_rules.',
    }
    try:
        with time_limit(int(os.environ.get('AUTOSCIENTIST_PLAN_TIMEOUT_SEC', '90')), 'llm_research_plan'):
            parsed, _raw = llm_json(json.dumps(prompt, ensure_ascii=False), cfg, system_prompt='Return strict JSON only.')
            if not parsed:
                text = llm_text(json.dumps(prompt, ensure_ascii=False), cfg, system_prompt='Return strict JSON only.')
                parsed = safe_json_loads(text.get('content', ''), {})
        if isinstance(parsed, dict) and parsed:
            for key, value in fallback.items():
                parsed.setdefault(key, value)
            return parsed
    except Exception as exc:
        fallback['llm_error'] = str(exc)
        fallback['llm_plan_fallback_policy'] = 'Proceed with evidence-gated fallback plan; do not strengthen scientific claims from this fallback.'
    return fallback


HARNESS = r'''#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math, random
from pathlib import Path

def make_data(seed, users=64, items=96, dim=12):
    rng = random.Random(seed)
    user = [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(users)]
    item = [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(items)]
    positives = []
    for u in range(users):
        scores = []
        for i in range(items):
            base = sum(user[u][k] * item[i][k] for k in range(dim)) / math.sqrt(dim)
            if i > int(items * 0.8):
                base -= 0.25
            scores.append((base + rng.gauss(0, 0.12), i))
        scores.sort(reverse=True)
        positives.append({i for _, i in scores[:8]})
    return user, item, positives

def variant_profile(variant):
    normalized = sum(ord(ch) for ch in variant or 'generic')
    mode = normalized % 3
    return {
        'collaborative_weight': 0.80 + 0.05 * mode,
        'global_bias_weight': 0.04 * ((normalized // 3) % 4),
        'tail_bias': 0.04 * ((normalized // 7) % 5),
        'noise': 0.01 * ((normalized // 11) % 4),
    }


def rank_items(user_vec, item, variant, rng):
    profile = variant_profile(variant)
    scored = []
    tail_start = int(len(item) * 0.8)
    for i, vec in enumerate(item):
        dot = sum(user_vec[k] * vec[k] for k in range(len(vec))) / math.sqrt(len(vec))
        semantic_proxy = sum(vec) / len(vec)
        score = profile['collaborative_weight'] * dot + profile['global_bias_weight'] * semantic_proxy
        if i >= tail_start:
            score += profile['tail_bias']
        score += rng.gauss(0, profile['noise'])
        scored.append((score, i))
    scored.sort(reverse=True)
    return [i for _, i in scored]

def ndcg_at_k(ranking, positives, k=10):
    dcg = 0.0
    for idx, item in enumerate(ranking[:k], start=1):
        if item in positives:
            dcg += 1.0 / math.log2(idx + 1)
    ideal_hits = min(k, len(positives))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--variant', required=True)
    ap.add_argument('--artifact-dir', required=True)
    ap.add_argument('--seed', type=int, default=1)
    args = ap.parse_args()
    artifact = Path(args.artifact_dir); artifact.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    user, item, positives = make_data(args.seed)
    vals, tail_vals, bad, scored_users = [], [], [], []
    for u, vec in enumerate(user):
        ranking = rank_items(vec, item, args.variant, rng)
        val = ndcg_at_k(ranking, positives[u], 10)
        vals.append(val)
        scored_users.append({'user': u, 'slice': 'worst_ndcg', 'ndcg_at_10': round(val, 6), 'top10': ranking[:10]})
        tail_pos = {i for i in positives[u] if i > int(len(item) * 0.8)}
        if tail_pos:
            tail_vals.append(ndcg_at_k(ranking, tail_pos, 10))
        if val < 0.18:
            bad.append({'user': u, 'slice': 'low_ndcg', 'ndcg_at_10': round(val, 6), 'top10': ranking[:10]})
    scored_users.sort(key=lambda row: row['ndcg_at_10'])
    if not bad:
        bad.extend(scored_users[:10])
    metrics = {'ndcg_at_10': sum(vals)/len(vals), 'tail_ndcg_at_10': sum(tail_vals)/len(tail_vals) if tail_vals else 0.0}
    verdict = 'support' if metrics['ndcg_at_10'] >= 0.20 else 'weak'
    if metrics['tail_ndcg_at_10'] > metrics['ndcg_at_10']:
        verdict = 'slice_support'
    bad_payload = {'path': str(artifact/'bad_cases.json'), 'count': len(bad[:20]), 'items': bad[:20], 'slices': sorted({row.get('slice', 'unknown') for row in bad[:20]})}
    audit = {'metrics': metrics, 'claim_verdict': verdict, 'novelty_note': 'Synthetic smoke benchmark only; cannot support final CIKM claim without real data.', 'counterexample_outcome': 'Fails as final evidence because benchmark is synthetic; useful only for pipeline validation.', 'bad_cases': bad_payload, 'bad_case_slices': bad_payload['slices']}
    (artifact/'metrics.json').write_text(json.dumps(metrics, indent=2)+'\n')
    (artifact/'bad_cases.json').write_text(json.dumps(bad[:20], indent=2)+'\n')
    (artifact/'audit.json').write_text(json.dumps(audit, indent=2)+'\n')
    print(json.dumps({'variant': args.variant, **metrics, 'claim_verdict': verdict}))

if __name__ == '__main__':
    main()
'''


def write_harness(repo: Path) -> Path:
    path = repo / 'autoscientist_experiment.py'
    path.write_text(HARNESS, encoding='utf-8')
    path.chmod(0o755)
    return path


def ensure_git(repo: Path) -> None:
    if not (repo / '.git').exists():
        run(['git', 'init'], repo, timeout=60)
    run(['git', 'config', 'user.email', 'ar@example.invalid'], repo, timeout=60)
    run(['git', 'config', 'user.name', 'TASTE AutoScientist'], repo, timeout=60)




def choose_probe_passed_dataset(paths) -> str:
    probe = load_json(paths.state / 'real_dataset_probe.json', {})
    for row in probe.get('probes', []) if isinstance(probe, dict) else []:
        if row.get('claim_ready') and (row.get('loader_probe_success') or row.get('loader_probe', {}).get('success')):
            return str(row.get('dataset', '')).strip()
    datasets = load_json(paths.state / 'dataset_registry.json', [])
    for row in datasets if isinstance(datasets, list) else []:
        name = str(row.get('name', '')).strip()
        if name and row.get('available') and row.get('claim_ready') and row.get('loader_probe_success') and not name.startswith('synthetic_'):
            return name
    return ''


def repo_data_blocked(paths) -> tuple[bool, list[str]]:
    req = load_json(paths.state / 'repo_data_requirements.json', {})
    if not isinstance(req, dict):
        return False, []
    blocked = [str(x) for x in req.get('blocked_datasets', []) if x]
    ready = [str(x) for x in req.get('ready_datasets', []) if x]
    return bool(blocked and not ready), blocked

def repo_real_command_template(dataset: str) -> str:
    script = ROOT / 'scripts' / 'run_active_repo_smoke.py'
    return f"python3 {shlex.quote(str(script))} --repo-path {{repo_path}} --dataset {shlex.quote(dataset)} --artifact-dir {{artifact_dir}} --seed {{seed}} --method-slug {{method_slug}}"

def register_synthetic_dataset(project: str) -> None:
    proc = run([sys.executable, str(ROOT / 'scripts/register_dataset.py'), '--project', project, '--name', 'generated_smoke_benchmark', '--task', 'generated pipeline smoke benchmark', '--access', 'generated locally by autoscientist_experiment.py', '--format', 'json metrics/audit artifacts', '--split', 'deterministic seeded synthetic split', '--metric', 'ndcg_at_10', '--available'], ROOT, timeout=120)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)


def run_project_cmd(args: list[str], timeout: int = 600) -> int:
    try:
        proc = run([sys.executable, *args], ROOT, timeout=timeout, env=os.environ.copy())
        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        stderr = (stderr or '') + chr(10) + f"TIMEOUT: {' '.join(args)} exceeded {timeout}s; AutoScientist will record the gap and continue." + chr(10)
        returncode = 124
    if stdout:
        print(stdout, end='')
    if stderr:
        print(stderr, end='', file=sys.stderr)
    return returncode



def main() -> int:
    ap = argparse.ArgumentParser(description='End-to-end LLM-first autonomous scientist loop: repo/data/env/reproduce/edit/experiment/paper.')
    ap.add_argument('--project', required=True)
    ap.add_argument('--venue', default='ICLR')
    ap.add_argument('--title', default='')
    ap.add_argument('--iterations', type=int, default=2)
    ap.add_argument('--max-launches', type=int, default=6)
    ap.add_argument('--real-bootstrap-env', action='store_true')
    args = ap.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        return guard_rc

    cfg = load_project_config(args.project)
    if not args.title:
        args.title = str(cfg.get('title') or cfg.get('topic') or args.project)
    paths = build_paths(args.project)
    setattr(paths, 'project', args.project)
    trace = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'project': args.project, 'venue': args.venue, 'steps': []}
    save_json(paths.state / 'autoscientist_trace.json', trace)

    rc = run_project_cmd(['scripts/run_project.py', '--project', args.project, '--topic', cfg.get('topic', args.project), '--max-results', '6', '--discover-retries', '1', '--venue', args.venue, '--skip-initialization', '--skip-semantic-scholar', '--skip-llm'], timeout=300)
    append_trace(paths, trace, 'discovery_initialization', return_code=rc, note='nonzero/timeout is an evidence gap, not a reason to fabricate content')
    try:
        repo = choose_repo(paths, cfg)
    except SystemExit:
        append_trace(paths, trace, 'repo_selection_blocked', note='no acceptable candidate after initial discovery; running post-veto restart search')
        run_project_cmd(['scripts/restart_after_veto.py', '--project', args.project, '--limit', '10'], timeout=900)
        repo = choose_repo(paths, cfg)
    repo_path = clone_or_update_repo(paths, repo)
    ensure_git(repo_path)
    trace['selected_repo'] = repo
    trace['repo_path'] = str(repo_path)
    save_json(paths.state / 'active_repo.json', {'name': repo.get('name', repo_path.name), 'url': repo.get('url', ''), 'repo_path': str(repo_path), 'selected_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'selection_bucket': repo.get('repo_selection_bucket', ''), 'repo_reuse_score': repo.get('repo_reuse_score', repo.get('score', 0))})
    rc = run_project_cmd(['scripts/audit_local_repo.py', '--project', args.project, '--repo-path', str(repo_path), '--name', repo.get('name', repo_path.name), '--url', repo.get('url', 'local'), '--summary', repo.get('summary', ''), '--task-fit', '--stars', str(repo.get('stars', 0) or 0), '--forks', str(repo.get('forks', 0) or 0), '--last-pushed-at', str(repo.get('last_pushed_at', '')), '--topics', ','.join(repo.get('topics', []) or [])], timeout=120)
    repo = reload_repo_candidate(paths, repo.get('name', repo_path.name), repo_path, repo)
    trace['selected_repo'] = repo
    save_json(paths.state / 'active_repo.json', {'name': repo.get('name', repo_path.name), 'url': repo.get('url', ''), 'repo_path': str(repo_path), 'selected_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'selection_bucket': repo.get('repo_selection_bucket', ''), 'repo_reuse_score': repo.get('repo_reuse_score', repo.get('score', 0)), 'repo_execution_ready': bool(repo.get('repo_execution_ready')), 'repo_support_signals': repo.get('repo_support_signals', [])})
    append_trace(paths, trace, 'repo_audit', return_code=rc)
    rc = run_project_cmd(['scripts/repo_first_backtrack.py', '--project', args.project, '--repo-path', str(repo_path)], timeout=120)
    append_trace(paths, trace, 'repo_first_backtracking', return_code=rc)
    rc = run_project_cmd(['scripts/build_repo_data_requirements.py', '--project', args.project, '--repo-path', str(repo_path)], timeout=120)
    append_trace(paths, trace, 'repo_data_requirements', return_code=rc)

    env_name = f"{slugify(args.project)}_{slugify(repo_path.name)}"
    bootstrap = ['scripts/bootstrap_repo_env.py', '--project', args.project, '--repo-path', str(repo_path), '--env-name', env_name, '--verify-only']
    bootstrap.append('--update-project-config' if args.real_bootstrap_env else '--prepare-only')
    rc = run_project_cmd(bootstrap, timeout=900)
    append_trace(paths, trace, 'environment_bootstrap', return_code=rc, mode='real' if args.real_bootstrap_env else 'prepare-only')

    rc = run_project_cmd(['scripts/probe_repo_dataset.py', '--project', args.project, '--repo-path', str(repo_path), '--env-name', env_name], timeout=180)
    append_trace(paths, trace, 'real_dataset_probe', return_code=rc)

    repo_context = read_repo_files(repo_path)
    plan = llm_research_plan(args.project, repo, repo_context, cfg)
    save_json(paths.state / 'autoscientist_plan.json', plan)
    (paths.planning / 'autoscientist_plan.md').write_text('# AutoScientist Plan\n\n```json\n' + json.dumps(plan, indent=2, ensure_ascii=False) + '\n```\n', encoding='utf-8')

    blocked_by_data, blocked_datasets = repo_data_blocked(paths)
    if blocked_by_data:
        append_trace(paths, trace, 'data_acquisition_block', blocked_datasets=blocked_datasets, note='active repo real datasets are missing; stop before synthetic fallback to avoid false scientific momentum')
        run_project_cmd(['scripts/plan_data_acquisition.py', '--project', args.project], timeout=120)
        run_project_cmd(['scripts/attempt_data_acquisition.py', '--project', args.project, '--repo-path', str(repo_path)], timeout=240)
        run_project_cmd(['scripts/build_repo_data_requirements.py', '--project', args.project, '--repo-path', str(repo_path)], timeout=120)
        run_project_cmd(['scripts/probe_repo_dataset.py', '--project', args.project, '--repo-path', str(repo_path), '--env-name', env_name], timeout=180)
        run_project_cmd(['scripts/data_unavailability_policy.py', '--project', args.project], timeout=120)
        policy = load_json(paths.state / 'data_unavailability_policy.json', {})
        trace['steps'][-1]['data_policy_decision'] = policy.get('decision', '')
        trace['steps'][-1]['data_policy_rationale'] = policy.get('rationale', '')
        if policy.get('decision') in {'expand_discovery_or_request_user_data_before_switching', 'ask_user_for_data_or_expand_discovery', 'switch_or_backtrack_to_evidence_ready_repo'}:
            restart_rc = run_project_cmd(['scripts/restart_after_data_blocker.py', '--project', args.project, '--limit', '8'], timeout=1200)
            trace['steps'][-1]['data_blocker_restart_return_code'] = restart_rc
            policy = load_json(paths.state / 'data_unavailability_policy.json', policy)
            trace['steps'][-1]['post_restart_data_policy_decision'] = policy.get('decision', '')
        save_json(paths.state / 'autoscientist_trace.json', trace)
        run_project_cmd(['scripts/propose_next_actions.py', '--project', args.project], timeout=120)
        run_project_cmd(['scripts/build_claim_ledger.py', '--project', args.project], timeout=120)
        run_project_cmd(['scripts/audit_paper_evidence.py', '--project', args.project, '--venue', args.venue], timeout=120)
        run_project_cmd(['scripts/research_manifest.py', '--project', args.project, '--venue', args.venue], timeout=120)
        run_project_cmd(['scripts/report_status.py', '--project', args.project, '--venue', args.venue], timeout=120)
        run_project_cmd(['scripts/generate_handoff.py', '--project', args.project], timeout=120)
        trace['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        trace['stopped_reason'] = 'active_repo_real_data_missing'
        save_json(paths.state / 'autoscientist_trace.json', trace)
        print(paths.state / 'autoscientist_trace.json')
        return 0

    real_dataset = choose_probe_passed_dataset(paths)
    if real_dataset:
        bootstrap_state = load_json(paths.state / 'repo_env_bootstrap.json', {})
        if bootstrap_state.get('status') != 'completed':
            install_rc = run_project_cmd(['scripts/bootstrap_repo_env.py', '--project', args.project, '--repo-path', str(repo_path), '--env-name', env_name, '--verify-only', '--update-project-config'], timeout=1800)
            append_trace(paths, trace, 'environment_bootstrap_before_real_experiment', return_code=install_rc, mode='real-required-for-real-data')
            bootstrap_state = load_json(paths.state / 'repo_env_bootstrap.json', {})
            if bootstrap_state.get('status') != 'completed':
                append_trace(paths, trace, 'environment_block', env_name=env_name, status=bootstrap_state.get('status', ''), note='real dataset is ready but conda environment did not complete; stop before experiments')
                run_project_cmd(['scripts/propose_next_actions.py', '--project', args.project], timeout=120)
                run_project_cmd(['scripts/report_status.py', '--project', args.project, '--venue', args.venue], timeout=120)
                trace['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
                trace['stopped_reason'] = 'active_repo_env_not_ready_for_real_experiment'
                save_json(paths.state / 'autoscientist_trace.json', trace)
                print(paths.state / 'autoscientist_trace.json')
                return 0
        methods = ['repo_real_reproduction_smoke', 'repo_real_metric_audit_probe', 'repo_real_hparam_sanity']
        dataset = real_dataset
        benchmark = f'{real_dataset}_repo_short_reproduction'
        metric = 'ndcg_at_10'
        command_template = repo_real_command_template(real_dataset)
        append_trace(paths, trace, 'experiment_mode', mode='real-dataset-first', dataset=real_dataset, note='synthetic fallback disabled because a real repo loader probe passed')
    else:
        write_harness(repo_path)
        register_synthetic_dataset(args.project)
        planned_methods = plan.get('methods') if isinstance(plan.get('methods'), list) else []
        methods = [slugify(str(m)) for m in planned_methods if str(m).strip()]
        if not methods:
            topic_slug = slugify(str(cfg.get('topic') or args.project))
            methods = [f'{topic_slug}_reference_smoke', f'{topic_slug}_candidate_smoke', f'{topic_slug}_stress_smoke']
        methods = methods[:3]
        dataset = str(plan.get('dataset', 'generated_smoke_benchmark'))
        benchmark = str(plan.get('benchmark', 'generated_pipeline_smoke'))
        metric = str(plan.get('metric', 'ndcg_at_10'))
        command_template = str(plan.get('experiment_command') or 'python3 autoscientist_experiment.py --variant {method_slug} --artifact-dir {artifact_dir} --seed {seed}')
        if '{method_slug}' not in command_template:
            command_template = 'python3 autoscientist_experiment.py --variant {method_slug} --artifact-dir {artifact_dir} --seed {seed}'
        append_trace(paths, trace, 'experiment_mode', mode='synthetic-fallback', note='no real repo loader probe passed; synthetic evidence cannot support paper claims')

    parts = shlex.split(command_template) if command_template and not command_needs_shell(command_template) else []
    if parts and parts[0] == 'python':
        parts[0] = cfg.get('python_executable', 'python3') or 'python3'
        command_template = shlex.join(parts)
    rc = run_project_cmd(['scripts/plan_experiments.py', '--project', args.project, '--methods', *methods, '--dataset', dataset, '--benchmark', benchmark, '--metric', metric, '--repo-name', repo.get('name', ''), '--repo-path', str(repo_path), '--env-name', env_name, '--command-template', command_template, '--max-trials-per-method', '2'], timeout=120)
    append_trace(paths, trace, 'plan_parallel_experiments', return_code=rc, methods=methods, dataset=dataset, benchmark=benchmark)

    for iteration in range(1, args.iterations + 1):
        append_trace(paths, trace, 'execute_parallel_plan', iteration=iteration)
        rc = run_project_cmd(['scripts/run_project.py', '--project', args.project, '--topic', cfg.get('topic', args.project), '--skip-discovery', '--skip-initialization', '--skip-llm', '--execute-plan', '--max-launches', str(args.max_launches), '--coding-backend', 'llm', '--venue', args.venue], timeout=900)
        trace['steps'][-1]['return_code'] = rc
        trace['steps'][-1]['note'] = 'execution must create experiment_registry/audit artifacts; absence keeps paper claims blocked'
        save_json(paths.state / 'autoscientist_trace.json', trace)
        run_project_cmd(['scripts/propose_next_actions.py', '--project', args.project], timeout=120)
        run_project_cmd(['scripts/reflect_iteration.py', '--project', args.project], timeout=120)
        run_project_cmd(['scripts/update_evolution_memory.py', '--project', args.project, '--venue', args.venue], timeout=120)

    run_project_cmd(['scripts/run_paper_pipeline.py', '--project', args.project, '--venue', args.venue, '--title', args.title, '--generate-inspection-paper', '--auto-install-latex'], timeout=600)
    run_project_cmd(['scripts/report_status.py', '--project', args.project, '--venue', args.venue], timeout=120)
    run_project_cmd(['scripts/generate_handoff.py', '--project', args.project], timeout=120)
    trace['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(paths.state / 'autoscientist_trace.json', trace)
    print(paths.state / 'autoscientist_trace.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
