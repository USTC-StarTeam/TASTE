#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path

from project_paths import build_paths, load_project_config

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def ensure_machine_profile(project: str) -> dict:
    paths = build_paths(project)
    profile_path = paths.reports / 'machine_profile.json'
    if not profile_path.exists():
        script = Path(__file__).resolve().parent / 'detect_machine_profile.py'
        proc = run(['python3', str(script), '--project', project], cwd=WORKSPACE_ROOT)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr or proc.stdout or 'failed to detect machine profile')
    return load_json(profile_path)


def miniforge_installer_url() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == 'Linux':
        arch = 'aarch64' if 'aarch64' in machine or 'arm64' in machine else 'x86_64'
        filename = f'Miniforge3-Linux-{arch}.sh'
    elif system == 'Darwin':
        arch = 'arm64' if 'arm' in machine else 'x86_64'
        filename = f'Miniforge3-MacOSX-{arch}.sh'
    else:
        raise SystemExit(f'unsupported system for automatic local conda bootstrap: {system}')
    return f'https://github.com/conda-forge/miniforge/releases/latest/download/{filename}'


def choose_downloader(machine_profile: dict) -> str:
    downloads = machine_profile.get('dependencies', {}).get('cli', {})
    if downloads.get('curl', {}).get('available'):
        return 'curl'
    if downloads.get('wget', {}).get('available'):
        return 'wget'
    return ''


def discover_conda_executable(machine_profile: dict) -> str:
    candidates = []
    on_path = shutil.which('conda')
    if on_path:
        candidates.append(Path(on_path))
    cli_path = machine_profile.get('dependencies', {}).get('cli', {}).get('conda', {}).get('path', '')
    if cli_path:
        candidates.append(Path(cli_path))
    candidates.append(WORKSPACE_ROOT / '.runtime' / 'miniforge3' / 'bin' / 'conda')
    for base in [WORKSPACE_ROOT.parent / 'miniforge', WORKSPACE_ROOT.parent / 'miniforge3', Path.home() / 'miniforge3', Path.home() / 'miniconda3', Path.home() / 'anaconda3', Path('/opt/conda')]:
        candidates.append(base / 'bin' / 'conda')
    seen = set()
    for candidate in candidates:
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        if candidate.exists():
            return text
    return ''


def install_local_conda(machine_profile: dict) -> str:
    downloader = choose_downloader(machine_profile)
    if not downloader:
        raise SystemExit('missing curl and wget; cannot automatically install a local Miniforge runtime')
    runtime_dir = WORKSPACE_ROOT / '.runtime'
    installers = runtime_dir / 'installers'
    installers.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / 'miniforge3'
    installer_path = installers / 'miniforge3.sh'
    url = miniforge_installer_url()
    if not installer_path.exists():
        proc = run(['curl', '-L', url, '-o', str(installer_path)], cwd=WORKSPACE_ROOT) if downloader == 'curl' else run(['wget', '-O', str(installer_path), url], cwd=WORKSPACE_ROOT)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr or proc.stdout or 'failed to download Miniforge installer')
    if not target.exists():
        proc = run(['bash', str(installer_path), '-b', '-p', str(target)], cwd=WORKSPACE_ROOT)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr or proc.stdout or 'failed to install local Miniforge runtime')
    conda_exe = target / 'bin' / 'conda'
    if not conda_exe.exists():
        raise SystemExit(f'local Miniforge installation incomplete: missing {conda_exe}')
    return str(conda_exe)


def infer_channels(accelerator: dict) -> list[str]:
    backend = accelerator.get('backend', 'unknown') if isinstance(accelerator, dict) else 'unknown'
    if backend == 'nvidia':
        return ['-c', 'pytorch', '-c', 'nvidia', '-c', 'conda-forge']
    return ['-c', 'conda-forge']


