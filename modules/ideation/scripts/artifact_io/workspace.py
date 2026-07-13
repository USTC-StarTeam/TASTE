from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IDEATION_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = IDEATION_ROOT / "scripts"
RUNTIME_ROOT = IDEATION_ROOT / ".runtime"
OUTPUT_ROOT = RUNTIME_ROOT / "output"
LATEST_RUN = RUNTIME_ROOT / "latest_run"
LATEST_RUN_LOCK = RUNTIME_ROOT / ".latest_run.lock"
RUN_ID_FORMAT = "%Y%m%dT%H%M%S%fZ"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_run_id() -> str:
    return datetime.now(timezone.utc).strftime(RUN_ID_FORMAT)


def ensure_inside_ideation(path: Path) -> Path:
    target = path.expanduser()
    probe = target if target.exists() else target.parent
    resolved_probe = probe.resolve()
    root = IDEATION_ROOT.resolve()
    try:
        resolved_probe.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"写入路径必须位于 modules/ideation 内：{target}") from exc
    return target


def ensure_parent(path: Path) -> None:
    ensure_inside_ideation(path)
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(str(text), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_runtime_run_dir(path: Path) -> Path:
    target = ensure_inside_ideation(path)
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(OUTPUT_ROOT.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"run 目录必须位于 modules/ideation/.runtime/output 内：{target}") from exc
    if target.name == "latest_run":
        raise ValueError("latest_run 只是人工审查副本，程序不能把它当 run 目录使用。")
    return target


def make_run_dir(run_id: str = "", output_root: str | Path | None = None) -> Path:
    del run_id
    root = ensure_runtime_run_dir(Path(output_root).expanduser()) if output_root else OUTPUT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        run_dir = ensure_runtime_run_dir(root / timestamp_run_id())
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            time.sleep(0.001)
            continue
        return run_dir
    raise FileExistsError(f"无法创建唯一 ideation run 目录：{root}")


def existing_run_dir(run_id_or_path: str | Path) -> Path:
    raw = Path(str(run_id_or_path)).expanduser()
    target = raw if raw.is_absolute() or raw.is_dir() else OUTPUT_ROOT / raw
    target = ensure_runtime_run_dir(target)
    if not target.is_dir():
        raise FileNotFoundError(f"run 目录不存在：{target}")
    return target


def refresh_latest_run(run_dir: Path) -> None:
    source = ensure_runtime_run_dir(run_dir)
    if not source.is_dir():
        return
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with LATEST_RUN_LOCK.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        tmp = RUNTIME_ROOT / f".latest_run_tmp_{os.getpid()}_{timestamp_run_id()}"
        try:
            for stale in RUNTIME_ROOT.glob(".latest_run_tmp_*"):
                if stale != tmp and stale.is_dir():
                    shutil.rmtree(stale, ignore_errors=True)
            shutil.copytree(source, tmp)
            if LATEST_RUN.exists():
                shutil.rmtree(LATEST_RUN)
            tmp.rename(LATEST_RUN)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def public_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(IDEATION_ROOT.resolve()))
    except ValueError:
        return str(path)


def compact_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
