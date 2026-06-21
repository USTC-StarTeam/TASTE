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
TORCHVISION_CUDA_SPEC = "torchvision==0.24.1+cu128"
TORCHAUDIO_CUDA_SPEC = f"torchaudio=={PYTORCH_CUDA_VERSION}+cu128"
PYG_CUDA_WHEEL_URL = f"https://data.pyg.org/whl/torch-{PYTORCH_CUDA_VERSION}+cu128.html"
PYG_REQUIRED_PACKAGES = ["torch-geometric", "torch-scatter", "torch-sparse", "torch-cluster"]
ESM_PIP_SPEC = "esm==3.2.1.post1"
PIP_PACKAGE_SPEC_OVERRIDES = {"esm": ESM_PIP_SPEC}
PYG_VERIFY_SNIPPET = (
    "import torch; "
    "print('torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available()); "
    "assert torch.cuda.is_available(), 'CUDA is not available in the run-local environment'; "
    "import torch_geometric, torch_scatter, torch_sparse, torch_cluster; "
    "print('pyg', torch_geometric.__version__)"
)
PROTDIS_METRICS_VERIFY_SNIPPET = (
    "import sys, numpy as np; "
    "sys.path.insert(0, 'tasks/proteinshake'); "
    "from src.models.metrics import compute_metrics, default_metrics; "
    "metric = default_metrics('classification'); "
    "assert metric == 'accuracy', metric; "
    "score = compute_metrics(np.array([[0.1, 0.9], [0.8, 0.2]]), np.array([1, 0]), 'classification', metric); "
    "assert float(score) == 1.0, score; "
    "print('tasks metrics module OK', metric, float(score))"
)
BIOPYTHON_PACKAGE_NAME = "biopython"
ATOM3D_PACKAGE_NAME = "atom3d"
BIOPYTHON_LEGACY_SPEC = "biopython==1.81"
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


def _pip_install_start_index(tokens: list[str]) -> tuple[list[str], int, int] | None:
    pip_tokens = _conda_run_inner_tokens(tokens) or tokens
    if not pip_tokens:
        return None
    head = Path(str(pip_tokens[0])).name
    if head.startswith("python") and len(pip_tokens) >= 4 and pip_tokens[1] == "-m" and str(pip_tokens[2]) in {"pip", "pip3"} and str(pip_tokens[3]) == "install":
        return pip_tokens, 4, len(tokens) - len(pip_tokens)
    if head in {"pip", "pip3"} and len(pip_tokens) >= 2 and str(pip_tokens[1]) == "install":
        return pip_tokens, 2, len(tokens) - len(pip_tokens)
    return None


def _normalize_pip_package_spec(token: str) -> str:
    package = _token_package_name(token)
    return PIP_PACKAGE_SPEC_OVERRIDES.get(package, str(token))


def _pip_install_package_specs(tokens: list[str]) -> list[str]:
    parsed = _pip_install_start_index(tokens)
    if not parsed:
        return []
    pip_tokens, start, _offset = parsed
    specs: list[str] = []
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
            specs.append(_normalize_pip_package_spec(value))
    return specs


def _non_policy_pip_package_specs(row: dict[str, Any], excluded_names: set[str]) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for spec in _pip_install_package_specs(_row_command_tokens(row)):
        package = _token_package_name(spec)
        if not package or package in excluded_names:
            continue
        normalized = _normalize_pip_package_spec(spec)
        key = _token_package_name(normalized)
        if key and key not in seen:
            seen.add(key)
            specs.append(normalized)
    return specs


def _extra_pip_install_row(source_phase: str, packages: list[str]) -> dict[str, Any]:
    return {
        "phase": f"{source_phase or 'install'}_non_pyg_deps",
        "command": ["python", "-m", "pip", "install", "--upgrade", "--no-cache-dir", *packages],
        "cwd": "repo",
        "timeout_sec": 1800,
        "required": True,
        "policy_managed": True,
    }


