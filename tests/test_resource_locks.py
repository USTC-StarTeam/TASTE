from __future__ import annotations

import multiprocessing
import queue
from pathlib import Path

import pytest

from path_helpers import ensure_script_paths

ensure_script_paths()


def _hold_crawl_lease(lock_dir: str, acquired, release, events) -> None:
    from runtime import resource_locks

    resource_locks.FRAMEWORK_LOCKS_DIR = Path(lock_dir)
    with resource_locks.crawl_resource_lease(operation="finding", project="alice"):
        events.put("first_acquired")
        acquired.set()
        release.wait(timeout=10)
    events.put("first_released")


def _wait_for_crawl_lease(lock_dir: str, acquired, waiting, events) -> None:
    from runtime import resource_locks

    resource_locks.FRAMEWORK_LOCKS_DIR = Path(lock_dir)
    acquired.wait(timeout=10)
    with resource_locks.crawl_resource_lease(
        operation="reading",
        project="bob",
        on_wait=waiting.set,
    ):
        events.put("second_acquired")


def test_crawl_resource_lease_serializes_separate_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    waiting = context.Event()
    events = context.Queue()
    first = context.Process(target=_hold_crawl_lease, args=(str(tmp_path), acquired, release, events))
    second = context.Process(target=_wait_for_crawl_lease, args=(str(tmp_path), acquired, waiting, events))

    first.start()
    second.start()
    assert acquired.wait(timeout=5)
    assert waiting.wait(timeout=5)
    assert events.get(timeout=5) == "first_acquired"
    with pytest.raises(queue.Empty):
        events.get(timeout=0.2)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert {events.get(timeout=5), events.get(timeout=5)} == {"first_released", "second_acquired"}
