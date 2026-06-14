#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from pipeline_guard import guard_fresh_base_blocker_entry

from paper_common import (
    choose_compiler,
    compiler_inventory,
    ensure_paper_dirs,
    install_latex_hint,
    load_json,
    make_failure_report,
    slugify,
    try_install_latex_toolchain,
    update_pipeline_state,
    venue_template_profile,
)


def decode_output(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value or '')


def run_compile_command(cmd: list[str], cwd: Path, timeout: int = 900) -> dict[str, object]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {'command': cmd, 'return_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr, 'timed_out': False}
    except subprocess.TimeoutExpired as exc:
        return {
            'command': cmd,
            'return_code': 124,
            'stdout': decode_output(exc.stdout),
            'stderr': decode_output(exc.stderr) + f'\nTimed out after {timeout}s',
            'timed_out': True,
        }


def bibtex_result_is_usable_for_springer_nature(result: dict[str, object], output_dir: Path) -> bool:
    if int(result.get('return_code') or 0) == 0:
        return True
    bbl = output_dir / 'paper.bbl'
    if not bbl.is_file() or bbl.stat().st_size <= 0:
        return False
    log = str(result.get('stdout') or '') + '\n' + str(result.get('stderr') or '')
    if 'sn-nature.bst' not in log or "can't pop an empty literal stack" not in log:
        return False
    return bool(re.search(r'\\bibitem\{[^{}]+\}', bbl.read_text(encoding='utf-8', errors='replace')))


def compile_springer_nature_preview(output_dir: Path, tex_name: str) -> tuple[int, list[dict[str, object]], str]:
    commands: list[dict[str, object]] = []
    commands.append(run_compile_command(['pdflatex', '-interaction=nonstopmode', '-halt-on-error', tex_name], output_dir))
    if int(commands[-1].get('return_code') or 0) not in {0, 1} and not (output_dir / 'paper.aux').is_file():
        return int(commands[-1].get('return_code') or 2), commands, 'initial pdflatex failed before aux generation'
    bib = run_compile_command(['bibtex', Path(tex_name).stem], output_dir)
    commands.append(bib)
    if not bibtex_result_is_usable_for_springer_nature(bib, output_dir):
        return int(bib.get('return_code') or 2), commands, 'bibtex failed and did not produce a usable Springer Nature .bbl'
    for _ in range(2):
        commands.append(run_compile_command(['pdflatex', '-interaction=nonstopmode', '-halt-on-error', tex_name], output_dir))
        if int(commands[-1].get('return_code') or 0) != 0:
            return int(commands[-1].get('return_code') or 2), commands, 'pdflatex failed after bibliography generation'
    return 0, commands, 'springer-nature preview compiled; sn-nature.bst BibTeX warnings retained in compile.log'


def command_log(commands: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for idx, item in enumerate(commands, start=1):
        parts.append(f"## command {idx}: {' '.join(str(x) for x in item.get('command', []))}\n")
        parts.append(f"return_code={item.get('return_code')} timed_out={item.get('timed_out', False)}\n")
        parts.append(str(item.get('stdout') or ''))
        stderr = str(item.get('stderr') or '')
        if stderr:
            parts.append('\n--- STDERR ---\n' + stderr)
        parts.append('\n')
    return '\n'.join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--venue', required=True)
    parser.add_argument('--auto-install-missing', action='store_true')
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        raise SystemExit(guard_rc)

    paper = ensure_paper_dirs(args.project)
    venue_slug = slugify(args.venue)
    output_dir = paper['output_dir'] / venue_slug
    tex_path = output_dir / 'paper.tex'
    if not tex_path.exists():
        raise SystemExit(f'Missing rendered TeX at {tex_path}. Run render_paper_tex.py first.')

    inventory = compiler_inventory()
    compiler = choose_compiler()
    install_result = {'success': False, 'attempts': []}
    if not compiler and args.auto_install_missing:
        install_result = try_install_latex_toolchain()
        inventory = compiler_inventory()
        compiler = choose_compiler()

    report = output_dir / 'compile_report.md'
    if not compiler:
        hints = install_latex_hint()
        bullets = [
            'status: missing-toolchain',
            f'auto_install_requested: {args.auto_install_missing}',
        ]
        bullets.extend([f'{name}: {available}' for name, available in inventory.items()])
        recovery = [f'`{hint}`' for hint in hints] if hints else ['Install latexmk, pdflatex or tectonic.']
        if install_result['attempts']:
            recovery.append(f'Inspect attempted installs in `{report}` before retrying.')
        make_failure_report(report, 'Paper Compile Report', bullets, recovery)
        if install_result['attempts']:
            with report.open('a', encoding='utf-8') as handle:
                handle.write('\n## Auto-Install Attempts\n\n')
                for attempt in install_result['attempts']:
                    handle.write(f"- command: `{attempt['command']}` return_code={attempt['return_code']}\n")
        update_pipeline_state(args.project, {
            'pdf_ready': False,
            'compile_report': str(report),
            'latex_missing': True,
            'compiler': '',
        }, venue=args.venue)
        print(report)
        raise SystemExit(2)

    profile = venue_template_profile(args.venue, project=args.project)
    commands: list[dict[str, object]] = []
    compile_note = ''
    if profile.get('family') == 'springer-nature':
        return_code, commands, compile_note = compile_springer_nature_preview(output_dir, tex_path.name)
        compiler = 'pdflatex+bibtex(springer-nature-preview)'
    else:
        if compiler == 'latexmk':
            cmd = ['latexmk', '-g', '-pdf', '-interaction=nonstopmode', '-halt-on-error', tex_path.name]
        elif compiler == 'tectonic':
            cmd = ['tectonic', tex_path.name]
        else:
            cmd = ['pdflatex', '-interaction=nonstopmode', '-halt-on-error', tex_path.name]
        commands = [run_compile_command(cmd, output_dir)]
        return_code = int(commands[-1].get('return_code') or 0)
    log_path = output_dir / 'compile.log'
    log_path.write_text(command_log(commands), encoding='utf-8')
    pdf_path = output_dir / 'paper.pdf'
    make_failure_report(report, 'Paper Compile Report', [
        f'compiler: {compiler}',
        f'return_code: {return_code}',
        f'pdf_ready: {pdf_path.exists()}',
        f'log: {log_path}',
        f'note: {compile_note}',
    ], [])

    update_pipeline_state(args.project, {
        'compiler': compiler,
        'compile_report': str(report),
        'compile_log': str(log_path),
        'pdf_ready': pdf_path.exists(),
        'pdf_path': str(pdf_path) if pdf_path.exists() else '',
        'latex_missing': False,
    }, venue=args.venue)
    print(report)
    if return_code != 0:
        raise SystemExit(return_code)


if __name__ == '__main__':
    main()
