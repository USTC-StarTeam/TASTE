export type Config = {
  research_interest: string;
  researcher_profile: string;
  provider: string;
  base_url: string;
  api_key: string;
  api_key_saved?: boolean;
  api_key_suffix?: string;
  config_saved_at?: string;
  config_path?: string;
  project_llm_synced?: boolean;
  model: string;
  temperature: number;
  llm_roles: Record<string, { provider?: string; base_url?: string; api_key?: string; api_key_saved?: boolean; api_key_suffix?: string; model?: string; temperature?: number | null }>;
  llm_concurrency: number;
  nonvenue_fetch_limit: number;
  max_recommended_papers: number;
  max_ideas: number;
  venue_title_scan_limit: number;
  venue_title_scan_fraction: number;
  title_abstract_scoring_limit: number;
  full_venue_corpus_audit: boolean;
  title_filter_timeout_sec: number;
  abstract_scoring_max_workers: number;
  abstract_scoring_batch_size?: number;
  abstract_scoring_timeout_sec: number;
  arxiv_max_queries: number;
  arxiv_timeout_sec: number;
  arxiv_categories: string[];
  arxiv_queries: string[];
  arxiv_start_date: string;
  arxiv_end_date: string;
  biorxiv_categories: string[];
  biorxiv_start_date: string;
  biorxiv_end_date: string;
  biorxiv_llm_candidate_limit?: number;
  biorxiv_llm_candidates_per_category?: number;
  nature_journals: string[];
  nature_article_types: string[];
  nature_start_date: string;
  nature_end_date: string;
  nature_candidate_limit: number;
  science_journals: string[];
  science_article_types: string[];
  science_start_date: string;
  science_end_date: string;
  science_candidate_limit: number;
  github_languages: string[];
  github_since: "daily" | "weekly" | "monthly";
  hf_include_papers: boolean;
  hf_include_models: boolean;
  default_find_selection?: {
    venue_ids?: string[];
    years?: number[];
    venue_years?: Array<{ venue_id: string; year: number }>;
    include_arxiv?: boolean;
    include_biorxiv?: boolean;
    include_huggingface?: boolean;
    include_github?: boolean;
    include_nature?: boolean;
    include_science?: boolean;
  };
  email: {
    smtp_server: string;
    smtp_port: number;
    sender: string;
    receivers: string[];
    smtp_password: string;
    smtp_password_saved?: boolean;
    manual_enabled: boolean;
    auto_send_enabled: boolean;
    auto_send_stages: string[];
  };
};

export type Venue = {
  id: string;
  canonical_id?: string;
  source: string;
  name: string;
  full_name: string;
  type: string;
  rank: string;
  field: string;
  years: number[];
  classification_source: string;
  aliases?: Array<Partial<Venue> & { id: string }>;
};

export type Job = {
  job_id: string;
  stage: string;
  internal?: boolean;
  display?: string;
  status: "queued" | "running" | "stale" | "interrupted" | "done" | "blocked" | "error" | "cancelling" | "cancelled" | "preview_available" | "needs_writing" | "preview_pdf_blocked";
  created_at: string;
  finished_at?: string;
  logs: string[];
  result?: any;
  error?: string;
  cancel_requested?: boolean;
  cancelled_at?: string;
  progress?: {
    phase: string;
    current: number;
    total: number;
    percent: number;
    message: string;
    read_progress?: any;
  };
};

export type RunInfo = {
  run_id: string;
  created_at: string;
  stages: string[];
  path: string;
  readonly?: boolean;
  project?: string;
  source?: string;
};

export type Artifact = {
  name: string;
  kind: "markdown" | "json";
  content: any;
  content_zh?: any;
  content_en?: any;
  path?: string;
};

type RequestOptions = {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
};

type ApiResponse = {
  ok: boolean;
  status: number;
  text(): Promise<string>;
  json(): Promise<any>;
};

function xhrRequest(url: string, options: RequestOptions = {}): Promise<ApiResponse> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(options.method || "GET", url, true);
    Object.entries(options.headers || {}).forEach(([key, value]) => request.setRequestHeader(key, value));
    request.onload = () => {
      const text = request.responseText || "";
      resolve({
        ok: request.status >= 200 && request.status < 300,
        status: request.status,
        text: async () => text,
        json: async () => text ? JSON.parse(text) : null,
      });
    };
    request.onerror = () => reject(new Error(`Request failed: ${url}`));
    request.send(options.body || null);
  });
}

function apiFetch(url: string, options: RequestOptions = {}): Promise<ApiResponse | Response> {
  const fetchImpl = typeof globalThis.fetch === "function" ? globalThis.fetch.bind(globalThis) : undefined;
  if (fetchImpl) return fetchImpl(url, options as RequestInit);
  return xhrRequest(url, options);
}

async function json<T>(response: ApiResponse | Response): Promise<T> {
  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new Event("taste:auth-required"));
    }
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

