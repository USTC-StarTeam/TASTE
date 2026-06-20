from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from common.io import first_json_object, read_json, write_json, write_text
from common.paths import READING_ROOT, ensure_inside_reading


DEFAULT_NODE_BIN = Path("/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin")


def build_deep_read_prompt(
    *,
    paper: dict[str, Any],
    packet: dict[str, Any],
    run_path: Path,
    output_path: Path,
) -> str:
    text_path = str(packet.get("text_path") or "")
    title = str(paper.get("title") or packet.get("title") or "未命名论文")
    abstract = str(paper.get("abstract") or "").strip()
    authors = ", ".join(str(item) for item in paper.get("authors") or [] if str(item).strip())
    return f"""你是 TASTE reading 模块的项目主控 Claude Code。请只处理下面这一篇论文，并且必须调用 Task/subagent 进行全文精读。

硬性规则：
1. 你必须把论文全文精读交给独立 Task/subagent；主控只负责分派、验收和汇总。
2. 如果当前 Claude Code 会话没有 Task/subagent 工具，立即写出 status=\"blocked_task_subagent_unavailable_for_deep_reading\" 的 JSON，不要由主控自己短写替代。
3. 只读取 `modules/reading` 下的文件；不要读取、修改 TASTE 其它模块、项目目录或前端文件。
4. 所有中间产物和最终产物只能写入这个运行目录：`{run_path}`。
5. 输出必须是中文、论文内容导向，不要写前端、网页、流程护栏、项目实现含义或泛泛推荐理由。
6. 精读必须基于正文文件 `{text_path}`；如果正文证据不足，必须说明缺失原因，不能用摘要凑完整精读。

论文信息：
- 标题：{title}
- 作者：{authors or "未提供"}
- URL：{paper.get("url") or paper.get("abs_url") or "未提供"}
- PDF：{packet.get("pdf_url") or paper.get("pdf_url") or "未提供"}
- 正文字数：{packet.get("full_text_chars") or packet.get("text_chars") or 0}
- 正文路径：`{text_path}`

摘要：
{abstract or "未提供"}

请让 subagent 读取正文后返回完整论文精读。你验收后把最终 JSON 写入：
`{output_path}`

JSON 顶层字段必须包括：
`status`, `source`, `paper_id`, `title`, `subagent_deep_read`, `deep_read_audit`, `reading`。

`subagent_deep_read` 必须是布尔值：成功调用 Task/subagent 并基于正文完成精读时写 `true`，不能写成对象；subagent 的细节放入 `deep_read_audit`。

其中 `reading` 必须包括：
`abstract_zh`, `motivation_zh`, `method_family_zh`, `method_details_zh`, `experiments_zh`, `limitations_zh`, `method_advantages_zh`, `method_disadvantages_zh`, `evidence_boundary_zh`。

`deep_read_audit` 至少包括：
`mode=\"task_subagent\"`, `subagent_used=true`, `status`, `text_path`, `evidence_chars`。

最后回复同一份 JSON，不要额外解释。
"""


def find_claude() -> str:
    explicit = str(os.environ.get("CLAUDE_PATH") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    if DEFAULT_NODE_BIN.exists():
        candidate = DEFAULT_NODE_BIN / "claude"
        if candidate.exists():
            return str(candidate)
    return shutil.which("claude") or ""


def claude_env() -> dict[str, str]:
    env = os.environ.copy()
    if DEFAULT_NODE_BIN.exists():
        env["PATH"] = os.pathsep.join([str(DEFAULT_NODE_BIN), env.get("PATH", "")])
    return env


def _normalize_claude_stdout(stdout: str) -> dict[str, Any]:
    payload = first_json_object(stdout)
    if payload:
        result = payload.get("result")
        if isinstance(result, str):
            nested = first_json_object(result)
            if nested:
                return nested
        return payload
    return {}


def run_claude_deep_read(
    *,
    prompt_path: Path,
    run_path: Path,
    expected_output_path: Path,
    timeout_sec: int = 1800,
    mode: str = "auto",
) -> dict[str, Any]:
    run_path = ensure_inside_reading(run_path, label="Claude 运行目录")
    expected_output_path = ensure_inside_reading(expected_output_path, label="Claude 输出文件")
    claude_dir = run_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    prompt = prompt_path.read_text(encoding="utf-8")
    if mode == "prepare":
        return {
            "status": "prepared_for_main_claude_subagent",
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "run_executed": False,
        }
    claude = find_claude()
    if not claude:
        return {
            "status": "blocked_claude_code_not_found",
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "run_executed": False,
        }
    cmd = [
        claude,
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--add-dir",
        str(READING_ROOT),
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=READING_ROOT,
            env=claude_env(),
            text=True,
            input=prompt,
            capture_output=True,
            timeout=max(60, int(timeout_sec)),
        )
        stdout_path = claude_dir / "stdout.json"
        stderr_path = claude_dir / "stderr.log"
        write_text(stdout_path, proc.stdout)
        write_text(stderr_path, proc.stderr)
        file_payload = read_json(expected_output_path, {})
        stdout_payload = _normalize_claude_stdout(proc.stdout)
        payload = file_payload if isinstance(file_payload, dict) and file_payload else stdout_payload
        status = "claude_completed" if proc.returncode == 0 else "claude_failed"
        if isinstance(payload, dict) and str(payload.get("status") or "").strip():
            status = str(payload.get("status"))
        receipt = {
            "status": status,
            "return_code": proc.returncode,
            "run_executed": True,
            "duration_seconds": round(time.time() - started, 3),
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "result_payload": payload if isinstance(payload, dict) else {},
        }
        write_json(claude_dir / "claude_receipt.json", receipt)
        return receipt
    except subprocess.TimeoutExpired as exc:
        receipt = {
            "status": "blocked_claude_timeout",
            "run_executed": True,
            "duration_seconds": round(time.time() - started, 3),
            "timeout_sec": timeout_sec,
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
        write_json(claude_dir / "claude_receipt.json", receipt)
        return receipt
