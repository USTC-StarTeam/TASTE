from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

try:
    from .contracts import STAGE_NAME, contract
except ImportError:
    from contracts import STAGE_NAME, contract

ROOT = Path(__file__).resolve().parents[2]


def _ensure_runtime_imports() -> None:
    framework_scripts = ROOT / "framework" / "scripts"
    if str(framework_scripts) not in sys.path:
        sys.path.insert(0, str(framework_scripts))
    from taste_pythonpath import ensure_taste_pythonpath

    ensure_taste_pythonpath(ROOT)


def _load_json(path: str, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def _copy_outputs(run_id: str, output_dir: str) -> None:
    if not output_dir:
        return
    from auto_research.storage import run_dir
    source = run_dir(run_id)
    target = Path(output_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    for name in ["find_results.json", "article.md", "source_status.md", "find_progress.json", "category_scan_report.json", "title_filter_report.json"]:
        src = source / name
        if src.exists():
            shutil.copy2(src, target / name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the standalone Finding module backend.")
    parser.add_argument("--config-json", default="", help="AppConfig-compatible JSON with LLM/profile fields.")
    parser.add_argument("--selection-json", default="", help="VenueSelection-compatible source selection JSON.")
    parser.add_argument("--output-dir", default="", help="Optional artifact directory to receive Finding outputs.")
    parser.add_argument("--contract", action="store_true", help="Print the module input/output contract and exit.")
    args = parser.parse_args()
    if args.contract:
        print(json.dumps(contract(), ensure_ascii=False, indent=2))
        return
    _ensure_runtime_imports()
    from auto_research.models import AppConfig, FindRequest, VenueSelection
    from auto_research.source_selection import default_source_selection, normalize_source_selection
    from auto_research.auto_find.pipeline import run_find

    config = AppConfig(**_load_json(args.config_json, {}))
    selection_payload = _load_json(args.selection_json, default_source_selection())
    selection = VenueSelection(**normalize_source_selection(selection_payload))
    result = run_find(FindRequest(config=config, selection=selection))
    run_id = str(result.get("run_id") or "") if isinstance(result, dict) else ""
    _copy_outputs(run_id, args.output_dir)
    print(json.dumps({"stage": STAGE_NAME, "run_id": run_id, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
