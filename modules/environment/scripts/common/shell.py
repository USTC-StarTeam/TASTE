from __future__ import annotations

import os
import selectors
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from scripts.common.io_utils import utc_now

DANGEROUS_HEADS = {"sudo", "su", "mount", "umount", "mkfs", "fdisk", "shutdown", "reboot"}
SHELL_HEADS = {"bash", "sh", "zsh"}
SHELL_OPTIONS_WITH_VALUE = {"-o", "--rcfile", "--init-file"}
DANGEROUS_FRAGMENTS = ("rm -rf /", "rm -fr /", ":(){", "> /dev/sd", "dd if=", "chmod -R 777 /")
EXTERNAL_RUNTIME_ENV_KEYS = {
    "CONDA_DEFAULT_ENV",
    "CONDA_EXE",
    "CONDA_PREFIX",
    "CONDA_PROMPT_MODIFIER",
    "CONDA_PYTHON_EXE",
    "CONDA_ROOT",
    "CONDA_SHLVL",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LIBRARY_PATH",
    "PIP_PREFIX",
    "PIP_TARGET",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
}
COMMAND_OUTPUT_HEAD_CHARS = 8000
COMMAND_OUTPUT_TAIL_CHARS = 8000


def command_tokens(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command if str(item).strip()]
    if isinstance(command, str):
        return shlex.split(command)
    raise TypeError(f"命令必须是字符串或数组，实际为 {type(command).__name__}")


def command_text(command: list[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in command)


def _shell_invocation_has_inline_command(command: list[str]) -> bool:
    index = 1
    while index < len(command):
        token = str(command[index] or "")
        if token == "--":
            return False
        if not token.startswith("-") or token == "-":
            return False
        if token in {"-c", "--command"}:
            return True
        if token.startswith("-") and not token.startswith("--"):
            short_options = token[1:]
            if "c" in short_options:
                return True
            if "o" in short_options:
                index += 2
                continue
        if token in SHELL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        index += 1
    return False


def _rm_target_is_high_risk(target: str) -> bool:
    value = str(target or "").strip().strip('"').strip("'")
    if not value or value == "--":
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "~")):
        return True
    if normalized in {".", "./", "./*", "..", "../", "../*"}:
        return True
    return normalized.startswith("../") or "/../" in normalized or normalized.endswith("/..")


def _rm_command_high_risk_reason(command: list[str]) -> str:
    if not command or Path(str(command[0])).name != "rm":
        return ""
    targets: list[str] = []
    parsing_options = True
    for raw in command[1:]:
        token = str(raw or "")
        if parsing_options and token == "--":
            parsing_options = False
            continue
        if parsing_options and token.startswith("--") and token != "--":
            continue
        if parsing_options and token.startswith("-") and token != "-":
            continue
        targets.append(token)
    for target in targets:
        if _rm_target_is_high_risk(target):
            return f"rm 目标路径高风险：{target}；禁止删除绝对路径、~、.、.. 或路径穿越目标，请只删除本次 run/repo 内明确的相对文件或目录"
    return ""


def command_is_dangerous(command: list[str]) -> str:
    if not command:
        return "空命令"
    raw_head = str(command[0])
    head = Path(raw_head).name
    if raw_head in {"source", ".", "activate", "deactivate"} or head in {"source", "activate", "deactivate"}:
        return "禁止执行只对交互 shell 生效的环境激活命令；请使用 conda run -p <run内prefix> ... 或 run 目录内脚本"
    if head in DANGEROUS_HEADS:
        return f"禁止执行高风险命令：{head}"
    rm_issue = _rm_command_high_risk_reason(command)
    if rm_issue:
        return rm_issue
    text = command_text(command).lower()
    for fragment in DANGEROUS_FRAGMENTS:
        normalized_fragment = fragment.lower()
        if normalized_fragment in text:
            return f"命令包含危险片段：{fragment}"
    if head in SHELL_HEADS and _shell_invocation_has_inline_command(command):
        return "禁止执行内联 shell；请把复杂命令写入本 run 目录脚本，再用 bash <script> 执行"
    if head in {"conda", "mamba", "micromamba"} and len(command) >= 2:
        if command[1] in {"activate", "deactivate"}:
            return "本模块不执行 conda activate/deactivate；每条命令必须通过 conda run -p <run内prefix> 或后端重写进入隔离环境"
        if len(command) >= 3 and command[1:3] == ["env", "remove"]:
            return "本模块不执行 conda env remove"
        if command[1] in {"remove", "uninstall"} and "--all" in command:
            return "本模块不执行 conda --all 删除"
    return ""


