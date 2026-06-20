from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from common.io import read_json, write_json, write_text
from common.paths import READING_ROOT, ensure_inside_reading, resolved

MIN_FULL_TEXT_CHARS = 1200
DEFAULT_CHANNELS = ["nips2025", "iclr2026", "icml2026", "sigkdd2026", "arxiv", "biorxiv", "nature", "science_family"]


def _inside_reading(path: str) -> bool:
    if not path:
        return False
    try:
        ensure_inside_reading(Path(path), label="审计路径")
        return True
    except Exception:
        return False


def _load_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _channel_dirs(batch_dir: Path, channels: list[str]) -> list[Path]:
    out: list[Path] = []
    for name in channels:
        candidate = batch_dir / name
        if candidate.is_dir():
            out.append(candidate)
    return out


def _paper_dirs(channel_dir: Path) -> list[Path]:
    dirs = [path for path in channel_dir.iterdir() if path.is_dir()]
    return sorted(dirs, key=lambda item: item.name)


def _status_ready(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "")
    return status in {"complete", "prepared_for_main_claude_subagent", "prepared_for_manual_main_claude_subagent", "prepared_no_claude_run"} or status.startswith("prepared_")


def audit_paper_dir(paper_dir: Path) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    read_results_path = paper_dir / "read_results.json"
    packet_path = paper_dir / "full_text_packet.json"
    prompt_path = paper_dir / "prompts" / "deep_read_prompt.md"
    read_md_path = paper_dir / "read.md"
    paper_json_path = paper_dir / "paper.json"

    for required in [read_results_path, packet_path, prompt_path, read_md_path, paper_json_path]:
        if not required.exists():
            problems.append(f"缺少文件：{required.name}")
    read_results = read_json(read_results_path, {}) if read_results_path.exists() else {}
    packet_file = read_json(packet_path, {}) if packet_path.exists() else {}
    packet = read_results.get("full_text_packet") if isinstance(read_results.get("full_text_packet"), dict) else {}
    if not packet and isinstance(packet_file, dict):
        papers = packet_file.get("papers") if isinstance(packet_file.get("papers"), list) else []
        packet = papers[0] if papers and isinstance(papers[0], dict) else packet_file

    full_text_chars = int(packet.get("full_text_chars") or packet.get("text_chars") or 0)
    text_path = str(packet.get("text_path") or "")
    prompt = _load_prompt(prompt_path)
    if not _status_ready(read_results):
        problems.append(f"read_results 状态不可用：{read_results.get('status')}")
    if packet.get("full_text_available") is not True:
        problems.append("full_text_available 不是 true")
    if full_text_chars < MIN_FULL_TEXT_CHARS:
        problems.append(f"正文字数不足：{full_text_chars}")
    if not text_path or not Path(text_path).exists():
        problems.append("正文路径不存在")
    elif not _inside_reading(text_path):
        problems.append(f"正文路径越界：{text_path}")
    for marker in ["Task/subagent", "subagent_deep_read"]:
        if marker not in prompt:
            problems.append(f"prompt 缺少标记：{marker}")
    if str(packet.get("text_kind") or "") in {"html", "full_text_xml"} and str(packet.get("full_text_status") or "") not in {"html_text_read", "full_text_read"}:
        problems.append(f"HTML/XML 正文状态异常：{packet.get('full_text_status')}")

    summary = {
        "paper_dir": str(paper_dir),
        "title": packet.get("title") or read_results.get("paper", {}).get("title") or paper_dir.name,
        "status": read_results.get("status"),
        "full_text_status": packet.get("full_text_status"),
        "text_kind": packet.get("text_kind"),
        "full_text_chars": full_text_chars,
        "text_path": text_path,
        "problem_count": len(problems),
        "problems": problems,
    }
    return summary, problems


