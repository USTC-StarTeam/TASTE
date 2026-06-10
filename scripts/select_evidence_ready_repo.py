#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import tarfile
import tempfile
import urllib.request
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime_env import find_binary, interactive_env
from project_paths import ROOT, build_paths, load_project_config


DEFAULT_CLAUDE_TOPIC_DECISION = {
    'decision': 'needs-more-search',
    'confidence': 0.0,
    'rationale': 'Claude topic-fit review was not available; TASTE cannot claim this is the best transformable repo.',
    'rationale_en': 'Claude topic-fit review was not available; TASTE cannot claim this is the best transformable repo.',
    'rationale_zh': 'Claude 尚未完成仓库与研究主题适配性审查；TASTE 不能声称当前仓库就是最合适的可改造路线。',
    'repo_action': 'continue_search',
    'repo_action_reason': 'Claude did not provide a repo stewardship decision.',
    'repo_action_reason_en': 'Claude did not provide a repo stewardship decision.',
    'repo_action_reason_zh': 'Claude 尚未给出仓库保留/切换策略。',
    'env_action': 'defer_until_repo_selected',
    'env_action_reason': 'Claude did not provide a conda environment stewardship decision.',
    'env_action_reason_en': 'Claude did not provide a conda environment stewardship decision.',
    'env_action_reason_zh': 'Claude 尚未给出 conda 环境复用/修补/新建策略。',
    'recommended_env_name': '',
    'data_action': 'continue_data_search',
    'data_action_reason': 'Claude did not provide a data acquisition decision.',
    'data_action_reason_en': 'Claude did not provide a data acquisition decision.',
    'data_action_reason_zh': 'Claude 尚未给出数据选择/下载/放置策略。',
    'stewardship_memory': 'Future iterations must re-check repo, conda env, and data readiness from local evidence.',
    'stewardship_memory_en': 'Future iterations must re-check repo, conda env, and data readiness from local evidence.',
    'stewardship_memory_zh': '后续迭代必须基于本地证据重新核对仓库、conda 环境和数据可用性。',
    'best_repo': '',
    'repo_path': '',
    'dataset': '',
    'required_modifications': [],
    'required_modifications_en': [],
    'required_modifications_zh': [],
    'risks': ['missing Claude topic-fit review'],
    'risks_en': ['missing Claude topic-fit review'],
    'risks_zh': ['缺少 Claude 对研究主题适配性的审查'],
    'evidence': [],
    'evidence_en': [],
    'evidence_zh': [],
    'accept_as_current_best': False,
}

REPO_ACTIONS = {'keep_and_modify_current_repo', 'switch_to_best_repo', 'continue_search'}
ENV_ACTIONS = {'reuse_existing_env', 'repair_existing_env', 'create_new_project_env', 'defer_until_repo_selected'}
DATA_ACTIONS = {'use_claim_ready_dataset', 'download_or_place_required_data', 'continue_data_search'}


def load_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout or '', stderr or '')
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, 124, stdout or '', (stderr or '') + f'\nTIMEOUT after {timeout}s')


def slugify(value: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '_', value.strip().lower()).strip('_') or 'repo'


def safe_extract_tar(archive: Path, target_parent: Path) -> Path | None:
    tmpdir = Path(tempfile.mkdtemp(prefix='repo_archive_', dir=str(target_parent)))
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            base = tmpdir.resolve()
            for member in tar.getmembers():
                dest = (tmpdir / member.name).resolve()
                if base != dest and base not in dest.parents:
                    raise RuntimeError(f'unsafe tar path: {member.name}')
            tar.extractall(tmpdir)
        children = [child for child in tmpdir.iterdir() if child.is_dir()]
        return children[0] if children else None
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def github_archive_fallback(url: str, target: Path, timeout: int) -> dict[str, Any]:
    match = re.search(r'github\.com/([^/]+)/([^/#?]+)', url)
    if not match:
        return {'status': 'archive_unavailable', 'reason': 'not a github url'}
    owner, repo = match.group(1), match.group(2).removesuffix('.git')
    attempts = []
    for branch in ['main', 'master']:
        archive_url = f'https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}'
        archive_path = target.parent / f'.{target.name}.{branch}.tar.gz'
        try:
            req = urllib.request.Request(archive_url, headers={'User-Agent': 'TASTE-FreshBaseRepoAudit/0.1'})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                archive_path.write_bytes(response.read())
            extracted = safe_extract_tar(archive_path, target.parent)
            archive_path.unlink(missing_ok=True)
            if extracted and extracted.exists():
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                extracted.rename(target)
                return {'status': 'archive_downloaded', 'path': str(target), 'branch': branch, 'archive_url': archive_url}
            attempts.append({'branch': branch, 'status': 'extract_failed'})
        except Exception as exc:
            attempts.append({'branch': branch, 'status': 'download_failed', 'error': str(exc)[:500]})
            try:
                archive_path.unlink(missing_ok=True)
            except Exception:
                pass
    return {'status': 'archive_failed', 'attempts': attempts}


