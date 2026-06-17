#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from literature_policy import (
    build_literature_policy,
    core_topic_fit_from_text,
    dedupe_keep_order,
    now_utc,
    paper_sort_key,
    score_paper,
)
from project_paths import ROOT, build_paths, load_project_config
from taste_pythonpath import ensure_taste_pythonpath

ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection


# ---- paper_quality tool ----
WORD_RE = re.compile('[a-zA-Z][a-zA-Z0-9_\\-/]{2,}')

NOVELTY_POSITIVE = {'novel', 'new', 'first', 'unified', 'generalizable', 'generalization', 'framework', 'paradigm', 'scaling', 'frontier'}

NOVELTY_NEGATIVE = {'simple', 'incremental', 'efficient', 'faster', 'lightweight', 'tuning', 'adapter', 'refinement', 'engineering'}

CLAIM_POSITIVE = {'ablation', 'benchmark', 'benchmarks', 'evaluation', 'compare', 'comparison', 'analysis', 'robustness', 'error'}

COUNTEREXAMPLE_POSITIVE = {'failure', 'failures', 'limitation', 'limitations', 'counterexample', 'adversarial', 'stress', 'robustness', 'worst-case'}

POSITIVE = {'assumption', 'generalization', 'scaling', 'long-horizon', 'reasoning', 'compositional', 'multimodal', 'search', 'agent'}

def _paper_quality_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

def _paper_quality_save_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def _paper_quality_tokenize(text: str) -> set[str]:
    return {token.lower() for token in WORD_RE.findall(text or '')}

def _paper_quality_bucket(score: int) -> str:
    if score >= 4:
        return 'high'
    if score <= 1:
        return 'low'
    return 'medium'

def _paper_quality_capped_count(tokens: set[str], keywords: set[str]) -> int:
    return sum((1 for token in tokens if token in keywords))

def _paper_quality_heuristic_signals(meta: dict[str, object]) -> dict[str, object]:
    title = str(meta.get('title', '') or '')
    summary = str(meta.get('summary', '') or meta.get('tldr', '') or '')
    text = f'{title} {summary}'.lower()
    tokens = _paper_quality_tokenize(text)
    novelty_score = 2 + min(2, _paper_quality_capped_count(tokens, NOVELTY_POSITIVE)) - min(2, _paper_quality_capped_count(tokens, NOVELTY_NEGATIVE))
    claim_score = 1 + min(3, _paper_quality_capped_count(tokens, CLAIM_POSITIVE))
    counterexample_score = min(4, _paper_quality_capped_count(tokens, COUNTEREXAMPLE_POSITIVE))
    taste_score = 1 + min(3, _paper_quality_capped_count(tokens, POSITIVE))
    broad_claim = any((phrase in text for phrase in ['state-of-the-art', 'sota', 'general', 'generalizable', 'frontier']))
    has_limitations = any((token in tokens for token in {'limitation', 'limitations', 'failure', 'failures'}))
    if broad_claim and (not has_limitations):
        counterexample_score = min(4, counterexample_score + 1)
    if 'benchmark' in text or 'benchmarks' in text:
        claim_score = min(4, claim_score + 1)
    if 'ablation' in text:
        claim_score = min(4, claim_score + 1)
    if 'new dataset' in text or 'new benchmark' in text:
        novelty_score = min(4, novelty_score + 1)
    novelty = _paper_quality_bucket(max(0, novelty_score))
    claim_strength = _paper_quality_bucket(max(0, claim_score))
    counterexample_pressure = _paper_quality_bucket(counterexample_score)
    taste = _paper_quality_bucket(max(0, taste_score))
    return {'title': title, 'novelty': novelty, 'claim_strength': claim_strength, 'counterexample_pressure': counterexample_pressure, 'taste': taste, 'broad_claim': broad_claim}

def _paper_quality_venue_short_label(row: dict[str, object]) -> str:
    candidates = row.get('venue_candidates', []) or []
    if candidates:
        return ', '.join((str(value) for value in candidates[:2]))
    return 'unknown'

