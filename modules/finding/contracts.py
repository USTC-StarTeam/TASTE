from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_NAME = "finding"
DISPLAY_NAME = "Finding"
RESPONSIBILITY = "Collect, filter, score, and rank literature/tool candidates from the research topic and profile. Full-text reading evidence is explicitly outside this module."
REQUIRED_EXTERNAL_INPUTS = ('llm_api', 'research_topic', 'research_interest', 'researcher_profile', 'source_selection')
ARTIFACTS_IN = ('config/profile JSON', 'venue/source selection JSON')
ARTIFACTS_OUT = ('find_results.json', 'article.md', 'source_status.md', 'category/title/detail/scoring reports')
LEGACY_ROOTS = ('modules/finding/scripts/find_pipeline.py', 'modules/finding/scripts/discover_*.py', 'modules/finding/scripts/build_literature_tool_packet.py')


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