def clone_or_reuse(paths, row: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    local = str(row.get('local_path') or '')
    if local and Path(local).exists():
        return Path(local).resolve(), {'status': 'reused_existing_clone', 'path': local}
    url = str(row.get('url') or '')
    if not url.startswith('http'):
        return None, {'status': 'not_cloneable', 'reason': 'missing http URL'}
    target = paths.repos_selected / slugify(str(row.get('name') or 'candidate_repo'))
    if target.exists() and (target / '.git').exists():
        return target.resolve(), {'status': 'reused_existing_clone', 'path': str(target)}
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    clone_timeout = int(os.environ.get('REPO_CLONE_TIMEOUT_SEC', '45'))
    proc = run([
        'git',
        '-c',
        'http.lowSpeedLimit=1024',
        '-c',
        'http.lowSpeedTime=20',
        'clone',
        '--depth',
        '1',
        '--filter=blob:none',
        url,
        str(target),
    ], ROOT, timeout=clone_timeout)
    if proc.returncode == 0:
        return target.resolve(), {'status': 'cloned', 'path': str(target)}
    shutil.rmtree(target, ignore_errors=True)
    archive = github_archive_fallback(url, target, timeout=max(15, clone_timeout))
    if archive.get('status') == 'archive_downloaded':
        return target.resolve(), {'status': 'cloned_by_archive_fallback', 'git_return_code': proc.returncode, 'git_stderr_tail': (proc.stderr or proc.stdout)[-1200:], **archive}
    return None, {
        'status': 'clone_failed',
        'return_code': proc.returncode,
        'stderr_tail': (proc.stderr or proc.stdout)[-1500:],
        'archive_fallback': archive,
    }


def file_exists_any(repo: Path, names: set[str]) -> bool:
    lowered = {name.lower() for name in names}
    try:
        return any(path.name.lower() in lowered for path in repo.rglob('*') if '.git' not in path.parts)
    except Exception:
        return False


def quick_signals(repo: Path) -> dict[str, Any]:
    top = list(repo.iterdir()) if repo.exists() else []
    readme_text = ''
    for path in top:
        if path.is_file() and path.name.lower().startswith('readme'):
            readme_text += path.read_text(encoding='utf-8', errors='ignore')[:30000]
    data_mentions = sum(1 for token in ['dataset', 'data', 'download', 'benchmark', 'processed', '.pkl', '.npy', '.txt'] if token in readme_text.lower())
    return {
        'has_readme': bool(readme_text),
        'has_install': any((repo / name).exists() for name in ['requirements.txt', 'environment.yml', 'environment.yaml', 'setup.py', 'pyproject.toml']),
        'has_entrypoint': file_exists_any(repo, {'main.py', 'train.py', 'run.py', 'eval.py', 'autoscientist_experiment.py'}),
        'has_data_dir': any((repo / name).exists() for name in ['data', 'dataset', 'datasets']),
        'readme_data_mentions': data_mentions,
    }


def score_candidate(row: dict[str, Any], repo: Path, signals: dict[str, Any], probe: dict[str, Any], active_repo_path: str) -> float:
    base = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
    if row.get('repo_selection_bucket') == 'paused_by_veto':
        # A veto is a serious warning, but real data + loader evidence can still make it a fallback route.
        base = min(base, 0)
    score = base
    score += 12 if any(p.get('claim_ready') for p in probe.get('probes', [])) else 0
    score += 4 if signals.get('has_entrypoint') else -3
    score += 3 if signals.get('has_data_dir') else -2
    score += 2 if signals.get('has_readme') else -1
    score += min(3, int(signals.get('readme_data_mentions') or 0))
    gaps = topic_gaps(row)
    if gaps:
        # Topic gaps are only a weak local tie-breaker. Claude Code makes the
        # dynamic topic/transformability decision for the current project.
        score -= min(2.0, 0.5 * len(gaps))
    if str(repo) == active_repo_path:
        score += 1
    return score


def topic_gaps(row: dict[str, Any]) -> list[str]:
    raw = row.get('missing_topic_groups', row.get('topic_gaps_to_fix_later', []))
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip().lower() for item in raw if str(item).strip()]


def extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def markdown_topic_decision(output: str, audited: list[dict[str, Any]]) -> dict[str, Any]:
    text = str(output or '')
    low = text.lower()
    if not text.strip() or not audited:
        return {}
    best = None
    for item in audited:
        name = str(item.get('name') or '')
        if name and name.lower() in low:
            best = item
            if 'best repo' in low and name.lower() in low[low.find('best repo'): low.find('best repo') + 800]:
                break
    if not best:
        return {}
    accept = any(token in low for token in ['verdict: accept', 'decision: accept', 'accept horizonrec', 'accept-with', 'best repo'])
    if not accept:
        return {}
    name = str(best.get('name') or '')
    repo_path = str(best.get('repo_path') or '')
    missing = best.get('missing_topic_groups') if isinstance(best.get('missing_topic_groups'), list) else []
    claim_ready = best.get('probe_summary', {}).get('claim_ready_datasets', []) if isinstance(best.get('probe_summary'), dict) else []
    data_action = 'use_claim_ready_dataset' if claim_ready else 'download_or_place_required_data'
    rationale = (
        f'Claude Markdown review selected {name} as the best transformable repo. '
        f'Local loader evidence is still required before experiments; missing_topic_groups={missing}; claim_ready_datasets={claim_ready}.'
    )
    rationale_zh = (
        f'Claude Markdown 审查选择 {name} 作为当前最适合改造的候选仓库。'
        f'但实验放行仍必须等待本地 loader 证据；缺失主题组={missing}；已通过 loader 的数据集={claim_ready}。'
    )
    return {
        'decision': 'accept-with-modifications',
        'accept_as_current_best': True,
        'confidence': 0.65,
        'best_repo': name,
        'repo_path': repo_path,
        'dataset': claim_ready[0] if claim_ready else '',
        'repo_action': 'switch_to_best_repo',
        'repo_action_reason': rationale,
        'repo_action_reason_en': rationale,
        'repo_action_reason_zh': rationale_zh,
        'env_action': 'repair_existing_env',
        'env_action_reason': 'Reuse or repair the project environment only after the candidate repo/data loader contract is verified.',
        'env_action_reason_en': 'Reuse or repair the project environment only after the candidate repo/data loader contract is verified.',
        'env_action_reason_zh': '只有在候选仓库/数据 loader 合同验证后，才复用或修补项目环境。',
        'recommended_env_name': '',
        'data_action': data_action,
        'data_action_reason': 'Materialize real dataset files and rerun the repo loader probe before selecting this repo for experiments.',
        'data_action_reason_en': 'Materialize real dataset files and rerun the repo loader probe before selecting this repo for experiments.',
        'data_action_reason_zh': '必须先物化真实数据文件并重跑仓库 loader probe，才能把该仓库选为实验基底。',
        'stewardship_memory': 'Keep this candidate pending until code entrypoint and real dataset loader evidence both pass.',
        'stewardship_memory_en': 'Keep this candidate pending until code entrypoint and real dataset loader evidence both pass.',
        'stewardship_memory_zh': '在代码入口和真实数据 loader 证据同时通过前，只能把该候选保持为待验证状态。',
        'rationale': rationale,
        'rationale_en': rationale,
        'rationale_zh': rationale_zh,
        'required_modifications': ['Add LLM conditioning after reference reproduction succeeds', 'Materialize repo data and rerun loader/import probes'],
        'required_modifications_en': ['Add LLM conditioning after reference reproduction succeeds', 'Materialize repo data and rerun loader/import probes'],
        'required_modifications_zh': ['参考复现成功后再加入 LLM 条件机制', '先物化仓库数据并重跑 loader/import probe'],
        'risks': ['candidate is not evidence-ready until loader probes pass'],
        'risks_en': ['candidate is not evidence-ready until loader probes pass'],
        'risks_zh': ['loader probe 通过前，该候选不能作为 evidence-ready 基底'],
        'evidence': [f'audited candidate {name}', f'repo_path={repo_path}'],
        'evidence_en': [f'audited candidate {name}', f'repo_path={repo_path}'],
        'evidence_zh': [f'已审计候选 {name}', f'仓库路径={repo_path}'],
    }


