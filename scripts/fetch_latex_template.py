#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import shutil
import subprocess
import tempfile
import urllib.error
from pathlib import Path
from typing import Any

from project_paths import ROOT
from paper_common import (
    download_binary,
    ensure_paper_dirs,
    fetch_url,
    filter_candidate_urls,
    find_download_links,
    load_json,
    make_failure_report,
    search_duckduckgo,
    slugify,
    unpack_archive,
    update_pipeline_state,
    venue_info,
    write_json,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def use_template_cache() -> bool:
    return str(os.environ.get('VENUE_TEMPLATE_USE_CACHE') or '').lower() in {'1', 'true', 'yes', 'on'}




def run_git_checked(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
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
        return subprocess.CompletedProcess(
            cmd,
            124,
            stdout or '',
            (stderr or '') + f'\ngit command timed out after {timeout}s',
        )


def text_files(root: Path) -> list[Path]:
    suffixes = {'.tex', '.sty', '.bst', '.cls', '.bbx', '.cbx'}
    return [path for path in root.rglob('*') if path.is_file() and path.suffix.lower() in suffixes]


def copy_source_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, destination / source.name)
        return
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        elif item.is_file():
            shutil.copy2(item, target)


def load_requirements(project: str, venue: str) -> dict[str, Any]:
    root = ROOT / 'projects' / project / 'paper' / 'venues' / slugify(venue)
    payload = load_json(root / 'venue_requirements.json', {})
    return payload if isinstance(payload, dict) and payload.get('status') in {'ok', 'pass'} else {}




def normalize_required_file_name(value: Any) -> str:
    raw = str(value or '').strip().strip(chr(96) + chr(34) + chr(39))
    if not raw:
        return ''
    match = re.search(r'([A-Za-z0-9_.+-]+\.(?:tex|sty|bst|cls|bbx|cbx|bib))', raw)
    return match.group(1) if match else raw


def validate_template_source(source_dir: Path, requirements: dict[str, Any]) -> dict[str, Any]:
    template = requirements.get('template') if isinstance(requirements.get('template'), dict) else {}
    required_files = list(dict.fromkeys(name for name in (normalize_required_file_name(item) for item in template.get('required_files', [])) if name))
    required_markers = [str(item) for item in template.get('required_markers', []) if str(item).strip()]
    forbidden_markers = [str(item) for item in template.get('forbidden_fallback_markers', []) if str(item).strip()]
    if not forbidden_markers:
        forbidden_markers = ['minimal compile fallback', 'compile-capable fallback', 'generated venue-aware fallback']
    failures: list[str] = []
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(text_files(source_dir)):
        rel = str(path.relative_to(source_dir))
        files[rel] = {'bytes': path.stat().st_size, 'sha256': sha256_file(path)}
    for name in required_files:
        direct = source_dir / name
        basename = Path(name).name
        matches = [direct] if direct.is_file() else [path for path in source_dir.rglob(basename) if path.is_file()]
        if not matches:
            failures.append(f'missing required template file: {name}')
    combined_parts: list[str] = []
    for path in text_files(source_dir):
        try:
            combined_parts.append(path.read_text(encoding='utf-8', errors='replace'))
        except Exception:
            continue
    combined = '\n'.join(combined_parts)
    lowered = combined.lower()
    for marker in required_markers:
        if marker not in combined:
            failures.append(f'missing required template marker: {marker}')
    for marker in forbidden_markers:
        if marker.lower() in lowered:
            failures.append(f'forbidden fallback marker found: {marker}')
    family = str(template.get('family') or '').lower()
    if family == 'iclr':
        style_files = [name for name in required_files if name.lower().startswith('iclr') and name.lower().endswith('.sty')]
        sty_name = style_files[0] if style_files else ''
        sty = next(iter(source_dir.rglob(sty_name)), None) if sty_name else next(iter(source_dir.rglob('iclr20*_conference.sty')), None)
        if not sty:
            failures.append('missing resolved official venue style sidecar from venue requirements')
        elif sty.stat().st_size < 4000:
            failures.append(f'resolved official venue style sidecar is too small to be credible: {sty.stat().st_size} bytes')
    if family == 'springer-nature':
        cls = next(iter(source_dir.rglob('sn-jnl.cls')), None)
        bst = next(iter(source_dir.rglob('sn-nature.bst')), None)
        main = next(iter(source_dir.rglob(str(template.get('main_tex') or 'sn-article.tex'))), None)
        if not cls:
            failures.append('missing Springer Nature sn-jnl.cls sidecar')
        if not bst:
            failures.append('missing Springer Nature sn-nature.bst bibliography style')
        if main:
            main_text = main.read_text(encoding='utf-8', errors='replace')
            if 'sn-nature' not in main_text:
                failures.append('Springer Nature main template does not expose sn-nature option')
            active = re.search(r'(?m)^\s*\\documentclass(?:\[([^\]]*)\])?\{sn-jnl\}', main_text)
            if not active:
                failures.append('Springer Nature main template has no active sn-jnl documentclass')
            else:
                active_options = {part.strip() for part in (active.group(1) or '').split(',') if part.strip()}
                missing = [option for option in ['pdflatex', 'sn-nature'] if option not in active_options]
                if missing:
                    failures.append('Springer Nature active documentclass is missing options: ' + ', '.join(missing))
        else:
            failures.append('missing Springer Nature main template tex')
    return {'status': 'pass' if not failures else 'block', 'failures': failures, 'files': files}




def existing_verified_source(source_dir: Path, requirements: dict[str, Any]) -> dict[str, Any] | None:
    if not source_dir.exists() or not source_dir.is_dir() or not any(source_dir.iterdir()):
        return None
    option_selection = select_springer_nature_template_option(source_dir, requirements)
    validation = validate_template_source(source_dir, requirements)
    if validation.get('status') != 'pass':
        return None
    template = requirements.get('template') if isinstance(requirements.get('template'), dict) else {}
    expected_commit = str(template.get('verified_repository_commit') or requirements.get('official_repository_commit') or '').strip()
    source_meta = load_json(source_dir.parent / 'template_source.json', {})
    actual_commit = ''
    git_proc = run_git_checked(['git', '-C', str(source_dir), 'rev-parse', 'HEAD'], cwd=source_dir.parent, timeout=20)
    if git_proc.returncode == 0 and git_proc.stdout.strip():
        actual_commit = git_proc.stdout.strip()
    elif isinstance(source_meta, dict):
        actual_commit = str(source_meta.get('source_commit') or '').strip()
    repo_url = str(template.get('verified_repository_url') or template.get('repository_url') or template.get('official_source_url') or '')
    if expected_commit and repo_url and 'github.com/' in repo_url and actual_commit != expected_commit:
        return None
    result = {
        'source_kind': 'existing_verified_source',
        'source_url': repo_url,
        'source_commit': actual_commit or expected_commit,
        'source_subdir': str(template.get('verified_directory_hint') or template.get('directory_hint') or '').strip('/'),
        'existing_source_reused': True,
    }
    if option_selection:
        result['template_option_selection'] = option_selection
    return result



def select_springer_nature_template_option(source_dir: Path, requirements: dict[str, Any]) -> dict[str, Any]:
    template = requirements.get('template') if isinstance(requirements.get('template'), dict) else {}
    if str(template.get('family') or '').lower() != 'springer-nature':
        return {'selected': False}
    main_name = str(template.get('main_tex') or 'sn-article.tex')
    main = next(iter(source_dir.rglob(main_name)), None)
    if not main or not main.is_file():
        return {'selected': False, 'error': 'missing Springer Nature main template'}
    original = main.read_text(encoding='utf-8', errors='replace')
    lines = original.splitlines()
    out: list[str] = []
    selected = False
    found_nature_option = False
    for line in lines:
        uncommented = re.sub(r'^\s*%+\s*', '', line).strip()
        is_sn_jnl_documentclass = uncommented.startswith('\\documentclass[') and 'sn-jnl' in uncommented
        if not is_sn_jnl_documentclass:
            out.append(line)
            continue
        if 'sn-nature' in uncommented and 'pdflatex' in uncommented:
            out.append(re.sub(r'^\s*%+\s*', '', line))
            selected = True
            found_nature_option = True
        elif line.lstrip().startswith('\\documentclass['):
            out.append('%%' + line)
        else:
            out.append(line)
    if selected:
        main.write_text("\n".join(out) + "\n", encoding='utf-8')
    return {
        'selected': selected,
        'found_nature_option': found_nature_option,
        'main_tex': str(main),
        'option': 'pdflatex,sn-nature',
    }


def sync_from_repository(template: dict[str, Any], source_dir: Path) -> dict[str, Any]:
    repo_url = str(template.get('repository_url') or template.get('official_source_url') or '').strip()
    if not repo_url:
        raise RuntimeError('template repository_url is empty')
    tmp_root = Path(tempfile.mkdtemp(prefix='venue_template_repo_'))
    try:
        cache_candidates = [Path('/tmp/iclr_master_template_check_codex')] if use_template_cache() else []
        repo_dir = None
        for candidate in cache_candidates:
            if not candidate.exists():
                continue
            remote = run_git_checked(['git', '-C', str(candidate), 'remote', 'get-url', 'origin'], cwd=tmp_root, timeout=20)
            if remote.returncode == 0 and repo_url.replace('.git', '') in remote.stdout.strip().replace('.git', ''):
                repo_dir = candidate
                break
        if repo_dir is None:
            repo_dir = tmp_root / 'repo'
            env = os.environ.copy()
            env['GIT_TERMINAL_PROMPT'] = '0'
            proc = run_git_checked(['git', 'clone', '--depth=1', repo_url, str(repo_dir)], cwd=tmp_root, env=env, timeout=int(os.environ.get('VENUE_GIT_TIMEOUT_SEC', '60') or 60))
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or 'git clone failed').strip())
        directory_hint = str(template.get('directory_hint') or '').strip().strip('/')
        candidates = [repo_dir / directory_hint] if directory_hint else []
        main_tex = str(template.get('main_tex') or '').strip()
        if main_tex:
            candidates.extend(path.parent for path in repo_dir.rglob(main_tex) if path.is_file())
        required = [str(item) for item in template.get('required_files', []) if str(item).strip()]
        for name in required:
            candidates.extend(path.parent for path in repo_dir.rglob(name) if path.is_file())
        candidates.append(repo_dir)
        chosen = next((path for path in candidates if path.exists()), None)
        if not chosen:
            raise RuntimeError('no template directory found in repository')
        copy_source_dir(chosen, source_dir)
        commit_proc = run_git_checked(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'], cwd=tmp_root, timeout=20)
        commit = commit_proc.stdout.strip() if commit_proc.returncode == 0 else ''
        return {'source_kind': 'repository', 'source_url': repo_url, 'source_commit': commit, 'source_subdir': str(chosen.relative_to(repo_dir)) if chosen != repo_dir else ''}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def sync_from_archive(archive_url: str, archive_dir: Path, source_dir: Path, directory_hint: str = '') -> dict[str, Any]:
    archive_name = Path(archive_url.split('?', 1)[0]).name or 'venue-template.zip'
    if not any(archive_name.lower().endswith(ext) for ext in ['.zip', '.tar.gz', '.tgz', '.tar']):
        archive_name = 'venue-template.zip'
    archive_path = download_binary(archive_url, archive_dir / archive_name)
    tmp_root = Path(tempfile.mkdtemp(prefix='venue_template_archive_'))
    try:
        unpack_archive(archive_path, tmp_root)
        selected = tmp_root / directory_hint.strip().strip('/') if directory_hint else tmp_root
        if not selected.exists():
            selected = tmp_root
        if selected == tmp_root and directory_hint:
            fallback = next((path for path in tmp_root.rglob(Path(directory_hint).name) if path.is_dir()), None)
            if fallback:
                selected = fallback
        copy_source_dir(selected, source_dir)
        return {'source_kind': 'archive', 'source_url': archive_url, 'archive_path': str(archive_path), 'source_subdir': directory_hint or (str(selected.relative_to(tmp_root)) if selected != tmp_root else '')}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def discover_archive(info: dict[str, Any], explicit_url: str = '') -> tuple[str, str]:
    candidates: list[str] = []
    if explicit_url:
        candidates.append(explicit_url)
    elif info.get('official_archive_url'):
        candidates.append(str(info.get('official_archive_url')))
    else:
        for query in info.get('queries', []):
            candidates.extend(search_duckduckgo(str(query)))
    candidates = filter_candidate_urls(candidates, info.get('domains', []))
    for candidate in candidates[:16]:
        lowered = candidate.lower()
        if any(lowered.endswith(ext) for ext in ['.zip', '.tar.gz', '.tgz', '.tar']):
            return candidate, ''
        try:
            html = fetch_url(candidate)
        except Exception:
            continue
        links = filter_candidate_urls(find_download_links(candidate, html), info.get('domains', []))
        if links:
            return links[0], candidate
    raise RuntimeError('Unable to locate an official venue template archive.')


def fail(project: str, venue: str, venue_root: Path, report: Path, metadata: dict[str, Any], status: str, error: Exception | str, recovery: list[str]) -> None:
    metadata['status'] = status
    metadata['error'] = str(error)
    metadata['fallback_template'] = False
    write_json(venue_root / 'template_source.json', metadata)
    update_pipeline_state(project, {
        'template_fetched': False,
        'template_source': metadata,
        'template_fetch_error': str(error),
        'template_fetch_report': str(report),
        'template_fallback': '',
    }, venue=venue)
    make_failure_report(report, 'Template Fetch Report', [
        f'venue: {venue}',
        f'status: {status}',
        f'error: {error}',
    ], recovery)
    print(report)
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description='Fetch the current official venue LaTeX template package for writing.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', required=True)
    parser.add_argument('--url', default='')
    parser.add_argument('--archive-path', default='')
    args = parser.parse_args()

    os.environ['PROJECT_ID'] = args.project
    paper = ensure_paper_dirs(args.project)
    info = venue_info(args.venue)
    venue_slug = info['slug']
    venue_root = paper['venue_dir'] / venue_slug
    archive_dir = venue_root / 'downloads'
    source_dir = venue_root / 'source'
    venue_root.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    report = venue_root / 'template_fetch_report.md'
    requirements = load_requirements(args.project, args.venue)
    template = requirements.get('template') if isinstance(requirements.get('template'), dict) else {}
    metadata: dict[str, Any] = {
        'venue': args.venue,
        'venue_slug': venue_slug,
        'source_dir': str(source_dir),
        'status': 'started',
        'manual_override_used': False,
        'fallback_template': False,
        'venue_requirements_path': str(venue_root / 'venue_requirements.json') if requirements else '',
    }

    if not args.archive_path and not args.url:
        existing = existing_verified_source(source_dir, requirements)
        if existing:
            metadata.update(existing)

    try:
        if metadata.get('source_kind') == 'existing_verified_source':
            pass
        elif args.archive_path:
            archive_path = Path(args.archive_path).expanduser().resolve()
            if not archive_path.exists():
                raise FileNotFoundError(f'Provided archive path does not exist: {archive_path}')
            if source_dir.exists():
                shutil.rmtree(source_dir)
            source_dir.mkdir(parents=True, exist_ok=True)
            unpack_archive(archive_path, source_dir)
            metadata.update({'source_kind': 'manual_archive', 'archive_path': str(archive_path), 'manual_override_used': True})
        elif template.get('repository_url') or (template.get('official_source_url') and 'github.com/' in str(template.get('official_source_url'))):
            metadata.update(sync_from_repository(template, source_dir))
        elif template.get('archive_url') or args.url:
            archive_url = args.url or str(template.get('archive_url') or '')
            metadata.update(sync_from_archive(archive_url, archive_dir, source_dir, str(template.get('directory_hint') or '')))
        else:
            archive_url, page_url = discover_archive(info, explicit_url=args.url)
            metadata.update(sync_from_archive(archive_url, archive_dir, source_dir))
            metadata['selected_page_url'] = page_url
    except Exception as exc:
        fail(args.project, args.venue, venue_root, report, metadata, 'failed-official-template-fetch', exc, [
            'Run scripts/resolve_venue_requirements.py so venue-intelligence records the latest official template source first.',
            'Provide a user-approved official archive via --archive-path only if automatic official-source discovery is unavailable.',
            'The workflow must not emit a local minimal fallback as a venue-compliant template.',
        ])

    option_selection = select_springer_nature_template_option(source_dir, requirements)
    if option_selection:
        metadata['template_option_selection'] = option_selection
    validation = validate_template_source(source_dir, requirements)
    if validation.get('status') != 'pass':
        fail(args.project, args.venue, venue_root, report, metadata, 'failed-template-validation', '; '.join(validation.get('failures') or []), [
            'Refresh venue_requirements.json from official sources and retry template fetch.',
            'Check required_files/required_markers in venue_requirements.json against the official author kit.',
        ])

    metadata.update({
        'status': 'ok',
        'template_validation': validation,
        'official_template': True,
    })
    write_json(venue_root / 'template_source.json', metadata)
    update_pipeline_state(args.project, {
        'template_fetched': True,
        'template_source': metadata,
        'template_fetch_error': '',
        'template_fetch_report': str(report),
        'template_fallback': '',
    }, venue=args.venue)
    files = validation.get('files') if isinstance(validation.get('files'), dict) else {}
    make_failure_report(report, 'Template Fetch Report', [
        f'venue: {args.venue}',
        'status: ok-official-template',
        f'source_dir: {source_dir}',
        f'source_kind: {metadata.get("source_kind", "")}',
        f'source_url: {metadata.get("source_url", "")}',
        f'file_count: {len(files)}',
    ], [])
    print(source_dir)


if __name__ == '__main__':
    main()
