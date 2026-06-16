#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
FRAMEWORK_SCRIPTS = ROOT / 'framework' / 'scripts'
if str(FRAMEWORK_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_SCRIPTS))
from taste_pythonpath import ensure_taste_pythonpath, script_resolver, taste_pythonpath_string

ensure_taste_pythonpath(ROOT)
os.environ['PYTHONPATH'] = taste_pythonpath_string(ROOT, os.environ.get('PYTHONPATH', ''))
SCRIPTS = script_resolver(ROOT)
sys.path.insert(0, str(SCRIPTS))

from paper_common import update_pipeline_state
from pipeline_guard import guard_fresh_base_blocker_entry


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def gate_passed(gate: Any, *, decision: str | None = None) -> bool:
    if not isinstance(gate, dict) or not gate:
        return False
    if gate.get('status') != 'pass':
        return False
    return decision is None or gate.get('decision') == decision


def preflight_science_gates(project: str, venue: str) -> tuple[bool, list[str], dict[str, Any]]:
    root = ROOT / 'projects' / project
    reference_gate = read_json(root / 'state' / 'reference_reproduction_gate.json', {})
    progress_gate = read_json(root / 'state' / 'scientific_progress_gate.json', {})
    iteration_audit = read_json(root / 'state' / 'experiment_iteration_audit.json', {})
    blockers: list[str] = []
    if not gate_passed(reference_gate, decision='continue_base'):
        gate_blockers = reference_gate.get('blockers', []) if isinstance(reference_gate, dict) and isinstance(reference_gate.get('blockers', []), list) else []
        detail = '; '.join(str(item) for item in gate_blockers[:3]) or f"status={reference_gate.get('status') if isinstance(reference_gate, dict) else 'missing'}; decision={reference_gate.get('decision') if isinstance(reference_gate, dict) else 'missing'}"
        blockers.append('reference reproduction gate blocked: ' + detail)
    if not gate_passed(progress_gate):
        gate_blockers = progress_gate.get('blockers', []) if isinstance(progress_gate, dict) and isinstance(progress_gate.get('blockers', []), list) else []
        detail = '; '.join(str(item) for item in gate_blockers[:3]) or f"status={progress_gate.get('status') if isinstance(progress_gate, dict) else 'missing'}"
        blockers.append('scientific progress gate blocked: ' + detail)
    if not gate_passed(iteration_audit):
        audit_blockers = iteration_audit.get('blockers', []) if isinstance(iteration_audit, dict) and isinstance(iteration_audit.get('blockers', []), list) else []
        audit_warnings = iteration_audit.get('warnings', []) if isinstance(iteration_audit, dict) and isinstance(iteration_audit.get('warnings', []), list) else []
        detail = '; '.join(str(item) for item in (audit_blockers + audit_warnings)[:3]) or f"status={iteration_audit.get('status') if isinstance(iteration_audit, dict) else 'missing'}"
        blockers.append('experiment trajectory audit blocked: ' + detail)
    snapshot = {
        'reference_reproduction_gate': reference_gate if isinstance(reference_gate, dict) else {},
        'scientific_progress_gate': progress_gate if isinstance(progress_gate, dict) else {},
        'experiment_iteration_audit': iteration_audit if isinstance(iteration_audit, dict) else {},
        'blockers': blockers,
    }
    if blockers:
        update_pipeline_state(project, {
            'updated_at': now_iso(),
            'venue': venue,
            'paper_stage_status': 'blocked_before_paper_generation',
            'paper_orchestra_bridge_status': 'blocked_before_paper_generation',
            'promotion_gate': 'hold-markdown-only',
            'submission_ready': False,
            'pdf_ready': False,
            'paper_generation_skipped': False,
            'paper_generation_skipped_reason': 'science gates are not cleared; generated output is a venue-formatted manuscript preview only, while submission readiness and claim promotion remain blocked',
            'science_gate_preflight': snapshot,
        }, venue=venue)
    else:
        current = read_json(root / 'paper' / 'metadata' / 'paper_pipeline.json', {})
        paper_stage_status = current.get('paper_stage_status') if isinstance(current, dict) else ''
        bridge_status = current.get('paper_orchestra_bridge_status') if isinstance(current, dict) else ''
        update = {
            'updated_at': now_iso(),
            'venue': venue,
            'paper_generation_skipped': False,
            'paper_generation_skipped_reason': '',
            'science_gate_preflight': snapshot,
        }
        if paper_stage_status == 'blocked_before_paper_generation':
            update['paper_stage_status'] = 'science_gates_pass'
        if bridge_status == 'blocked_before_paper_generation':
            update['paper_orchestra_bridge_status'] = 'science_gates_pass'
        update_pipeline_state(project, update, venue=venue)
    return not blockers, blockers, snapshot