def setup_py_declares_package(setup_py: Path) -> bool:
    try:
        text = setup_py.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return False
    declares_setup = bool(re.search(r'(^|[^A-Za-z0-9_])(?:setuptools\.)?setup\s*\(', text))
    imports_packaging = 'setuptools' in text or 'distutils' in text
    return declares_setup and imports_packaging


def repo_has_editable_package(repo: Path) -> bool:
    pyproject = repo / 'pyproject.toml'
    setup_py = repo / 'setup.py'
    if pyproject.exists():
        return True
    return setup_py.exists() and setup_py_declares_package(setup_py)


def verification_steps(env_name: str) -> list[list[str]]:
    return [['run', '-n', env_name, 'python', '-c', 'import torch, scipy, numpy, yaml; print("imports-ok"); print("cuda", torch.cuda.is_available())']]


def infer_install_steps(repo: Path, env_name: str, python_version: str, accelerator: dict, conda_exe: str = '') -> list[list[str]]:
    channels = infer_channels(accelerator)
    py_spec = f'python={python_version}' if python_version else 'python=3.10'
    steps: list[list[str]] = []
    if not conda_exe or not conda_env_exists(conda_exe, env_name):
        steps.append(['create', '-y', '-n', env_name, py_spec, 'pip', *channels])
    env_file = repo / 'environment.yml'
    req_file = repo / 'requirements.txt'
    if env_file.exists():
        steps.append(['env', 'update', '-n', env_name, '-f', str(env_file)])
    else:
        if req_file.exists():
            steps.append(['run', '-n', env_name, 'python', '-m', 'pip', 'install', '-r', str(req_file)])
        inferred = infer_pip_packages(repo)
        if inferred:
            steps.append(['run', '-n', env_name, 'python', '-m', 'pip', 'install', *inferred])
        if repo_has_editable_package(repo):
            steps.append(['run', '-n', env_name, 'python', '-m', 'pip', 'install', '-e', str(repo)])
    # Verify imports needed by the repo loader. CUDA availability is informative, not a hard gate for dataset probing.
    steps.extend(verification_steps(env_name))
    return steps


def conda_cmd(conda_exe: str, step: list[str]) -> list[str]:
    return [conda_exe, *step]


def command_record(cmd: list[str], proc: subprocess.CompletedProcess[str], reason: str = '') -> dict:
    record = {
        'command': ' '.join(cmd),
        'return_code': proc.returncode,
        'stdout': proc.stdout[-4000:],
        'stderr': proc.stderr[-4000:],
    }
    if reason:
        record['reason'] = reason
    return record


def is_python_pip_step(step: list[str], env_name: str) -> bool:
    return step[:6] == ['run', '-n', env_name, 'python', '-m', 'pip']


def ensure_env_python_pip(conda_exe: str, env_name: str) -> tuple[bool, list[dict]]:
    records: list[dict] = []
    check = [conda_exe, 'run', '-n', env_name, 'python', '-m', 'pip', '--version']
    proc = run(check, cwd=WORKSPACE_ROOT)
    records.append(command_record(check, proc, 'check-python-pip'))
    if proc.returncode == 0:
        return True, records

    repairs = [
        ([conda_exe, 'install', '-y', '-n', env_name, 'pip'], 'install-conda-pip'),
        ([conda_exe, 'run', '-n', env_name, 'python', '-m', 'ensurepip', '--upgrade'], 'ensurepip-upgrade'),
    ]
    for repair_cmd, reason in repairs:
        repair_proc = run(repair_cmd, cwd=WORKSPACE_ROOT)
        records.append(command_record(repair_cmd, repair_proc, reason))
        verify_proc = run(check, cwd=WORKSPACE_ROOT)
        records.append(command_record(check, verify_proc, f'verify-after-{reason}'))
        if verify_proc.returncode == 0:
            return True, records
    return False, records


def append_execution_log(lines: list[str], record: dict, heading: str | None = None) -> None:
    title = heading or record.get('reason') or record.get('command', 'command')
    lines.extend([
        f"\n## {title}\n\n",
        f"`{record.get('command', '')}`\n\n",
        '```\n',
        record.get('stdout', ''),
        '\n--- STDERR ---\n',
        record.get('stderr', ''),
        '\n```\n',
    ])


