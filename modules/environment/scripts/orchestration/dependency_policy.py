from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scripts.common.shell import command_text, command_tokens

PYG_CONDA_PACKAGE_NAMES = {"pyg", "pytorch-cluster", "pytorch-scatter", "pytorch-sparse"}
PYG_PIP_PACKAGE_NAMES = {"torch-geometric", "torch-cluster", "torch-scatter", "torch-sparse", "pyg-lib"}
PYTORCH_PACKAGE_NAMES = {"torch", "torchvision", "torchaudio", "pytorch", "pytorch-cuda"}
PYTORCH_CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu128"
PYTORCH_CUDA_VERSION = "2.9.1"
PYG_CUDA_WHEEL_URL = f"https://data.pyg.org/whl/torch-{PYTORCH_CUDA_VERSION}+cu128.html"
PYG_REQUIRED_PACKAGES = ["torch-geometric", "torch-scatter", "torch-sparse", "torch-cluster"]
PYG_VERIFY_SNIPPET = (
    "import torch; "
    "print('torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available()); "
    "assert torch.cuda.is_available(), 'CUDA is not available in the run-local environment'; "
    "import torch_geometric, torch_scatter, torch_sparse, torch_cluster; "
    "print('pyg', torch_geometric.__version__)"
)
INLINE_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
CONDA_RUN_OPTIONS_WITH_VALUE = {"-n", "--name", "-p", "--prefix", "--cwd"}
CONDA_RUN_ASSIGNMENT_OPTIONS_WITH_VALUE = ("--name=", "--prefix=", "--cwd=")


def _token_package_name(token: str) -> str:
    value = str(token or "").strip().strip('"').strip("'")
    if not value or value.startswith("-") or value.startswith(("http://", "https://", "git+", "file://")):
        return ""
    value = value.split("[", 1)[0]
    value = re.split(r"[<>=!~]", value, maxsplit=1)[0]
    return value.strip().lower().replace("_", "-")


def _split_inline_env_tokens(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    index = 1 if Path(str(tokens[0])).name == "env" else 0
    while index < len(tokens) and INLINE_ENV_ASSIGNMENT_RE.match(str(tokens[index] or "")):
        index += 1
    return tokens[index:] if index else tokens


def _row_command_tokens(row: dict[str, Any]) -> list[str]:
    return _split_inline_env_tokens(command_tokens(row.get("command")))


def _conda_action(tokens: list[str]) -> str:
    if len(tokens) < 2 or Path(str(tokens[0])).name not in {"conda", "mamba", "micromamba"}:
        return ""
    if str(tokens[1]) == "env" and len(tokens) > 2:
        return str(tokens[2])
    return str(tokens[1])


def _conda_run_inner_command_index(tokens: list[str]) -> int | None:
    if len(tokens) < 3 or Path(str(tokens[0])).name not in {"conda", "mamba", "micromamba"} or str(tokens[1]) != "run":
        return None
    index = 2
    while index < len(tokens):
        token = str(tokens[index] or "")
        if token == "--":
            return index + 1 if index + 1 < len(tokens) else None
        if token in CONDA_RUN_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(prefix) for prefix in CONDA_RUN_ASSIGNMENT_OPTIONS_WITH_VALUE):
            index += 1
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        return index
    return None


def _conda_run_inner_tokens(tokens: list[str]) -> list[str]:
    index = _conda_run_inner_command_index(tokens)
    return tokens[index:] if index is not None else []


def _pip_install_packages(tokens: list[str]) -> set[str]:
    pip_tokens = _conda_run_inner_tokens(tokens) or tokens
    if not pip_tokens:
        return set()
    head = Path(str(pip_tokens[0])).name
    if head.startswith("python") and len(pip_tokens) >= 4 and pip_tokens[1] == "-m" and str(pip_tokens[2]) in {"pip", "pip3"} and str(pip_tokens[3]) == "install":
        start = 4
    elif head in {"pip", "pip3"} and len(pip_tokens) >= 2 and str(pip_tokens[1]) == "install":
        start = 2
    else:
        return set()
    packages: set[str] = set()
    skip_next = False
    options_with_values = {
        "-r", "--requirement", "-c", "--constraint", "-f", "--find-links", "-i", "--index-url",
        "--extra-index-url", "--trusted-host", "--platform", "--python-version", "--implementation",
        "--abi", "--target", "--prefix", "--root", "--src", "--cache-dir",
    }
    for token in pip_tokens[start:]:
        value = str(token or "")
        if skip_next:
            skip_next = False
            continue
        if value in options_with_values:
            skip_next = True
            continue
        if value.startswith("-"):
            continue
        package = _token_package_name(value)
        if package:
            packages.add(package)
    return packages


