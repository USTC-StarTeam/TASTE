#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _scalar(value: Any) -> Any:
    try:
        import numpy as np  # type: ignore

        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return list(value)
    except Exception:
        return []


def _targets_for(task: Any, split: str) -> list[Any]:
    targets = _as_list(getattr(task, f"{split}_targets", None))
    if targets:
        return [_scalar(item) for item in targets]
    index = _as_list(getattr(task, f"{split}_index", None))
    all_targets = _as_list(getattr(task, "target", None)) or _as_list(getattr(task, "targets", None))
    if index and all_targets:
        return [_scalar(all_targets[int(item)]) for item in index if int(item) < len(all_targets)]
    return []


def _accuracy(gold: list[Any], pred_label: Any) -> float:
    if not gold:
        return 0.0
    return sum(1 for item in gold if item == pred_label) / len(gold)


def _macro_f1_for_majority(gold: list[Any], pred_label: Any) -> float:
    if not gold:
        return 0.0
    scores: list[float] = []
    for label in sorted(set(gold), key=lambda item: str(item)):
        tp = sum(1 for item in gold if item == label and pred_label == label)
        fp = sum(1 for item in gold if item != label and pred_label == label)
        fn = sum(1 for item in gold if item == label and pred_label != label)
        denom = 2 * tp + fp + fn
        scores.append((2 * tp / denom) if denom else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def _entropy(labels: list[Any]) -> float:
    total = len(labels)
    if total <= 0:
        return 0.0
    counts = Counter(labels)
    return -sum((count / total) * math.log(count / total, 2) for count in counts.values())


def _task_probe(root: Path, task_name: str) -> dict[str, Any]:
    if task_name != "ProteinFamilyTask":
        return {
            "task": task_name,
            "status": "unsupported_task",
            "error": "Only ProteinFamilyTask is implemented as a bounded real-data probe.",
        }
    from proteinshake.tasks import ProteinFamilyTask  # type: ignore

    started = time.time()
    task = ProteinFamilyTask(root=str(root), use_precomputed=True, n_jobs=1, skip_signature_check=True, verbosity=1)
    train_targets = _targets_for(task, "train")
    val_targets = _targets_for(task, "val")
    test_targets = _targets_for(task, "test")
    majority_label, majority_count = (None, 0)
    if train_targets:
        majority_label, majority_count = Counter(train_targets).most_common(1)[0]
    metrics = {
        "proteinshake_train_samples": len(train_targets),
        "proteinshake_val_samples": len(val_targets),
        "proteinshake_test_samples": len(test_targets),
        "proteinshake_num_classes": int(getattr(task, "num_classes", 0) or 0),
        "proteinshake_train_majority_class_fraction": (majority_count / len(train_targets)) if train_targets else 0.0,
        "proteinshake_val_majority_accuracy": _accuracy(val_targets, majority_label),
        "proteinshake_test_majority_accuracy": _accuracy(test_targets, majority_label),
        "proteinshake_val_majority_macro_f1": _macro_f1_for_majority(val_targets, majority_label),
        "proteinshake_test_majority_macro_f1": _macro_f1_for_majority(test_targets, majority_label),
        "proteinshake_train_label_entropy_bits": _entropy(train_targets),
    }
    return {
        "task": task_name,
        "dataset": "ProteinShake ProteinFamilyTask",
        "status": "loaded",
        "elapsed_sec": round(time.time() - started, 3),
        "root": str(root),
        "task_type": list(getattr(task, "task_type", [])),
        "metrics": metrics,
        "majority_label": _scalar(majority_label),
        "split_sizes": {"train": len(train_targets), "val": len(val_targets), "test": len(test_targets)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe a bounded real ProteinShake dataset and write TASTE experiment artifacts.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--task", default="ProteinFamilyTask")
    args = parser.parse_args(argv)

    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    data_root = Path(args.data_root).expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        probe = _task_probe(data_root, args.task)
        metrics = probe.get("metrics") if isinstance(probe.get("metrics"), dict) else {}
        accepted = bool(
            probe.get("status") == "loaded"
            and metrics.get("proteinshake_train_samples", 0)
            and metrics.get("proteinshake_test_samples", 0)
        )
        blockers = [] if accepted else [{
            "code": "proteinshake_real_data_unavailable",
            "message": "ProteinShake real-data task did not load usable train/test labels.",
            "evidence": probe,
        }]
        summary = {
            "status": "completed" if accepted else "blocked",
            "acceptance_status": "accepted_real_data_probe" if accepted else "blocked_real_data_unavailable",
            "dataset": "ProteinShake ProteinFamilyTask",
            "commands": [{
                "command": "proteinshake.tasks.ProteinFamilyTask(use_precomputed=True)",
                "status": probe.get("status"),
                "data_root": str(data_root),
            }],
            "metrics": metrics,
            "acceptance_blockers": blockers,
            "next_action": "在该真实数据划分上接入 ProtDiS 编码或 GP 排序器；ProtDBench/PDFBench 主结论仍需对应数据/工具链。",
            "elapsed_sec": round(time.time() - started, 3),
        }
        audit = {
            "status": "real_data_probe_passed" if accepted else "real_data_probe_blocked",
            "probe": probe,
            "claim_verdict": "weak_real_data_plumbing_only" if accepted else "unsupported",
            "limits": [
                "该探针只证明 ProteinShake ProteinFamily 真实数据可下载、可加载并可计算标签基线。",
                "该探针不等价于 ProtDBench wet-lab 结合亲和力实验，也不覆盖 PDFBench 16 维评估。",
                "尚未接入 ProtDiS 表示解耦编码器或 Flexible Kernels/GP 排序器。",
            ],
        }
        write_json(artifact_dir / "proteinshake_realdata_probe.json", probe)
        write_json(artifact_dir / "metrics.json", metrics)
        write_json(artifact_dir / "audit.json", audit)
        write_json(artifact_dir / "experiment_iteration_summary.json", summary)
        print(json.dumps({"status": summary["status"], "artifact_dir": str(artifact_dir), "metrics": metrics}, ensure_ascii=False))
        return 0 if accepted else 2
    except Exception as exc:
        probe = {
            "task": args.task,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
            "root": str(data_root),
        }
        summary = {
            "status": "blocked",
            "acceptance_status": "blocked_real_data_probe_failed",
            "dataset": "ProteinShake",
            "commands": [{"command": "proteinshake real-data probe", "status": "failed", "data_root": str(data_root)}],
            "metrics": {},
            "acceptance_blockers": [{"code": "proteinshake_real_data_probe_failed", "message": str(exc), "evidence": probe}],
            "next_action": "修复 ProteinShake 包/网络/数据缓存后重新运行真实数据探针；禁止降级为 synthetic demo。",
        }
        write_json(artifact_dir / "proteinshake_realdata_probe.json", probe)
        write_json(artifact_dir / "metrics.json", {})
        write_json(artifact_dir / "audit.json", {"status": "failed", "probe": probe})
        write_json(artifact_dir / "experiment_iteration_summary.json", summary)
        print(json.dumps({"status": "failed", "artifact_dir": str(artifact_dir), "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
