from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from artifact_io.workspace import IDEATION_ROOT, write_json, write_text


@dataclass(slots=True)
class ClaudeRunConfig:
    model: str = "sonnet"
    effort: str = "high"
    timeout_sec: int = 900
    max_budget_usd: float | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


def _candidate_claude_paths() -> list[str]:
    candidates: list[str] = []
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    candidates.extend(sorted(glob.glob("/home/fmh/workspace/.nvm/versions/node/*/bin/claude"), reverse=True))
    candidates.extend(sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "claude")), reverse=True))
    out: list[str] = []
    for item in candidates:
        if item and item not in out and Path(item).exists():
            out.append(item)
    return out


def find_claude_executable() -> str:
    candidates = _candidate_claude_paths()
    if not candidates:
        raise RuntimeError("未找到 Claude Code CLI；请先确认 nvm 环境中存在 claude。")
    return candidates[0]


def _json_span(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\\s*", "", text)
        text = re.sub(r"\\s*```$", "", text)
    first_obj = text.find("{")
    first_arr = text.find("[")
    starts = [idx for idx in (first_obj, first_arr) if idx >= 0]
    if not starts:
        raise ValueError("Claude 输出中没有 JSON 对象或数组。")
    start = min(starts)
    closing = "}" if text[start] == "{" else "]"
    end = text.rfind(closing)
    if end < start:
        raise ValueError("Claude 输出中的 JSON 缺少闭合括号。")
    return text[start : end + 1]


def parse_json_from_text(text: str) -> Any:
    span = _json_span(text)
    try:
        return json.loads(span)
    except json.JSONDecodeError:
        repaired = re.sub(r",\\s*([}\\]])", r"\\1", span)
        return json.loads(repaired)


def _outer_result_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("result", "text", "content", "output", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, list):
                chunks: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        chunks.append(item)
                    elif isinstance(item, dict):
                        for inner_key in ("text", "content", "output_text"):
                            inner = item.get(inner_key)
                            if isinstance(inner, str) and inner.strip():
                                chunks.append(inner)
                                break
                if chunks:
                    return "\n".join(chunks)
            if isinstance(value, dict):
                nested = _outer_result_text(value)
                if nested.strip():
                    return nested
    return ""


def _extract_payload(stdout: str) -> dict[str, Any]:
    outer: Any
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        outer = parse_json_from_text(stdout)
    if isinstance(outer, dict):
        structured = outer.get("structured_output")
        if isinstance(structured, dict) and isinstance(structured.get("ideas"), list):
            return structured
        if isinstance(outer.get("ideas"), list):
            return outer
    result_text = _outer_result_text(outer)
    if not result_text:
        raise ValueError("Claude Code 没有返回可解析的结果文本。")
    inner = parse_json_from_text(result_text)
    if isinstance(inner, list):
        return {"ideas": inner}
    if not isinstance(inner, dict):
        raise ValueError("Claude Code 结果不是 JSON 对象。")
    return inner


def extract_payload_from_stdout(stdout: str) -> dict[str, Any]:
    return _extract_payload(stdout)


def _safe_env(config: ClaudeRunConfig) -> dict[str, str]:
    env = os.environ.copy()
    node_bins = sorted(glob.glob("/home/fmh/workspace/.nvm/versions/node/*/bin"), reverse=True)
    if node_bins:
        existing = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([node_bins[0], existing]) if existing else node_bins[0]
    env.update(config.extra_env)
    return env


def run_claude_json(
    prompt: str,
    schema: dict[str, Any],
    run_dir: Path,
    config: ClaudeRunConfig | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_config = config or ClaudeRunConfig()
    executable = find_claude_executable()
    command = [
        executable,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
        "--no-session-persistence",
        "--add-dir",
        str(IDEATION_ROOT),
        "--tools",
        "",
    ]
    if run_config.model:
        command.extend(["--model", run_config.model])
    if run_config.effort:
        command.extend(["--effort", run_config.effort])
    if run_config.max_budget_usd is not None:
        command.extend(["--max-budget-usd", str(run_config.max_budget_usd)])

    meta = {
        "executable": executable,
        "model": run_config.model,
        "effort": run_config.effort,
        "timeout_sec": run_config.timeout_sec,
        "cwd": str(IDEATION_ROOT),
        "allowed_dir": str(IDEATION_ROOT),
        "tools": "disabled",
    }
    write_json(run_dir / "claude_command.json", {"command": command, "meta": meta})
    proc = subprocess.run(
        command,
        input=prompt,
        cwd=IDEATION_ROOT,
        env=_safe_env(run_config),
        text=True,
        capture_output=True,
        timeout=run_config.timeout_sec,
    )
    stderr_path = run_dir / "claude_stderr.log"
    write_text(run_dir / "claude_stdout.json", proc.stdout)
    if proc.stderr.strip():
        write_text(stderr_path, proc.stderr)
    meta.update({"returncode": proc.returncode, "stderr_chars": len(proc.stderr or ""), "stdout_chars": len(proc.stdout or "")})
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code 调用失败，returncode={proc.returncode}，详见 {stderr_path}")
    payload = _extract_payload(proc.stdout)
    return payload, meta
