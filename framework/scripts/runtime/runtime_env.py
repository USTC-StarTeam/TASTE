#!/usr/bin/env python3
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime.framework_io import read_json as _read_json
from runtime.framework_io import write_json_raw as _write_json
from runtime.taste_pythonpath import taste_pythonpath_string

from project.project_paths import (
    ROOT,
    management_python,
    build_paths,
    load_project_config,
    project_experiment_python_from_config,
    runtime_conda_base,
)


DEFAULT_ENV_KEYS = {
    "PATH", "HOME", "SHELL", "USER", "LANG", "LC_ALL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "LLM_API_KEY", "LLM_API_BASE",
    "LLM_API_KEY_ENV", "LLM_MODEL", "LLM_PROVIDER", "LLM_API_MODE",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CONDA_EXE", "CONDA_PREFIX", "CONDA_BASE",
}


def project_runtime_config(project: str | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if cfg is None and project:
        try:
            cfg = load_project_config(project)
        except Exception:
            cfg = {}
    cfg = cfg or {}
    runtime = cfg.get("runtime", {}) if isinstance(cfg.get("runtime", {}), dict) else {}
    coding = cfg.get("coding_agent", {}) if isinstance(cfg.get("coding_agent", {}), dict) else {}
    env_cfg = cfg.get("environment", {}) if isinstance(cfg.get("environment", {}), dict) else {}
    configured_management_python = str(
        runtime.get("management_python")
        or runtime.get("python_executable")
        or ""
    ).strip()
    if configured_management_python and not Path(configured_management_python).expanduser().is_absolute():
        configured_management_python = ""
    management_python_path = str(
        configured_management_python
        or management_python()
        or sys.executable
        or ""
    )
    experiment_python = str(
        runtime.get("experiment_python")
        or env_cfg.get("experiment_python")
        or project_experiment_python_from_config(cfg)
        or ""
    )
    if "conda_env" in cfg:
        conda_env = str(cfg.get("conda_env") or "")
    elif "conda_env" in runtime:
        conda_env = str(runtime.get("conda_env") or "")
    else:
        conda_env = str(env_cfg.get("conda_env") or "")
    return {
        "source_bashrc": False,
        "bashrc_path": "",
        "node_bin": str(runtime.get("node_bin") or ""),
        "claude_path": str(runtime.get("claude_path") or coding.get("claude_path_hint") or ""),
        "conda_env": conda_env,
        "conda_base": str(runtime.get("conda_base") or env_cfg.get("conda_base_hint") or runtime_conda_base(cfg) or ""),
        "management_python": management_python_path,
        "experiment_python": experiment_python,
        "python_executable": management_python_path,
        "extra_path": runtime.get("extra_path", []) if isinstance(runtime.get("extra_path", []), list) else [],
        "env_overrides": runtime.get("env_overrides", {}) if isinstance(runtime.get("env_overrides", {}), dict) else {},
    }


def update_project_runtime(project: str, patch: dict[str, Any]) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = _read_json(paths.config, {})
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid project config: {paths.config}")
    runtime = dict(cfg.get("runtime") or {}) if isinstance(cfg.get("runtime"), dict) else {}
    runtime["source_bashrc"] = False
    runtime["bashrc_path"] = ""
    for key in ["node_bin", "claude_path", "conda_base", "python_executable", "management_python", "experiment_python"]:
        if key in patch:
            value = str(patch.get(key) or "").strip()
            if value:
                runtime[key] = value
    if "python_executable" in patch and "management_python" not in patch:
        management_value = str(patch.get("python_executable") or "").strip()
        if management_value:
            runtime["management_python"] = management_value
    if "conda_env" in patch:
        conda_env_value = str(patch.get("conda_env") or "").strip()
        if conda_env_value and not re.fullmatch(r"[A-Za-z0-9_.-]+", conda_env_value):
            raise ValueError("conda_env may contain only letters, numbers, dot, underscore, and dash")
        previous_conda_env = str(cfg.get("conda_env") or runtime.get("conda_env") or "").strip()
        cfg["conda_env"] = conda_env_value
        runtime["conda_env"] = conda_env_value
        if conda_env_value != previous_conda_env:
            for key in ["conda_env_prefix", "experiment_python", "environment_run_id", "environment_runtime_source_run_id"]:
                runtime.pop(key, None)
    if "extra_path" in patch:
        value = patch.get("extra_path")
        if isinstance(value, str):
            runtime["extra_path"] = [item.strip() for item in value.split(":") if item.strip()]
        elif isinstance(value, list):
            runtime["extra_path"] = [str(item).strip() for item in value if str(item).strip()]
    if "env_overrides" in patch and isinstance(patch.get("env_overrides"), dict):
        runtime["env_overrides"] = {str(k): str(v) for k, v in patch["env_overrides"].items() if str(k)}
    cfg["runtime"] = runtime
    coding = dict(cfg.get("coding_agent") or {}) if isinstance(cfg.get("coding_agent"), dict) else {}
    if runtime.get("claude_path"):
        coding["claude_path_hint"] = runtime["claude_path"]
    if coding:
        cfg["coding_agent"] = coding
    env_cfg = dict(cfg.get("environment") or {}) if isinstance(cfg.get("environment"), dict) else {}
    if runtime.get("conda_base"):
        env_cfg["conda_base_hint"] = runtime["conda_base"]
    if runtime.get("experiment_python"):
        env_cfg["experiment_python"] = runtime["experiment_python"]
    if env_cfg:
        cfg["environment"] = env_cfg
    management_python = runtime.get("management_python") or runtime.get("python_executable")
    if management_python:
        cfg["python_executable"] = management_python
        runtime["python_executable"] = management_python
    _write_json(paths.config, cfg)
    return project_runtime_config(project, cfg)


def detect_project_runtime(project: str) -> dict[str, Any]:
    cfg = load_project_config(project)
    runtime = project_runtime_config(project, cfg)
    nvm_candidates = [
        str(ROOT.parent / ".nvm"),
        str(Path.home() / ".nvm"),
    ]
    nvm_root = next((item for item in nvm_candidates if item and Path(item).expanduser().exists()), "")
    nvm_bins = _nvm_node_bins(nvm_root)
    env = interactive_env(project, cfg)
    node_path = find_binary("node", project, cfg) or shutil.which("node", path=env.get("PATH", "")) or ""
    node_bin = ""
    if runtime.get("node_bin") and Path(str(runtime["node_bin"])).expanduser().exists():
        node_bin = str(runtime["node_bin"])
    elif node_path:
        node_bin = str(Path(node_path).expanduser().parent)
    elif nvm_bins:
        node_bin = nvm_bins[0]
    conda_base = runtime_conda_base(cfg)
    cfg_for_experiment = dict(cfg)
    cfg_for_experiment["runtime"] = {**(cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}), "conda_base": conda_base}
    management_python_path = management_python()
    experiment_python = project_experiment_python_from_config(cfg_for_experiment)
    patch = {
        "source_bashrc": False,
        "bashrc_path": "",
        "node_bin": node_bin,
        "claude_path": find_binary("claude", project, cfg) or shutil.which("claude", path=env.get("PATH", "")) or "",
        "conda_base": conda_base,
        "management_python": management_python_path,
        "python_executable": management_python_path,
        "experiment_python": experiment_python,
    }
    patch = {key: value for key, value in patch.items() if value is not None and value != ""}
    updated = update_project_runtime(project, patch)
    diagnostics = runtime_diagnostics(project)
    diagnostics["detected"] = patch
    diagnostics["runtime"] = updated
    return diagnostics