def runtime_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    nvm_bin = env.get("NVM_BIN", "")
    if not nvm_bin:
        candidates = [Path("/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin")]
        for candidate in candidates:
            if candidate.exists():
                nvm_bin = str(candidate)
                env["NVM_BIN"] = nvm_bin
                env.setdefault("NVM_DIR", str(candidate.parents[2]))
                break
    if nvm_bin:
        parts = [nvm_bin, *[part for part in env.get("PATH", "").split(os.pathsep) if part and part != nvm_bin]]
        env["PATH"] = os.pathsep.join(parts)
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.setdefault("CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL", "1")
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env



def _resolved_path_text(value: str | Path) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return str(value)


def _path_without_entries(path_value: str, blocked_entries: set[str]) -> str:
    parts: list[str] = []
    for part in str(path_value or "").split(os.pathsep):
        if not part or _resolved_path_text(part) in blocked_entries:
            continue
        parts.append(part)
    return os.pathsep.join(parts)


def scrub_external_runtime_env(env: dict[str, str]) -> dict[str, str]:
    conda_prefix_keys = {key for key in env if key.startswith("CONDA_PREFIX_")}
    blocked_prefix_bins = {
        _resolved_path_text(Path(str(env[key])).expanduser() / "bin")
        for key in {"CONDA_PREFIX", "VIRTUAL_ENV", *conda_prefix_keys}
        if str(env.get(key) or "").strip()
    }
    if blocked_prefix_bins and env.get("PATH"):
        env["PATH"] = _path_without_entries(env.get("PATH", ""), blocked_prefix_bins)
    for key in {*EXTERNAL_RUNTIME_ENV_KEYS, *conda_prefix_keys}:
        env.pop(key, None)
    return env


def safe_runtime_extra(extra: dict[str, str] | None = None) -> dict[str, str]:
    if not extra:
        return {}
    blocked = {key.upper() for key in EXTERNAL_RUNTIME_ENV_KEYS}
    return {str(k): str(v) for k, v in extra.items() if str(k).upper() not in blocked}


