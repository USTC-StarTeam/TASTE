#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile
from html import unescape
from pathlib import Path
from typing import Any, Iterable

from project_paths import ROOT, build_paths, load_project_config

USER_AGENT = 'Mozilla/5.0 (TASTE Paper Pipeline)'
OFFICIAL_VENUE_HINTS = {
    'cikm': {
        'domains': ['cikm2026.org', 'cikm.org', 'acm.org'],
        'queries': ['CIKM 2026 author guidelines ACM sigconf template', 'CIKM 2026 latex template ACM proceedings', 'ACM proceedings template sigconf zip'],
        'format': 'ACM sigconf',
        'template_family': 'acm-sigconf',
        'official_source_url': 'https://cikm2026.diag.uniroma1.it/full-research-papers/',
        'page_limit_note': 'CIKM 2026 full research papers: at most 10 body/content pages including figures/tables/appendices, plus up to 2 additional pages exclusively for references. TASTE targets 9-10 body pages so undersized drafts do not pass preview gates.',
        'deadlines': {
            'full_applied_abstract_due': '2026-05-16',
            'full_applied_paper_due': '2026-05-23',
            'short_resource_demo_abstract_due': '2026-05-30',
            'short_resource_demo_paper_due': '2026-06-06',
        },
        'required_disclosures': ['ACM CCS concepts', 'ACM reference format', 'GenAI Usage Disclosure when applicable'],
    },
    'acm': {
        'domains': ['acm.org'],
        'queries': ['ACM proceedings template sigconf zip', 'ACM acmart latex template'],
        'format': 'ACM sigconf',
        'template_family': 'acm-sigconf',
    },
    'neurips': {
        'domains': ['neurips.cc'],
        'queries': ['NeurIPS author kit zip', 'NeurIPS latex template'],
    },
    'iclr': {
        'domains': ['iclr.cc', 'openreview.net', 'github.com', 'codeload.github.com'],
        'queries': ['ICLR author instructions latex template', 'ICLR submission template zip'],
        'format': 'ICLR official LaTeX review template',
        'template_family': 'iclr',
        'official_source_url': 'https://github.com/ICLR/Master-Template',
    },
    'icml': {
        'domains': ['icml.cc'],
        'queries': ['ICML author kit latex template', 'ICML style files zip'],
    },
    'cvpr': {
        'domains': ['thecvf.com', 'cvpr.thecvf.com'],
        'queries': ['CVPR author kit zip', 'CVPR template latex'],
    },
    'eccv': {
        'domains': ['eccv.ecva.net'],
        'queries': ['ECCV author kit zip', 'ECCV latex template'],
    },
    'acl': {
        'domains': ['aclweb.org', 'aclanthology.org'],
        'queries': ['ACL author kit latex template', 'ACL style files'],
    },
    'emnlp': {
        'domains': ['aclweb.org', 'aclanthology.org'],
        'queries': ['EMNLP author kit latex template', 'EMNLP style files'],
    },
    'aaai': {
        'domains': ['aaai.org'],
        'queries': ['AAAI author kit latex template', 'AAAI style files'],
    },
    'jmlr': {
        'domains': ['jmlr.org'],
        'queries': ['JMLR latex style file', 'JMLR author kit'],
    },
    'nature': {
        'domains': ['nature.com', 'springernature.com', 'cms-resources.apps.public.k8s.springernature.io'],
        'queries': ['Nature for authors formatting guide initial submission', 'Springer Nature LaTeX author support journal article template package'],
        'format': 'Nature-family flexible initial-submission manuscript plus Springer Nature journal article LaTeX preview template',
        'template_family': 'springer-nature',
        'official_source_url': 'https://www.nature.com/nature/for-authors',
        'official_archive_url': 'https://cms-resources.apps.public.k8s.springernature.io/springer-cms/rest/v1/content/18782940/data/v12',
        'page_limit_note': 'Nature initial submissions use flexible manuscript formatting; TASTE records exact page/word limits only when the current official journal page exposes them.',
    },
    'springer-nature': {
        'domains': ['springernature.com', 'cms-resources.apps.public.k8s.springernature.io'],
        'queries': ['Springer Nature LaTeX author support journal article template package'],
        'format': 'Springer Nature journal article LaTeX template',
        'template_family': 'springer-nature',
        'official_source_url': 'https://www.springernature.com/gp/authors/campaigns/latex-author-support',
        'official_archive_url': 'https://cms-resources.apps.public.k8s.springernature.io/springer-cms/rest/v1/content/18782940/data/v12',
    },
}

DEFAULT_REVIEWERS = [
    {
        'name': 'novelty_reviewer',
        'focus': 'novelty delta, nearest-neighbor prior work, whether the paper moves a field-level assumption',
    },
    {
        'name': 'claim_reviewer',
        'focus': 'claim strength, benchmark support, ablation credibility, whether the headline outruns the evidence',
    },
    {
        'name': 'counterexample_reviewer',
        'focus': 'counterexamples, falsification pressure, scope boundaries, stress settings that would break the claim',
    },
    {
        'name': 'bad_case_reviewer',
        'focus': 'bad-case slicing, weakest data slices, error analysis quality, whether the loop learned from failures',
    },
    {
        'name': 'taste_reviewer',
        'focus': 'top-tier taste, contribution sharpness, prune discipline, and whether the narrative feels like a real conference paper',
    },
]

PLACEHOLDER_PATTERNS = [
    re.compile(r'^-\s+[A-Z][^:]{0,80}:\s*$'),
    re.compile(r'^TODO\b', re.IGNORECASE),
    re.compile(r'^TBD\b', re.IGNORECASE),
    re.compile(r'^No structured .* yet\.$'),
]


def slugify(text: str) -> str:
    lowered = re.sub(r'[^a-zA-Z0-9]+', '-', text.strip().lower())
    return lowered.strip('-') or 'venue'


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def ensure_paper_dirs(project: str) -> dict[str, Path]:
    paths = build_paths(project)
    paper_root = paths.root / 'paper'
    draft_dir = paper_root / 'drafts'
    review_dir = paper_root / 'reviews'
    review_internal_dir = review_dir / 'internal'
    venue_dir = paper_root / 'venues'
    output_dir = paper_root / 'output'
    metadata_dir = paper_root / 'metadata'
    response_dir = review_dir / 'responses'
    rereview_dir = review_dir / 're_review'
    for directory in [paper_root, draft_dir, review_dir, review_internal_dir, venue_dir, output_dir, metadata_dir, response_dir, rereview_dir]:
        directory.mkdir(parents=True, exist_ok=True)
    return {
        'root': paper_root,
        'draft_dir': draft_dir,
        'review_dir': review_dir,
        'review_internal_dir': review_internal_dir,
        'response_dir': response_dir,
        'rereview_dir': rereview_dir,
        'venue_dir': venue_dir,
        'output_dir': output_dir,
        'metadata_dir': metadata_dir,
        'draft_md': draft_dir / 'paper_draft.md',
        'revised_md': draft_dir / 'paper_revision.md',
        'review_md': review_dir / 'paper_review_packet.md',
        'aggregate_review_md': review_dir / 'aggregated_review.md',
        'author_response_md': response_dir / 'author_response.md',
        're_review_md': rereview_dir / 're_review_summary.md',
        'aggregate_review_json': metadata_dir / 'aggregated_review.json',
        're_review_json': metadata_dir / 're_review_summary.json',
        'paper_metadata': metadata_dir / 'paper_metadata.json',
        'pipeline_state': metadata_dir / 'paper_pipeline.json',
    }


def draft_title_from_config(cfg: dict) -> str:
    topic = cfg.get('topic', 'Research Project').strip()
    words = [word.capitalize() for word in re.split(r'[^a-zA-Z0-9]+', topic) if word]
    return ' '.join(words[:12]) or 'Research Project'


