from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_NAME = "environment"
DISPLAY_NAME = "Environment"
RESPONSIBILITY = "Select audited code/data bases, probe loaders, and lock the experiment runtime. It does not run novel experiments or write paper claims."
REQUIRED_EXTERNAL_INPUTS = ('selected_plan_contract', 'candidate_repo_data_artifacts', 'runtime_config')
ARTIFACTS_IN = ('plans.json', 'literature_tool_packet.json', 'repo/data candidates')
ARTIFACTS_OUT = ('evidence_ready_repo_selection.json', 'repo_env_bootstrap.json', 'dataset registry', 'reference/data gates')
LEGACY_ROOTS = ('modules/environment/scripts/run_environment_stage.py', 'modules/environment/scripts/select_evidence_ready_repo.py', 'modules/environment/scripts/bootstrap_repo_env.py')


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
