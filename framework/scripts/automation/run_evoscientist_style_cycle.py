#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from project.project_paths import ROOT, build_paths, load_project_config
from orchestration.commands import module_command as module_cmd
from policies.pipeline_guard import guard_fresh_base_blocker_entry
from runtime.framework_io import read_json as load_json
from runtime.framework_io import write_json_raw as save_json
from runtime.taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)

ROLES = ['planner', 'researcher', 'coder', 'debugger', 'analyst', 'writer']
RECOVERABLE_PATTERNS = [
    ('network_timeout', ['timeout', 'timed out', 'temporary failure', 'connection reset', 'urlopen error']),
    ('data_missing', ['missing', 'data', 'dataset files', 'all_data.pkl', 'dist_mat.npy']),
    ('llm_unavailable', ['llm-api-key-missing', 'llm request failed', 'api', 'rate limit', 'llm_readiness', 'check_llm_ready', 'llm readiness']),
    ('clone_unavailable', ['git clone', 'clone_failed', 'repository not found']),
    ('env_not_ready', ['conda', 'environment', 'module not found', 'no module named']),
    ('taste_recoverable', ['taste native frontend timed out', 'taste_sync', 'skipped_no_taste_output', 'completed_no_usable_taste_items']),
]


def run(cmd: list[str], timeout: int, env: dict[str, str], cwd: Path = ROOT) -> dict:
    started = dt.datetime.now(dt.timezone.utc)
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
        rc = proc.returncode
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
    except subprocess.TimeoutExpired as exc:
        rc = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'ignore')
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'ignore')
        stderr = (stderr or '') + f'\nTIMEOUT after {timeout}s'
    finished = dt.datetime.now(dt.timezone.utc)
    text = (stdout + '\n' + stderr).lower()
    tags = []
    command_text = ' '.join(cmd).lower()
    for tag, needles in RECOVERABLE_PATTERNS:
        if any(needle in text or needle in command_text for needle in needles):
            tags.append(tag)
    if rc != 0 and 'check_llm_ready.py' in command_text:
        tags.append('llm_unavailable')
    return {
        'command': ' '.join(cmd),
        'return_code': rc,
        'started_at': started.isoformat(),
        'finished_at': finished.isoformat(),
        'duration_sec': round((finished - started).total_seconds(), 3),
        'stdout_tail': stdout[-2500:],
        'stderr_tail': stderr[-2500:],
        'recoverable_tags': sorted(set(tags)),
        'recoverable': rc != 0 and bool(tags),
    }