def strip_markdown_header(text: str) -> str:
    return '\n'.join(line for line in text.splitlines() if not line.startswith('#')).strip()


def is_summary_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith('#'):
        return False
    if re.match(r'^\|?\s*[-:]{3,}', stripped):
        return False
    if stripped.startswith('|'):
        return False
    return True


def extract_summary_lines(text: str, limit: int = 8) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not is_summary_line(stripped):
            continue
        if stripped not in lines:
            lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def compact_bullets(lines: Iterable[str]) -> str:
    cleaned = []
    for line in lines:
        if not line or not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith('- '):
            stripped = stripped[2:].strip()
        cleaned.append(stripped)
    return '\n'.join(f'- {line}' for line in cleaned)


def count_placeholder_lines(text: str) -> int:
    total = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if any(pattern.match(stripped) for pattern in PLACEHOLDER_PATTERNS):
            total += 1
    return total


def list_placeholder_lines(text: str, limit: int = 10) -> list[str]:
    hits: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if any(pattern.match(stripped) for pattern in PLACEHOLDER_PATTERNS):
            hits.append(stripped)
        if len(hits) >= limit:
            break
    return hits


def summarize_experiments(rows: list[dict]) -> dict[str, object]:
    completed = [row for row in rows if str(row.get('status', '')).lower() in {'completed', 'success'}]
    failed = [row for row in rows if str(row.get('status', '')).lower() in {'failed', 'error'}]
    claim_checked = [row for row in rows if str(row.get('claim_verdict', '')).strip()]
    bad_case_runs = [row for row in rows if str(row.get('bad_case_path', '')).strip()]
    best = None
    scored = [row for row in completed if isinstance(row.get('metric_value'), (int, float))]
    if scored:
        scored.sort(key=lambda row: float(row.get('metric_value', -1e18)), reverse=True)
        best = scored[0]
    return {
        'completed_count': len(completed),
        'failed_count': len(failed),
        'claim_checked_count': len(claim_checked),
        'bad_case_count': len(bad_case_runs),
        'best': best,
    }


def ascii_latex_text(text: str) -> str:
    # ACM fallback uses pdfLaTeX; keep generated drafts compile-safe even when project notes contain CJK text.
    out = []
    replaced = False
    for char in str(text or ''):
        if ord(char) < 128:
            out.append(char)
        elif char.isspace():
            out.append(' ')
        else:
            replaced = True
    cleaned = ''.join(out)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if replaced and not cleaned:
        return '[non-ASCII project text omitted in PDF draft]'
    if replaced:
        return cleaned + ' [non-ASCII text omitted in PDF draft]'
    return cleaned


def escape_latex(text: str) -> str:
    text = ascii_latex_text(text)
    replacements = {
        '\\': r'\textbackslash{}',
        '&': r'\&',
        '%': r'\%',
        '$': r'\$',
        '#': r'\#',
        '_': r'\_',
        '{': r'\{',
        '}': r'\}',
        '~': r'\textasciitilde{}',
        '^': r'\textasciicircum{}',
    }
    out = []
    for char in text:
        out.append(replacements.get(char, char))
    return ''.join(out)


def markdown_to_latex(markdown: str) -> str:
    latex_lines: list[str] = []
    in_list = False
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_list:
                latex_lines.append(r'\end{itemize}')
                in_list = False
            latex_lines.append('')
            continue
        if stripped.startswith('# '):
            if in_list:
                latex_lines.append(r'\end{itemize}')
                in_list = False
            latex_lines.append(rf'\section{{{escape_latex(stripped[2:].strip())}}}')
            continue
        if stripped.startswith('## '):
            if in_list:
                latex_lines.append(r'\end{itemize}')
                in_list = False
            latex_lines.append(rf'\subsection{{{escape_latex(stripped[3:].strip())}}}')
            continue
        if stripped.startswith('### '):
            if in_list:
                latex_lines.append(r'\end{itemize}')
                in_list = False
            latex_lines.append(rf'\subsubsection{{{escape_latex(stripped[4:].strip())}}}')
            continue
        bullet = re.match(r'^[-*]\s+(.*)$', stripped)
        numbered = re.match(r'^\d+\.\s+(.*)$', stripped)
        if bullet or numbered:
            if not in_list:
                latex_lines.append(r'\begin{itemize}')
                in_list = True
            content = bullet.group(1) if bullet else numbered.group(1)
            latex_lines.append(rf'\item {escape_latex(content)}')
            continue
        if in_list:
            latex_lines.append(r'\end{itemize}')
            in_list = False
        latex_lines.append(escape_latex(stripped))
    if in_list:
        latex_lines.append(r'\end{itemize}')
    return '\n'.join(latex_lines).strip() + '\n'


def venue_info(venue: str) -> dict:
    slug = slugify(venue)
    for key, value in OFFICIAL_VENUE_HINTS.items():
        if key in slug:
            return {'slug': slug, **value}
    return {'slug': slug, 'domains': [], 'queries': [f'{venue} latex template', f'{venue} author kit zip']}


def active_project_from_env() -> str:
    return str(os.environ.get('PROJECT_ID') or os.environ.get('ACTIVE_PROJECT') or '').strip()


def load_venue_requirements(venue: str, project: str = '') -> dict:
    project = (project or active_project_from_env()).strip()
    if not project:
        return {}
    path = build_paths(project).root / 'paper' / 'venues' / slugify(venue) / 'venue_requirements.json'
    try:
        payload = load_json(path, {})
    except Exception:
        return {}
    if not isinstance(payload, dict) or payload.get('status') not in {'ok', 'pass'}:
        return {}
    return payload


def dynamic_venue_submission_policy(venue: str, track: str = '', project: str = '') -> dict[str, object]:
    payload = load_venue_requirements(venue, project=project)
    policy = payload.get('venue_submission_policy') if isinstance(payload.get('venue_submission_policy'), dict) else {}
    if not policy:
        return {}
    merged = dict(policy)
    merged.setdefault('venue', venue)
    merged.setdefault('slug', slugify(venue))
    merged.setdefault('track', slugify(track) if track else str(payload.get('track') or 'conference'))
    merged.setdefault('status', 'known')
    merged.setdefault('source_checked_date', str(payload.get('source_checked_at') or '')[:10])
    merged.setdefault('official_sources', payload.get('official_sources', []))
    paper_shape = payload.get('paper_shape') if isinstance(payload.get('paper_shape'), dict) else {}
    if isinstance(paper_shape, dict):
        if paper_shape.get('required_back_matter') and not merged.get('required_back_matter'):
            merged['required_back_matter'] = list(paper_shape.get('required_back_matter') or [])
        if paper_shape.get('nature_family_article_mode') and not merged.get('nature_family_article_mode'):
            merged['nature_family_article_mode'] = True
    return merged


def dynamic_venue_template_profile(venue: str, project: str = '') -> dict[str, object]:
    payload = load_venue_requirements(venue, project=project)
    profile = payload.get('venue_template_profile') if isinstance(payload.get('venue_template_profile'), dict) else {}
    if not profile:
        return {}
    merged = dict(profile)
    merged.setdefault('venue', venue)
    merged.setdefault('slug', slugify(venue))
    template = payload.get('template') if isinstance(payload.get('template'), dict) else {}
    merged.setdefault('family', str(template.get('family') or 'generic'))
    merged.setdefault('template_aliases', venue_slug_aliases(venue))
    merged.setdefault('submission_policy', dynamic_venue_submission_policy(venue, project=project))
    return merged


def allow_static_venue_policy_fallback() -> bool:
    return str(os.environ.get('ALLOW_STATIC_VENUE_POLICY_FALLBACK') or '').lower() in {'1', 'true', 'yes', 'on'}


