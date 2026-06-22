from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")


def _load_experiment_runner():
    experimenting_module_root = ROOT / "modules" / "experimenting"
    for name in ["experiment_plan", "experiment_records", "file_utils", "runtime_environment"]:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "experimenting_run_autonomous_experiment",
        experimenting_module_root / "scripts" / "orchestration" / "run_autonomous_experiment.py",
    )
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def _load_reading_main():
    spec = importlib.util.spec_from_file_location("reading_main_cli", ROOT / "modules" / "reading" / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_find_pipeline():
    finding_module_root = ROOT / "modules" / "finding"
    for path in [ROOT / "framework" / "scripts", ROOT / "web" / "backend", finding_module_root, finding_module_root / "scripts"]:
        value = str(path)
        if value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)
    for name in list(sys.modules):
        if name == "find_support" or name == "find_pipeline" or name == "sources" or name.startswith("sources."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("finding_find_pipeline_contract", finding_module_root / "scripts" / "find_pipeline.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _venue_cache_rows(count: int, audit: dict) -> list[dict]:
    return [
        {
            "id": f"paper_{index}",
            "title": f"Verified venue paper {index}",
            "venue": "ICLR",
            "year": 2026,
            "metadata": {"venue_metadata_audit": dict(audit)},
        }
        for index in range(count)
    ]


def _load_environment_module(module_name: str, relative_path: str):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, str(sys.path.pop(sys.path.index(str(environment_module_root)))))
    spec = importlib.util.spec_from_file_location(module_name, environment_module_root / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_current_find_read_public_projection_is_compact_and_katex_ready():
    spec = importlib.util.spec_from_file_location(
        "reading_current_find_contract",
        ROOT / "modules" / "reading" / "scripts" / "orchestration" / "ensure_current_find_research_plan.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    long_method = (
        "该方法定义生成目标 $L_{gen}=\\sum_i \\log p_\\theta(y_i|x)$，并用候选集合重排。"
        "它先编码输入，再通过噪声条件模块生成候选，最后按偏好分数筛选。"
        * 30
    )
    reading = {
        "paper_id": "p1",
        "title": "Readable Formula Paper",
        "venue": "ICLR",
        "year": 2026,
        "full_text_available": True,
        "full_text_status": "pdf_text_read",
        "abstract_zh": "本文研究生成式推荐中的偏好建模问题，并给出可复现实验。" * 8,
        "motivation_zh": "现有方法难以同时处理生成质量和偏好约束，因此需要显式目标。" * 6,
        "method_details_zh": long_method,
        "experiments_zh": "实验覆盖两个数据集、多个 baseline、NDCG@10 与 Recall@20，并报告消融。" * 10,
        "limitations_zh": "主要风险是候选生成成本和长尾样本迁移，需要额外审计。" * 8,
        "method_advantages_zh": ["目标函数清晰，容易映射到现有训练循环。" * 3],
        "method_disadvantages_zh": ["生成候选较多时推理成本上升。" * 4],
    }

    public = module.build_public_reading_views([reading])
    assert len(public) == 1
    assert public[0]["public_formulas"]
    assert public[0]["public_formulas"][0].startswith("$")
    assert "\\textit" not in json.dumps(public, ensure_ascii=False)
    assert len(public[0]["method_details_zh"]) < len(long_method)

    par_method = (
        "PAR的形式化建模框架如下：给定Cα结构x∈R^{L×3}，"
        "定义分解分布将x分解为集合X={x1,...,xn}（其中xn=x），"
        "然后通过尺度级自回归p_θ(x)=∏_{i=1}^n p_θ(x_i|X_{<i})生成结构。"
    )
    par_public = module.build_public_reading_views([{**reading, "paper_id": "par", "method_details_zh": par_method}])[0]
    par_payload = json.dumps(par_public, ensure_ascii=False)
    assert not re.search(r"X=\{x1,\.(?:\s|$)", par_payload)
    assert par_public["public_formulas"] == []

    noisy_method = (
        "跨专家接口包含表示级锚定嵌入 e_gen_i = e(k_hat_i) + W_proj h_LLM_i，"
        "扩散目标 L_gen = E_t,ε [1/|Z_x| Σ_i || ε_i - ε_θ(...) ||^2_2] 通过锚定路径回传。"
        "阶段I训练10K步，lr=1e-3，批大小256；外循环K≈20轮；Score>15且X={x1,...,xn}。"
    )
    noisy_public = module.build_public_reading_views([{**reading, "paper_id": "noisy", "method_details_zh": noisy_method, "method_advantages_zh": [noisy_method]}])[0]
    noisy_payload = json.dumps(noisy_public, ensure_ascii=False)
    assert noisy_public["public_formulas"] == []
    assert "e_gen_i" not in noisy_payload
    assert "L_gen = E_t" not in noisy_payload
    assert "lr=1e-3" not in noisy_payload
    assert "K≈20" not in noisy_payload
    assert "Score>15" not in noisy_payload
    assert "X={x1" not in noisy_payload

    rendered = module.render_read_md([reading, {**reading, "paper_id": "par", "title": "PAR", "method_details_zh": par_method}], "find_test_run")
    assert "### 数学/形式化" in rendered
    assert "L_{gen}" in rendered
    assert not re.search(r"X=\{x1,\.(?:\s|$)", rendered)
    assert "未单独抽取安全、完整的公式" in rendered
    paragraphs = [chunk.strip() for chunk in rendered.split("\n\n") if chunk.strip()]
    assert max(len(chunk) for chunk in paragraphs) < 1800


def test_find_neurips_official_papers_parser_reads_papers_nips_hash_links():
    find_pipeline = _load_find_pipeline()
    parser = sys.modules["sources.parsing"]
    html = """
    <html><body><ul>
      <li><div><a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">A Reliable Test-Time Scaling Method</a> Alice A., Bob B. <span>Main Conference Track</span></div></li>
      <li><div><a href="/paper_files/paper/2025/hash/def-Abstract-Datasets_and_Benchmarks_Track.html">Benchmarking Protein Models at Scale</a> Chen C. <span>Datasets and Benchmarks Track</span></div></li>
    </ul></body></html>
    """

    rows = parser._parse_neurips_official_papers_list(html, "https://papers.nips.cc/paper_files/paper/2025", 100)

    assert len(rows) == 2
    assert rows[0]["source"] == "neurips_official_papers"
    assert rows[0]["title"] == "A Reliable Test-Time Scaling Method"
    assert rows[0]["authors"] == "Alice A., Bob B."
    assert rows[0]["track"] == "Main Conference Track"
    assert rows[0]["pdf_url"].endswith("abc-Paper-Conference.pdf")


def test_find_cache_store_keeps_official_neurips_index_over_smaller_dblp(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(find_pipeline, "WORKFLOW_RUNTIME_DIR", workspace / "runtime")
    venue = {"id": "ccf_ai_conference_a_neurips_conference_on_neural_information_processing_systems", "name": "NeurIPS"}

    official_audit = {
        "status": "complete",
        "source_verified": True,
        "complete": True,
        "title_index_complete": True,
        "title_index_completeness_status": "complete",
        "adapter": "neurips_official_papers",
        "paper_count": 120,
        "has_abstracts": False,
        "any_abstracts": False,
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
    official_rows = _venue_cache_rows(120, official_audit)
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
    scoped_state = workspace / "modules" / "finding" / ".runtime" / "protein" / "state"
    scoped_state.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(find_pipeline, "WORKFLOW_RUNTIME_DIR", workspace / "runtime")

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
    rows = _venue_cache_rows(80, audit)
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
    (scoped_state / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")

    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2026], limit=1)

    assert adapter == "openreview_reference_cache"
    assert len(loaded) == 1
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows, "openreview_reference") is True
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows[:1], "openreview_reference") is False


def test_find_title_index_cache_discovers_scoped_runtime_dblp_title_corpus(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    scoped_state = workspace / "modules" / "finding" / ".runtime" / "protein" / "state"
    scoped_state.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(find_pipeline, "WORKFLOW_RUNTIME_DIR", workspace / "runtime")

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
    (scoped_state / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")

    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2026], limit=1)
    fields = find_pipeline._venue_metadata_status_fields(find_pipeline._online_venue_metadata_audit(rows, "dblp"))

    assert adapter == "dblp_cache"
    assert len(loaded) == 1
    assert fields["source_integrity_status"] == "passed"
    assert fields["metadata_completeness_status"] == "title_index_only"
    assert fields["official_title_index_verified"] is False
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows, "dblp") is True


def test_find_source_health_refresh_replaces_stale_one_row_source_artifact(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    scoped_state = workspace / "modules" / "finding" / ".runtime" / "protein" / "state"
    scoped_state.mkdir(parents=True)
    artifact_dir = workspace / "projects" / "protein" / "planning" / "finding"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(find_pipeline, "WORKFLOW_RUNTIME_DIR", workspace / "runtime")

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
    rows = _venue_cache_rows(80, complete_audit)
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
    (scoped_state / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")
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


def test_find_title_index_cache_rejects_one_row_partial_core_venue(monkeypatch, tmp_path):
    find_pipeline = _load_find_pipeline()
    workspace = tmp_path / "workspace"
    scoped_state = workspace / "modules" / "finding" / ".runtime" / "protein" / "state"
    scoped_state.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(find_pipeline, "WORKFLOW_RUNTIME_DIR", workspace / "runtime")

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
    (scoped_state / "venue_title_indexes.json").write_text(json.dumps(cache), encoding="utf-8")

    loaded, adapter = find_pipeline._load_venue_title_index_cache(venue, [2026], limit=1)

    assert loaded == []
    assert adapter == "none"
    assert find_pipeline._venue_title_index_cache_rows_usable(venue, [2026], rows, "openreview") is False


def test_reading_cli_explicit_action_keeps_project_for_child_command(monkeypatch):
    reading_main = _load_reading_main()
    calls = []

    def fake_run_script(action, args):
        calls.append((action, list(args)))
        return 0

    monkeypatch.setattr(reading_main, "_run_script", fake_run_script)

    rc = reading_main.main(["--action", "current_find_research_plan", "--project", "protein", "--read-limit", "0", "--idea-count", "5", "--force"])

    assert rc == 0
    assert calls == [(
        "ensure_current_find_research_plan",
        ["--project", "protein", "--read-limit", "0", "--idea-count", "5", "--force"],
    )]


def test_reading_cli_positional_action_still_forwards_remaining_args(monkeypatch):
    reading_main = _load_reading_main()
    calls = []

    def fake_run_script(action, args):
        calls.append((action, list(args)))
        return 0

    monkeypatch.setattr(reading_main, "_run_script", fake_run_script)

    rc = reading_main.main(["current-find-research-plan", "--project", "protein", "--force"])

    assert rc == 0
    assert calls == [("ensure_current_find_research_plan", ["--project", "protein", "--force"])]


def test_reading_current_find_wrapper_imports_with_private_common_first():
    reading_main = _load_reading_main()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "modules" / "reading" / "scripts" / "ensure_current_find_research_plan.py"), "--help"],
        cwd=ROOT,
        env=reading_main._python_env(),
        text=True,
        capture_output=True,
        timeout=30,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "No module named 'common.read_ranking'" not in output
    assert "--project" in output


