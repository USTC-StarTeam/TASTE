#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from project_paths import build_paths


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


def norm_path(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    if annotated_missing_path(text):
        return ''
    try:
        return str(Path(text).resolve())
    except Exception:
        return text


def annotated_missing_path(value: Any) -> bool:
    text = str(value or '').strip().lower()
    if not text:
        return False
    markers = [
        'does not exist',
        "doesn't exist",
        'not found',
        'no such file',
        '(missing)',
        ' missing',
        'missing ',
    ]
    return any(marker in text for marker in markers)


def path_hint(value: Any) -> str:
    text = str(value or '').strip()
    return one_line(text, 360) if text else ''


def existing_path(value: Any) -> str:
    path = norm_path(value)
    if not path:
        return ''
    try:
        return path if Path(path).exists() else ''
    except Exception:
        return ''


def one_line(value: Any, limit: int = 360) -> str:
    text = ' '.join(str(value or '').replace('\n', ' ').split())
    return text[:limit] + ('...' if len(text) > limit else '')


def safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def text_contains(path: Path, needles: list[str]) -> bool:
    try:
        if not path.exists():
            return False
        text = path.read_text(encoding='utf-8', errors='replace').lower()
    except Exception:
        return False
    return any(needle and needle.lower() in text for needle in needles)


def payload_contains(payload: Any, needle: str) -> bool:
    if not needle:
        return False
    try:
        return needle.lower() in json.dumps(payload, ensure_ascii=False).lower()
    except Exception:
        return False


def payload_repo_path(payload: Any) -> str:
    row = safe_dict(payload)
    for key in ['repo_path', 'active_repo_path', 'local_path', 'path']:
        value = norm_path(row.get(key))
        if value:
            return value
    for key in ['repo', 'selected_repo', 'new_route', 'proposed_route']:
        child = row.get(key)
        if isinstance(child, dict):
            value = payload_repo_path(child)
            if value:
                return value
    return ''


def matches_repo(payload: Any, repo_path: str, repo_name: str = '') -> bool:
    row = safe_dict(payload)
    repo_path = norm_path(repo_path)
    own = payload_repo_path(row)
    if repo_path and own and own == repo_path:
        return True
    if repo_path and payload_contains(row, repo_path):
        return True
    if repo_name and payload_contains(row, repo_name):
        return True
    return False


def current_find_run_id(paths) -> str:
    for rel in [
        paths.planning / 'finding' / 'find_progress.json',
        paths.state / 'current_find_research_plan.json',
        paths.state / 'evidence_ready_repo_selection.json',
    ]:
        payload = load_json(rel, {})
        if isinstance(payload, dict):
            for key in ['run_id', 'source_run_id', 'find_run_id', 'fresh_find_run_id', 'current_find_run_id']:
                value = str(payload.get(key) or '').strip()
                if value:
                    return value
    return ''


def selected_route(paths) -> dict[str, Any]:
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    selected = safe_dict(selection.get('selected')) if isinstance(selection, dict) else {}
    active = load_json(paths.state / 'active_repo.json', {})
    reference_gate = load_json(paths.state / 'reference_reproduction_gate.json', {})
    audit = load_json(paths.state / 'fresh_base_reference_reproduction_audit.json', {})
    repo_path = norm_path(
        selected.get('repo_path')
        or safe_dict(active).get('repo_path')
        or safe_dict(reference_gate).get('active_repo_path')
        or safe_dict(audit).get('repo_path')
    )
    name = str(selected.get('name') or safe_dict(active).get('name') or safe_dict(audit).get('repo_name') or '').strip()
    title = str(selected.get('literature_base_title') or safe_dict(active).get('selected_base_title') or safe_dict(audit).get('paper_title') or safe_dict(audit).get('base_title') or '').strip()
    dataset = str(selected.get('claim_ready_dataset') or safe_dict(audit).get('dataset') or '').strip()
    return {'name': name, 'title': title, 'dataset': dataset, 'repo_path': repo_path}


def proposal_from_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    route_proposal = safe_dict(payload.get('route_proposal'))
    cycle2 = safe_dict(payload.get('cycle2_ideation'))
    cycle2_proposal = safe_dict(cycle2.get('route_switch_proposal'))
    proposed = (
        safe_dict(payload.get('proposed_route'))
        or safe_dict(payload.get('candidate_route'))
        or safe_dict(payload.get('new_route'))
        or safe_dict(route_proposal.get('primary_candidate'))
        or safe_dict(cycle2_proposal.get('primary'))
    )
    current = safe_dict(payload.get('current_route')) or safe_dict(payload.get('previous_route')) or safe_dict(payload.get('current_cycle_status')) or safe_dict(payload.get('current_route_status'))
    repo = str(proposed.get('repo') or proposed.get('name') or proposed.get('repo_name') or '').strip()
    title = str(proposed.get('title') or proposed.get('paper_title') or proposed.get('selected_base_title') or '').strip()
    raw_path_values = [proposed.get('repo_path'), proposed.get('local_path'), proposed.get('path')]
    proposed_path_hint = next((path_hint(value) for value in raw_path_values if path_hint(value)), '')
    repo_path = ''
    for raw_path in raw_path_values:
        repo_path = existing_path(raw_path)
        if repo_path:
            break
    dataset = str(proposed.get('dataset') or proposed.get('benchmark') or '').strip()
    status = str(payload.get('status') or route_proposal.get('status') or proposed.get('status') or '').strip()
    proposal_type = str(payload.get('type') or route_proposal.get('type') or 'route_switch_proposal' if (route_proposal or cycle2_proposal) else '').strip()
    return {
        'source': str(path),
        'status': status,
        'type': proposal_type,
        'generated_at': str(payload.get('generated_at') or payload.get('updated_at') or payload.get('executed_at') or '').strip(),
        'fresh_find_run_id': str(payload.get('fresh_find_run_id') or payload.get('current_find_run_id') or proposed.get('fresh_find_run_id') or '').strip(),
        'repo': repo,
        'title': title,
        'dataset': dataset,
        'repo_path': repo_path,
        'proposed_path_hint': proposed_path_hint,
        'current_route': current,
        'raw_keys': list(payload)[:40],
        'invalid_execution': str(payload.get('status') or '').startswith('invalid'),
        'authorized_execution': str(payload.get('status') or '').startswith('authorized'),
        'requires_deterministic_base_switch_gate': bool(route_proposal.get('requires_deterministic_base_switch_gate') or 'deterministic_base_switch_gate' in str(proposed.get('required_gate') or '')),
    }


def _markdown_field(text: str, labels: list[str]) -> str:
    for label in labels:
        pattern = rf'(?:^|\n)\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[:：]\s*([^\n]+)'
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = re.sub(r'[`*_]', '', match.group(1)).strip()
            if value:
                return one_line(value, 240)
    return ''


def proposal_from_markdown(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding='utf-8', errors='replace')[:20000]
    status_match = re.search(r'\*\*Status\*\*\s*:\s*([^\n]+)', text)
    repo = _markdown_field(text, ['repo', 'repository', 'candidate repo', 'candidate repository', '仓库', '候选仓库'])
    title = _markdown_field(text, ['title', 'paper title', 'candidate title', '论文', '论文标题', '候选论文'])
    dataset = _markdown_field(text, ['dataset', 'benchmark', '数据集'])
    return {
        'source': str(path),
        'status': one_line(status_match.group(1), 220) if status_match else '',
        'type': 'route_switch_proposal_markdown',
        'generated_at': '',
        'fresh_find_run_id': '',
        'repo': repo,
        'title': title,
        'dataset': dataset,
        'repo_path': '',
        'proposed_path_hint': '',
        'current_route': {},
        'raw_keys': [],
        'invalid_execution': False,
        'authorized_execution': False,
    }


def collect_proposals(paths) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    json_paths = []
    for name in ['next_cycle_route_proposal.json', 'ideation_cycle2_plan.json', 'selected_route_switch_proposal.json']:
        path = paths.state / name
        if path.exists():
            json_paths.append(path)
    json_paths.extend(sorted(paths.state.glob('*route_switch_proposal*.json')))
    json_paths.extend(sorted(paths.state.glob('*base_switch_execution.json')))
    seen_json: set[Path] = set()
    for path in json_paths:
        if path in seen_json:
            continue
        seen_json.add(path)
        payload = load_json(path, {})
        if isinstance(payload, dict):
            proposal = proposal_from_json(path, payload)
            if proposal.get('repo') or proposal.get('title') or proposal.get('repo_path'):
                proposals.append(proposal)
    for path in sorted(paths.planning.glob('route_switch_proposal*.md')):
        try:
            proposal = proposal_from_markdown(path)
            if proposal.get('repo') or proposal.get('title'):
                proposals.append(proposal)
        except Exception:
            continue
    return proposals


def infer_repo_path(paths, repo_name: str, title: str = '') -> str:
    tokens: list[str] = []
    repo_name = str(repo_name or '').strip()
    title = str(title or '').strip()
    if repo_name:
        tokens.append(repo_name.split('/')[-1])
        tokens.append(repo_name.replace('/', '_'))
    if title:
        tokens.append(title.split(':', 1)[0])
    normalized_tokens = []
    for token in tokens:
        clean = re.sub(r'[^a-z0-9]+', '_', token.lower()).strip('_')
        if clean:
            normalized_tokens.append(clean)
    roots = [paths.root / 'repos' / 'selected', paths.root / 'repos']
    for root in roots:
        if not root.exists():
            continue
        for child in root.rglob('*'):
            if not child.is_dir():
                continue
            name = child.name.lower()
            if any(token == name or token in name or name.endswith(token) for token in normalized_tokens):
                return str(child.resolve())
    return ''


def choose_candidate(proposals: list[dict[str, Any]], current_repo_path: str) -> dict[str, Any]:
    current_repo_path = norm_path(current_repo_path)
    candidates = [p for p in proposals if norm_path(p.get('repo_path')) != current_repo_path or not p.get('repo_path')]
    structured = [p for p in candidates if Path(str(p.get('source') or '')).name in {'next_cycle_route_proposal.json', 'ideation_cycle2_plan.json', 'selected_route_switch_proposal.json'}]
    if structured:
        return structured[0]
    json_candidates = [p for p in candidates if str(p.get('source') or '').endswith('.json') and p.get('type') == 'route_switch_proposal']
    if json_candidates:
        return json_candidates[0]
    non_invalid = [p for p in candidates if not p.get('invalid_execution')]
    return (non_invalid or candidates or [{}])[0]


def candidate_find_provenance(paths, candidate: dict[str, Any], run_id: str) -> dict[str, Any]:
    title = str(candidate.get('title') or '').strip()
    repo = str(candidate.get('repo') or '').strip()
    needles = [value for value in [title, repo.split('/')[-1] if repo else '', repo] if value]
    in_find = text_contains(paths.planning / 'finding' / 'find_results.json', needles)
    in_read = text_contains(paths.planning / 'finding' / 'read_results.json', needles)
    proposal_run = str(candidate.get('fresh_find_run_id') or '').strip()
    run_matches = bool(proposal_run and run_id and proposal_run == run_id)
    return {
        'current_find_run_id': run_id,
        'proposal_find_run_id': proposal_run,
        'candidate_in_current_find_results': in_find,
        'candidate_in_current_read_results': in_read,
        'clear': bool(run_matches or in_find or in_read),
        'evidence': [
            str(paths.planning / 'finding' / 'find_results.json'),
            str(paths.planning / 'finding' / 'read_results.json'),
            str(paths.state / 'current_find_research_plan.json'),
        ],
    }


def find_matching_payload(paths, patterns: list[str], repo_path: str, repo_name: str, predicate) -> tuple[bool, str, dict[str, Any]]:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(sorted(paths.state.glob(pattern)))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        if not matches_repo(payload, repo_path, repo_name):
            continue
        try:
            passed = bool(predicate(payload))
        except Exception:
            passed = False
        if passed:
            return True, str(path), payload
    return False, '', {}


def build_check(check_id: str, ok: bool, detail: str, evidence: list[str] | None = None, severity: str = 'block') -> dict[str, Any]:
    return {
        'id': check_id,
        'status': 'pass' if ok else 'blocked',
        'severity': 'pass' if ok else severity,
        'detail': detail,
        'evidence': evidence or [],
    }


def build_gate(project: str, venue: str = '') -> dict[str, Any]:
    paths = build_paths(project)
    run_id = current_find_run_id(paths)
    selected = selected_route(paths)
    selected_repo_path = norm_path(selected.get('repo_path'))
    selected_base_viability = load_json(paths.state / 'selected_base_viability_gate.json', {})
    reference_gate = load_json(paths.state / 'reference_reproduction_gate.json', {})
    base_switch_execution = load_json(paths.state / 'base_switch_execution.json', {})
    proposals = collect_proposals(paths)
    candidate = choose_candidate(proposals, selected_repo_path)
    candidate_repo = str(candidate.get('repo') or '').strip()
    candidate_title = str(candidate.get('title') or '').strip()
    candidate_repo_path = existing_path(candidate.get('repo_path')) or infer_repo_path(paths, candidate_repo, candidate_title)
    if candidate_repo_path and isinstance(candidate, dict):
        candidate['repo_path'] = candidate_repo_path
    elif isinstance(candidate, dict):
        candidate['repo_path'] = ''
    candidate_name = candidate_repo or candidate_title or candidate_repo_path
    provenance = candidate_find_provenance(paths, candidate, run_id) if candidate else {'clear': False, 'evidence': []}

    selected_gate_required = bool(
        isinstance(selected_base_viability, dict)
        and selected_base_viability.get('status') == 'blocked'
        and selected_base_viability.get('decision') == 'base_switch_gate_required'
    )
    reference_passed = bool(
        isinstance(reference_gate, dict)
        and reference_gate.get('status') == 'pass'
        and reference_gate.get('decision') == 'continue_base'
    )
    proposal_non_authoritative = bool(
        candidate
        and (
            'non_authoritative' in str(candidate.get('status') or '').lower()
            or 'proposal' in str(candidate.get('type') or '').lower()
            or str(candidate.get('source') or '').endswith('.md')
        )
        and not candidate.get('authorized_execution')
    )
    candidate_distinct = bool(candidate and (not candidate_repo_path or candidate_repo_path != selected_repo_path) and candidate_name)
    invalid_execution = bool(isinstance(base_switch_execution, dict) and str(base_switch_execution.get('status') or '').startswith('invalid'))
    preexisting_authorized_execution = bool(isinstance(base_switch_execution, dict) and str(base_switch_execution.get('status') or '').startswith('authorized'))

    loader_ok, loader_path, loader_payload = find_matching_payload(
        paths,
        ['*loader*probe*.json', 'real_dataset_probe.json'],
        candidate_repo_path,
        candidate_repo,
        lambda p: p.get('decision') == 'loader_contract_passed'
        or p.get('loader_probe_success') is True
        or any(isinstance(row, dict) and row.get('loader_probe_success') for row in safe_list(p.get('probes'))),
    )
    data_ok, data_path, data_payload = find_matching_payload(
        paths,
        ['*data*contract*.json', '*data_acquisition*.json'],
        candidate_repo_path,
        candidate_repo,
        lambda p: p.get('status') == 'ready' and p.get('decision') == 'ready_for_loader_probe' and bool(p.get('ready_datasets')),
    )
    protocol_ok, protocol_path, protocol_payload = find_matching_payload(
        paths,
        ['*reference_protocol_probe.json'],
        candidate_repo_path,
        candidate_repo,
        lambda p: p.get('status') == 'reference_protocol_probe_passed' and p.get('decision') == 'ready_for_bounded_reference_smoke',
    )
    smoke_ok, smoke_path, smoke_payload = find_matching_payload(
        paths,
        ['*reference_smoke.json'],
        candidate_repo_path,
        candidate_repo,
        lambda p: p.get('status') == 'reference_smoke_passed' and p.get('decision') == 'ready_for_reference_reproduction_audit',
    )
    full_ok, full_path, full_payload = find_matching_payload(
        paths,
        ['*reference_reproduction_audit.json'],
        candidate_repo_path,
        candidate_repo,
        lambda p: p.get('status') == 'completed_reference_reproduction'
        and int(p.get('return_code') or 0) == 0
        and p.get('audit_ready') is True
        and p.get('paper_level_reproduction_passed') is True,
    )
    artifact_local_ok = bool(full_ok and (full_payload.get('artifact_dir') or full_payload.get('stdout_path')) and full_payload.get('hashes') is not None)

    checks = [
        build_check('selected_base_viability_requires_gate', selected_gate_required, 'selected_base_viability_gate must be blocked/base_switch_gate_required before any switch authorization.', [str(paths.state / 'selected_base_viability_gate.json')]),
        build_check('selected_base_reference_reproduction_passed', reference_passed, 'current selected-base reference reproduction must pass first.', [str(paths.state / 'reference_reproduction_gate.json')]),
        build_check('candidate_route_proposal_exists', bool(candidate), 'a candidate route proposal must exist and remain separate from execution.', [str(p.get('source')) for p in proposals if p.get('source')][:8]),
        build_check('candidate_route_is_non_authoritative', proposal_non_authoritative, 'candidate route must be a non-authoritative proposal, not an already executed/authorized switch.', [str(candidate.get('source') or '')] if candidate else []),
        build_check('candidate_route_distinct_from_selected_base', candidate_distinct, 'candidate route must be distinct from current selected-base.', [str(candidate.get('source') or '')] if candidate else []),
        build_check('candidate_find_run_provenance_clear', bool(provenance.get('clear')), 'candidate must be traceable to current Find/read evidence or an explicit matching fresh_find_run_id.', safe_list(provenance.get('evidence'))),
        build_check('candidate_loader_import_probe_passed', loader_ok, 'candidate loader/import probe must pass for the candidate repo.', [loader_path] if loader_path else []),
        build_check('candidate_data_contract_passed', data_ok, 'candidate real-data contract must pass for the candidate repo.', [data_path] if data_path else []),
        build_check('candidate_reference_protocol_passed', protocol_ok, 'candidate reference protocol/env manifest probe must pass.', [protocol_path] if protocol_path else []),
        build_check('candidate_reference_smoke_passed', smoke_ok, 'candidate bounded no-training smoke must pass.', [smoke_path] if smoke_path else []),
        build_check('candidate_full_reference_reproduction_passed', full_ok, 'candidate full reference reproduction must pass with paper-level audit readiness.', [full_path] if full_path else []),
        build_check('candidate_artifact_local_audit_ready', artifact_local_ok, 'candidate full reproduction audit must record artifact-local logs/hashes.', [full_path] if full_path else []),
        build_check('previous_invalid_switch_is_not_authorization', True, 'previous invalid_unapproved_switch is retained as audit history and does not authorize or block the new deterministic gate.', [str(paths.state / 'base_switch_execution.json')]),
        build_check('no_preexisting_unaudited_authorization', not preexisting_authorized_execution, 'base_switch_execution must not claim authorization before this deterministic gate passes.', [str(paths.state / 'base_switch_execution.json')]),
    ]

    if not selected_gate_required:
        status = 'not_applicable'
        decision = 'not_required'
        switch_authorized = False
        if preexisting_authorized_execution:
            summary = 'selected-base viability gate does not request base-switch authorization; existing base_switch_execution authorization is stale/non-authoritative and must be invalidated by the route guard.'
        else:
            summary = 'selected-base viability gate does not currently request base-switch authorization.'
    else:
        failed = [row for row in checks if row.get('status') != 'pass']
        if failed:
            switch_authorized = False
            status = 'blocked'
            decision = 'base_switch_not_authorized'
            summary = 'deterministic base-switch gate did not authorize a route switch; keep selected-base unchanged and proposals non-authoritative.'
        else:
            switch_authorized = True
            status = 'pass'
            decision = 'authorize_base_switch'
            summary = 'deterministic base-switch gate authorizes switching to the audited candidate route; execution still requires execute_authorized_base_switch.py and must not promote paper claims by itself.'

    return {
        'project': project,
        'venue': venue,
        'updated_at': now_iso(),
        'status': status,
        'decision': decision,
        'switch_authorized': switch_authorized,
        'authorization_status': 'authorized' if switch_authorized else 'not_authorized',
        'summary': summary,
        'summary_zh': '候选路线证据已通过确定性门控，可进入受控切换执行；执行前当前 selected-base 仍保持不变，且不能自动提升论文结论。' if switch_authorized else '候选路线证据只记录为 proposal；当前权威 selected-base 必须保持不变，不能自动切换到任何历史或候选路线。',
        'current_selected_route': selected,
        'candidate_route': {
            'repo': candidate_repo,
            'title': candidate_title,
            'dataset': candidate.get('dataset', ''),
            'repo_path': candidate_repo_path,
            'proposed_path_hint': candidate.get('proposed_path_hint', ''),
            'source': candidate.get('source', ''),
            'status': candidate.get('status', ''),
            'type': candidate.get('type', ''),
        } if candidate else {},
        'current_find_run_id': run_id,
        'candidate_find_provenance': provenance,
        'base_switch_execution_status': safe_dict(base_switch_execution).get('status', ''),
        'base_switch_execution_authoritative': False,
        'base_switch_execution_action': 'invalidate_stale_execution' if preexisting_authorized_execution else 'none',
        'previous_invalid_switch_recorded': invalid_execution,
        'checks': checks,
        'failed_checks': [row for row in checks if row.get('status') != 'pass'],
        'evidence': [
            str(paths.state / 'selected_base_viability_gate.json'),
            str(paths.state / 'reference_reproduction_gate.json'),
            str(paths.state / 'base_switch_execution.json'),
            str(paths.state / 'evidence_ready_repo_selection.json'),
            str(paths.state / 'active_repo.json'),
        ] + [str(p.get('source')) for p in proposals if p.get('source')][:8],
        'guardrail': 'This gate is deterministic and read-only: it does not edit active_repo.json or evidence_ready_repo_selection.json. A passed gate authorizes only the separate execute_authorized_base_switch.py step; it does not promote paper claims or import experiments by itself.',
    }


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / 'base_switch_gate.md'
    lines = [
        '# Deterministic Base-Switch Gate\n\n',
        f"- status: {payload.get('status', '')}\n",
        f"- decision: {payload.get('decision', '')}\n",
        f"- switch_authorized: {payload.get('switch_authorized', False)}\n",
        f"- summary: {payload.get('summary', '')}\n",
        f"- summary_zh: {payload.get('summary_zh', '')}\n",
        '\n## Current Selected Route\n',
    ]
    selected = safe_dict(payload.get('current_selected_route'))
    for key in ['name', 'title', 'dataset', 'repo_path']:
        lines.append(f"- {key}: {selected.get(key, '')}\n")
    lines.append('\n## Candidate Route\n')
    candidate = safe_dict(payload.get('candidate_route'))
    for key in ['repo', 'title', 'dataset', 'repo_path', 'proposed_path_hint', 'source', 'status']:
        lines.append(f"- {key}: {candidate.get(key, '')}\n")
    lines.append('\n## Checks\n')
    for row in safe_list(payload.get('checks')):
        lines.append(f"- [{row.get('status')}] {row.get('id')}: {row.get('detail')}\n")
    lines.append('\n## Evidence\n')
    for item in safe_list(payload.get('evidence'))[:20]:
        lines.append(f'- {item}\n')
    out.write_text(''.join(lines), encoding='utf-8')
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description='Audit whether a candidate route is deterministically authorized to replace the current selected base.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', default='')
    args = parser.parse_args()
    paths = build_paths(args.project)
    payload = build_gate(args.project, args.venue)
    save_json(paths.state / 'base_switch_gate.json', payload)
    report = write_report(paths, payload)
    print(report)
    return 0 if payload.get('status') in {'pass', 'not_applicable'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