def unresolved_venue_policy(venue: str, track: str = '') -> dict[str, object]:
    return {
        'status': 'blocked_unresolved_venue_policy',
        'venue': venue,
        'slug': slugify(venue),
        'track': slugify(track) if track else 'conference',
        'source_label': 'venue-intelligence not resolved yet',
        'source_checked_date': '',
        'format_label': '',
        'template_family': '',
        'required_documentclass': '',
        'required_documentclass_options': [],
        'body_page_min': 0,
        'body_page_max': 0,
        'reference_page_max': 0,
        'total_page_max': 0,
        'min_references': 0,
        'official_min_references': 0,
        'reference_quality_target': 0,
        'reference_quality_target': 0,
        'reference_target_source': 'none',
        'estimated_references_per_page': 0,
        'anonymous_required': False,
        'desk_reject_risks': [
            {
                'id': 'venue_requirements_unresolved',
                'requirement': 'Run venue-intelligence to resolve latest official rules and template before paper writing/audit.',
                'automatable': True,
                'severity': 'block',
            }
        ],
    }


def venue_slug_aliases(venue: str) -> list[str]:
    slug = slugify(venue)
    aliases = [slug]
    if 'cikm' in slug:
        if not re.search(r'(?:^|-)20\d{2}(?:-|$)', slug):
            aliases.extend(['cikm-2026', 'cikm-2025'])
        aliases.append(re.sub(r'-20\d{2}\b', '', slug).strip('-') or 'cikm')
    if 'acm' in slug:
        aliases.append('acm-sigconf')
    if 'nature' in slug or 'springer-nature' in slug:
        aliases.extend(['nature', 'springer-nature'])
    deduped: list[str] = []
    for alias in aliases:
        if alias and alias not in deduped:
            deduped.append(alias)
    return deduped


def venue_template_profile(venue: str, project: str = '') -> dict[str, object]:
    dynamic = dynamic_venue_template_profile(venue, project=project)
    if dynamic:
        return dynamic
    if not allow_static_venue_policy_fallback():
        return {
            'venue': venue,
            'slug': slugify(venue),
            'family': 'unresolved',
            'format_label': '',
            'documentclass': '',
            'required_options': [],
            'recommended_options': [],
            'forbidden_documentclasses': [],
            'template_aliases': venue_slug_aliases(venue),
            'page_limit_note': 'venue-intelligence must resolve the latest official template before writing or auditing.',
            'submission_policy': unresolved_venue_policy(venue),
            'submission_notes': ['Resolve official venue requirements and template first; do not infer rules from static hints.'],
        }
    info = venue_info(venue)
    slug = str(info.get('slug') or slugify(venue))
    family = str(info.get('template_family') or '')
    policy = venue_submission_policy(venue, project=project)
    if family == 'acm-sigconf' or 'cikm' in slug or slug == 'acm':
        return {
            'venue': venue,
            'slug': slug,
            'family': 'acm-sigconf',
            'format_label': 'ACM Master Article Submission Template, 2-column sigconf',
            'documentclass': 'acmart',
            'required_options': ['sigconf'] + (['anonymous'] if policy.get('anonymous_required') else []),
            'recommended_options': ['anonymous'],
            'forbidden_documentclasses': ['article', 'llncs', 'IEEEtran'],
            'template_aliases': venue_slug_aliases(venue),
            'page_limit_note': str(info.get('page_limit_note') or ''),
            'submission_policy': policy,
            'submission_notes': [
                'Use the ACM Master Article Submission Template in 2-column sigconf format.',
                'For review submissions, keep the manuscript anonymous and do not include author-identifying information.',
                'Do not change ACM template fonts, margins, or column layout.',
            ],
        }
    if family == 'springer-nature' or 'nature' in slug:
        return {
            'venue': venue,
            'slug': slug,
            'family': 'springer-nature',
            'format_label': str(info.get('format') or 'Springer Nature journal article LaTeX preview template'),
            'documentclass': 'sn-jnl',
            'required_options': ['pdflatex', 'sn-nature'],
            'recommended_options': [],
            'forbidden_documentclasses': ['article', 'acmart', 'llncs', 'IEEEtran'],
            'template_aliases': venue_slug_aliases(venue),
            'page_limit_note': str(info.get('page_limit_note') or ''),
            'submission_policy': policy,
            'submission_notes': [
                'Nature-family initial submission may accept flexible manuscript format or a single Word/PDF according to official journal instructions; TASTE LaTeX output is a preview artifact unless the resolved contract says LaTeX is required.',
                'Use the official Springer Nature journal article template only when producing TASTE LaTeX/PDF preview artifacts.',
                'The Nature Portfolio style option is sn-nature; keep data/code availability and reproducible Methods when applicable.',
            ],
        }
    if family == 'iclr' or 'iclr' in slug:
        return {
            'venue': venue,
            'slug': slug,
            'family': 'iclr',
            'format_label': 'ICLR official LaTeX review template',
            'documentclass': 'article',
            'required_options': [],
            'recommended_options': [],
            'forbidden_documentclasses': ['acmart', 'llncs', 'IEEEtran'],
            'template_aliases': venue_slug_aliases(venue),
            'page_limit_note': str(info.get('page_limit_note') or ''),
            'submission_policy': policy,
            'submission_notes': [
                'Use the official ICLR LaTeX review template from ICLR/Master-Template.',
                'Use the resolved official ICLR conference style and matching bibliography/style sidecars, not a local minimal fallback.',
                'Keep review submissions anonymous unless preparing camera-ready output.',
            ],
        }
    return {
        'venue': venue,
        'slug': slug,
        'family': 'generic',
        'format_label': str(info.get('format') or 'venue official LaTeX template'),
        'documentclass': '',
        'required_options': [],
        'recommended_options': [],
        'forbidden_documentclasses': ['article'],
        'template_aliases': venue_slug_aliases(venue),
        'page_limit_note': str(info.get('page_limit_note') or ''),
        'submission_policy': policy,
        'submission_notes': [
            'Use the target venue official LaTeX submission template.',
            'Do not silently fall back to a generic article template when a venue is specified.',
        ],
    }



def venue_reference_target(venue: str, project: str = '', explicit_min: int = 0) -> dict[str, Any]:
    """Resolve the active citation/reference target from current venue state.

    `explicit_min` is only used when a caller intentionally passes a value. A
    missing venue contract must not silently fall back to a hard-coded target.
    """
    try:
        explicit = int(explicit_min or 0)
    except (TypeError, ValueError):
        explicit = 0
    policy = venue_submission_policy(venue, project=project)
    status = str(policy.get('status') or '') if isinstance(policy, dict) else ''
    try:
        official_min = int(policy.get('official_min_references') or policy.get('min_references') or 0) if isinstance(policy, dict) else 0
    except (TypeError, ValueError):
        official_min = 0
    try:
        quality_target = int(policy.get('reference_quality_target') or policy.get('reference_quality_target') or 0) if isinstance(policy, dict) else 0
    except (TypeError, ValueError):
        quality_target = 0
    if official_min and official_min >= quality_target:
        target = official_min
        source = 'official'
    elif quality_target:
        target = quality_target
        source = 'quality_target'
    elif explicit:
        target = explicit
        source = 'explicit_cli'
    else:
        target = 0
        source = 'unresolved_venue_policy' if status.startswith('blocked_unresolved') else 'none'
    return {
        'target': target,
        'source': source,
        'official_min_references': official_min,
        'reference_quality_target': quality_target,
        'policy_status': status,
        'policy': policy if isinstance(policy, dict) else {},
    }


