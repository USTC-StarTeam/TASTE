#!/usr/bin/env python3
from __future__ import annotations

# Consolidated framework wiki/obsidian utilities.


import argparse
import sys
import json
from collections import Counter, defaultdict
from pathlib import Path
from project_paths import build_paths

def boot_load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

def boot_save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def boot_guess_entities(meta: dict[str, object]) -> list[str]:
    entities: list[str] = []
    for author in meta.get('authors', [])[:3]:
        if author:
            entities.append(author)
    for category in meta.get('categories', []):
        if category:
            entities.append(category)
    return entities

def boot_update_markdown_list(path: Path, header: str, items: list[str]) -> None:
    path.write_text(header + '\n\n' + ''.join((f'- {item}\n' for item in items)), encoding='utf-8')

def boot_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    paths = build_paths(args.project)
    compiled = boot_load_json(paths.state / 'compiled_ids.json')
    category_to_papers: dict[str, list[str]] = defaultdict(list)
    entity_to_papers: dict[str, list[str]] = defaultdict(list)
    concept_freq: Counter[str] = Counter()
    overview_lines = ['# Overview\n\n', 'Status: bootstrap-generated\n\n', '## Current landscape\n', '- This overview is a provisional field map built from currently ingested papers.\n', '- It should be replaced or refined by deeper backend-assisted synthesis after deeper reading.\n\n']
    field_map_lines = ['# Field Map\n\n', '## Problem -> Main Approaches -> Representative Papers\n']
    assumptions_lines = ['# Shared Assumptions\n\n', '## Candidate assumptions to test\n']
    for paper_dir in sorted([p for p in paths.raw_papers.iterdir() if p.is_dir()]) if paths.raw_papers.exists() else []:
        meta_path = paper_dir / 'metadata.json'
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        paper_id = meta['paper_id']
        title = meta.get('title', '')
        categories = meta.get('categories', [])
        entities = boot_guess_entities(meta)
        summary = meta.get('summary', '')
        tldr = meta.get('tldr') or 'No TLDR available.'
        concept_links = [f"[[{category.replace('.', '_')}]]" for category in categories]
        entity_links = [f"[[{entity.replace(' ', '_')}]]" for entity in entities]
        paper_text = f"---\npaper_id: {paper_id}\ntitle: {title}\nauthors: {meta.get('authors', [])}\nyear: {str(meta.get('published', ''))[:4]}\nvenue: unknown\nstatus: skimmed\nconfidence: low\ntags: {categories}\n---\n\n## One-sentence Contribution\n{title} addresses part of the project landscape, but still needs deeper synthesis.\n\n## Problem Setting\nDerived from metadata and abstract: {summary}\n\n## Method Core\nBootstrap mode cannot guarantee faithful method reconstruction; replace this with deeper backend-assisted synthesis after reading.\n\n## Experimental Conclusions\nNo trustworthy structured extraction yet; verify against source.\n\n## Limitations and Assumptions\n- Implicit assumptions are not yet deeply audited.\n- Bootstrap notes may miss crucial evaluation caveats.\n\n## Relation to Existing Work\n- built_on: {(' '.join(concept_links) if concept_links else 'unknown')}\n- related_entities: {(' '.join(entity_links) if entity_links else 'unknown')}\n- contradiction: none explicitly marked yet\n\n## Gap Signals\n- What minimal experiment would falsify the central claim is still unclear.\n- Possible weaknesses should be compared against nearby concept pages.\n\n## TLDR\n{tldr}\n"
        (paths.wiki_papers / f'{paper_id}.md').write_text(paper_text, encoding='utf-8')
        for category in categories:
            key = category.replace('.', '_')
            category_to_papers[key].append(paper_id)
            concept_freq[key] += 1
        for entity in entities:
            key = entity.replace(' ', '_')
            entity_to_papers[key].append(paper_id)
        if paper_id not in compiled:
            compiled.append(paper_id)
    for concept, paper_ids in sorted(category_to_papers.items()):
        (paths.wiki_concepts / f'{concept}.md').write_text(f'# {concept}\n\n## Cross-paper Summary\nThis concept page should accumulate understanding across papers rather than repeat any one abstract.\n\n## Related Papers\n' + ''.join((f'- [[{paper_id}]]\n' for paper_id in paper_ids)) + '\n## Open Tensions\n- Are gains coming from this concept itself, or from surrounding engineering choices?\n', encoding='utf-8')
        field_map_lines.append(f"- Problem cluster `{concept}` -> current approach family -> {', '.join((f'[[{p}]]' for p in paper_ids))}\n")
        assumptions_lines.append(f'- `[[{concept}]]` may depend on hidden assumptions that are currently under-specified.\n')
    for entity, paper_ids in sorted(entity_to_papers.items()):
        (paths.wiki_entities / f'{entity}.md').write_text(f'# {entity}\n\n## Entity Type\nAuthor / dataset / benchmark / system (needs manual refinement).\n\n## Related Papers\n' + ''.join((f'- [[{paper_id}]]\n' for paper_id in paper_ids)), encoding='utf-8')
    high_freq = [concept for concept, freq in concept_freq.items() if freq >= 3]
    boot_update_markdown_list(paths.wiki_gaps / 'questions.md', '# Open Questions', ['Which common assumption across the current papers is least justified empirically?', 'Which nearest-neighbor methods need a direct comparison page?', 'Which idea has a clean minimal experiment but no published ablation yet?'])
    boot_update_markdown_list(paths.wiki_gaps / 'confirmed-gaps.md', '# Confirmed Gaps', ['Cross-paper contradictions are still weakly structured and need explicit comparison pages.', 'Many paper pages still lack strong method-core and experiment-core extraction.'])
    boot_update_markdown_list(paths.wiki_gaps / 'hypotheses.md', '# Hypotheses', ['status: draft | Hypothesis: one fragile shared assumption may explain much of the current benchmark progress.', 'status: draft | Hypothesis: the best improvement path may come from tightening evaluation rather than inventing a brand-new module.'])
    research_gaps_text = '# Research Gaps\n\n## Novelty Delta\n- Require a concrete delta over nearest baselines; novelty cannot be claimed from topic mixing alone.\n- Record what mechanism, data slice, or evaluation contract would make the idea genuinely different.\n\n## Counterexample And Falsification\n- Every direction needs a counterexample before scaling: sparse users, long-tail targets, temporal drift, semantic mismatch, or leakage.\n- If a direction fails its own counterexample slice, narrow the claim or prune the route.\n\n## Bad-Case Slicing\n- Experiments must export bad-case slices, not only aggregate metrics.\n- Missing bad-case evidence blocks paper promotion.\n\n## Prune Or Pause Rules\n- Prune after bounded evidence-valid attempts when gains, novelty, or slice behavior remain weak.\n- Data-blocked repos must either acquire verified real data or be replaced by an evidence-ready route.\n'
    (paths.wiki_gaps / 'research_gaps.md').write_text(research_gaps_text, encoding='utf-8')
    overview_lines.append('## High-frequency concept clusters\n')
    for concept in high_freq:
        overview_lines.append(f'- [[{concept}]] appears in 3+ ingested papers and likely deserves deeper synthesis.\n')
    paths.wiki_overview.write_text(''.join(overview_lines), encoding='utf-8')
    (paths.wiki_synthesis / 'field-map.md').write_text(''.join(field_map_lines), encoding='utf-8')
    (paths.wiki_synthesis / 'shared-assumptions.md').write_text(''.join(assumptions_lines), encoding='utf-8')
    boot_save_json(paths.state / 'compiled_ids.json', sorted(set(compiled)))
    print(paths.wiki_papers)

