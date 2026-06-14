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

def _repo_root_from_script() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "framework").is_dir() and (candidate / "modules").is_dir() and (candidate / "web").is_dir():
            return candidate
    return current.parents[1]

ROOT = _repo_root_from_script()
STAGE_MODULE_DIRS = (
    "finding",
    "reading",
    "ideation",
    "planning",
    "environment",
    "experimenting",
    "writing",
)


def _dedupe_path_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _ensure_framework_pythonpath() -> None:
    entries: list[Path] = [
        ROOT / "framework",
        ROOT / "web" / "backend",
        ROOT / "framework" / "scripts",
    ]
    entries.extend(ROOT / "modules" / name for name in STAGE_MODULE_DIRS)
    entries.extend(ROOT / "modules" / name / "scripts" for name in STAGE_MODULE_DIRS)
    entries.append(ROOT)
    existing = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    entry_strings = [str(path) for path in entries if path.exists()]
    for item in reversed(entry_strings):
        if item not in sys.path:
            sys.path.insert(0, item)
    os.environ["PYTHONPATH"] = os.pathsep.join(_dedupe_path_strings(entry_strings + existing))


_ensure_framework_pythonpath()
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


def load_runtime_config() -> dict:
    runtime_dir = Path(os.environ.get('WORKFLOW_RUNTIME_DIR') or ROOT / 'runtime').expanduser()
    for candidate in [runtime_dir / '.config.json', ROOT / 'runtime' / '.config.json']:
        try:
            payload = json.loads(candidate.read_text(encoding='utf-8'))
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _positive_config_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def configured_max_ideas(name: str = '', cfg: dict[str, Any] | None = None, *, explicit: Any = None, default: int = 5) -> int:
    explicit_value = _positive_config_int(explicit)
    if explicit_value:
        return explicit_value
    payloads: list[dict[str, Any]] = []
    if isinstance(cfg, dict):
        payloads.append(cfg)
    if name:
        try:
            loaded = load_project_config(name)
        except Exception:
            loaded = {}
        if isinstance(loaded, dict) and loaded is not cfg:
            payloads.append(loaded)
    runtime = load_runtime_config()
    if isinstance(runtime, dict):
        payloads.append(runtime)
    for payload in payloads:
        for container in [payload, payload.get('research') if isinstance(payload.get('research'), dict) else {}, payload.get('workflow') if isinstance(payload.get('workflow'), dict) else {}]:
            value = _positive_config_int((container or {}).get('max_ideas') if isinstance(container, dict) else None)
            if value:
                return value
    fallback = _positive_config_int(default)
    return fallback or 5


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