def venue_submission_policy(venue: str, track: str = '', project: str = '') -> dict[str, object]:
    """Return venue hard requirements that should outrank paper polishing.

    The normal path is dynamic: venue-intelligence writes
    paper/venues/<venue>/venue_requirements.json from current official sources.
    Static hints below are legacy fallbacks and are disabled unless explicitly
    requested, so missing venue-intelligence cannot masquerade as fresh policy.
    """
    dynamic = dynamic_venue_submission_policy(venue, track=track, project=project)
    if dynamic:
        return dynamic
    if not allow_static_venue_policy_fallback():
        return unresolved_venue_policy(venue, track=track)
    info = venue_info(venue)
    slug = str(info.get('slug') or slugify(venue))
    normalized_track = slugify(track) if track else ''
    if 'iclr' in slug:
        return {
            'status': 'known',
            'venue': venue,
            'slug': slug,
            'track': normalized_track or 'conference',
            'source_url': str(info.get('official_source_url') or 'https://github.com/ICLR/Master-Template'),
            'source_label': 'ICLR official submission template/instructions',
            'source_checked_date': '2026-05-25',
            'format_label': 'ICLR official LaTeX review template',
            'template_family': 'iclr',
            'required_documentclass': 'article',
            'required_documentclass_options': [],
            'body_page_min': 0,
            'body_page_max': 9,
            'reference_page_max': 0,
            'total_page_max': 0,
            'min_references': 0,
            'official_min_references': 0,
            'reference_quality_target': 0,
            'reference_quality_target': 0,
            'reference_target_source': 'none',
            'min_word_count': 3000,
            'estimated_references_per_page': 30,
            'canonical_sections': ['Introduction', 'Related Work', 'Method', 'Experiments', 'Conclusion'],
            'max_main_sections': 7,
            'anonymous_required': True,
            'self_citation_third_person_required': True,
            'reviewer_nomination_required': False,
            'reproducibility_expected': True,
            'desk_reject_risks': [
                {
                    'id': 'wrong_template',
                    'requirement': 'Use the official ICLR LaTeX review template/style.',
                    'automatable': True,
                    'severity': 'block',
                },
                {
                    'id': 'not_anonymous',
                    'requirement': 'Double-blind submission must not contain author names, affiliations, or identifying information.',
                    'automatable': True,
                    'severity': 'block',
                },
            ],
        }
    if 'cikm' in slug and (not normalized_track or 'full' in normalized_track or 'research' in normalized_track):
        return {
            'status': 'known',
            'venue': venue,
            'slug': slug,
            'track': 'full-research',
            'source_url': 'https://cikm2026.diag.uniroma1.it/full-research-papers/',
            'source_label': 'CIKM 2026 Full Research Papers submission guidelines',
            'source_checked_date': '2026-05-19',
            'format_label': 'ACM Master Article Submission Template, 2-column sigconf',
            'template_family': 'acm-sigconf',
            'required_documentclass': 'acmart',
            'required_documentclass_options': ['sigconf', 'anonymous'],
            'body_page_min': 9,
            'body_page_max': 10,
            'body_page_min_reason': 'TASTE quality target for a complete full-paper draft; prevents undersized papers from passing local gates.',
            'reference_page_max': 2,
            'total_page_max': 12,
            'min_references': 0,
            'official_min_references': 0,
            'reference_quality_target': 0,
            'reference_quality_target': 0,
            'reference_target_source': 'none',
            'estimated_references_per_page': 34,
            'canonical_sections': ['Introduction', 'Related Work', 'Method', 'Experiments', 'Conclusion'],
            'max_main_sections': 7,
            'anonymous_required': True,
            'self_citation_third_person_required': True,
            'reviewer_nomination_required': True,
            'reproducibility_expected': True,
            'desk_reject_risks': [
                {
                    'id': 'overlength',
                    'requirement': 'Body/content must not exceed 10 pages; references may use at most 2 additional pages.',
                    'automatable': True,
                    'severity': 'block',
                },
                {
                    'id': 'undersized_body',
                    'requirement': 'generated full-paper body must reach 9 pages before it can be treated as a complete preview.',
                    'automatable': True,
                    'severity': 'block',
                },
                {
                    'id': 'wrong_template',
                    'requirement': 'Use ACM Master Article Submission Templates in 2-column sigconf format.',
                    'automatable': True,
                    'severity': 'block',
                },
                {
                    'id': 'not_anonymous',
                    'requirement': 'Double-blind submission must not contain author names, affiliations, or identifying information.',
                    'automatable': True,
                    'severity': 'block',
                },
                {
                    'id': 'missing_author_reviewer_nomination',
                    'requirement': 'At least one author must be nominated as a reviewer in EasyChair; CIKM states failure leads to desk rejection.',
                    'automatable': False,
                    'severity': 'block',
                },
            ],
        }
    return {
        'status': 'unknown',
        'venue': venue,
        'slug': slug,
        'track': normalized_track,
        'source_url': str(info.get('official_source_url') or ''),
        'source_label': '',
        'source_checked_date': '',
        'format_label': str(info.get('format') or 'venue official template'),
        'template_family': str(info.get('template_family') or 'generic'),
        'body_page_min': 0,
        'body_page_max': 0,
        'reference_page_max': 0,
        'total_page_max': 0,
        'min_references': 0,
        'canonical_sections': [],
        'max_main_sections': 0,
        'anonymous_required': False,
        'reviewer_nomination_required': False,
        'reproducibility_expected': False,
        'desk_reject_risks': [],
        'policy_gap': 'No explicit venue policy has been encoded yet; The workflow must fetch official author instructions before marking submission-ready.',
    }


def latex_documentclass(text: str) -> dict[str, object]:
    match = re.search(r'\\documentclass(?:\[(?P<options>[^\]]*)\])?\{(?P<class>[^{}]+)\}', text)
    if not match:
        return {'exists': False, 'class': '', 'options': []}
    options = [item.strip() for item in (match.group('options') or '').split(',') if item.strip()]
    return {'exists': True, 'class': match.group('class').strip(), 'options': options}


def _strip_latex_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        escaped = False
        out: list[str] = []
        for char in line:
            if char == '%' and not escaped:
                break
            out.append(char)
            escaped = char == '\\' and not escaped
            if char != '\\':
                escaped = False
        lines.append(''.join(out))
    return '\n'.join(lines)


def _is_springer_nature_family(venue: str, project: str = '', *, text: str = '') -> bool:
    profile = venue_template_profile(venue, project=project) if venue else {}
    parsed = latex_documentclass(text) if text else {}
    options = {str(item) for item in parsed.get('options', [])} if isinstance(parsed, dict) else set()
    return bool(
        profile.get('family') == 'springer-nature'
        or parsed.get('class') == 'sn-jnl'
        or 'sn-nature' in options
    )



def normalized_latex_section_headings(text: str) -> list[str]:
    headings = re.findall(r'\\section\*?\{([^{}]+)\}', text)
    return [re.sub(r'[^a-z0-9 ]+', ' ', item.lower()).strip() for item in headings]


def heading_present(headings: list[str], canonical: str) -> bool:
    target = re.sub(r'[^a-z0-9 ]+', ' ', canonical.lower()).strip()
    aliases = {target}
    if target.endswith('s'):
        aliases.add(target[:-1])
    if target == 'method':
        aliases.add('methods')
    if target == 'methods':
        aliases.add('method')
    if target == 'results':
        aliases.add('result')
    if target == 'experiments':
        aliases.update({'experiment', 'experimental results', 'results', 'evaluation'})
    return any(any(alias == heading or alias in heading for alias in aliases if alias) for heading in headings)


def _latex_back_matter_headings(text: str) -> list[str]:
    headings = re.findall(r'\\(?:bmhead|section\*?|paragraph\*?)\{([^{}]+)\}', text)
    return [re.sub(r'[^a-z0-9 ]+', ' ', item.lower()).strip() for item in headings]