def missing_import_from_output(output: str) -> str:
    marker = "No module named '"
    if marker not in output:
        return ''
    tail = output.split(marker, 1)[1]
    return tail.split("'", 1)[0].split('.')[0].strip()


def install_missing_import(conda_exe: str, env_name: str, import_name: str) -> subprocess.CompletedProcess[str] | None:
    package = IMPORT_TO_PACKAGE.get(import_name)
    if not package:
        return None
    return run([conda_exe, 'run', '-n', env_name, 'python', '-m', 'pip', 'install', package], cwd=WORKSPACE_ROOT)


IMPORT_TO_PACKAGE = {
    'numpy': 'numpy',
    'scipy': 'scipy',
    'torch': 'torch',
    'yaml': 'pyyaml',
    'tqdm': 'tqdm',
    'sklearn': 'scikit-learn',
    'pandas': 'pandas',
    'matplotlib': 'matplotlib',
    'wandb': 'wandb',
    'torchmetrics': 'torchmetrics',
    'faiss': 'faiss-cpu',
}


def conda_env_exists(conda_exe: str, env_name: str) -> bool:
    proc = run([conda_exe, 'env', 'list', '--json'])
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return False
    needle = f'/envs/{env_name}'
    return any(str(path).endswith(needle) or Path(path).name == env_name for path in data.get('envs', []))


def infer_python_imports(repo: Path) -> list[str]:
    imports = set()
    for path in sorted(repo.rglob('*.py')):
        if '.git' in path.parts or '__pycache__' in path.parts:
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('import '):
                name = stripped.split()[1].split('.')[0].split(',')[0]
                imports.add(name)
            elif stripped.startswith('from '):
                name = stripped.split()[1].split('.')[0]
                imports.add(name)
    return sorted(imports)


def _readme_files(repo: Path) -> list[Path]:
    return [path for path in sorted(repo.glob('README*')) if path.is_file()]


def _pip_package_from_token(token: str) -> str:
    clean = token.strip().strip('`').strip("\"'")
    if not clean or clean.startswith('-'):
        return ''
    if clean in {'pip', 'install', 'python', '-m'}:
        return ''
    if clean.startswith(('./', '../', 'http://', 'https://', 'git+', '$')):
        return ''
    if clean.endswith(('.txt', '.yml', '.yaml', '.toml', '.sh')):
        return ''
    if any(char in clean for char in ['/', '\\']) or clean in {'\\'}:
        return ''
    return clean.rstrip('.,;')


def infer_readme_pip_packages(repo: Path) -> list[str]:
    packages: list[str] = []
    for readme in _readme_files(repo):
        try:
            lines = readme.read_text(encoding='utf-8', errors='ignore').splitlines()
        except Exception:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or 'pip install' not in line.lower():
                continue
            line = re.sub(r'^```.*$', '', line).strip()
            line = re.sub(r'^[>*$#\s]+', '', line).strip()
            line = line.replace('python -m pip install', 'pip install')
            match = re.search(r'\bpip\s+install\s+(.+)$', line, flags=re.IGNORECASE)
            if not match:
                continue
            tail = match.group(1).replace('\\', ' ')
            for token in re.split(r'\s+', tail):
                package = _pip_package_from_token(token)
                if package and package not in packages:
                    packages.append(package)
    return packages


