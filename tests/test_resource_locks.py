from __future__ import annotations

import multiprocessing
import queue
from pathlib import Path

import pytest

from path_helpers import ensure_script_paths

ensure_script_paths()
ROOT = Path(__file__).resolve().parents[1]


def _hold_crawl_service(lock_dir: str, service: str, acquired, release, events) -> None:
    from runtime import resource_locks

    with resource_locks.crawl_service_slot(service, state_root=Path(lock_dir)):
        events.put("first_acquired")
        acquired.set()
        release.wait(timeout=10)
    events.put("first_released")


def _wait_for_crawl_service(lock_dir: str, service: str, acquired, started, events) -> None:
    from runtime import resource_locks

    acquired.wait(timeout=10)
    started.set()
    with resource_locks.crawl_service_slot(service, state_root=Path(lock_dir)):
        events.put("second_acquired")


def _hold_project_lease(lock_dir: str, project: str, acquired, release, events) -> None:
    from runtime import resource_locks

    resource_locks.FRAMEWORK_LOCKS_DIR = Path(lock_dir)
    with resource_locks.project_workflow_lease(workflow="current_find", project=project):
        events.put(f"{project}_acquired")
        acquired.set()
        release.wait(timeout=10)


def _wait_for_project_lease(lock_dir: str, project: str, started, events) -> None:
    from runtime import resource_locks

    resource_locks.FRAMEWORK_LOCKS_DIR = Path(lock_dir)
    started.set()
    with resource_locks.project_workflow_lease(workflow="current_find", project=project):
        events.put(f"{project}_acquired")


def test_crawl_service_slot_blocks_same_service_but_not_other_services(tmp_path):
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    same_started = context.Event()
    other_started = context.Event()
    events = context.Queue()
    first = context.Process(target=_hold_crawl_service, args=(str(tmp_path), "arxiv", acquired, release, events))
    same = context.Process(target=_wait_for_crawl_service, args=(str(tmp_path), "arxiv", acquired, same_started, events))
    other = context.Process(target=_wait_for_crawl_service, args=(str(tmp_path), "openreview", acquired, other_started, events))

    first.start()
    assert acquired.wait(timeout=5)
    assert events.get(timeout=5) == "first_acquired"
    same.start()
    other.start()
    assert same_started.wait(timeout=5)
    assert other_started.wait(timeout=5)
    assert events.get(timeout=5) == "second_acquired"
    with pytest.raises(queue.Empty):
        events.get(timeout=0.2)
    release.set()
    first.join(timeout=10)
    same.join(timeout=10)
    other.join(timeout=10)

    assert first.exitcode == 0
    assert same.exitcode == 0
    assert other.exitcode == 0
    assert {events.get(timeout=5), events.get(timeout=5)} == {"first_released", "second_acquired"}


def test_project_workflow_lease_blocks_same_project_but_not_other_projects(tmp_path):
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    same_started = context.Event()
    other_started = context.Event()
    events = context.Queue()
    holder = context.Process(target=_hold_project_lease, args=(str(tmp_path), "account1_demo", acquired, release, events))
    same = context.Process(target=_wait_for_project_lease, args=(str(tmp_path), "account1_demo", same_started, events))
    other = context.Process(target=_wait_for_project_lease, args=(str(tmp_path), "account2_demo", other_started, events))

    holder.start()
    assert acquired.wait(timeout=5)
    assert events.get(timeout=5) == "account1_demo_acquired"
    same.start()
    other.start()
    assert same_started.wait(timeout=5)
    assert other_started.wait(timeout=5)
    assert events.get(timeout=5) == "account2_demo_acquired"
    with pytest.raises(queue.Empty):
        events.get(timeout=0.2)

    release.set()
    holder.join(timeout=10)
    same.join(timeout=10)
    other.join(timeout=10)

    assert holder.exitcode == 0
    assert same.exitcode == 0
    assert other.exitcode == 0
    assert events.get(timeout=5) == "account1_demo_acquired"


def test_find_and_read_orchestrators_do_not_take_a_whole_job_crawl_lease():
    for relative_path in [
        "framework/scripts/orchestration/run_module.py",
        "framework/scripts/orchestration/run_frontend.py",
        "web/backend/auto_research/web/server.py",
    ]:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "crawl_resource_lease(" not in source