def springer_nature_article_shape_failures(text: str, venue: str, project: str = '') -> list[str]:
    if not _is_springer_nature_family(venue, project=project, text=text):
        return []
    policy = venue_submission_policy(venue, project=project) if venue else {}
    sections = normalized_latex_section_headings(text)
    back_matter = _latex_back_matter_headings(text)
    failures: list[str] = []

    if re.search(r'\\keywords\s*\{', text):
        failures.append('Nature-family article preview must not render a Keywords block unless the resolved journal contract explicitly requires one')

    conference_sections = {
        'related work': 'Related Work',
        'experiments': 'Experiments',
        'conclusion': 'Conclusion',
    }
    for normalized, label in conference_sections.items():
        if normalized in sections:
            failures.append(f'Nature-family article mode must not use conference-style top-level section: {label}')

    required_sections = [str(item) for item in policy.get('canonical_sections', []) if str(item).strip()] if isinstance(policy, dict) else []
    if not required_sections:
        required_sections = ['Introduction', 'Results', 'Discussion', 'Methods']
    missing_sections = [section for section in required_sections if not heading_present(sections, section)]
    if missing_sections:
        failures.append('missing Nature-family article sections: ' + ', '.join(missing_sections))

    required_back_matter = [str(item) for item in policy.get('required_back_matter', []) if str(item).strip()] if isinstance(policy, dict) else []
    if isinstance(policy, dict) and policy.get('data_availability_expected') and 'Data availability' not in required_back_matter:
        required_back_matter.append('Data availability')
    if isinstance(policy, dict) and policy.get('code_availability_expected') and 'Code availability' not in required_back_matter:
        required_back_matter.append('Code availability')
    if not required_back_matter:
        required_back_matter = ['Data availability', 'Code availability']
    missing_back_matter = [section for section in required_back_matter if not heading_present(back_matter, section)]
    if missing_back_matter:
        failures.append('missing Nature-family back matter: ' + ', '.join(missing_back_matter))

    if re.search(r'\\section\*?\{\s*Discussion\s*\}.*?\\section\*?\{\s*Conclusion\s*\}', text, flags=re.IGNORECASE | re.DOTALL):
        failures.append('Nature-family article mode should integrate conclusion into Discussion instead of using a separate Conclusion section')
    return failures

def _extract_latex_macro(text: str, macro: str) -> str:
    match = re.search(r'\\' + re.escape(macro) + r'(?:\[[^\]]*\])?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', text, flags=re.DOTALL)
    return match.group(1).strip() if match else ''