function asArrayResponse<T>(value: any, key = "items"): T[] {
  if (Array.isArray(value)) return value as T[];
  if (value && typeof value === "object") {
    const nested = value[key] ?? value.jobs ?? value.runs ?? value.projects;
    return Array.isArray(nested) ? nested as T[] : [];
  }
  return [];
}

function asObjectResponse<T extends Record<string, any>>(value: any, fallback: T): T {
  return value && typeof value === "object" && !Array.isArray(value) ? value as T : fallback;
}

export type AuthUser = { id: string; username: string };

export async function getCurrentUser(): Promise<AuthUser | null> {
  const response = await apiFetch("/api/auth/me");
  if (response.status === 401) return null;
  const payload = await json<{ user: AuthUser }>(response);
  return payload.user;
}

export async function login(username: string, password: string) {
  return json<{ user: AuthUser }>(await apiFetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }));
}

export async function register(username: string, password: string) {
  return json<{ user: AuthUser }>(await apiFetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }));
}

export async function logout() {
  return json<{ status: string }>(await apiFetch("/api/auth/logout", { method: "POST" }));
}

export async function getFrontendVersion() {
  return asObjectResponse<{ version: string; built_at: string; files?: string[] }>(await json<any>(await apiFetch("/api/frontend/version")), { version: "", built_at: "", files: [] });
}

export async function getConfig() {
  return json<Config>(await apiFetch("/api/config"));
}

export async function saveConfig(payload: Config) {
  return json<Config>(
    await apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function probeLLMConfig() {
  return json<{ ok: boolean; error: string; probe: string; summary: Record<string, any> }>(
    await apiFetch("/api/config/llm-probe", { method: "POST" }),
  );
}

export async function getConfigMeta() {
  return json<{ saved?: boolean }>(await apiFetch("/api/config/meta"));
}

export async function getVenues() {
  return json<Venue[]>(await apiFetch("/api/catalog/venues"));
}

export async function checkVenueHealth(payload: { project?: string; venue_ids: string[]; years: number[]; venue_years?: Array<{ venue_id: string; year: number }>; sample_limit: number }) {
  return json<{ results: Array<{ venue_id: string; year: number; ok: boolean; sample_count: number; source_adapter: string; message: string; samples: Array<{ title: string; url: string; abstract: string }> }> }>(
    await apiFetch("/api/catalog/venue-health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function startFind(config: Config, selection: any, options: Record<string, any> = {}) {
  return json<Job>(
    await apiFetch("/api/jobs/find", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config, selection, ...options }),
    }),
  );
}

export async function startRead(runId: string, paperIds: string[], maxPapers = 0) {
  const selected = Array.isArray(paperIds) ? paperIds.filter(Boolean) : [];
  const limit = Math.max(0, Math.trunc(Number(maxPapers) || 0));
  return json<Job>(
    await apiFetch("/api/jobs/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, paper_ids: selected, max_papers: limit || selected.length }),
    }),
  );
}

export async function startIdea(runId: string, maxIdeas: number, project: string) {
  return json<Job>(
    await apiFetch("/api/jobs/idea", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, max_ideas: maxIdeas, project }),
    }),
  );
}

export async function startPlan(runId: string, ideaIds: string[], repairRounds: number) {
  return json<Job>(
    await apiFetch("/api/jobs/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, idea_ids: ideaIds, repair_rounds: repairRounds }),
    }),
  );
}

export async function startPlanPolish(runId: string, planId: string, versionId: string, rounds: number) {
  return json<Job>(
    await apiFetch("/api/jobs/plan-polish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, plan_id: planId, version_id: versionId, rounds }),
    }),
  );
}

export async function startEmail(payload: { run_id: string; artifact_names?: string[]; receivers?: string[]; subject?: string; include_ranking?: boolean }) {
  return json<Job>(
    await apiFetch("/api/jobs/email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function finishPlan(runId: string, planId: string) {
  return json<any>(
    await apiFetch(`/api/runs/${runId}/plans/${planId}/finish`, {
      method: "POST",
    }),
  );
}

export async function getRuns(project?: string) {
  const query = project ? `?project=${encodeURIComponent(project)}` : "";
  return asArrayResponse<RunInfo>(await json<any>(await apiFetch(`/api/runs${query}`)), "runs");
}

export async function getArtifacts(runId: string, options: { light?: boolean; scope?: "find" | "read" | "ideas" | "plan"; project?: string } = {}) {
  const params = new URLSearchParams();
  if (options.light) params.set("light", "1");
  if (options.scope) params.set("scope", options.scope);
  if (options.project) params.set("project", options.project);
  const suffix = params.size ? `?${params.toString()}` : "";
  return json<{ run_id: string; artifacts: Artifact[] }>(await apiFetch(`/api/runs/${runId}/artifacts${suffix}`));
}

export async function getJobs(project?: string) {
  const params = new URLSearchParams({ compact: "1", limit: "12", include_history: "1" });
  if (project) params.set("project", project);
  const jobs = asArrayResponse<Job>(await json<any>(await apiFetch(`/api/jobs?${params.toString()}`)), "jobs");
  const liveReadJobs = jobs.filter((job) =>
    String(job.stage || "").toLowerCase() === "read"
    && ["queued", "running", "cancelling"].includes(String(job.status || "")),
  );
  if (!liveReadJobs.length) return jobs;
  const detailed = await Promise.all(liveReadJobs.map(async (job) => {
    try {
      return await json<Job>(await apiFetch(`/api/jobs/${encodeURIComponent(job.job_id)}?compact=0`));
    } catch {
      return job;
    }
  }));
  const byId = new Map(detailed.map((job) => [job.job_id, job]));
  return jobs.map((job) => byId.get(job.job_id) || job);
}

export async function cancelJob(jobId: string) {
  return json<Job>(
    await apiFetch(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
    }),
  );
}

export async function deleteRun(runId: string) {
  return json<{ status: string; run_id: string }>(
    await apiFetch(`/api/runs/${runId}`, {
      method: "DELETE",
    }),
  );
}

export async function patchIdea(runId: string, ideaId: string, patch: Record<string, string>, project: string) {
  const params = `?project=${encodeURIComponent(project)}`;
  return json<any>(
    await apiFetch(`/api/runs/${runId}/ideas/${ideaId}${params}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function updateIdeaMarkdown(runId: string, markdown: string, project: string) {
  const params = `?project=${encodeURIComponent(project)}`;
  return json<any>(
    await apiFetch(`/api/runs/${runId}/idea-markdown${params}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown }),
    }),
  );
}