def _load_claude_project_session():
    framework_scripts = ROOT / "framework" / "scripts"
    if str(framework_scripts) not in sys.path:
        sys.path.insert(0, str(framework_scripts))
    spec = importlib.util.spec_from_file_location(
        "framework_claude_project_session_policy",
        framework_scripts / "claude_project_session.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_current_find_orchestrator():
    framework_scripts = ROOT / "framework" / "scripts"
    reading_scripts = ROOT / "modules" / "reading" / "scripts"
    reading_private = reading_scripts / "private"
    finding_scripts = ROOT / "modules" / "finding" / "scripts"
    for path in [str(framework_scripts), str(finding_scripts), str(reading_scripts), str(reading_private)]:
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    for name in ["common", "literature_policy", "project_paths", "runtime_env", "project_config"]:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "reading_current_find_orchestrator",
        reading_scripts / "orchestration" / "ensure_current_find_research_plan.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_current_find_derives_targeted_queries_from_claude_artifacts(tmp_path):
    orchestrator = _load_current_find_orchestrator()

    class Paths:
        state = tmp_path / "state"

    Paths.state.mkdir()
    ideas = {
        "run_id": "find_test",
        "source": orchestrator.CLAUDE_TAKEOVER_SOURCE,
        "ideas": [
            {
                "id": "idea_5",
                "title": "知识引导解耦可解释评估框架",
                "new_method": "Use ProtDiS representations and Flexible Kernels GP ranking for protein design evaluation.",
                "initial_experiment": "Validate ProtDiS, Flexible Kernels, PDFBench, and ProtDBench protocols before experiments.",
                "inspired_by": [
                    {"paper_id": "paper_9f58", "title": "Learning Protein Structure-Function Relationships through Knowledge-guided Representation Decomposition"},
                    {"paper_id": "paper_6f85", "title": "Flexible Kernels for Protein Property Prediction"},
                ],
            }
        ],
    }
    plans = {
        "run_id": "find_test",
        "source": orchestrator.CLAUDE_TAKEOVER_SOURCE,
        "plans": [
            {
                "plan_id": "plan_5",
                "idea_id": "idea_5",
                "title": "知识引导解耦的可解释生成评估框架实施计划",
                "selected_for_execution": True,
                "execute_next": True,
                "execution_selection": {"selected": True},
                "environment_requirements": ["ProtDiS encoder", "Flexible Kernels GP", "PDFBench and ProtDBench data"],
            }
        ],
    }

    queries = orchestrator.extract_targeted_search_queries(Paths, {}, ideas, plans, {})

    assert len(queries) >= 3
    assert any("ProtDiS" in query for query in queries)
    assert any("Flexible Kernels" in query for query in queries)


def test_all_stage_contracts_and_framework_dry_run_are_callable():
    for stage in STAGES:
        proc = subprocess.run([sys.executable, str(ROOT / "modules" / stage / "main.py"), "--contract"], cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, (stage, proc.stderr or proc.stdout)
        payload = json.loads(proc.stdout)
        assert payload["stage"] == stage
        assert payload["entrypoint"] == f"modules/{stage}/main.py"
        assert payload["scripts_are_private_backend"] is True
        assert payload["required_external_inputs"]
        assert payload["artifacts_out"]

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
    plan_path = state_root / "pytest_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"title": "pytest plan", "repo_url": "https://github.com/example/repo"}), encoding="utf-8")
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


def test_environment_dependency_policy_rewrites_pyg_conda_plan():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)
    normalize_environment_plan_commands = dependency_policy.normalize_environment_plan_commands

    plan = {
        "python_version": "3.9",
        "commands": [
            {"phase": "conda_create", "command": ["conda", "create", "-n", "rigid", "python=3.9", "pip", "-y"], "required": True},
            {"phase": "conda_install_pytorch", "command": ["conda", "install", "-n", "rigid", "pytorch>=2.5.1", "pytorch-cuda>=12.4", "-y"], "required": True},
            {"phase": "conda_install_pyg", "command": ["conda", "install", "-n", "rigid", "-c", "pyg", "pyg", "pytorch-scatter", "pytorch-sparse", "pytorch-cluster", "-y"], "required": True},
            {"phase": "verify_import", "command": ["conda", "run", "-n", "rigid", "python", "-c", "import torch_geometric"], "required": True},
        ],
    }
    machine = {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}

    normalized = normalize_environment_plan_commands(plan, machine=machine, policy_version="test-policy")
    commands = [row["command"] for row in normalized["commands"]]
    command_text = "\n".join(" ".join(command) for command in commands)

    assert normalized["python_version"] == "3.11"
    assert normalized["commands"][0]["command"] == ["conda", "create", "-n", "rigid", "python=3.11", "pip", "-y"]
    assert "torch==2.9.1+cu128" in command_text
    assert "torchvision==0.24.1+cu128" in command_text
    assert "torchaudio==2.9.1+cu128" in command_text
    assert "https://download.pytorch.org/whl/cu128" in command_text
    assert "https://data.pyg.org/whl/torch-2.9.1+cu128.html" in command_text
    assert "conda install -n rigid -c pyg pyg" not in command_text
    assert any(row["phase"] == "verify_pyg_cuda_import" for row in normalized["commands"])
    assert normalized["backend_dependency_policy"]["policy_version"] == "test-policy"
    assert len(normalized["plan_policy_rewrites"]) >= 4


def test_environment_rewrites_python_entrypoints_to_run_local_prefix():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    env_prefix = ROOT / "modules" / "environment" / "runs" / "pytest_run" / "conda_envs" / "rigid"
    conda_exe = "/home/fmh/workspace/miniforge/bin/conda"

    pip_command = autonomous_deploy.rewrite_command(["pip", "install", "torch"], conda_exe, "rigid", env_prefix)
    assert pip_command == [str(env_prefix / "bin" / "python"), "-m", "pip", "install", "torch"]
    assert autonomous_deploy.command_uses_conda_prefix(pip_command, env_prefix)
    assert autonomous_deploy._conda_prefix_tokens_have_setup_action(pip_command)

    run_command = autonomous_deploy.rewrite_command(
        ["conda", "run", "-n", "rigid", "python", "-c", "import torch; import dm_tree; from dm_tree import map_structure"],
        conda_exe,
        "rigid",
        env_prefix,
    )
    assert run_command == [str(env_prefix / "bin" / "python"), "-c", "import torch; import tree as dm_tree; from tree import map_structure"]
    assert autonomous_deploy.command_uses_conda_prefix(run_command, env_prefix)
    assert autonomous_deploy._conda_prefix_tokens_have_verify_action(run_command)


def test_environment_repo_review_falls_back_to_plan_github_candidates():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_repo_review",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    candidates = ["https://github.com/ZhanghanNi/RigidSSL", "https://github.com/Long-Kai/Steering-PLMs"]
    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(candidates, {"return_code": 0, "json": {}, "stdout_tail": "ready"})
    assert selected == candidates
    assert fallback is True
    assert "repo candidate review did not produce valid JSON" in issues

    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(
        candidates,
        {"return_code": 0, "json": {"status": "ready", "ordered_repo_urls": [candidates[1], candidates[0]]}},
    )
    assert selected == [candidates[1], candidates[0]]
    assert fallback is False


def test_environment_plan_schema_extracts_selected_idea_repo_urls(tmp_path):
    plan_schema = _load_environment_module("environment_plan_schema_selected_idea", "scripts/common/plan_schema.py")
    plan = {
        "selected_plan_id": "plan_5",
        "selected_idea_id": "idea_5",
        "selected_plan": {"plan_id": "plan_5", "idea_id": "idea_5", "title": "selected plan"},
        "selected_idea": {
            "id": "idea_5",
            "initial_experiment": "Use ProtDiS (https://github.com/protdis/protdis) and Flexible Kernels (https://github.com/GenerateBiomedicines/flexible-kernels).",
        },
    }

    normalized = plan_schema.normalize_plan(plan, tmp_path / "experiment_plan.json")

    assert normalized["repo_candidates"] == [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
    ]
    assert normalized["repo_candidate_specs"][0]["source"] == "selected_idea.initial_experiment"


def test_environment_discovery_recovers_official_replacement_repos_from_reject():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_discovery_recovery",
        "scripts/orchestration/autonomous_deploy.py",
    )
    discovered = {
        "status": "reject",
        "repo_url": "",
        "confidence": 0.0,
        "evidence": [
            "ProtDiS: planned https://github.com/protdis/protdis does not exist; official repository is https://github.com/AI-HPC-Research-Team/ProtDiS.",
            "MiAE/TEDBench: official repository is https://github.com/BorgwardtLab/TEDBench.",
        ],
        "reject_reason": "Flexible Kernels has no public code.",
    }

    candidates = autonomous_deploy.discovered_repo_candidates(discovered)

    assert [row["url"] for row in candidates] == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert candidates[0]["source"].startswith("discovery_text")

    reviewed = {
        "status": "reject",
        "evidence": ["Original candidate is wrong; official replacement is https://github.com/AI-HPC-Research-Team/ProtDiS."],
        "reject_reason": "Original plan URL was stale.",
    }
    recovered = autonomous_deploy.discovered_repo_candidates(reviewed)
    assert [row["url"] for row in recovered] == ["https://github.com/AI-HPC-Research-Team/ProtDiS"]


def test_environment_repo_review_reject_uses_known_replacements_not_freeform_short_names():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_review_reject_no_freeform_short_names",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    reviewed = {
        "status": "reject",
        "ordered_repo_urls": [],
        "evidence": [
            "GitHub API 对 https://github.com/protdis/protdis 返回 404，用户/组织 protdis 不存在；实际官方仓库为 AI-HPC-Research-Team/ProtDiS",
            "GitHub API 对 https://github.com/GenerateBiomedicines/flexible-kernels 返回 404，组织 GenerateBiomedicines 无公开 GitHub 存在；论文代码未公开发布",
            "GitHub API 对 https://github.com/tedbench/miae 返回 404；实际官方仓库为 BorgwardtLab/TEDBench (ICML 2026 oral)",
            "actual MiAE/TEDBench repo is github.com/BorgwardtLab/TEDBench",
        ],
        "reject_reason": "三个候选 URL 经 GitHub API 验证均返回 404 Not Found。protdis/protdis 的正确 URL 应为 AI-HPC-Research-Team/ProtDiS；tedbench/miae 的正确 URL 应为 BorgwardtLab/TEDBench；GenerateBiomedicines/flexible-kernels 对应的论文代码未公开发布。",
    }

    recovered = autonomous_deploy.discovered_repo_candidates(reviewed)
    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, {"return_code": 0, "json": reviewed})

    assert [row["url"] for row in recovered] == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert "https://github.com/MiAE/TEDBench" not in [row["url"] for row in recovered]
    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert "https://github.com/GenerateBiomedicines/flexible-kernels" not in selected
    assert fallback is False
    assert issues[0] == reviewed["reject_reason"]
    assert any("已知过期仓库候选已替换" in item for item in issues)
    assert any("暂无公开可克隆代码" in item for item in issues)


def test_environment_repo_review_reject_recovers_positive_owner_repo_short_names():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_review_reject_positive_owner_repo",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    reviewed = {
        "status": "reject",
        "ordered_repo_urls": [],
        "evidence": [
            "HTTP 404: https://github.com/protdis/protdis; the actual official repo is at AI-HPC-Research-Team/ProtDiS (HTTP 200).",
            "HTTP 404: https://github.com/tedbench/miae; the correct repository is BorgwardtLab/TEDBench, not MiAE/TEDBench.",
            "GenerateBiomedicines/flexible-kernels has no public code repository found.",
        ],
        "reject_reason": "Use verified official replacements only.",
    }

    recovered = autonomous_deploy.official_replacement_repo_candidates_from_review(reviewed, original)
    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, {"return_code": 0, "json": reviewed})

    recovered_urls = [row["url"] for row in recovered]
    assert recovered_urls == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert "https://github.com/MiAE/TEDBench" not in recovered_urls
    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert fallback is False
    assert issues[0] == reviewed["reject_reason"]


def test_environment_repo_review_recovered_replacements_preserve_original_candidate_order():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_recovered_replacement_order",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    reviewed = {
        "status": "reject",
        "ordered_repo_urls": [],
        "evidence": [
            "GenerateBiomedicines/flexible-kernels returns 404; the real repo is generatebio/lock_gp (https://github.com/generatebio/lock_gp).",
            "protdis/protdis -> AI-HPC-Research-Team/ProtDiS (https://github.com/AI-HPC-Research-Team/ProtDiS) official code.",
            "tedbench/miae -> BorgwardtLab/TEDBench (https://github.com/BorgwardtLab/TEDBench) official code.",
        ],
        "reject_reason": "Original URLs are stale.",
    }

    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, {"return_code": 0, "json": reviewed})

    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/generatebio/lock_gp",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert fallback is False
    assert issues[0] == "Original URLs are stale."


def test_environment_review_uses_verified_historical_lock_gp_replacement(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_historical_lock_gp_replacement",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    repo = tmp_path / "old_run" / "repos" / "generatebio_lock_gp_8513f55d"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        "[remote \"origin\"]\n	url = https://github.com/generatebio/lock_gp\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Flexible Kernels for Protein Property Prediction\nThis repo contains LOCK GP code.",
        encoding="utf-8",
    )

    rows = autonomous_deploy.recovered_repo_candidate_rows_from_review(
        {
            "status": "reject",
            "evidence": [
                "protdis/protdis actual official repo is AI-HPC-Research-Team/ProtDiS.",
                "GenerateBiomedicines/flexible-kernels may not be public.",
                "tedbench/miae correct repository is BorgwardtLab/TEDBench.",
            ],
            "reject_reason": "Some candidates are stale.",
        },
        original,
        tmp_path,
    )

    assert [row["url"] for row in rows] == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/generatebio/lock_gp",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert any(row["source"] == "historical_verified_clone" for row in rows)


def test_environment_repo_review_recovers_replacements_from_malformed_json_text(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_malformed_review_text_recovery",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    log_path = tmp_path / "claude_repo_candidate_review.log"
    log_path.write_text(
        '''--- STDOUT ---
| `protdis/protdis` | `AI-HPC-Research-Team/ProtDiS` | actual official repo |
| `GenerateBiomedicines/flexible-kernels` | `generatebio/lock_gp` | correct repo for Flexible Kernels |
| `tedbench/miae` | `BorgwardtLab/TEDBench` | correct repository for MiAE/TEDBench |
由于候选 URL 返回 404，审阅结论：拒绝。
''',
        encoding="utf-8",
    )
    review_result = {
        "return_code": 0,
        "status": "failed",
        "json": {},
        "stdout_tail": "输出已写入指定路径。",
        "log_path": str(log_path),
    }

    recovered = autonomous_deploy.official_replacement_repo_candidates_from_review(
        autonomous_deploy._text_review_payload_from_result(review_result),
        original,
    )
    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, review_result)

    assert "https://github.com/MiAE/TEDBench" not in [row["url"] for row in recovered]
    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/generatebio/lock_gp",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert fallback is False
    assert "valid JSON" in issues[0]


def test_environment_repo_review_ready_uses_known_stale_repo_replacements():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_known_stale_repo_replacements",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    reviewed = {
        "status": "ready",
        "ordered_repo_urls": list(original),
        "selected_reason": "三个候选均可信，按实验核心依赖排序。",
        "evidence": [
            "protdis/protdis：候选名称与论文方法名 ProtDiS 对应",
            "GenerateBiomedicines/flexible-kernels：作者机构一致",
            "tedbench/miae：MiAE 在实验阶段1环境验证中被列为依赖",
        ],
    }

    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, {"return_code": 0, "json": reviewed})

    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert fallback is False
    assert any("已知过期仓库候选已替换" in item for item in issues)
    assert any("暂无公开可克隆代码" in item for item in issues)



def test_environment_repo_review_ready_requires_explicit_github_replacements():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_review_ready_explicit_github_recovery",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    reviewed = {
        "status": "ready",
        "ordered_repo_urls": [original[0], original[1], original[2]],
        "selected_reason": "优先级沿用原始实验计划。",
        "evidence": [
            "搜索验证：protdis/protdis 对应 ICML 2026 论文，官方代码库在 github.com/AI-HPC-Research-Team/ProtDiS（候选URL可能重定向）",
            "搜索验证：GenerateBiomedicines/flexible-kernels 对应论文，Generate Biomedicines 为作者所属机构，但未明确给出替代仓库",
            "搜索验证：tedbench/miae 对应 ICML 2026 Oral 论文；实际主项目仓库为 github.com/BorgwardtLab/TEDBench，实验阶段需处理候选URL有效性",
        ],
        "reject_reason": "",
    }

    recovered = autonomous_deploy.official_replacement_repo_candidates_from_review(reviewed, original)
    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, {"return_code": 0, "json": reviewed})

    assert [row["url"] for row in recovered] == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert original[0] not in selected
    assert original[1] not in selected
    assert original[2] not in selected
    assert fallback is False
    assert "ready" in issues[0]
    assert any("已知过期仓库候选已替换" in item for item in issues)
    assert any("暂无公开可克隆代码" in item for item in issues)


