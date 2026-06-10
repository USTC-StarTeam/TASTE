#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from project_paths import build_paths, load_project_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    out = paths.reports / 'agent_upgrade_notes.md'
    out.write_text(
        '# High-Autonomy Agent Upgrade Notes\n\n'
        '## Environment Adaptation\n'
        '- Detect GPU/CUDA before first serious run.\n'
        '- Prefer repo-specific conda envs over a single shared base env.\n'
        '- Keep a prepare-only path so environment creation can be resumed safely.\n\n'
        '## Parallel Method Search\n'
        f"- Configured max concurrent methods: {cfg.get('parallel_experiments', {}).get('max_concurrent_methods', 3)}\n"
        f"- Configured max trials per method: {cfg.get('parallel_experiments', {}).get('max_concurrent_trials_per_method', 2)}\n"
        '- Search several plausible methods together, then compare and prune.\n\n'
        '## Deep Failure Analysis\n'
        '- Do not stop after one failed run.\n'
        '- Check hyperparameters, module coordination, implementation bugs, and bad-case slices.\n'
        '- But also stop wasting time on methods whose failure modes look fundamental.\n\n'
        '## Git Hygiene\n'
        '- Track core code, configs, and experiment metadata.\n'
        '- Ignore large artifacts, generated outputs, caches, and raw bulky assets.\n',
        encoding='utf-8'
    )
    print(out)


if __name__ == '__main__':
    main()