def latex_plain_text(text: str) -> str:
    text = re.sub(r'\\[a-zA-Z]+\*?(?:\[[^\]]*\])?', ' ', text)
    text = re.sub(r'[{}$]', ' ', text)
    text = re.sub(r'~|\\&|\\%', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _latex_macro_spans(text: str, macro: str) -> list[dict[str, object]]:
    spans: list[dict[str, object]] = []
    pattern = re.compile(r'\\' + re.escape(macro) + r'\*?(?:\[[^\]]*\])?\{')
    for match in pattern.finditer(text):
        body_start = match.end()
        depth = 1
        idx = body_start
        escaped = False
        while idx < len(text):
            ch = text[idx]
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    spans.append({
                        'start': match.start(),
                        'end': idx + 1,
                        'macro': text[match.start():idx + 1],
                        'body': text[body_start:idx],
                    })
                    break
            idx += 1
    return spans


def _springer_nature_affiliation_macros(text: str) -> list[dict[str, object]]:
    return _latex_macro_spans(text, 'affil')


def _springer_nature_author_macros(text: str) -> list[dict[str, object]]:
    return _latex_macro_spans(text, 'author')


def _is_anonymous_author_body(body: str) -> bool:
    plain = latex_plain_text(body).lower()
    return 'anonymous' in plain and ('author' in plain or 'authors' in plain)


def _is_placeholder_affiliation_body(body: str) -> bool:
    plain = latex_plain_text(body).lower()
    compact = re.sub(r'[^a-z0-9]+', ' ', plain).strip()
    if not compact:
        return False
    placeholder_phrases = [
        'department institution',
        'department organization',
        'institution city country',
        'organization street city',
        'anonymous institution',
        'city country',
        'state country',
    ]
    return any(phrase in compact for phrase in placeholder_phrases)


def _normalize_springer_nature_anonymous_author_block(text: str) -> tuple[str, bool]:
    changed = False
    pieces: list[str] = []
    cursor = 0
    for span in _springer_nature_author_macros(text):
        start = int(span['start'])
        end = int(span['end'])
        macro_text = str(span['macro'])
        body = str(span['body'])
        pieces.append(text[cursor:start])
        if _is_anonymous_author_body(body):
            replacement = r'\author{\fnm{Anonymous} \sur{Authors}}'
            pieces.append(replacement)
            if macro_text != replacement:
                changed = True
        else:
            pieces.append(macro_text)
        cursor = end
    if not pieces:
        return text, False
    pieces.append(text[cursor:])
    return ''.join(pieces), changed


def _remove_springer_nature_placeholder_affiliations(text: str) -> tuple[str, int]:
    removed = 0
    pieces: list[str] = []
    cursor = 0
    for span in _springer_nature_affiliation_macros(text):
        start = int(span['start'])
        end = int(span['end'])
        body = str(span['body'])
        if _is_placeholder_affiliation_body(body):
            pieces.append(text[cursor:start])
            cursor = end
            while cursor < len(text) and text[cursor] in ' \t\r\n':
                cursor += 1
            removed += 1
            continue
        pieces.append(text[cursor:end])
        cursor = end
    if not pieces:
        return text, 0
    pieces.append(text[cursor:])
    return ''.join(pieces), removed


def springer_nature_placeholder_front_matter_failures(text: str) -> list[str]:
    failures: list[str] = []
    for match in _springer_nature_author_macros(text):
        body = str(match.get('body') or '')
        macro = str(match.get('macro') or '')
        if _is_anonymous_author_body(body) and (macro.startswith(r'\author*') or re.match(r'\\author\*?\[[^\]]+\]', macro)):
            failures.append('Springer Nature anonymous preview must not use corresponding-author stars or numeric affiliation labels')
            break
    for match in _springer_nature_affiliation_macros(text):
        if _is_placeholder_affiliation_body(str(match.get('body') or '')):
            failures.append('Springer Nature anonymous preview must not include placeholder affiliation text such as Department/Institution/City/Country')
            break
    return failures


def normalize_venue_front_matter(text: str, venue: str, project: str = '') -> tuple[str, list[str]]:
    if not text or not _is_springer_nature_family(venue, project=project, text=text):
        return text, []
    changes: list[str] = []
    out = text
    out, anonymous_author_changed = _normalize_springer_nature_anonymous_author_block(out)
    if anonymous_author_changed:
        changes.append('springer_nature_anonymous_author_unlinked_from_placeholder_affiliation')
    out, placeholder_affiliations_removed = _remove_springer_nature_placeholder_affiliations(out)
    if placeholder_affiliations_removed:
        changes.append('springer_nature_placeholder_affiliation_removed')
    env_pattern = re.compile(r'\\begin\{abstract\}(?P<body>.*?)\\end\{abstract\}', flags=re.DOTALL)
    match = env_pattern.search(out)
    if match:
        body = match.group('body').strip()
        out = out[:match.start()] + r'\abstract{' + body + '}\n' + out[match.end():]
        changes.append('springer_nature_abstract_environment_converted_to_macro')
    macro_match = re.search(r'\\abstract\{', out)
    maketitle_match = re.search(r'\\maketitle\b', out)
    if macro_match and maketitle_match and macro_match.start() > maketitle_match.start():
        abstract_block = re.search(r'\\abstract\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', out[macro_match.start():], flags=re.DOTALL)
        if abstract_block:
            start = macro_match.start()
            end = start + abstract_block.end()
            block = out[start:end].strip()
            out = out[:start] + out[end:]
            maketitle_match = re.search(r'\\maketitle\b', out)
            if maketitle_match:
                out = out[:maketitle_match.start()] + block + '\n\n' + out[maketitle_match.start():]
                changes.append('springer_nature_abstract_macro_moved_before_maketitle')
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out, changes


def springer_nature_front_matter_failures(text: str, venue: str, project: str = '') -> list[str]:
    if not _is_springer_nature_family(venue, project=project, text=text):
        return []
    visible = _strip_latex_comments(text)
    failures: list[str] = []
    if re.search(r'\\begin\{abstract\}', visible):
        failures.append('Springer Nature sn-jnl front matter must use \\abstract{...}, not the abstract environment')
    if not re.search(r'\\abstract\{', visible):
        failures.append('Springer Nature sn-jnl front matter is missing \\abstract{...} before \\maketitle')
    maketitle = re.search(r'\\maketitle\b', visible)
    if not maketitle:
        failures.append('Springer Nature sn-jnl front matter is missing \\maketitle')
        return failures
    before = visible[:maketitle.start()]
    for macro, label in [('title', 'title'), ('author', 'author'), ('abstract', 'abstract')]:
        if not re.search(r'\\' + macro + r'\*?(?:\[[^\]]*\])?\{', before):
            failures.append(f'Springer Nature sn-jnl front matter must place {label} before \\maketitle')
    if re.search(r'\\abstract\{', visible[maketitle.end():]):
        failures.append('Springer Nature sn-jnl front matter places \\abstract{...} after \\maketitle')
    failures.extend(springer_nature_placeholder_front_matter_failures(visible))
    return failures


def pdf_first_page_text(pdf_path: Path) -> dict[str, object]:
    if not pdf_path.exists() or not pdf_path.is_file():
        return {'available': False, 'text': '', 'lines': [], 'error': 'missing_pdf'}
    try:
        proc = subprocess.run(
            ['pdftotext', '-f', '1', '-l', '1', '-layout', str(pdf_path), '-'],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception as exc:
        return {'available': False, 'text': '', 'lines': [], 'error': str(exc)}
    if proc.returncode != 0:
        return {'available': False, 'text': '', 'lines': [], 'error': proc.stderr.strip() or f'pdftotext exited {proc.returncode}'}
    lines = [re.sub(r'\s+', ' ', line).strip() for line in (proc.stdout or '').splitlines() if line.strip()]
    return {'available': True, 'text': re.sub(r'\s+', ' ', proc.stdout or '').strip(), 'lines': lines, 'error': ''}


def springer_nature_pdf_front_matter_failures(pdf_path: Path, tex_text: str, venue: str, project: str = '') -> tuple[list[str], dict[str, object]]:
    if not _is_springer_nature_family(venue, project=project, text=tex_text):
        return [], {'skipped': True}
    first_page = pdf_first_page_text(pdf_path)
    failures: list[str] = []
    if not first_page.get('available'):
        return ['Springer Nature PDF first-page text could not be extracted for front-matter audit: ' + str(first_page.get('error') or 'unknown error')], first_page
    text_plain = str(first_page.get('text') or '')
    text_norm = re.sub(r'[^a-z0-9]+', ' ', text_plain.lower()).strip()
    lines = first_page.get('lines') if isinstance(first_page.get('lines'), list) else []
    top_text = ' '.join(str(item) for item in lines[:6])
    top_norm = re.sub(r'[^a-z0-9]+', ' ', top_text.lower()).strip()
    title = latex_plain_text(_extract_latex_macro(tex_text, 'title'))
    title_words = [word for word in re.findall(r'[a-z0-9]+', title.lower()) if len(word) > 3]
    first_line = str(lines[0]) if lines else ''
    first_token_match = re.search(r'[A-Za-z]+', first_line)
    first_token = first_token_match.group(0).lower() if first_token_match else ''
    if title_words:
        covered_total = sum(1 for word in title_words[:8] if word in text_norm)
        covered_top = sum(1 for word in title_words[:8] if word in top_norm)
        needed = min(3, len(title_words[:8]))
        first_title_word = title_words[0]
        if covered_total < needed or covered_top < needed or (first_token and first_token != first_title_word):
            failures.append('Springer Nature PDF first page does not show the manuscript title in the front-matter area')
    abstract = _extract_latex_macro(tex_text, 'abstract')
    if not abstract:
        env_match = re.search(r'\\begin\{abstract\}(.*?)\\end\{abstract\}', tex_text, flags=re.DOTALL)
        abstract = env_match.group(1).strip() if env_match else ''
    abstract = latex_plain_text(abstract)
    abstract_words = [word for word in re.findall(r'[a-z]+', abstract.lower()) if len(word) > 4]
    if first_line and abstract_words:
        all_abstract_words = [word for word in re.findall(r'[a-z]+', abstract.lower()) if len(word) > 3]
        cropped_words = {word[1:] for word in all_abstract_words if len(word) > 5}
        if first_token in cropped_words:
            failures.append('Springer Nature PDF first visible word appears to be a cropped abstract word; front matter is malformed')
    if re.search(r'\bAnonymous Authors\s*\d+\*', top_text):
        failures.append('Springer Nature PDF first page renders anonymous author with a corresponding-author footnote/affiliation marker')
    top_placeholder = re.sub(r'[^a-z0-9]+', ' ', top_text.lower()).strip()
    if any(phrase in top_placeholder for phrase in ['department institution', 'institution city country', 'anonymous institution', 'city country']):
        failures.append('Springer Nature PDF first page exposes placeholder affiliation text')
    return failures, first_page


def validate_venue_template_format(text: str, venue: str, project: str = '') -> dict[str, object]:
    profile = venue_template_profile(venue, project=project)
    parsed = latex_documentclass(text)
    doc_class = str(parsed.get('class') or '')
    options = [str(item) for item in parsed.get('options', [])]
    required_class = str(profile.get('documentclass') or '')
    required_options = [str(item) for item in profile.get('required_options', [])]
    recommended_options = [str(item) for item in profile.get('recommended_options', [])]
    forbidden_classes = [str(item) for item in profile.get('forbidden_documentclasses', [])]
    normalized_options = {item.split('=', 1)[0].strip(): item for item in options}
    # If the venue submission policy explicitly requires a document class,
    # that takes priority over the generic forbidden list.
    sub_policy = profile.get('submission_policy') or {}
    policy_required_class = str(sub_policy.get('required_documentclass') or '') if isinstance(sub_policy, dict) else ''
    policy_requires_article = bool(policy_required_class == 'article')

    def option_present(name: str) -> bool:
        return name in options or name in normalized_options

    failures: list[str] = []
    warnings: list[str] = []
    if profile.get('family') == 'unresolved':
        failures.append('venue requirements unresolved; run venue-intelligence and official template fetch before validating paper format')
    if venue and not parsed.get('exists'):
        failures.append('missing \\documentclass')
    if required_class and doc_class != required_class:
        failures.append(f'documentclass must be {required_class}, found {doc_class or "none"}')
    if doc_class in forbidden_classes and not (policy_requires_article and doc_class == 'article'):
        failures.append(f'forbidden generic/wrong documentclass: {doc_class}')
    missing_options = [item for item in required_options if not option_present(item)]
    if missing_options:
        failures.append('missing required documentclass options: ' + ', '.join(missing_options))
    missing_recommended = [item for item in recommended_options if not option_present(item)]
    if missing_recommended:
        warnings.append('missing recommended review options: ' + ', '.join(missing_recommended))
    if profile.get('family') == 'generic' and doc_class == 'article' and not policy_requires_article:
        failures.append('generic article template is not an acceptable venue-specific submission template')
    lowered = text.lower()
    required_markers = [str(item) for item in profile.get('required_markers', []) if str(item).strip()]
    if profile.get('validate_required_markers_in_tex'):
        for marker in required_markers:
            if marker not in text:
                failures.append('venue template required marker missing: ' + marker)
    forbidden_markers = [str(item) for item in profile.get('forbidden_markers', []) if str(item).strip()]
    if not forbidden_markers:
        forbidden_markers = ['minimal compile fallback', 'compile-capable fallback', 'generated venue-aware fallback']
    for marker in forbidden_markers:
        if marker.lower() in lowered:
            failures.append('venue fallback/template-forbidden marker detected: ' + marker)
    if profile.get('family') == 'iclr':
        if doc_class != 'article':
            failures.append(f'resolved venue template must use article documentclass, found {doc_class or "none"}')
        required_style_stems = [
            Path(item).stem for item in profile.get('required_files', [])
            if str(item).lower().startswith('iclr') and str(item).lower().endswith('.sty')
        ]
        if required_style_stems:
            if not any(stem in text for stem in required_style_stems):
                failures.append('resolved venue template must load the official conference style: ' + ', '.join(required_style_stems))
        elif not re.search(r'iclr20\d{2}_conference', text):
            failures.append('resolved venue template must load the official year-specific conference style')
    if profile.get('family') == 'springer-nature':
        if doc_class != 'sn-jnl':
            failures.append(f'resolved Springer Nature template must use sn-jnl documentclass, found {doc_class or "none"}')
        if 'sn-nature' not in options and 'sn-nature' not in text:
            failures.append('Springer Nature template must expose the Nature Portfolio sn-nature option')
        failures.extend(springer_nature_front_matter_failures(text, venue, project=project))
        if 'sn-jnl.cls' not in text and 'Style for submissions to Nature Portfolio journals' not in text:
            warnings.append('Springer Nature template validation did not see the expected sn-jnl sidecar marker in template text')
    return {
        'status': 'pass' if not failures else 'block',
        'venue': venue,
        'profile': profile,
        'documentclass': parsed,
        'failures': failures,
        'warnings': warnings,
    }


def venue_fallback_template(title: str, venue: str, project: str = '') -> str:
    escaped = escape_latex(title).replace('\n', ' ')
    profile = venue_template_profile(venue, project=project)
    if profile.get('family') == 'acm-sigconf':
        return rf"""\documentclass[sigconf,natbib=true,anonymous=true]{{acmart}}
\settopmatter{{printacmref=false}}
\renewcommand\footnotetextcopyrightpermission[1]{{}}
\acmYear{{2026}}
\copyrightyear{{2026}}
\begin{{document}}
\title{{{escaped}}}
\author{{Anonymous Authors}}
\maketitle
\begin{{abstract}}
TODO: Abstract.
\end{{abstract}}
\section{{Introduction}}
TODO: Introduction.
\section{{Related Work}}
TODO: Related Work.
\section{{Method}}
TODO: Method.
\section{{Experiments}}
TODO: Experiments.
\section{{Conclusion}}
TODO: Conclusion.
\bibliographystyle{{ACM-Reference-Format}}
\bibliography{{refs}}
\end{{document}}
"""
    return rf"""% generated venue-aware fallback; not acceptable as a venue-compliant writing template
\documentclass{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage{{amsmath,amssymb,booktabs,graphicx,hyperref,natbib}}
\usepackage[margin=1in]{{geometry}}
\title{{{escaped}}}
\author{{Anonymous Authors}}
\date{{}}
\begin{{document}}
\maketitle
\begin{{abstract}}
TODO: Abstract.
\end{{abstract}}
\section{{Introduction}}
TODO: Introduction.
\section{{Related Work}}
TODO: Related Work.
\section{{Method}}
TODO: Method.
\section{{Experiments}}
TODO: Experiments.
\section{{Conclusion}}
TODO: Conclusion.
\bibliographystyle{{plainnat}}
\bibliography{{refs}}
\end{{document}}
"""


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8', errors='ignore')


def search_duckduckgo(query: str) -> list[str]:
    encoded = urllib.parse.urlencode({'q': query})
    html = fetch_url(f'https://html.duckduckgo.com/html/?{encoded}')
    matches = re.findall(r'nofollow" class="result__a" href="(.*?)"', html)
    if not matches:
        matches = re.findall(r'result__url[^>]*>(.*?)<', html)
    urls = []
    for match in matches:
        url = unescape(match)
        if url.startswith('//'):
            url = 'https:' + url
        if url.startswith('http'):
            urls.append(url)
    return urls


def filter_candidate_urls(urls: Iterable[str], domains: list[str]) -> list[str]:
    filtered: list[str] = []
    for url in urls:
        host = urllib.parse.urlparse(url).netloc.lower()
        if domains:
            if any(domain in host for domain in domains):
                filtered.append(url)
        else:
            filtered.append(url)
    seen = set()
    deduped = []
    for url in filtered:
        if url in seen:
            continue
        deduped.append(url)
        seen.add(url)
    return deduped


def find_download_links(page_url: str, html: str) -> list[str]:
    links = re.findall(r'href=["\']([^"\']+)["\']', html)
    candidates = []
    for link in links:
        absolute = urllib.parse.urljoin(page_url, unescape(link))
        lowered = absolute.lower()
        if any(lowered.endswith(ext) for ext in ['.zip', '.tar.gz', '.tgz', '.tar']) or any(token in lowered for token in ['author-kit', 'style', 'template', 'latex']):
            candidates.append(absolute)
    return candidates


def download_binary(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())
    return destination


def unpack_archive(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    lower = archive_path.name.lower()
    if lower.endswith('.zip'):
        with zipfile.ZipFile(archive_path, 'r') as handle:
            handle.extractall(destination)
    elif lower.endswith('.tar.gz') or lower.endswith('.tgz') or lower.endswith('.tar'):
        with tarfile.open(archive_path, 'r:*') as handle:
            handle.extractall(destination)
    else:
        raise ValueError(f'Unsupported archive type: {archive_path.name}')
    return destination


def find_main_tex(source_dir: Path) -> Path | None:
    preferred_names = ['main.tex', 'template.tex', 'paper.tex', 'submission.tex']
    tex_files = sorted(source_dir.rglob('*.tex'))
    for name in preferred_names:
        for path in tex_files:
            if path.name.lower() == name:
                return path
    scored: list[tuple[int, Path]] = []
    for path in tex_files:
        text = read_text(path)
        score = 0
        if '\\documentclass' in text:
            score += 5
        if '\\begin{document}' in text:
            score += 5
        if '\\title{' in text:
            score += 2
        if '\\maketitle' in text:
            score += 2
        if score:
            scored.append((score, path))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], len(str(item[1]))))
    return scored[0][1]


