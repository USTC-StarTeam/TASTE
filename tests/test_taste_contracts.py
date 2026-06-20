from __future__ import annotations

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