def _path_if_executable(path: str) -> str:
    if not path:
        return ""
    candidate = Path(path).expanduser()
    return str(candidate) if candidate.exists() and os.access(candidate, os.X_OK) else ""


def _nvm_node_bins(nvm_root: str) -> list[str]:
    if not nvm_root:
        return []
    root = Path(nvm_root).expanduser()
    versions = root / "versions" / "node"
    if not versions.exists():
        return []
    return [str(path / "bin") for path in sorted(versions.glob("*"), reverse=True) if (path / "bin").exists()]


def _candidate_paths(binary: str, project: str | None = None, cfg: dict[str, Any] | None = None) -> list[str]:
    runtime = project_runtime_config(project, cfg)
    candidates: list[str] = []
    if binary == "claude":
        candidates.append(runtime.get("claude_path", ""))
    if binary == "python":
        candidates.append(runtime.get("management_python", ""))
        candidates.append(runtime.get("python_executable", ""))
        candidates.append(runtime.get("experiment_python", ""))
    candidates.append(os.environ.get(f"{binary.upper()}_BIN", ""))
    for bindir in _runtime_path_entries(runtime):
        candidates.append(str(Path(bindir) / binary))
    found = shutil.which(binary)
    if found:
        candidates.append(found)
    for item in [
        str(Path.home() / ".local" / "bin" / binary),
        str(Path.home() / "workspace" / "bin" / binary),
        str(Path.home() / ".npm-global" / "bin" / binary),
        str(Path.home() / ".bun" / "bin" / binary),
        str(ROOT.parent / ".nvm" / "versions" / "node" / "*" / "bin" / binary),
        str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / binary),
        f"/usr/local/bin/{binary}",
        f"/usr/bin/{binary}",
    ]:
        candidates.append(item)
    expanded: list[str] = []
    for item in candidates:
        if not item:
            continue
        if "*" in item:
            expanded.extend(glob.glob(item))
        else:
            expanded.append(item)
    return expanded


def find_binary(binary: str, project: str | None = None, cfg: dict[str, Any] | None = None) -> str:
    seen: set[str] = set()
    for item in _candidate_paths(binary, project, cfg):
        if not item or item in seen:
            continue
        seen.add(item)
        path = Path(item).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return ""


