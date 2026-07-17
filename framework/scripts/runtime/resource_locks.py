from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Callable, Iterator

from .framework_paths import FRAMEWORK_LOCKS_DIR


@contextmanager
def project_workflow_lease(*, workflow: str, project: str) -> Iterator[None]:
    """Serialize one project's artifact workflow across server processes."""
    workflow = str(workflow or "").strip()
    project = str(project or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow):
        raise ValueError(f"Invalid workflow lock name: {workflow}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
        raise ValueError(f"Invalid project name for {workflow} lock: {project}")
    FRAMEWORK_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = FRAMEWORK_LOCKS_DIR / f"{workflow}_{project}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def crawl_resource_lease(
    *,
    operation: str,
    project: str = "",
    on_wait: Callable[[], None] | None = None,
    on_acquired: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Serialize crawl-heavy work across all server processes.

    Finding and Reading retain their internal worker limits. This process-wide
    lease makes those existing limits global across all account workflows.
    """
    FRAMEWORK_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = FRAMEWORK_LOCKS_DIR / "crawl_global.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_wait is not None:
                on_wait()
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if on_acquired is not None:
            on_acquired()
        lease = {
            "pid": os.getpid(),
            "operation": str(operation or "crawl"),
            "project": str(project or ""),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(lease, ensure_ascii=False) + "\n")
        handle.flush()
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
