from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = 'ideation'
DISPLAY_NAME = 'Ideation'
RESPONSIBILITY = 'Consume one normalized evidence bundle and turn it into editable research ideas without discovering project inputs or selecting an execution route.'
REQUIRED_EXTERNAL_INPUTS = ('caller_normalized_input_bundle', 'claude_code', 'runtime_config')
ARTIFACTS_IN = ('ideation_input.json',)
ARTIFACTS_OUT = ('idea.md', 'ideas.json')
PRIVATE_BACKEND_ROOTS = (
    'modules/ideation/scripts/idea_pipeline.py',
)


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


def _ensure_runtime_imports() -> None:
    scripts = str(Path(__file__).resolve().parent / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def _load_json(path: str, default):
    if not path:
        return default
    candidate = Path(path).expanduser()
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(path)


def _load_app_config(path_or_json: str = "") -> dict[str, Any]:
    if path_or_json:
        return _load_json(path_or_json, {})
    env_payload = os.environ.get("TASTE_IDEATION_CONFIG_JSON", "").strip()
    return json.loads(env_payload) if env_payload else {}


def _contract_payload() -> dict:
    payload = contract()
    payload["entrypoint"] = f"modules/{STAGE_NAME}/main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _require_taste_conda() -> None:
    if os.environ.get("CONDA_DEFAULT_ENV", "").strip() != "taste":
        raise SystemExit("Ideation must run in the conda environment named 'taste'.")


DIRECT_ACTIONS = {"", "idea"}
PATCH_ACTIONS = {"patch"}
UPDATE_MARKDOWN_ACTIONS = {"update_markdown"}


def _run_idea(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Ideation module backend.")
    parser.add_argument("--run-id", required=True, help="调用方提供的当前 Find run_id。")
    parser.add_argument("--input-json", required=True, help="调用方构建并校验的规范化输入包。")
    parser.add_argument("--config-json", default="")
    parser.add_argument("--max-ideas", type=int, default=0)
    parser.add_argument("--mock", action="store_true", help="仅用于本地链路自检。")
    ns = parser.parse_args(list(args))
    _ensure_runtime_imports()
    from idea_pipeline import run_idea

    result = run_idea(
        ns.run_id,
        ns.max_ideas,
        _load_app_config(ns.config_json),
        input_json=ns.input_json,
        mock=ns.mock,
    )
    print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_patch(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Patch one idea in an existing Ideation run.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True, help="要原地修改的 timestamp Ideation run。")
    parser.add_argument("--idea-id", required=True)
    parser.add_argument("--patch-json", default="", help="JSON 字符串或 JSON 文件路径；为空时读 TASTE_IDEATION_PATCH_JSON。")
    parser.add_argument("--config-json", default="")
    ns = parser.parse_args(list(args))
    patch_payload = _load_json(ns.patch_json, {}) if ns.patch_json else json.loads(os.environ.get("TASTE_IDEATION_PATCH_JSON", "{}"))
    _ensure_runtime_imports()
    from idea_pipeline import patch_idea

    result = patch_idea(ns.run_dir, ns.run_id, ns.idea_id, patch_payload, config=_load_app_config(ns.config_json))
    print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "action": "patch", "result": result}, ensure_ascii=False, indent=2))
    return 0


def _run_update_markdown(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Replace idea.md in an existing Ideation run.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--markdown-file", default="")
    parser.add_argument("--stdin-markdown", action="store_true")
    parser.add_argument("--config-json", default="")
    ns = parser.parse_args(list(args))
    if ns.stdin_markdown:
        markdown = sys.stdin.read()
    elif ns.markdown_file:
        markdown = Path(ns.markdown_file).expanduser().read_text(encoding="utf-8")
    else:
        raise SystemExit("--stdin-markdown or --markdown-file is required")
    _ensure_runtime_imports()
    from idea_pipeline import update_idea_markdown

    result = update_idea_markdown(ns.run_dir, ns.run_id, markdown, config=_load_app_config(ns.config_json))
    print(json.dumps({"stage": STAGE_NAME, "run_id": ns.run_id, "action": "update_markdown", "result": result}, ensure_ascii=False, indent=2))
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ideation module public backend entrypoint.", add_help=True)
    parser.add_argument("--action", default="idea", help="Backend action. Default: idea.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    _require_taste_conda()
    action = _normalize_action(ns.action)
    if action in PATCH_ACTIONS:
        return _run_patch(rest)
    if action in UPDATE_MARKDOWN_ACTIONS:
        return _run_update_markdown(rest)
    if action in DIRECT_ACTIONS:
        return _run_idea(rest)
    raise SystemExit(f"Unknown {STAGE_NAME} module action: {ns.action}")


if __name__ == "__main__":
    raise SystemExit(main())