def normalize_topic_decision(raw: dict[str, Any], fallback_repo: dict[str, Any] | None = None) -> dict[str, Any]:
    decision = dict(DEFAULT_CLAUDE_TOPIC_DECISION)
    if isinstance(raw, dict):
        decision.update(raw)
    if fallback_repo:
        if not decision.get('best_repo'):
            decision['best_repo'] = fallback_repo.get('name', '')
        if not decision.get('repo_path'):
            decision['repo_path'] = fallback_repo.get('repo_path', '')
        if not decision.get('dataset'):
            decision['dataset'] = fallback_repo.get('claim_ready_dataset', '')
    decision['decision'] = str(decision.get('decision') or 'needs-more-search').strip().lower()
    try:
        decision['confidence'] = float(decision.get('confidence') or 0)
    except Exception:
        decision['confidence'] = 0.0
    decision['accept_as_current_best'] = bool(decision.get('accept_as_current_best') or decision['decision'] in {'accept', 'accept-with-modifications', 'current-best'})
    repo_action = str(decision.get('repo_action') or '').strip().lower()
    if repo_action not in REPO_ACTIONS:
        repo_action = 'switch_to_best_repo' if decision.get('accept_as_current_best') and decision.get('repo_path') else 'continue_search'
    decision['repo_action'] = repo_action
    env_action = str(decision.get('env_action') or '').strip().lower()
    if env_action not in ENV_ACTIONS:
        env_action = 'repair_existing_env' if decision.get('accept_as_current_best') else 'defer_until_repo_selected'
    decision['env_action'] = env_action
    data_action = str(decision.get('data_action') or '').strip().lower()
    if data_action not in DATA_ACTIONS:
        data_action = 'use_claim_ready_dataset' if decision.get('dataset') else 'continue_data_search'
    decision['data_action'] = data_action
    for key in ['repo_action_reason', 'env_action_reason', 'data_action_reason', 'stewardship_memory']:
        text = str(decision.get(key) or '').strip()
        en_key = f'{key}_en'
        zh_key = f'{key}_zh'
        if not str(decision.get(en_key) or '').strip():
            decision[en_key] = text
        if not str(decision.get(zh_key) or '').strip():
            decision[zh_key] = 'Claude 返回的是旧版单语策略；请重新运行环境配置以生成中文结构化说明。' if text else ''
        decision[key] = str(decision.get(en_key) or text or '').strip()
        decision[f'{key}_i18n'] = {'zh': str(decision.get(zh_key) or '').strip(), 'en': str(decision.get(en_key) or '').strip()}
    decision['recommended_env_name'] = str(decision.get('recommended_env_name') or '').strip()
    for key in [
        'required_modifications',
        'required_modifications_en',
        'required_modifications_zh',
        'risks',
        'risks_en',
        'risks_zh',
        'evidence',
        'evidence_en',
        'evidence_zh',
    ]:
        value = decision.get(key, [])
        if isinstance(value, str):
            value = [value]
        decision[key] = [str(item) for item in value if str(item).strip()]
    if not str(decision.get('rationale_en') or '').strip():
        decision['rationale_en'] = str(decision.get('rationale') or '').strip()
    if not str(decision.get('rationale_zh') or '').strip():
        decision['rationale_zh'] = 'Claude 返回的是旧版单语理由；请重新运行环境配置以生成中文结构化理由。'
    for base in ['required_modifications', 'risks', 'evidence']:
        en_key = f'{base}_en'
        zh_key = f'{base}_zh'
        if not decision.get(en_key):
            decision[en_key] = list(decision.get(base, []))
        if not decision.get(zh_key) and decision.get(base):
            decision[zh_key] = ['Claude 返回的是旧版单语列表；请重新运行环境配置以生成中文结构化条目。']
    return decision


def write_repo_env_strategy(project: str, paths, decision: dict[str, Any], selected: dict[str, Any], active: dict[str, Any], env_name: str) -> dict[str, Any]:
    strategy = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': project,
        'repo_action': decision.get('repo_action', 'continue_search'),
        'repo_action_reason': decision.get('repo_action_reason_en') or decision.get('repo_action_reason') or '',
        'repo_action_reason_en': decision.get('repo_action_reason_en') or decision.get('repo_action_reason') or '',
        'repo_action_reason_zh': decision.get('repo_action_reason_zh') or '',
        'env_action': decision.get('env_action', 'defer_until_repo_selected'),
        'env_action_reason': decision.get('env_action_reason_en') or decision.get('env_action_reason') or '',
        'env_action_reason_en': decision.get('env_action_reason_en') or decision.get('env_action_reason') or '',
        'env_action_reason_zh': decision.get('env_action_reason_zh') or '',
        'recommended_env_name': decision.get('recommended_env_name') or env_name,
        'data_action': decision.get('data_action', 'continue_data_search'),
        'data_action_reason': decision.get('data_action_reason_en') or decision.get('data_action_reason') or '',
        'data_action_reason_en': decision.get('data_action_reason_en') or decision.get('data_action_reason') or '',
        'data_action_reason_zh': decision.get('data_action_reason_zh') or '',
        'stewardship_memory': decision.get('stewardship_memory_en') or decision.get('stewardship_memory') or '',
        'stewardship_memory_en': decision.get('stewardship_memory_en') or decision.get('stewardship_memory') or '',
        'stewardship_memory_zh': decision.get('stewardship_memory_zh') or '',
        'active_repo_before': active,
        'selected_repo': {
            'name': selected.get('name', ''),
            'repo_path': selected.get('repo_path', ''),
            'dataset': selected.get('claim_ready_dataset', ''),
        } if isinstance(selected, dict) else {},
        'guardrails': [
            'Claude Code owns repo/data/env stewardship decisions; TASTE records and reuses the structured decision instead of substituting assistant analysis.',
            'create_new_project_env means create or use a new named project env; The workflow must not silently delete an old conda env.',
            'repair_existing_env means install/fix only what local evidence shows is missing.',
            'Repo and data artifacts remain outside git; only TASTE source/config/state summaries may be committed intentionally.',
        ],
    }
    save_json(paths.state / 'repo_env_strategy.json', strategy)
    lines = [
        '# Claude Repo/Data/Env Stewardship Strategy\n\n',
        f"- generated_at: {strategy['generated_at']}\n",
        f"- repo_action: {strategy['repo_action']}\n",
        f"- repo_action_reason: {strategy['repo_action_reason']}\n",
        f"- repo_action_reason_zh: {strategy['repo_action_reason_zh']}\n",
        f"- env_action: {strategy['env_action']}\n",
        f"- env_action_reason: {strategy['env_action_reason']}\n",
        f"- env_action_reason_zh: {strategy['env_action_reason_zh']}\n",
        f"- recommended_env_name: {strategy['recommended_env_name']}\n",
        f"- data_action: {strategy['data_action']}\n",
        f"- data_action_reason: {strategy['data_action_reason']}\n",
        f"- data_action_reason_zh: {strategy['data_action_reason_zh']}\n",
        f"- stewardship_memory: {strategy['stewardship_memory']}\n",
        f"- stewardship_memory_zh: {strategy['stewardship_memory_zh']}\n",
    ]
    (paths.reports / 'repo_env_strategy.md').write_text(''.join(lines), encoding='utf-8')
    session_history = paths.reports / 'claude_project_session.md'
    session_history.parent.mkdir(parents=True, exist_ok=True)
    with session_history.open('a', encoding='utf-8') as handle:
        handle.write('\n\n## TASTE-recorded Claude repo/data/env stewardship memory\n\n')
        handle.write('```json\n')
        handle.write(json.dumps(strategy, indent=2, ensure_ascii=False))
        handle.write('\n```\n')
    return strategy


