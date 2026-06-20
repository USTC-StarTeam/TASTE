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

from project_paths import ROOT, build_paths


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    tmp = path.with_name(f'{path.name}.tmp.{os.getpid()}')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


def _mtime_iso(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
    except Exception:
        return ''


def _parse_iso(value: Any) -> dt.datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _run_watchdog(project: str) -> dict[str, Any]:
    watchdog = Path(__file__).resolve().parents[1] / 'execution' / 'experiment_run_watchdog.py'
    if not watchdog.exists():
        return {'status': 'blocked', 'return_code': None, 'issues': [{'severity': 'block', 'issue': 'experiment_run_watchdog.py is missing', 'evidence': [str(watchdog)]}]}
    proc = subprocess.run(
        [sys.executable, str(watchdog), '--project', project],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except Exception:
        payload = {
            'status': 'blocked',
            'return_code': proc.returncode,
            'stdout_tail': proc.stdout[-2000:],
            'stderr_tail': proc.stderr[-2000:],
            'issues': [{'severity': 'block', 'issue': 'experiment_run_watchdog.py did not emit parseable JSON'}],
        }
    if isinstance(payload, dict):
        payload.setdefault('return_code', proc.returncode)
        if proc.returncode != 0:
            issues = payload.get('issues') if isinstance(payload.get('issues'), list) else []
            issues.append({'severity': 'block', 'issue': f'experiment_run_watchdog.py returned {proc.returncode}', 'stderr_tail': proc.stderr[-1200:]})
            payload['issues'] = issues
            payload['status'] = 'blocked'
    return payload if isinstance(payload, dict) else {'status': 'blocked', 'issues': [{'severity': 'block', 'issue': 'invalid watchdog payload'}]}


def _issue_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get('issue') or item.get('message') or item.get('reason') or item.get('type') or item)
    return str(item)


def build_runtime_integrity_audit(project: str, *, run_watchdog: bool = True) -> dict[str, Any]:
    paths = build_paths(project)
    generated_at = now_iso()
    watchdog_payload = _run_watchdog(project) if run_watchdog else load_json(paths.state / 'experiment_run_watchdog.json', {})
    manifest = load_json(paths.state / 'experiment_run_manifest.json', {})
    registry_path = paths.state / 'experiment_registry.json'
    full_cycle_path = paths.state / 'full_research_cycle.json'
    record_table_path = paths.state / 'experiment_record_table.json'

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    watchdog_status = str(watchdog_payload.get('status') or 'missing') if isinstance(watchdog_payload, dict) else 'missing'
    if not isinstance(watchdog_payload, dict) or not watchdog_payload:
        issues.append({'severity': 'block', 'issue': 'current experiment watchdog payload is missing', 'evidence': [str(paths.state / 'experiment_run_watchdog.json')]})
    elif watchdog_status == 'blocked':
        rows = watchdog_payload.get('issues') if isinstance(watchdog_payload.get('issues'), list) else []
        for row in rows:
            severity = str(row.get('severity') if isinstance(row, dict) else 'block')
            target = issues if severity == 'block' else warnings
            target.append({'severity': severity, 'issue': _issue_text(row), 'evidence': row.get('evidence', []) if isinstance(row, dict) else []})
        if not rows:
            issues.append({'severity': 'block', 'issue': 'experiment_run_watchdog status is blocked without detailed issues', 'evidence': [str(paths.state / 'experiment_run_watchdog.json')]})
    elif watchdog_status not in {'ok', 'pass'}:
        warnings.append({'severity': 'warn', 'issue': f'experiment_run_watchdog status={watchdog_status}', 'evidence': [str(paths.state / 'experiment_run_watchdog.json')]})

    if isinstance(watchdog_payload, dict) and watchdog_payload.get('stop_duplicate_writers_requested'):
        issues.append({
            'severity': 'block',
            'issue': 'experiment watchdog was invoked with stop-duplicate-writers; automatic research loops must audit duplicate writers non-invasively by default.',
            'evidence': [str(paths.state / 'experiment_run_watchdog.json')],
        })
    if isinstance(watchdog_payload, dict) and watchdog_payload.get('stop_duplicate_writers_enabled'):
        issues.append({
            'severity': 'block',
            'issue': 'experiment watchdog stop mode was enabled; runtime integrity evidence is not non-invasive.',
            'evidence': [str(paths.state / 'experiment_run_watchdog.json')],
        })

    if not isinstance(manifest, dict) or not manifest:
        issues.append({'severity': 'block', 'issue': 'experiment_run_manifest.json is missing after watchdog audit', 'evidence': [str(paths.state / 'experiment_run_manifest.json')]})
    else:
        manifest_generated = _parse_iso(manifest.get('generated_at'))
        audit_generated = _parse_iso(generated_at)
        if manifest_generated and audit_generated and abs((audit_generated - manifest_generated).total_seconds()) > 300:
            warnings.append({'severity': 'warn', 'issue': 'experiment_run_manifest.json was not refreshed close to this runtime integrity audit', 'evidence': [str(paths.state / 'experiment_run_manifest.json')]})

    active_runs = watchdog_payload.get('active_runs', []) if isinstance(watchdog_payload, dict) and isinstance(watchdog_payload.get('active_runs'), list) else []
    for run in active_runs:
        if not isinstance(run, dict):
            continue
        policy = run.get('python_policy') if isinstance(run.get('python_policy'), dict) else {}
        if policy.get('status') == 'reject':
            issues.append({'severity': 'block', 'issue': f"active run uses disallowed Python: {policy.get('reason')}", 'evidence': [run.get('artifact_dir', ''), run.get('contract_path', '')]})
        worker_pids = run.get('worker_pids') if isinstance(run.get('worker_pids'), list) else []
        if len(worker_pids) > 1:
            issues.append({'severity': 'block', 'issue': 'multiple python workers target one artifact_dir', 'evidence': [run.get('artifact_dir', '')], 'worker_pids': worker_pids})

    prior = load_json(paths.state / 'experiment_runtime_integrity.json', {})
    prior_policy = str(prior.get('policy') or '') if isinstance(prior, dict) else ''
    if "method-specific" in prior_policy.lower():
        warnings.append({'severity': 'warn', 'issue': 'previous runtime integrity policy was method-specific and has been replaced by this current generic launcher/watchdog audit.', 'evidence': [str(paths.state / 'experiment_runtime_integrity.json')]})

    status = 'blocked' if issues else 'warn' if warnings else 'pass'
    payload = {
        'project': project,
        'generated_at': generated_at,
        'updated_at': generated_at,
        'status': status,
        'current': True,
        'source': 'modules/experimenting/scripts/audits/audit_experiment_runtime_integrity.py',
        'policy': 'Runtime integrity is regenerated from the current read-only experiment watchdog and artifact-local launcher contracts. Automatic research loops must not enable stop mode; duplicate writers are contaminated and reported, not killed by default.',
        'watchdog_status': watchdog_status,
        'watchdog_generated_at': watchdog_payload.get('generated_at', '') if isinstance(watchdog_payload, dict) else '',
        'manifest_generated_at': manifest.get('generated_at', '') if isinstance(manifest, dict) else '',
        'active_run_count': watchdog_payload.get('active_run_count', 0) if isinstance(watchdog_payload, dict) else 0,
        'active_runs': active_runs,
        'issues': issues,
        'warnings': warnings,
        'stale_previous_runtime_integrity_replaced': bool(any(str(row.get('issue') or '').startswith('previous runtime integrity policy') for row in warnings)),
        'evidence': {
            'experiment_run_watchdog': str(paths.state / 'experiment_run_watchdog.json'),
            'experiment_run_manifest': str(paths.state / 'experiment_run_manifest.json'),
            'experiment_registry': str(registry_path),
            'experiment_record_table': str(record_table_path),
            'full_research_cycle': str(full_cycle_path),
        },
        'evidence_mtimes': {
            'experiment_run_watchdog': _mtime_iso(paths.state / 'experiment_run_watchdog.json'),
            'experiment_run_manifest': _mtime_iso(paths.state / 'experiment_run_manifest.json'),
            'experiment_registry': _mtime_iso(registry_path),
            'experiment_record_table': _mtime_iso(record_table_path),
            'full_research_cycle': _mtime_iso(full_cycle_path),
        },
    }
    save_json(paths.state / 'experiment_runtime_integrity.json', payload)
    lines = [
        '# Experiment Runtime Integrity\n\n',
        f"- status: {status}\n",
        f"- generated_at: {generated_at}\n",
        f"- watchdog_status: {watchdog_status}\n",
        f"- active_run_count: {payload['active_run_count']}\n",
        '- policy: read-only watchdog plus artifact-local launcher contracts; no automatic stop mode.\n\n',
        '## Issues\n',
    ]
    if issues:
        for issue in issues:
            lines.append(f"- {issue.get('issue')}\n")
    else:
        lines.append('- No blocking runtime integrity issue.\n')
    lines.append('\n## Warnings\n')
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning.get('issue')}\n")
    else:
        lines.append('- No warning.\n')
    (paths.reports / 'experiment_runtime_integrity.md').write_text(''.join(lines), encoding='utf-8')
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description='Regenerate current TASTE experiment runtime integrity from live watchdog and artifact contracts.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--skip-watchdog', action='store_true')
    args = parser.parse_args()
    payload = build_runtime_integrity_audit(args.project, run_watchdog=not args.skip_watchdog)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get('status') == 'pass' else 2 if payload.get('status') == 'blocked' else 1


if __name__ == '__main__':
    raise SystemExit(main())
