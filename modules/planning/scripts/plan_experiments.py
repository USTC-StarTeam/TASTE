#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
from pathlib import Path

from project_paths import build_paths, load_project_config

TRIAL_FOCUS_LIBRARY = [
    'baseline reproduction on the selected repo and dataset',
    'targeted hyperparameter sweep aimed at the current weakest slice',
    'module-coordination or implementation sanity check with the same benchmark',
    'focused repair run after inspecting bad cases',
]
SHELL_META_TOKENS = ('|', '&', ';', '<', '>', '(', ')', '$', '`', '\n')


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def load_mapping(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def slugify(value: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', value.strip().lower()).strip('_')
    return slug or 'item'


def discover_repo(repo_rows: list[dict], repo_name: str | None, repo_path: str | None) -> dict:
    if repo_path:
        target = Path(repo_path)
        match = next((row for row in repo_rows if row.get('local_path') == str(target)), None)
        return match or {
            'name': target.name,
            'url': 'local',
            'local_path': str(target),
            'summary': 'manually provided repo path',
            'score': 0,
            'notes': 'selected from --repo-path',
        }
    if repo_name:
        for row in repo_rows:
            if row.get('name') == repo_name:
                return row
        raise SystemExit(f'No repo candidate named {repo_name}')
    for row in repo_rows:
        if row.get('local_path'):
            return row
    return repo_rows[0] if repo_rows else {}


def discover_dataset(dataset_rows: list[dict], dataset_name: str) -> dict:
    for row in dataset_rows:
        if row.get('name') == dataset_name:
            return row
    return {'name': dataset_name, 'available': False, 'notes': 'not registered yet'}


def load_machine_summary(paths) -> tuple[dict, int]:
    machine_path = paths.reports / 'machine_profile.json'
    if not machine_path.exists():
        return {}, 0
    machine = json.loads(machine_path.read_text(encoding='utf-8'))
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    gpus = accelerator.get('gpus', []) if isinstance(accelerator, dict) else []
    return machine, len(gpus)


def command_needs_shell(command: str) -> bool:
    return any(token in (command or '') for token in SHELL_META_TOKENS)


def infer_command_spec(repo_path: str | None, explicit_template: str | None, python_executable: str) -> tuple[str, list[str], str, str]:
    if explicit_template:
        normalized_template = explicit_template.strip()
        argv = []
        kind = 'shell'
        if not command_needs_shell(normalized_template):
            try:
                argv = shlex.split(normalized_template)
                if argv and argv[0] == 'python':
                    argv[0] = python_executable
                    normalized_template = shlex.join(argv)
                kind = 'argv'
            except ValueError:
                argv = []
        return normalized_template, argv, 'user-provided', kind
    if not repo_path:
        return '', [], 'missing-repo-path', 'shell'
    repo = Path(repo_path)
    for candidate in ('train.py', 'main.py', 'run.py'):
        if (repo / candidate).exists():
            return f'{python_executable} {candidate}', [python_executable, candidate], f'auto-detected:{candidate}', 'argv'
    return '', [], 'no-entrypoint-detected', 'shell'


def infer_env_name(project: str, repo: dict, explicit_env: str | None) -> str:
    if explicit_env:
        return explicit_env
    repo_name = repo.get('name') or Path(str(repo.get('local_path', project))).name
    return f"{slugify(project)}_{slugify(str(repo_name))}"


def build_trial_focuses(count: int) -> list[str]:
    if count <= len(TRIAL_FOCUS_LIBRARY):
        return TRIAL_FOCUS_LIBRARY[:count]
    focuses = TRIAL_FOCUS_LIBRARY[:]
    while len(focuses) < count:
        focuses.append(f'targeted follow-up attempt {len(focuses) + 1} after cross-method comparison')
    return focuses


def render_template(template: str, context: dict[str, object]) -> str:
    if not template:
        return ''
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace('{' + key + '}', str(value))
    return rendered


def load_quality_focus(paths) -> dict:
    paper_quality = load_mapping(paths.state / 'paper_quality.json')
    papers = paper_quality.get('papers', []) if isinstance(paper_quality, dict) else []
    best = next((row for row in papers if row.get('top_tier_readiness') == 'promising'), papers[0] if papers else None)
    if not best:
        return {
            'novelty_target': 'Define a precise delta over a strong baseline before scaling experiments.',
            'claim_target': 'State what benchmark movement would support the core claim.',
            'counterexample_target': 'List at least one slice or stress test that could falsify the claim.',
        }
    return {
        'paper_id': best.get('paper_id', ''),
        'novelty_target': f"Clarify how the method is meaningfully different from the nearest-neighbor work around {best.get('paper_id', '')}.",
        'claim_target': 'Tie every method to a claim that can be supported or weakened by the planned benchmark metric.',
        'counterexample_target': 'Identify the slices or failure settings most likely to break the central claim early.',
    }




def infer_method_role(method: str, cfg: dict) -> str:
    policy = cfg.get('experiment', {}).get('method_role_policy', {}) if isinstance(cfg, dict) else {}
    roles = policy.get('method_roles', {}) if isinstance(policy, dict) else {}
    if isinstance(roles, dict) and method in roles:
        return str(roles[method])
    prompt = (cfg.get('topic', '') + ' ' + cfg.get('user_prompt', '')).lower() if isinstance(cfg, dict) else ''
    lowered = method.lower()
    generic_control = ('baseline', 'control', 'ablation', 'reference', 'reproduction')
    generic_candidate = ('proposed', 'candidate', 'variant', 'ours', 'intervention', 'treatment')
    if any(token in lowered for token in generic_control):
        return 'control'
    if any(token in lowered for token in generic_candidate):
        return 'candidate'
    return 'unknown'


def build_method_contract(method: str, dataset_name: str, benchmark: str, metric: str, quality_focus: dict) -> dict:
    return {
        'novelty_hypothesis': f'{method} should create a non-trivial delta over the strongest baseline on {benchmark}, not just minor tuning noise.',
        'claim_to_test': f'If {method} is valid, it should improve {metric} on {dataset_name} under the same benchmark protocol.',
        'support_threshold': f'Need reproducible improvement on {metric} plus evidence on difficult slices, not only one global average.',
        'counterexample_test': quality_focus.get('counterexample_target', 'Test the weakest slice first.'),
        'bad_case_slices': [
            'worst-performing slice from the first executable baseline',
            'out-of-distribution or long-tail slice if available',
            'high-latency or high-context examples if relevant to the task',
        ],
        'continue_rule': 'Continue only if the method shows either a meaningful aggregate gain or a clearly better failure profile on hard slices.',
        'prune_rule': 'Prune or pause after repeated weak results if the novelty story is weak and the error profile is not more repairable than stronger alternatives.',
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--methods', nargs='+', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--benchmark', required=True)
    parser.add_argument('--metric', required=True)
    parser.add_argument('--repo-name')
    parser.add_argument('--repo-path')
    parser.add_argument('--env-name')
    parser.add_argument('--command-template')
    parser.add_argument('--metrics-path-template', default='{artifact_dir}/metrics.json')
    parser.add_argument('--bad-case-path-template', default='{artifact_dir}/bad_cases.json')
    parser.add_argument('--audit-path-template', default='{artifact_dir}/audit.json')
    parser.add_argument('--max-methods', type=int)
    parser.add_argument('--max-trials-per-method', type=int)
    parser.add_argument('--gpu-per-trial', type=int)
    parser.add_argument('--seed-start', type=int, default=1)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo_rows = load_json(paths.state / 'repo_candidates.json')
    dataset_rows = load_json(paths.state / 'dataset_registry.json')
    repo = discover_repo(repo_rows, args.repo_name, args.repo_path)
    dataset = discover_dataset(dataset_rows, args.dataset)
    machine, visible_gpus = load_machine_summary(paths)
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    quality_focus = load_quality_focus(paths)

    max_methods = args.max_methods or cfg.get('parallel_experiments', {}).get('max_concurrent_methods', 3)
    selected_methods = args.methods[:max_methods]
    planned_trials = args.max_trials_per_method or cfg.get('parallel_experiments', {}).get('max_concurrent_trials_per_method', 2)
    gpu_per_trial = args.gpu_per_trial if args.gpu_per_trial is not None else (1 if visible_gpus > 0 else 0)
    resource_bound_parallel = max(1, visible_gpus // max(gpu_per_trial, 1)) if visible_gpus and gpu_per_trial else max(1, len(selected_methods))
    max_parallel_trials = min(max(1, len(selected_methods)), resource_bound_parallel, max_methods)
    python_executable = cfg.get('python_executable', 'python3')
    command_template, command_argv_template, command_source, command_kind = infer_command_spec(repo.get('local_path'), args.command_template, python_executable)
    env_name = infer_env_name(args.project, repo, args.env_name)

    methods = []
    for rank, method in enumerate(selected_methods, start=1):
        method_slug = slugify(method)
        method_role = infer_method_role(method, cfg)
        trial_focuses = build_trial_focuses(planned_trials)
        contract = build_method_contract(method, args.dataset, args.benchmark, args.metric, quality_focus)
        trials = []
        for index, focus in enumerate(trial_focuses, start=1):
            artifact_dir = paths.artifacts / method_slug / f'trial_{index:02d}'
            context = {
                'project': args.project,
                'project_root': str(paths.root.resolve()),
                'repo_path': repo.get('local_path', ''),
                'repo_name': repo.get('name', ''),
                'method': method,
                'method_slug': method_slug,
                'dataset': args.dataset,
                'benchmark': args.benchmark,
                'metric': args.metric,
                'trial': index,
                'seed': args.seed_start + index - 1,
                'artifact_dir': str(artifact_dir.resolve()),
            }
            command = render_template(command_template, context)
            command_argv = [render_template(item, context) for item in command_argv_template] if command_argv_template else []
            trials.append({
                'experiment_id': f'{method_slug}_trial_{index:02d}',
                'trial_index': index,
                'seed': context['seed'],
                'focus': focus,
                'artifact_dir': str(artifact_dir.resolve()),
                'command': command,
                'command_argv': command_argv,
                'command_kind': command_kind,
                'command_source': command_source,
                'metrics_path': render_template(args.metrics_path_template, context),
                'bad_case_path': render_template(args.bad_case_path_template, context),
                'audit_path': render_template(args.audit_path_template, context),
                'status': 'planned',
                'result_summary': 'pending',
                'method_role': method_role,
                'comparison_role': method_role,
            })

        methods.append({
            'method': method,
            'method_slug': method_slug,
            'method_role': method_role,
            'comparison_role': method_role,
            'priority': rank,
            'status': 'planned',
            'decision': 'open',
            'repo_name': repo.get('name', ''),
            'repo_path': repo.get('local_path', ''),
            'env_name': env_name,
            'dataset': args.dataset,
            'benchmark': args.benchmark,
            'metric': args.metric,
            'gpu_per_trial': gpu_per_trial,
            'launch_ready': bool((command_template or command_argv_template) and repo.get('local_path')),
            'command_template': command_template,
            'command_argv_template': command_argv_template,
            'command_kind': command_kind,
            'command_source': command_source,
            'planned_trials': planned_trials,
            'claim_contract': contract,
            'trials': trials,
            'notes': '',
        })

    plan = {
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'project': args.project,
        'dataset': args.dataset,
        'benchmark': args.benchmark,
        'metric': args.metric,
        'selected_repo': {
            'name': repo.get('name', ''),
            'url': repo.get('url', ''),
            'local_path': repo.get('local_path', ''),
            'score': repo.get('score', 0),
            'notes': repo.get('notes', ''),
        },
        'selected_dataset': dataset,
        'environment': {
            'env_name': env_name,
            'python_version': cfg.get('environment', {}).get('python_version', ''),
            'gpu_backend': accelerator.get('backend', 'unknown') if isinstance(accelerator, dict) else 'unknown',
            'cuda_version': accelerator.get('cuda_version', '') if isinstance(accelerator, dict) else '',
        },
        'resource_plan': {
            'visible_gpu_count': visible_gpus,
            'gpu_per_trial': gpu_per_trial,
            'max_parallel_trials': max_parallel_trials,
            'max_methods_considered': max_methods,
            'resource_policy': cfg.get('parallel_experiments', {}).get('resource_policy', 'fit-to-visible-gpus'),
        },
        'decision_policy': {
            'min_followup_attempts': cfg.get('failure_analysis', {}).get('min_followup_attempts', 2),
            'early_drop_patience': cfg.get('failure_analysis', {}).get('early_drop_patience', 2),
            'max_total_attempts_per_method': cfg.get('failure_analysis', {}).get('max_total_attempts_per_method', 6),
            'require_bad_case_evidence_before_scaling': True,
            'require_claim_check_before_deepen': True,
        },
        'quality_focus': quality_focus,
        'methods': methods,
    }

    out_json = paths.state / 'parallel_plan.json'
    out_json.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    lines = [
        '# Parallel Experiment Plan\n\n',
        f"- created_at: {plan['created_at']}\n",
        f"- repo: {plan['selected_repo']['name']} ({plan['selected_repo']['local_path'] or 'no local path'})\n",
        f'- dataset: {args.dataset}\n',
        f'- benchmark: {args.benchmark}\n',
        f'- metric: {args.metric}\n',
        f'- env_name: {env_name}\n',
        f'- visible_gpu_count: {visible_gpus}\n',
        f'- max_parallel_trials: {max_parallel_trials}\n',
        f'- command_source: {command_source}\n',
        f'- command_kind: {command_kind}\n\n',
        '## Readiness Checks\n',
        f"- repo_registered: {bool(repo)}\n",
        f"- repo_path_available: {bool(plan['selected_repo']['local_path'])}\n",
        f"- dataset_registered: {dataset.get('name', '') == args.dataset}\n",
        f"- dataset_available: {dataset.get('available', False)}\n",
        f"- launch_ready_methods: {sum(1 for row in methods if row.get('launch_ready'))}/{len(methods)}\n\n",
        '## Research Quality Contract\n',
        f"- novelty_target: {quality_focus.get('novelty_target', '')}\n",
        f"- claim_target: {quality_focus.get('claim_target', '')}\n",
        f"- counterexample_target: {quality_focus.get('counterexample_target', '')}\n\n",
        '| Priority | Method | Launch Ready | GPU/Trial | Planned Trials | Command Kind | Command Source | Trial Focuses | Audit Contract |\n',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n',
    ]
    for row in methods:
        focuses = '; '.join(trial['focus'] for trial in row['trials'])
        lines.append(
            f"| {row['priority']} | {row['method']} | {row['launch_ready']} | {row['gpu_per_trial']} | {row['planned_trials']} | {row['command_kind']} | {row['command_source']} | {focuses} | metrics + bad_cases + audit.json |\n"
        )
        contract = row['claim_contract']
        lines.extend([
            f"\n### {row['method']} claim contract\n",
            f"- novelty_hypothesis: {contract['novelty_hypothesis']}\n",
            f"- claim_to_test: {contract['claim_to_test']}\n",
            f"- support_threshold: {contract['support_threshold']}\n",
            f"- counterexample_test: {contract['counterexample_test']}\n",
            f"- continue_rule: {contract['continue_rule']}\n",
            f"- prune_rule: {contract['prune_rule']}\n",
        ])
    (paths.planning / 'parallel_experiment_plan.md').write_text(''.join(lines), encoding='utf-8')
    print(paths.planning / 'parallel_experiment_plan.md')


if __name__ == '__main__':
    main()
