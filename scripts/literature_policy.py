#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import re
from typing import Any

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-/]{2,}")

DEFAULT_LITERATURE_POLICY: dict[str, Any] = {
    'primary_window_days': 180,
    'secondary_window_days': 365,
    'deprioritize_older_than_days': 730,
    'max_foundational_age_days': 1825,
    'max_foundational_older_papers': 3,
    'foundational_citation_threshold': 200,
    'recent_high_quality_floor': 11.0,
    'recent_candidate_floor': 7.0,
    'preferred_venues': [
        'NeurIPS', 'ICLR', 'ICML', 'CVPR', 'ICCV', 'ECCV', 'ACL', 'EMNLP',
        'NAACL', 'AAAI', 'IJCAI', 'KDD', 'COLM', 'AISTATS', 'CoRL', 'RSS',
        'MICCAI', 'SIGGRAPH',
    ],
    'secondary_venues': [
        'UAI', 'AAMAS', 'WWW', 'SIGIR', 'WSDM', 'COLING', 'CoNLL', 'ECAI',
        'ICRA', 'IROS', 'ICASSP', 'ACMMM',
    ],
    'preferred_journals': [
        'Nature Machine Intelligence', 'Journal of Machine Learning Research',
        'JMLR', 'Transactions on Machine Learning Research', 'TMLR',
        'IEEE Transactions on Pattern Analysis and Machine Intelligence',
        'TPAMI', 'International Journal of Computer Vision', 'IJCV',
        'Artificial Intelligence', 'Journal of Artificial Intelligence Research',
        'JAIR', 'Machine Learning',
    ],
    'github_recent_activity_days': 180,
    'github_deprioritize_activity_days': 365,
    'repo_min_stars_for_trust': 30,
    'repo_high_stthreshold': 200,
    'repo_candidate_floor': 8.0,
    'idea_pursue_floor': 15.0,
    'idea_watch_floor': 11.0,
}

TOKENS = {
    'reasoning', 'multimodal', 'agent', 'agents', 'foundation', 'generalization',
    'compositional', 'search', 'planning', 'retrieval', 'alignment', 'scaling',
    'world', 'long-context', 'long-horizon', 'scientific', 'discovery',
}

ACTIONABILITY_TOKENS = {
    'open-source', 'opensource', 'code', 'github', 'implementation', 'reproducible',
    'recipe', 'benchmark', 'dataset', 'release', 'released', 'available', 'repo',
    'training', 'evaluation', 'eval', 'baseline',
}

GENERIC_TOPIC_STOPWORDS = {
    'about', 'abstract', 'algorithm', 'approach', 'article', 'baseline', 'based',
    'benchmark', 'candidate', 'code', 'data', 'dataset', 'datasets', 'domain',
    'evaluation', 'experiment', 'experiments', 'framework', 'method', 'methods',
    'model', 'models', 'paper', 'papers', 'project', 'research', 'result', 'results',
    'study', 'system', 'systems', 'task', 'tasks', 'topic', 'with', 'using',
}


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, set):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _topic_terms_from_text(text: str, *, limit: int = 40) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", text or ''):
        cleaned = normalize_text(token).lower()
        if not cleaned or cleaned in GENERIC_TOPIC_STOPWORDS or cleaned.isdigit():
            continue
        if cleaned not in terms:
            terms.append(cleaned)
        if len(terms) >= limit:
            break
    return terms


