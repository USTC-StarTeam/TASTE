from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_NAME = "reading"
DISPLAY_NAME = "Reading"
RESPONSIBILITY = "Acquire verified paper-body text for the selected Find packet and synthesize reading notes. Same-run replacements for unavailable public full text happen here, never inside Finding."
REQUIRED_EXTERNAL_INPUTS = ('llm_api_or_claude', 'finding_artifact_packet', 'artifact_root')
ARTIFACTS_IN = ('find_results.json', 'article.md', 'full_text_reading/manual_full_text_sources.json')
ARTIFACTS_OUT = ('read_results.json', 'read.md', 'full_text_reading/full_text_packet.json', 'current_find_full_text_evidence_repair.json')
LEGACY_ROOTS = ('modules/reading/scripts/read_pipeline.py', 'modules/reading/scripts/repair_current_find_full_text_evidence.py', 'modules/reading/scripts/ensure_current_find_research_plan.py')


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
