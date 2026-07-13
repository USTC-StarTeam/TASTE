from __future__ import annotations

import ast
import argparse
import importlib.util
import json
import multiprocessing
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
    path = ROOT / "modules" / "reading" / ".runtime" / "output" / run_id / "input" / "source_input.json"
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


def test_reading_http_client_classifies_jina_reader_service():
    http_client = _load_reading_common()

    assert http_client.service_from_url("https://r.jina.ai/http://duckduckgo.com/html/?q=test") == "reader"
    assert http_client.SERVICE_MIN_INTERVAL_SEC["reader"] >= 1.0


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
        "**来源：** arXiv 2026-06-01",
        "**论文链接：** URL：[论文页面](<https://arxiv.org/abs/2606.02386v2>)；PDF：[PDF](<https://arxiv.org/pdf/2606.02386v2>)",
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
    assert iclr_lines[0] == "**来源：** ICLR 2026"

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
    assert spotlight_lines[0] == "**来源：** ICML 2026 Spotlight"

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
    assert poster_lines[0] == "**来源：** NeurIPS 2025 Poster"

    dated_source_with_stray_presentation = claude_subagent.article_metadata_markdown_lines(
        {
            **arxiv_paper,
            "presentation_type": "oral",
        },
        {},
    )
    assert dated_source_with_stray_presentation[0] == "**来源：** arXiv 2026-06-01"

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
    assert journal_with_stray_tier[0] == "**来源：** Cell 2026"

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
    assert acquired_pdf_lines[1] == "**论文链接：** URL：[论文页面](<https://icml.cc/virtual/2026/poster/1>)；PDF：[PDF](<https://arxiv.org/pdf/2605.00001>)"

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
    assert openreview_official_pdf_lines[1] == "**论文链接：** URL：[论文页面](<https://openreview.net/forum?id=officialNote>)；PDF：[PDF](<https://openreview.net/pdf?id=officialNote>)"

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
    assert conference_with_arxiv_pdf_lines[0] == "**来源：** ICML 2026 Poster"

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
    assert "`**来源：** arXiv 2026-06-01`" in prompt
    assert "`**论文链接：** URL：[论文页面](<https://arxiv.org/abs/2606.02386v2>)；PDF：[PDF](<https://arxiv.org/pdf/2606.02386v2>)`" in prompt
    assert "两个 `$` 分别紧贴公式的首字符和尾字符" in prompt
    assert "开始行和结束行各自只写 `$$`" in prompt
    assert "可逆关系固定写成 `\\rightleftharpoons`" in prompt
    assert "行末使用 `\\\\`；附加行距写成 `\\\\[4pt]`" in prompt
    assert "栏目之间只保留一个空行" in prompt
    assert "英文原文摘要（翻译为中文）：" in prompt

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


def test_reading_standalone_title_reuses_article_cache_before_acquisition(monkeypatch):
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
    monkeypatch.setattr(read_pipeline, "refresh_latest_run", lambda directory: directory)
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


def test_reading_run_read_prepare_splits_full_text_and_reading_subagent_phases(monkeypatch):
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
    _cleanup_reading_output(run_id)
    _cleanup_reading_input("pytest_read_prepare_two_phase_contract")


def test_reading_run_read_uses_subagent_article_markdown_aggregation_and_audit(monkeypatch):
    monkeypatch.setenv("READING_DISABLE_ARTICLE_CACHE", "1")
    _cleanup_reading_output("pytest_read_subagent_md_contract")
    _cleanup_reading_input("pytest_read_subagent_md_contract")
    read_pipeline = _load_reading_pipeline()
    paper_sources = _load_reading_paper_sources()
    input_path = _write_reading_input("pytest_read_subagent_md_contract", {
        "articles": [
            {"source": "arxiv", "title": "subagent one", "paper_id": "p1", "url": "https://example.org/one"},
            {"source": "openreview", "title": "subagent two", "paper_id": "p2", "url": "https://example.org/two"},
        ]
    })
    run_id = _reading_run_id_from_input(input_path)

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
            "expected_output_path": str(expected_output_path),
            "expected_output_audit": {"exists": True, "valid_json": True},
            "nonruntime_artifact_audit": {"status": "passed", "problem_count": 0},
            "external_temp_artifact_audit": {"status": "passed", "problem_count": 0},
            "result_payload": payload,
        }

    def fake_run_claude_deep_read(prompt_path, run_path, expected_output_path, timeout_sec=1800, mode="auto"):
        assert mode == "run"
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
    assert payload["read_markdown_aggregation"]["valid"] is True
    assert "method_summary_table" not in payload["read_markdown_aggregation"]
    assert "ARTICLE_MD_BY_SUBAGENT" in read_md
    assert "## 逐篇精读" in read_md
    assert "## 方法总结表格" not in read_md
    assert "| 序号 | 论文 | 来源 | 方法类别 | 核心机制 | 关键实验/指标 | 主要优点 | 主要局限 | 可借鉴点 |" not in read_md
    assert "原论文摘要（中文）" not in read_md
    assert all(item["validation"]["deep_read_complete"] is True for item in payload["items"])
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
    assert cmd[cmd.index("--add-dir") + 1] == "."
    assert cmd[cmd.index("--add-dir") + 1] != str(reading_root)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["TMPDIR"] == "tmp"
    assert env["TMP"] == "tmp"
    assert env["TEMP"] == "tmp"
    assert env["TASTE_READING_RUN_DIR"] == "."
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