def test_environment_repo_review_reject_recovers_official_replacements_only():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_review_reject_recovery",
        "scripts/orchestration/autonomous_deploy.py",
    )
    original = [
        "https://github.com/protdis/protdis",
        "https://github.com/GenerateBiomedicines/flexible-kernels",
        "https://github.com/tedbench/miae",
    ]
    reviewed = {
        "status": "reject",
        "ordered_repo_urls": [original[1], original[0], original[2]],
        "evidence": [
            "protdis/protdis is 404; actual official ProtDiS repo is https://github.com/AI-HPC-Research-Team/ProtDiS.",
            "tedbench/miae is 404; actual official MiAE repo is https://github.com/BorgwardtLab/TEDBench.",
        ],
        "reject_reason": "Original URLs are stale or inaccessible.",
    }

    selected, issues, fallback = autonomous_deploy.repo_candidates_after_review(original, {"return_code": 0, "json": reviewed})

    assert selected == [
        "https://github.com/AI-HPC-Research-Team/ProtDiS",
        "https://github.com/BorgwardtLab/TEDBench",
    ]
    assert fallback is False
    assert issues[0] == "Original URLs are stale or inaccessible."
    assert any("已知过期仓库候选已替换" in item for item in issues)
    assert any("暂无公开可克隆代码" in item for item in issues)


