#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_state import append_agent_log
from project_config import project_source_selection, project_target_venue, update_project_settings
from project_paths import ROOT, build_paths
from work_status import append_supervision_status

SAFE_BLOCKED_RC = {0, 2}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def parse_iso_time(value: Any) -> dt.datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def current_find_revision_time(paths) -> dt.datetime | None:
    candidates: list[dt.datetime] = []
    find_results = load_json(paths.planning / 'finding' / 'find_results.json', {})
    find_progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    for payload in [find_results, find_progress]:
        if not isinstance(payload, dict):
            continue
        for key in ['updated_at', 'generated_at', 'created_at']:
            parsed = parse_iso_time(payload.get(key))
            if parsed is not None:
                candidates.append(parsed)
        strict = payload.get('strict_reclassification')
        if isinstance(strict, dict):
            parsed = parse_iso_time(strict.get('generated_at'))
            if parsed is not None:
                candidates.append(parsed)
    for file_path in [paths.planning / 'finding' / 'find_results.json', paths.planning / 'finding' / 'find_progress.json']:
        try:
            candidates.append(dt.datetime.fromtimestamp(file_path.stat().st_mtime, dt.timezone.utc))
        except OSError:
            pass
    return max(candidates) if candidates else None


def _timestamp_current(value: Any, revision: dt.datetime | None) -> bool:
    parsed = parse_iso_time(value)
    if parsed is None:
        return False
    return revision is None or parsed + dt.timedelta(seconds=2) >= revision


FULL_TEXT_READ_POLICY_VERSION = 'full_text_required_v5_detailed_deep_read'


