#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from project_paths import build_paths, load_project_config
from paper_common import texlive_tool_candidates

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
WORKSPACE_ROOT = ROOT


def tool_candidates(name: str, cfg: dict | None = None) -> list[str]:
    candidates: list[str] = []
    on_path = shutil.which(name)
    if on_path:
        candidates.append(on_path)
    env_cfg = (cfg or {}).get('environment', {}) if isinstance(cfg, dict) else {}
    base_hint = str(env_cfg.get('conda_base_hint', '') or '').strip()
    roots = [WORKSPACE_ROOT, WORKSPACE_ROOT.parent, Path.home(), Path('/opt')]
    bases: list[Path] = []
    if base_hint:
        bases.append(Path(base_hint))
    for root in roots:
        for dirname in ['miniforge', 'miniforge3', 'miniconda', 'miniconda3', 'anaconda3', 'conda']:
            bases.append(root / dirname)
    for base in bases:
        candidates.append(str(base / 'bin' / name))
        candidates.append(str(base / 'condabin' / name))
    candidates.extend(str(candidate) for candidate in texlive_tool_candidates(name))
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def which_tool(name: str, cfg: dict | None = None) -> str:
    for candidate in tool_candidates(name, cfg):
        path = Path(candidate)
        if path.exists() and path.is_file():
            return str(path)
    return ''


def run(cmd: list[str], cfg: dict | None = None) -> subprocess.CompletedProcess[str] | None:
    exe = which_tool(cmd[0], cfg)
    if not exe:
        return None
    return subprocess.run([exe, *cmd[1:]], text=True, capture_output=True)

def read_os_release() -> dict[str, str]:
    path = Path('/etc/os-release')
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key] = value.strip().strip('"')
    return data


def detect_memory_gb() -> float | None:
    meminfo = Path('/proc/meminfo')
    if meminfo.exists():
        for line in meminfo.read_text(encoding='utf-8').splitlines():
            if line.startswith('MemTotal:'):
                parts = line.split()
                if len(parts) >= 2:
                    return round(int(parts[1]) / 1024 / 1024, 2)
    if shutil.which('sysctl'):
        proc = subprocess.run(['sysctl', '-n', 'hw.memsize'], text=True, capture_output=True)
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            return round(int(proc.stdout.strip()) / 1024 / 1024 / 1024, 2)
    return None


def conda_candidates(cfg: dict | None = None) -> list[Path]:
    env_cfg = (cfg or {}).get('environment', {}) if isinstance(cfg, dict) else {}
    bases: list[Path] = []
    hint = str(env_cfg.get('conda_base_hint', '') or '').strip()
    if hint:
        bases.append(Path(hint))
    roots = [WORKSPACE_ROOT, WORKSPACE_ROOT.parent, Path.home(), Path('/opt')]
    names = ['miniforge', 'miniforge3', 'miniconda', 'miniconda3', 'anaconda3', 'conda']
    for root in roots:
        for name in names:
            bases.append(root / name)
    candidates: list[Path] = []
    env_exe = os.environ.get('CONDA_EXE', '')
    if env_exe:
        candidates.append(Path(env_exe))
    on_path = shutil.which('conda')
    if on_path:
        candidates.append(Path(on_path))
    for base in bases:
        candidates.append(base / 'bin' / 'conda')
        candidates.append(base / 'condabin' / 'conda')
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        text = str(candidate)
        if text not in seen:
            seen.add(text)
            unique.append(candidate)
    return unique


def detect_conda(cfg: dict | None = None) -> dict:
    conda_exe = ''
    for candidate in conda_candidates(cfg):
        if candidate.exists() and candidate.is_file():
            conda_exe = str(candidate)
            break
    info = {
        'available': bool(conda_exe),
        'path': conda_exe or '',
        'version': '',
        'base': '',
        'env_count': 0,
    }
    if not conda_exe:
        return info
    version = subprocess.run([conda_exe, '--version'], text=True, capture_output=True)
    if version.returncode == 0:
        info['version'] = version.stdout.strip() or version.stderr.strip()
    base = subprocess.run([conda_exe, 'info', '--base'], text=True, capture_output=True)
    if base.returncode == 0:
        info['base'] = base.stdout.strip()
    envs = subprocess.run([conda_exe, 'env', 'list', '--json'], text=True, capture_output=True)
    if envs.returncode == 0:
        try:
            payload = json.loads(envs.stdout)
            info['env_count'] = len(payload.get('envs', []))
        except Exception:
            pass
    return info


