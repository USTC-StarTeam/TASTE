from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_NAME = "ideation"
DISPLAY_NAME = "Ideation"
RESPONSIBILITY = "Turn reading/finding artifacts into editable research ideas without selecting an execution route."
REQUIRED_EXTERNAL_INPUTS = ('llm_api_or_claude', 'reading_artifacts', 'research_profile')
ARTIFACTS_IN = ('find_results.json', 'read_results.json', 'read.md')
ARTIFACTS_OUT = ('ideas.json', 'idea.md', 'hypothesis_arena.md', 'idea candidate audits')
LEGACY_ROOTS = ('modules/ideation/scripts/idea_pipeline.py', 'modules/ideation/scripts/assess_idea_candidates.py', 'modules/ideation/scripts/build_hypothesis_arena.py')


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
