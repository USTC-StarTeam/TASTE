#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, load_project_config


COLUMNS = [
    "时间",
    "实验ID",
    "实验目的",
    "方法/变体",
    "仓库",
    "数据集",
    "运行环境",
    "关键配置/命令",
    "指标",
    "坏例/切片",
    "审计状态",
    "结论/反思",
    "下一步行动",
    "证据路径",
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def one_line(value: Any, limit: int = 360) -> str:
    if isinstance(value, (list, tuple)):
        text = "; ".join(str(item) for item in value if str(item).strip())
    elif isinstance(value, dict):
        text = "; ".join(f"{key}={val}" for key, val in value.items() if not isinstance(val, (dict, list)))
    else:
        text = str(value or "")
    text = " ".join(text.replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def metric_value(row: dict[str, Any], key: str = "ndcg_at_10") -> Any:
    metrics = row.get("metrics")
    if isinstance(metrics, dict) and key in metrics:
        return metrics.get(key)
    metric_name = str(row.get("metric_name") or row.get("metric") or "").strip().lower()
    if metric_name == key:
        return row.get("metric_value", row.get("result"))
    return None


def format_metric(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "未记录"
    return f"{number:.4f}"


def display_config(cfg: dict[str, Any]) -> dict[str, Any]:
    exp = cfg.get("experiment", {}) if isinstance(cfg, dict) else {}
    return exp.get("display_labels", {}) if isinstance(exp.get("display_labels", {}), dict) else {}


def role_config(cfg: dict[str, Any]) -> dict[str, Any]:
    exp = cfg.get("experiment", {}) if isinstance(cfg, dict) else {}
    return exp.get("method_role_policy", {}) if isinstance(exp.get("method_role_policy", {}), dict) else {}


def lookup_label(value: Any, mapping: dict[str, Any] | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    mapping = mapping or {}
    variants = [text, re.sub(r"_trial_\d+$", "", text), re.sub(r"^repo_cycle\d+_", "", re.sub(r"_trial_\d+$", "", text))]
    for item in variants:
        label = mapping.get(item)
        if label:
            return str(label)
    return variants[-1].replace("_", " ").strip()


def clean_identifier(value: Any, labels: dict[str, Any] | None = None) -> str:
    return lookup_label(value, labels)


def repo_label(row: dict[str, Any], labels: dict[str, Any] | None = None) -> str:
    repo = str(row.get("repo") or "").strip()
    repo_path = str(row.get("repo_path") or "").strip()
    mapping = labels or {}
    for key in [repo, Path(repo_path).name if repo_path else "", repo_path]:
        if key and mapping.get(key):
            return str(mapping[key])
    return repo or (Path(repo_path).name.replace("_", " ") if repo_path else "未记录")


def dataset_label(row: dict[str, Any], labels: dict[str, Any] | None = None) -> str:
    dataset = str(row.get("dataset") or row.get("benchmark") or "").strip()
    if not dataset:
        return "未记录"
    mapping = labels or {}
    if mapping.get(dataset):
        return str(mapping[dataset])
    return dataset.replace("_", " ")


def env_label(row: dict[str, Any], labels: dict[str, Any] | None = None) -> str:
    env = str(row.get("env_name") or "").strip()
    if not env:
        return "未记录"
    mapping = labels or {}
    return str(mapping.get(env) or env)


def row_role(row: dict[str, Any], role_policy: dict[str, Any] | None = None) -> str:
    for key in ("comparison_role", "method_role", "claim_role", "experiment_role", "role"):
        value = str(row.get(key) or "").strip()
        if value:
            return value.replace("_", " ")
    method = str(row.get("method") or row.get("method_slug") or "").strip()
    roles = (role_policy or {}).get("method_roles", {}) if isinstance(role_policy, dict) else {}
    if isinstance(roles, dict) and method in roles:
        return str(roles[method]).replace("_", " ")
    return ""


def readable_goal(row: dict[str, Any], method_labels: dict[str, Any] | None = None, dataset_labels: dict[str, Any] | None = None, role_policy: dict[str, Any] | None = None) -> str:
    for key in ("human_goal", "goal", "hypothesis", "experiment_goal"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    method = str(row.get("method") or row.get("method_slug") or "").strip()
    dataset = dataset_label(row, dataset_labels)
    notes = str(row.get("notes") or "")
    for part in notes.split(";"):
        part = part.strip()
        if part.startswith("focus="):
            focus = part.removeprefix("focus=").strip()
            return TRANSLATIONS.get(focus, focus)
    if "synthetic" in str(row.get("dataset") or "").lower():
        return "验证实验流水线能运行；该记录不支撑论文科学结论"
    role = row_role(row, role_policy)
    role_text = f"（{role}）" if role else ""
    method_label = clean_identifier(method, method_labels) or "未命名方法"
    return f"验证 {method_label}{role_text} 在 {dataset} 上是否产生可审计证据"


TRANSLATIONS = {
    "baseline reproduction on the selected repo and dataset": "复现当前仓库与数据集上的基线表现",
    "targeted hyperparameter sweep aimed at the current weakest slice": "围绕当前最弱切片做定向超参数尝试",
}


def metrics_text(row: dict[str, Any]) -> str:
    metrics: dict[str, Any] = {}
    raw = row.get("metrics")
    if isinstance(raw, dict):
        metrics.update({str(k): v for k, v in raw.items() if not isinstance(v, (dict, list))})
    name = str(row.get("metric_name") or row.get("metric") or "").strip()
    value = row.get("metric_value")
    if (value is None or value == "") and row.get("result") not in (None, ""):
        value = row.get("result")
    if name and name not in metrics and value not in (None, ""):
        metrics[name] = value
    return "; ".join(f"{key}={value}" for key, value in metrics.items() if str(key).strip()) or one_line(value)


def infer_goal(row: dict[str, Any], method_labels: dict[str, Any] | None = None, dataset_labels: dict[str, Any] | None = None, role_policy: dict[str, Any] | None = None) -> str:
    return one_line(readable_goal(row, method_labels, dataset_labels, role_policy), 220)


def audit_text(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "").strip().lower()
    if status == "running":
        epoch = row.get("progress_epoch")
        planned = row.get("planned_epochs")
        progress = f"；进度 epoch {epoch}/{planned}" if epoch not in (None, "") and planned not in (None, "") else ""
        return "运行中：等待训练完成后审计，不可作为论文结论" + progress
    if row.get("audit_ready"):
        return "通过：证据文件齐全"
    missing = row.get("missing_audit_fields", [])
    if isinstance(missing, list) and missing:
        return "阻塞：缺少 " + ", ".join(str(item) for item in missing)
    return "未通过/未记录"


def reflection(
    row: dict[str, Any],
    method_labels: dict[str, Any] | None = None,
    dataset_labels: dict[str, Any] | None = None,
    role_policy: dict[str, Any] | None = None,
) -> str:
    verdict = str(row.get("claim_verdict") or "").strip()
    novelty = str(row.get("novelty_note") or "").strip()
    counter = str(row.get("counterexample_outcome") or "").strip()
    status = str(row.get("status") or "").strip()
    combined = " ".join(item for item in [novelty, counter, str(row.get("notes") or "")] if item)
    dataset = dataset_label(row, dataset_labels)
    method = clean_identifier(row.get("method") or row.get("method_slug") or "", method_labels) or "本次方法"
    ndcg = metric_value(row, "ndcg_at_10")
    best_ndcg = metric_value(row, "best_ndcg_at_10")
    metric_hint = f"NDCG@10={format_metric(ndcg)}" if ndcg not in (None, "") else ""
    if best_ndcg not in (None, ""):
        metric_hint = (metric_hint + "，" if metric_hint else "") + f"最好记录={format_metric(best_ndcg)}"
    if "synthetic" in str(row.get("dataset") or "").lower():
        return "这是流程自测，只证明流水线能跑通，不能用于论文结论。"
    role = row_role(row, role_policy).lower()
    if status.lower() == "running":
        progress = ""
        if row.get("progress_epoch") not in (None, "") and row.get("planned_epochs") not in (None, ""):
            progress = f"，当前 epoch {row.get('progress_epoch')}/{row.get('planned_epochs')}"
        metric_part = f"；最近评测 {metric_hint}" if metric_hint else ""
        return one_line(f"本次实验仍在运行{progress}{metric_part}。当前记录只用于实时监督，不能支撑论文结论，需等待 artifact-local audit。")
    if status.lower() in {"interrupted", "timeout", "failed", "error"}:
        prefix = "本次实验未完整跑完"
        if status.lower() == "interrupted":
            prefix = "本次实验中途停止"
        detail = f"；已有中间结果为 {metric_hint}" if metric_hint else "；需要先补齐完整运行日志和结果"
        return one_line(prefix + detail + "。")
    if role in {"reference", "baseline", "control", "ablation"}:
        role_text = {
            "reference": "参考复现",
            "baseline": "基线",
            "control": "对照",
            "ablation": "消融对照",
        }.get(role, "对照")
        detail = f"本次是{role_text}实验，用来建立比较基准"
        if metric_hint:
            detail += f"（{metric_hint}）"
        return one_line(detail + "，不是候选创新结果。")
    verdict_lower = verdict.lower()
    weak_signals = ("no improvement", "below", "does not help", "weak", "unsupported", "not beat", "not comparable")
    if verdict_lower in {"weak", "unsupported", "negative"} or contains_any(combined, weak_signals):
        detail = f"当前 {method} 在 {dataset} 上没有超过可比基线"
        if metric_hint:
            detail += f"（{metric_hint}）"
        return one_line(detail + "，暂不能支撑论文主张，需要换思路或重新设计实验。")
    if verdict_lower in {"partial", "mixed"}:
        detail = f"当前 {method} 只有部分证据"
        if metric_hint:
            detail += f"（{metric_hint}）"
        return one_line(detail + "，还不能作为主结果，需要补充对照、坏例和稳定性验证。")
    if verdict_lower in {"supported", "support", "strong"}:
        detail = f"当前 {method} 结果支持阶段性结论"
        if metric_hint:
            detail += f"（{metric_hint}）"
        return one_line(detail + "，仍需通过复现、对照和证据门控后才能写入论文。")
    if status.lower() == "completed":
        detail = "实验已完成"
        if metric_hint:
            detail += f"（{metric_hint}）"
        return detail + "，但尚未形成可直接支撑论文的明确结论。"
    if status:
        return one_line(f"当前状态：{status}。")
    return "尚未记录可读反思。"


def next_action(row: dict[str, Any]) -> str:
    if str(row.get("status") or "").strip().lower() == "running":
        return "等待训练进程结束；随后写本地审计、登记最终指标并刷新所有门控"
    decision = str(row.get("decision") or "").strip()
    verdict = str(row.get("claim_verdict") or "").lower()
    dataset = str(row.get("dataset") or "").lower()
    audit_ready = bool(row.get("audit_ready"))
    if decision:
        decision_map = {
            "completed": "记录已完成；若要支撑论文，还需补齐审计证据",
            "synthetic_only": "仅可作为流程验证；不能作为论文主结果",
            "repair_metric_logging": "先修复指标记录，确保每次实验都能自动留下可审计结果",
            "prune": "该方向证据不足，建议停止投入并转向更有希望的方案",
            "baseline_established": "基线已建立；下一步必须用同协议候选方法与它比较",
        }
        return decision_map.get(decision, decision)
    if not audit_ready:
        return "先补齐指标、坏例、审计文件和可复现实验配置"
    if "synthetic" in dataset:
        return "仅作为流程自测证据；下一步必须切换或补齐真实数据实验"
    if verdict in {"weak", "unsupported", "partial"}:
        return "针对弱证据补做真实数据、坏例切片和反例压力测试"
    return "根据当前指标和坏例切片决定继续深化、加强对照或停止该方向"


def evidence_paths(row: dict[str, Any]) -> str:
    keys = ["artifact_path", "metrics_path", "bad_case_path", "audit_path", "failure_analysis_path"]
    values = []
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    if not values:
        return "未登记证据文件"
    labels = []
    for value in values:
        name = Path(value).name
        parent = Path(value).parent.name
        if name == "audit.json":
            labels.append("审计文件 audit.json")
        elif name == "bad_cases.json":
            labels.append("坏例切片 bad_cases.json")
        elif name == "metrics.json":
            labels.append("指标文件 metrics.json")
        elif name.startswith("failure_analysis_"):
            labels.append("失败分析报告")
        elif "." in name:
            labels.append(name)
        else:
            labels.append("实验产物目录")
    return "; ".join(dict.fromkeys(labels))


def command_summary(row: dict[str, Any], method_labels: dict[str, Any] | None = None, dataset_labels: dict[str, Any] | None = None) -> str:
    if row.get("config_summary"):
        return one_line(row.get("config_summary"), 260)
    notes = str(row.get("notes") or "").strip()
    command = str(row.get("command") or "").strip()
    method = str(row.get("method") or row.get("method_slug") or "").strip()
    dataset = dataset_label(row, dataset_labels)
    epochs = ""
    for source in [notes, command]:
        match = re.search(r"(\d+)[ -]?epoch", source, flags=re.IGNORECASE)
        if match:
            epochs = f"{match.group(1)} 轮训练"
            break
        match = re.search(r"--epoches\s+(\d+)", source)
        if match:
            epochs = f"{match.group(1)} 轮训练"
            break
    duration = row.get("duration_sec")
    duration_text = ""
    if isinstance(duration, (int, float)) and duration:
        duration_text = f"运行约 {duration / 60:.1f} 分钟" if duration >= 120 else f"运行 {duration:.1f} 秒"
    pieces = []
    if dataset and dataset != "未记录":
        pieces.append(f"数据集={dataset}")
    if epochs:
        pieces.append(epochs)
    if duration_text:
        pieces.append(duration_text)
    if "synthetic" in str(row.get("dataset") or "").lower():
        seed = re.search(r"--seed\s+(\d+)", command)
        pieces.append(f"seed={seed.group(1) if seed else '未记录'}")
        pieces.append("仅验证流程，不支撑论文主结论")
        return one_line("；".join(pieces), 260)
    if command:
        if "run_real_repo_smoke.py" in command:
            seed = re.search(r"--seed\s+(\d+)", command)
            method_slug = re.search(r"--method-slug\s+([^\s]+)", command)
            pieces.append(f"seed={seed.group(1) if seed else '未记录'}")
            if method_slug:
                pieces.append(f"方法={clean_identifier(method_slug.group(1), method_labels)}")
            return one_line("真实数据加载/指标探针；" + "；".join(pieces), 260)
        cmd = command.replace(str(ROOT) + "/", "[project-file]/")
        cmd = cmd.replace("CUDA_VISIBLE_DEVICES=0 ", "GPU 0; ")
        return one_line(cmd, 260)
    method_label = clean_identifier(method, method_labels)
    if notes:
        if any(token in notes.lower() for token in ("cycle", "llm", "legacy", "without", "with ")):
            return one_line("；".join([item for item in [f"历史运行记录：{method_label}", *pieces] if item]) or "历史运行记录", 260)
        return one_line(notes, 260)
    return one_line("；".join([item for item in [method_label, *pieces] if item]) or "未记录关键配置", 260)


def bad_case_text(row: dict[str, Any], slices: list[Any], bad_case_summary: dict[str, Any]) -> str:
    count = bad_case_summary.get("count")
    if slices:
        return one_line("、".join(str(item).replace("_", " ") for item in slices))
    if count:
        return f"记录 {count} 个坏例"
    return "未记录坏例切片"


def run_label(row: dict[str, Any], method_labels: dict[str, Any] | None = None) -> str:
    # The table's primary identifier must stay machine-stable.  Human-readable
    # labels belong in the method/goal columns; otherwise repeated baseline
    # runs collapse into indistinguishable rows in the dashboard.
    for key in ("experiment_id", "name", "run_id", "artifact_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return one_line(value, 180)
    method = str(row.get("method") or row.get("method_slug") or "").strip()
    trial = row.get("trial_index")
    trial_text = f"第 {trial} 次" if isinstance(trial, int) else ""
    if "synthetic" in str(row.get("dataset") or "").lower():
        return one_line(f"流程自测 {trial_text}".strip())
    label = clean_identifier(method, method_labels)
    return one_line(f"{label} {trial_text}".strip() or "未命名实验", 180)


def build_rows(registry: list[dict[str, Any]], cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    display = display_config(cfg or {})
    method_labels = display.get("method_labels", {}) if isinstance(display.get("method_labels", {}), dict) else {}
    repo_labels = display.get("repo_labels", {}) if isinstance(display.get("repo_labels", {}), dict) else {}
    dataset_labels = display.get("dataset_labels", {}) if isinstance(display.get("dataset_labels", {}), dict) else {}
    env_labels = display.get("env_labels", {}) if isinstance(display.get("env_labels", {}), dict) else {}
    role_policy = role_config(cfg or {})
    out: list[dict[str, Any]] = []
    for row in sorted((item for item in registry if isinstance(item, dict)), key=lambda item: str(item.get("timestamp") or item.get("finished_at") or item.get("started_at") or "")):
        bad_case_summary = row.get("bad_case_summary", {}) if isinstance(row.get("bad_case_summary", {}), dict) else {}
        slices = row.get("bad_case_slices")
        if not isinstance(slices, list):
            slices = bad_case_summary.get("slices", []) if isinstance(bad_case_summary.get("slices", []), list) else []
        record = {
            "时间": row.get("finished_at") or row.get("timestamp") or row.get("started_at") or "",
            "实验ID": run_label(row, method_labels),
            "实验目的": infer_goal(row, method_labels, dataset_labels, role_policy),
            "方法/变体": clean_identifier(row.get("method") or row.get("method_slug") or "", method_labels),
            "仓库": repo_label(row, repo_labels),
            "数据集": dataset_label(row, dataset_labels),
            "运行环境": env_label(row, env_labels),
            "关键配置/命令": command_summary(row, method_labels, dataset_labels),
            "指标": metrics_text(row),
            "坏例/切片": bad_case_text(row, slices, bad_case_summary),
            "审计状态": audit_text(row),
            "结论/反思": reflection(row, method_labels, dataset_labels, role_policy),
            "下一步行动": next_action(row),
            "证据路径": evidence_paths(row),
        }
        out.append(record)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in COLUMNS})


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    latest = list(reversed(rows[-30:]))
    lines = [
        "# 实验记录\n\n",
        f"- 更新时间: {now_iso()}\n",
        f"- 记录数: {len(rows)}\n",
        f"- CSV: `{path.parent / 'experiment_records.csv'}`\n\n",
        "| 时间 | 实验ID | 目的 | 方法 | 数据集 | 指标 | 审计 | 下一步 |\n",
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n",
    ]
    for row in latest:
        lines.append(
            "| "
            + " | ".join(
                one_line(row.get(key, ""), 120).replace("|", "/")
                for key in ["时间", "实验ID", "实验目的", "方法/变体", "数据集", "指标", "审计状态", "下一步行动"]
            )
            + " |\n"
        )
    path.write_text("".join(lines), encoding="utf-8")


def build_experiment_record_table(project: str) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    registry = load_json(paths.state / "experiment_registry.json", [])
    if not isinstance(registry, list):
        registry = []
    rows = build_rows(registry, cfg)
    payload = {
        "project": project,
        "updated_at": now_iso(),
        "row_count": len(rows),
        "columns": COLUMNS,
        "rows": rows,
        "csv_path": str(paths.experiments / "experiment_records.csv"),
        "report_path": str(paths.experiments / "实验记录.md"),
        "json_path": str(paths.state / "experiment_record_table.json"),
        "source": str(paths.state / "experiment_registry.json"),
    }
    save_json(paths.state / "experiment_record_table.json", payload)
    write_csv(paths.experiments / "experiment_records.csv", rows)
    write_report(paths.experiments / "实验记录.md", rows)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a human-readable experiment record table from TASTE experiment registry.")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    payload = build_experiment_record_table(args.project)
    paths = build_paths(args.project)
    print(paths.experiments / "experiment_records.csv")
    if payload.get("row_count") == 0:
        print("warning: no experiment rows found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
