#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from llm_client import call_llm, llm_available, llm_disabled_reason
from project_paths import ROOT, build_paths, load_project_config

PROMPT = ROOT / 'prompts' / 'wiki_compiler.md'


def extract_section(text: str, title: str) -> str:
    marker = f'## {title}'
    if marker not in text:
        return ''
    after = text.split(marker, 1)[1]
    chunks = after.split('\n## ', 1)
    section = chunks[0].strip()
    return section


def rewrite_report(paths, llm_text: str) -> list[Path]:
    targets = []
    mapping = {
        'Overview': paths.wiki_overview,
        'Field Map': paths.wiki_synthesis / 'field-map.md',
        'Shared Assumptions': paths.wiki_synthesis / 'shared-assumptions.md',
        'Confirmed Gaps': paths.wiki_gaps / 'confirmed-gaps.md',
        'Hypotheses': paths.wiki_gaps / 'hypotheses.md',
        'Open Questions': paths.wiki_gaps / 'questions.md',
        'Loop Summary': paths.reports / 'loop_summary.md',
    }
    for title, path in mapping.items():
        section = extract_section(llm_text, title)
        if section:
            path.write_text(f'# {title}\n\n{section}\n', encoding='utf-8')
            targets.append(path)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    if not llm_available(cfg):
        print(llm_disabled_reason(cfg))
        return 1

    prompt_text = PROMPT.read_text(encoding='utf-8') + f"\n\nProject root: {paths.root}\n"
    shared = paths.reports / 'shared_research.md'
    if shared.exists():
        prompt_text += '\n\n# Shared Research Context\n\n' + shared.read_text(encoding='utf-8')[:50000]

    system = 'You are an autonomous research synthesis agent. Produce concise, skeptical, reusable Markdown sections.'
    result = call_llm(prompt_text, cfg, system_prompt=system)
    content = result.get('content', '')
    log = {
        'provider': result.get('provider', ''),
        'model': result.get('model', ''),
        'content': content,
    }
    (paths.logs / 'llm_compile.log').write_text(json.dumps(log, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    targets = rewrite_report(paths, content)
    print(json.dumps({'written': [str(path) for path in targets], 'provider': result.get('provider', ''), 'model': result.get('model', '')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
