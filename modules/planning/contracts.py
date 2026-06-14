from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_NAME = "planning"
DISPLAY_NAME = "Planning"
RESPONSIBILITY = "Select and repair executable research plans from approved ideas; downstream modules consume only explicit selected plan contracts."
REQUIRED_EXTERNAL_INPUTS = ('llm_api_or_claude', 'idea_artifacts', 'project_constraints')
ARTIFACTS_IN = ('ideas.json', 'idea.md', 'user selection/approval')
ARTIFACTS_OUT = ('plans.json', 'plan.md', 'experiment_plan.json', 'taste_plan_bridge.json', 'blocker action plans')
LEGACY_ROOTS = ('modules/planning/auto_research/auto_plan', 'modules/planning/scripts/plan_experiments.py', 'modules/planning/scripts/build_workflow_blueprint.py')


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
        "legacy_roots": list(LEGACY_ROOTS),
    }
