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
import os
import re
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry
from paper_common import (
    ensure_paper_dirs,
    find_main_tex,
    get_active_paper_state,
    load_json,
    markdown_to_latex,
    normalize_venue_front_matter,
    read_text,
    slugify,
    update_pipeline_state,
    validate_venue_template_format,
    venue_fallback_template,
    venue_slug_aliases,
    venue_template_profile,
    write_json,
    write_text,
)


def latest_markdown(*paths):
    existing = [path for path in paths if path.exists() and read_text(path).strip()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def strip_title_macros(preamble: str) -> str:
    patterns = [r'\\title\{.*?\}\n?', r'\\author\{.*?\}\n?', r'\\date\{.*?\}\n?', r'\\thanks\{.*?\}\n?']
    out = preamble
    for pattern in patterns:
        out = re.sub(pattern, '', out, flags=re.DOTALL)
    return out


def find_venue_template_source(paper: dict, venue: str) -> tuple[Path | None, list[dict]]:
    rejected: list[dict] = []
    for alias in venue_slug_aliases(venue):
        source_dir = paper['venue_dir'] / alias / 'source'
        if not source_dir.exists():
            continue
        candidates = []
        main_tex = find_main_tex(source_dir)
        if main_tex:
            candidates.append(main_tex)
        candidates.extend(sorted(source_dir.rglob('*.tex')))
        for candidate in candidates:
            text = read_text(candidate)
            validation = validate_venue_template_format(text, venue, project=args.project)
            if validation.get('status') == 'pass':
                return candidate, rejected
            rejected.append({'path': str(candidate), 'validation': validation})
    return None, rejected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', required=True)
    parser.add_argument('--title', default='')
    args = parser.parse_args()
    os.environ["PROJECT_ID"] = args.project

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(args.project)
    venue_slug = slugify(args.venue)
    output_dir = paper['output_dir'] / venue_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / 'render_report.md'

    state = get_active_paper_state(args.project, venue=args.venue)
    preview_tex = Path(str(state.get('conference_preview_tex') or state.get('rendered_tex') or ''))
    preview_pdf = Path(str(state.get('conference_preview_pdf') or state.get('pdf_path') or state.get('latest_preview_pdf') or ''))
    source_label = str(state.get('paper_content_source_label') or '')
    if state.get('conference_preview_ready') and preview_tex.is_file() and preview_pdf.is_file() and (source_label == 'workspace_final' or '/paper/writing/' in str(preview_tex)):
        write_text(
            report_path,
            '# Paper Render Report\n\n'
            f'- venue: {args.venue}\n'
            '- status: skipped-existing-writing-preview\n'
            f'- preserved_tex: {preview_tex}\n'
            f'- preserved_pdf: {preview_pdf}\n'
            '- reason: writing already produced the active conference preview; legacy Markdown rendering is not allowed to overwrite it.\n',
        )
        update_pipeline_state(
            args.project,
            {
                'rendered_tex': str(preview_tex),
                'render_ready': True,
                'render_report': str(report_path),
                'pdf_ready': True,
                'pdf_path': str(preview_pdf),
            },
            venue=args.venue,
        )
        print(report_path)
        return

    template_main, rejected_templates = find_venue_template_source(paper, args.venue)
    if template_main is not None:
        template_text = read_text(template_main)
        template_source_label = str(template_main)
    else:
        template_text = ''
        template_source_label = 'missing official venue template'
    template_validation = validate_venue_template_format(template_text, args.venue, project=args.project)
    if template_validation.get('status') != 'pass':
        write_text(
            report_path,
            '# Paper Render Report\n\n'
            f'- venue: {args.venue}\n'
            '- status: template-format-blocked\n'
            f'- template_source: {template_source_label}\n'
            f'- validation: {template_validation}\n'
            f'- rejected_templates: {rejected_templates}\n',
        )
        update_pipeline_state(
            args.project,
            {
                'render_ready': False,
                'render_report': str(report_path),
                'paper_venue_format_status': 'block',
                'paper_venue_format_validation': template_validation,
            },
            venue=args.venue,
        )
        print(report_path)
        raise SystemExit(2)

    generated_tex = output_dir / 'paper.tex'
    preamble, _, _ = template_text.partition('\\begin{document}')
    preamble = strip_title_macros(preamble)
    if preamble.strip() and not preamble.rstrip().endswith('\\begin{document}'):
        preamble = preamble.rstrip() + '\n\n'

    metadata = load_json(paper['paper_metadata'], {})
    draft_path = latest_markdown(paper['revised_md'], paper['draft_md']) or paper['draft_md']
    draft_text = read_text(draft_path)
    title = args.title or metadata.get('title', 'Research Paper Draft')
    body_latex = markdown_to_latex(draft_text)
    profile = venue_template_profile(args.venue, project=args.project)
    if profile.get('family') == 'springer-nature':
        tex = (
            preamble
            + '\\begin{document}\n'
            + f'\\title{{{title}}}\n'
            + '\\author{\\fnm{Anonymous} \\sur{Authors}}\n'
            + '\\abstract{TODO: Abstract.}\n'
            + '\\maketitle\n\n'
            + body_latex
            + '\\end{document}\n'
        )
    else:
        tex = (
            preamble
            + f'\\title{{{title}}}\n'
            + '\\author{Anonymous Authors}\n'
            + '\\date{}\n'
            + '\\begin{document}\n'
            + '\\maketitle\n\n'
            + body_latex
            + '\\end{document}\n'
        )
    tex, front_matter_changes = normalize_venue_front_matter(tex, args.venue, project=args.project)
    write_text(generated_tex, tex)
    output_validation = validate_venue_template_format(tex, args.venue, project=args.project)
    write_json(output_dir / 'render_metadata.json', {
        'venue': args.venue,
        'template_main': template_source_label,
        'template_profile': venue_template_profile(args.venue, project=args.project),
        'template_validation': template_validation,
        'output_validation': output_validation,
        'front_matter_normalization': front_matter_changes,
        'rejected_templates': rejected_templates,
        'generated_tex': str(generated_tex),
        'draft_source': str(draft_path),
    })
    report_path.write_text(
        '# Paper Render Report\n\n'
        f'- venue: {args.venue}\n'
        f'- template_main: {template_source_label}\n'
        f'- venue_template_format: {output_validation.get("status")}\n'
        f'- generated_tex: {generated_tex}\n'
        f'- draft_source: {draft_path}\n',
        encoding='utf-8',
    )
    update_pipeline_state(
        args.project,
        {
            'rendered_tex': str(generated_tex),
            'render_ready': output_validation.get('status') == 'pass',
            'render_report': str(report_path),
            'paper_venue_format_status': output_validation.get('status'),
            'paper_venue_format_profile': venue_template_profile(args.venue, project=args.project),
            'paper_venue_format_validation': output_validation,
            'venue_template_format_ready': output_validation.get('status') == 'pass',
        },
        venue=args.venue,
    )
    print(generated_tex)


if __name__ == '__main__':
    main()
