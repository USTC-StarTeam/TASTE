from __future__ import annotations

import ast
import json
import subprocess
import sys
import warnings
from pathlib import Path

from path_helpers import ensure_script_paths, script_dirs

ensure_script_paths()

from auto_research.module_boundaries import (
    ALL_OWNERS,
    NON_STAGE_OWNERS,
    STAGE_MODULES,
    STAGE_MODULE_KEYS,
    WEB_FRONTEND_OWNER,
    classify_script,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SUFFIXES = {".py", ".sh"}
FLATTENED_STAGE_MODULES = ("finding", "reading", "ideation", "planning")
FORBIDDEN_STAGE_AUTO_DIRS = {"auto_research", "auto_find", "auto_read", "auto_idea", "auto_plan"}


def _migrated_scripts() -> list[Path]:
    return sorted(path for directory in script_dirs() for path in directory.iterdir() if path.is_file() and path.suffix in SCRIPT_SUFFIXES)


def test_top_level_scripts_directory_is_removed() -> None:
    assert not (ROOT / "scripts").exists()


def test_non_vendor_code_does_not_import_removed_scripts_namespace() -> None:
    scan_roots = [ROOT / "tests", ROOT / "framework", ROOT / "modules", ROOT / "web" / "backend"]
    offenders: list[str] = []
    for scan_root in scan_roots:
        for path in scan_root.rglob("*.py"):
            if "modules/writing/vendor" in path.as_posix():
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "scripts" or module.startswith("scripts."):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: from {module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.name
                        if name == "scripts" or name.startswith("scripts."):
                            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: import {name}")
    assert offenders == []


def test_find_to_plan_stage_modules_stay_flattened() -> None:
    offenders: list[str] = []
    for stage in FLATTENED_STAGE_MODULES:
        module_dir = ROOT / "modules" / stage
        assert module_dir.is_dir(), stage
        for path in module_dir.rglob("*"):
            if path.is_dir() and path.name in FORBIDDEN_STAGE_AUTO_DIRS:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_find_to_plan_latest_outputs_use_stage_names() -> None:
    scan_roots = [ROOT / "modules", ROOT / "framework", ROOT / "web" / "backend"]
    forbidden = ("auto_find", "auto_read", "auto_idea", "auto_plan")
    offenders: list[str] = []
    for scan_root in scan_roots:
        for path in scan_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if any(term in text for term in forbidden):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_seven_stage_module_directories_have_contracts_and_manifests() -> None:
    assert STAGE_MODULE_KEYS == ("finding", "reading", "ideation", "planning", "environment", "experimenting", "writing")
    assert len(STAGE_MODULES) == 7
    for module in STAGE_MODULES:
        directory = ROOT / module.directory
        assert directory.is_dir(), module.key
        for filename in ["README.md", "__init__.py", "contracts.py", "cli.py", "script_manifest.json"]:
            assert (directory / filename).is_file(), f"{module.key} missing {filename}"
        contract_text = (directory / "contracts.py").read_text(encoding="utf-8")
        assert f'STAGE_NAME = "{module.key}"' in contract_text
        assert module.responsibility.split(".", 1)[0] in (directory / "README.md").read_text(encoding="utf-8")


def test_every_migrated_script_has_exactly_one_known_owner() -> None:
    scripts = _migrated_scripts()
    assert scripts
    owners = {path.name: classify_script(path.name) for path in scripts}
    assert set(owners.values()) <= set(ALL_OWNERS)
    assert len(owners) == len(scripts)
    assert owners["repair_current_find_full_text_evidence.py"] == "reading"
    assert owners["run_environment_stage.py"] == "environment"
    assert owners["run_coding_agent.py"] == "experimenting"
    assert owners["run_paper_pipeline.py"] == "writing"
    assert owners["run_full_research_cycle.py"] == "taste_framework"


def test_generated_manifests_match_current_script_dirs() -> None:
    scripts = _migrated_scripts()
    owners = {path.name: classify_script(path.name) for path in scripts}
    assert set(owners.values()) <= set(ALL_OWNERS)
    assert len(owners) == len(scripts)

    for module in STAGE_MODULES:
        manifest_path = ROOT / module.directory / "script_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {name for name, owner in owners.items() if owner == module.key}
        actual = {row["script"] for row in manifest["scripts"]}
        assert actual == expected, module.key
        assert manifest["script_count"] == len(expected)

    framework_manifest = json.loads((ROOT / "framework" / "script_manifest.json").read_text(encoding="utf-8"))
    framework_expected = {name for name, owner in owners.items() if owner in NON_STAGE_OWNERS and owner != WEB_FRONTEND_OWNER}
    framework_actual = {row["script"] for row in framework_manifest["scripts"]}
    assert framework_actual == framework_expected
    assert framework_manifest["script_count"] == len(framework_expected)

    web_manifest = json.loads((ROOT / "web" / "backend" / "auto_research" / "web" / "script_manifest.json").read_text(encoding="utf-8"))
    web_expected = {name for name, owner in owners.items() if owner == WEB_FRONTEND_OWNER}
    web_actual = {row["script"] for row in web_manifest["scripts"]}
    assert web_actual == web_expected
    assert web_manifest["script_count"] == len(web_expected)


def test_finding_reading_full_text_responsibility_boundary() -> None:
    pipeline_text = (ROOT / "modules/finding/scripts/find_pipeline.py").read_text(encoding="utf-8")
    forbidden_find_terms = [
        "full_text_readability_gate",
        "_ensure_full_text_readable_recommendations",
        "missing_find_full_text_evidence_for_read_stage",
        "readable_for_read_stage",
    ]
    for term in forbidden_find_terms:
        assert term not in pipeline_text
    reading_manifest = json.loads((ROOT / "modules/reading/script_manifest.json").read_text(encoding="utf-8"))
    reading_scripts = {row["script"] for row in reading_manifest["scripts"]}
    assert "repair_current_find_full_text_evidence.py" in reading_scripts
    assert "ensure_current_find_research_plan.py" in reading_scripts


def test_stage_cli_contracts_are_callable() -> None:
    for stage in STAGE_MODULE_KEYS:
        cli = ROOT / "modules" / stage / "cli.py"
        proc = subprocess.run([sys.executable, str(cli), "--contract"], cwd=ROOT, text=True, capture_output=True, timeout=20)
        assert proc.returncode == 0, (stage, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["stage"] == stage
        assert payload["required_external_inputs"]
        assert payload["artifacts_out"]


def test_web_reading_packet_view_uses_read_stage_replacements() -> None:
    from auto_research.web.project_bridge import _current_find_reading_validation_view_for_web

    find_results = {
        "run_id": "find_test",
        "strong_recommendations": [
            {
                "id": "p1",
                "title": "Readable Original",
                "abstract": "This paper contains a real literature abstract with enough method and experiment detail to pass the public recommendation validation gate.",
                "url": "https://example.org/p1",
                "recommended_for_deep_reading": True,
                "find_recommendation": True,
                "reason_source": "llm abstract evaluation",
                "llm_fit_score": 8.4,
            },
            {
                "id": "p2",
                "title": "Unavailable Original",
                "abstract": "This second paper also contains a real literature abstract with enough method and experiment detail to pass the public recommendation validation gate.",
                "url": "https://example.org/p2",
                "recommended_for_deep_reading": True,
                "find_recommendation": True,
                "reason_source": "llm abstract evaluation",
                "llm_fit_score": 8.1,
            },
        ],
    }
    packet = {
        "run_id": "find_test",
        "papers": [
            {"id": "p1", "title": "Readable Original", "text_path": "texts/p1.txt", "text_chars": 5000, "full_text_status": "pdf_text_read"},
            {
                "id": "r1",
                "title": "Same Run Replacement",
                "text_path": "texts/r1.txt",
                "text_chars": 6000,
                "full_text_status": "pdf_text_read",
                "read_replacement": True,
                "replacement_for_unavailable_recommendation": {"title": "Unavailable Original"},
                "replacement_source_pool": "screened_ranking",
                "replacement_source_rank": 21,
            },
        ],
    }

    originals, reading_rows, validation_find_results = _current_find_reading_validation_view_for_web(find_results, packet, 0)

    assert [row["title"] for row in originals] == ["Readable Original", "Unavailable Original"]
    assert [row["title"] for row in reading_rows] == ["Readable Original", "Same Run Replacement"]
    assert [row["reading_packet_role"] for row in reading_rows] == ["original_recommendation_with_full_text", "read_stage_full_text_replacement"]
    packet_summary = validation_find_results["current_reading_packet"]
    assert packet_summary["replacement_count"] == 1
    assert packet_summary["unavailable_original_recommendation_count"] == 0
