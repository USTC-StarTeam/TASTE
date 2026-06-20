#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from file_utils import atomic_write_json, compact_text, now_iso

COLUMNS = [
    "时间", "运行ID", "实验ID", "迭代", "状态", "方法/变体", "数据集", "运行环境",
    "关键命令", "指标", "Claude结果", "审计/坏例", "下一步", "证据路径",
]

METRIC_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_@./-]{1,48})\s*[:=]\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
METRIC_HINTS = ("metric", "loss", "acc", "accuracy", "precision", "recall", "ndcg", "auc", "f1", "map", "mrr", "hit", "hr")


def load_registry(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        payload = []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_metrics(target: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    candidates = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else payload
    if isinstance(candidates, dict):
        for key, value in candidates.items():
            if isinstance(value, (int, float, str)) and parse_float(value) is not None:
                target[str(key)] = parse_float(value)


def _metric_scan_text(text: str) -> str:
    # Claude JSON output and shell headers may contain escaped newlines such as
    # \naccuracy=0.91. Decode the common escapes before regex scanning so
    # they do not become bogus keys like naccuracy.
    cleaned = text.replace("\\n", "\n").replace("\\t", "\t")
    return "\n".join(line for line in cleaned.splitlines() if not line.startswith("# command:"))


def collect_metrics(artifact_dir: Path, log_paths: list[Path]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for path in [artifact_dir / "metrics.json", artifact_dir / "audit.json", artifact_dir / "experiment_summary.json", artifact_dir / "experiment_iteration_summary.json"]:
        try:
            _merge_metrics(metrics, json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})
        except Exception:
            pass
    for log_path in log_paths:
        try:
            text = _metric_scan_text(log_path.read_text(encoding="utf-8", errors="replace")[-60000:])
        except Exception:
            continue
        for key, value in METRIC_KEY_RE.findall(text):
            low = key.lower()
            if any(hint in low for hint in METRIC_HINTS):
                metrics[key] = parse_float(value)
    return metrics


def primary_metric(metrics: dict[str, Any], preferred: str = "") -> tuple[str, Any]:
    if preferred and preferred in metrics:
        return preferred, metrics[preferred]
    preferred_lower = preferred.lower()
    if preferred_lower:
        for key, value in metrics.items():
            if key.lower() == preferred_lower:
                return key, value
    for key in ["ndcg_at_10", "NDCG@10", "accuracy", "acc", "f1", "loss"]:
        if key in metrics:
            return key, metrics[key]
    for key, value in metrics.items():
        return key, value
    return "", None


def upsert_record(registry_path: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = load_registry(registry_path)
    identity = (record.get("run_id"), record.get("iteration"))
    for index, row in enumerate(rows):
        if (row.get("run_id"), row.get("iteration")) == identity:
            merged = dict(row)
            merged.update({key: value for key, value in record.items() if value not in (None, "", [], {})})
            rows[index] = merged
            atomic_write_json(registry_path, rows)
            return rows
    rows.append(record)
    atomic_write_json(registry_path, rows)
    return rows


def _metrics_text(row: dict[str, Any]) -> str:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    if metrics:
        return "; ".join(f"{key}={value}" for key, value in metrics.items())
    return "未解析到指标"


def _evidence_text(row: dict[str, Any]) -> str:
    pieces = []
    for key, label in [
        ("artifact_path", "产物目录"),
        ("claude_log_path", "Claude日志"),
        ("validation_log_path", "验证日志"),
        ("environment_lock_path", "环境锁"),
    ]:
        value = str(row.get(key) or "").strip()
        if value:
            pieces.append(f"{label}: {value}")
    return "; ".join(pieces) or "未登记"


def table_rows(registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in sorted(registry, key=lambda item: str(item.get("timestamp") or "")):
        status = str(row.get("status") or "")
        next_action = row.get("next_action") or ("停止并分析可用证据" if status == "success" else "修复失败原因后继续下一轮")
        bad_case = ""
        for key in ["bad_case_path", "audit_path"]:
            if row.get(key):
                bad_case = str(row.get(key))
                break
        rows.append({
            "时间": row.get("timestamp", ""),
            "运行ID": row.get("run_id", ""),
            "实验ID": row.get("experiment_id", ""),
            "迭代": row.get("iteration", ""),
            "状态": status,
            "方法/变体": row.get("method", ""),
            "数据集": row.get("dataset", ""),
            "运行环境": row.get("env_name", ""),
            "关键命令": compact_text(row.get("command", ""), 260),
            "指标": _metrics_text(row),
            "Claude结果": compact_text(row.get("claude_summary") or row.get("claude_status") or "", 260),
            "审计/坏例": bad_case or row.get("audit_status", "未登记"),
            "下一步": compact_text(next_action, 260),
            "证据路径": _evidence_text(row),
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in COLUMNS})


def write_markdown(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# 实验记录表\n\n",
        f"- 更新时间: {now_iso()}\n",
        f"- 记录数: {len(rows)}\n",
        "- 说明: 该表由 experimenting 独立后端根据运行日志、指标文件和 Claude 迭代结果生成。\n\n",
        "| 时间 | 运行ID | 实验ID | 迭代 | 状态 | 方法 | 数据集 | 指标 | 下一步 |\n",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n",
    ]
    for row in reversed(rows[-80:]):
        values = [compact_text(row.get(key, ""), 140).replace("|", "/") for key in ["时间", "运行ID", "实验ID", "迭代", "状态", "方法/变体", "数据集", "指标", "下一步"]]
        lines.append("| " + " | ".join(values) + " |\n")
    path.write_text("".join(lines), encoding="utf-8")


def write_record_tables(output_root: Path, registry: list[dict[str, Any]]) -> dict[str, str]:
    state = output_root / "state"
    records = output_root / "records"
    state.mkdir(parents=True, exist_ok=True)
    records.mkdir(parents=True, exist_ok=True)
    rows = table_rows(registry)
    table_payload = {"updated_at": now_iso(), "row_count": len(rows), "columns": COLUMNS, "rows": rows}
    json_path = state / "experiment_record_table.json"
    csv_path = records / "experiment_records.csv"
    md_path = records / "实验记录.md"
    atomic_write_json(json_path, table_payload)
    write_csv(csv_path, rows)
    write_markdown(md_path, rows)
    return {"record_table_json": str(json_path), "record_table_csv": str(csv_path), "record_table_md": str(md_path)}