import argparse
import shutil
from pathlib import Path
from project_paths import build_paths, load_project_config

def export_mirror_markdown(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for path in src_dir.rglob('*.md'):
        rel = path.relative_to(src_dir)
        out = dst_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)

def export_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    vault = paths.obsidian
    export_mirror_markdown(paths.wiki, vault / 'wiki')
    export_mirror_markdown(paths.wiki_gaps, vault / 'wiki' / 'gaps')
    export_mirror_markdown(paths.wiki_synthesis, vault / 'wiki' / 'synthesis')
    export_mirror_markdown(paths.reports, vault / 'reports')
    export_mirror_markdown(paths.planning, vault / 'planning')
    export_mirror_markdown(paths.experiments, vault / 'experiments')
    export_mirror_markdown(paths.benchmarks, vault / 'benchmarks')
    export_mirror_markdown(paths.repos_selected, vault / 'repos' / 'selected')
    overview = vault / 'README.md'
    overview.write_text(f"# Obsidian Export: {cfg.get('name', args.project)}\n\n- topic: {cfg.get('topic', '')}\n- conda_env: {cfg.get('conda_env', '')}\n- mode: read-only mirror of generated research assets\n\n## Entry points\n- [wiki/index.md](wiki/index.md)\n- [wiki/overview.md](wiki/overview.md)\n- [wiki/synthesis/field-map.md](wiki/synthesis/field-map.md)\n- [wiki/synthesis/shared-assumptions.md](wiki/synthesis/shared-assumptions.md)\n- [wiki/gaps/confirmed-gaps.md](wiki/gaps/confirmed-gaps.md)\n- [planning/init_brief.md](planning/init_brief.md)\n- [planning/paper_quality.md](planning/paper_quality.md)\n- [experiments/experiment_log.md](experiments/experiment_log.md)\n", encoding='utf-8')
    print(vault)

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from project_paths import build_paths
LINK_RE = re.compile('\\[\\[([^\\]]+)\\]\\]')
WORD_RE = re.compile('\\b[a-zA-Z][a-zA-Z0-9_\\-.]{2,}\\b')
PLACEHOLDER_RE = re.compile('\\b(TODO|TBD|FIXME|unclear|needs manual refinement|under-specified)\\b', re.IGNORECASE)
STOPWORDS = {'research', 'paper', 'papers', 'method', 'methods', 'results', 'baseline', 'experiment', 'overview', 'status', 'this', 'that', 'with', 'from', 'into', 'across', 'than', 'then', 'there', 'their', 'they', 'them', 'have', 'has', 'had', 'should', 'would', 'could', 'will', 'might', 'about', 'after', 'before', 'which', 'while', 'where', 'when', 'what', 'manual', 'refinement', 'current', 'project', 'system', 'needs', 'need', 'page', 'pages', 'wiki', 'concept', 'concepts', 'author', 'authors', 'title', 'summary', 'metadata', 'dataset', 'benchmark', 'benchmarks', 'paper_id', 'none', 'draft'}