def base_env(use_llm: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault('ARXIV_TIMEOUT_SEC', '12')
    env.setdefault('GITHUB_TIMEOUT_SEC', '12')
    env.setdefault('REPO_CLONE_TIMEOUT_SEC', '30')
    env.setdefault('LLM_TIMEOUT_SEC', '25')
    env.setdefault('LLM_RETRIES', '1')
    if not use_llm:
        env['LLM_ENABLED'] = '0'
    return env


def append_phase(record: dict, paths, phase: str, role: str, commands: list[tuple[list[str], int]], env: dict[str, str], required: bool = False) -> dict:
    phase_record = {
        'phase': phase,
        'role': role,
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'commands': [],
        'status': 'running',
        'recoverable_exceptions': [],
    }
    record.setdefault('phases', []).append(phase_record)
    save_json(paths.state / 'evoscientist_style_cycle.json', record)
    for cmd, timeout in commands:
        result = run(cmd, timeout=timeout, env=env)
        phase_record['commands'].append(result)
        if result['return_code'] != 0:
            exception = {
                'command': result['command'],
                'return_code': result['return_code'],
                'tags': result.get('recoverable_tags', []),
                'policy': 'recover_and_continue_with_evidence_gate' if result.get('recoverable') or not required else 'required_phase_failed',
                'stderr_tail': result.get('stderr_tail', '')[-1000:],
            }
            phase_record['recoverable_exceptions'].append(exception)
            if required and not result.get('recoverable'):
                phase_record['status'] = 'failed_required'
                break
        save_json(paths.state / 'evoscientist_style_cycle.json', record)
    if phase_record['status'] == 'running':
        phase_record['status'] = 'completed_with_recoveries' if phase_record['recoverable_exceptions'] else 'completed'
    phase_record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(paths.state / 'evoscientist_style_cycle.json', record)
    return phase_record


def active_blocked(paths) -> bool:
    blocker = load_json(paths.state / 'blocker_resolution_packet.json', {})
    return blocker.get('blocker_type') not in {'', 'none'} or blocker.get('paper_gate_summary') == 'hold-markdown-only'


def write_workflow_smoke(paths, project: str, venue: str) -> dict:
    artifact = paths.artifacts / 'workflow_smoke' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    artifact.mkdir(parents=True, exist_ok=True)
    blocker = load_json(paths.state / 'blocker_resolution_packet.json', {})
    payload = {
        'project': project,
        'venue': venue,
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'artifact_type': 'workflow_validation_only',
        'status': 'completed',
        'evidence_level': 'not_scientific_evidence',
        'claim_verdict': 'unsupported_for_paper_claims',
        'reason': 'Used only to prove the AutoScientist control loop can execute Code/Execute/Evaluate phases while real-data evidence is blocked.',
        'active_blocker_type': blocker.get('blocker_type', ''),
        'paper_gate_summary': blocker.get('paper_gate_summary', ''),
        'guardrail': 'Do not cite this artifact as experimental support. It is a workflow smoke artifact only.',
    }
    save_json(artifact / 'audit.json', payload)
    save_json(artifact / 'metrics.json', {'workflow_smoke_completed': 1, 'scientific_metric_count': 0})
    save_json(artifact / 'bad_cases.json', [{'slice': 'workflow_only_no_scientific_bad_case', 'evidence': payload['reason']}])
    return {'artifact_dir': str(artifact), **payload}


def update_memory(paths, record: dict) -> None:
    memory_path = paths.state / 'evo_recoverable_memory.json'
    memory = load_json(memory_path, {'ideation_memory': [], 'experimentation_memory': [], 'exception_memory': []})
    exceptions = []
    for phase in record.get('phases', []):
        for item in phase.get('recoverable_exceptions', []):
            exceptions.append({'phase': phase.get('phase'), 'role': phase.get('role'), **item, 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()})
    if exceptions:
        memory.setdefault('exception_memory', []).extend(exceptions)
        memory['exception_memory'] = memory['exception_memory'][-200:]
    memory.setdefault('experimentation_memory', []).append({
        'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': record.get('project'),
        'result': record.get('final_status'),
        'paper_gate': record.get('paper_gate_summary'),
        'lesson': 'Recoverable blockers should feed routing/discovery/data acquisition, not final paper claims.',
    })
    memory['experimentation_memory'] = memory['experimentation_memory'][-100:]
    save_json(memory_path, memory)


def write_report(paths, record: dict) -> None:
    lines = ['# EvoScientist-Style Recoverable Cycle\n\n']
    for key in ['project', 'venue', 'started_at', 'finished_at', 'final_status', 'scientific_completion', 'paper_gate_summary']:
        lines.append(f'- {key}: {record.get(key, "")}\n')
    lines.append(f"- recoverable_exception_count: {sum(len(p.get('recoverable_exceptions', [])) for p in record.get('phases', []))}\n")
    lines.append('\n## Phase Trace\n')
    for phase in record.get('phases', []):
        lines.append(f"- {phase.get('phase')} | role={phase.get('role')} | status={phase.get('status')} | commands={len(phase.get('commands', []))} | recoveries={len(phase.get('recoverable_exceptions', []))}\n")
    if record.get('workflow_smoke_artifact'):
        lines.append('\n## Workflow Smoke\n')
        lines.append(f"- artifact_dir: {record['workflow_smoke_artifact'].get('artifact_dir')}\n")
        lines.append(f"- evidence_level: {record['workflow_smoke_artifact'].get('evidence_level')}\n")
        lines.append(f"- guardrail: {record['workflow_smoke_artifact'].get('guardrail')}\n")
    lines.append('\n## Guardrail\n')
    lines.append('- Full-flow runnable does not mean scientific completion. Scientific completion still requires real data, loader-probed experiments, and paper evidence gates.\n')
    out = paths.reports / 'evoscientist_style_cycle.md'
    out.write_text(''.join(lines), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='source-style recoverable full-cycle runner for AutoScientist.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    parser.add_argument('--use-llm', action='store_true')
    parser.add_argument('--cycle-timeout-sec', type=int, default=180)
    parser.add_argument('--command-timeout-sec', type=int, default=45)
    parser.add_argument('--allow-restart-discovery', action='store_true')
    parser.add_argument('--skip-taste', action='store_true', help='Skip the vendored TASTE research frontend for this cycle.')
    parser.add_argument('--taste-timeout-sec', type=int, default=900, help='Budget for Find->Read->Idea->Plan before recoverable fallback.')
    parser.add_argument('--taste-max-papers', type=int, default=5)
    parser.add_argument('--taste-max-ideas', type=int, default=4)
    parser.add_argument('--taste-fast-mode', action='store_true', help='Run TASTE with smaller budgets and slower sources disabled.')
    args = parser.parse_args()
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    env = base_env(args.use_llm)
    record = {
        'project': args.project,
        'venue': args.venue,
        'topic': cfg.get('topic', ''),
        'roles': ROLES,
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'phases': [],
        'principles': [
            'source-style six-phase workflow: Intake, Plan, Research, Code/Execute, Evaluate, Write, Verify.',
            'Exceptions are recoverable routing signals unless evidence proves a hard safety failure.',
            'Workflow smoke artifacts prove control-flow runnability but never support paper claims.',
        ],
    }
    save_json(paths.state / 'evoscientist_style_cycle.json', record)

    append_phase(record, paths, 'intake', 'planner', [
        ([sys.executable, str(SCRIPTS / 'report_status.py'), '--project', args.project, '--venue', args.venue], args.command_timeout_sec),
        ([sys.executable, str(SCRIPTS / 'check_llm_ready.py'), '--project', args.project] + (['--live'] if args.use_llm else []), min(90, args.command_timeout_sec)),
    ], env)
    append_phase(record, paths, 'plan', 'planner', [
        (module_cmd('planning', 'next_actions', '--project', args.project), args.command_timeout_sec),
        ([sys.executable, str(SCRIPTS / 'build_stagnation_report.py'), '--project', args.project, '--venue', args.venue], args.command_timeout_sec),
    ], env)
    research_commands: list[tuple[list[str], int]] = []
    if not args.skip_taste:
        taste_cmd = [
            sys.executable, 'framework/scripts/main.py', 'find',
            '--project', args.project,
            '--max-papers', str(args.taste_max_papers),
            '--max-ideas', str(args.taste_max_ideas),
            '--timeout-sec', str(args.taste_timeout_sec),
        ]
        if args.taste_fast_mode:
            taste_cmd.append('--fast-mode' )
        research_commands.append((taste_cmd, args.taste_timeout_sec + 30))
        research_commands.append(([sys.executable, str(SCRIPTS / 'sync_outputs.py'), '--project', args.project, '--allow-empty'], min(180, args.command_timeout_sec)))
    research_cmd = [sys.executable, str(SCRIPTS / 'run_autoscientist_supervisor.py'), '--project', args.project, '--venue', args.venue, '--cycles', '1', '--fast-cycle', '--timeout-sec', str(args.cycle_timeout_sec), '--command-timeout-sec', str(args.command_timeout_sec), '--audit-limit', '3', '--stop-on-blocker']
    if args.allow_restart_discovery:
        research_cmd.append('--allow-restart-discovery')
    if args.use_llm:
        research_cmd.append('--use-llm')
    research_commands.append((research_cmd, args.cycle_timeout_sec + 30))
    append_phase(record, paths, 'research', 'researcher', research_commands, env)

    if active_blocked(paths):
        smoke = write_workflow_smoke(paths, args.project, args.venue)
        record['workflow_smoke_artifact'] = smoke
        save_json(paths.state / 'evoscientist_style_cycle.json', record)
        append_phase(record, paths, 'code_execute', 'coder_debugger', [
            ([sys.executable, '-c', 'import json,sys; print(json.dumps({"workflow_smoke":"ok","scientific_evidence":False}))'], 20),
        ], env)
    else:
        append_phase(record, paths, 'code_execute', 'coder_debugger', [
            ([sys.executable, str(SCRIPTS / 'run_autonomous_research.py'), '--project', args.project, '--venue', args.venue, '--iterations', '1', '--execute-plan', '--max-launches', '2'], args.cycle_timeout_sec),
        ], env)

    append_phase(record, paths, 'evaluate', 'analyst', [
        (module_cmd('writing', 'audit_evidence', '--project', args.project, '--venue', args.venue), args.command_timeout_sec),
        (module_cmd('planning', 'blocker_resolution', '--project', args.project, '--venue', args.venue), args.command_timeout_sec),
        ([sys.executable, str(SCRIPTS / 'build_stagnation_report.py'), '--project', args.project, '--venue', args.venue], args.command_timeout_sec),
    ], env)
    paper_cfg = cfg.get('paper') if isinstance(cfg.get('paper'), dict) else {}
    write_cmd = module_cmd('writing', 'run', '--project', args.project, '--venue', args.venue)
    paper_title = str(paper_cfg.get('title') or '').strip()
    if paper_title:
        write_cmd.extend(['--title', paper_title])
    append_phase(record, paths, 'write', 'writer', [
        (write_cmd, min(180, args.cycle_timeout_sec)),
    ], env)
    append_phase(record, paths, 'verify', 'debugger_analyst', [
        ([sys.executable, str(SCRIPTS / 'audit_pipeline_runnability.py'), '--project', args.project, '--venue', args.venue], min(120, args.cycle_timeout_sec)),
        ([sys.executable, str(SCRIPTS / 'research_healthcheck.py'), '--project', args.project, '--venue', args.venue], args.command_timeout_sec),
        ([sys.executable, str(SCRIPTS / 'wiki_tools.py'), '--tool-action', 'lint', '--project', args.project], args.command_timeout_sec),
        ([sys.executable, str(SCRIPTS / 'report_status.py'), '--project', args.project, '--venue', args.venue], args.command_timeout_sec),
    ], env)

    blocker = load_json(paths.state / 'blocker_resolution_packet.json', {})
    paper_gate = blocker.get('paper_gate_summary', '')
    record['paper_gate_summary'] = paper_gate
    record['scientific_completion'] = bool(blocker.get('blocker_type') in {'', 'none'} and paper_gate != 'hold-markdown-only')
    record['final_status'] = 'scientific_complete' if record['scientific_completion'] else 'full_flow_runnable_but_evidence_gated'
    record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    update_memory(paths, record)
    save_json(paths.state / 'evoscientist_style_cycle.json', record)
    write_report(paths, record)
    print(paths.reports / 'evoscientist_style_cycle.md')


if __name__ == '__main__':
    main()