def _conda_install_packages(tokens: list[str]) -> set[str]:
    if _conda_action(tokens) not in {"install", "create", "update"}:
        return set()
    packages: set[str] = set()
    skip_next = False
    options_with_values = {"-n", "--name", "-p", "--prefix", "-c", "--channel", "--solver", "--file"}
    assignment_prefixes = ("--name=", "--prefix=", "--channel=", "--solver=", "--file=")
    for token in tokens[2:]:
        value = str(token or "")
        if skip_next:
            skip_next = False
            continue
        if value in options_with_values:
            skip_next = True
            continue
        if any(value.startswith(prefix) for prefix in assignment_prefixes) or value.startswith("-"):
            continue
        package = _token_package_name(value)
        if package:
            packages.add(package)
    return packages


def _row_installs_pyg_with_conda(row: dict[str, Any]) -> bool:
    return bool(_conda_install_packages(_row_command_tokens(row)) & PYG_CONDA_PACKAGE_NAMES)


def _row_installs_pytorch_with_conda(row: dict[str, Any]) -> bool:
    return bool(_conda_install_packages(_row_command_tokens(row)) & PYTORCH_PACKAGE_NAMES)


def _row_installs_pyg_with_pip(row: dict[str, Any]) -> bool:
    return bool(_pip_install_packages(_row_command_tokens(row)) & PYG_PIP_PACKAGE_NAMES)


def _row_verifies_pyg_import(row: dict[str, Any]) -> bool:
    text = command_text(_row_command_tokens(row)).lower()
    return "torch_geometric" in text and "torch_scatter" in text and "torch_sparse" in text and "torch_cluster" in text


def _row_verifies_cuda(row: dict[str, Any]) -> bool:
    text = command_text(_row_command_tokens(row)).lower()
    return "torch.cuda.is_available" in text or "cuda is not available" in text


def _normalize_create_command_python(row: dict[str, Any], python_version: str) -> dict[str, Any]:
    row = dict(row)
    tokens = list(command_tokens(row.get("command")))
    if _conda_action(tokens) != "create":
        return row
    python_seen = False
    new_tokens: list[str] = []
    for token in tokens:
        if _token_package_name(str(token)) == "python":
            if not python_seen:
                new_tokens.append(f"python={python_version}")
                python_seen = True
            continue
        new_tokens.append(token)
    if not python_seen:
        insert_at = next((i for i, token in enumerate(new_tokens[2:], start=2) if str(token).startswith("-")), len(new_tokens))
        new_tokens.insert(insert_at, f"python={python_version}")
    row["command"] = new_tokens
    return row


def _torch_cuda_install_row(source_phase: str) -> dict[str, Any]:
    return {
        "phase": source_phase or "install_torch_cuda",
        "command": [
            "python", "-m", "pip", "install", "--upgrade", "--no-cache-dir",
            f"torch=={PYTORCH_CUDA_VERSION}+cu128", "torchvision", "torchaudio",
            "--index-url", PYTORCH_CUDA_INDEX_URL,
        ],
        "cwd": "repo",
        "timeout_sec": 1800,
        "required": True,
        "policy_managed": True,
    }


def _pyg_install_row(source_phase: str) -> dict[str, Any]:
    return {
        "phase": source_phase or "install_pyg_wheels",
        "command": [
            "python", "-m", "pip", "install", "--upgrade", "--no-cache-dir",
            *PYG_REQUIRED_PACKAGES, "-f", PYG_CUDA_WHEEL_URL,
        ],
        "cwd": "repo",
        "timeout_sec": 1800,
        "required": True,
        "policy_managed": True,
    }


def _pyg_verify_row() -> dict[str, Any]:
    return {
        "phase": "verify_pyg_cuda_import",
        "command": ["python", "-c", PYG_VERIFY_SNIPPET],
        "cwd": "repo",
        "timeout_sec": 300,
        "required": True,
        "policy_managed": True,
    }


def _machine_needs_modern_cuda_stack(machine: dict[str, Any] | None) -> bool:
    if not isinstance(machine, dict):
        return False
    gpu_rows = machine.get("gpu") if isinstance(machine.get("gpu"), list) else []
    for row in gpu_rows:
        if not isinstance(row, dict):
            continue
        text = " ".join(str(row.get(key) or "") for key in ("name", "compute_capability", "driver_version")).lower()
        if "5090" in text or " 12." in text:
            return True
        try:
            if float(str(row.get("compute_capability") or "0")) >= 12.0:
                return True
        except Exception:
            pass
    return False