def isolated_runtime_env(run_dir: Path, extra: dict[str, str] | None = None, isolate_home: bool = False) -> dict[str, str]:
    env = runtime_env(safe_runtime_extra(extra))
    scrub_external_runtime_env(env)
    root = Path(run_dir).expanduser().resolve() / ".runtime"
    dirs = {
        "cache": root / "cache",
        "tmp": root / "tmp",
        "config": root / "config",
        "data": root / "share",
        "home": root / "home",
        "pip": root / "cache" / "pip",
        "hf": root / "cache" / "huggingface",
        "hf_datasets": root / "cache" / "huggingface" / "datasets",
        "torch": root / "cache" / "torch",
        "mpl": root / "cache" / "matplotlib",
        "wandb": root / "wandb",
        "conda_pkgs": root / "conda_pkgs",
        "pycache": root / "pycache",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    env.update({
        "ENVIRONMENT_DEV_RUN_DIR": str(Path(run_dir).expanduser().resolve()),
        "XDG_CACHE_HOME": str(dirs["cache"]),
        "XDG_CONFIG_HOME": str(dirs["config"]),
        "XDG_DATA_HOME": str(dirs["data"]),
        "TMPDIR": str(dirs["tmp"]),
        "TEMP": str(dirs["tmp"]),
        "TMP": str(dirs["tmp"]),
        "PIP_CACHE_DIR": str(dirs["pip"]),
        "HF_HOME": str(dirs["hf"]),
        "HF_DATASETS_CACHE": str(dirs["hf_datasets"]),
        "TRANSFORMERS_CACHE": str(dirs["hf"] / "transformers"),
        "TORCH_HOME": str(dirs["torch"]),
        "MPLCONFIGDIR": str(dirs["mpl"]),
        "WANDB_DIR": str(dirs["wandb"]),
        "WANDB_CACHE_DIR": str(dirs["wandb"] / "cache"),
        "WANDB_CONFIG_DIR": str(dirs["wandb"] / "config"),
        "WANDB_MODE": env.get("WANDB_MODE", "offline"),
        "WANDB_SILENT": "true",
        "PYTHONPYCACHEPREFIX": str(dirs["pycache"]),
        "CONDA_PKGS_DIRS": str(dirs["conda_pkgs"]),
    })
    if isolate_home:
        env["HOME"] = str(dirs["home"])
    return env

def run_logged(
    command: Any,
    cwd: Path,
    log_path: Path,
    timeout_sec: int | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    required: bool = True,
) -> dict[str, Any]:
    tokens = command_tokens(command)
    issue = command_is_dangerous(tokens)
    started = utc_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "command": command_text(tokens),
        "tokens": tokens,
        "cwd": str(cwd),
        "log_path": str(log_path),
        "started_at": started,
        "timeout_sec": timeout_sec,
        "required": required,
    }
    if issue:
        record.update({"status": "blocked_by_guard", "return_code": 126, "stderr_tail": issue, "finished_at": utc_now()})
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n$ {record['command']}\n[命令守卫阻止] {issue}\n")
        return record

    output_parts: list[str] = []
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {record['command']}\n# cwd={cwd}\n# started_at={started}\n")
        handle.flush()
        try:
            proc = subprocess.Popen(
                tokens,
                cwd=str(cwd),
                env=env or runtime_env(),
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            if input_text is not None and proc.stdin is not None:
                proc.stdin.write(input_text)
                proc.stdin.close()
            selector = selectors.DefaultSelector()
            if proc.stdout is not None:
                selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_sec if timeout_sec and timeout_sec > 0 else None
            timed_out = False
            while True:
                if deadline and time.monotonic() > deadline and proc.poll() is None:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except Exception:
                            proc.kill()
                    break
                events = selector.select(timeout=0.5)
                for key, _ in events:
                    chunk = key.fileobj.readline()
                    if chunk:
                        output_parts.append(chunk)
                        handle.write(chunk)
                        handle.flush()
                if proc.poll() is not None:
                    break
            if proc.stdout is not None:
                for chunk in proc.stdout.readlines():
                    output_parts.append(chunk)
                    handle.write(chunk)
            return_code = proc.returncode if proc.returncode is not None else 124
            if timed_out:
                return_code = 124
                handle.write(f"\n[超时] 命令超过 {timeout_sec}s，已终止。\n")
            combined_output = "".join(output_parts)
            record.update({
                "status": "timeout" if timed_out else ("passed" if return_code == 0 else "failed"),
                "return_code": return_code,
                "stdout_head": combined_output[:COMMAND_OUTPUT_HEAD_CHARS],
                "stdout_tail": combined_output[-COMMAND_OUTPUT_TAIL_CHARS:],
                "stdout_char_count": len(combined_output),
                "stdout_truncated": len(combined_output) > max(COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS),
                "finished_at": utc_now(),
            })
        except Exception as exc:
            record.update({"status": "error", "return_code": 125, "stderr_tail": f"{type(exc).__name__}: {exc}", "finished_at": utc_now()})
            handle.write(f"\n[执行异常] {type(exc).__name__}: {exc}\n")
    return record