def parse_nvidia() -> dict:
    if shutil.which('nvidia-smi') is None:
        return {'backend': 'cpu-or-unavailable', 'gpus': [], 'cuda_version': None}
    detail = subprocess.run(['nvidia-smi'], text=True, capture_output=True)
    header = subprocess.run(
        ['nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader'],
        text=True,
        capture_output=True,
    )
    rows = []
    if header.returncode == 0:
        for line in header.stdout.splitlines():
            parts = [piece.strip() for piece in line.split(',')]
            if len(parts) >= 3:
                rows.append({'name': parts[0], 'memory_total': parts[1], 'driver_version': parts[2]})
    cuda_version = None
    if detail.returncode == 0:
        marker = 'CUDA Version:'
        for line in detail.stdout.splitlines():
            if marker in line:
                cuda_version = line.split(marker, 1)[1].split()[0].strip()
                break
    return {'backend': 'nvidia', 'gpus': rows, 'cuda_version': cuda_version}


def detect_download_tools() -> dict:
    curl_path = shutil.which('curl') or ''
    wget_path = shutil.which('wget') or ''
    return {
        'curl': {'available': bool(curl_path), 'path': curl_path},
        'wget': {'available': bool(wget_path), 'path': wget_path},
        'preferred': 'curl' if curl_path else ('wget' if wget_path else ''),
    }


def detect_package_managers() -> list[str]:
    candidates = ['apt-get', 'dnf', 'yum', 'brew', 'pacman', 'zypper']
    return [name for name in candidates if shutil.which(name)]


