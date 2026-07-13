from __future__ import annotations

import glob
import json
import os
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
    explicit = os.environ.get("CLAUDE_PATH", "").strip()
    if explicit:
        candidates.append(explicit)
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    nvm_dir = Path(os.environ.get("NVM_DIR", "")).expanduser() if os.environ.get("NVM_DIR") else Path.home() / ".nvm"
    candidates.extend(sorted(glob.glob(str(nvm_dir / "versions" / "node" / "*" / "bin" / "claude")), reverse=True))
    out: list[str] = []
    for item in candidates:
        if item and item not in out and Path(item).exists():
            out.append(item)
    return out


def find_claude_executable() -> str:
    candidates = _candidate_claude_paths()
    if not candidates:
        raise RuntimeError("未找到 Claude Code CLI；请先确认当前用户环境中存在 claude。")
    return candidates[0]


def _outer_result_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("result", "text", "content", "output", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            chunks = [
                str(item.get("text") or item.get("content") or item.get("output_text") or "") if isinstance(item, dict) else str(item)
                for item in value
            ]
            text = "\n".join(chunk for chunk in chunks if chunk.strip())
            if text:
                return text
        nested = _outer_result_text(value)
        if nested.strip():
            return nested
    return ""


def _safe_env(config: ClaudeRunConfig) -> dict[str, str]:
    env = os.environ.copy()
    nvm_dir = Path(env.get("NVM_DIR", "")).expanduser() if env.get("NVM_DIR") else Path.home() / ".nvm"
    node_bins = sorted(glob.glob(str(nvm_dir / "versions" / "node" / "*" / "bin")), reverse=True)
    if node_bins:
        env["PATH"] = os.pathsep.join([node_bins[0], env.get("PATH", "")]).rstrip(os.pathsep)
    env.update(config.extra_env)
    return env


def extract_markdown_from_stdout(stdout: str) -> str:
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        text = str(stdout or "").strip()
    else:
        text = _outer_result_text(outer).strip() or (outer.strip() if isinstance(outer, str) else "")
    if not text:
        raise ValueError("Claude Code 没有返回可解析的 Markdown 文本。")
    return text


def run_claude_markdown(
    prompt: str,
    run_dir: Path,
    config: ClaudeRunConfig | None = None,
    *,
    artifact_prefix: str = "claude",
) -> tuple[str, dict[str, Any]]:
    run_config = config or ClaudeRunConfig()
    if not artifact_prefix.replace("_", "").isalnum():
        raise ValueError("Claude artifact_prefix must be alphanumeric with optional underscores.")
    command = [
        find_claude_executable(),
        "-p",
        "--output-format",
        "json",
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
        "executable": command[0],
        "model": run_config.model,
        "effort": run_config.effort,
        "timeout_sec": run_config.timeout_sec,
        "cwd": str(IDEATION_ROOT),
        "allowed_dir": str(IDEATION_ROOT),
        "tools": "disabled",
        "output_contract": "idea.md markdown",
    }
    write_json(run_dir / f"{artifact_prefix}_command.json", {"command": command, "meta": meta})
    proc = subprocess.run(
        command,
        input=prompt,
        cwd=IDEATION_ROOT,
        env=_safe_env(run_config),
        text=True,
        capture_output=True,
        timeout=run_config.timeout_sec,
    )
    write_text(run_dir / f"{artifact_prefix}_stdout.json", proc.stdout)
    if proc.stderr.strip():
        write_text(run_dir / f"{artifact_prefix}_stderr.log", proc.stderr)
    meta.update({"returncode": proc.returncode, "stderr_chars": len(proc.stderr or ""), "stdout_chars": len(proc.stdout or "")})
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code 调用失败，returncode={proc.returncode}，详见 {run_dir / f'{artifact_prefix}_stderr.log'}")
    return extract_markdown_from_stdout(proc.stdout), meta
