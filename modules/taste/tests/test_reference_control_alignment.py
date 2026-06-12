import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("experiment_contracts", SCRIPTS / "experiment_contracts.py")
experiment_contracts = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(experiment_contracts)


def _passed_reference_gate():
    return {
        "status": "pass",
        "best_reproduction": {
            "experiment_id": "fix_ref_sasrec_2layer_amazon-beauty_30epoch_20260612_033223",
            "method": "selected_base_reference",
            "dataset": "amazon-beauty",
            "metric_name": "ndcg_at_10",
            "metric_value": 0.0508,
            "audit_ready": True,
            "artifact_path": "/artifacts/fix_ref",
            "audit_path": "/state/fresh_base_reference_full_reproduction_audit.json",
            "artifact_audit_path": "/artifacts/fix_ref/audit.json",
            "repo_path": "/repos/selected/rsir",
            "repo_name": "USTC-StarTeam/RSIR",
            "mode": "full",
        },
    }


def test_align_reference_best_control_injects_passed_reference_when_control_missing():
    progress = {
        "status": "blocked",
        "margin": 0.005,
        "metric_name": "ndcg_at_10",
        "best_candidate": {},
        "best_control": {},
        "best_audit_ready_control": {},
        "control_real_runs": 0,
        "control_audit_ready_runs": 0,
        "excluded_control_reasons": {"old_256_layer_reference": ["missing_bad_case_slices"]},
        "blockers": [
            "No comparable real-data baseline/control metric exists.",
            "No audit-ready comparable baseline/control run exists.",
            "No audit-ready real-data candidate/proposed-method run exists.",
        ],
    }

    aligned = experiment_contracts.align_reference_best_control(progress, _passed_reference_gate())

    assert aligned["best_control"]["experiment_id"] == "fix_ref_sasrec_2layer_amazon-beauty_30epoch_20260612_033223"
    assert aligned["best_control"]["comparison_role"] == "reference"
    assert aligned["control_real_runs"] == 1
    assert aligned["control_audit_ready_runs"] == 1
    assert aligned["excluded_control_reasons"] == {}
    assert "No comparable real-data baseline/control metric exists." not in aligned["blockers"]
    assert "No audit-ready comparable baseline/control run exists." not in aligned["blockers"]
    assert "No audit-ready real-data candidate/proposed-method run exists." in aligned["blockers"]
    assert aligned["status"] == "blocked"


def test_align_reference_best_control_rechecks_candidate_against_injected_reference():
    progress = {
        "status": "pass",
        "margin": 0.005,
        "metric_name": "ndcg_at_10",
        "best_candidate": {
            "experiment_id": "candidate_small_delta",
            "method": "candidate",
            "dataset": "amazon-beauty",
            "metric_name": "ndcg_at_10",
            "metric_value": 0.051,
            "audit_ready": True,
            "status": "completed",
            "comparison_role": "candidate",
        },
        "best_control": {},
        "best_audit_ready_control": {},
        "control_real_runs": 0,
        "control_audit_ready_runs": 0,
        "blockers": [],
    }

    aligned = experiment_contracts.align_reference_best_control(progress, _passed_reference_gate())

    assert aligned["comparison_pass"] is False
    assert aligned["status"] == "blocked"
    assert any("candidate=0.051" in blocker and "control=0.0508" in blocker for blocker in aligned["blockers"])


def test_align_reference_best_control_updates_existing_reference_with_metrics_dict():
    progress = {
        "status": "blocked",
        "margin": 0.005,
        "metric_name": "ndcg_at_10",
        "best_candidate": {},
        "best_control": {
            "experiment_id": "fix_ref_sasrec_2layer_amazon-beauty_30epoch_20260612_033223",
            "method": "selected_base_reference",
            "dataset": "amazon-beauty",
            "metric_name": "ndcg_at_10",
            "metric_value": 0.0508,
            "metrics": {"ndcg_at_10": 0.0508},
        },
        "best_audit_ready_control": {},
        "control_real_runs": 1,
        "control_audit_ready_runs": 1,
        "blockers": ["No audit-ready real-data candidate/proposed-method run exists."],
    }

    aligned = experiment_contracts.align_reference_best_control(progress, _passed_reference_gate())

    assert aligned["best_control"]["metrics"] == {"ndcg_at_10": 0.0508}
    assert aligned["best_control"]["artifact_path"] == "/artifacts/fix_ref"
    assert aligned["status"] == "blocked"