def _row_installs_package(row: dict[str, Any], package_name: str) -> bool:
    package = _token_package_name(package_name)
    return package in (_pip_install_packages(_row_command_tokens(row)) | _conda_install_packages(_row_command_tokens(row)))


def _row_imports_module(row: dict[str, Any], module_name: str) -> bool:
    module = re.escape(str(module_name or "").strip())
    if not module:
        return False
    text = command_text(_row_command_tokens(row))
    return bool(re.search(rf"\bimport\s+{module}\b|\bfrom\s+{module}\b", text))


def _esm_install_row() -> dict[str, Any]:
    return _extra_pip_install_row("install_esm_sdk", [ESM_PIP_SPEC]) | {"phase": "install_esm_sdk"}


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


def _row_has_protdis_metrics_keys_mischeck(row: dict[str, Any]) -> bool:
    text = command_text(_row_command_tokens(row))
    lowered = text.lower()
    return "tasks/proteinshake" in lowered and "default_metrics" in lowered and ".keys()" in text


def _row_requires_legacy_biopython(row: dict[str, Any]) -> bool:
    tokens = _row_command_tokens(row)
    packages = _pip_install_packages(tokens) | _conda_install_packages(tokens)
    return bool(packages & {BIOPYTHON_PACKAGE_NAME, ATOM3D_PACKAGE_NAME})


