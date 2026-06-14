from pathlib import Path

from path_helpers import load_script

ROOT = Path(__file__).resolve().parents[1]
build_experiment_record_table = load_script("build_experiment_record_table")


def test_invalidated_reference_reflection_does_not_call_it_current_baseline():
    row = {
        "status": "invalidated",
        "comparison_role": "reference",
        "comparison_status": "not_comparable",
        "method": "selected_base_reference",
        "dataset": "amazon-beauty",
        "metric_name": "ndcg_at_10",
        "metric_value": 0.0059,
        "notes": "invalidated_by_num_layer_256_config",
        "counterexample_outcome": "旧参考复现 NDCG@10=0.0059 来自错误的 256 层配置；当前有效基准是 2 层修复复现 NDCG@10=0.0508。",
    }

    text = build_experiment_record_table.reflection(row)

    assert "当前有效基准" in text
    assert "建立比较基准" not in text


def test_not_comparable_candidate_reflection_uses_counterexample_context():
    row = {
        "status": "completed",
        "comparison_role": "candidate",
        "comparison_status": "not_comparable",
        "method": "adacurr_synrec_v1",
        "dataset": "amazon-beauty",
        "metric_name": "ndcg_at_10",
        "metric_value": 0.0059,
        "counterexample_outcome": "修复后当前参考 NDCG@10=0.0508，v5 NDCG@10=0.0059 不能作为有效候选提升证据。",
    }

    text = build_experiment_record_table.reflection(row)

    assert "0.0508" in text
    assert "不能作为有效候选提升证据" in text
