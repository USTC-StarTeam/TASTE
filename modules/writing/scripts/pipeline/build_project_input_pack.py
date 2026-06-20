#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)

import argparse
import csv
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from writing_paths import MODULE_ROOT, REPO_ROOT


def now_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "").strip()).strip("-") or "project"


def read_text(path: Path, limit: int | None = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit] if limit else text


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_file(src: Path, dst: Path, copied: list[dict[str, Any]], *, required: bool = False, limit_bytes: int = 2_000_000) -> None:
    row = {"source": str(src), "target": str(dst), "required": required, "copied": False}
    if not src.exists() or not src.is_file():
        row["error"] = "missing"
        copied.append(row)
        return
    if src.stat().st_size > limit_bytes:
        row["error"] = f"too_large>{limit_bytes}"
        copied.append(row)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    row["copied"] = True
    row["bytes"] = dst.stat().st_size
    copied.append(row)


def csv_preview(path: Path, max_rows: int = 20) -> str:
    if not path.exists():
        return ""
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for idx, row in enumerate(reader):
            rows.append(row)
            if idx >= max_rows:
                break
    if not rows:
        return ""
    return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)


def summarize_registry(registry: Any) -> str:
    rows = registry if isinstance(registry, list) else registry.get("rows", []) if isinstance(registry, dict) else []
    out = ["# 实验注册表摘要", ""]
    for row in rows[-12:]:
        if not isinstance(row, dict):
            continue
        metrics = row.get("metrics") or row.get("metric") or row.get("指标") or {}
        if isinstance(metrics, dict):
            metrics_text = "; ".join(f"{k}={v}" for k, v in list(metrics.items())[:20])
        else:
            metrics_text = str(metrics)
        out.append(f"- id: {row.get('experiment_id') or row.get('实验ID') or row.get('name')}")
        out.append(f"  method: {row.get('method') or row.get('方法/变体')}")
        out.append(f"  status: {row.get('status') or row.get('审计状态')}")
        out.append(f"  audit_ready: {row.get('audit_ready', '')}")
        out.append(f"  metrics: {metrics_text[:1200]}")
        conclusion = row.get("结论/反思") or row.get("notes") or row.get("result") or ""
        if conclusion:
            out.append(f"  conclusion: {str(conclusion)[:1200]}")
    return "\n".join(out) + "\n"


def collect_artifact_records(project_root: Path, out_dir: Path, copied: list[dict[str, Any]]) -> None:
    artifacts = project_root / "artifacts"
    if not artifacts.is_dir():
        return
    dst_root = out_dir / "records" / "artifacts"
    kept = 0
    for child in sorted(artifacts.iterdir()):
        if not child.is_dir():
            continue
        wanted = ["metrics.json", "audit.json", "bad_cases.json", "experiment.json", "run_contract.json"]
        if not any((child / name).exists() for name in wanted):
            continue
        for name in wanted:
            src = child / name
            if src.exists():
                copy_file(src, dst_root / child.name / name, copied, limit_bytes=1_500_000)
        kept += 1
        if kept >= 12:
            break


def main() -> int:
    parser = argparse.ArgumentParser(description="把已有 TASTE project 的论文写作输入复制成 writing 独立输入包。")
    parser.add_argument("--project", required=True)
    parser.add_argument("--pack-id", default="")
    parser.add_argument("--output-root", default=str(MODULE_ROOT / "runs" / "source_packs"))
    args = parser.parse_args()

    project = slugify(args.project)
    project_root = REPO_ROOT / "projects" / project
    if not project_root.is_dir():
        raise SystemExit(f"project 不存在: {project_root}")
    output_root = Path(args.output_root).expanduser().resolve()
    allowed_root = (MODULE_ROOT / "runs").resolve()
    if allowed_root != output_root and allowed_root not in output_root.parents:
        raise SystemExit("output-root 必须位于 modules/writing/runs 内。")
    pack_id = args.pack_id or f"{project}-{now_id()}"
    pack_dir = output_root / pack_id
    if pack_dir.exists():
        shutil.rmtree(pack_dir)
    pack_dir.mkdir(parents=True)

    copied: list[dict[str, Any]] = []
    idea_candidates = [
        project_root / "planning" / "finding" / "idea.md",
        project_root / "obsidian" / "planning" / "finding" / "idea.md",
        project_root / "paper" / "writing" / "iclr" / "workspace" / "inputs" / "idea.md",
    ]
    plan_candidates = [
        project_root / "planning" / "finding" / "plan.md",
        project_root / "obsidian" / "planning" / "finding" / "plan.md",
        project_root / "planning" / "research_plan.md",
    ]
    idea = next((p for p in idea_candidates if p.exists() and p.stat().st_size > 0), idea_candidates[0])
    plan = next((p for p in plan_candidates if p.exists() and p.stat().st_size > 0), plan_candidates[0])
    copy_file(idea, pack_dir / "idea.md", copied, required=True)
    copy_file(plan, pack_dir / "plan.md", copied, required=True)

    exp_parts = ["# 实验记录汇总", "", f"来源 project: `{project}`", ""]
    log_path = project_root / "experiments" / "experiment_log.md"
    if log_path.exists():
        exp_parts.extend(["## experiment_log.md", "", read_text(log_path, 12000), ""])
    csv_path = project_root / "experiments" / "experiment_records.csv"
    if csv_path.exists():
        exp_parts.extend(["## experiment_records.csv 预览", "", csv_preview(csv_path, max_rows=20), ""])
    record_table = load_json(project_root / "state" / "experiment_record_table.json", {})
    if record_table:
        exp_parts.extend(["## experiment_record_table.json 摘要", "", summarize_registry(record_table), ""])
    registry = load_json(project_root / "state" / "experiment_registry.json", [])
    if registry:
        exp_parts.extend(["## experiment_registry.json 摘要", "", summarize_registry(registry), ""])
    iteration = read_text(project_root / "reports" / "experiment_iteration_audit.md", 12000)
    if iteration:
        exp_parts.extend(["## experiment_iteration_audit.md", "", iteration, ""])
    write_text(pack_dir / "experimental_log.md", "\n".join(exp_parts))

    for rel in [
        "experiments/experiment_records.csv",
        "state/experiment_record_table.json",
        "state/experiment_registry.json",
        "state/experiment_iteration_audit.json",
        "state/submission_readiness.json",
        "planning/claim_ledger.md",
        "paper/drafts/paper_draft.md",
        "paper/output/iclr/paper.tex",
        "paper/output/iclr/refs.bib",
        "paper/venues/iclr/venue_requirements.json",
        "paper/venues/iclr/template_source.json",
        "paper/writing/iclr/workspace/citation_pool.json",
    ]:
        src = project_root / rel
        if src.exists():
            copy_file(src, pack_dir / "records" / rel, copied, limit_bytes=2_500_000)
    collect_artifact_records(project_root, pack_dir, copied)

    blockers = [row for row in copied if row.get("required") and not row.get("copied")]
    manifest = {
        "project": project,
        "project_root": str(project_root),
        "pack_dir": str(pack_dir),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "idea": str(pack_dir / "idea.md"),
        "plan": str(pack_dir / "plan.md"),
        "experimental_log": str(pack_dir / "experimental_log.md"),
        "records": str(pack_dir / "records"),
        "copied": copied,
        "blockers": blockers,
        "policy": "本输入包只复制已有 project 的写作证据；不修改原 project。",
    }
    write_json(pack_dir / "input_pack_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
