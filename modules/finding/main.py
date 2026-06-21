from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = 'finding'
DISPLAY_NAME = 'Finding'
RESPONSIBILITY = 'Collect, filter, score, and rank literature/tool candidates from the research topic and profile. Full-text reading evidence is explicitly outside this module.'
REQUIRED_EXTERNAL_INPUTS = ('llm_api', 'research_topic', 'research_interest', 'researcher_profile', 'source_selection')
ARTIFACTS_IN = ('config/profile JSON', 'venue/source selection JSON')
ARTIFACTS_OUT = ('find_results.json', 'article.md', 'source_status.md', 'category/title/detail/scoring reports')
PRIVATE_BACKEND_ROOTS = (
    'modules/finding/scripts/find_pipeline.py',
    'modules/finding/scripts/find_support.py',
    'modules/finding/scripts/catalog',
    'modules/finding/scripts/local_store',
    'modules/finding/scripts/quality',
    'modules/finding/scripts/selection',
    'modules/finding/scripts/research_profile',
    'modules/finding/scripts/ranking',
    'modules/finding/scripts/sources',
    'modules/finding/scripts/finding_quality_tools.py',
    'modules/finding/scripts/build_literature_tool_packet.py',
)


@dataclass(slots=True)
class ArtifactRef:
    name: str
    path: str = ""
    kind: str = "json"
    role: str = "input"
    required: bool = False


@dataclass(slots=True)
class StageInvocation:
    research_topic: str = ""
    research_interest: str = ""
    researcher_profile: str = ""
    artifact_root: str = ""
    llm: dict[str, Any] = field(default_factory=dict)
    inputs: list[ArtifactRef] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)

    def root_path(self) -> Path:
        return Path(self.artifact_root).expanduser() if self.artifact_root else Path.cwd()


@dataclass(slots=True)
class StageResult:
    status: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""


def contract() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
    }


MODULE_ROOT = Path(__file__).resolve().parent
MODULE_RUNTIME_DIR = MODULE_ROOT / ".runtime"
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = MODULE_ROOT / "scripts"


def _default_runtime_dir() -> str:
    return str(MODULE_RUNTIME_DIR)


def _configure_private_runtime_env(env: dict[str, str] | None = None) -> None:
    target = env if env is not None else os.environ
    if not str(target.get("WORKFLOW_RUNTIME_DIR") or "").strip():
        target["WORKFLOW_RUNTIME_DIR"] = _default_runtime_dir()


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    _configure_private_runtime_env(env)
    entries: list[str] = [
        str(MODULE_ROOT),
        str(SCRIPTS),
        str(ROOT / "framework"),
        str(ROOT / "framework" / "scripts"),
        str(ROOT / "web" / "backend"),
        str(ROOT),
    ]
    modules_root = ROOT / "modules"
    for stage_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        entries.append(str(stage_dir))
        scripts = stage_dir / "scripts"
        if scripts.is_dir():
            entries.append(str(scripts))
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(ROOT)
    return env


def _ensure_runtime_imports() -> None:
    _configure_private_runtime_env()
    for entry in reversed(_python_env()["PYTHONPATH"].split(os.pathsep)):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)


def _load_json(path: str, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def _contract_payload() -> dict:
    payload = contract()
    payload["entrypoint"] = f"modules/{STAGE_NAME}/main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _run_script(script_stem: str, args: Sequence[str]) -> int:
    script = SCRIPTS / f"{_normalize_action(script_stem)}.py"
    if not script.exists():
        raise SystemExit(f"Unknown {STAGE_NAME} module action: {script_stem}")
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


DIRECT_ACTIONS = {"", "find", "pipeline", "find_pipeline"}
FINDING_QUALITY_TOOL_ACTIONS = {"plan_literature", "paper_quality", "literature_base_candidates"}
ACTION_ALIASES = {
    "literature_tool": "run_literature_tool",
    "literature_base_audit": "run_literature_base_audit",
    "tool_packet": "build_literature_tool_packet",
    "venue_metadata_cache": "build_venue_metadata_cache",
    "openreview_cache": "build_openreview_cache",
    "discover_arxiv": "discover_arxiv",
    "discover_semantic_scholar": "discover_semantic_scholar",
    "discover_github": "discover_github_repos",
    "ingest_discovery": "ingest_discovery",
    "local_database": "update_local_database",
    "fresh_base_selection": "select_fresh_research_base",
}


def _copy_outputs(run_id: str, output_dir: str) -> None:
    if not output_dir:
        return
    _ensure_runtime_imports()
    from auto_research.storage import run_dir

    source = run_dir(run_id)
    target = Path(output_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    for name in [
        "find_results.json",
        "article.md",
        "source_status.md",
        "find_progress.json",
        "category_scan_report.json",
        "title_filter_report.json",
    ]:
        src = source / name
        if src.exists():
            shutil.copy2(src, target / name)


def _run_refresh_source_health(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Refresh Finding venue source-health artifacts without rerunning scoring.")
    parser.add_argument("--artifact-dir", required=True, help="Finding artifact directory to refresh in place.")
    parser.add_argument("--selection-json", default="", help="Optional VenueSelection-compatible source selection JSON.")
    ns = parser.parse_args(list(args))
    _ensure_runtime_imports()
    from auto_research.source_selection import normalize_source_selection
    from find_pipeline import refresh_find_source_health

    selection = normalize_source_selection(_load_json(ns.selection_json, {})) if ns.selection_json else None
    result = refresh_find_source_health(Path(ns.artifact_dir), selection=selection)
    print(json.dumps({"stage": STAGE_NAME, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_find(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Finding module backend.")
    parser.add_argument("--config-json", default="", help="AppConfig-compatible JSON with LLM/profile fields.")
    parser.add_argument("--selection-json", default="", help="VenueSelection-compatible source selection JSON.")
    parser.add_argument("--output-dir", default="", help="Optional artifact directory to receive Finding outputs.")
    ns = parser.parse_args(list(args))
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, FindRequest, VenueSelection
    from auto_research.source_selection import default_source_selection, normalize_source_selection
    from find_pipeline import run_find

    config = AppConfig(**_load_json(ns.config_json, {}))
    selection_payload = _load_json(ns.selection_json, default_source_selection())
    selection = VenueSelection(**normalize_source_selection(selection_payload))
    log_stream = io.StringIO()
    with contextlib.redirect_stdout(log_stream):
        result = run_find(FindRequest(config=config, selection=selection))
    logs = log_stream.getvalue()
    if logs:
        print(logs, end="", file=sys.stderr)
    run_id = str(result.get("run_id") or "") if isinstance(result, dict) else ""
    _copy_outputs(run_id, ns.output_dir)
    artifact_dir = ""
    if run_id:
        from auto_research.storage import run_dir

        artifact_dir = str(run_dir(run_id))
    print(json.dumps({"stage": STAGE_NAME, "run_id": run_id, "artifact_dir": artifact_dir, "result": result}, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finding module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="find", help="Backend action. Default: find.")
    parser.add_argument("--contract", action="store_true", help="Print module contract and exit.")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    if action in DIRECT_ACTIONS:
        return _run_find(rest)
    if action in {"refresh_source_health", "source_health", "refresh_venue_source_health"}:
        return _run_refresh_source_health(rest)
    if action in FINDING_QUALITY_TOOL_ACTIONS:
        return _run_script("finding_quality_tools", ["--tool-action", action, *rest])
    return _run_script(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