def _paper_quality_quality_row(meta: dict[str, object], cfg: dict[str, object], reference_time) -> dict[str, object]:
    scored = score_paper(meta, cfg, reference_time=reference_time)
    base = _paper_quality_heuristic_signals(meta)
    top_tier_ready = 'watch'
    if scored.get('selection_bucket') == 'recent_high_priority' and base['novelty'] != 'low' and (base['claim_strength'] != 'low') and (base['taste'] != 'low'):
        top_tier_ready = 'promising'
    elif scored.get('selection_bucket') == 'deprioritized' and (not scored.get('foundational_keep')):
        top_tier_ready = 'weak'
    elif base['novelty'] == 'low' and scored.get('selection_bucket') != 'older_foundational':
        top_tier_ready = 'weak'
    concerns: list[str] = []
    if scored.get('selection_bucket') == 'deprioritized' and (not scored.get('foundational_keep')):
        concerns.append('This paper falls outside the preferred recent literature window and does not look strong enough to anchor the loop.')
    if scored.get('stale_penalty_active') and (not scored.get('foundational_keep')):
        concerns.append('The paper is aging relative to the current field and should not dominate initialization unless it is uniquely relevant.')
    if base['novelty'] == 'low':
        concerns.append('The apparent contribution looks incremental relative to nearby work.')
    if base['claim_strength'] == 'low':
        concerns.append('The summary does not yet signal decisive evaluation, ablations, or strong comparison discipline.')
    if base['counterexample_pressure'] == 'high':
        concerns.append('The paper appears to make broad claims without enough visible falsification pressure.')
    if base['taste'] == 'low':
        concerns.append('The framing does not obviously move a field-level assumption or central bottleneck.')
    if scored.get('actionability_score', 0) == 0:
        concerns.append('The paper currently looks hard to borrow from directly because code/reproducibility cues are weak at abstract level.')
    if not concerns:
        concerns.append('The current signals make this worth deeper reading, but abstract-level screening is still only a first pass.')
    next_checks: list[str] = []
    if scored.get('selection_bucket') in {'recent_candidate', 'deprioritized'}:
        next_checks.append('Verify that a newer, higher-tier paper has not already dominated this angle in the last 6-12 months.')
    if base['novelty'] != 'high':
        next_checks.append('Map the nearest-neighbor papers and force a precise delta claim before investing execution budget.')
    if base['claim_strength'] != 'high':
        next_checks.append('Inspect whether the paper really includes decisive baselines, ablations, and stress tests.')
    if base['counterexample_pressure'] != 'low':
        next_checks.append("List what evidence would falsify the paper's core claim and whether the paper already covers it.")
    if base['taste'] != 'high':
        next_checks.append('Check whether success would change a meaningful community assumption or only polish a known recipe.')
    if scored.get('actionability_score', 0) < 2:
        next_checks.append('Search for linked code, reproducibility notes, or companion repos before allocating large implementation budget.')
    if scored.get('selection_bucket') == 'older_foundational':
        next_checks.append('Use this as a foundation or control, not as proof that the current frontier is still open.')
    if not next_checks:
        next_checks.append('Translate the central claim into a reproducible benchmark-level experiment contract.')
    return {'paper_id': meta.get('paper_id', ''), 'title': meta.get('title', ''), 'source': meta.get('source', ''), **base, **scored, 'claim_ready_anchor': bool(meta.get('claim_ready_anchor', False)), 'top_tier_readiness': top_tier_ready, 'concerns': concerns, 'next_checks': next_checks}

def _paper_quality_append_group(lines: list[str], title: str, rows: list[dict[str, object]]) -> None:
    lines.append(f'## {title}\n\n')
    if not rows:
        lines.append('- None yet.\n\n')
        return
    for row in rows:
        lines.append(f"- `{row['paper_id']}` | source={row.get('source', '')} | score={row.get('discovery_priority_score', 0)} | idea={row.get('idea_worthiness_score', 0)} | bucket={row.get('selection_bucket', '')} | recency={row.get('recency_bucket', '')} | age_days={row.get('paper_age_days', '')} | venue={_paper_quality_venue_short_label(row)} | title={row.get('title', '')}\n")
    lines.append('\n')

