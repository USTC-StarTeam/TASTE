from __future__ import annotations

import ast
import argparse
import importlib.util
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")
_READING_TEST_RUN_COUNTER = 0


def _reading_test_run_id() -> str:
    global _READING_TEST_RUN_COUNTER
    _READING_TEST_RUN_COUNTER += 1
    return f"20260709T000000{_READING_TEST_RUN_COUNTER:06d}Z"


def _reading_run_id_from_input(path: Path) -> str:
    return path.parent.parent.name


def _cleanup_reading_output(name: str) -> None:
    path = ROOT / "modules" / "reading" / ".runtime" / "output" / name
    shutil.rmtree(path, ignore_errors=True)


def _write_reading_input(name: str, payload: dict) -> Path:
    run_id = name if re.fullmatch(r"\d{8}T\d{6}\d{6}Z", str(name or "")) else _reading_test_run_id()
    run_root = ROOT / "modules" / "reading" / ".runtime" / "output" / run_id
    shutil.rmtree(run_root, ignore_errors=True)
    path = run_root / "input" / "source_input.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _cleanup_reading_input(name: str) -> None:
    path = ROOT / "modules" / "reading" / ".runtime" / "output" / name / "input"
    shutil.rmtree(path, ignore_errors=True)


def _load_experiment_controller():
    experimenting_main = _load_experimenting_main()
    return experimenting_main._load_private_script_module(
        "orchestration/controller_session.py",
        "experimenting_controller_session_from_main",
    )


def _load_experimenting_private(relative_path: str, module_name: str):
    experimenting_main = _load_experimenting_main()
    return experimenting_main._load_private_script_module(relative_path, module_name)


def _load_experimenting_main():
    spec = importlib.util.spec_from_file_location(
        "experimenting_main_contract",
        ROOT / "modules" / "experimenting" / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_reading_main():
    spec = importlib.util.spec_from_file_location("reading_main_cli", ROOT / "modules" / "reading" / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_reading_common():
    reading_main = _load_reading_main()
    return reading_main._common_module()


def _load_reading_pipeline():
    reading_main = _load_reading_main()
    return reading_main._read_pipeline_module()


@pytest.fixture
def isolated_reading_latest_run(monkeypatch, tmp_path):
    common = _load_reading_common()
    target = tmp_path / "latest_run"
    monkeypatch.setattr(common, "LATEST_RUN_ROOT", target)
    return target


def _load_reading_private(relative_path: str, module_name: str):
    reading_main = _load_reading_main()
    return reading_main._load_private_script_module(relative_path, module_name)


def _load_reading_claude_subagent():
    return _load_reading_private("orchestration/claude_subagent.py", "reading_claude_subagent_contract_from_main")


def _load_reading_paper_sources():
    reading_main = _load_reading_main()
    reading_main._ensure_runtime_imports()
    import importlib

    return importlib.import_module("acquisition.paper_sources")


def _load_reading_conference_sources():
    reading_main = _load_reading_main()
    reading_main._ensure_runtime_imports()
    import importlib

    return importlib.import_module("acquisition.conference_sources")


def _load_reading_openreview_official():
    reading_main = _load_reading_main()
    reading_main._ensure_runtime_imports()
    import importlib

    return importlib.import_module("acquisition.openreview_official")


def _load_reading_semantic_scholar():
    reading_main = _load_reading_main()
    reading_main._ensure_runtime_imports()
    import importlib

    return importlib.import_module("acquisition.semantic_scholar")


def _markdown_without_protected_spans(text: str) -> str:
    patterns = [
        r"```.*?```",
        r"~~~.*?~~~",
        r"`+.*?`+",
        r"\$\$.*?\$\$",
        r"\$[^$\n]+\$",
        r"\\\(.*?\\\)",
        r"\\\[.*?\\\]",
        r"https?://[^\s)>\]]+",
    ]
    out = str(text or "")
    for pattern in patterns:
        out = re.sub(pattern, "", out, flags=re.DOTALL)
    return out


def _assert_no_raw_reading_math(text: str) -> None:
    unprotected = _markdown_without_protected_spans(text)
    assert not re.search(r"[\u0300-\u036F\u0370-\u03FF\u00B5\u2200-\u22FF\u2190-\u21FF\u27E8-\u27E9×≤≥≈≠±¬ℝᵈ½]", unprotected), unprotected[:1000]


def _assert_no_split_reading_math(text: str) -> None:
    bad_patterns = [
        r"\$[^$\n]+\$_",
        r"\^\$[^$\n]+\$",
        r"\$[^$\n]+\$\$[^$\n]+\$",
        r"\$[^$\n]+\$\^[-{A-Za-z0-9]",
        r"\\sqrt\{\}",
        r"\$\\(?:in|notin|to|pm|times|cdot|leq|geq|subseteq|propto)\$",
        r"[A-Za-z][A-Za-z0-9_]*(?:\([^$\n]*\)|\*)?\$\\(?:in|notin|to|pm|times|cdot|leq|geq|subseteq|propto)\$",
        r"[A-Za-z0-9%Å°)\]]\$\\(?:to|pm|times|cdot|leq|geq|subseteq|propto)\$[A-Za-z0-9%Å°([]",
        r"Cohen'\$s[^$\n]*\$",
        r"\\\$[A-Za-z]+",
        r"\babla_",
        r"\$[^$\n]*ˆ[^$\n]*\$",
        r"\\mu_\{M\}",
        r"\\Delta_\{G\}_\{MM\}",
    ]
    for pattern in bad_patterns:
        match = re.search(pattern, text)
        assert not match, text[max(0, match.start() - 200): match.end() + 200] if match else pattern


def _assert_no_cjk_in_reading_math(text: str) -> None:
    for match in re.finditer(r"\$([^$\n]+)\$", text):
        body = match.group(1)
        assert not re.search(r"[\u4e00-\u9fff，。；：、（）【】]", body), text[max(0, match.start() - 200): match.end() + 200]


def test_reading_runtime_paths_are_under_dot_runtime():
    paths = _load_reading_common()

    reading_root = ROOT / "modules" / "reading"
    assert paths.RUNTIME_ROOT == reading_root / ".runtime"
    assert paths.CONFIG_ROOT == reading_root / "config"
    assert paths.OUTPUT_ROOT == paths.RUNTIME_ROOT / "output"
    assert paths.INPUT_ROOT == paths.OUTPUT_ROOT
    assert paths.WORKSPACE_ROOT == paths.OUTPUT_ROOT
    assert paths.RUNS_ROOT == paths.OUTPUT_ROOT
    assert paths.BATCH_TESTS_ROOT == paths.OUTPUT_ROOT
    assert paths.LEGACY_RUNTIME_RUNS_ROOT == paths.RUNTIME_ROOT / "runs"
    assert paths.LEGACY_RUNTIME_BATCH_TESTS_ROOT == paths.RUNTIME_ROOT / "batch_tests"
    assert paths.LEGACY_WORKSPACE_ROOT == reading_root / "workspace"
    assert paths.CACHE_BATCH_TEST_ROOTS == (paths.OUTPUT_ROOT,)
    assert paths.CACHE_RUN_ROOTS == (paths.OUTPUT_ROOT,)
    assert paths.ensure_workspace() == paths.OUTPUT_ROOT

    run_path = paths.create_run_dir()
    assert re.fullmatch(r"\d{8}T\d{6}\d{6}Z", run_path.name)
    assert run_path == paths.OUTPUT_ROOT / run_path.name
    assert paths.existing_run_dir(run_path.name) == run_path
    try:
        paths.run_dir("pytest_runtime_contract")
    except ValueError as exc:
        assert "精确时间戳" in str(exc)
    else:
        raise AssertionError("non-timestamp Reading run id was accepted")
    try:
        paths.ensure_inside_runtime(paths.LEGACY_WORKSPACE_ROOT / "runs" / "bad", label="legacy")
    except ValueError as exc:
        assert ".runtime" in str(exc)
    else:
        raise AssertionError("legacy workspace path was accepted as a runtime output path")
    for bad_path in [
        paths.LEGACY_RUNTIME_BATCH_TESTS_ROOT / "bad",
        paths.LEGACY_RUNTIME_RUNS_ROOT / "bad",
        paths.RUNTIME_ROOT / "audits" / "bad.md",
    ]:
        try:
            paths.ensure_inside_output(bad_path, label="legacy runtime")
        except ValueError as exc:
            assert ".runtime/output" in str(exc)
        else:
            raise AssertionError(f"legacy runtime path was accepted as an output path: {bad_path}")
    shutil.rmtree(run_path, ignore_errors=True)


def test_reading_latest_run_refresh_replaces_the_complete_snapshot(isolated_reading_latest_run):
    paths = _load_reading_common()
    source = paths.create_run_dir()
    target = isolated_reading_latest_run

    def snapshot(directory: Path) -> dict[str, bytes]:
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    try:
        (source / "read.md").write_text("# current read\n", encoding="utf-8")
        (source / "read_results.json").write_text(
            json.dumps({"run_id": source.name, "status": "complete"}) + "\n",
            encoding="utf-8",
        )
        paper_dir = source / "papers" / "001"
        paper_dir.mkdir(parents=True)
        (paper_dir / "read.md").write_text("# paper\n", encoding="utf-8")

        assert os.environ.get("PYTEST_CURRENT_TEST")
        assert not target.exists()
        assert paths.refresh_latest_run(source) == target
        assert snapshot(target) == snapshot(source)

        (target / "run_manifest.json").write_text('{"run_id":"stale"}\n', encoding="utf-8")
        (target / "read.md").write_text("# stale read\n", encoding="utf-8")
        (target / "stale_only.txt").write_text("stale\n", encoding="utf-8")
        paths.refresh_latest_run(source)
        assert snapshot(target) == snapshot(source)

        (target / "read.md").unlink()
        paths.refresh_latest_run(source)
        assert snapshot(target) == snapshot(source)
    finally:
        shutil.rmtree(source, ignore_errors=True)


def test_reading_http_client_classifies_jina_reader_service():
    http_client = _load_reading_common()

    assert http_client.service_from_url("https://r.jina.ai/http://duckduckgo.com/html/?q=test") == "reader"
    assert http_client.service_from_url("https://s.jina.ai/exact%20paper%20title") == "reader"
    assert http_client.SERVICE_MIN_INTERVAL_SEC["reader"] >= 1.0


def test_reading_http_client_serializes_search_and_github_backends():
    http_client = _load_reading_common()

    assert http_client.service_from_url("https://html.duckduckgo.com/html/") == "web_search"
    assert http_client.service_from_url("https://www.startpage.com/sp/search") == "web_search"
    assert http_client.service_from_url("https://api.github.com/repos/org/repo/contents") == "github"
    assert http_client.SERVICE_MIN_INTERVAL_SEC["web_search"] >= 1.0
    assert http_client.SERVICE_MIN_INTERVAL_SEC["github"] >= 1.0


def test_reading_read_env_loads_supported_integration_settings(monkeypatch, tmp_path):
    common = _load_reading_common()
    settings = {
        "OPENREVIEW_USERNAME": "account@example.test",
        "OPENREVIEW_PASSWORD": "secret",
        "READING_OPENREVIEW_ALLOW_ANONYMOUS_OFFICIAL_CLIENT": "0",
        "UNPAYWALL_EMAIL": "unpaywall@example.test",
        "OPENALEX_MAILTO": "openalex@example.test",
        "CROSSREF_MAILTO": "crossref@example.test",
        "READING_CONTACT_EMAIL": "reading@example.test",
    }
    for key in settings:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "read.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in settings.items()) + "\n", encoding="utf-8")

    loaded = common.load_read_env_file(path)

    assert loaded == settings
    assert {key: os.environ.get(key) for key in settings} == settings


def test_reading_service_contact_emails_keep_provider_boundaries(monkeypatch):
    common = _load_reading_common()
    monkeypatch.setenv("READING_CONTACT_EMAIL", "reading@example.test")
    monkeypatch.setenv("OPENALEX_MAILTO", "openalex@example.test")
    monkeypatch.setenv("CROSSREF_MAILTO", "crossref@example.test")

    assert common.service_contact_email() == "reading@example.test"
    assert common.service_contact_email("openalex") == "openalex@example.test"
    assert common.service_contact_email("crossref") == "crossref@example.test"

    monkeypatch.delenv("OPENALEX_MAILTO")
    monkeypatch.delenv("CROSSREF_MAILTO")
    assert common.service_contact_email("openalex") == "reading@example.test"
    assert common.service_contact_email("crossref") == "reading@example.test"


def test_reading_process_http_blocker_uses_retry_after_for_429(monkeypatch):
    common = _load_reading_common()

    class RetryAfterResponse:
        status_code = 429
        headers = {"retry-after": "17"}

    class NoRetryAfterResponse:
        status_code = 429
        headers = {}

    class ForbiddenResponse:
        status_code = 403
        headers = {}

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})

    rate_limited = common.mark_process_http_blocker("rate-limited", RetryAfterResponse(), "http_429")
    fallback = common.mark_process_http_blocker("rate-limited-fallback", NoRetryAfterResponse(), "http_429")
    forbidden = common.mark_process_http_blocker("forbidden", ForbiddenResponse(), "http_403")

    assert rate_limited["ttl_sec"] == 17.0
    assert fallback["ttl_sec"] == common.SERVICE_RATE_LIMIT_COOLDOWN_SEC
    assert forbidden["ttl_sec"] == common.PROCESS_ACCESS_BLOCKER_SEC


def test_reading_http_client_classifies_biorxiv_service():
    http_client = _load_reading_common()

    assert http_client.service_from_url("https://www.biorxiv.org/content/10.64898/2026.05.31.727600.full.pdf") == "biorxiv"
    assert http_client.SERVICE_MIN_INTERVAL_SEC["biorxiv"] >= 3.0


def test_reading_official_conference_hosts_are_single_request_services():
    http_client = _load_reading_common()
    conference_sources = _load_reading_conference_sources()

    assert http_client.service_from_url("https://api2.openreview.net/notes?id=paper") == "openreview"
    assert http_client.service_from_url("https://iclr.cc/virtual/2026/papers.html") == "iclr"
    assert http_client.service_from_url("https://icml.cc/virtual/2026/poster/61459") == "icml"
    for service in ["openreview", "iclr", "icml"]:
        assert http_client.SERVICE_MIN_INTERVAL_SEC[service] >= 10.0
    assert not hasattr(conference_sources, "crawl_conference_channel")
    assert not hasattr(conference_sources, "conference_channel_report")


def test_reading_service_get_serializes_same_host(monkeypatch, tmp_path):
    http_client = _load_reading_common()
    monkeypatch.setattr(http_client, "_SERVICE_STATE_ROOT", tmp_path)
    monkeypatch.setitem(http_client.SERVICE_MIN_INTERVAL_SEC, "icml", 0.0)
    active = 0
    maximum_active = 0
    counter_lock = threading.Lock()

    class FakeResponse:
        status_code = 200
        url = "https://icml.cc/virtual/2026/poster/61459"
        headers = {"content-type": "text/html"}
        content = b"ok"

    def fake_get(*_args, **_kwargs):
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with counter_lock:
            active -= 1
        return FakeResponse()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    threads = [
        threading.Thread(target=http_client.service_get, args=(FakeResponse.url,))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum_active == 1


def test_reading_service_get_serializes_same_host_across_processes(monkeypatch, tmp_path):
    http_client = _load_reading_common()
    context = multiprocessing.get_context("fork")
    active = context.Value("i", 0)
    maximum_active = context.Value("i", 0)
    counter_lock = context.Lock()
    monkeypatch.setattr(http_client, "_SERVICE_STATE_ROOT", tmp_path)
    monkeypatch.setitem(http_client.SERVICE_MIN_INTERVAL_SEC, "iclr", 0.0)

    class FakeResponse:
        status_code = 200
        url = "https://iclr.cc/virtual/2026/poster/10010812"
        headers = {"content-type": "text/html"}
        content = b"ok"

    def fake_get(*_args, **_kwargs):
        with counter_lock:
            active.value += 1
            maximum_active.value = max(maximum_active.value, active.value)
        time.sleep(0.04)
        with counter_lock:
            active.value -= 1
        return FakeResponse()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    processes = [context.Process(target=http_client.service_get, args=(FakeResponse.url,)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)

    assert [process.exitcode for process in processes] == [0, 0, 0]
    assert maximum_active.value == 1


def test_reading_openreview_403_opens_shared_circuit(monkeypatch, tmp_path):
    http_client = _load_reading_common()
    monkeypatch.setattr(http_client, "_SERVICE_STATE_ROOT", tmp_path)
    monkeypatch.setitem(http_client.SERVICE_MIN_INTERVAL_SEC, "openreview", 0.0)
    calls = 0

    class FakeResponse:
        status_code = 403
        url = "https://api2.openreview.net/notes?id=paper"
        headers = {"content-type": "text/html"}
        content = b"403 Forbidden"

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    assert http_client.service_get(FakeResponse.url).status_code == 403
    try:
        http_client.service_get("https://openreview.net/pdf?id=paper")
    except http_client.ServiceCooldownActive as exc:
        assert exc.service == "openreview"
        assert exc.remaining > 0
    else:
        raise AssertionError("OpenReview request escaped the shared 403 circuit")
    assert calls == 1


def test_reading_openreview_browser_fallback_can_follow_one_direct_failure(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    captured: dict[str, object] = {}

    class Slot:
        def __enter__(self):
            return {}

        def __exit__(self, *_args):
            return False

    def fake_slot(service, *, allow_during_cooldown=False):
        captured["service"] = service
        captured["allow_during_cooldown"] = allow_during_cooldown
        return Slot()

    monkeypatch.setattr(read_pipeline, "service_cooldown_remaining", lambda _service: 120.0)
    monkeypatch.setattr(read_pipeline, "service_request_slot", fake_slot)
    monkeypatch.setattr(
        read_pipeline,
        "_download_openreview_pdf_with_browser_login_unlocked",
        lambda _url, _target: (True, {"accepted": True, "reason": "openreview_browser_login_pdf"}),
    )

    downloaded, receipt = read_pipeline._download_openreview_pdf_with_browser_login(
        "https://openreview.net/pdf?id=paper",
        tmp_path / "paper.pdf",
        after_direct_failure=True,
    )

    assert downloaded is True
    assert receipt["accepted"] is True
    assert captured == {"service": "openreview", "allow_during_cooldown": True}


def test_reading_openreview_client_timeout_has_no_detached_request_thread():
    openreview_official = _load_reading_openreview_official()
    source = (ROOT / "modules" / "reading" / "scripts" / "acquisition" / "openreview_official.py").read_text(encoding="utf-8")
    assert "threading.Thread" not in source

    captured: dict[str, object] = {}

    class Session:
        def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return "ok"

    class Client:
        session = Session()

    openreview_official._configure_client_timeout(Client())
    assert Client.session.request("GET", "https://api2.openreview.net/notes") == "ok"
    assert float(captured["timeout"]) >= 5.0


def test_reading_has_no_python_markdown_math_formatter():
    common = _load_reading_common()

    assert not hasattr(common, "normalize_read_markdown_math")
    assert not hasattr(common, "validate_read_markdown_math")
    assert not hasattr(common, "finalize_read_markdown")

    source = (ROOT / "modules" / "reading" / "scripts" / "core" / "common.py").read_text(encoding="utf-8")
    assert "def normalize_read_markdown_math" not in source
    assert "def validate_read_markdown_math" not in source
    assert "def finalize_read_markdown" not in source


def test_reading_has_no_python_read_md_renderers():
    reading_main = _load_reading_main()

    assert not hasattr(reading_main, "_render_project_read_md")

    source = (ROOT / "modules" / "reading" / "scripts" / "pipeline" / "read_pipeline.py").read_text(encoding="utf-8")
    for name in [
        "_render_read_md",
        "_render_single_read_md",
        "_render_standalone_read_md",
        "_format_article_markdown_user_sections",
    ]:
        assert f"def {name}" not in source
    assert "Final read.md is deterministic per-paper Markdown concatenation only" in source
    assert "Python only assembles completed subagent-written article Markdown and does not synthesize scientific summaries" in source


def test_reading_has_no_markdown_math_repair_loop():
    source = (ROOT / "modules" / "reading" / "scripts" / "pipeline" / "read_pipeline.py").read_text(encoding="utf-8")

    forbidden = [
        "READING_MARKDOWN_MATH_REPAIR_ROUNDS",
        "_repair_article_math_with_subagent",
        "_build_article_math_repair_prompt",
        "_build_final_read_md_repair_prompt",
        "deep_read_math_repair_prompt",
        "math_repair_round",
        "返修轮",
    ]
    for item in forbidden:
        assert item not in source


def test_reading_removed_subagent_routes_stay_removed():
    claude_subagent = _load_reading_claude_subagent()

    assert not hasattr(claude_subagent, "build_read_markdown_aggregate_prompt")
    assert not hasattr(claude_subagent, "build_read_markdown_math_audit_prompt")
    assert not hasattr(claude_subagent, "build_method_summary_table_prompt")
    assert not hasattr(claude_subagent, "run_claude_method_summary_table")


def test_reading_single_paper_prompt_uses_fixed_source_and_link_metadata():
    reading_root = ROOT / "modules" / "reading"
    claude_subagent = _load_reading_claude_subagent()
    run_id = _reading_test_run_id()

    arxiv_paper = {
        "title": "Prompt Metadata Paper",
        "source": "arxiv",
        "venue": "arXiv",
        "published": "2026-06-01T15:35:02Z",
        "url": "https://arxiv.org/abs/2606.02386v2",
        "pdf_url": "https://arxiv.org/pdf/2606.02386v2",
        "metadata": {"published": "2026-06-01", "doi": "10.0000/example"},
    }
    arxiv_packet = {
        "pdf_url": "https://runtime.example.invalid/not-input-metadata.pdf",
        "published": "2026-06-30",
        "text_path": f".runtime/output/{run_id}/papers/001/extracted/full_text.txt",
        "full_text_chars": 2400,
    }
    metadata_lines = claude_subagent.article_metadata_markdown_lines(arxiv_paper, arxiv_packet)
    assert metadata_lines == [
        "- **来源：** arXiv 2026-06-01",
        "- **论文链接：** URL：[论文页面](<https://arxiv.org/abs/2606.02386v2>)；PDF：[PDF](<https://arxiv.org/pdf/2606.02386v2>)",
    ]

    iclr_lines = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "ICLR Paper",
            "source": "openreview",
            "venue": "ICLR",
            "year": 2026,
            "url": "https://openreview.net/forum?id=abc12345",
            "pdf_url": "https://openreview.net/pdf?id=abc12345",
        },
        {},
    )
    assert iclr_lines[0] == "- **来源：** ICLR 2026"

    spotlight_lines = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "ICML Spotlight Paper",
            "source": "openreview",
            "venue": "ICML",
            "year": 2026,
            "presentation_type": "spotlight",
        },
        {},
    )
    assert spotlight_lines[0] == "- **来源：** ICML 2026 Spotlight"

    poster_lines = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "NeurIPS Poster Paper",
            "source": "neurips_official_papers",
            "venue": "NeurIPS",
            "year": 2025,
            "presentation_type": "poster",
        },
        {},
    )
    assert poster_lines[0] == "- **来源：** NeurIPS 2025 Poster"

    dated_source_with_stray_presentation = claude_subagent.article_metadata_markdown_lines(
        {
            **arxiv_paper,
            "presentation_type": "oral",
        },
        {},
    )
    assert dated_source_with_stray_presentation[0] == "- **来源：** arXiv 2026-06-01"

    journal_with_stray_tier = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "Journal Paper",
            "source": "journal",
            "venue": "Cell",
            "year": 2026,
            "presentation_type": "poster",
        },
        {},
    )
    assert journal_with_stray_tier[0] == "- **来源：** Cell 2026"

    acquired_pdf_lines = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "Acquired PDF Paper",
            "source": "icml_official_virtual",
            "venue": "ICML",
            "year": 2026,
            "url": "https://icml.cc/virtual/2026/poster/1",
            "pdf_url": "",
        },
        {"pdf_url": "https://arxiv.org/pdf/2605.00001"},
    )
    assert acquired_pdf_lines[1] == "- **论文链接：** URL：[论文页面](<https://icml.cc/virtual/2026/poster/1>)；PDF：[PDF](<https://arxiv.org/pdf/2605.00001>)"

    openreview_official_pdf_lines = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "Official OpenReview PDF Paper",
            "source": "openreview",
            "venue": "ICLR",
            "year": 2026,
            "url": "https://openreview.net/forum?id=officialNote",
            "pdf_url": "",
        },
        {"pdf_url": "openreview://officialNote/pdf"},
    )
    assert openreview_official_pdf_lines[1] == "- **论文链接：** URL：[论文页面](<https://openreview.net/forum?id=officialNote>)；PDF：[PDF](<https://openreview.net/pdf?id=officialNote>)"

    conference_with_arxiv_pdf_lines = claude_subagent.article_metadata_markdown_lines(
        {
            "title": "Conference Paper With Preprint PDF",
            "source": "icml_official_virtual",
            "venue": "ICML",
            "year": 2026,
            "url": "https://icml.cc/virtual/2026/poster/2",
            "pdf_url": "https://arxiv.org/pdf/2605.00002",
            "presentation_type": "poster",
        },
        {},
    )
    assert conference_with_arxiv_pdf_lines[0] == "- **来源：** ICML 2026 Poster"

    run_path = reading_root / ".runtime" / "output" / run_id / "papers" / "001"
    prompt = claude_subagent.build_deep_read_prompt(
        paper=arxiv_paper,
        packet=arxiv_packet,
        run_path=run_path,
        output_path=run_path / "outputs" / "reading_result.json",
        article_md_path=run_path / "read.md",
    )
    assert "论文来源、PDF、DOI、URL、代码等元数据" not in prompt
    assert "DOI" not in prompt.split("单篇 Markdown 必须是完整用户阅读正文", 1)[1].split("## 摘要", 1)[0]
    assert "`- **来源：** arXiv 2026-06-01`" in prompt
    assert "`- **论文链接：** URL：[论文页面](<https://arxiv.org/abs/2606.02386v2>)；PDF：[PDF](<https://arxiv.org/pdf/2606.02386v2>)`" in prompt
    assert "两个 `$` 分别紧贴公式的首字符和尾字符" in prompt
    assert "开始行和结束行各自只写 `$$`" in prompt
    assert "可逆关系固定写成 `\\rightleftharpoons`" in prompt
    assert "行末使用 `\\\\`；附加行距写成 `\\\\[4pt]`" in prompt
    assert "栏目之间只保留一个空行" in prompt
    assert "英文原文摘要（翻译为中文）：" in prompt
    assert "markdown-it-texmath` 和 KaTeX 0.16" in prompt
    assert "进程退出码为 0 不代表内容合格" in prompt

    fixed_abstract_prompt = claude_subagent.build_deep_read_prompt(
        paper={
            **arxiv_paper,
            "abstract": "English abstract must not replace the supplied Chinese abstract.",
            "abstract_zh": "这是 Framework 提供的固定中文摘要。",
        },
        packet=arxiv_packet,
        run_path=run_path,
        output_path=run_path / "outputs" / "reading_result.json",
        article_md_path=run_path / "read.md",
    )
    assert "`摘要` 固定逐字写入下方中文摘要全文，包含原有标点。" in fixed_abstract_prompt
    assert "中文摘要（固定输入）：\n这是 Framework 提供的固定中文摘要。" in fixed_abstract_prompt
    assert "English abstract must not replace" not in fixed_abstract_prompt

    repair_prompt = claude_subagent.build_deep_read_repair_prompt(
        paper=arxiv_paper,
        run_path=run_path,
        output_path=run_path / "outputs" / "reading_result.json",
        article_md_path=run_path / "read.md",
        quality_issue="invalid_katex_syntax",
        quality_reason="网页 KaTeX 语法校验失败：Expected '}', got 'EOF'。",
    )
    assert "唯一输入产物是当前目录中的 `read.md`" in repair_prompt
    assert "不要重新精读或重写整篇文章" in repair_prompt
    assert "Expected '}', got 'EOF'" in repair_prompt
    assert "除修复失败处所必需的字符或句子外" in repair_prompt
    assert "正文路径（当前运行目录相对）" not in repair_prompt


def test_reading_uses_abstract_zh_verbatim_and_keeps_translation_fallback():
    read_pipeline = _load_reading_pipeline()
    markdown = """# Paper

**来源：** arXiv 2026-07-12

**论文链接：** URL：[论文页面](<https://example.test/paper>)；PDF：未提供

## 摘要

English abstract awaiting translation.

## 动机与核心创新

动机：测试。
"""
    fixed = read_pipeline._normalize_article_markdown_metadata(
        markdown,
        {
            "title": "Paper",
            "source": "arxiv",
            "published": "2026-07-12",
            "url": "https://example.test/paper",
            "abstract_zh": "固定“中文”摘要，标点保持一致。",
        },
    )
    assert "## 摘要\n\n固定“中文”摘要，标点保持一致。\n\n## 动机与核心创新" in fixed
    assert "English abstract awaiting translation." not in fixed

    fallback = read_pipeline._normalize_article_markdown_metadata(
        markdown,
        {"title": "Paper", "source": "arxiv", "published": "2026-07-12", "url": "https://example.test/paper"},
    )
    assert "English abstract awaiting translation." in fallback


def test_reading_routes_unresolved_prose_latex_back_to_the_llm():
    read_pipeline = _load_reading_pipeline()
    claude_subagent = _load_reading_claude_subagent()
    paper = read_pipeline._normalize_local_input_paper({
        "title": r"\textbf{Prot}ein design",
        "abstract": r"We introduce \textbf{Prot}ein-Ligand Conditioned Diffusion.",
        "abstract_zh": r"\textbf{Prot}蛋白质\textbf{L}配体条件\textbf{D}离散\textbf{D}扩散模型。",
    })

    assert paper["title"] == r"\textbf{Prot}ein design"
    assert paper["abstract"] == r"We introduce \textbf{Prot}ein-Ligand Conditioned Diffusion."
    assert "abstract_zh" not in paper
    assert read_pipeline.has_unresolved_prose_latex_markup(r"plain \sourceformat{word}") is True
    assert read_pipeline.has_unresolved_prose_latex_markup(r"plain \unknowncommand text") is True
    assert read_pipeline.has_unresolved_prose_latex_markup(r"formula $\sourceformat{x}$") is False

    run_path = ROOT / "modules" / "reading" / ".runtime" / "output" / _reading_test_run_id() / "papers" / "001"
    prompt = claude_subagent.build_deep_read_prompt(
        paper=paper,
        packet={"title": paper["title"]},
        run_path=run_path,
        output_path=run_path / "outputs" / "reading_result.json",
        article_md_path=run_path / "read.md",
    )
    assert "英文原文摘要（翻译为中文）" in prompt
    assert "中文摘要（固定输入）" not in prompt
    assert "公式定界符之外的任意 LaTeX 命令均视为来源排版标记" in prompt


def test_reading_article_markdown_requires_a_chinese_abstract(tmp_path):
    read_pipeline = _load_reading_pipeline()
    article_path = tmp_path / "read.md"
    result_payload = {"article_markdown_path": str(article_path)}
    article_path.write_text(
        "# Paper\n\n## 摘要\n\nEnglish abstract copied without translation.\n\n## 动机与核心创新\n\n中文分析。\n",
        encoding="utf-8",
    )

    assert read_pipeline._article_markdown_quality_issue(article_path.read_text(encoding="utf-8")) == "abstract_missing_chinese"
    assert read_pipeline._article_markdown_ready(article_path, result_payload) is False
    assert read_pipeline._article_markdown_quality_issue(
        "# Paper\n\n## 摘要\n\n包含 \\sourceformat{来源排版} 的中文摘要。\n",
    ) == "unresolved_prose_latex_markup"

    article_path.write_text(
        "# Paper\n\n## 摘要\n\n这是经过翻译的中文摘要。\n\n## 动机与核心创新\n\n中文分析。\n",
        encoding="utf-8",
    )
    assert read_pipeline._article_markdown_ready(article_path, result_payload) is True


def test_reading_article_markdown_rejects_invalid_web_katex(monkeypatch):
    frontend_modules = ROOT / "web" / "frontend" / "client" / "node_modules"
    monkeypatch.setenv("NODE_PATH", str(frontend_modules))
    read_pipeline = _load_reading_pipeline()
    prefix = "# Paper\n\n## 摘要\n\n这是完整的中文摘要。\n\n## 方法\n\n"

    assert read_pipeline._article_markdown_quality_issue(
        prefix + r"目标函数为 $L=\lVert x-y\rVert_2^2$。",
    ) == ""
    assert read_pipeline._article_markdown_quality_issue(prefix + r"目标函数为 $x_{a$。") == "invalid_katex_syntax"
    assert "公式定界符 `$` 未闭合" in read_pipeline._article_markdown_quality_reason(prefix + r"目标函数为 $x+1。")

    katex_dir = frontend_modules / "katex"
    if shutil.which("node") and katex_dir.is_dir():
        assert read_pipeline._article_markdown_quality_issue(
            prefix + r"目标函数为 $\notacommand{x}$。",
        ) == "invalid_katex_syntax"


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_reading_retries_content_quality_failure_and_preserves_exact_status(monkeypatch, repair_succeeds):
    read_pipeline = _load_reading_pipeline()
    run_id = _reading_test_run_id()
    item_dir = ROOT / "modules" / "reading" / ".runtime" / "output" / run_id / "papers" / "001_paper"
    item_dir.mkdir(parents=True)
    read_results_path = item_dir / "read_results.json"
    calls: list[str] = []
    logs: list[str] = []

    def fake_run_claude_deep_read(
        prompt_path,
        run_path,
        expected_output_path,
        timeout_sec=1800,
        mode="auto",
        receipt_dir_name="claude",
    ):
        calls.append(receipt_dir_name)
        is_retry = receipt_dir_name == "claude_content_quality_retry"
        if is_retry:
            prompt_text = Path(prompt_path).read_text(encoding="utf-8")
            assert "abstract_missing_chinese" in prompt_text
            assert "完整翻译为中文" in prompt_text
            assert "唯一输入产物是当前目录中的 `read.md`" in prompt_text
            assert "不要重新精读或重写整篇文章" in prompt_text
        abstract = "这是完整的中文摘要翻译。" if is_retry and repair_succeeds else "English abstract copied without translation."
        (run_path / "read.md").write_text(
            f"# Paper\n\n## 摘要\n\n{abstract}\n\n## 动机与核心创新\n\n中文分析。\n",
            encoding="utf-8",
        )
        payload = {
            "status": "completed",
            "subagent_deep_read": True,
            "article_markdown_path": "read.md",
            "deep_read_audit": {
                "subagent_used": True,
                "article_markdown_path": "read.md",
                "article_markdown_written": True,
            },
        }
        expected_output_path.parent.mkdir(parents=True, exist_ok=True)
        expected_output_path.write_text(json.dumps(payload), encoding="utf-8")
        return {
            "status": "completed",
            "run_executed": True,
            "return_code": 0,
            "expected_output_audit": {"exists": True, "valid_json": True},
            "nonruntime_artifact_audit": {"status": "passed", "problem_count": 0},
            "external_temp_artifact_audit": {"status": "passed", "problem_count": 0},
            "result_payload": payload,
        }

    monkeypatch.setattr(read_pipeline, "run_claude_deep_read", fake_run_claude_deep_read)
    prepared = {
        "run_id": run_id,
        "paper_index": 1,
        "paper": {"paper_id": "paper-1", "title": "Paper", "abstract": "English abstract."},
        "full_text_packet": {"full_text_available": True, "full_text_chars": 2000},
        "reading": {},
        "validation": {"full_text_ready": True, "deep_read_complete": False},
        "artifacts": {"read_results": str(read_results_path)},
    }
    try:
        result = read_pipeline._run_reading_subagent_for_prepared_paper(
            prepared=prepared,
            claude_mode="run",
            timeout_sec=60,
            log=logs.append,
        )
        assert calls == ["claude", "claude_content_quality_retry"]
        retry = result["validation"]["content_quality_retry"]
        assert retry["attempted"] is True
        assert retry["new_claude_process"] is True
        assert retry["initial_issue"] == "abstract_missing_chinese"
        assert "未包含合格的中文摘要" in retry["initial_reason"]
        if repair_succeeds:
            assert result["status"] == "complete"
            assert result["validation"]["deep_read_complete"] is True
            assert retry["resolved"] is True
        else:
            assert result["status"] == "abstract_missing_chinese"
            assert result["validation"]["phase"] == "reading_subagent_content_quality_failed"
            assert result["validation"]["content_quality_issue"] == "abstract_missing_chinese"
            assert retry["resolved"] is False
        assert any("content_quality_issue=abstract_missing_chinese" in line for line in logs)
    finally:
        _cleanup_reading_output(run_id)


def test_reading_machine_result_excludes_markdown_body_content():
    read_pipeline = _load_reading_pipeline()
    cleaned = read_pipeline._machine_read_result({
        "status": "complete",
        "paper": {
            "paper_id": "paper-1",
            "title": "Paper One",
            "authors": ["Author"],
            "url": "https://example.test/paper",
            "abstract_zh": "正文摘要",
            "reason_zh": "正文推荐理由",
            "metadata": {"abstract_zh": "嵌套正文摘要"},
        },
        "reading": {"paper_id": "paper-1", "title": "Paper One", "deep_read_complete": True},
        "claude_result": {"status": "completed", "abstract_zh": "正文摘要", "method_details_zh": "正文方法"},
        "claude": {"result_payload": {"status": "completed", "experiments_zh": "正文实验"}},
    })

    assert cleaned["paper"] == {
        "paper_id": "paper-1",
        "title": "Paper One",
        "authors": ["Author"],
        "url": "https://example.test/paper",
    }
    assert cleaned["reading"]["deep_read_complete"] is True
    assert cleaned["claude_result"] == {"status": "completed"}
    assert cleaned["claude"]["result_payload"] == {"status": "completed"}


def test_reading_has_no_python_markdown_math_structure_gate():
    read_pipeline = _load_reading_pipeline()

    assert not hasattr(read_pipeline, "_audit_markdown_math_structure")
    assert not hasattr(read_pipeline, "_audit_reading_payload_math_structure")
    source = (ROOT / "modules" / "reading" / "scripts" / "pipeline" / "read_pipeline.py").read_text(encoding="utf-8")
    assert "def _audit_markdown_math_structure" not in source
    assert "def _audit_reading_payload_math_structure" not in source


def test_reading_standalone_complete_requires_task_subagent_audit():
    read_pipeline = _load_reading_pipeline()

    status = read_pipeline._standalone_final_status(
        {"full_text_chars": 2000},
        {
            "status": "complete",
            "run_executed": True,
            "expected_output_audit": {"valid_json": True},
            "nonruntime_artifact_audit": {"problem_count": 0},
            "external_temp_artifact_audit": {"status": "passed", "problem_count": 0},
        },
        {
            "subagent_deep_read": True,
            "deep_read_audit": {"markdown_math_valid": True},
        },
    )

    assert status == "blocked_claude_result_missing_or_invalid"


def test_reading_standalone_title_reuses_article_cache_before_acquisition(monkeypatch, isolated_reading_latest_run):
    read_pipeline = _load_reading_pipeline()
    paper_sources = _load_reading_paper_sources()
    input_path = _write_reading_input(_reading_test_run_id(), {
        "title": "Cached Standalone Paper Title",
    })
    run_dir = input_path.parent.parent

    def fake_restore(item_dir, paper, *, run_id, paper_index):
        assert paper["title"] == "Cached Standalone Paper Title"
        assert run_id == run_dir.name
        assert paper_index == 1
        (item_dir / "read.md").write_text("# Cached Standalone Paper Title\n", encoding="utf-8")
        return {
            "run_id": run_id,
            "status": "complete",
            "paper": paper,
            "full_text_packet": {"full_text_available": True, "full_text_chars": 2000},
            "validation": {"full_text_ready": True, "deep_read_complete": True},
            "article_cache": {"hit": True},
        }

    monkeypatch.setattr(read_pipeline, "_restore_article_read_cache", fake_restore)
    monkeypatch.setattr(
        paper_sources,
        "acquire_full_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network acquisition ran before cache reuse")),
    )
    args = argparse.Namespace(
        input_json=str(input_path),
        article="",
        run_id="",
        paper_id="",
        title="",
        authors="",
        abstract="",
        url="",
        pdf_url="",
        source="",
        claude_mode="prepare",
        timeout_sec=60,
        force=False,
    )
    try:
        result = read_pipeline.run_standalone_deep_read(args)
        assert result["status"] == "complete"
        assert result["article_cache"]["hit"] is True
        assert result["public_final_artifact_present"] is True
        assert Path(result["latest_run"]) == isolated_reading_latest_run
        assert (isolated_reading_latest_run / "read.md").read_text(encoding="utf-8").startswith("# Cached Standalone")
        latest_result = json.loads((isolated_reading_latest_run / "read_results.json").read_text(encoding="utf-8"))
        assert latest_result["run_id"] == run_dir.name
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_reading_read_cache_without_full_text_forces_acquisition(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    cache_dir = tmp_path / "article-cache"
    cache_dir.mkdir()
    (cache_dir / "read.md").write_text("# Cached read\n", encoding="utf-8")

    monkeypatch.setattr(read_pipeline, "_restore_article_read_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(read_pipeline, "_locate_article_cache_dir", lambda *_args, **_kwargs: cache_dir)
    monkeypatch.setattr(
        read_pipeline,
        "_deep_read_cache_index",
        lambda: (_ for _ in ()).throw(AssertionError("stale timestamp run was consulted")),
    )

    result = read_pipeline._existing_complete_read_result_for_repair(
        tmp_path / "current-item",
        {"paper_id": "paper-1", "title": "Cached read requiring refreshed full text"},
        run_id=_reading_test_run_id(),
        paper_index=1,
    )

    assert result == {}


def test_reading_full_text_replacement_forces_subagent_preparation(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    paper_sources = _load_reading_paper_sources()
    packet = {
        "paper_id": "paper-1",
        "title": "Paper With Refreshed Official Full Text",
        "full_text_available": True,
        "full_text_chars": 5000,
        "text_chars": 5000,
        "text_path": "extracted/full_text.txt",
    }

    monkeypatch.setattr(read_pipeline, "_existing_complete_read_result_for_repair", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(read_pipeline, "_existing_full_text_packet_for_deep_read", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(read_pipeline, "_restore_article_full_text_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(paper_sources, "acquire_full_text", lambda *_args, **_kwargs: dict(packet))
    monkeypatch.setattr(
        read_pipeline,
        "_publish_article_full_text_cache",
        lambda *_args, **_kwargs: {"content_changed": True, "read_cache_invalidated": True},
    )
    monkeypatch.setattr(
        read_pipeline,
        "_restore_article_read_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale read.md was restored after full-text replacement")),
    )

    result = read_pipeline._prepare_local_read_paper(
        run_id=_reading_test_run_id(),
        directory=tmp_path,
        index=1,
        row={"paper_id": "paper-1", "title": packet["title"]},
        log=lambda _message: None,
    )

    assert result["status"] == "prepared_full_text_for_reading_subagent"
    assert result["validation"]["deep_read_complete"] is False


def test_reading_article_cache_invalidates_read_when_pdf_or_full_text_changes(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    cache_dir = tmp_path / "cache"
    item_dir = tmp_path / "item"
    for root in [cache_dir, item_dir]:
        (root / "downloads").mkdir(parents=True)
        (root / "extracted").mkdir(parents=True)
    (cache_dir / "downloads" / "article.pdf").write_bytes(b"%PDF-1.7\nold official body")
    (cache_dir / "extracted" / "full_text.txt").write_text("old full text", encoding="utf-8")
    (cache_dir / "read.md").write_text("# Read from old full text\n", encoding="utf-8")
    (cache_dir / "outputs").mkdir()
    (cache_dir / "outputs" / "reading_result.json").write_text("{}", encoding="utf-8")
    old_fingerprints = read_pipeline._article_cache_content_fingerprints(cache_dir)
    (cache_dir / "manifest.json").write_text(json.dumps({
        "has_full_text": True,
        "has_pdf": True,
        "has_read_md": True,
        "read_content_revision": old_fingerprints["content_revision"],
    }), encoding="utf-8")

    new_pdf = item_dir / "downloads" / "paper.pdf"
    new_text = item_dir / "extracted" / "full_text.txt"
    new_pdf.write_bytes(b"%PDF-1.7\nnew official body")
    new_text.write_text("new full text", encoding="utf-8")
    packet = {
        "pdf_path": str(new_pdf),
        "text_path": str(new_text),
        "pdf_url": "openreview://official-note/pdf",
        "full_text_available": True,
        "full_text_chars": 5000,
        "pdf_acquisition": {"selected": {"kind": "openreview_official_note_pdf"}},
    }

    monkeypatch.setattr(read_pipeline, "ensure_inside_reading", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(read_pipeline, "resolve_reading_path", lambda value: Path(value))
    monkeypatch.setattr(read_pipeline, "_article_full_text_cache_enabled", lambda: True)
    monkeypatch.setattr(read_pipeline, "_target_article_cache_dir", lambda *_args, **_kwargs: (cache_dir, ["title:paper"]))
    monkeypatch.setattr(read_pipeline, "_write_article_cache_aliases", lambda *_args, **_kwargs: None)

    publication = read_pipeline._publish_article_full_text_cache(
        item_dir,
        {"paper_id": "paper-1", "title": "Paper"},
        packet,
    )

    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    assert publication["content_changed"] is True
    assert publication["read_cache_invalidated"] is True
    assert not (cache_dir / "read.md").exists()
    assert not (cache_dir / "outputs").exists()
    assert manifest["has_read_md"] is False
    assert manifest["read_content_revision"] == ""
    assert manifest["full_text_source_kind"] == "openreview_official_note_pdf"
    assert manifest["full_text_content_revision"] == publication["content_revision"]


def test_reading_article_cache_keeps_read_when_full_text_is_identical(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    cache_dir = tmp_path / "cache"
    item_dir = tmp_path / "item"
    for root in [cache_dir, item_dir]:
        (root / "downloads").mkdir(parents=True)
        (root / "extracted").mkdir(parents=True)
    pdf_bytes = b"%PDF-1.7\nsame paper body"
    full_text = "same extracted full text"
    (cache_dir / "downloads" / "article.pdf").write_bytes(pdf_bytes)
    (cache_dir / "extracted" / "full_text.txt").write_text(full_text, encoding="utf-8")
    (cache_dir / "read.md").write_text("# Valid cached read\n", encoding="utf-8")
    fingerprints = read_pipeline._article_cache_content_fingerprints(cache_dir)
    (cache_dir / "manifest.json").write_text(json.dumps({
        "has_full_text": True,
        "has_pdf": True,
        "has_read_md": True,
        "read_content_revision": fingerprints["content_revision"],
    }), encoding="utf-8")
    new_pdf = item_dir / "downloads" / "paper.pdf"
    new_text = item_dir / "extracted" / "full_text.txt"
    new_pdf.write_bytes(pdf_bytes)
    new_text.write_text(full_text, encoding="utf-8")

    monkeypatch.setattr(read_pipeline, "ensure_inside_reading", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(read_pipeline, "resolve_reading_path", lambda value: Path(value))
    monkeypatch.setattr(read_pipeline, "_article_full_text_cache_enabled", lambda: True)
    monkeypatch.setattr(read_pipeline, "_target_article_cache_dir", lambda *_args, **_kwargs: (cache_dir, ["title:paper"]))
    monkeypatch.setattr(read_pipeline, "_write_article_cache_aliases", lambda *_args, **_kwargs: None)

    publication = read_pipeline._publish_article_full_text_cache(
        item_dir,
        {"paper_id": "paper-1", "title": "Paper"},
        {
            "pdf_path": str(new_pdf),
            "text_path": str(new_text),
            "pdf_url": "openreview://official-note/pdf",
            "full_text_available": True,
            "full_text_chars": 5000,
            "pdf_acquisition": {"selected": {"kind": "openreview_official_note_pdf"}},
        },
    )

    assert publication["content_changed"] is False
    assert publication["read_cache_invalidated"] is False
    assert (cache_dir / "read.md").read_text(encoding="utf-8") == "# Valid cached read\n"


def test_reading_article_cache_restore_backfills_content_binding(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    cache_dir = tmp_path / "cache"
    item_dir = tmp_path / "item"
    (cache_dir / "downloads").mkdir(parents=True)
    (cache_dir / "extracted").mkdir(parents=True)
    pdf_path = cache_dir / "downloads" / "article.pdf"
    text_path = cache_dir / "extracted" / "full_text.txt"
    pdf_path.write_bytes(b"%PDF-1.7\nofficial paper body")
    text_path.write_text("official extracted full text\n" * 100, encoding="utf-8")
    paper = {
        "paper_id": "paper-1",
        "title": "Legacy Cache With Current Official Full Text",
        "source": "openreview",
        "venue": "ICLR",
        "year": 2026,
        "url": "https://openreview.net/forum?id=official-note",
    }
    packet = {
        "paper_id": "paper-1",
        "title": paper["title"],
        "full_text_available": True,
        "full_text_chars": text_path.stat().st_size,
        "text_chars": text_path.stat().st_size,
        "text_path": str(text_path),
        "pdf_path": str(pdf_path),
        "pdf_url": "openreview://official-note/pdf",
        "pdf_acquisition": {"selected": {"kind": "openreview_official_note_pdf"}},
    }
    (cache_dir / "paper.json").write_text(json.dumps(paper), encoding="utf-8")
    (cache_dir / "full_text_packet.json").write_text(json.dumps({"papers": [packet]}), encoding="utf-8")
    (cache_dir / "read.md").write_text(
        f"# {paper['title']}\n\n## 摘要\n\n缓存阅读产物。\n",
        encoding="utf-8",
    )
    (cache_dir / "manifest.json").write_text(json.dumps({
        "has_full_text": True,
        "has_pdf": True,
        "has_read_md": True,
    }), encoding="utf-8")

    monkeypatch.setattr(read_pipeline, "ensure_inside_reading", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(read_pipeline, "resolve_reading_path", lambda value: Path(value))
    monkeypatch.setattr(read_pipeline, "_article_cache_enabled", lambda: True)
    monkeypatch.setattr(read_pipeline, "_locate_article_cache_dir", lambda *_args, **_kwargs: cache_dir)
    monkeypatch.setattr(read_pipeline, "_article_cache_same_paper_ok", lambda *_args, **_kwargs: True)

    result = read_pipeline._restore_article_read_cache(
        item_dir,
        paper,
        run_id=_reading_test_run_id(),
        paper_index=1,
    )

    fingerprints = read_pipeline._article_cache_content_fingerprints(cache_dir)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert manifest["full_text_content_revision"] == fingerprints["content_revision"]
    assert manifest["read_content_revision"] == fingerprints["content_revision"]
    assert manifest["full_text_sha256"] == fingerprints["full_text_sha256"]
    assert manifest["pdf_sha256"] == fingerprints["pdf_sha256"]
    assert manifest["full_text_source_kind"] == "openreview_official_note_pdf"
    assert manifest["full_text_pdf_url"] == "openreview://official-note/pdf"


def test_reading_article_cache_invalidates_non_chinese_abstract(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    cache_dir = tmp_path / "cache"
    item_dir = tmp_path / "item"
    cache_dir.mkdir()
    (cache_dir / "read.md").write_text(
        "# Cached Paper\n\n## 摘要\n\nEnglish abstract copied without translation.\n\n## 方法\n\n中文分析。\n",
        encoding="utf-8",
    )
    (cache_dir / "manifest.json").write_text(json.dumps({
        "has_read_md": True,
        "read_content_revision": "legacy",
    }), encoding="utf-8")

    monkeypatch.setattr(read_pipeline, "_article_cache_enabled", lambda: True)
    monkeypatch.setattr(read_pipeline, "_locate_article_cache_dir", lambda *_args, **_kwargs: cache_dir)
    monkeypatch.setattr(read_pipeline, "_article_cache_same_paper_ok", lambda *_args, **_kwargs: True)

    result = read_pipeline._restore_article_read_cache(
        item_dir,
        {"paper_id": "paper-1", "title": "Cached Paper"},
        run_id=_reading_test_run_id(),
        paper_index=1,
    )

    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result == {}
    assert not (cache_dir / "read.md").exists()
    assert manifest["has_read_md"] is False
    assert manifest["read_invalidation_reason"] == "read_content_quality:abstract_missing_chinese"
    assert manifest["read_quality_policy_version"] == read_pipeline.READING_CONTENT_QUALITY_POLICY_VERSION


def test_reading_title_only_cache_keeps_cached_source_and_locators():
    read_pipeline = _load_reading_pipeline()
    merged = read_pipeline._merge_cached_paper_hints(
        {
            "title": "Cached Paper",
            "source": "standalone_input",
            "url": "",
            "pdf_url": "",
            "metadata": {"requested_by": "title"},
        },
        {
            "title": "Cached Paper",
            "source": "nips",
            "venue": "NeurIPS",
            "year": 2025,
            "url": "https://papers.nips.cc/paper.html",
            "pdf_url": "https://papers.nips.cc/paper.pdf",
            "metadata": {"conference_channel": "nips"},
        },
    )

    assert merged["source"] == "nips"
    assert merged["venue"] == "NeurIPS"
    assert merged["year"] == 2025
    assert merged["url"].endswith("paper.html")
    assert merged["pdf_url"].endswith("paper.pdf")
    assert merged["metadata"] == {"conference_channel": "nips", "requested_by": "title"}


def test_reading_http_client_classifies_science_service():
    http_client = _load_reading_common()

    assert http_client.service_from_url("https://www.science.org/doi/pdf/10.1126/science.adz3624") == "science"
    assert http_client.SERVICE_MIN_INTERVAL_SEC["science"] >= 3.0


def test_reading_neurips_pdf_url_preserves_track_suffix():
    read_pipeline = _load_reading_pipeline()
    conference_sources = _load_reading_conference_sources()

    abstract_url = (
        "https://proceedings.neurips.cc/paper_files/paper/2025/hash/"
        "0013efa1327c079e73154d4061c3a396-Abstract-Datasets_and_Benchmarks_Track.html"
    )
    expected_pdf = (
        "https://proceedings.neurips.cc/paper_files/paper/2025/file/"
        "0013efa1327c079e73154d4061c3a396-Paper-Datasets_and_Benchmarks_Track.pdf"
    )

    assert read_pipeline._neurips_pdf_url_from_abstract(abstract_url) == expected_pdf
    assert conference_sources._neurips_pdf_url_from_abstract_url(abstract_url) == expected_pdf
    candidates = conference_sources.official_conference_pdf_candidates({
        "source": "nips",
        "title": "EngiBench: A Framework for Data-Driven Engineering Design Research",
        "url": abstract_url,
    })
    assert candidates[0]["pdf_url"] == expected_pdf


def test_reading_conference_source_only_derives_full_text_from_input_metadata():
    conference_sources = _load_reading_conference_sources()

    candidates = conference_sources.official_conference_pdf_candidates({
        "source": "eccv",
        "title": "Robust Fitting on a Gate Quantum Computer",
        "pdf_url": "https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/00037.pdf",
    })

    assert [row["pdf_url"] for row in candidates] == [
        "https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/00037.pdf"
    ]
    assert candidates[0]["kind"] == "conference_official_pdf_from_input_metadata"


def test_reading_official_title_search_covers_all_conference_channels():
    conference_sources = _load_reading_conference_sources()
    channels = [
        "nips", "iclr", "icml", "sigkdd", "sigir", "cikm", "aaai",
        "iccv", "www", "cvpr", "acl", "ijcai", "eccv", "emnlp",
    ]

    specs_by_channel = {
        channel: conference_sources.official_conference_title_search_specs({
            "source": channel,
            "title": "A Complete Conference Paper Title",
        })
        for channel in channels
    }

    assert all(specs_by_channel[channel] for channel in channels)
    assert specs_by_channel["iclr"][0]["domain"] == "proceedings.iclr.cc"
    assert specs_by_channel["icml"][0]["domain"] == "proceedings.mlr.press"
    assert specs_by_channel["cvpr"][0]["domain"] == "openaccess.thecvf.com"
    assert specs_by_channel["acl"][0]["domain"] == "aclanthology.org"


def test_reading_iclr_proceedings_abstract_derives_official_pdf():
    conference_sources = _load_reading_conference_sources()
    abstract_url = (
        "https://proceedings.iclr.cc/paper_files/paper/2025/hash/"
        "a9b0e4e205bdf232da9f74bfb9469539-Abstract-Conference.html"
    )

    candidates = conference_sources.official_conference_pdf_candidates({
        "source": "iclr",
        "title": "An Official ICLR Proceedings Paper",
        "url": abstract_url,
    })

    assert candidates[0]["pdf_url"] == (
        "https://proceedings.iclr.cc/paper_files/paper/2025/file/"
        "a9b0e4e205bdf232da9f74bfb9469539-Paper-Conference.pdf"
    )
    assert candidates[0]["official_source"] == "ICLR Proceedings"


def test_reading_pmlr_landing_derives_both_official_storage_layouts():
    conference_sources = _load_reading_conference_sources()

    candidates = conference_sources.official_conference_pdf_candidates({
        "source": "icml",
        "title": "A Paper Stored In A PMLR Volume Repository",
        "url": "https://proceedings.mlr.press/v235/example-paper.html",
    })
    urls = [candidate["pdf_url"] for candidate in candidates]

    assert urls == [
        "https://proceedings.mlr.press/v235/example-paper/example-paper.pdf",
        "https://raw.githubusercontent.com/mlresearch/v235/main/assets/example-paper/example-paper.pdf",
    ]


def test_reading_module_scripts_do_not_contain_test_or_batch_audit_scripts():
    scripts_root = ROOT / "modules" / "reading" / "scripts"
    bad = [
        path.relative_to(scripts_root).as_posix()
        for path in scripts_root.rglob("*.py")
        if "test" in path.name.lower() or path.name == "audit_channel_batch.py"
    ]
    assert bad == []

    reading_main = (ROOT / "modules" / "reading" / "main.py").read_text(encoding="utf-8")
    assert "channel_batch_test" not in reading_main
    assert "audit_channel_batch" not in reading_main


def test_reading_readme_is_user_manual_not_work_log():
    text = (ROOT / "modules" / "reading" / "README.md").read_text(encoding="utf-8")
    forbidden = [
        "当前 14 渠道固定输入验收记录",
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "focused run",
        "audit passed",
        "probe_",
        "本轮",
        "复盘仍保留",
        "工作日志",
        "channel-batch-test",
        "audit-channel-batch",
    ]
    for token in forbidden:
        assert token not in text
    assert "read.md" in text


def test_reading_run_read_does_not_self_select_weak_fallback_pool(monkeypatch):
    _cleanup_reading_output("pytest_read_no_weak_pool_contract")
    _cleanup_reading_input("pytest_read_no_weak_pool_contract")
    read_pipeline = _load_reading_pipeline()

    input_path = _write_reading_input("pytest_read_no_weak_pool_contract", {
        "articles": [],
        "strong_recommendations": [],
        "read_candidates": [],
        "screened_ranking": [{"id": "weak", "title": "weak pool paper", "abstract": "x" * 200}],
        "title_candidates": [{"id": "title", "title": "title pool paper", "abstract": "x" * 200}],
        "evaluated_candidates": [{"id": "eval", "title": "evaluated pool paper", "abstract": "x" * 200}],
    })
    run_id = _reading_run_id_from_input(input_path)
    run_root = input_path.parent.parent

    try:
        read_pipeline.run_read(
            run_id=run_id,
            input_json=str(input_path),
            max_papers=5,
            log=lambda _message: None,
        )
    except SystemExit as exc:
        assert "articles/input_articles/papers" in str(exc)
    else:
        raise AssertionError("read selected weak fallback pools instead of requiring local input articles")

    assert not (run_root / "read_results.json").exists()
    assert not (run_root / "read.md").exists()
    _cleanup_reading_output(run_id)
    _cleanup_reading_input("pytest_read_no_weak_pool_contract")


def test_reading_run_read_prepare_splits_full_text_and_reading_subagent_phases(monkeypatch, isolated_reading_latest_run):
    monkeypatch.setenv("READING_DISABLE_ARTICLE_CACHE", "1")
    _cleanup_reading_output("pytest_read_prepare_two_phase_contract")
    _cleanup_reading_input("pytest_read_prepare_two_phase_contract")
    read_pipeline = _load_reading_pipeline()
    paper_sources = _load_reading_paper_sources()
    input_path = _write_reading_input("pytest_read_prepare_two_phase_contract", {
        "articles": [
            {"source": "arxiv", "title": "prepared one", "paper_id": "p1", "url": "https://example.org/one"},
            {"source": "biorxiv", "title": "prepared two", "paper_id": "p2", "url": "https://example.org/two"},
        ]
    })
    run_id = _reading_run_id_from_input(input_path)
    isolated_reading_latest_run.mkdir(parents=True)
    (isolated_reading_latest_run / "read.md").write_text("# stale read\n", encoding="utf-8")
    (isolated_reading_latest_run / "run_manifest.json").write_text('{"run_id":"stale"}\n', encoding="utf-8")

    def fake_acquire_full_text(paper, item_dir, log=print):
        text = "full text evidence " * 120
        text_path = item_dir / "extracted" / "full_text.txt"
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        return {
            "title": paper.get("title"),
            "source": paper.get("source"),
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "full_text_chars": len(text),
            "text_path": str(text_path),
            "pdf_downloaded": True,
        }

    monkeypatch.setattr(paper_sources, "acquire_full_text", fake_acquire_full_text)
    result = read_pipeline.run_read(
        run_id=run_id,
        input_json=str(input_path),
        claude_mode="prepare",
        max_workers=2,
        log=lambda _message: None,
    )

    run_root = input_path.parent.parent
    payload = json.loads((run_root / "read_results.json").read_text(encoding="utf-8"))

    assert result["status"] == "prepared_all_full_text_pending_claude"
    assert payload["execution_phases"] == [
        "full_text_acquisition_for_all_inputs",
        "parallel_reading_subagents_after_full_text_collection",
        "final_read_md_deterministic_concatenation",
    ]
    assert payload["worker_count"] == 2
    assert payload["reading_subagent_worker_count"] == 2
    assert payload["full_text_ready_count"] == 2
    assert payload["deep_read_complete_count"] == 0
    assert all(item["validation"]["full_text_ready"] is True for item in payload["items"])
    assert all(item["validation"]["deep_read_complete"] is False for item in payload["items"])
    assert all(item["validation"]["phase"] == "reading_subagent_completed_after_full_text_collection" for item in payload["items"])
    assert all(item["claude"]["run_executed"] is False for item in payload["items"])
    aggregation = payload["read_markdown_aggregation"]
    assert aggregation["mode"] == "prepare"
    assert aggregation["status"] == "not_started_prepare_mode"
    assert aggregation["valid"] is None
    assert "method_summary_table" not in aggregation
    assert payload["public_final_artifact_present"] is False
    assert not (run_root / "read.md").exists()
    latest_payload = json.loads((isolated_reading_latest_run / "read_results.json").read_text(encoding="utf-8"))
    assert latest_payload["run_id"] == run_id
    assert not (isolated_reading_latest_run / "read.md").exists()
    _cleanup_reading_output(run_id)
    _cleanup_reading_input("pytest_read_prepare_two_phase_contract")


def test_reading_batch_requeues_cooldown_papers_once_with_one_recovery_worker(monkeypatch, isolated_reading_latest_run):
    monkeypatch.setenv("READING_DISABLE_ARTICLE_CACHE", "1")
    read_pipeline = _load_reading_pipeline()
    paper_sources = _load_reading_paper_sources()
    input_path = _write_reading_input("pytest_read_cooldown_batch_requeue", {
        "articles": [
            {"source": "arxiv", "title": "cooldown one", "paper_id": "cool-1", "pdf_url": "https://arxiv.org/pdf/2601.00001"},
            {"source": "arxiv", "title": "cooldown two", "paper_id": "cool-2", "pdf_url": "https://arxiv.org/pdf/2601.00002"},
        ]
    })
    run_id = _reading_run_id_from_input(input_path)
    calls: dict[str, int] = {}
    call_lock = threading.Lock()

    def fake_acquire_full_text(paper, item_dir, log=print):
        paper_id = str(paper["paper_id"])
        with call_lock:
            calls[paper_id] = calls.get(paper_id, 0) + 1
            attempt = calls[paper_id]
        if attempt == 1:
            return {
                "paper_id": paper_id,
                "title": paper["title"],
                "full_text_available": False,
                "full_text_status": "deferred_service_cooldown_retry",
                "full_text_chars": 0,
                "blocked_full_text_reason": {
                    "code": "deferred_service_cooldown_before_full_text_request",
                    "retryable_after_cooldown": True,
                    "cooldown_services": ["arxiv"],
                },
                "pdf_acquisition": {
                    "attempts": [{
                        "service": "arxiv",
                        "pdf_url": paper["pdf_url"],
                        "download_failure_reason": "skipped_due_to_active_challenge_cooldown",
                    }]
                },
            }
        text = "recovered full text evidence " * 100
        text_path = item_dir / "extracted" / "full_text.txt"
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        return {
            "paper_id": paper_id,
            "title": paper["title"],
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "full_text_chars": len(text),
            "text_path": str(text_path),
            "pdf_downloaded": True,
        }

    monkeypatch.setattr(paper_sources, "acquire_full_text", fake_acquire_full_text)
    monkeypatch.setattr(read_pipeline, "service_cooldown_remaining", lambda _service: 0.0)
    result = read_pipeline.run_read(
        run_id=run_id,
        input_json=str(input_path),
        claude_mode="prepare",
        max_workers=16,
        log=lambda _message: None,
    )

    payload = json.loads((input_path.parent.parent / "read_results.json").read_text(encoding="utf-8"))
    assert calls == {"cool-1": 2, "cool-2": 2}
    assert result["full_text_ready_count"] == 2
    assert payload["cooldown_requeue"] == {
        "status": "complete",
        "attempted_paper_count": 2,
        "recovered_full_text_count": 2,
        "worker_count": 1,
        "services": ["arxiv"],
        "waited_sec": 0.0,
    }
    assert "cooldown_expiry_batch_requeue" in payload["execution_phases"]
    assert all(item["validation"]["cooldown_requeue"]["attempted"] is True for item in payload["items"])
    _cleanup_reading_output(run_id)


def test_reading_run_read_uses_subagent_article_markdown_aggregation_and_audit(monkeypatch, isolated_reading_latest_run):
    monkeypatch.setenv("READING_DISABLE_ARTICLE_CACHE", "1")
    _cleanup_reading_output("pytest_read_subagent_md_contract")
    _cleanup_reading_input("pytest_read_subagent_md_contract")
    read_pipeline = _load_reading_pipeline()
    paper_sources = _load_reading_paper_sources()
    input_path = _write_reading_input("pytest_read_subagent_md_contract", {
        "research_topic": "偏好学习与直接模型优化",
        "research_interest": "可迁移的训练目标与实验方法",
        "researcher_profile": "关注机制、数学推导和方法创新",
        "articles": [
            {"source": "arxiv", "title": "subagent one", "paper_id": "p1", "url": "https://example.org/one"},
            {"source": "openreview", "title": "subagent two", "paper_id": "p2", "url": "https://example.org/two"},
        ]
    })
    run_id = _reading_run_id_from_input(input_path)
    isolated_reading_latest_run.mkdir(parents=True)
    (isolated_reading_latest_run / "read.md").write_text("# stale read\n", encoding="utf-8")
    (isolated_reading_latest_run / "run_manifest.json").write_text('{"run_id":"stale"}\n', encoding="utf-8")

    def fake_acquire_full_text(paper, item_dir, log=print):
        text = "这是用于精读的完整正文证据，包含方法、实验、局限和证据边界。 " * 120
        text_path = item_dir / "extracted" / "full_text.txt"
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        return {
            "title": paper.get("title"),
            "source": paper.get("source"),
            "full_text_available": True,
            "full_text_status": "pdf_text_read",
            "full_text_chars": len(text),
            "text_path": str(text_path),
            "pdf_downloaded": True,
        }

    def fake_receipt(status, expected_output_path, payload):
        return {
            "status": status,
            "run_executed": True,
            "return_code": 0,
            "expected_output_path": str(expected_output_path),
            "expected_output_audit": {"exists": True, "valid_json": True},
            "nonruntime_artifact_audit": {"status": "passed", "problem_count": 0},
            "external_temp_artifact_audit": {"status": "passed", "problem_count": 0},
            "result_payload": payload,
        }

    def fake_run_claude_deep_read(
        prompt_path,
        run_path,
        expected_output_path,
        timeout_sec=1800,
        mode="auto",
        receipt_dir_name="claude",
    ):
        assert mode == "run"
        if receipt_dir_name == "claude_scoring":
            payload = {
                "status": "complete",
                "scores": [
                    {"paper_index": 1, "match_score": 8, "transferability_score": 7},
                    {"paper_index": 2, "match_score": 9, "transferability_score": 9},
                ],
            }
            expected_output_path.parent.mkdir(parents=True, exist_ok=True)
            expected_output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return fake_receipt("complete", expected_output_path, payload)
        article_md = run_path / "read.md"
        article_md.write_text(
            "# 论文精读：ARTICLE_MD_BY_SUBAGENT\n\n"
            "## 摘要\n\n"
            "ARTICLE_MD_BY_SUBAGENT 是对原文摘要的中文翻译，说明论文提出一种直接利用偏好样本优化模型的方法，并在目标任务中展示出比传统分阶段流程更紧凑的训练路径。\n\n"
            "## 动机与核心创新\n\n"
            "动机：现有方法在目标场景中需要在复杂流程之间传递监督信号，训练目标和实际偏好存在间隔，导致优化路径绕、误差来源多，难以稳定获得符合需求的输出。论文希望把偏好学习从分散的打分、筛选和再训练环节中收拢出来，让模型直接围绕最终偏好调整。\n\n"
            "核心创新：本文把偏好关系直接写进训练目标，用一个可比较的概率项衡量优选输出相对劣选输出的优势，并通过简单的对数损失推动模型扩大这种差距，从而把原本依赖后处理或额外反馈的步骤压缩到统一优化过程里，使训练信号更贴近用户想要的排序。\n\n"
            "## 方法\n\n"
            "作者的创新方法是把成对偏好转化为直接优化的概率比较。给定输入 x、优选输出 y_w 和劣选输出 y_l，方法用 $p_\\theta(y_w > y_l \\mid x)$ 表示模型认为优选输出更好的概率，再最小化 $-\\log p_\\theta(y_w > y_l \\mid x)$。这个公式的直观含义是：如果模型已经把优选答案排在劣选答案前面，损失会变小；如果排序相反，损失会变大。实际训练时，模型不再先学习一个独立奖励器再用复杂策略优化，而是直接从偏好样本中获得梯度。这样每一对样本都告诉模型应该增加哪类回答的相对概率、压低哪类回答的相对概率，把偏好对齐变成稳定的监督式目标，同时保留对原模型输出分布的可控约束。它的重点不是新的实验配置，而是把排序偏好、概率差距和语言模型更新合成一个可解释的训练目标。\n\n"
            "## 实验结果\n\n"
            "作者做了主要任务和对照实验，整体效果优于基线，但证据仍以论文报告为准。\n\n"
            "## 优缺点总结\n\n"
            "优点是机制清晰；缺点是外推范围和复现细节仍需核查。\n",
            encoding="utf-8",
        )
        payload = {
            "status": "complete",
            "source": "pytest",
            "paper_id": run_path.name,
            "title": run_path.name,
            "subagent_deep_read": True,
            "article_markdown_path": "read.md",
            "deep_read_audit": {
                "mode": "dedicated_claude_subagent",
                "subagent_used": True,
                "status": "complete",
                "text_path": "extracted/full_text.txt",
                "evidence_chars": 2400,
                "article_markdown_path": "read.md",
                "article_markdown_written": True,
                "markdown_math_valid": True,
                "markdown_math_checked_fields": ["article_markdown"],
                "markdown_format_self_check_rounds": 1,
                "markdown_format_self_check_passed": True,
                "markdown_format_self_check_remaining_issues": [],
            },
        }
        expected_output_path.parent.mkdir(parents=True, exist_ok=True)
        expected_output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return fake_receipt("complete", expected_output_path, payload)

    monkeypatch.setattr(paper_sources, "acquire_full_text", fake_acquire_full_text)
    monkeypatch.setattr(read_pipeline, "run_claude_deep_read", fake_run_claude_deep_read)

    result = read_pipeline.run_read(
        run_id=run_id,
        input_json=str(input_path),
        claude_mode="run",
        max_workers=2,
        log=lambda _message: None,
    )

    run_root = input_path.parent.parent
    payload = json.loads((run_root / "read_results.json").read_text(encoding="utf-8"))
    read_md = (run_root / "read.md").read_text(encoding="utf-8")

    assert result["status"] == "complete"
    assert payload["status"] == "complete"
    assert payload["reading_scoring"]["status"] == "complete"
    assert [item["paper_index"] for item in payload["items"]] == [2, 1]
    assert all(item["match_score"] is not None and item["transferability_score"] is not None for item in payload["items"])
    assert payload["read_markdown_aggregation"]["valid"] is True
    assert "method_summary_table" not in payload["read_markdown_aggregation"]
    assert "ARTICLE_MD_BY_SUBAGENT" in read_md
    assert "## 逐篇精读" not in read_md
    assert "## 1. 论文精读：ARTICLE_MD_BY_SUBAGENT" in read_md
    assert "### 摘要" in read_md
    assert "#### 摘要" not in read_md
    assert "- **匹配度：** 9/10\n- **可借鉴性：** 9/10\n- **来源：**" in read_md
    assert "- **论文链接：** URL：[论文页面](<https://example.org/two>)" in read_md
    assert "## 方法总结表格" not in read_md
    assert "| 序号 | 论文 | 来源 | 方法类别 | 核心机制 | 关键实验/指标 | 主要优点 | 主要局限 | 可借鉴点 |" not in read_md
    assert "原论文摘要（中文）" not in read_md
    assert all(item["validation"]["deep_read_complete"] is True for item in payload["items"])
    latest_payload = json.loads((isolated_reading_latest_run / "read_results.json").read_text(encoding="utf-8"))
    assert latest_payload["run_id"] == run_id
    assert (isolated_reading_latest_run / "read.md").read_text(encoding="utf-8") == read_md
    for item in payload["items"]:
        article_md = Path(item["artifacts"]["article_markdown"])
        if not article_md.is_absolute():
            article_md = ROOT / "modules" / "reading" / article_md
        assert "ARTICLE_MD_BY_SUBAGENT" in article_md.read_text(encoding="utf-8")
    _assert_no_split_reading_math(read_md)
    _cleanup_reading_output(run_id)
    _cleanup_reading_input("pytest_read_subagent_md_contract")


def test_reading_claude_subagent_runs_inside_single_runtime_item(monkeypatch, tmp_path):
    reading_root = ROOT / "modules" / "reading"
    claude_subagent = _load_reading_claude_subagent()

    run_id = _reading_test_run_id()
    run_path = reading_root / ".runtime" / "output" / run_id
    prompt_path = run_path / "prompts" / "deep_read_prompt.md"
    output_path = run_path / "outputs" / "reading_result.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("prompt", encoding="utf-8")

    captured: dict[str, object] = {}

    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env")

        def communicate(self, input=None, timeout=None):
            write_expected_output()
            return json.dumps({"status": "complete"}), ""

        def kill(self):
            self.returncode = -9

    def fake_monitor(_pid):
        event = claude_subagent.threading.Event()

        class Thread:
            def join(self, timeout=None):
                return None

        return event, [], Thread()

    def write_expected_output():
        output_path.write_text(
            json.dumps({
                "status": "complete",
                "source": "pytest",
                "paper_id": "pytest",
                "title": "pytest",
                "subagent_deep_read": True,
                "article_markdown_path": "read.md",
                "deep_read_audit": {"mode": "task_subagent", "subagent_used": True, "status": "complete", "text_path": "", "evidence_chars": 1200},
            }),
            encoding="utf-8",
        )

    monkeypatch.setattr(claude_subagent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(claude_subagent.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(claude_subagent, "_start_external_temp_process_monitor", fake_monitor)
    write_expected_output()

    receipt = claude_subagent.run_claude_deep_read(
        prompt_path=prompt_path,
        run_path=run_path,
        expected_output_path=output_path,
        timeout_sec=60,
        mode="run",
    )

    assert captured["cwd"] == run_path
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[cmd.index("--disallowedTools") + 1] == "Agent,Task,EnterWorktree,ExitWorktree"
    assert cmd[cmd.index("--add-dir") + 1] == "."
    assert cmd[cmd.index("--add-dir") + 1] != str(reading_root)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["TMPDIR"] == "tmp"
    assert env["TMP"] == "tmp"
    assert env["TEMP"] == "tmp"
    assert env["TASTE_READING_RUN_DIR"] == "."
    assert str(ROOT) in env["GIT_CEILING_DIRECTORIES"].split(os.pathsep)
    assert receipt["expected_output_audit"]["valid_json"] is True
    assert receipt["nonruntime_artifact_audit"]["problem_count"] == 0
    assert receipt["external_temp_artifact_audit"]["status"] == "passed"
    _cleanup_reading_output(run_id)


def test_reading_claude_receipt_blocks_external_tmp_helper(monkeypatch):
    reading_root = ROOT / "modules" / "reading"
    claude_subagent = _load_reading_claude_subagent()

    run_id = _reading_test_run_id()
    run_path = reading_root / ".runtime" / "output" / run_id
    prompt_path = run_path / "prompts" / "deep_read_prompt.md"
    output_path = run_path / "outputs" / "reading_result.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("prompt", encoding="utf-8")

    class FakePopen:
        returncode = 0
        pid = 12346

        def __init__(self, _cmd, **_kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            payload = {
                "status": "complete",
                "source": "pytest",
                "paper_id": "pytest",
                "title": "pytest",
                "subagent_deep_read": True,
                "article_markdown_path": "read.md",
                "deep_read_audit": {
                    "mode": "task_subagent",
                    "subagent_used": True,
                    "status": "complete",
                    "text_path": "",
                    "evidence_chars": 1200,
                },
                }
            output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return (
                "```json\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\n```"
                + "\ncat > /tmp/write_reading_result.py << 'PY'\n{}\nPY\npython3 /tmp/write_reading_result.py",
                "",
            )

        def kill(self):
            self.returncode = -9

    def fake_monitor(_pid):
        event = claude_subagent.threading.Event()

        class Thread:
            def join(self, timeout=None):
                return None

        return event, [], Thread()

    monkeypatch.setattr(claude_subagent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(claude_subagent.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(claude_subagent, "_start_external_temp_process_monitor", fake_monitor)

    receipt = claude_subagent.run_claude_deep_read(
        prompt_path=prompt_path,
        run_path=run_path,
        expected_output_path=output_path,
        timeout_sec=60,
        mode="run",
    )

    assert receipt["status"] == "blocked_external_temp_artifact_created"
    _cleanup_reading_output(run_id)
    assert receipt["expected_output_audit"]["valid_json"] is True
    assert receipt["external_temp_artifact_audit"]["status"] == "failed_external_temp_artifact_detected"
    assert receipt["external_temp_artifact_audit"]["problem_count"] >= 1


def test_reading_rejects_arxiv_abs_html_as_full_text():
    paper_sources = _load_reading_paper_sources()

    text, receipt = paper_sources._fetch_html_text("https://arxiv.org/abs/1706.03762")

    assert text == ""
    assert receipt["accepted"] is False
    assert receipt["reason"] == "arxiv_abs_page_is_metadata_not_paper_full_text"


def test_reading_blocked_reason_prefers_acm_403_over_openreview_probe_403():
    paper_sources = _load_reading_paper_sources()

    reason = paper_sources._blocked_full_text_reason(
        {
            "title": "ACM blocked same-paper example",
            "doi": "10.1145/3711896.3736799",
            "url": "https://dl.acm.org/doi/10.1145/3711896.3736799",
            "pdf_url": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
        },
        {
            "attempts": [
                {
                    "kind": "acm_official_pdf",
                    "service": "acm",
                    "pdf_url": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
                    "status_code": 403,
                    "download_failure_reason": "http_403",
                }
            ],
            "candidate_discovery": [
                {
                    "kind": "openreview_official_title_search",
                    "service": "openreview",
                    "status_code": 403,
                    "reason": "openreview_official_title_search_forbidden",
                }
            ],
        },
        {
            "attempts": [
                {
                    "kind": "acm_official_html",
                    "service": "acm",
                    "url": "https://dl.acm.org/doi/10.1145/3711896.3736799",
                    "status_code": 403,
                    "reason": "http_403",
                }
            ]
        },
        {"accepted": False, "reason": "missing_pmc_id"},
        {"status": "no_openalex_full_text_hints", "hints": []},
    )

    assert reason["code"] == "blocked_acm_official_pdf_403_no_verified_open_full_text"
    assert "ACM DL 官方" in reason["message_zh"]
    assert reason["site_status_codes"] == [403]


def test_reading_blocked_reason_reports_openreview_network_barrier_after_browser_fallback():
    paper_sources = _load_reading_paper_sources()
    reason = paper_sources._blocked_full_text_reason(
        {
            "title": "FIDIA Function Informed Sequence Design",
            "url": "https://openreview.net/forum?id=pvbJsa0ia0",
        },
        {
            "attempts": [{
                "kind": "openreview_official_note_pdf",
                "pdf_url": "openreview://pvbJsa0ia0/pdf",
                "download_receipt": {
                    "reason": "openreview_official_client_forbidden",
                    "openreview_browser_login": {
                        "url": "https://openreview.net/pdf?id=pvbJsa0ia0",
                        "reason": "openreview_login_page_network_error",
                    },
                },
            }],
        },
        {},
        {},
        {},
        {},
    )

    assert reason["code"] == "blocked_openreview_403_no_verified_open_full_text"
    assert "带凭据浏览器" in reason["message_zh"]
    assert "未配置" not in reason["message_zh"]


def test_reading_deferred_reason_carries_prior_openreview_browser_cooldown():
    paper_sources = _load_reading_paper_sources()
    reason = paper_sources._blocked_full_text_reason(
        {
            "title": "OpenReview Paper During Shared Cooldown",
            "url": "https://openreview.net/forum?id=CG4TVesbcR",
        },
        {
            "attempts": [{
                "kind": "openreview_official_note_pdf",
                "download_receipt": {
                    "reason": "openreview_service_cooldown_active",
                    "attempts": [{
                        "kind": "openreview_official_client_init",
                        "reason": "openreview_service_cooldown_active",
                        "cooldown_reason": "openreview_login_page_network_error",
                    }],
                },
            }],
        },
        {},
        {},
        {},
        {},
    )

    assert reason["code"] == "deferred_service_cooldown_before_full_text_request"
    assert reason["cooldown_services"] == ["openreview"]
    assert reason["pdf_request_count"] == 0
    assert any(item.get("cooldown_reason") == "openreview_login_page_network_error" for item in reason["cooldown_origins"])


def test_reading_pdf_candidate_without_cache_downloads_url(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    calls: list[tuple[str, str]] = []

    def fake_candidates(_paper, **_kwargs):
        return [{"kind": "indexed_pdf", "pdf_url": "https://example.test/paper.pdf", "accepted": True}]

    def fake_download_with_receipt(url, target):
        calls.append((url, str(target)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.5\n")
        return True, {"accepted": True, "url": url}

    def fail_copy(_source, _target):
        raise AssertionError("non-cache PDF candidate tried to copy from an empty cache path")

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", fake_candidates)
    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", fake_download_with_receipt)
    monkeypatch.setattr(read_pipeline, "_copy_pdf", fail_copy)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path, max_chars=None: "full text " * 300)

    downloaded, pdf_path, pdf_url, receipt = read_pipeline._download_first_readable_pdf(
        {"id": "paper", "paper_id": "paper"},
        tmp_path / "pdfs",
        lambda _message: None,
    )

    assert downloaded is True
    assert pdf_url == "https://example.test/paper.pdf"
    assert pdf_path.exists()
    assert calls == [("https://example.test/paper.pdf", str(pdf_path))]
    assert receipt["selected"]["downloaded"] is True


def test_reading_upgrades_arxiv_http_pdf_before_download(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    calls = []

    class Response:
        status_code = 200
        content = b"%PDF-1.5\n"
        headers = {"content-type": "application/pdf"}
        url = "https://arxiv.org/pdf/2606.00001v1"

    def get(url, **kwargs):
        calls.append((url, kwargs.get("timeout")))
        return Response()

    monkeypatch.setattr(read_pipeline, "service_cooldown_remaining", lambda _service: 0)
    monkeypatch.setattr(read_pipeline, "service_get", get)

    target = tmp_path / "paper.pdf"
    downloaded, receipt = read_pipeline._download_pdf_with_receipt(
        "http://arxiv.org/pdf/2606.00001v1",
        target,
    )

    assert downloaded is True
    assert target.exists()
    assert calls == [("https://arxiv.org/pdf/2606.00001v1", 45)]
    assert receipt["url"] == "https://arxiv.org/pdf/2606.00001v1"
    assert read_pipeline._normalize_arxiv_https_url("http://export.arxiv.org/pdf/2606.00001v1") == "https://export.arxiv.org/pdf/2606.00001v1"
    assert read_pipeline._normalize_arxiv_https_url("http://mirror.arxiv.org/paper.pdf") == "http://mirror.arxiv.org/paper.pdf"
    assert read_pipeline._normalize_arxiv_https_url("http://[invalid") == "http://[invalid"
    assert read_pipeline._normalize_arxiv_https_url("http://example.org/paper.pdf") == "http://example.org/paper.pdf"


def test_reading_exact_openreview_locator_downloads_before_broad_discovery(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    discovery_modes: list[bool] = []

    def fake_candidates(_paper, *, fast_only=False):
        discovery_modes.append(fast_only)
        if not fast_only:
            raise AssertionError("broad title discovery ran before the exact OpenReview candidate succeeded")
        return [{
            "kind": "openreview_official_note_pdf",
            "pdf_url": "openreview://pvbJsa0ia0/pdf",
            "openreview_note_id": "pvbJsa0ia0",
            "accepted": True,
        }]

    def fake_download(_candidate, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.5\n")
        return True, {"accepted": True}

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", fake_candidates)
    monkeypatch.setattr(read_pipeline, "download_openreview_official_pdf", fake_download)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path, max_chars=None: "full text " * 300)

    downloaded, _path, url, _receipt = read_pipeline._download_first_readable_pdf(
        {
            "id": "fidia",
            "title": "FIDIA Function Informed Sequence Design",
            "url": "https://openreview.net/forum?id=pvbJsa0ia0",
        },
        tmp_path / "pdfs",
        lambda _message: None,
    )

    assert downloaded is True
    assert url == "openreview://pvbJsa0ia0/pdf"
    assert discovery_modes == [True]


def test_reading_pdf_candidate_continues_after_unextractable_pdf(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    calls: list[str] = []

    def fake_candidates(_paper, **_kwargs):
        return [
            {"kind": "indexed_pdf", "pdf_url": "https://example.test/bad.pdf", "accepted": True},
            {"kind": "arxiv_title_verified_pdf", "pdf_url": "https://example.test/good.pdf", "accepted": True, "arxiv_match": {"entry_id": "arxiv:good"}},
        ]

    def fake_download_with_receipt(url, target):
        calls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.5\n")
        return True, {"accepted": True, "url": url}

    def fake_extract(path, max_chars=None):
        if str(path).endswith("_1.pdf"):
            return "too short"
        return "full text " * 300

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", fake_candidates)
    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", fake_download_with_receipt)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", fake_extract)

    downloaded, pdf_path, pdf_url, receipt = read_pipeline._download_first_readable_pdf(
        {"id": "paper", "paper_id": "paper", "title": "same paper"},
        tmp_path / "pdfs",
        lambda _message: None,
    )

    assert downloaded is True
    assert pdf_url == "https://example.test/good.pdf"
    assert calls == ["https://example.test/bad.pdf", "https://example.test/good.pdf"]
    assert pdf_path.name.endswith("_2.pdf")
    assert receipt["attempts"][0]["rejected_reason"] == "pdf_text_too_short_or_unextractable"
    assert receipt["selected"]["pdf_url"] == "https://example.test/good.pdf"


def test_reading_openreview_official_pdf_precedes_arxiv_title_fallback(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    arxiv_calls: list[str] = []

    def fake_arxiv_candidates(paper, max_results=5):
        arxiv_calls.append(str(paper.get("title")))
        return [{"kind": "arxiv_title_search_candidate", "accepted": True, "pdf_url": "https://arxiv.org/pdf/2510.14989v2", "title": paper.get("title"), "similarity": 1.0}]

    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", fake_arxiv_candidates)
    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "Constrained Diffusion for Protein Design with Hard Structural Constraints",
        "url": "https://openreview.net/forum?id=kkvqVRu2Zy",
        "pdf_url": "https://example.test/indexed-fallback.pdf",
        "source": "openreview",
        "venue": "ICLR",
    })

    assert arxiv_calls == ["Constrained Diffusion for Protein Design with Hard Structural Constraints"]
    assert candidates[0]["kind"] == "openreview_official_note_pdf"
    assert candidates[0]["pdf_url"] == "openreview://kkvqVRu2Zy/pdf"
    assert any(
        item["kind"] == "indexed_pdf"
        and item["pdf_url"] == "https://example.test/indexed-fallback.pdf"
        for item in candidates[1:]
    )
    assert any(
        item["kind"] == "arxiv_title_verified_pdf"
        and item["pdf_url"] == "https://arxiv.org/pdf/2510.14989v2"
        for item in candidates
    )
    assert any(item["kind"] == "openreview_pdf_from_forum_url" for item in candidates)


def test_reading_icml_title_lookup_prefers_openreview_official_over_arxiv(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(
        read_pipeline,
        "openreview_official_pdf_candidates",
        lambda _paper: [{
            "kind": "openreview_official_title_verified_pdf",
            "pdf_url": "openreview://official-note/pdf",
            "openreview_note_id": "official-note",
            "accepted": True,
        }],
    )
    monkeypatch.setattr(
        read_pipeline,
        "_arxiv_pdf_candidates",
        lambda _paper: [{
            "kind": "arxiv_title_search_candidate",
            "pdf_url": "https://arxiv.org/pdf/2601.00001",
            "accepted": True,
        }],
    )

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "A Protein Design Paper Presented at ICML",
        "source": "icml_official_virtual",
        "venue": "ICML",
        "url": "https://icml.cc/virtual/2026/poster/12345",
    })

    assert [candidate["kind"] for candidate in candidates[:2]] == [
        "openreview_official_title_verified_pdf",
        "arxiv_title_verified_pdf",
    ]


def test_reading_openreview_note_id_adds_attachment_pdf_candidates(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "openreview_official_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "semantic_scholar_pdf_candidates", lambda _paper, **_kwargs: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning",
        "source": "iclr",
        "metadata": {"conference_channel": "iclr", "openreview_id": "vDlkJewkDu"},
    })

    by_kind = {item["kind"]: item["pdf_url"] for item in candidates}
    assert by_kind["openreview_pdf_from_forum_url"] == "https://openreview.net/pdf?id=vDlkJewkDu"
    assert by_kind["openreview_pdf_named_from_note_id"] == "https://openreview.net/pdf?id=vDlkJewkDu&name=pdf"
    assert by_kind["openreview_pdf_download_from_note_id"] == "https://openreview.net/pdf?id=vDlkJewkDu&download=true"
    assert by_kind["openreview_api_pdf_from_note_id"] == "https://api.openreview.net/pdf?id=vDlkJewkDu"
    assert by_kind["openreview_api2_pdf_from_note_id"] == "https://api2.openreview.net/pdf?id=vDlkJewkDu"
    assert by_kind["openreview_attachment_pdf_from_note_id"] == "https://openreview.net/attachment?id=vDlkJewkDu&name=pdf"
    assert by_kind["openreview_api_attachment_pdf_from_note_id"] == "https://api.openreview.net/attachment?id=vDlkJewkDu&name=pdf"
    assert by_kind["openreview_api2_attachment_pdf_from_note_id"] == "https://api2.openreview.net/attachment?id=vDlkJewkDu&name=pdf"


def test_reading_title_only_uses_generic_same_paper_resolution(monkeypatch):
    read_pipeline = _load_reading_pipeline()
    calls: list[str] = []
    semantic_calls: list[str] = []

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(
        read_pipeline,
        "_semantic_scholar_pdf_candidates_for_reading",
        lambda paper, **_kwargs: semantic_calls.append(str(paper.get("source") or "")) or [],
    )
    monkeypatch.setattr(
        read_pipeline,
        "openreview_official_pdf_candidates",
        lambda _paper: calls.append("openreview") or [],
    )
    monkeypatch.setattr(
        read_pipeline,
        "_search_result_pdf_candidates",
        lambda _paper: calls.append("search") or [{
            "kind": "search_result_pdf_requires_text_identity",
            "pdf_url": "https://authors.example.test/paper.pdf",
            "accepted": True,
        }],
    )

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "A Standalone Paper Title With No Metadata",
        "source": "standalone_input",
    })

    assert calls == ["openreview", "search"]
    assert semantic_calls == []
    assert candidates == [{
        "kind": "search_result_pdf_requires_text_identity",
        "pdf_url": "https://authors.example.test/paper.pdf",
        "accepted": True,
        "search_result_match": {
            "kind": "search_result_pdf_requires_text_identity",
            "pdf_url": "https://authors.example.test/paper.pdf",
            "accepted": True,
        },
        "requires_pdf_text_identity_check": True,
    }]

    calls.clear()
    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "A Protein Design Paper Presented at ICML",
        "source": "icml_official_virtual",
        "venue": "ICML",
        "url": "https://icml.cc/virtual/2026/poster/12345",
    })

    assert calls == ["openreview", "search"]
    assert semantic_calls == []
    assert candidates[0]["pdf_url"] == "https://authors.example.test/paper.pdf"
    assert candidates[0]["requires_pdf_text_identity_check"] is True

    calls.clear()
    semantic_calls.clear()
    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "An ACL Paper With Only A Metadata Page",
        "source": "acl",
        "venue": "ACL",
        "url": "https://aclanthology.org/events/acl-2026/",
    })

    assert calls == ["search"]
    assert semantic_calls == []
    assert candidates[0]["pdf_url"] == "https://authors.example.test/paper.pdf"


def test_reading_broad_search_scans_each_result_page_once(monkeypatch):
    read_pipeline = _load_reading_pipeline()
    scans: list[str] = []
    duplicate = [{"kind": "search_result", "url": "https://icml.cc/virtual/2026/poster/61459", "accepted": True}]

    monkeypatch.setenv("READING_SEARCH_QUERY_LIMIT", "2")
    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", lambda _query, limit=8: duplicate)
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda _query, limit=8: duplicate)
    monkeypatch.setattr(read_pipeline, "_startpage_result_urls", lambda _query, limit=8: duplicate)

    def fake_page(_paper, url, **_kwargs):
        scans.append(url)
        return [{"kind": "search_result_page", "url": url, "accepted": False, "reason": "no_pdf"}]

    monkeypatch.setattr(read_pipeline, "_publisher_page_candidates_from_url", fake_page)
    read_pipeline._search_result_pdf_candidates({"title": "A Paper Title With Repeated Search Results"})

    assert scans == ["https://icml.cc/virtual/2026/poster/61459"]


def test_reading_conference_title_search_prioritizes_official_domains(monkeypatch):
    read_pipeline = _load_reading_pipeline()
    queries: list[str] = []

    def fake_search(query, limit=8):
        queries.append(query)
        if query.startswith("site:proceedings.iclr.cc"):
            url = "https://proceedings.iclr.cc/paper_files/paper/2025/file/official.pdf"
        elif query.startswith("site:iclr.cc"):
            url = "https://iclr.cc/media/iclr-2026/Papers/paper.pdf"
        else:
            url = "https://authors.example.test/paper.pdf"
        return [{"kind": "search_result", "url": url, "accepted": True}]

    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", fake_search)
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(read_pipeline, "_startpage_result_urls", lambda *_args, **_kwargs: [])

    candidates = read_pipeline._search_result_pdf_candidates({
        "title": "A Verified ICLR Conference Paper",
        "source": "iclr",
        "venue": "ICLR",
    })
    accepted = [item for item in candidates if item.get("accepted")]

    assert queries == [
        'site:proceedings.iclr.cc "A Verified ICLR Conference Paper"',
        'site:iclr.cc "A Verified ICLR Conference Paper"',
        '"A Verified ICLR Conference Paper" PDF',
    ]
    assert [item["pdf_url"] for item in accepted] == [
        "https://proceedings.iclr.cc/paper_files/paper/2025/file/official.pdf",
        "https://iclr.cc/media/iclr-2026/Papers/paper.pdf",
        "https://authors.example.test/paper.pdf",
    ]
    assert accepted[0]["official_conference_title_search"] is True
    assert accepted[1]["official_conference_title_search"] is True
    assert accepted[2]["official_conference_title_search"] is False
    assert read_pipeline._is_conference_presentation_pdf_url(
        "https://iclr.cc/media/iclr-2026/Slides/10009623.pdf"
    ) is True
    assert read_pipeline._is_conference_presentation_pdf_url(
        "https://proceedings.iclr.cc/paper_files/paper/2025/file/official.pdf"
    ) is False


def test_reading_optional_openreview_metadata_accelerates_resolution(monkeypatch):
    read_pipeline = _load_reading_pipeline()
    official_calls: list[dict] = []

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_chatpaper_openreview_cached_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_semantic_scholar_pdf_candidates_for_reading", lambda _paper, **_kwargs: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    def fake_official(paper):
        official_calls.append(paper)
        return [{
            "kind": "openreview_official_note_pdf",
            "pdf_url": "openreview://pvbJsa0ia0/pdf",
            "openreview_note_id": "pvbJsa0ia0",
            "accepted": True,
        }]

    monkeypatch.setattr(read_pipeline, "openreview_official_pdf_candidates", fake_official)
    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "FIDIA Function Informed Sequence Design",
        "source": "input",
        "metadata": {"paper_url": "https://openreview.net/forum?id=pvbJsa0ia0"},
    })

    assert len(official_calls) == 1
    assert any(candidate["pdf_url"] == "openreview://pvbJsa0ia0/pdf" for candidate in candidates)


def test_reading_non_openreview_official_pdf_does_not_add_attachment_candidates(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [{
        "kind": "cvf_official_pdf",
        "accepted": True,
        "pdf_url": "https://openaccess.thecvf.com/content/CVPR2026/papers/example.pdf",
    }])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "A CVPR Paper with an Official Proceedings PDF",
        "source": "cvpr",
        "url": "https://openaccess.thecvf.com/content/CVPR2026/html/example.html",
        "metadata": {"conference_channel": "cvpr"},
    })

    assert candidates == [{
        "kind": "cvf_official_pdf",
        "pdf_url": "https://openaccess.thecvf.com/content/CVPR2026/papers/example.pdf",
        "accepted": True,
        "conference_official_match": {
            "kind": "cvf_official_pdf",
            "accepted": True,
            "pdf_url": "https://openaccess.thecvf.com/content/CVPR2026/papers/example.pdf",
        },
    }]
    assert not any("openreview" in str(item).lower() or "attachment" in str(item).lower() for item in candidates)


def test_reading_declared_cvf_landing_page_accepts_exact_title_without_author_meta(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    html_url = "https://openaccess.thecvf.com/content/ICCV2025/html/Wang_kh_Symmetry_Understanding_of_3D_Shapes_via_Chirality_Disentanglement_ICCV_2025_paper.html"
    pdf_url = "https://openaccess.thecvf.com/content/ICCV2025/papers/Wang_kh_Symmetry_Understanding_of_3D_Shapes_via_Chirality_Disentanglement_ICCV_2025_paper.pdf"
    html = f"""
    <html><head>
      <meta name="citation_title" content="kh: Symmetry Understanding of 3D Shapes via Chirality Disentanglement">
      <meta name="citation_pdf_url" content="{pdf_url}">
    </head><body></body></html>
    """
    monkeypatch.setattr(read_pipeline, "_request_html", lambda *_args, **_kwargs: (html_url, html, {"status_code": 200}))

    candidates = read_pipeline._publisher_page_candidates_from_url(
        {
            "title": "kh: Symmetry Understanding of 3D Shapes via Chirality Disentanglement",
            "authors": ["Weikang Wang", "Tobias Weissberg"],
            "url": html_url,
            "pdf_url": pdf_url,
        },
        html_url,
        kind="publisher_same_paper_page",
    )

    assert candidates[0]["accepted"] is True
    assert candidates[0]["pdf_url"] == pdf_url


def test_reading_acm_doi_does_not_probe_openreview_title_search(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    calls: list[str] = []

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    def fail_openreview(_paper):
        calls.append("openreview")
        raise AssertionError("ACM DOI inputs must not probe OpenReview title search")

    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", fail_openreview)

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "ACM DOI Paper",
        "source": "sigkdd",
        "doi": "10.1145/3711896.3736799",
        "url": "https://dl.acm.org/doi/10.1145/3711896.3736799",
        "pdf_url": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
    })

    assert calls == []
    assert candidates[0]["kind"] == "indexed_pdf"
    assert candidates[0]["pdf_url"] == "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799"


def test_reading_acm_doi_defers_publisher_page_scan_until_after_pdf_403(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    page_calls: list[str] = []

    def fail_page_scan(_paper):
        page_calls.append("publisher_page")
        raise AssertionError("ACM publisher page scan must be deferred until official PDF has been attempted")

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", fail_page_scan)
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "ACM DOI Paper",
        "doi": "10.1145/3711896.3736799",
        "url": "https://dl.acm.org/doi/10.1145/3711896.3736799",
        "pdf_url": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
    })

    assert page_calls == []
    assert candidates[0]["kind"] == "indexed_pdf"


def test_reading_acm_403_late_search_fallback_requires_identity(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    search_calls: list[str] = []
    download_calls: list[str] = []

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", lambda _paper, **_kwargs: [{
        "kind": "indexed_pdf",
        "pdf_url": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
        "accepted": True,
    }])

    def fake_search(paper):
        search_calls.append(str(paper.get("title")))
        return [{
            "kind": "search_result_pdf_requires_text_identity",
            "pdf_url": "https://author.example.edu/acm-paper.pdf",
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    def fake_download(url, target):
        download_calls.append(url)
        if "dl.acm.org" in url:
            return False, {"accepted": False, "reason": "http_403"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF fake")
        return True, {"accepted": True}

    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", fake_search)
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", fake_download)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path, max_chars=None: "ACM DOI Paper\nAlice Example\nAbstract\n" + "paper body " * 300)

    downloaded, _pdf_path, pdf_url, receipt = read_pipeline._download_first_readable_pdf(
        {
            "title": "ACM DOI Paper",
            "authors": ["Alice Example"],
            "doi": "10.1145/3711896.3736799",
            "url": "https://dl.acm.org/doi/10.1145/3711896.3736799",
            "pdf_url": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
        },
        tmp_path,
        lambda _msg: None,
    )

    assert downloaded is True
    assert pdf_url == "https://author.example.edu/acm-paper.pdf"
    assert search_calls == ["ACM DOI Paper"]
    assert download_calls == [
        "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
        "https://author.example.edu/acm-paper.pdf",
    ]
    assert receipt["selected"]["late_fallback_after_acm_403"] is True
    assert receipt["selected"]["pdf_text_identity_check"] is True


def test_reading_failed_conference_candidate_triggers_title_search(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    search_calls: list[str] = []
    semantic_calls: list[str] = []

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", lambda _paper, **_kwargs: [{
        "kind": "indexed_pdf",
        "pdf_url": "https://broken.example.test/acl-paper.pdf",
        "accepted": True,
    }])
    monkeypatch.setattr(
        read_pipeline,
        "_search_result_pdf_candidates",
        lambda paper: search_calls.append(str(paper.get("title"))) or [{
            "kind": "search_result_pdf_requires_text_identity",
            "pdf_url": "https://aclanthology.org/2026.acl-long.1.pdf",
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }],
    )
    monkeypatch.setattr(
        read_pipeline,
        "_semantic_scholar_pdf_candidates_for_reading",
        lambda paper, **_kwargs: semantic_calls.append(str(paper.get("title"))) or [],
    )

    def fake_download(url, target):
        if "broken.example.test" in url:
            return False, {"accepted": False, "reason": "http_404"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF fake")
        return True, {"accepted": True}

    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", fake_download)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path, max_chars=None: "ACL Paper Title\nAlice Example\nAbstract\n" + "paper body " * 300)
    monkeypatch.setattr(read_pipeline, "_pdf_text_identity_ok", lambda _paper, _text: True)
    monkeypatch.setattr(read_pipeline.time, "sleep", lambda _seconds: None)

    downloaded, _pdf_path, pdf_url, receipt = read_pipeline._download_first_readable_pdf(
        {
            "title": "ACL Paper Title",
            "authors": ["Alice Example"],
            "source": "acl",
            "venue": "ACL",
            "url": "https://aclanthology.org/events/acl-2026/",
            "pdf_url": "https://broken.example.test/acl-paper.pdf",
        },
        tmp_path,
        lambda _msg: None,
    )

    assert downloaded is True
    assert pdf_url == "https://aclanthology.org/2026.acl-long.1.pdf"
    assert semantic_calls == []
    assert search_calls == ["ACL Paper Title"]
    assert receipt["selected"]["late_fallback_after_conference_candidates"] is True
    assert receipt["selected"]["pdf_text_identity_check"] is True


def test_reading_acm_search_uses_crossref_authors_and_authorizer_queries(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    seen_queries: list[str] = []
    monkeypatch.setattr(read_pipeline, "_crossref_metadata_hints", lambda _paper: {
        "accepted": True,
        "doi": "10.1145/3711896.3736799",
        "authors": ["Alice Example"],
        "container_title": "Proceedings of the 31st ACM SIGKDD Conference",
    })
    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", lambda query, limit=8: seen_queries.append(query) or [{"kind": "duckduckgo_search", "query": query, "accepted": False, "status_code": 202}])
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda query, limit=8: [])
    monkeypatch.setattr(read_pipeline, "_startpage_result_urls", lambda query, limit=8: [])

    read_pipeline._search_result_pdf_candidates({
        "title": "Causality - Exploiting Multi-Modal Data",
        "doi": "10.1145/3711896.3736799",
        "url": "https://dl.acm.org/doi/10.1145/3711896.3736799",
    })

    assert any("ACM Author-Izer" in query for query in seen_queries)
    assert any("Author-Izer" in query for query in seen_queries)
    assert any("Alice Example" in query for query in seen_queries)
    assert len(seen_queries) >= 6


def test_reading_crossref_similarity_checking_link_is_not_a_pdf_candidate(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(read_pipeline, "_request_json", lambda *_args, **_kwargs: ({
        "message": {
            "DOI": "10.1145/3711896.3736799",
            "title": ["Causality - Exploiting Multi-Modal Data"],
            "link": [
                {
                    "URL": "https://dl.acm.org/doi/pdf/10.1145/3711896.3736799",
                    "content-type": "application/pdf",
                    "content-version": "vor",
                    "intended-application": "similarity-checking",
                },
                {
                    "URL": "https://publisher.example.test/text-mining/paper.pdf",
                    "content-type": "application/pdf",
                    "content-version": "vor",
                    "intended-application": "text-mining",
                },
            ],
        },
    }, {"status_code": 200, "accepted": True}))

    candidates = read_pipeline._crossref_pdf_candidates({
        "title": "Causality - Exploiting Multi-Modal Data",
        "doi": "10.1145/3711896.3736799",
    })

    similarity_link = candidates[0]
    assert similarity_link["accepted"] is False
    assert similarity_link["reason"] == "crossref_similarity_checking_link_not_authorized_for_tdm"
    assert similarity_link["crossref_intended_application"] == "similarity-checking"
    assert candidates[1]["accepted"] is True
    assert candidates[1]["crossref_intended_application"] == "text-mining"


def test_reading_official_pdf_keeps_priority_over_runtime_cache_duplicate(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    pdf_url = "https://proceedings.neurips.cc/paper_files/paper/2025/file/example-Paper-Conference.pdf"
    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [{
        "kind": "conference_derived_official_pdf",
        "accepted": True,
        "pdf_url": pdf_url,
    }])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [{
        "kind": "reading_runtime_cached_pdf",
        "accepted": True,
        "pdf_url": pdf_url,
        "cached_pdf_path": "/tmp/example.pdf",
        "requires_pdf_text_identity_check": True,
    }])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "A NeurIPS Paper",
        "source": "nips",
        "url": "https://proceedings.neurips.cc/paper_files/paper/2025/hash/example-Abstract-Conference.html",
        "pdf_url": pdf_url,
    })

    assert candidates[0]["kind"] == "conference_derived_official_pdf"
    assert candidates[0]["pdf_url"] == pdf_url
    assert "cached_pdf" not in candidates[0]
    assert not candidates[0].get("requires_pdf_text_identity_check")


def test_reading_duckduckgo_result_urls_parses_redirect_links(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://html.duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"<html></html>"
        text = (
            '<html><body>'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fa.pdf&amp;rut=1">A</a>'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fb&amp;rut=2">B</a>'
            "</body></html>"
        )

    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: FakeResponse())

    urls = read_pipeline._duckduckgo_result_urls('"exact title"', limit=5)

    assert [item["url"] for item in urls] == ["https://example.test/a.pdf", "https://example.test/b"]
    assert all(item["accepted"] for item in urls)


def test_reading_duckduckgo_result_urls_rejects_challenge_page(monkeypatch):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://html.duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"<html></html>"
        text = "<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.</body></html>"

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: FakeResponse())

    urls = read_pipeline._duckduckgo_result_urls('"exact title"', limit=5)

    assert urls
    assert all(item["accepted"] is False for item in urls)
    assert all(item["reason"] == "duckduckgo_challenge" for item in urls)


def test_reading_duckduckgo_direct_search_defaults_to_one_attempt(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    calls: list[dict] = []

    def fake_service_get(*_args, **kwargs):
        calls.append(kwargs)
        raise read_pipeline.requests.exceptions.SSLError("synthetic ssl eof")

    monkeypatch.delenv("READING_DDG_DIRECT_ATTEMPTS", raising=False)
    monkeypatch.delenv("READING_DDG_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)

    urls = read_pipeline._duckduckgo_result_urls('"exact title"', limit=5)

    assert len(calls) == 1
    assert calls[0]["timeout"] == (3.0, 5.0)
    assert urls == [{"kind": "duckduckgo_search", "query": '"exact title"', "accepted": False, "error": "SSLError"}]


def test_reading_duckduckgo_reader_result_urls_parses_markdown_redirects(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        content = b"markdown"
        text = (
            "[Greg Anderson](http://duckduckgo.com/l/?uddg=https%3A%2F%2Fpeople.reed.edu%2F~grega%2F&rut=1)\n"
            "[Paper](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fpaper.pdf&rut=2)\n"
            "[Noise](https://duckduckgo.com/html/?q=test)"
        )

    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: FakeResponse())

    urls = read_pipeline._duckduckgo_reader_result_urls('"exact title"', limit=5)

    assert [item["url"] for item in urls] == ["https://people.reed.edu/~grega/", "https://example.test/paper.pdf"]
    assert all(item["kind"] == "duckduckgo_reader_result_url" for item in urls)


def test_reading_jina_search_uses_optional_api_key(monkeypatch):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        url = "https://s.jina.ai/exact%20title"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        content = b"markdown"
        text = "[Paper](https://authors.example.test/paper.pdf)"

    def fake_service_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)

    urls = read_pipeline._jina_search_result_urls('"exact title"', limit=5)

    assert urls[0]["url"] == "https://authors.example.test/paper.pdf"
    assert captured["url"].startswith("https://s.jina.ai/")
    assert captured["headers"]["Authorization"] == "Bearer test-jina-key"


def test_reading_jina_search_429_blocker_uses_retry_after(monkeypatch):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 429
        url = "https://s.jina.ai/exact%20title"
        headers = {"content-type": "text/plain", "retry-after": "23"}
        content = b"rate limited"
        text = "rate limited"

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: FakeResponse())

    result = read_pipeline._jina_search_result_urls('"exact title"')
    blocker = common.process_blocker("jina_search_authenticated")

    assert result[0]["status_code"] == 429
    assert blocker["reason"] == "http_429"
    assert blocker["ttl_sec"] == 23.0


def test_reading_reader_access_failure_is_not_repeated_in_process(monkeypatch):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()
    calls: list[str] = []

    class FakeResponse:
        status_code = 401
        url = "https://r.jina.ai/http://duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/plain", "retry-after": ""}
        content = b"unauthorized"
        text = "unauthorized"

    def fake_service_get(url, **_kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)

    first = read_pipeline._duckduckgo_reader_result_urls('"first title"')
    second = read_pipeline._duckduckgo_reader_result_urls('"second title"')

    assert len(calls) == 1
    assert first[0]["status_code"] == 401
    assert second[0]["reason"] == "skipped_after_prior_backend_access_failure"


def test_reading_github_repo_scan_uses_optional_token(monkeypatch):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()
    captured: dict = {}

    class FakeResponse:
        status_code = 404
        url = "https://api.github.com/repos/org/repo/contents"
        headers = {"content-type": "application/json", "retry-after": ""}
        content = b"{}"
        text = "{}"

    def fake_service_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)

    result = read_pipeline._github_repo_hints("https://github.com/org/repo")

    assert result[0]["status_code"] == 404
    assert captured["headers"]["Authorization"] == "Bearer test-github-token"
    assert captured["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


def test_reading_duckduckgo_reader_no_results_is_not_accepted(monkeypatch):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        content = b"challenge"
        text = "Markdown Content:\nUnfortunately, bots use DuckDuckGo too.\nPlease complete the following challenge."

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: FakeResponse())

    urls = read_pipeline._duckduckgo_reader_result_urls('"exact title"', limit=5)

    assert len(urls) == 1
    assert urls[0]["kind"] == "duckduckgo_reader_search"
    assert urls[0]["accepted"] is False
    assert urls[0]["reason"] == "duckduckgo_challenge"


def test_reading_startpage_result_urls_parse_direct_links(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://www.startpage.com/sp/search?query=test"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"html"
        text = """
        <a href="https://www.startpage.com/sp/search?query=test">Startpage</a>
        <a href="https://openreview.net/forum?id=eC89CbINIw">OpenReview</a>
        <a href="https://arxiv.org/pdf/2605.07397">PDF</a>
        https://www.startpage.com/do/search
        <a href="https://twitter.com/startpage">Social</a>
        """

    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: FakeResponse())

    urls = read_pipeline._startpage_result_urls('"Differentiable Lifting for Topological Neural Networks" PDF', limit=5)

    assert [item["url"] for item in urls] == [
        "https://openreview.net/forum?id=eC89CbINIw",
        "https://arxiv.org/pdf/2605.07397",
    ]
    assert all(item["kind"] == "startpage_result_url" for item in urls)


def test_reading_search_result_fallback_uses_startpage_after_duckduckgo_block(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(
        read_pipeline,
        "_duckduckgo_result_urls",
        lambda query, *, limit=8: [{"kind": "duckduckgo_search", "query": query, "accepted": False, "status_code": 202}],
    )
    monkeypatch.setattr(
        read_pipeline,
        "_duckduckgo_reader_result_urls",
        lambda query, *, limit=8: [{"kind": "duckduckgo_reader_search", "query": query, "accepted": False, "reason": "duckduckgo_challenge"}],
    )
    monkeypatch.setattr(
        read_pipeline,
        "_startpage_result_urls",
        lambda query, *, limit=8: [{"kind": "startpage_result_url", "query": query, "url": "https://example.edu/paper.pdf", "accepted": True}],
    )

    candidates = read_pipeline._search_result_pdf_candidates({
        "title": "Differentiable Lifting for Topological Neural Networks",
        "authors": ["Jorge Franco"],
    })

    assert candidates[0]["kind"] == "search_result_pdf_requires_text_identity"
    assert candidates[0]["pdf_url"] == "https://example.edu/paper.pdf"
    assert candidates[0]["requires_pdf_text_identity_check"] is True
    assert candidates[0]["source_result_url"] == "https://example.edu/paper.pdf"


def test_reading_search_result_fallback_skips_cv_pdf_candidates(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(
        read_pipeline,
        "_duckduckgo_result_urls",
        lambda query, *, limit=8: [{"kind": "duckduckgo_search", "query": query, "accepted": False, "status_code": 202}],
    )
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda query, *, limit=8: [])
    monkeypatch.setattr(
        read_pipeline,
        "_startpage_result_urls",
        lambda query, *, limit=8: [
            {"kind": "startpage_result_url", "query": query, "url": "https://example.edu/CV.pdf", "accepted": True},
            {"kind": "startpage_result_url", "query": query, "url": "https://example.edu/paper.pdf", "accepted": True},
        ],
    )

    candidates = read_pipeline._search_result_pdf_candidates({
        "title": "Differentiable Lifting for Topological Neural Networks",
        "authors": ["Jorge Franco"],
    })

    urls = [item.get("pdf_url") for item in candidates if item.get("accepted")]
    assert "https://example.edu/CV.pdf" not in urls
    assert "https://example.edu/paper.pdf" in urls
    assert read_pipeline._is_likely_cv_or_resume_pdf_url("https://example.edu/New_CV_Dun_Yuan.pdf") is True
    assert read_pipeline._is_likely_cv_or_resume_pdf_url("https://example.edu/iccv-paper.pdf") is False


def test_reading_openreview_pdf_challenge_is_reported_without_retries(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    monkeypatch.setenv("READING_OPENREVIEW_BROWSER_LOGIN_FALLBACK", "0")
    for key in ["OPENREVIEW_USERNAME", "OPENREVIEW_EMAIL", "OPENREVIEW_PASSWORD"]:
        monkeypatch.delenv(key, raising=False)

    calls: list[str] = []

    class FakeResponse:
        status_code = 200
        url = "https://openreview.net/challenge?redirect=%2Fpdf%3Fid%3DvDlkJewkDu"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"<!doctype html><html>Complete the check below to continue to OpenReview</html>"
        text = content.decode("utf-8")

    def fake_service_get(url, **_kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)
    monkeypatch.setattr(read_pipeline, "service_cooldown_remaining", lambda _service: 0.0)

    ok, receipt = read_pipeline._download_pdf_with_receipt(
        "https://openreview.net/pdf?id=vDlkJewkDu",
        tmp_path / "paper.pdf",
    )

    assert ok is False
    assert len(calls) == 1
    assert receipt["reason"] == "openreview_challenge"
    assert receipt["attempts"][0]["status_code"] == 200


def test_reading_openreview_http_auth_headers_are_redacted(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()
    monkeypatch.setenv("READING_OPENREVIEW_BROWSER_LOGIN_FALLBACK", "0")
    for key in ["OPENREVIEW_USERNAME", "OPENREVIEW_EMAIL", "OPENREVIEW_PASSWORD"]:
        monkeypatch.delenv(key, raising=False)

    for key in [
        "READING_OPENREVIEW_COOKIE",
        "OPENREVIEW_COOKIE",
        "READING_OPENREVIEW_AUTHORIZATION",
        "OPENREVIEW_AUTHORIZATION",
    ]:
        monkeypatch.delenv(key, raising=False)
    cookie_value = "pytest_openreview_cookie_value"
    authorization_value = "pytest_openreview_authorization_value"
    monkeypatch.setenv("READING_OPENREVIEW_COOKIE", cookie_value)
    monkeypatch.setenv("READING_OPENREVIEW_AUTHORIZATION", authorization_value)

    seen_headers: list[dict[str, str]] = []

    class FakeResponse:
        status_code = 403
        url = "https://api2.openreview.net/pdf?id=vDlkJewkDu"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"forbidden"
        text = "forbidden"

    def fake_service_get(_url, **kwargs):
        seen_headers.append(dict(kwargs.get("headers") or {}))
        return FakeResponse()

    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)
    monkeypatch.setattr(read_pipeline, "service_cooldown_remaining", lambda _service: 0.0)

    ok, receipt = read_pipeline._download_pdf_with_receipt(
        "https://api2.openreview.net/pdf?id=vDlkJewkDu",
        tmp_path / "paper.pdf",
    )

    assert ok is False
    assert seen_headers == [{
        "Accept": "application/pdf,*/*",
        "Cookie": cookie_value,
        "Authorization": authorization_value,
    }]
    auth_receipt = receipt["openreview_http_auth"]
    assert auth_receipt["configured"] is True
    assert auth_receipt["cookie_configured"] is True
    assert auth_receipt["authorization_configured"] is True
    assert auth_receipt["source_env"] == ["READING_OPENREVIEW_COOKIE", "READING_OPENREVIEW_AUTHORIZATION"]
    assert auth_receipt["redacted"] is True
    receipt_text = json.dumps(receipt, ensure_ascii=False)
    assert cookie_value not in receipt_text
    assert authorization_value not in receipt_text


def test_reading_openreview_http_auth_not_applied_to_other_hosts(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setenv("READING_OPENREVIEW_COOKIE", "pytest_openreview_cookie_value")
    monkeypatch.setenv("READING_OPENREVIEW_AUTHORIZATION", "pytest_openreview_authorization_value")
    seen_headers: list[dict[str, str]] = []

    class FakeResponse:
        status_code = 403
        url = "https://example.test/paper.pdf?next=openreview.net"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"forbidden"
        text = "forbidden"

    def fake_service_get(_url, **kwargs):
        seen_headers.append(dict(kwargs.get("headers") or {}))
        return FakeResponse()

    monkeypatch.setattr(read_pipeline, "service_get", fake_service_get)

    ok, receipt = read_pipeline._download_pdf_with_receipt(
        "https://example.test/paper.pdf?next=openreview.net",
        tmp_path / "paper.pdf",
    )

    assert ok is False
    assert seen_headers == [{"Accept": "application/pdf,*/*"}]
    assert "openreview_http_auth" not in receipt


def test_reading_openreview_official_reports_package_and_credentials(monkeypatch, tmp_path):
    openreview_official = _load_reading_openreview_official()

    monkeypatch.delenv("OPENREVIEW_USERNAME", raising=False)
    monkeypatch.delenv("OPENREVIEW_EMAIL", raising=False)
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)
    monkeypatch.setattr(openreview_official.importlib.util, "find_spec", lambda name: None if name == "openreview" else importlib.util.find_spec(name))

    ok, receipt = openreview_official.download_openreview_official_pdf(
        {"openreview_note_id": "vDlkJewkDu"},
        tmp_path / "paper.pdf",
    )

    assert ok is False
    assert receipt["reason"] == "missing_openreview_py"
    first_attempt = receipt["attempts"][0]
    assert first_attempt["package_status"]["reason"] == "missing_openreview_py"
    assert first_attempt["credential_status"]["reason"] == "missing_openreview_credentials"
    assert first_attempt["missing_reasons"] == ["missing_openreview_py", "missing_openreview_credentials"]


def test_reading_openreview_official_allows_anonymous_client(monkeypatch, tmp_path):
    openreview_official = _load_reading_openreview_official()
    http_client = _load_reading_common()
    monkeypatch.setattr(http_client, "_SERVICE_STATE_ROOT", tmp_path)
    monkeypatch.setitem(http_client.SERVICE_MIN_INTERVAL_SEC, "openreview", 0.0)

    constructed: list[dict] = []

    class FakeOpenReviewClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    class FakeOpenReview:
        class api:
            OpenReviewClient = FakeOpenReviewClient

        Client = FakeOpenReviewClient

    monkeypatch.delenv("OPENREVIEW_USERNAME", raising=False)
    monkeypatch.delenv("OPENREVIEW_EMAIL", raising=False)
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)
    monkeypatch.delenv("READING_OPENREVIEW_ALLOW_ANONYMOUS_OFFICIAL_CLIENT", raising=False)
    monkeypatch.setitem(sys.modules, "openreview", FakeOpenReview)
    monkeypatch.setattr(openreview_official.importlib.util, "find_spec", lambda name: object() if name == "openreview" else importlib.util.find_spec(name))

    client, receipt = openreview_official._client(2)

    assert isinstance(client, FakeOpenReviewClient)
    assert receipt["accepted"] is True
    assert receipt["auth_mode"] == "anonymous"
    assert receipt["credential_status"]["reason"] == "missing_openreview_credentials"
    assert constructed == [{"baseurl": "https://api2.openreview.net", "token": "e30.e30."}]


def test_reading_openreview_official_reuses_authenticated_client(monkeypatch, tmp_path):
    openreview_official = _load_reading_openreview_official()
    http_client = _load_reading_common()
    monkeypatch.setattr(http_client, "_SERVICE_STATE_ROOT", tmp_path)
    monkeypatch.setitem(http_client.SERVICE_MIN_INTERVAL_SEC, "openreview", 0.0)

    constructed: list[dict] = []
    login_calls: list[tuple[str, str]] = []

    class FakeOpenReviewClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.headers = {}
            self.token = None

        def login_user(self, username, password):
            login_calls.append((username, password))

    class FakeOpenReview:
        class api:
            OpenReviewClient = FakeOpenReviewClient

        Client = FakeOpenReviewClient

    monkeypatch.setenv("OPENREVIEW_USERNAME", "account@example.test")
    monkeypatch.setenv("OPENREVIEW_PASSWORD", "secret")
    monkeypatch.setitem(sys.modules, "openreview", FakeOpenReview)
    monkeypatch.setattr(openreview_official.importlib.util, "find_spec", lambda name: object() if name == "openreview" else importlib.util.find_spec(name))
    openreview_official._CLIENT_CACHE.clear()

    first, first_receipt = openreview_official._client(2)
    second, second_receipt = openreview_official._client(2)

    assert first is second
    assert constructed == [{"baseurl": "https://api2.openreview.net", "token": "e30.e30."}]
    assert login_calls == [("account@example.test", "secret")]
    assert first_receipt["client_reused"] is False
    assert second_receipt["client_reused"] is True
    openreview_official._CLIENT_CACHE.clear()


def test_reading_openreview_official_attachment_uses_client_signature(monkeypatch, tmp_path):
    openreview_official = _load_reading_openreview_official()
    attachment_calls: list[tuple[str, str]] = []

    class FakeClient:
        def get_pdf(self, _note_id):
            return b"missing"

        def get_attachment(self, field_name, *, id):
            attachment_calls.append((field_name, id))
            return b"%PDF-1.7\narticle"

    monkeypatch.setattr(openreview_official, "_client", lambda _api_version: (FakeClient(), {"accepted": True}))
    monkeypatch.setattr(openreview_official, "_guarded_openreview_call", lambda call: call())
    target = tmp_path / "paper.pdf"

    ok, receipt = openreview_official.download_openreview_official_pdf(
        {"openreview_note_id": "note-id"},
        target,
    )

    assert ok is True
    assert attachment_calls == [("pdf", "note-id")]
    assert target.read_bytes().startswith(b"%PDF")
    assert receipt["selected"]["kind"] == "get_attachment"


def test_reading_pdf_candidate_failure_sleep_is_bounded(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.delenv("READING_PDF_FAILURE_SLEEP_SEC", raising=False)
    monkeypatch.setattr(
        read_pipeline,
        "_pdf_candidates_for_reading",
        lambda _paper, **_kwargs: [
            {"kind": "direct", "pdf_url": "https://openreview.net/pdf?id=one", "accepted": True},
            {"kind": "direct", "pdf_url": "https://openreview.net/pdf?id=two", "accepted": True},
        ],
    )
    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", lambda _url, _path: (False, {"reason": "http_403"}))
    sleeps: list[float] = []
    monkeypatch.setattr(read_pipeline.time, "sleep", lambda value: sleeps.append(value))

    downloaded, _path, _url, acquisition = read_pipeline._download_first_readable_pdf(
        {"paper_id": "p1", "title": "Synthetic OpenReview Paper"},
        tmp_path,
        lambda _message: None,
    )

    assert downloaded is False
    assert sleeps == [0.3, 0.3]
    assert acquisition["candidate_count"] == 2


def test_reading_does_not_fetch_robots_disallowed_iclr_static_data(monkeypatch):
    reading_root = ROOT / "modules" / "reading"
    http_client = _load_reading_common()
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            reading_root / "scripts" / "pipeline" / "read_pipeline.py",
            reading_root / "scripts" / "acquisition" / "conference_sources.py",
        ]
    )

    assert "iclr.cc/static/" not in source
    monkeypatch.setattr(http_client.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("robots-disallowed URL reached the network")))
    try:
        http_client.service_get("https://iclr.cc/static/virtual/data/iclr-2026-orals-posters.json")
    except http_client.RobotsPolicyBlocked:
        pass
    else:
        raise AssertionError("ICLR robots-disallowed /static path was accepted")


def test_reading_search_result_fallback_requires_text_identity(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    def fake_duckduckgo_results(query: str, *, limit: int = 8):
        return [
            {"kind": "duckduckgo_result_url", "query": query, "url": "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf", "accepted": True},
            {"kind": "duckduckgo_result_url", "query": query, "url": "https://mlanthology.org/iclr/2026/gheda2026iclr-checkmate/", "accepted": True},
        ]

    def fake_page_candidates(paper, url, *, kind, scan_assets=False, allow_pdf_text_identity_check=False):
        if "mlanthology.org" not in url:
            return []
        return [{
            "kind": kind + "_pdf_link",
            "pdf_url": "https://openreview.net/pdf?id=92fliNrbxY",
            "landing_page_url": url,
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", fake_duckduckgo_results)
    monkeypatch.setattr(read_pipeline, "_publisher_page_candidates_from_url", fake_page_candidates)
    monkeypatch.setattr(read_pipeline, "_github_repo_hints", lambda url, limit=6: [])

    candidates = read_pipeline._search_result_pdf_candidates(
        {
            "title": "Robust Adaptive Multi-Step Predictive Shielding",
            "authors": ["Tanmay Sadanand Ambadkar", "Darshan Chudiwal", "Greg Anderson", "Abhinav Verma"],
            "venue": "ICLR",
        }
    )

    assert any(
        item.get("pdf_url") == "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf"
        and item.get("requires_pdf_text_identity_check") is True
        for item in candidates
    )
    assert any(item.get("pdf_url") == "https://openreview.net/pdf?id=92fliNrbxY" for item in candidates)


def test_reading_search_result_rejects_cross_openreview_note_id(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    def fake_duckduckgo_results(query: str, *, limit: int = 8):
        return [
            {
                "kind": "duckduckgo_result_url",
                "query": query,
                "url": "https://openreview.net/attachment?id=qL6sgVeOaI&name=pdf",
                "accepted": True,
            },
            {
                "kind": "duckduckgo_result_url",
                "query": query,
                "url": "https://example.test/difflift-index.html",
                "accepted": True,
            },
        ]

    def fake_page_candidates(_paper, url, *, kind, scan_assets=False, allow_pdf_text_identity_check=False):
        if "example.test" not in url:
            return []
        return [{
            "kind": kind + "_pdf_link",
            "pdf_url": "https://openreview.net/pdf?id=qL6sgVeOaI",
            "landing_page_url": url,
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", fake_duckduckgo_results)
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda query, limit=8: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_candidates_from_url", fake_page_candidates)

    candidates = read_pipeline._search_result_pdf_candidates(
        {
            "title": "Differentiable Lifting for Topological Neural Networks",
            "authors": ["Jorge Luiz Franco", "Gabriel Duarte", "Alexander Nikitin", "Moacir Antonelli Ponti", "Diego P. P. Mesquita", "Amauri H. Souza"],
            "source": "iclr",
            "metadata": {"conference_channel": "iclr", "openreview_id": "eC89CbINIw"},
        }
    )

    mismatches = [
        item for item in candidates
        if item.get("reason") == "openreview_cross_submission_note_id_mismatch"
    ]
    assert len(mismatches) >= 2
    assert {item.get("candidate_openreview_id") for item in mismatches} == {"qL6sgVeOaI"}
    assert all(item.get("expected_openreview_ids") == ["eC89CbINIw"] for item in mismatches)
    assert any(item.get("kind") == "duckduckgo_result_url" for item in mismatches)
    assert any(item.get("kind") == "search_result_page_pdf_link" for item in mismatches)
    assert not any(item.get("accepted") and "qL6sgVeOaI" in str(item.get("pdf_url") or item.get("url") or "") for item in candidates)


def test_reading_chatpaper_openreview_cache_requires_exact_note(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    def fake_request_html(url, *, timeout=30):
        if "search?keywords=" in url:
            return url, '<div class="document" data-doc="244460"></div><a href="/paper/111111?from=subpath-search">x</a>', {"accepted": True, "url": url}
        if url.endswith("/paper/244460"):
            return url, '<a href="https://openreview.net/forum?id=vDlkJewkDu">OpenReview</a>', {"accepted": True, "url": url}
        if url.endswith("/paper/111111"):
            return url, '<a href="https://openreview.net/forum?id=qL6sgVeOaI">OpenReview</a>', {"accepted": True, "url": url}
        return url, "", {"accepted": False, "url": url}

    monkeypatch.setattr(read_pipeline, "_request_html", fake_request_html)
    candidates = read_pipeline._chatpaper_openreview_cached_pdf_candidates({
        "title": "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning",
        "authors": ["Dun Yuan", "Di Wu", "Xue Liu"],
        "metadata": {"openreview_id": "vDlkJewkDu"},
    })

    accepted = [item for item in candidates if item.get("accepted")]
    assert len(accepted) == 1
    assert accepted[0]["pdf_url"] == "https://chatpaper.com/api/v1/articles/download/244460"
    assert accepted[0]["source_openreview_id"] == "vDlkJewkDu"
    assert accepted[0]["requires_pdf_text_identity_check"] is True

    mismatch = read_pipeline._chatpaper_openreview_cached_pdf_candidates({
        "title": "Differentiable Lifting for Topological Neural Networks",
        "authors": ["Jorge Franco"],
        "metadata": {"openreview_id": "eC89CbINIw"},
    })
    assert not any(item.get("accepted") for item in mismatch)
    assert any(item.get("reason") == "chatpaper_openreview_note_id_mismatch" for item in mismatch)


def test_reading_search_result_fallback_uses_reader_results(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    def fake_duckduckgo_results(query: str, *, limit: int = 8):
        return [{"kind": "duckduckgo_search", "query": query, "accepted": False, "status_code": 202}]

    def fake_reader_results(query: str, *, limit: int = 8):
        return [{"kind": "duckduckgo_reader_result_url", "query": query, "url": "https://people.reed.edu/~grega/", "accepted": True}]

    def fake_page_candidates(paper, url, *, kind, scan_assets=False, allow_pdf_text_identity_check=False):
        assert url == "https://people.reed.edu/~grega/"
        return [{
            "kind": kind + "_pdf_link_requires_text_identity",
            "pdf_url": "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf",
            "landing_page_url": url,
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", fake_duckduckgo_results)
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", fake_reader_results)
    monkeypatch.setattr(read_pipeline, "_publisher_page_candidates_from_url", fake_page_candidates)

    candidates = read_pipeline._search_result_pdf_candidates(
        {
            "title": "Robust Adaptive Multi-Step Predictive Shielding",
            "authors": ["Tanmay Sadanand Ambadkar", "Greg Anderson"],
            "venue": "ICLR",
        }
    )

    assert any(
        item.get("pdf_url") == "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf"
        and item.get("source_result_url") == "https://people.reed.edu/~grega/"
        and item.get("requires_pdf_text_identity_check") is True
        for item in candidates
    )


def test_reading_search_result_fallback_skips_infrastructure_pdfs(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(
        read_pipeline,
        "_duckduckgo_result_urls",
        lambda query, *, limit=8: [{
            "kind": "duckduckgo_result_url",
            "query": query,
            "url": "https://www.doi.org/resources/130718-trademark-policy.pdf",
            "accepted": True,
        }],
    )
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda query, limit=8: [])
    monkeypatch.setattr(read_pipeline, "_startpage_result_urls", lambda query, limit=8: [])

    candidates = read_pipeline._search_result_pdf_candidates({
        "title": "Continuous Evaluation in Information Retrieval Across Methods and Time",
        "authors": [],
    })

    assert not any(item.get("accepted") and item.get("pdf_url") for item in candidates)
    assert any(item.get("reason") == "likely_infrastructure_pdf_not_article_body" for item in candidates)


def test_reading_search_result_fallback_limits_author_queries(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    queries: list[str] = []

    def fake_duckduckgo_results(query: str, *, limit: int = 8):
        queries.append(query)
        return [{"kind": "duckduckgo_search", "query": query, "accepted": False, "reason": "synthetic_no_results"}]

    monkeypatch.delenv("READING_SEARCH_QUERY_LIMIT", raising=False)
    monkeypatch.setattr(read_pipeline, "_duckduckgo_result_urls", fake_duckduckgo_results)
    monkeypatch.setattr(read_pipeline, "_duckduckgo_reader_result_urls", lambda query, limit=8: [])
    monkeypatch.setattr(read_pipeline, "_startpage_result_urls", lambda query, limit=8: [])

    candidates = read_pipeline._search_result_pdf_candidates(
        {
            "title": "Differentiable Lifting for Topological Neural Networks",
            "authors": ["Jorge Franco", "Gabriel Duarte", "Alexander Nikitin", "Moacir Ponti", "Diego Mesquita", "Amauri Souza"],
            "venue": "ICLR",
        }
    )

    assert queries == [
        'site:proceedings.iclr.cc "Differentiable Lifting for Topological Neural Networks"',
        'site:iclr.cc "Differentiable Lifting for Topological Neural Networks"',
        '"Differentiable Lifting for Topological Neural Networks" PDF',
        '"Differentiable Lifting for Topological Neural Networks"',
        '"Differentiable Lifting for Topological Neural Networks" "Jorge Franco"',
    ]
    assert any(item.get("kind") == "search_query_budget" and item.get("used_query_count") == 5 for item in candidates)


def test_reading_iclr_source_without_venue_uses_search_fallback(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    search_calls: list[str] = []

    def fake_search_candidates(paper):
        search_calls.append(str(paper.get("title")))
        return [{
            "kind": "search_result_page_pdf_link_requires_text_identity",
            "pdf_url": "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf",
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "openreview_official_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "semantic_scholar_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", fake_search_candidates)
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "title": "Robust Adaptive Multi-Step Predictive Shielding",
        "source": "iclr",
        "url": "https://iclr.cc/virtual/2026/poster/10011722",
        "metadata": {"conference_channel": "iclr"},
    })

    assert search_calls == ["Robust Adaptive Multi-Step Predictive Shielding"]
    assert any(item["kind"] == "search_result_page_pdf_link_requires_text_identity" for item in candidates)


def test_reading_iclr_uses_semantic_scholar_late_fallback(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    enabled_values: list[object] = []

    def fake_semantic_candidates(_paper, **kwargs):
        enabled_values.append(kwargs.get("enabled"))
        return [{
            "kind": "semantic_scholar_open_access_pdf",
            "accepted": False,
            "reason": "http_429_rate_limited",
            "status_code": 429,
        }]

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "openreview_official_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "semantic_scholar_pdf_candidates", fake_semantic_candidates)
    monkeypatch.setattr(read_pipeline, "_publisher_direct_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openreview_title_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    paper = {
        "title": "Differentiable Lifting for Topological Neural Networks",
        "source": "iclr",
        "url": "https://iclr.cc/virtual/2026/poster/10008342",
        "metadata": {"conference_channel": "iclr"},
    }
    read_pipeline._pdf_candidates_for_reading(paper)

    discovery = paper.get("_same_paper_pdf_candidate_discovery")
    assert enabled_values == [None]
    assert any(
        item.get("kind") == "semantic_scholar_open_access_pdf"
        and item.get("reason") == "http_429_rate_limited"
        for item in discovery
    )


def test_reading_arxiv_parser_rejects_conference_poster_urls():
    paper_sources = _load_reading_paper_sources()
    semantic_scholar = _load_reading_semantic_scholar()

    assert paper_sources.arxiv_id_from_text("https://iclr.cc/virtual/2026/poster/10008342") == ""
    assert semantic_scholar._arxiv_from_text("https://iclr.cc/virtual/2026/poster/10008342") == ""
    assert paper_sources.arxiv_id_from_text("https://arxiv.org/abs/2506.10085v5") == "2506.10085"
    assert semantic_scholar._arxiv_from_text("arXiv:2506.10085v5") == "2506.10085"


def test_reading_biorxiv_direct_pdf_supports_new_doi_prefix():
    read_pipeline = _load_reading_pipeline()

    candidates = read_pipeline._publisher_direct_pdf_candidates({
        "source": "biorxiv",
        "title": "bioRxiv current DOI example",
        "doi": "10.64898/2026.05.31.727600",
        "url": "https://www.biorxiv.org/content/10.64898/2026.05.31.727600v1",
    })

    assert candidates == [{
        "kind": "doi_direct_biorxiv_full_pdf",
        "pdf_url": "https://www.biorxiv.org/content/10.64898/2026.05.31.727600.full.pdf",
        "doi": "10.64898/2026.05.31.727600",
        "accepted": True,
    }]
    assert "https://www.biorxiv.org/content/10.64898/2026.05.31.727600" in read_pipeline._same_paper_landing_urls({
        "doi": "10.64898/2026.05.31.727600",
    })


def test_reading_biorxiv_doi_defers_landing_scan_until_after_official_pdf(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    page_calls: list[str] = []

    def fail_page_scan(_paper):
        page_calls.append("publisher_page")
        raise AssertionError("bioRxiv landing page scan must wait until the official full PDF has been attempted")

    monkeypatch.setattr(read_pipeline, "official_conference_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_pdf_links_from_html_page", lambda _url: [])
    monkeypatch.setattr(read_pipeline, "_springer_nature_api_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_crossref_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", fail_page_scan)
    monkeypatch.setattr(read_pipeline, "_iclr_mlanthology_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_openalex_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_unpaywall_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_arxiv_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", lambda _paper: [])
    monkeypatch.setattr(read_pipeline, "_runtime_cached_pdf_candidates", lambda _paper: [])

    candidates = read_pipeline._pdf_candidates_for_reading({
        "source": "biorxiv",
        "title": "bioRxiv current DOI example",
        "doi": "10.64898/2026.05.31.727600",
        "url": "https://www.biorxiv.org/content/10.64898/2026.05.31.727600v1",
        "pdf_url": "https://www.biorxiv.org/content/10.64898/2026.05.31.727600v1",
    })

    discovery = candidates and candidates[0]
    assert page_calls == []
    assert discovery["kind"] == "doi_direct_biorxiv_full_pdf"
    assert discovery["pdf_url"] == "https://www.biorxiv.org/content/10.64898/2026.05.31.727600.full.pdf"


def test_reading_biorxiv_challenge_late_searches_external_same_paper_pdf(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    search_calls: list[str] = []
    page_calls: list[str] = []
    download_calls: list[str] = []

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", lambda _paper, **_kwargs: [{
        "kind": "doi_direct_biorxiv_full_pdf",
        "pdf_url": "https://www.biorxiv.org/content/10.64898/2026.05.27.727191.full.pdf",
        "accepted": True,
    }])

    def fail_page_scan(_paper):
        page_calls.append("publisher_page")
        raise AssertionError("bioRxiv challenge fallback must not scan bioRxiv landing pages")

    def fake_search(paper):
        search_calls.append(str(paper.get("title")))
        return [{
            "kind": "search_result_pdf_requires_text_identity",
            "pdf_url": "https://institution.example.edu/biorxiv-preprint.pdf",
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    def fake_download(url, target):
        download_calls.append(url)
        if "biorxiv.org" in url:
            return False, {
                "accepted": False,
                "reason": "http_403",
                "selected": {
                    "service": "biorxiv",
                    "status_code": 403,
                    "headers_subset": {"cf-mitigated": "challenge"},
                    "challenge_type": "cloudflare",
                },
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF fake")
        return True, {"accepted": True}

    monkeypatch.setattr(read_pipeline, "_publisher_page_pdf_candidates", fail_page_scan)
    monkeypatch.setattr(read_pipeline, "_search_result_pdf_candidates", fake_search)
    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", fake_download)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", lambda _path, max_chars=None: "Scouting ecological drivers of natural enemies in citrus orchards\nCarrie\n" + "paper body " * 300)
    monkeypatch.setattr(read_pipeline, "_pdf_text_identity_ok", lambda _paper, _text: True)

    downloaded, _pdf_path, pdf_url, receipt = read_pipeline._download_first_readable_pdf(
        {
            "source": "biorxiv",
            "title": "Scouting ecological drivers of natural enemies in citrus orchards",
            "authors": ["E. Carrie"],
            "doi": "10.64898/2026.05.27.727191",
            "url": "https://www.biorxiv.org/content/10.64898/2026.05.27.727191v2",
        },
        tmp_path,
        lambda _msg: None,
    )

    assert downloaded is True
    assert pdf_url == "https://institution.example.edu/biorxiv-preprint.pdf"
    assert page_calls == []
    assert search_calls == ["Scouting ecological drivers of natural enemies in citrus orchards"]
    assert download_calls == [
        "https://www.biorxiv.org/content/10.64898/2026.05.27.727191.full.pdf",
        "https://institution.example.edu/biorxiv-preprint.pdf",
    ]
    assert receipt["selected"]["late_fallback_after_biorxiv_official_blocker"] is True
    assert receipt["selected"]["pdf_text_identity_check"] is True


def test_reading_biorxiv_api_jatsxml_can_supply_official_xml_text(monkeypatch):
    paper_sources = _load_reading_paper_sources()

    calls: list[str] = []

    class FakeResponse:
        def __init__(self, url: str, status_code: int, text: str, content_type: str, payload: dict | None = None):
            self.url = url
            self.status_code = status_code
            self._text = text
            self.content = text.encode("utf-8")
            self.headers = {"content-type": content_type}
            self._payload = payload

        @property
        def text(self):
            return self._text

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    body = " ".join(
        [
            "Scouting ecological drivers of natural enemies in citrus orchards",
            "Carrie",
            "Abstract Introduction Methods Results Discussion References",
            *(f"official xml body sentence {index}" for index in range(900)),
        ]
    )
    xml = f"<article><body><sec><p>{body}</p></sec></body></article>"

    def fake_service_get(url, **kwargs):
        calls.append(url)
        if "api.biorxiv.org/details/biorxiv/" in url:
            return FakeResponse(
                url,
                200,
                "{}",
                "application/json",
                {
                    "collection": [
                        {
                            "doi": "10.64898/2026.05.27.727191",
                            "jatsxml": "https://www.biorxiv.org/content/early/2026/06/01//2026.05.27.727191.source.xml",
                        }
                    ]
                },
            )
        if url.endswith(".source.xml"):
            return FakeResponse(url, 200, xml, "application/xml")
        raise AssertionError(url)

    remaining_values = [3.0, 0.0, 0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(paper_sources, "service_cooldown_remaining", lambda _service: remaining_values.pop(0) if remaining_values else 0.0)
    monkeypatch.setattr(paper_sources.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(paper_sources, "service_get", fake_service_get)
    monkeypatch.setattr(paper_sources, "_read_pipeline_func", lambda _name: (lambda _paper, _text: True))

    text, receipt = paper_sources._fetch_biorxiv_jats_xml_text({
        "source": "biorxiv",
        "title": "Scouting ecological drivers of natural enemies in citrus orchards",
        "authors": ["E. Carrie"],
        "doi": "10.64898/2026.05.27.727191",
    })

    assert receipt["accepted"] is True
    assert receipt["source"] == "biorxiv_api_jatsxml"
    assert receipt["pdf_text_identity_check"] is True
    assert sleeps == [3.0]
    assert receipt["cooldown_waits"][0]["stage"] == "before_api_details"
    assert len(text) >= paper_sources.MIN_FULL_TEXT_CHARS
    assert calls == [
        "https://api.biorxiv.org/details/biorxiv/10.64898/2026.05.27.727191",
        "https://www.biorxiv.org/content/early/2026/06/01/2026.05.27.727191.source.xml",
    ]


def test_reading_biorxiv_cloudflare_blocker_is_explicit():
    paper_sources = _load_reading_paper_sources()

    reason = paper_sources._blocked_full_text_reason(
        {
            "source": "biorxiv",
            "title": "bioRxiv challenge example",
            "doi": "10.64898/2026.05.29.728749",
            "url": "https://www.biorxiv.org/content/10.64898/2026.05.29.728749v1",
        },
        {
            "attempts": [
                {
                    "kind": "doi_direct_biorxiv_full_pdf",
                    "service": "biorxiv",
                    "url": "https://www.biorxiv.org/content/10.64898/2026.05.29.728749.full.pdf",
                    "status_code": 403,
                    "headers_subset": {"cf-mitigated": "challenge"},
                    "download_failure_reason": "http_403",
                }
            ],
            "selected": {},
        },
        {},
        {},
    )

    assert reason["code"] == "blocked_biorxiv_official_challenge_no_verified_open_full_text"
    assert "Cloudflare" in reason["message_zh"]


def test_reading_science_cloudflare_blocker_is_explicit():
    paper_sources = _load_reading_paper_sources()

    reason = paper_sources._blocked_full_text_reason(
        {
            "source": "science",
            "title": "Science challenge example",
            "doi": "10.1126/science.adz3624",
            "url": "https://www.science.org/doi/10.1126/science.adz3624",
        },
        {
            "attempts": [
                {
                    "kind": "doi_direct_science_pdf",
                    "service": "science",
                    "url": "https://www.science.org/doi/pdf/10.1126/science.adz3624",
                    "status_code": 403,
                    "headers_subset": {"cf-mitigated": "challenge"},
                    "download_failure_reason": "http_403",
                }
            ],
            "selected": {},
        },
        {},
        {},
    )

    assert reason["code"] == "blocked_science_official_challenge_no_open_xml_or_pdf"
    assert "补充材料" in reason["next_research_action"]


def test_reading_science_official_html_is_tried_before_pdf(monkeypatch):
    reading_root = ROOT / "modules" / "reading"
    paper_sources = _load_reading_paper_sources()

    body = "Introduction\nMethods\nResults\nDiscussion\n" + ("science official article body " * 600)
    seen_urls: list[str] = []

    def fake_fetch_html(url, timeout=30):
        seen_urls.append(url)
        return body, {
            "accepted": True,
            "url": url,
            "service": "science",
            "status_code": 200,
            "content_type": "text/html",
            "text_chars": len(body),
            "paper_body_markers": True,
        }

    def fail_download(*_args, **_kwargs):
        raise AssertionError("Science PDF should remain fallback when official HTML is already accepted")

    monkeypatch.setattr(paper_sources, "_fetch_html_text", fake_fetch_html)
    monkeypatch.setattr(paper_sources, "_download_first_readable_pdf", fail_download)

    run_dir = reading_root / ".runtime" / "output" / _reading_test_run_id()
    shutil.rmtree(run_dir, ignore_errors=True)
    packet = paper_sources.acquire_full_text(
        {
            "source": "science",
            "paper_id": "10.1126/science.example",
            "doi": "10.1126/science.example",
            "title": "Science official HTML example",
            "url": "https://www.science.org/doi/abs/10.1126/science.example",
            "pdf_url": "https://www.science.org/doi/pdf/10.1126/science.example",
        },
        run_dir,
        log=lambda _message: None,
    )

    assert seen_urls[0] == "https://www.science.org/doi/full/10.1126/science.example"
    assert packet["full_text_available"] is True
    assert packet["full_text_status"] == "html_text_read"
    assert packet["full_text_evidence_kind"] == "html"
    assert packet["pdf_downloaded"] is False
    assert packet["pdf_acquisition"]["skipped"] == "science_official_html_ready_before_pdf"
    assert packet["html_acquisition"]["selected"]["kind"] == "science_official_html_before_pdf"
    assert (reading_root / packet["text_path"]).exists()
    shutil.rmtree(run_dir, ignore_errors=True)


def test_reading_cloudflare_challenge_services_are_detected():
    paper_sources = _load_reading_paper_sources()

    services = paper_sources._cloudflare_challenged_services({
        "attempts": [
            {
                "url": "https://www.biorxiv.org/content/10.64898/example.full.pdf",
                "headers_subset": {"cf-mitigated": "challenge"},
            },
            {
                "service": "science",
                "challenge_type": "cloudflare",
                "url": "https://www.science.org/doi/pdf/10.1126/science.example",
            },
        ]
    })

    assert services == {"biorxiv", "science"}


def test_reading_cooldown_skip_is_deferred_not_reported_as_unreadable_pdf():
    paper_sources = _load_reading_paper_sources()

    reason = paper_sources._blocked_full_text_reason(
        {
            "source": "biorxiv",
            "title": "bioRxiv cooldown example",
            "doi": "10.64898/2026.05.27.727191",
            "url": "https://www.biorxiv.org/content/10.64898/2026.05.27.727191v2",
        },
        {
            "attempts": [
                {
                    "kind": "doi_direct_biorxiv_full_pdf",
                    "service": "biorxiv",
                    "url": "https://www.biorxiv.org/content/10.64898/2026.05.27.727191.full.pdf",
                    "download_failure_reason": "skipped_due_to_active_challenge_cooldown",
                    "cooldown_remaining_sec": 30.0,
                }
            ],
            "selected": {},
        },
        {},
        {},
    )

    assert reason["code"] == "deferred_service_cooldown_before_full_text_request"
    assert reason["retryable_after_cooldown"] is True
    assert reason["cooldown_services"] == ["biorxiv"]
    assert reason["pdf_request_count"] == 0
    assert "不是 PDF 不可读" in reason["message_zh"]


def test_reading_active_challenge_cooldown_skips_pdf_request(monkeypatch, tmp_path):
    common = _load_reading_common()
    read_pipeline = _load_reading_pipeline()

    monkeypatch.setattr(common, "_SERVICE_STATE_ROOT", tmp_path / "http_locks")
    monkeypatch.setitem(common.SERVICE_MIN_INTERVAL_SEC, "biorxiv", 0.0)
    with common.service_request_slot("biorxiv") as gate:
        gate.update({"cooldown_sec": 30.0, "cooldown_reason": "test_challenge"})
    monkeypatch.setattr(read_pipeline, "service_get", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network request should be skipped")))
    downloaded, receipt = read_pipeline._download_pdf_with_receipt(
        "https://www.biorxiv.org/content/10.64898/2026.05.31.727600.full.pdf",
        tmp_path / "paper.pdf",
    )

    assert not downloaded
    assert receipt["reason"] == "skipped_due_to_active_challenge_cooldown"
    assert receipt["service"] == "biorxiv"


def test_reading_reader_pdf_text_url_uses_canonical_jina_form():
    paper_sources = _load_reading_paper_sources()

    reader_url = paper_sources._reader_pdf_text_url("https://iris.unito.it/retrieve/file name (1).pdf")

    assert reader_url.startswith("https://r.jina.ai/http://iris.unito.it/")
    assert "http://https://" not in reader_url
    assert "file%20name%20%281%29.pdf" in reader_url


def test_reading_meta_parser_accepts_unquoted_citation_tags():
    read_pipeline = _load_reading_pipeline()

    html = (
        '<meta name=citation_title content="CheckMate! Watermarking Graph Diffusion Models in Polynomial Time">'
        '<meta name=citation_pdf_url content=https://openreview.net/pdf/hash.pdf>'
    )

    assert read_pipeline._meta_content_values(html, {"citation_title"}) == [
        "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time"
    ]
    assert read_pipeline._meta_content_values(html, {"citation_pdf_url"}) == [
        "https://openreview.net/pdf/hash.pdf"
    ]


def test_reading_pdf_text_identity_accepts_iclr_header_and_wrapped_title():
    read_pipeline = _load_reading_pipeline()

    paper = {
        "title": "Robust Adaptive Multi-Step Predictive Shielding",
        "authors": ["Tanmay Sadanand Ambadkar", "Darshan Chudiwal", "Greg Anderson", "Abhinav Verma"],
    }
    text = (
        "Published as a conference paper at ICLR 2026\n"
        "ROBUST ADAPTIVE MULTI-STEP PREDICTIVE SHIELD-\n"
        "ING\n"
        "Tanmay Ambadkar\n"
        "Darshan Chudiwal\n"
        "Greg Anderson\n"
        "Abhinav Verma\n"
        "ABSTRACT\n"
        "full text " * 300
    )

    assert read_pipeline._pdf_text_identity_ok(paper, text) is True

    cappo_paper = {
        "title": "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning",
        "authors": ["Dun Yuan", "Di Wu", "Xue Liu"],
    }
    cappo_text = (
        "Published as a conference paper at ICLR 2026\n"
        "ESCAPING POLICY CONTRACTION:\n"
        "CONTRACTION-\n"
        "AWARE\n"
        "PPO\n"
        "(CAPPO)\n"
        "FOR\n"
        "STABLE\n"
        "LANGUAGE\n"
        "MODEL FINE-TUNING\n"
        "Dun Yuan\n"
        "Di Wu\n"
        "Xue Liu\n"
        "ABSTRACT\n"
        "full text " * 300
    )

    assert read_pipeline._pdf_text_identity_ok(cappo_paper, cappo_text) is True


def test_reading_reader_pdf_text_fallback_requires_identity(monkeypatch, tmp_path):
    reading_root = ROOT / "modules" / "reading"
    common = _load_reading_common()
    paper_sources = _load_reading_paper_sources()

    pdf_url = "https://iris.unito.it/retrieve/dc916e27-4a93-4f88-a328-64084c513c0a/5673_CheckMate_Watermarking_Gr%20%281%29.pdf"

    def fake_download(_paper, pdf_dir, _log):
        return False, pdf_dir / "paper.pdf", "", {
            "attempts": [{
                "kind": "search_result_pdf_requires_text_identity",
                "pdf_url": pdf_url,
                "accepted": True,
                "downloaded": False,
                "download_failure_reason": "http_403",
                "requires_pdf_text_identity_check": True,
            }],
            "selected": {},
        }

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://https://iris.unito.it/retrieve/checkmate.pdf"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        text = (
            "Title: 5673_CheckMate_Watermarking_Gr.pdf\n"
            "URL Source: https://iris.unito.it/retrieve/checkmate.pdf\n"
            "Number of Pages: 24\n"
            "Markdown Content:\n"
            "Original Citation:\n"
            "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time\n"
            "Published as a conference paper at ICLR 2026\n"
            "# CHECK MATE ! WATERMARKING GRAPH DIFFUSION\n"
            "# MODELS IN POLYNOMIAL TIME\n"
            "Roberto Gheda\n"
            "Abele Malan\n"
            "Robert Birke\n"
            "Maksim Kitsak\n"
            "Lydia Chen\n"
            "## ABSTRACT\n"
            "Watermarking provides an effective means for data governance.\n"
            "## 1 INTRODUCTION\n"
            + ("experiment evaluation references graph diffusion watermarking " * 260)
        )
        content = text.encode("utf-8")

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setattr(paper_sources, "_download_first_readable_pdf", fake_download)
    monkeypatch.setattr(paper_sources, "_openalex_full_text_hints", lambda _paper: {"status": "no_openalex_full_text_hints", "hints": []})
    monkeypatch.setattr(paper_sources, "_same_paper_html_hints", lambda _paper: {"status": "no_same_paper_html_hints", "hints": [], "attempts": []})
    monkeypatch.setattr(paper_sources, "service_get", lambda *_args, **_kwargs: FakeResponse())

    run_dir = reading_root / ".runtime" / "output" / _reading_test_run_id()
    shutil.rmtree(run_dir, ignore_errors=True)
    packet = paper_sources.acquire_full_text(
        {
            "paper_id": "10011153",
            "title": "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time",
            "authors": ["Roberto Gheda", "Abele Malan", "Robert Birke", "Maksim Kitsak", "Lydia Chen"],
        },
        run_dir,
        log=lambda _message: None,
    )

    assert packet["full_text_available"] is True
    assert packet["full_text_status"] == "html_text_read"
    assert packet["text_kind"] == "html"
    assert packet["full_text_evidence_kind"] == "reader_pdf_text"
    assert packet["true_pdf_full_text"] is False
    assert packet["pdf_downloaded"] is False
    assert packet["pdf_url"] == pdf_url
    assert packet["html_acquisition"]["selected"]["pdf_text_identity_check"] is True
    assert packet["html_acquisition"]["selected"]["paper_body_markers"] is True
    assert (reading_root / packet["text_path"]).exists()
    shutil.rmtree(run_dir, ignore_errors=True)


def test_reading_openreview_reader_pdf_text_accepts_verified_body(monkeypatch):
    common = _load_reading_common()
    paper_sources = _load_reading_paper_sources()

    pdf_url = "https://openreview.net/pdf/8a993afd3ac54dd1e47a9dfe5181476339338afa.pdf"

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://openreview.net/pdf/8a993afd3ac54dd1e47a9dfe5181476339338afa.pdf"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        text = (
            "Title: Escaping Policy Contraction\n"
            "URL Source: https://openreview.net/pdf/8a993afd3ac54dd1e47a9dfe5181476339338afa.pdf\n"
            "Markdown Content:\n"
            "Published as a conference paper at ICLR 2026\n"
            "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning\n"
            "Dun Yuan\n"
            "Cheng Chen\n"
            "Yiming Chen\n"
            "ABSTRACT\n"
            "Policy contraction can destabilize reinforcement learning from human feedback.\n"
            "1 INTRODUCTION\n"
            + ("policy optimization language model fine tuning contraction evaluation " * 260)
        )
        content = text.encode("utf-8")

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setattr(paper_sources, "service_get", lambda *_args, **_kwargs: FakeResponse())

    text, receipt = paper_sources._fetch_reader_pdf_text(
        {
            "title": "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning",
            "authors": ["Dun Yuan", "Cheng Chen", "Yiming Chen"],
        },
        pdf_url,
    )

    assert text
    assert receipt["accepted"] is True
    assert receipt["paper_body_markers"] is True
    assert receipt["pdf_text_identity_check"] is True


def test_reading_openreview_reader_pdf_text_rejects_challenge(monkeypatch):
    common = _load_reading_common()
    paper_sources = _load_reading_paper_sources()

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://openreview.net/pdf?id=vDlkJewkDu"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        text = "Verifying your browser before accessing OpenReview. Complete the check below."
        content = text.encode("utf-8")

    monkeypatch.setattr(common, "_PROCESS_BLOCKERS", {})
    monkeypatch.setattr(paper_sources, "service_get", lambda *_args, **_kwargs: FakeResponse())

    text, receipt = paper_sources._fetch_reader_pdf_text(
        {
            "title": "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning",
            "authors": ["Dun Yuan"],
        },
        "https://openreview.net/pdf?id=vDlkJewkDu",
    )

    assert text == ""
    assert receipt["accepted"] is False
    assert receipt["reason"] == "openreview_reader_challenge"


def test_reading_reader_pdf_text_prioritizes_openreview_hash_before_attachment(monkeypatch):
    paper_sources = _load_reading_paper_sources()

    seen_urls: list[str] = []

    def fake_fetch(_paper, pdf_url, timeout=45):
        seen_urls.append(pdf_url)
        return "", {"kind": "reader_pdf_text", "accepted": False, "pdf_url": pdf_url, "reason": "synthetic_reject"}

    monkeypatch.setattr(paper_sources, "_fetch_reader_pdf_text", fake_fetch)

    paper_sources._reader_pdf_text_from_failed_pdf_attempts(
        {"title": "Escaping Policy Contraction: Contraction-Aware PPO (CaPPO) for Stable Language Model Fine-Tuning"},
        {
            "attempts": [
                {"kind": "openreview_attachment_pdf_from_note_id", "pdf_url": "https://openreview.net/attachment?id=vDlkJewkDu&name=pdf"},
                {"kind": "openreview_pdf_from_forum_url", "pdf_url": "https://openreview.net/pdf?id=vDlkJewkDu"},
                {
                    "kind": "iclr_mlanthology_page_pdf_link_requires_text_identity",
                    "pdf_url": "https://openreview.net/pdf/8a993afd3ac54dd1e47a9dfe5181476339338afa.pdf",
                    "requires_pdf_text_identity_check": True,
                },
            ]
        },
        limit=3,
    )

    assert seen_urls == [
        "https://openreview.net/pdf/8a993afd3ac54dd1e47a9dfe5181476339338afa.pdf",
        "https://openreview.net/pdf?id=vDlkJewkDu",
        "https://openreview.net/attachment?id=vDlkJewkDu&name=pdf",
    ]


def test_reading_runtime_cached_full_text_requires_identity(monkeypatch):
    reading_root = ROOT / "modules" / "reading"
    paper_sources = _load_reading_paper_sources()

    cache_root = reading_root / ".runtime" / "output" / _reading_test_run_id()
    run_dir = reading_root / ".runtime" / "output" / _reading_test_run_id()
    shutil.rmtree(cache_root, ignore_errors=True)
    shutil.rmtree(run_dir, ignore_errors=True)
    cached_text_path = cache_root / "cached" / "extracted" / "html_text.txt"
    cached_text_path.parent.mkdir(parents=True)
    cached_text = (
        "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time\n"
        "Published as a conference paper at ICLR 2026\n"
        "Roberto Gheda\n"
        "Abele Malan\n"
        "Robert Birke\n"
        "Maksim Kitsak\n"
        "Lydia Chen\n"
        "ABSTRACT\n"
        "Watermarking provides an effective means for data governance.\n"
        "1 INTRODUCTION\n"
        + ("experiment evaluation references graph diffusion watermarking " * 260)
    )
    cached_text_path.write_text(cached_text, encoding="utf-8")
    (cache_root / "cached" / "read_results.json").write_text(
        json.dumps(
            {
                "paper": {"title": "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time"},
                "full_text_packet": {
                    "full_text_available": True,
                    "full_text_evidence_kind": "reader_pdf_text",
                    "text_kind": "html",
                    "text_path": str(cached_text_path),
                    "pdf_url": "https://iris.unito.it/retrieve/checkmate.pdf",
                    "full_text_chars": len(cached_text),
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_download(_paper, pdf_dir, _log):
        return False, pdf_dir / "paper.pdf", "", {"attempts": [], "selected": {}}

    monkeypatch.setattr(paper_sources, "OUTPUT_ROOT", cache_root)
    monkeypatch.setattr(paper_sources, "_FULL_TEXT_CACHE_INDEX", None)
    monkeypatch.setattr(paper_sources, "_download_first_readable_pdf", fake_download)
    monkeypatch.setattr(paper_sources, "_openalex_full_text_hints", lambda _paper: {"status": "no_openalex_full_text_hints", "hints": []})
    monkeypatch.setattr(paper_sources, "_same_paper_html_hints", lambda _paper: {"status": "no_same_paper_html_hints", "hints": [], "attempts": []})

    packet = paper_sources.acquire_full_text(
        {
            "paper_id": "10011153",
            "title": "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time",
            "authors": ["Roberto Gheda", "Abele Malan", "Robert Birke", "Maksim Kitsak", "Lydia Chen"],
        },
        run_dir,
        log=lambda _message: None,
    )

    assert packet["full_text_available"] is True
    assert packet["full_text_evidence_kind"] == "runtime_cached_full_text"
    assert packet["true_pdf_full_text"] is False
    assert packet["html_acquisition"]["selected"]["pdf_text_identity_check"] is True
    assert packet["html_acquisition"]["selected"]["paper_body_markers"] is True
    assert (reading_root / packet["text_path"]).exists()
    shutil.rmtree(cache_root, ignore_errors=True)
    shutil.rmtree(run_dir, ignore_errors=True)


def test_reading_iclr_mlanthology_slug_route_uses_first_author_and_title(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    seen_urls: list[str] = []
    locator_flags: list[bool] = []

    def fake_page_candidates(
        _paper,
        url,
        *,
        kind,
        scan_assets=False,
        allow_pdf_text_identity_check=False,
        include_openreview_locators=False,
    ):
        seen_urls.append(url)
        locator_flags.append(include_openreview_locators)
        return [{
            "kind": kind + "_pdf_link",
            "pdf_url": "https://openreview.net/pdf/hash.pdf",
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    monkeypatch.setattr(read_pipeline, "_publisher_page_candidates_from_url", fake_page_candidates)

    candidates = read_pipeline._iclr_mlanthology_candidates({
        "title": "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time",
        "authors": ["Roberto Gheda", "Abele Mălan"],
        "venue": "ICLR 2026",
        "url": "https://iclr.cc/virtual/2026/poster/10011153",
    })

    assert seen_urls == ["https://mlanthology.org/iclr/2026/gheda2026iclr-checkmate/"]
    assert locator_flags == [True]
    assert candidates[0]["pdf_url"] == "https://openreview.net/pdf/hash.pdf"
    assert candidates[0]["requires_pdf_text_identity_check"] is True


def test_reading_mlanthology_openreview_locator_feeds_same_paper_mirrors(monkeypatch):
    read_pipeline = _load_reading_pipeline()
    page_url = "https://mlanthology.org/iclr/2026/gheda2026iclr-checkmate/"
    html = (
        '<meta name="citation_title" content="CheckMate! Watermarking Graph Diffusion Models in Polynomial Time">'
        '<a href="https://openreview.net/forum?id=92fliNrbxY">OpenReview</a>'
    )
    monkeypatch.setattr(
        read_pipeline,
        "_request_html",
        lambda *_args, **_kwargs: (page_url, html, {"status_code": 200, "accepted": True}),
    )

    candidates = read_pipeline._publisher_page_candidates_from_url(
        {"title": "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time"},
        page_url,
        kind="iclr_mlanthology_page",
        allow_pdf_text_identity_check=True,
        include_openreview_locators=True,
    )
    enriched = read_pipeline._paper_with_discovered_openreview_id(
        {"title": "CheckMate! Watermarking Graph Diffusion Models in Polynomial Time"},
        {str(candidates[0].get("openreview_note_id") or "")},
    )

    assert candidates[0]["pdf_url"] == "https://openreview.net/pdf?id=92fliNrbxY"
    assert candidates[0]["openreview_note_id"] == "92fliNrbxY"
    assert enriched["openreview_id"] == "92fliNrbxY"


def test_reading_search_result_pdf_candidate_downloads_only_after_identity_check(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    def fake_candidates(_paper, **_kwargs):
        return [{
            "kind": "search_result_pdf_requires_text_identity",
            "pdf_url": "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf",
            "accepted": True,
            "requires_pdf_text_identity_check": True,
        }]

    def fake_download(url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.5\n")
        return True, {"accepted": True, "url": url}

    def fake_extract(_path, max_chars=None):
        return (
            "Robust Adaptive Multi-Step Predictive Shielding\n"
            "Tanmay Sadanand Ambadkar, Darshan Chudiwal, Greg Anderson, Abhinav Verma\n"
            "Abstract\n"
            "full text " * 300
        )

    monkeypatch.setattr(read_pipeline, "_pdf_candidates_for_reading", fake_candidates)
    monkeypatch.setattr(read_pipeline, "_download_pdf_with_receipt", fake_download)
    monkeypatch.setattr(read_pipeline, "_extract_pdf_text", fake_extract)

    downloaded, pdf_path, pdf_url, receipt = read_pipeline._download_first_readable_pdf(
        {
            "id": "paper",
            "paper_id": "paper",
            "title": "Robust Adaptive Multi-Step Predictive Shielding",
            "authors": ["Tanmay Sadanand Ambadkar", "Darshan Chudiwal", "Greg Anderson", "Abhinav Verma"],
        },
        tmp_path / "pdfs",
        lambda _message: None,
    )

    assert downloaded is True
    assert pdf_url == "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf"
    assert pdf_path.exists()
    assert receipt["selected"]["pdf_text_identity_check"] is True
    assert receipt["selected"]["readable_pdf"] is True


def test_reading_runtime_cached_pdf_candidates_index_read_results(monkeypatch, tmp_path):
    read_pipeline = _load_reading_pipeline()

    reading_root = ROOT / "modules" / "reading"
    cache_root = reading_root / ".runtime" / "output" / _reading_test_run_id()
    shutil.rmtree(cache_root, ignore_errors=True)
    cached_run = cache_root / "run" / "paper"
    cached_pdf = cached_run / "downloads" / "paper.pdf"
    cached_pdf.parent.mkdir(parents=True)
    cached_pdf.write_bytes(b"%PDF-1.7\ncached")
    read_results = cached_run / "read_results.json"
    read_results.write_text(
        json.dumps(
            {
                "paper": {"title": "Robust Adaptive Multi-Step Predictive Shielding", "doi": "10.1145/123.456"},
                "full_text_packet": {
                    "pdf_url": "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf",
                    "pdf_path": str(cached_pdf),
                    "full_text_chars": 57057,
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(read_pipeline, "CACHE_BATCH_TEST_ROOTS", (cache_root,))
    monkeypatch.setattr(read_pipeline, "CACHE_RUN_ROOTS", ())
    read_pipeline._PDF_CACHE_INDEX = None

    candidates = read_pipeline._runtime_cached_pdf_candidates(
        {"title": "Robust Adaptive Multi-Step Predictive Shielding"},
        limit=4,
    )

    assert candidates
    assert candidates[0]["kind"] == "reading_runtime_cached_pdf"
    assert candidates[0]["cached_pdf_path"] == str(cached_pdf)
    assert candidates[0]["pdf_url"] == "https://people.reed.edu/~grega/papers/ramps-iclr-26.pdf"
    assert candidates[0]["requires_pdf_text_identity_check"] is True

    doi_candidates = read_pipeline._runtime_cached_pdf_candidates(
        {"title": "A Different Local Title", "doi": "10.1145/123.456"},
        limit=4,
    )
    assert doi_candidates
    assert doi_candidates[0]["cached_pdf_path"] == str(cached_pdf)
    assert doi_candidates[0]["requires_pdf_text_identity_check"] is False
    assert doi_candidates[0]["runtime_cache_identity_basis"] == "doi_exact_match"
    shutil.rmtree(cache_root, ignore_errors=True)


def test_reading_does_not_blame_conference_metadata_for_missing_full_text():
    paper_sources = _load_reading_paper_sources()

    reason = paper_sources._blocked_full_text_reason(
        {"title": "Flexibility-Aware Geometric Latent Diffusion for Full-Atom Peptide Design", "url": "https://icml.cc/virtual/2026/poster/62058"},
        {"attempts": [], "candidate_discovery": []},
        {"attempts": []},
        {},
    )

    assert reason["code"] == "blocked_no_same_paper_full_text_locator"
    assert "virtual" not in reason["message_zh"]
    assert "poster" not in reason["message_zh"]


def _load_find_pipeline():
    finding_main = _load_finding_main()
    return finding_main._private_import("flow.pipeline")


def _load_finding_main():
    finding_module_root = ROOT / "modules" / "finding"
    spec = importlib.util.spec_from_file_location("finding_main_contract", finding_module_root / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _isolate_find_runtime(monkeypatch, find_pipeline, runtime: Path) -> None:
    monkeypatch.setattr(find_pipeline, "WORKFLOW_RUNTIME_DIR", runtime)
    runtime_module = sys.modules.get("finding_runtime")
    if runtime_module is not None:
        monkeypatch.setattr(runtime_module, "WORKFLOW_RUNTIME_DIR", runtime, raising=False)
        monkeypatch.setattr(runtime_module, "RUNS_DIR", runtime / "runs", raising=False)
        monkeypatch.setattr(runtime_module, "STATE_DIR", runtime / "state", raising=False)
        monkeypatch.setattr(runtime_module, "LOCAL_DATABASE_DIR", runtime / "local_database", raising=False)
        monkeypatch.setattr(runtime_module, "CONFIG_PATH", runtime / ".config.json", raising=False)


def test_find_translation_keeps_prose_markup_visible_to_llm_without_splitting_words():
    find_pipeline = _load_find_pipeline()
    item = {
        "abstract_en": (
            r"We introduce \textbf{Prot}ein-\textbf{L}igand Conditioned "
            r"\textbf{D}iscrete \textbf{D}iffusion (ProtLiD²) with $p_\theta$."
        ),
    }

    prompt_text = find_pipeline._prepare_abstract_translation_prompt_text(item, 2600)

    assert r"\textbf{Prot}ein-\textbf{L}igand Conditioned \textbf{D}iscrete \textbf{D}iffusion (ProtLiD²)" in prompt_text
    assert prompt_text.endswith("with [[LATEX_1]].")
    assert item["_abstract_translation_latex_segments"] == [
        {"placeholder": "[[LATEX_1]]", "latex": r"$p_\theta$"},
    ]
    restored = find_pipeline._restore_latex_translation_placeholders(item, "该方法保留 ProtLiD² 和 [[LATEX_1]]。")
    assert restored == r"该方法保留 ProtLiD² 和 $p_\theta$。"
    assert find_pipeline._has_unresolved_prose_latex_markup(r"plain \sourceformat{word}") is True
    assert find_pipeline._has_unresolved_prose_latex_markup(r"formula $\sourceformat{x}$") is False
    assert find_pipeline._chinese_translation_reject_reason(
        r"这是包含 \sourceformat{来源排版标记} 的完整中文翻译，必须交回模型重新处理，并保留公式 $p_\theta$。",
        str(item["abstract_en"]),
        item,
    ) == "unresolved_prose_latex_markup"


def test_find_title_prefilter_uses_local_ids_and_recovers_missing_rows(monkeypatch):
    find_pipeline = _load_find_pipeline()
    monkeypatch.setenv("USE_LLM_TITLE_FILTER", "1")
    monkeypatch.setenv("TITLE_FILTER_SEQUENTIAL", "1")
    monkeypatch.setenv("FIND_TITLE_SCORE_CACHE", "0")

    class IncompleteFirstAttemptLLM:
        enabled = True
        timeout_sec = 120
        provider = "openai_compatible"

        def __init__(self):
            self.calls = []

        def json_or_error(self, prompt, **kwargs):
            aliases = re.findall(r"^- (p\d{3}):", prompt, flags=re.MULTILINE)
            self.calls.append({"aliases": aliases, "max_tokens": kwargs.get("max_tokens"), "prompt": prompt})
            omitted = min(3, len(aliases) - 1)
            returned = aliases[:-omitted] if "attempt 1" in prompt and omitted else aliases
            return {
                "ok": True,
                "data": {
                    "scored": [
                        {
                            "id": alias,
                            "fit_score": "7.0",
                            "diversity_score": "5.0",
                            "hit_directions": ["protein design"],
                            "category": "protein",
                            "reason": "标题与研究方向相关。",
                        }
                        for alias in returned
                    ] + [{"id": "unknown-row", "fit_score": 1.0, "diversity_score": 1.0}]
                },
                "error": "",
            }

    items = [
        {
            "id": f"paper-{index}",
            "title": f"Protein design paper {index}",
            "abstract": "Protein generation with diffusion and reinforcement learning.",
            "venue": "TestVenue",
            "year": 2026,
        }
        for index in range(102)
    ]
    llm = IncompleteFirstAttemptLLM()
    reports = []
    selected = find_pipeline._prefilter_titles(
        items,
        find_pipeline.AppConfig(
            provider="openai_compatible",
            research_topic="protein design",
            research_interest="protein diffusion and reinforcement learning",
            llm_concurrency=1,
        ),
        llm,
        "TestVenue",
        lambda _message: None,
        lambda: False,
        scan_all=True,
        title_filter_reports=reports,
    )

    assert len(selected) == 102
    assert all(item["reason_source"] == "llm title filter" for item in selected)
    assert reports[0]["llm_title_scored_papers"] == 102
    assert [len(call["aliases"]) for call in llm.calls] == [100, 3, 2, 1]
    assert all(call["max_tokens"] == 0 for call in llm.calls)
    assert all("paper-0:" not in call["prompt"] for call in llm.calls)


def test_find_title_prefilter_falls_back_locally_after_retry_exhaustion(monkeypatch):
    find_pipeline = _load_find_pipeline()
    monkeypatch.setenv("USE_LLM_TITLE_FILTER", "1")
    monkeypatch.setenv("TITLE_FILTER_SEQUENTIAL", "1")
    monkeypatch.setenv("FIND_TITLE_SCORE_CACHE", "0")

    class EmptyTitleLLM:
        enabled = True
        timeout_sec = 120
        provider = "openai_compatible"

        def __init__(self):
            self.calls = 0

        def json_or_error(self, _prompt, **_kwargs):
            self.calls += 1
            return {"ok": True, "data": {"scored": []}, "error": ""}

    items = [
        {
            "id": f"paper-{index}",
            "title": f"Protein design paper {index}",
            "abstract": "Protein generation with diffusion.",
            "venue": "TestVenue",
            "year": 2026,
        }
        for index in range(3)
    ]
    llm = EmptyTitleLLM()
    logs = []
    reports = []
    selected = find_pipeline._prefilter_titles(
        items,
        find_pipeline.AppConfig(
            provider="openai_compatible",
            research_topic="protein design",
            research_interest="protein diffusion",
            llm_concurrency=1,
        ),
        llm,
        "TestVenue",
        logs.append,
        lambda: False,
        scan_all=True,
        title_filter_reports=reports,
    )

    assert llm.calls == 5
    assert len(selected) == 3
    assert all(item["reason_source"] == "local title screen" for item in selected)
    assert all(item["title_filter_fallback_used"] for item in selected)
    assert all(item["title_llm_retry_exhausted"] for item in selected)
    assert reports[0]["mode"] == "llm_with_local_fallback"
    assert reports[0]["llm_title_scored_papers"] == 0
    assert reports[0]["local_title_ranked_papers"] == 3
    assert any("local title scores retained" in message for message in logs)

    scoring_groups = find_pipeline._select_title_abstract_scoring_groups(
        [("TestVenue", selected, "venue")],
        find_pipeline.AppConfig(provider="openai_compatible", title_abstract_scoring_limit=10),
        require_title_llm_score=True,
        log=lambda _message: None,
    )
    assert len(scoring_groups) == 1
    assert len(scoring_groups[0][1]) == 3


def test_find_json_recovery_keeps_completed_scoring_and_translation_rows():
    finding_main = _load_finding_main()
    finding_runtime = finding_main._private_import("finding_runtime")

    scored = finding_runtime.extract_json('{"scored":[{"id":"p001","fit_score":7.0}')
    translated = finding_runtime.extract_json('{"translations":[{"id":"p001","abstract_zh":"摘要"}')

    assert scored == {"scored": [{"id": "p001", "fit_score": 7.0}]}
    assert translated == {"translations": [{"id": "p001", "abstract_zh": "摘要"}]}


def test_find_abstract_scoring_uses_local_ids_and_retries_mismatched_id(monkeypatch):
    find_pipeline = _load_find_pipeline()
    monkeypatch.setenv("ABSTRACT_SCORING_BATCH_SIZE", "10")
    monkeypatch.setenv("ABSTRACT_SCORING_MAX_BATCH_SIZE", "10")
    monkeypatch.setenv("ABSTRACT_SCORING_MAX_WORKERS", "1")
    monkeypatch.setenv("ABSTRACT_SCORING_WORKER_CAP", "1")
    monkeypatch.setenv("OMITTED_ITEM_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("FIND_FINAL_SCORE_CACHE", "0")

    class MismatchedIdLLM:
        enabled = True
        timeout_sec = 120
        retries = 1
        provider = "openai_compatible"

        def __init__(self):
            self.calls = []

        def json_or_error(self, prompt, **kwargs):
            aliases = re.findall(r"^ID: (p\d{3})$", prompt, flags=re.MULTILINE)
            self.calls.append({"aliases": aliases, "max_tokens": kwargs.get("max_tokens"), "prompt": prompt})
            rows = []
            for index, alias in enumerate(aliases):
                returned_id = "rewritten-paper-id" if len(aliases) > 1 and index == len(aliases) - 1 else alias
                rows.append({
                    "id": returned_id,
                    "category": "protein generation",
                    "fit_score": 8.1,
                    "diversity_score": 6.2,
                    "recommend_for_deep_reading": True,
                    "topic_evidence": "passed: protein diffusion",
                    "topic_evidence_supported": True,
                    "matched_topic_route": "protein diffusion",
                    "topic_evidence_basis": "The abstract evaluates protein diffusion generation.",
                    "missing_topic_evidence": [],
                    "hit_directions_zh": ["蛋白质扩散生成"],
                    "hit_directions_en": ["protein diffusion generation"],
                    "fit_explanation_zh": "摘要给出了蛋白质扩散生成方法与实验结果。该方法可用于当前研究。",
                    "fit_explanation_en": "The abstract presents a protein diffusion method and results. It is reusable for this project.",
                    "reason_zh": "论文提出可控生成方法，可帮助比较约束策略，并借鉴其评测设计。",
                    "reason_en": "The method fits controlled generation and provides reusable evaluation design.",
                })
            return {"ok": True, "data": {"evaluations": rows}, "error": ""}

    items = [
        {
            "id": f"real-paper-{index}",
            "title": f"Protein diffusion study {index}",
            "abstract": "We develop and evaluate a diffusion method for controllable protein generation.",
            "source": "test",
            "venue": "TestVenue",
            "year": 2026,
        }
        for index in range(10)
    ]
    llm = MismatchedIdLLM()
    evaluated = find_pipeline._evaluate_items(
        items,
        find_pipeline.AppConfig(
            provider="openai_compatible",
            research_topic="protein diffusion",
            research_interest="controllable protein generation",
            title_abstract_scoring_limit=10,
            abstract_scoring_batch_size=10,
            abstract_scoring_max_workers=1,
        ),
        llm,
        "TestVenue",
        lambda _message: None,
    )

    assert len(evaluated) == 10
    assert all(item["reason_source"] == "llm abstract evaluation" for item in evaluated)
    assert [len(call["aliases"]) for call in llm.calls] == [10, 1]
    assert all(call["max_tokens"] == 0 for call in llm.calls)
    assert all("ID: real-paper-" not in call["prompt"] for call in llm.calls)


def test_find_recommendations_rank_without_topic_or_score_gates():
    find_pipeline = _load_find_pipeline()
    config = find_pipeline.AppConfig(
        provider="openai_compatible",
        api_key="test-key",
        model="test-model",
        max_recommended_papers=2,
        research_interest="protein design",
    )

    def candidate(item_id: str, score: float, **updates) -> dict:
        row = {
            "id": item_id,
            "title": f"Paper {item_id}",
            "abstract": "This real abstract describes a concrete method, evaluation protocol, and experimental result.",
            "reason_source": "llm abstract evaluation",
            "fit_score": score,
            "llm_fit_score": score,
            "diversity_score": score,
            "recommendation_score": score,
            "topic_evidence_supported": False,
            "topic_evidence": "weak: missing adaptive topic evidence",
            "missing_topic_evidence": ["protein target"],
            "llm_complete_route_guard_failed": True,
            "foundation_demoted_from_strong": True,
            "not_positive_support": True,
        }
        row.update(updates)
        return row

    ranked = find_pipeline._recommended(
        [
            candidate("low", 2.1),
            candidate("high", 4.9, title="Shared protein design paper"),
            candidate("duplicate", 1.0, title="Shared protein design paper"),
            candidate("missing-abstract", 10.0, abstract=""),
            candidate("unscored", 8.0, reason_source="llm title filter"),
        ],
        config,
        source_count=1,
    )

    assert [item["id"] for item in ranked] == ["high", "low"]
    assert all(item["find_recommendation"] for item in ranked)
    assert all(item["topic_evidence_supported"] is False for item in ranked)
    assert all("foundation_demoted_from_strong" not in item for item in ranked)
    assert all("not_positive_support" not in item for item in ranked)
    assert ranked[1]["fit_score"] == 2.1

    local_only = candidate("local-only", 9.9, reason_source="adaptive profile fallback")
    assert find_pipeline._recommended(
        [local_only],
        find_pipeline.AppConfig(provider="mock", api_key="", max_recommended_papers=1),
        source_count=1,
    ) == []


def test_find_recommendation_target_is_at_least_five_per_selected_source():
    find_pipeline = _load_find_pipeline()

    config = find_pipeline.AppConfig(
        provider="openai_compatible",
        api_key="test-key",
        model="test-model",
        max_recommended_papers=20,
    )
    assert find_pipeline._strong_recommendation_target_count(
        config,
        source_count=5,
    ) == 25
    assert find_pipeline._strong_recommendation_target_count(
        find_pipeline.AppConfig(max_recommended_papers=40),
        source_count=5,
    ) == 40

    candidates = [
        {
            "id": f"paper-{index}",
            "title": f"Unique final-scored paper number {index}",
            "abstract": "This real abstract describes a concrete method, controlled evaluation, and reusable result.",
            "reason_source": "llm abstract evaluation",
            "fit_score": 10.0 - index * 0.1,
            "llm_fit_score": 10.0 - index * 0.1,
            "diversity_score": 5.0,
            "recommendation_score": 10.0 - index * 0.1,
        }
        for index in range(30)
    ]
    recommended = find_pipeline._recommended(candidates, config, source_count=5)
    assert len(recommended) == 25
    assert [item["id"] for item in recommended] == [f"paper-{index}" for index in range(25)]


def test_find_weak_topic_audit_does_not_rewrite_final_llm_scores():
    find_pipeline = _load_find_pipeline()
    item = {
        "id": "audit-only",
        "title": "Transfer method",
        "abstract": "This real abstract describes a reusable transfer method and controlled evaluation protocol.",
        "reason_source": "llm abstract evaluation",
        "fit_score": 7.4,
        "diversity_score": 6.8,
    }

    find_pipeline._apply_llm_topic_evidence(
        item,
        {
            "topic_evidence_supported": False,
            "topic_evidence": "weak: missing adaptive topic evidence",
            "missing_topic_evidence": ["protein target"],
        },
        "protein design",
    )

    assert item["topic_evidence_audit_only"] is True
    assert item["topic_evidence_supported"] is False
    assert item["fit_score"] == 7.4
    assert item["diversity_score"] == 6.8

    foundation = {
        **item,
        "recommendation_score": 7.25,
        "stable_rank_score": 7.1,
        "stable_source_score": 6.9,
        "foundation_invalid_reason": "audit-only route mismatch",
    }
    find_pipeline._demote_unstable_foundation_item(foundation)
    assert foundation["fit_score"] == 7.4
    assert foundation["diversity_score"] == 6.8
    assert foundation["recommendation_score"] == 7.25
    assert foundation["stable_rank_score"] == 7.1
    assert foundation["stable_source_score"] == 6.9


def test_find_stage0_prompt_requires_complete_searchable_domain_expansions():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")
    prompt = research_profile.build_stage0_prompt(finding_runtime.AppConfig(
        research_topic="非英文领域任务",
        research_interest="非英文方法和应用",
    ))
    compact_prompt = " ".join(prompt.split())

    assert "complete English safe expansion" in compact_prompt
    assert "stand alone as a literature-search phrase" in compact_prompt
    assert "full domain/object/task qualifiers" in compact_prompt


def test_find_search_terms_keep_all_llm_keywords_equal_and_ignore_inferred_categories():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")
    class FakeLLM:
        enabled = True

        def __init__(self):
            self.prompt = ""

        def json_or_none(self, prompt, **_kwargs):
            self.prompt = prompt
            return {
                "search_keywords": [
                    "Retrieval application terms",
                    "protein",
                    "diffusion",
                    "RL",
                    "protein inverse folding",
                    "reinforcement learning",
                ],
                "arxiv_categories": ["q-bio.BM", "q-bio.QM", "q-bio.ZZ"],
                "biorxiv_categories": ["bioinformatics", "biophysics", "epidemiology", "invented biology"],
            }

    config = finding_runtime.AppConfig(
        research_topic="蛋白质条件生成扩散后训练",
        research_interest="离散扩散后训练用于蛋白质逆折叠",
        researcher_profile="研究蛋白质生成与强化学习",
        arxiv_categories=[],
        biorxiv_categories=[],
    )
    llm = FakeLLM()
    terms = research_profile.extract_search_terms(config, llm)

    assert terms["search_keywords"] == [
        "protein",
        "diffusion",
        "RL",
        "protein inverse folding",
        "reinforcement learning",
    ]
    assert terms["arxiv_categories"] == []
    assert terms["biorxiv_categories"] == []
    assert terms["arxiv_category_source"] == "none"
    assert terms["biorxiv_category_source"] == "none"
    assert "anchor_terms" not in terms
    assert "refine_groups" not in terms
    assert "研究蛋白质生成与强化学习" in llm.prompt
    assert '"arxiv_categories"' not in llm.prompt


def test_find_search_terms_manual_categories_override_llm_suggestions():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")

    class FakeLLM:
        enabled = True

        def json_or_none(self, *_args, **_kwargs):
            return {
                "search_keywords": ["protein", "diffusion", "RL"],
                "arxiv_categories": ["q-bio.BM", "cs.LG"],
                "biorxiv_categories": ["biophysics", "bioengineering"],
            }

    config = finding_runtime.AppConfig(
        research_topic="protein design",
        research_interest="protein inverse folding with diffusion",
        arxiv_categories=["cs.ai", "physics.ed-ph"],
        biorxiv_categories=["bioinformatics", "molecular biology"],
    )
    terms = research_profile.extract_search_terms(config, FakeLLM())

    assert terms["arxiv_categories"] == ["cs.AI", "physics.ed-ph"]
    assert terms["arxiv_category_source"] == "configured"
    assert terms["biorxiv_categories"] == ["bioinformatics", "molecular biology"]
    assert terms["biorxiv_category_source"] == "configured"


def test_find_search_terms_use_json_parse_retry_result():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")
    calls = []

    class FakeLLM:
        enabled = True

        def json_or_error(self, _prompt, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "data": {
                    "search_keywords": [
                        "protein",
                        "protein inverse folding",
                        "protein thermostability",
                        "reinforcement learning",
                        "discrete diffusion",
                    ],
                },
            }

        def json_or_none(self, *_args, **_kwargs):
            raise AssertionError("json_or_none must not bypass the parse-retry result")

    terms = research_profile.extract_search_terms(
        finding_runtime.AppConfig(
            research_topic="protein inverse folding",
            research_interest="reinforcement learning for protein thermostability",
        ),
        FakeLLM(),
    )

    assert calls == [{"temperature": 0.1, "max_tokens": 1800}]
    assert terms["search_keywords"] == [
        "protein",
        "protein inverse folding",
        "protein thermostability",
        "reinforcement learning",
        "discrete diffusion",
    ]


def test_find_search_terms_reject_more_than_three_words_and_prompt_requires_basics():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")

    class FakeLLM:
        enabled = True

        def __init__(self):
            self.prompt = ""

        def json_or_none(self, prompt, **_kwargs):
            self.prompt = prompt
            return {
                "search_keywords": [
                    "protein",
                    "diffusion",
                    "reinforcement learning",
                    "protein inverse folding",
                    "reinforcement learning for diffusion models",
                ]
            }

    llm = FakeLLM()
    terms = research_profile.extract_search_terms(
        finding_runtime.AppConfig(
            research_topic="protein diffusion",
            research_interest="reinforcement learning for protein inverse folding",
        ),
        llm,
    )

    assert terms["search_keywords"] == [
        "protein",
        "diffusion",
        "reinforcement learning",
        "protein inverse folding",
    ]
    assert "Every item must contain 1-3 words" in llm.prompt
    assert "MUST include the basic, standalone concepts" in llm.prompt


def test_find_search_terms_keep_valid_manual_phrases_as_equal_status_keywords():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")

    class FakeLLM:
        enabled = True

        def json_or_none(self, *_args, **_kwargs):
            return {"search_keywords": ["protein", "diffusion"]}

    terms = research_profile.extract_search_terms(
        finding_runtime.AppConfig(
            research_topic="protein diffusion",
            arxiv_queries=[
                "reinforcement learning",
                "protein inverse folding",
                "too many words for one valid search phrase",
            ],
        ),
        FakeLLM(),
    )

    assert terms["search_keywords"] == [
        "protein",
        "diffusion",
        "reinforcement learning",
        "protein inverse folding",
    ]


def test_find_search_terms_do_not_demote_method_keywords():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")
    class FakeLLM:
        enabled = True

        def json_or_none(self, *_args, **_kwargs):
            return {
                "search_keywords": ["protein", "diffusion", "RL", "protein inverse folding"],
            }

    config = finding_runtime.AppConfig(
        research_topic="蛋白质逆折叠",
        research_interest="离散扩散用于蛋白质生成",
    )
    terms = research_profile.extract_search_terms(config, FakeLLM())

    assert terms["search_keywords"] == ["protein", "diffusion", "RL", "protein inverse folding"]


def test_find_search_terms_never_use_llm_inferred_source_categories():
    finding_main = _load_finding_main()
    research_profile = finding_main._private_import("research_profile")
    finding_runtime = finding_main._private_import("finding_runtime")
    class FakeLLM:
        enabled = True

        def json_or_none(self, *_args, **_kwargs):
            return {
                "search_keywords": ["protein", "reinforcement learning", "discrete diffusion models"],
                "arxiv_categories": ["q-bio.BM", "q-bio.QM", "cs.LG"],
                "biorxiv_categories": ["bioinformatics", "biophysics"],
            }

    config = finding_runtime.AppConfig(
        research_topic="蛋白质条件生成扩散后训练",
        research_interest="离散扩散后训练用于蛋白质逆折叠和热稳定性增强",
        arxiv_categories=[],
        biorxiv_categories=[],
    )
    terms = research_profile.extract_search_terms(config, FakeLLM())

    assert terms["search_keywords"] == ["protein", "reinforcement learning", "discrete diffusion models"]
    assert terms["arxiv_categories"] == []
    assert terms["biorxiv_categories"] == []
    assert terms["source"] == "llm"


def test_find_source_queries_use_one_equal_status_keyword_or_group():
    finding_main = _load_finding_main()
    sources = finding_main._private_import("sources")
    queries = sources.build_arxiv_targeted_queries(
        ["protein", "diffusion", "RL", "protein inverse folding"],
        ["q-bio.BM", "cs.LG"],
        "2026-01-01",
        "2026-07-01",
    )

    assert [label for label, _query in queries] == ["keywords"]
    query = queries[0][1]
    assert '(ti:protein OR abs:protein)' in query
    assert '(ti:diffusion OR abs:diffusion)' in query
    assert '(ti:RL OR abs:RL)' in query
    assert '(ti:"protein inverse folding" OR abs:"protein inverse folding")' in query
    assert "AND (cat:q-bio.BM OR cat:cs.LG)" in query
    assert sources.build_biorxiv_search_phrases({
        "search_keywords": ["protein", "diffusion", "RL", "protein inverse folding"],
    }) == ["protein", "diffusion", "RL", "protein inverse folding"]


def test_find_framework_passes_only_the_saved_researcher_profile():
    source = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")

    assert "feedback_profile" not in source
    assert "Previous TASTE frontend summary" not in source
    assert "researcher_profile = project_profile[:18000]" in source


def test_find_arxiv_atom_categories_are_official_not_query_labels():
    from xml.etree import ElementTree as ET

    finding_main = _load_finding_main()
    sources = finding_main._private_import("sources")
    entry = ET.fromstring("""
      <entry xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
        <id>http://arxiv.org/abs/2606.00001v1</id>
        <published>2026-06-20T00:00:00Z</published>
        <updated>2026-06-21T00:00:00Z</updated>
        <title>Protein inverse folding with diffusion</title>
        <summary>Abstract.</summary>
        <author><name>A. Author</name></author>
        <arxiv:primary_category term="q-bio.BM" />
        <category term="q-bio.BM" />
        <category term="cs.LG" />
      </entry>
    """)
    papers = []
    by_key = {}
    sources._append_arxiv_entry(
        papers,
        by_key,
        entry,
        {"a": "http://www.w3.org/2005/Atom"},
        "keywords",
        'all:"protein inverse folding"',
        "2026-01-01",
        "2026-07-01",
    )

    assert papers[0]["category"] == "q-bio.BM"
    assert papers[0]["categories"] == ["q-bio.BM", "cs.LG"]
    assert papers[0]["classification_source"] == "official"
    assert papers[0]["url"] == "https://arxiv.org/abs/2606.00001v1"
    assert papers[0]["pdf_url"] == "https://arxiv.org/pdf/2606.00001v1"
    assert papers[0]["id"] == sources.stable_id("paper", "http://arxiv.org/abs/2606.00001v1")
    assert "keywords" not in papers[0]["categories"]
    assert papers[0]["metadata"]["matched_queries"][0]["label"] == "keywords"
    assert "fallback" not in papers[0]["metadata"]["matched_queries"][0]


def test_find_source_wall_timeout_sets_cooperative_cancel_and_stops_work():
    find_pipeline = _load_find_pipeline()
    cancelled = threading.Event()
    calls = []

    def cooperative_worker():
        while not cancelled.is_set():
            calls.append(time.monotonic())
            cancelled.wait(0.002)
        return "stopped"

    done, value, error = find_pipeline._run_with_wall_timeout(
        "cooperative source",
        cooperative_worker,
        0.02,
        on_timeout=cancelled.set,
    )

    assert done is False
    assert value is None
    assert error is None
    assert cancelled.is_set()
    count_after_timeout = len(calls)
    time.sleep(0.02)
    assert len(calls) == count_after_timeout


def test_find_adaptive_route_terms_ignore_stopwords():
    find_pipeline = _load_find_pipeline()

    terms = find_pipeline._adaptive_signal_terms("Graph-based fraud detection for streaming transactions")

    assert "for" not in terms
    assert {"fraud", "detection", "streaming", "transactions"}.issubset(set(terms))


def test_find_complete_route_guard_requires_domain_anchor_with_mixed_language_interest():
    find_pipeline = _load_find_pipeline()
    interest = "\n".join([
        "Core topic route: graph-based fraud detection for streaming transactions",
        "Retrieval method terms: contrastive learning, graph neural networks",
        "Retrieval domain terms: fraud detection, streaming transactions",
        "Soft penalties: 通用表示学习",
    ])
    generic_item = {
        "id": "generic_graph_learning",
        "title": "Contrastive Graph Representation Learning for Generic Benchmarks",
        "abstract": (
            "We train graph neural networks with contrastive objectives and evaluate "
            "generic representation quality on citation and image-region benchmarks."
        ),
        "topic_evidence": "passed: direct title+abstract evidence for graph neural networks",
        "topic_evidence_supported": True,
        "matched_topic_route": "graph neural networks",
        "fit_score": 8.3,
        "diversity_score": 7.0,
        "reason_source": "llm abstract evaluation",
    }

    find_pipeline._apply_complete_route_guard(generic_item, interest)

    assert generic_item["topic_evidence_supported"] is False
    assert generic_item["evidence_role"] == "weak_or_boundary"
    assert generic_item["llm_complete_route_guard_failed"] is True
    assert any(term in generic_item["missing_topic_evidence"] for term in ["fraud", "transactions"])

    matching_item = {
        "id": "streaming_fraud_graph",
        "title": "Graph-Based Fraud Detection for Streaming Transactions",
        "abstract": (
            "The paper detects fraud in streaming transaction graphs using graph neural "
            "networks and online contrastive updates for transaction-risk scoring."
        ),
        "topic_evidence": "passed: direct title+abstract evidence for graph neural networks",
        "topic_evidence_supported": True,
        "matched_topic_route": "graph neural networks",
        "fit_score": 8.3,
        "diversity_score": 7.0,
        "reason_source": "llm abstract evaluation",
    }

    find_pipeline._apply_complete_route_guard(matching_item, interest)

    assert matching_item["topic_evidence_supported"] is True
    assert matching_item["matched_topic_route"] == "graph-based fraud detection for streaming transactions"
    assert "fraud" in matching_item["source_supported_adaptive_terms"]


def test_finding_llm_local_config_is_generic_and_local_only(monkeypatch, tmp_path):
    finding_main = _load_finding_main()
    local_config = tmp_path / "llm.local.json"
    local_config.write_text(
        json.dumps({
            "provider": "openai_compatible",
            "base_url": "https://llm.example.test/v1",
            "model": "generic-model",
            "api_key": "local-secret",
            "temperature": 0,
            "default_find_selection": {"venue_ids": ["should_not_be_loaded"]},
            "research_topic": "should not be loaded",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("FINDING_LLM_CONFIG", str(local_config))
    for name in ["LLM_PROVIDER", "LLM_API_BASE", "OPENAI_API_BASE", "OPENAI_API_KEY", "LLM_API_KEY", "LLM_MODEL", "LLM_TEMPERATURE"]:
        monkeypatch.delenv(name, raising=False)

    config = finding_main._with_llm_env_defaults({"research_topic": "explicit topic", "model": "explicit-model"})

    assert config["provider"] == "openai_compatible"
    assert config["base_url"] == "https://llm.example.test/v1"
    assert config["model"] == "explicit-model"
    assert config["api_key"] == "local-secret"
    assert config["temperature"] == 0
    assert config["research_topic"] == "explicit topic"
    assert "default_find_selection" not in config


def _venue_cache_rows(count: int, audit: dict, *, with_abstract: bool = False) -> list[dict]:
    return [
        {
            "id": f"paper_{index}",
            "title": f"Verified venue paper {index}",
            "abstract": (
                f"This is a real cached abstract for verified venue paper {index}. "
                "It is long enough to satisfy the Find metadata contract and represents official metadata."
                if with_abstract or audit.get("has_abstracts")
                else ""
            ),
            "venue": "ICLR",
            "year": 2026,
            "metadata": {"venue_metadata_audit": dict(audit)},
        }
        for index in range(count)
    ]


def test_finding_backend_is_self_contained():
    finding_root = ROOT / "modules" / "finding"
    forbidden_tokens = [
        "auto_research",
        "project_paths",
        "taste_pythonpath",
        "framework/scripts",
        "web/backend",
        "third_party/reference_TASTE_latest",
        "REFERENCE_ROOT",
        "WORKSPACE_ROOT",
        "modules/reading",
        "modules/ideation",
        "modules/planning",
        "modules/environment",
        "modules/experimenting",
        "modules/writing",
    ]
    forbidden_files = {
        "discover_arxiv.py",
        "discover_semantic_scholar.py",
        "discover_github_repos.py",
        "ingest_discovery.py",
        "finding_quality_tools.py",
        "build_literature_tool_packet.py",
        "run_literature_tool.py",
        "run_literature_base_audit.py",
        "literature_policy.py",
    }
    hits: list[str] = []
    for path in sorted(finding_root.rglob("*.py")):
        if ".runtime" in path.parts or "__pycache__" in path.parts:
            continue
        if path.name in forbidden_files:
            hits.append(str(path.relative_to(ROOT)))
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)} contains {token}")
    assert hits == []


def _load_framework_run_module():
    from orchestration import run_module

    return run_module


def _load_framework_script(relative_path: str, module_name: str):
    run_module = _load_framework_run_module()
    return run_module._load_framework_script_module(relative_path, module_name)


def test_local_maintainer_notes_are_ignored_not_tracked():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode("utf-8", "surrogateescape").split("\0")
    forbidden = [path for path in tracked if Path(path).name in {"工作状态.txt", "测试报告.md"}]
    assert forbidden == []

    ignored_paths = [
        "工作状态.txt",
        "framework/工作状态.txt",
        "modules/finding/工作状态.txt",
        "modules/experimenting/测试报告.md",
    ]
    ignored_raw = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin"],
        cwd=ROOT,
        input=("\0".join(ignored_paths) + "\0").encode("utf-8"),
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    ignored = ignored_raw.decode("utf-8", "surrogateescape").strip("\0").split("\0")
    assert set(ignored) == set(ignored_paths)


def test_find_neurips_official_papers_parser_reads_papers_nips_hash_links():
    find_pipeline = _load_find_pipeline()
    parser = sys.modules["sources.parsing"]
    html = """
    <html><body><ul>
      <li><div><a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">A Reliable Test-Time Scaling Method</a> Alice A., Bob B. <span>Main Conference Track</span></div></li>
      <li><div><a href="/paper_files/paper/2025/hash/def-Abstract-Datasets_and_Benchmarks_Track.html">Benchmarking Protein Models at Scale</a> Chen C. <span>Datasets and Benchmarks Track</span></div></li>
      <li><div><a href="/paper_files/paper/2025/hash/ghi-Abstract-Datasets_and_Benchmarks_Track.html">Protein Design Benchmark</a> Dana D., Evan E. <span>Dana D.</span><span>Evan E.</span> Datasets and Benchmarks Track</div></li>
    </ul></body></html>
    """

    rows = parser._parse_neurips_official_papers_list(html, "https://papers.nips.cc/paper_files/paper/2025", 100)

    assert len(rows) == 3
    assert rows[0]["source"] == "neurips_official_papers"
    assert rows[0]["title"] == "A Reliable Test-Time Scaling Method"
    assert rows[0]["authors"] == "Alice A., Bob B."
    assert rows[0]["track"] == "Main Conference Track"
    assert rows[0]["category"] == ""
    assert rows[0]["classification_source"] == "official_track"
    assert rows[0]["pdf_url"].endswith("abc-Paper-Conference.pdf")
    assert rows[2]["authors"] == "Dana D., Evan E."
    assert rows[2]["track"] == "Datasets and Benchmarks Track"
    assert rows[2]["category"] == ""


def test_find_neurips_legacy_tracks_skip_category_pruning(monkeypatch):
    find_pipeline = _load_find_pipeline()
    venue = {"id": "openreview_neurips", "name": "NeurIPS"}
    audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "adapter": "neurips_official_papers",
        "source_adapter": "neurips_official_papers",
        "source_scope": "official_neurips_papers_index",
        "paper_count": 60,
        "has_abstracts": True,
        "any_abstracts": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "official_title_index_verified": True,
        "official_accepted_list_verified": True,
    }
    tracks = ["Main Conference Track", "Datasets and Benchmarks Track", "Position Paper Track"]
    rows = []
    for index in range(60):
        track = tracks[index % len(tracks)]
        rows.append({
            "id": f"paper-{index}",
            "source": "neurips_official_papers",
            "title": f"NeurIPS paper title {index}",
            "abstract": "A real abstract for category semantics testing.",
            "url": "https://papers.nips.cc/paper_files/paper/2025/hash/"
            f"{index}-Abstract-{track.replace('Main Conference Track', 'Conference').replace(' ', '_')}.html",
            "venue": "NeurIPS",
            "year": 2025,
            "primary_area": track,
            "category": track,
            "track": track,
            "classification_source": "official",
            "metadata": {"venue_metadata_audit": dict(audit)},
        })
    local = {
        "venue_id": "openreview_neurips",
        "venue": "NeurIPS",
        "year": 2025,
        "paper_count": len(rows),
        "papers": rows,
        "source_adapter": "neurips_official_papers",
        "papers_path": "local/neurips/2025/papers.json",
        "category_summary_path": "local/neurips/2025/category_summary.json",
        "metadata_completeness_audit": dict(audit),
        "category_summary": {
            "paper_count": len(rows),
            "category_summary": [{"name": track, "count": 20} for track in tracks],
        },
    }
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: local)

    result = find_pipeline._load_local_category_guided_index(
        venue,
        [2025],
        find_pipeline.AppConfig(provider="mock", research_topic="protein design"),
        object(),
        None,
        lambda _message: None,
    )

    assert result is not None
    selected, reports, corpus = result
    assert len(selected) == len(corpus) == 60
    assert all(not row.get("category") and not row.get("primary_area") for row in selected)
    assert {row.get("track") for row in selected} == set(tracks)
    assert reports[0]["has_official_categories"] is False
    assert reports[0]["category_status"] == "no_official_categories"
    assert reports[0]["category_pruning_applied"] is False
    assert reports[0]["selected_category_papers"] == 60
    assert reports[0]["title_filter_input_papers"] == 60

    online_selected, online_reports = find_pipeline._select_official_category_title_index(
        venue,
        [2025],
        selected,
        reports[0]["metadata_audit"],
        find_pipeline.AppConfig(provider="mock", research_topic="protein design"),
        object(),
        lambda _message: None,
    )
    assert len(online_selected) == 60
    assert online_reports[0]["category_status"] == "no_official_categories"
    assert online_reports[0]["selected_category_papers"] == 60
    assert online_reports[0]["category_pruning_applied"] is False


def test_find_category_match_failure_retains_full_online_and_local_title_pools(monkeypatch):
    find_pipeline = _load_find_pipeline()
    venue = {"id": "test_venue", "name": "TestVenue"}
    audit = {
        "complete": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "official_title_index_verified": True,
        "official_accepted_list_verified": True,
    }
    papers = [
        {
            "id": f"paper-{index}",
            "title": f"Protein paper {index}",
            "abstract": "A complete abstract about protein design.",
            "venue": "TestVenue",
            "year": 2026,
            "primary_area": "Actual Category",
            "category": "Actual Category",
            "classification_source": "official",
        }
        for index in range(3)
    ]

    def mismatched_selection(*_args, **_kwargs):
        return {
            "venue": "TestVenue",
            "year": 2026,
            "category_count": 1,
            "selected_paper_count": 3,
            "ranked_categories": ["Missing Category"],
            "useful_through_rank": 1,
            "selected_categories": [{"name": "Missing Category", "reason": "test mismatch"}],
            "rejected_categories": [],
            "fallback_used": False,
            "selection_mode": "llm_useful_prefix_then_minimum_paper_target",
            "category_ranking_source": "llm",
            "llm_error": "",
        }

    monkeypatch.setattr(find_pipeline, "select_relevant_categories", mismatched_selection)
    logs = []
    online_selected, online_reports = find_pipeline._select_official_category_title_index(
        venue,
        [2026],
        [dict(paper) for paper in papers],
        audit,
        find_pipeline.AppConfig(provider="mock", research_topic="protein design"),
        object(),
        logs.append,
    )

    assert len(online_selected) == 3
    assert online_reports[0]["used_all_categories_fallback"] is True
    assert online_reports[0]["category_pruning_applied"] is False
    assert online_reports[0]["selection"]["category_match_fallback_used"] is True

    local = {
        "venue_id": "test_venue",
        "venue": "TestVenue",
        "year": 2026,
        "paper_count": len(papers),
        "papers": [dict(paper) for paper in papers],
        "source_adapter": "official_test_source",
        "papers_path": "local/test/2026/papers.json",
        "category_summary_path": "local/test/2026/category_summary.json",
        "metadata_completeness_audit": dict(audit),
        "category_summary": {
            "paper_count": len(papers),
            "category_summary": [{"name": "Actual Category", "count": len(papers)}],
        },
    }
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: local)
    local_result = find_pipeline._load_local_category_guided_index(
        venue,
        [2026],
        find_pipeline.AppConfig(provider="mock", research_topic="protein design"),
        object(),
        None,
        logs.append,
    )

    assert local_result is not None
    local_selected, local_reports, local_corpus = local_result
    assert len(local_selected) == len(local_corpus) == 3
    assert local_reports[0]["used_all_categories_fallback"] is True
    assert local_reports[0]["category_pruning_applied"] is False
    assert local_reports[0]["selection"]["category_match_fallback_used"] is True
    assert any("retaining all 3 papers" in message for message in logs)


def test_find_category_selection_keeps_complete_useful_prefix_above_1000(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")
    monkeypatch.delenv("VENUE_CATEGORY_SELECT_MAX", raising=False)

    entries = [
        {
            "name": f"Topic {index:02d}",
            "count": 600,
            "sample_titles": [f"Protein diffusion topic {index}"],
            "sample_keywords": ["protein", "diffusion"],
        }
        for index in range(5)
    ]
    prompts = []

    class FakeLLM:
        enabled = True

        def json_or_none(self, prompt, *_args, **_kwargs):
            prompts.append(prompt)
            return {
                "ranked_category_ids": [f"c{index:03d}" for index in range(1, len(entries) + 1)],
                "useful_through_rank": 3,
            }

    selection = selection_module.select_relevant_categories(
        {
            "venue_id": "dblp_icml",
            "venue": "ICML",
            "year": 2026,
            "paper_count": 5000,
            "category_summary": entries,
        },
        finding_runtime.AppConfig(
            provider="openai_compatible",
            research_topic="protein design",
            research_interest="protein diffusion",
            researcher_profile="Researcher studying controllable protein generation.",
        ),
        FakeLLM(),
    )

    assert selection["category_selection_max"] == 5
    assert selection["category_selection_target_papers"] == 1000
    assert selection["useful_through_rank"] == 3
    assert selection["useful_category_cutoff"] == "Topic 02"
    assert selection["useful_category_paper_count"] == 1800
    assert selection["selected_paper_count"] == 1800
    assert [row["name"] for row in selection["selected_categories"]] == [entry["name"] for entry in entries[:3]]
    assert all("relevant/useful" in row["reason"] for row in selection["selected_categories"])
    assert len(prompts) == 1
    assert "protein design" in prompts[0]
    assert "protein diffusion" in prompts[0]
    assert "controllable protein generation" in prompts[0]
    assert "Topic 04" in prompts[0]
    assert '"id": "c005"' in prompts[0]
    assert "ranked_category_ids" in prompts[0]
    assert "Rank every available category" in prompts[0]
    assert "useful_through_rank" in prompts[0]
    assert "fewer than 1000 categorized papers" in prompts[0]

    filtered = selection_module.filter_papers_by_selected_categories(
        [
            {"id": "selected", "primary_area": selection["selected_categories"][0]["name"]},
            {"id": "rejected", "primary_area": "Not a venue category"},
            {"id": "poster-only", "category": "", "track": "Poster"},
        ],
        selection,
    )
    assert [row["id"] for row in filtered] == ["selected"]


def test_find_category_selection_supplements_only_when_useful_prefix_is_below_1000(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")

    entries = [
        {"name": f"Topic {index}", "count": 250, "sample_titles": [], "sample_keywords": []}
        for index in range(6)
    ]

    class FakeLLM:
        enabled = True

        def json_or_none(self, *_args, **_kwargs):
            return {
                "ranked_categories": [entry["name"] for entry in entries],
                "useful_through_rank": 2,
            }

    selection = selection_module.select_relevant_categories(
        {
            "venue_id": "venue",
            "venue": "Venue",
            "year": 2026,
            "paper_count": 1500,
            "category_summary": entries,
        },
        finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion"),
        FakeLLM(),
    )

    assert selection["useful_category_paper_count"] == 500
    assert selection["selected_paper_count"] == 1000
    assert [row["name"] for row in selection["selected_categories"]] == [entry["name"] for entry in entries[:4]]
    assert all("relevant/useful" in row["reason"] for row in selection["selected_categories"][:2])
    assert all("supplement" in row["reason"].lower() for row in selection["selected_categories"][2:])
    assert [row["minimum_target_supplement"] for row in selection["category_ranking"]] == [False, False, True, True, False, False]


def test_find_category_selection_keeps_all_categories_when_total_is_below_1000(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")
    entries = [
        {"name": f"Topic {index}", "count": 200, "sample_titles": [], "sample_keywords": []}
        for index in range(4)
    ]

    class FakeLLM:
        enabled = True

        def json_or_none(self, *_args, **_kwargs):
            return {
                "ranked_categories": [entry["name"] for entry in entries],
                "useful_through_rank": 1,
            }

    selection = selection_module.select_relevant_categories(
        {"venue": "Venue", "year": 2026, "paper_count": 800, "category_summary": entries},
        finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion"),
        FakeLLM(),
    )

    assert selection["selected_paper_count"] == 800
    assert len(selection["selected_categories"]) == 4


def test_find_category_selection_falls_back_after_incomplete_llm_ranking(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")

    class IncompleteLLM:
        enabled = True
        calls = 0

        def json_or_none(self, *_args, **_kwargs):
            self.calls += 1
            return {"ranked_categories": ["Topic A"], "useful_through_rank": 1}

    llm = IncompleteLLM()
    selection = selection_module.select_relevant_categories(
        {
            "venue_id": "venue",
            "venue": "Venue",
            "year": 2026,
            "paper_count": 1200,
            "category_summary": [
                {"name": "Topic A", "count": 600},
                {"name": "Topic B", "count": 600},
            ],
        },
        finding_runtime.AppConfig(
            provider="openai_compatible",
            research_topic="protein diffusion",
            research_interest="reinforcement learning",
        ),
        llm,
    )

    assert llm.calls == 2
    assert selection["fallback_used"] is True
    assert selection["category_ranking_source"] == "deterministic_fallback"
    assert set(selection["ranked_categories"]) == {"Topic A", "Topic B"}
    assert selection["selected_paper_count"] == 1200
    assert "incomplete category ranking" in selection["llm_error"]


def test_find_category_selection_repairs_one_non_strict_llm_ranking(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")

    class RepairingLLM:
        enabled = True
        calls = 0

        def json_or_none(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"ranked_categories": ["Topic A ", "Unknown"], "useful_through_rank": 1}
            return {"ranked_categories": ["Topic A", "Topic B"], "useful_through_rank": 1}

    llm = RepairingLLM()
    selection = selection_module.select_relevant_categories(
        {
            "venue": "Venue",
            "year": 2026,
            "paper_count": 1200,
            "category_summary": [
                {"name": "Topic A", "count": 600},
                {"name": "Topic B", "count": 600},
            ],
        },
        finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion"),
        llm,
    )

    assert llm.calls == 2
    assert selection["ranked_categories"] == ["Topic A", "Topic B"]
    assert selection["fallback_used"] is False
    assert selection["category_ranking_source"] == "llm_repair"


def test_find_category_selection_uses_fallback_after_two_non_strict_llm_payloads(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")
    invalid_payloads = [
        {"ranked_category_ids": ["c001"], "useful_through_rank": 1},
        {"ranked_category_ids": ["c001", "c001"], "useful_through_rank": 1},
        {"ranked_category_ids": ["c001", "c999"], "useful_through_rank": 1},
        {"ranked_category_ids": ["c001", "c002"], "useful_through_rank": 1, "reason": "extra"},
        {"ranked_categories": ["Topic A", "Topic B"]},
        {"ranked_categories": ["Topic A", "Topic B"], "useful_through_rank": True},
        {"ranked_categories": ["Topic A", "Topic B"], "useful_through_rank": "1"},
        {"ranked_categories": ["Topic A", "Topic B"], "useful_through_rank": 3},
        {"ranked_categories": ["Topic A", "Topic A"], "useful_through_rank": 1},
        {"ranked_categories": ["Topic A", "Unknown"], "useful_through_rank": 1},
        {"ranked_categories": [{"name": "Topic A"}, "Topic B"], "useful_through_rank": 1},
        {"ranked_categories": ["Topic A", "Topic B"], "useful_through_rank": 1, "reason": "extra"},
    ]
    category_summary = {
        "venue": "Venue",
        "year": 2026,
        "paper_count": 1200,
        "category_summary": [
            {"name": "Topic A", "count": 600},
            {"name": "Topic B", "count": 600},
        ],
    }
    config = finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion")

    for payload in invalid_payloads:
        class InvalidLLM:
            enabled = True
            calls = 0

            def json_or_none(self, *_args, **_kwargs):
                self.calls += 1
                return payload

        llm = InvalidLLM()
        selection = selection_module.select_relevant_categories(category_summary, config, llm)
        assert llm.calls == 2
        assert selection["fallback_used"] is True
        assert selection["category_ranking_source"] == "deterministic_fallback"
        assert set(selection["ranked_categories"]) == {"Topic A", "Topic B"}
        assert selection["llm_error"]


def test_find_category_selection_canonicalizes_safe_name_formatting_without_retry(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")

    class WhitespaceLLM:
        enabled = True
        calls = 0

        def json_or_none(self, *_args, **_kwargs):
            self.calls += 1
            return {"ranked_categories": [" topic a ", "TOPIC B"], "useful_through_rank": 1}

    llm = WhitespaceLLM()
    selection = selection_module.select_relevant_categories(
        {
            "venue": "Venue",
            "year": 2026,
            "paper_count": 1200,
            "category_summary": [
                {"name": "Topic A", "count": 600},
                {"name": "Topic B", "count": 600},
            ],
        },
        finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion"),
        llm,
    )

    assert llm.calls == 1
    assert selection["ranked_categories"] == ["Topic A", "Topic B"]
    assert selection["fallback_used"] is False


def test_find_category_selection_repairs_unparseable_first_response(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")

    class ParseRepairLLM:
        enabled = True
        calls = 0

        def json_or_none(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return None
            return {"ranked_category_ids": ["c002", "c001"], "useful_through_rank": 1}

    llm = ParseRepairLLM()
    selection = selection_module.select_relevant_categories(
        {
            "venue": "Venue",
            "year": 2026,
            "paper_count": 1200,
            "category_summary": [
                {"name": "Topic A", "count": 600},
                {"name": "Topic B", "count": 600},
            ],
        },
        finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion"),
        llm,
    )

    assert llm.calls == 2
    assert selection["ranked_categories"] == ["Topic B", "Topic A"]
    assert selection["category_ranking_source"] == "llm_repair"


def test_find_category_selection_uses_deterministic_fallback_without_llm(monkeypatch):
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    finding_runtime = finding_main._private_import("finding_runtime")
    monkeypatch.setenv("USE_LLM_CATEGORY_SELECT", "1")
    monkeypatch.setenv("DISABLE_FIND_CATEGORY_SELECT_CACHE", "1")

    class DisabledLLM:
        enabled = False

    selection = selection_module.select_relevant_categories(
        {
            "venue": "Venue",
            "year": 2026,
            "paper_count": 1200,
            "category_summary": [
                {"name": "Topic A", "count": 600},
                {"name": "Topic B", "count": 600},
            ],
        },
        finding_runtime.AppConfig(provider="openai_compatible", research_topic="protein diffusion"),
        DisabledLLM(),
    )

    assert selection["fallback_used"] is True
    assert selection["category_ranking_source"] == "deterministic_fallback"
    assert selection["selected_paper_count"] == 1200


def test_find_category_selection_cache_recomputes_prefix_from_cutoff():
    finding_main = _load_finding_main()
    selection_module = finding_main._private_import("selection")
    entries = [
        {"name": "Topic A", "count": 600},
        {"name": "Topic B", "count": 600},
        {"name": "Topic C", "count": 100},
    ]
    cached = {
        "schema": selection_module.CATEGORY_SELECT_CACHE_SCHEMA_VERSION,
        "ranked_categories": ["Topic A", "Topic B", "Topic C"],
        "useful_through_rank": 1,
        "selected_categories": [{"name": "Topic C", "reason": "stale"}],
        "rejected_categories": [],
    }

    selection = selection_module._valid_cached_selection(cached, entries, 3)

    assert selection is not None
    assert [row["name"] for row in selection["selected_categories"]] == ["Topic A", "Topic B"]
    assert selection["useful_through_rank"] == 1
    assert selection["useful_category_paper_count"] == 600
    assert selection["selected_paper_count"] == 1200


def test_find_icml_presentation_labels_are_not_topic_categories():
    find_pipeline = _load_find_pipeline()
    poster = {
        "id": "poster",
        "venue": "ICML",
        "year": 2026,
        "primary_area": "Poster",
        "category": "Poster",
        "track": "Poster",
        "classification_source": "llm_inferred",
        "metadata": {"presentation_type": "poster"},
    }
    topical = {
        "id": "topic",
        "venue": "ICML",
        "year": 2026,
        "primary_area": "Applications->Chemistry, Physics, and Earth Sciences",
        "category": "Applications->Chemistry, Physics, and Earth Sciences",
        "track": "Spotlight",
        "classification_source": "official",
        "metadata": {"presentation_type": "spotlight", "category_status": "official_or_cached_categories"},
    }

    find_pipeline._sanitize_presentation_category_row(poster)
    find_pipeline._sanitize_presentation_category_row(topical)
    summary = find_pipeline._category_summary_from_title_index(
        {"id": "dblp_icml", "name": "ICML"},
        [2026],
        [poster, topical],
    )

    assert poster["category"] == poster["primary_area"] == ""
    assert poster["track"] == "Poster"
    assert [row["name"] for row in summary["category_summary"]] == [
        "Applications->Chemistry, Physics, and Earth Sciences"
    ]


def test_find_track_session_and_issue_labels_are_not_topic_categories():
    find_pipeline = _load_find_pipeline()
    fixtures = [
        ("aaai_ojs", "Vol. 40 No. 1: AAAI-26 Technical Tracks 1"),
        ("cikm_official_proceedings", "SESSION: Full Research Papers"),
        ("www_official_accepted", "Short Papers"),
        ("sigir_official_accepted", "Demo Papers"),
    ]
    rows = []
    for index, (source, label) in enumerate(fixtures):
        rows.append({
            "id": f"paper-{index}",
            "source": source,
            "title": f"Paper {index}",
            "venue": source,
            "year": 2026,
            "category": label,
            "track": label,
            "classification_source": "official",
            "metadata": {
                "venue_metadata_audit": {
                    "adapter": source,
                    "has_official_categories": True,
                    "category_status": "official_or_cached_categories",
                },
            },
        })

    sanitized = find_pipeline._sanitize_venue_title_index_rows(rows)
    summary = find_pipeline._category_summary_from_title_index(
        {"id": "mixed", "name": "Mixed"},
        [2026],
        sanitized,
    )

    assert summary["category_summary"] == []
    assert all(not row.get("category") and not row.get("primary_area") for row in sanitized)
    assert all(row.get("classification_source") == "official_track" for row in sanitized)
    assert [row.get("track") for row in sanitized] == [label for _source, label in fixtures]
    assert all(
        row["metadata"]["venue_metadata_audit"]["category_status"] == "no_official_categories"
        for row in sanitized
    )


def test_find_cache_store_keeps_official_neurips_index_over_smaller_dblp(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    _isolate_find_runtime(monkeypatch, find_pipeline, workspace / "runtime")
    venue = {"id": "ccf_ai_conference_a_neurips_conference_on_neural_information_processing_systems", "name": "NeurIPS"}

    official_audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "neurips_official_papers",
        "paper_count": 120,
        "missing_abstract_count": 0,
        "has_abstracts": True,
        "any_abstracts": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "source_scope": "official_neurips_papers_index",
        "official_title_index_verified": True,
        "official_accepted_list_verified": True,
    }
    dblp_audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "dblp_toc",
        "paper_count": 80,
        "has_abstracts": False,
        "any_abstracts": False,
        "has_official_categories": False,
        "category_status": "no_official_categories",
        "source_scope": "dblp_current_index_not_official_accepted_list",
        "official_title_index_verified": False,
        "official_accepted_list_verified": False,
    }
    official_rows = _venue_cache_rows(120, official_audit, with_abstract=True)
    dblp_rows = _venue_cache_rows(80, dblp_audit)
    for row in official_rows + dblp_rows:
        row["venue"] = "NeurIPS"

    find_pipeline._store_venue_title_index_cache(venue, [2025], official_rows, "neurips_official_papers")
    find_pipeline._store_venue_title_index_cache(venue, [2025], dblp_rows, "dblp")
    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2025], limit=None)

    assert adapter == "neurips_official_papers_cache"
    assert len(loaded) == 120


def test_find_title_index_cache_validates_full_rows_before_limit(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    runtime = workspace / "runtime"
    state_dir = runtime / "state"
    state_dir.mkdir(parents=True)
    _isolate_find_runtime(monkeypatch, find_pipeline, runtime)

    venue = {"id": "openreview_iclr", "name": "ICLR"}
    audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "openreview_reference",
        "paper_count": 80,
        "has_abstracts": True,
        "any_abstracts": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "source_scope": "official_openreview_metadata",
        "official_title_index_verified": True,
        "official_accepted_list_verified": True,
    }
    rows = _venue_cache_rows(80, audit, with_abstract=True)
    cache = {
        "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
        "entries": {
            find_pipeline._venue_title_index_cache_key(venue, [2026]): {
                "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
                "venue_id": "openreview_iclr",
                "venue_name": "ICLR",
                "years": [2026],
                "adapter": "openreview_reference",
                "papers": rows,
                "count": len(rows),
            }
        },
    }
    (state_dir / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")

    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2026], limit=1)

    assert adapter == "openreview_reference_cache"
    assert len(loaded) == 1
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows, "openreview_reference") is True
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows[:1], "openreview_reference") is False


def test_find_title_index_cache_discovers_private_runtime_dblp_title_corpus(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    runtime = workspace / "runtime"
    state_dir = runtime / "state"
    state_dir.mkdir(parents=True)
    _isolate_find_runtime(monkeypatch, find_pipeline, runtime)

    venue = {"id": "ccf_dm_cs_conference_a_sigkdd_acm_sigkdd_conference_on_knowledge_discovery_and_data_mining", "name": "SIGKDD"}
    audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "dblp_search_api",
        "paper_count": 70,
        "has_abstracts": False,
        "any_abstracts": False,
        "has_official_categories": False,
        "category_status": "no_official_categories",
        "source_scope": "dblp_current_index_not_official_accepted_list",
        "official_title_index_verified": False,
        "official_accepted_list_verified": False,
    }
    rows = _venue_cache_rows(70, audit)
    for row in rows:
        row["venue"] = "SIGKDD"
    cache = {
        "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
        "entries": {
            find_pipeline._venue_title_index_cache_key(venue, [2026]): {
                "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
                "venue_id": venue["id"],
                "venue_name": "SIGKDD",
                "years": [2026],
                "adapter": "dblp",
                "papers": rows,
                "count": len(rows),
            }
        },
    }
    (state_dir / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")

    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2026], limit=1)
    audit = find_pipeline._online_venue_metadata_audit(rows, "dblp")
    audit["venue_id"] = venue["id"]
    audit["venue"] = venue["name"]
    fields = find_pipeline._venue_metadata_status_fields(audit)

    assert adapter == "none"
    assert loaded == []
    assert fields["source_integrity_status"] == "passed"
    assert fields["metadata_completeness_status"] == "abstract_incomplete"
    assert fields["official_title_index_verified"] is False
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows, "dblp") is False


def test_find_title_index_cache_refreshes_weak_cache_for_official_venues(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    runtime = tmp_path / "runtime"
    (runtime / "state").mkdir(parents=True)
    _isolate_find_runtime(monkeypatch, find_pipeline, runtime)

    venue = {"id": "openreview_iclr", "name": "ICLR", "full_name": "International Conference on Learning Representations"}
    weak_audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "dblp_search_api",
        "paper_count": 70,
        "has_abstracts": False,
        "any_abstracts": False,
        "has_official_categories": False,
        "category_status": "no_official_categories",
        "source_scope": "dblp_current_index_not_official_accepted_list",
        "official_title_index_verified": False,
        "official_accepted_list_verified": False,
    }
    strong_audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "openreview_reference",
        "paper_count": 120,
        "has_abstracts": True,
        "any_abstracts": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "source_scope": "official_openreview_metadata",
        "official_title_index_verified": True,
        "official_accepted_list_verified": True,
    }
    weak_rows = _venue_cache_rows(70, weak_audit)
    strong_rows = _venue_cache_rows(120, strong_audit, with_abstract=True)
    for row in weak_rows + strong_rows:
        row["venue"] = "ICLR"

    find_pipeline._store_venue_title_index_cache(venue, [2026], weak_rows, "dblp")
    assert find_pipeline._load_venue_title_index_cache(venue, [2026], limit=None, require_strong=True) == ([], "none")

    calls = {"online": 0}

    def fake_fetch_all(fetch_venue, years):
        calls["online"] += 1
        assert fetch_venue == venue
        assert years == [2026]
        return strong_rows, "openreview_reference"

    monkeypatch.setattr(find_pipeline, "fetch_venue_title_index_all", fake_fetch_all)

    refreshed, refreshed_adapter = find_pipeline._fetch_venue_title_index_for_find(venue, [2026], None, timeout_sec=0)
    assert calls["online"] == 1
    assert refreshed_adapter == "openreview_reference"
    assert len(refreshed) == 120

    reused, reused_adapter = find_pipeline._fetch_venue_title_index_for_find(venue, [2026], None, timeout_sec=0)
    assert calls["online"] == 1
    assert reused_adapter == "openreview_reference_cache"
    assert len(reused) == 120


def test_find_neurips_year_probe_stops_after_one_official_title(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class Response:
        text = "<html></html>"

    row = {
        "id": "neurips-2025-probe",
        "source": "neurips_official_papers",
        "title": "NeurIPS availability probe paper",
        "authors": "",
        "abstract": "",
        "url": "https://papers.nips.cc/paper_files/paper/2025/hash/probe-Abstract-Conference.html",
        "pdf_url": "",
        "venue": "NeurIPS",
        "year": 2025,
        "category": "",
        "classification_source": "llm_inferred",
        "metadata": {},
    }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("one-paper availability probe continued into another source")

    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(find_support, "_parse_neurips_official_papers_list", lambda *_args, **_kwargs: [dict(row)])
    monkeypatch.setattr(find_support, "_enrich_neurips_official_with_virtual_presentations", unexpected)
    monkeypatch.setattr(find_support, "fetch_openreview_venue", unexpected)
    monkeypatch.setattr(find_support, "fetch_dblp_venue", unexpected)

    papers, adapter = find_support.fetch_venue_title_index(
        {"id": "openreview_neurips", "name": "NeurIPS", "address": "https://dblp.org/db/conf/nips/"},
        [2025],
        1,
    )

    assert [paper["title"] for paper in papers] == ["NeurIPS availability probe paper"]
    assert adapter == "neurips_virtual"


def test_find_neurips_official_index_returns_before_optional_presentation_lookup(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class Response:
        text = "<html></html>"

    row = {
        "id": "neurips-2025-official",
        "source": "neurips_official_papers",
        "title": "NeurIPS official index paper",
        "authors": "",
        "abstract": "",
        "url": "https://papers.nips.cc/paper_files/paper/2025/hash/official-Abstract-Conference.html",
        "pdf_url": "",
        "venue": "NeurIPS",
        "year": 2025,
        "category": "",
        "classification_source": "llm_inferred",
        "metadata": {},
    }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("valid official titles waited for optional presentation metadata")

    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(find_support, "_parse_neurips_official_papers_list", lambda *_args, **_kwargs: [dict(row)])
    monkeypatch.setattr(find_support, "_enrich_neurips_official_with_virtual_presentations", unexpected)

    papers = find_support.fetch_neurips_title_index(2025, 100000)

    assert [paper["title"] for paper in papers] == ["NeurIPS official index paper"]
    assert papers[0]["metadata"]["venue_metadata_audit"]["official_title_index_verified"] is True


def test_find_neurips_full_fetch_keeps_virtual_rows_after_official_tls_failure(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class Response:
        text = "<html></html>"

    def request(url, *_args, **_kwargs):
        if "papers.nips.cc" in url:
            raise OSError("TLS handshake failed")
        if url == "https://neurips.cc/virtual/2025/papers.html":
            return Response()
        raise AssertionError(f"NeurIPS fetch continued into an unnecessary source: {url}")

    def parse_virtual(_html, page_url, _limit):
        return [(f"{page_url}#paper-1", "Recovered NeurIPS virtual paper")]

    def unexpected(*_args, **_kwargs):
        raise AssertionError("NeurIPS virtual rows were discarded in favor of a slower fallback")

    monkeypatch.setattr(find_support, "_request", request)
    monkeypatch.setattr(find_support, "_parse_neurips_list", parse_virtual)
    monkeypatch.setattr(find_support, "fetch_openreview_venue", unexpected)
    monkeypatch.setattr(find_support, "fetch_dblp_venue", unexpected)

    papers, adapter = find_support.fetch_venue_title_index_all(
        {"id": "openreview_neurips", "name": "NeurIPS", "address": "https://dblp.org/db/conf/nips/"},
        [2025],
    )

    assert adapter == "neurips_virtual"
    assert [paper["title"] for paper in papers] == ["Recovered NeurIPS virtual paper"]
    audit = papers[0]["metadata"]["venue_metadata_audit"]
    assert audit["official_title_index_verified"] is True
    assert "TLS handshake failed" in papers[0]["metadata"]["official_papers_error"]


def test_find_icml_2026_prefers_fast_guide_before_large_official_downloads(monkeypatch):
    _load_find_pipeline()
    find_sources = sys.modules["sources"]
    guide_rows = [{"id": "icml-guide", "title": "ICML guide paper", "year": 2026, "venue": "ICML"}]

    monkeypatch.setattr(find_sources, "_icml2026_guide_papers", lambda _max_items: guide_rows)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("official ICML static download ran despite an available guide corpus")

    monkeypatch.setattr(find_sources, "_load_json_url_with_cache", unexpected)

    assert find_sources.fetch_icml_official_virtual_2026(100000) == guide_rows


def test_find_priority_venue_year_probes_stop_at_first_live_source(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    catalog = find_support.catalog_by_id()
    cases = [
        ("openreview_neurips", 2025, "neurips_virtual"),
        ("openreview_iclr", 2026, "openreview_reference"),
        ("dblp_icml", 2026, "icml_official_virtual"),
        ("dblp_kdd", 2026, "dblp"),
        ("dblp_www", 2026, "www_official_accepted"),
        ("dblp_sigir", 2025, "sigir_official_proceedings"),
        ("dblp_cikm", 2025, "cikm_official_proceedings"),
        ("dblp_aaai", 2026, "aaai_ojs"),
        ("dblp_iccv", 2025, "cvf_openaccess"),
        ("dblp_cvpr", 2026, "cvf_openaccess"),
        ("dblp_acl", 2026, "acl_anthology"),
        ("dblp_ijcai", 2025, "ijcai_proceedings"),
        ("dblp_eccv", 2024, "eccv_virtual"),
        ("dblp_emnlp", 2025, "acl_anthology"),
    ]

    def row(venue_id, year, source):
        venue = catalog[venue_id]
        return [{
            "id": f"{venue_id}-{year}",
            "source": source,
            "title": f"{venue.get('name')} live probe paper",
            "abstract": "",
            "url": f"https://example.test/{venue_id}/{year}",
            "venue": venue.get("name"),
            "year": year,
            "metadata": {},
        }]

    monkeypatch.setattr(find_support, "fetch_openreview_iclr_2026", lambda _limit: row("openreview_iclr", 2026, "openreview"))
    monkeypatch.setattr(find_support, "fetch_neurips_title_index", lambda year, _limit: row("openreview_neurips", year, "neurips_official_papers"))
    monkeypatch.setattr(find_support, "fetch_icml_official_virtual_2026", lambda _limit: row("dblp_icml", 2026, "icml_official_virtual"))
    monkeypatch.setattr(find_support, "fetch_acl_anthology", lambda venue, years, _limit: row(venue["id"], years[0], "acl_anthology") if venue["id"] in {"dblp_acl", "dblp_emnlp"} else [])
    monkeypatch.setattr(find_support, "fetch_cikm_official_proceedings", lambda venue, years, _limit: row(venue["id"], years[0], "cikm_official_proceedings") if venue["id"] == "dblp_cikm" else [])
    monkeypatch.setattr(find_support, "fetch_www_official_accepted", lambda venue, years, _limit: row(venue["id"], years[0], "www_official_accepted") if venue["id"] == "dblp_www" else [])
    monkeypatch.setattr(find_support, "fetch_sigir_official_proceedings", lambda venue, years, _limit: row(venue["id"], years[0], "sigir_official_proceedings") if venue["id"] == "dblp_sigir" else [])
    monkeypatch.setattr(find_support, "fetch_aaai_ojs", lambda venue, years, _limit: row(venue["id"], years[0], "aaai_ojs") if venue["id"] == "dblp_aaai" else [])
    monkeypatch.setattr(find_support, "fetch_ijcai_proceedings", lambda venue, years, _limit: row(venue["id"], years[0], "ijcai_proceedings") if venue["id"] == "dblp_ijcai" else [])
    def cvf_only(venue, years, _limit):
        if venue["id"] == "dblp_eccv":
            raise AssertionError("ECCV probe used the unrelated CVF Open Access adapter")
        return row(venue["id"], years[0], "cvf_openaccess") if venue["id"] in {"dblp_iccv", "dblp_cvpr"} else []

    monkeypatch.setattr(find_support, "fetch_cvf_openaccess", cvf_only)
    monkeypatch.setattr(find_support, "fetch_eccv_virtual", lambda years, _limit: row("dblp_eccv", years[0], "eccv_virtual"))

    def dblp_only_for_kdd(venue, years, _limit):
        if venue["id"] != "dblp_kdd":
            raise AssertionError(f"{venue['id']} availability probe continued into DBLP")
        return row(venue["id"], years[0], "dblp")

    monkeypatch.setattr(find_support, "fetch_dblp_venue", dblp_only_for_kdd)
    monkeypatch.setattr(find_support, "fetch_icml_downloads", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ICML probe did not stop")))
    monkeypatch.setattr(find_support, "fetch_sigir_official_accepted", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SIGIR probe did not stop")))
    monkeypatch.setattr(find_support, "fetch_openreview_venue", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("official probe continued into OpenReview fallback")))

    for venue_id, year, expected_adapter in cases:
        papers, adapter = find_support.fetch_venue_title_index(catalog[venue_id], [year], 1)
        assert len(papers) == 1, venue_id
        assert adapter == expected_adapter, venue_id


def test_find_acl_anthology_uses_official_xml_and_filters_findings_volume(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    main_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <collection id="2025.emnlp"><volume id="main"><meta><booktitle>EMNLP 2025</booktitle></meta>
      <paper id="1"><title>Main <fixed-case>LLM</fixed-case> Paper</title>
        <author><first>Ada</first><last>Lovelace</last></author>
        <abstract>Main official abstract.</abstract><url>2025.emnlp-main.1</url><doi>10.18653/v1/test</doi>
      </paper>
    </volume></collection>"""
    findings_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <collection id="2025.findings">
      <volume id="acl"><meta><booktitle>Findings ACL</booktitle></meta>
        <paper id="1"><title>Unrelated ACL Findings Paper</title><abstract>Wrong venue.</abstract><url>2025.findings-acl.1</url></paper>
      </volume>
      <volume id="emnlp"><meta><booktitle>Findings EMNLP</booktitle></meta>
        <paper id="2"><title>Relevant EMNLP Findings Paper</title><abstract>Findings abstract.</abstract><url>2025.findings-emnlp.2</url></paper>
      </volume>
    </collection>"""

    class Response:
        def __init__(self, text):
            self.text = text

    def request(url, **_kwargs):
        if url.endswith("2025.emnlp.xml"):
            return Response(main_xml)
        if url.endswith("2025.findings.xml"):
            return Response(findings_xml)
        raise AssertionError(f"unexpected ACL source: {url}")

    monkeypatch.setattr(find_support, "_request", request)
    papers = find_support.fetch_acl_anthology({"id": "dblp_emnlp", "name": "EMNLP"}, [2025], 10)

    assert [paper["title"] for paper in papers] == ["Main LLM Paper", "Relevant EMNLP Findings Paper"]
    assert papers[0]["authors"] == "Ada Lovelace"
    assert papers[0]["abstract"] == "Main official abstract."
    assert papers[0]["metadata"]["abstract_source"] == "acl_anthology_xml"
    assert all("Unrelated ACL" not in paper["title"] for paper in papers)


def test_find_acl_anthology_xml_does_not_treat_other_findings_as_emnlp(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    findings_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <collection id="2026.findings"><volume id="acl"><meta><booktitle>Findings ACL</booktitle></meta>
      <paper id="1"><title>ACL Findings Paper Only</title><abstract>Not EMNLP.</abstract><url>2026.findings-acl.1</url></paper>
    </volume></collection>"""

    class Response:
        def __init__(self, text="", status_code=200):
            self.text = text
            self.status_code = status_code

    class NotFound(Exception):
        def __init__(self):
            self.response = Response(status_code=404)

    def request(url, **_kwargs):
        if url.endswith("2026.emnlp.xml"):
            raise NotFound()
        if url.endswith("2026.findings.xml"):
            return Response(findings_xml)
        raise AssertionError(f"slow event-page fallback ran after an authoritative XML check: {url}")

    monkeypatch.setattr(find_support, "_request", request)
    assert find_support.fetch_acl_anthology({"id": "dblp_emnlp", "name": "EMNLP"}, [2026], 1) == []


def test_find_future_release_date_keeps_live_requested_year_without_cache(monkeypatch):
    find_pipeline = _load_find_pipeline()
    venue = find_pipeline.catalog_by_id()["dblp_kdd"]
    calls = []
    monkeypatch.delenv("FIND_VENUE_YEAR_FULL_RELEASE_PROBE", raising=False)
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: None)

    def live_probe(_venue, years, limit, **_kwargs):
        calls.append((list(years), limit))
        assert years == [2026]
        return [{"title": "Live KDD 2026 paper", "year": 2026}], "dblp"

    monkeypatch.setattr(find_pipeline, "_fetch_venue_title_index_for_find", live_probe)
    resolved, reason = find_pipeline._resolve_venue_years(venue, [2026], as_of=date(2026, 7, 18))

    assert resolved == [2026]
    assert reason == ""
    assert calls == [([2026], 1)]


@pytest.mark.parametrize(
    ("venue_id", "failure_mode", "expected_detail"),
    [
        ("openreview_neurips", "timeout", "wall timeout after 30s"),
        ("dblp_cikm", "exception", "temporary DNS failure"),
    ],
)
def test_find_transient_year_probe_failure_does_not_skip_to_an_older_year(
    monkeypatch, venue_id, failure_mode, expected_detail
):
    find_pipeline = _load_find_pipeline()
    venue = find_pipeline.catalog_by_id()[venue_id]
    calls = []
    monkeypatch.delenv("FIND_VENUE_YEAR_FULL_RELEASE_PROBE", raising=False)
    monkeypatch.delenv("FIND_VENUE_YEAR_PROBE_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("VENUE_YEAR_PROBE_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: None)

    def live_probe(_venue, years, limit, **_kwargs):
        year = years[0]
        calls.append((year, limit))
        if year == 2026:
            return [], "none"
        if year == 2025 and failure_mode == "timeout":
            return [], "timeout"
        if year == 2025:
            raise OSError("temporary DNS failure")
        raise AssertionError(f"transient 2025 failure incorrectly skipped to {year}")

    monkeypatch.setattr(find_pipeline, "_fetch_venue_title_index_for_find", live_probe)
    resolved, reason = find_pipeline._resolve_venue_years(venue, [2026], as_of=date(2026, 7, 18))

    assert resolved == [2025]
    assert calls == [(2026, 1), (2025, 1)]
    assert "2025 year availability probe failed transiently" in reason
    assert expected_detail in reason
    assert "retaining year 2025" in reason
    assert "not backfilling further" in reason


def test_find_transient_requested_year_probe_failure_keeps_requested_year(monkeypatch):
    find_pipeline = _load_find_pipeline()
    venue = find_pipeline.catalog_by_id()["dblp_kdd"]
    calls = []
    monkeypatch.delenv("FIND_VENUE_YEAR_FULL_RELEASE_PROBE", raising=False)
    monkeypatch.delenv("FIND_VENUE_YEAR_PROBE_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("VENUE_YEAR_PROBE_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: None)

    def timed_out(_venue, years, limit, **_kwargs):
        calls.append((years[0], limit))
        return [], "timeout"

    monkeypatch.setattr(find_pipeline, "_fetch_venue_title_index_for_find", timed_out)
    resolved, reason = find_pipeline._resolve_venue_years(venue, [2026], as_of=date(2026, 7, 18))

    assert resolved == [2026]
    assert calls == [(2026, 1)]
    assert "2026 year availability probe failed transiently" in reason
    assert "retaining requested year 2026" in reason
    assert "not backfilling to an older year" in reason


def test_find_empty_released_year_probe_is_not_treated_as_authoritative_absence(monkeypatch):
    find_pipeline = _load_find_pipeline()
    venue = find_pipeline.catalog_by_id()["dblp_cikm"]
    calls = []
    monkeypatch.delenv("FIND_VENUE_YEAR_FULL_RELEASE_PROBE", raising=False)
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: None)

    def empty_probe(_venue, years, limit, **_kwargs):
        calls.append((years[0], limit))
        if years[0] < 2025:
            raise AssertionError("an unverified empty 2025 response incorrectly triggered an older fallback")
        return [], "none"

    monkeypatch.setattr(find_pipeline, "_fetch_venue_title_index_for_find", empty_probe)
    resolved, reason = find_pipeline._resolve_venue_years(venue, [2026], as_of=date(2026, 7, 18))

    assert resolved == [2025]
    assert calls == [(2026, 1), (2025, 1)]
    assert "2025 year availability probe returned no title rows without an authoritative absence signal" in reason
    assert "retaining year 2025" in reason


def test_find_empty_current_year_probe_records_unverified_absence(monkeypatch):
    find_pipeline = _load_find_pipeline()
    venue = find_pipeline.catalog_by_id()["dblp_aaai"]
    calls = []
    monkeypatch.delenv("FIND_VENUE_YEAR_FULL_RELEASE_PROBE", raising=False)
    monkeypatch.setattr(find_pipeline, "load_local_venue_year", lambda *_args, **_kwargs: None)

    def empty_probe(_venue, years, limit, **_kwargs):
        calls.append((years[0], limit))
        return [], "none"

    monkeypatch.setattr(find_pipeline, "_fetch_venue_title_index_for_find", empty_probe)
    resolved, reason = find_pipeline._resolve_venue_years(venue, [2026], as_of=date(2026, 7, 18))

    assert resolved == [2026]
    assert calls == [(2026, 1)]
    assert "2026 year availability probe returned no title rows without an authoritative absence signal" in reason
    assert "retaining requested year 2026" in reason


def test_find_dblp_one_row_probe_skips_full_toc_fetch(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    row = {"title": "DBLP live probe", "year": 2026}
    monkeypatch.setattr(find_support, "fetch_dblp_stream_api", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(find_support, "_dblp_toc_papers", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("one-row DBLP probe fetched the full TOC")))

    assert find_support.fetch_dblp_venue({"id": "dblp_kdd"}, [2026], 1) == [row]


def test_find_venue_health_requires_live_abstract_enrichment(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    row = {"title": "Live metadata paper", "abstract": "", "url": "https://example.test/paper"}
    monkeypatch.setattr(find_support, "fetch_venue_title_index", lambda *_args, **_kwargs: ([dict(row)], "official_test"))

    def enrich(items, **kwargs):
        assert kwargs["wall_timeout_sec"] == 30.0
        items[0]["abstract"] = "A live detail-page abstract with enough metadata evidence."
        return items

    monkeypatch.setattr(find_support, "fetch_selected_venue_details", enrich)
    ready = find_support.fetch_venue_sample({"id": "test", "name": "Test"}, 2026, 1)
    assert ready["ok"] is True
    assert ready["source_adapter"] == "official_test"
    assert ready["samples"][0]["abstract"]

    monkeypatch.setattr(find_support, "fetch_selected_venue_details", lambda items, **_kwargs: items)
    missing = find_support.fetch_venue_sample({"id": "test", "name": "Test"}, 2026, 1)
    assert missing["ok"] is False
    assert "still lack abstracts" in missing["message"]


def test_find_acm_live_defaults_use_targeted_fallbacks_not_full_venue_scan(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    for name in ("ACM_CHATPAPER_FALLBACK", "ACM_OPENALEX_TITLE_FALLBACK", "ACM_SEMANTIC_SCHOLAR_FALLBACK"):
        monkeypatch.delenv(name, raising=False)
    papers = [
        {"title": "OpenAlex title match", "doi": "10.1145/1.1", "abstract": "", "metadata": {"doi": "10.1145/1.1"}},
        {"title": "Semantic title match", "doi": "10.1145/1.2", "abstract": "", "metadata": {"doi": "10.1145/1.2"}},
    ]
    calls = {"openalex_title": 0, "semantic": 0}
    empty_stats = {"attempted": 0, "abstracts_filled": 0}
    monkeypatch.setattr(find_support, "_apply_cached_acm_abstract_sources", lambda items: (items, dict(empty_stats)))
    monkeypatch.setattr(find_support, "enrich_acm_doi_with_hal", lambda items, **_kwargs: (items, dict(empty_stats)))
    monkeypatch.setattr(find_support, "enrich_acm_doi_with_chatpaper", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full-venue ChatPaper scan ran by default")))
    monkeypatch.setattr(find_support, "enrich_acm_doi_with_openalex", lambda items, **_kwargs: (items, dict(empty_stats)))
    monkeypatch.setattr(find_support, "enrich_acm_doi_with_openalex_oa_pdf", lambda items, **_kwargs: (items, dict(empty_stats)))
    monkeypatch.setattr(find_support, "enrich_acm_doi_with_official_pdf", lambda items, **_kwargs: (items, dict(empty_stats)))

    def openalex_title(items, **_kwargs):
        calls["openalex_title"] += 1
        items[0]["abstract"] = "OpenAlex title-matched abstract."
        items[0].setdefault("metadata", {})["openalex_id"] = "W1"
        return items

    def semantic(items, **_kwargs):
        calls["semantic"] += 1
        for item in items:
            item["abstract"] = "Semantic Scholar DOI-matched abstract."
            item.setdefault("metadata", {})["abstract_source"] = "semantic_scholar_doi"
        return items

    monkeypatch.setattr(find_support, "enrich_with_openalex", openalex_title)
    monkeypatch.setattr(find_support, "enrich_with_semantic_scholar", semantic)

    enriched, _stats = find_support.enrich_acm_doi_with_indexed_abstracts(papers)
    assert calls == {"openalex_title": 1, "semantic": 1}
    assert all(paper["abstract"] for paper in enriched)


def test_find_priority_live_audit_enriches_details_without_cache(monkeypatch, tmp_path):
    finding_main = _load_finding_main()
    audit_module = finding_main._private_import("cache.audit_priority_venue_metadata")
    venue = {"id": "openreview_iclr", "name": "ICLR"}
    rows = [{"id": "paper", "source": "openreview", "title": "Live audit paper", "abstract": "", "year": 2026, "venue": "ICLR", "metadata": {}}]
    calls = {"details": 0}

    def metadata_audit(items):
        complete = bool(items and all(item.get("abstract") for item in items))
        return {
            "status": "complete",
            "source_verified": True,
            "complete": True,
            "title_index_complete": True,
            "official_metadata_complete": complete,
            "adapter": "openreview_reference",
            "paper_count": len(items),
            "missing_abstract_count": sum(1 for item in items if not item.get("abstract")),
            "has_abstracts": complete,
            "any_abstracts": complete,
            "has_official_categories": True,
            "category_status": "official_or_cached_categories",
            "source_scope": "official_openreview_metadata",
            "official_title_index_verified": True,
            "official_accepted_list_verified": True,
            "venue_id": "openreview_iclr",
            "venue": "ICLR",
        }

    def enrich(items, **kwargs):
        calls["details"] += 1
        assert kwargs["wall_timeout_sec"] == 0.0
        items[0]["abstract"] = "Live official detail abstract."
        return items

    monkeypatch.setenv("ACM_PRIORITY_AUDIT_CACHE_ONLY", "1")
    monkeypatch.setattr(audit_module, "catalog_by_id", lambda: {"openreview_iclr": venue})
    monkeypatch.setattr(audit_module, "fetch_venue_title_index_all", lambda *_args, **_kwargs: (rows, "openreview_reference"))
    monkeypatch.setattr(audit_module, "_apply_cached_acm_abstract_sources", lambda items: (items, {}))
    monkeypatch.setattr(audit_module, "fetch_selected_venue_details", enrich)
    monkeypatch.setattr(audit_module, "venue_metadata_audit_from_papers", metadata_audit)

    result = audit_module._audit_one(
        "openreview_iclr",
        2026,
        output_root=tmp_path / "empty-cache",
        write_cache=False,
        as_of=date(2026, 7, 18),
        max_backfill_years=3,
    )

    assert calls["details"] == 1
    assert result["ok"] is True
    assert result["paper_count"] == 1


def test_find_title_index_cache_reuses_verified_dblp_for_dblp_only_venue(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    runtime = tmp_path / "runtime"
    (runtime / "state").mkdir(parents=True)
    _isolate_find_runtime(monkeypatch, find_pipeline, runtime)

    venue = {"id": "dblp_kdd", "name": "SIGKDD", "full_name": "ACM SIGKDD Conference on Knowledge Discovery and Data Mining"}
    audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "dblp_search_api",
        "paper_count": 80,
        "has_abstracts": False,
        "any_abstracts": False,
        "has_official_categories": False,
        "category_status": "no_official_categories",
        "source_scope": "dblp_current_index_not_official_accepted_list",
        "official_title_index_verified": False,
        "official_accepted_list_verified": False,
    }
    rows = _venue_cache_rows(80, audit, with_abstract=True)
    for row in rows:
        row["venue"] = "SIGKDD"
    find_pipeline._store_venue_title_index_cache(venue, [2026], rows, "dblp")

    calls = {"online": 0}

    def fake_fetch_all(_venue, _years):
        calls["online"] += 1
        return rows, "dblp"

    monkeypatch.setattr(find_pipeline, "fetch_venue_title_index_all", fake_fetch_all)

    loaded, adapter = find_pipeline._fetch_venue_title_index_for_find(venue, [2026], None, timeout_sec=0)
    assert calls["online"] == 1
    assert adapter == "dblp"
    assert len(loaded) == 80


def test_find_source_health_refresh_replaces_stale_one_row_source_artifact(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    runtime = workspace / "runtime"
    state_dir = runtime / "state"
    state_dir.mkdir(parents=True)
    artifact_dir = workspace / "projects" / "protein" / "planning" / "finding"
    artifact_dir.mkdir(parents=True)
    _isolate_find_runtime(monkeypatch, find_pipeline, runtime)

    venue = {"id": "openreview_iclr", "name": "ICLR"}
    selection = {
        "venue_ids": ["openreview_iclr"],
        "years": [2026],
        "venue_years": [{"venue_id": "openreview_iclr", "year": 2026}],
        "include_arxiv": False,
        "include_biorxiv": False,
        "include_huggingface": False,
        "include_github": False,
        "include_nature": False,
        "include_science": False,
    }
    complete_audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "openreview_reference",
        "paper_count": 80,
        "has_abstracts": True,
        "any_abstracts": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "source_scope": "official_openreview_metadata",
        "official_title_index_verified": True,
        "official_accepted_list_verified": True,
    }
    rows = _venue_cache_rows(80, complete_audit, with_abstract=True)
    cache = {
        "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
        "entries": {
            find_pipeline._venue_title_index_cache_key(venue, [2026]): {
                "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
                "venue_id": "openreview_iclr",
                "venue_name": "ICLR",
                "years": [2026],
                "adapter": "openreview_reference",
                "papers": rows,
                "count": len(rows),
            }
        },
    }
    (state_dir / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")
    stale_row = {
        "venue_id": "openreview_iclr",
        "venue": "ICLR",
        "adapter": "openreview_cache",
        "requested_years": [2026],
        "effective_years": [2026],
        "sample_count": 1,
        "candidate_count": 1,
        "corpus_count": 1,
        "ok": True,
        "limited": True,
        "metadata_completeness_status": "partial",
        "title_index_completeness_status": "partial",
    }
    payload = {
        "run_id": "find_stale",
        "selection": selection,
        "venue_health_report": [stale_row],
        "source_status": [{"source": "ICLR", "count": 1, "source_kind": "venue", "venue": "ICLR"}],
        "raw_title_index": rows[:1],
        "counts": {"raw_title_index": 1, "raw_title_index_papers": 1, "title_total_papers": 1},
        "strong_recommendations": [],
        "read_candidates": [],
        "evaluated_candidates": [],
        "scoring_runtime": {},
    }
    (artifact_dir / "find_results.json").write_text(json.dumps(payload), encoding="utf-8")
    (artifact_dir / "find_progress.json").write_text(json.dumps({**payload, "phase": "complete"}), encoding="utf-8")

    result = find_pipeline.refresh_find_source_health(artifact_dir, selection=selection, log=lambda _message: None)
    refreshed = json.loads((artifact_dir / "find_results.json").read_text(encoding="utf-8"))

    assert result["source_integrity_gate"]["status"] == "passed"
    assert result["raw_title_index_count"] == 80
    row = refreshed["venue_health_report"][0]
    assert row["corpus_count"] == 80
    assert row["title_index_completeness_status"] == "complete"
    assert row["source_integrity_status"] == "passed"
    assert refreshed["diagnostics"]["source_integrity_gate"]["status"] == "passed"
    assert refreshed["counts"]["raw_title_index"] == 80


def test_find_collects_all_selected_sources_before_final_scoring(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    run_dir = tmp_path / "run"
    _isolate_find_runtime(monkeypatch, find_pipeline, tmp_path / "runtime")
    events: list[str] = []

    def fake_create_run_dir(prefix="find"):
        run_dir.mkdir(parents=True, exist_ok=True)
        return f"{prefix}_test", run_dir

    def fake_write_json(path, data):
        if Path(path).name == "find_progress.json" and isinstance(data, dict):
            phase = str(data.get("phase") or "")
            if phase:
                events.append(f"phase:{phase}")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def source_item(source: str) -> dict:
        return {
            "id": f"{source}_1",
            "source": source,
            "venue": source,
            "year": 2026,
            "title": f"{source} conditional protein generation",
            "abstract": "A concrete method for conditional de novo protein generation with structure and function constraints.",
            "url": f"https://example.test/{source}",
        }

    def fake_evaluate_items(items, _config, _llm, source_name, *_args, **_kwargs):
        events.append(f"score:{source_name}")
        rows = []
        for item in items:
            row = dict(item)
            row.update({
                "reason_source": "llm abstract evaluation",
                "fit_score": 8.2,
                "diversity_score": 6.4,
                "score": 7.66,
                "stable_rank_score": 8.2,
                "recommendation_score": 8.2,
                "topic_evidence_supported": True,
                "topic_evidence": "passed: conditional de novo protein generation",
                "matched_topic_route": "conditional de novo protein generation",
                "reason_zh": "该条目直接支持条件蛋白生成。",
                "reason_en": "This item directly supports conditional protein generation.",
                "fit_explanation_zh": "摘要包含条件生成证据。",
                "fit_explanation_en": "The abstract contains conditional generation evidence.",
            })
            rows.append(row)
        return rows

    monkeypatch.setattr(find_pipeline, "create_run_dir", fake_create_run_dir)
    monkeypatch.setattr(find_pipeline, "write_json", fake_write_json)
    monkeypatch.setattr(find_pipeline, "sync_latest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(find_pipeline, "update_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(find_pipeline, "_load_stage0_profile_cache", lambda _config: {})
    monkeypatch.setattr(find_pipeline, "_store_stage0_profile_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(find_pipeline, "normalize_user_profile", lambda config, _llm: ({"summary": config.research_interest or config.research_topic}, True, "test"))
    monkeypatch.setattr(find_pipeline, "profile_retrieval_text", lambda profile: str(profile.get("summary") or "conditional protein generation"))
    monkeypatch.setattr(find_pipeline, "_prefilter_titles", lambda items, *_args, **_kwargs: list(items))
    monkeypatch.setattr(find_pipeline, "rank_papers_tfidf", lambda items, *_args, **_kwargs: (list(items), {"strategy": "test"}))
    monkeypatch.setattr(find_pipeline, "fetch_nature_portfolio", lambda *_args, **_kwargs: ([source_item("nature")], find_pipeline._source_status("nature", True, 1, "ok")))
    monkeypatch.setattr(find_pipeline, "enrich_nature_details", lambda items, **_kwargs: (list(items), {"attempted": len(items)}))
    monkeypatch.setattr(find_pipeline, "fetch_science_family", lambda *_args, **_kwargs: ([source_item("science")], find_pipeline._source_status("science", True, 1, "ok")))
    monkeypatch.setattr(find_pipeline, "enrich_science_details", lambda items, **_kwargs: (list(items), {"attempted": len(items)}))
    monkeypatch.setattr(find_pipeline, "fetch_arxiv", lambda *_args, **_kwargs: ([source_item("arxiv")], find_pipeline._source_status("arxiv", True, 1, "ok")))
    monkeypatch.setattr(find_pipeline, "fetch_biorxiv", lambda *_args, **_kwargs: ([source_item("biorxiv")], find_pipeline._source_status("biorxiv", True, 1, "ok")))
    monkeypatch.setattr(find_pipeline, "_evaluate_items", fake_evaluate_items)
    monkeypatch.setattr(find_pipeline, "_recommended", lambda items, _config, source_count=None: list(items)[: max(1, int(source_count or 1) * 5)])
    monkeypatch.setattr(find_pipeline, "_screened_ranking", lambda items, _config=None: list(items))
    monkeypatch.setattr(find_pipeline, "_triage_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(find_pipeline, "_critique_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(find_pipeline, "_recommendation_quality_audit", lambda _items: {"status": "test"})
    monkeypatch.setattr(find_pipeline, "_run_diagnostics", lambda _artifacts: {"source_integrity_gate": {"status": "passed"}, "survey_stats": {}})
    monkeypatch.setattr(find_pipeline, "_attach_abstract_language_fields", lambda *_args, **_kwargs: {"status": "completed"})

    config = find_pipeline.AppConfig(
        provider="mock",
        research_topic="protein design",
        research_interest="conditional protein generation",
        max_recommended_papers=20,
        nature_candidate_limit=10,
        science_candidate_limit=10,
        nonvenue_fetch_limit=10,
    )
    selection = sys.modules["finding_runtime.models"].VenueSelection(
        venue_ids=[],
        years=[2026],
        venue_years=[],
        include_arxiv=True,
        include_biorxiv=True,
        include_nature=True,
        include_science=True,
    )

    result = find_pipeline.run_find(find_pipeline.FindRequest(config=config, selection=selection), log=lambda _message: None)

    assert result["run_id"] == "find_test"
    assert len(result["source_status"]) == 4
    source_collection_index = events.index("phase:source_collection_complete")
    first_score_index = min(index for index, event in enumerate(events) if event.startswith("score:"))
    assert source_collection_index < first_score_index
    assert {event for event in events if event.startswith("score:")} == {
        "score:nature",
        "score:science",
        "score:arxiv",
        "score:biorxiv",
    }


def test_find_title_abstract_scoring_groups_use_global_rank_and_deduplication():
    find_pipeline = _load_find_pipeline()
    config = find_pipeline.AppConfig(
        provider="openai_compatible",
        api_key="test-key",
        model="test-model",
        title_abstract_scoring_limit=3,
    )
    low = {
        "id": "low",
        "title": "Unique lower ranked paper",
        "title_llm_fit_score": 6.0,
    }
    duplicate_high = {
        "id": "duplicate-high",
        "title": "Shared duplicate paper title",
        "title_llm_fit_score": 9.0,
    }
    missing_llm_score = {
        "id": "missing-score",
        "title": "Paper without an LLM title score",
        "fit_score": 10.0,
    }
    duplicate_low = {
        "id": "duplicate-low",
        "title": "Shared duplicate paper title",
        "title_llm_fit_score": 7.0,
    }
    middle = {
        "id": "middle",
        "title": "Unique middle ranked paper",
        "title_llm_fit_score": 8.0,
    }
    logs = []

    selected_groups = find_pipeline._select_title_abstract_scoring_groups(
        [
            ("source-a", [low, duplicate_high, missing_llm_score], "sink-a"),
            ("source-b", [duplicate_low, middle], "sink-b"),
        ],
        config,
        require_title_llm_score=True,
        log=logs.append,
    )

    selected = [item for _source, items, _sink in selected_groups for item in items]
    assert {item["id"] for item in selected} == {"duplicate-high", "middle", "low"}
    assert {item["id"]: item["title_abstract_scoring_global_rank"] for item in selected} == {
        "duplicate-high": 1,
        "middle": 2,
        "low": 3,
    }
    assert "title_abstract_scoring_selected" not in duplicate_low
    assert "title_abstract_scoring_selected" not in missing_llm_score
    assert logs and "eligible_title_scored=4" in logs[-1]
    assert "unique_selected=3" in logs[-1]


def test_find_journal_year_window_marks_partial_coverage_limited(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    assert find_support._coverage_misses_start(["2026-04-29", "2026-06-24"], "2025-06-24") is True
    assert find_support._coverage_misses_start(["2025-06-24", "2026-06-24"], "2025-06-24") is False
    assert find_support._date_window_days("2025-06-24", "2026-06-24") >= 360

    monkeypatch.delenv("NATURE_MAX_PAGES_PER_JOURNAL", raising=False)
    monkeypatch.delenv("SCIENCE_MAX_CROSSREF_PAGES_PER_JOURNAL", raising=False)
    nature_pages = max(10, min(100, (find_support._date_window_days("2025-06-24", "2026-06-24") // 7 + 1) * 3))
    science_pages = max(8, min(50, find_support._date_window_days("2025-06-24", "2026-06-24") // 20 + 2))

    assert nature_pages > 10
    assert science_pages > 8


def test_find_nonconference_default_end_date_is_today():
    find_pipeline = _load_find_pipeline()
    config = find_pipeline.AppConfig()
    today = date.today().isoformat()

    for source, default_days in [("arxiv", 180), ("biorxiv", 180), ("nature", 365), ("science", 365)]:
        start_date, end_date, source_label = find_pipeline._source_effective_date_window(source, config)
        assert end_date == today
        assert start_date == (date.today() - timedelta(days=default_days)).isoformat()
        assert "default_recent" in source_label


def test_find_journal_targeted_openalex_uses_issn_filters(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    requested_urls: list[str] = []

    class FakeResponse:
        text = ""

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_request(url, *_args, **_kwargs):
        requested_urls.append(url)
        if "0036-8075" in url:
            payload = {
                "results": [{
                    "id": "https://openalex.org/W2",
                    "doi": "https://doi.org/10.1126/test.1",
                    "display_name": "Science targeted protein design article",
                    "publication_date": "2026-06-24",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {
                        "landing_page_url": "https://www.science.org/doi/abs/10.1126/test.1",
                        "source": {"display_name": "Science", "issn_l": "0036-8075", "issn": ["0036-8075", "1095-9203"]},
                    },
                }]
            }
        else:
            payload = {
                "results": [{
                    "id": "https://openalex.org/W1",
                    "doi": "https://doi.org/10.1038/test.1",
                    "display_name": "Nature targeted protein design article",
                    "publication_date": "2026-06-24",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {
                        "landing_page_url": "https://www.nature.com/articles/test",
                        "source": {"display_name": "Nature", "issn_l": "0028-0836", "issn": ["0028-0836", "1476-4687"]},
                    },
                }]
            }
        return FakeResponse(payload)

    monkeypatch.setenv("NATURE_TARGETED_ONLY", "1")
    monkeypatch.setenv("NATURE_TARGETED_MAX_PAGES_PER_PHRASE", "1")
    monkeypatch.setenv("SCIENCE_TARGETED_ONLY", "1")
    monkeypatch.setenv("SCIENCE_OPENALEX_FALLBACK", "1")
    monkeypatch.setenv("SCIENCE_OPENALEX_MAX_PAGES_PER_JOURNAL", "1")
    monkeypatch.setenv("OPENALEX_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    nature_papers, nature_status = find_support.fetch_nature_portfolio(
        ["nature"],
        ["article"],
        max_items=5,
        start_date="2026-06-01",
        end_date="2026-06-24",
        enrich_details=False,
        search_phrases=["protein design"],
    )
    science_papers, science_status = find_support.fetch_science_family(
        ["science"],
        ["Research Article"],
        max_items=5,
        start_date="2026-06-01",
        end_date="2026-06-24",
        search_phrases=["protein design"],
    )

    assert len(nature_papers) == 1
    assert nature_status["targeted_search_filter_kind"] == "openalex_issn"
    assert len(science_papers) == 1
    assert science_status["targeted_search_filter_kind"] == "openalex_issn"
    parsed_filters = [parse_qs(urlparse(url).query).get("filter", [""])[0] for url in requested_urls]
    assert any("primary_location.source.issn:0028-0836" in value for value in parsed_filters)
    assert any("primary_location.source.issn:0036-8075" in value for value in parsed_filters)
    assert all("from_publication_date:2026-06-01" in value for value in parsed_filters)
    assert all("to_publication_date:2026-06-24" in value for value in parsed_filters)


def test_find_journal_targeted_raw_pool_ignores_candidate_limit(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        text = ""

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self):
            return self._payload

    def openalex_item(index: int, *, science: bool = False) -> dict:
        doi_prefix = "10.1126" if science else "10.1038"
        source = {"display_name": "Science", "issn_l": "0036-8075", "issn": ["0036-8075"]} if science else {"display_name": "Nature", "issn_l": "0028-0836", "issn": ["0028-0836"]}
        return {
            "id": f"https://openalex.org/W{index}",
            "doi": f"https://doi.org/{doi_prefix}/raw.{index}",
            "display_name": f"{'Science' if science else 'Nature'} raw targeted paper {index}",
            "publication_date": "2026-06-24",
            "type": "article",
            "authorships": [],
            "primary_location": {"landing_page_url": f"https://example.test/{index}", "source": source},
        }

    def fake_request(url, *_args, **_kwargs):
        is_science = "0036-8075" in url
        return FakeResponse({"results": [openalex_item(1, science=is_science), openalex_item(2, science=is_science)]})

    monkeypatch.setenv("NATURE_TARGETED_ONLY", "1")
    monkeypatch.setenv("NATURE_TARGETED_MAX_PAGES_PER_PHRASE", "1")
    monkeypatch.setenv("SCIENCE_TARGETED_ONLY", "1")
    monkeypatch.setenv("SCIENCE_OPENALEX_FALLBACK", "1")
    monkeypatch.setenv("SCIENCE_OPENALEX_MAX_PAGES_PER_JOURNAL", "1")
    monkeypatch.setenv("OPENALEX_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    nature_papers, nature_status = find_support.fetch_nature_portfolio(
        ["nature"],
        ["article"],
        max_items=1,
        start_date="2026-06-01",
        end_date="2026-06-24",
        enrich_details=False,
        search_phrases=["protein design"],
    )
    science_papers, science_status = find_support.fetch_science_family(
        ["science"],
        ["Research Article"],
        max_items=1,
        start_date="2026-06-01",
        end_date="2026-06-24",
        search_phrases=["protein design"],
    )

    assert len(nature_papers) == 2
    assert nature_status["candidate_limit"] == 1
    assert nature_status["target_count_reached"] is False
    assert len(science_papers) == 2
    assert science_status["candidate_limit"] == 1
    assert science_status["target_count_reached"] is False


def test_find_journal_targeted_exhaustion_not_date_coverage_limited(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        text = ""

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_request(url, *_args, **_kwargs):
        if "0036-8075" in url:
            payload = {
                "results": [{
                    "id": "https://openalex.org/WS1",
                    "doi": "https://doi.org/10.1126/exhausted.1",
                    "display_name": "Science exhausted targeted result",
                    "publication_date": "2026-06-24",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {
                        "landing_page_url": "https://www.science.org/doi/abs/10.1126/exhausted.1",
                        "source": {"display_name": "Science", "issn_l": "0036-8075", "issn": ["0036-8075"]},
                    },
                }]
            }
        else:
            payload = {
                "results": [{
                    "id": "https://openalex.org/WN1",
                    "doi": "https://doi.org/10.1038/exhausted.1",
                    "display_name": "Nature exhausted targeted result",
                    "publication_date": "2026-06-24",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {
                        "landing_page_url": "https://www.nature.com/articles/exhausted",
                        "source": {"display_name": "Nature", "issn_l": "0028-0836", "issn": ["0028-0836"]},
                    },
                }]
            }
        return FakeResponse(payload)

    monkeypatch.setenv("NATURE_TARGETED_ONLY", "1")
    monkeypatch.setenv("NATURE_TARGETED_PER_PAGE", "200")
    monkeypatch.setenv("NATURE_TARGETED_MAX_PAGES_PER_PHRASE", "5")
    monkeypatch.setenv("SCIENCE_TARGETED_ONLY", "1")
    monkeypatch.setenv("SCIENCE_OPENALEX_FALLBACK", "1")
    monkeypatch.setenv("SCIENCE_OPENALEX_MAX_PAGES_PER_JOURNAL", "5")
    monkeypatch.setenv("OPENALEX_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    nature_papers, nature_status = find_support.fetch_nature_portfolio(
        ["nature"],
        ["article"],
        max_items=5,
        start_date="2026-01-01",
        end_date="2026-06-24",
        enrich_details=False,
        search_phrases=["rare topic"],
    )
    science_papers, science_status = find_support.fetch_science_family(
        ["science"],
        ["Research Article"],
        max_items=5,
        start_date="2026-01-01",
        end_date="2026-06-24",
        search_phrases=["rare topic"],
    )

    assert len(nature_papers) == 1
    assert nature_status["targeted_query_exhausted"] is True
    assert nature_status.get("coverage_limited") is not True
    assert nature_status["limited"] is False
    assert len(science_papers) == 1
    assert science_status["targeted_query_exhausted"] is True
    assert science_status.get("coverage_limited") is not True
    assert science_status["limited"] is False


def test_find_targeted_query_exhaustion_satisfies_completeness_date_basis():
    find_pipeline = _load_find_pipeline()
    rows = [{
        "id": "nature-late-only",
        "source": "nature",
        "title": "Late-only targeted result",
        "metadata": {"published": "2026-06-24", "doi": "10.1038/late"},
    }]
    params = {
        "start_date": "2026-01-01",
        "end_date": "2026-06-24",
        "max_items": 5,
        "raw_item_limit": 0,
    }
    status = find_pipeline._annotate_source_completeness(
        "nature",
        {"ok": True, "limited": False, "targeted_search_used": True, "targeted_only": True, "targeted_query_exhausted": True},
        rows,
        params,
    )

    assert status["completeness_level"] == "keyword_targeted_complete_window"
    assert status["date_coverage_reaches_start"] is True
    assert status["cap_truncated"] is False


def test_find_arxiv_fetch_limit_keeps_newest_targeted_results(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        text = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2606.00001v1</id>
    <published>2026-06-20T00:00:00Z</published>
    <updated>2026-06-20T00:00:00Z</updated>
    <title>Conditional protein design one</title>
    <summary>Topic matched abstract one.</summary>
    <author><name>A Author</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2606.00002v1</id>
    <published>2026-06-21T00:00:00Z</published>
    <updated>2026-06-21T00:00:00Z</updated>
    <title>Conditional protein design two</title>
    <summary>Topic matched abstract two.</summary>
    <author><name>B Author</name></author>
  </entry>
</feed>"""

    monkeypatch.setenv("ARXIV_TARGETED_COMPLETE", "1")
    monkeypatch.setenv("ARXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request_arxiv_page", lambda *_args, **_kwargs: FakeResponse())

    papers, status = find_support.fetch_arxiv(
        ["cs.AI"],
        fetch_limit=1,
        start_date="2026-06-01",
        end_date="2026-06-24",
        targeted_queries=[("topic:test", "all:protein AND submittedDate:[202606010000 TO 202606242359]")],
    )

    assert [paper["title"] for paper in papers] == ["Conditional protein design two"]
    assert status["fetch_limit"] == 1
    assert status["fetch_limit_reached"] is True
    assert status["raw_item_limit"] == 1
    assert "minimum_target" not in status


def test_find_arxiv_scans_past_first_full_page_until_query_exhaustion(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    starts = []

    class FakeResponse:
        def __init__(self, start):
            count = 100 if start == 0 else 1 if start == 100 else 0
            entries = []
            for offset in range(count):
                index = start + offset
                entries.append(
                    f"<entry><id>http://arxiv.org/abs/2606.{index:05d}v1</id>"
                    "<published>2026-06-20T00:00:00Z</published>"
                    "<updated>2026-06-20T00:00:00Z</updated>"
                    f"<title>Protein design {index}</title><summary>Abstract.</summary>"
                    "<author><name>A</name></author></entry>"
                )
            self.text = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"

    def fake_request(url, *_args, **_kwargs):
        start = int(parse_qs(urlparse(url).query)["start"][0])
        starts.append(start)
        return FakeResponse(start)

    monkeypatch.setenv("ARXIV_TARGETED_COMPLETE", "1")
    monkeypatch.delenv("ARXIV_FULL_SCAN", raising=False)
    monkeypatch.setenv("ARXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request_arxiv_page", fake_request)

    papers, status = find_support.fetch_arxiv(
        ["q-bio.BM"],
        fetch_limit=200,
        start_date="2026-06-01",
        end_date="2026-06-24",
        targeted_queries=[("anchor", 'all:"protein design"')],
        max_queries=1,
    )

    assert len(papers) == 101
    assert starts == [0, 100]
    assert status["page_size"] == 100
    assert status["fetch_limit_reached"] is False


def test_find_arxiv_targeted_fetch_limit_is_a_result_limit(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        text = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>http://arxiv.org/abs/2606.00001v1</id><published>2026-06-20T00:00:00Z</published><updated>2026-06-20T00:00:00Z</updated><title>Protein design one</title><summary>One.</summary><author><name>A</name></author><arxiv:primary_category term="q-bio.BM"/><category term="q-bio.BM"/></entry>
  <entry><id>http://arxiv.org/abs/2606.00002v1</id><published>2026-06-21T00:00:00Z</published><updated>2026-06-21T00:00:00Z</updated><title>Protein design two</title><summary>Two.</summary><author><name>B</name></author><arxiv:primary_category term="q-bio.BM"/><category term="q-bio.BM"/></entry>
</feed>"""

    monkeypatch.delenv("ARXIV_TARGETED_COMPLETE", raising=False)
    monkeypatch.setenv("ARXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request_arxiv_page", lambda *_args, **_kwargs: FakeResponse())

    papers, status = find_support.fetch_arxiv(
        ["q-bio.BM"],
        fetch_limit=1,
        start_date="2026-06-01",
        end_date="2026-06-24",
        targeted_queries=[("anchor", 'all:"protein design" AND cat:q-bio.BM')],
    )

    assert [paper["title"] for paper in papers] == ["Protein design two"]
    assert status["fetch_limit"] == 1
    assert status["fetch_limit_reached"] is True
    assert status["raw_item_limit"] == 1


def test_find_arxiv_non_targeted_honors_query_limit_without_result_truncation(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    seen_urls = []

    class FakeResponse:
        text = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>http://arxiv.org/abs/2606.00001v1</id><published>2026-06-20T00:00:00Z</published><updated>2026-06-20T00:00:00Z</updated><title>Protein design one</title><summary>One.</summary><author><name>A</name></author><arxiv:primary_category term="q-bio.BM"/><category term="q-bio.BM"/></entry>
  <entry><id>http://arxiv.org/abs/2606.00002v1</id><published>2026-06-21T00:00:00Z</published><updated>2026-06-21T00:00:00Z</updated><title>Protein design two</title><summary>Two.</summary><author><name>B</name></author><arxiv:primary_category term="q-bio.BM"/><category term="q-bio.BM"/></entry>
</feed>"""

    def fake_request(url, *_args, **_kwargs):
        seen_urls.append(url)
        return FakeResponse()

    monkeypatch.delenv("ARXIV_FULL_SCAN", raising=False)
    monkeypatch.setenv("ARXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request_arxiv_page", fake_request)

    papers, status = find_support.fetch_arxiv(
        ["q-bio.BM", "cs.LG"],
        fetch_limit=10,
        start_date="2026-06-01",
        end_date="2026-06-24",
        topic_queries=[],
        max_queries=1,
    )

    assert len(papers) == 2
    assert len(seen_urls) == 1
    assert status["full_scan"] is False
    assert status["query_limit"] == 1
    assert len(status["queries"]) == 1


def test_find_arxiv_targeted_query_never_uses_category_only_fallback(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    seen_urls = []

    class EmptyResponse:
        text = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    def fake_request(url, *_args, **_kwargs):
        seen_urls.append(url)
        return EmptyResponse()

    monkeypatch.delenv("ARXIV_FULL_SCAN", raising=False)
    monkeypatch.setenv("ARXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request_arxiv_page", fake_request)

    papers, status = find_support.fetch_arxiv(
        ["q-bio.BM", "cs.LG"],
        fetch_limit=5,
        start_date="2026-06-01",
        end_date="2026-06-24",
        targeted_queries=[
            ("keywords", '(all:protein OR all:diffusion OR all:RL)'),
        ],
        max_queries=3,
    )

    assert papers == []
    assert len(seen_urls) == 1
    assert status["queries"] == ['(all:protein OR all:diffusion OR all:RL)']
    assert "recall_fallback_used" not in status


def test_find_biorxiv_europepmc_uses_publisher_and_accepts_new_doi(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    seen_urls = []

    class FakeResponse:
        def json(self):
            return {
                "resultList": {
                    "result": [
                        {
                            "doi": "10.64898/2026.04.20.719544",
                            "title": "Protein inverse folding through joint geometry",
                            "abstractText": "A bioRxiv protein-design preprint.",
                            "firstPublicationDate": "2026-04-20",
                            "authorString": "A. Author",
                            "bookOrReportDetails": {"publisher": "bioRxiv"},
                        },
                        {
                            "doi": "10.1101/medrxiv-record",
                            "title": "A medRxiv record",
                            "abstractText": "Not a bioRxiv record.",
                            "firstPublicationDate": "2026-04-21",
                            "authorString": "B. Author",
                            "bookOrReportDetails": {"publisher": "medRxiv"},
                        },
                    ]
                }
            }

    def fake_request(url, *_args, **_kwargs):
        seen_urls.append(url)
        return FakeResponse()

    monkeypatch.setenv("EUROPEPMC_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)
    status = {"errors": [], "pages_fetched": 0}
    papers = find_support._fetch_biorxiv_europepmc(
        ["protein inverse folding"],
        10,
        "2026-01-01",
        "2026-07-01",
        status,
    )

    query = parse_qs(urlparse(seen_urls[0]).query)["query"][0]
    assert 'PUBLISHER:"bioRxiv"' in query
    assert 'TITLE:"protein inverse folding"' in query
    assert 'ABSTRACT:"protein inverse folding"' in query
    assert "sort_date:y" in query
    assert [paper["biorxiv_doi"] for paper in papers] == ["10.64898/2026.04.20.719544"]
    assert papers[0]["classification_source"] == "unavailable"


def test_find_biorxiv_openalex_reports_exhausted_daily_budget_without_waiting(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    calls = []

    class RateLimitError(RuntimeError):
        pass

    error = RateLimitError("429 Client Error: Too Many Requests")
    error.response = type("Response", (), {
        "status_code": 429,
        "headers": {"Retry-After": "64000", "X-RateLimit-Remaining": "0", "X-RateLimit-Remaining-USD": "0"},
        "text": "Insufficient budget. Resets at midnight UTC.",
    })()

    def fail_request(url, *_args, **_kwargs):
        calls.append(url)
        raise error

    monkeypatch.setenv("OPENALEX_REQUEST_RETRIES", "4")
    monkeypatch.setenv("OPENALEX_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fail_request)
    status = {"errors": [], "pages_fetched": 0, "limited": False}

    papers = find_support._fetch_biorxiv_openalex(
        ["protein design", "protein inverse folding", "protein sequence design"],
        10,
        "2026-01-01",
        "2026-07-01",
        status,
    )

    assert papers == []
    assert len(calls) == 1
    assert status["limited"] is True
    assert status["openalex_rate_limited"] is True
    assert status["stopped_reason"] == "openalex_daily_budget_exhausted"
    assert status["openalex_retry_after_sec"] == 64000


def test_find_biorxiv_openalex_queries_one_equal_status_or_group(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    requested = []

    class FakeResponse:
        def __init__(self, phrase):
            self.phrase = phrase

        def json(self):
            return {"results": [{"phrase": self.phrase}], "meta": {"next_cursor": None}}

    def fake_request(url, *_args, **_kwargs):
        phrase = parse_qs(urlparse(url).query)["search"][0]
        requested.append(phrase)
        return FakeResponse(phrase)

    def fake_paper(item, *_args):
        phrase = item["phrase"]
        return {
            "id": phrase,
            "source": "biorxiv",
            "biorxiv_doi": f"10.64898/{phrase}",
            "title": phrase,
            "metadata": {"published": "2026-06-01"},
        }

    monkeypatch.setenv("OPENALEX_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)
    monkeypatch.setattr(find_support, "_biorxiv_paper_from_openalex", fake_paper)
    status = {"errors": [], "pages_fetched": 0, "limited": False}

    papers = find_support._fetch_biorxiv_openalex(
        ["protein", "diffusion", "RL"],
        2,
        "2026-01-01",
        "2026-07-01",
        status,
    )

    assert requested == ["protein OR diffusion OR RL"]
    assert len(papers) == 1


def test_find_biorxiv_openalex_uses_api_key_and_retries_short_429(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    calls = []

    class RateLimitError(RuntimeError):
        pass

    error = RateLimitError("429 for api_key=secret-key")
    error.response = type("Response", (), {
        "status_code": 429,
        "headers": {"Retry-After": "1", "X-RateLimit-Remaining": "0", "X-RateLimit-Remaining-USD": "0.97"},
        "text": "Too many requests per second.",
    })()

    class SuccessResponse:
        def json(self):
            return {"results": [], "meta": {"next_cursor": None}}

    def fake_request(url, *_args, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise error
        return SuccessResponse()

    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key")
    monkeypatch.setenv("OPENALEX_REQUEST_RETRIES", "2")
    monkeypatch.setattr(find_support.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(find_support, "_request", fake_request)
    status = {"errors": [], "pages_fetched": 0, "limited": False}

    papers = find_support._fetch_biorxiv_openalex(
        ["protein", "diffusion"],
        10,
        "2026-01-01",
        "2026-07-01",
        status,
    )

    assert papers == []
    assert len(calls) == 2
    assert all(parse_qs(urlparse(url).query)["api_key"] == ["secret-key"] for url in calls)
    assert status["openalex_api_key_configured"] is True
    assert "secret-key" not in " ".join(status["errors"])


def test_find_biorxiv_openalex_long_retry_is_not_misreported_as_daily_budget(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class RateLimitError(RuntimeError):
        pass

    error = RateLimitError("429 Client Error: Too Many Requests")
    error.response = type("Response", (), {
        "status_code": 429,
        "headers": {"Retry-After": "60", "X-RateLimit-Remaining": "0", "X-RateLimit-Remaining-USD": "0.85"},
        "text": "Too many requests per second.",
    })()

    monkeypatch.setenv("OPENALEX_MAX_RETRY_WAIT_SEC", "30")
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    status = {"errors": [], "pages_fetched": 0, "limited": False}

    papers = find_support._fetch_biorxiv_openalex(
        ["protein"],
        10,
        "2026-01-01",
        "2026-07-01",
        status,
    )

    assert papers == []
    assert status["stopped_reason"] == "openalex_rate_limited"
    assert "daily API budget exhausted" not in " ".join(status["errors"])


def test_find_biorxiv_targeted_full_target_skips_openalex_and_native_fallback(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    targeted = [{
        "id": f"targeted-{index}",
        "source": "biorxiv",
        "biorxiv_doi": f"10.64898/targeted-{index}",
        "title": f"Targeted protein design {index}",
        "abstract": "A targeted match.",
        "metadata": {"published": "2026-06-02", "doi": f"10.64898/targeted-{index}"},
    } for index in range(5)]

    monkeypatch.delenv("BIORXIV_COMPLETE_WINDOW", raising=False)
    monkeypatch.setattr(find_support, "_fetch_biorxiv_europepmc", lambda *_args, **_kwargs: list(targeted))
    monkeypatch.setattr(find_support, "_fetch_biorxiv_openalex", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OpenAlex must not run after Europe PMC succeeds")))
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("native fallback must not run after targeted success")))

    papers, status = find_support.fetch_biorxiv(
        ["all"],
        fetch_limit=5,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=["protein design"],
    )

    assert papers == targeted
    assert status["targeted"] is True
    assert status["openalex_skipped_reason"] == "explicit internal raw item limit reached"
    assert status["fetch_limit_reached"] is True


def test_find_biorxiv_targeted_partial_europepmc_does_not_treat_limit_as_target(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    epmc = [{
        "id": "epmc",
        "source": "biorxiv",
        "biorxiv_doi": "10.64898/epmc",
        "title": "Europe PMC protein design",
        "abstract": "A targeted match.",
        "metadata": {"published": "2026-06-02", "doi": "10.64898/epmc"},
    }]
    monkeypatch.setattr(find_support, "_fetch_biorxiv_europepmc", lambda *_args, **_kwargs: list(epmc))
    monkeypatch.setattr(find_support, "_fetch_biorxiv_openalex", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("a fetch limit is not a backfill target")))

    papers, status = find_support.fetch_biorxiv_targeted(
        ["protein design"],
        fetch_limit=3,
        start_date="2026-06-01",
        end_date="2026-06-02",
    )

    assert {paper["id"] for paper in papers} == {"epmc"}
    assert status["openalex_skipped_reason"] == "Europe PMC returned keyword-matched papers"
    assert status["fetch_limit_reached"] is False


def test_find_biorxiv_targeted_category_filter_does_not_turn_limit_into_minimum(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    epmc = [
        {
            "id": f"epmc-{index}",
            "source": "biorxiv",
            "biorxiv_doi": f"10.64898/epmc-{index}",
            "title": f"Europe PMC protein design {index}",
            "metadata": {"published": "2026-06-02"},
        }
        for index in range(3)
    ]
    openalex = [{
        "id": "openalex",
        "source": "biorxiv",
        "biorxiv_doi": "10.64898/openalex",
        "title": "OpenAlex protein design",
        "metadata": {"published": "2026-06-01"},
    }]
    openalex_calls = []

    monkeypatch.setattr(find_support, "_fetch_biorxiv_europepmc", lambda *_args, **_kwargs: list(epmc))

    def fake_openalex(*_args, **_kwargs):
        openalex_calls.append(True)
        return list(openalex)

    monkeypatch.setattr(find_support, "_fetch_biorxiv_openalex", fake_openalex)
    monkeypatch.setattr(
        find_support,
        "_filter_biorxiv_targeted_by_official_category",
        lambda papers, *_args, **_kwargs: [paper for paper in papers if paper["id"] in {"epmc-0", "openalex"}],
    )

    papers, status = find_support.fetch_biorxiv_targeted(
        ["protein design"],
        fetch_limit=3,
        start_date="2026-06-01",
        end_date="2026-06-02",
        categories=["bioinformatics"],
    )

    assert openalex_calls == []
    assert {paper["id"] for paper in papers} == {"epmc-0"}
    assert status["openalex_skipped_reason"] == "explicit internal raw item limit reached"
    assert status["fetch_limit_reached"] is True


def test_find_biorxiv_partial_target_never_falls_back_to_native_window(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    targeted = [{
        "id": "targeted",
        "source": "biorxiv",
        "biorxiv_doi": "10.64898/targeted",
        "title": "Targeted protein design",
        "abstract": "A targeted match.",
        "metadata": {"published": "2026-06-02", "doi": "10.64898/targeted"},
    }]

    monkeypatch.delenv("BIORXIV_COMPLETE_WINDOW", raising=False)
    monkeypatch.setattr(find_support, "fetch_biorxiv_targeted", lambda *_args, **_kwargs: (
        list(targeted),
        {"message": "partial", "queries": ["targeted"], "errors": []},
    ))
    monkeypatch.setattr(
        find_support,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("native window must not run")),
    )

    papers, status = find_support.fetch_biorxiv(
        ["all"],
        fetch_limit=3,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=["protein design"],
    )

    assert papers == targeted
    assert status["queries"] == ["targeted"]


def test_find_biorxiv_native_fallback_pushes_multiple_categories_to_api(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    seen_categories = []

    class FakeResponse:
        def __init__(self, category):
            self.category = category

        def json(self):
            return {
                "messages": [{"total": "3", "count": "3", "category": self.category}],
                "collection": [
                    {
                            "date": "2026-06-02",
                        "category": self.category,
                        "title": f"Native {self.category} protein paper {index}",
                        "abstract": "A native category result.",
                        "doi": f"10.64898/{self.category.replace(' ', '-')}-{index}",
                        "version": "1",
                        "authors": "A. Author",
                    }
                    for index in range(3)
                ],
            }

    def fake_request(url, *_args, **_kwargs):
        category = parse_qs(urlparse(url).query).get("category", ["all"])[0]
        seen_categories.append(category)
        return FakeResponse(category)

    monkeypatch.setenv("BIORXIV_TARGETED", "0")
    monkeypatch.delenv("BIORXIV_COMPLETE_WINDOW", raising=False)
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    papers, status = find_support.fetch_biorxiv(
        ["bioinformatics", "biophysics"],
        fetch_limit=10,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=[],
    )

    assert len(papers) == 6
    assert set(seen_categories) == {"bioinformatics", "biophysics"}
    assert {paper["category"] for paper in papers} == {"bioinformatics", "biophysics"}
    assert all(paper["classification_source"] == "official" for paper in papers)
    assert status["fetch_limit_reached"] is False


def test_find_biorxiv_native_query_scans_all_pages_without_minimum_truncation(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    seen_cursors = []

    class FakeResponse:
        def __init__(self, cursor):
            self.cursor = cursor

        def json(self):
            records = []
            for index in range(self.cursor, min(47, self.cursor + 30)):
                published = (date(2025, 1, 1) + timedelta(days=index)).isoformat()
                records.append({
                    "date": published,
                    "category": "paleontology",
                    "title": f"Chronological paper {index}",
                    "abstract": "An official category record.",
                    "doi": f"10.64898/chronological-{index}",
                    "version": "1",
                    "authors": "A. Author",
                })
            return {
                "messages": [{"total": "47", "count": str(len(records))}],
                "collection": records,
            }

    def fake_request(url, *_args, **_kwargs):
        cursor = int(str(url).rstrip("/").split("/")[-2])
        seen_cursors.append(cursor)
        return FakeResponse(cursor)

    monkeypatch.setenv("BIORXIV_TARGETED", "0")
    monkeypatch.delenv("BIORXIV_COMPLETE_WINDOW", raising=False)
    monkeypatch.setenv("BIORXIV_WINDOW_DAYS", "60")
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    papers, status = find_support.fetch_biorxiv(
        ["paleontology"],
        fetch_limit=50,
        start_date="2025-01-01",
        end_date="2025-02-16",
        search_phrases=[],
    )

    assert len(papers) == 47
    assert papers[0]["title"] == "Chronological paper 46"
    assert papers[-1]["title"] == "Chronological paper 0"
    assert seen_cursors == [0, 30]
    assert status["fetch_limit_reached"] is False
    assert status["stopped_reason"] == "selected queries exhausted"


def test_find_biorxiv_explicit_category_drops_unverified_targeted_records(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    paper = {
        "id": "unverified",
        "source": "biorxiv",
        "biorxiv_doi": "10.64898/unverified",
        "title": "Unverified category paper",
        "metadata": {"doi": "10.64898/unverified", "published": "2026-06-02"},
    }

    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    status = {"errors": [], "limited": False}
    papers = find_support._filter_biorxiv_targeted_by_official_category(
        [paper],
        ["molecular biology"],
        status,
    )

    assert papers == []
    assert paper["classification_source"] == "unavailable"
    assert status["official_category_unverified_count"] == 1
    assert status["official_category_unverified_dropped_count"] == 1
    assert status["limited"] is True


def test_find_biorxiv_rejects_category_api_silent_fallback_to_all(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    calls = []

    class SilentFallbackResponse:
        def json(self):
            return {
                "messages": [{"total": "367", "count": "30", "category": "all"}],
                "collection": [{
                    "date": "2026-06-02",
                    "category": "neuroscience",
                    "title": "Unrelated all-category record",
                    "abstract": "This must not enter an unsupported category request.",
                    "doi": "10.64898/unrelated-all",
                    "version": "1",
                    "authors": "A. Author",
                }],
            }

    def fake_request(url, *_args, **_kwargs):
        calls.append(url)
        return SilentFallbackResponse()

    monkeypatch.setenv("BIORXIV_TARGETED", "0")
    monkeypatch.delenv("BIORXIV_COMPLETE_WINDOW", raising=False)
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    papers, status = find_support.fetch_biorxiv(
        ["epidemiology"],
        fetch_limit=5,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=[],
    )

    assert papers == []
    assert len(calls) == 1
    assert status["unsupported_categories"] == ["epidemiology"]
    assert status["limited"] is True
    assert "API reported 'all'" in status["errors"][0]


def test_find_biorxiv_complete_window_does_not_truncate_raw_pool(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        def json(self):
            return {
                "messages": [{"total": "2", "count": "2"}],
                "collection": [
                    {
                        "date": "2026-06-02",
                        "category": "bioinformatics",
                        "title": "Native bioRxiv conditional protein design one",
                        "abstract": "A native bioRxiv record about conditional protein design.",
                        "doi": "10.1101/native1",
                        "version": "1",
                        "authors": "A. Author",
                    },
                    {
                        "date": "2026-06-01",
                        "category": "bioinformatics",
                        "title": "Native bioRxiv conditional protein design two",
                        "abstract": "Another native bioRxiv record about conditional protein design.",
                        "doi": "10.1101/native2",
                        "version": "1",
                        "authors": "B. Author",
                    },
                ],
            }

    targeted = [{
        "id": "targeted",
        "source": "biorxiv",
        "biorxiv_doi": "10.1101/targeted",
        "title": "Targeted bioRxiv conditional protein design",
        "abstract": "Targeted seed record.",
        "metadata": {"published": "2026-06-02", "doi": "10.1101/targeted"},
    }]
    targeted_status = {
        "source": "biorxiv",
        "ok": True,
        "limited": False,
        "count": 1,
        "queries": ["targeted"],
        "errors": [],
        "message": "ok targeted",
    }

    monkeypatch.setenv("BIORXIV_COMPLETE_WINDOW", "1")
    monkeypatch.setenv("BIORXIV_LATEST_FIRST", "1")
    monkeypatch.setenv("BIORXIV_TARGETED_BEFORE_COMPLETE_WINDOW", "1")
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "fetch_biorxiv_targeted", lambda *_args, **_kwargs: (list(targeted), dict(targeted_status)))
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: FakeResponse())

    papers, status = find_support.fetch_biorxiv(
        ["bioinformatics"],
        fetch_limit=10,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=["protein design"],
    )

    assert len(papers) == 3
    assert status["complete_window_scan"] is True
    assert status["latest_first"] is False
    assert status["targeted_recall_used"] is True
    assert status["limited"] is False
    assert status["stopped_reason"] == "full window scanned"


def test_find_biorxiv_complete_window_skips_targeted_seed_by_default(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        def json(self):
            return {
                "messages": [{"total": "1", "count": "1"}],
                "collection": [
                    {
                        "date": "2026-06-02",
                        "category": "bioinformatics",
                        "title": "Native bioRxiv complete window record",
                        "abstract": "A native bioRxiv complete-window record.",
                        "doi": "10.1101/native-complete",
                        "version": "1",
                        "authors": "A. Author",
                    }
                ],
            }

    def fail_targeted(*_args, **_kwargs):
        raise AssertionError("targeted seed should not run before complete native window scan by default")

    monkeypatch.setenv("BIORXIV_COMPLETE_WINDOW", "1")
    monkeypatch.delenv("BIORXIV_TARGETED_BEFORE_COMPLETE_WINDOW", raising=False)
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "fetch_biorxiv_targeted", fail_targeted)
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: FakeResponse())

    papers, status = find_support.fetch_biorxiv(
        ["bioinformatics"],
        fetch_limit=10,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=["protein design"],
    )

    assert len(papers) == 1
    assert status["complete_window_scan"] is True
    assert status["targeted_recall_skipped"] is True
    assert status["targeted_recall_skip_reason"] == "complete_window_scan_uses_native_biorxiv_window_first"
    assert status["stopped_reason"] == "full window scanned"


def test_find_biorxiv_complete_window_fetches_all_native_cursors(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        def __init__(self, cursor: int):
            self.cursor = cursor

        def json(self):
            total = 61
            remaining = max(0, total - self.cursor)
            count = min(30, remaining)
            records = []
            for offset in range(count):
                index = self.cursor + offset
                records.append({
                    "date": "2026-06-02",
                    "category": "bioinformatics",
                    "title": f"Native bioRxiv complete-window record {index}",
                    "abstract": "A native bioRxiv complete-window record.",
                    "doi": f"10.1101/native-cursor-{index}",
                    "version": "1",
                    "authors": "A. Author",
                })
            return {
                "messages": [{"total": str(total), "count": str(count)}],
                "collection": records,
            }

    seen_cursors = []
    seen_categories = []

    def fake_request(url, *_args, **_kwargs):
        cursor = int(str(url).rstrip("/").split("/")[-2])
        seen_cursors.append(cursor)
        seen_categories.append(parse_qs(urlparse(url).query).get("category", ["all"])[0])
        return FakeResponse(cursor)

    monkeypatch.setenv("BIORXIV_COMPLETE_WINDOW", "1")
    monkeypatch.setenv("BIORXIV_PARALLEL_PAGES", "3")
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    papers, status = find_support.fetch_biorxiv(
        ["bioinformatics"],
        fetch_limit=100,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=["protein design"],
    )

    assert len(papers) == 61
    assert sorted(seen_cursors) == [0, 30, 60]
    assert set(seen_categories) == {"bioinformatics"}
    assert status["complete_window_scan"] is True
    assert status["parallel_page_fetch_used"] is True
    assert status["pages_fetched"] == 3
    assert status["stopped_reason"] == "full window scanned"


def test_find_biorxiv_complete_window_does_not_schedule_after_raw_cap(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    calls = []

    class FakeResponse:
        def json(self):
            return {
                "messages": [{"total": "300", "count": "30"}],
                "collection": [
                    {
                        "date": "2026-06-02",
                        "category": "bioinformatics",
                        "title": f"Native capped paper {index}",
                        "abstract": "A category-filtered native result.",
                        "doi": f"10.64898/capped-{index}",
                        "version": "1",
                        "authors": "A. Author",
                    }
                    for index in range(30)
                ],
            }

    def fake_request(url, *_args, **_kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setenv("BIORXIV_COMPLETE_WINDOW", "1")
    monkeypatch.setenv("BIORXIV_PARALLEL_PAGES", "4")
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    papers, status = find_support.fetch_biorxiv(
        ["bioinformatics"],
        fetch_limit=1,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=[],
    )

    assert len(papers) == 1
    assert len(calls) == 1
    assert parse_qs(urlparse(calls[0]).query)["category"] == ["bioinformatics"]
    assert status["raw_item_limit_reached"] is True
    assert status["limited"] is True


def test_find_biorxiv_targeted_fetch_limit_keeps_newest_results(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        def json(self):
            return {
                "resultList": {
                    "result": [
                        {
                            "doi": "10.1101/target1",
                            "title": "Targeted bioRxiv seed one",
                            "abstractText": "A targeted seed for conditional protein design.",
                            "firstPublicationDate": "2026-06-02",
                            "authorString": "A. Author",
                            "bookOrReportDetails": {"publisher": "bioRxiv"},
                        },
                        {
                            "doi": "10.1101/target2",
                            "title": "Targeted bioRxiv seed two",
                            "abstractText": "Another targeted seed for conditional protein design.",
                            "firstPublicationDate": "2026-06-01",
                            "authorString": "B. Author",
                            "bookOrReportDetails": {"publisher": "bioRxiv"},
                        },
                    ]
                }
            }

    monkeypatch.setenv("BIORXIV_OPENALEX", "0")
    monkeypatch.delenv("BIORXIV_TARGETED_RAW_MAX_ITEMS", raising=False)
    monkeypatch.setenv("EUROPEPMC_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: FakeResponse())

    papers, status = find_support.fetch_biorxiv_targeted(
        ["protein design"],
        fetch_limit=1,
        start_date="2026-06-01",
        end_date="2026-06-02",
    )

    assert [paper["title"] for paper in papers] == ["Targeted bioRxiv seed one"]
    assert status["fetch_limit"] == 1
    assert status["fetch_limit_reached"] is True
    assert status["raw_item_limit"] == 1
    assert "minimum_target" not in status


def test_find_biorxiv_targeted_reports_raw_cap_before_category_filter(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    targeted = [
        {
            "id": f"targeted-{index}",
            "source": "biorxiv",
            "biorxiv_doi": f"10.64898/targeted-{index}",
            "title": f"Targeted protein paper {index}",
            "abstract": "A precise keyword match.",
            "metadata": {"published": "2026-06-02", "doi": f"10.64898/targeted-{index}"},
        }
        for index in range(2)
    ]

    class CategoryResponse:
        def json(self):
            return {
                "collection": [{"doi": "", "category": "bioinformatics"}],
            }

    monkeypatch.setattr(find_support, "_fetch_biorxiv_europepmc", lambda *_args, **_kwargs: list(targeted))
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: CategoryResponse())

    papers, status = find_support.fetch_biorxiv_targeted(
        ["protein inverse folding"],
        fetch_limit=2,
        start_date="2026-06-01",
        end_date="2026-06-02",
        categories=["biophysics"],
    )

    assert papers == []
    assert status["raw_count"] == 2
    assert status["raw_item_limit_reached"] is True
    assert status["fetch_limit_reached"] is True
    assert status["official_category_rejected_count"] == 2
    assert status["limited"] is True


def test_find_dynamic_source_raw_cache_helpers_are_removed(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    _isolate_find_runtime(monkeypatch, find_pipeline, tmp_path / "runtime")

    params = {
        "journals": ["nature"],
        "article_types": ["article"],
        "start_date": "2026-06-01",
        "end_date": "2026-06-28",
        "date_window_source": "test",
        "max_items": 200,
        "search_phrases": ["protein design"],
    }
    rows = [{
        "id": "n1",
        "source": "nature",
        "title": "Nature paper from current run",
        "url": "https://example.test/n1",
        "metadata": {"published": "2026-06-10", "doi": "10.1038/current-run"},
    }]
    status = find_pipeline._annotate_source_completeness("nature", {"ok": True, "limited": False, "count": 1}, rows, params)

    assert not hasattr(find_pipeline, "_write_source_raw_cache")
    assert not hasattr(find_pipeline, "_load_source_raw_cache_seed")
    assert not (tmp_path / "runtime" / "state" / "source_raw_cache.json").exists()
    assert "min_count_required" not in status
    assert "cache_min_count_required" not in status


def test_find_preserves_natural_llm_recommendation_reason_without_template_rewrite(monkeypatch):
    find_pipeline = _load_find_pipeline()
    monkeypatch.setenv("RECOMMENDATION_REASON_MIN_ZH_CHARS", "999")
    monkeypatch.setenv("RECOMMENDATION_REASON_MIN_EN_CHARS", "999")
    assert find_pipeline.RECOMMENDATION_REASON_MIN_ZH_CHARS == 20
    assert find_pipeline.RECOMMENDATION_REASON_MIN_EN_CHARS == 40
    concise_reason_zh = "论文提出可控生成方法，可帮助比较约束策略，并借鉴其评测设计。"
    concise_reason_en = "The method fits controlled generation and provides reusable evaluation design."
    assert find_pipeline._recommendation_reason_unusable(concise_reason_zh, zh=True) is False
    assert find_pipeline._recommendation_reason_unusable(concise_reason_en, zh=False) is False
    assert find_pipeline._recommendation_reason_unusable(
        "论文在无需额外标注的条件下完成生成，并帮助迁移评测设计。", zh=True
    ) is False
    assert find_pipeline._recommendation_reason_unusable(
        "The method works without extra labels and helps transfer its evaluation design.", zh=False
    ) is False
    reason_zh = (
        "论文研究条件蛋白生成中的结构约束，与项目关注的可控蛋白设计问题直接契合。"
        "其条件编码与生成评测能够帮助比较现有路线的约束表达能力，并为实验基线选择提供依据。"
        "方法中的条件注入方式、消融设置和结构质量指标都可迁移到后续模型设计与验证。"
    )
    reason_en = (
        "The paper studies structural constraints for conditional protein generation, directly matching the controlled protein-design topic. "
        "Its conditioning and evaluation setup helps compare constraint representations and choose experimental baselines. "
        "The conditioning mechanism, ablations, and structure-quality metrics are transferable to later model design and validation."
    )
    item = {
        "title": "Reusable protein design implementation",
        "abstract": "This work studies reusable protein design methods.",
        "reason_zh": reason_zh,
        "reason_en": reason_en,
        "fit_explanation": "其方法可复用于您的模型开发。",
    }
    find_pipeline._ensure_recommendation_readability(item, None)

    assert item["reason_zh"] == reason_zh
    assert item["reason_en"] == reason_en
    assert item.get("reason_quality_invalid") is None
    assert "您的" not in item["fit_explanation"]
    fixed_opener = "对当前研究方向来说，该论文提供可借鉴的方法结构、评测信号和实验设计参考，能够支持后续研究。"
    assert find_pipeline._recommendation_reason_has_generic_opener(fixed_opener, zh=True) is True
    assert find_pipeline._recommendation_reason_unusable(fixed_opener, zh=True) is True
    assert find_pipeline.FINAL_LLM_SCORE_CACHE_PROMPT_POLICY == "final_title_abstract_prompt_v32_natural_recommendation_reason"
    source = (ROOT / "modules" / "finding" / "scripts" / "flow" / "pipeline.py").read_text(encoding="utf-8")
    assert "do not use a prescribed opening, generic research-direction boilerplate" in source
    assert "def zh_reason()" not in source


def test_find_arxiv_title_match_does_not_write_cross_run_cache(monkeypatch, tmp_path):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]
    state_dir = tmp_path / "runtime" / "state"
    monkeypatch.setattr(find_support, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(find_support.time, "sleep", lambda *_args, **_kwargs: None)

    class FakeResponse:
        status_code = 200
        text = """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2601.00001</id>
            <title>Universal Topic Retrieval for Research Workflows</title>
            <summary>A reusable retrieval method for research workflow topic matching and audit.</summary>
            <author><name>Example Author</name></author>
          </entry>
        </feed>
        """

        def raise_for_status(self):
            return None

    monkeypatch.setattr(find_support.requests, "get", lambda *_args, **_kwargs: FakeResponse())
    papers = [{"title": "Universal Topic Retrieval for Research Workflows", "authors": ""}]

    find_support.enrich_with_arxiv_title_match(papers, limit=1)

    assert papers[0]["abstract"].startswith("A reusable retrieval method")
    assert papers[0]["metadata"]["abstract_source"] == "arxiv_title_match"
    assert not hasattr(find_support, "_load_arxiv_title_match_cache")
    assert not hasattr(find_support, "_save_arxiv_title_match_cache")
    assert not (state_dir / "arxiv_title_match_cache.json").exists()


def test_find_journal_fetch_marks_coverage_limited_after_collecting_dates(monkeypatch):
    _load_find_pipeline()
    find_support = sys.modules["support.find_support"]

    class FakeResponse:
        text = "<empty />"

        def json(self):
            return {"message": {"items": []}}

    nature_item = {
        "id": "nature_recent",
        "source": "nature",
        "title": "Recent conditional protein generation article",
        "metadata": {"published": "2026-06-01"},
        "url": "https://example.test/nature/recent",
    }
    science_item = {
        "id": "science_recent",
        "source": "science",
        "title": "Recent conditional protein generation report",
        "metadata": {"published": "2026-06-02", "doi": "10.1126/test.1"},
        "url": "https://example.test/science/recent",
    }

    monkeypatch.setenv("NATURE_MAX_PAGES_PER_JOURNAL", "1")
    monkeypatch.setenv("SCIENCE_MAX_CROSSREF_PAGES_PER_JOURNAL", "1")
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(find_support, "_parse_nature_feed", lambda *_args, **_kwargs: [dict(nature_item)])
    monkeypatch.setattr(find_support, "_parse_nature_listing_html", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(find_support, "enrich_nature_details", lambda items, **_kwargs: (list(items), {"attempted": len(items)}))
    monkeypatch.setattr(find_support, "_parse_science_crossref_items", lambda *_args, **_kwargs: [dict(science_item)])
    monkeypatch.setattr(find_support, "_parse_science_feed", lambda *_args, **_kwargs: [])

    nature_papers, nature_status = find_support.fetch_nature_portfolio(
        ["nature"],
        ["article"],
        max_items=10,
        start_date="2025-06-24",
        end_date="2026-06-24",
    )
    science_papers, science_status = find_support.fetch_science_family(
        ["science"],
        ["Research Article"],
        max_items=10,
        start_date="2025-06-24",
        end_date="2026-06-24",
    )

    assert nature_papers == [nature_item]
    assert nature_status["coverage_limited"] is True
    assert nature_status["limited"] is True
    assert nature_status["date_coverage"]["oldest"] == "2026-06-01"
    assert "coverage did not reach requested start_date 2025-06-24" in nature_status["message"]
    assert science_papers == [science_item]
    assert science_status["coverage_limited"] is True
    assert science_status["limited"] is True
    assert science_status["date_coverage"]["oldest"] == "2026-06-02"
    assert "coverage did not reach requested start_date 2025-06-24" in science_status["message"]


def test_find_title_index_cache_rejects_one_row_partial_core_venue(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    runtime = workspace / "runtime"
    state_dir = runtime / "state"
    state_dir.mkdir(parents=True)
    _isolate_find_runtime(monkeypatch, find_pipeline, runtime)

    venue = {"id": "openreview_iclr", "name": "ICLR"}
    audit = {
        "status": "partial",
        "source_verified": True,
        "complete": False,
        "title_index_complete": False,
        "title_index_completeness_status": "partial",
        "adapter": "openreview",
        "paper_count": 1,
        "has_abstracts": True,
        "any_abstracts": True,
        "has_official_categories": True,
        "category_status": "official_or_cached_categories",
        "source_scope": "openreview_official_venue_notes",
        "official_title_index_verified": False,
        "official_accepted_list_verified": False,
    }
    rows = _venue_cache_rows(1, audit)
    cache = {
        "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
        "entries": {
            find_pipeline._venue_title_index_cache_key(venue, [2026]): {
                "schema": find_pipeline.VENUE_TITLE_INDEX_CACHE_SCHEMA_VERSION,
                "venue_id": "openreview_iclr",
                "venue_name": "ICLR",
                "years": [2026],
                "adapter": "openreview",
                "papers": rows,
                "count": len(rows),
            }
        },
    }
    (state_dir / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")

    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2026], limit=1)

    assert loaded == []
    assert adapter == "none"
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows, "openreview") is False


def test_reading_module_rejects_find_and_project_actions():
    reading_main = _load_reading_main()
    source = (ROOT / "modules" / "reading" / "main.py").read_text(encoding="utf-8")

    assert "current_find" not in source
    assert "--project" not in source
    try:
        reading_main.main(["--action", "current_find_research_plan"])
    except SystemExit as exc:
        assert "Unknown reading module action" in str(exc)
    else:
        raise AssertionError("Reading module accepted a Framework-only current-Find action")


def test_reading_generic_input_preserves_optional_locator_metadata():
    read_pipeline = _load_reading_pipeline()
    paper = read_pipeline._normalize_local_input_paper({
        "title": "Paper With Optional Locator Metadata",
        "source": "input",
        "recommendation_score": 9.5,
        "openreview_id": "pvbJsa0ia0",
        "metadata": {
            "paper_url": "https://openreview.net/forum?id=pvbJsa0ia0",
            "icml_event_id": "61459",
        },
    })

    assert paper["title"] == "Paper With Optional Locator Metadata"
    assert paper["recommendation_score"] == 9.5
    assert paper["openreview_id"] == "pvbJsa0ia0"
    assert paper["metadata"]["paper_url"].endswith("pvbJsa0ia0")


def test_reading_module_has_no_project_projection_helpers():
    reading_main = _load_reading_main()

    assert not hasattr(reading_main, "_write_current_find_runtime_artifacts")
    assert not hasattr(reading_main, "_load_current_find_input")


def test_framework_reading_bridge_prepares_input_and_syncs_outputs(tmp_path):
    bridge = _load_framework_script("bridges/reading_bridge.py", "framework_reading_bridge_contract")

    projects = tmp_path / "projects"
    reading_root = tmp_path / "modules" / "reading"
    project_root = projects / "demo"
    finding = project_root / "planning" / "finding"
    finding.mkdir(parents=True)
    (project_root / "state").mkdir(parents=True)
    (finding / "find_results.json").write_text(json.dumps({
        "run_id": "find_bridge",
        "strong_recommendations": [{
            "title": "Paper A",
            "venue": "ICLR",
            "year": 2026,
            "url": "https://example.test/a",
            "metadata": {
                "paper_url": "https://openreview.net/forum?id=paperA",
                "presentation_label": "ICLR 2026 Oral",
            },
        }],
    }), encoding="utf-8")

    prepared = bridge.prepare_current_find_read_input(
        "demo",
        projects_root=projects,
        reading_root=reading_root,
    )
    input_path = Path(prepared["input_json"])
    assert re.fullmatch(r"\d{8}T\d{6}\d{6}Z", prepared["reading_run_id"])
    assert input_path == reading_root / ".runtime" / "output" / prepared["reading_run_id"] / "input" / "source_input.json"
    prepared_payload = json.loads(input_path.read_text(encoding="utf-8"))
    assert prepared_payload["articles"][0]["title"] == "Paper A"
    assert prepared_payload["articles"][0]["metadata"]["paper_url"].endswith("paperA")
    assert prepared_payload["articles"][0]["presentation_type"] == "oral"

    reading_run_dir = reading_root / ".runtime" / "output" / prepared["reading_run_id"]
    read_md = "# 论文精读\n\n## 逐篇精读\n\n### 001. Paper A\n"
    (reading_run_dir / "read.md").write_text(read_md, encoding="utf-8")
    (reading_run_dir / "read_results.json").write_text(json.dumps({
        "run_id": prepared["reading_run_id"],
        "read_markdown_aggregation": {"valid": True, "mode": "deterministic_concat"},
        "reading_scoring": {"status": "complete", "expected_article_count": 1, "scored_article_count": 1},
        "items": [{
            "paper": prepared_payload["articles"][0],
            "full_text_packet": {"full_text_available": True, "full_text_chars": 1500},
            "artifacts": {"article_markdown": "papers/001/read.md"},
            "validation": {"full_text_ready": True, "deep_read_complete": True},
        }],
    }), encoding="utf-8")

    result = bridge.sync_current_find_read_outputs(
        "demo",
        result_payload={"run_dir": str(reading_run_dir), "run_id": prepared["reading_run_id"]},
        projects_root=projects,
        reading_root=reading_root,
    )

    assert result["status"] == "current_find_deep_read_complete"
    assert result["public_final_artifact_present"] is True
    assert (project_root / "planning" / "finding" / "read.md").read_text(encoding="utf-8") == read_md
    assert json.loads((project_root / "state" / "current_find_research_plan.json").read_text(encoding="utf-8"))["run_id"] == "find_bridge"
    project_run = project_root / "planning" / "finding" / "reading_runs" / prepared["reading_run_id"]
    assert (project_run / "read_results.json").is_file()
    assert not (reading_run_dir / "project_sync").exists()


def test_framework_reading_bridge_only_passes_conference_presentation_metadata():
    bridge = _load_framework_script("bridges/reading_bridge.py", "framework_reading_presentation_contract")

    poster = bridge._article_for_reading({
        "title": "Conference Paper",
        "venue": "NeurIPS",
        "year": 2025,
        "metadata": {"tier": "Poster"},
    }, 1)
    journal = bridge._article_for_reading({
        "title": "Journal Paper",
        "source": "journal",
        "venue": "Cell",
        "year": 2026,
        "presentation_type": "poster",
        "metadata": {"tier": "Poster"},
    }, 2)
    unlabeled = bridge._article_for_reading({
        "title": "Unlabeled Conference Paper",
        "source": "openreview",
        "venue": "ICLR",
        "year": 2026,
    }, 3)

    assert poster["presentation_type"] == "poster"
    assert "presentation_type" not in journal
    assert "presentation_type" not in unlabeled


def test_framework_run_module_current_find_read_injects_runtime_input(monkeypatch, tmp_path):
    run_module = _load_framework_run_module()

    reading_root = tmp_path / "modules" / "reading"
    run_dir = reading_root / ".runtime" / "output" / "20260709T010000000001Z"
    input_path = run_dir / "input" / "source_input.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text(json.dumps({"articles": [{"title": "Paper A"}]}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_prepare(project, read_limit=0):
        captured["prepared_project"] = project
        captured["prepared_read_limit"] = read_limit
        return {"input_json": str(input_path), "run_id": "find_bridge"}

    def fake_run_streaming(cmd, *, env):
        captured["cmd"] = cmd
        captured["env"] = env
        assert str(ROOT / "modules" / "reading" / "main.py") in cmd
        assert cmd[cmd.index("--action") + 1] == "read"
        assert "--input-json" in cmd
        assert cmd[cmd.index("--input-json") + 1] == str(input_path)
        assert "--project" not in cmd
        assert "--find-run-id" not in cmd
        assert "current_find_research_plan" not in cmd
        assert cmd[cmd.index("--claude-mode") + 1] == "auto"
        assert cmd[cmd.index("--read-workers") + 1] == "16"
        assert cmd.count("--input-json") == 1
        return 0, json.dumps({"run_dir": str(run_dir), "run_id": run_dir.name})

    def fake_sync(project, *, result_payload=None, stdout_text="", **_kwargs):
        captured["synced_project"] = project
        captured["sync_payload"] = result_payload
        captured["sync_stdout"] = stdout_text
        return {"status": "current_find_deep_read_complete", "public_final_artifact_present": True}

    monkeypatch.setattr(run_module, "prepare_current_find_read_input", fake_prepare)
    monkeypatch.setattr(run_module, "_run_streaming", fake_run_streaming)
    monkeypatch.setattr(run_module, "sync_current_find_read_outputs", fake_sync)

    rc = run_module._run_current_find_read_bridge(
        "current_find_research_plan",
        ["--project", "demo", "--force", "--read-limit", "0"],
    )

    assert rc == 0
    assert captured["prepared_project"] == "demo"
    assert captured["synced_project"] == "demo"
    assert captured["sync_payload"]["run_dir"] == str(run_dir)


def _load_claude_project_session():
    return _load_framework_script("runtime/claude_project_session.py", "framework_claude_project_session_policy")


def test_current_find_allows_controlled_idea_scoring_audit_write():
    session = _load_claude_project_session()

    assert session.current_find_tool_policy_issue(
        "Write",
        {"file_path": str(ROOT / "projects" / "protein" / "planning" / "finding" / "idea_scoring.json")},
        "current_find_read_idea_plan",
    ) == ""

    assert session.current_find_tool_policy_issue(
        "Write",
        {"file_path": str(ROOT / "projects" / "protein" / "state" / "idea_scoring.json")},
        "current_find_read_idea_plan",
    ) == session.CURRENT_FIND_FILE_WRITE_WHITELIST_POLICY

    unsafe = "/home/fmh/workspace/miniforge/envs/ar_taste/bin/python -c \"open('planning/finding/idea_scoring.json','w').write('{}')\""
    assert session.current_find_artifact_generator_policy_issue(unsafe, "current_find_read_idea_plan") == session.CURRENT_FIND_ARTIFACT_WRITER_POLICY

def test_framework_scripts_exposes_only_main_entrypoint():
    scripts = ROOT / "framework" / "scripts"
    assert {path.name for path in scripts.iterdir() if path.is_file()} == {"main.py"}


def test_all_stage_contracts_and_framework_dry_run_are_callable():
    for stage in STAGES:
        proc = subprocess.run([sys.executable, str(ROOT / "modules" / stage / "main.py"), "--contract"], cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, (stage, proc.stderr or proc.stdout)
        payload = json.loads(proc.stdout)
        assert payload["stage"] == stage
        expected_entrypoint = "main.py" if stage == "reading" else f"modules/{stage}/main.py"
        assert payload["entrypoint"] == expected_entrypoint
        assert payload["scripts_are_private_backend"] is True
        assert payload["required_external_inputs"]
        assert payload["artifacts_out"]
        if stage == "finding":
            assert payload["public_final_artifact"] == "find.md"
            assert "find.md" in payload["artifacts_out"]
            assert "find_results.json" in payload.get("machine_support_artifacts", [])
        if stage == "reading":
            assert payload["public_final_artifact"] == "read.md"
            assert payload["artifacts_out"][0] == "read.md"
            assert "read_results.json" in payload.get("machine_support_artifacts", [])

    run_id = "pytest_contract_dry_run"
    state_root = ROOT / "framework" / "workspace" / "pytest"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "framework" / "scripts" / "main.py"),
            "workflow",
            "run",
            "--mode",
            "dry-run",
            "--strategy",
            "deterministic",
            "--research-goal",
            "pytest contract smoke",
            "--run-id",
            run_id,
            "--state-root",
            str(state_root),
            "--no-contract-probe",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    status = json.loads((state_root / "runs" / run_id / "public" / "frontend_status.json").read_text(encoding="utf-8"))
    assert status["progress"] == {"completed": 7, "total": 7, "percent": 100.0}
    assert status["status"] == "paper_pipeline_finished"


def test_framework_only_stage_reports_single_stage_scope():
    run_id = "pytest_only_environment"
    state_root = ROOT / "framework" / "workspace" / "pytest"
    project = "pytest_only_environment"
    project_root = ROOT / "projects" / project
    plan_path = project_root / "state" / "experiment_plan.json"
    shutil.rmtree(project_root, ignore_errors=True)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    (project_root / "project.json").write_text(json.dumps({"name": project, "topic": "pytest"}), encoding="utf-8")
    plan_path.write_text(json.dumps({"title": "pytest plan", "repo_url": "https://github.com/example/repo"}), encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "framework" / "scripts" / "main.py"),
                "workflow",
                "run",
                "--mode",
                "dry-run",
                "--strategy",
                "deterministic",
                "--only-stage",
                "environment",
                "--project",
                project,
                "--research-goal",
                "pytest single stage",
                "--run-id",
                run_id,
                "--state-root",
                str(state_root),
                "--plan-json",
                str(plan_path),
                "--module-arg",
                f"environment=--plan {plan_path} --run-id {run_id}",
                "--no-contract-probe",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        status = json.loads((state_root / "runs" / run_id / "public" / "frontend_status.json").read_text(encoding="utf-8"))
        assert status["stage_scope"] == ["environment"]
        assert status["progress"] == {"completed": 1, "total": 1, "percent": 100.0}
        assert status["status"] == "stage_scope_finished"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_experimenting_domain_specific_probe_is_prompt_not_script():
    probe_script = ROOT / "modules" / "experimenting" / "scripts" / "execution" / "proteinshake_realdata_probe.py"
    prompt = ROOT / "modules" / "experimenting" / "prompts" / "real-data-smoke.md"
    audit = ROOT / "modules" / "experimenting" / "SCRIPT_AUDIT.md"

    assert not probe_script.exists()
    prompt_text = prompt.read_text(encoding="utf-8")
    audit_text = audit.read_text(encoding="utf-8")
    assert "Real Data Smoke Prompt" in prompt_text
    assert "Domain-specific probes" in prompt_text
    assert "proteinshake_realdata_probe.py" in audit_text
    assert "ProteinShake is a domain-specific probe" in audit_text


def test_experimenting_controller_contract_has_no_run_action(monkeypatch):
    experimenting_main = _load_experimenting_main()
    actions = experimenting_main.contract()["public_actions"]
    source = (ROOT / "modules" / "experimenting" / "main.py").read_text(encoding="utf-8")

    assert "work" in actions
    assert "chat" in actions
    assert "run" not in actions
    assert "autonomous_experiment" not in actions
    assert "claude_project_session.py" not in source
    assert not (ROOT / "modules" / "experimenting" / "scripts" / "orchestration" / "run_autonomous_experiment.py").exists()
    monkeypatch.setattr(experimenting_main, "_ensure_taste_management_env", lambda: None)
    monkeypatch.setattr(
        experimenting_main,
        "_create_run_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unknown action created a run")),
    )
    try:
        experimenting_main.main(["--action", "run"])
    except SystemExit as exc:
        assert "Unknown experimenting action" in str(exc)
    else:
        raise AssertionError("removed Experimenting run action was accepted")


def test_experimenting_controller_actions_create_no_main_run(monkeypatch):
    experimenting_main = _load_experimenting_main()
    calls = []
    monkeypatch.setattr(experimenting_main, "_ensure_taste_management_env", lambda: None)
    monkeypatch.setattr(
        experimenting_main,
        "_create_run_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("controller run created")),
    )
    monkeypatch.setattr(
        experimenting_main,
        "_run_script",
        lambda action, args, run_dir=None: calls.append((action, list(args), run_dir)) or 0,
    )

    assert experimenting_main.main(["--action", "chat", "--project", "demo", "--message", "status"]) == 0
    assert calls == [("chat", ["--project", "demo", "--message", "status"], None)]


def test_experimenting_controller_keeps_one_session_per_project(tmp_path, monkeypatch):
    controller = _load_experiment_controller()
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    controller_dir = tmp_path / "controllers" / "demo"
    controller_dir.mkdir(parents=True)
    monkeypatch.setattr(controller, "SESSION_INDEX", tmp_path / "controller_sessions.json")

    state = controller._controller_state(controller_dir, "demo", project_root)
    controller._save_state(controller_dir, project_root, state)
    reloaded = controller._controller_state(controller_dir, "demo", project_root)
    index = json.loads((tmp_path / "controller_sessions.json").read_text(encoding="utf-8"))

    assert reloaded["session_id"] == state["session_id"]
    assert index["sessions"]["demo"]["session_id"] == state["session_id"]
    assert index["sessions"]["demo"]["project_root"] == str(project_root)


def test_experimenting_controller_prioritizes_interrupting_web_message(tmp_path, monkeypatch):
    controller = _load_experiment_controller()
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    controller_dir = tmp_path / "controllers" / "demo"
    controller_dir.mkdir(parents=True)
    monkeypatch.setattr(controller, "SESSION_INDEX", tmp_path / "controller_sessions.json")
    state = controller._controller_state(controller_dir, "demo", project_root)
    state["queue"] = [
        {"message_id": "normal", "status": "queued", "created_at": "2026-07-11T00:00:00+00:00", "interrupt_current": False},
        {"message_id": "priority", "status": "queued", "created_at": "2026-07-11T00:00:01+00:00", "interrupt_current": True},
    ]
    controller._save_state(controller_dir, project_root, state)

    selected = controller._next_message(controller_dir, project_root)

    assert selected["message_id"] == "priority"


def test_experimenting_controller_prioritizes_queued_web_chat_over_module_work(tmp_path, monkeypatch):
    controller = _load_experiment_controller()
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    controller_dir = tmp_path / "controllers" / "demo"
    controller_dir.mkdir(parents=True)
    monkeypatch.setattr(controller, "SESSION_INDEX", tmp_path / "controller_sessions.json")
    state = controller._controller_state(controller_dir, "demo", project_root)
    state["queue"] = [
        {"message_id": "work", "kind": "work", "status": "queued", "created_at": "2026-07-11T00:00:00+00:00"},
        {"message_id": "web", "kind": "chat", "status": "queued", "created_at": "2026-07-11T00:00:01+00:00"},
    ]
    controller._save_state(controller_dir, project_root, state)

    selected = controller._next_message(controller_dir, project_root)

    assert selected["message_id"] == "web"


def test_experimenting_controller_reports_busy_web_message_as_queued(tmp_path, monkeypatch):
    controller = _load_experiment_controller()
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    controllers = tmp_path / "controllers"
    monkeypatch.setattr(controller, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(controller, "CONTROLLERS_DIR", controllers)
    monkeypatch.setattr(controller, "SESSION_INDEX", tmp_path / "controller_sessions.json")
    controller_dir = controllers / "demo"
    controller_dir.mkdir(parents=True)
    state = controller._controller_state(controller_dir, "demo", project_root)
    state["busy"] = True
    controller._save_state(controller_dir, project_root, state)
    events = []

    result = controller.run_controller_message(
        project="demo",
        kind="chat",
        message="优先检查当前实验",
        timeout_sec=2,
        permission_mode="bypassPermissions",
        interrupt_current=False,
        dry_run=True,
        on_queued=events.append,
    )

    assert result["queued"] is True
    assert events[0]["status"] == "queued"
    assert events[0]["message"] == "优先检查当前实验"


def test_experimenting_controller_does_not_overlap_live_orphan_process(tmp_path, monkeypatch):
    controller = _load_experiment_controller()
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    controllers = tmp_path / "controllers"
    monkeypatch.setattr(controller, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(controller, "CONTROLLERS_DIR", controllers)
    monkeypatch.setattr(controller, "SESSION_INDEX", tmp_path / "controller_sessions.json")
    monkeypatch.setattr(controller, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(controller, "_invoke_controller", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("overlap")))
    controller_dir = controllers / "demo"
    controller_dir.mkdir(parents=True)
    state = controller._controller_state(controller_dir, "demo", project_root)
    state.update({"busy": True, "active_pid": 4242, "active_id": "active"})
    state["queue"] = [{"message_id": "active", "status": "running", "kind": "work", "message": "continue"}]
    controller._save_state(controller_dir, project_root, state)

    result = controller.run_controller_message(
        project="demo",
        kind="chat",
        message="排队等待",
        timeout_sec=0.05,
        permission_mode="bypassPermissions",
        interrupt_current=False,
        dry_run=True,
    )

    assert result["status"] == "queued_timeout"
    assert result["queued"] is True


def test_experimenting_validation_record_gate_enforces_write_order(tmp_path):
    controller = _load_experiment_controller()
    project_root = tmp_path / "projects" / "demo"
    state_dir = project_root / "state"
    state_dir.mkdir(parents=True)
    item = {
        "message_id": "work-1",
        "created_at": "2026-07-11T00:00:00+00:00",
        "registry_mtime_ns": 0,
    }
    registry = state_dir / "experiment_registry.json"
    registry.write_text(json.dumps([{
        "experiment_id": "exp-1",
        "status": "completed",
        "validation_finished_at": "2026-07-11T00:00:01+00:00",
        "validation_return_code": 0,
        "recorded_at": "2026-07-11T00:00:02+00:00",
    }]), encoding="utf-8")

    assert controller._validation_record_gate(project_root, item)["status"] == "pass"

    registry.write_text(json.dumps([{
        "experiment_id": "exp-2",
        "status": "completed",
        "validation_finished_at": "2026-07-11T00:00:03+00:00",
        "validation_return_code": 0,
        "recorded_at": "2026-07-11T00:00:02+00:00",
    }]), encoding="utf-8")
    blocked = controller._validation_record_gate(project_root, item)

    assert blocked["status"] == "blocked"
    assert any("recorded_at precedes validation_finished_at" in value for value in blocked["blockers"])


def test_experimenting_recording_is_prompt_not_import_script():
    importer = ROOT / "modules" / "experimenting" / "scripts" / "records" / "import_experiment_artifacts.py"
    record_tools = ROOT / "modules" / "experimenting" / "scripts" / "records" / "experiment_record_tools.py"
    helper = ROOT / "modules" / "experimenting" / "scripts" / "common" / "experiment_records.py"
    prompt = ROOT / "modules" / "experimenting" / "prompts" / "experiment-recording.md"
    manifest = json.loads((ROOT / "modules" / "experimenting" / "script_manifest.json").read_text(encoding="utf-8"))

    assert not importer.exists()
    assert not record_tools.exists()
    assert not helper.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "Experiment Recording Prompt" in text
    assert "experiment_record.json" in text
    assert "experiment_registry.json" in text
    assert "experiment_records.csv" in text
    assert "validation_finished_at" in text
    assert "recorded_at" in text
    assert "import_artifacts" not in manifest["public_actions"]
    assert "record_table" not in manifest["public_actions"]





def test_experimenting_controller_owns_project_records_without_framework_run_copy():
    controller_source = (
        ROOT / "modules" / "experimenting" / "scripts" / "orchestration" / "controller_session.py"
    ).read_text(encoding="utf-8")
    catalog_source = (
        ROOT / "framework" / "scripts" / "contracts" / "module_catalog.py"
    ).read_text(encoding="utf-8")

    assert 'cwd=str(project_root)' in controller_source
    assert 'projects/<project>' in (
        ROOT / "modules" / "experimenting" / "README.md"
    ).read_text(encoding="utf-8")
    assert '"experimenting": "work"' in catalog_source
    assert "claude_project_session.py" not in controller_source