def test_environment_repo_manager_prefers_historical_clone_without_network(tmp_path, monkeypatch):
    repo_manager = _load_environment_module(
        "environment_repo_manager_historical_clone",
        "scripts/repository/repo_manager.py",
    )
    repo_url = "https://github.com/example/repo"
    runs_root = tmp_path / "runs"
    old_repo = runs_root / "old_run" / "repos" / repo_manager.repo_slug(repo_url)
    old_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=old_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "taste@example.com"], cwd=old_repo, check=True)
    subprocess.run(["git", "config", "user.name", "TASTE"], cwd=old_repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", repo_url + ".git"], cwd=old_repo, check=True)
    (old_repo / "README.md").write_text("historical clone\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=old_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=old_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=old_repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

    def fail_on_network(command, **kwargs):
        raise AssertionError(f"historical clone should avoid network command: {command}")

    monkeypatch.setattr(repo_manager, "run_logged", fail_on_network)
    current_repos = runs_root / "new_run" / "repos"
    result = repo_manager.clone_or_reuse(repo_url, current_repos, runs_root / "new_run" / "logs")

    assert result["exists"] is True
    assert result["head_commit"] == head
    assert result["clone_receipt"]["reused_historical_clone"] is True
    assert Path(result["repo_path"], "README.md").read_text(encoding="utf-8") == "historical clone\n"


def test_environment_repo_manager_skips_wrong_origin_workspace_historical_clone(tmp_path, monkeypatch):
    repo_manager = _load_environment_module(
        "environment_repo_manager_historical_clone_origin_guard",
        "scripts/repository/repo_manager.py",
    )
    repo_url = "https://github.com/generatebio/lock_gp"
    runs_root = tmp_path / "runs"
    bad_repo = runs_root / "newer_bad" / "repos" / repo_manager.repo_slug(repo_url)
    bad_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=bad_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "taste@example.com"], cwd=bad_repo, check=True)
    subprocess.run(["git", "config", "user.name", "TASTE"], cwd=bad_repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:USTC-StarTeam/TASTE.git"], cwd=bad_repo, check=True)
    (bad_repo / "工作状态.txt").write_text("TASTE status\n", encoding="utf-8")
    (bad_repo / "modules").mkdir()
    (bad_repo / "web").mkdir()
    subprocess.run(["git", "add", "."], cwd=bad_repo, check=True)
    subprocess.run(["git", "commit", "-m", "bad workspace copy"], cwd=bad_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    good_repo = runs_root / "older_good" / "repos" / "generatebio_lock_gp"
    good_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=good_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "taste@example.com"], cwd=good_repo, check=True)
    subprocess.run(["git", "config", "user.name", "TASTE"], cwd=good_repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/generatebio/lock_gp.git"], cwd=good_repo, check=True)
    (good_repo / "README.md").write_text("# Flexible Kernels for Protein Property Prediction\nLOCK GP\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=good_repo, check=True)
    subprocess.run(["git", "commit", "-m", "good lock gp"], cwd=good_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    good_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=good_repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

    def fail_on_network(command, **kwargs):
        raise AssertionError(f"historical clone should avoid network command: {command}")

    monkeypatch.setattr(repo_manager, "run_logged", fail_on_network)
    result = repo_manager.clone_or_reuse(repo_url, runs_root / "current" / "repos", runs_root / "current" / "logs")

    assert result["exists"] is True
    assert result["head_commit"] == good_head
    assert result["clone_receipt"]["reused_historical_clone"] is True
    assert result["clone_receipt"]["reused_from_repo_path"] == str(good_repo)
    copied = Path(result["repo_path"])
    assert (copied / "README.md").read_text(encoding="utf-8").startswith("# Flexible Kernels")
    assert not (copied / "工作状态.txt").exists()



def test_environment_repo_manager_rejects_wrong_origin_clone_result_and_reuses_history(tmp_path, monkeypatch):
    repo_manager = _load_environment_module(
        "environment_repo_manager_post_clone_origin_guard",
        "scripts/repository/repo_manager.py",
    )
    repo_url = "https://github.com/BorgwardtLab/TEDBench"
    runs_root = tmp_path / "runs"
    good_repo = runs_root / "older_good" / "repos" / "BorgwardtLab_TEDBench"
    good_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=good_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "taste@example.com"], cwd=good_repo, check=True)
    subprocess.run(["git", "config", "user.name", "TASTE"], cwd=good_repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/BorgwardtLab/TEDBench.git"], cwd=good_repo, check=True)
    (good_repo / "README.md").write_text("# TEDBench\nreal repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=good_repo, check=True)
    subprocess.run(["git", "commit", "-m", "good tedbench"], cwd=good_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    good_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=good_repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

    def fake_run_logged(command, **kwargs):
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            subprocess.run(["git", "config", "user.email", "taste@example.com"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.name", "TASTE"], cwd=target, check=True)
            subprocess.run(["git", "remote", "add", "origin", "git@github.com:USTC-StarTeam/TASTE.git"], cwd=target, check=True)
            (target / "工作状态.txt").write_text("TASTE status\n", encoding="utf-8")
            (target / "web").mkdir()
            (target / "projects").mkdir()
            subprocess.run(["git", "add", "."], cwd=target, check=True)
            subprocess.run(["git", "commit", "-m", "bad clone"], cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return {"command": " ".join(map(str, command)), "return_code": 0, "status": "passed", "stdout_tail": "cloned wrong repo"}
        raise AssertionError(f"unexpected command after invalid clone: {command}")

    monkeypatch.setattr(repo_manager, "run_logged", fake_run_logged)
    result = repo_manager.clone_or_reuse(repo_url, runs_root / "current" / "repos", runs_root / "current" / "logs", branch="main")

    assert result["exists"] is True
    assert result["head_commit"] == good_head
    assert result["clone_receipt"]["reused_historical_clone"] is True
    assert "invalid_clone_reason" in result["clone_receipt"]
    copied = Path(result["repo_path"])
    assert (copied / "README.md").read_text(encoding="utf-8").startswith("# TEDBench")
    assert not (copied / "工作状态.txt").exists()
    assert repo_manager._repo_origin_matches(repo_url, copied) is True


def test_environment_rewrites_huggingface_cli_to_current_hf_cli(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_hf",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    env_prefix = tmp_path / "run" / "conda_envs" / "rigid"
    command = autonomous_deploy.rewrite_command(
        ["huggingface-cli", "download", "tonynzh/RigidSSL", "--repo-type", "dataset", "--resume-download", "--local-dir", "data/raw"],
        "/home/fmh/workspace/miniforge/bin/conda",
        "rigid",
        env_prefix,
    )
    assert command == [str(env_prefix / "bin" / "hf"), "download", "tonynzh/RigidSSL", "--repo-type", "dataset", "--local-dir", "data/raw"]

    run_dir = tmp_path / "run"
    script = run_dir / "scripts" / "download_setup.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        """#!/usr/bin/env bash
huggingface-cli download tonynzh/RigidSSL \
  --repo-type dataset \
  --resume-download \
  --local-dir data/raw
""",
        encoding="utf-8",
    )
    migrations = autonomous_deploy.normalize_generated_script_commands_for_command(
        ["conda", "run", "-p", str(env_prefix), "--no-capture-output", "bash", "scripts/download_setup.sh"],
        run_dir,
        run_dir,
    )
    updated = script.read_text(encoding="utf-8")
    assert migrations and migrations[0]["path"] == str(script)
    assert "hf download tonynzh/RigidSSL" in updated
    assert "huggingface-cli" not in updated
    assert "--resume-download" not in updated


def test_environment_isolated_runtime_scrubs_inconsistent_conda_activation_state(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))

    from scripts.common.shell import isolated_runtime_env

    env = isolated_runtime_env(tmp_path, extra={"CONDA_SHLVL": "1", "CONDA_EXE": "/bad/conda", "CONDA_PREFIX": "/bad/env"})
    assert "CONDA_SHLVL" not in env
    assert "CONDA_EXE" not in env
    assert "CONDA_PREFIX" not in env


def test_environment_blocks_missing_generated_shell_scripts(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_missing_script",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    command = ["bash", str(run_dir / "round_01" / "write_setup_script.sh")]
    issue = autonomous_deploy.missing_shell_script_issue(command, run_dir, run_dir)
    assert "shell 脚本不存在" in issue
    assert "write_setup_script.sh" in issue

    existing = run_dir / "scripts" / "download.sh"
    existing.parent.mkdir()
    existing.write_text("""#!/usr/bin/env bash
echo ok
""", encoding="utf-8")
    assert autonomous_deploy.missing_shell_script_issue(["bash", "scripts/download.sh"], run_dir, run_dir) == ""



def test_environment_prompt_forbids_dependency_matrix_search(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_prompt",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    prompt = autonomous_deploy.prompt_environment_plan(
        {"title": "RigidSSL", "target_metrics": []},
        {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_gb": 31}]},
        {"readmes": []},
        {"target_metrics": []},
        [],
        tmp_path / "plan.json",
        1,
    )
    assert "不要在计划生成阶段运行 `conda search`" in prompt
    assert "由后端 policy 在执行前统一规范化" in prompt

def test_environment_dependency_policy_pins_rigidssl_biopython_for_atom3d():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy_biopython",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)

    plan = {
        "title": "RigidSSL reproduction",
        "env_name": "rigidssl_protein",
        "commands": [
            {"phase": "pip_install", "command": ["python", "-m", "pip", "install", "atom3d", "biopython", "mdtraj"], "required": True},
            {"phase": "pip_install_indirect", "command": ["python", "-m", "pip", "install", "atom3d", "mdtraj"], "required": True},
        ],
    }
    normalized = dependency_policy.normalize_environment_plan_commands(plan, machine={}, policy_version="test-policy")
    commands = [row["command"] for row in normalized["commands"]]
    assert commands[0] == ["python", "-m", "pip", "install", "atom3d", "biopython==1.81", "mdtraj"]
    assert commands[1] == ["python", "-m", "pip", "install", "biopython==1.81", "atom3d", "mdtraj"]
    assert normalized["backend_dependency_policy"]["biopython_legacy_spec"] == "biopython==1.81"



def test_environment_deterministic_rigidssl_plan_validates(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_deterministic",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "RigidSSL"
    (repo / "examples").mkdir(parents=True)
    (repo / "model").mkdir()
    (repo / "examples" / "RigidSSL_Perturb.py").write_text("", encoding="utf-8")
    (repo / "model" / "velocity_network.py").write_text("", encoding="utf-8")
    plan = autonomous_deploy.deterministic_rigidssl_environment_plan(run_dir, repo, {})
    plan = autonomous_deploy.normalize_environment_plan_commands(plan, machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}, policy_version="test-policy")
    issues = autonomous_deploy.validate_environment_plan(plan, require_full_reproduction=False, repo_path=repo, run_dir=run_dir, machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_gb": 31}]}, paper_evidence={"target_metrics": []})
    assert not issues
    command_text = "\n".join(" ".join(row["command"]) for row in plan["commands"] if isinstance(row, dict))
    assert "biopython==1.81" in command_text
    assert "torch==2.9.1+cu128" in command_text
    assert any(row.get("phase") == "reproduce_smoke" and row.get("required") is True for row in plan["commands"] if isinstance(row, dict))

def test_environment_rewrites_rigidssl_model_and_smoke_probes(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_rigidssl",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "RigidSSL"
    (repo / "examples").mkdir(parents=True)
    (repo / "model").mkdir()
    (repo / "examples" / "RigidSSL_Perturb.py").write_text("", encoding="utf-8")
    (repo / "model" / "velocity_network.py").write_text("", encoding="utf-8")

    command, migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "verify_model"},
        [str(run_dir / "conda_envs" / "rigid" / "bin" / "python"), "-c", "from model.velocity_network import VelocityNetwork; m = VelocityNetwork()"],
        repo,
        run_dir,
    )
    assert command[1] == "-c"
    assert "model_setup" in command[2]
    assert "VelocityNetwork()" not in command[2]
    assert migrations

    smoke, smoke_migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "reproduce_smoke"},
        [str(run_dir / "conda_envs" / "rigid" / "bin" / "python"), "RigidSSL_Perturb.py", "--epochs", "1"],
        repo,
        run_dir,
    )
    assert smoke[1] == "-c"
    assert "load_dataset" in smoke[2]
    assert "next(iter(loader))" in smoke[2]
    assert "_single_worker_dataloader" in smoke[2]
    assert "loader_kwargs['num_workers'] = 0" in smoke[2]
    assert "loader_kwargs['pin_memory'] = False" in smoke[2]
    assert "RigidSSL_Perturb.py" not in smoke[0:2]
    assert smoke_migrations

    full, full_migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "reproduce_full"},
        [
            str(run_dir / "conda_envs" / "rigid" / "bin" / "python"),
            "RigidSSL_Perturb.py",
            "--dataset_portion",
            "full",
            "--epochs",
            "10",
            "--input_data_dir",
            str(run_dir / "data" / "RigidSSL_Perturb_data"),
            "--output_model_dir",
            str(run_dir / "output" / "perturb"),
            "--seed",
            "42",
        ],
        repo,
        run_dir,
    )
    assert full[1] == "-c"
    assert "runpy.run_path" in full[2]
    assert "_pyg_loader.DataLoader = _single_worker_dataloader" in full[2]
    assert "loader_kwargs['num_workers'] = 0" in full[2]
    assert "--epochs" in full[2] and "10" in full[2]
    assert "RigidSSL_Perturb.py" not in full[0:2]
    assert full_migrations

    env = autonomous_deploy.command_environment({"PYTHONPATH": str(run_dir / "extra")}, repo, {})
    assert env["PYTHONPATH"].split(":", 1)[0] == str(repo.resolve())
    assert env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1"


def test_environment_rewrites_protdis_nested_feature_dims_smoke(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_protdis_feature_dims",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "ProtDiS"
    (repo / "src" / "models").mkdir(parents=True)
    (repo / "src" / "models" / "kon.py").write_text("class KON: pass\n", encoding="utf-8")

    code = (
        "from src.models.kon import KON; "
        "model = KON(d_model=256, feature_dims=[[8],[16],[22],[12]], discrete_labels=['ss']); "
        "print(model)"
    )
    command, migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "model_smoke"},
        [str(run_dir / "conda_envs" / "protdis_env" / "bin" / "python"), "-c", code],
        repo,
        run_dir,
    )

    assert migrations
    assert "feature_dims=[8, 16, 22, 12]" in command[2]
    assert "feature_dims=[[8]" not in command[2]

    training_code = "\n".join([
        "from src.models.kon import KON; from torch.optim import AdamW",
        "model = KON(d_model=256, feature_dims=[[8],[16],[22],[12]], discrete_labels=['ss'])",
        "optimizer = AdamW(model.parameters(), lr=1e-3)",
        "for step in range(3):",
        "    optimizer.zero_grad()",
        "    out = model(batch)",
        "    loss = out['total_loss']",
        "    loss.backward()",
        "    optimizer.step()",
        "    print(f'Step {step+1}: total_loss={loss.item():.4f}')",
        "    print('Training smoke test PASSED')",
    ])
    training_command, training_migrations = autonomous_deploy.normalize_repository_command_for_execution(
        {"phase": "reproduce_smoke"},
        [str(run_dir / "conda_envs" / "protdis_env" / "bin" / "python"), "-c", training_code],
        repo,
        run_dir,
    )

    assert training_migrations
    assert "feature_dims=[8, 16, 22, 12]" in training_command[2]
    assert "feature_dims=[[8]" not in training_command[2]
    assert "for step in range(3):" in training_command[2]
    assert "\n    out = model(batch)" in training_command[2]
    assert "\n    optimizer.step()" in training_command[2]
    ast.parse(training_command[2])


def test_environment_machine_alignment_accepts_noncritical_supported_hardware_row():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_machine_alignment",
        "scripts/orchestration/autonomous_deploy.py",
    )

    plan = {
        "machine_assessment": {
            "paper_hardware_or_runtime_requirement": "≥1×24GB GPU",
            "local_machine_summary": "NVIDIA GeForce RTX 5090 with 32607 MiB VRAM and CUDA available",
            "fit_for_local_machine": True,
            "adaptation_actions": ["Use CUDA 12.8 wheels and FP32 validation on one GPU"],
            "evidence": ["machine_profile", "nvidia-smi", "GPU CUDA VRAM"],
        },
        "paper_config_alignment": [
            {
                "paper_item": "hardware/precision",
                "paper_value": "≥1×24GB GPU, no precision requirement",
                "implementation_choice": "RTX 5090 32GB, CUDA 12.8, FP32",
                "command_phase": "verify_cuda_imports",
                "evidence_source": "machine_profile runtime_probe nvidia-smi",
                "match_status": "matched",
                "critical": False,
            }
        ],
    }

    issues = autonomous_deploy.machine_assessment_issues(
        plan,
        {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_total": "32607 MiB", "compute_capability": "12.0"}]},
    )

    assert not issues


def test_environment_execute_plan_keeps_named_conda_envs_separate(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_named_envs",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    round_dir = run_dir / "round_01"
    executed: list[list[str]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        executed.append([str(item) for item in command])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return {"command": autonomous_deploy.command_text(command), "status": "passed", "return_code": 0, "log_path": str(log_path), "required": required}

    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)

    plan = {
        "env_name": "default_env",
        "commands": [
            {"phase": "conda_setup_protdis", "command": ["conda", "create", "-y", "-n", "protdis_env", "python=3.11"], "required": True},
            {"phase": "verify_protdis", "command": ["python", "-c", "print('protdis')"], "required": True},
            {"phase": "conda_setup_kermut", "command": ["conda", "create", "-y", "-n", "kermut_env", "python=3.11"], "required": True},
            {"phase": "verify_kermut", "command": ["python", "-c", "print('kermut')"], "required": True},
            {"phase": "verify_protdis_again", "command": ["conda", "run", "-n", "protdis_env", "python", "-c", "print('again')"], "required": True},
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    protdis_python = str(run_dir / "conda_envs" / "protdis_env" / "bin" / "python")
    kermut_python = str(run_dir / "conda_envs" / "kermut_env" / "bin" / "python")
    assert len(receipts) == 5
    assert executed[0][0:4] == ["conda", "create", "-y", "-p"]
    assert executed[0][4] == str(run_dir / "conda_envs" / "protdis_env")
    assert executed[1][0] == protdis_python
    assert executed[2][4] == str(run_dir / "conda_envs" / "kermut_env")
    assert executed[3][0] == kermut_python
    assert executed[4][0] == protdis_python


def test_environment_plan_rejects_uncloned_run_repo_references(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_uncloned_repo_refs",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "AI-HPC-Research-Team_ProtDiS_ef2b8b2b"
    repo.mkdir(parents=True)
    plan = {
        "status": "ready_to_execute",
        "env_name": "bad_repo_refs",
        "machine_assessment": {
            "paper_hardware_or_runtime_requirement": "single CUDA GPU with at least 24GB VRAM",
            "local_machine_summary": "GPU: NVIDIA GeForce RTX 5090 32GB, CUDA available",
            "fit_for_local_machine": True,
            "adaptation_actions": ["Use CUDA 12.8 wheels and run bounded import smoke tests on one GPU"],
            "evidence": ["runtime_probe GPU", "nvidia-smi RTX 5090 32GB"],
        },
        "commands": [
            {
                "phase": "conda_setup",
                "command": ["conda", "create", "-y", "-n", "bad_repo_refs", "python=3.11", "pip"],
                "cwd": "run",
                "required": True,
            },
            {
                "phase": "install_made_up_repo",
                "command": ["python", "-m", "pip", "install", "-e", "repos/petergroth_kermut/"],
                "cwd": "run",
                "required": True,
            },
            {
                "phase": "verify_made_up_repo",
                "command": [
                    "python",
                    "-c",
                    "import sys; sys.path.insert(0, 'repos/BorgwardtLab_TEDBench'); print('bad')",
                ],
                "cwd": "run",
                "required": True,
            },
        ],
        "success_criteria": [],
    }

    issues = autonomous_deploy.validate_environment_plan(
        plan,
        require_full_reproduction=False,
        repo_path=repo,
        run_dir=run_dir,
        machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_total_gb": 32}]},
        paper_evidence={},
    )

    joined = "\n".join(issues)
    assert "repos/petergroth_kermut" in joined
    assert "repos/BorgwardtLab_TEDBench" in joined
    assert "只能使用已克隆仓库目录" in joined


def test_environment_plan_allows_run_root_aux_repo_paths_with_primary_cwd(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_aux_repo_paths",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    primary_repo = run_dir / "repos" / "AI-HPC-Research-Team_ProtDiS_ef2b8b2b"
    tedbench_repo = run_dir / "repos" / "BorgwardtLab_TEDBench_8bee7df5"
    primary_repo.mkdir(parents=True)
    tedbench_repo.mkdir(parents=True)
    (tedbench_repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    round_dir = run_dir / "round_01"
    executed: list[tuple[list[str], Path]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        executed.append(([str(item) for item in command], Path(cwd)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("install ok\n", encoding="utf-8")
        return {
            "command": autonomous_deploy.command_text(command),
            "status": "passed",
            "return_code": 0,
            "stdout_tail": "install ok",
            "log_path": str(log_path),
            "required": required,
        }

    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)

    plan = {
        "status": "ready_to_execute",
        "env_name": "prot_ev_env",
        "machine_assessment": {
            "paper_hardware_or_runtime_requirement": "single CUDA GPU with at least 24GB VRAM",
            "local_machine_summary": "GPU: NVIDIA GeForce RTX 5090 32GB, CUDA available",
            "fit_for_local_machine": True,
            "adaptation_actions": ["Use CUDA 12.8 wheels and run bounded import smoke tests on one GPU"],
            "evidence": ["runtime_probe GPU", "nvidia-smi RTX 5090 32GB"],
        },
        "commands": [
            {
                "phase": "conda_setup",
                "command": ["conda", "create", "-y", "-n", "prot_ev_env", "python=3.11", "pip"],
                "cwd": "repos/AI-HPC-Research-Team_ProtDiS_ef2b8b2b",
                "required": True,
                "timeout_sec": 30,
            },
            {
                "phase": "install_tedbench_deps",
                "command": ["conda", "run", "-n", "prot_ev_env", "pip", "install", "-r", "repos/BorgwardtLab_TEDBench_8bee7df5/requirements.txt"],
                "cwd": ".",
                "required": True,
                "timeout_sec": 30,
            },
            {
                "phase": "verify_imports",
                "command": ["conda", "run", "-n", "prot_ev_env", "python", "-c", "print('ok')"],
                "cwd": "repos/BorgwardtLab_TEDBench_8bee7df5",
                "required": True,
                "timeout_sec": 30,
            },
        ],
        "success_criteria": [
            {"name": "environment_ready", "operator": "==", "value": 1, "source": "environment_gate"},
        ],
        "paper_config_alignment": [
            {
                "paper_item": "hardware/runtime",
                "paper_value": "single CUDA GPU with at least 24GB VRAM",
                "implementation_choice": "RTX 5090 32GB, CUDA 12.8 run-local Conda environment",
                "command_phase": "verify_imports",
                "evidence_source": "runtime_probe nvidia-smi",
                "match_status": "matched",
                "critical": False,
            }
        ],
    }

    issues = autonomous_deploy.validate_environment_plan(
        plan,
        require_full_reproduction=False,
        repo_path=primary_repo,
        run_dir=run_dir,
        machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "memory_total_gb": 32}]},
        paper_evidence={},
    )

    assert not issues
    receipts = autonomous_deploy.execute_plan_commands(
        plan, primary_repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={}
    )

    assert all(receipt["return_code"] == 0 for receipt in receipts)
    install_command, install_cwd = executed[1]
    assert install_cwd == primary_repo
    assert str(tedbench_repo / "requirements.txt") in install_command
    verify_command, verify_cwd = executed[2]
    assert verify_cwd == tedbench_repo
    assert verify_command[-1] == "print('ok')"


def test_environment_execute_plan_skips_existing_conda_create_prefix(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_existing_prefix",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    prefix = run_dir / "conda_envs" / "demo_env"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").write_text("", encoding="utf-8")
    round_dir = run_dir / "round_01"
    executed: list[list[str]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        executed.append([str(item) for item in command])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("verify ok\n", encoding="utf-8")
        return {"command": autonomous_deploy.command_text(command), "status": "passed", "return_code": 0, "log_path": str(log_path), "required": required}

    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)

    plan = {
        "env_name": "demo_env",
        "commands": [
            {"phase": "conda_setup", "command": ["conda", "create", "-y", "-n", "demo_env", "python=3.11", "pip"], "required": True},
            {"phase": "verify", "command": ["python", "-c", "print('ok')"], "required": True},
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    assert len(receipts) == 2
    assert receipts[0]["existing_prefix_reused"] is True
    assert receipts[0]["return_code"] == 0
    assert "复用已存在" in Path(receipts[0]["log_path"]).read_text(encoding="utf-8")
    assert len(executed) == 1
    assert executed[0][0] == str(prefix / "bin" / "python")


def test_environment_execute_plan_creates_missing_run_cwd_before_git_clone(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_git_clone_missing_cwd",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    clone_cwd = run_dir / "repos"
    round_dir = run_dir / "round_01"
    seen_cwds: list[Path] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        seen_cwds.append(Path(cwd))
        assert Path(cwd).is_dir()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("clone ok\n", encoding="utf-8")
        return {
            "command": autonomous_deploy.command_text(command),
            "status": "passed",
            "return_code": 0,
            "stdout_tail": "done",
            "log_path": str(log_path),
            "required": required,
        }

    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)
    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")

    plan = {
        "env_name": "demo_env",
        "commands": [
            {
                "phase": "clone_tedbench",
                "command": ["git", "clone", "https://github.com/BorgwardtLab/TEDBench.git"],
                "cwd": "repos",
                "required": False,
                "timeout_sec": 30,
            }
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    assert receipts[0]["return_code"] == 0
    assert seen_cwds == [clone_cwd]
    assert clone_cwd.is_dir()


def test_environment_execute_plan_retries_transient_git_clone_failures(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_git_clone_retry",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    target = run_dir / "repos" / "lock_gp"
    round_dir = run_dir / "round_01"
    attempts: list[list[str]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        attempts.append([str(item) for item in command])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("attempt\n", encoding="utf-8")
        if len(attempts) == 1:
            target.mkdir(parents=True)
            (target / ".git").mkdir()
            return {
                "command": autonomous_deploy.command_text(command),
                "status": "failed",
                "return_code": 128,
                "stdout_tail": "fatal: unable to access 'https://github.com/generatebio/lock_gp/': GnuTLS recv error (-110): The TLS connection was non-properly terminated.",
                "log_path": str(log_path),
                "required": required,
            }
        target.mkdir(parents=True)
        (target / "README.md").write_text("ok", encoding="utf-8")
        return {
            "command": autonomous_deploy.command_text(command),
            "status": "passed",
            "return_code": 0,
            "stdout_tail": "done",
            "log_path": str(log_path),
            "required": required,
        }

    monkeypatch.setenv("TASTE_GIT_CLONE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)
    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")

    plan = {
        "env_name": "demo_env",
        "commands": [
            {
                "phase": "clone_lock_gp",
                "command": ["git", "clone", "--depth", "1", "https://github.com/generatebio/lock_gp", str(target)],
                "cwd": str(run_dir / "repos"),
                "required": True,
                "timeout_sec": 30,
            }
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    assert len(receipts) == 1
    assert receipts[0]["return_code"] == 0
    assert receipts[0]["git_clone_retried"] is True
    assert receipts[0]["git_clone_attempt_count"] == 2
    assert attempts[0][:2] == ["git", "clone"]
    assert attempts[1][:4] == ["git", "-c", "http.version=HTTP/1.1", "clone"]
    assert (target / "README.md").read_text(encoding="utf-8") == "ok"
    assert "transient retry summary" in (round_dir / "logs" / "00_clone_lock_gp.log").read_text(encoding="utf-8")


def test_environment_execute_plan_does_not_retry_permanent_git_clone_404(monkeypatch, tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_git_clone_no_retry",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    round_dir = run_dir / "round_01"
    attempts: list[list[str]] = []

    def fake_run_logged(command, *, cwd, log_path, timeout_sec, env, required):
        attempts.append([str(item) for item in command])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("attempt\n", encoding="utf-8")
        return {
            "command": autonomous_deploy.command_text(command),
            "status": "failed",
            "return_code": 128,
            "stdout_tail": "remote: Repository not found. fatal: repository 'https://github.com/missing/repo/' not found",
            "log_path": str(log_path),
            "required": required,
        }

    monkeypatch.setenv("TASTE_GIT_CLONE_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(autonomous_deploy, "run_logged", fake_run_logged)
    monkeypatch.setattr(autonomous_deploy, "find_conda_executable", lambda: "conda")

    plan = {
        "env_name": "demo_env",
        "commands": [
            {
                "phase": "clone_missing",
                "command": ["git", "clone", "--depth", "1", "https://github.com/missing/repo", str(run_dir / "repos" / "missing")],
                "cwd": str(run_dir / "repos"),
                "required": True,
                "timeout_sec": 30,
            }
        ],
    }

    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_dir, include_full=False, default_timeout=30, command_env={})

    assert len(receipts) == 1
    assert receipts[0]["return_code"] == 128
    assert receipts[0].get("git_clone_retried") is not True
    assert len(attempts) == 1


def test_environment_reusable_receipts_invalidated_by_later_conda_recreate(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_reuse_invalidation",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    prefix = run_dir / "conda_envs" / "demo_env"
    round_01 = run_dir / "round_01"
    round_02 = run_dir / "round_02"
    round_01.mkdir(parents=True)
    round_02.mkdir(parents=True)
    install_cmd = str(prefix / "bin" / "python") + " -m pip install torch"
    create_cmd = "conda create -y -p " + str(prefix) + " python=3.11 pip"
    (round_01 / "command_receipts.json").write_text(json.dumps([
        {"phase": "install_torch", "command": install_cmd, "required": True, "status": "passed", "return_code": 0, "conda_env_prefix": str(prefix), "log_path": str(round_01 / "install.log")},
    ]), encoding="utf-8")
    (round_02 / "command_receipts.json").write_text(json.dumps([
        {"phase": "conda_setup", "command": create_cmd, "required": True, "status": "passed", "return_code": 0, "conda_env_prefix": str(prefix), "log_path": str(round_02 / "conda.log")},
    ]), encoding="utf-8")

    reusable = autonomous_deploy.build_reusable_command_receipt_index([
        {"round": 1, "receipts_path": str(round_01 / "command_receipts.json")},
        {"round": 2, "receipts_path": str(round_02 / "command_receipts.json")},
    ], run_dir)

    assert not reusable


def test_environment_rewrites_inline_python_import_aliases_and_compound_statements(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_inline_normalize",
        "scripts/orchestration/autonomous_deploy.py",
    )

    command = autonomous_deploy.rewrite_command(
        [
            "conda",
            "run",
            "-n",
            "demo_env",
            "python",
            "-c",
            "import torch, biopython, dm_tree; from biopython import SeqIO; x = 0; for i in range(2): x += i; print(x)",
        ],
        "conda",
        "demo_env",
        tmp_path / "conda_envs" / "demo_env",
    )

    assert command[0] == str(tmp_path / "conda_envs" / "demo_env" / "bin" / "python")
    assert command[1] == "-c"
    assert "import torch, Bio, tree as dm_tree" in command[2]
    assert "from Bio import SeqIO" in command[2]
    assert "biopython" not in command[2]
    assert "dm_tree" not in command[2].replace("tree as dm_tree", "")
    assert "\nfor i in range(2):" in command[2]
    compile(command[2], "<environment-inline>", "exec")


def test_environment_inline_python_compound_normalization_preserves_loop_boundary(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_inline_loop_boundary",
        "scripts/orchestration/autonomous_deploy.py",
    )

    command = autonomous_deploy.rewrite_command(
        [
            "python",
            "-c",
            "events = []; for k in ['a', 'b']: events.append(k); events.append('after'); print(events)",
        ],
        "conda",
        "demo_env",
        tmp_path / "conda_envs" / "demo_env",
    )

    namespace: dict[str, object] = {}
    exec(command[2], namespace)

    assert namespace["events"] == ["a", "b", "after"]
    assert "\nevents.append('after')" in command[2]


def test_environment_inline_path_guard_handles_fstring_variable_prefix(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_inline_fstring_guard",
        "scripts/orchestration/autonomous_deploy.py",
    )
    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)

    valid_code = "dr='demo_data'; import os; os.makedirs(f'{dr}/esm_encodings', exist_ok=True); open(f'{dr}/labels/demo.tsv','w').write('ok')"
    assert autonomous_deploy._python_inline_code_boundary_issues(valid_code, repo, run_dir) == []

    unsafe_code = "name='x'; open(f'/outside/{name}.txt','w').write('bad')"
    issues = autonomous_deploy._python_inline_code_boundary_issues(unsafe_code, repo, run_dir)
    assert issues and "/outside/" in issues[0]


def test_environment_skip_full_omits_full_dependent_output_checks():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_skip_full_outputs",
        "scripts/orchestration/autonomous_deploy.py",
    )
    plan = {
        "commands": [
            {"phase": "verify_imports", "command": ["python", "-c", "import torch"], "required": True},
            {"phase": "reproduce_full", "command": ["python", "train.py"], "required": True},
            {"phase": "verify_outputs", "command": ["python", "-c", "check outputs"], "required": True},
            {"phase": "eval_metrics", "command": ["python", "eval.py"], "depends_on": "reproduce_full", "required": True},
        ]
    }

    skip_phases = [row["phase"] for row in autonomous_deploy.command_rows(plan, include_full=False)]
    full_phases = [row["phase"] for row in autonomous_deploy.command_rows(plan, include_full=True)]

    assert skip_phases == ["verify_imports"]
    assert full_phases == ["verify_imports", "reproduce_full", "verify_outputs", "eval_metrics"]


def test_environment_reuses_previous_success_receipts_but_never_reproduce_full(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_reuse",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    repo.mkdir(parents=True)
    round_01 = run_dir / "round_01"
    round_02 = run_dir / "round_02"
    receipts_path = round_01 / "command_receipts.json"
    previous_install_log = round_01 / "logs" / "00_conda_create.log"
    previous_full_log = round_01 / "logs" / "01_reproduce_full.log"
    previous_install_log.parent.mkdir(parents=True)
    previous_install_log.write_text("install already ran\n", encoding="utf-8")
    previous_full_log.write_text("full already ran but must not be reused\n", encoding="utf-8")

    install_command = ["python", "-c", "from pathlib import Path; Path('install_ran.txt').write_text('ran', encoding='utf-8')"]
    full_command = ["python", "-c", "from pathlib import Path; Path('full_ran.txt').write_text('ran', encoding='utf-8')"]
    receipts_path.write_text(json.dumps([
        {"phase": "conda_create", "command": autonomous_deploy.command_text(install_command), "required": True, "status": "passed", "return_code": 0, "log_path": str(previous_install_log)},
        {"phase": "verify", "command": "python -c 'raise SystemExit(1)'", "required": True, "status": "failed", "return_code": 1, "log_path": str(round_01 / "logs" / "failed.log")},
        {"phase": "dataset", "command": "python -c 'print(1)'", "required": False, "status": "passed", "return_code": 0, "log_path": str(round_01 / "logs" / "optional.log")},
        {"phase": "reproduce_full", "command": autonomous_deploy.command_text(full_command), "required": True, "status": "passed", "return_code": 0, "log_path": str(previous_full_log)},
    ]), encoding="utf-8")

    reusable = autonomous_deploy.build_reusable_command_receipt_index([{"round": 1, "receipts_path": str(receipts_path)}], run_dir)
    assert ("conda_create", autonomous_deploy.command_text(install_command)) in reusable
    assert ("verify", "python -c 'raise SystemExit(1)'") not in reusable
    assert ("dataset", "python -c 'print(1)'") not in reusable
    assert ("reproduce_full", autonomous_deploy.command_text(full_command)) not in reusable

    plan = {"env_name": "", "commands": [
        {"phase": "conda_create", "command": install_command, "cwd": "run", "required": True},
        {"phase": "reproduce_full", "command": full_command, "cwd": "run", "required": True},
    ]}
    receipts = autonomous_deploy.execute_plan_commands(plan, repo, run_dir, round_02, True, 30, autonomous_deploy.runtime_env(), reusable_receipts=reusable)

    assert len(receipts) == 2
    assert receipts[0]["reused_receipt"] is True
    assert receipts[0]["reused_from_log_path"] == str(previous_install_log)
    assert not (run_dir / "install_ran.txt").exists()
    assert receipts[1].get("reused_receipt") is not True
    assert (run_dir / "full_ran.txt").read_text(encoding="utf-8") == "ran"
    assert "既有成功回执" in (round_02 / "logs" / "00_conda_create.log").read_text(encoding="utf-8")

def test_environment_dependency_policy_orders_conda_create_before_policy_installs():
    dependency_policy = _load_environment_module(
        "environment_dependency_policy_conda_order",
        "scripts/orchestration/dependency_policy.py",
    )
    plan = {
        "env_name": "protdis_eval_env",
        "commands": [
            {"phase": "clone_lockgp", "command": ["git", "clone", "https://github.com/generatebio/lock_gp.git", "repos/generatebio_lock_gp"], "required": True},
            {"phase": "install_torch_cuda", "command": ["python", "-m", "pip", "install", "torch==2.10.0", "torchvision==0.21.0", "torchaudio==2.10.0"], "required": True},
            {"phase": "clone_tedbench", "command": ["git", "clone", "https://github.com/BorgwardtLab/TEDBench.git", "repos/BorgwardtLab_TEDBench"], "required": True},
            {"phase": "conda_create", "command": ["conda", "create", "-y", "-n", "protdis_eval_env", "python=3.10", "pip"], "required": True},
            {"phase": "install_deps", "command": ["python", "-m", "pip", "install", "torch-geometric", "torch-scatter", "torch-sparse", "torch-cluster"], "required": True},
        ],
    }

    normalized = dependency_policy.normalize_environment_plan_commands(
        plan,
        machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]},
        policy_version="test-policy",
    )
    phases = [row["phase"] for row in normalized["commands"] if isinstance(row, dict)]

    assert phases[:3] == ["clone_lockgp", "clone_tedbench", "conda_create"]
    conda_index = phases.index("conda_create")
    assert phases.index("install_torch_cuda") > conda_index
    assert phases.index("install_deps") > conda_index
    assert not any(
        row.get("policy_managed") and dependency_policy.command_text(row["command"]).startswith("python -m pip")
        for row in normalized["commands"][:conda_index]
        if isinstance(row, dict)
    )
    assert any(
        rewrite.get("phase") == "conda_bootstrap_order"
        for rewrite in normalized.get("plan_policy_rewrites", [])
    )


def test_environment_dependency_policy_rewrites_incoherent_torch_pip_versions():
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_dependency_policy_torch_pip",
        environment_module_root / "scripts" / "orchestration" / "dependency_policy.py",
    )
    assert spec and spec.loader
    dependency_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_policy)

    plan = {
        "env_name": "rigid",
        "commands": [
            {"phase": "install_core", "command": ["python", "-m", "pip", "install", "torch==2.10.0", "torchvision==0.21.0", "torchaudio==2.10.0", "--index-url", "https://download.pytorch.org/whl/cu128"], "required": True},
            {"phase": "install_pyg", "command": ["python", "-m", "pip", "install", "torch_geometric", "torch_scatter", "torch_sparse", "torch_cluster", "-f", "https://data.pyg.org/whl/torch-2.10.0+cu128.html"], "required": True},
        ],
    }
    machine = {"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]}

    normalized = dependency_policy.normalize_environment_plan_commands(plan, machine=machine, policy_version="test-policy")
    command_text = "\n".join(" ".join(row["command"]) for row in normalized["commands"] if isinstance(row, dict))

    assert normalized["commands"][0]["phase"] == "conda_create"
    assert "torch==2.10.0" not in command_text
    assert "torchvision==0.21.0" not in command_text
    assert "torch==2.9.1+cu128" in command_text
    assert "torchvision==0.24.1+cu128" in command_text
    assert "torchaudio==2.9.1+cu128" in command_text
    assert "https://data.pyg.org/whl/torch-2.9.1+cu128.html" in command_text
    assert any(row.get("phase") == "verify_pyg_cuda_import" for row in normalized["commands"] if isinstance(row, dict))


def test_environment_dependency_policy_preserves_non_pyg_protdis_deps_and_installs_esm():
    dependency_policy = _load_environment_module(
        "environment_dependency_policy_protdis_non_pyg",
        "scripts/orchestration/dependency_policy.py",
    )
    plan = {
        "env_name": "protdis_env",
        "commands": [
            {
                "phase": "install_protdis_deps",
                "command": [
                    "python", "-m", "pip", "install",
                    "torch_geometric", "torch_scatter", "torch_sparse", "torch_cluster", "torch_spline_conv",
                    "esm", "gpytorch", "pytorch-lightning", "biopython", "pandas",
                    "scikit-learn", "pyyaml", "tqdm", "matplotlib", "seaborn",
                ],
                "required": True,
            },
            {
                "phase": "verify_imports",
                "command": ["python", "-c", "from esm.models.esmc import ESMC; import matplotlib, seaborn; print('ok')"],
                "required": True,
            },
        ],
    }

    normalized = dependency_policy.normalize_environment_plan_commands(
        plan,
        machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]},
        policy_version="test-policy",
    )
    command_text = "\n".join(dependency_policy.command_text(row["command"]) for row in normalized["commands"] if isinstance(row, dict))
    phases = [row["phase"] for row in normalized["commands"] if isinstance(row, dict)]

    assert "esm==3.2.1.post1" in command_text
    assert "matplotlib" in command_text
    assert "seaborn" in command_text
    assert "gpytorch" in command_text
    assert "pytorch-lightning" in command_text
    assert "torch-spline-conv" in command_text
    non_pyg_row = next(row for row in normalized["commands"] if isinstance(row, dict) and row["phase"] == "install_protdis_deps_non_pyg_deps")
    non_pyg_command_text = dependency_policy.command_text(non_pyg_row["command"])
    assert "torch_spline_conv" not in non_pyg_command_text
    assert "torch-spline-conv" not in non_pyg_command_text
    assert phases.index("install_protdis_deps_non_pyg_deps") < phases.index("verify_imports")
    assert command_text.count("torch-geometric") == 1



def test_environment_dependency_policy_replaces_evolutionaryscale_esm_git_install():
    dependency_policy = _load_environment_module(
        "environment_dependency_policy_esm_git_replace",
        "scripts/orchestration/dependency_policy.py",
    )
    plan = {
        "env_name": "protdis_env",
        "commands": [
            {"phase": "conda", "command": ["conda", "create", "-n", "protdis_env", "python=3.11", "pip", "-y"], "required": True},
            {
                "phase": "install_pyg",
                "command": ["python", "-m", "pip", "install", "torch_geometric", "torch_scatter", "torch_sparse", "torch_cluster", "torch_spline_conv"],
                "required": True,
            },
            {
                "phase": "install_esm",
                "command": ["python", "-m", "pip", "install", "git+https://github.com/evolutionaryscale/esm.git"],
                "required": True,
            },
            {"phase": "verify_esm", "command": ["python", "-c", "import esm; print('esm ok')"], "required": True},
        ],
    }

    normalized = dependency_policy.normalize_environment_plan_commands(
        plan,
        machine={"gpu": [{"name": "NVIDIA GeForce RTX 5090", "compute_capability": "12.0"}]},
        policy_version="test-policy",
    )
    command_text = "\n".join(dependency_policy.command_text(row["command"]) for row in normalized["commands"] if isinstance(row, dict))
    install_esm_row = next(row for row in normalized["commands"] if isinstance(row, dict) and row["phase"] == "install_esm")

    assert "git+https://github.com/evolutionaryscale/esm.git" not in command_text
    assert dependency_policy.command_text(install_esm_row["command"]).endswith("esm==3.2.1.post1")
    assert command_text.count("esm==3.2.1.post1") == 1


def test_environment_dependency_policy_rewrites_protdis_metrics_function_smoke():
    dependency_policy = _load_environment_module(
        "environment_dependency_policy_protdis_metrics",
        "scripts/orchestration/dependency_policy.py",
    )
    bad_snippet = (
        "import sys; sys.path.insert(0, 'tasks/proteinshake'); "
        "from src.models.metrics import compute_metrics, default_metrics; "
        "print(f'tasks metrics module OK, available: {list(default_metrics.keys())}')"
    )
    plan = {
        "env_name": "protdis_env",
        "commands": [
            {
                "phase": "verify_tasks_import",
                "command": ["conda", "run", "-n", "protdis_env", "python", "-c", bad_snippet],
                "cwd": "repo",
                "timeout_sec": 300,
                "required": True,
            }
        ],
    }

    normalized = dependency_policy.normalize_environment_plan_commands(plan, machine={}, policy_version="test-policy")
    row = normalized["commands"][0]
    command_text = dependency_policy.command_text(row["command"])
    code = row["command"][-1]

    assert row["phase"] == "verify_tasks_import"
    assert row["required"] is True
    assert "default_metrics.keys()" not in code
    assert "default_metrics('classification')" in code
    assert "compute_metrics" in code
    assert row["command"][:5] == ["conda", "run", "-n", "protdis_env", "python"]
    assert "default_metrics.keys()" not in command_text
    assert any("default_metrics is a function" in item.get("reason", "") for item in normalized.get("plan_policy_rewrites", []))


def test_environment_success_criteria_keeps_operational_handoff_gates():
    criteria_policy = _load_environment_module(
        "environment_criteria_policy_contract",
        "scripts/orchestration/criteria_policy.py",
    )
    plan = {
        "success_criteria": [
            {"name": "cuda_available", "operator": ">=", "value": 1, "source": "verify_imports confirms CUDA device_count >= 1"},
            {"name": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1"},
        ]
    }

    normalized = criteria_policy.normalize_success_criteria(plan, paper_evidence={}, policy_version="test-policy")

    assert len(normalized["success_criteria"]) == 2
    by_name = {row["name"]: row for row in normalized["success_criteria"]}
    assert by_name["cuda_available"]["approval_scope"] == "environment_gate"
    assert by_name["cuda_available"]["paper_metric"] is False
    assert by_name["designability"]["approval_scope"] == "paper_metric"
    assert by_name["designability"]["paper_metric"] is True


def test_environment_success_criteria_empty_plan_gets_handoff_gate_from_required_commands():
    criteria_policy = _load_environment_module(
        "environment_criteria_policy_empty_handoff_contract",
        "scripts/orchestration/criteria_policy.py",
    )
    plan = {
        "success_criteria": [],
        "commands": [
            {"phase": "conda_create", "command": ["conda", "create", "-n", "protdis_env"], "required": True},
            {"phase": "install_core_deps", "command": ["conda", "run", "-n", "protdis_env", "python", "-m", "pip", "install", "numpy"], "required": True},
            {"phase": "clone_tedbench", "command": ["git", "clone", "https://github.com/BorgwardtLab/TEDBench", "repos/BorgwardtLab_TEDBench"], "required": True},
            {"phase": "verify_imports", "command": ["conda", "run", "-n", "protdis_env", "python", "-c", "import torch"], "required": True},
            {"phase": "reproduce_smoke", "command": ["conda", "run", "-n", "protdis_env", "python", "-c", "print('smoke ok')"], "required": True},
            {"phase": "reproduce_full", "command": ["conda", "run", "-n", "protdis_env", "python", "train.py"], "required": False},
        ],
    }

    normalized = criteria_policy.normalize_success_criteria(plan, paper_evidence={}, policy_version="test-policy")
    criteria = normalized["success_criteria"]
    rewrite = normalized["success_criteria_policy_rewrites"][-1]
    names = {row["name"] for row in criteria}

    assert rewrite["fallback_from_environment_handoff_commands"] is True
    assert names >= {"conda_environment_ready", "runtime_smoke_ready", "data_runtime_ready", "required_environment_commands_ready"}
    assert all(row["approval_scope"] == "environment_gate" for row in criteria)
    assert all(row["paper_metric"] is False for row in criteria)
    assert all("paper claims" in row["non_paper_approval_note"] for row in criteria)


def test_environment_metric_criteria_passed_ignores_handoff_gates():
    decision = _load_environment_module(
        "environment_reproduction_decision_environment_gate_metrics",
        "scripts/reproduction/decision.py",
    )
    criteria = [
        {
            "name": "runtime_smoke_ready",
            "operator": ">=",
            "value": 1,
            "source": "environment handoff: smoke command",
            "approval_scope": "environment_gate",
            "paper_metric": False,
        },
        {
            "name": "designability",
            "operator": ">=",
            "value": 0.758,
            "source": "paper Table 1",
            "approval_scope": "paper_metric",
            "paper_metric": True,
        },
    ]
    receipts = [
        {"phase": "verify_imports", "required": True, "return_code": 0, "stdout_tail": "runtime_smoke_ready: 1"},
        {"phase": "reproduce_full", "required": True, "return_code": 0, "stdout_tail": "loss: 1.23"},
    ]

    env_only_ok, env_only_evidence = decision.metric_criteria_passed(criteria[:1], receipts, allowed_phases={"reproduce_full"})
    mixed_ok, mixed_evidence = decision.metric_criteria_passed(criteria, receipts, allowed_phases={"reproduce_full"})

    assert env_only_ok is False
    assert env_only_evidence == []
    assert mixed_ok is False
    assert len(mixed_evidence) == 1
    assert mixed_evidence[0]["metric"] == "designability"
    assert "runtime_smoke_ready" not in json.dumps(mixed_evidence, ensure_ascii=False)


def test_environment_gate_success_criteria_cannot_approve_paper_metrics():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_success_scope",
        "scripts/orchestration/autonomous_deploy.py",
    )
    env_plan = {
        "success_criteria": [
            {"name": "cuda_available", "operator": ">=", "value": 1, "source": "verify_imports", "approval_scope": "environment_gate", "paper_metric": False},
            {"name": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1", "approval_scope": "paper_metric", "paper_metric": True},
        ]
    }
    paper_evidence = {"target_metrics": [{"name": "designability_target", "operator": ">=", "value": 0.758, "source": "paper Table 1"}]}

    ok, evidence = autonomous_deploy._success_criteria_paper_binding_gate(env_plan, paper_evidence)

    assert ok, evidence
    assert evidence["criteria_count"] == 2
    assert evidence["paper_metric_criteria_count"] == 1
    assert evidence["environment_gate_criteria_count"] == 1
    assert evidence["matched_count"] == 1

    env_only_plan = {"success_criteria": [env_plan["success_criteria"][0]]}
    ok, evidence = autonomous_deploy._success_criteria_paper_binding_gate(env_only_plan, paper_evidence)
    assert not ok
    assert "环境交接" in " ".join(evidence["issues"])


def test_environment_binds_rigidssl_designability_target_alias_and_local_full_text_source(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_binding",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    env_plan = {
        "status": "ready_to_execute",
        "env_name": "rigidssl_protein",
        "success_criteria": [
            {"name": "designability", "metric": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1"},
            {"name": "scRMSD", "metric": "scRMSD", "operator": "<", "value": 2.0, "source": "paper Table 1"},
        ],
    }
    paper_evidence = {
        "target_metrics": [
            {"name": "designability_target", "operator": ">=", "value": 0.758, "source": "RigidSSL paper Table 1"},
            {"name": "scrmsd", "operator": "<", "value": 2.0, "source": "selected_plan.stages[1].tasks[0]"},
        ],
        "paper_claims_or_training_signals": [{"source": "paper", "text": "RigidSSL reports designability."}],
        "text_blocks": [{"source": "local_full_text:/tmp/rigidssl.txt", "text": "RigidSSL full paper text"}],
        "has_paper_context": True,
    }

    ok, evidence = autonomous_deploy._success_criteria_paper_binding_gate(env_plan, paper_evidence)
    assert ok, evidence
    assert evidence["matched_count"] == 2
    assert evidence["matches"][0]["paper_target_source"] == "RigidSSL paper Table 1"
    paper_ok, paper_context = autonomous_deploy._paper_context_gate(paper_evidence)
    assert paper_ok, paper_context
    assert paper_context["substantive_source_count"] == 1

def test_environment_normalizes_selected_plan_metrics_and_paper_source(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, sys.path.pop(sys.path.index(str(environment_module_root))))

    from scripts.common.plan_schema import load_experiment_plan, normalize_plan

    plan_path = tmp_path / "experiment_plan.json"
    plan_path.write_text(json.dumps({
        "selected_plan_id": "plan_rigidssl_controlled",
        "plans": [
            {
                "plan_id": "plan_rigidssl_controlled",
                "title": "RigidSSL controlled reproduction",
                "repo_url": "https://github.com/ZhanghanNi/RigidSSL",
                "data_protocol": {
                    "training_data": "AF2 Structure Database plus CATH domains",
                    "evaluation_metrics": [
                        "Designability improves by 43% on protein design benchmarks",
                        "设计复现容限不超过 3%",
                    ],
                },
            }
        ],
    }), encoding="utf-8")

    normalized = normalize_plan(load_experiment_plan(plan_path), plan_path)
    metrics = {row["name"]: row for row in normalized["target_metrics"]}

    assert normalized["schema_version"] == "environment.normalized_plan.v2"
    assert normalized["selected_plan_id"] == "plan_rigidssl_controlled"
    assert normalized["paper_url"] == "https://openreview.net/forum?id=YAWpZcXHnP"
    assert normalized["paper_source"]["title"].startswith("Rigidity-Aware Geometric Pretraining")
    assert metrics["designability_improvement"]["operator"] == ">="
    assert metrics["designability_improvement"]["value"] == "43%"
    assert metrics["designability_tolerance"]["operator"] == "<="
    assert metrics["designability_tolerance"]["value"] in {"3%", "5%"}
    assert "AF2 Structure Database" in normalized["dataset"]["training_data"]


def test_environment_workspace_audit_allows_web_runtime_log():
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_workspace_log",
        "scripts/orchestration/autonomous_deploy.py",
    )

    assert autonomous_deploy._is_framework_runtime_write("runtime/web_8765.log") is True
    assert autonomous_deploy._filter_framework_runtime_writes(["runtime/web_8765.log", "projects/protein/state/result.json"]) == ["projects/protein/state/result.json"]


def test_environment_handoff_reuses_historical_dataset_without_relaxing_full_approval(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_handoff_history",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    round_01 = run_dir / "round_01"
    round_03 = run_dir / "round_03"
    repo = run_dir / "repos" / "paper_repo"
    dataset_repo = run_dir / "repos" / "TEDBench"
    env_prefix = run_dir / "conda_envs" / "protdis_env"
    for item in [round_01, round_03, repo, dataset_repo, env_prefix / "bin"]:
        item.mkdir(parents=True, exist_ok=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")

    env_plan = {
        "env_name": "protdis_env",
        "commands": [
            {"phase": "conda_setup", "command": ["conda", "create", "-p", str(env_prefix), "python=3.11", "pip"], "required": True},
            {"phase": "verify_imports", "command": [str(env_prefix / "bin" / "python"), "-c", "import torch"], "required": True},
            {"phase": "verify_model", "command": [str(env_prefix / "bin" / "python"), "-c", "print('model ok')"], "required": True},
            {"phase": "reproduce_smoke", "command": [str(env_prefix / "bin" / "python"), "-c", "print('smoke ok')"], "required": True},
            {"phase": "reproduce_full", "command": [str(env_prefix / "bin" / "python"), "train.py"], "required": True},
        ],
        "machine_assessment": {
            "status": "suitable",
            "fit_for_local_machine": True,
            "paper_hardware_or_runtime_requirement": "CUDA GPU",
            "local_machine_summary": "local CUDA GPU runtime is available on RTX 5090",
            "adaptation_actions": ["use CUDA wheel smoke before full reproduction"],
            "evidence": ["runtime_probe", "nvidia-smi", "machine_profile.json"],
        },
        "paper_config_alignment": [
            {"paper_item": "model_architecture", "paper_value": "ProtDiS encoder", "implementation_choice": "verify_model imports and runs model", "command_phase": "verify_model", "evidence_source": "repo smoke", "match_status": "matched", "critical": True},
            {"paper_item": "dataset", "paper_value": "TEDBench", "implementation_choice": "dataset cloned in previous environment round", "command_phase": "dataset", "evidence_source": "dataset receipt", "match_status": "missing", "critical": True},
            {"paper_item": "hardware_precision", "paper_value": "CUDA", "implementation_choice": "verify_imports checks CUDA wheel", "command_phase": "verify_imports", "evidence_source": "runtime probe", "match_status": "adapted_for_machine", "critical": True},
        ],
        "success_criteria": [
            {"name": "auroc", "operator": ">=", "value": 0.8, "source": "paper table", "approval_scope": "paper_metric", "paper_metric": True}
        ],
    }
    env_plan_path = round_03 / "claude_environment_plan_round_03.json"
    env_plan_path.write_text(json.dumps(env_plan), encoding="utf-8")
    dataset_receipts = [
        {
            "phase": "dataset",
            "required": True,
            "return_code": 0,
            "status": "passed",
            "command": f"git clone --depth 1 https://github.com/BorgwardtLab/TEDBench {dataset_repo}",
            "tokens": ["git", "clone", "--depth", "1", "https://github.com/BorgwardtLab/TEDBench", str(dataset_repo)],
            "log_path": str(round_01 / "dataset.log"),
            "conda_env_prefix": str(env_prefix),
        }
    ]
    latest_receipts = [
        {"phase": "conda_setup", "required": True, "return_code": 0, "status": "passed", "command": f"conda create -y -p {env_prefix} python=3.11 pip", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "conda.log")},
        {"phase": "verify_imports", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'import torch'", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "verify.log")},
        {"phase": "verify_model", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'print(model)'", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "model.log")},
        {"phase": "reproduce_smoke", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'print(smoke)'", "conda_env_prefix": str(env_prefix), "log_path": str(round_03 / "smoke.log")},
    ]
    (round_01 / "command_receipts.json").write_text(json.dumps(dataset_receipts), encoding="utf-8")
    (round_03 / "command_receipts.json").write_text(json.dumps(latest_receipts), encoding="utf-8")

    strict_alignment_ok, strict_alignment_issues = autonomous_deploy.paper_config_alignment_passed(env_plan)
    assert strict_alignment_ok is False
    assert any("missing" in item for item in strict_alignment_issues)

    decision = {
        "run_id": "pytest_handoff_history",
        "decision": "continue_repair",
        "exit_code": 30,
        "rounds": [
            {"round": 1, "receipts_path": str(round_01 / "command_receipts.json")},
            {"round": 3, "env_plan_path": str(env_plan_path), "receipts_path": str(round_03 / "command_receipts.json"), "metric_evidence": []},
        ],
        "approval_gate": {"checks": [{"name": "workspace_write_audit", "passed": True, "reason": "audit ok"}]},
        "workspace_write_audit": {"status": "passed", "outside_workspace_writes": []},
        "machine_summary": {"gpu": [{"name": "RTX 5090", "memory_gb": 31}]},
    }
    repo_info = {"repo_url": "https://github.com/example/paper_repo", "repo_path": str(repo), "exists": True, "head_commit": "abc", "clone_receipt": {"return_code": 0, "status": "passed"}}

    updated = autonomous_deploy.attach_environment_handoff(decision, run_dir, {"title": "ProtDiS"}, repo_info)
    handoff = updated["environment_handoff"]
    checks = {row["name"]: row for row in handoff["handoff_gate"]["checks"]}

    assert handoff["ready_for_experimenting"] is True
    assert updated["decision"] == "environment_ready"
    assert checks["dataset_runtime"]["passed"] is True
    assert checks["paper_config_alignment"]["passed"] is True
    assert checks["paper_config_alignment"]["evidence"]["pending_downstream_alignment"][0]["paper_item"] == "dataset"
    assert len(handoff["data"]["successful_dataset_receipts"]) == 1




def test_environment_handoff_rejects_synthetic_dataset_for_real_data_plan(tmp_path):
    autonomous_deploy = _load_environment_module(
        "environment_autonomous_deploy_handoff_synthetic_dataset",
        "scripts/orchestration/autonomous_deploy.py",
    )

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "protdis"
    demo_data = repo / "demo_data"
    env_prefix = run_dir / "conda_envs" / "protdis_env"
    for item in [repo, demo_data, env_prefix / "bin"]:
        item.mkdir(parents=True, exist_ok=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    env_plan = {
        "env_name": "protdis_env",
        "commands": [
            {"phase": "conda_setup", "command": ["conda", "create", "-p", str(env_prefix), "python=3.11"], "required": True},
            {"phase": "verify_imports", "command": [str(env_prefix / "bin" / "python"), "-c", "import torch"], "required": True},
            {"phase": "dataset", "command": [str(env_prefix / "bin" / "python"), "prepare_demo_data.py", "--output", str(demo_data)], "required": True},
            {"phase": "reproduce_smoke", "command": [str(env_prefix / "bin" / "python"), "train.py", "-C", "config_kon_demo.yaml"], "required": True},
        ],
        "paper_config_alignment": [
            {"paper_item": "dataset", "paper_value": "ProtDBench wet-lab data / PDFBench SwissTest / ProteinShake 12-task set", "implementation_choice": "prepare_demo_data synthetic demo only", "command_phase": "dataset", "evidence_source": "demo smoke", "match_status": "matched", "critical": True},
            {"paper_item": "hardware", "paper_value": "CUDA", "implementation_choice": "local CUDA", "command_phase": "verify_imports", "match_status": "matched", "critical": True},
        ],
        "machine_assessment": {"status": "suitable", "fit_for_local_machine": True, "paper_hardware_or_runtime_requirement": "CUDA", "local_machine_summary": "CUDA ok", "adaptation_actions": ["use local GPU"], "evidence": ["nvidia-smi"]},
    }
    receipts = [
        {"phase": "conda_setup", "required": True, "return_code": 0, "status": "passed", "command": f"conda create -p {env_prefix} python=3.11", "conda_env_prefix": str(env_prefix)},
        {"phase": "verify_imports", "required": True, "return_code": 0, "status": "passed", "command": f"{env_prefix / 'bin' / 'python'} -c 'import torch'", "conda_env_prefix": str(env_prefix)},
        {"phase": "dataset", "required": True, "return_code": 0, "status": "passed", "command": f"python prepare_demo_data.py --output {demo_data}", "tokens": ["python", "prepare_demo_data.py", "--output", str(demo_data)], "conda_env_prefix": str(env_prefix)},
        {"phase": "reproduce_smoke", "required": True, "return_code": 0, "status": "passed", "command": "python train.py -C config_kon_demo.yaml", "stdout_tail": "synthetic demo smoke passed", "conda_env_prefix": str(env_prefix)},
    ]
    handoff = autonomous_deploy.build_environment_handoff(
        "pytest_synthetic_data",
        run_dir,
        {"selected_plan": {"environment_requirements": {"training_data": ["ProtDBench wet-lab data", "PDFBench SwissTest", "ProteinShake 12-task set"]}}},
        {"repo_url": "https://github.com/protdis/protdis", "repo_path": str(repo), "exists": True, "head_commit": "abc", "clone_receipt": {"return_code": 0, "status": "passed"}},
        env_plan,
        receipts,
        {"checks": [{"name": "workspace_write_audit", "passed": True, "reason": "audit ok"}]},
        [],
        machine={},
        workspace_audit={"status": "passed", "outside_workspace_writes": []},
    )
    checks = {row["name"]: row for row in handoff["handoff_gate"]["checks"]}
    assert handoff["ready_for_experimenting"] is False
    assert checks["dataset_runtime"]["passed"] is False
    evidence = checks["dataset_runtime"]["evidence"]
    assert "ProtDBench wet-lab data" in evidence["missing_dataset_receipt_names"]
    assert evidence["synthetic_or_toy_markers"]


def test_proteinshake_realdata_probe_writes_real_metrics(tmp_path, monkeypatch):
    script_path = ROOT / "modules" / "experimenting" / "scripts" / "execution" / "proteinshake_realdata_probe.py"
    spec = importlib.util.spec_from_file_location("proteinshake_realdata_probe_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeProteinFamilyTask:
        task_type = ("protein", "multi_class")
        num_classes = 3
        train_targets = [0, 0, 1, 2]
        val_targets = [0, 1]
        test_targets = [0, 2]

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    import types
    fake_tasks = types.ModuleType("proteinshake.tasks")
    fake_tasks.ProteinFamilyTask = FakeProteinFamilyTask
    fake_pkg = types.ModuleType("proteinshake")
    monkeypatch.setitem(sys.modules, "proteinshake", fake_pkg)
    monkeypatch.setitem(sys.modules, "proteinshake.tasks", fake_tasks)

    artifact_dir = tmp_path / "artifact"
    data_root = tmp_path / "data"
    rc = module.main(["--artifact-dir", str(artifact_dir), "--data-root", str(data_root)])

    assert rc == 0
    summary = json.loads((artifact_dir / "experiment_iteration_summary.json").read_text(encoding="utf-8"))
    metrics = json.loads((artifact_dir / "metrics.json").read_text(encoding="utf-8"))
    assert summary["acceptance_status"] == "accepted_real_data_probe"
    assert summary["dataset"] == "ProteinShake ProteinFamilyTask"
    assert metrics["proteinshake_train_samples"] == 4
    assert metrics["proteinshake_test_majority_accuracy"] == 0.5


def test_environment_handoff_ready_without_promoting_paper_metrics(tmp_path):
    environment_module_root = ROOT / "modules" / "environment"
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            sys.modules.pop(name, None)
    if str(environment_module_root) not in sys.path:
        sys.path.insert(0, str(environment_module_root))
    else:
        sys.path.insert(0, str(sys.path.pop(sys.path.index(str(environment_module_root)))))
    spec = importlib.util.spec_from_file_location(
        "environment_autonomous_deploy_handoff",
        environment_module_root / "scripts" / "orchestration" / "autonomous_deploy.py",
    )
    assert spec and spec.loader
    autonomous_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autonomous_deploy)

    run_dir = tmp_path / "run"
    repo = run_dir / "repos" / "repo"
    env_prefix = run_dir / "conda_envs" / "rigid"
    (repo / "examples").mkdir(parents=True)
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("", encoding="utf-8")
    env_plan = {
        "env_name": "rigid",
        "commands": [
            {"phase": "conda_create", "command": ["conda", "run", "-p", str(env_prefix), "python", "-m", "pip", "install", "torch"], "required": True},
            {"phase": "verify", "command": ["conda", "run", "-p", str(env_prefix), "python", "-c", "import torch"], "required": True},
            {"phase": "dataset", "command": ["hf", "download", "AF2"], "required": True},
            {"phase": "reproduce_smoke", "command": ["conda", "run", "-p", str(env_prefix), "python", "-c", "print('loader smoke')"], "required": True},
            {"phase": "reproduce_full", "command": ["python", "train.py"], "required": True},
        ],
        "success_criteria": [{"name": "designability", "operator": ">=", "value": 0.758, "source": "paper Table 1"}],
        "machine_assessment": {
            "status": "suitable",
            "fit_for_local_machine": True,
            "paper_hardware_or_runtime_requirement": "single GPU CUDA training",
            "local_machine_summary": "local CUDA GPU runtime is available for the smoke and reproduction commands",
            "adaptation_actions": ["use CUDA wheel and bounded smoke before full reproduction"],
            "evidence": ["runtime_probe", "nvidia-smi", "machine_profile.json"],
        },
        "paper_config_alignment": [
            {"paper_item": "designability metric", "paper_value": "0.758", "implementation_choice": "success_criteria designability >= 0.758", "command_phase": "reproduce_full", "evidence_source": "paper Table 1", "match_status": "matched", "critical": True},
            {"paper_item": "epochs", "paper_value": "10 epochs", "implementation_choice": "reproduce_full trains 10 epochs", "command_phase": "reproduce_full", "evidence_source": "repo config", "match_status": "matched", "critical": True},
            {"paper_item": "batch_size", "paper_value": "batch_size 64", "implementation_choice": "reproduce_full uses batch_size 64", "command_phase": "reproduce_full", "evidence_source": "repo config", "match_status": "matched", "critical": True},
            {"paper_item": "learning_rate", "paper_value": "lr=1e-4", "implementation_choice": "reproduce_full uses learning rate 1e-4", "command_phase": "reproduce_full", "evidence_source": "repo config", "match_status": "matched", "critical": True},
            {"paper_item": "hardware/precision", "paper_value": "CUDA GPU", "implementation_choice": "verify uses local CUDA GPU with CUDA wheels", "command_phase": "verify", "evidence_source": "runtime_probe nvidia-smi", "match_status": "adapted_for_machine", "critical": True},
        ],
    }
    receipts = [
        {"phase": "conda_create", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": f"conda run -p {env_prefix} python -m pip install torch"},
        {"phase": "verify", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": f"conda run -p {env_prefix} python -c 'import torch'"},
        {"phase": "dataset", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": "hf download AF2 Structure Database", "stdout_tail": "AF2 Structure Database ready"},
        {"phase": "reproduce_smoke", "required": True, "return_code": 0, "status": "passed", "conda_env_prefix": str(env_prefix), "command": f"conda run -p {env_prefix} python -c 'loader smoke'", "stdout_tail": "loader smoke passed"},
        {"phase": "reproduce_full", "required": True, "return_code": 30, "status": "blocked", "conda_env_prefix": str(env_prefix), "command": "python train.py", "stdout_tail": "full metrics pending"},
    ]
    approval_gate = {"checks": [
        {"name": "repository_source", "passed": True, "reason": "repo ok"},
        {"name": "repository_documentation", "passed": False, "reason": "paper-level docs pending"},
        {"name": "conda_environment", "passed": True, "reason": "env ok"},
        {"name": "machine_fit", "passed": True, "reason": "machine ok"},
        {"name": "dataset_evidence", "passed": False, "reason": "paper-level dataset evidence pending"},
        {"name": "required_commands", "passed": False, "reason": "full reproduction pending"},
        {"name": "paper_config_alignment", "passed": True, "reason": "alignment ok"},
        {"name": "workspace_write_audit", "passed": True, "reason": "audit ok"},
        {"name": "metric_evidence", "passed": False, "reason": "metrics pending"},
        {"name": "reproduce_full", "passed": False, "reason": "full pending"},
    ]}
    handoff = autonomous_deploy.build_environment_handoff(
        "pytest_run",
        run_dir,
        {"title": "RigidSSL", "paper_url": "https://openreview.net/forum?id=YAWpZcXHnP", "selected_plan_id": "plan"},
        {
            "repo_url": "https://github.com/example/repo",
            "repo_path": str(repo),
            "exists": True,
            "head_commit": "abc",
            "clone_receipt": {"return_code": 0, "status": "passed"},
        },
        env_plan,
        receipts,
        approval_gate,
        [{"metric": "designability", "passed": False}],
        machine={},
        workspace_audit={"status": "passed", "outside_workspace_writes": []},
    )
    assert handoff["ready_for_experimenting"] is True
    assert handoff["handoff_gate"]["passed"] is True
    assert handoff["pending_downstream_metrics"][0]["metric"] == "designability"
    assert handoff["pending_downstream_metrics"][0]["status"] == "pending_experimenting_evaluation"
    handoff_checks = {row["name"]: row for row in handoff["handoff_gate"]["checks"]}
    assert "metric_evidence" not in handoff_checks
    assert "reproduce_full" not in handoff_checks
    assert "repository_documentation" not in handoff_checks
    assert "dataset_evidence" not in handoff_checks
    assert handoff_checks["required_commands"]["passed"] is True
    assert handoff_checks["required_commands"]["evidence"]["ignored_reproduce_full_count"] == 1


def test_experimenting_default_permission_mode_is_bypass_permissions():
    runner = _load_experiment_runner()
    args = runner.parse_args(["--plan", "plan.json", "--repo-path", "repo"])
    assert args.permission_mode == "bypassPermissions"


def test_experimenting_rejects_permission_denied_claude_success(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "The bash/python execution requires approval.",
        "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "python smoke_test.py"}},
        ],
    }
    log_path.write_text("# started_at: now\n\n" + json.dumps(payload, ensure_ascii=False) + "\n# finished_at: now\n# return_code: 0\n", encoding="utf-8")

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_claude_permission_denied"
    assert any(row["code"] == "claude_permission_denied" for row in acceptance["acceptance_blockers"])
    fallback_summary = json.loads((artifact_dir / "experiment_iteration_summary.json").read_text(encoding="utf-8"))
    assert fallback_summary["status"] == "blocked_claude_permission_denied"
    assert fallback_summary["metrics"] == {}


def test_experimenting_trusts_empty_structured_permission_denials(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "Previous iteration's permission-denied blocker is resolved; commands executed successfully.",
        "permission_denials": [],
    }
    log_path.write_text(
        "# started_at: now\n\n" + json.dumps(payload, ensure_ascii=False) + "\n# finished_at: now\n# return_code: 0\n",
        encoding="utf-8",
    )
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "success",
                "commands": [{"description": "validation", "status": "passed"}],
                "metrics": {"smoke_metric": 1.0},
                "acceptance_status": "accepted",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is True
    assert acceptance["acceptance_status"] == "accepted"
    assert acceptance["permission_denials"] == []


def test_experimenting_blocks_synthetic_smoke_for_real_data_plan(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    log_path.write_text(
        "# started_at: now\n\n" + json.dumps({"type": "result", "subtype": "success", "result": "done", "permission_denials": []}) + "\n# finished_at: now\n# return_code: 0\n",
        encoding="utf-8",
    )
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [{"command": "python train.py -C config_kon_demo.yaml", "status": "passed"}],
                "metrics": {"best_monitor_loss": 16.5881},
                "dataset": "synthetic_demo",
                "acceptance_status": "accepted",
                "judgment": {"verdict": "pipeline_validated", "weakest_slice": "synthetic demo only"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "metrics.json").write_text(
        json.dumps({"run_metadata": {"dataset": "synthetic_demo"}, "best_monitor_loss": 16.5881}, ensure_ascii=False),
        encoding="utf-8",
    )
    plan = runner.ExperimentPlan(
        path=tmp_path / "plan.json",
        raw={
            "plan_id": "plan_5",
            "environment_requirements": {"training_data": ["ProtDBench wet-lab data", "PDFBench SwissTest", "ProteinShake 12-task set"]},
            "experiment_stages": [{"success_gate": "GP evaluator on ProtDBench Spearman r > 0.5"}],
        },
        text="",
        experiment_id="plan_5",
        title="real data plan",
        method="experiment",
        dataset="ProtDBench wet-lab data / PDFBench SwissTest",
        metric="spearman",
        run_command="",
        conda_env="",
        summary="Use real benchmark/wet-lab data, not synthetic demo.",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {"best_monitor_loss": 16.5881},
        plan,
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_real_data_experiment_required"
    codes = {row["code"] for row in acceptance["acceptance_blockers"]}
    assert "real_data_experiment_required" in codes


def test_experimenting_rejects_summary_acceptance_blockers_without_permission_denial(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "Previous iteration's permission-denied blocker is resolved; commands executed successfully.",
        "permission_denials": [],
    }
    log_path.write_text(
        "# started_at: now\n\n" + json.dumps(payload, ensure_ascii=False) + "\n# finished_at: now\n# return_code: 0\n",
        encoding="utf-8",
    )
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [{"description": "validation", "status": "passed"}],
                "metrics": {"throughput": 2.9},
                "acceptance_status": "partial_with_generation_blocker",
                "acceptance_blockers": [
                    {"code": "missing_generation_pipeline", "message": "No generation script."},
                    {"code": "missing_evaluation_pipeline", "message": "No evaluation script."},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_generation_evaluation_pipeline_missing"
    assert acceptance["permission_denials"] == []
    codes = {row["code"] for row in acceptance["acceptance_blockers"]}
    assert "claude_permission_denied" not in codes
    assert {"missing_generation_pipeline", "missing_evaluation_pipeline"} <= codes


def test_experimenting_imports_autonomous_wrapper_to_project_registry(tmp_path):
    sys.path.insert(0, str(ROOT / "framework" / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "experimenting_import_experiment_artifacts",
        ROOT / "modules" / "experimenting" / "scripts" / "records" / "import_experiment_artifacts.py",
    )
    assert spec and spec.loader
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)

    class Paths:
        root = tmp_path / "projects" / "demo"
        state = root / "state"
        experiments = root / "experiments"

    Paths.state.mkdir(parents=True)
    Paths.experiments.mkdir(parents=True)
    artifact_dir = tmp_path / "runtime" / "runs" / "demo_run" / "iteration_01"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "acceptance_status": "partial_with_generation_blocker",
                "acceptance_blockers": [{"code": "missing_generation_pipeline", "message": "No generation script."}],
                "metrics": {"throughput": 2.9},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "wrapper_iteration_result.json").write_text(
        json.dumps(
            {
                "record": {
                    "timestamp": "2026-06-21T00:10:00Z",
                    "run_id": "demo_run",
                    "experiment_id": "demo_experiment",
                    "iteration": 1,
                    "status": "failed",
                    "method": "experiment",
                    "repo_path": "/tmp/repo",
                    "artifact_path": str(artifact_dir),
                    "metrics": {"throughput": 2.9},
                    "metric_name": "throughput",
                    "metric_value": 2.9,
                    "acceptance_status": "blocked_generation_pipeline_missing",
                    "acceptance_blockers": [{"code": "missing_generation_pipeline", "message": "No generation script."}],
                    "experiment_iteration_summary_status": "completed",
                    "experiment_iteration_summary_acceptance_status": "partial_with_generation_blocker",
                    "next_action": "replan",
                },
                "acceptance": {"acceptance_status": "blocked_generation_pipeline_missing"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = importer.import_artifact(Paths, artifact_dir)
    registry = json.loads((Paths.state / "experiment_registry.json").read_text(encoding="utf-8"))

    assert result["status"] == "imported_autonomous_wrapper"
    assert len(registry) == 1
    assert registry[0]["run_id"] == "demo_run"
    assert registry[0]["acceptance_status"] == "blocked_generation_pipeline_missing"
    assert registry[0]["experiment_iteration_summary_acceptance_status"] == "partial_with_generation_blocker"
    assert registry[0]["metrics"] == {"throughput": 2.9}





def test_framework_syncs_experimenting_module_records_to_project(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "framework" / "scripts"))
    from taste_backend.orchestration import orchestrator
    from taste_backend.orchestration.state import WorkflowState
    from taste_backend.runtime.context import FrameworkContext
    from taste_backend.runtime.executor import CommandResult

    workspace = tmp_path / "taste"
    output_root = workspace / "modules" / "experimenting" / "runtime" / "web" / "demo"
    artifact_dir = output_root / "runs" / "demo_run" / "iteration_01"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [{"command": "python train.py -C config_kon_demo.yaml", "status": "passed"}],
                "judgment": {"verdict": "pipeline_validated", "weakest_slice": "synthetic demo only"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "metrics.json").write_text(
        json.dumps({"run_metadata": {"dataset": "synthetic_demo"}, "best_monitor_loss": 16.5881}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "wrapper_iteration_result.json").write_text("{}", encoding="utf-8")
    registry_dir = output_root / "state"
    registry_dir.mkdir(parents=True)
    (registry_dir / "experiment_registry.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-06-21T07:48:10Z",
                    "run_id": "demo_run",
                    "experiment_id": "plan_5",
                    "iteration": 1,
                    "status": "success",
                    "method": "experiment",
                    "artifact_path": str(artifact_dir),
                    "acceptance_status": "accepted",
                    "metrics": {"best_epoch": 1.0},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_state = workspace / "projects" / "demo" / "state"
    project_state.mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "_refresh_project_experiment_table", lambda ctx, project: {"return_code": 0})
    ctx = FrameworkContext(
        workspace_root=workspace,
        framework_root=workspace / "framework",
        state_root=workspace / "framework" / "workspace",
        run_id="framework_demo",
        python=sys.executable,
        mode="execute",
    )
    state = WorkflowState(run_id="framework_demo", research_goal="", project="demo")
    result = CommandResult(
        stage="experimenting",
        action="run",
        command=[sys.executable, str(workspace / "modules" / "experimenting" / "main.py"), "--output-root", str(output_root)],
        status="completed",
        return_code=0,
        started_at="2026-06-21T07:43:47Z",
        finished_at="2026-06-21T07:48:10Z",
    )

    orchestrator._sync_experimenting_outputs_to_project(ctx, state, result)
    project_registry = json.loads((project_state / "experiment_registry.json").read_text(encoding="utf-8"))

    assert len(project_registry) == 1
    row = project_registry[0]
    assert row["run_id"] == "demo_run"
    assert row["dataset"] == "synthetic_demo"
    assert row["decision"] == "synthetic_only"
    assert row["command"] == "python train.py -C config_kon_demo.yaml"
    assert row["metrics"]["best_monitor_loss"] == 16.5881
    assert row["project_record_source"].endswith("modules/experimenting/runtime/web/demo/state/experiment_registry.json")
    assert any("experimenting 记录已同步到项目 demo" in note for note in state.notes)


def test_experiment_record_tools_acceptance_accepted_is_not_blocker():
    for path in [
        ROOT / "modules" / "experimenting" / "scripts" / "common",
        ROOT / "framework" / "scripts",
    ]:
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    spec = importlib.util.spec_from_file_location(
        "experiment_record_tools_for_acceptance",
        ROOT / "modules" / "experimenting" / "scripts" / "records" / "experiment_record_tools.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.acceptance_gate_text({"acceptance_status": "accepted", "acceptance_blockers": []}) == ""
    probe_row = {
        "acceptance_status": "accepted_real_data_probe",
        "acceptance_blockers": [],
        "status": "success",
        "dataset": "ProteinShake ProteinFamilyTask",
        "metrics": {"proteinshake_test_majority_accuracy": 0.0134},
    }
    assert module.acceptance_gate_text(probe_row) == ""
    assert "真实数据探针已验收" in module.audit_text(probe_row)
    assert "弱证据" in module.audit_text(probe_row)
    assert "不能支撑主论文结论" in module.reflection(probe_row)
    assert "missing_generation_pipeline" in module.acceptance_gate_text(
        {"acceptance_status": "blocked_generation_evaluation_pipeline_missing", "acceptance_blockers": [{"code": "missing_generation_pipeline"}]}
    )

def test_experimenting_requires_iteration_summary_for_success(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    log_path.write_text("# started_at: now\n\n" + json.dumps({"type": "result", "subtype": "success", "result": "done"}) + "\n# finished_at: now\n# return_code: 0\n", encoding="utf-8")

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_missing_experiment_summary"
    assert any(row["code"] == "missing_experiment_iteration_summary" for row in acceptance["acceptance_blockers"])


def test_experimenting_rejects_success_summary_without_execution_evidence(tmp_path):
    runner = _load_experiment_runner()
    artifact_dir = tmp_path / "iteration_01"
    artifact_dir.mkdir()
    log_path = artifact_dir / "claude_stdout.log"
    log_path.write_text("# started_at: now\n\n" + json.dumps({"type": "result", "subtype": "success", "result": "done"}) + "\n# finished_at: now\n# return_code: 0\n", encoding="utf-8")
    (artifact_dir / "experiment_iteration_summary.json").write_text(
        json.dumps({"status": "success", "changed_files": ["model.py"], "metrics": {}, "commands": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    acceptance = runner.evaluate_iteration_acceptance(
        artifact_dir,
        {"return_code": 0, "log_path": str(log_path)},
        {"return_code": 0, "status": "not_configured", "log_path": ""},
        {},
    )

    assert acceptance["accepted"] is False
    assert acceptance["acceptance_status"] == "blocked_missing_iteration_evidence"
    assert any(row["code"] == "missing_iteration_evidence" for row in acceptance["acceptance_blockers"])
