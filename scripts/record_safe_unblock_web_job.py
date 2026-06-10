from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get('WORKSPACE_ROOT') or Path(__file__).resolve().parents[1]).expanduser().resolve()
WEB_JOBS = ROOT / 'modules' / 'taste' / 'auto_research' / 'state' / 'web_jobs.json'


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def tail(text: Any, limit: int = 700) -> str:
    text = ' '.join(str(text or '').replace('\n', ' ').split())
    return text[:limit] + ('...' if len(text) > limit else '')


def selected_base_label(selection: dict[str, Any]) -> str:
    selected = selection.get('selected', {}) if isinstance(selection.get('selected'), dict) else {}
    return str(selected.get('title') or selected.get('literature_base_title') or selected.get('selected_base_title') or selected.get('name') or selected.get('repo') or selected.get('repo_path') or '环境阶段选出的基底')


def main() -> int:
    parser = argparse.ArgumentParser(description='Record a hidden safe-unblock web job for the current project.')
    parser.add_argument('--project', default=os.environ.get('PROJECT_ID') or os.environ.get('DEFAULT_PROJECT_ID') or '')
    parser.add_argument('--venue', default='ICLR')
    args = parser.parse_args()
    project = str(args.project or "").strip()
    if not project:
        raise SystemExit('project is required; pass --project or set PROJECT_ID')
    state = ROOT / 'projects' / project / 'state'
    planning = ROOT / 'projects' / project / 'planning' / 'finding'
    job_id = f'safe-unblock_{project}'
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    full = load(state / 'full_research_cycle.json', {})
    blocker = load(state / 'blocker_action_plan.json', {})
    fda = load(state / 'fresh_base_data_acquisition.json', {})
    loader = load(state / 'real_dataset_probe.json', {})
    protocol = load(state / 'fresh_base_reference_protocol_probe.json', {})
    smoke = load(state / 'fresh_base_reference_smoke.json', {})
    find_plan = load(state / 'current_find_research_plan.json', {})
    selection = load(state / 'evidence_ready_repo_selection.json', {})
    readings = load(planning / 'read_results.json', {})
    ideas = load(planning / 'ideas.json', {})
    plans = load(planning / 'plans.json', {})

    base_label = selected_base_label(selection if isinstance(selection, dict) else {})
    attempts = fda.get('attempts', []) if isinstance(fda, dict) else []
    latest_attempt = attempts[-1] if attempts else {}
    ready = loader.get('ready_datasets', []) if isinstance(loader, dict) else []
    blocked = loader.get('blocked_datasets', []) if isinstance(loader, dict) else []
    route = blocker.get('summary', {}).get('top_route', '') if isinstance(blocker.get('summary'), dict) else ''
    loader_passed = bool(ready and isinstance(loader, dict) and (loader.get('decision') == 'loader_contract_passed' or any(isinstance(row, dict) and row.get('loader_probe_success') for row in loader.get('probes', []) if isinstance(loader.get('probes', []), list))))
    protocol_passed = bool(isinstance(protocol, dict) and protocol.get('status') == 'reference_protocol_probe_passed')
    smoke_passed = bool(isinstance(smoke, dict) and smoke.get('status') == 'reference_smoke_passed')
    full_status = str(full.get('status', '')) if isinstance(full, dict) else ''
    current_blocker = full.get('current_blocker', {}) if isinstance(full, dict) and isinstance(full.get('current_blocker'), dict) else {}

    if smoke_passed:
            message = f'{base_label} 有界 reference smoke 已通过；当前等待论文级 reference reproduction audit。'
            phase = 'safe-unblock-reference-reproduction'
    elif protocol_passed:
            message = f'{base_label} 参考协议已通过；当前自动尝试有界 no-training reference smoke。'
            phase = 'safe-unblock-reference-smoke'
    elif loader_passed:
            message = f'{base_label} 数据/loader 已通过；当前等待环境 manifest 与参考协议只读探针。'
            phase = 'safe-unblock-reference-probe'
    else:
            message = f'仍缺 {base_label} loader-ready 真实数据或 loader 合同；已继续尝试数据获取和只读 loader/import probe。'
            phase = 'safe-unblock-data'
    blocker_category = str(current_blocker.get('category') or phase)
    logs = [
        'TASTE safe-unblock heartbeat executed.',
        f'target_venue={args.venue} find_run={find_plan.get("run_id", "") if isinstance(find_plan, dict) else ""} selected_base={base_label}',
        f'full_status={full_status} blocker={blocker_category} top_route={route}',
        f'planning_packet=readings:{len(readings.get("readings", []) if isinstance(readings, dict) else [])} ideas:{len(ideas.get("ideas", []) if isinstance(ideas, dict) else [])} plans:{len(plans.get("plans", []) if isinstance(plans, dict) else [])}',
        f'data_status={fda.get("status", "") if isinstance(fda, dict) else ""} data_decision={fda.get("decision", "") if isinstance(fda, dict) else ""} ready_datasets={ready} blocked_datasets={blocked}',
        f'protocol_status={protocol.get("status", "") if isinstance(protocol, dict) else ""} smoke_status={smoke.get("status", "") if isinstance(smoke, dict) else ""}',
        f'latest_attempt={latest_attempt.get("kind", "") if isinstance(latest_attempt, dict) else ""} rc={latest_attempt.get("return_code", "") if isinstance(latest_attempt, dict) else ""} tail={tail((latest_attempt.get("stderr_tail") or latest_attempt.get("stdout_tail")) if isinstance(latest_attempt, dict) else "")}',
        'guardrail: no training, no paper writing, no claim promotion, no second Find, no pair_compare, no legacy main-route fallback.',
    ]
    job = {
        'job_id': job_id,
        'stage': 'safe-unblock',
        'internal': True,
        'display': 'hidden',
        'status': 'blocked',
        'created_at': now,
        'logs': logs,
        'log_count': len(logs),
        'result': {
            'project': project,
            'target_venue': args.venue,
            'find_run_id': find_plan.get('run_id', '') if isinstance(find_plan, dict) else '',
            'main_base': base_label,
            'full_status': full_status,
            'blocker': blocker_category,
            'top_route': route,
            'ready_datasets': ready,
            'blocked_datasets': blocked,
            'latest_download_attempt': latest_attempt,
        },
        'error': '',
        'cancel_requested': False,
        'cancelled_at': '',
        'progress': {'phase': phase, 'current': 1, 'total': 1, 'percent': 100, 'message': message},
    }
    web = load(WEB_JOBS, {'jobs': []})
    jobs = [j for j in web.get('jobs', []) if isinstance(j, dict) and j.get('job_id') != job_id]
    jobs.insert(0, job)
    web['jobs'] = jobs[:300]
    save(WEB_JOBS, web)
    print(WEB_JOBS)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
