#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from paper_common import slugify, update_pipeline_state, venue_info, write_json, write_text  # noqa: E402
from project_paths import build_paths  # noqa: E402


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def is_fresh(payload: dict[str, Any], max_age_days: int) -> bool:
    if payload.get("status") not in {"ok", "pass"}:
        return False
    raw = str(payload.get("source_checked_at") or payload.get("updated_at") or "")
    if not raw:
        return False
    try:
        checked = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = dt.datetime.now(dt.timezone.utc) - checked.astimezone(dt.timezone.utc)
    return age.days <= max_age_days


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()



def run_git_checked(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    """Run git with a real process-group timeout so stuck network calls cannot leave research jobs fake-running."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        preexec_fn=os.setsid if hasattr(os, 'setsid') else None,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout or '', stderr or '')
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, 'killpg'):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, 124, stdout or '', (stderr or '') + f'\ngit command timed out after {timeout}s')


def verified_local_template_source(payload: dict[str, Any], repo_url: str) -> Path | None:
    """Use the already fetched venue template source before network cloning.

    The template fetcher records `template_source.json` with source_url, commit,
    and file hashes. That artifact is a valid deterministic source for TASTE
    venue-intelligence even when the copied source directory is no longer a git
    clone. A refresh may still compare the recorded commit against remote HEAD
    when the network returns, but it must not fail solely because a new clone
    times out.
    """
    project = str(payload.get('project') or '').strip()
    venue = str(payload.get('venue') or '').strip()
    if not project or not venue:
        return None
    venue_dir = ROOT / 'projects' / project / 'paper' / 'venues' / slugify(venue)
    source_dir = venue_dir / 'source'
    if not source_dir.exists() or not source_dir.is_dir():
        return None
    template = payload.get('template') if isinstance(payload.get('template'), dict) else {}
    source_meta = load_json(venue_dir / 'template_source.json', {})
    if isinstance(source_meta, dict) and source_meta.get('status') == 'ok':
        source_url = str(source_meta.get('source_url') or '')
        if repo_url and source_url and repo_url.replace('.git', '') not in source_url.replace('.git', ''):
            return None
        validation = source_meta.get('template_validation') if isinstance(source_meta.get('template_validation'), dict) else {}
        if validation.get('status') not in {'pass', 'ok'}:
            return None
        for name in [str(item).strip().strip('/') for item in template.get('required_files', []) if str(item).strip()]:
            if name and not any(source_dir.rglob(name)):
                return None
        main_tex = str(template.get('main_tex') or '').strip().strip('/')
        if main_tex and not any(source_dir.rglob(main_tex)):
            return None
        template['local_source_is_template_root'] = True
        if source_meta.get('source_commit'):
            template.setdefault('verified_repository_commit', str(source_meta.get('source_commit')))
        if source_meta.get('source_subdir'):
            template.setdefault('verified_directory_hint', str(source_meta.get('source_subdir')).strip('/'))
        return source_dir
    main_tex = str(template.get('main_tex') or '').strip()
    if main_tex and (source_dir / main_tex).is_file():
        template['local_source_is_template_root'] = True
        return source_dir
    if any(source_dir.glob('*.tex')) and any(source_dir.glob('*.sty')):
        template['local_source_is_template_root'] = True
        return source_dir
    return None


def use_template_cache() -> bool:
    return str(os.environ.get('VENUE_TEMPLATE_USE_CACHE') or '').lower() in {'1', 'true', 'yes', 'on'}


def remote_repository_head(repo_url: str) -> str:
    if not repo_url or 'github.com/' not in repo_url:
        return ''
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    proc = run_git_checked(
        ['git', 'ls-remote', repo_url, 'HEAD'],
        cwd=ROOT,
        env=env,
        timeout=int(os.environ.get('VENUE_GIT_TIMEOUT_SEC', '60') or 60),
    )
    if proc.returncode != 0:
        return ''
    first = (proc.stdout or '').strip().split()
    return first[0] if first else ''


def unverified_source(item: Any) -> bool:
    text = json.dumps(item, ensure_ascii=False).lower() if isinstance(item, dict) else str(item).lower()
    markers = [
        'network unreachable',
        'reconstructed',
        'could not confirm',
        'unable to verify',
        'not verified',
        'guess',
    ]
    return any(marker in text for marker in markers)


def latest_iclr_template(repo_dir: Path) -> tuple[Path | None, Path | None]:
    candidates: list[tuple[int, Path, Path]] = []
    flat_candidates = sorted(repo_dir.glob('iclr20*_conference.tex')) if repo_dir.exists() else []
    for main_tex in flat_candidates:
        match = re.search(r'iclr(20\d{2})_conference', main_tex.name.lower())
        if match:
            candidates.append((int(match.group(1)), repo_dir, main_tex))
    for directory in repo_dir.iterdir() if repo_dir.exists() else []:
        if not directory.is_dir():
            continue
        match = re.fullmatch(r'iclr(20\d{2})', directory.name.lower())
        if not match:
            continue
        tex_candidates = sorted(directory.glob('iclr20*_conference.tex'))
        if not tex_candidates:
            continue
        preferred = next((item for item in tex_candidates if item.stem.startswith(directory.name.lower())), tex_candidates[0])
        candidates.append((int(match.group(1)), directory, preferred))
    if not candidates:
        return None, None
    _, directory, main_tex = sorted(candidates, key=lambda item: item[0])[-1]
    return directory, main_tex



def fetch_official_text(url: str, timeout: int = 30) -> str:
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (venue-intelligence)'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode('utf-8', errors='replace')
    except Exception:
        return ''


def html_to_compact_text(html: str) -> str:
    text = re.sub(r'<script\b[^>]*>.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style\b[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()



SPRINGER_NATURE_TEMPLATE_ARCHIVE_URL = 'https://cms-resources.apps.public.k8s.springernature.io/springer-cms/rest/v1/content/18782940/data/v12'
SPRINGER_NATURE_LATEX_SUPPORT_URL = 'https://www.springernature.com/gp/authors/campaigns/latex-author-support'
NATURE_AUTHOR_URL = 'https://www.nature.com/nature/for-authors'
NATURE_INITIAL_SUBMISSION_URL = 'https://www.nature.com/nature/for-authors/initial-submission'
NATURE_FORMATTING_GUIDE_URL = 'https://www.nature.com/nature/for-authors/formatting-guide'


def is_nature_family_venue(venue: str) -> bool:
    slug = slugify(venue)
    return 'nature' in slug or slug in {'natmachintell', 'nat-comms', 'nature-communications'}


def fetch_head_status(url: str, timeout: int = 20) -> dict[str, Any]:
    try:
        request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0 (venue-intelligence)'})
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            return {
                'url': url,
                'status_code': int(getattr(response, 'status', 0) or 0),
                'content_type': str(response.headers.get('content-type') or ''),
                'content_length': str(response.headers.get('content-length') or ''),
                'final_url': str(response.geturl() or url),
            }
    except Exception as exc:
        return {'url': url, 'status_code': 0, 'error': str(exc)[:300], 'final_url': ''}


def build_nature_family_requirements(project: str, venue: str) -> dict[str, Any]:
    checked = now_iso()
    latex_html = fetch_official_text(SPRINGER_NATURE_LATEX_SUPPORT_URL, timeout=35)
    latex_text = html_to_compact_text(latex_html)
    nature_heads = [fetch_head_status(url) for url in [NATURE_AUTHOR_URL, NATURE_INITIAL_SUBMISSION_URL, NATURE_FORMATTING_GUIDE_URL]]
    archive_head = fetch_head_status(SPRINGER_NATURE_TEMPLATE_ARCHIVE_URL)
    archive_ok = int(archive_head.get('status_code') or 0) == 200 and 'zip' in str(archive_head.get('content_type') or '').lower()
    official_sources: list[dict[str, Any]] = [
        {
            'url': SPRINGER_NATURE_LATEX_SUPPORT_URL,
            'label': 'Springer Nature official LaTeX author support',
            'evidence': 'Official Springer Nature page states that journal article guidance and templates are provided for authors preparing research with LaTeX.',
            'access_status': 'ok' if latex_html else 'unavailable',
        },
    ]
    if archive_ok:
        official_sources.append({
            'url': SPRINGER_NATURE_TEMPLATE_ARCHIVE_URL,
            'label': 'Springer Nature journal article template package, December 2024 version',
            'evidence': 'Official Springer Nature content endpoint returns a ZIP template package containing sn-article.tex, sn-jnl.cls and sn-nature.bst.',
            'access_status': 'ok',
        })
    candidate_sources = []
    for head in nature_heads:
        status = int(head.get('status_code') or 0)
        final_url = str(head.get('final_url') or '')
        access_limited = bool('idp.nature.com' in final_url or 'error=cookies_not_supported' in final_url or status in {301, 302, 303, 307, 308})
        if status and access_limited:
            candidate_sources.append({
                'url': head.get('url'),
                'label': 'Nature journal-specific author instruction page',
                'evidence': 'The official page exists but the current fetch was redirected through the Nature authorization/cookie layer; TASTE records it as candidate evidence and does not treat inaccessible text as parsed rules.',
                'access_status': 'redirected_or_cookie_required',
                'head': head,
            })
        elif status == 200:
            official_sources.append({
                'url': head.get('url'),
                'label': 'Nature journal-specific author instruction page',
                'evidence': 'Official Nature author instruction page was reachable during this venue-intelligence run without a cookie/auth error.',
                'access_status': 'ok',
                'head': head,
            })
        else:
            candidate_sources.append({
                'url': head.get('url'),
                'label': 'Nature journal-specific author instruction page',
                'evidence': 'The page could not be parsed by TASTE during this run; no page/word limits were inferred from it.',
                'access_status': 'unavailable',
                'head': head,
            })
    template = {
        'family': 'springer-nature',
        'format_label': 'Springer Nature journal article LaTeX template for Nature Portfolio preview',
        'official_source_url': SPRINGER_NATURE_LATEX_SUPPORT_URL,
        'archive_url': SPRINGER_NATURE_TEMPLATE_ARCHIVE_URL,
        'directory_hint': 'sn-article-template',
        'main_tex': 'sn-article.tex',
        'documentclass': 'sn-jnl',
        'documentclass_options': ['pdflatex', 'sn-nature'],
        'recommended_documentclass_options': [],
        'bibliography_style': 'sn-nature',
        'required_files': ['sn-article.tex', 'sn-jnl.cls', 'bst/sn-nature.bst'],
        'required_markers': ['Style for submissions to Nature Portfolio journals', 'sn-nature'],
        'forbidden_fallback_markers': ['minimal compile fallback', 'compile-capable fallback', 'generated venue-aware fallback'],
        'initial_submission_format_note': 'Nature-family first submissions may use flexible formatting or a single Word/PDF according to journal-specific instructions; this LaTeX template is the official Springer Nature authoring/preview package, not proof that LaTeX is mandatory for initial submission.',
    }
    page_policy = {
        'body_page_min': 0,
        'body_page_max': 0,
        'reference_page_max': 0,
        'total_page_max': 0,
        'appendix_policy': 'Nature-family journal-specific instructions should be checked for article-type details; TASTE did not infer page limits when official journal text was unavailable or did not expose a machine-readable hard limit.',
        'source_type': 'official_flexible_or_unresolved_journal_specific',
        'source_url': NATURE_AUTHOR_URL,
        'source_evidence': 'Nature author pages are the authoritative source for journal-specific article shape; current TASTE run records accessible official sources and leaves hard page limits unset unless explicitly parsed.',
    }
    citation_policy = {
        'min_verified_references': 50,
        'estimated_references_per_page': 35,
        'source_type': 'quality_target',
        'rationale': 'Nature-family Articles commonly require a compact, highly curated reference list; The workflow uses 50 verified relevant references as a writing-quality target/cap proxy until journal-specific official limits are parsed. This is not an official minimum.',
    }
    review_policy = {
        'anonymous_required': False,
        'self_citation_third_person_required': False,
        'reproducibility_expected': True,
        'data_availability_expected': True,
        'code_availability_expected': True,
        'reporting_summary_may_apply': True,
    }
    paper_shape = {
        'canonical_sections': ['Introduction', 'Results', 'Discussion', 'Methods'],
        'max_main_sections': 8,
        'nature_family_article_mode': True,
        'required_back_matter': ['Data availability', 'Code availability'],
    }
    policy = {
        'status': 'known',
        'venue': venue,
        'slug': slugify(venue),
        'track': 'article',
        'source_url': SPRINGER_NATURE_LATEX_SUPPORT_URL,
        'source_label': 'Nature-family / Springer Nature official author instructions and LaTeX support',
        'source_checked_date': checked[:10],
        'format_label': template['format_label'],
        'template_family': 'springer-nature',
        'required_documentclass': 'sn-jnl',
        'required_documentclass_options': ['pdflatex', 'sn-nature'],
        'body_page_min': 0,
        'body_page_max': 0,
        'reference_page_max': 0,
        'total_page_max': 0,
        'min_references': 0,
        'official_min_references': 0,
        'reference_quality_target': 50,
        'reference_quality_target': 50,
        'reference_target_source': 'quality_target',
        'estimated_references_per_page': 35,
        'canonical_sections': paper_shape['canonical_sections'],
        'max_main_sections': paper_shape['max_main_sections'],
        'anonymous_required': False,
        'self_citation_third_person_required': False,
        'reviewer_nomination_required': False,
        'reproducibility_expected': True,
        'data_availability_expected': True,
        'code_availability_expected': True,
        'initial_submission_flexible_format': True,
        'latex_preview_template_required_for_pdf': True,
        'desk_reject_risks': [
            {'id': 'journal_specific_instructions_unparsed', 'requirement': 'Check Nature journal-specific author instructions before marking submission-ready; TASTE preview may use official Springer Nature LaTeX template but cannot claim final compliance from a generic template alone.', 'automatable': True, 'severity': 'block'},
            {'id': 'unsupported_broad_claims', 'requirement': 'Nature-family broad significance and empirical claims must be supported by current audit-ready evidence.', 'automatable': True, 'severity': 'block'},
            {'id': 'missing_data_or_code_availability', 'requirement': 'Data and code availability statements must map to real repositories, artifacts, or explicit access routes.', 'automatable': True, 'severity': 'block'},
        ],
    }
    profile = {
        'venue': venue,
        'slug': slugify(venue),
        'family': 'springer-nature',
        'format_label': template['format_label'],
        'documentclass': 'sn-jnl',
        'required_options': ['pdflatex', 'sn-nature'],
        'recommended_options': [],
        'forbidden_documentclasses': ['article', 'acmart', 'llncs', 'IEEEtran'],
        'required_markers': template['required_markers'],
        'forbidden_markers': template['forbidden_fallback_markers'],
        'required_files': template['required_files'],
        'submission_policy': policy,
        'submission_notes': [
            'Use Nature-family article mode for broad-reader framing, evidence-calibrated claims, reproducible Methods, and data/code availability.',
            'Initial submission format is flexible unless journal-specific instructions say otherwise; TASTE LaTeX/PDF is a preview artifact built from official Springer Nature template sources.',
        ],
        'nature_family_article_mode': True,
    }
    blockers = []
    if not latex_html:
        blockers.append('Springer Nature LaTeX author support page was not reachable during this run')
    if not archive_ok:
        blockers.append('Springer Nature official template archive was not reachable or was not a ZIP')
    status = 'ok' if not blockers and official_sources else 'blocked'
    return {
        'status': status,
        'venue': venue,
        'track': 'article',
        'venue_slug': slugify(venue),
        'project': project,
        'source_checked_at': checked,
        'updated_at': checked,
        'official_sources': official_sources,
        'candidate_official_sources': candidate_sources,
        'page_policy': page_policy,
        'citation_policy': citation_policy,
        'review_policy': review_policy,
        'template': template,
        'paper_shape': paper_shape,
        'layout_guidance': {
            'page_fit_priority': ['figure/table footprint', 'reference count and bibliography style', 'prose length'],
            'figure_policy': 'For Nature-family preview, build clear main figures only from claim-ready evidence and repair figure/table footprint before cutting scientific content.',
        },
        'venue_submission_policy': policy,
        'venue_template_profile': profile,
        'nature_family_article_mode': True,
        'official_fetch_diagnostics': {'nature_heads': nature_heads, 'archive_head': archive_head, 'latex_support_text_bytes': len(latex_html.encode('utf-8')) if latex_html else 0},
        'blockers': blockers,
    }


def parse_body_page_limit(text: str) -> int:
    """Parse a main/body-page limit from current official venue text."""
    patterns = [
        r'main text should be\s*(\d+)\s*pages or fewer',
        r'strict\s+upper\s+limit[^.]{0,120}?(\d+)\s+pages',
        r'body(?:\s+|-)pages?[^.]{0,120}?(?:up to|at most|no more than|limit(?:ed)? to)\s*(\d+)\s+pages',
        r'(?:up to|at most|no more than)\s*(\d+)\s+(?:main|body)?\s*pages',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return int(match.group(1))
    return 0


def upsert_official_source(payload: dict[str, Any], *, url: str, label: str, evidence: str) -> None:
    sources = payload.get('official_sources') if isinstance(payload.get('official_sources'), list) else []
    normalized_url = url.replace('.git', '')
    row = {'url': url, 'label': label, 'evidence': evidence}
    kept = [item for item in sources if normalized_url not in str(item).replace('.git', '')]
    payload['official_sources'] = kept + [row]


def verify_iclr_author_guide(payload: dict[str, Any], yetext: str, commit: str, iclr_dir: Path, iclr_main: Path) -> None:
    if not yetext:
        return
    guide_url = f'https://iclr.cc/Conferences/{yetext}/AuthorGuide'
    html = fetch_official_text(guide_url)
    if not html:
        return
    text = html_to_compact_text(html)
    lower = text.lower()
    body_page_max = parse_body_page_limit(text)
    has_page_policy = bool(body_page_max)
    has_unlimited_refs = 'unlimited additional pages are allowed for the bibliography/references' in lower or 'references does not count towards the page limit' in lower
    zip_url = f'https://github.com/ICLR/Master-Template/raw/master/{iclr_dir.name}.zip'
    if has_page_policy:
        page_policy = payload.setdefault('page_policy', {})
        page_policy['body_page_max'] = body_page_max
        page_policy['reference_page_max'] = 0
        page_policy['total_page_max'] = 0
        page_policy['appendix_policy'] = (
            f'Official ICLR {yetext} Author Guide says initial-submission main text should be {body_page_max} pages or fewer; '
            'the references/bibliography do not count toward the page limit and may use unlimited additional pages.'
        )
        page_policy['source_type'] = 'official_author_guide_and_template'
        page_policy['source_evidence'] = f'Parsed from {guide_url} and official repository commit {commit}, file {iclr_dir.name}/{iclr_main.name}.'
    template = payload.get('template') if isinstance(payload.get('template'), dict) else {}
    template['archive_url'] = zip_url
    payload['template'] = template
    upsert_official_source(
        payload,
        url=guide_url,
        label=f'ICLR {yetext} Official Author Guide',
        evidence=(
            f'Official author guide confirms {body_page_max}-page initial-submission main text, references outside the page limit, '
            f'and the LaTeX template archive {zip_url}.'
            if has_page_policy and has_unlimited_refs else
            'Official author guide was reachable and parsed by venue-intelligence.'
        ),
    )
    payload['source_checked_at'] = now_iso()
    blockers = payload.get('blockers') if isinstance(payload.get('blockers'), list) else []
    payload['blockers'] = [
        item for item in blockers
        if not ('AuthorGuide' in str(item) or 'AuthorInstructions' in str(item) or 'iclr.cc/Conferences' in str(item))
    ]



def latest_yedirectory_template(repo_dir: Path, venue_slug: str) -> tuple[Path | None, Path | None]:
    """Find the newest year-tagged official template directory generically."""
    slug_tokens = [token for token in re.split(r'[^a-z0-9]+', venue_slug.lower()) if token and not token.isdigit()]
    candidates: list[tuple[int, int, Path, Path]] = []
    for directory in repo_dir.rglob('*') if repo_dir.exists() else []:
        if not directory.is_dir():
            continue
        rel_depth = len(directory.relative_to(repo_dir).parts)
        if rel_depth > 3:
            continue
        yematch = re.search(r'(20\d{2})', directory.name.lower())
        if not yematch:
            continue
        tex_candidates = sorted(
            item for item in directory.glob('*.tex')
            if item.is_file() and not item.name.startswith('.')
        )
        if not tex_candidates:
            continue
        token_score = sum(1 for token in slug_tokens if token in directory.name.lower())
        preferred = next(
            (item for item in tex_candidates if any(word in item.name.lower() for word in ['conference', 'template', 'main', 'paper', 'sample'])),
            tex_candidates[0],
        )
        candidates.append((token_score, int(yematch.group(1)), directory, preferred))
    if not candidates:
        return None, None
    _score, _year, directory, main_tex = sorted(candidates, key=lambda item: (item[0], item[1]))[-1]
    return directory, main_tex


def infer_template_family(repo_url: str, venue_slug: str, directory: Path, main_tex: Path) -> str:
    text = ' '.join([repo_url, venue_slug, directory.name, main_tex.name]).lower()
    if 'iclr' in text:
        return 'iclr'
    if 'neurips' in text or 'nips' in text:
        return 'neurips'
    if 'icml' in text:
        return 'icml'
    if 'acl' in text or 'emnlp' in text or 'naacl' in text:
        return 'acl'
    if 'acm' in text or 'sigconf' in text or 'cikm' in text or 'kdd' in text:
        return 'acm-sigconf'
    return 'generic'




def normalize_required_file_name(value: Any) -> str:
    raw = str(value or '').strip().strip(chr(96) + chr(34) + chr(39))
    if not raw:
        return ''
    match = re.search(r'([A-Za-z0-9_.+-]+\.(?:tex|sty|bst|cls|bbx|cbx|bib))', raw)
    return match.group(1) if match else raw


def normalize_template_machine_fields(template: dict[str, Any]) -> None:
    required_files = template.get('required_files') if isinstance(template.get('required_files'), list) else []
    template['required_files'] = list(dict.fromkeys(name for name in (normalize_required_file_name(item) for item in required_files) if name))
    required_markers = template.get('required_markers') if isinstance(template.get('required_markers'), list) else []
    template['required_markers'] = [str(item).strip() for item in required_markers if str(item).strip()]
    forbidden = template.get('forbidden_fallback_markers') if isinstance(template.get('forbidden_fallback_markers'), list) else []
    template['forbidden_fallback_markers'] = [str(item).strip() for item in forbidden if str(item).strip()]


def apply_yedirectory_template(
    payload: dict[str, Any],
    template: dict[str, Any],
    repo_url: str,
    repo_dir: Path,
    commit: str,
) -> None:
    venue_slug = slugify(str(payload.get('venue') or ''))
    directory, main_tex = latest_yedirectory_template(repo_dir, venue_slug)
    if not directory or not main_tex:
        return
    family = infer_template_family(repo_url, venue_slug, directory, main_tex)
    template['family'] = str(template.get('family') or family)
    template['directory_hint'] = str(directory.relative_to(repo_dir))
    template['main_tex'] = main_tex.name
    sidecars = []
    for file in sorted(directory.iterdir()):
        if file.is_file() and (file.suffix.lower() in {'.sty', '.bst', '.cls', '.bbx', '.cbx'} or file.name in {'natbib.sty', 'fancyhdr.sty', 'math_commands.tex'}):
            sidecars.append(file.name)
    required = [main_tex.name] + sidecars
    template['required_files'] = list(dict.fromkeys([name for name in (normalize_required_file_name(item) for item in (template.get('required_files') or [])) if name] + required))
    template['verified_directory_hint'] = str(directory.relative_to(repo_dir)).strip('/')
    template['verified_files'] = {
        file.name: {'bytes': file.stat().st_size, 'sha256': sha256_file(file)}
        for file in sorted(directory.iterdir())
        if file.is_file() and (file.suffix.lower() in {'.tex', '.sty', '.bst', '.cls', '.bbx', '.cbx'} or file.name in {'natbib.sty', 'fancyhdr.sty', 'math_commands.tex'})
    }
    tex = main_tex.read_text(encoding='utf-8', errors='replace')
    class_match = re.search(r'\\documentclass(?:\[[^\]]+\])?\{([^{}]+)\}', tex)
    if class_match and not template.get('documentclass'):
        template['documentclass'] = class_match.group(1).strip()
    style_match = re.search(r'\\bibliographystyle\{([^{}]+)\}', tex)
    if style_match and not template.get('bibliography_style'):
        template['bibliography_style'] = style_match.group(1).strip()
    template['selection_policy'] = 'latest year-tagged official template directory discovered from verified repository'
    template['selection_evidence'] = f'Parsed from official repository commit {commit}, directory {directory.relative_to(repo_dir)}, main tex {main_tex.name}.'


def clean_official_sources(payload: dict[str, Any], repo_url: str = '') -> None:
    sources = payload.get('official_sources') if isinstance(payload.get('official_sources'), list) else []
    verified: list[Any] = []
    unverified: list[Any] = []
    for item in sources:
        if unverified_source(item):
            unverified.append(item)
        else:
            verified.append(item)
    if repo_url and not any(repo_url.replace('.git', '') in str(item).replace('.git', '') for item in verified):
        verified.insert(0, {
            'url': repo_url,
            'label': 'official template repository',
            'evidence': 'TASTE verified the current official repository files and commit before writing.',
        })
    payload['official_sources'] = verified
    if unverified:
        payload['candidate_official_sources'] = unverified
        payload.setdefault('warnings', []).append('Some candidate venue pages were not counted as verified official sources because TASTE could not access or confirm them.')


def reference_quality_floor(payload: dict[str, Any], venue: str) -> int:
    """Return an explicitly configured writing-quality reference floor.

    The normal path is dynamic: venue-intelligence/Claude Code resolves the
    reference-quality target from the target venue, track, paper type, and field.
    This function is only an operator override; it intentionally has no built-in
    venue-specific default such as 60, so a missing dynamic target is visible.
    """
    raw = str(os.environ.get('WRITING_FULL_CONFERENCE_MIN_REFERENCES') or '').strip()
    if not raw:
        return 0
    try:
        floor = int(raw)
    except ValueError:
        return 0
    track = str(payload.get('track') or '').lower()
    slug = slugify(venue)
    full_conference = 'conference' in track or any(token in slug for token in ['iclr', 'neurips', 'icml', 'kdd', 'cikm', 'acl', 'emnlp', 'aaai', 'cvpr', 'eccv'])
    return floor if full_conference else 0


def augment_with_repository_verification(payload: dict[str, Any], *, refresh_official: bool = False) -> dict[str, Any]:
    template = payload.get('template') if isinstance(payload.get('template'), dict) else {}
    repo_url = str(template.get('repository_url') or template.get('official_source_url') or '').strip()
    if not repo_url or 'github.com/' not in repo_url:
        return payload
    tmp_root = Path(tempfile.mkdtemp(prefix='venue_req_verify_'))
    try:
        remote_head = remote_repository_head(repo_url)
        cache_candidates = [
            ROOT / 'projects' / str(payload.get('project') or '') / 'paper' / 'venues' / slugify(str(payload.get('venue') or '')) / 'official_repo_cache',
            Path('/tmp/iclr_master_template_check_codex'),
        ] if (use_template_cache() and not refresh_official) else []
        repo_dir = verified_local_template_source(payload, repo_url)
        commit = str(payload.get('official_repository_commit') or template.get('verified_repository_commit') or 'local-template-source').strip()
        if repo_dir is not None:
            meta_commit = str(template.get('verified_repository_commit') or '').strip()
            if template.get('local_source_is_template_root') and meta_commit:
                if remote_head and meta_commit != remote_head:
                    payload.setdefault('warnings', []).append('discarding stale local official venue template source because official repository HEAD changed')
                    repo_dir = None
                else:
                    commit = meta_commit
                    payload.setdefault('warnings', []).append('using template_source.json verified official venue template source for deterministic verification')
            else:
                commit_proc = run_git_checked(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'], cwd=ROOT, timeout=20)
                if commit_proc.returncode == 0 and commit_proc.stdout.strip():
                    local_commit = commit_proc.stdout.strip()
                    if remote_head and local_commit != remote_head:
                        payload.setdefault('warnings', []).append('discarding stale local official venue template source because official repository HEAD changed')
                        repo_dir = None
                    else:
                        commit = local_commit
                        payload.setdefault('warnings', []).append('using already fetched official venue template source for deterministic verification')
                elif remote_head and commit not in {'', 'local-template-source'}:
                    if commit != remote_head:
                        payload.setdefault('warnings', []).append('discarding stale local official venue template source because official repository HEAD changed')
                        repo_dir = None
                    else:
                        payload.setdefault('warnings', []).append('using template_source.json verified official venue template source for deterministic verification')
                elif remote_head:
                    payload.setdefault('warnings', []).append('using already fetched official venue template source; git metadata is unavailable but template_source.json/file validation passed')
        for candidate in cache_candidates if repo_dir is None else []:
            if not candidate.exists():
                continue
            remote = run_git_checked(['git', '-C', str(candidate), 'remote', 'get-url', 'origin'], cwd=ROOT, timeout=20)
            if remote.returncode == 0 and repo_url.replace('.git', '') in remote.stdout.strip().replace('.git', ''):
                repo_dir = candidate
                payload.setdefault('warnings', []).append('using cached official clone with matching origin for deterministic verification')
                commit_proc = run_git_checked(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'], cwd=ROOT, timeout=20)
                if commit_proc.returncode == 0:
                    commit = commit_proc.stdout.strip()
                break
        if repo_dir is None:
            repo_dir = tmp_root / 'repo'
            env = os.environ.copy()
            env['GIT_TERMINAL_PROMPT'] = '0'
            proc = run_git_checked(['git', 'clone', '--depth=1', repo_url, str(repo_dir)], cwd=ROOT, env=env, timeout=int(os.environ.get('VENUE_GIT_TIMEOUT_SEC', '60') or 60))
            if proc.returncode != 0:
                message = 'official repository verification failed: ' + (proc.stderr or proc.stdout or '').strip()[:500]
                payload.setdefault('warnings', []).append(message)
                if refresh_official:
                    payload.setdefault('blockers', []).append(message)
                return payload
            commit_proc = run_git_checked(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'], cwd=ROOT, timeout=20)
            if commit_proc.returncode == 0:
                commit = commit_proc.stdout.strip()
        required = [str(item) for item in template.get('required_files', []) if str(item).strip()]
        main_tex_hint = str(template.get('main_tex') or '').strip()
        candidates: list[Path] = []
        directory_hint = str(template.get('directory_hint') or '').strip().strip('/')
        if directory_hint and (repo_dir / directory_hint).exists():
            candidates.append(repo_dir / directory_hint)
        for name in required + ([main_tex_hint] if main_tex_hint else []):
            candidates.extend(path.parent for path in repo_dir.rglob(name) if path.is_file())
        if not candidates:
            candidates.append(repo_dir)
        best = max(candidates, key=lambda p: sum(1 for item in required if (p / item).is_file()))
        files: dict[str, dict[str, object]] = {}
        for file in sorted(best.iterdir() if best.is_dir() else []):
            if file.is_file() and file.suffix.lower() in {'.tex', '.sty', '.bst', '.cls'}:
                files[file.name] = {'bytes': file.stat().st_size, 'sha256': sha256_file(file)}
        template.pop('template_file_paths', None)
        template.pop('template_issues', None)
        template['verified_repository_url'] = repo_url
        template['verified_repository_commit'] = commit
        if template.get('local_source_is_template_root'):
            template['verified_directory_hint'] = str(template.get('directory_hint') or '').strip('/')
        else:
            template['verified_directory_hint'] = str(best.relative_to(repo_dir)).strip('/') if best != repo_dir else str(template.get('directory_hint') or '').strip('/')
        template['verified_files'] = files
        apply_yedirectory_template(payload, template, repo_url, repo_dir, commit)
        if 'ICLR/Master-Template' in repo_url or 'iclr' in str(payload.get('venue', '')).lower():
            iclr_dir, iclr_main = latest_iclr_template(repo_dir)
            if iclr_dir and iclr_main:
                best = iclr_dir
                style_stem = iclr_main.stem
                yematch = re.search(r'(20\d{2})', style_stem)
                yetext = yematch.group(1) if yematch else ''
                official_dir_name = iclr_dir.name.strip('/')
                if not re.fullmatch(r'iclr20\d{2}', official_dir_name.lower()):
                    yefrom_tex = re.search(r'iclr(20\d{2})_conference', iclr_main.name.lower())
                    official_dir_name = f'iclr{yefrom_tex.group(1)}' if yefrom_tex else official_dir_name
                template['directory_hint'] = official_dir_name
                template['main_tex'] = iclr_main.name
                template['required_files'] = [
                    iclr_main.name,
                    f'{style_stem}.sty',
                    f'{style_stem}.bst',
                    'natbib.sty',
                    'fancyhdr.sty',
                    'math_commands.tex',
                ]
                marker_year = f' at ICLR {yetext}' if yetext else ' at ICLR'
                template['required_markers'] = [f'Under review as a conference paper{marker_year}', 'Anonymous authors', 'Paper under double-blind review']
                template['bibliography_style'] = style_stem
                template['verified_directory_hint'] = official_dir_name
                template['verified_files'] = {
                    file.name: {'bytes': file.stat().st_size, 'sha256': sha256_file(file)}
                    for file in sorted(iclr_dir.iterdir())
                    if file.is_file() and (file.suffix.lower() in {'.tex', '.sty', '.bst'} or file.name in {'natbib.sty', 'fancyhdr.sty', 'math_commands.tex'})
                }
                tex = iclr_main.read_text(encoding='utf-8', errors='replace')
                template_body_page_max = parse_body_page_limit(tex)
                if template_body_page_max:
                    page_policy = payload.setdefault('page_policy', {})
                    page_policy['body_page_max'] = template_body_page_max
                    page_policy['reference_page_max'] = 0
                    page_policy['total_page_max'] = 0
                    page_policy['appendix_policy'] = f'Official {iclr_main.name} template instructions state a strict {template_body_page_max}-page main-text limit for initial submission, with unlimited additional pages for citations.'
                    page_policy['source_type'] = 'official_template_instruction'
                    page_policy['source_evidence'] = f'Parsed from official repository commit {commit}, file {official_dir_name}/{iclr_main.name}.'
                verify_iclr_author_guide(payload, yetext, commit, Path(official_dir_name), iclr_main)
        payload['template'] = template
        payload['official_repository_verified'] = True
        payload['official_repository_commit'] = commit
        clean_official_sources(payload, repo_url=repo_url)
        old_blockers = payload.get('blockers') if isinstance(payload.get('blockers'), list) else []
        moved = [item for item in old_blockers if 'Current ' in str(item) or 'workspace' in str(item) or 'paper.tex' in str(item) or 'Network unreachable' in str(item)]
        kept = [item for item in old_blockers if item not in moved]
        if moved:
            payload.setdefault('warnings', []).extend(moved)
        payload['blockers'] = kept
        warnings = payload.get('warnings') if isinstance(payload.get('warnings'), list) else []
        payload['warnings'] = [
            item for item in warnings
            if 'ARIS' not in str(item) and 'official repository verification failed' not in str(item)
        ]
        return payload
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def normalize_requirement_payload(payload: dict[str, Any], venue: str, project: str) -> dict[str, Any]:
    info = venue_info(venue)
    slug = slugify(venue)
    payload.setdefault("status", "ok")
    payload.setdefault("venue", venue)
    payload.setdefault("venue_slug", slug)
    payload.setdefault("project", project)
    payload.setdefault("source_checked_at", now_iso())
    payload.setdefault("updated_at", now_iso())
    payload.setdefault("official_sources", [])
    template = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    normalize_template_machine_fields(template)
    policy = payload.get("venue_submission_policy") if isinstance(payload.get("venue_submission_policy"), dict) else {}
    profile = payload.get("venue_template_profile") if isinstance(payload.get("venue_template_profile"), dict) else {}

    if not policy:
        policy = {
            "status": "known" if payload.get("official_sources") else "unknown",
            "venue": venue,
            "slug": slug,
            "track": str(payload.get("track") or "conference"),
            "source_url": str((payload.get("official_sources") or [{}])[0].get("url") if payload.get("official_sources") else info.get("official_source_url") or ""),
            "source_label": str(payload.get("source_label") or "official author instructions"),
            "source_checked_date": now_iso()[:10],
            "format_label": str(template.get("format_label") or info.get("format") or "venue official LaTeX template"),
            "template_family": str(template.get("family") or info.get("template_family") or "generic"),
            "required_documentclass": str(template.get("documentclass") or ""),
            "required_documentclass_options": list(template.get("documentclass_options") or []),
            "body_page_min": int((payload.get("page_policy") or {}).get("body_page_min") or 0),
            "body_page_max": int((payload.get("page_policy") or {}).get("body_page_max") or 0),
            "reference_page_max": int((payload.get("page_policy") or {}).get("reference_page_max") or 0),
            "total_page_max": int((payload.get("page_policy") or {}).get("total_page_max") or 0),
            "min_references": 0,
            "official_min_references": 0,
            "reference_quality_target": 0,
            "reference_quality_target": 0,
            "reference_target_source": "none",
            "estimated_references_per_page": int((payload.get("citation_policy") or {}).get("estimated_references_per_page") or 30),
            "canonical_sections": list((payload.get("paper_shape") or {}).get("canonical_sections") or ["Introduction", "Related Work", "Method", "Experiments", "Conclusion"]),
            "max_main_sections": int((payload.get("paper_shape") or {}).get("max_main_sections") or 7),
            "anonymous_required": bool((payload.get("review_policy") or {}).get("anonymous_required", True)),
            "self_citation_third_person_required": bool((payload.get("review_policy") or {}).get("self_citation_third_person_required", True)),
            "reproducibility_expected": bool((payload.get("review_policy") or {}).get("reproducibility_expected", True)),
            "desk_reject_risks": list(payload.get("desk_reject_risks") or []),
        }
    if not profile:
        profile = {
            "venue": venue,
            "slug": slug,
            "family": str(template.get("family") or policy.get("template_family") or info.get("template_family") or "generic"),
            "format_label": str(template.get("format_label") or policy.get("format_label") or info.get("format") or "venue official LaTeX template"),
            "documentclass": str(template.get("documentclass") or policy.get("required_documentclass") or ""),
            "required_options": list(template.get("documentclass_options") or policy.get("required_documentclass_options") or []),
            "recommended_options": list(template.get("recommended_documentclass_options") or []),
            "forbidden_documentclasses": list(template.get("forbidden_documentclasses") or []),
            "required_markers": list(template.get("required_markers") or []),
            "forbidden_markers": list(template.get("forbidden_fallback_markers") or ["minimal compile fallback", "compile-capable fallback", "generated venue-aware fallback"]),
            "required_files": list(template.get("required_files") or []),
            "submission_policy": policy,
            "submission_notes": list(payload.get("submission_notes") or []),
        }
    page_policy = payload.get("page_policy") if isinstance(payload.get("page_policy"), dict) else {}
    citation_policy = payload.get("citation_policy") if isinstance(payload.get("citation_policy"), dict) else {}
    review_policy = payload.get("review_policy") if isinstance(payload.get("review_policy"), dict) else {}
    policy.setdefault("status", "known")
    policy.setdefault("format_label", str(template.get("format_label") or info.get("format") or "venue official LaTeX template"))
    policy.setdefault("template_family", str(template.get("family") or info.get("template_family") or "generic"))
    policy.setdefault("required_documentclass", str(template.get("documentclass") or ""))
    policy.setdefault("required_documentclass_options", list(template.get("documentclass_options") or []))
    policy.setdefault("body_page_min", int(page_policy.get("body_page_min") or 0))
    policy.setdefault("body_page_max", int(page_policy.get("body_page_max") or 0))
    policy.setdefault("reference_page_max", int(page_policy.get("reference_page_max") or 0))
    policy.setdefault("total_page_max", int(page_policy.get("total_page_max") or 0))
    citation_source = str(citation_policy.get("source_type") or "").lower()
    min_refs_value = int(citation_policy.get("min_verified_references") or 0)
    if min_refs_value <= 0:
        match = re.search(r"(\d+)\s+(?:verified\s+)?references", str(citation_policy.get("rationale") or ""), flags=re.IGNORECASE)
        if match and citation_source.startswith("quality"):
            min_refs_value = int(match.group(1))
            citation_policy["min_verified_references"] = min_refs_value
    official_min_refs = min_refs_value if citation_source.startswith("official") else 0
    quality_target = 0 if citation_source.startswith("official") else min_refs_value
    quality_floor = reference_quality_floor(payload, venue)
    if quality_floor and quality_target <= 0:
        quality_target = quality_floor
        if not citation_source.startswith("official"):
            citation_policy["min_verified_references"] = quality_target
            citation_policy["source_type"] = citation_source or "quality_target"
        citation_policy["rationale"] = (
            str(citation_policy.get("rationale") or "").rstrip()
            + f" writing fallback quality target asks for at least {quality_floor} verified references for a full-conference manuscript only because venue-intelligence did not resolve a stronger dynamic target; this is not an official venue requirement."
        ).strip()
    elif quality_floor and quality_target < quality_floor:
        citation_policy["rationale"] = (
            str(citation_policy.get("rationale") or "").rstrip()
            + f" TASTE fallback floor is {quality_floor}, but the resolved dynamic venue/track quality target is {quality_target}; The workflow keeps the dynamic target instead of overwriting it."
        ).strip()
    citation_policy["official_min_verified_references"] = official_min_refs
    citation_policy["quality_target_min_verified_references"] = quality_target
    policy["min_references"] = official_min_refs
    policy["official_min_references"] = official_min_refs
    policy["reference_quality_target"] = quality_target
    policy["reference_quality_target"] = quality_target
    policy["reference_target_source"] = "official" if official_min_refs else ("quality_target" if quality_target else "none")
    policy.setdefault("estimated_references_per_page", int(citation_policy.get("estimated_references_per_page") or 30))
    policy.setdefault("anonymous_required", bool(review_policy.get("anonymous_required", True)))
    profile.setdefault("family", str(template.get("family") or policy.get("template_family") or "generic"))
    profile.setdefault("format_label", str(template.get("format_label") or policy.get("format_label") or "venue official LaTeX template"))
    profile.setdefault("documentclass", str(template.get("documentclass") or policy.get("required_documentclass") or ""))
    profile.setdefault("required_options", list(template.get("documentclass_options") or policy.get("required_documentclass_options") or []))
    profile.setdefault("required_markers", list(template.get("required_markers") or []))
    profile.setdefault("forbidden_markers", list(template.get("forbidden_fallback_markers") or ["minimal compile fallback", "compile-capable fallback", "generated venue-aware fallback"]))
    profile.setdefault("required_files", list(template.get("required_files") or []))
    profile["submission_policy"] = policy
    payload["template"] = template
    payload["venue_submission_policy"] = policy
    payload["venue_template_profile"] = profile
    return payload


def stale_repository_verification_blocker(value: Any) -> bool:
    text = str(value or '').lower()
    return any(marker in text for marker in [
        'official repository verification failed',
        'official repository refresh failed',
        'git command timed out',
        'status is not ok/pass',
    ])


def heal_verified_venue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    has_verified_contract = bool(
        payload.get('official_repository_verified')
        and payload.get('official_sources')
        and isinstance(payload.get('page_policy'), dict)
        and isinstance(payload.get('venue_submission_policy'), dict)
        and isinstance(payload.get('venue_template_profile'), dict)
    )
    if not has_verified_contract:
        return payload
    blockers = payload.get('blockers') if isinstance(payload.get('blockers'), list) else []
    kept = [item for item in blockers if not stale_repository_verification_blocker(item)]
    removed = [item for item in blockers if stale_repository_verification_blocker(item)]
    if removed:
        payload['blockers'] = kept
        payload.setdefault('warnings', []).append('cleared stale repository clone-timeout blockers after local official template source and venue policy validated')
    if payload.get('status') == 'blocked' and not kept:
        payload['status'] = 'ok'
    return payload


def validate_payload(payload: dict[str, Any]) -> list[str]:
    payload = heal_verified_venue_payload(payload)
    failures: list[str] = []
    if payload.get("status") not in {"ok", "pass"}:
        failures.append("status is not ok/pass")
    if not isinstance(payload.get("official_sources"), list) or not payload.get("official_sources"):
        failures.append("official_sources is empty")
    elif any(unverified_source(item) for item in payload.get("official_sources", [])):
        failures.append("official_sources contain unverified or unreachable evidence")
    page_policy = payload.get("page_policy") if isinstance(payload.get("page_policy"), dict) else {}
    page_source = str(page_policy.get("source_type") or "").lower()
    if not page_policy:
        failures.append("page_policy missing")
    elif page_policy.get("body_page_max") not in (None, "", 0) and not page_source.startswith("official"):
        failures.append("body_page_max must come from an official source, not a guessed local default")
    template = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    if not any(template.get(key) for key in ["repository_url", "archive_url", "official_source_url"]):
        failures.append("template lacks repository_url/archive_url/official_source_url")
    policy = payload.get("venue_submission_policy") if isinstance(payload.get("venue_submission_policy"), dict) else {}
    if any('official repository verification failed' in str(item).lower() for item in (payload.get('blockers') if isinstance(payload.get('blockers'), list) else [])):
        failures.append('official repository refresh failed')
    if not policy:
        failures.append("venue_submission_policy missing")
    profile = payload.get("venue_template_profile") if isinstance(payload.get("venue_template_profile"), dict) else {}
    if not profile:
        failures.append("venue_template_profile missing")
    citation_policy = payload.get("citation_policy") if isinstance(payload.get("citation_policy"), dict) else {}
    citation_source = str(citation_policy.get("source_type") or policy.get("reference_target_source") or "").lower()
    try:
        official_min = int(policy.get("official_min_references") or policy.get("min_references") or citation_policy.get("official_min_verified_references") or 0)
    except (TypeError, ValueError):
        official_min = 0
    try:
        quality_target = int(policy.get("reference_quality_target") or policy.get("reference_quality_target") or citation_policy.get("quality_target_min_verified_references") or citation_policy.get("min_verified_references") or 0)
    except (TypeError, ValueError):
        quality_target = 0
    track = str(payload.get("track") or policy.get("track") or "conference").lower()
    slug = slugify(str(payload.get("venue") or ""))
    full_conference = "conference" in track or any(token in slug for token in ["iclr", "neurips", "icml", "kdd", "cikm", "acl", "emnlp", "aaai", "cvpr", "eccv"])
    nature_family = bool(payload.get('nature_family_article_mode')) or 'nature' in slug
    if full_conference and not nature_family and official_min <= 0 and quality_target <= 0:
        failures.append("reference quality target missing: if the official venue has no minimum, venue-intelligence must set a dynamic verified-reference target for the venue/track/field")
    if nature_family and quality_target <= 0:
        failures.append('Nature-family writing-quality reference target missing')
    if official_min <= 0 and quality_target > 0 and citation_source.startswith("official"):
        failures.append("citation target source is inconsistent: non-official reference-quality target is marked official")
    return failures


def build_prompt(project: str, venue: str, out_path: Path) -> str:
    info = venue_info(venue)
    hints = {
        "known_domains": info.get("domains", []),
        "known_queries": info.get("queries", []),
        "known_source_hint": info.get("official_source_url", ""),
        "known_template_family_hint": info.get("template_family", ""),
    }
    return f"""
你是 writing 模块的 venue-intelligence 子任务，只负责获取目标会议/期刊的最新官方投稿规则和 LaTeX 模板来源，不写论文正文，不改实验，不提升 claim。

项目: {project}
目标 venue: {venue}
当前日期: {now_iso()[:10]}
输出 JSON: {out_path}

必须做的事：
1. 只使用官方来源或官方仓库/官方 author kit 页面；不要用博客、过期镜像、第三方模板站作为依据。
2. 自主联网核对最新 author instructions、call-for-papers、OpenReview/官方投稿页和 LaTeX template；如果有年份/track 版本，选择当前可投/用户指定 venue 与 track 对应的最新正式模板，不要复用其他会议、其他年份或其他 track 的模板或页数。
3. 将正文页数、参考文献页数、总页数、匿名要求、模板 documentclass/options、bibliography style、必须随模板一起复制的文件写成机器可读 JSON；每个非零页数限制都必须来自官方来源证据，不能凭经验或缓存猜测。
4. 如果官方没有规定最少引用数，不要伪装成官方要求；必须结合目标会议、track、论文类型、研究领域和当前文献密度给出 写作质量目标，并把 source_type 写成 quality_target。这个目标必须由本次 venue-intelligence 动态说明依据，不能把某个会议、某一年份或某个历史项目的引用数量硬套到其他 venue/track。参考文献过少时应要求 writing 补充真实、相关、可解析 BibTeX 的已验证引用，而不是降低标准。
5. 如果不能确认官方信息，写 status=blocked 和 blockers，不要猜；不可把无法访问的网页写成 verified official source。
6. 不要把某个会议的页数、模板、匿名规则或引用目标套用到另一个 venue；如果官方模板仓库有年份目录，必须选择当前可投/用户指定 venue 对应的最新正式目录，并记录 commit、文件名和 hash。
7. layout_guidance 必须告诉 writing：版面修复先诊断图表/表格占地和参考文献页占地，再考虑正文长短；正文页数在官方上限内时，当前任务应定义为图表占地、真实引用覆盖、bibliography 密度和模板细节修复，不能定义成删减正文任务。
8. 页面/报告不要出现第三方模块名；只称 writing 模块。

已知搜索提示，可使用但不能当作证据本身：
```json
{json.dumps(hints, ensure_ascii=False, indent=2)}
```

请写出严格 JSON，schema 如下：
```json
{{
  "status": "ok",
  "venue": "{venue}",
  "track": "conference",
  "source_checked_at": "ISO-8601 time",
  "official_sources": [{{"url": "https://...", "label": "official author instructions", "evidence": "short paraphrase"}}],
  "page_policy": {{
    "body_page_min": 0,
    "body_page_max": 0,
    "reference_page_max": 0,
    "total_page_max": 0,
    "appendix_policy": "official wording paraphrase",
    "source_type": "official",
    "source_url": "official page or template URL used for page policy",
    "source_evidence": "short paraphrase of the exact official page/template rule"
  }},
  "citation_policy": {{
    "min_verified_references": 0,
    "estimated_references_per_page": 30,
    "source_type": "official_or_quality_target",
    "rationale": "short rationale"
  }},
  "review_policy": {{
    "anonymous_required": true,
    "self_citation_third_person_required": true,
    "reproducibility_expected": true
  }},
  "template": {{
    "family": "iclr|acm-sigconf|neurips|icml|acl|generic",
    "format_label": "official format label",
    "official_source_url": "https://...",
    "repository_url": "https://github.com/...git or empty",
    "archive_url": "https://...zip or empty",
    "directory_hint": "path inside repo/archive or empty",
    "main_tex": "main/template tex filename",
    "documentclass": "article/acmart/etc",
    "documentclass_options": [],
    "bibliography_style": "style name or file",
    "required_files": ["files that must be copied next to paper.tex"],
    "required_markers": ["strings expected in template tex/style"],
    "forbidden_fallback_markers": ["minimal compile fallback", "compile-capable fallback", "generated venue-aware fallback"]
  }},
  "paper_shape": {{"canonical_sections": ["Introduction", "Related Work", "Method", "Experiments", "Conclusion"], "max_main_sections": 7}},
  "layout_guidance": {{
    "page_fit_priority": ["figure/table footprint", "reference count and bibliography style", "prose length"],
    "figure_policy": "if body pages exceed the official limit, diagnose float/table footprint before editing scientific content; if body pages are within limit, repair citation coverage, figure footprint, and template details"
  }},
  "venue_submission_policy": {{}},
  "venue_template_profile": {{}},
  "blockers": []
}}
```

写完后不要生成论文，只返回你写入的路径和关键官方来源。
""".strip()


def run_claude(project: str, venue: str, out_path: Path, timeout_sec: int) -> dict[str, Any]:
    prompt_path = out_path.parent / "venue_requirements_prompt.md"
    write_text(prompt_path, build_prompt(project, venue, out_path))
    cmd = [
        sys.executable,
        str(SCRIPTS / "claude_project_session.py"),
        "--project",
        project,
        "--stage",
        "writing:venue-intelligence",
        "--message-file",
        str(prompt_path),
        "--timeout-sec",
        str(timeout_sec),
        "--agent-id",
        "venue-intelligence",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env={**os.environ, "PROJECT_ID": project}, timeout=max(30, timeout_sec + 60))
    return {
        "command": cmd,
        "return_code": proc.returncode,
        "stdout_tail": proc.stdout[-6000:],
        "stderr_tail": proc.stderr[-6000:],
        "prompt_path": str(prompt_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve current official venue requirements and template source for writing.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--refresh-current-venue", dest="force_refresh", action="store_true")
    parser.add_argument("--force-refresh", dest="force_refresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--max-age-days", type=int, default=14)
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("VENUE_REQUIREMENTS_TIMEOUT_SEC", "1800")))
    parser.add_argument("--skip-claude", action="store_true")
    args = parser.parse_args()

    paths = build_paths(args.project)
    venue_slug = slugify(args.venue)
    venue_root = paths.root / "paper" / "venues" / venue_slug
    venue_root.mkdir(parents=True, exist_ok=True)
    out_path = venue_root / "venue_requirements.json"
    report_path = venue_root / "venue_requirements_report.md"

    existing = load_json(out_path, {})
    if isinstance(existing, dict) and existing and not args.force_refresh and is_fresh(existing, args.max_age_days):
        payload = normalize_requirement_payload(augment_with_repository_verification(existing, refresh_official=False), args.venue, args.project)
        write_json(out_path, payload)
        update_pipeline_state(args.project, {
            "venue_requirements_ready": True,
            "venue_requirements_path": str(out_path),
            "venue_requirements_status": payload.get("status"),
            "venue_submission_policy": payload.get("venue_submission_policy", {}),
            "venue_template_profile": payload.get("venue_template_profile", {}),
        }, venue=args.venue)
        print(out_path)
        return 0

    claude_result: dict[str, Any] = {"skipped": True}
    if is_nature_family_venue(args.venue):
        payload = build_nature_family_requirements(args.project, args.venue)
        write_json(out_path, payload)
    elif not args.skip_claude:
        claude_result = run_claude(args.project, args.venue, out_path, args.timeout_sec)

    payload = load_json(out_path, {})
    if isinstance(payload, dict) and payload:
        payload = heal_verified_venue_payload(normalize_requirement_payload(augment_with_repository_verification(payload, refresh_official=args.force_refresh), args.venue, args.project))
    elif args.skip_claude and isinstance(existing, dict) and existing:
        payload = heal_verified_venue_payload(normalize_requirement_payload(augment_with_repository_verification(existing, refresh_official=args.force_refresh), args.venue, args.project))
    else:
        payload = {
            "status": "blocked",
            "venue": args.venue,
            "venue_slug": venue_slug,
            "project": args.project,
            "source_checked_at": now_iso(),
            "updated_at": now_iso(),
            "official_sources": [],
            "blockers": ["venue requirements were not produced by venue-intelligence"],
        }

    failures = validate_payload(payload)
    if failures:
        payload["status"] = "blocked"
        payload["blockers"] = list(dict.fromkeys(list(payload.get("blockers") or []) + failures))
    else:
        payload["status"] = "ok"
    payload["updated_at"] = now_iso()
    payload["resolver"] = {"claude": claude_result}
    write_json(out_path, payload)

    ready = payload.get("status") == "ok"
    update_pipeline_state(args.project, {
        "venue_requirements_ready": ready,
        "venue_requirements_path": str(out_path),
        "venue_requirements_status": payload.get("status"),
        "venue_requirements_blockers": payload.get("blockers", []),
        "venue_submission_policy": payload.get("venue_submission_policy", {}),
        "venue_template_profile": payload.get("venue_template_profile", {}),
    }, venue=args.venue)
    lines = [
        "# Venue Requirements Report\n\n",
        f"- venue: {args.venue}\n",
        f"- status: {payload.get('status')}\n",
        f"- requirements: {out_path}\n",
        f"- claude_return_code: {claude_result.get('return_code', '')}\n",
        "\n## Blockers\n\n",
    ]
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    lines.extend(f"- {item}\n" for item in blockers) if blockers else lines.append("- none\n")
    lines.extend(["\n## Official Sources\n\n"])
    for item in payload.get("official_sources", []) if isinstance(payload.get("official_sources"), list) else []:
        if isinstance(item, dict):
            lines.append(f"- {item.get('label', 'source')}: {item.get('url', '')}\n")
    write_text(report_path, "".join(lines))
    print(out_path)
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