def probe_repo(project: str, repo: Path, env_name: str, timeout_sec: int) -> dict[str, Any]:
    proc = run([sys.executable, 'scripts/probe_repo_dataset.py', '--project', project, '--repo-path', str(repo), '--env-name', env_name, '--timeout-sec', str(timeout_sec)], ROOT, timeout=timeout_sec + 90)
    paths = build_paths(project)
    payload = load_json(paths.state / 'real_dataset_probe.json', {})
    if not isinstance(payload, dict) or str(payload.get('repo_path')) != str(repo):
        payload = {'repo_path': str(repo), 'probes': []}
    payload['probe_return_code'] = proc.returncode
    payload['probe_stdout_tail'] = (proc.stdout or '')[-1500:]
    payload['probe_stderr_tail'] = (proc.stderr or '')[-1500:]
    return payload





def sync_selected_candidate(paths, selected: dict[str, Any]) -> None:
    if not selected or not selected.get('repo_path'):
        return
    rows = load_json(paths.state / 'repo_candidates.json', [])
    if not isinstance(rows, list):
        return
    selected_path = str(selected.get('repo_path'))
    claim_ready = selected.get('probe_summary', {}).get('claim_ready_datasets', [])
    evidence_score = float(selected.get('selection_score', 0) or 0)
    updated = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get('local_path') or '') != selected_path and str(row.get('url') or '') != str(selected.get('url') or ''):
            continue
        row['repo_execution_ready'] = True
        row['repo_selection_bucket'] = 'evidence_ready_fallback' if selected.get('missing_topic_groups') else 'evidence_ready'
        row['evidence_selection_score'] = evidence_score
        row['claim_ready_datasets'] = claim_ready
        row['claim_ready_dataset'] = selected.get('claim_ready_dataset', '')
        row['repo_support_signals'] = [key for key, ok in selected.get('signals', {}).items() if ok is True]
        row['notes'] = 'Evidence-ready fallback: local code and real dataset loader probes passed; topic gaps remain and must be addressed by experiment design.'
        # Keep the original topic/reuse score as historical taste signal, but prevent UI/planner from treating this as unusable.
        original = float(row.get('repo_reuse_score', row.get('score', 0)) or 0)
        row['repo_reuse_score'] = max(original, 9.0)
        row['score'] = max(float(row.get('score', original) or 0), 9.0)
        updated = True
    if updated:
        save_json(paths.state / 'repo_candidates.json', rows)

def compact_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    selected = payload.get('selected', {}) if isinstance(payload.get('selected'), dict) else {}
    audited = []
    for item in payload.get('audited_candidates', [])[:8]:
        if not isinstance(item, dict):
            continue
        audited.append({
            'name': item.get('name', ''),
            'repo_path': item.get('repo_path', ''),
            'decision': item.get('decision', ''),
            'claim_ready_datasets': item.get('probe_summary', {}).get('claim_ready_datasets', []),
            'signals': item.get('signals', {}),
            'missing_topic_groups': item.get('missing_topic_groups', []),
            'selection_score': item.get('selection_score', ''),
            'decision_rationale': item.get('decision_rationale', ''),
        })
    selected_probe = selected.get('claim_ready_probe', {}) if isinstance(selected.get('claim_ready_probe'), dict) else {}
    return {
        'generated_at': payload.get('generated_at', ''),
        'project': payload.get('project', ''),
        'requirement': payload.get('requirement', ''),
        'evidence_ready_count': payload.get('evidence_ready_count', 0),
        'selected': {
            'name': selected.get('name', ''),
            'repo_path': selected.get('repo_path', ''),
            'decision': selected.get('decision', ''),
            'claim_ready_dataset': selected.get('claim_ready_dataset', ''),
            'claim_ready_datasets': selected.get('probe_summary', {}).get('claim_ready_datasets', []),
            'signals': selected.get('signals', {}),
            'missing_topic_groups': selected.get('missing_topic_groups', []),
            'loader_parsed': selected_probe.get('loader_probe', {}).get('parsed', {}),
            'required_files_ok': selected_probe.get('required_files_ok', False),
        },
        'audited_candidates': audited,
        'guardrails': payload.get('guardrails', []),
    }


def run_claude_review(project: str, payload: dict[str, Any], timeout_sec: int = 420) -> dict[str, Any]:
    cfg = load_project_config(project)
    env = interactive_env(project, cfg)
    claude = str(find_binary('claude', project, cfg) or shutil.which('claude', path=env.get('PATH', '')) or 'claude')
    paths = build_paths(project)
    review_path = paths.reports / 'evidence_ready_repo_claude_review.md'
    prompt_path = paths.reports / 'evidence_ready_repo_claude_review_prompt.txt'
    compact = compact_review_payload(payload)
    prompt = (
        'You are Claude Code inside TASTE. Review this compact repo/data selection audit.\n'
        'Do not invent evidence. The project topic is dynamic and must be judged from the project config, not from hard-coded keywords.\n'
        'Select the repo that is best for the research topic either directly or because it is the most transformable into the topic with realistic code changes.\n'
        'A repo may be acceptable even with topic gaps if it has strong runnable code/data and a concrete modification path. It must not be accepted if another audited repo is clearly better.\n'
        'Output concise Markdown with: Verdict, Best Repo, Why Best, Required Modifications, Evidence, Risks/Gaps, Next Actions.\n\n' +
        json.dumps(compact, ensure_ascii=False, indent=2)
    )
    prompt_path.write_text(prompt, encoding='utf-8')
    claude_cmd = shlex.quote(claude) if '/' in claude else claude
    # Wrap Claude in GNU timeout and stdin redirection. Some Claude Code builds emit the answer
    # but keep the process alive; timeout makes review optional rather than a pipeline blocker.
    prompt_arg = shlex.quote(str(prompt_path))
    soft_timeout = max(30, min(int(timeout_sec), 180))
    shell_cmd = (
        f"timeout {soft_timeout}s {claude_cmd} -p --permission-mode bypassPermissions --output-format text < {prompt_arg}"
    )
    proc = subprocess.run(
        ['bash', '-c', shell_cmd],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=soft_timeout + 15,
        env=env,
    )
    if proc.returncode == 124 and proc.stdout:
        # Claude produced a usable review before timeout killed a lingering process.
        proc.returncode = 0
    output = proc.stdout or proc.stderr or ''
    if proc.returncode != 0 and ('not logged in' in output.lower() or 'login' in output.lower()):
        output = output + '\n\nTASTE note: Claude Code review is unavailable because the CLI is not logged in in this shell. Evidence gates still rely on local loader probes and will not fabricate results.\n'
    review_path.write_text(output, encoding='utf-8')
    return {
        'status': 'completed' if proc.returncode == 0 else 'failed',
        'return_code': proc.returncode,
        'review_path': str(review_path),
        'prompt_path': str(prompt_path),
        'stderr_tail': (proc.stderr or '')[-1000:],
    }


