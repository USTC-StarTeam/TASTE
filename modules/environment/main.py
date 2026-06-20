from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = "environment"
DISPLAY_NAME = "Environment / 自主环境部署后端"
RESPONSIBILITY = (
    "给定实验 plan 后，在隔离工作区内让 Claude Code 自主选择/拉取 GitHub 仓库，"
    "依据 README、论文配置和本机画像生成 Conda 部署方案，执行验证与参考复现，"
    "最后输出批准、拒绝或继续修复裁决。"
)
REQUIRED_EXTERNAL_INPUTS = ("experiment_plan_json", "local_runtime", "optional_paper_or_repo_hints")
ARTIFACTS_IN = ("experiment_plan.json", "paper/repo hints")
ARTIFACTS_OUT = (
    "runs/<run_id>/environment_deployment_decision.json",
    "runs/<run_id>/claude_environment_plan_round_*.json",
    "runs/<run_id>/command_receipts_round_*.json",
    "latest_decision.json",
)
PRIVATE_BACKEND_ROOTS = (
    "modules/environment/scripts/orchestration/autonomous_deploy.py",
    "modules/environment/scripts/common/",
    "modules/environment/scripts/repository/",
    "modules/environment/scripts/environment/",
    "modules/environment/scripts/reproduction/",
)

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = Path(__file__).resolve().parent
SCRIPTS = MODULE_ROOT / "scripts"


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
        "backend_only": True,
        "frontend_dependency": False,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
        "decision_schema": {
            "approve": "复现证据达到论文声明，allow_next_module=true，退出码 0。",
            "reject": "仓库/数据/论文不可靠，或算力不可满足且无合理降级路径，并有不可修证据，allow_next_module=false，退出码 20。",
            "continue_repair": "尚未复现且未能证明不可修，Claude Code 应继续修复，allow_next_module=false，退出码 30。",
        },
    }


def _contract_payload() -> dict[str, Any]:
    payload = contract()
    payload["entrypoint"] = "modules/environment/main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    entries: list[str] = [str(MODULE_ROOT)]
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    env["WORKSPACE_ROOT"] = str(ROOT)
    env["ENVIRONMENT_ROOT"] = str(MODULE_ROOT)
    return env


ACTION_TO_SCRIPT = {
    "": "orchestration/autonomous_deploy.py",
    "run": "orchestration/autonomous_deploy.py",
    "deploy": "orchestration/autonomous_deploy.py",
    "deploy_from_plan": "orchestration/autonomous_deploy.py",
    "autonomous_deploy": "orchestration/autonomous_deploy.py",
    "run_autonomous_deploy": "orchestration/autonomous_deploy.py",
    "status": "orchestration/status.py",
}


REPO_DATA_TOOL_ACTIONS: dict[str, str] = {}


ENV_DATA_TOOL_ACTIONS: dict[str, str] = {}


def _run_script(relative_script: str, args: Sequence[str]) -> int:
    script = SCRIPTS / relative_script
    if not script.exists():
        raise SystemExit(f"未知 environment 模块动作脚本：{relative_script}")
    proc = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=_python_env(), text=True)
    return int(proc.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment module public backend entrypoint.")
    parser.add_argument("--action", default="deploy_from_plan", help="后端动作，默认 deploy_from_plan。")
    parser.add_argument("--contract", action="store_true", help="输出模块契约。")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = _normalize_action(ns.action)
    script = ACTION_TO_SCRIPT.get(action)
    if not script:
        raise SystemExit(f"未知 environment 模块动作：{action}")
    return _run_script(script, rest)


if __name__ == "__main__":
    raise SystemExit(main())