def should_regenerate_current_preview(project: str, venue: str, explicit: bool = False) -> bool:
    if explicit:
        return True
    root = ROOT / 'projects' / project
    submission = read_json(root / 'state' / 'submission_readiness.json', {})
    full_cycle = read_json(root / 'state' / 'full_research_cycle.json', {})
    if isinstance(submission, dict) and submission and not submission.get('submission_ready'):
        return True
    if isinstance(full_cycle, dict) and (full_cycle.get('paper_iteration_required') or full_cycle.get('pdf_changed_this_cycle') is False):
        return True
    return False



def existing_file(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.exists() and path.is_file() else None


def record_missing_preview_repair_artifact(project: str, venue: str, repair_rc: int) -> bool:
    project_root = ROOT / 'projects' / project
    state_path = project_root / 'paper' / 'metadata' / 'paper_pipeline.json'
    pipeline_state = read_json(state_path, {})
    if isinstance(pipeline_state, dict) and pipeline_state.get('conference_preview_ready'):
        return False
    loop_path = project_root / 'state' / 'paper_preview_repair_loop.json'
    if loop_path.exists():
        return False
    report_path = project_root / 'reports' / 'paper_preview_repair_loop.md'
    reason = f'repair_paper_preview_loop exited rc={repair_rc} without writing {loop_path}'
    payload = {
        'project': project,
        'venue': venue,
        'updated_at': now_iso(),
        'status': 'framework_error_missing_repair_artifact',
        'repair_return_code': repair_rc,
        'reason': reason,
    }
    loop_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    loop_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report_path.write_text(
        '# Writing Preview Revision\n\n'
        '- status: framework_error_missing_repair_artifact\n'
        f'- repair_return_code: {repair_rc}\n'
        f'- reason: {reason}\n',
        encoding='utf-8',
    )
    update_pipeline_state(project, {
        'paper_preview_repair_loop_status': 'framework_error_missing_repair_artifact',
        'paper_preview_repair_loop_report': str(report_path),
        'paper_preview_repair_loop_json': str(loop_path),
        'paper_preview_repair_rounds': 0,
        'paper_preview_repair_blocker': reason,
        'paper_self_review_ready': False,
    }, venue=venue, promote_to_top=True)
    return True


def run(cmd: list[str], required: bool = True) -> int:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout, end='')
    if proc.stderr:
        print(proc.stderr, end='', file=sys.stderr)
    if required and proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.returncode


def refresh_venue_requirements(project: str, venue: str, *, refresh_current_venue: bool, strict: bool) -> bool:
    """Resolve target venue rules before any paper audit consumes them."""
    requirements_cmd = [sys.executable, str(SCRIPTS / 'resolve_venue_requirements.py'), '--project', project, '--venue', venue]
    if refresh_current_venue:
        requirements_cmd.append('--refresh-current-venue')
    requirements_rc = run(requirements_cmd, required=False)
    if requirements_rc != 0:
        print('TASTE venue requirements are not ready; paper generation stops before writing so the manuscript cannot use stale or guessed venue rules.')
        if strict:
            raise SystemExit(requirements_rc)
        return False
    return True