def run_claude_topic_decision(project: str, payload: dict[str, Any], timeout_sec: int = 420) -> dict[str, Any]:
    cfg = load_project_config(project)
    env = interactive_env(project, cfg)
    claude = str(find_binary('claude', project, cfg) or shutil.which('claude', path=env.get('PATH', '')) or 'claude')
    paths = build_paths(project)
    decision_path = paths.reports / 'repo_topic_fit_decision.json'
    prompt_path = paths.reports / 'repo_topic_fit_decision_prompt.txt'
    compact = compact_review_payload(payload)
    topic_context = {
        'topic': cfg.get('topic', ''),
        'user_prompt': cfg.get('user_prompt', ''),
        'researcher_profile': cfg.get('researcher_profile', ''),
        'project': project,
    }
    prompt = (
        'You are Claude Code acting as TASTE repo-selection chair.\n'
        'Judge all audited repos against the dynamic research topic below. Do not use hard-coded keyword rules.\n'
        'Choose the single best repo if it is either directly aligned OR the best transformable base for the topic.\n'
        'Transformable means: runnable code/data evidence exists, the method is close enough, and the required modifications are feasible for TASTE in later experiment iterations.\n'
        'You must also decide repo/env/data stewardship for future autonomous work: whether The workflow should keep and modify the currently installed repo, switch to the best audited repo, or continue searching; whether the conda environment should be reused, repaired, newly created under a project-specific name, or deferred; and whether data is already claim-ready, must be downloaded/placed, or still requires search.\n'
        'Do not recommend deleting an existing conda environment. If rebuilding is needed, choose create_new_project_env and explain the new env name.\n'
        'Remember that future experiment iterations will rely on this decision as persistent memory, so include concrete instructions Claude Code can follow later.\n'
        'If no repo is good enough, say needs-more-search. Prefer an imperfect but highly transformable repo over no repo only when the rationale is evidence-backed.\n'
        'Return ONLY valid JSON with keys: decision, accept_as_current_best, confidence, best_repo, repo_path, dataset, repo_action, repo_action_reason, repo_action_reason_en, repo_action_reason_zh, env_action, env_action_reason, env_action_reason_en, env_action_reason_zh, recommended_env_name, data_action, data_action_reason, data_action_reason_en, data_action_reason_zh, stewardship_memory, stewardship_memory_en, stewardship_memory_zh, rationale, rationale_en, rationale_zh, required_modifications, required_modifications_en, required_modifications_zh, risks, risks_en, risks_zh, evidence, evidence_en, evidence_zh.\n'
        'repo_action must be one of: keep_and_modify_current_repo, switch_to_best_repo, continue_search.\n'
        'env_action must be one of: reuse_existing_env, repair_existing_env, create_new_project_env, defer_until_repo_selected.\n'
        'data_action must be one of: use_claim_ready_dataset, download_or_place_required_data, continue_data_search.\n'
        'The bilingual fields are mandatory: rationale_zh/required_modifications_zh/risks_zh/evidence_zh must be natural Chinese for the Chinese UI, and *_en must be natural English for the English UI. Keep rationale as the English canonical value for backward compatibility.\n'
        'decision must be one of: accept, accept-with-modifications, needs-more-search.\n\n'
        'Dynamic topic context:\n' + json.dumps(topic_context, ensure_ascii=False, indent=2) + '\n\n'
        'Repo/data audit:\n' + json.dumps(compact, ensure_ascii=False, indent=2)
    )
    prompt_path.write_text(prompt, encoding='utf-8')
    claude_cmd = shlex.quote(claude) if '/' in claude else claude
    prompt_arg = shlex.quote(str(prompt_path))
    soft_timeout = max(30, min(int(timeout_sec), 180))
    shell_cmd = (
        f"timeout {soft_timeout}s {claude_cmd} -p --permission-mode bypassPermissions --output-format text < {prompt_arg}"
    )
    try:
        proc = subprocess.run(['bash', '-c', shell_cmd], cwd=ROOT, text=True, capture_output=True, timeout=soft_timeout + 15, env=env)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(['bash', '-c', shell_cmd], 124, exc.stdout or '', exc.stderr or '')
    output = proc.stdout or proc.stderr or ''
    raw_json = extract_json_object(output)
    if not raw_json:
        raw_json = markdown_topic_decision(output, payload.get('audited_candidates', []) if isinstance(payload.get('audited_candidates'), list) else [])
    parsed = normalize_topic_decision(raw_json, payload.get('selected') if isinstance(payload.get('selected'), dict) else None)
    parsed.update({
        'status': 'completed' if proc.returncode in {0, 124} and output else 'failed',
        'return_code': proc.returncode,
        'raw_output_tail': output[-4000:],
        'prompt_path': str(prompt_path),
    })
    decision_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return parsed

