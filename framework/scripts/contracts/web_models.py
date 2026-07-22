from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from policies.source_selection import default_source_selection


LLMRole = Literal["find"]


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
    llm_concurrency: int = 10
    nonvenue_fetch_limit: int = 5000
    max_recommended_papers: int = 20
    max_ideas: int = 6
    venue_title_scan_limit: int = 0
    venue_title_scan_fraction: float = 1.0
    title_abstract_scoring_limit: int = 1000
    full_venue_corpus_audit: bool = True
    title_filter_timeout_sec: int = 120
    abstract_scoring_max_workers: int = 10
    abstract_scoring_batch_size: int = 10
    abstract_scoring_timeout_sec: int = 180
    arxiv_max_queries: int = 3
    arxiv_timeout_sec: int = 15
    arxiv_categories: list[str] = Field(default_factory=list)
    arxiv_queries: list[str] = Field(default_factory=list)
    arxiv_start_date: str = ""
    arxiv_end_date: str = ""
    arxiv_llm_candidate_limit: int = 0
    arxiv_llm_candidates_per_category: int = 0
    biorxiv_categories: list[str] = Field(default_factory=list)
    biorxiv_start_date: str = ""
    biorxiv_end_date: str = ""
    biorxiv_llm_candidate_limit: int = 0
    biorxiv_llm_candidates_per_category: int = 0
    nature_journals: list[str] = Field(default_factory=lambda: ["nature", "natmachintell", "natcomputsci", "nmeth", "ncomms"])
    nature_article_types: list[str] = Field(default_factory=lambda: ["article"])
    nature_start_date: str = ""
    nature_end_date: str = ""
    nature_candidate_limit: int = 200
    science_journals: list[str] = Field(default_factory=lambda: ["science", "sciadv"])
    science_article_types: list[str] = Field(default_factory=lambda: ["Research Article"])
    science_start_date: str = ""
    science_end_date: str = ""
    science_candidate_limit: int = 200
    github_languages: list[str] = Field(default_factory=lambda: ["all"])
    github_since: Literal["daily", "weekly", "monthly"] = "daily"
    hf_include_papers: bool = True
    hf_include_models: bool = True
    runtime_tuning: dict[str, Any] = Field(default_factory=dict)
    default_find_selection: dict[str, Any] = Field(default_factory=dict)
    email: EmailConfig = Field(default_factory=EmailConfig)


class VenueSelection(BaseModel):
    venue_ids: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=lambda: [date.today().year])
    venue_years: list[dict[str, Any]] = Field(default_factory=list)
    include_arxiv: bool = False
    include_biorxiv: bool = False
    include_huggingface: bool = False
    include_github: bool = False
    include_nature: bool = False
    include_science: bool = False


class FindRequest(BaseModel):
    project: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_.-]*$")
    config: AppConfig | None = None
    selection: VenueSelection = Field(default_factory=lambda: VenueSelection(**default_source_selection()))
    force_new_find: bool = False
    restart_full_cycle: bool = False
    human_approved_new_find: bool = False
    approval_reason: str = ""


class ReadRequest(BaseModel):
    run_id: str
    paper_ids: list[str] = Field(default_factory=list)
    max_papers: int | None = Field(default=None, ge=0)
    force: bool = False


class IdeaRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    project: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    max_ideas: int | None = Field(default=None, ge=1, le=50)


class IdeaMarkdownUpdate(BaseModel):
    markdown: str = Field(min_length=1, max_length=1_000_000)


class PlanMarkdownUpdate(BaseModel):
    markdown: str


class PlanRequest(BaseModel):
    run_id: str
    idea_ids: list[str] = Field(default_factory=list)
    repair_rounds: int = Field(default=3, ge=0)


class PlanPolishRequest(BaseModel):
    run_id: str
    plan_id: str
    version_id: str = ""
    rounds: int = 1


class VenueHealthRequest(BaseModel):
    project: str = ""
    venue_ids: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=lambda: [date.today().year])
    venue_years: list[dict[str, Any]] = Field(default_factory=list)
    sample_limit: int = 3


class EmailJobRequest(BaseModel):
    run_id: str
    artifact_scope: Literal["find", "read", "idea", "plan", "environment", "experiment", "paper"] = "find"
    artifact_names: list[str] = Field(default_factory=list)
    receivers: list[str] = Field(default_factory=list)
    subject: str = ""
    include_ranking: bool = True


class IdeaPatch(BaseModel):
    status: Literal["pending", "approved", "deleted"] | None = None
    title: str | None = Field(default=None, max_length=300)
    new_method: str | None = Field(default=None, max_length=12_000)
    initial_experiment: str | None = Field(default=None, max_length=12_000)