def _plan_or_commands_mention_rigidssl(plan: dict[str, Any], rows: list[Any]) -> bool:
    parts = [
        str(plan.get("env_name") or ""),
        str(plan.get("paper_url") or ""),
        str(plan.get("title") or ""),
        str(plan.get("topic") or ""),
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        parts.append(str(row.get("phase") or ""))
        try:
            parts.append(command_text(_row_command_tokens(row)))
        except Exception:
            parts.append(str(row.get("command") or ""))
    return "rigidssl" in "\n".join(parts).lower()


def _pin_legacy_biopython(row: dict[str, Any]) -> dict[str, Any]:
    updated = dict(row)
    tokens = list(command_tokens(row.get("command")))
    changed = False
    packages = _pip_install_packages(tokens) | _conda_install_packages(tokens)
    for index, token in enumerate(tokens):
        if _token_package_name(str(token)) == BIOPYTHON_PACKAGE_NAME and str(token) != BIOPYTHON_LEGACY_SPEC:
            tokens[index] = BIOPYTHON_LEGACY_SPEC
            changed = True
    if BIOPYTHON_PACKAGE_NAME not in packages and ATOM3D_PACKAGE_NAME in packages:
        insert_at = None
        pip_tokens = _conda_run_inner_tokens(tokens) or tokens
        if pip_tokens:
            head = Path(str(pip_tokens[0])).name
            if head.startswith("python") and len(pip_tokens) >= 4 and pip_tokens[1:4] == ["-m", "pip", "install"]:
                insert_at = len(tokens) - len(pip_tokens) + 4
            elif head in {"pip", "pip3"} and len(pip_tokens) >= 2 and pip_tokens[1] == "install":
                insert_at = len(tokens) - len(pip_tokens) + 2
        if insert_at is not None:
            tokens.insert(insert_at, BIOPYTHON_LEGACY_SPEC)
            changed = True
    if changed:
        updated["command"] = tokens
    return updated


def _replace_python_c_snippet(tokens: list[str], snippet: str) -> list[str] | None:
    inner = _conda_run_inner_tokens(tokens) or tokens
    offset = len(tokens) - len(inner)
    for index in range(offset, len(tokens) - 1):
        if Path(str(tokens[index])).name.startswith("python") and str(tokens[index + 1]) == "-c":
            updated = list(tokens)
            if index + 2 < len(updated):
                updated[index + 2] = snippet
            else:
                updated.append(snippet)
            return updated
    return None


def _protdis_metrics_verify_row(row: dict[str, Any]) -> dict[str, Any]:
    updated = dict(row)
    tokens = list(command_tokens(row.get("command")))
    updated["command"] = _replace_python_c_snippet(tokens, PROTDIS_METRICS_VERIFY_SNIPPET) or ["python", "-c", PROTDIS_METRICS_VERIFY_SNIPPET]
    updated["timeout_sec"] = int(updated.get("timeout_sec") or 300)
    updated["required"] = True
    updated["policy_managed"] = True
    return updated


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


def _conda_create_row(env_name: str) -> dict[str, Any]:
    return {
        "phase": "conda_create",
        "command": ["conda", "create", "-y", "-n", env_name or "environment_env", "python=3.11", "pip"],
        "cwd": "repo",
        "timeout_sec": 900,
        "required": True,
        "policy_managed": True,
    }


def _row_creates_conda_env(row: dict[str, Any]) -> bool:
    tokens = _row_command_tokens(row)
    action = _conda_action(tokens)
    return action == "create" or (len(tokens) >= 3 and Path(str(tokens[0])).name in {"conda", "mamba", "micromamba"} and str(tokens[1]) == "env" and str(tokens[2]) == "create")


def _torch_cuda_install_row(source_phase: str) -> dict[str, Any]:
    return {
        "phase": source_phase or "install_torch_cuda",
        "command": [
            "python", "-m", "pip", "install", "--upgrade", "--no-cache-dir",
            f"torch=={PYTORCH_CUDA_VERSION}+cu128", TORCHVISION_CUDA_SPEC, TORCHAUDIO_CUDA_SPEC,
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
    needs_biopython_legacy = any(isinstance(row, dict) and _row_requires_legacy_biopython(row) for row in rows) and _plan_or_commands_mention_rigidssl(plan, rows)
    has_protdis_metrics_mischeck = any(isinstance(row, dict) and _row_has_protdis_metrics_keys_mischeck(row) for row in rows)
    if not (needs_pyg or (_machine_needs_modern_cuda_stack(machine) and conda_installs_torch) or needs_biopython_legacy or has_protdis_metrics_mischeck):
        return plan

    normalized = dict(plan)
    rewrites: list[dict[str, Any]] = []
    normalized["python_version"] = "3.11"
    new_rows: list[dict[str, Any]] = []
    torch_row_present = False
    pyg_row_present = False
    conda_create_present = any(isinstance(row, dict) and _row_creates_conda_env(row) for row in rows)
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            new_rows.append(raw_row)
            continue
        row = dict(raw_row)
        if _row_creates_conda_env(row):
            updated = _normalize_create_command_python(row, "3.11")
            conda_create_present = True
            if updated.get("command") != row.get("command"):
                rewrites.append({"index": index, "phase": row.get("phase"), "reason": "RTX 5090/modern CUDA stack requires Python 3.11 for current PyTorch/PyG binary wheels", "before": row.get("command"), "after": updated.get("command")})
            row = updated
        if needs_biopython_legacy and _row_requires_legacy_biopython(row):
            updated = _pin_legacy_biopython(row)
            if updated.get("command") != row.get("command"):
                rewrites.append({"index": index, "phase": row.get("phase"), "reason": "RigidSSL dataset code imports Bio.PDB.Polypeptide.three_to_one, which is absent from latest Biopython; pin the Python 3.11 compatible legacy wheel", "before": row.get("command"), "after": updated.get("command")})
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
                rewrites.append({"index": index, "phase": row.get("phase"), "reason": "PyG pip install must use the torch/CUDA matched wheel index and all required extension packages; non-PyG package specs from the original install are preserved in a separate pip row", "before": row.get("command"), "after": replacement.get("command")})
                new_rows.append(replacement)
                preserved = _non_policy_pip_package_specs(row, PYG_PIP_PACKAGE_NAMES | PYTORCH_PACKAGE_NAMES)
                if preserved:
                    extra = _extra_pip_install_row(str(row.get("phase") or "install"), preserved)
                    new_rows.append(extra)
                    rewrites.append({"index": index, "phase": extra.get("phase"), "reason": "Preserve non-PyG dependencies from a policy-rewritten PyG install row", "after": extra.get("command")})
                pyg_row_present = True
                continue
        if _pip_install_packages(_row_command_tokens(row)) & {"torch", "torchvision", "torchaudio"}:
            text_command = command_text(_row_command_tokens(row))
            replacement = _torch_cuda_install_row(str(row.get("phase") or "install_torch_cuda"))
            if (
                f"torch=={PYTORCH_CUDA_VERSION}+cu128" not in text_command
                or TORCHVISION_CUDA_SPEC not in text_command
                or TORCHAUDIO_CUDA_SPEC not in text_command
                or PYTORCH_CUDA_INDEX_URL not in text_command
            ):
                rewrites.append({"index": index, "phase": row.get("phase"), "reason": "PyTorch pip install must use a coherent CUDA 12.8 wheel set verified on RTX 5090/Blackwell", "before": row.get("command"), "after": replacement.get("command")})
                new_rows.append(replacement)
                torch_row_present = True
                continue
            torch_row_present = True
        if _row_installs_pyg_with_pip(row):
            pyg_row_present = True
        if _row_has_protdis_metrics_keys_mischeck(row):
            updated = _protdis_metrics_verify_row(row)
            rewrites.append({"index": index, "phase": row.get("phase"), "reason": "ProtDiS tasks/proteinshake default_metrics is a function, not a mapping; verify the metrics module by calling default_metrics and compute_metrics instead of inspecting .keys()", "before": row.get("command"), "after": updated.get("command")})
            row = updated
        new_rows.append(row)

    if (needs_pyg or torch_row_present or conda_installs_torch) and not conda_create_present:
        row = _conda_create_row(str(normalized.get("env_name") or ""))
        new_rows.insert(0, row)
        conda_create_present = True
        rewrites.append({"index": None, "phase": "conda_create", "reason": "Policy-managed pip/torch/PyG installs require a run-local Conda prefix before conda run can execute", "after": row["command"]})
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
    if any(isinstance(row, dict) and _row_imports_module(row, "esm") for row in new_rows) and not any(isinstance(row, dict) and _row_installs_package(row, "esm") for row in new_rows):
        row = _esm_install_row()
        insert_at = next((i for i, item in enumerate(new_rows) if isinstance(item, dict) and _row_imports_module(item, "esm")), len(new_rows))
        new_rows.insert(insert_at, row)
        rewrites.append({"index": None, "phase": "install_esm_sdk", "reason": "Verify/import commands require the esm Python module; install the ESM SDK before import gates", "after": row["command"]})

    normalized["commands"] = new_rows
    if rewrites:
        normalized["plan_policy_rewrites"] = [*(normalized.get("plan_policy_rewrites") if isinstance(normalized.get("plan_policy_rewrites"), list) else []), *rewrites]
        normalized["backend_dependency_policy"] = {
            "policy_version": policy_version,
            "python_version": "3.11",
            "torch_version": PYTORCH_CUDA_VERSION,
            "torchvision_spec": TORCHVISION_CUDA_SPEC,
            "torchaudio_spec": TORCHAUDIO_CUDA_SPEC,
            "torch_cuda_index_url": PYTORCH_CUDA_INDEX_URL,
            "pyg_wheel_url": PYG_CUDA_WHEEL_URL,
            "biopython_legacy_spec": BIOPYTHON_LEGACY_SPEC if needs_biopython_legacy else "",
            "reason": "RTX 5090/compute 12.0 requires a modern CUDA-enabled PyTorch stack; conda pyg packages did not solve for the generated Python/PyTorch/CUDA matrix.",
        }
    return normalized
