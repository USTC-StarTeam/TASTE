from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from .paths import FRAMEWORK_LOCKS_DIR


@contextmanager
def crawl_resource_lease(
    *,
    operation: str,
    project: str = "",
    on_wait: Callable[[], None] | None = None,
    on_acquired: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Serialize crawl-heavy framework work across all server processes.

    Finding and Reading keep their own internal worker limits. This lease makes
    those limits global by allowing only one crawl-heavy workflow to allocate
    its internal workers at a time.
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