def configured_topic_axes(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    cfg = cfg or {}
    literature = cfg.get('literature', {}) if isinstance(cfg.get('literature', {}), dict) else {}
    raw_axes = literature.get('topic_axes') or literature.get('topic_groups') or cfg.get('topic_axes') or cfg.get('topic_groups')
    axes: list[dict[str, Any]] = []
    iterable: list[tuple[str, Any]] = []
    if isinstance(raw_axes, dict):
        iterable = [(str(name), spec) for name, spec in raw_axes.items()]
    elif isinstance(raw_axes, list):
        iterable = [(str((spec or {}).get('name') or f'axis_{index+1}') if isinstance(spec, dict) else f'axis_{index+1}', spec) for index, spec in enumerate(raw_axes)]
    for name, spec in iterable:
        if isinstance(spec, dict):
            triggers = _listify(spec.get('triggers') or spec.get('terms') or spec.get('keywords') or spec.get('required_any'))
            required_any = _listify(spec.get('required_any') or spec.get('terms') or spec.get('keywords') or triggers)
            hard = bool(spec.get('hard') or spec.get('required') or spec.get('hard_required'))
        else:
            triggers = _listify(spec)
            required_any = list(triggers)
            hard = False
        triggers = dedupe_keep_order([term.lower() for term in triggers if str(term).strip()])
        required_any = dedupe_keep_order([term.lower() for term in required_any if str(term).strip()])
        if not name or not (triggers or required_any):
            continue
        axes.append({'name': normalize_label(name).replace(' ', '_') or name, 'triggers': triggers or required_any, 'required_any': required_any or triggers, 'hard': hard})
    if axes:
        return axes
    blob = ' '.join([
        str(cfg.get('topic', '')),
        str(cfg.get('user_prompt', '')),
        str(cfg.get('research_interest', '')),
        ' '.join(str(q) for q in cfg.get('queries', []) or []),
    ])
    terms = _topic_terms_from_text(blob)
    if not terms:
        return []
    return [{'name': 'project_topic', 'triggers': terms, 'required_any': terms, 'hard': False}]


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalize_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def normalize_label(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in WORD_RE.findall(text or '')}


def coerce_int(value: Any) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def coerce_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def parse_datetime_text(value: Any) -> dt.datetime | None:
    text = normalize_text(value)
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = dt.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    for fmt in ('%Y-%m-%d', '%Y-%m', '%Y'):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
    return None


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = normalize_text(value)
        key = normalize_label(cleaned)
        if not cleaned or not key or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def build_literature_policy(cfg: dict[str, Any] | None) -> dict[str, Any]:
    data = cfg or {}
    raw = data.get('literature', {}) if isinstance(data, dict) else {}
    policy = dict(DEFAULT_LITERATURE_POLICY)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value not in (None, ''):
                policy[key] = value
    for key in ('preferred_venues', 'secondary_venues', 'preferred_journals'):
        values = policy.get(key, [])
        policy[key] = dedupe_keep_order(list(values) if isinstance(values, list) else [])
    return policy


def extract_text_blob(item: dict[str, Any]) -> str:
    parts = [
        item.get('title', ''),
        item.get('summary', ''),
        item.get('tldr', ''),
        ' '.join(item.get('categories', []) or []),
        item.get('venue', ''),
        item.get('journal', ''),
    ]
    publication_venue = item.get('publicationVenue')
    if isinstance(publication_venue, dict):
        parts.extend([
            publication_venue.get('name', ''),
            publication_venue.get('venue', ''),
            publication_venue.get('journal', ''),
        ])
    elif publication_venue:
        parts.append(str(publication_venue))
    return ' '.join(normalize_text(part) for part in parts if part)


def extract_repo_blob(item: dict[str, Any]) -> str:
    parts = [
        item.get('name', ''),
        item.get('summary', ''),
        item.get('notes', ''),
        item.get('url', ''),
        item.get('language', ''),
        ' '.join(item.get('topics', []) or []),
    ]
    return ' '.join(normalize_text(part) for part in parts if part)


def extract_venue_candidates(item: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ('venue', 'journal'):
        value = item.get(key)
        if isinstance(value, dict):
            candidates.extend(str(v) for v in value.values() if v)
        elif isinstance(value, list):
            candidates.extend(str(v) for v in value if v)
        elif value:
            candidates.append(str(value))
    publication_venue = item.get('publicationVenue')
    if isinstance(publication_venue, dict):
        candidates.extend(
            str(publication_venue.get(name, ''))
            for name in ('name', 'venue', 'journal')
            if publication_venue.get(name)
        )
    elif publication_venue:
        candidates.append(str(publication_venue))
    return dedupe_keep_order(candidates)


def _match_signal(candidate: str, target: str) -> bool:
    a = normalize_label(candidate)
    b = normalize_label(target)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def venue_quality(item: dict[str, Any], policy: dict[str, Any]) -> tuple[int, str, list[str], list[str]]:
    candidates = extract_venue_candidates(item)
    venue_matches = [name for name in policy.get('preferred_venues', []) if any(_match_signal(candidate, name) for candidate in candidates)]
    secondary_matches = [name for name in policy.get('secondary_venues', []) if any(_match_signal(candidate, name) for candidate in candidates)]
    journal_matches = [name for name in policy.get('preferred_journals', []) if any(_match_signal(candidate, name) for candidate in candidates)]
    score = 0
    label = 'unknown'
    if venue_matches or journal_matches:
        score = 3
        label = 'top'
    elif secondary_matches:
        score = 2
        label = 'strong'
    elif candidates:
        score = 1
        label = 'unverified'
    return score, label, dedupe_keep_order(venue_matches + secondary_matches), journal_matches



def _contains_any(text: str, values: set[str]) -> bool:
    low = text.lower()
    return any(value.lower() in low for value in values)


def project_required_topic_groups(cfg: dict[str, Any] | None) -> list[str]:
    return [str(axis.get('name') or '') for axis in configured_topic_axes(cfg) if str(axis.get('name') or '')]


def topic_group_hits_from_text(text: str, cfg: dict[str, Any] | None) -> dict[str, bool]:
    lowered = (text or '').lower()
    return {str(axis.get('name') or ''): _contains_any(lowered, set(_listify(axis.get('required_any')))) for axis in configured_topic_axes(cfg) if str(axis.get('name') or '')}


def topic_group_hits(item: dict[str, Any], cfg: dict[str, Any] | None) -> dict[str, bool]:
    return topic_group_hits_from_text(extract_text_blob(item), cfg)


def core_topic_fit_from_text(text: str, cfg: dict[str, Any] | None, *, repo_candidate: bool = False) -> dict[str, Any]:
    axes = configured_topic_axes(cfg)
    groups = [str(axis.get('name') or '') for axis in axes if str(axis.get('name') or '')]
    hits = topic_group_hits_from_text(text, cfg)
    missing = [name for name in groups if not hits.get(name)]
    hard_missing = [str(axis.get('name') or '') for axis in axes if axis.get('hard') and not hits.get(str(axis.get('name') or ''))]
    return {
        'required_topic_groups': groups,
        'topic_group_hits': hits,
        'missing_topic_groups': missing,
        'hard_topic_mismatch': bool(dict.fromkeys(hard_missing)),
        'hard_missing_topic_groups': list(dict.fromkeys(hard_missing)),
    }


def core_topic_fit(item: dict[str, Any], cfg: dict[str, Any] | None) -> dict[str, Any]:
    return core_topic_fit_from_text(extract_text_blob(item), cfg, repo_candidate=False)


def repo_core_topic_fit(item: dict[str, Any], cfg: dict[str, Any] | None) -> dict[str, Any]:
    return core_topic_fit_from_text(extract_repo_blob(item), cfg, repo_candidate=True)

def topic_match_score(item: dict[str, Any], cfg: dict[str, Any] | None) -> int:
    text = extract_text_blob(item).lower()
    tokens = tokenize(text)
    cfg = cfg or {}
    phrases = [cfg.get('topic', '')] + list(cfg.get('queries', []) or [])
    phrase_hits = sum(1 for phrase in phrases if normalize_text(phrase) and normalize_text(phrase).lower() in text)
    query_tokens = tokenize(' '.join(str(phrase) for phrase in phrases if phrase))
    overlap = len(tokens & query_tokens)
    score = min(5, (2 * min(phrase_hits, 2)) + min(3, overlap))
    return score


def repo_topic_match_score(item: dict[str, Any], cfg: dict[str, Any] | None) -> int:
    text = extract_repo_blob(item).lower()
    tokens = tokenize(text)
    cfg = cfg or {}
    fit = repo_core_topic_fit(item, cfg)
    if fit.get('hard_topic_mismatch'):
        return 0
    phrases = [cfg.get('topic', '')] + list(cfg.get('queries', []) or [])
    phrase_hits = sum(1 for phrase in phrases if normalize_text(phrase) and normalize_text(phrase).lower() in text)
    query_tokens = tokenize(' '.join(str(phrase) for phrase in phrases if phrase))
    overlap = len(tokens & query_tokens)
    group_bonus = sum(1 for hit in fit.get('topic_group_hits', {}).values() if hit)
    return min(5, (2 * min(phrase_hits, 2)) + min(2, overlap) + min(2, group_bonus))


def taste_signal(item: dict[str, Any]) -> int:
    tokens = tokenize(extract_text_blob(item).lower())
    return min(2, sum(1 for token in tokens if token in TOKENS))


def paper_actionability_score(item: dict[str, Any]) -> int:
    text = extract_text_blob(item).lower()
    tokens = tokenize(text)
    hits = sum(1 for token in tokens if token in ACTIONABILITY_TOKENS)
    direct_phrases = sum(1 for phrase in ('code available', 'github', 'open source', 'released code', 'benchmark', 'implementation') if phrase in text)
    return min(4, hits + direct_phrases)


def recency_features(item: dict[str, Any], policy: dict[str, Any], reference_time: dt.datetime | None = None) -> dict[str, Any]:
    reference_time = reference_time or now_utc()
    published_at = parse_datetime_text(item.get('published')) or parse_datetime_text(item.get('updated'))
    age_days = None
    if published_at is not None:
        age_days = max(0, (reference_time - published_at.astimezone(dt.timezone.utc)).days)
    primary = int(policy.get('primary_window_days', 180))
    secondary = int(policy.get('secondary_window_days', 365))
    deprioritize = int(policy.get('deprioritize_older_than_days', 730))
    bucket = 'unknown'
    score = 1
    stale_penalty = 0
    stale_penalty_active = False
    if age_days is None:
        bucket = 'unknown'
        score = 1
    elif age_days <= primary:
        bucket = 'primary_recent'
        score = 4
    elif age_days <= secondary:
        bucket = 'secondary_recent'
        score = 3
    elif age_days <= deprioritize:
        bucket = 'aging'
        score = 1
        stale_penalty = 1
        stale_penalty_active = True
    else:
        bucket = 'legacy'
        score = 0
        stale_penalty = 3
        stale_penalty_active = True
    return {
        'published_at': published_at.isoformat() if published_at else '',
        'paper_age_days': age_days,
        'recency_bucket': bucket,
        'recency_score': score,
        'stale_penalty': stale_penalty,
        'stale_penalty_active': stale_penalty_active,
        'within_primary_window': age_days is not None and age_days <= primary,
        'within_secondary_window': age_days is not None and age_days <= secondary,
        'reference_time': reference_time.isoformat(),
    }


def citation_signal(item: dict[str, Any], policy: dict[str, Any]) -> tuple[int, int | None, int | None]:
    citations = coerce_int(item.get('citations'))
    influential = coerce_int(item.get('influential_citations'))
    threshold = int(policy.get('foundational_citation_threshold', 200))
    score = 0
    if citations is not None:
        if citations >= threshold:
            score += 2
        elif citations >= 50:
            score += 1
    if influential is not None:
        if influential >= 25:
            score += 1
        elif influential >= 10 and score == 0:
            score += 1
    return min(3, score), citations, influential


def foundational_keep(item: dict[str, Any], policy: dict[str, Any], recency: dict[str, Any], venue_score: int, citation_score_value: int) -> bool:
    age_days = recency.get('paper_age_days')
    if age_days is None:
        return False
    if age_days <= int(policy.get('secondary_window_days', 365)):
        return False
    if age_days > int(policy.get('max_foundational_age_days', 1825)):
        return False
    return citation_score_value >= 2 or venue_score >= 3


def selection_bucket(score: float, recency: dict[str, Any], venue_score: int, foundational: bool, policy: dict[str, Any]) -> str:
    recent_floor = float(policy.get('recent_high_quality_floor', 11.0))
    candidate_floor = float(policy.get('recent_candidate_floor', 7.0))
    bucket = recency.get('recency_bucket')
    if bucket in {'primary_recent', 'secondary_recent'} and score >= recent_floor and venue_score >= 2:
        return 'recent_high_priority'
    if bucket in {'primary_recent', 'secondary_recent'} and score >= candidate_floor:
        return 'recent_candidate'
    if foundational:
        return 'older_foundational'
    return 'deprioritized'


def is_not_positive_literature_signal(item: dict[str, Any]) -> bool:
    """True when upstream survey kept a paper only as a critique/search signal."""
    if bool(item.get('not_positive_support')) or bool(item.get('weak_candidate_for_critique')):
        return True
    if bool(item.get('foundation_demoted_from_strong')) or bool(item.get('retrieval_pool_only')):
        return True
    if bool(item.get('not_scientific_evidence')):
        return True
    paper_id = normalize_label(item.get('paper_id') or item.get('id') or item.get('entry_id'))
    if paper_id.startswith('taste fallback'):
        return True
    source = normalize_label(item.get('source'))
    if source == 'taste recoverable fallback':
        return True
    pool_role = normalize_label(item.get('taste_pool_role') or item.get('selection_role') or item.get('taste_candidate_role'))
    if pool_role.replace(' ', '_') in {'evaluated_candidate', 'title_candidate', 'critique_candidate', 'nethreshold_arxiv', 'weak_or_boundary', 'retrieval_candidate'}:
        return True
    tier = normalize_label(item.get('evidence_tier') or item.get('source_evidence_tier') or item.get('evaluated_evidence_tier')).replace(' ', '_')
    role = normalize_label(item.get('evidence_role') or item.get('source_evidence_role') or item.get('evaluated_evidence_role')).replace(' ', '_')
    if tier in {'retrieval_only', 'nethreshold_for_reading', 'critique_or_boundary_case'}:
        return True
    if role in {'weak_or_boundary', 'negative', 'critique_only', 'retrieval_candidate'}:
        return True
    note_blob = ' '.join(
        normalize_text(item.get(key))
        for key in ('guardrail', 'recommendation_note', 'taste_reason', 'reason')
        if item.get(key)
    ).lower()
    return any(
        phrase in note_blob
        for phrase in (
            'not as positive support',
            'weak or boundary candidate',
            'did not promote',
            '不建议作为当前优先级推荐',
            '不匹配',
            '缺乏关键主题',
            '缺少关键主题',
        )
    )


def score_paper(item: dict[str, Any], cfg: dict[str, Any] | None, reference_time: dt.datetime | None = None) -> dict[str, Any]:
    policy = build_literature_policy(cfg)
    recency = recency_features(item, policy, reference_time=reference_time)
    venue_score, venue_label, venue_matches, journal_matches = venue_quality(item, policy)
    topic_score = topic_match_score(item, cfg)
    citation_score_value, citations, influential = citation_signal(item, policy)
    taste = taste_signal(item)
    actionability = paper_actionability_score(item)
    foundational = foundational_keep(item, policy, recency, venue_score, citation_score_value)
    topic_fit = core_topic_fit(item, cfg)
    groups = topic_fit.get('required_topic_groups', []) if isinstance(topic_fit.get('required_topic_groups', []), list) else []
    missing_groups = topic_fit.get('missing_topic_groups', []) if isinstance(topic_fit.get('missing_topic_groups', []), list) else []
    missing_soft_axis_penalty = round(min(2.5, 2.5 * (len(missing_groups) / max(1, len(groups)))), 3) if groups and missing_groups else 0.0
    hard_mismatch_penalty = 8.0 if topic_fit.get('hard_topic_mismatch') else 0.0
    score = round(
        (recency['recency_score'] * 2.5)
        + (venue_score * 1.8)
        + (topic_score * 1.2)
        + citation_score_value
        + taste
        + (0.7 * actionability)
        + (1.0 if foundational else 0.0)
        - recency['stale_penalty']
        - hard_mismatch_penalty
        - missing_soft_axis_penalty,
        3,
    )
    idea_worthiness = round(score + (0.9 * actionability) + (0.5 * venue_score), 3)
    bucket = selection_bucket(score, recency, venue_score, foundational, policy)
    if topic_fit.get('hard_topic_mismatch') and not foundational:
        bucket = 'deprioritized'
    elif missing_soft_axis_penalty and bucket == 'recent_high_priority':
        bucket = 'recent_candidate'
    not_positive = is_not_positive_literature_signal(item)
    if not_positive:
        bucket = 'deprioritized'
        score = min(score, 0.0)
        idea_worthiness = min(idea_worthiness, 0.0)
    return {
        **recency,
        **topic_fit,
        'literature_policy': policy,
        'venue_candidates': extract_venue_candidates(item),
        'venue_matches': venue_matches,
        'journal_matches': journal_matches,
        'venue_quality': venue_label,
        'venue_score': venue_score,
        'topic_match_score': topic_score,
        'hard_mismatch_penalty': hard_mismatch_penalty,
        'missing_soft_axis_penalty': missing_soft_axis_penalty,
        'citation_signal': citation_score_value,
        'citations': citations,
        'influential_citations': influential,
        'taste_signal': taste,
        'actionability_score': actionability,
        'foundational_keep': foundational,
        'not_positive_support': not_positive,
        'selection_bucket': bucket,
        'discovery_priority_score': score,
        'idea_worthiness_score': idea_worthiness,
        'high_quality_recent': (bucket == 'recent_high_priority' and not not_positive),
    }


def repo_activity_features(item: dict[str, Any], policy: dict[str, Any], reference_time: dt.datetime | None = None) -> dict[str, Any]:
    reference_time = reference_time or now_utc()
    pushed_at = parse_datetime_text(item.get('last_pushed_at') or item.get('updated_at') or item.get('pushed_at'))
    activity_days = None
    if pushed_at is not None:
        activity_days = max(0, (reference_time - pushed_at.astimezone(dt.timezone.utc)).days)
    recent_window = int(policy.get('github_recent_activity_days', 180))
    deprioritize_window = int(policy.get('github_deprioritize_activity_days', 365))
    activity_score = 0
    activity_bucket = 'unknown'
    if bool(item.get('recent_activity')) and activity_days is None:
        activity_score = 3
        activity_bucket = 'recent'
    elif activity_days is None:
        activity_score = 1
        activity_bucket = 'unknown'
    elif activity_days <= recent_window:
        activity_score = 3
        activity_bucket = 'recent'
    elif activity_days <= deprioritize_window:
        activity_score = 2
        activity_bucket = 'aging'
    else:
        activity_score = 0
        activity_bucket = 'stale'
    return {
        'last_pushed_at': pushed_at.isoformat() if pushed_at else normalize_text(item.get('last_pushed_at') or item.get('updated_at') or item.get('pushed_at')),
        'activity_age_days': activity_days,
        'activity_bucket': activity_bucket,
        'activity_score': activity_score,
    }


def repo_adoption_score(item: dict[str, Any], policy: dict[str, Any]) -> tuple[int, int, int]:
    stars = coerce_int(item.get('stars')) or 0
    forks = coerce_int(item.get('forks')) or 0
    high_star = int(policy.get('repo_high_stthreshold', 200))
    min_stars = int(policy.get('repo_min_stars_for_trust', 30))
    score = 0
    if stars >= high_star:
        score += 3
    elif stars >= min_stars:
        score += 2
    elif stars >= 5:
        score += 1
    if forks >= 50:
        score += 1
    return min(4, score), stars, forks


def repo_usability_score(item: dict[str, Any]) -> tuple[int, list[str]]:
    checks = [
        ('has_readme', 1, 'readme'),
        ('has_license', 1, 'license'),
        ('has_install', 2, 'install'),
        ('has_entrypoint', 2, 'entrypoint'),
        ('has_tests', 1, 'tests'),
        ('has_dataset_docs', 1, 'dataset_docs'),
    ]
    score = 0
    supports: list[str] = []
    for key, value, label in checks:
        if item.get(key):
            score += value
            supports.append(label)
    return score, supports


def score_repo_candidate(item: dict[str, Any], cfg: dict[str, Any] | None, reference_time: dt.datetime | None = None) -> dict[str, Any]:
    policy = build_literature_policy(cfg)
    activity = repo_activity_features(item, policy, reference_time=reference_time)
    topic_fit = repo_core_topic_fit(item, cfg)
    topic_score = repo_topic_match_score(item, cfg)
    adoption_score_value, stars, forks = repo_adoption_score(item, policy)
    usability_score_value, supports = repo_usability_score(item)
    executable = 2 if item.get('has_install') and item.get('has_entrypoint') else 0
    local_audit_bonus = 2 if item.get('local_path') else 0
    hard_penalty = 8.0 if topic_fit.get('hard_topic_mismatch') else 0.0
    code_score = round(
        activity['activity_score']
        + (1.2 * topic_score)
        + adoption_score_value
        + usability_score_value
        + executable
        + local_audit_bonus
        - hard_penalty,
        3,
    )
    candidate_floor = float(policy.get('repo_candidate_floor', 8.0))
    selection_bucket = 'promising' if code_score >= candidate_floor else 'watch' if code_score >= candidate_floor - 2 else 'weak'
    return {
        **activity,
        **topic_fit,
        'stars': stars,
        'forks': forks,
        'repo_topic_match_score': topic_score,
        'repo_adoption_score': adoption_score_value,
        'repo_usability_score': usability_score_value,
        'repo_support_signals': supports,
        'repo_execution_ready': bool(item.get('has_install') and item.get('has_entrypoint')),
        'repo_reuse_score': code_score,
        'repo_selection_bucket': selection_bucket,
        'literature_policy': policy,
    }


def dataset_readiness_component(item: dict[str, Any]) -> float:
    readiness = coerce_float(item.get('readiness_score')) or 0.0
    available_bonus = 2.0 if item.get('available') else 0.0
    download_bonus = 1.0 if item.get('download_tested') else 0.0
    return round((0.25 * readiness) + available_bonus + download_bonus, 3)


def idea_recommendation(score: float, policy: dict[str, Any]) -> str:
    if score >= float(policy.get('idea_pursue_floor', 15.0)):
        return 'pursue'
    if score >= float(policy.get('idea_watch_floor', 11.0)):
        return 'watch'
    return 'prune'


def paper_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    age_days = item.get('paper_age_days')
    age_key = age_days if isinstance(age_days, int) else 10 ** 9
    return (
        selection_rank(str(item.get('selection_bucket', 'deprioritized'))),
        -float(item.get('discovery_priority_score', 0.0) or 0.0),
        -float(item.get('idea_worthiness_score', 0.0) or 0.0),
        age_key,
        normalize_label(item.get('title', '')),
    )


def repo_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        {'promising': 0, 'watch': 1, 'weak': 2}.get(str(item.get('repo_selection_bucket', 'weak')), 3),
        -float(item.get('repo_reuse_score', 0.0) or 0.0),
        item.get('activity_age_days') if isinstance(item.get('activity_age_days'), int) else 10 ** 9,
        -int(item.get('stars', 0) or 0),
        normalize_label(item.get('name', '')),
    )


def selection_rank(bucket: str) -> int:
    return {
        'recent_high_priority': 0,
        'recent_candidate': 1,
        'older_foundational': 2,
        'deprioritized': 3,
    }.get(bucket, 4)
