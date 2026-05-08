from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


ClassificationSource = Literal["official", "llm_inferred", "fallback"]
LLMRole = Literal["find", "read", "idea_generator", "idea_judge", "plan_generator", "plan_evaluator"]


class LLMRoleConfig(BaseModel):
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


class EmailConfig(BaseModel):
    smtp_server: str = ""
    smtp_port: int = 465
    sender: str = ""
    receivers: list[str] = Field(default_factory=list)
    smtp_password: str = ""
    manual_enabled: bool = True
    auto_send_enabled: bool = False
    auto_send_stages: list[str] = Field(default_factory=lambda: ["find", "read", "idea", "plan"])


class AppConfig(BaseModel):
    research_interest: str = ""
    researcher_profile: str = ""
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.4
    llm_roles: dict[str, LLMRoleConfig] = Field(default_factory=dict)
    llm_concurrency: int = 16
    idea_parallel_workers: int = 1
    max_fetch_papers: int = 40
    max_recommended_papers: int = 20
    max_ideas: int = 6
    venue_title_scan_limit: int = 200
    venue_title_scan_fraction: float = 1.0
    arxiv_categories: list[str] = Field(default_factory=lambda: ["cs.AI"])
    arxiv_start_date: str = ""
    arxiv_end_date: str = ""
    github_languages: list[str] = Field(default_factory=lambda: ["all"])
    github_since: Literal["daily", "weekly", "monthly"] = "daily"
    hf_include_papers: bool = True
    hf_include_models: bool = True
    email: EmailConfig = Field(default_factory=EmailConfig)


class VenueSelection(BaseModel):
    venue_ids: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=lambda: [date.today().year])
    include_arxiv: bool = True
    include_huggingface: bool = True
    include_github: bool = True


class FindRequest(BaseModel):
    config: AppConfig | None = None
    selection: VenueSelection = Field(default_factory=VenueSelection)


class ReadRequest(BaseModel):
    run_id: str
    paper_ids: list[str] = Field(default_factory=list)
    max_papers: int = 5


class IdeaRequest(BaseModel):
    run_id: str
    max_ideas: int | None = None
    candidate_multiplier: int = 2
    parallel_workers: int | None = None


class PlanRequest(BaseModel):
    run_id: str
    idea_ids: list[str] = Field(default_factory=list)
    repair_rounds: int = 3


class PlanPolishRequest(BaseModel):
    run_id: str
    plan_id: str
    version_id: str = ""
    rounds: int = 1


class VenueHealthRequest(BaseModel):
    venue_ids: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=lambda: [date.today().year])
    sample_limit: int = 3


class EmailJobRequest(BaseModel):
    run_id: str
    artifact_names: list[str] = Field(default_factory=list)
    receivers: list[str] = Field(default_factory=list)
    subject: str = ""
    include_ranking: bool = True


class IdeaPatch(BaseModel):
    title: str | None = None
    hypothesis: str | None = None
    min_experiment: str | None = None
    status: Literal["pending", "approved", "deleted"] | None = None


class Artifact(BaseModel):
    name: str
    kind: Literal["markdown", "json"]
    content: Any
    path: str = ""
