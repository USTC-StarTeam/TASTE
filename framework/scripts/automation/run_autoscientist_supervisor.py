#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from project.project_paths import ROOT, build_paths
from orchestration.commands import module_command as module_cmd
from policies.pipeline_guard import guard_fresh_base_blocker_entry
from runtime.framework_io import read_json as load_json
from runtime.framework_io import write_json_raw as save_json
from runtime.taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)


def run(cmd: list[str], timeout: int, env: dict[str, str] | None = None, label: str = '') -> dict:
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
        stderr += f'\nTIMEOUT after {timeout}s'
    finished = dt.datetime.now(dt.timezone.utc)
    return {
        'label': label or (Path(cmd[1]).name if len(cmd) > 1 else 'command'),
        'command': ' '.join(cmd),
        'return_code': rc,
        'started_at': started.isoformat(),
        'finished_at': finished.isoformat(),
        'duration_sec': round((finished - started).total_seconds(), 3),
        'stdout_tail': stdout[-2500:],
        'stderr_tail': stderr[-2500:],
    }


def real_evidence_ready(paths) -> bool:
    packet = load_json(paths.state / 'blocker_resolution_packet.json', {})
    return packet.get('blocker_type') in {'none', ''} and packet.get('paper_gate_summary') != 'hold-markdown-only'


def latest_decision(paths) -> str:
    packet = load_json(paths.state / 'blocker_resolution_packet.json', {})
    policy = load_json(paths.state / 'data_unavailability_policy.json', {})
    return str(packet.get('data_policy_decision') or policy.get('decision', ''))


def active_repo_path(paths) -> str:
    active = load_json(paths.state / 'active_repo.json', {})
    if isinstance(active, dict) and active.get('repo_path'):
        return str(active.get('repo_path'))
    req = load_json(paths.state / 'repo_data_requirements.json', {})
    if isinstance(req, dict) and req.get('repo_path'):
        return str(req.get('repo_path'))
    return ''


def append_command(record: dict, cmd: list[str], timeout: int, env: dict[str, str], label: str, hard_deadline: dt.datetime | None = None) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    if hard_deadline is not None:
        remaining = int((hard_deadline - now).total_seconds())
        if remaining <= 2:
            skipped = {
                'label': label,
                'command': ' '.join(cmd),
                'return_code': 125,
                'started_at': now.isoformat(),
                'finished_at': now.isoformat(),
                'duration_sec': 0,
                'stdout_tail': '',
                'stderr_tail': 'SKIPPED: supervisor total budget exhausted',
            }
            record['commands'].append(skipped)
            return skipped
        timeout = min(timeout, max(1, remaining))
    result = run(cmd, timeout=timeout, env=env, label=label)
    record['commands'].append(result)
    return result