def infer_pip_packages(repo: Path) -> list[str]:
    packages: list[str] = []
    for package in infer_readme_pip_packages(repo):
        if package not in packages:
            packages.append(package)
    imports = infer_python_imports(repo)
    for name in imports:
        pkg = IMPORT_TO_PACKAGE.get(name)
        if pkg and pkg not in packages:
            packages.append(pkg)
    # README install commands are authoritative when requirements files are absent;
    # inferred imports fill small gaps such as yaml -> pyyaml.
    return packages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--repo-path', required=True)
    parser.add_argument('--env-name')
    parser.add_argument('--python-version')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--update-project-config', action='store_true')
    parser.add_argument('--auto-install-missing', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='Only verify an existing environment; never create envs or install packages.')
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo = Path(args.repo_path).resolve()
    if not repo.exists():
        raise SystemExit(f'Repo path does not exist: {repo}')

    machine = ensure_machine_profile(args.project)
    accelerator = machine.get('accelerator', {}) if isinstance(machine, dict) else {}
    env_name = args.env_name or f"{args.project}_{repo.name}".replace('-', '_')
    python_version = args.python_version or cfg.get('environment', {}).get('python_version') or '3.10'
    conda_exe = discover_conda_executable(machine)
    auto_installed = False
    missing_runtime_tools: list[str] = []
    if not conda_exe:
        missing_runtime_tools.append('conda')
        if args.auto_install_missing:
            conda_exe = install_local_conda(machine)
            auto_installed = True
    if not conda_exe and not choose_downloader(machine):
        missing_runtime_tools.append('curl_or_wget')

    env_exists_before = conda_env_exists(conda_exe, env_name) if conda_exe else False
    env_exists = env_exists_before
    if args.verify_only:
        steps = verification_steps(env_name) if conda_exe and env_exists else []
    else:
        steps = infer_install_steps(repo, env_name, python_version, accelerator, conda_exe) if conda_exe else []
    out_json = paths.state / 'repo_env_bootstrap.json'
    out_md = paths.reports / 'repo_env_bootstrap.md'
    payload = {
        'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
        'repo_path': str(repo),
        'env_name': env_name,
        'python_version': python_version,
        'conda_executable': conda_exe,
        'detected_backend': accelerator.get('backend', 'unknown') if isinstance(accelerator, dict) else 'unknown',
        'detected_cuda': accelerator.get('cuda_version', '') if isinstance(accelerator, dict) else '',
        'prepare_only': args.prepare_only,
        'auto_install_missing': args.auto_install_missing,
        'verify_only': args.verify_only,
        'env_exists': env_exists,
        'env_exists_before': env_exists_before,
        'auto_installed_local_conda': auto_installed,
        'missing_runtime_tools': missing_runtime_tools,
        'steps': [' '.join(conda_cmd(conda_exe, step)) for step in steps] if conda_exe else [],
        'executed': [],
        'status': 'blocked' if args.verify_only and conda_exe and not env_exists else ('prepared' if conda_exe else 'blocked'),
    }

    lines = [
        '# Repo Environment Bootstrap Plan\n\n',
        f'- repo: {repo}\n',
        f'- env_name: {env_name}\n',
        f'- python_version: {python_version}\n',
        f"- conda_executable: {conda_exe or 'missing'}\n",
        f"- detected_backend: {payload['detected_backend']}\n",
        f"- detected_cuda: {payload['detected_cuda']}\n",
        f'- auto_install_missing: {args.auto_install_missing}\n',
        f'- verify_only: {args.verify_only}\n',
        f'- env_exists_before: {env_exists_before}\n',
        '- portability rule: adapt to the detected machine profile rather than assuming a fixed GPU model, CUDA version, package manager, or conda base path.\n',
        '- install strategy: repo-first-adaptive using conda with verification commands after installation.\n\n',
    ]

    if not conda_exe:
        lines.append('## Blockers\n')
        lines.append('- conda runtime is not currently available.\n')
        lines.append(f'- remediation: install/configure conda externally, then run `python3 scripts/bootstrap_repo_env.py --project {args.project} --repo-path {repo} --verify-only --prepare-only`\n')
    elif args.verify_only and not env_exists:
        lines.append('## Blockers\n')
        lines.append(f'- configured environment `{env_name}` does not exist; TASTE will not create or mutate environments automatically.\n')
        lines.append('- remediation: create/configure the environment outside TASTE, or run bootstrap explicitly without `--verify-only` if you intentionally want repo dependency installation.\n')
    else:
        lines.append('## Planned Steps\n')
        for step in steps:
            lines.append(f"- `{' '.join(conda_cmd(conda_exe, step))}`\n")

    if not args.prepare_only and conda_exe and (not args.verify_only or env_exists):
        payload['status'] = 'running'
        python_pip_checked = False
        for step in steps:
            if is_python_pip_step(step, env_name) and not python_pip_checked:
                python_pip_checked = True
                pip_ready, pip_records = ensure_env_python_pip(conda_exe, env_name)
                payload['executed'].extend(pip_records)
                for record in pip_records:
                    append_execution_log(lines, record)
                if not pip_ready:
                    payload['status'] = 'failed'
                    payload['failed_step'] = pip_records[-1]['command'] if pip_records else 'python -m pip readiness'
                    payload['missing_import'] = 'pip'
                    break
            proc = run(conda_cmd(conda_exe, step), cwd=repo)
            step_record = command_record(conda_cmd(conda_exe, step), proc)
            payload['executed'].append(step_record)
            lines.extend([
                f"\n## {' '.join(conda_cmd(conda_exe, step))}\n\n",
                '```\n',
                proc.stdout,
                '\n--- STDERR ---\n',
                proc.stderr,
                '\n```\n',
            ])
            if proc.returncode != 0:
                combined = (proc.stdout or '') + '\n' + (proc.stderr or '')
                missing_import = missing_import_from_output(combined)
                if missing_import and args.auto_install_missing:
                    install_proc = install_missing_import(conda_exe, env_name, missing_import)
                    if install_proc is not None:
                        package = IMPORT_TO_PACKAGE.get(missing_import, missing_import)
                        retry_record = {
                            'command': ' '.join([conda_exe, 'run', '-n', env_name, 'python', '-m', 'pip', 'install', package]),
                            'return_code': install_proc.returncode,
                            'stdout': install_proc.stdout[-4000:],
                            'stderr': install_proc.stderr[-4000:],
                            'reason': f'auto-install-missing-import:{missing_import}',
                        }
                        payload['executed'].append(retry_record)
                        lines.extend([
                            f"\n## Auto-install missing import `{missing_import}`\n\n",
                            '```\n', install_proc.stdout, '\n--- STDERR ---\n', install_proc.stderr, '\n```\n',
                        ])
                        if install_proc.returncode == 0:
                            proc = run(conda_cmd(conda_exe, step), cwd=repo)
                            retry_verify = {
                                'command': ' '.join(conda_cmd(conda_exe, step)),
                                'return_code': proc.returncode,
                                'stdout': proc.stdout[-4000:],
                                'stderr': proc.stderr[-4000:],
                                'reason': f'retry-after-install:{missing_import}',
                            }
                            payload['executed'].append(retry_verify)
                            lines.extend([
                                f"\n## Retry {' '.join(conda_cmd(conda_exe, step))}\n\n",
                                '```\n', proc.stdout, '\n--- STDERR ---\n', proc.stderr, '\n```\n',
                            ])
                if proc.returncode != 0:
                    payload['status'] = 'failed'
                    payload['failed_step'] = ' '.join(conda_cmd(conda_exe, step))
                    payload['missing_import'] = missing_import
                    break
        else:
            payload['status'] = 'completed'

    if conda_exe:
        env_exists_after = conda_env_exists(conda_exe, env_name)
        payload['env_exists_after'] = env_exists_after
        payload['env_exists'] = env_exists_after

    if payload['status'] == 'completed' and args.update_project_config:
        data = json.loads(paths.config.read_text(encoding='utf-8'))
        data['conda_env'] = env_name
        data.setdefault('environment', {})['conda_base_hint'] = str(Path(conda_exe).resolve().parents[1])
        paths.config.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    out_md.write_text(''.join(lines), encoding='utf-8')
    print(out_md)


if __name__ == '__main__':
    main()