def test_reading_blocked_reason_carries_prior_openreview_browser_cooldown():
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

    assert reason["code"] == "blocked_openreview_403_no_verified_open_full_text"
    assert any(item.get("cooldown_reason") == "openreview_login_page_network_error" for item in reason["openreview_reasons"])


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

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(read_pipeline.requests, "Session", lambda: FakeSession())

    urls = read_pipeline._duckduckgo_result_urls('"exact title"', limit=5)

    assert [item["url"] for item in urls] == ["https://example.test/a.pdf", "https://example.test/b"]
    assert all(item["accepted"] for item in urls)


def test_reading_duckduckgo_result_urls_rejects_challenge_page(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://html.duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/html; charset=utf-8", "retry-after": ""}
        content = b"<html></html>"
        text = "<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.</body></html>"

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(read_pipeline.requests, "Session", lambda: FakeSession())

    urls = read_pipeline._duckduckgo_result_urls('"exact title"', limit=5)

    assert urls
    assert all(item["accepted"] is False for item in urls)
    assert all(item["reason"] == "duckduckgo_challenge" for item in urls)


def test_reading_duckduckgo_direct_search_defaults_to_one_attempt(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    calls: list[dict] = []

    class FakeSession:
        def get(self, *_args, **kwargs):
            calls.append(kwargs)
            raise read_pipeline.requests.exceptions.SSLError("synthetic ssl eof")

    monkeypatch.delenv("READING_DDG_DIRECT_ATTEMPTS", raising=False)
    monkeypatch.delenv("READING_DDG_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(read_pipeline.requests, "Session", lambda: FakeSession())

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


def test_reading_duckduckgo_reader_no_results_is_not_accepted(monkeypatch):
    read_pipeline = _load_reading_pipeline()

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://duckduckgo.com/html/?q=test"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        content = b"challenge"
        text = "Markdown Content:\nUnfortunately, bots use DuckDuckGo too.\nPlease complete the following challenge."

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
        '"Differentiable Lifting for Topological Neural Networks" PDF',
        '"Differentiable Lifting for Topological Neural Networks"',
        '"Differentiable Lifting for Topological Neural Networks" "Jorge Franco"',
    ]
    assert any(item.get("kind") == "search_query_budget" and item.get("used_query_count") == 3 for item in candidates)


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
    assert enabled_values == [True]
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


def test_reading_biorxiv_cooldown_skip_uses_challenge_blocker():
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
                }
            ],
            "selected": {},
        },
        {},
        {},
    )

    assert reason["code"] == "blocked_biorxiv_official_challenge_no_verified_open_full_text"


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
    paper_sources = _load_reading_paper_sources()

    class FakeResponse:
        status_code = 200
        url = "https://r.jina.ai/http://openreview.net/pdf?id=vDlkJewkDu"
        headers = {"content-type": "text/plain; charset=utf-8", "retry-after": ""}
        text = "Verifying your browser before accessing OpenReview. Complete the check below."
        content = text.encode("utf-8")

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

    def fake_page_candidates(_paper, url, *, kind, scan_assets=False, allow_pdf_text_identity_check=False):
        seen_urls.append(url)
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
    assert candidates[0]["pdf_url"] == "https://openreview.net/pdf/hash.pdf"
    assert candidates[0]["requires_pdf_text_identity_check"] is True


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


def test_reading_classifies_conference_virtual_without_full_text_locator():
    paper_sources = _load_reading_paper_sources()

    reason = paper_sources._blocked_full_text_reason(
        {"title": "Flexibility-Aware Geometric Latent Diffusion for Full-Atom Peptide Design", "url": "https://icml.cc/virtual/2026/poster/62058"},
        {"attempts": [], "candidate_discovery": []},
        {"attempts": [{"accepted": False, "url": "https://icml.cc/virtual/2026/poster/62058", "reason": "conference_poster_page_is_not_paper_full_text"}]},
        {},
    )

    assert reason["code"] == "blocked_conference_virtual_without_full_text_locator"
    assert "不能使用摘要" not in reason["message_zh"]


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
    import run_module

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
    assert rows[0]["pdf_url"].endswith("abc-Paper-Conference.pdf")
    assert rows[2]["authors"] == "Dana D., Evan E."
    assert rows[2]["track"] == "Datasets and Benchmarks Track"
    assert rows[2]["category"] == "Datasets and Benchmarks Track"


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
        max_fetch_papers=10,
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


