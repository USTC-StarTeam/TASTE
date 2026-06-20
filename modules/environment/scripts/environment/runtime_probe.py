from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.common.shell import runtime_env
from scripts.common.io_utils import utc_now


def _which(name: str) -> str:
    return shutil.which(name, path=runtime_env().get("PATH", "")) or ""


def _run(cmd: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=runtime_env())
        return {"return_code": proc.returncode, "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-2000:]}
    except Exception as exc:
        return {"return_code": 125, "error": f"{type(exc).__name__}: {exc}"}


def find_conda_executable() -> str:
    candidates: list[Path] = []
    for value in [os.environ.get("CONDA_EXE"), _which("conda")]:
        if value:
            candidates.append(Path(value))
    candidates.extend([
        Path("/home/fmh/workspace/miniforge/bin/conda"),
        Path("/home/fmh/workspace/miniforge/condabin/conda"),
        Path.home() / "miniforge3" / "bin" / "conda",
        Path.home() / "miniconda3" / "bin" / "conda",
        Path.home() / "anaconda3" / "bin" / "conda",
        Path("/opt/conda/bin/conda"),
    ])
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        if candidate.exists():
            return text
    return ""


def conda_env_exists(conda_exe: str, env_name: str) -> bool:
    if not conda_exe or not env_name:
        return False
    proc = _run([conda_exe, "env", "list", "--json"], timeout=30)
    if proc.get("return_code") != 0:
        return False
    try:
        payload = json.loads(str(proc.get("stdout") or "{}"))
    except Exception:
        return False
    for item in payload.get("envs", []) if isinstance(payload, dict) else []:
        path = Path(str(item))
        if path.name == env_name or str(path).endswith(f"/envs/{env_name}"):
            return True
    return False


def detect_machine_profile() -> dict[str, Any]:
    conda_exe = find_conda_executable()
    nvidia = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap", "--format=csv,noheader"], timeout=20) if _which("nvidia-smi") else {}
    gpu_rows: list[dict[str, str]] = []
    if nvidia.get("return_code") == 0:
        for line in str(nvidia.get("stdout") or "").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 4:
                gpu_rows.append({"name": parts[0], "memory_total": parts[1], "driver_version": parts[2], "compute_capability": parts[3]})
    conda_info = _run([conda_exe, "info", "--json"], timeout=30) if conda_exe else {}
    conda_payload: dict[str, Any] = {}
    if conda_info.get("return_code") == 0:
        try:
            conda_payload = json.loads(str(conda_info.get("stdout") or "{}"))
        except Exception:
            conda_payload = {}
    return {
        "schema_version": "environment.machine_profile.v1",
        "created_at": utc_now(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "active_conda_env": os.environ.get("CONDA_DEFAULT_ENV", ""),
        "conda_executable": conda_exe,
        "conda_root_prefix": conda_payload.get("root_prefix", "") if isinstance(conda_payload, dict) else "",
        "conda_envs_dirs": conda_payload.get("envs_dirs", []) if isinstance(conda_payload, dict) else [],
        "tools": {name: _which(name) for name in ["git", "rg", "curl", "wget", "nvidia-smi", "claude", "node", "npm", "mamba"]},
        "node_version": _run(["node", "--version"], timeout=10) if _which("node") else {},
        "claude_version": _run(["claude", "--version"], timeout=20) if _which("claude") else {},
        "gpu": gpu_rows,
        "nvidia_smi": nvidia,
    }
