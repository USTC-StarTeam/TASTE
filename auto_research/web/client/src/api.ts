export type Config = {
  research_interest: string;
  researcher_profile: string;
  provider: string;
  base_url: string;
  api_key: string;
  model: string;
  temperature: number;
  llm_roles: Record<string, { provider?: string; base_url?: string; api_key?: string; model?: string; temperature?: number | null }>;
  llm_concurrency: number;
  idea_parallel_workers: number;
  max_fetch_papers: number;
  max_recommended_papers: number;
  max_ideas: number;
  venue_title_scan_limit: number;
  venue_title_scan_fraction: number;
  arxiv_categories: string[];
  arxiv_start_date: string;
  arxiv_end_date: string;
  biorxiv_categories: string[];
  biorxiv_start_date: string;
  biorxiv_end_date: string;
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
  email: {
    smtp_server: string;
    smtp_port: number;
    sender: string;
    receivers: string[];
    smtp_password: string;
    manual_enabled: boolean;
    auto_send_enabled: boolean;
    auto_send_stages: string[];
  };
};

export type Venue = {
  id: string;
  source: string;
  name: string;
  full_name: string;
  type: string;
  rank: string;
  field: string;
  years: number[];
  classification_source: string;
};

export type Job = {
  job_id: string;
  stage: string;
  status: "queued" | "running" | "done" | "error" | "cancelling" | "cancelled";
  created_at: string;
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
  };
};

export type RunInfo = {
  run_id: string;
  created_at: string;
  stages: string[];
  path: string;
};

export type Artifact = {
  name: string;
  kind: "markdown" | "json";
  content: any;
  path?: string;
};

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function getConfig() {
  return json<Config>(await fetch("/api/config"));
}

export async function saveConfig(payload: Config) {
  return json<Config>(
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function getConfigMeta() {
  return json<{ path: string }>(await fetch("/api/config/meta"));
}

export async function getVenues() {
  return json<Venue[]>(await fetch("/api/catalog/venues"));
}

export async function checkVenueHealth(payload: { venue_ids: string[]; years: number[]; sample_limit: number }) {
  return json<{ results: Array<{ venue_id: string; year: number; ok: boolean; sample_count: number; source_adapter: string; message: string; samples: Array<{ title: string; url: string; abstract: string }> }> }>(
    await fetch("/api/catalog/venue-health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function startFind(config: Config, selection: any) {
  return json<Job>(
    await fetch("/api/jobs/find", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config, selection }),
    }),
  );
}

export async function startRead(runId: string, paperIds: string[]) {
  return json<Job>(
    await fetch("/api/jobs/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, paper_ids: paperIds, max_papers: 8 }),
    }),
  );
}

export async function startIdea(runId: string, maxIdeas: number, parallelWorkers: number) {
  return json<Job>(
    await fetch("/api/jobs/idea", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, max_ideas: maxIdeas, parallel_workers: parallelWorkers, candidate_multiplier: 2 }),
    }),
  );
}

export async function startPlan(runId: string, ideaIds: string[], repairRounds: number) {
  return json<Job>(
    await fetch("/api/jobs/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, idea_ids: ideaIds, repair_rounds: repairRounds }),
    }),
  );
}

export async function startPlanPolish(runId: string, planId: string, versionId: string, rounds: number) {
  return json<Job>(
    await fetch("/api/jobs/plan-polish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, plan_id: planId, version_id: versionId, rounds }),
    }),
  );
}

export async function startEmail(payload: { run_id: string; artifact_names?: string[]; receivers?: string[]; subject?: string; include_ranking?: boolean }) {
  return json<Job>(
    await fetch("/api/jobs/email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function finishPlan(runId: string, planId: string) {
  return json<any>(
    await fetch(`/api/runs/${runId}/plans/${planId}/finish`, {
      method: "POST",
    }),
  );
}

export async function getRuns() {
  return json<RunInfo[]>(await fetch("/api/runs"));
}

export async function getArtifacts(runId: string) {
  return json<{ run_id: string; artifacts: Artifact[] }>(await fetch(`/api/runs/${runId}/artifacts`));
}

export async function getJobs() {
  return json<Job[]>(await fetch("/api/jobs"));
}

export async function cancelJob(jobId: string) {
  return json<Job>(
    await fetch(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
    }),
  );
}

export async function deleteRun(runId: string) {
  return json<{ status: string; run_id: string }>(
    await fetch(`/api/runs/${runId}`, {
      method: "DELETE",
    }),
  );
}

export async function patchIdea(runId: string, ideaId: string, patch: Record<string, string>) {
  return json<any>(
    await fetch(`/api/runs/${runId}/ideas/${ideaId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function confirmIdea(runId: string, ideaId: string) {
  return json<Job>(
    await fetch(`/api/runs/${runId}/ideas/${ideaId}/confirm`, {
      method: "POST",
    }),
  );
}

export function watchJob(jobId: string, onMessage: (message: any) => void) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/jobs/${jobId}`);
  socket.onmessage = (event) => onMessage(JSON.parse(event.data));
  return socket;
}