def base_env(use_llm: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault('ARXIV_TIMEOUT_SEC', '20')
    env.setdefault('DISCOVER_ARXIV_TIMEOUT_SEC', '45')
    env.setdefault('DISCOVER_GITHUB_TIMEOUT_SEC', '45')
    env.setdefault('GITHUB_TIMEOUT_SEC', '30')
    env.setdefault('AUTOSCIENTIST_PLAN_TIMEOUT_SEC', '45')
    env.setdefault('LLM_TIMEOUT_SEC', '60')
    env.setdefault('REPO_CLONE_TIMEOUT_SEC', '45')
    if not use_llm:
        env['LLM_ENABLED'] = '0'
    return env


def fast_supervisor_step(project: str, venue: str, use_llm: bool, total_timeout_sec: int, command_timeout_sec: int, audit_limit: int, allow_data_attempt: bool, allow_restart_discovery: bool) -> dict:
    env = base_env(use_llm)
    paths = build_paths(project)
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(30, total_timeout_sec))
    record: dict = {
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': project,
        'venue': venue,
        'mode': 'fast_cycle',
        'commands': [],
        'guardrail': 'Fast supervisor mode performs bounded evidence maintenance only; it must not create paper claims or treat synthetic smoke results as final evidence.',
    }
    append_command(
        record,
        module_cmd('environment', 'deploy_from_plan', '--project', project),
        command_timeout_sec,
        env,
        'environment_deploy_from_selected_plan',
        deadline,
    )
    maintenance = [
        ('planning_blocker_resolution', module_cmd('planning', 'blocker_resolution', '--project', project, '--venue', venue)),
        ('audit_paper_evidence', module_cmd('writing', 'audit_evidence', '--project', project, '--venue', venue)),
        ('research_manifest', [sys.executable, str(SCRIPTS / 'research_manifest.py'), '--project', project, '--venue', venue]),
        ('report_status', [sys.executable, str(SCRIPTS / 'report_status.py'), '--project', project, '--venue', venue]),
        ('generate_handoff', [sys.executable, str(SCRIPTS / 'generate_handoff.py'), '--project', project, '--venue', venue]),
    ]
    for label, cmd in maintenance:
        append_command(record, cmd, min(180, command_timeout_sec), env, label, deadline)

    record['data_policy_decision'] = latest_decision(paths)
    record['blocker_packet'] = load_json(paths.state / 'blocker_resolution_packet.json', {})
    record['real_evidence_ready'] = real_evidence_ready(paths)
    record['nonzero_command_count'] = sum(1 for cmd in record['commands'] if int(cmd.get('return_code', 0) or 0) != 0)
    if record['real_evidence_ready']:
        record['supervisor_action'] = 'evidence_ready_next_cycle_can_run_full_autonomous_iteration'
    elif record.get('blocker_packet', {}).get('blocker_type'):
        record['supervisor_action'] = 'blocked_safely_continue_data_or_candidate_discovery_without_claim_advancement'
    else:
        record['supervisor_action'] = 'normal_fast_maintenance_completed'
    record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return record