def normalize_environment_plan_commands(plan: dict[str, Any], machine: dict[str, Any] | None = None, policy_version: str = "") -> dict[str, Any]:
    if not isinstance(plan, dict):
        return plan
    rows = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    if not rows:
        return plan
    needs_pyg = any(isinstance(row, dict) and (_row_installs_pyg_with_conda(row) or _row_installs_pyg_with_pip(row) or _row_verifies_pyg_import(row)) for row in rows)
    conda_installs_torch = any(isinstance(row, dict) and _row_installs_pytorch_with_conda(row) for row in rows)
    if not (needs_pyg or (_machine_needs_modern_cuda_stack(machine) and conda_installs_torch)):
        return plan

    normalized = dict(plan)
    rewrites: list[dict[str, Any]] = []
    normalized["python_version"] = "3.11"
    new_rows: list[dict[str, Any]] = []
    torch_row_present = False
    pyg_row_present = False
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            new_rows.append(raw_row)
            continue
        row = dict(raw_row)
        if _conda_action(_row_command_tokens(row)) == "create":
            updated = _normalize_create_command_python(row, "3.11")
            if updated.get("command") != row.get("command"):
                rewrites.append({"index": index, "phase": row.get("phase"), "reason": "RTX 5090/modern CUDA stack requires Python 3.11 for current PyTorch/PyG binary wheels", "before": row.get("command"), "after": updated.get("command")})
            row = updated
        if _row_installs_pytorch_with_conda(row):
            replacement = _torch_cuda_install_row(str(row.get("phase") or "install_torch_cuda"))
            rewrites.append({"index": index, "phase": row.get("phase"), "reason": "Conda resolved PyTorch to CPU or incompatible CUDA builds; use PyTorch CUDA 12.8 pip wheels for Blackwell GPUs", "before": row.get("command"), "after": replacement.get("command")})
            new_rows.append(replacement)
            torch_row_present = True
            continue
        if _row_installs_pyg_with_conda(row):
            replacement = _pyg_install_row(str(row.get("phase") or "install_pyg_wheels"))
            rewrites.append({"index": index, "phase": row.get("phase"), "reason": "Conda pyg channel does not provide a compatible PyG/PyTorch/CUDA/Python matrix here; use official PyG wheel index matching torch+cu128", "before": row.get("command"), "after": replacement.get("command")})
            new_rows.append(replacement)
            pyg_row_present = True
            continue
        if _row_installs_pyg_with_pip(row):
            packages = _pip_install_packages(_row_command_tokens(row))
            if not set(PYG_REQUIRED_PACKAGES).issubset(packages) or PYG_CUDA_WHEEL_URL not in command_text(_row_command_tokens(row)):
                replacement = _pyg_install_row(str(row.get("phase") or "install_pyg_wheels"))
                rewrites.append({"index": index, "phase": row.get("phase"), "reason": "PyG pip install must use the torch/CUDA matched wheel index and all required extension packages", "before": row.get("command"), "after": replacement.get("command")})
                new_rows.append(replacement)
                pyg_row_present = True
                continue
        if _pip_install_packages(_row_command_tokens(row)) & {"torch", "torchvision", "torchaudio"}:
            torch_row_present = True
        if _row_installs_pyg_with_pip(row):
            pyg_row_present = True
        new_rows.append(row)

    if needs_pyg and not torch_row_present:
        row = _torch_cuda_install_row("install_torch_cuda")
        new_rows.insert(1 if new_rows else 0, row)
        rewrites.append({"index": None, "phase": "install_torch_cuda", "reason": "PyG workload needs explicit CUDA-enabled PyTorch wheel before PyG extensions", "after": row["command"]})
    if needs_pyg and not pyg_row_present:
        row = _pyg_install_row("install_pyg_wheels")
        new_rows.append(row)
        rewrites.append({"index": None, "phase": "install_pyg_wheels", "reason": "PyG workload lacked a compatible PyG extension install row", "after": row["command"]})
    if needs_pyg and not any(isinstance(row, dict) and _row_verifies_pyg_import(row) and _row_verifies_cuda(row) for row in new_rows):
        row = _pyg_verify_row()
        new_rows.append(row)
        rewrites.append({"index": None, "phase": "verify_pyg_cuda_import", "reason": "PyG/CUDA import gate must prove CUDA-enabled torch and compiled PyG extensions are usable", "after": row["command"]})

    normalized["commands"] = new_rows
    if rewrites:
        normalized["plan_policy_rewrites"] = [*(normalized.get("plan_policy_rewrites") if isinstance(normalized.get("plan_policy_rewrites"), list) else []), *rewrites]
        normalized["backend_dependency_policy"] = {
            "policy_version": policy_version,
            "python_version": "3.11",
            "torch_version": PYTORCH_CUDA_VERSION,
            "torch_cuda_index_url": PYTORCH_CUDA_INDEX_URL,
            "pyg_wheel_url": PYG_CUDA_WHEEL_URL,
            "reason": "RTX 5090/compute 12.0 requires a modern CUDA-enabled PyTorch stack; conda pyg packages did not solve for the generated Python/PyTorch/CUDA matrix.",
        }
    return normalized
