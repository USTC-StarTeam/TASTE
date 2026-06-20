from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")


def test_all_stage_contracts_and_framework_dry_run_are_callable():
    for stage in STAGES:
        proc = subprocess.run([sys.executable, str(ROOT / "modules" / stage / "main.py"), "--contract"], cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, (stage, proc.stderr or proc.stdout)
        payload = json.loads(proc.stdout)
        assert payload["stage"] == stage
        assert payload["entrypoint"] == f"modules/{stage}/main.py"
        assert payload["scripts_are_private_backend"] is True
        assert payload["required_external_inputs"]
        assert payload["artifacts_out"]

    run_id = "pytest_contract_dry_run"
    state_root = ROOT / "framework" / "workspace" / "pytest"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"),
            "run",
            "--mode",
            "dry-run",
            "--strategy",
            "deterministic",
            "--research-goal",
            "pytest contract smoke",
            "--run-id",
            run_id,
            "--state-root",
            str(state_root),
            "--no-contract-probe",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    status = json.loads((state_root / "runs" / run_id / "public" / "frontend_status.json").read_text(encoding="utf-8"))
    assert status["progress"] == {"completed": 7, "total": 7, "percent": 100.0}
    assert status["status"] == "paper_pipeline_finished"


def test_framework_only_stage_reports_single_stage_scope():
    run_id = "pytest_only_environment"
    state_root = ROOT / "framework" / "workspace" / "pytest"
    plan_path = state_root / "pytest_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"title": "pytest plan", "repo_url": "https://github.com/example/repo"}), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"),
            "run",
            "--mode",
            "dry-run",
            "--strategy",
            "deterministic",
            "--only-stage",
            "environment",
            "--research-goal",
            "pytest single stage",
            "--run-id",
            run_id,
            "--state-root",
            str(state_root),
            "--plan-json",
            str(plan_path),
            "--module-arg",
            f"environment=--plan {plan_path} --run-id {run_id}",
            "--no-contract-probe",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    status = json.loads((state_root / "runs" / run_id / "public" / "frontend_status.json").read_text(encoding="utf-8"))
    assert status["stage_scope"] == ["environment"]
    assert status["progress"] == {"completed": 1, "total": 1, "percent": 100.0}
    assert status["status"] == "stage_scope_finished"


def test_environment_dependency_policy_rewrites_pyg_conda_plan():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)
    normalize_environment_plan_commands = dependency_policy.normalize_environment_plan_commands

    plan = {
        "python_version": "3.9",
        "commands": [
            {"phase": "conda_create", "command": ["conda", "create", "-n", "rigid", "python=3.9", "pip", "-y"], "required": True},
            {"phase": "conda_install_pytorch", "command": ["conda", "install", "-n", "rigid", "pytorch>=2.5.1", "pytorch-cuda>=12.4", "-y"], "required": True},
            {"phase": "conda_install_pyg", "command": ["conda", "install", "-n", "rigid", "-c", "pyg", "pyg", "pytorch-scatter", "pytorch-sparse", "pytorch-cluster", "-y"], "required": True},
            {"phase": "verify_import", "command": ["conda", "run", "-n", "rigid", "python", "-c", "import torch_geometric"], "required": True},
        ],
    }
    machine = {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}

    normalized = normalize_environment_plan_commands(plan, machine=machine, policy_version="test-policy")
    commands = [row["command"] for row in normalized["commands"]]
    command_text = "\n".join(" ".join(command) for command in commands)

    assert normalized["python_version"] == "3.11"
    assert normalized["commands"][0]["command"] == ["conda", "create", "-n", "rigid", "python=3.11", "pip", "-y"]
    assert "torch==2.9.1+cu128" in command_text
    assert "https://download.pytorch.org/whl/cu128" in command_text
    assert "https://data.pyg.org/whl/torch-2.9.1+cu128.html" in command_text
    assert "conda install -n rigid -c pyg pyg" not in command_text
    assert any(row["phase"] == "verify_pyg_cuda_import" for row in normalized["commands"])
    assert normalized["backend_dependency_policy"]["policy_version"] == "test-policy"
    assert len(normalized["plan_policy_rewrites"]) >= 4