def run_paper_quality(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    reference_time = now_utc()
    policy = build_literature_policy(cfg)
    assessments: list[dict[str, object]] = []
    paper_dirs = sorted([p for p in paths.raw_papers.iterdir() if p.is_dir()]) if paths.raw_papers.exists() else []
    for paper_dir in paper_dirs:
        metadata_path = paper_dir / 'metadata.json'
        meta = _paper_quality_load_json(metadata_path)
        row = _paper_quality_quality_row(meta, cfg, reference_time=reference_time)
        assessments.append(row)
        updated_meta = dict(meta)
        for key in ['published_at', 'paper_age_days', 'recency_bucket', 'recency_score', 'within_primary_window', 'within_secondary_window', 'required_topic_groups', 'topic_group_hits', 'missing_topic_groups', 'hard_topic_mismatch', 'hard_missing_topic_groups', 'venue_candidates', 'venue_matches', 'journal_matches', 'venue_quality', 'venue_score', 'topic_match_score', 'hard_mismatch_penalty', 'missing_soft_axis_penalty', 'citation_signal', 'actionability_score', 'foundational_keep', 'not_positive_support', 'selection_bucket', 'discovery_priority_score', 'idea_worthiness_score', 'high_quality_recent', 'top_tier_readiness']:
            updated_meta[key] = row.get(key)
        if row.get('not_positive_support'):
            updated_meta['weak_candidate_for_critique'] = True
            updated_meta['guardrail'] = updated_meta.get('guardrail') or 'This item is retained for critique/search expansion only, not as positive paper support.'
        _paper_quality_save_json(metadata_path, updated_meta)
    assessments = sorted(assessments, key=paper_sort_key)
    summary = {'paper_count': len(assessments), 'recent_high_priority_count': sum((1 for row in assessments if row.get('selection_bucket') == 'recent_high_priority')), 'recent_candidate_count': sum((1 for row in assessments if row.get('selection_bucket') == 'recent_candidate')), 'older_foundational_count': sum((1 for row in assessments if row.get('selection_bucket') == 'older_foundational')), 'deprioritized_count': sum((1 for row in assessments if row.get('selection_bucket') == 'deprioritized')), 'promising_count': sum((1 for row in assessments if row.get('top_tier_readiness') == 'promising'))}
    payload = {'generated_at': reference_time.isoformat(), 'reference_time': reference_time.isoformat(), 'literature_policy': policy, 'summary': summary, 'papers': assessments}
    (paths.state / 'paper_quality.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    recent_high = [row for row in assessments if row.get('selection_bucket') == 'recent_high_priority']
    recent_candidates = [row for row in assessments if row.get('selection_bucket') == 'recent_candidate']
    older_foundational = [row for row in assessments if row.get('selection_bucket') == 'older_foundational']
    lines = ['# Paper Quality Assessment\n\n']
    lines.extend([f'- reference_time_utc: {reference_time.isoformat()}\n', f"- primary_window_days: {policy.get('primary_window_days', 180)}\n", f"- secondary_window_days: {policy.get('secondary_window_days', 365)}\n", f"- deprioritize_older_than_days: {policy.get('deprioritize_older_than_days', 730)}\n", f"- paper_count: {summary['paper_count']}\n", f"- recent_high_priority_count: {summary['recent_high_priority_count']}\n", f"- recent_candidate_count: {summary['recent_candidate_count']}\n", f"- older_foundational_count: {summary['older_foundational_count']}\n", f"- deprioritized_count: {summary['deprioritized_count']}\n\n", '## Screening Rule\n\n', '- Default reading priority should emphasize papers from roughly the last 6 months, then the last year, with explicit preference for top AI venues and top journals relevant to the topic.\n', '- arXiv and other fresh sources are valuable, but they should be weighted by topic fit, venue/journal quality when known, citation signal, and borrowability into real experiments.\n', '- Older papers should only survive as foundations, controls, or still-unbeaten references.\n\n'])
    _paper_quality_append_group(lines, 'Recent High-Priority Papers', recent_high)
    _paper_quality_append_group(lines, 'Recent Candidate Papers', recent_candidates)
    _paper_quality_append_group(lines, 'Older Foundational Keepers', older_foundational)
    if not assessments:
        lines.append('- No imported papers available yet.\n')
    for row in assessments:
        lines.extend([f"## {row['paper_id']}\n\n", f"- title: {row['title']}\n", f"- source: {row.get('source', '')}\n", f"- top_tier_readiness: {row['top_tier_readiness']}\n", f"- selection_bucket: {row.get('selection_bucket', '')}\n", f"- discovery_priority_score: {row.get('discovery_priority_score', 0)}\n", f"- idea_worthiness_score: {row.get('idea_worthiness_score', 0)}\n", f"- recency_bucket: {row.get('recency_bucket', '')}\n", f"- paper_age_days: {row.get('paper_age_days', '')}\n", f"- venue_quality: {row.get('venue_quality', '')}\n", f"- venue_candidates: {', '.join(row.get('venue_candidates', []) or [])}\n", f"- novelty: {row['novelty']}\n", f"- claim_strength: {row['claim_strength']}\n", f"- counterexample_pressure: {row['counterexample_pressure']}\n", f"- taste: {row['taste']}\n", f"- topic_match_score: {row.get('topic_match_score', 0)}\n", f"- citation_signal: {row.get('citation_signal', 0)}\n", f"- actionability_score: {row.get('actionability_score', 0)}\n", f"- foundational_keep: {row.get('foundational_keep', False)}\n", f"- broad_claim_present: {row['broad_claim']}\n", '\n### Key Concerns\n'])
        for concern in row['concerns']:
            lines.append(f'- {concern}\n')
        lines.append('\n### Required Follow-up Checks\n')
        for item in row['next_checks']:
            lines.append(f'- {item}\n')
        lines.append('\n')
    lines.append('## Usage Note\n')
    lines.append('- These are screening and prioritization signals for the literature loop. They sharpen search and selection, but they do not replace full-paper reading or direct evidence checks.\n')
    out = paths.planning / 'paper_quality.md'
    out.write_text(''.join(lines), encoding='utf-8')
    print(out)


# ---- literature_base_candidates tool ----
def _base_candidates_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def _base_candidates_load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default

def _base_candidates_save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def _base_candidates_as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

def _base_candidates_normalize_title(value: Any) -> str:
    text = re.sub('\\s+', ' ', str(value or '').strip().lower())
    return text

def _base_candidates_has_any(text: str, tokens: list[str]) -> bool:
    low = text.lower()
    return any((token in low for token in tokens))

def _base_candidates_has_positive_signal(text: str, tokens: list[str]) -> bool:
    low = text.lower()
    negations = ['缺少', '没有', '无', '不涉及', '未涉及', '完全没有', 'does not', 'do not', 'without', 'no ', 'not ', 'lack', 'lacks', 'missing', 'absence of']
    for token in tokens:
        token_low = token.lower()
        start = 0
        while True:
            pos = low.find(token_low, start)
            if pos < 0:
                break
            local = low[max(0, pos - 64):pos + len(token_low) + 64]
            if not any((neg in local[:local.find(token_low) if token_low in local else len(local)] for neg in negations)):
                return True
            start = pos + len(token_low)
    return False

def _base_candidates_code_signals(row: dict[str, Any]) -> list[str]:
    values = []
    for key in ['code_url', 'github_url', 'repo_url', 'url', 'pdf_url', 'abstract', 'abstract_en', 'reason', 'recommendation_note', 'fit_explanation']:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    text = '\n'.join(values)
    signals = []
    for pattern in ['github.com', 'gitlab', '4open.science', 'anonymous.4open', 'code', 'repository', 'repo']:
        if pattern in text.lower():
            signals.append(pattern)
    return sorted(set(signals))

def _base_candidates_candidate_url(row: dict[str, Any]) -> str:
    for key in ['code_url', 'github_url', 'repo_url', 'url', 'pdf_url', 'openreview_url', 'link']:
        value = str(row.get(key) or '').strip()
        if value:
            return value
    return ''

def _base_candidates_merge_rows(packet_rows: list[dict[str, Any]], find_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_title: dict[str, dict[str, Any]] = {}
    for row in find_rows + packet_rows:
        if not isinstance(row, dict):
            continue
        title = _base_candidates_normalize_title(row.get('title') or row.get('name'))
        if not title:
            continue
        merged = dict(by_title.get(title, {}))
        merged.update({k: v for k, v in row.items() if v not in (None, '', [], {})})
        by_title[title] = merged
    return list(by_title.values())

def _base_candidates_assess_candidate(row: dict[str, Any], rank: int, cfg: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get('title') or row.get('name') or '')
    abstract = str(row.get('abstract') or row.get('abstract_en') or row.get('summary') or '')
    reason = str(row.get('reason') or row.get('recommendation_note') or row.get('fit_explanation') or '')
    text = '\n'.join([title, abstract, reason])
    topic_fit = core_topic_fit_from_text(text, cfg)
    hits = topic_fit.get('topic_group_hits', {}) if isinstance(topic_fit.get('topic_group_hits'), dict) else {}
    matched_groups = [str(name) for name, ok in hits.items() if ok]
    required_groups = [str(name) for name in topic_fit.get('required_topic_groups', []) if str(name).strip()] if isinstance(topic_fit.get('required_topic_groups'), list) else []
    matched_count = len(matched_groups)
    signals = _base_candidates_code_signals(row)
    score = float(row.get('score') or row.get('final_score') or row.get('total_score') or 0)
    base_priority = 0.0
    base_priority += min(7.0, 2.5 * matched_count)
    if required_groups and matched_count >= len(required_groups):
        base_priority += 3.0
    base_priority += min(score, 10.0) / 10.0
    base_priority += 2.0 if signals else 0.0
    if topic_fit.get('hard_topic_mismatch'):
        base_priority -= 5.0
    if rank <= 10:
        base_priority += 1.0
    if required_groups and matched_count >= len(required_groups):
        fit = 'direct_topic_candidate'
    elif matched_count:
        fit = 'topic_component_candidate'
    else:
        fit = 'weak_or_background_candidate'
    needs = []
    if not signals:
        needs.append('code_or_repo_lookup')
    needs.extend(['dataset_protocol_check', 'paper_target_metric_check', 'local_runnability_probe'])
    route = 'needs_repo_data_env_audit' if matched_count and (not topic_fit.get('hard_topic_mismatch')) else 'background_only'
    return {'rank': rank, 'title': title, 'venue': row.get('venue') or row.get('source') or '', 'year': row.get('year') or row.get('published') or '', 'score': score, 'url': _base_candidates_candidate_url(row), 'fit': fit, 'route': route, 'required_topic_groups': required_groups, 'topic_group_hits': hits, 'matched_topic_groups': matched_groups, 'matched_topic_group_count': matched_count, 'missing_topic_groups': topic_fit.get('missing_topic_groups', []), 'hard_topic_mismatch': bool(topic_fit.get('hard_topic_mismatch')), 'code_signals': signals, 'base_priority': round(base_priority, 4), 'needs_audit': needs, 'reason': reason[:1000]}

def _base_candidates_build(project: str, top_n: int) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    packet = _base_candidates_load_json(paths.state / 'literature_tool_packet.json', {})
    find_results = _base_candidates_load_json(paths.planning / 'finding' / 'find_results.json', {})
    repo_selection = _base_candidates_load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    repo_blocker = _base_candidates_load_json(paths.state / 'repo_selection_blocker.json', {})
    literature_audit = _base_candidates_load_json(paths.state / 'literature_base_audit.json', {})
    strong = _base_candidates_as_list(find_results.get('strong_recommendations')) or _base_candidates_as_list(find_results.get('articles'))
    packet_base = _base_candidates_as_list(packet.get('base_work_candidates'))
    packet_strong = _base_candidates_as_list(packet.get('strong_papers'))
    merged = _base_candidates_merge_rows(packet_base + packet_strong, strong)
    assessed = [_base_candidates_assess_candidate(row, i + 1, cfg) for i, row in enumerate(merged[:max(top_n, 1)])]
    assessed = sorted(assessed, key=lambda x: (-float(x.get('base_priority') or 0), int(x.get('rank') or 999)))
    audit_required = [row for row in assessed if row.get('route') == 'needs_repo_data_env_audit']
    fresh_find_run_id = find_results.get('run_id') or ''
    current_selection_generated = str(repo_selection.get('generated_at') or '') if isinstance(repo_selection, dict) else ''
    current_blocker_updated = str(repo_blocker.get('updated_at') or repo_blocker.get('search_timestamp') or '') if isinstance(repo_blocker, dict) else ''
    stale_reason = ''
    stale_existing_base_decision = bool(audit_required)
    if audit_required:
        stale_reason = f'Fresh Find {fresh_find_run_id} produced {len(audit_required)} literature base candidates requiring repo/data/env audit. Existing repo_selection_blocker/evidence_ready_repo_selection cannot prove no better base unless these candidates are audited.'
    status = 'blocked_pending_literature_base_audit' if audit_required else 'no_literature_base_candidates'
    audit_matches_current_find = isinstance(literature_audit, dict) and bool(literature_audit.get('audit_complete')) and (str(literature_audit.get('fresh_find_run_id') or '') == str(fresh_find_run_id or ''))
    audit_selected = literature_audit.get('selected') if isinstance(literature_audit, dict) and isinstance(literature_audit.get('selected'), dict) else {}
    if audit_matches_current_find:
        status = 'fresh_literature_base_audit_completed_selected_base' if audit_selected else 'fresh_literature_base_audit_completed_no_evidence_ready_base'
        stale_existing_base_decision = False
        stale_reason = 'Fresh literature base audit already completed for this Find run and selected an evidence-ready base; downstream gates should evaluate that selected fresh route.' if audit_selected else 'Fresh literature base audit already completed for this Find run, but no evidence-ready repo/data/env route was selected. Downstream gates must continue fresh-base implementation/code/data work instead of resetting to pending audit or a historical route.'
    payload = {'project': project, 'generated_at': _base_candidates_now_iso(), 'status': status, 'fresh_find_run_id': fresh_find_run_id, 'literature_packet_generated_at': packet.get('generated_at') if isinstance(packet, dict) else '', 'strong_count': len(strong), 'packet_base_work_candidates': len(packet_base), 'assessed_count': len(assessed), 'audit_required_count': len(audit_required), 'current_repo_selection_generated_at': current_selection_generated, 'current_repo_blocker_updated_at': current_blocker_updated, 'stale_existing_base_decision': stale_existing_base_decision, 'stale_reason': stale_reason, 'top_candidates': assessed[:top_n], 'audit_required_candidates': audit_required[:top_n], 'policy': {'fresh_find_must_drive_base_selection': True, 'positive_anchor_pools': ['articles', 'strong_recommendations'], 'audit_pool_policy': 'screened/read/evaluated/title candidates can propose repo/base audits but cannot support claims.', 'historical_route_policy': 'A historical route cannot remain the main route after a fresh Find until the fresh literature base candidates are audited or rejected with evidence.'}, 'required_next_actions': ['For each audit_required_candidate, search/resolve code repository and dataset/protocol availability.', 'Run repo/data/env selector on discovered repos; update evidence_ready_repo_selection.json with fresh_find_run_id.', 'Only after this fresh assessment may reference_reproduction_gate decide switch_base/no_viable_base_switch_route/continue_base.']}
    if audit_matches_current_find and isinstance(literature_audit, dict):
        payload.update({'last_audit_generated_at': literature_audit.get('generated_at', ''), 'last_audit_status': literature_audit.get('status', ''), 'last_audit_repo_candidates_discovered_count': literature_audit.get('repo_candidates_discovered_count', 0), 'last_audit_selection_gate': literature_audit.get('selection_gate', ''), 'last_audit_selected': audit_selected, 'last_audit_candidate_count': literature_audit.get('candidate_count', 0), 'last_audit_total_required_count': literature_audit.get('total_audit_required_count', 0), 'last_audit_complete': bool(literature_audit.get('audit_complete')), 'last_audit_remaining_candidate_count': literature_audit.get('remaining_candidate_count', 0)})
    return payload

def _base_candidates_write_report(paths, payload: dict[str, Any]) -> Path:
    lines = ['# Literature Base Candidate Assessment\n\n']
    for key in ['status', 'fresh_find_run_id', 'strong_count', 'packet_base_work_candidates', 'audit_required_count', 'stale_existing_base_decision']:
        lines.append(f'- {key}: {payload.get(key)}\n')
    if payload.get('stale_reason'):
        lines.append(f"- stale_reason: {payload.get('stale_reason')}\n")
    lines.append('\n## Audit Required Candidates\n')
    for row in payload.get('audit_required_candidates', [])[:30]:
        lines.append(f"- rank={row.get('rank')} priority={row.get('base_priority')} fit={row.get('fit')} title={row.get('title')} venue={row.get('venue')} year={row.get('year')} url={row.get('url')} needs={row.get('needs_audit')}\n")
    out = paths.reports / 'literature_base_candidate_assessment.md'
    out.write_text(''.join(lines), encoding='utf-8')
    return out

def run_literature_base_candidates(argv=None):
    parser = argparse.ArgumentParser(description='Assess fresh TASTE/Find literature candidates before TASTE can keep or switch a base work.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--top-n', type=int, default=30)
    args = parser.parse_args(argv)
    paths = build_paths(args.project)
    payload = _base_candidates_build(args.project, args.top_n)
    _base_candidates_save_json(paths.state / 'literature_base_candidate_assessment.json', payload)
    report = _base_candidates_write_report(paths, payload)
    print(report)
    return 2 if payload.get('status') == 'blocked_pending_literature_base_audit' else 0


# ---- plan_literature tool ----
AI_CORE = ['ICLR', 'NeurIPS', 'ICML']

CORE_VENUE_IDS = ['openreview_iclr_2026', 'openreview_neurips', 'dblp_icml', 'dblp_kdd']

CORE_VENUE_NAMES = ['ICLR', 'NeurIPS', 'ICML', 'KDD']

DEFAULT_SECONDARY_VENUES = ['AAAI', 'IJCAI', 'AISTATS', 'COLM']

AI_SECONDARY = list(DEFAULT_SECONDARY_VENUES)

VENUE_IDS = {'NeurIPS': 'openreview_neurips', 'ICLR': 'openreview_iclr_2026', 'ICML': 'dblp_icml', 'KDD': 'dblp_kdd', 'SIGIR': 'dblp_sigir', 'WWW': 'dblp_www', 'TheWebConf': 'dblp_www', 'WSDM': 'dblp_wsdm', 'CIKM': 'dblp_cikm', 'AAAI': 'dblp_aaai', 'IJCAI': 'dblp_ijcai', 'ACL': 'dblp_acl', 'EMNLP': 'dblp_emnlp', 'COLM': 'dblp_colm', 'ACMMM': 'dblp_acmmm'}

CUSTOM_VENUES = [{'id': 'dblp_icml', 'source': 'dblp', 'name': 'ICML', 'full_name': 'International Conference on Machine Learning', 'type': 'conference', 'rank': 'high-level', 'field': 'Artificial Intelligence / Machine Learning', 'field_key': 'AI', 'address': 'https://dblp.uni-trier.de/db/conf/icml/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_kdd', 'source': 'dblp', 'name': 'KDD', 'full_name': 'ACM SIGKDD Conference on Knowledge Discovery and Data Mining', 'type': 'conference', 'rank': 'high-level', 'field': 'Data Mining', 'field_key': 'DM_CS', 'address': 'https://dblp.uni-trier.de/db/conf/kdd/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_sigir', 'source': 'dblp', 'name': 'SIGIR', 'full_name': 'ACM SIGIR Conference on Research and Development in Information Retrieval', 'type': 'conference', 'rank': 'high-level', 'field': 'Information Retrieval', 'field_key': 'DM_CS', 'address': 'https://dblp.uni-trier.de/db/conf/sigir/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_www', 'source': 'dblp', 'name': 'WWW', 'full_name': 'The Web Conference', 'type': 'conference', 'rank': 'high-level', 'field': 'Web / IR', 'field_key': 'DM_CS', 'address': 'https://dblp.uni-trier.de/db/conf/www/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_wsdm', 'source': 'dblp', 'name': 'WSDM', 'full_name': 'ACM International Conference on Web Search and Data Mining', 'type': 'conference', 'rank': 'high-level', 'field': 'Web Search / Data Mining', 'field_key': 'DM_CS', 'address': 'https://dblp.uni-trier.de/db/conf/wsdm/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_cikm', 'source': 'dblp', 'name': 'CIKM', 'full_name': 'ACM International Conference on Information and Knowledge Management', 'type': 'conference', 'rank': 'strong', 'field': 'Information Retrieval / Knowledge Management', 'field_key': 'DM_CS', 'address': 'https://dblp.uni-trier.de/db/conf/cikm/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_acmmm', 'source': 'dblp', 'name': 'ACMMM', 'full_name': 'ACM International Conference on Multimedia', 'type': 'conference', 'rank': 'high-level', 'field': 'Multimedia', 'field_key': 'CGAndMT', 'address': 'https://dblp.uni-trier.de/db/conf/mm/', 'years': [], 'classification_source': 'topic_policy'}, {'id': 'dblp_colm', 'source': 'dblp', 'name': 'COLM', 'full_name': 'Conference on Language Modeling', 'type': 'conference', 'rank': 'high-level', 'field': 'Language Modeling', 'field_key': 'AI', 'address': 'https://dblp.uni-trier.de/db/conf/colm/', 'years': [], 'classification_source': 'topic_policy'}]

def _literature_plan_save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def _literature_plan_tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall('[a-zA-Z][a-zA-Z0-9_+-]{2,}|[\\u4e00-\\u9fff]+', text or '')}

def _literature_plan_has_any(blob: str, needles: list[str]) -> bool:
    low = blob.lower()
    return any((n.lower() in low for n in needles))

def _literature_plan_infer_direction(cfg: dict[str, Any]) -> dict[str, Any]:
    blob = ' '.join([str(cfg.get('topic', '')), str(cfg.get('user_prompt', '')), ' '.join(cfg.get('queries', []) or [])])
    return {'topic_terms': sorted(_literature_plan_tokens(blob))[:40]}

def _literature_plan_build_queries(cfg: dict[str, Any], direction: dict[str, Any]) -> list[str]:
    base = list(cfg.get('queries', []) or [])
    for key in ('literature_queries', 'search_queries'):
        if isinstance(cfg.get(key), list):
            base.extend(cfg.get(key) or [])
    topic = str(cfg.get('topic', '') or '').strip()
    if topic:
        base.append(topic)
    interest = str(cfg.get('research_interest', '') or '').strip()
    if interest:
        base.extend((part.strip() for part in re.split('[;；]', interest) if part.strip()))
    return dedupe_keep_order([str(query).strip() for query in base if str(query).strip()])[:10]

def _literature_plan_plan_venues(cfg: dict[str, Any], direction: dict[str, Any], *, core_only: bool=True) -> tuple[list[str], list[str], list[str], list[str]]:
    preferred = list(AI_CORE)
    secondary = list(AI_SECONDARY)
    journals: list[str] = []
    policy = build_literature_policy(cfg)
    preferred.extend(policy.get('preferred_venues', []))
    secondary.extend(policy.get('secondary_venues', []))
    journals.extend(policy.get('preferred_journals', []))
    preferred = dedupe_keep_order(preferred)
    secondary = [v for v in dedupe_keep_order(secondary) if v not in preferred]
    journals = dedupe_keep_order(journals)
    venue_ids = dedupe_keep_order([VENUE_IDS[v] for v in preferred + secondary if v in VENUE_IDS])
    if core_only:
        preferred = [name for name in CORE_VENUE_NAMES if name in preferred or name in VENUE_IDS]
        secondary = []
        venue_ids = list(CORE_VENUE_IDS)
    return (preferred, secondary, journals, venue_ids)

def _literature_plan_update_project_config(paths, cfg: dict[str, Any], preferred: list[str], secondary: list[str], journals: list[str], queries: list[str]) -> None:
    cfg.setdefault('literature', {})
    cfg['literature']['preferred_venues'] = preferred
    cfg['literature']['secondary_venues'] = secondary
    cfg['literature']['preferred_journals'] = journals
    cfg['literature'].setdefault('primary_window_days', 180)
    cfg['literature'].setdefault('secondary_window_days', 365)
    cfg['literature'].setdefault('deprioritize_older_than_days', 730)
    cfg['queries'] = queries
    cfg.setdefault('discovery', {}).setdefault('semantic_scholar', {})['enabled'] = True
    selection = canonical_source_selection()
    enabled = ['manual', 'semantic_scholar']
    if selection.get('include_arxiv'):
        enabled.append('arxiv')
    if selection.get('include_github'):
        enabled.append('github')
    cfg.setdefault('discovery', {})['enabled_sources'] = enabled
    _literature_plan_save_json(paths.config, cfg)

def _literature_plan_write_taste_custom_venues(years: list[int]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    candidate_paths = [repo_root / 'modules' / 'finding' / 'data' / 'custom_venues.json', repo_root / 'external' / 'TASTE' / 'auto_research' / 'data' / 'custom_venues.json']
    rows = []
    for row in CUSTOM_VENUES:
        copy = dict(row)
        copy['years'] = years
        rows.append(copy)
    for path in candidate_paths:
        if path.parent.exists():
            _literature_plan_save_json(path, rows)

def run_plan_literature(argv=None):
    parser = argparse.ArgumentParser(description='Create an adaptive recent-literature review plan from the project topic.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--primary-window-days', type=int, default=90)
    parser.add_argument('--secondary-window-days', type=int, default=120)
    parser.add_argument('--wide-venue-survey', action='store_true', help='Allow broader multi-venue planning; default is latest-year core-five survey.')
    parser.add_argument('--max-queries', type=int, default=8)
    args = parser.parse_args(argv)
    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    reference_time = now_utc()
    primary_start = reference_time - dt.timedelta(days=args.primary_window_days)
    secondary_start = reference_time - dt.timedelta(days=args.secondary_window_days)
    years = [reference_time.year]
    direction = _literature_plan_infer_direction(cfg)
    queries = _literature_plan_build_queries(cfg, direction)[:args.max_queries]
    preferred, secondary, journals, venue_ids = _literature_plan_plan_venues(cfg, direction, core_only=not args.wide_venue_survey)
    _literature_plan_update_project_config(paths, cfg, preferred, secondary, journals, queries)
    _literature_plan_write_taste_custom_venues(years)
    plan = {'project': args.project, 'generated_at_utc': reference_time.isoformat(), 'research_topic': cfg.get('topic', ''), 'direction_signals': direction, 'time_windows': {'primary_recent': {'days': args.primary_window_days, 'start_utc': primary_start.isoformat(), 'end_utc': reference_time.isoformat(), 'purpose': 'highest priority for fresh high-quality papers'}, 'secondary_recent': {'days': args.secondary_window_days, 'start_utc': secondary_start.isoformat(), 'end_utc': reference_time.isoformat(), 'purpose': 'important recent SOTA and strong transferable work'}, 'older_foundational': {'max_age_days': 1825, 'purpose': 'only keep if highly cited/foundational or still an unbeaten baseline'}}, 'venue_strategy': {'core_ai_top_venues': AI_CORE, 'topic_preferred_venues': preferred, 'topic_secondary_venues': secondary, 'topic_journals': journals, 'taste_venue_ids': venue_ids}, 'queries': queries, 'source_strategy': {'arxiv': 'search planned queries in a strict recent window; keep only high-score fresh preprints to prevent arXiv from dominating conference survey', 'semantic_scholar': 'search every planned query for citation, venue, OA PDF, TLDR, and publication-date signals', 'github': 'search planned code-oriented queries; score by topic fit, stars/forks, recent activity, license/install/entrypoint cues', 'taste': 'use topic-selected venue ids plus arXiv/GitHub/HuggingFace when enabled; feed TASTE reflection back into researcher_profile'}, 'selection_rules': ['Prefer the latest-year core-five venue scan first; widen only after TASTE records evidence that the narrow survey is insufficient.', 'Prefer primary window papers first, then secondary window papers.', 'Prioritize the configured venue set for the current project; domain-specific venues must come from project config, not framework keyword inference.', 'Keep arXiv only when topic fit, recency, borrowability, and citation/repo signals make it useful for idea generation.', 'Keep older papers only as foundations, baselines, or still-unbeaten SOTA anchors; do not let stale work dominate initialization.', 'A paper should feed an idea only if novelty delta, claim strength, counterexamples, bad-case implications, and implementation feasibility can be inspected.']}
    _literature_plan_save_json(paths.state / 'literature_review_plan.json', plan)
    md = ['# Adaptive Literature Review Plan\n\n']
    md.append(f"- generated_at_utc: {plan['generated_at_utc']}\n")
    md.append(f"- topic: {plan['research_topic']}\n")
    md.append('- survey_scope: latest-year core-five venues by default; use --wide-venue-survey only with recorded evidence need\n')
    md.append(f"- years: {', '.join((str(y) for y in years))}\n")
    md.append(f'- primary_recent_window: {primary_start.date()} to {reference_time.date()} UTC ({args.primary_window_days} days)\n')
    md.append(f'- secondary_recent_window: {secondary_start.date()} to {reference_time.date()} UTC ({args.secondary_window_days} days)\n')
    md.append('\n## Direction Signals\n')
    for k, v in direction.items():
        md.append(f'- {k}: {v}\n')
    md.append('\n## Venue Focus\n')
    md.append('- core_ai_top_venues: ' + ', '.join(AI_CORE) + '\n')
    md.append('- topic_preferred_venues: ' + ', '.join(preferred) + '\n')
    md.append('- topic_secondary_venues: ' + ', '.join(secondary) + '\n')
    md.append('- topic_journals: ' + ', '.join(journals) + '\n')
    md.append('- taste_venue_ids: ' + ', '.join(venue_ids) + '\n')
    md.append('\n## Queries\n')
    for q in queries:
        md.append(f'- {q}\n')
    md.append('\n## Selection Rules\n')
    for rule in plan['selection_rules']:
        md.append(f'- {rule}\n')
    (paths.planning / 'literature_review_plan.md').write_text(''.join(md), encoding='utf-8')
    print(paths.planning / 'literature_review_plan.md')
    return 0


TOOL_ACTIONS = {
    "literature_base_candidates": run_literature_base_candidates,
    "paper_quality": run_paper_quality,
    "plan_literature": run_plan_literature,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finding module private literature planning and quality tools.")
    parser.add_argument("--tool-action", required=True, choices=sorted(TOOL_ACTIONS))
    ns, rest = parser.parse_known_args(argv)
    TOOL_ACTIONS[ns.tool_action](rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