def audit_batch(batch_dir: Path, *, per_channel: int, channels: list[str] | None = None) -> dict[str, Any]:
    batch_dir = ensure_inside_reading(batch_dir, label="批量验收目录")
    report = read_json(batch_dir / "batch_report.json", {})
    target_channels = channels or DEFAULT_CHANNELS
    channel_summaries: dict[str, Any] = {}
    all_problems: list[str] = []
    xml_ready_count = 0
    total_checked = 0

    for channel_dir in _channel_dirs(batch_dir, target_channels):
        checked: list[dict[str, Any]] = []
        for paper_dir in _paper_dirs(channel_dir):
            if len(checked) >= per_channel:
                break
            summary, problems = audit_paper_dir(paper_dir)
            if not _status_ready(read_json(paper_dir / "read_results.json", {})) and summary.get("problem_count"):
                continue
            checked.append(summary)
            total_checked += 1
            if summary.get("text_kind") == "full_text_xml":
                xml_ready_count += 1
            for problem in problems:
                all_problems.append(f"{channel_dir.name}/{paper_dir.name}: {problem}")
        ready = [item for item in checked if not item.get("problems")]
        channel_summaries[channel_dir.name] = {
            "checked_count": len(checked),
            "ready_count": len(ready),
            "problem_count": sum(int(item.get("problem_count") or 0) for item in checked),
            "papers": checked,
        }
        if len(ready) < per_channel:
            all_problems.append(f"{channel_dir.name}: 合格产物不足 {len(ready)}/{per_channel}")

    status = "passed" if not all_problems else "failed"
    return {
        "status": status,
        "run_id": report.get("run_id") or batch_dir.name,
        "batch_dir": str(batch_dir),
        "batch_report_status": report.get("status"),
        "target_per_channel": per_channel,
        "checked_total": total_checked,
        "xml_ready_count": xml_ready_count,
        "problem_count": len(all_problems),
        "problems": all_problems,
        "channels": channel_summaries,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    lines = [
        f"# Reading 多渠道批量产物审计：{audit.get('run_id')}",
        "",
        f"- 状态：`{audit.get('status')}`",
        f"- 原批量报告状态：`{audit.get('batch_report_status')}`",
        f"- 目标每渠道数量：{audit.get('target_per_channel')}",
        f"- 实际核对条目：{audit.get('checked_total')}",
        f"- XML 全文兜底条目：{audit.get('xml_ready_count')}",
        f"- problem_count: {audit.get('problem_count')}",
        "",
        "## 渠道汇总",
        "",
        "| 渠道 | 核对数 | 合格数 | 问题数 |",
        "|---|---:|---:|---:|",
    ]
    channels = audit.get("channels") if isinstance(audit.get("channels"), dict) else {}
    for name, summary in channels.items():
        lines.append(f"| {name} | {summary.get('checked_count')} | {summary.get('ready_count')} | {summary.get('problem_count')} |")
    lines.extend(["", "## 问题清单", ""])
    problems = audit.get("problems") if isinstance(audit.get("problems"), list) else []
    if problems:
        lines.extend(f"- {problem}" for problem in problems)
    else:
        lines.append("未发现问题。")
    lines.extend(["", "## 核对样本", ""])
    for name, summary in channels.items():
        lines.append(f"### {name}")
        for paper in summary.get("papers") or []:
            lines.append(f"- `{Path(str(paper.get('paper_dir'))).name}`：{paper.get('full_text_status')}，{paper.get('text_kind')}，{paper.get('full_text_chars')} 字，问题 {paper.get('problem_count')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="审计 Reading 多渠道批量验收产物。")
    parser.add_argument("--run-id", default="", help="workspace/batch_tests 下的运行 ID。")
    parser.add_argument("--batch-dir", default="", help="可选批量运行目录；优先于 --run-id。")
    parser.add_argument("--per-channel", type=int, default=10, help="每个渠道核对多少个合格产物。")
    parser.add_argument("--channels", default=",".join(DEFAULT_CHANNELS), help="逗号分隔渠道列表。")
    parser.add_argument("--output", default="", help="可选 Markdown 输出路径；默认写到批量目录。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch_dir:
        batch_dir = Path(args.batch_dir).expanduser()
    elif args.run_id:
        batch_dir = READING_ROOT / "workspace" / "batch_tests" / args.run_id
    else:
        raise SystemExit("必须提供 --run-id 或 --batch-dir。")
    channels = [part.strip() for part in str(args.channels or "").split(",") if part.strip()]
    audit = audit_batch(batch_dir, per_channel=args.per_channel, channels=channels)
    output = Path(args.output).expanduser() if args.output else resolved(batch_dir) / "manual_audit_zh.md"
    output = ensure_inside_reading(output, label="审计报告")
    json_output = output.with_suffix(".json")
    write_json(json_output, audit)
    write_text(output, render_audit_markdown(audit))
    print(json.dumps({
        "status": audit.get("status"),
        "problem_count": audit.get("problem_count"),
        "checked_total": audit.get("checked_total"),
        "xml_ready_count": audit.get("xml_ready_count"),
        "audit_md": str(output),
        "audit_json": str(json_output),
    }, ensure_ascii=False, indent=2))
    return 0 if audit.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
