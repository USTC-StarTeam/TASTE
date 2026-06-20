#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import argparse
import json
import re
from pathlib import Path

from experiment_contracts import SUPPORTIVE_CLAIM_VERDICTS, WEAK_CLAIM_VERDICTS, row_promotion_blockers
from project_paths import build_paths
from pipeline_guard import guard_fresh_base_blocker_entry

CLAIM_PATTERNS = [
    re.compile(r'^- Headline claim under test:(.*)$', re.MULTILINE),
    re.compile(r'^- Scope boundary:(.*)$', re.MULTILINE),
    re.compile(r'^- Minimal evidence contract:(.*)$', re.MULTILINE),
]


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def norm_path(value: object) -> str:
    return str(value or '').rstrip('/')


def current_route_row(row: dict, active_repo: dict) -> bool:
    """Only current selected-base experiments may support current paper claims."""
    active_path = norm_path(active_repo.get('repo_path') or active_repo.get('local_path'))
    active_name = str(active_repo.get('name') or active_repo.get('repo_name') or '').strip().lower()
    row_path = norm_path(row.get('repo_path') or row.get('active_repo_path') or row.get('local_path'))
    row_name = str(row.get('repo_name') or row.get('repo') or row.get('base_repo') or '').strip().lower()
    if active_path and row_path:
        return row_path == active_path
    if active_name and row_name:
        return row_name == active_name
    return False


def extract_claims(text: str) -> list[dict[str, str]]:
    claims = []
    labels = ['headline_claim', 'scope_boundary', 'minimal_evidence_contract']
    for label, pattern in zip(labels, CLAIM_PATTERNS):
        match = pattern.search(text)
        claims.append({'claim_type': label, 'text': match.group(1).strip() if match else ''})
    return claims


def score_claim(support_count: int, weakening_count: int, weak_only_count: int = 0) -> str:
    if support_count == 0 and weakening_count == 0:
        return 'weak' if weak_only_count else 'unsupported'
    if support_count == 0 and weakening_count > 0:
        return 'unsupported'
    if support_count > 0 and weakening_count == 0:
        return 'supported' if support_count >= 2 else 'partially_supported'
    if support_count > weakening_count:
        return 'mixed'
    return 'weak'


def run_is_reproduction_only(row: dict) -> bool:
    text = ' '.join(str(row.get(key, '')) for key in ('novelty_note', 'counterexample_outcome', 'benchmark', 'method', 'notes')).lower()
    return any(token in text for token in ['reproduction only', 'smoke reproduction', 'no novelty', 'no novelty or improvement', 'cannot support final'])


def run_has_real_claim_support(row: dict, active_repo: dict) -> bool:
    if not current_route_row(row, active_repo):
        return False
    if str(row.get('status', '')).lower() not in {'completed', 'success'} or not row.get('audit_ready'):
        return False
    if row_promotion_blockers(row):
        return False
    if run_is_reproduction_only(row):
        return False
    novelty = str(row.get('novelty_note', '')).lower()
    counter = str(row.get('counterexample_outcome', '')).lower()
    if any(token in novelty for token in ['no novelty', 'not support', 'reproduction only', 'smoke']):
        return False
    if any(token in counter for token in ['blocked', 'not tested', 'strong claims remain blocked', 'counterexample']):
        return False
    return str(row.get('claim_verdict', '')).strip().lower() in SUPPORTIVE_CLAIM_VERDICTS


def claim_matches_run(claim_type: str, row: dict, active_repo: dict) -> bool:
    note = ' '.join(str(row.get(key, '')) for key in ('novelty_note', 'counterexample_outcome', 'notes')).lower()
    if claim_type == 'headline_claim':
        return run_has_real_claim_support(row, active_repo)
    if claim_type == 'scope_boundary':
        return run_has_real_claim_support(row, active_repo) and ('scope' in note or 'slice' in note or bool(row.get('bad_case_slices')))
    if claim_type == 'minimal_evidence_contract':
        return bool(row.get('audit_ready')) and not run_is_reproduction_only(row) and run_has_real_claim_support(row, active_repo)
    return run_has_real_claim_support(row, active_repo)


def claim_weak_evidence_run(claim_type: str, row: dict) -> bool:
    if not row.get('audit_ready'):
        return False
    if row_promotion_blockers(row):
        return True
    if run_is_reproduction_only(row):
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, "", Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paths = build_paths(args.project)
    draft = (paths.root / 'paper' / 'drafts' / 'paper_draft.md').read_text(encoding='utf-8') if (paths.root / 'paper' / 'drafts' / 'paper_draft.md').exists() else ''
    experiments = load_json(paths.state / 'experiment_registry.json', [])
    active_repo = load_json(paths.state / 'active_repo.json', {})
    if not isinstance(active_repo, dict):
        active_repo = {}
    claims = extract_claims(draft)
    for claim in claims:
        supporting = []
        weakening = []
        weak_only = []
        claim_text = str(claim.get('text') or '').strip().lower()
        if not claim_text or claim_text == 'missing':
            claim['supporting_runs'] = []
            claim['weakening_runs'] = []
            claim['weak_only_runs'] = []
            claim['support_count'] = 0
            claim['weakening_count'] = 0
            claim['weak_only_count'] = 0
            claim['status'] = 'unsupported'
            continue
        for row in experiments:
            if claim_weak_evidence_run(claim.get('claim_type', ''), row):
                weak_only.append(row.get('name', ''))
            if not claim_matches_run(claim.get('claim_type', ''), row, active_repo):
                continue
            verdict = str(row.get('claim_verdict', '')).strip().lower()
            if verdict in SUPPORTIVE_CLAIM_VERDICTS:
                supporting.append(row.get('name', ''))
            if verdict in WEAK_CLAIM_VERDICTS:
                weakening.append(row.get('name', ''))
        claim['supporting_runs'] = supporting[:10]
        claim['weakening_runs'] = weakening[:10]
        claim['weak_only_runs'] = weak_only[:10]
        claim['support_count'] = len(supporting)
        claim['weakening_count'] = len(weakening)
        claim['weak_only_count'] = len(weak_only)
        claim['status'] = score_claim(len(supporting), len(weakening), len(weak_only))
    out_json = paths.state / 'claim_ledger.json'
    out_md = paths.planning / 'claim_ledger.md'
    out_json.write_text(json.dumps({'claims': claims}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = ['# Claim Ledger\n\n']
    for idx, claim in enumerate(claims, start=1):
        lines.append(f'## Claim {idx}: {claim["claim_type"]}\n\n')
        lines.append(f'- text: {claim["text"] or "missing"}\n')
        lines.append(f'- status: {claim["status"]}\n')
        lines.append(f'- support_count: {claim["support_count"]}\n')
        lines.append(f'- weakening_count: {claim["weakening_count"]}\n')
        lines.append(f'- weak_only_count: {claim.get("weak_only_count", 0)}\n')
        lines.append(f'- supporting_runs: {", ".join(claim["supporting_runs"]) if claim["supporting_runs"] else "none"}\n')
        lines.append(f'- weakening_runs: {", ".join(claim["weakening_runs"]) if claim["weakening_runs"] else "none"}\n')
        lines.append(f'- weak_only_runs: {", ".join(claim.get("weak_only_runs", [])) if claim.get("weak_only_runs") else "none"}\n\n')
    out_md.write_text(''.join(lines), encoding='utf-8')
    print(out_md)


if __name__ == '__main__':
    main()
