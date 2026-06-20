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
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from writing_paths import MODULE_ROOT, REPO_ROOT, pythonpath


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return value or "paper-run"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_input(src: str, dst: Path, *, label: str, required: bool) -> dict[str, Any]:
    row = {"label": label, "source": src, "target": str(dst), "required": required, "copied": False}
    if not src:
        if required:
            row["error"] = "未提供必需输入"
        return row
    path = Path(src).expanduser().resolve()
    if not path.exists():
        row["error"] = f"输入不存在: {path}"
        return row
    dst.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(path, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"))
    else:
        shutil.copy2(path, dst)
    row["source"] = str(path)
    row["copied"] = True
    return row


def find_claude() -> str:
    return shutil.which("claude") or "claude"


def run_claude(prompt: Path, work_dir: Path, *, timeout_sec: int) -> dict[str, Any]:
    claude = find_claude()
    cmd = [
        claude,
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--add-dir",
        str(MODULE_ROOT),
        "--add-dir",
        str(work_dir),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath(env.get("PYTHONPATH", ""))
    env["WORKSPACE_ROOT"] = str(REPO_ROOT)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
    started = now_iso()
    try:
        with prompt.open("r", encoding="utf-8") as handle:
            proc = subprocess.run(cmd, cwd=work_dir, stdin=handle, text=True, capture_output=True, timeout=max(60, timeout_sec), env=env)
        rc = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        timed_out = True
    return {
        "command": cmd,
        "started_at": started,
        "finished_at": now_iso(),
        "return_code": rc,
        "timed_out": timed_out,
        "stdout_tail": stdout[-12000:],
        "stderr_tail": stderr[-12000:],
        "prompt": str(prompt),
    }



def run_quality_audit(run_dir: Path) -> dict[str, Any]:
    audit_script = MODULE_ROOT / "scripts" / "audit" / "audit_standalone_paper.py"
    if not audit_script.exists():
        return {"status": "missing_audit_script"}
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath(env.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, str(audit_script), "--run-dir", str(run_dir)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        payload = {}
    payload.setdefault("status", "pass" if proc.returncode == 0 else "blocked")
    payload["return_code"] = proc.returncode
    payload["stdout_tail"] = proc.stdout[-4000:]
    payload["stderr_tail"] = proc.stderr[-4000:]
    return payload


def build_prompt(run_dir: Path, venue: str, title: str) -> str:
    skills = MODULE_ROOT / "skills"
    return f"""
你是 Claude Code，现在要在一个完全独立的 writing run 目录中生成目标 venue 论文。所有产物只能写入这个 run 目录：

Run directory: {run_dir}
Workspace: {run_dir / 'workspace'}
Venue: {venue}
Title hint: {title or '(由论文内容决定)'}

必须先阅读这些本地 skill：
1. {skills / 'taste-paper-writing' / 'SKILL.md'}
2. {skills / 'venue-intelligence' / 'SKILL.md'}
3. {skills / 'citation-integrity' / 'SKILL.md'}
4. {skills / 'writing-quality' / 'SKILL.md'}
5. 如果 venue 是 Nature/Springer Nature，再读 {skills / 'nature-family-writing' / 'SKILL.md'}
6. 如需 PaperOrchestra 流程细节，读 {skills / 'paper-orchestra' / 'SKILL.md'} 及其直接引用的必要文件。

输入文件在 `{run_dir / 'workspace' / 'inputs'}`：
- idea.md
- plan.md
- experimental_log.md
- records/（可能为空）

先建立证据边界：
- 写作前必须完整阅读 idea、plan、experimental_log 和 records 中的实验表/审计/指标文件，分清“已完成实验证据”“计划中的方法”“失败或负结果”“只可理论推导的性质”。
- 必须在 `{run_dir / 'workspace' / 'audits' / 'claim_evidence_audit.json'}` 写入主要 claim 到证据的台账，至少包含每个 claim 的正文位置、支撑来源、证据等级、是否允许写成强结论。
- 如果输入显示候选方法没有超过基线、实验尚未完成或只能支撑基础设施/协议，论文必须诚实写成方法框架、可复现实验基座、负结果诊断和后续验证路线；不得写成已经取得 SOTA、显著提升、跨数据集验证或完整实证胜利。
- 数值型性能、延迟、加速、页数、数据集数量只能来自输入记录或官方/真实文献；没有直接测量时只能写为理论复杂度分析，并在 claim 台账中标记为 theoretical。

严格任务：
1. 自行联网调研目标会议/期刊当前官方投稿要求，只接受官方来源或官方明确链接的模板来源。
2. 在 `{run_dir / 'venue'}` 写入 `venue_requirements.json`、`venue_requirements_report.md`，并下载/展开最新官方 LaTeX 模板到 `{run_dir / 'venue' / 'template_source'}`。如果官方允许 flexible format，也要诚实记录，但仍为预览生成 LaTeX/PDF。
3. 把可用模板主文件复制或渲染到 `{run_dir / 'workspace' / 'inputs' / 'template.tex'}`，并保留必要 style/bst/cls sidecar。
4. 基于 idea、plan、实验记录表和 records 写完整论文。正文必须符合 venue 行文和格式；引用必须真实并写入 `{run_dir / 'workspace' / 'refs.bib'}`；正文 citation key 必须和 BibTeX 一致。ICLR/Nature 级别稿件正文实际引用去重数和 BibTeX 条目数均不少于 30，官方要求更高时按官方要求。
5. 判断正文页数是否达标：明确区分 body pages、references pages、appendix/supplement，写入 `{run_dir / 'workspace' / 'audits' / 'page_audit.json'}`。
6. 生成并尽力编译：`{run_dir / 'workspace' / 'final' / 'paper.tex'}` 和 `{run_dir / 'workspace' / 'final' / 'paper.pdf'}`。若 LaTeX 工具缺失或模板编译失败，保留完整阻塞报告，不要伪造 PDF。
7. 写入 `{run_dir / 'workspace' / 'provenance.json'}`，记录输入、官方来源、模板来源、引用来源、生成/编译状态。

禁止事项：
- 不要写入 `{REPO_ROOT / 'projects'}`、`modules/writing`、web 前端、其它模块或全局 `.claude/skills`。
- 不要把 gate report、计划书、审计报告、任务日志、pipeline 状态当成论文正文。
- 没有真实作者/机构信息时，严禁写 Author One、Institution Name、City 00000、author.one@institution.edu、[Names]、GitHub placeholder 等假元数据；用 Anonymous Authors / Affiliation withheld 这类中性占位，并在 provenance 中说明不是最终投稿元数据。Springer/Nature 模板不得留下 `\street{{}}`、`\city{{}}`、`\postcode{{}}` 等空地址字段导致 PDF 出现连续逗号。
- 不要使用 first framework、state-of-the-art、outperform、surpass、significantly improves、validated across multiple/four datasets 等强 claim，除非 claim_evidence_audit.json 中有直接实验或文献证据支撑。
- 不要虚构引用、实验结果、页数规则或模板来源。没有真实公开 URL/DOI/归档编号时，不要写代码、框架或基础设施“已发布/公开可用”；只能写“项目工作区内有审计记录”或“完成后将发布”。
- 不要读取/打印任何 API key/token/secret 环境变量。

结束时只用中文输出：最终状态、paper.tex 路径、paper.pdf 路径、主要阻塞项（如有）。
结束时只用中文输出：最终状态、paper.tex 路径、paper.pdf 路径、主要阻塞项（如有）。
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="独立运行 writing：从 idea/plan/实验记录生成目标 venue 论文。")
    parser.add_argument("--venue", required=True, help="目标会议/期刊，例如 ICLR 2026、CIKM 2026、Nature Machine Intelligence")
    parser.add_argument("--idea", required=True, help="实验 idea Markdown/text 文件")
    parser.add_argument("--plan", required=True, help="实验 plan Markdown/text 文件")
    parser.add_argument("--experimental-log", required=True, help="实验记录 Markdown/text/CSV 文件")
    parser.add_argument("--records", default="", help="可选：实验记录表、图、日志等目录")
    parser.add_argument("--title", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--runs-root", default=str(MODULE_ROOT / "runs"))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("WRITING_STANDALONE_TIMEOUT_SEC", "14400")))
    parser.add_argument("--prepare-only", action="store_true", help="只准备 run 目录和 prompt，不启动 Claude Code")
    args = parser.parse_args()

    run_id = args.run_id or f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(args.venue)}"
    runs_root = Path(args.runs_root).expanduser().resolve()
    module_root = MODULE_ROOT.resolve()
    default_runs = module_root / "runs"
    if runs_root != default_runs and default_runs not in runs_root.parents:
        raise SystemExit("为了隔离中间产物，--runs-root 必须位于 modules/writing/runs 下或保持默认值。")
    run_dir = runs_root / run_id
    inputs = run_dir / "workspace" / "inputs"
    audits = run_dir / "workspace" / "audits"
    for path in [inputs, audits, run_dir / "venue", run_dir / "logs"]:
        path.mkdir(parents=True, exist_ok=True)

    copied = [
        copy_input(args.idea, inputs / "idea.md", label="idea", required=True),
        copy_input(args.plan, inputs / "plan.md", label="plan", required=True),
        copy_input(args.experimental_log, inputs / "experimental_log.md", label="experimental_log", required=True),
        copy_input(args.records, inputs / "records", label="records", required=False),
    ]
    blockers = [row.get("error") for row in copied if row.get("error")]
    manifest = {
        "module": "writing",
        "mode": "standalone",
        "run_id": run_id,
        "venue": args.venue,
        "title": args.title,
        "created_at": now_iso(),
        "run_dir": str(run_dir),
        "inputs": copied,
        "blockers": blockers,
    }
    write_json(run_dir / "run_manifest.json", manifest)
    prompt_path = run_dir / "claude_standalone_prompt.md"
    write_text(prompt_path, build_prompt(run_dir, args.venue, args.title))
    if blockers:
        print(json.dumps({"status": "blocked", "run_dir": str(run_dir), "blockers": blockers}, ensure_ascii=False, indent=2))
        return 2
    if args.prepare_only:
        print(json.dumps({"status": "prepared", "run_dir": str(run_dir), "prompt": str(prompt_path)}, ensure_ascii=False, indent=2))
        return 0
    result = run_claude(prompt_path, run_dir, timeout_sec=args.timeout_sec)
    write_json(run_dir / "logs" / "claude_result.json", result)
    quality_audit = run_quality_audit(run_dir)
    status = "generated" if result["return_code"] == 0 and quality_audit.get("status") == "pass" else "blocked"
    summary = {
        "status": status,
        "run_dir": str(run_dir),
        "paper_tex": str(run_dir / "workspace" / "final" / "paper.tex"),
        "paper_pdf": str(run_dir / "workspace" / "final" / "paper.pdf"),
        "claude_return_code": result["return_code"],
        "quality_audit_status": quality_audit.get("status"),
        "quality_audit": quality_audit.get("run_dir") and str(run_dir / "workspace" / "audits" / "standalone_quality_audit.json"),
        "prompt": str(prompt_path),
        "log": str(run_dir / "logs" / "claude_result.json"),
    }
    write_json(run_dir / "run_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status == "generated" else 3


if __name__ == "__main__":
    raise SystemExit(main())