def build_install_hints(package_managers: list[str], downloads: dict, missing: list[str]) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    pm = package_managers[0] if package_managers else ''
    pm_commands = {
        'apt-get': {
            'git': 'sudo apt-get update && sudo apt-get install -y git',
            'ripgrep': 'sudo apt-get update && sudo apt-get install -y ripgrep',
            'latex': 'sudo apt-get update && sudo apt-get install -y latexmk texlive-latex-extra texlive-fonts-recommended',
            'curl_or_wget': 'sudo apt-get update && sudo apt-get install -y curl wget',
        },
        'dnf': {
            'git': 'sudo dnf install -y git',
            'ripgrep': 'sudo dnf install -y ripgrep',
            'latex': 'sudo dnf install -y latexmk texlive-scheme-medium',
            'curl_or_wget': 'sudo dnf install -y curl wget',
        },
        'yum': {
            'git': 'sudo yum install -y git',
            'ripgrep': 'sudo yum install -y ripgrep',
            'latex': 'sudo yum install -y latexmk texlive',
            'curl_or_wget': 'sudo yum install -y curl wget',
        },
        'brew': {
            'git': 'brew install git',
            'ripgrep': 'brew install ripgrep',
            'latex': 'brew install --cask mactex-no-gui',
            'curl_or_wget': 'brew install curl wget',
        },
        'pacman': {
            'git': 'sudo pacman -S --noconfirm git',
            'ripgrep': 'sudo pacman -S --noconfirm ripgrep',
            'latex': 'sudo pacman -S --noconfirm texlive-core texlive-latexextra latexmk',
            'curl_or_wget': 'sudo pacman -S --noconfirm curl wget',
        },
        'zypper': {
            'git': 'sudo zypper install -y git',
            'ripgrep': 'sudo zypper install -y ripgrep',
            'latex': 'sudo zypper install -y latexmk texlive',
            'curl_or_wget': 'sudo zypper install -y curl wget',
        },
    }
    if 'git' in missing:
        hints.append({'tool': 'git', 'command': pm_commands.get(pm, {}).get('git', 'install git with your system package manager')})
    if 'rg' in missing:
        hints.append({'tool': 'ripgrep', 'command': pm_commands.get(pm, {}).get('ripgrep', 'install ripgrep with your system package manager')})
    latex_missing = {'latexmk', 'pdflatex'} & set(missing)
    if latex_missing:
        hints.append({'tool': 'latex', 'command': pm_commands.get(pm, {}).get('latex', 'install latexmk and a TeX distribution')})
    if 'curl_or_wget' in missing:
        hints.append({'tool': 'download-tool', 'command': pm_commands.get(pm, {}).get('curl_or_wget', 'install curl or wget with your system package manager')})
    if 'conda' in missing:
        hints.append({'tool': 'conda', 'command': 'install Miniforge or Miniconda, or bootstrap a local Miniforge inside the current workspace .runtime/miniforge3'})
    return hints


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    os_release = read_os_release()
    conda = detect_conda(cfg)
    downloads = detect_download_tools()
    accelerator = parse_nvidia()
    package_managers = detect_package_managers()

    git_proc = run(['git', '--version'], cfg)
    rg_proc = run(['rg', '--version'], cfg)
    cli = {
        'python3': {'available': bool(which_tool('python3', cfg)), 'path': which_tool('python3', cfg), 'version': platform.python_version()},
        'git': {'available': bool(which_tool('git', cfg)), 'path': which_tool('git', cfg), 'version': (git_proc.stdout.strip() if git_proc else '')},
        'conda': conda,
        'rg': {'available': bool(which_tool('rg', cfg)), 'path': which_tool('rg', cfg), 'version': (rg_proc.stdout.splitlines()[0] if rg_proc and rg_proc.stdout else '')},
        'curl': downloads['curl'],
        'wget': downloads['wget'],
        'nvidia-smi': {'available': shutil.which('nvidia-smi') is not None, 'path': shutil.which('nvidia-smi') or ''},
        'latexmk': {'available': bool(which_tool('latexmk', cfg)), 'path': which_tool('latexmk', cfg)},
        'pdflatex': {'available': bool(which_tool('pdflatex', cfg)), 'path': which_tool('pdflatex', cfg)},
    }

    required_missing: list[str] = []
    if not cli['git']['available']:
        required_missing.append('git')
    if not cli['conda']['available']:
        required_missing.append('conda')
    if not (cli['curl']['available'] or cli['wget']['available']):
        required_missing.append('curl_or_wget')

    recommended_missing: list[str] = []
    if not cli['rg']['available']:
        recommended_missing.append('rg')

    optional_missing: list[str] = []
    if not cli['latexmk']['available']:
        optional_missing.append('latexmk')
    if not cli['pdflatex']['available']:
        optional_missing.append('pdflatex')
    if not cli['nvidia-smi']['available']:
        optional_missing.append('nvidia-smi')

    install_hints = build_install_hints(package_managers, downloads, required_missing + recommended_missing + optional_missing)
    workspace_disk = shutil.disk_usage(WORKSPACE_ROOT)
    profile = {
        'workspace_root': str(WORKSPACE_ROOT),
        'hostname': platform.node(),
        'platform': platform.platform(),
        'system': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'python': f'Python {platform.python_version()}',
        'cpu_count': os.cpu_count() or 0,
        'memory_gb': detect_memory_gb(),
        'disk_free_gb': round(workspace_disk.free / 1024 / 1024 / 1024, 2),
        'os_release': os_release,
        'package_managers': package_managers,
        'accelerator': accelerator,
        'dependencies': {
            'cli': cli,
            'required_missing': required_missing,
            'recommended_missing': recommended_missing,
            'optional_missing': optional_missing,
            'download_preferred': downloads.get('preferred', ''),
            'install_hints': install_hints,
            'ready_for_core_loop': not required_missing,
            'ready_for_latex': cli['latexmk']['available'] and cli['pdflatex']['available'],
        },
    }

    out_json = paths.reports / 'machine_profile.json'
    out_md = paths.reports / 'machine_profile.md'
    install_md = paths.reports / 'dependency_install_plan.md'
    install_json = paths.reports / 'dependency_install_plan.json'
    out_json.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    install_json.write_text(json.dumps({'install_hints': install_hints}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    gpu_rows = accelerator.get('gpus', []) if isinstance(accelerator, dict) else []
    lines = [
        '# Machine Profile\n\n',
        f'- workspace_root: {WORKSPACE_ROOT}\n',
        f"- hostname: {profile['hostname']}\n",
        f"- platform: {profile['platform']}\n",
        f"- machine: {profile['machine']}\n",
        f"- python: {profile['python']}\n",
        f"- cpu_count: {profile['cpu_count']}\n",
        f"- memory_gb: {profile['memory_gb']}\n",
        f"- disk_free_gb: {profile['disk_free_gb']}\n",
        f"- package_managers: {', '.join(package_managers) if package_managers else 'none-detected'}\n",
        f"- accelerator_backend: {accelerator.get('backend', 'unknown')}\n",
        f"- cuda_version: {accelerator.get('cuda_version', '')}\n",
        f"- gpu_count: {len(gpu_rows)}\n",
    ]
    for row in gpu_rows:
        lines.append(f"- gpu: {row['name']} | mem={row['memory_total']} | driver={row['driver_version']}\n")
    lines.extend([
        '\n## Dependency Readiness\n',
        f"- core_loop_ready: {profile['dependencies']['ready_for_core_loop']}\n",
        f"- latex_ready: {profile['dependencies']['ready_for_latex']}\n",
        f"- required_missing: {', '.join(required_missing) if required_missing else 'none'}\n",
        f"- recommended_missing: {', '.join(recommended_missing) if recommended_missing else 'none'}\n",
        f"- optional_missing: {', '.join(optional_missing) if optional_missing else 'none'}\n",
        '\n## Portability Note\n',
        '- Always adapt to the detected machine profile at runtime instead of assuming a fixed GPU model, GPU count, CUDA version, package manager, or conda location.\n',
        f'- Detailed install guidance: {install_md}\n',
    ])
    out_md.write_text(''.join(lines), encoding='utf-8')

    install_lines = ['# Dependency Install Plan\n\n']
    if install_hints:
        install_lines.append('## Suggested Commands\n')
        for hint in install_hints:
            install_lines.append(f"- {hint['tool']}: `{hint['command']}`\n")
    else:
        install_lines.append('All tracked dependencies required by the runtime line are available.\n')
    install_lines.append('\n## Notes\n')
    install_lines.append(f"- local Miniforge target if conda is missing: {WORKSPACE_ROOT / '.runtime' / 'miniforge3'}\n")
    install_md.write_text(''.join(install_lines), encoding='utf-8')
    print(out_md)


if __name__ == '__main__':
    main()
