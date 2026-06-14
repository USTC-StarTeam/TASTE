#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path

from experiment_contracts import extract_bad_case_summary, parse_float
from build_experiment_record_table import build_experiment_record_table
from project_paths import build_paths


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def load_metrics_blob(metrics_json: str, metrics_path: str) -> dict:
    if metrics_json:
        return json.loads(metrics_json)
    if metrics_path and Path(metrics_path).exists():
        return json.loads(Path(metrics_path).read_text(encoding='utf-8'))
    return {}


def git_snapshot(repo_path: str) -> dict[str, object]:
    if not repo_path:
        return {}
    repo = Path(repo_path)
    if not repo.exists() or not (repo / '.git').exists():
        return {}

    def run(cmd: list[str]) -> str:
        proc = subprocess.run(cmd, cwd=repo, text=True, capture_output=True)
        return proc.stdout.strip() if proc.returncode == 0 else ''

    status = run(['git', 'status', '--short'])
    return {
        'git_commit': run(['git', 'rev-parse', 'HEAD']),
        'git_branch': run(['git', 'rev-parse', '--abbrev-ref', 'HEAD']),
        'repo_dirty': bool(status),
        'git_status_short': status,
    }


def experiment_identity(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get('experiment_id') or row.get('name') or ''),
        str(row.get('repo_path') or ''),
        str(row.get('dataset') or ''),
    )


def upsert(rows: list[dict], item: dict) -> list[dict]:
    identity = experiment_identity(item)
    legacy_identity = (identity[0], '', '')
    for index, row in enumerate(rows):
        row_identity = experiment_identity(row)
        if identity[0] and row_identity == identity:
            merged = dict(row)
            merged.update({k: v for k, v in item.items() if v not in ('', None, [], {})})
            rows[index] = merged
            return rows
        # Preserve backward compatibility only for records that never had repo/dataset context.
        if identity[0] and row_identity == legacy_identity and not row.get('repo_path') and not row.get('dataset'):
            merged = dict(row)
            merged.update({k: v for k, v in item.items() if v not in ('', None, [], {})})
            rows[index] = merged
            return rows
    rows.append(item)
    return rows


