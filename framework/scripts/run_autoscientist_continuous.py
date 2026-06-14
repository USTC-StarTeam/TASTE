#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from project_paths import ROOT, build_paths
from pipeline_guard import guard_fresh_base_blocker_entry


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(cmd: list[str], timeout: int, env: dict[str, str]) -> dict:
    started = dt.datetime.now(dt.timezone.utc)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, env=env)
        rc = proc.returncode
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
    except subprocess.TimeoutExpired as exc:
        rc = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        stderr = (stderr or '') + f'\nTIMEOUT after {timeout}s'
    finished = dt.datetime.now(dt.timezone.utc)
    return {
        'command': ' '.join(cmd),
        'return_code': rc,
        'started_at': started.isoformat(),
        'finished_at': finished.isoformat(),
        'duration_sec': round((finished - started).total_seconds(), 3),
        'stdout_tail': stdout[-2500:],
        'stderr_tail': stderr[-2500:],
    }


def build_env(use_llm: bool, live_llm: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault('ARXIV_TIMEOUT_SEC', '15')
    env.setdefault('GITHUB_TIMEOUT_SEC', '15')
    env.setdefault('DISCOVER_ARXIV_TIMEOUT_SEC', '30')
    env.setdefault('DISCOVER_GITHUB_TIMEOUT_SEC', '30')
    env.setdefault('AUTOSCIENTIST_PLAN_TIMEOUT_SEC', '30')
    env.setdefault('LLM_TIMEOUT_SEC', '25')
    env.setdefault('LLM_RETRIES', '1')
    if not use_llm:
        env['LLM_ENABLED'] = '0'
    elif not live_llm:
        # Allow configuration checks but avoid blocking core evidence maintenance on live API calls.
        env.setdefault('LLM_ENABLED', env.get('LLM_ENABLED', '1'))
    return env


def collect_status(paths) -> dict:
    packet = load_json(paths.state / 'blocker_resolution_packet.json', {})
    policy = load_json(paths.state / 'data_unavailability_policy.json', {})
    pool = load_json(paths.state / 'repo_candidate_pool_audit.json', {})
    paper_audit = (paths.reports / 'paper_evidence_audit.md').read_text(encoding='utf-8', errors='ignore') if (paths.reports / 'paper_evidence_audit.md').exists() else ''
    llm = load_json(paths.reports / 'llm_readiness.json', {})
    status = load_json(paths.state / 'pipeline_runnability.json', {})
    blocker_type = str(packet.get('blocker_type', '')) if isinstance(packet, dict) else ''
    paper_gate = str(packet.get('paper_gate_summary', '')) if isinstance(packet, dict) else ''
    evidence_ready = blocker_type in {'', 'none'} and paper_gate != 'hold-markdown-only'
    safety_issues = []
    if blocker_type not in {'', 'none'} and paper_gate != 'hold-markdown-only':
        safety_issues.append('paper gate is not holding despite an active blocker')
    if 'synthetic smoke runs cannot support final paper claims' not in paper_audit and blocker_type not in {'', 'none'}:
        safety_issues.append('paper audit does not explicitly reject synthetic final evidence while blocked')
    return {
        'blocker_type': blocker_type or 'unknown',
        'data_policy_decision': policy.get('decision', '') if isinstance(policy, dict) else '',
        'paper_gate_summary': paper_gate or 'unknown',
        'evidence_ready_candidate_count': pool.get('evidence_ready_count', packet.get('evidence_ready_candidate_count', 0)) if isinstance(pool, dict) else 0,
        'real_evidence_ready': evidence_ready,
        'llm_configured': bool(llm.get('configured')) if isinstance(llm, dict) else False,
        'llm_live_ok': bool(llm.get('live_ok')) if isinstance(llm, dict) else False,
        'pipeline_issue_count': status.get('issue_count', 0) if isinstance(status, dict) else 0,
        'safety_issues': safety_issues,
    }


def write_report(paths, payload: dict) -> None:
    latest = payload.get('latest', {}) if isinstance(payload, dict) else {}
    lines = ['# AutoScientist Continuous Runner\n\n']
    lines.append(f"- project: {payload.get('project', '')}\n")
    lines.append(f"- venue: {payload.get('venue', '')}\n")
    lines.append(f"- started_at: {payload.get('started_at', '')}\n")
    lines.append(f"- updated_at: {payload.get('updated_at', '')}\n")
    lines.append(f"- cycles_completed: {len(payload.get('cycles', []))}\n")
    lines.append(f"- stop_reason: {payload.get('stop_reason', '')}\n")
    lines.append(f"- real_evidence_ready: {latest.get('real_evidence_ready', False)}\n")
    lines.append(f"- blocker_type: {latest.get('blocker_type', '')}\n")
    lines.append(f"- data_policy_decision: {latest.get('data_policy_decision', '')}\n")
    lines.append(f"- paper_gate_summary: {latest.get('paper_gate_summary', '')}\n")
    lines.append(f"- evidence_ready_candidate_count: {latest.get('evidence_ready_candidate_count', 0)}\n")
    lines.append(f"- llm_configured: {latest.get('llm_configured', False)}\n")
    lines.append(f"- llm_live_ok: {latest.get('llm_live_ok', False)}\n")
    lines.append(f"- pipeline_issue_count: {latest.get('pipeline_issue_count', 0)}\n")
    stagnation = payload.get('cycles', [])[-1].get('stagnation', {}) if payload.get('cycles') else {}
    lines.append(f"- stagnated: {stagnation.get('stagnated', False) if isinstance(stagnation, dict) else False}\n")
    lines.append(f"- query_coverage: {stagnation.get('query_coverage', '') if isinstance(stagnation, dict) else ''}\n")
    lines.append(f"- candidate_coverage: {stagnation.get('candidate_coverage', '') if isinstance(stagnation, dict) else ''}\n")
    if latest.get('safety_issues'):
        lines.append('\n## Safety Issues\n')
        for issue in latest.get('safety_issues', []):
            lines.append(f'- {issue}\n')
    lines.append('\n## Recent Cycles\n')
    for cycle in payload.get('cycles', [])[-8:]:
        status = cycle.get('status', {})
        supervisor = cycle.get('supervisor', {})
        lines.append(
            f"- cycle={cycle.get('cycle_index')} rc={supervisor.get('return_code')} duration={supervisor.get('duration_sec', '')}s "
            f"ready={status.get('real_evidence_ready')} blocker={status.get('blocker_type')} decision={status.get('data_policy_decision')}\n"
        )
    lines.append('\n## Guardrail\n')
    lines.append('- This runner supervises AutoScientist only. It must not invent data, results, claims, or paper acceptance. Scientific completion requires auditable real-data experiments and evidence-backed paper gates.\n')
    (paths.reports / 'autoscientist_continuous.md').write_text(''.join(lines), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Continuously supervise AutoScientist with evidence gates and bounded cycle budgets.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--max-cycles', type=int, default=3)
    parser.add_argument('--sleep-sec', type=float, default=0.0)
    parser.add_argument('--cycle-timeout-sec', type=int, default=240)
    parser.add_argument('--command-timeout-sec', type=int, default=60)
    parser.add_argument('--audit-limit', type=int, default=2)
    parser.add_argument('--use-llm', action='store_true')
    parser.add_argument('--live-llm-check', action='store_true', help='Run check_llm_ready.py --live each cycle; may block on unstable providers.')
    parser.add_argument('--allow-restart-discovery', action='store_true')
    parser.add_argument('--allow-data-attempt', action='store_true')
    parser.add_argument('--stop-on-evidence-ready', action='store_true')
    args = parser.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paths = build_paths(args.project)
    env = build_env(args.use_llm, args.live_llm_check)
    state_path = paths.state / 'autoscientist_continuous.json'
    payload = load_json(state_path, {'project': args.project, 'venue': args.venue, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'cycles': []})
    payload['project'] = args.project
    payload['venue'] = args.venue
    payload.setdefault('started_at', dt.datetime.now(dt.timezone.utc).isoformat())
    payload.setdefault('cycles', [])
    payload['stop_reason'] = 'running'
    payload['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(state_path, payload)
    write_report(paths, payload)

    for _ in range(max(1, args.max_cycles)):
        cycle_index = len(payload['cycles']) + 1
        cycle = {'cycle_index': cycle_index, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'commands': []}
        if args.use_llm:
            llm_cmd = [sys.executable, 'framework/scripts/check_llm_ready.py', '--project', args.project]
            if args.live_llm_check:
                llm_cmd.append('--live')
            cycle['commands'].append({'label': 'check_llm_ready', **run(llm_cmd, timeout=min(60, args.command_timeout_sec), env=env)})
        supervisor_cmd = [
            sys.executable, 'framework/scripts/run_autoscientist_supervisor.py',
            '--project', args.project,
            '--venue', args.venue,
            '--cycles', '1',
            '--fast-cycle',
            '--timeout-sec', str(args.cycle_timeout_sec),
            '--command-timeout-sec', str(args.command_timeout_sec),
            '--audit-limit', str(args.audit_limit),
            '--stop-on-blocker',
        ]
        if args.use_llm:
            supervisor_cmd.append('--use-llm')
        if args.allow_restart_discovery:
            supervisor_cmd.append('--allow-restart-discovery')
        if args.allow_data_attempt:
            supervisor_cmd.append('--allow-data-attempt')
        supervisor = run(supervisor_cmd, timeout=args.cycle_timeout_sec + 30, env=env)
        cycle['supervisor'] = supervisor
        cycle['commands'].append({'label': 'run_autoscientist_supervisor', **supervisor})
        stagnation_cmd = [sys.executable, 'framework/scripts/build_stagnation_report.py', '--project', args.project, '--venue', args.venue]
        cycle['commands'].append({'label': 'build_stagnation_report', **run(stagnation_cmd, timeout=min(60, args.command_timeout_sec), env=env)})
        cycle['status'] = collect_status(paths)
        cycle['stagnation'] = load_json(paths.state / 'stagnation_report.json', {})
        cycle['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        payload['cycles'].append(cycle)
        payload['latest'] = cycle['status']
        payload['updated_at'] = cycle['finished_at']
        if cycle['status'].get('safety_issues'):
            payload['stop_reason'] = 'safety_issue_detected'
            save_json(state_path, payload)
            write_report(paths, payload)
            break
        if args.stop_on_evidence_ready and cycle['status'].get('real_evidence_ready'):
            payload['stop_reason'] = 'real_evidence_ready'
            save_json(state_path, payload)
            write_report(paths, payload)
            break
        payload['stop_reason'] = 'cycle_budget_exhausted'
        save_json(state_path, payload)
        write_report(paths, payload)
        if args.sleep_sec > 0:
            time.sleep(args.sleep_sec)

    payload['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(state_path, payload)
    write_report(paths, payload)
    print(paths.reports / 'autoscientist_continuous.md')


if __name__ == '__main__':
    main()
