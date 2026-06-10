#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


@dataclass
class ProjectPaths:
    name: str
    root: Path
    config: Path
    agents_file: Path
    discover: Path
    raw: Path
    raw_papers: Path
    raw_notes: Path
    raw_assets: Path
    wiki: Path
    wiki_index: Path
    wiki_log: Path
    wiki_overview: Path
    wiki_papers: Path
    wiki_concepts: Path
    wiki_entities: Path
    wiki_comparisons: Path
    wiki_gaps: Path
    wiki_synthesis: Path
    reports: Path
    logs: Path
    state: Path
    obsidian: Path
    planning: Path
    experiments: Path
    artifacts: Path
    repos_candidates: Path
    repos_selected: Path
    datasets_registry: Path
    datasets_notes: Path
    benchmarks: Path
    work_status: Path


def validate_project_name(name: str) -> str:
    candidate = str(name or '').strip()
    if not candidate:
        raise ValueError('project name is required')
    if candidate in {'.', '..'} or '/' in candidate or '\\' in candidate:
        raise ValueError(
            f"invalid project name {name!r}: pass the project id only, e.g. "
            "'my_project_id', not a path such as 'projects/my_project_id'"
        )
    if candidate.startswith('projects') or candidate.startswith('/'):
        raise ValueError(
            f"invalid project name {name!r}: project names must not include the projects/ prefix"
        )
    if not PROJECT_NAME_RE.match(candidate):
        raise ValueError(
            f"invalid project name {name!r}: use letters, numbers, '.', '_' or '-'"
        )
    return candidate


def get_project_root(name: str) -> Path:
    name = validate_project_name(name)
    return ROOT / 'projects' / name


def load_project_config(name: str) -> dict:
    name = validate_project_name(name)
    return json.loads((get_project_root(name) / 'project.json').read_text(encoding='utf-8'))



def _path_text(value: Any) -> str:
    return str(value or '').strip()


def _existing_dir(value: Any) -> str:
    text = _path_text(value)
    if not text:
        return ''
    try:
        path = Path(text).expanduser()
        return str(path.resolve()) if path.is_dir() else ''
    except Exception:
        return ''


def _existing_file(value: Any) -> str:
    text = _path_text(value)
    if not text:
        return ''
    try:
        path = Path(text).expanduser()
        return str(path.resolve()) if path.is_file() else ''
    except Exception:
        return ''


def _config_dict(cfg: Any, key: str) -> dict[str, Any]:
    if isinstance(cfg, dict) and isinstance(cfg.get(key), dict):
        return cfg.get(key) or {}
    return {}


def runtime_conda_base(cfg: dict[str, Any] | None = None) -> str:
    """Resolve a conda base without baking a workstation path into TASTE."""
    runtime = _config_dict(cfg, 'runtime')
    env_cfg = _config_dict(cfg, 'environment')
    candidates: list[Any] = [
        os.environ.get('CONDA_BASE'),
        os.environ.get('CONDA_BASE'),
        runtime.get('conda_base'),
        env_cfg.get('conda_base_hint'),
    ]
    prefix = _path_text(os.environ.get('CONDA_PREFIX'))
    if prefix:
        prefix_path = Path(prefix).expanduser()
        if prefix_path.parent.name == 'envs':
            candidates.append(prefix_path.parent.parent)
        else:
            candidates.append(prefix_path)
    home = Path.home()
    candidates.extend([
        home / 'miniforge3',
        home / 'miniforge',
        home / 'mambaforge',
        home / 'miniconda3',
        home / 'anaconda3',
    ])
    for candidate in candidates:
        resolved = _existing_dir(candidate)
        if resolved:
            return resolved
    conda = shutil.which('conda')
    if conda:
        path = Path(conda).resolve()
        if path.parent.name == 'bin':
            return str(path.parent.parent)
    return ''


def conda_executable(cfg: dict[str, Any] | None = None) -> str:
    explicit = _existing_file(os.environ.get('CONDA_EXE'))
    if explicit:
        return explicit
    base = runtime_conda_base(cfg)
    if base:
        candidate = Path(base) / 'bin' / 'conda'
        if candidate.exists():
            return str(candidate.resolve())
    return shutil.which('conda') or ''


def management_python() -> str:
    explicit = _existing_file(os.environ.get('MANAGEMENT_PYTHON'))
    if explicit:
        return explicit
    return str(Path(sys.executable).resolve())


def project_experiment_python_from_config(cfg: dict[str, Any] | None, *, fallback_to_current: bool = False) -> str:
    cfg = cfg or {}
    runtime = _config_dict(cfg, 'runtime')
    env_cfg = _config_dict(cfg, 'environment')
    for key in ['EXPERIMENT_PYTHON', 'PROJECT_PYTHON']:
        explicit = _existing_file(os.environ.get(key))
        if explicit:
            return explicit
    for value in [runtime.get('experiment_python'), env_cfg.get('experiment_python')]:
        explicit = _existing_file(value)
        if explicit:
            return explicit
    env_name = _path_text(cfg.get('conda_env'))
    conda_base = runtime_conda_base(cfg)
    if env_name and conda_base:
        candidate = Path(conda_base) / 'envs' / env_name / 'bin' / 'python'
        if candidate.exists():
            return str(candidate.resolve())
    python_executable = _path_text(cfg.get('python_executable'))
    if python_executable:
        if Path(python_executable).expanduser().is_absolute():
            explicit = _existing_file(python_executable)
            if explicit:
                return explicit
        elif not env_name:
            found = shutil.which(python_executable)
            if found:
                return str(Path(found).resolve())
    return str(Path(sys.executable).resolve()) if fallback_to_current else ''

def build_paths(name: str) -> ProjectPaths:
    name = validate_project_name(name)
    root = get_project_root(name)
    wiki = root / 'wiki'
    return ProjectPaths(
        name=name,
        root=root,
        config=root / 'project.json',
        agents_file=root / 'AGENTS.md',
        discover=root / 'discover',
        raw=root / 'raw',
        raw_papers=root / 'raw' / 'papers',
        raw_notes=root / 'raw' / 'notes',
        raw_assets=root / 'raw' / 'assets',
        wiki=wiki,
        wiki_index=wiki / 'index.md',
        wiki_log=wiki / 'log.md',
        wiki_overview=wiki / 'overview.md',
        wiki_papers=wiki / 'papers',
        wiki_concepts=wiki / 'concepts',
        wiki_entities=wiki / 'entities',
        wiki_comparisons=wiki / 'comparisons',
        wiki_gaps=wiki / 'gaps',
        wiki_synthesis=wiki / 'synthesis',
        reports=root / 'reports',
        logs=root / 'logs',
        state=root / 'state',
        obsidian=root / 'obsidian',
        planning=root / 'planning',
        experiments=root / 'experiments',
        artifacts=root / 'artifacts',
        repos_candidates=root / 'repos' / 'candidates',
        repos_selected=root / 'repos' / 'selected',
        datasets_registry=root / 'datasets' / 'registry',
        datasets_notes=root / 'datasets' / 'notes',
        benchmarks=root / 'benchmarks',
        work_status=ROOT / '工作状态.txt',
    )