def rewrite_markdown(path: Path, rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda row: row.get('timestamp', ''), reverse=True)
    lines = [
        '# Experiment Log\n\n',
        '| Timestamp | Experiment | Method | Status | Metric | Result | Decision | Claim Verdict | Audit | Git |\n',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n',
    ]
    for row in rows:
        metric_bits = row.get('metric_name', '')
        if row.get('metric_value') is not None:
            metric_bits = f"{metric_bits}={row['metric_value']}" if metric_bits else str(row['metric_value'])
        audit_bits = 'ready' if row.get('audit_ready') else f"missing:{','.join(row.get('missing_audit_fields', []))}"
        lines.append(
            f"| {row.get('timestamp', '')} | {row.get('name', '')} | {row.get('method', '')} | {row.get('status', '')} | {metric_bits} | {row.get('result', '')} | {row.get('decision', '')} | {row.get('claim_verdict', '')} | {audit_bits} | {row.get('git_commit', '')[:8]} |\n"
        )
    lines.append('\n## Details\n')
    for row in rows[:20]:
        lines.extend([
            f"\n### {row.get('name', '')}\n\n",
            f"- experiment_id: {row.get('experiment_id', '')}\n",
            f"- method: {row.get('method', '')}\n",
            f"- method_slug: {row.get('method_slug', '')}\n",
            f"- dataset: {row.get('dataset', '')}\n",
            f"- benchmark: {row.get('benchmark', '')}\n",
            f"- status: {row.get('status', '')}\n",
            f"- audit_ready: {row.get('audit_ready', False)}\n",
            f"- missing_audit_fields: {', '.join(row.get('missing_audit_fields', []))}\n",
            f"- audit_path: {row.get('audit_path', '')}\n",
            f"- env_name: {row.get('env_name', '')}\n",
            f"- repo_path: {row.get('repo_path', '')}\n",
            f"- command: `{row.get('command', '')}`\n",
            f"- metric_name: {row.get('metric_name', '')}\n",
            f"- metric_value: {row.get('metric_value', '')}\n",
            f"- result: {row.get('result', '')}\n",
            f"- claim_verdict: {row.get('claim_verdict', '')}\n",
            f"- novelty_note: {row.get('novelty_note', '')}\n",
            f"- counterexample_outcome: {row.get('counterexample_outcome', '')}\n",
            f"- bad_case_slices: {', '.join(row.get('bad_case_slices', []))}\n",
            f"- return_code: {row.get('return_code', '')}\n",
            f"- duration_sec: {row.get('duration_sec', '')}\n",
            f"- artifact_path: {row.get('artifact_path', '')}\n",
            f"- bad_case_path: {row.get('bad_case_path', '')}\n",
            f"- failure_analysis_path: {row.get('failure_analysis_path', '')}\n",
            f"- git_commit: {row.get('git_commit', '')}\n",
            f"- repo_dirty: {row.get('repo_dirty', '')}\n",
            f"- notes: {row.get('notes', '')}\n",
        ])
    path.write_text(''.join(lines), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--experiment-id')
    parser.add_argument('--repo', default='')
    parser.add_argument('--repo-path', default='')
    parser.add_argument('--dataset', default='')
    parser.add_argument('--benchmark', default='')
    parser.add_argument('--method', default='')
    parser.add_argument('--method-slug', default='')
    parser.add_argument('--status', default='planned')
    parser.add_argument('--metric', default='')
    parser.add_argument('--metric-value')
    parser.add_argument('--metrics-json', default='')
    parser.add_argument('--metrics-path', default='')
    parser.add_argument('--result', default='')
    parser.add_argument('--notes', default='')
    parser.add_argument('--git_commit', default='')
    parser.add_argument('--artifact_path', default='')
    parser.add_argument('--env-name', default='')
    parser.add_argument('--command', default='')
    parser.add_argument('--return-code', type=int)
    parser.add_argument('--duration-sec', type=float)
    parser.add_argument('--started-at', default='')
    parser.add_argument('--finished-at', default='')
    parser.add_argument('--trial-index', type=int)
    parser.add_argument('--priority', type=int)
    parser.add_argument('--decision', default='')
    parser.add_argument('--bad-case-path', default='')
    parser.add_argument('--failure-analysis-path', default='')
    parser.add_argument('--claim-verdict', default='')
    parser.add_argument('--novelty-note', default='')
    parser.add_argument('--counterexample-outcome', default='')
    parser.add_argument('--audit-path', default='')
    parser.add_argument('--audit-ready', default='false')
    parser.add_argument('--missing-audit-fields', default='')
    parser.add_argument('--method-role', default='')
    parser.add_argument('--comparison-role', default='')
    parser.add_argument('--human-label', default='')
    parser.add_argument('--human-goal', default='')
    parser.add_argument('--config-summary', default='')
    args = parser.parse_args()

    paths = build_paths(args.project)
    registry_path = paths.state / 'experiment_registry.json'
    rows = load_json(registry_path)
    metrics = load_metrics_blob(args.metrics_json, args.metrics_path)
    metric_value = parse_float(args.metric_value)
    if metric_value is None and args.metric:
        metric_value = parse_float(metrics.get(args.metric))
    if metric_value is None and args.result and not str(args.result).startswith('return_code='):
        metric_value = parse_float(args.result)
    git_info = git_snapshot(args.repo_path)
    if args.git_commit:
        git_info['git_commit'] = args.git_commit

    bad_case_summary = extract_bad_case_summary(args.bad_case_path)
    timestamp = args.finished_at or args.started_at or dt.datetime.now(dt.timezone.utc).isoformat()
    item = {
        'timestamp': timestamp,
        'started_at': args.started_at,
        'finished_at': args.finished_at,
        'experiment_id': args.experiment_id or args.name,
        'name': args.name,
        'repo': args.repo,
        'repo_path': args.repo_path,
        'dataset': args.dataset,
        'benchmark': args.benchmark,
        'method': args.method,
        'method_slug': args.method_slug or args.method,
        'method_role': args.method_role,
        'comparison_role': args.comparison_role or args.method_role,
        'human_label': args.human_label,
        'human_goal': args.human_goal,
        'config_summary': args.config_summary,
        'status': args.status,
        'metric_name': args.metric,
        'metric_value': metric_value,
        'metrics': metrics,
        'result': args.result,
        'notes': args.notes,
        'artifact_path': args.artifact_path,
        'env_name': args.env_name,
        'command': args.command,
        'return_code': args.return_code,
        'duration_sec': args.duration_sec,
        'trial_index': args.trial_index,
        'priority': args.priority,
        'decision': args.decision,
        'bad_case_path': args.bad_case_path,
        'bad_case_summary': bad_case_summary,
        'bad_case_slices': bad_case_summary.get('slices', []),
        'failure_analysis_path': args.failure_analysis_path,
        'claim_verdict': args.claim_verdict,
        'novelty_note': args.novelty_note,
        'counterexample_outcome': args.counterexample_outcome,
        'audit_path': args.audit_path,
        'audit_ready': str(args.audit_ready).lower() in {'1', 'true', 'yes'},
        'missing_audit_fields': [item for item in args.missing_audit_fields.split(',') if item],
    }
    item.update(git_info)
    rows = upsert(rows, item)
    save_json(registry_path, rows)

    md_path = paths.experiments / 'experiment_log.md'
    rewrite_markdown(md_path, rows)
    build_experiment_record_table(args.project)
    print(md_path)


if __name__ == '__main__':
    main()