def _int_value(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def reading_validation_is_ready(validation: Any, run_id: str) -> bool:
    if not isinstance(validation, dict):
        return False
    if str(validation.get('run_id') or '').strip() != str(run_id or '').strip():
        return False
    if validation.get('valid') is not True:
        return False
    if str(validation.get('policy_version') or '').strip() != FULL_TEXT_READ_POLICY_VERSION:
        return False
    expected = _int_value(validation.get('expected_recommendation_count'))
    actual = _int_value(validation.get('actual_reading_count'))
    full_text = _int_value(validation.get('full_text_reading_count'))
    pending = _int_value(validation.get('pending_full_text_reading_count'))
    if not expected or actual != expected or full_text < expected or pending > 0:
        return False
    if validation.get('blockers'):
        return False
    return True


def audit_current_find_claude_state(paths, run_id: str) -> dict[str, Any]:
    revision = current_find_revision_time(paths)
    takeover = load_json(paths.state / 'current_find_claude_takeover_result.json', {})
    validation = load_json(paths.state / 'current_find_claude_reading_validation.json', {})
    repairs: list[str] = []
    result = {
        'run_id': run_id,
        'current_find_revision_at': revision.isoformat() if revision is not None else '',
        'takeover_status': takeover.get('status', '') if isinstance(takeover, dict) else '',
        'takeover_finished_at': takeover.get('finished_at', '') if isinstance(takeover, dict) else '',
        'takeover_stale': False,
        'reading_validation_valid': reading_validation_is_ready(validation, run_id),
        'reading_validation_raw_valid': bool(isinstance(validation, dict) and validation.get('valid') is True),
        'reading_validation_policy': validation.get('policy_version', '') if isinstance(validation, dict) else '',
        'full_text_reading_count': _int_value(validation.get('full_text_reading_count')) if isinstance(validation, dict) else 0,
        'pending_full_text_reading_count': _int_value(validation.get('pending_full_text_reading_count')) if isinstance(validation, dict) else 0,
        'reading_validation_generated_at': validation.get('generated_at', '') if isinstance(validation, dict) else '',
        'reading_validation_stale': False,
        'repairs': repairs,
    }
    if isinstance(takeover, dict) and takeover.get('return_code') == 0 and not _timestamp_current(takeover.get('finished_at') or takeover.get('started_at'), revision):
        result['takeover_stale'] = True
        payload = {
            'run_id': run_id,
            'status': 'stale_current_find_claude_takeover',
            'detected_at': now_iso(),
            'takeover_finished_at': takeover.get('finished_at', ''),
            'current_find_revision_at': result['current_find_revision_at'],
            'policy': 'Claude takeover output is stale when it predates the latest find_results/find_progress revision or strict reclassification; it must not be treated as current TASTE understanding.',
        }
        save_json(paths.state / 'current_find_claude_takeover_stale.json', payload)
        repairs.append('recorded_stale_current_find_claude_takeover')
    if isinstance(validation, dict) and validation.get('valid') is True and not reading_validation_is_ready(validation, run_id):
        result['reading_validation_stale'] = True
        payload = {
            'run_id': run_id,
            'status': 'invalid_current_find_claude_reading_validation',
            'detected_at': now_iso(),
            'validation_generated_at': validation.get('generated_at', ''),
            'current_find_revision_at': result['current_find_revision_at'],
            'previous_valid': True,
            'policy_version': validation.get('policy_version', ''),
            'full_text_reading_count': _int_value(validation.get('full_text_reading_count')),
            'pending_full_text_reading_count': _int_value(validation.get('pending_full_text_reading_count')),
            'policy': 'A current-Find reading validation is usable only when policy_version=full_text_required_v5_detailed_deep_read and every user-visible recommendation has full-text/PDF/page evidence plus Chinese deep-read synthesis. Older or metadata-only validations are audit history only.',
        }
        save_json(paths.state / 'current_find_claude_reading_validation_stale.json', payload)
        repairs.append('recorded_invalid_current_find_claude_reading_validation')
    elif isinstance(validation, dict) and validation.get('valid') is True and not _timestamp_current(validation.get('generated_at'), revision):
        result['reading_validation_stale'] = True
        payload = {
            'run_id': run_id,
            'status': 'stale_current_find_claude_reading_validation',
            'detected_at': now_iso(),
            'validation_generated_at': validation.get('generated_at', ''),
            'current_find_revision_at': result['current_find_revision_at'],
            'previous_valid': True,
            'policy': 'A reading validation generated before the latest strict current-Find revision is audit history only; current planning must rebuild or revalidate it before use.',
        }
        save_json(paths.state / 'current_find_claude_reading_validation_stale.json', payload)
        repairs.append('recorded_stale_current_find_claude_reading_validation')
    save_json(paths.state / 'current_find_claude_state_audit.json', {**result, 'audited_at': now_iso()})
    return result


def compact_text(value: Any, limit: int = 700) -> str:
    text = ' '.join(str(value or '').replace('\n', ' ').split())
    return text[:limit] + ('...' if len(text) > limit else '')


def public_action_text(route: Any, action: Any) -> str:
    text = compact_text(action)
    lower = text.lower()
    route_text = str(route or '')
    if route_text in {'experiment_evidence_repair', 'selected_base_viability_gate'} or any(marker in lower for marker in ['paper_evidence_audit', 'hold-markdown-only', 'scientific_progress_gate', 'no audit-ready promotable', 'promotable candidate']):
        return '参考复现已通过；当前缺少当前主线下可审计、可推广的 项目目标候选实验。下一步由 project agent 继续真实实验迭代，论文/claim 暂停。'
    if route_text == 'active_full_research_cycle_worker':
        return text
    if len(text) > 220 or text.count(';') >= 2 or text.count('/') >= 6:
        return '当前存在项目门控阻塞；网页展示摘要，完整证据保留在 state/report 文件中。'
    return text


def stage_action_text(stage: Any) -> str:
    text = str(stage or '').strip().replace('_', '-')
    if not text:
        return '完整科研循环正在运行；等待下一条阶段日志。'
    if 'literature-tool-packet' in text or 'sync-outputs' in text or 'third-party' in text:
        return '完整科研循环正在同步现有文献包和科研工具栈；这不是新 Find，完成后会继续环境/实验门控。'
    if 'guidance-checkin' in text or 'claude' in text:
        return '项目 Claude Code 正在检查队列和当前科研状态；等待其写入阶段回执和实验计划。'
    if any(marker in text for marker in ['autonomous', 'experiment', 'trajectory', 'blocker-action', 'paper-evidence', 'submission-readiness']):
        return '完整科研循环正在推进实验/证据门控；当前重点是当前主线下真实 项目目标候选实验。'
    if 'paper' in text or 'latex' in text:
        return '完整科研循环正在检查论文阶段，但论文/claim 仍受实验和证据门控约束。'
    return f'完整科研循环正在运行当前阶段：{text}。'


def count_packet_rows(value: Any, *keys: str) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in keys:
            row = value.get(key)
            if isinstance(row, list):
                return len(row)
            if isinstance(row, dict):
                return len(row)
    return 0


def _find_progress_llm_blocker(progress: dict[str, Any]) -> tuple[bool, str]:
    status = str(progress.get('status') or '').lower()
    phase = str(progress.get('phase') or '').lower()
    reason = str(progress.get('blocked_reason') or progress.get('reason') or '').strip()
    reason_lower = reason.lower()
    blocked = bool(
        'blocked_llm' in status
        or 'blocked_llm' in phase
        or ('llm' in reason_lower and any(marker in reason_lower for marker in ['quota', '429', 'api', 'configuration', 'key']))
    )
    return blocked, reason


def literature_snapshot(paths) -> dict[str, Any]:
    progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    progress = progress if isinstance(progress, dict) else {}
    current_run = str(progress.get('run_id') or '').strip()
    progress_blocked_llm, blocked_reason = _find_progress_llm_blocker(progress)
    find_results = load_json(paths.planning / 'finding' / 'find_results.json', {})
    if isinstance(find_results, dict) and current_run:
        result_run = str(find_results.get('run_id') or '').strip()
        if result_run and result_run != current_run:
            find_results = {}
    if not isinstance(find_results, dict):
        find_results = {}
    pools = ['articles', 'strong_recommendations', 'screened_ranking', 'read_candidates', 'evaluated_candidates', 'critique_candidates']
    counts = {key: count_packet_rows(find_results.get(key), key) for key in pools}
    progress_counts = progress.get('counts') if isinstance(progress.get('counts'), dict) else {}
    if progress:
        counts.update({
            'raw_title_index_papers': progress_counts.get('raw_title_index') or progress_counts.get('raw_title_index_papers') or 0,
            'title_candidates': progress_counts.get('title_candidates') or 0,
            'traceable_candidates': progress_counts.get('traceable_candidates') or progress_counts.get('title_candidates') or 0,
            'detail_fetched': progress_counts.get('detail_fetched') or 0,
            'evaluated_candidates': progress_counts.get('evaluated_candidates') or counts.get('evaluated_candidates', 0),
            'strong_recommendations': progress.get('strong_recommendation_count') or progress_counts.get('strong_recommendations') or counts.get('strong_recommendations', 0),
            'recommendation_target_count': progress.get('recommendation_target_count') or 0,
            'recommendation_shortfall': progress.get('recommendation_shortfall') or 0,
        })
    top_strong: list[str] = []
    if not progress_blocked_llm:
        for row in (find_results.get('strong_recommendations') or find_results.get('articles') or [])[:8]:
            if isinstance(row, dict):
                title = str(row.get('title') or 'Untitled').strip()
                score = row.get('fit_score', row.get('score', ''))
                tier = str(row.get('evidence_tier') or row.get('topic_evidence') or '').strip()
                top_strong.append(' | '.join(str(part) for part in [title, f'fit={score}' if score != '' else '', tier] if part))
    source_rows = []
    for row in (progress.get('venue_health_report') or find_results.get('venue_health_report') or []):
        if isinstance(row, dict):
            source_rows.append({
                'venue_id': row.get('venue_id', ''),
                'venue': row.get('venue') or row.get('venue_id') or '',
                'ok': bool(row.get('ok')),
                'adapter': row.get('adapter', ''),
                'effective_years': row.get('effective_years', []),
                'corpus_count': row.get('corpus_count', 0),
                'candidate_count': row.get('candidate_count', 0),
                'sample_count': row.get('sample_count', 0),
                'fallback': row.get('year_fallback_reason', ''),
            })
    selection = progress.get('selection') if isinstance(progress.get('selection'), dict) else find_results.get('selection') if isinstance(find_results.get('selection'), dict) else {}
    return {
        'run_id': current_run or find_results.get('run_id', ''),
        'status': progress.get('status', ''),
        'phase': progress.get('phase', ''),
        'blocked_reason': blocked_reason,
        'selection': selection,
        'counts': counts,
        'top_strong': top_strong,
        'source_rows': source_rows,
        'files': {
            'find_results': str(paths.planning / 'finding' / 'find_results.json'),
            'find_progress': str(paths.planning / 'finding' / 'find_progress.json'),
            'article': str(paths.planning / 'finding' / 'article.md'),
        },
    }


def http_json(url: str, timeout: int = 45, retries: int = 2) -> tuple[Any, str]:
    last_error = ''
    for _attempt in range(max(1, retries + 1)):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                raw = response.read().decode('utf-8', 'replace')
            return json.loads(raw), ''
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
    return {}, last_error


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
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


def _looks_like_experiment_command_text(cmd: str) -> bool:
    lower = str(cmd or '').lower()
    if '--artifact_dir' in lower or '/artifacts/' in lower:
        return True
    script_names: list[str] = []
    for part in re.split(r"\s+", lower):
        cleaned = part.strip(chr(34) + chr(39))
        if cleaned.endswith('.py'):
            script_names.append(Path(cleaned).name)
    for name in script_names:
        if name == 'single_train.py':
            return True
        if name.startswith('train') or name.startswith('finetune'):
            return True
        if name == 'main.py' and any(flag in lower for flag in ['--data', '--dataset', '--config']):
            return True
    return False


def process_rows() -> list[dict[str, Any]]:
    proc = subprocess.run(['ps', '-eo', 'pid=,ppid=,etimes=,stat=,pcpu=,pmem=,cmd='], cwd=ROOT, text=True, capture_output=True, timeout=20)
    rows: list[dict[str, Any]] = []
    if proc.returncode != 0:
        return rows
    interesting = [
        'uvicorn auto_research.web.server',
        'run_full_research_cycle.py',
        'claude_project_session.py',
        'run_paper_pipeline.py',
        'run_paper_orchestra_bridge.py',
        'run_safe_unblock.py',
        'single_train.py',
        'train.py --data',
        'main.py',
        'finetune.py',
        'finetune_llm.py',
        '--artifact_dir',
        '/artifacts/',
        '/claude -p',
    ]
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        cmd = parts[6]
        cmd_parts = cmd.split()
        if any(skip in cmd for skip in [' grep ', ' rg ', ' ps -', 'sed -n', 'curl -sS']):
            continue
        if not any(token in cmd for token in interesting) and not _looks_like_experiment_command_text(cmd):
            continue
        if any(token in cmd for token in ['run_full_research_cycle.py', 'claude_project_session.py', 'run_paper_pipeline.py', 'run_paper_orchestra_bridge.py', 'run_safe_unblock.py']) and not any('python' in part for part in cmd_parts[:2]):
            continue
        cwd_text = ''
        try:
            cwd_text = str(Path(f'/proc/{int(parts[0])}/cwd').resolve())
        except Exception:
            cwd_text = ''
        rows.append({'pid': int(parts[0]), 'ppid': int(parts[1]), 'etimes': int(parts[2]), 'stat': parts[3], 'pcpu': parts[4], 'pmem': parts[5], 'cmd': cmd, 'cwd': cwd_text})
    return rows


def command_text(value: Any) -> str:
    if isinstance(value, list):
        return ' '.join(str(part) for part in value)
    return str(value or '')


def active_project_worker_row(project: str) -> dict[str, Any] | None:
    paths = build_paths(project) if project else None
    project_root = str(paths.root) if paths else ''
    worker_tokens = ['claude_project_session.py', 'single_train.py', 'train.py --data', 'main.py', 'finetune.py', 'finetune_llm.py', '--artifact_dir', '/artifacts/', 'run_paper_pipeline.py', 'run_paper_orchestra_bridge.py', '/claude -p']
    for row in process_rows():
        cmd = str(row.get('cmd') or '')
        cwd = str(row.get('cwd') or '')
        project_owned = bool(project and project in cmd) or bool(project_root and (project_root in cmd or project_root in cwd))
        if project_owned and (any(token in cmd for token in worker_tokens) or _looks_like_experiment_command_text(cmd)):
            return row
    return None


def normalize_full_cycle_job(project: str, job: Any) -> dict[str, Any]:
    src = dict(job) if isinstance(job, dict) else {}
    cmd_text = command_text(src.get('cmd') or src.get('command'))
    if cmd_text and not src.get('cmd'):
        src['cmd'] = cmd_text
    live_rows = [row for row in process_rows() if 'run_full_research_cycle.py' in str(row.get('cmd') or '') and (not project or project in str(row.get('cmd') or ''))]
    live_row = None
    pid_text = str(src.get('pid') or '').strip()
    if pid_text:
        live_row = next((row for row in live_rows if str(row.get('pid') or '') == pid_text), None)
    if live_row is None and live_rows:
        live_row = live_rows[0]
    if live_row is not None:
        src.update({'project': src.get('project') or project, 'status': 'running', 'pid': live_row.get('pid'), 'cmd': live_row.get('cmd', ''), 'process_alive': True, 'alive': True, 'elapsed_sec': live_row.get('etimes'), 'pcpu': live_row.get('pcpu', ''), 'pmem': live_row.get('pmem', ''), 'kind': 'full_cycle'})
        return src
    child_row = active_project_worker_row(project)
    if not src:
        if child_row is not None:
            return {
                'project': project,
                'status': 'stale',
                'process_alive': False,
                'alive': False,
                'kind': 'full_cycle',
                'stale_reason': 'no_matching_live_full_cycle_process',
                'active_project_worker': {key: child_row.get(key) for key in ['pid', 'ppid', 'etimes', 'pcpu', 'pmem', 'cmd', 'cwd']},
            }
        return {}
    if child_row is not None:
        src['active_project_worker'] = {key: child_row.get(key) for key in ['pid', 'ppid', 'etimes', 'pcpu', 'pmem', 'cmd', 'cwd']}
    if str(src.get('status') or '').lower() == 'running':
        src['status'] = 'stale'
        src['stale_reason'] = 'no_matching_live_full_cycle_process'
    src['process_alive'] = False
    src['alive'] = False
    return src




def _public_phase_from_stage(stage: Any) -> str:
    text = str(stage or '').strip().lower().replace('_', '-')
    if any(marker in text for marker in ['paper-evidence-audit-precheck', 'submission-readiness-precheck', 'trajectory-evidence-refresh', 'blocker-action-plan-precheck']):
        return 'experiment'
    if any(marker in text for marker in ['autonomous', 'experiment', 'trajectory', 'training', 'repair', 'blocker', 'guidance-checkin']):
        return 'experiment'
    if any(marker in text for marker in ['paper', 'latex', 'conference-preview']):
        return 'paper'
    environment_literature_markers = [
        'sync-outputs', 'literature-sync', 'literature-tool-packet', 'build-literature-tool-packet',
        'fresh-research-base-selection', 'research-base-selection', 'base-selection', 'base-candidate',
        'literature-base-candidate', 'literature-base-audit', 'method-stack-sync',
    ]
    if any(marker in text for marker in environment_literature_markers):
        return 'environment'
    fresh_find_markers = ['literature-survey', 'run-finding', 'run-driver', 'run-literature-tool']
    if any(marker in text for marker in fresh_find_markers) or text in {'find', 'literature', 'finding'}:
        return 'find'
    if any(marker in text for marker in ['reference', 'environment', 'loader', 'smoke']):
        return 'environment'
    if 'plan' in text:
        return 'plan'
    if 'ideation' in text or 'idea' in text:
        return 'idea'
    if 'read' in text:
        return 'read'
    return 'experiment'


def _looks_like_experiment_process(row: dict[str, Any]) -> bool:
    cmd = str(row.get('cmd') or '').lower()
    cwd = str(row.get('cwd') or '')
    if 'run_full_research_cycle.py' in cmd or 'claude_project_session.py' in cmd:
        return False
    return _looks_like_experiment_command_text(cmd) and bool(cwd or cmd)


def sync_full_cycle_job_state(paths, job: Any) -> list[str]:
    if not isinstance(job, dict) or not job:
        return []
    repairs: list[str] = []
    normalized = dict(job)
    job_path = paths.state / 'full_cycle_job.json'
    existing_job = load_json(job_path, {})
    if existing_job != normalized:
        save_json(job_path, normalized)
        repairs.append('synced_full_cycle_job_state')

    full_path = paths.state / 'full_research_cycle.json'
    full_state = load_json(full_path, {})
    if isinstance(full_state, dict):
        changed = False
        if full_state.get('full_cycle_job') != normalized:
            full_state['full_cycle_job'] = normalized
            changed = True
        is_live = normalized.get('status') == 'running' and normalized.get('process_alive') is True and str(normalized.get('kind') or '') != 'active_child_worker'
        active_live_job = normalized if is_live else {}
        if full_state.get('active_live_job') != active_live_job:
            full_state['active_live_job'] = active_live_job
            changed = True
        active_worker = normalized.get('active_project_worker') if isinstance(normalized.get('active_project_worker'), dict) else {}
        if full_state.get('active_project_worker') != active_worker:
            full_state['active_project_worker'] = active_worker
            changed = True
        is_live = normalized.get('status') == 'running' and normalized.get('process_alive') is True and str(normalized.get('kind') or '') != 'active_child_worker'
        if is_live:
            stage = str(normalized.get('stage') or (full_state.get('latest_step') or {}).get('stage') if isinstance(full_state.get('latest_step'), dict) else normalized.get('stage') or '')
            phase = _public_phase_from_stage(stage)
            project_root = str(paths.root)
            active_experiments = [row for row in process_rows() if (project_root in str(row.get('cmd', '')) or project_root in str(row.get('cwd', ''))) and _looks_like_experiment_process(row)]
            if active_experiments:
                phase = 'experiment'
                compact_rows = [{key: row.get(key) for key in ['pid', 'ppid', 'etimes', 'pcpu', 'pmem', 'cmd', 'cwd']} for row in active_experiments[:8]]
                if full_state.get('active_experiment_processes') != compact_rows:
                    full_state['active_experiment_processes'] = compact_rows
                    changed = True
            summary = f"完整科研自循环正在运行；阶段={phase}；PID={normalized.get('pid') or '-'}。"
            current_goal = stage_action_text(stage)
            if (
                full_state.get('summary') != summary
                or full_state.get('summary_zh') != summary
                or full_state.get('public_phase') != phase
                or full_state.get('current_goal') != current_goal
            ):
                full_state['summary'] = summary
                full_state['summary_zh'] = summary
                full_state['public_phase'] = phase
                full_state['current_goal'] = current_goal
                changed = True
        else:
            latest = full_state.get('latest_step') if isinstance(full_state.get('latest_step'), dict) else {}
            latest_claims_live = isinstance(latest, dict) and latest.get('status') == 'running'
        if (
            not is_live
            and (
                full_state.get('status') == 'running'
                or str(full_state.get('summary') or '').startswith('完整科研自循环正在运行')
                or str(full_state.get('summary_zh') or '').startswith('完整科研自循环正在运行')
                or latest_claims_live
            )
        ):
            raw_stage = str(normalized.get('stage') or latest.get('stage') or 'full-cycle')
            phase = _public_phase_from_stage(raw_stage)
            full_state['status'] = 'stale_full_research_cycle_snapshot'
            full_state['summary'] = f"完整科研自循环进程已停止；最后步骤={raw_stage}；阶段={phase}；没有正在运行的 full-cycle。"
            full_state['summary_zh'] = full_state['summary']
            full_state['public_phase'] = phase
            full_state['continuation_required'] = True
            full_state['continuation_reason'] = 'previous full-cycle process is stale; restart through /api/jobs/project to continue'
            if isinstance(latest, dict) and latest.get('status') == 'running':
                latest = dict(latest)
                latest['status'] = 'stale'
                latest['process_alive'] = False
                latest['stale_reason'] = 'no_matching_live_full_cycle_process'
                full_state['latest_step'] = latest
            changed = True
        if changed:
            full_state['updated_at'] = now_iso()
            save_json(full_path, full_state)
            repairs.append('synced_full_research_cycle_job_state')
    return repairs


def repo_path_from_mapping(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["repo_path", "active_repo_path", "local_path", "path", "current_selected_repo_path"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def selected_base_viability_current_selection(paths, current_run: str = "") -> dict[str, Any]:
    gate = load_json(paths.state / "selected_base_viability_gate.json", {})
    if not isinstance(gate, dict):
        return {}
    status = str(gate.get("status") or "").lower()
    decision = str(gate.get("decision") or "").lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return {}
    repo_path = repo_path_from_mapping(gate)
    repo_name = str(gate.get("current_selected_repo") or "").strip()
    title = str(gate.get("selected_base_title") or gate.get("literature_base_title") or repo_name or "").strip()
    if not (repo_path or repo_name or title):
        return {}

    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    impl_repo = impl.get("repo") if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    guard = load_json(paths.state / "selected_base_route_guard.json", {})
    trusted = guard.get("trusted_audit") if isinstance(guard, dict) and isinstance(guard.get("trusted_audit"), dict) else {}
    legacy_audit = load_json(paths.state / "fresh_base_reference_reproduction_audit.json", {})
    legacy_selected = legacy_audit.get("selected_base") if isinstance(legacy_audit, dict) and isinstance(legacy_audit.get("selected_base"), dict) else {}
    aligned_paths = {
        value
        for value in [
            repo_path_from_mapping(impl_repo),
            repo_path_from_mapping(trusted),
            repo_path_from_mapping(legacy_audit) or repo_path_from_mapping(legacy_selected),
        ]
        if value
    }
    if repo_path and aligned_paths and repo_path not in aligned_paths:
        return {}

    selected_run = str(gate.get("fresh_find_run_id") or (guard.get("selected_base_find_run_id") if isinstance(guard, dict) else "") or current_run or "").strip()
    ready_datasets = impl.get("ready_datasets", []) if isinstance(impl, dict) and isinstance(impl.get("ready_datasets"), list) else []
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
    }
    if ready_datasets:
        selected["claim_ready_dataset"] = str(ready_datasets[0])
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
        "candidate_switch_conflict": True,
    }


def current_impl_repo_path(paths) -> str:
    viability = selected_base_viability_current_selection(paths, current_literature_run_id(paths))
    selected = viability.get("selected") if isinstance(viability.get("selected"), dict) else {}
    repo_path = repo_path_from_mapping(selected)
    if repo_path:
        return repo_path

    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if isinstance(selection, dict):
        selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
        gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            repo_path = repo_path_from_mapping(selected)
            if repo_path:
                return repo_path
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            repo_path = repo_path_from_mapping(active)
            if repo_path:
                return repo_path
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return repo_path_from_mapping(repo)

def artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ['repo_path', 'active_repo_path', 'local_path', 'path']:
        value = str(payload.get(key) or '').strip()
        if value and value != current_repo:
            return False
    return True


def fresh_base_state_names(paths, suffix: str) -> list[str]:
    names = [f"fresh_base_{suffix}.json"]
    try:
        for path in sorted(paths.state.glob(f"*_{suffix}.json")):
            if path.name not in names:
                names.append(path.name)
    except Exception:
        pass
    return names


def current_payload(paths, names: list[str]) -> dict[str, Any]:
    for name in names:
        payload = load_json(paths.state / name, {})
        if artifact_matches_current_repo(paths, payload):
            return payload if isinstance(payload, dict) else {}
    return {}

def compact_probe(source: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: source.get(key) for key in keys if key in source}


def fresh_base_gate_snapshot(paths) -> dict[str, Any]:
    data = load_json(paths.state / 'fresh_base_data_acquisition.json', {})
    impl = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
    real_probe = load_json(paths.state / 'real_dataset_probe.json', {})
    loader = real_probe if isinstance(real_probe, dict) else {}
    if not loader:
        loader = current_payload(paths, ['real_dataset_probe.json'] + fresh_base_state_names(paths, 'loader_contract_probe'))
    protocol = current_payload(paths, fresh_base_state_names(paths, 'reference_protocol_probe'))
    smoke = current_payload(paths, fresh_base_state_names(paths, 'reference_smoke'))
    data = data if isinstance(data, dict) else {}
    impl = impl if isinstance(impl, dict) else {}
    loader = loader if isinstance(loader, dict) else {}
    protocol = protocol if isinstance(protocol, dict) else {}
    smoke = smoke if isinstance(smoke, dict) else {}
    ready_from_impl = impl.get('ready_datasets', []) if isinstance(impl.get('ready_datasets'), list) else []
    blocked_from_impl = impl.get('blocked_datasets', []) if isinstance(impl.get('blocked_datasets'), list) else []
    return {
        'data_status': data.get('status', ''),
        'data_decision': data.get('decision', ''),
        'loader_status': loader.get('status', ''),
        'loader_decision': loader.get('decision', '') or ('loader_contract_passed' if ready_from_impl else ''),
        'ready_datasets': loader.get('ready_datasets', []) if isinstance(loader.get('ready_datasets'), list) else ready_from_impl,
        'blocked_datasets': loader.get('blocked_datasets', []) if isinstance(loader.get('blocked_datasets'), list) else blocked_from_impl,
        'reference_protocol_status': protocol.get('status', ''),
        'reference_protocol_decision': protocol.get('decision', ''),
        'reference_smoke_status': smoke.get('status', ''),
        'reference_smoke_decision': smoke.get('decision', ''),
        'fresh_base_data_acquisition': compact_probe(data, ['status', 'decision', 'ready_datasets', 'blocked_datasets', 'generated_at', 'official_data_url', 'required_files_per_dataset', 'guardrail']),
        'loader_contract_probe': compact_probe(loader, ['status', 'decision', 'ready_datasets', 'blocked_datasets', 'generated_at', 'repo_path', 'required_files_per_dataset', 'guardrail']),
        'reference_protocol_probe': compact_probe(protocol, ['status', 'decision', 'ready_datasets', 'generated_at', 'python_executable']),
        'reference_smoke_probe': compact_probe(smoke, ['status', 'decision', 'selected_dataset', 'generated_at', 'artifact_dir']),
    }


def sync_fresh_base_gate_state(paths, snapshot: dict[str, Any]) -> list[str]:
    full_path = paths.state / 'full_research_cycle.json'
    full_state = load_json(full_path, {})
    if not isinstance(full_state, dict) or not isinstance(snapshot, dict):
        return []
    changed = False
    for key in [
        'data_status', 'data_decision', 'loader_status', 'loader_decision',
        'ready_datasets', 'blocked_datasets', 'reference_protocol_status',
        'reference_protocol_decision', 'reference_smoke_status', 'reference_smoke_decision',
        'fresh_base_data_acquisition', 'loader_contract_probe',
        'reference_protocol_probe', 'reference_smoke_probe',
    ]:
        value = snapshot.get(key)
        if value in (None, '', [], {}) and key not in full_state:
            continue
        if full_state.get(key) != value:
            full_state[key] = value
            changed = True
    if changed:
        full_state['updated_at'] = now_iso()
        save_json(full_path, full_state)
        return ['synced_fresh_base_gate_state']
    return []

def run_step(name: str, cmd: list[str], *, timeout: int = 240) -> dict[str, Any]:
    started = now_iso()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        return {'name': name, 'command': cmd, 'started_at': started, 'finished_at': now_iso(), 'return_code': proc.returncode, 'timed_out': False, 'stdout_tail': (proc.stdout or '')[-3000:], 'stderr_tail': (proc.stderr or '')[-3000:]}
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        return {'name': name, 'command': cmd, 'started_at': started, 'finished_at': now_iso(), 'return_code': 124, 'timed_out': True, 'stdout_tail': (stdout or '')[-3000:], 'stderr_tail': ((stderr or '') + f'\nTIMEOUT after {timeout}s')[-3000:]}


def current_literature_run_id(paths) -> str:
    find_progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    if isinstance(find_progress, dict) and str(find_progress.get('run_id') or '').strip():
        return str(find_progress.get('run_id') or '').strip()
    find_results = load_json(paths.planning / 'finding' / 'find_results.json', {})
    return str(find_results.get('run_id') or '') if isinstance(find_results, dict) else ''



def title_key_for_current_find(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_recommended_title_keys(paths_or_root) -> set[str]:
    payload = load_json(paths_or_root.planning / "finding" / "find_results.json", {})
    if not isinstance(payload, dict):
        return set()
    keys: set[str] = set()
    for pool in ["articles", "strong_recommendations"]:
        rows = payload.get(pool)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                key = title_key_for_current_find(row.get("title") or row.get("paper_title"))
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
    audit = load_json(root / "state" / "fresh_base_reference_reproduction_audit.json", {})
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
    gate = load_json(root / "state" / "base_switch_gate.json", {})
    execution = load_json(root / "state" / "base_switch_execution.json", {})
    candidate = gate.get("candidate_route") if isinstance(gate, dict) and isinstance(gate.get("candidate_route"), dict) else {}
    candidate_repo = str(candidate.get("repo_path") or "").strip()
    return bool(
        selected_repo
        and candidate_repo
        and selected_repo == candidate_repo
        and isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )

def current_environment_base_selection(paths) -> dict[str, Any]:
    current_run = current_literature_run_id(paths)
    viability = selected_base_viability_current_selection(paths, current_run)
    if viability:
        return viability

    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if not isinstance(selection, dict):
        return {'valid': False, 'current_find_run_id': current_run, 'reason': 'missing_evidence_ready_repo_selection'}
    selected = selection.get('selected', {}) if isinstance(selection.get('selected'), dict) else {}
    selected_run = str(selected.get('fresh_find_run_id') or selection.get('fresh_find_run_id') or '')
    stage = str(selection.get('selection_stage') or selection.get('selected_by_stage') or selected.get('selection_stage') or '')
    decision = selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
    raw_selection_gate = str(selection.get('selection_gate') or selected.get('selection_gate') or '').strip()
    accepted = bool(selection.get('accepted_by_claude') or raw_selection_gate.startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate')) or decision.get('accept_as_current_best'))
    public_selection_gate = raw_selection_gate
    if accepted and not raw_selection_gate.startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate')):
        public_selection_gate = 'accepted_by_claude_topic_fit'
    in_current_find = selected_title_in_current_find(paths, selected, decision)
    valid = bool(selected and stage == 'environment_claude_code' and accepted and in_current_find)
    return {'valid': valid, 'current_find_run_id': current_run, 'fresh_find_run_id': selected_run, 'selection_stage': stage, 'accepted_by_claude': accepted, 'selected': selected, 'selection_gate': public_selection_gate, 'raw_selection_gate': raw_selection_gate, 'reason': 'current_environment_base_selected' if valid else ('selected_base_not_in_current_find_recommendations' if not in_current_find else 'environment_base_selection_pending_or_stale')}


def selected_base_label(env_selection: dict[str, Any]) -> str:
    if not isinstance(env_selection, dict) or not env_selection.get('valid'):
        reason = str((env_selection or {}).get('reason') or '') if isinstance(env_selection, dict) else ''
        if reason == 'selected_base_not_in_current_find_recommendations':
            return '当前基底待环境阶段重新选择（旧选择不在当前 Find 推荐中）'
        return '当前基底待环境阶段 Claude Code 选择'
    selected = env_selection.get('selected', {}) if isinstance(env_selection.get('selected'), dict) else {}
    return str(selected.get('title') or selected.get('literature_base_title') or selected.get('selected_base_title') or selected.get('name') or selected.get('repo') or '当前基底待环境阶段 Claude Code 选择')


def refresh_gates(project: str, venue: str) -> list[dict[str, Any]]:
    py = sys.executable
    return [
        run_step('audit_reference_reproduction', [py, str(ROOT / 'scripts' / 'audit_reference_reproduction.py'), '--project', project, '--venue', venue], timeout=180),
        run_step('build_blocker_action_plan', [py, str(ROOT / 'scripts' / 'build_blocker_action_plan.py'), '--project', project, '--venue', venue], timeout=180),
        run_step('audit_framework_content_coupling', [py, str(ROOT / 'scripts' / 'audit_framework_content_coupling.py'), '--project', project], timeout=180),
        run_step('audit_obsolete_baseline_cleanup', [py, str(ROOT / 'scripts' / 'audit_obsolete_baseline_cleanup.py'), '--project', project], timeout=180),
    ]


def literature_gate_shortfall(paths) -> dict[str, Any]:
    progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    progress = progress if isinstance(progress, dict) else {}
    current_run = str(progress.get('run_id') or '').strip()
    progress_blocked_llm, blocked_reason = _find_progress_llm_blocker(progress)
    if current_run:
        target = progress.get('recommendation_target_count') or 20
        strong = progress.get('strong_recommendation_count') or 0
        shortfall = progress.get('recommendation_shortfall')
        if shortfall in (None, ''):
            try:
                shortfall = max(0, int(target) - int(strong))
            except Exception:
                shortfall = 0
        try:
            shortfall_int = int(shortfall or 0)
        except Exception:
            shortfall_int = 0
        if progress_blocked_llm:
            reason = blocked_reason or 'LLM API/configuration unavailable'
            action = f'Current Find {current_run} is blocked before mandatory LLM abstract scoring: {reason}. Strong recommendations remain {strong}/{target}; shortfall={shortfall_int}. Repair the LLM API configuration/quota first, then rerun complete Find from the TASTE entrypoint. Do not run targeted Find, experiments, base promotion, paper repair, or claim promotion while LLM scoring is unavailable.'
            return {
                'blocked': True,
                'llm_blocked': True,
                'route': 'literature_llm_quota_exhausted',
                'top_action': action,
                'strong': strong,
                'target': target,
                'shortfall': shortfall_int,
                'gate_status': 'blocked_llm_quota_exhausted',
                'packet_status': '',
                'run_id': current_run,
                'blocked_reason': reason,
            }
    packet = load_json(paths.state / 'literature_tool_packet.json', {})
    if current_run and isinstance(packet, dict):
        packet_run = str(packet.get('run_id') or packet.get('source_run_id') or packet.get('find_run_id') or '').strip()
        if packet_run and packet_run != current_run:
            packet = {}
    blocker_plan = load_json(paths.state / 'blocker_action_plan.json', {})
    summary = packet.get('summary', {}) if isinstance(packet, dict) and isinstance(packet.get('summary'), dict) else {}
    blocker_summary = blocker_plan.get('summary', {}) if isinstance(blocker_plan, dict) and isinstance(blocker_plan.get('summary'), dict) else {}
    route = str(blocker_plan.get('top_route') or blocker_summary.get('top_route') or '') if isinstance(blocker_plan, dict) else ''
    action = str(blocker_plan.get('top_action') or blocker_summary.get('top_action') or '') if isinstance(blocker_plan, dict) else ''
    target = summary.get('recommendation_target_count') or progress.get('recommendation_target_count')
    strong = summary.get('strong_paper_anchors') or summary.get('strong_recommendations') or progress.get('strong_recommendation_count')
    shortfall = summary.get('recommendation_shortfall')
    if shortfall in (None, ''):
        shortfall = progress.get('recommendation_shortfall')
    try:
        shortfall_int = int(shortfall or 0)
    except Exception:
        shortfall_int = 0
    gate_status = str(summary.get('recommendation_gate_status') or progress.get('status') or '').lower()
    route_is_literature = route == 'literature_recommendation_gate' or 'strong recommendation' in action.lower()
    blocked = shortfall_int > 0 or gate_status == 'shortfall' or route_is_literature
    return {'blocked': blocked, 'llm_blocked': False, 'route': route, 'top_action': action, 'strong': strong, 'target': target, 'shortfall': shortfall_int, 'gate_status': gate_status, 'packet_status': packet.get('status') if isinstance(packet, dict) else '', 'run_id': current_run}


def current_find_selection(project: str) -> dict[str, Any]:
    paths = build_paths(project)
    find_results = load_json(paths.planning / 'finding' / 'find_results.json', {})
    if isinstance(find_results, dict) and isinstance(find_results.get('selection'), dict):
        return dict(find_results['selection'])
    return {}


def ensure_project_config(project: str, venue: str) -> list[str]:
    repairs: list[str] = []
    selection = project_source_selection(project)
    current_venue = project_target_venue(project, 'ICLR').upper()
    patch: dict[str, Any] = {}
    if current_venue != venue:
        patch.update({'target_venue': venue, 'venue': venue})
    if not selection.get('venue_ids'):
        find_selection = current_find_selection(project)
        if find_selection:
            patch['default_find_selection'] = find_selection
            repairs.append('initialized_project_find_selection_from_existing_find')
    if patch:
        update_project_settings(project, patch)
        if current_venue != venue:
            repairs.append('synced_project_target_venue')
    return repairs


def launch_project_full_cycle(paths, project: str, venue: str) -> dict[str, Any] | None:
    for row in process_rows():
        if 'run_full_research_cycle.py' in row.get('cmd', '') and project in row.get('cmd', ''):
            return {'status': 'running', 'already_running': True, 'pid': row.get('pid'), 'cmd': row.get('cmd'), 'kind': 'full_cycle'}
    child_row = active_project_worker_row(project)
    if child_row is not None:
        return {'status': 'running', 'already_running': True, 'controller_missing': True, 'pid': child_row.get('pid'), 'cmd': child_row.get('cmd'), 'kind': 'active_child_worker'}
    state_path = paths.state / 'full_cycle_job.json'
    existing = load_json(state_path, {})
    if isinstance(existing, dict) and existing.get('status') == 'running' and pid_alive(existing.get('pid')):
        return {**existing, 'already_running': True}
    log_dir = paths.logs / 'supervision'
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')
    log_path = log_dir / f'full_research_cycle_{stamp}.log'
    cmd = [sys.executable, str(ROOT / 'scripts' / 'run_full_research_cycle.py'), '--project', project, '--venue', venue, '--max-cycles', '3', '--iterations-per-cycle', '1', '--trajectory-rounds', '1', '--max-launches', '1', '--use-existing-literature-packet']
    with log_path.open('ab') as handle:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    payload = {'project': project, 'venue': venue, 'status': 'running', 'pid': proc.pid, 'command': cmd, 'log_path': str(log_path), 'started_at': now_iso(), 'guardrail': 'Continue the single full research pipeline from the validated current Find packet; no second Find and no legacy main-route fallback.'}
    save_json(state_path, payload)
    append_agent_log(project, 'main', f'TASTE supervision launched full research cycle PID={proc.pid}')
    return payload


def append_work_status(project: str, payload: dict[str, Any]) -> None:
    append_supervision_status(project, payload)


def generic_supervision_tick(project: str, venue: str, *, supervise: bool) -> dict[str, Any]:
    paths = build_paths(project)
    repairs = ensure_project_config(project, venue)
    jobs_payload, jobs_error = http_json('http://127.0.0.1:8765/api/jobs?compact=1&limit=20', timeout=30, retries=1)
    compact_payload, compact_error = http_json(f'http://127.0.0.1:8765/api/projects/{project}?compact=1', timeout=60, retries=2)
    full = load_json(paths.state / 'full_research_cycle.json', {})
    find_plan = load_json(paths.state / 'current_find_research_plan.json', {})
    env_selection = current_environment_base_selection(paths)
    compact_full = compact_payload.get('full_research_cycle', {}) if isinstance(compact_payload, dict) else {}
    human = compact_payload.get('human_supervision', {}) if isinstance(compact_payload, dict) else {}
    status = str(compact_full.get('status') or human.get('status') or full.get('status') or 'not_started') if isinstance(compact_full, dict) else str(human.get('status') or full.get('status') or 'not_started')
    action = 'refresh_project_state'
    action_rc: int | str = 0
    steps: list[dict[str, Any]] = []
    project_root_text = str(paths.root)
    processes = [
        row for row in process_rows()
        if project in str(row.get('cmd', ''))
        or project_root_text in str(row.get('cmd', ''))
        or project_root_text in str(row.get('cwd', ''))
    ]
    full_cycle_job = normalize_full_cycle_job(project, load_json(paths.state / 'full_cycle_job.json', {}))
    lit_gate = literature_gate_shortfall(paths)
    claude_state = audit_current_find_claude_state(paths, current_literature_run_id(paths))
    repairs.extend(claude_state.get('repairs', []))
    if supervise:
        if full_cycle_job.get('status') == 'running' and full_cycle_job.get('process_alive') is True:
            action = 'supervise_running_full_research_cycle'
            status = 'running'
            steps.extend(refresh_gates(project, venue))
            action_rc = 0 if all(step.get('return_code') in SAFE_BLOCKED_RC for step in steps) else 2
        elif current_literature_run_id(paths) and isinstance(find_plan, dict) and str(find_plan.get('run_id') or '') == current_literature_run_id(paths):
            steps.extend(refresh_gates(project, venue))
            lit_gate = literature_gate_shortfall(paths)
            action_rc = 0 if all(step.get('return_code') in SAFE_BLOCKED_RC for step in steps) else 2
            if lit_gate.get('blocked'):
                action = 'blocked_literature_recommendation_gate_refresh'
                status = 'blocked_literature_recommendation_gate'
                full_cycle_job = normalize_full_cycle_job(project, load_json(paths.state / 'full_cycle_job.json', {}))
            else:
                launched = launch_project_full_cycle(paths, project, venue)
                full_cycle_job = normalize_full_cycle_job(project, launched or load_json(paths.state / 'full_cycle_job.json', {}))
                action = 'launch_or_supervise_full_research_cycle'
                status = 'running' if full_cycle_job.get('status') == 'running' else 'continuing_full_research_cycle'
        else:
            action = 'blocked_missing_current_find_packet_or_current_plan'
            action_rc = 2
    elif full_cycle_job.get('status') == 'running':
        status = 'running'
    elif status in {'', 'not_started'}:
        action = 'observe_not_started'
    processes = [
        row for row in process_rows()
        if project in str(row.get('cmd', ''))
        or project_root_text in str(row.get('cmd', ''))
        or project_root_text in str(row.get('cwd', ''))
    ]
    full_cycle_job = normalize_full_cycle_job(project, full_cycle_job or load_json(paths.state / 'full_cycle_job.json', {}))
    repairs.extend(sync_full_cycle_job_state(paths, full_cycle_job))
    if full_cycle_job.get('status') == 'stale' and status == 'running':
        status = 'stale_full_research_cycle_snapshot'
        action = 'refresh_project_state'
        action_rc = 0
    fresh_gate = fresh_base_gate_snapshot(paths)
    repairs.extend(sync_fresh_base_gate_state(paths, fresh_gate))
    selected_label = selected_base_label(env_selection)
    env_selection_for_payload = dict(env_selection)
    base_selection_status = 'selected' if env_selection.get('valid') else 'waiting_for_environment_claude_code'
    if lit_gate.get('blocked'):
        env_selection_for_payload = {
            **env_selection_for_payload,
            'valid': False,
            'blocked_by': 'literature_llm_quota_exhausted' if lit_gate.get('llm_blocked') else 'literature_recommendation_shortfall',
            'base_selection_status': 'blocked_by_literature_gate',
            'blocked_selection': env_selection.get('selected', {}) if isinstance(env_selection.get('selected'), dict) else {},
            'reason': 'current Find is blocked before mandatory LLM abstract scoring; environment base selection is audit-only until LLM scoring succeeds' if lit_gate.get('llm_blocked') else 'current Find strong-recommendation gate is short; environment base selection is audit-only until the gate passes',
        }
        base_selection_status = 'blocked_by_literature_gate'
        selected_label = 'LLM 摘要打分未通过，环境基底选择暂不生效' if lit_gate.get('llm_blocked') else 'Find 推荐门控未过，环境基底选择暂不生效'
    blocker = human.get('blocker') if isinstance(human.get('blocker'), dict) else {}
    packet_route = human.get('main_route') if isinstance(human.get('main_route'), dict) else {}
    blocker_plan = load_json(paths.state / 'blocker_action_plan.json', {})
    blocker_summary = blocker_plan.get('summary', {}) if isinstance(blocker_plan.get('summary'), dict) else {}
    blocker_status = str(blocker_plan.get('status') or '') if isinstance(blocker_plan, dict) else ''
    top_route = str(blocker_plan.get('top_route') or blocker_summary.get('top_route') or '') if isinstance(blocker_plan, dict) else ''
    top_action = public_action_text(top_route, blocker_plan.get('top_action') or blocker_summary.get('top_action') or '') if isinstance(blocker_plan, dict) else ''
    observations: list[dict[str, Any]] = []
    if claude_state.get('takeover_stale'):
        observations.append({'code': 'stale_current_find_claude_takeover', 'severity': 'audit_history', 'message': '旧 Claude takeover 早于当前 Find 严格重分类，只能作为审计历史，不能作为当前理解。'})
    if claude_state.get('reading_validation_stale'):
        observations.append({'code': 'stale_current_find_claude_reading_validation', 'severity': 'audit_history', 'message': '旧 Claude reading validation 早于当前 Find 严格重分类，当前规划必须重新验证或保持 blocked。'})

    if full_cycle_job.get('status') == 'running' and full_cycle_job.get('process_alive') is True:
        pid = full_cycle_job.get('pid', '')
        elapsed = full_cycle_job.get('elapsed_sec') or full_cycle_job.get('etimes') or ''
        stage_text = str(full_cycle_job.get('stage') or (full.get('latest_step') or {}).get('stage') if isinstance(full.get('latest_step'), dict) else full_cycle_job.get('stage') or '')
        stage_summary = stage_action_text(stage_text)
        next_action = f'当前完整科研循环 worker 正在运行，PID={pid}'
        if elapsed != '':
            next_action += f'，已运行 {elapsed}s'
        next_action += f'；{stage_summary} 继续监督实时日志、训练指标、门控和网页状态，不启动重复 full-cycle。'
        top_route = 'active_full_research_cycle_worker'
        top_action = f'当前完整科研循环 worker 正在运行，PID={pid}；{stage_summary}'
        blocker_status = 'running'
        observations.append({'code': 'active_full_research_cycle_worker', 'severity': 'running', 'message': top_action})
    else:
        if lit_gate.get('blocked'):
            strong = lit_gate.get('strong') if lit_gate.get('strong') not in (None, '') else '?'
            target = lit_gate.get('target') if lit_gate.get('target') not in (None, '') else '?'
            shortfall = lit_gate.get('shortfall') if lit_gate.get('shortfall') not in (None, '') else '?'
            if lit_gate.get('llm_blocked'):
                next_action = 'LLM API 额度/配置不可用；请在网页保存可用 API key/base/model 后重新启动完整 Find。恢复前不启动实验、论文或 claim promotion。'
            else:
                next_action = f'当前 Find 推荐文章 {strong}/{target}，短缺 {shortfall}；修复标题+摘要评分或通过 TASTE 统一 literature tool 补检索并刷新 packet。短缺未清零前，不启动实验、论文或 claim promotion。'
            top_route = lit_gate.get('route') or top_route or 'literature_recommendation_gate'
            top_action = lit_gate.get('top_action') or top_action or next_action
            blocker_status = blocker_status or 'blocked'
            observations.append({'code': 'literature_recommendation_gate_active', 'severity': 'blocked_gate', 'message': top_action})
        else:
            next_action = str(blocker.get('next_action') or '继续按当前项目配置、当前 Find 产物和当前环境阶段选择推进 TASTE 科研闭环。')
            if blocker_status == 'blocked':
                observations.append({'code': 'blocker_action_plan_active', 'severity': 'blocked_gate', 'message': f'{top_route}: {top_action}'})

    payload = {
        'schema_version': 2,
        'generated_at': now_iso(),
        'project': project,
        'target_venue': venue,
        'status': status or 'not_started',
        'action': action,
        'action_rc': action_rc,
        'find_run_id': current_literature_run_id(paths),
        'main_base': selected_label,
        'compact_status': status,
        'blocker_category': 'literature_llm_quota_exhausted' if lit_gate.get('llm_blocked') else 'literature_recommendation_shortfall' if lit_gate.get('blocked') else str(blocker.get('category') or ''),
        'packet_counts': {'readings': int(packet_route.get('readings') or 0), 'ideas': int(packet_route.get('ideas') or 0), 'plans': int(packet_route.get('plans') or 0)},
        'data_status': fresh_gate.get('data_status', ''),
        'data_decision': fresh_gate.get('data_decision', ''),
        'loader_status': fresh_gate.get('loader_status', ''),
        'loader_decision': fresh_gate.get('loader_decision', ''),
        'ready_datasets': fresh_gate.get('ready_datasets', []),
        'blocked_datasets': fresh_gate.get('blocked_datasets', []),
        'reference_protocol_status': fresh_gate.get('reference_protocol_status', ''),
        'reference_protocol_decision': fresh_gate.get('reference_protocol_decision', ''),
        'reference_smoke_status': fresh_gate.get('reference_smoke_status', ''),
        'reference_smoke_decision': fresh_gate.get('reference_smoke_decision', ''),
        'full_cycle_job': full_cycle_job,
        'environment_base_selection': env_selection_for_payload,
        'base_selection_status': base_selection_status,
        'claude_current_find_state': {key: value for key, value in claude_state.items() if key != 'repairs'},
        'api': {'jobs_error': jobs_error, 'compact_error': compact_error, 'jobs_count': len(jobs_payload) if isinstance(jobs_payload, list) else 0},
        'issues': ([{'code': 'jobs_api_unavailable', 'severity': 'repair', 'message': compact_text(jobs_error)}] if jobs_error else []) + ([{'code': 'compact_api_unavailable', 'severity': 'repair', 'message': compact_text(compact_error)}] if compact_error else []),
        'observations': observations,
        'blocker_plan_status': blocker_status,
        'top_route': top_route,
        'top_action': top_action,
        'repairs': repairs,
        'steps': steps,
        'literature': literature_snapshot(paths),
        'next_action': next_action,
        'processes': processes,
        'guardrails': ['project-specific config only', 'current Find packet only; no second Find unless explicitly requested', 'Find prepares candidates only; anchor/base selection happens in environment-stage Claude Code', 'repo-specific adapters run only after current-run environment selection', 'no paper writing or claim promotion before project gates pass'],
    }
    save_json(paths.state / 'supervision_tick.json', payload)
    append_work_status(project, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description='One generic TASTE supervision tick for the current project route.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--download-timeout-sec', type=int, default=120)
    parser.add_argument('--supervise', action='store_true')
    args = parser.parse_args()
    venue = (args.venue or project_target_venue(args.project, 'ICLR') or 'ICLR').upper()
    payload = generic_supervision_tick(args.project, venue, supervise=args.supervise)
    print(json.dumps({'status': payload['status'], 'action': payload['action'], 'action_rc': payload['action_rc'], 'compact_status': payload.get('compact_status', ''), 'blocker': payload.get('blocker_category', ''), 'full_cycle_job': payload.get('full_cycle_job', {}), 'environment_base_selection': payload.get('base_selection_status', ''), 'main_base': payload.get('main_base', ''), 'issues': len(payload.get('issues', [])), 'repairs': payload.get('repairs', [])}, ensure_ascii=False, indent=2))
    if payload.get('action_rc') not in ('', 0, 2):
        return int(payload['action_rc'])
    return 0 if not payload.get('issues') else 2


if __name__ == '__main__':
    raise SystemExit(main())
