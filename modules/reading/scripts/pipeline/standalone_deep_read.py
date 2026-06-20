from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Sequence

from acquisition.paper_sources import MIN_FULL_TEXT_CHARS, acquire_full_text, build_paper_record
from common.io import read_json, safe_slug, write_json, write_text
from common.paths import READING_ROOT, ensure_inside_reading, run_dir
from orchestration.claude_subagent import build_deep_read_prompt, run_claude_deep_read


def now_compact() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_run_id(article: str, title: str = "", explicit: str = "") -> str:
    if explicit:
        return safe_slug(explicit, fallback="reading_run")
    return f"{now_compact()}_{safe_slug(title or article, fallback='paper', max_len=48)}"


def _load_input_json(path: str) -> dict[str, Any]:
    if not path:
        return {}
    candidate = Path(path).expanduser()
    payload = read_json(candidate, {})
    if not isinstance(payload, dict):
        raise SystemExit(f"输入 JSON 不是对象：{candidate}")
    return payload


def _render_markdown(payload: dict[str, Any]) -> str:
    paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
    packet = payload.get("full_text_packet") if isinstance(payload.get("full_text_packet"), dict) else {}
    claude = payload.get("claude") if isinstance(payload.get("claude"), dict) else {}
    result = payload.get("claude_result") if isinstance(payload.get("claude_result"), dict) else {}
    reading = result.get("reading") if isinstance(result.get("reading"), dict) else {}
    title = str(paper.get("title") or packet.get("title") or "未命名论文")
    lines = [
        f"# 论文精读：{title}",
        "",
        f"- 运行 ID：`{payload.get('run_id', '')}`",
        f"- 状态：`{payload.get('status', '')}`",
        f"- PDF：{packet.get('pdf_url') or paper.get('pdf_url') or '未获取'}",
        f"- 正文路径：`{packet.get('text_path') or '未生成'}`",
        f"- 正文字数：{packet.get('full_text_chars') or packet.get('text_chars') or 0}",
        f"- Claude/subagent 状态：`{claude.get('status', '')}`",
        "",
    ]
    if reading:
        lines.extend([
            "## 原论文摘要（中文）",
            str(reading.get("abstract_zh") or "未提供。"),
            "",
            "## 论文动机",
            str(reading.get("motivation_zh") or "未提供。"),
            "",
            "## 方法机制",
            str(reading.get("method_details_zh") or "未提供。"),
            "",
            "## 实验设置与结果",
            str(reading.get("experiments_zh") or "未提供。"),
            "",
            "## 局限性",
            str(reading.get("limitations_zh") or "未提供。"),
            "",
            "## 方法优缺点",
            "优点：",
        ])
        for item in reading.get("method_advantages_zh") or []:
            lines.append(f"- {item}")
        lines.append("不足：")
        for item in reading.get("method_disadvantages_zh") or []:
            lines.append(f"- {item}")
        lines.extend(["", "## 证据边界", str(reading.get("evidence_boundary_zh") or "未提供。"), ""])
    else:
        lines.extend([
            "## 当前结果",
            "",
            "本次运行已完成题录整理、PDF 获取尝试、正文抽取和 Claude/subagent 精读提示生成。若状态不是 `complete`，请查看同目录下的 `prompts/deep_read_prompt.md`、`claude/claude_receipt.json` 或 `full_text_packet.json`。",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _load_claude_result(result_path: Path, claude_receipt: dict[str, Any]) -> dict[str, Any]:
    file_payload = read_json(result_path, {})
    if isinstance(file_payload, dict) and file_payload:
        return file_payload
    payload = claude_receipt.get("result_payload") if isinstance(claude_receipt, dict) else {}
    return payload if isinstance(payload, dict) else {}


def _final_status(packet: dict[str, Any], claude_receipt: dict[str, Any], claude_result: dict[str, Any]) -> str:
    if int(packet.get("full_text_chars") or packet.get("text_chars") or 0) < MIN_FULL_TEXT_CHARS:
        return "blocked_full_text_unavailable"
    if claude_result.get("subagent_deep_read") is True and isinstance(claude_result.get("reading"), dict):
        return "complete"
    status = str(claude_receipt.get("status") or "").strip()
    if status.startswith("prepared_"):
        return status
    if status.startswith("blocked_"):
        return status
    if claude_receipt.get("run_executed"):
        return "blocked_claude_result_missing_or_invalid"
    return "prepared_for_main_claude_subagent"


def run_standalone_deep_read(args: argparse.Namespace) -> dict[str, Any]:
    input_payload = _load_input_json(args.input_json)
    article = args.article or str(input_payload.get("article") or input_payload.get("url") or input_payload.get("pdf_url") or "")
    if not article:
        raise SystemExit("必须提供 --article，或在 --input-json 中提供 article/url/pdf_url。")
    title = args.title or str(input_payload.get("title") or "")
    run_id = make_run_id(article, title, args.run_id or str(input_payload.get("run_id") or ""))
    current_run_dir = run_dir(run_id)
    write_json(current_run_dir / "input.json", {"cli": vars(args), "input_json": input_payload})

    paper = build_paper_record(
        article=article,
        title=title,
        authors=args.authors or input_payload.get("authors") or "",
        abstract=args.abstract or str(input_payload.get("abstract") or ""),
        paper_id=args.paper_id or str(input_payload.get("paper_id") or input_payload.get("id") or ""),
        pdf_url=args.pdf_url or str(input_payload.get("pdf_url") or ""),
        url=args.url or str(input_payload.get("url") or ""),
        source=args.source,
    )
    write_json(current_run_dir / "paper.json", paper)
    packet_entry = acquire_full_text(paper, current_run_dir)
    full_text_packet = {
        "run_id": run_id,
        "source": "modules/reading standalone deep_read",
        "papers": [packet_entry],
        "policy": "独立精读流水线只在 modules/reading/workspace 下保存下载、抽取、提示和结果；Claude 精读必须由主控调用 Task/subagent 完成。",
    }
    write_json(current_run_dir / "full_text_packet.json", full_text_packet)

    output_path = ensure_inside_reading(current_run_dir / "outputs" / "reading_result.json", label="精读输出")
    prompt_path = ensure_inside_reading(current_run_dir / "prompts" / "deep_read_prompt.md", label="Claude 提示")
    prompt = build_deep_read_prompt(paper=paper, packet=packet_entry, run_path=current_run_dir, output_path=output_path)
    write_text(prompt_path, prompt)

    claude_mode = args.claude_mode
    if int(packet_entry.get("full_text_chars") or packet_entry.get("text_chars") or 0) < MIN_FULL_TEXT_CHARS and claude_mode == "auto":
        claude_mode = "prepare"
    claude_receipt = run_claude_deep_read(
        prompt_path=prompt_path,
        run_path=current_run_dir,
        expected_output_path=output_path,
        timeout_sec=args.timeout_sec,
        mode=claude_mode,
    )
    claude_result = _load_claude_result(output_path, claude_receipt)
    status = _final_status(packet_entry, claude_receipt, claude_result)
    result_payload = {
        "run_id": run_id,
        "status": status,
        "source": "modules/reading standalone deep_read",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reading_root": str(READING_ROOT),
        "run_dir": str(current_run_dir),
        "paper": paper,
        "full_text_packet": packet_entry,
        "claude": claude_receipt,
        "claude_result": claude_result,
        "artifacts": {
            "input": str(current_run_dir / "input.json"),
            "paper": str(current_run_dir / "paper.json"),
            "full_text_packet": str(current_run_dir / "full_text_packet.json"),
            "prompt": str(prompt_path),
            "claude_expected_output": str(output_path),
            "read_results": str(current_run_dir / "read_results.json"),
            "read_md": str(current_run_dir / "read.md"),
        },
    }
    write_json(current_run_dir / "read_results.json", result_payload)
    write_text(current_run_dir / "read.md", _render_markdown(result_payload))
    write_json(READING_ROOT / "workspace" / "latest_run.json", {
        "run_id": run_id,
        "status": status,
        "run_dir": str(current_run_dir),
        "read_results": str(current_run_dir / "read_results.json"),
        "read_md": str(current_run_dir / "read.md"),
    })
    return result_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reading 模块独立论文精读入口。")
    parser.add_argument("--article", default="", help="论文 URL、arXiv 链接/编号、PDF URL 或 DOI。")
    parser.add_argument("--input-json", default="", help="可选输入 JSON；只读取，不在其旁边写产物。")
    parser.add_argument("--run-id", default="", help="可选运行 ID；产物写入 modules/reading/workspace/runs/<run-id>。")
    parser.add_argument("--paper-id", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--authors", default="")
    parser.add_argument("--abstract", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--pdf-url", default="")
    parser.add_argument("--source", default="standalone_input")
    parser.add_argument("--claude-mode", choices=["auto", "run", "prepare"], default="auto", help="auto 会在可用且正文足够时调用 Claude；prepare 只生成 prompt。")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_standalone_deep_read(args)
    print(json.dumps({
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "run_dir": result.get("run_dir"),
        "read_results": result.get("artifacts", {}).get("read_results"),
        "read_md": result.get("artifacts", {}).get("read_md"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"complete", "prepared_for_main_claude_subagent"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
