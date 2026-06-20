from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from scripts.common.io_utils import ensure_within, read_json, utc_now, write_json, write_text
from scripts.common.shell import runtime_env


def find_claude() -> str:
    env = runtime_env()
    found = shutil.which("claude", path=env.get("PATH", ""))
    return found or "claude"


JSON_OBJECT_KEY_HINTS = {
    "schema_version", "status", "decision", "allow_next_module", "repo_url", "ordered_repo_urls",
    "env_name", "commands", "success_criteria", "machine_assessment", "failure_taxonomy",
}


def _json_object_score(payload: dict[str, Any], span_length: int) -> tuple[int, int]:
    return (sum(1 for key in JSON_OBJECT_KEY_HINTS if key in payload), span_length)


def _decode_json_object_candidates(text: str) -> list[tuple[dict[str, Any], tuple[int, int]]]:
    raw = str(text or "")
    decoder = json.JSONDecoder()
    out: list[tuple[dict[str, Any], tuple[int, int]]] = []
    for start, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(raw[start:])
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append((parsed, _json_object_score(parsed, end)))
    return out


def extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "")
    candidates: list[tuple[dict[str, Any], tuple[int, int]]] = []
    for fence in re.finditer(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S | re.I):
        candidates.extend(_decode_json_object_candidates(fence.group(1)))
    candidates.extend(_decode_json_object_candidates(raw))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def _valid_expected_json(path: Path) -> dict[str, Any]:
    payload = read_json(path, {})
    return payload if isinstance(payload, dict) and payload else {}


def _finish_log(log_path: Path, cmd: list[str], prompt_path: Path, started: str, stdout: str, stderr: str, note: str = "") -> None:
    suffix = f"\n--- NOTE ---\n{note}\n" if note else ""
    log_path.write_text(
        f"$ {' '.join(cmd)} < {prompt_path}\n# started_at={started}\n# finished_at={utc_now()}\n\n"
        f"--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}\n{suffix}",
        encoding="utf-8",
    )


def run_claude_json(
    prompt: str,
    cwd: Path,
    expected_json_path: Path,
    log_path: Path,
    timeout_sec: int = 1800,
    add_dirs: list[Path] | None = None,
    dry_run: bool = False,
    system_prompt: str = "",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    cwd = cwd.expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        expected_json_path = ensure_within(expected_json_path, cwd)
        log_path = ensure_within(log_path, cwd)
        prompt_path = ensure_within(expected_json_path.with_suffix(".prompt.md"), cwd)
        safe_add_dirs = [ensure_within(path, cwd) for path in add_dirs or [] if path.exists()]
    except Exception as exc:
        safe_log = cwd / "claude_runner_path_guard.log"
        write_text(safe_log, f"Claude Code 路径守卫拒绝：{type(exc).__name__}: {exc}\n")
        return {"return_code": 126, "status": "blocked_by_path_guard", "json": {}, "stderr_tail": str(exc), "prompt_path": "", "log_path": str(safe_log)}
    expected_json_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_text(prompt_path, prompt)
    if dry_run:
        payload = {
            "status": "dry_run",
            "expected_json_path": str(expected_json_path),
            "prompt_path": str(prompt_path),
            "created_at": utc_now(),
        }
        write_json(expected_json_path, payload)
        write_text(log_path, "dry-run：未调用 Claude Code。\n")
        return {"return_code": 0, "status": "dry_run", "json": payload, "prompt_path": str(prompt_path), "log_path": str(log_path)}

    cmd = [find_claude(), "-p", "--permission-mode", "bypassPermissions", "--output-format", "text"]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    for path in safe_add_dirs:
        cmd.extend(["--add-dir", str(path)])
    started = utc_now()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env or runtime_env(),
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()
        # communicate() will flush self.stdin if it is still attached; detach after close.
        proc.stdin = None
        deadline = time.monotonic() + timeout_sec if timeout_sec and timeout_sec > 0 else None
        artifact_note = ""
        while proc.poll() is None:
            payload = _valid_expected_json(expected_json_path)
            if payload:
                artifact_note = f"Claude Code 已写出有效 JSON：{expected_json_path}；后端终止额外探测并继续校验。"
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if deadline is not None and time.monotonic() >= deadline:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=10)
                log_path.write_text(f"Claude Code 超时：{timeout_sec}s\n--- STDOUT ---\n{stdout or ''}\n--- STDERR ---\n{stderr or ''}\n", encoding="utf-8")
                return {"return_code": 124, "status": "timeout", "json": {}, "prompt_path": str(prompt_path), "log_path": str(log_path)}
            time.sleep(1.0)
        stdout, stderr = proc.communicate(timeout=10)
        stdout = stdout or ""
        stderr = stderr or ""
        payload = _valid_expected_json(expected_json_path)
        if not payload:
            payload = extract_json_object(stdout)
            if payload:
                write_json(expected_json_path, payload)
        _finish_log(log_path, cmd, prompt_path, started, stdout, stderr, artifact_note)
        return {
            "return_code": 0 if payload and artifact_note else int(proc.returncode or 0),
            "status": "passed" if (payload and (artifact_note or int(proc.returncode or 0) == 0)) else "failed",
            "json": payload if isinstance(payload, dict) else {},
            "stdout_tail": stdout[-8000:],
            "stderr_tail": stderr[-4000:],
            "prompt_path": str(prompt_path),
            "log_path": str(log_path),
            "artifact_completed": bool(artifact_note),
        }
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"Claude Code 超时：{timeout_sec}s\n{exc}\n", encoding="utf-8")
        return {"return_code": 124, "status": "timeout", "json": {}, "prompt_path": str(prompt_path), "log_path": str(log_path)}
    except Exception as exc:
        log_path.write_text(f"Claude Code 调用失败：{type(exc).__name__}: {exc}\n", encoding="utf-8")
        return {"return_code": 125, "status": "error", "json": {}, "prompt_path": str(prompt_path), "log_path": str(log_path)}