def test_find_arxiv_targeted_complete_raw_pool_ignores_candidate_limit(monkeypatch):
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
        max_items=1,
        start_date="2026-06-01",
        end_date="2026-06-24",
        targeted_queries=[("topic:test", "all:protein AND submittedDate:[202606010000 TO 202606242359]")],
    )

    assert len(papers) == 2
    assert status["candidate_limit"] == 1
    assert status["targeted_complete_scan"] is True
    assert status["target_count_reached"] is False


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
        max_items=1,
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
        max_items=1,
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

    def fake_request(url, *_args, **_kwargs):
        cursor = int(str(url).rstrip("/").split("/")[-2])
        seen_cursors.append(cursor)
        return FakeResponse(cursor)

    monkeypatch.setenv("BIORXIV_COMPLETE_WINDOW", "1")
    monkeypatch.setenv("BIORXIV_PARALLEL_PAGES", "3")
    monkeypatch.setenv("BIORXIV_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", fake_request)

    papers, status = find_support.fetch_biorxiv(
        ["bioinformatics"],
        max_items=1,
        start_date="2026-06-01",
        end_date="2026-06-02",
        search_phrases=["protein design"],
    )

    assert len(papers) == 61
    assert sorted(seen_cursors) == [0, 30, 60]
    assert status["complete_window_scan"] is True
    assert status["parallel_page_fetch_used"] is True
    assert status["pages_fetched"] == 3
    assert status["stopped_reason"] == "full window scanned"


def test_find_biorxiv_targeted_seed_ignores_candidate_limit(monkeypatch):
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
                        },
                        {
                            "doi": "10.1101/target2",
                            "title": "Targeted bioRxiv seed two",
                            "abstractText": "Another targeted seed for conditional protein design.",
                            "firstPublicationDate": "2026-06-01",
                            "authorString": "B. Author",
                        },
                    ]
                }
            }

    monkeypatch.setenv("BIORXIV_OPENALEX", "0")
    monkeypatch.setenv("EUROPEPMC_REQUEST_SPACING_SEC", "0")
    monkeypatch.setattr(find_support, "_request", lambda *_args, **_kwargs: FakeResponse())

    papers, status = find_support.fetch_biorxiv_targeted(
        ["protein design"],
        max_items=1,
        start_date="2026-06-01",
        end_date="2026-06-02",
    )

    assert len(papers) == 2
    assert status["candidate_limit"] == 1
    assert status["target_count_reached"] is False
    assert status["raw_item_limit"] is None


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
    status = find_pipeline._annotate_source_completeness("nature", {"ok": True, "limited": False, "count": 1}, rows, params, min_count=1)

    assert not hasattr(find_pipeline, "_write_source_raw_cache")
    assert not hasattr(find_pipeline, "_load_source_raw_cache_seed")
    assert not (tmp_path / "runtime" / "state" / "source_raw_cache.json").exists()
    assert status["min_count_required"] == 1
    assert "cache_min_count_required" not in status


def test_find_public_recommendation_text_allows_implementation_and_hides_profile_label():
    find_pipeline = _load_find_pipeline()

    reason_en = (
        "This open-source implementation is useful for the current research direction "
        "because it provides reusable method structure, evaluation signals, and scope risks."
    )
    reason_zh = "对当前研究画像的核心价值：它提供可复用的方法结构、评测信号和摘要层面的风险边界。"

    assert find_pipeline._has_internal_find_public_text(reason_en, zh=False) is False
    sanitized = find_pipeline._sanitize_public_recommendation_text(reason_zh, zh=True)
    assert "当前研究画像" not in sanitized
    assert "当前研究方向" in sanitized
    assert "当前项目的" in find_pipeline._sanitize_public_recommendation_text("其方法可复用于您的模型开发。", zh=True)
    sanitized_en = find_pipeline._sanitize_public_recommendation_text(
        "The title and abstract connect clearly to the research profile and offer reusable method value.",
        zh=False,
    )
    assert "research profile" not in sanitized_en.lower()
    assert "current research direction" in sanitized_en.lower()
    item = {
        "title": "Reusable protein design implementation",
        "abstract": "This work studies reusable protein design methods.",
        "reason_zh": "对当前研究方向来说，该工作提供可复用的方法结构、评测信号、风险边界和实验设计参考。",
        "reason_en": "This work provides reusable method structure, evaluation signals, risk boundaries, and project guidance for the current research direction.",
        "fit_explanation": "其方法可复用于您的模型开发。",
    }
    find_pipeline._ensure_recommendation_readability(item, None)
    assert "您的" not in item["fit_explanation"]


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
    bridge = _load_framework_script("auto_research/reading_bridge.py", "framework_reading_bridge_contract")

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
    bridge = _load_framework_script("auto_research/reading_bridge.py", "framework_reading_presentation_contract")

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
    return _load_framework_script("claude_project_session.py", "framework_claude_project_session_policy")


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
            str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"),
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
                str(ROOT / "framework" / "scripts" / "orchestration" / "run_taste_framework.py"),
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
        ROOT / "framework" / "scripts" / "taste_backend" / "contracts" / "module_catalog.py"
    ).read_text(encoding="utf-8")

    assert 'cwd=str(project_root)' in controller_source
    assert 'projects/<project>' in (
        ROOT / "modules" / "experimenting" / "README.md"
    ).read_text(encoding="utf-8")
    assert '"experimenting": "work"' in catalog_source
    assert "claude_project_session.py" not in controller_source
