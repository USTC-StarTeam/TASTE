#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from file_utils import atomic_write_json, atomic_write_text, now_iso


def _path_if_dir(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    return str(path.resolve()) if path.is_dir() else ""


def resolve_conda_base(explicit: str = "") -> str:
    candidates: list[Any] = [explicit, os.environ.get("CONDA_BASE")]
    prefix = os.environ.get("CONDA_PREFIX", "")
    if prefix:
        prefix_path = Path(prefix).expanduser()
        candidates.append(prefix_path.parent.parent if prefix_path.parent.name == "envs" else prefix_path)
    candidates.extend([
        Path.home() / "workspace" / "miniforge",
        Path.home() / "workspace" / "miniforge3",
        Path.home() / "miniforge",
        Path.home() / "miniforge3",
        Path.home() / "mambaforge",
        Path.home() / "miniconda3",
        Path.home() / "anaconda3",
    ])
    for candidate in candidates:
        found = _path_if_dir(candidate)
        if found and (Path(found) / "etc" / "profile.d" / "conda.sh").exists():
            return found
    conda = shutil.which("conda") or os.environ.get("CONDA_EXE", "")
    if conda:
        path = Path(conda).expanduser().resolve()
        if path.parent.name == "bin":
            base = path.parent.parent
            if (base / "etc" / "profile.d" / "conda.sh").exists():
                return str(base)
    return ""


def conda_env_prefix(conda_base: str, env_name: str) -> str:
    env_name = str(env_name or "").strip()
    if not env_name:
        return ""
    as_path = Path(env_name).expanduser()
    if as_path.is_absolute() and (as_path / "bin" / "python").exists():
        return str(as_path.resolve())
    base = Path(conda_base).expanduser() if conda_base else None
    if base:
        for candidate in [base / "envs" / env_name, base if base.name == env_name else Path()]:
            if candidate and (candidate / "bin" / "python").exists():
                return str(candidate.resolve())
    conda = str(Path(conda_base) / "bin" / "conda") if conda_base else (shutil.which("conda") or "")
    if conda:
        try:
            proc = subprocess.run([conda, "env", "list", "--json"], text=True, capture_output=True, timeout=20)
            payload = json.loads(proc.stdout) if proc.returncode == 0 else {}
            for item in payload.get("envs", []):
                path = Path(str(item))
                if path.name == env_name and (path / "bin" / "python").exists():
                    return str(path.resolve())
        except Exception:
            pass
    return ""


def _semver_key(path: Path) -> tuple[int, int, int, str]:
    nums = [int(x) for x in re.findall(r"\d+", path.name)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2], path.name


def find_nvm_node_bin(explicit_nvm_dir: str = "") -> str:
    candidates = [explicit_nvm_dir, os.environ.get("NVM_DIR"), Path.home() / "workspace" / ".nvm", Path.home() / ".nvm"]
    bins: list[Path] = []
    for item in candidates:
        root = Path(str(item)).expanduser() if item else None
        versions = root / "versions" / "node" if root else None
        if versions and versions.exists():
            bins.extend(path / "bin" for path in versions.iterdir() if (path / "bin" / "node").exists())
    if bins:
        return str(sorted(bins, key=_semver_key, reverse=True)[0].resolve())
    node = shutil.which("node")
    return str(Path(node).resolve().parent) if node else ""


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def runtime_path_entries(conda_base: str, env_prefix: str, node_bin: str) -> list[str]:
    entries = [
        node_bin,
        str(Path(env_prefix) / "bin") if env_prefix else "",
        str(Path(conda_base) / "bin") if conda_base else "",
        str(Path(conda_base) / "condabin") if conda_base else "",
        str(Path.home() / "workspace" / "bin"),
        str(Path.home() / ".local" / "bin"),
    ]
    return _dedupe([item for item in entries if item and Path(item).exists()])


def find_executable(name: str, entries: list[str]) -> str:
    found = shutil.which(name, path=os.pathsep.join([*entries, os.environ.get("PATH", "")]))
    return str(Path(found).resolve()) if found else ""


def version_of(path: str, env: dict[str, str]) -> str:
    if not path:
        return ""
    try:
        proc = subprocess.run([path, "--version"], text=True, capture_output=True, timeout=10, env=env)
        text = (proc.stdout or proc.stderr or "").strip()
        return text.splitlines()[0] if text else ""
    except Exception as exc:
        return f"version check failed: {exc}"


def build_env(lock: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    entries = lock.get("path_entries", []) if isinstance(lock.get("path_entries"), list) else []
    env["PATH"] = os.pathsep.join(_dedupe([*entries, env.get("PATH", "")]))
    env["PYTHONUNBUFFERED"] = "1"
    if lock.get("conda_base"):
        env["CONDA_BASE"] = str(lock["conda_base"])
        env["CONDA_EXE"] = str(Path(lock["conda_base"]) / "bin" / "conda")
    if lock.get("conda_env"):
        env["CONDA_DEFAULT_ENV"] = str(lock["conda_env"])
    if lock.get("conda_env_prefix"):
        env["CONDA_PREFIX"] = str(lock["conda_env_prefix"])
        env["EXPERIMENT_PYTHON"] = str(Path(lock["conda_env_prefix"]) / "bin" / "python")
    if lock.get("nvm_dir"):
        env["NVM_DIR"] = str(lock["nvm_dir"])
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def build_runtime_lock(conda_env: str, *, conda_base: str = "", nvm_dir: str = "", require_claude: bool = True) -> dict[str, Any]:
    base = resolve_conda_base(conda_base)
    prefix = conda_env_prefix(base, conda_env)
    node_bin = find_nvm_node_bin(nvm_dir)
    resolved_nvm = str(Path(nvm_dir or os.environ.get("NVM_DIR") or Path.home() / "workspace" / ".nvm").expanduser())
    entries = runtime_path_entries(base, prefix, node_bin)
    preliminary = {"conda_base": base, "conda_env": conda_env, "conda_env_prefix": prefix, "nvm_dir": resolved_nvm, "path_entries": entries}
    env = build_env(preliminary)
    tools = {}
    for name in ["python", "pip", "conda", "rg", "node", "npm", "claude"]:
        path = find_executable(name, entries)
        tools[name] = {"path": path, "ok": bool(path), "version": version_of(path, env) if path else ""}
    checks = {
        "conda_base": bool(base),
        "conda_env_prefix": bool(prefix),
        "python": tools["python"]["ok"],
        "rg": tools["rg"]["ok"],
        "node": tools["node"]["ok"],
        "claude": tools["claude"]["ok"] if require_claude else True,
    }
    return {
        **preliminary,
        "generated_at": now_iso(),
        "tools": tools,
        "checks": checks,
        "ready": all(checks.values()),
        "activation_command": activation_command(base, conda_env, resolved_nvm),
    }


def activation_command(conda_base: str, conda_env: str, nvm_dir: str) -> str:
    lines = ["set -euo pipefail"]
    if conda_base:
        lines.append(f". {sh_quote(str(Path(conda_base) / 'etc' / 'profile.d' / 'conda.sh'))}")
        if conda_env:
            lines.append(f"conda activate {sh_quote(conda_env)}")
    if nvm_dir:
        lines.append(f"export NVM_DIR={sh_quote(nvm_dir)}")
        lines.append('[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"')
    return "\n".join(lines)


def sh_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def assert_runtime_ready(lock: dict[str, Any], *, require_claude: bool = True) -> None:
    missing = [key for key, ok in (lock.get("checks") or {}).items() if not ok]
    if missing:
        raise RuntimeError("实验运行环境未就绪，缺失: " + ", ".join(missing))
    if require_claude and not ((lock.get("tools") or {}).get("claude") or {}).get("path"):
        raise RuntimeError("找不到 Claude Code CLI")


def write_environment_files(output_root: Path, lock: dict[str, Any]) -> dict[str, str]:
    state = output_root / "state"
    script_dir = output_root / "scripts"
    state.mkdir(parents=True, exist_ok=True)
    script_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state / "environment_lock.json"
    setup_path = script_dir / "activate_experiment_env.sh"
    atomic_write_json(lock_path, lock)
    atomic_write_text(setup_path, lock.get("activation_command", "") + "\n")
    setup_path.chmod(0o755)
    return {"environment_lock_path": str(lock_path), "activation_script_path": str(setup_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查并写出 Experimenting 独立后端运行环境锁。")
    parser.add_argument("--conda-env", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--conda-base", default="")
    parser.add_argument("--nvm-dir", default="")
    parser.add_argument("--no-claude", action="store_true")
    args = parser.parse_args(argv)
    lock = build_runtime_lock(args.conda_env, conda_base=args.conda_base, nvm_dir=args.nvm_dir, require_claude=not args.no_claude)
    files = write_environment_files(Path(args.output_root).expanduser().resolve(), lock)
    payload = {**lock, **files}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if lock.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
