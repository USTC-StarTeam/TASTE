from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from policies.current_find_route import (
    current_find_recommended_title_keys,
    selected_title_in_current_find,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "framework" / "scripts"
FUNCTIONAL_CATEGORIES = {
    "automation",
    "bridges",
    "contracts",
    "integrations",
    "launchers",
    "orchestration",
    "policies",
    "project",
    "reporting",
    "runtime",
    "validation",
}


def _source_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh"} and "__pycache__" not in path.parts
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_framework_scripts_have_one_flat_functional_category_layer():
    assert {path.name for path in SCRIPTS.iterdir() if path.is_dir() and path.name != "__pycache__"} == FUNCTIONAL_CATEGORIES
    assert {path.name for path in SCRIPTS.iterdir() if path.is_file()} == {"main.py"}
    for category in FUNCTIONAL_CATEGORIES:
        nested = [
            path
            for path in (SCRIPTS / category).iterdir()
            if path.is_dir() and path.name != "__pycache__"
        ]
        assert nested == []


def test_framework_script_names_are_unambiguous():
    stems = [path.stem for path in _source_files() if path.name != "__init__.py"]
    assert len(stems) == len(set(stems))


def test_framework_script_manifest_matches_files_and_ast():
    manifest = json.loads((ROOT / "framework" / "script_manifest.json").read_text(encoding="utf-8"))
    files = _source_files()
    expected_paths = [f"scripts/{path.relative_to(SCRIPTS).as_posix()}" for path in files]
    assert manifest["canonical_entrypoints"] == ["scripts/main.py"]
    assert manifest["script_count"] == len(files)
    assert manifest["categories"] == [
        "automation",
        "bridges",
        "contracts",
        "integrations",
        "launchers",
        "orchestration",
        "policies",
        "project",
        "public_entrypoint",
        "reporting",
        "runtime",
        "validation",
    ]
    assert [row["path"] for row in manifest["scripts"]] == expected_paths
    for row, path in zip(manifest["scripts"], files, strict=True):
        expected_category = "public_entrypoint" if path == SCRIPTS / "main.py" else path.relative_to(SCRIPTS).parts[0]
        assert row["category"] == expected_category
        if path.suffix == ".sh":
            assert row["kind"] == "shell"
            assert row["functions"] == []
            assert row["classes"] == []
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert row["kind"] == "python"
        assert row["functions"] == functions
        assert row["classes"] == classes
        assert row["function_count"] == len(functions)
        assert row["class_count"] == len(classes)


def test_current_find_titles_only_come_from_the_current_find_run(tmp_path):
    paths = SimpleNamespace(root=tmp_path, planning=tmp_path / "planning")
    _write_json(paths.planning / "finding" / "find_progress.json", {"run_id": "find_new"})
    _write_json(
        paths.planning / "finding" / "find_results.json",
        {"run_id": "find_old", "articles": [{"title": "Old selected title"}]},
    )
    _write_json(
        tmp_path / "state" / "evidence_ready_repo_selection.json",
        {"selected": {"title": "Old selected title"}},
    )

    assert current_find_recommended_title_keys(paths) == set()
    assert selected_title_in_current_find(paths, {"title": "Old selected title"}) is False

    _write_json(
        paths.planning / "finding" / "find_results.json",
        {"run_id": "find_new", "articles": [{"title": "Current title"}]},
    )
    assert selected_title_in_current_find(
        paths,
        {"title": " Current   Title ", "fresh_find_run_id": "find_new"},
    ) is True


def test_base_switch_title_fallback_requires_explicit_authorization(tmp_path):
    paths = SimpleNamespace(root=tmp_path, planning=tmp_path / "planning")
    selected = {"title": "New base", "repo_path": "/tmp/new-base"}
    gate = {
        "status": "pass",
        "decision": "authorize_base_switch",
        "candidate_route": {"repo_path": "/tmp/new-base"},
    }
    _write_json(tmp_path / "state" / "base_switch_gate.json", gate)
    _write_json(
        tmp_path / "state" / "base_switch_execution.json",
        {"status": "authorized_by_deterministic_base_switch_gate"},
    )
    assert selected_title_in_current_find(paths, selected) is False

    gate["switch_authorized"] = True
    _write_json(tmp_path / "state" / "base_switch_gate.json", gate)
    assert selected_title_in_current_find(paths, selected) is True