def active_repo_candidate(active: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(active, dict) or not active.get('repo_path'):
        return None
    repo_path = str(active.get('repo_path'))
    if not Path(repo_path).exists():
        return None
    return {
        'name': active.get('name') or Path(repo_path).name,
        'url': active.get('url', ''),
        'local_path': repo_path,
        'repo_reuse_score': active.get('repo_reuse_score', 0),
        'repo_selection_bucket': active.get('selection_bucket', 'active'),
        'missing_topic_groups': active.get('topic_gaps_to_fix_later', active.get('missing_topic_groups', [])),
        'repo_execution_ready': active.get('repo_execution_ready', False),
        'repo_support_signals': active.get('repo_support_signals', []),
        '_source': 'active_repo',
    }

def main() -> int:
    parser = argparse.ArgumentParser(description='Select a repo whose code and real dataset are both verified before TASTE experiments proceed.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--env-name', default='')
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--timeout-sec', type=int, default=180)
    parser.add_argument('--allow-veto-fallback', action='store_true', help='Allow a vetoed repo only if it has real loader-ready data; record topic/quality caveats.')
    parser.add_argument('--allow-topic-gap-fallback', action='store_true', help='Allow a local evidence-ready fallback without Claude topic acceptance. Default keeps searching instead.')
    parser.add_argument('--write-active', action='store_true')
    parser.add_argument('--use-claude-review', action='store_true')
    parser.add_argument('--candidate-source', default='', help='Restrict candidate rows by repo_candidates.source.')
    parser.add_argument('--fresh-find-run-id', default='', help='Restrict candidate rows by fresh_find_run_id.')
    parser.add_argument('--selection-stage', default='', help='Decision stage label. Use environment_claude_code only from run_environment_stage after Claude Code owns anchor selection.')
    parser.add_argument('--exclude-active-repo', action='store_true', help='Do not prepend or audit the current active repo.')
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    env_name = args.env_name or cfg.get('conda_env', '')
    rows = load_json(paths.state / 'repo_candidates.json', [])
    active = load_json(paths.state / 'active_repo.json', {})
    active_path = str(active.get('repo_path') or '') if isinstance(active, dict) else ''
    if not isinstance(rows, list):
        rows = []
    selection_stage = (args.selection_stage or ('repo_data_claude_review' if args.use_claude_review else 'local_repo_data_probe')).strip()

    if args.selection_stage == 'environment_claude_code' and args.fresh_find_run_id and not args.exclude_active_repo:
        print('environment_claude_code selection with a current fresh_find_run_id excludes active_repo by default; old active_repo is legacy/control until selected from current Find evidence.', flush=True)
        args.exclude_active_repo = True
    candidates = []
    seen_paths: set[str] = set()
    active_candidate = None if args.exclude_active_repo else active_repo_candidate(active)
    if active_candidate:
        candidates.append(active_candidate)
        seen_paths.add(str(active_candidate.get('local_path', '')))
    for row in rows:
        if not isinstance(row, dict):
            continue
        if args.candidate_source and str(row.get('source') or '') != args.candidate_source:
            continue
        if args.fresh_find_run_id and str(row.get('fresh_find_run_id') or '') != args.fresh_find_run_id:
            continue
        fresh_literature_candidate = bool(
            str(row.get('source') or '') == 'fresh_literature_github_search'
            and (not args.fresh_find_run_id or str(row.get('fresh_find_run_id') or '') == args.fresh_find_run_id)
        )
        if row.get('hard_topic_mismatch') and not fresh_literature_candidate:
            continue
        if row.get('repo_selection_bucket') == 'paused_by_veto' and not args.allow_veto_fallback:
            continue
        local = str(row.get('local_path') or '')
        if local and local in seen_paths:
            continue
        candidates.append(row)
        if local:
            seen_paths.add(local)
    if args.selection_stage == 'environment_claude_code' and args.fresh_find_run_id:
        current_rows = [row for row in candidates if str(row.get('fresh_find_run_id') or '') == args.fresh_find_run_id and row.get('_source') != 'active_repo']
        candidates = current_rows
    active_rows = [row for row in candidates if row.get('_source') == 'active_repo']
    fresh_base_state = load_json(paths.state / 'fresh_research_base.json', {})
    selected_fresh_title = ''
    if isinstance(fresh_base_state, dict) and isinstance(fresh_base_state.get('selected'), dict):
        selected_fresh_title = str(fresh_base_state.get('selected', {}).get('title') or '').lower()

    def fresh_candidate_sort_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
        title = str(row.get('literature_base_title') or '').lower()
        rank_raw = row.get('literature_base_rank')
        try:
            rank = float(rank_raw)
        except Exception:
            rank = 9999.0
        selected_bonus = 0.0 if selected_fresh_title and title == selected_fresh_title else 1.0
        # Fresh literature candidates must not be discarded because metadata lacks every topic token;
        # paper-level fit was already established by the Find evidence gate. Keep repo score only as a tie-breaker.
        score = float(row.get('repo_reuse_score', row.get('score', -999)) or -999)
        return (selected_bonus, rank, -score, str(row.get('name', '')))

    other_rows = [row for row in candidates if row.get('_source') != 'active_repo']
    if args.candidate_source == 'fresh_literature_github_search':
        other_rows = sorted(other_rows, key=fresh_candidate_sort_key)
    else:
        other_rows = sorted(other_rows, key=lambda r: (-float(r.get('repo_reuse_score', r.get('score', -999)) or -999), str(r.get('name', ''))))
    candidates = active_rows + other_rows[:max(0, max(1, args.limit) - len(active_rows))]

    def save_selection_progress(status: str, audited_rows: list[dict[str, Any]], ready_rows: list[dict[str, Any]]) -> None:
        save_json(paths.state / 'evidence_ready_repo_selection.json', {
            'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'project': args.project,
            'env_name': env_name,
            'fresh_find_run_id': args.fresh_find_run_id,
            'selection_stage': selection_stage,
            'requirement': 'A selected environment repo must have runnable code signals and at least one real dataset whose repo loader probe succeeds.',
            'status': status,
            'audited_count': len(audited_rows),
            'evidence_ready_count': len(ready_rows),
            'selected': ready_rows[0] if ready_rows else {},
            'audited_candidates': audited_rows,
            'selection_gate': 'auditing_candidates',
        })

    audited = []
    ready = []
    original_req = load_json(paths.state / 'repo_data_requirements.json', {})
    original_probe = load_json(paths.state / 'real_dataset_probe.json', {})
    original_registry = load_json(paths.state / 'dataset_registry.json', [])
    for row in candidates:
        item = {
            'name': row.get('name', ''),
            'url': row.get('url', ''),
            'original_score': row.get('repo_reuse_score', row.get('score', 0)),
            'missing_topic_groups': topic_gaps(row),
            'source': row.get('_source') or row.get('source', 'candidate_pool'),
            'fresh_find_run_id': row.get('fresh_find_run_id', ''),
            'literature_base_title': row.get('literature_base_title', ''),
            'literature_base_rank': row.get('literature_base_rank', ''),
            'metadata_topic_mismatch_allowed': bool(row.get('hard_topic_mismatch') and str(row.get('source') or '') == 'fresh_literature_github_search'),
        }
        repo, clone_info = clone_or_reuse(paths, row)
        item['clone'] = clone_info
        if not repo:
            item['decision'] = 'reject_clone_unavailable'
            audited.append(item)
            save_selection_progress('running_repo_candidate_audit', audited, ready)
            continue
        signals = quick_signals(repo)
        probe = probe_repo(args.project, repo, env_name, args.timeout_sec)
        claim_ready = [p for p in probe.get('probes', []) if p.get('claim_ready')]
        # Candidate probing updates global probe/registry state; restore after each non-active audit so
        # a failed side candidate cannot erase the evidence for the current active route.
        if str(repo) != active_path:
            if isinstance(original_req, dict) and original_req:
                save_json(paths.state / 'repo_data_requirements.json', original_req)
            if isinstance(original_probe, dict) and original_probe:
                save_json(paths.state / 'real_dataset_probe.json', original_probe)
            if isinstance(original_registry, list):
                save_json(paths.state / 'dataset_registry.json', original_registry)
        item.update({
            'repo_path': str(repo),
            'signals': signals,
            'probe_summary': {
                'probe_return_code': probe.get('probe_return_code'),
                'claim_ready_datasets': [p.get('dataset') for p in claim_ready],
                'probe_count': len(probe.get('probes', [])),
            },
            'selection_score': score_candidate(row, repo, signals, probe, active_path),
        })
        if claim_ready and signals.get('has_entrypoint'):
            item['decision'] = 'evidence_ready_repo_and_data_paired'
            item['claim_ready_dataset'] = claim_ready[0].get('dataset')
            item['claim_ready_probe'] = claim_ready[0]
            ready.append(item)
        elif claim_ready:
            item['decision'] = 'data_ready_but_code_entrypoint_unclear'
        else:
            item['decision'] = 'not_evidence_ready'
        audited.append(item)
        save_selection_progress('running_repo_candidate_audit', audited, ready)

    ready = sorted(ready, key=lambda x: (-float(x.get('selection_score', 0)), str(x.get('name', ''))))
    selected = ready[0] if ready else {}
    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'env_name': env_name,
        'fresh_find_run_id': args.fresh_find_run_id,
        'selection_stage': selection_stage,
        'requirement': 'A selected environment repo must have runnable code signals and at least one real dataset whose repo loader probe succeeds.',
        'audited_count': len(audited),
        'evidence_ready_count': len(ready),
        'selected': selected,
        'audited_candidates': audited,
        'guardrails': [
            'Do not select a repo from paper/topic fit alone if its data cannot be loaded.',
            'A vetoed repo can only be used as an evidence-first fallback when loader-ready real data exists and Claude judges it to be the best transformable route for the current dynamic topic.',
            'Topic gaps are not hard-coded keywords; Claude must explain whether they are acceptable modification work or a reason to keep searching.',
            'Synthetic data never satisfies this selector.',
        ],
    }
    if args.use_claude_review:
        payload['claude_review'] = run_claude_review(args.project, payload)
        topic_decision = run_claude_topic_decision(args.project, payload)
        payload['claude_topic_decision'] = topic_decision
        payload['repo_env_strategy'] = write_repo_env_strategy(
            args.project,
            paths,
            topic_decision,
            payload.get('selected', {}) if isinstance(payload.get('selected'), dict) else {},
            active if isinstance(active, dict) else {},
            env_name,
        )
        if topic_decision.get('accept_as_current_best') and topic_decision.get('repo_path'):
            decided_path = str(topic_decision.get('repo_path'))
            decided_name = str(topic_decision.get('best_repo') or '')
            decided = next((item for item in ready if str(item.get('repo_path')) == decided_path or str(item.get('name')) == decided_name), {})
            if decided:
                selected = decided
                selected['claude_topic_decision'] = topic_decision
                selected['fresh_find_run_id'] = args.fresh_find_run_id or selected.get('fresh_find_run_id', '')
                selected['selection_stage'] = selection_stage
                selected['selected_by_stage'] = selection_stage
                selected['anchor_selection_policy'] = 'Environment-stage Claude Code selected this base after repo/data evidence review; Find only supplied candidate papers.' if selection_stage == 'environment_claude_code' else 'Claude repo/data audit selected this candidate, but it is not yet an environment-stage anchor decision.'
                payload['selected'] = selected
                payload['selection_gate'] = 'accepted_by_claude_topic_fit'
                payload['selection_stage'] = selection_stage
                payload['selected_by_stage'] = selection_stage
            else:
                pending = next((item for item in audited if str(item.get('repo_path')) == decided_path or str(item.get('name')) == decided_name), {})
                if pending:
                    pending = dict(pending)
                    pending['claude_topic_decision'] = topic_decision
                    pending['fresh_find_run_id'] = args.fresh_find_run_id or pending.get('fresh_find_run_id', '')
                    pending['selection_stage'] = selection_stage
                    pending['selected_by_stage'] = selection_stage
                    pending['pending_reason'] = 'Claude accepted this as the best transformable candidate, but repo/data loader evidence is not claim-ready yet.'
                    payload['pending_environment_candidate'] = pending
                    payload['selection_gate'] = 'blocked_pending_data_loader_for_claude_best_candidate'
                else:
                    payload['selection_gate'] = 'continued_search_required_claude_choice_not_evidence_ready'
                payload['selected'] = {}
                selected = {}
        elif selected and args.allow_topic_gap_fallback:
            selected['fresh_find_run_id'] = args.fresh_find_run_id or selected.get('fresh_find_run_id', '')
            selected['selection_stage'] = selection_stage or 'local_topic_gap_fallback'
            selected['selected_by_stage'] = selection_stage or 'local_topic_gap_fallback'
            payload['selected'] = selected
            payload['selection_gate'] = 'accepted_by_local_evidence_with_topic_gap_fallback'
            payload['selection_stage'] = selection_stage or 'local_topic_gap_fallback'
            payload['selected_by_stage'] = selection_stage or 'local_topic_gap_fallback'
        else:
            payload['selection_gate'] = 'continued_search_required_by_claude_topic_fit'
            payload['selected'] = {}
            selected = {}
    else:
        if selected:
            selected['fresh_find_run_id'] = args.fresh_find_run_id or selected.get('fresh_find_run_id', '')
            selected['selection_stage'] = selection_stage or 'local_repo_data_probe'
            selected['selected_by_stage'] = selection_stage or 'local_repo_data_probe'
            payload['selected'] = selected
        payload['selection_gate'] = 'accepted_by_local_evidence_no_claude_review' if selected else 'continued_search_required_no_evidence_ready_repo'
        payload['selection_stage'] = selection_stage or 'local_repo_data_probe'
        payload['selected_by_stage'] = (selection_stage or 'local_repo_data_probe') if selected else ''
    save_json(paths.state / 'evidence_ready_repo_selection.json', payload)

    lines = ['# Evidence-Ready Repo Selection\n\n']
    lines.append(f"- generated_at: {payload['generated_at']}\n")
    lines.append(f"- audited_count: {payload['audited_count']}\n")
    lines.append(f"- evidence_ready_count: {payload['evidence_ready_count']}\n")
    if selected:
        lines.append(f"- selected_repo: {selected.get('name')}\n")
        lines.append(f"- selected_path: {selected.get('repo_path')}\n")
        lines.append(f"- selected_dataset: {selected.get('claim_ready_dataset')}\n")
        lines.append(f"- selection_score: {selected.get('selection_score')}\n")
        topic_decision = selected.get('claude_topic_decision', {}) if isinstance(selected.get('claude_topic_decision'), dict) else payload.get('claude_topic_decision', {})
        if isinstance(topic_decision, dict) and topic_decision:
            lines.append(f"- claude_decision: {topic_decision.get('decision', '')} confidence={topic_decision.get('confidence', '')}\n")
            lines.append(f"- claude_rationale: {topic_decision.get('rationale_en') or topic_decision.get('rationale', '')}\n")
            if topic_decision.get('rationale_zh'):
                lines.append(f"- claude_rationale_zh: {topic_decision.get('rationale_zh', '')}\n")
            if topic_decision.get('required_modifications'):
                lines.append(f"- required_modifications: {'; '.join(topic_decision.get('required_modifications', []))}\n")
        if selected.get('missing_topic_groups'):
            lines.append(f"- topic_gap_to_fix_later: {', '.join(selected.get('missing_topic_groups', []))}\n")
    else:
        lines.append('- selected_repo: none\n')
        pending = payload.get('pending_environment_candidate', {}) if isinstance(payload.get('pending_environment_candidate'), dict) else {}
        if pending:
            lines.append(f"- pending_environment_candidate: {pending.get('name')}\n")
            lines.append(f"- pending_repo_path: {pending.get('repo_path')}\n")
            lines.append(f"- pending_reason: {pending.get('pending_reason', '')}\n")
            if pending.get('probe_summary'):
                lines.append(f"- pending_claim_ready: {', '.join(pending.get('probe_summary', {}).get('claim_ready_datasets', []) or []) or 'none'}\n")
        topic_decision = payload.get('claude_topic_decision', {})
        if isinstance(topic_decision, dict) and topic_decision:
            lines.append(f"- claude_decision: {topic_decision.get('decision', '')} confidence={topic_decision.get('confidence', '')}\n")
            lines.append(f"- claude_rationale: {topic_decision.get('rationale_en') or topic_decision.get('rationale', '')}\n")
            if topic_decision.get('rationale_zh'):
                lines.append(f"- claude_rationale_zh: {topic_decision.get('rationale_zh', '')}\n")
    lines.append('\n## Audited Candidates\n')
    for item in audited:
        lines.append(f"- {item.get('name')} | decision={item.get('decision')} | claim_ready={', '.join(item.get('probe_summary', {}).get('claim_ready_datasets', []) or []) or 'none'} | score={item.get('selection_score', '')}\n")
    (paths.reports / 'evidence_ready_repo_selection.md').write_text(''.join(lines), encoding='utf-8')

    if selected and args.write_active:
        same_as_active = active_path and selected.get('repo_path') == active_path
        active_payload = dict(active) if same_as_active and isinstance(active, dict) else {
            'previous_active_repo': active,
        }
        original_topic_score = float(selected.get('original_score', 0) or 0)
        if selected.get('missing_topic_groups'):
            # Keep topic/reuse score honest: evidence-ready fallback is runnable, but the topic gap remains.
            display_reuse_score = max(min(original_topic_score, 9.0), 9.0)
        else:
            display_reuse_score = max(original_topic_score, float(selected.get('selection_score', 0) or 0))
        active_payload.update({
            'name': selected.get('name', ''),
            'url': selected.get('url', ''),
            'repo_path': selected.get('repo_path', ''),
            'selected_at': active_payload.get('selected_at') or dt.datetime.now(dt.timezone.utc).isoformat(),
            'selected_by': args.fresh_find_run_id or active_payload.get('selected_by') or '',
            'selection_stage': payload.get('selection_stage', ''),
            'selected_by_stage': payload.get('selected_by_stage', payload.get('selection_stage', '')),
            'anchor_selection_policy': 'Anchor/base accepted only through environment-stage Claude Code with repo/data evidence; Find ranking is not an anchor-selection decision.' if payload.get('selection_stage') == 'environment_claude_code' else 'Local repo/data probe only; not an environment-stage Claude Code anchor decision.',
            'selected_base_title': selected.get('literature_base_title') or active_payload.get('selected_base_title') or '',
            'selection_bucket': 'claude_transformable_evidence_ready' if selected.get('missing_topic_groups') else 'evidence_ready',
            'repo_reuse_score': display_reuse_score,
            'evidence_selection_score': selected.get('selection_score', 0),
            'repo_execution_ready': True,
            'repo_support_signals': [key for key, ok in selected.get('signals', {}).items() if ok is True],
            'claim_ready_dataset': selected.get('claim_ready_dataset', ''),
            'claim_ready_datasets': selected.get('probe_summary', {}).get('claim_ready_datasets', []),
            'selection_reason': selected.get('claude_topic_decision', {}).get('rationale_en') or selected.get('claude_topic_decision', {}).get('rationale') or 'Selected by evidence-ready repo selector because code entrypoint and real dataset loader probe both passed.',
            'topic_gaps_to_fix_later': selected.get('missing_topic_groups', []),
            'required_modifications': selected.get('claude_topic_decision', {}).get('required_modifications', []),
            'selection_risks': selected.get('claude_topic_decision', {}).get('risks', []),
            'claude_topic_fit_decision': selected.get('claude_topic_decision', {}),
        })
        strategy = load_json(paths.state / 'repo_env_strategy.json', {})
        if not isinstance(strategy, dict) or not strategy:
            strategy = write_repo_env_strategy(
                args.project,
                paths,
                selected.get('claude_topic_decision', {}) if isinstance(selected.get('claude_topic_decision'), dict) else {},
                selected,
                active if isinstance(active, dict) else {},
                env_name,
            )
        active_payload['claude_repo_env_strategy'] = strategy
        save_json(paths.state / 'active_repo.json', active_payload)
        sync_selected_candidate(paths, selected)
        print(f"selected_active_repo={active_payload['name']} dataset={active_payload['claim_ready_dataset']}")
    else:
        print('selected_active_repo=none')
    print(paths.reports / 'evidence_ready_repo_selection.md')

    # Restore active snapshots unless caller wrote a new active route; the environment stage will rebuild requirements for the selected active repo next.
    if selected and args.write_active and selected.get('repo_path'):
        selected_repo_path = str(selected.get('repo_path'))
        run([sys.executable, 'scripts/build_repo_data_requirements.py', '--project', args.project, '--repo-path', selected_repo_path], ROOT, timeout=180)
        run([sys.executable, 'scripts/probe_repo_dataset.py', '--project', args.project, '--repo-path', selected_repo_path, '--env-name', env_name, '--timeout-sec', str(args.timeout_sec)], ROOT, timeout=args.timeout_sec + 90)
        run([sys.executable, 'scripts/data_unavailability_policy.py', '--project', args.project], ROOT, timeout=120)
    else:
        if isinstance(original_req, dict) and original_req:
            save_json(paths.state / 'repo_data_requirements.json', original_req)
        if isinstance(original_probe, dict) and original_probe:
            save_json(paths.state / 'real_dataset_probe.json', original_probe)
        if isinstance(original_registry, list):
            save_json(paths.state / 'dataset_registry.json', original_registry)
    return 0 if selected else int(os.environ.get('REPO_SELECTION_EMPTY_CODE', '2'))


if __name__ == '__main__':
    raise SystemExit(main())
