from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from taste_backend.common.io import json_safe, tail_text, utc_now, write_text
from taste_backend.contracts.module_catalog import ModuleContract
from taste_backend.runtime.context import FrameworkContext


def _run_streaming(command: list[str], *, cwd: Path, env: dict[str, str], timeout_sec: int | None) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def pump(stream, target: list[str], sink) -> None:
        if stream is None:
            return
        for line in stream:
            target.append(line)
            print(line, end="", file=sink, flush=True)

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, stdout_parts, sys.stdout), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, stderr_parts, sys.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        return_code = int(proc.wait(timeout=timeout_sec))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        return_code = 124
        proc.wait()
    for thread in threads:
        thread.join(timeout=5)
    return return_code, "".join(stdout_parts), "".join(stderr_parts), timed_out


@dataclass(slots=True)
class CommandResult:
    stage: str
    action: str
    command: list[str]
    status: str
    return_code: int
    started_at: str
    finished_at: str
    kind: str = "module"
    stdout_log: str = ""
    stderr_log: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict[str, object]:
        return json_safe(self)


def _log_paths(ctx: FrameworkContext, index: int, stage: str, action: str, kind: str) -> tuple[Path, Path]:
    safe_action = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in action or "default")
    prefix = f"{index:03d}_{kind}_{stage}_{safe_action}"
    log_dir = ctx.run_dir / "logs"
    return log_dir / f"{prefix}.stdout.txt", log_dir / f"{prefix}.stderr.txt"


def _completed_result(
    *,
    ctx: FrameworkContext,
    contract: ModuleContract,
    action: str,
    command: list[str],
    index: int,
    kind: str,
    started_at: str,
    finished_at: str,
    return_code: int,
    stdout: str,
    stderr: str,
) -> CommandResult:
    stdout_log, stderr_log = _log_paths(ctx, index, contract.key, action, kind)
    write_text(ctx.run_dir / "commands" / f"{index:03d}_{kind}_{contract.key}.txt", " ".join(command) + "\n")
    write_text(stdout_log, stdout)
    write_text(stderr_log, stderr)
    status = "completed" if return_code == 0 else "blocked" if contract.key == "environment" and return_code in {20, 30} else "failed"
    return CommandResult(
        stage=contract.key,
        action=action,
        command=command,
        status=status,
        return_code=return_code,
        started_at=started_at,
        finished_at=finished_at,
        kind=kind,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        stdout_tail=tail_text(stdout, 4000),
        stderr_tail=tail_text(stderr, 4000),
    )


def run_module(
    ctx: FrameworkContext,
    *,
    contract: ModuleContract,
    action: str,
    args: Iterable[str] = (),
    index: int = 1,
    kind: str = "module",
    timeout_sec: int | None = None,
) -> CommandResult:
    selected_action = action or contract.default_action
    command = contract.command(ctx.workspace_root, ctx.python, selected_action, args)
    started_at = utc_now()
    if ctx.mode == "dry-run":
        stdout = "dry-run: " + " ".join(command) + "\n"
        finished_at = utc_now()
        return _completed_result(
            ctx=ctx,
            contract=contract,
            action=selected_action,
            command=command,
            index=index,
            kind=kind,
            started_at=started_at,
            finished_at=finished_at,
            return_code=0,
            stdout=stdout,
            stderr="",
        )
    return_code, stdout, stderr, timed_out = _run_streaming(
        command,
        cwd=ctx.workspace_root,
        env=ctx.env(),
        timeout_sec=timeout_sec,
    )
    if timed_out:
        stderr = (stderr + f"\nCommand timed out after {timeout_sec} seconds.\n").lstrip()
    return _completed_result(
        ctx=ctx,
        contract=contract,
        action=selected_action,
        command=command,
        index=index,
        kind=kind,
        started_at=started_at,
        finished_at=utc_now(),
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
    )
