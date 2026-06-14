#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from project_paths import build_paths

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
WORD_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9_\-.]{2,}\b")
PLACEHOLDER_RE = re.compile(r"\b(TODO|TBD|FIXME|unclear|needs manual refinement|under-specified)\b", re.IGNORECASE)

STOPWORDS = {
    'research', 'paper', 'papers', 'method', 'methods', 'results', 'baseline', 'experiment', 'overview', 'status', 'this',
    'that', 'with', 'from', 'into', 'across', 'than', 'then', 'there', 'their', 'they', 'them', 'have', 'has', 'had',
    'should', 'would', 'could', 'will', 'might', 'about', 'after', 'before', 'which', 'while', 'where', 'when', 'what',
    'manual', 'refinement', 'current', 'project', 'system', 'needs', 'need', 'page', 'pages', 'wiki', 'concept', 'concepts',
    'author', 'authors', 'title', 'summary', 'metadata', 'dataset', 'benchmark', 'benchmarks', 'paper_id', 'none', 'draft',
}


def looks_meaningful(token: str) -> bool:
    low = token.lower()
    if low in STOPWORDS:
        return False
    if len(low) < 5:
        return False
    if low.startswith('demo-') or low.startswith('cs.'):
        return False
    if any(ch.isdigit() for ch in low) and low.count('-') + low.count('_') < 2:
        return False
    if low in {'false', 'true', 'unclear', 'unknown'}:
        return False
    return True


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    issues: list[str] = []
    notes: list[str] = []
    quality_pressure: list[str] = []

    wiki_pages = list(paths.wiki.rglob('*.md'))
    page_names = {path.stem for path in wiki_pages}
    incoming: Counter[str] = Counter()
    token_counter: Counter[str] = Counter()
    placeholder_hits: list[str] = []

    for path in wiki_pages:
        text = path.read_text(encoding='utf-8')
        for target in LINK_RE.findall(text):
            name = Path(target).stem if Path(target).suffix == '.md' else Path(target).name
            incoming[name] += 1
            if name not in page_names:
                issues.append(f'Broken wiki link in {path.relative_to(paths.root)} -> [[{target}]]')
        if PLACEHOLDER_RE.search(text):
            placeholder_hits.append(str(path.relative_to(paths.root)))
        for token in WORD_RE.findall(text):
            low = token.lower()
            if looks_meaningful(low):
                token_counter[low] += 1

    for path in wiki_pages:
        if path.stem not in incoming and path.name not in {'index.md', 'log.md', 'overview.md'}:
            issues.append(f'Orphan page: {path.relative_to(paths.root)}')

    research_gaps = paths.wiki_gaps / 'research_gaps.md'
    hypotheses = paths.wiki_gaps / 'hypotheses.md'
    shared_assumptions = paths.wiki_synthesis / 'shared-assumptions.md'
    field_map = paths.wiki_synthesis / 'field-map.md'

    if not research_gaps.exists():
        issues.append('Missing wiki/gaps/research_gaps.md')
    if not shared_assumptions.exists():
        issues.append('Missing wiki/synthesis/shared-assumptions.md')
    if not field_map.exists():
        issues.append('Missing wiki/synthesis/field-map.md')

    gaps_text = research_gaps.read_text(encoding='utf-8') if research_gaps.exists() else ''
    shared_text = shared_assumptions.read_text(encoding='utf-8') if shared_assumptions.exists() else ''
    if 'counterexample' not in gaps_text.lower():
        quality_pressure.append('Research gaps are missing an explicit counterexample or falsification section.')
    if 'novelty' not in gaps_text.lower() and 'delta' not in gaps_text.lower():
        quality_pressure.append('Research gaps are not clearly stating the novelty delta versus nearby work.')
    if 'prune' not in gaps_text.lower() and 'stop' not in gaps_text.lower():
        quality_pressure.append('Research gaps do not yet define when a direction should be paused or pruned.')
    if 'assumption' not in shared_text.lower():
        quality_pressure.append('Shared assumptions are under-specified, which weakens taste and claim screening.')
    if 'status: draft' in (hypotheses.read_text(encoding='utf-8') if hypotheses.exists() else ''):
        notes.append('There are draft hypotheses that should be challenged against new papers or experiments.')

    if placeholder_hits:
        quality_pressure.append(f'Wiki still contains unresolved placeholders in {len(placeholder_hits)} files.')

    concept_pages = {p.stem.lower() for p in paths.wiki_concepts.glob('*.md')}
    top_candidates = [
        (token, count) for token, count in token_counter.most_common(25)
        if count >= 4 and token not in concept_pages
    ]
    for token, count in top_candidates[:12]:
        notes.append(f'Concept page candidate with real reuse signal: `{token}` ({count} mentions)')

    payload = {
        'issue_count': len(issues),
        'quality_pressure_count': len(quality_pressure),
        'placeholder_files': placeholder_hits,
        'top_concept_candidates': top_candidates[:12],
    }
    save_json(paths.state / 'lint_report.json', payload)

    report = paths.reports / 'lint_report.md'
    lines = ['# Lint Report\n\n']
    lines.append('## Issues\n')
    if issues:
        for issue in issues:
            lines.append(f'- {issue}\n')
    else:
        lines.append('- None\n')
    lines.append('\n## Research Quality Pressure\n')
    if quality_pressure:
        for item in quality_pressure:
            lines.append(f'- {item}\n')
    else:
        lines.append('- None\n')
    lines.append('\n## Improvement Signals\n')
    if notes:
        for note in notes:
            lines.append(f'- {note}\n')
    else:
        lines.append('- None\n')
    report.write_text(''.join(lines), encoding='utf-8')
    print(report)
    print(f'issues={len(issues)}')


if __name__ == '__main__':
    main()