def supervisor_step(project: str, venue: str, max_launches: int, use_llm: bool, timeout_sec: int) -> dict:
    env = base_env(use_llm)
    record: dict = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'project': project, 'venue': venue, 'mode': 'full_cycle', 'commands': []}
    auto_cmd = [sys.executable, str(SCRIPTS / 'run_autonomous_research.py'), '--project', project, '--venue', venue, '--iterations', '1', '--execute-plan', '--max-launches', str(max_launches)]
    record['commands'].append(run(auto_cmd, timeout=timeout_sec, env=env, label='run_autonomous_research'))
    maintenance = [
        ('planning_blocker_resolution', module_cmd('planning', 'blocker_resolution', '--project', project, '--venue', venue)),
        ('audit_paper_evidence', module_cmd('writing', 'audit_evidence', '--project', project, '--venue', venue)),
        ('research_manifest', [sys.executable, str(SCRIPTS / 'research_manifest.py'), '--project', project, '--venue', venue]),
        ('report_status', [sys.executable, str(SCRIPTS / 'report_status.py'), '--project', project, '--venue', venue]),
        ('generate_handoff', [sys.executable, str(SCRIPTS / 'generate_handoff.py'), '--project', project, '--venue', venue]),
    ]
    for label, cmd in maintenance:
        record['commands'].append(run(cmd, timeout=240, env=env, label=label))
    paths = build_paths(project)
    decision = latest_decision(paths)
    record['data_policy_decision'] = decision
    record['blocker_packet'] = load_json(paths.state / 'blocker_resolution_packet.json', {})
    record['real_evidence_ready'] = real_evidence_ready(paths)
    record['nonzero_command_count'] = sum(1 for cmd in record['commands'] if int(cmd.get('return_code', 0) or 0) != 0)
    if decision in {'expand_discovery_or_request_user_data_before_switching', 'ask_user_for_data_or_expand_discovery'}:
        record['supervisor_action'] = 'continued_data_ready_discovery_and_pool_audit'
    elif decision == 'switch_or_backtrack_to_evidence_ready_repo':
        record['supervisor_action'] = 'switching_allowed_only_after_evidence_ready_candidate_review'
    elif decision == 'verify_loader_then_run_real_smoke':
        record['supervisor_action'] = 'next_cycle_should_probe_loader_and_run_real_smoke'
    else:
        record['supervisor_action'] = 'normal_or_blocked_follow_packet'
    record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description='Evidence-safe supervisor for repeated AutoScientist cycles.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--max-launches', type=int, default=1)
    parser.add_argument('--timeout-sec', type=int, default=1800)
    parser.add_argument('--use-llm', action='store_true', help='Allow configured LLM calls; otherwise disables LLM for deterministic supervision.')
    parser.add_argument('--fast-cycle', action='store_true', help='Run bounded evidence-maintenance cycle instead of the full autonomous scientist loop.')
    parser.add_argument('--command-timeout-sec', type=int, default=240, help='Per-command timeout for fast-cycle maintenance steps.')
    parser.add_argument('--audit-limit', type=int, default=2, help='Number of alternative repo candidates to audit in fast-cycle mode.')
    parser.add_argument('--allow-data-attempt', action='store_true', help='Allow one bounded data acquisition attempt if the project has not exhausted the attempt cap.')
    parser.add_argument('--allow-restart-discovery', action='store_true', help='Allow bounded network discovery expansion from the data-blocker policy during fast-cycle mode.')
    parser.add_argument('--stop-on-blocker', action='store_true', help='Stop after one cycle if blocker remains. Default continues until cycle budget.')
    args = parser.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paths = build_paths(args.project)
    history = load_json(paths.state / 'autoscientist_supervisor_history.json', {'cycles': []})
    cycles = history.get('cycles', []) if isinstance(history, dict) else []
    for _idx in range(1, args.cycles + 1):
        if args.fast_cycle:
            step = fast_supervisor_step(
                args.project, args.venue, args.use_llm, args.timeout_sec, args.command_timeout_sec,
                args.audit_limit, args.allow_data_attempt, args.allow_restart_discovery,
            )
        else:
            step = supervisor_step(args.project, args.venue, args.max_launches, args.use_llm, args.timeout_sec)
        step['cycle_index'] = len(cycles) + 1
        cycles.append(step)
        save_json(paths.state / 'autoscientist_supervisor_history.json', {'cycles': cycles, 'latest': step})
        if step.get('real_evidence_ready'):
            break
        if args.stop_on_blocker and step.get('blocker_packet', {}).get('blocker_type') not in {'none', ''}:
            break

    latest = cycles[-1] if cycles else {}
    lines = ['# AutoScientist Supervisor\n\n']
    lines.append(f"- total_cycles: {len(cycles)}\n")
    lines.append(f"- latest_decision: {latest.get('data_policy_decision', '')}\n")
    lines.append(f"- latest_action: {latest.get('supervisor_action', '')}\n")
    lines.append(f"- mode: {latest.get('mode', 'full_cycle')}\n")
    lines.append(f"- real_evidence_ready: {latest.get('real_evidence_ready', False)}\n")
    lines.append(f"- nonzero_command_count: {latest.get('nonzero_command_count', 0)}\n")
    packet = latest.get('blocker_packet', {}) if isinstance(latest, dict) else {}
    if packet:
        lines.append(f"- blocker_type: {packet.get('blocker_type', '')}\n")
        lines.append(f"- completion_condition: {packet.get('completion_condition', '')}\n")
    lines.append('\n## Recent Commands\n')
    for cmd in latest.get('commands', [])[-12:]:
        lines.append(f"- {cmd.get('label', '')}: rc={cmd.get('return_code')} duration={cmd.get('duration_sec', '')}s | `{cmd.get('command')}`\n")
    out = paths.reports / 'autoscientist_supervisor.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