TEXLIVE_YEAR_HINTS = ('2026', '2025', '2024')


def texlive_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ('TEXLIVE_ROOT', 'TEXLIVE_ROOT'):
        value = os.environ.get(env_name, '').strip()
        if value:
            candidates.append(Path(value).expanduser())
    candidates.extend(
        [
            ROOT / '.runtime' / 'texlive',
            ROOT.parent / 'texlive',
            Path.home() / 'workspace' / 'texlive',
            Path.home() / 'texlive',
            Path('/opt/texlive'),
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key and key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def texlive_tool_candidates(name: str) -> list[Path]:
    out: list[Path] = []
    for root in texlive_root_candidates():
        out.append(root / 'bin' / 'x86_64-linux' / name)
        for year in TEXLIVE_YEAR_HINTS:
            out.append(root / year / 'bin' / 'x86_64-linux' / name)
        if root.exists():
            try:
                years = sorted((p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r'20\d{2}', p.name)), reverse=True)
            except OSError:
                years = []
            for year in years:
                out.append(root / year / 'bin' / 'x86_64-linux' / name)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in out:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def workspace_tool_path(name: str) -> str:
    for candidate in texlive_tool_candidates(name):
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return ''


def compiler_inventory() -> dict[str, bool]:
    return {
        'latexmk': shutil.which('latexmk') is not None or bool(workspace_tool_path('latexmk')),
        'pdflatex': shutil.which('pdflatex') is not None or bool(workspace_tool_path('pdflatex')),
        'xelatex': shutil.which('xelatex') is not None or bool(workspace_tool_path('xelatex')),
        'lualatex': shutil.which('lualatex') is not None or bool(workspace_tool_path('lualatex')),
        'biber': shutil.which('biber') is not None or bool(workspace_tool_path('biber')),
        'tectonic': shutil.which('tectonic') is not None,
    }


def choose_compiler() -> str:
    inventory = compiler_inventory()
    if inventory['latexmk']:
        return shutil.which('latexmk') or workspace_tool_path('latexmk') or 'latexmk'
    if inventory['tectonic']:
        return 'tectonic'
    if inventory['pdflatex']:
        return shutil.which('pdflatex') or workspace_tool_path('pdflatex') or 'pdflatex'
    return ''


def install_latex_hint() -> list[str]:
    hints = []
    if shutil.which('conda'):
        hints.append('conda install -n base -c conda-forge latexmk tectonic')
        hints.append('conda install -n base -c conda-forge texlive-core biber')
    if shutil.which('apt-get'):
        hints.append('sudo apt-get update && sudo apt-get install -y latexmk texlive-latex-extra texlive-fonts-recommended biber')
    if shutil.which('brew'):
        hints.append('brew install --cask mactex-no-gui')
    return hints


VENUE_SCOPED_PIPELINE_KEYS = {
    'paper_orchestra_workspace', 'paper_orchestra_report', 'paper_orchestra_final_pdf', 'paper_orchestra_final_tex',
    'paper_orchestra_pdf_generated', 'paper_orchestra_tex_generated', 'paper_orchestra_bridge_status',
    'paper_orchestra_bridge_error', 'paper_orchestra_bridge_stdout_tail', 'paper_orchestra_bridge_stderr_tail',
    'conference_preview_ready', 'conference_preview_pdf', 'conference_preview_tex', 'conference_preview_pages',
    'conference_preview_body_pages', 'conference_preview_body_page_limit', 'conference_preview_reference_pages',
    'conference_preview_blocker_summary', 'conference_preview_blockers', 'blocked_preview_pdf', 'blocked_preview_tex',
    'blocked_pdf_path', 'blocked_tex_path', 'latest_generated_pdf_path', 'latest_preview_pdf', 'latest_preview_tex',
    'rendered_tex', 'pdf_path', 'pdf_ready', 'paper_venue_format_status', 'paper_venue_format_validation',
    'venue_template_format_ready', 'venue_template_validation', 'venue_template_profile', 'venue_submission_policy',
    'venue_submission_policy_status', 'paper_normality_status', 'paper_normality_pages', 'paper_normality_body_pages',
    'paper_normality_estimated_reference_pages', 'paper_figure_quality_status', 'paper_layout_footprint_warnings',
    'paper_public_diagnostics', 'paper_reference_quality_target', 'paper_reference_official_min',
    'paper_normality_citation_count', 'paper_normality_citation_target', 'paper_normality_reference_target_source',
    'paper_self_review_status', 'paper_self_review_ready', 'paper_self_review_receipt', 'paper_self_review_blockers',
    'paper_self_review_evidence_blockers', 'paper_self_review_evidence_blocker_count',
    'paper_self_review_preview_only_ready', 'paper_self_review_submission_evidence_ready',
    'paper_self_review_independent_findings_count', 'paper_self_review_repairs_count',
}


def _cletop_level_venue_scoped_state(state: dict) -> None:
    for key in VENUE_SCOPED_PIPELINE_KEYS:
        state.pop(key, None)


def update_pipeline_state(project: str, update: dict, venue: str = '', promote_to_top: bool = True) -> dict:
    paper = ensure_paper_dirs(project)
    state = load_json(paper['pipeline_state'], {})
    if not isinstance(state, dict):
        state = {}
    venues = state.get('venues', {})
    if not isinstance(venues, dict):
        venues = {}
    if venue:
        slug = slugify(venue)
        prior_active = str(state.get('active_venue') or '')
        current = venues.get(slug, {})
        if not isinstance(current, dict):
            current = {}
        current.update(update)
        current['venue'] = venue
        current['venue_slug'] = slug
        venues[slug] = current
        state['venues'] = venues
        state['active_venue'] = slug
        if promote_to_top:
            if prior_active and prior_active != slug:
                _cletop_level_venue_scoped_state(state)
            top = dict(current)
            top.pop('venue', None)
            top.pop('venue_slug', None)
            state.update(top)
            state['venue'] = venue
            state['venue_slug'] = slug
    else:
        state.update(update)
    write_json(paper['pipeline_state'], state)
    return state


def _configured_project_venue(project: str) -> str:
    try:
        cfg = load_project_config(project)
    except Exception:
        return ''
    if not isinstance(cfg, dict):
        return ''
    paper_cfg = cfg.get('paper', {}) if isinstance(cfg.get('paper'), dict) else {}
    return str(cfg.get('target_venue') or cfg.get('venue') or paper_cfg.get('target_venue') or '').strip()


def get_active_paper_state(project: str, venue: str = '') -> dict:
    paper = ensure_paper_dirs(project)
    state = load_json(paper['pipeline_state'], {})
    if not isinstance(state, dict):
        return {}
    venues = state.get('venues', {}) if isinstance(state.get('venues', {}), dict) else {}
    requested_venue = str(venue or _configured_project_venue(project) or '').strip()
    slug = slugify(requested_venue) if requested_venue else str(state.get('active_venue', '') or '')
    venue_state = venues.get(slug, {}) if slug else {}
    if slug and isinstance(venue_state, dict):
        merged = {
            k: v
            for k, v in state.items()
            if k not in VENUE_SCOPED_PIPELINE_KEYS and k not in {'venue', 'venue_slug', 'target_venue'}
        }
        if venue_state:
            merged.update(venue_state)
        merged['active_venue'] = slug
        merged['venue_slug'] = str(venue_state.get('venue_slug') or slug)
        merged['venue'] = str(venue_state.get('venue') or requested_venue or '')
        merged['target_venue'] = str(venue_state.get('target_venue') or requested_venue or merged.get('venue') or '')
        return merged
    return dict(state)


def make_failure_report(path: Path, title: str, bullets: list[str], recovery: list[str]) -> None:
    lines = [f'# {title}\n\n']
    for item in bullets:
        lines.append(f'- {item}\n')
    if recovery:
        lines.append('\n## Recovery Options\n\n')
        for item in recovery:
            lines.append(f'- {item}\n')
    write_text(path, ''.join(lines))


def try_install_latex_toolchain() -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    if shutil.which('conda'):
        commands = [
            ['conda', 'install', '-y', '-n', 'base', '-c', 'conda-forge', 'tectonic'],
            ['conda', 'install', '-y', '-n', 'base', '-c', 'conda-forge', 'latexmk', 'texlive-core', 'biber'],
        ]
        for cmd in commands:
            proc = subprocess.run(cmd, text=True, capture_output=True)
            attempts.append({'command': ' '.join(cmd), 'return_code': proc.returncode, 'stdout_tail': proc.stdout[-5000:], 'stderr_tail': proc.stderr[-5000:]})
            if proc.returncode == 0 and choose_compiler():
                return {'success': True, 'attempts': attempts}
    return {'success': False, 'attempts': attempts}