def _runtime_path_entries(runtime: dict[str, Any], *, include_experiment_python: bool = True) -> list[str]:
    entries: list[str] = []
    for key in ["node_bin"]:
        value = str(runtime.get(key) or "").strip()
        if value:
            entries.append(value)
    executable_path_keys = ["claude_path", "management_python", "python_executable"]
    if include_experiment_python:
        executable_path_keys.append("experiment_python")
    for key in executable_path_keys:
        value = str(runtime.get(key) or "").strip()
        if value:
            entries.append(str(Path(value).expanduser().parent))
    conda_base = str(runtime.get("conda_base") or "").strip()
    if conda_base:
        entries.append(str(Path(conda_base).expanduser() / "bin"))
        entries.append(str(Path(conda_base).expanduser() / "condabin"))
    entries.extend(str(item).strip() for item in runtime.get("extra_path", []) if str(item).strip())
    entries.append(str(Path.home() / "workspace" / "bin"))
    entries.append(str(Path.home() / ".local" / "bin"))
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def interactive_env(project: str | None = None, cfg: dict[str, Any] | None = None, *, include_experiment_python: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    runtime = project_runtime_config(project, cfg)
    path_entries = _runtime_path_entries(runtime, include_experiment_python=include_experiment_python)
    existing = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([*path_entries, existing]) if existing else os.pathsep.join(path_entries)
    env["WORKSPACE_ROOT"] = str(ROOT)
    if project:
        env["PROJECT_ID"] = project
        env["DEFAULT_PROJECT_ID"] = project
        env["PROJECT_CONFIG"] = str(build_paths(project).config)
    env["PYTHONPATH"] = taste_pythonpath_string(ROOT, env.get("PYTHONPATH", ""))
    management_python = str(runtime.get("management_python") or runtime.get("python_executable") or "").strip()
    experiment_python = str(runtime.get("experiment_python") or "").strip()
    if management_python:
        env["MANAGEMENT_PYTHON"] = management_python
    if include_experiment_python and experiment_python:
        env["EXPERIMENT_PYTHON"] = experiment_python
        env["PROJECT_PYTHON"] = experiment_python
    for key, value in runtime.get("env_overrides", {}).items():
        env[str(key)] = str(value)
    conda_base = str(runtime.get("conda_base") or "").strip()
    if conda_base:
        env["CONDA_BASE"] = conda_base
        conda_exe = Path(conda_base).expanduser() / "bin" / "conda"
        if conda_exe.exists():
            env["CONDA_EXE"] = str(conda_exe)
    return env


def _version_check(path: str, env: dict[str, str]) -> str:
    if not path:
        return ""
    try:
        proc = subprocess.run([path, "--version"], text=True, capture_output=True, timeout=8, env=env)
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
    except Exception as exc:
        return f"version check failed: {exc}"


def _executable_check(name: str, path: str, env: dict[str, str], *, reason: str = "") -> dict[str, Any]:
    resolved = _path_if_executable(path)
    return {
        "path": resolved,
        "ok": bool(resolved),
        "version": _version_check(resolved, env) if resolved else "",
        "reason": "ok" if resolved else (reason or f"{name} executable not found"),
    }


def runtime_diagnostics(project: str) -> dict[str, Any]:
    cfg = load_project_config(project)
    runtime = project_runtime_config(project, cfg)
    env = interactive_env(project, cfg)
    checks: dict[str, Any] = {}
    for name, binary in [("node", "node"), ("npm", "npm"), ("claude", "claude"), ("conda", "conda")]:
        path = find_binary(binary, project, cfg)
        if not path and binary in {"node", "npm", "conda"}:
            path = shutil.which(binary, path=env.get("PATH", "")) or ""
        checks[name] = {
            "path": path,
            "ok": bool(path),
            "version": _version_check(path, env) if path else "",
            "reason": "ok" if path else f"{binary} not found in project runtime PATH",
        }
    management_python = str(runtime.get("management_python") or runtime.get("python_executable") or "").strip()
    experiment_python = str(runtime.get("experiment_python") or "").strip()
    checks["management_python"] = _executable_check("management_python", management_python, env, reason="management Python is not configured")
    checks["experiment_python"] = _executable_check("experiment_python", experiment_python, env, reason="Experiment Python is not configured or cannot be derived from conda_env/conda_base")
    checks["python"] = checks["management_python"]
    conda_base = str(runtime.get("conda_base") or "")
    checks["conda_base"] = {"path": conda_base, "ok": bool(conda_base and (Path(conda_base) / "etc" / "profile.d" / "conda.sh").exists()), "reason": "ok" if conda_base and (Path(conda_base) / "etc" / "profile.d" / "conda.sh").exists() else "conda.sh not found under conda base"}
    return {
        "project": project,
        "runtime": runtime,
        "checks": checks,
        "path_head": env.get("PATH", "").split(os.pathsep)[:12],
        "pythonpath_head": env.get("PYTHONPATH", "").split(os.pathsep)[:6],
    }