def lint_looks_meaningful(token: str) -> bool:
    low = token.lower()
    if low in STOPWORDS:
        return False
    if len(low) < 5:
        return False
    if low.startswith('demo-') or low.startswith('cs.'):
        return False
    if any((ch.isdigit() for ch in low)) and low.count('-') + low.count('_') < 2:
        return False
    if low in {'false', 'true', 'unclear', 'unknown'}:
        return False
    return True

def lint_save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

def lint_main() -> None:
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
            if lint_looks_meaningful(low):
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
    top_candidates = [(token, count) for token, count in token_counter.most_common(25) if count >= 4 and token not in concept_pages]
    for token, count in top_candidates[:12]:
        notes.append(f'Concept page candidate with real reuse signal: `{token}` ({count} mentions)')
    payload = {'issue_count': len(issues), 'quality_pressure_count': len(quality_pressure), 'placeholder_files': placeholder_hits, 'top_concept_candidates': top_candidates[:12]}
    lint_save_json(paths.state / 'lint_report.json', payload)
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

import argparse
import datetime as dt
from pathlib import Path
from project_paths import build_paths
SECTIONS = [('Overview', ['overview.md']), ('Papers', ['papers']), ('Concepts', ['concepts']), ('Entities', ['entities']), ('Comparisons', ['comparisons']), ('Gaps', ['gaps']), ('Synthesis', ['synthesis'])]

def index_one_line_summary(path: Path) -> str:
    lines = [line.strip() for line in path.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        if not line or line.startswith('#') or line.startswith('---'):
            continue
        return line[:180]
    return ''

def index_gather_markdown(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob('*.md'))
    return []

def index_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--log-entry')
    args = parser.parse_args()
    paths = build_paths(args.project)
    lines = ['# Research Wiki Index\n\n']
    for title, rels in SECTIONS:
        lines.append(f'## {title}\n')
        for rel in rels:
            target = paths.wiki / rel
            for path in index_gather_markdown(target):
                relpath = path.relative_to(paths.wiki)
                summary = index_one_line_summary(path)
                lines.append(f'- [[{path.stem}]] `{relpath}` {summary}\n')
        lines.append('\n')
    paths.wiki_index.write_text(''.join(lines), encoding='utf-8')
    if args.log_entry:
        with paths.wiki_log.open('a', encoding='utf-8') as fh:
            fh.write(f'- {dt.datetime.now(dt.timezone.utc).isoformat()} {args.log_entry}\n')
    print(paths.wiki_index)



TOOL_ACTIONS = {
    'bootstrap': boot_main,
    'bootstrap_wiki': boot_main,
    'export': export_main,
    'export_obsidian': export_main,
    'lint': lint_main,
    'lint_wiki': lint_main,
    'refresh_index': index_main,
    'refresh_index_and_log': index_main,
}


def _run_legacy_main(action: str, argv: list[str]) -> int:
    runner = TOOL_ACTIONS.get(action)
    if runner is None:
        print(f'unknown wiki tool action: {action}', file=sys.stderr)
        return 2
    old_argv = sys.argv
    try:
        sys.argv = [f'wiki_tools.py:{action}', *argv]
        runner()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    finally:
        sys.argv = old_argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Framework wiki/obsidian utility tools.')
    parser.add_argument('--tool-action', default='refresh_index')
    ns, rest = parser.parse_known_args(argv)
    action = str(ns.tool_action or '').strip().replace('-', '_')
    return _run_legacy_main(action, rest)


if __name__ == '__main__':
    raise SystemExit(main())

