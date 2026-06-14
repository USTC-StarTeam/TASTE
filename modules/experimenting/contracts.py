from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_NAME = "experimenting"
DISPLAY_NAME = "Experimenting"
RESPONSIBILITY = "Modify or execute selected project code, run auditable experiments, parse metrics/logs, and produce evidence gates for claims."
REQUIRED_EXTERNAL_INPUTS = ('selected_plan_contract', 'locked_environment', 'repo_path', 'experiment_python')
ARTIFACTS_IN = ('evidence_ready_repo_selection.json', 'repo_env_bootstrap.json', 'experiment_plan.json')
ARTIFACTS_OUT = ('experiment_registry.json', 'experiment artifacts/logs', 'runtime integrity audit', 'reference/scientific progress gates')
LEGACY_ROOTS = ('modules/experimenting/scripts/run_coding_agent.py', 'modules/experimenting/scripts/launch_experiment_run.py', 'modules/experimenting/scripts/experiment_contracts.py')


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