export async function updatePlanMarkdown(runId: string, markdown: string, project?: string) {
  const params = project ? `?project=${encodeURIComponent(project)}` : "";
  return json<any>(
    await apiFetch(`/api/runs/${runId}/plan-markdown${params}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown }),
    }),
  );
}

export function watchJob(jobId: string, onMessage: (message: any) => void) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/jobs/${jobId}`);
  socket.onmessage = (event) => onMessage(JSON.parse(event.data));
  return socket;
}

export type Project = {
  id: string;
  name: string;
  topic: string;
  conda_env: string;
  path: string;
  literature_survey_preview?: Record<string, any>;
};

export type ProjectSummary = {
  project: string;
  path: string;
  config: Record<string, any>;
  run_preferences?: Record<string, any>;
  state: Record<string, any>;
  stages?: Record<string, any>;
  trajectory_system?: Record<string, any>;
  full_research_cycle?: Record<string, any>;
  literature_survey?: Record<string, any>;
  current_find_pipeline?: Record<string, any>;
  full_text_reading_count?: number;
  pending_full_text_reading_count?: number;
  human_gate_summary?: Record<string, any>;
  human_supervision?: Record<string, any>;
  supervision?: Record<string, any>;
  claude_status?: Record<string, any>;
  agent_state?: Record<string, any>;
  runtime?: Record<string, any>;
  blockers?: any[];
  current_blocker?: Record<string, any>;
  next_actions?: any[];
  next_action?: string;
  blocker_action_plan_summary?: Record<string, any>;
  artifacts: Artifact[];
};

export type ClaudeLatestResponse = {
  status?: string;
  stage?: string;
  return_code?: string | number;
  started_at?: string;
  finished_at?: string;
  session_id?: string;
  source?: string;
  response_markdown: string;
  response_chcount: number;
  returned_chcount: number;
  truncated: boolean;
  truncated_head_chars?: number;
  full_response_available: boolean;
  content_compacted?: boolean;
};

export async function getProjects() {
  return asArrayResponse<Project>(await json<any>(await apiFetch("/api/projects")), "projects");
}

export async function getProject(project: string, options: { compact?: boolean } = {}) {
  const compact = options.compact !== false;
  return asObjectResponse<ProjectSummary>(await json<any>(await apiFetch(`/api/projects/${encodeURIComponent(project)}?compact=${compact ? "1" : "0"}`)), {
    project,
    path: "",
    config: {},
    state: {},
    artifacts: [],
  });
}

export async function getClaudeLatestResponse(project: string, stage = "") {
  const params = new URLSearchParams();
  if (stage) params.set("stage", stage);
  const query = params.toString();
  return json<ClaudeLatestResponse>(await apiFetch(`/api/projects/${encodeURIComponent(project)}/claude/latest-response${query ? `?${query}` : ""}`));
}

export async function createProject(payload: Record<string, any>) {
  return json<ProjectSummary>(
    await apiFetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function saveRuntime(project: string, payload: Record<string, any>) {
  return json<any>(
    await apiFetch(`/api/projects/${encodeURIComponent(project)}/runtime`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function saveProjectConfig(project: string, payload: Record<string, any>) {
  return json<ProjectSummary>(
    await apiFetch(`/api/projects/${encodeURIComponent(project)}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function detectRuntime(project: string) {
  return json<any>(
    await apiFetch(`/api/projects/${encodeURIComponent(project)}/runtime/detect`, {
      method: "POST",
    }),
  );
}

export async function startProjectJob(payload: Record<string, any>) {
  return json<Job>(
    await apiFetch("/api/jobs/project", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}
