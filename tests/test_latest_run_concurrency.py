from __future__ import annotations

import importlib.util
import os
from multiprocessing.synchronize import Event
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _publish_finding(runtime: str, run_name: str, start: Event, results) -> None:
    module = _load_module(
        ROOT / "modules" / "finding" / "scripts" / "core" / "finding_runtime.py",
        f"finding_runtime_concurrency_{os.getpid()}",
    )
    runtime_root = Path(runtime)
    module.RUNTIME_DIR = runtime_root
    module.RUNS_DIR = runtime_root / "runs"
    module.LATEST_RUN_DIR = runtime_root / "latest_run"
    start.wait(timeout=10)
    try:
        module.publish_latest_run_for_review(module.RUNS_DIR / run_name)
        results.put("")
    except Exception as exc:  # pragma: no cover - assertion reports child failure
        results.put(repr(exc))


def _publish_reading(runtime: str, run_name: str, start: Event, results) -> None:
    module = _load_module(
        ROOT / "modules" / "reading" / "scripts" / "core" / "common.py",
        f"reading_common_concurrency_{os.getpid()}",
    )
    runtime_root = Path(runtime)
    module.READING_ROOT = runtime_root.parent
    module.RUNTIME_ROOT = runtime_root
    module.OUTPUT_ROOT = runtime_root / "output"
    module.LATEST_RUN_ROOT = runtime_root / "latest_run"
    os.environ["READING_REFRESH_LATEST_DURING_TESTS"] = "1"
    start.wait(timeout=10)
    try:
        module.refresh_latest_run(module.OUTPUT_ROOT / run_name)
        results.put("")
    except Exception as exc:  # pragma: no cover - assertion reports child failure
        results.put(repr(exc))


def _make_run(path: Path, marker: str) -> None:
    path.mkdir(parents=True)
    (path / "marker.txt").write_text(marker, encoding="utf-8")
    for index in range(20):
        (path / f"artifact_{index:02d}.txt").write_text(marker * 4096, encoding="utf-8")


def _assert_complete_review_copy(latest: Path) -> None:
    marker = (latest / "marker.txt").read_text(encoding="utf-8")
    assert marker in {"A", "B"}
    assert len(list(latest.glob("artifact_*.txt"))) == 20
    assert all(path.read_text(encoding="utf-8") == marker * 4096 for path in latest.glob("artifact_*.txt"))


def test_finding_latest_run_publish_is_process_safe(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("fork")
    runtime = tmp_path / "finding_runtime"
    _make_run(runtime / "runs" / "find_a", "A")
    _make_run(runtime / "runs" / "find_b", "B")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_publish_finding, args=(str(runtime), run_name, start, results))
        for run_name in ("find_a", "find_b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert [results.get(timeout=5), results.get(timeout=5)] == ["", ""]
    _assert_complete_review_copy(runtime / "latest_run")


def test_reading_latest_run_publish_is_process_safe(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("fork")
    runtime = tmp_path / "reading" / ".runtime"
    _make_run(runtime / "output" / "20260717T010000000001Z", "A")
    _make_run(runtime / "output" / "20260717T010000000002Z", "B")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_publish_reading, args=(str(runtime), run_name, start, results))
        for run_name in ("20260717T010000000001Z", "20260717T010000000002Z")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert [results.get(timeout=5), results.get(timeout=5)] == ["", ""]
    _assert_complete_review_copy(runtime / "latest_run")