def fetch_venue_template(args: argparse.Namespace, *, strict: bool) -> bool:
    fetch_cmd = [sys.executable, str(SCRIPTS / 'fetch_latex_template.py'), '--project', args.project, '--venue', args.venue]
    if args.template_url:
        fetch_cmd.extend(['--url', args.template_url])
    if args.template_archive_path:
        fetch_cmd.extend(['--archive-path', args.template_archive_path])
    fetch_rc = run(fetch_cmd, required=False)
    if fetch_rc != 0:
        print('TASTE official venue template is not ready; paper generation stops before writing so no fallback template can be exposed as a paper.')
        if strict:
            raise SystemExit(fetch_rc)
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', required=True)
    parser.add_argument('--title', default='')
    parser.add_argument('--template-url', default='')
    parser.add_argument('--template-archive-path', default='')
    parser.add_argument('--skip-fetch', action='store_true')
    parser.add_argument('--skip-compile', action='store_true')
    parser.add_argument('--strict-template', action='store_true')
    parser.add_argument('--force-template', dest='force_template', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--generate-inspection-paper', dest='force_template', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--generate-paper-preview', dest='force_template', action='store_true')
    parser.add_argument('--refresh-current-paper', dest='regenerate_current_paper', action='store_true')
    parser.add_argument('--force-refresh', dest='regenerate_current_paper', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--refresh-current-venue', dest='force_venue_refresh', action='store_true')
    parser.add_argument('--force-venue-refresh', dest='force_venue_refresh', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--auto-install-latex', action='store_true')
    args = parser.parse_args()
    os.environ['PROJECT_ID'] = args.project
    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name)
    if guard_rc is not None:
        raise SystemExit(guard_rc)
    regenerate_current_preview = should_regenerate_current_preview(args.project, args.venue, explicit=args.regenerate_current_paper)
    venue_refresh_requested = bool(args.force_venue_refresh or regenerate_current_preview or args.force_template)
    venue_contract_strict = bool(args.strict_template or args.force_template or regenerate_current_preview or args.force_venue_refresh)

    if not args.skip_fetch:
        update_pipeline_state(args.project, {
            'updated_at': now_iso(),
            'venue': args.venue,
            'paper_generation_entrypoint': 'writing_current_venue_preview',
            'paper_generation_entrypoint_policy': 'resolve current official venue requirements and validate the official LaTeX template before writing; never infer one venue rules from another venue',
            'paper_current_regeneration_requested': bool(regenerate_current_preview),
            'paper_current_regeneration_policy': 'rebuild the current venue-formatted manuscript preview from current TASTE evidence; this is a writing/layout/citation task, not a research-intervention directive',
        }, venue=args.venue)
        # The paper entry point may generate a paper preview, but venue
        # rules/templates must be refreshed before audits consume them.
        # User-triggered preview regeneration also refreshes venue facts so
        # page limits, official templates, and reference targets never rely on
        # stale assumptions from another venue or year.
        venue_ok = refresh_venue_requirements(
            args.project,
            args.venue,
            refresh_current_venue=venue_refresh_requested,
            strict=venue_contract_strict,
        )
        if not venue_ok:
            return
        template_ok = fetch_venue_template(args, strict=venue_contract_strict)
        if not template_ok:
            return

    run([sys.executable, str(SCRIPTS / 'sync_third_party_research_stack.py'), '--project', args.project], required=False)
    run([sys.executable, str(SCRIPTS / 'audit_reference_reproduction.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'audit_experiment_iteration.py'), '--project', args.project], required=False)
    run([sys.executable, str(SCRIPTS / 'audit_paper_evidence.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'audit_submission_readiness.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'build_blocker_action_plan.py'), '--project', args.project, '--venue', args.venue], required=False)
    gates_ok, blockers, _ = preflight_science_gates(args.project, args.venue)
    if not gates_ok and not args.force_template:
        print('Paper evidence gates are blocked; The workflow will still generate or revise the current venue-formatted manuscript preview because the user requested paper generation.')
        for item in blockers:
            print(f'- {item}')
        args.force_template = True
    if not gates_ok and args.force_template:
        print('TASTE 正在生成目标格式稿件预览；证据与投稿门控保持真实状态，预览稿不得被视为投稿通过或结论提升。')

    trajectory_cmd = [sys.executable, str(SCRIPTS / 'build_research_trajectory_system.py'), '--project', args.project, '--venue', args.venue]
    run(trajectory_cmd, required=False)

    build_cmd = [sys.executable, str(SCRIPTS / 'build_paper_md.py'), '--project', args.project, '--venue', args.venue]
    if args.title:
        build_cmd.extend(['--title', args.title])
    run([sys.executable, str(SCRIPTS / 'build_paper_orchestra_state.py'), '--project', args.project, '--venue', args.venue], required=False)
    bridge_cmd = [sys.executable, str(SCRIPTS / 'run_paper_orchestra_bridge.py'), '--project', args.project, '--venue', args.venue]
    if args.title:
        bridge_cmd.extend(['--title', args.title])
    if regenerate_current_preview:
        bridge_cmd.append('--refresh-current-paper')
    run(bridge_cmd, required=False)
    run(build_cmd)
    run([sys.executable, str(SCRIPTS / 'review_paper_md.py'), '--project', args.project, '--venue', args.venue])
    run([sys.executable, str(SCRIPTS / 'build_claim_ledger.py'), '--project', args.project], required=False)
    run([sys.executable, str(SCRIPTS / 'build_paper_orchestra_state.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'aggregate_paper_reviews.py'), '--project', args.project, '--venue', args.venue])
    run([sys.executable, str(SCRIPTS / 'revise_paper_md.py'), '--project', args.project, '--venue', args.venue])
    run([sys.executable, str(SCRIPTS / 'build_paper_orchestra_state.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'review_response_tools.py'), '--tool-action', 'respond', '--project', args.project, '--venue', args.venue])
    run([sys.executable, str(SCRIPTS / 'audit_paper_evidence.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'review_response_tools.py'), '--tool-action', 're_review', '--project', args.project, '--venue', args.venue])
    run([sys.executable, str(SCRIPTS / 'build_aris_review_board.py'), '--project', args.project], required=False)
    run([sys.executable, str(SCRIPTS / 'build_paper_orchestra_state.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'audit_paper_orchestra.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'audit_submission_readiness.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'build_blocker_action_plan.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'research_manifest.py'), '--project', args.project, '--venue', args.venue])
    preview_cmd = [sys.executable, str(SCRIPTS / 'build_conference_preview_paper.py'), '--project', args.project, '--venue', args.venue]
    if args.title:
        preview_cmd.extend(['--title', args.title])
    run(preview_cmd, required=False)
    preview_repair_cmd = [sys.executable, str(SCRIPTS / 'repair_paper_preview_loop.py'), '--project', args.project, '--venue', args.venue, '--title', args.title or '', '--max-rounds', '5']
    if regenerate_current_preview:
        preview_repair_cmd.append('--refresh-current-paper')
    preview_repair_rc = run(preview_repair_cmd, required=False)
    record_missing_preview_repair_artifact(args.project, args.venue, preview_repair_rc)
    run([sys.executable, str(SCRIPTS / 'audit_submission_readiness.py'), '--project', args.project, '--venue', args.venue], required=False)
    run([sys.executable, str(SCRIPTS / 'build_blocker_action_plan.py'), '--project', args.project, '--venue', args.venue], required=False)

    project_root = ROOT / 'projects' / args.project
    preview_state = read_json(project_root / 'paper' / 'metadata' / 'paper_pipeline.json', {})
    preview_pdf = existing_file(preview_state.get('conference_preview_pdf') or preview_state.get('latest_preview_pdf') or preview_state.get('blocked_preview_pdf'))
    preview_tex = existing_file(preview_state.get('conference_preview_tex') or preview_state.get('latest_preview_tex') or preview_state.get('blocked_preview_tex') or preview_state.get('rendered_tex'))
    if preview_pdf and preview_tex:
        if preview_state.get('conference_preview_ready'):
            print('writing venue-formatted preview generated; skipping legacy Markdown render/compile so the preview PDF is not overwritten.')
            return
        if args.force_template:
            print('writing preview exists but quality/evidence gates are not all cleared; preserving writing artifacts and refusing legacy Markdown overwrite.')
            return
    elif args.force_template:
        print('writing preview is not ready; refusing legacy Markdown render/compile because it can expose internal status text as a paper PDF.')
        return

    if not args.force_template:
        gate_cmd = [sys.executable, '-c', (
            'import sys; sys.path.insert(0, "' + str(SCRIPTS) + '"); '
            'from paper_common import get_active_paper_state; '
            f'state=get_active_paper_state("{args.project}", venue="{args.venue}"); '
            'import sys as _s; '
            '_s.exit(0 if state.get("promotion_gate") == "allow-template" and state.get("submission_ready") else 3)'
        )]
        gate_rc = run(gate_cmd, required=False)
        run([sys.executable, str(SCRIPTS / 'report_status.py'), '--project', args.project, '--venue', args.venue], required=False)
        if gate_rc != 0:
            print('Paper pipeline generated a blocked draft/preview only. TASTE evidence, venue, figure, or submission-readiness gates have not cleared, so the full-cycle supervisor must continue repair instead of treating this as final.')
            return

    render_rc = run([sys.executable, str(SCRIPTS / 'render_paper_tex.py'), '--project', args.project, '--venue', args.venue], required=False)
    if render_rc != 0:
        if args.strict_template:
            raise SystemExit(render_rc)
        print('Paper pipeline stopped after Markdown review/revision because template rendering is not ready.')
        return

    if not args.skip_compile:
        compile_cmd = [sys.executable, str(SCRIPTS / 'compile_paper_pdf.py'), '--project', args.project, '--venue', args.venue]
        if args.auto_install_latex:
            compile_cmd.append('--auto-install-missing')
        compile_rc = run(compile_cmd, required=False)
        if compile_rc != 0:
            print('Paper pipeline generated a compile report instead of a ready PDF.')


if __name__ == '__main__':
    main()
