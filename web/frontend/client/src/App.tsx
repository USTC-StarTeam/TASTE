import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import MarkdownIt from "markdown-it";
import katex from "katex";
import texmath from "markdown-it-texmath";
import "katex/dist/katex.min.css";
import {
  Project,
  ProjectSummary,
  AuthUser,
  Artifact,
  Config,
  Job,
  RunInfo,
  Venue,
  cancelJob,
  checkVenueHealth,
  createProject,
  deleteRun,
  detectRuntime,
  finishPlan,
  getProject,
  getProjects,
  getArtifacts,
  getConfig,
  getConfigMeta,
  getClaudeLatestResponse,
  getFrontendVersion,
  getCurrentUser,
  getJobs,
  getRuns,
  getVenues,
  probeLLMConfig,
  patchIdea,
  requestEmailVerification,
  updateIdeaMarkdown,
  updatePlanMarkdown,
  saveConfig,
  saveRuntime,
  saveProjectConfig,
  login,
  logout,
  register,
  startProjectJob,
  startEmail,
  startFind,
  startIdea,
  startPlan,
  startPlanPolish,
  startRead,
  watchJob,
} from "./api";

const STANDARD_FIND_DEFAULTS = {
  llm_concurrency: 10,
  nonvenue_fetch_limit: 5000,
  max_recommended_papers: 20,
  venue_title_scan_limit: 0,
  venue_title_scan_fraction: 1.0,
  title_abstract_scoring_limit: 1000,
  full_venue_corpus_audit: true,
  title_filter_timeout_sec: 120,
  abstract_scoring_max_workers: 10,
  abstract_scoring_batch_size: 10,
  abstract_scoring_timeout_sec: 180,
  arxiv_max_queries: 3,
  arxiv_timeout_sec: 15,
} as const;

const DEFAULT_READ_PAPER_LIMIT = 50;

const DEFAULT_CONFIG: Config = {
  research_interest: "",
  researcher_profile: "",
  provider: "openai",
  base_url: "https://api.openai.com/v1",
  api_key: "",
  model: "gpt-4o-mini",
  temperature: 0.4,
  llm_roles: {},
  max_ideas: 6,
  ...STANDARD_FIND_DEFAULTS,
  arxiv_categories: [],
  arxiv_queries: [],
  arxiv_start_date: "",
  arxiv_end_date: "",
  biorxiv_categories: [],
  biorxiv_start_date: "",
  biorxiv_end_date: "",
  biorxiv_llm_candidate_limit: 0,
  biorxiv_llm_candidates_per_category: 0,
  nature_journals: ["nature", "natmachintell", "natcomputsci", "nmeth", "ncomms"],
  nature_article_types: ["article"],
  nature_start_date: "",
  nature_end_date: "",
  nature_candidate_limit: 200,
  science_journals: ["science", "sciadv"],
  science_article_types: ["Research Article"],
  science_start_date: "",
  science_end_date: "",
  science_candidate_limit: 200,
  github_languages: ["python"],
  github_since: "daily",
  hf_include_papers: true,
  hf_include_models: true,
  default_find_selection: {
    venue_ids: [],
    years: [2026],
    venue_years: [],
    include_arxiv: false,
    include_biorxiv: false,
    include_huggingface: false,
    include_github: false,
    include_nature: false,
    include_science: false,
  },
  email: {
    smtp_server: "",
    smtp_port: 465,
    sender: "",
    receivers: [],
    smtp_password: "",
    manual_enabled: true,
    auto_send_enabled: false,
    auto_send_stages: ["find", "read", "idea", "plan"],
  },
};

const NATURE_JOURNALS = [
  { slug: "nature", name: "Nature", tier: "T0" },
  { slug: "natmachintell", name: "Nature Machine Intelligence", tier: "T1" },
  { slug: "natcomputsci", name: "Nature Computational Science", tier: "T1" },
  { slug: "nmeth", name: "Nature Methods", tier: "T1" },
  { slug: "nbt", name: "Nature Biotechnology", tier: "T1" },
  { slug: "natbiomedeng", name: "Nature Biomedical Engineering", tier: "T1" },
  { slug: "ncomms", name: "Nature Communications", tier: "T1" },
  { slug: "nmat", name: "Nature Materials", tier: "T2" },
  { slug: "nchem", name: "Nature Chemistry", tier: "T2" },
  { slug: "natchemeng", name: "Nature Chemical Engineering", tier: "T2" },
  { slug: "natcatal", name: "Nature Catalysis", tier: "T2" },
  { slug: "natsynth", name: "Nature Synthesis", tier: "T2" },
  { slug: "nphys", name: "Nature Physics", tier: "T2" },
  { slug: "natelectron", name: "Nature Electronics", tier: "T2" },
  { slug: "nnano", name: "Nature Nanotechnology", tier: "T2" },
  { slug: "nphoton", name: "Nature Photonics", tier: "T2" },
  { slug: "nenergy", name: "Nature Energy", tier: "T2" },
  { slug: "nm", name: "Nature Medicine", tier: "T3" },
  { slug: "ng", name: "Nature Genetics", tier: "T3" },
  { slug: "neuro", name: "Nature Neuroscience", tier: "T3" },
  { slug: "nathumbehav", name: "Nature Human Behaviour", tier: "T3" },
  { slug: "nclimate", name: "Nature Climate Change", tier: "T3" },
  { slug: "sustainability", name: "Nature Sustainability", tier: "T3" },
  { slug: "ngeo", name: "Nature Geoscience", tier: "T3" },
  { slug: "natecolevol", name: "Nature Ecology & Evolution", tier: "T3" },
  { slug: "s41545", name: "Nature Water", tier: "T3" },
  { slug: "s43016", name: "Nature Food", tier: "T3" },
];

const NATURE_PRESETS = [
  {
    id: "core",
    name: "Core AI / Computational",
    journals: ["nature", "natmachintell", "natcomputsci", "nmeth", "ncomms"],
  },
  {
    id: "methods",
    name: "Methods / Bioengineering",
    journals: ["nmeth", "nbt", "natbiomedeng", "ncomms"],
  },
  {
    id: "materials",
    name: "AI for Science / Materials",
    journals: ["nmat", "nchem", "natchemeng", "natcatal", "natsynth", "nphys", "natelectron", "nnano", "nphoton", "nenergy"],
  },
  {
    id: "broad",
    name: "Broad Nature Research",
    journals: ["nm", "ng", "neuro", "nathumbehav", "nclimate", "sustainability", "ngeo", "natecolevol", "s41545", "s43016"],
  },
];

const NATURE_JOURNAL_NAMES = Object.fromEntries(NATURE_JOURNALS.map((journal) => [journal.slug, journal.name]));

const SCIENCE_JOURNALS = [
  { slug: "science", name: "Science", tier: "T0" },
  { slug: "sciadv", name: "Science Advances", tier: "T1" },
  { slug: "scirobotics", name: "Science Robotics", tier: "T1" },
  { slug: "stm", name: "Science Translational Medicine", tier: "T2" },
  { slug: "sciimmunol", name: "Science Immunology", tier: "T2" },
  { slug: "stke", name: "Science Signaling", tier: "T2" },
];

const SCIENCE_PARTNER_JOURNALS = [
  { slug: "adi", name: "Advanced Devices & Instrumentation", tier: "SPJ" },
  { slug: "bmr", name: "Biomaterials Research", tier: "SPJ" },
  { slug: "bmef", name: "BME Frontiers", tier: "SPJ" },
  { slug: "csbj", name: "Computational and Structural Biotechnology Journal", tier: "SPJ" },
  { slug: "csbr", name: "Computational and Structural Biotechnology Reports", tier: "SPJ" },
  { slug: "ehs", name: "Ecosystem Health and Sustainability", tier: "SPJ" },
  { slug: "energymatadv", name: "Energy Material Advances", tier: "SPJ" },
  { slug: "hds", name: "Health Data Science", tier: "SPJ" },
  { slug: "icomputing", name: "Intelligent Computing", tier: "SPJ" },
  { slug: "jemdr", name: "Journal of EMDR Practice and Research", tier: "SPJ" },
  { slug: "remotesensing", name: "Journal of Remote Sensing", tier: "SPJ" },
  { slug: "olar", name: "Ocean-Land-Atmosphere Research", tier: "SPJ" },
  { slug: "research", name: "Research", tier: "SPJ" },
  { slug: "space", name: "Space: Science & Technology", tier: "SPJ" },
  { slug: "ultrafastscience", name: "Ultrafast Science", tier: "SPJ" },
  { slug: "plantphenomics", name: "Plant Phenomics", tier: "migrated", disabled: true },
];

const SCIENCE_PRESETS = [
  { id: "core", name: "Science Core", journals: ["science", "sciadv"] },
  { id: "ai_robotics", name: "AI / Robotics / Engineering", journals: ["science", "sciadv", "scirobotics"] },
  { id: "bio_medicine", name: "Bio / Medicine", journals: ["stm", "sciimmunol", "stke", "sciadv"] },
  { id: "all", name: "All Science Family", journals: ["science", "sciadv", "scirobotics", "stm", "sciimmunol", "stke"] },
  { id: "spj_verified", name: "Science Partner Journals", journals: SCIENCE_PARTNER_JOURNALS.filter((journal) => !journal.disabled).map((journal) => journal.slug) },
];

const SCIENCE_JOURNAL_NAMES = Object.fromEntries([...SCIENCE_JOURNALS, ...SCIENCE_PARTNER_JOURNALS].map((journal) => [journal.slug, journal.name]));

type Tab = "find" | "read" | "ideas" | "plan" | "environment" | "experiment" | "paperWrite";
type Lang = "zh" | "en";
type ArtifactPanelSnapshot = { runId: string; artifacts: Artifact[] };
type CurrentFindArtifactScope = "find" | "read" | "ideas" | "plan";
type IdeaEditorDraft = { title: string; new_method: string; initial_experiment: string };

const markdownRenderer = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
}).use(texmath, {
  engine: katex,
  delimiters: ["dollars", "brackets", "beg_end"],
  katexOptions: {
    throwOnError: false,
    strict: "ignore",
    trust: false,
    output: "html",
  },
});

function planTitlesFromMarkdown(markdown: string) {
  const titles: Record<string, string> = {};
  const tokens = markdownRenderer.parse(String(markdown || ""), {});
  let candidateTitle = "";
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token.type === "heading_open" && token.tag === "h2") {
      const heading = String(tokens[index + 1]?.content || "").match(/^\d+\.\s+(.+)$/);
      candidateTitle = heading ? heading[1].trim() : "";
      continue;
    }
    if (!candidateTitle || token.type !== "inline") continue;
    const planId = String(token.content || "").match(/^\*\*Plan ID\*\*:\s*`([^`]+)`\s*$/i)?.[1]?.trim();
    if (planId) titles[planId] = candidateTitle;
  }
  return titles;
}

function fitRenderedMath(root: ParentNode = document) {
  const mathNodes = Array.from(root.querySelectorAll<HTMLElement>(".markdownBody .katex"));
  mathNodes.forEach((math) => { math.style.fontSize = ""; });
  const displayNodes = Array.from(root.querySelectorAll<HTMLElement>(".markdownBody .katex-display"));
  displayNodes.forEach((display) => { display.classList.remove("fitWrappedMath"); });
  const narrowViewport = window.matchMedia("(max-width: 760px)").matches;

  const shrinkToWidth = (math: HTMLElement, container: HTMLElement, useScrollWidth = false) => {
    const containerWidth = container.clientWidth;
    const available = containerWidth - 3;
    if (available <= 0) return;
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const required = useScrollWidth ? container.scrollWidth : math.getBoundingClientRect().width;
      if (required <= containerWidth + 1) break;
      const fontSize = Number.parseFloat(window.getComputedStyle(math).fontSize);
      if (!Number.isFinite(fontSize) || fontSize <= 0) break;
      math.style.fontSize = `${fontSize * available / required}px`;
    }
  };

  displayNodes.forEach((display) => {
    const math = display.querySelector<HTMLElement>(":scope > .katex");
    if (!math) return;
    if (display.scrollWidth > display.clientWidth + 1) {
      display.classList.add("fitWrappedMath");
    }
    if (display.scrollWidth > display.clientWidth + 1) {
      shrinkToWidth(math, display, true);
    }
  });
  if (!narrowViewport) return;
  mathNodes.forEach((math) => {
    if (math.closest(".katex-display")) return;
    const container = math.closest<HTMLElement>("p, li, td, th, .markdownBody");
    if (container) shrinkToWidth(math, container);
  });
}

const FIND_RUN_ARTIFACT_TABS: Tab[] = ["find", "read", "ideas", "plan"];
const CURRENT_FIND_SCOPE_ARTIFACT_NAMES: Record<CurrentFindArtifactScope, string[]> = {
  find: ["find.md", "source_status.md", "find_progress.json", "find_results.json", "selection.json"],
  read: ["read.md", "read_results.json"],
  ideas: ["idea.md", "ideas.json"],
  plan: ["plan.md", "plans.json"],
};

function currentFindArtifactScope(tab: Tab): CurrentFindArtifactScope {
  return FIND_RUN_ARTIFACT_TABS.includes(tab) ? tab as CurrentFindArtifactScope : "find";
}


function isFallbackPaper(paper: any) {
  const source = String(paper?.reason_source || "").toLowerCase();
  return source.startsWith("adaptive profile");
}

function normalizedMetadataText(value: any) {
  return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function paperTldrText(paper: any) {
  const metadata = paper?.metadata && typeof paper.metadata === "object" ? paper.metadata : {};
  const raw = metadata.tldr || paper?.tldr;
  if (raw && typeof raw === "object") return normalizedMetadataText(raw.text);
  return normalizedMetadataText(raw);
}

function hasRealPaperAbstract(paper: any) {
  const abstractText = String(paper?.abstract_en || paper?.abstract || "").trim();
  const abstract = normalizedMetadataText(abstractText);
  const normalized = abstract.replace(/\.$/, "");
  if (!abstract || ["no abstract available", "abstract not available", "n/a", "none", "null"].includes(normalized)) return false;
  const metadata = paper?.metadata && typeof paper.metadata === "object" ? paper.metadata : {};
  const abstractSource = String(metadata.abstract_source || paper?.abstract_source || "").toLowerCase();
  if (abstractSource.includes("tldr")) return false;
  const tldr = paperTldrText(paper);
  if (tldr && abstract === tldr) return false;
  return true;
}

function isFinalFindRecommendationPaper(paper: any) {
  if (!paper || typeof paper !== "object") return false;
  if (isFallbackPaper(paper)) return false;
  if (paper.retrieval_pool_only || paper.llm_final_scoring_skipped || paper.llm_retry_exhausted) return false;
  if (!Boolean(paper.title || paper.id)) return false;
  if (!hasRealPaperAbstract(paper)) return false;
  const publicRecommendation = Boolean(paper.public_recommendation);
  const reasonSource = String(paper.reason_source || "").toLowerCase();
  const scoreSource = String(paper.score_source || "").toLowerCase();
  const finalScored = publicRecommendation || reasonSource === "llm abstract evaluation" || scoreSource === "llm_title_abstract_score_only";
  if (!finalScored) return false;
  const hasFit = paper.llm_fit_score !== undefined || paper.fit_score !== undefined;
  const recommended = publicRecommendation || Boolean(paper.find_recommendation || paper.recommended_by_llm_ranking || paper._user_visible_recommendation);
  return recommended && hasFit;
}

function isPositiveLiteraturePaper(paper: any) {
  return isFinalFindRecommendationPaper(paper);
}

function positiveLiteraturePapers(rows: any[]) {
  return asArray(rows).filter((paper: any) => isPositiveLiteraturePaper(paper));
}

function recommendationLiteraturePapers(rows: any[]) {
  return asArray(rows).filter((paper: any) => isFinalFindRecommendationPaper(paper));
}

function readableLiteraturePapers(rows: any[]) {
  return asArray(rows).filter((paper: any) => isFinalFindRecommendationPaper(paper));
}

function auditLiteraturePapers(_rows: any[]) {
  return [];
}

function strictStrongLiteraturePapers(rows: any[]) {
  return positiveLiteraturePapers(rows);
}
function sourceAllowedBySelection(item: any, selection: any) {
  if (!item || typeof item !== "object") return false;
  const source = String(item.source || item.venue || "").toLowerCase();
  const url = String(item.url || item.pdf_url || "").toLowerCase();
  if (source === "arxiv" || url.includes("arxiv.org")) return Boolean(selection?.include_arxiv);
  if (source === "biorxiv" || url.includes("biorxiv.org")) return Boolean(selection?.include_biorxiv);
  if (source === "nature" || url.includes("nature.com")) return Boolean(selection?.include_nature);
  if (source === "science" || url.includes("science.org")) return Boolean(selection?.include_science);
  if (source === "huggingface" || source === "hf" || url.includes("huggingface.co")) return Boolean(selection?.include_huggingface);
  if (source === "github" || url.includes("github.com")) return Boolean(selection?.include_github);
  return true;
}

function filterBySourceSelection(items: any[], selection: any) {
  return asArray(items).filter((item: any) => sourceAllowedBySelection(item, selection));
}


function containsCJKText(value: any) {
  return /[一-鿿]/.test(String(value ?? ""));
}

const INTERNAL_FIND_PUBLIC_TEXT_MARKERS = [
  "weak:",
  "passed:",
  "strong:",
  "topic_evidence",
  "matched_topic_route",
  "adaptive topic evidence",
  "adaptive_llm_topic_route",
  "missing adaptive topic evidence",
  "缺少当前主题",
  "高召回",
  "内部候选",
  "对系统实现的直接含义",
  "对AR实现",
  "guardrail",
  "最终 LLM",
  "LLM 题名",
  "LLM 评分",
  "题名+摘要评分",
  "最终题名+摘要",
  "题名筛选线索",
  "最终相关性评分",
  "Find",
  "Top-N",
  "证据门控",
  "用户可见推荐",
  "推荐池",
  "检索候选",
  "Gate reason",
  "paper-conclusion",
  "claim",
  "foundation",
  "high-recall",
  "internal candidate",
  "implementation",
  "final title+abstract",
  "LLM score",
  "evidence gate",
  "user-visible",
  "recommendation pool",
  "retrieval candidate",
  "fallback-only",
  "minimum_target",
  "minimum target",
  "fetch_limit=",
];

function hasInternalFindPublicText(value: any) {
  const lower = String(value ?? "").toLowerCase();
  return Boolean(lower) && INTERNAL_FIND_PUBLIC_TEXT_MARKERS.some((marker) => lower.includes(String(marker).toLowerCase()));
}


const DEFAULT_FIND_YEAR = 2026;

const CORE_VENUE_IDS = ["openreview_iclr_2026", "openreview_neurips", "dblp_icml", "dblp_kdd"];

const CORE_VENUE_FALLBACKS: Record<string, Venue> = {
  openreview_iclr_2026: {
    id: "openreview_iclr_2026",
    source: "openreview",
    name: "ICLR",
    full_name: "International Conference on Learning Representations",
    type: "conference",
    rank: "high-level",
    field: "Artificial Intelligence",
    years: [2026, 2025, 2024, 2023],
    classification_source: "official",
  },
  openreview_neurips: {
    id: "openreview_neurips",
    source: "openreview",
    name: "NeurIPS",
    full_name: "Conference on Neural Information Processing Systems",
    type: "conference",
    rank: "high-level",
    field: "Artificial Intelligence / Machine Learning",
    years: [2026, 2025, 2024, 2023],
    classification_source: "official",
  },
  dblp_icml: {
    id: "dblp_icml",
    source: "dblp",
    name: "ICML",
    full_name: "International Conference on Machine Learning",
    type: "conference",
    rank: "high-level",
    field: "Artificial Intelligence / Machine Learning",
    years: [2026, 2025, 2024, 2023],
    classification_source: "official",
  },
  dblp_kdd: {
    id: "dblp_kdd",
    source: "dblp",
    name: "KDD",
    full_name: "ACM SIGKDD Conference on Knowledge Discovery and Data Mining",
    type: "conference",
    rank: "high-level",
    field: "Data Mining / Recommendation",
    years: [2026, 2025, 2024, 2023],
    classification_source: "official",
  },
  dblp_sigir: {
    id: "dblp_sigir",
    source: "dblp",
    name: "SIGIR",
    full_name: "ACM SIGIR Conference on Research and Development in Information Retrieval",
    type: "conference",
    rank: "high-level",
    field: "Information Retrieval / Recommendation",
    years: [2026, 2025, 2024, 2023],
    classification_source: "official",
  },
};

function venueYearLabel(venue: Venue, label = "Available years", empty = "not indexed") {
  const years = (venue.years || []).filter(Boolean);
  return years.length ? `${label}: ${years.slice(0, 4).join(", ")}` : `${label}: ${empty}`;
}

function selectedYearLabel(years: number[], label = "Selected year") {
  const selected = years.filter(Boolean);
  const zh = /[一-鿿]/.test(label);
  const separator = zh ? "，" : ", ";
  const colon = zh ? "：" : ": ";
  return `${label}${colon}${selected.length ? selected.join(separator) : DEFAULT_FIND_YEAR}`;
}

function uniqueYearsDesc(values: Array<number | string | undefined | null>) {
  const years = values
    .map((item) => Number(item))
    .filter((year) => Number.isInteger(year) && year >= 2000 && year <= 2100);
  return Array.from(new Set(years)).sort((a, b) => b - a);
}

function normalizeVenueIdentityText(value: any) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
}

function venueIdentityKey(venue?: Venue, fallbackId = "") {
  const fullName = normalizeVenueIdentityText(venue?.full_name);
  if (fullName) return `full:${fullName}`;
  const name = normalizeVenueIdentityText(venue?.name);
  if (name) return `name:${name === "kdd" ? "sigkdd" : name}`;
  return normalizeVenueIdentityText(fallbackId || venue?.id).replace(/[_-](19|20)\d{2}$/, "");
}

function uniqueVenuesByIdentity(venues: Venue[]) {
  const seen = new Set<string>();
  return venues.filter((venue) => {
    const key = venueIdentityKey(venue, venue.id);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function selectedVenueIdForVenue(selectedVenueIds: string[], targetVenue: Venue, venueById: Map<string, Venue>) {
  const targetKey = venueIdentityKey(targetVenue, targetVenue.id);
  return selectedVenueIds.find((id) => venueIdentityKey(venueById.get(id) || CORE_VENUE_FALLBACKS[id], id) === targetKey) || "";
}

function dedupeVenueSelectionByIdentity(venueIds: string[], venueYears: Record<string, number[]>, venueById: Map<string, Venue>) {
  const seen = new Map<string, string>();
  const nextIds: string[] = [];
  const nextYears: Record<string, number[]> = {};
  for (const id of venueIds) {
    const venue = venueById.get(id) || CORE_VENUE_FALLBACKS[id];
    const canonicalId = venue?.canonical_id && venueById.has(venue.canonical_id) ? venue.canonical_id : id;
    const key = venueIdentityKey(venue, id);
    const existingId = seen.get(key);
    if (existingId) {
      nextYears[existingId] = uniqueYearsDesc([...(nextYears[existingId] || []), ...(venueYears[id] || []), ...(venueYears[canonicalId] || [])]);
      continue;
    }
    seen.set(key, canonicalId);
    nextIds.push(canonicalId);
    nextYears[canonicalId] = uniqueYearsDesc([...(venueYears[id] || []), ...(venueYears[canonicalId] || [])]);
  }
  return { venueIds: nextIds, venueYears: nextYears };
}

function venueMapWithAliases(venues: Venue[]) {
  const map = new Map<string, Venue>();
  for (const venue of venues) {
    map.set(venue.id, venue);
    for (const alias of venue.aliases || []) {
      const aliasId = String(alias?.id || "").trim();
      if (aliasId && !map.has(aliasId)) map.set(aliasId, { ...venue, id: aliasId, canonical_id: venue.id });
    }
  }
  return map;
}

function sameVenueYearMap(left: Record<string, number[]>, right: Record<string, number[]>) {
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return sameStringArray(leftKeys, rightKeys) && leftKeys.every((key) => sameStringArray((left[key] || []).map(String), (right[key] || []).map(String)));
}

function normalizedVenueIdKey(value: any) {
  return normalizeVenueIdentityText(value).replace(/[_-](19|20)\d{2}$/, "");
}

function venueComparableKeys(venueId: any, venueById: Map<string, Venue>) {
  const id = String(venueId || "").trim();
  const keys = new Set<string>();
  const idKey = normalizedVenueIdKey(id);
  if (idKey) keys.add(`id:${idKey}`);
  const venue = venueById.get(id) || CORE_VENUE_FALLBACKS[id];
  const identityKey = venueIdentityKey(venue, id);
  if (identityKey) keys.add(`venue:${identityKey}`);
  return keys;
}

function venueYearComparableKeys(venueId: any, year: any, venueById: Map<string, Venue>) {
  const yearNumber = Number(year);
  const yearKey = Number.isInteger(yearNumber) ? String(yearNumber) : String(year || "").trim();
  return Array.from(venueComparableKeys(venueId, venueById)).map((key) => `${key}:${yearKey}`);
}

function sameStringArray(left: string[], right: string[]) {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function defaultVenueYearMap(venueIds: string[] = []) {
  return Object.fromEntries(venueIds.map((venueId) => [venueId, [DEFAULT_FIND_YEAR]])) as Record<string, number[]>;
}

function venueYearMapFromSelection(selection: any, venueIds: string[]) {
  const result: Record<string, number[]> = {};
  const addYears = (venueId: string, rawYears: any) => {
    const id = String(venueId || "").trim();
    if (!id) return;
    const values = Array.isArray(rawYears) ? rawYears : [rawYears];
    result[id] = uniqueYearsDesc([...(result[id] || []), ...values]);
  };
  const explicitVenueYears = Array.isArray(selection?.venue_years) ? selection.venue_years : [];
  if (explicitVenueYears.length) {
    for (const item of explicitVenueYears) {
      if (!item || typeof item !== "object") continue;
      addYears(item.venue_id || item.venue || item.id, Array.isArray(item.years) ? item.years : item.year);
    }
  }
  if (!Object.keys(result).length) {
    const selectedYears = Array.isArray(selection?.years) && selection.years.length
      ? normalizeSelectedYears(selection.years)
      : [];
    const latestConfiguredYear = selectedYears[0] || DEFAULT_FIND_YEAR;
    for (const venueId of venueIds) addYears(venueId, [latestConfiguredYear]);
  }
  for (const venueId of venueIds) {
    if (!result[venueId]?.length) result[venueId] = [DEFAULT_FIND_YEAR];
  }
  return result;
}

function yearsForVenue(map: Record<string, number[]>, venueId: string) {
  return map[venueId]?.length ? map[venueId] : [DEFAULT_FIND_YEAR];
}

function addYearsForVenue(map: Record<string, number[]>, venueId: string, years: number[]) {
  return {
    ...map,
    [venueId]: uniqueYearsDesc([...(map[venueId] || []), ...years]),
  };
}

function venueYearPairs(venueIds: string[], map: Record<string, number[]>) {
  return venueIds.flatMap((venueId) => yearsForVenue(map, venueId).map((year) => ({ venue_id: venueId, year })));
}

function yearsFromVenueYearMap(venueIds: string[], map: Record<string, number[]>) {
  return uniqueYearsDesc(venueYearPairs(venueIds, map).map((pair) => pair.year));
}

function venueMetaLabel(venue: Venue, labels?: Record<string, string>, selectedYears?: number[]) {
  const yearLabel = selectedYears && selectedYears.length
    ? selectedYearLabel(selectedYears, labels?.selectedYear || "Selected year")
    : venueYearLabel(venue, labels?.availableYears || "Available years", labels?.notIndexed || "not indexed");
  const selectedYearSet = new Set((selectedYears || []).filter(Boolean));
  const unavailableSelectedYears = selectedYearSet.size
    ? Array.from(selectedYearSet).filter((year) => !(venue.years || []).includes(year))
    : [];
  const availabilityNote = unavailableSelectedYears.length
    ? `${labels?.notIndexed || "not indexed"}: ${unavailableSelectedYears.join(", ")}`
    : "";
  return [venue.source, venue.type, venue.rank, yearLabel, availabilityNote].filter(Boolean).join(" / ");
}


function sourceStatusLabel(item: any, venueById?: Map<string, Venue>, lang: Lang = "zh") {
  const source = String(item?.source || item?.venue || item?.venue_id || "source");
  const kind = String(item?.source_kind || "");
  if (kind === "venue_summary") return lang === "zh" ? "出版渠道汇总" : "Publication venue summary";
  if (kind === "venue") {
    const years = asArray(item?.effective_years).length ? ` ${asArray(item.effective_years).join(",")}` : "";
    const venue = venueById?.get(String(item?.venue_id || "")) || venueById?.get(source);
    const label = String(venue?.name || item?.venue || source || "venue").trim();
    return `${label}${years}`;
  }
  if (source === "biorxiv") return "bioRxiv";
  if (source === "nature") return "Nature Portfolio";
  if (source === "science") return "Science Family";
  if (source === "venue_summary") return lang === "zh" ? "出版渠道汇总" : "Publication venue summary";
  return source;
}

function venueSourceStatusRows(findResults: any) {
  return asArray(findResults?.venue_health_report).map((row: any) => {
    const effectiveYears = asArray(row?.effective_years);
    const parts = [];
    if (row?.adapter) parts.push(`adapter=${row.adapter}`);
    if (effectiveYears.length) parts.push(`years=${effectiveYears.join(",")}`);
    if (row?.corpus_count !== undefined) parts.push(`corpus=${row.corpus_count}`);
    if (row?.candidate_count !== undefined) parts.push(`screen_input=${row.candidate_count}`);
    if (row?.sample_count !== undefined) parts.push(`fetched=${row.sample_count}`);
    if (row?.year_fallback_reason) parts.push(String(row.year_fallback_reason));
    if (row?.error) parts.push(String(row.error));
    const normalized = {
      source: row?.venue || row?.venue_id || "venue",
      source_kind: "venue",
      venue_id: row?.venue_id || "",
      venue: row?.venue || row?.venue_id || "venue",
      ok: Boolean(row?.ok),
      limited: Boolean(row?.limited || row?.metadata_completeness_limited),
      count: Number(row?.candidate_count || row?.sample_count || row?.corpus_count || 0),
      message: parts.join("; ") || (row?.ok ? "ok" : "No papers fetched."),
      adapter: row?.adapter || "",
      requested_years: row?.requested_years || [],
      effective_years: effectiveYears,
      raw_title_index_count: row?.corpus_count || row?.sample_count || 0,
      candidate_count: row?.candidate_count || row?.sample_count || 0,
      title_index_completeness_status: row?.title_index_completeness_status || "",
      title_index_completeness_ok: Boolean(row?.title_index_completeness_ok),
      metadata_completeness_status: row?.metadata_completeness_status || "",
      metadata_completeness_ok: Boolean(row?.metadata_completeness_ok),
      metadata_completeness_limited: Boolean(row?.metadata_completeness_limited),
      metadata_completeness_basis: row?.metadata_completeness_basis || "",
      source_scope: row?.source_scope || "",
      official_title_index_verified: row?.official_title_index_verified,
      official_accepted_list_verified: row?.official_accepted_list_verified,
      source_verified: row?.source_verified,
      category_status: row?.category_status || "",
      has_official_categories: Boolean(row?.has_official_categories),
      has_abstracts: Boolean(row?.has_abstracts),
      has_abstracts_in_title_index: Boolean(row?.has_abstracts_in_title_index || row?.has_abstracts),
      any_abstracts: Boolean(row?.any_abstracts || row?.has_abstracts),
      missing_abstract_count: row?.missing_abstract_count || 0,
    };
    normalized.limited = sourceStatusIsLimited(normalized);
    return normalized;
  });
}

function expandedSourceStatusRows(findResults: any) {
  const rows = asArray(findResults?.source_status);
  const nonAggregate = rows.filter((row: any) => {
    const source = String(row?.source || "").trim().toLowerCase();
    const kind = String(row?.source_kind || "").trim().toLowerCase();
    return source !== "venues" && source !== "venue summary" && source !== "venue_summary" && kind !== "venue_summary";
  });
  // find_progress.json already carries the current per-channel stage counts.
  // Do not append venue_health_report-derived rows on top of it, or the UI
  // shows each venue twice and mixes health-scan counts with live pipeline counts.
  if (nonAggregate.length) return nonAggregate;
  return venueSourceStatusRows(findResults);
}

function sourceStatusHasUsableOpenReviewMetadata(item: any) {
  const adapter = String(item?.adapter || item?.source_adapter || "").toLowerCase();
  const categoryStatus = String(item?.category_status || "").toLowerCase();
  const hasOfficialCategories = Boolean(item?.has_official_categories) && !["no_official_categories", "missing_categories", "no_or_partial_categories"].includes(categoryStatus);
  const hasAbstracts = Boolean(item?.has_abstracts_in_title_index || item?.has_abstracts || item?.any_abstracts);
  const metadataComplete = Boolean(item?.metadata_completeness_ok);
  const titleIndexComplete = Boolean(item?.title_index_completeness_ok || item?.title_index_complete);
  const sourceVerified = Boolean(item?.source_verified || item?.official_title_index_verified || String(item?.source_scope || "") === "official_openreview_metadata");
  return Boolean(item?.ok) && adapter.includes("openreview") && hasOfficialCategories && hasAbstracts && metadataComplete && titleIndexComplete && sourceVerified;
}

function sourceStatusIsLimited(item: any) {
  if (item?.limited || item?.metadata_completeness_limited) return true;
  if (item && Object.prototype.hasOwnProperty.call(item, "metadata_completeness_ok") && !Boolean(item.metadata_completeness_ok)) return true;
  if (item && Object.prototype.hasOwnProperty.call(item, "title_index_completeness_ok") && !Boolean(item.title_index_completeness_ok)) return true;
  if (item && Object.prototype.hasOwnProperty.call(item, "title_index_complete") && !Boolean(item.title_index_complete)) return true;
  if (sourceStatusHasUsableOpenReviewMetadata(item)) return false;
  return false;
}

function sourceStatusMessageText(value: any, lang: Lang) {
  const text = String(value ?? "").trim();
  if (!text || /^(adapter|years|corpus|screen_input|fetched|metadata|category)=/i.test(text)) return "";
  const basis = text.toLowerCase();
  if (basis.includes("local venue database integrity check") || basis.includes("title corpus was verified") || basis.includes("this source does not expose abstracts") || basis.includes("no trusted official venue categories") || basis.includes("ar skips category pruning")) return "";
  if (lang !== "zh") return text.replace(/_/g, " ");
  const lowered = text.toLowerCase();
  if (lowered.startsWith("openreview official venue notes were fetched")) return "OpenReview 官方元数据已抓取，并解析标题、摘要和分类";
  if (lowered.startsWith("source remains partial until")) return "";
  if (lowered.startsWith("requested years") && lowered.includes("had no usable")) {
    return text
      .replace(/^requested years/i, "请求年份")
      .replace(/had no usable/i, "暂无可用")
      .replace(/title index as of/i, "标题索引，截至")
      .replace(/release date/i, "发布时间")
      .replace(/is after run date/i, "晚于运行日期")
      .replace(/ via ([^;]+)/i, "（适配器 $1）");
  }
  if (lowered.startsWith("using latest available")) {
    return text
      .replace(/^using latest available/i, "使用最新可用")
      .replace(/title index year/i, "标题索引年份")
      .replace(/ via /i, "，适配器 ")
      .replace(/\.$/, "");
  }
  if (lowered === "no fallback year was used") return "未使用年份回退";
  if (lowered === "no title index found.") return "未找到标题索引。";
  if (lowered.startsWith("adapter did not provide an explicit venue metadata completeness audit")) return "";
  if (lowered.startsWith("official icml downloads/virtual page is reachable")) return "ICML 官方下载页可访问，已扫描符合条件的论文链接";
  if (lowered.startsWith("dblp paginated stream search over the current dblp index")) return "已扫描当前 DBLP 索引；这只验证标题索引";
  if (lowered.includes("the workflow skips category pruning and uses title llm screening")) return "无官方分类时，直接对标题库做 LLM 标题筛选";
  if (lowered === "ok") return "抓取正常";
  return text.replace(/_/g, " ");
}

function sourceMetadataStatusText(status: any, lang: Lang, item?: any) {
  const zh = lang === "zh";
  const key = String(status || "").trim().toLowerCase();
  if (!key) return "";
  if (key === "complete") return zh ? "元数据完整" : "metadata complete";
  if (key === "title_index_only") return zh ? "标题索引可用，详情阶段补摘要" : "title index available; abstracts are enriched later";
  if (key === "partial") return sourceStatusHasUsableOpenReviewMetadata(item) ? "" : zh ? "元数据部分可用" : "metadata partially available";
  if (key === "missing") return zh ? "元数据缺失" : "metadata missing";
  return key.replace(/_/g, " ");
}

function sourceScopeText(item: any, lang: Lang) {
  const zh = lang === "zh";
  const scope = String(item?.source_scope || "").trim().toLowerCase();
  const adapter = String(item?.adapter || item?.source_adapter || "").trim().toLowerCase();
  if (scope === "official_icml_downloads_title_index" || adapter.startsWith("icml_downloads")) return zh ? "ICML 官方标题索引已核验" : "official ICML title index verified";
  if (scope === "official_openreview_metadata" || adapter.startsWith("openreview")) return zh ? "OpenReview 官方元数据已核验" : "official OpenReview metadata verified";
  if (scope === "dblp_current_index_not_official_accepted_list" || adapter.startsWith("dblp")) return zh ? "DBLP 当前索引，非官方录用清单" : "DBLP current index, not an official accepted list";
  if (item?.official_title_index_verified === true) return zh ? "官方标题索引已核验" : "official title index verified";
  if (item?.official_title_index_verified === false) return zh ? "未核验官方标题索引" : "official title index not verified";
  return "";
}

function sourceCategoryAvailabilityText(item: any, lang: Lang) {
  const zh = lang === "zh";
  if (item?.has_official_categories) return zh ? "有官方分类" : "official categories available";
  const status = String(item?.category_status || "").trim().toLowerCase();
  if (status === "no_official_categories" || status === "no_or_partial_categories" || status === "missing_categories") return zh ? "无官方分类，进入标题筛选" : "no official categories; title screening is used";
  if (status && status !== "unknown") return status.replace(/_/g, " ");
  return "";
}

function sourceAbstractAvailabilityText(item: any, lang: Lang) {
  const zh = lang === "zh";
  if (item?.has_abstracts_in_title_index || item?.has_abstracts) return zh ? "题录含摘要" : "abstracts present in records";
  if (item?.any_abstracts) return zh ? "部分条目已有摘要" : "some abstracts present";
  const missing = Number(item?.missing_abstract_count || 0);
  if (missing > 0 || String(item?.metadata_completeness_status || "") === "title_index_only") return zh ? "题录无摘要，详情阶段补摘要" : "records have no abstracts; details stage enriches abstracts";
  return "";
}

function sourceStatusDetail(item: any, lang: Lang = "zh") {
  const zh = lang === "zh";
  const labels = zh
    ? { status: "状态", ok: "正常", limited: "受限", failed: "失败", checking: "检查中", raw: "题录总数", screen: "来源候选", detail: "详情已抓取", adapter: "来源适配器", years: "有效年份", requested: "请求年份", metadata: "元数据完整性" }
    : { status: "Status", ok: "ok", limited: "limited", failed: "failed", checking: "checking", raw: "record total", screen: "source candidates", detail: "details fetched", adapter: "adapter", years: "effective years", requested: "requested years", metadata: "metadata completeness" };
  const rawStatus = String(item?.status || item?.phase || "").trim().toLowerCase();
  const limited = sourceStatusIsLimited(item);
  const state = rawStatus === "checking" || rawStatus === "fetching" ? labels.checking : limited ? labels.limited : item?.ok ? labels.ok : labels.failed;
  const parts: string[] = [];
  const seen = new Set<string>();
  const pushPart = (value: any) => {
    const text = String(value ?? "").trim();
    if (!text) return;
    const key = text.toLowerCase().replace(/\s+/g, " ");
    if (seen.has(key)) return;
    seen.add(key);
    parts.push(text);
  };
  pushPart(`${labels.status}: ${state}`);
  const isVenueHealth = String(item?.source_kind || "").trim().toLowerCase() === "venue_health";
  const rawTitleIndex = isVenueHealth ? undefined : item?.raw_title_index_count ?? item?.corpus_count;
  if (rawTitleIndex !== undefined && rawTitleIndex !== "") pushPart(`${labels.raw}: ${rawTitleIndex}`);
  const count = isVenueHealth ? item?.health_sample_count ?? item?.sample_count ?? item?.count : item?.count ?? item?.candidate_count ?? item?.sample_count;
  if (isVenueHealth) {
    if (count !== undefined && count !== "") pushPart(`${zh ? "健康检查样本" : "health-check samples"}: ${count}`);
  } else if (count !== undefined && count !== "") pushPart(`${labels.screen}: ${count}`);
  const fetchLimit = Number(item?.fetch_limit || 0);
  if (fetchLimit > 0) pushPart(`${zh ? "抓取上限" : "fetch limit"}: ${fetchLimit}`);
  const detailFetched = item?.detail_fetched_count ?? item?.detail_fetched ?? item?.fetched_count;
  if (detailFetched !== undefined && detailFetched !== "") pushPart(`${labels.detail}: ${detailFetched}`);
  const scopeText = sourceScopeText(item, lang);
  if (scopeText) pushPart(scopeText);
  const metadataText = sourceMetadataStatusText(item?.metadata_completeness_status, lang, item);
  if (metadataText) pushPart(metadataText);
  const categoryText = sourceCategoryAvailabilityText(item, lang);
  if (categoryText) pushPart(categoryText);
  const abstractText = sourceAbstractAvailabilityText(item, lang);
  if (abstractText) pushPart(abstractText);
  if (item?.adapter) pushPart(`${labels.adapter}: ${item.adapter}`);
  if (asArray(item?.effective_years).length) pushPart(`${labels.years}: ${asArray(item.effective_years).join(", ")}`);
  if (asArray(item?.requested_years).length) pushPart(`${labels.requested}: ${asArray(item.requested_years).join(", ")}`);
  if (item?.raw_count !== undefined) pushPart(`${zh ? "原始条目" : "raw"}: ${item.raw_count}`);
  if (item?.prefiltered_count !== undefined) pushPart(`${zh ? "预筛后" : "prefiltered"}: ${item.prefiltered_count}`);
  if (asArray(item?.journals).length) pushPart(`${zh ? "期刊" : "journals"}: ${asArray(item.journals).join(", ")}`);
  if (asArray(item?.categories).length) pushPart(`${zh ? "分类" : "categories"}: ${asArray(item.categories).join(", ")}`);
  if (item?.date_coverage?.oldest || item?.date_coverage?.newest) pushPart(`${zh ? "日期范围" : "dates"}: ${item.date_coverage.oldest || "?"}..${item.date_coverage.newest || "?"}`);
  const stoppedReason = String(item?.stopped_reason || "").trim().toLowerCase();
  if (limited && stoppedReason === "openalex_daily_budget_exhausted") pushPart(zh ? "OpenAlex 日预算已耗尽" : "OpenAlex daily API budget exhausted");
  if (limited && stoppedReason === "openalex_rate_limited") pushPart(zh ? "OpenAlex 请求频率受限" : "OpenAlex rate limited");
  if (item?.message) String(item.message).split(";").map((chunk) => sourceStatusMessageText(chunk, lang)).filter((chunk) => !hasInternalFindPublicText(chunk)).forEach((chunk) => pushPart(chunk));
  return parts.join(" / ");
}

function sourceStatusCompactDetail(item: any, lang: Lang = "zh") {
  const zh = lang === "zh";
  const labels = zh
    ? { status: "状态", ok: "正常", limited: "受限", failed: "失败", checking: "检查中", raw: "题录总数", screen: "来源候选", yearUsed: "使用年份", requested: "请求年份" }
    : { status: "Status", ok: "ok", limited: "limited", failed: "failed", checking: "checking", raw: "record total", screen: "source candidates", yearUsed: "year used", requested: "requested year" };
  const rawStatus = String(item?.status || item?.phase || "").trim().toLowerCase();
  const limited = sourceStatusIsLimited(item);
  const state = rawStatus === "checking" || rawStatus === "fetching" ? labels.checking : limited ? labels.limited : item?.ok ? labels.ok : labels.failed;
  const parts: string[] = [];
  const seen = new Set<string>();
  const pushPart = (value: any) => {
    const line = String(value ?? "").trim();
    if (!line) return;
    const key = line.toLowerCase().replace(/\s+/g, " ");
    if (seen.has(key)) return;
    seen.add(key);
    parts.push(line);
  };
  pushPart(`${labels.status}: ${state}`);
  const isVenueHealth = String(item?.source_kind || "").trim().toLowerCase() === "venue_health";
  const rawTitleIndex = isVenueHealth ? undefined : item?.raw_title_index_count ?? item?.corpus_count;
  if (rawTitleIndex !== undefined && rawTitleIndex !== "") pushPart(`${labels.raw}: ${rawTitleIndex}`);
  const count = isVenueHealth ? item?.health_sample_count ?? item?.sample_count ?? item?.count : item?.count ?? item?.candidate_count ?? item?.sample_count;
  if (isVenueHealth) {
    if (count !== undefined && count !== "") pushPart(`${zh ? "健康检查样本" : "health-check samples"}: ${count}`);
  } else if (count !== undefined && count !== "") pushPart(`${labels.screen}: ${count}`);
  const fetchLimit = Number(item?.fetch_limit || 0);
  if (fetchLimit > 0) pushPart(`${zh ? "抓取上限" : "fetch limit"}: ${fetchLimit}`);
  const effectiveYears = asArray(item?.effective_years).map(String).filter(Boolean);
  const requestedYears = asArray(item?.requested_years).map(String).filter(Boolean);
  if (effectiveYears.length && requestedYears.length && effectiveYears.join(",") !== requestedYears.join(",")) {
    pushPart(`${labels.yearUsed}: ${effectiveYears.join(", ")}`);
    pushPart(`${labels.requested}: ${requestedYears.join(", ")}`);
  }
  const failed = !item?.ok && !limited && rawStatus !== "checking" && rawStatus !== "fetching";
  if (failed && item?.message) {
    const conciseFailure = String(item.message).split(";").map((chunk) => sourceStatusMessageText(chunk, lang)).filter((chunk) => !hasInternalFindPublicText(chunk)).find(Boolean);
    if (conciseFailure) pushPart(conciseFailure);
  }
  return parts.join(" / ");
}

function sourceStatusArtifactMarkdown(rows: any[], lang: Lang = "zh") {
  const sourceRows = asArray(rows).filter((row: any) => row && typeof row === "object");
  if (!sourceRows.length) return "";
  const title = lang === "zh" ? "来源状态" : "Source Status";
  const intro = lang === "zh"
    ? "每一行对应一个真实 Find 来源或出版渠道，来源候选只描述该行自身的抓取结果。下方统计候选题录进入标题筛选、标题 LLM 和标题+摘要 LLM 综合评分的数量；不同来源可以从不同步骤进入。详情已抓取表示详情阶段获得摘要或链接的候选数量。"
    : "Each row represents one real Find source or publication venue, and source candidates describe only that row's retrieval result. The counts below track candidates entering title screening, title LLM, and title+abstract LLM scoring; sources may enter at different steps. Details fetched counts candidates enriched with abstracts or links.";
  const lines = [`# ${title}`, "", intro, ""];
  sourceRows.forEach((item: any) => {
    lines.push(`## ${sourceStatusLabel(item, undefined, lang)}`, "", `- ${sourceStatusDetail(item, lang)}`, "");
  });
  return `${lines.join("\n").trim()}\n`;
}

function paperQualityLabels(paper: any) {
  const explicit = asArray(paper?.quality_labels).concat(asArray(paper?.presentation_labels));
  const text = [
    paper?.track,
    paper?.decision,
    paper?.presentation,
    paper?.presentation_type,
    paper?.paper_type,
    paper?.acceptance_type,
    paper?.status,
  ].join(" ").toLowerCase();
  const labels = explicit.map((item) => String(item).trim()).filter(Boolean);
  if (/\b(best|award|outstanding|distinguished)[-\s]+paper\b/.test(text)) labels.push("best paper/award");
  if (/\boral\b/.test(text)) labels.push("oral");
  if (/\bspotlight\b/.test(text)) labels.push("spotlight");
  if (/\bhighlight\b/.test(text)) labels.push("highlight");
  if (/\bnotable\b/.test(text)) labels.push("notable");
  if (/top[-\s]?5%/.test(text)) labels.push("top-5%");
  return Array.from(new Set(labels));
}

const TEXT = {
  zh: {
    profile: "研究画像",
    interest: "研究兴趣",
    interestHelp: "描述你当前关注的问题、方法、应用场景或研究意图。发现/想法/计划阶段都会基于这段信息自适应匹配。",
    researcher: "研究者画像",
    researcherHelp: "填写你的背景、已有项目、偏好的实验条件、长期研究方向等。",
    llm: "LLM 配置",
    llmHelp: "这里只配置 Find 使用的 LLM，用于题名/摘要评分、分类推断和补检索评分。Read、Idea、Plan 及后续阶段不使用这套 LLM 配置。",
    provider: "服务商",
    providerHelp: "兼容 OpenAI 协议的服务类型，例如 openai、siliconflow；mock 表示不调用远程 LLM。",
    baseUrl: "基础地址",
    baseUrlHelp: "兼容 OpenAI 协议的 API 地址，例如 https://api.openai.com/v1。",
    model: "模型",
    modelHelp: "用于评分和生成的模型名称。",
    apiKey: "API 密钥",
    apiKeyHelp: "仅保存在本地配置文件，用于调用你的 LLM 服务。",
    temperature: "温度",
    temperatureHelp: "控制生成随机性；精读和筛选建议 0.2-0.6。",
    validateLLM: "验证 LLM",
    validatingLLM: "验证中...",
    llmProbeHelp: "使用 Find 相同的 JSON 评分探针验证当前保存的 LLM 配置；不会显示 API key。",
    emailSettings: "邮件配置",
    emailHelp: "可选通知/导出配置，只影响底部产物面板的手动发送和任务完成后的自动发送；不参与科研主流程。SMTP 密码只保存在本地配置文件。",
    smtpServer: "SMTP 服务器",
    smtpPort: "SMTP 端口",
    emailSender: "发件邮箱",
    emailReceivers: "收件邮箱",
    smtpPassword: "SMTP 密码 / 授权码",
    autoEmail: "任务完成后自动发送",
    autoEmailStages: "自动发送阶段",
    sendEmail: "发送邮件",
    sendingEmail: "发送中...",
    emailSubject: "邮件主题",
    emailReceiversHelp: "多个收件人用逗号或空格分隔。手动发送时可临时覆盖配置中的收件人。",
    artifactPath: "文件位置",
    openPdf: "打开 PDF",
    openTex: "打开 TeX",
    workspaceLabel: "工作区",
    conferencePreviewPages: "稿件预览页数",
    figureQualityStatus: "图表质量审计",
    figureQualityBlocked: "阻塞图表/表格数",
    figureRepairLoop: "图表修复循环",
    previewRepairLoop: "写作修订状态",
    strictStrongOnlyNotice: "推荐文章按最终题名+摘要 LLM 评分和真实摘要筛选。",
    noRanking: "当前运行还没有推荐文章。请检查来源状态或等待 Find 完成。",
    literatureCoverage: "调研覆盖",
    literatureCoverageHelp: "调研覆盖统计属于 Find 阶段；实验页不展示文献池。",
    strongRecommendations: "推荐文章",
    studyCandidates: "调研候选",
    readCandidates: "推荐精读论文",
    evaluatedCandidates: "已抓详情评分",
    baseWorkCandidates: "代码/复现线索",
    critiqueCandidates: "边界/反例候选",
    surveyFlowExplanation: "Find 抓取、标题筛选、详情评分和推荐计数只在发现页展示。",
    sourceLimitations: "源覆盖限制",
    literatureGateNote: "审计说明",
    noStrongRecommendationButCandidates: "调研已完成并保留了未入选线索；只是当前没有足够论文进入推荐列表，这不是爬取失败。",
    diversityScore: "Diversity",
    diversityHelp: "Diversity 是 LLM 对论文覆盖当前研究方向广度的 1-10 分，并参与最终全局排序，不作为推荐硬门槛。",
    abstract: "摘要",
    scoreDetail: "评分明细",
    sourceBonus: "新颖/引用",
    qualityBonus: "质量加分",
    finalScore: "最终分",
    stableScore: "排序参考分",
    labels: "标识",
    researchLiteratureSurvey: "Find 文献调研验收",
    researchLiteratureSurveyHelp: "显示当前 Find run 各来源的抓取状态，以及候选题录进入标题筛选、标题 LLM、标题+摘要 LLM 综合评分和最终推荐的处理数量。不同来源可以从不同步骤进入。",
    venuePapersScanned: "已抓取题录",
    rawTitleIndexPapers: "题录总数",
    titleScreenInputPapers: "标题 LLM 输入",
    categoryFilteredPapers: "进入标题筛选",
    tfidfScreenedPapers: "进入标题 LLM",
    titleScoredPapers: "标题 LLM 已评分",
    abstractScoredPapers: "标题+摘要 LLM 已评分",
    titleCandidatePapers: "标题 LLM 后候选",
    recentArxivCandidates: "近半年 arXiv 候选",
    notEnabled: "未启用",
    papersRead: "已精读",
    topSurveyCandidates: "推荐文章",
    noLiteratureSurvey: "当前 Find run 尚未产出可展示的调研验收结果；Find 完成后这里会显示抓取、筛选、评分和推荐计数。",
    recommendationShortfall: "推荐不足",
    findRunBudget: "Find 运行设置",
    findBudgetHelp: "标准使用只需设置最低推荐数量和标题 LLM 预筛并发；最终标题+摘要评分独立采用每批 10 篇、默认 10 并发。高级设置用于调整抓取深度或评分成本。",
    advancedFindSettings: "高级预算",
    standardFindProfile: "标准配置",
    restoreStandardFindDefaults: "恢复标准值",
    findStandardDefaultsApplied: "已填入标准 Find 配置，保存后生效。",
    ideaRunBudget: "想法生成预算",
    ideaBudgetHelp: "这些配置只影响想法阶段；Read/Plan/环境/实验/论文不会读取这里的数量上限。",
    projectRunHistoryHelp: "只显示当前项目的历史运行；run ID 保留用于定位具体产物。",
    llmConcurrency: "标题 LLM 预筛并发数",
    llmConcurrencyHelp: "只控制 Find 标题预筛的 LLM 并发请求数，范围 1-32，默认 10。最终标题+摘要评分使用独立并发，默认同为 10。",
    repairRounds: "计划修复轮数",
    repairRoundsHelp: "Claude 生成初版后，精确执行这里设置的修复轮数；0 表示不追加修复。",
    polishRounds: "继续优化轮数",
    polishFurther: "继续优化",
    finishPlan: "选为执行计划",
    planCompleted: "已选定",
    finishPlanConfirm: "确认将此候选设为唯一执行计划？Claude Code 会重写并检查最终 plan.md。",
    nonvenueFetchLimit: "arXiv/bioRxiv 抓取上限",
    nonvenueFetchLimitHelp: "默认 5000。检索结果超过设置值时，每个来源只保留该上限内最近发表的文章。",
    recommendLimit: "最低推荐数量",
    recommendLimitHelp: "Find 的最低推荐目标；实际 N 取此值与已选渠道数 × 5 的较大者，并从有真实摘要且完成 LLM 评分的候选中按全局排名取前 N。",
    ideaLimit: "想法最大数量",
    ideaLimitHelp: "想法阶段生成的研究想法数量上限。",
    titleScanLimit: "出版渠道题录全扫保护上限",
    titleScanLimitHelp: "会议或期刊题录默认按所选出版渠道和年份全量扫描；填 0 表示不设数量上限。只有测试或异常来源保护时才填正数。",
    titleScanFraction: "题录扫描比例",
    titleScanFractionHelp: "对已抓到的出版渠道题录池抽取多少比例，1 表示全扫；只有想节省时间时才调低。",
    titleAbstractScoringLimit: "标题+摘要 LLM 综合评分上限",
    titleAbstractScoringLimitHelp: "所有完成标题 LLM 评分的候选全局去重并按标题分排序后，最多选择此数量抓取摘要/详情并进入标题+摘要 LLM 综合评分。默认 1000。",
    titleFilterTimeout: "标题筛选单批超时秒数",
    titleFilterTimeoutHelp: "LLM 标题筛选每个批次的最长等待时间。",
    abstractWorkers: "摘要评分最大并发",
    abstractWorkersHelp: "最终标题+摘要评分阶段的最大 LLM 并发，默认 10；与标题预筛并发相互独立。",
    abstractTimeout: "摘要评分单批超时秒数",
    abstractTimeoutHelp: "最终评分每个批次的最长等待时间。",
    arxivMaxQueries: "arXiv 备用查询组上限",
    arxivMaxQueriesHelp: "正常 Find 将全部关键词合成一个平级 OR 查询；此项只限制非标准备用查询路径的查询组数量。",
    arxivTimeout: "arXiv 单检索词超时秒数",
    arxivTimeoutHelp: "单个 arXiv 请求超时；超时或 429 会降级为受限状态。",
    saveConfig: "保存配置",
    saving: "保存中...",
    saved: "配置已保存",
    checkVenue: "检查可抓取性",
    checking: "检查中...",
    healthOk: "可抓取",
    healthFail: "不可抓取",
    noApprovedIdeas: "当前运行还没有通过的想法。请先在想法页点击“通过”。",
    selectAll: "全选",
    clearAll: "清空",
    rendered: "渲染",
    raw: "源码",
    stop: "停止",
    deleteRun: "删除",
    deleteRunConfirm: "确定删除这条历史运行记录？该操作会删除本地运行目录。",
    runs: "历史运行",
    showAllRuns: "显示全部历史",
    showRecentRuns: "收起历史",
    find: "发现",
    read: "精读",
    ideas: "想法",
    plan: "计划",
    environment: "环境配置",
    experiment: "实验迭代",
    fullCycle: "完整科研流程",
    paperWrite: "论文撰写",
    runFind: "运行发现",
    venues: "出版渠道（会议或期刊）",
    venueHelp: "选择一个或多个会议或期刊出版渠道。ICLR 使用官方分类；CCF/DBLP 分类由 LLM 推断并标注。",
    selectedVenuesTitle: "已选出版渠道",
    availableVenuesTitle: "可选出版渠道",
    add: "添加",
    remove: "移除",
    venueSearch: "搜索出版渠道、领域或等级",
    years: "年份",
    yearsHelp: "默认待添加年份为最新一年；修改这里不会改变已选出版渠道，点击下方渠道的添加后才会把年份加入该渠道。",
    selectedYear: "选择年份",
    addYears: "待添加年份",
    availableYears: "可用年份",
    notIndexed: "未索引",
    selected: "已选",
    shown: "显示",
    sources: "来源",
    sourcesHelp: "控制是否额外收集 arXiv、bioRxiv、Nature、Science、HuggingFace 和 GitHub 热门内容；未勾选不会进入本轮 Find。",
    arxivCategories: "arXiv 分类",
    arxivHelp: "留空表示不限制分类；也可输入多个分类作为明确限制，例如 cs.AI, cs.CV。",
    arxivDateHelp: "可选日期范围，格式 YYYY-MM-DD 或 YYYY/MM/DD；arXiv/HuggingFace/GitHub 共用。arXiv 两个日期都留空时默认抓取近半年。",
    sourceStatus: "来源状态",
    biorxivCategories: "bioRxiv 分类",
    biorxivHelp: "留空或输入 all 表示不限制分类；也可输入多个官方分类作为明确限制，例如 bioinformatics, neuroscience。",
    biorxivDateHelp: "可选日期范围，格式 YYYY-MM-DD 或 YYYY/MM/DD；留空时默认抓取最近 180 天。",
    naturePortfolio: "Nature Portfolio",
    natureHelp: "作为独立期刊流抓取重要 Nature-branded 期刊，并合并进入论文推荐。默认关闭，只有勾选后才进入本轮 Find。",
    naturePresets: "Nature 预设",
    natureJournals: "Nature 期刊范围",
    natureDateHelp: "可选日期范围；留空时使用 Nature 最新可用 feed，不继承 arXiv 日期。",
    natureCandidateLimit: "Nature 候选数量",
    natureCandidateLimitTooltip: "最多从 Nature Portfolio 收集多少篇候选文章进入评分；不是最终推荐数量。",
    natureArticleTypes: "Nature 文章类型",
    natureArticleTypesTooltip: "默认 article 表示只抓研究论文类内容，避免 News、Editorial、Comment、Career 等噪声。建议保持默认。",
    scienceFamily: "Science Family",
    scienceHelp: "作为独立期刊流抓取 AAAS Science 系列期刊，并合并进入论文推荐。默认关闭，只有勾选后才进入本轮 Find。",
    sciencePresets: "Science 预设",
    scienceJournals: "Science 期刊范围",
    sciencePartnerJournals: "Science Partner Journals",
    sciencePartnerHelp: "默认不选。这里只展示已验证 RSS 可抓取的 SPJ；Plant Phenomics 标记为 migrated，不参与抓取。",
    scienceDateHelp: "可选日期范围；留空时使用 Science 最新可用 feed，不继承 arXiv 日期。",
    scienceCandidateLimit: "Science 候选数量",
    scienceCandidateLimitTooltip: "最多从 Science 系列收集多少篇候选文章进入评分；不是最终推荐数量。",
    scienceArticleTypes: "Science 文章类型",
    scienceArticleTypesTooltip: "默认 Research Article 表示只抓研究论文类内容，避免 Books、Editorial、News 等噪声。建议保持默认。",
    candidateLimit: "候选上限",
    githubLanguages: "GitHub 语言",
    githubLanguagesHelp: "GitHub 趋势榜语言过滤，可输入 all 或 python、javascript 等。",
    startDate: "开始日期",
    endDate: "结束日期",
    runRead: "运行精读",
    runIdeas: "生成想法",
    runPlan: "生成计划",
    selectExecutionPlan: "让主控 Claude Code 选择唯一执行计划",
    approve: "通过",
    pending: "待定",
    delete: "删除",
    job: "任务",
    artifacts: "产物",
    artifactHelp: "",
    noRunArtifacts: "当前选中的运行还没有可展示的 Markdown 产物。若 Find 正在运行，这表示产物仍在生成中；已有的 JSON 产物会列在下方供审计。",
    loadingRunArtifacts: "正在加载当前运行产物...",
    idle: "空闲",
    researchProject: "项目",
    researchProjectHelp: "把 自动科研闭环接入同一个网页：项目状态、自动科研迭代、论文阶段、健康检查和工作状态记录。",
    languageChinese: "中文",
    languageEnglish: "英文",
    researchRunLoop: "运行自动科研",
    runFullResearchCycle: "运行完整科研流程",
    fullResearchCycleHelp: "从调研/idea、环境复现、实验迭代、论文生成到审计修复串起来运行；状态分散显示在对应页面，不单独作为另一套流程。",
    fullCycleAlreadyRunning: "完整科研流程正在运行",
    fullCycleAlreadyRunningHelp: "已有完整科研流程进程存活，网页已禁用重复启动；请在任务栏查看 PID、日志和阶段进度。",
    venueHardRules: "投稿会议或期刊硬要求",
    bodyPages: "正文页数",
    referencePages: "参考页数",
    totalPages: "总页数",
    keyBlockers: "关键阻塞",
    continueCycleHint: "下一次点击“运行完整科研流程”会基于这些阻塞继续修复，不会清空已有项目。",
    researchInit: "初始化/记录请求",
    researchHealth: "健康检查",
    researchStatus: "生成状态报告",
    researchHandoff: "刷新工作状态",
    researchPaper: "运行 论文阶段",
    researchRefresh: "刷新项目状态",
    researchPrompt: "自然语言需求 / 提示词",
    researchTopic: "研究主题",
    researchVenue: "投稿会议/期刊",
    researchTitle: "论文标题",
    researchIterations: "迭代轮数",
    researchOptions: "执行选项",
    researchCodingBackend: "模块 Claude Code",
    researchCodingBackendHelp: "Environment、Experimenting、Writing 分别使用各自模块的主控 Claude Code；Find 阶段保留 LLM 评分。",
    researchExecutePlan: "执行实验计划",
    researchPrepareEnv: "准备环境计划",
    researchRealBootstrapEnv: "真实创建/安装 Conda 环境",
    researchSkipPaper: "自动科研后跳过论文流水线",
    researchForceTemplate: "系统会按当前投稿会议或期刊要求生成论文预览",
    researchAutoInstallLatex: "缺 LaTeX 依赖时尝试自动安装",
    researchArtifacts: "阶段摘要",
    researchNoProject: "未找到 项目。",
    researchProjectLoading: "正在加载 项目...",
    artifactAdvancedDetails: "高级产物详情",
    artifactLocalPathNote: "本地文件路径用于审计当前 run 的真实产物。",
    noData: "暂无",
    unnamed: "未命名",
    runtimeSaved: "运行环境已保存并重新诊断。",
    runtimeDetected: "已自动检测并保存 Claude Code 和 Node 路径。",
    envConfigSaved: "实验环境配置已保存；Conda/Python 仅在环境配置步骤使用。",
    runtimeLockedReady: "环境已锁定",
    runtimeLockedReadyDetail: "使用远端已锁定配置；无需重复创建或重新诊断。",
    researchRunningTask: "正在执行",
    researchResearchTopic: "研究主题",
    currentProject: "当前 项目",
    createProject: "创建 项目",
    researchProjectId: "项目 ID",
    researchProjectIdPlaceholder: "例如 my_project_id",
    researchCreateProjectHelp: "只创建项目目录和稳定身份，不会自动启动 Find、实验或论文流水线；投稿目标、研究兴趣、研究者画像和自然语言请求在项目内可随运行继续调整。",
    researchProjectCreated: "项目已创建并切换。",
    researchGlobalHelp: "这里仅放全局研究主题；仓库、数据、环境状态和阻塞原因会在调研/计划之后进入“环境配置”阶段展示。",
    researchRuntimeTitle: "运行环境",
    researchRuntimeHelp: "这里仅配置 Claude Code、Node 和额外 PATH。Conda/Python 实验环境只在“环境配置”步骤设置；系统会用这些显式路径同时覆盖交互式与非交互式执行。",
    remoteToolPaths: "远端工具路径",
    managementPythonExecutable: "管理 Python",
    experimentPythonExecutable: "实验 Python",
    nodeBinDir: "Node 可执行目录",
    claudeExecutable: "Claude Code 可执行文件",
    extraPath: "额外路径",
    autoDetectClaude: "自动检测 Claude Code",
    saveAndDiagnose: "保存并诊断",
    missing: "缺失",
    noDiagnostics: "暂无诊断",
    claudeWaiting: "模块 Claude 已收到指令，正在等待输出...",
    claudeSessionTitle: "模块 Claude Code 对话",
    claudeSessionHelp: "这里是少量人类监督入口；Environment、Experimenting、Writing 分别发送给各自模块 Claude Code。",
    notCreated: "尚未创建",
    claudeDone: "最近一次模块指令已完成",
    claudeFailed: "最近一次模块指令失败",
    claudeWorking: "模块 Claude 正在处理这条指令",
    events: "条事件",
    claudeSentWaiting: "已提交给对应模块 Claude Code。",
    claudeEnvPlaceholder: "例如：请自主检查当前仓库和数据门控，说明是否能进入真实实验，不要使用我的分析结论作为证据。",
    claudeExperimentPlaceholder: "例如：请基于当前计划和真实加载器就绪数据，检查下一步实验应如何实现，必须自己读取代码和证据。",
    claudePaperPlaceholder: "例如：请继续按目标 venue 格式自主修订论文，检查引用、图表和证据门控，不合格就继续迭代，不要手写或虚构实验结论。",
    queueAgentGuidance: "发送给模块 Claude Code",
    interruptEnvironmentClaude: "打断当前任务并优先发送",
    agentGuidanceQueued: "指令已提交给对应模块 Claude Code；需要排队时由该模块自己的检查点读取。",
    queuedGuidance: "等待模块 Claude Code",
    claudeTranscriptTitle: "最近一次模块主控处理摘要",
    noClaudeTranscript: "还没有模块主控处理摘要；真实运行日志请看底部任务栏中的当前 job。",
    arxivTopicQueries: "arXiv/bioRxiv 手工检索词",
    arxivTopicQueriesHelp: "可留空。每项必须是 1-3 个英文单词，用逗号分隔；填写后与 LLM 抽取词平级合并，并同时用于 arXiv 和 bioRxiv。",
    arxivTopicQueriesPlaceholder: "留空则按当前研究主题自动生成",
    retrievalPool: "未入选检索线索",
    retrievalPoolHelp: "未入选线索只用于排查推荐质量和补充检索，不在主列表展示；人类监督只看覆盖统计、推荐文章和精读论文。",
    noRetrievalCandidates: "当前运行还没有检索候选。",
    environmentHelp: "根据 Find/Plan结果选择最适合跟进的仓库，检查数据可用性，并按当前机器自动准备 Conda 实验环境。",
    envLockedCreated: "环境已创建并锁定",
    firstCreateEnv: "首次创建环境",
    currentEnvSummary: "环境配置摘要",
    notRunEnvironment: "尚未运行环境配置",
    activeRepoLabel: "当前仓库",
    repoPathLabel: "仓库路径",
    condaEnvLabel: "Conda 环境",
    envLockLabel: "环境锁",
    envLockNotLocked: "尚未锁定；只允许首次创建",
    claudeRepoJudgment: "Claude 仓库判断",
    notCompleted: "尚未完成",
    confidence: "置信度",
    selectionReason: "选择理由",
    currentBlockReason: "当前阻塞原因",
    nonMainDataGap: "非主路线数据缺口",
    oneShotEnvRule: "一次性环境规则",
    oneShotEnvLocked: "该环境已创建并锁定；工作流和网页都不会再重复安装、修改或创建新环境。",
    oneShotEnvUnlocked: "只有首次创建时会执行 Conda/bootstrap；创建成功后自动锁定。",
    experimentCondaPythonConfig: "实验 Conda/Python 配置",
    experimentCondaPythonHelp: "这些项只属于环境配置阶段：实验进程使用 Conda 环境名称派生出的 Python；也可以显式填写实验 Python，避免训练命令误用 管理环境。",
    condaEnvName: "Conda 环境名称",
    condaBase: "Conda 基础目录",
    pythonExecutable: "管理 Python",
    saveExperimentEnv: "保存实验环境配置",
    firstEnvCreateControl: "首次环境创建控制",
    firstEnvCreateHelp: "环境配置只负责第一次仓库/数据/Conda bootstrap。创建成功后按钮会自动锁定，后续实验只能复用既有环境。",
    researchPromptPlaceholder: "新的自然语言科研需求，可用于初始化/记录",
    envLockedNoRepeat: "环境已锁定，不再允许从网页重复创建/安装",
    realBootstrapConda: "真实创建/验证 Conda 环境",
    envAssetsBlockDetails: "环境资产与阻塞详情",
    envAssetsBlockHelp: "点击每个卡片查看具体仓库、数据集、可用性和阻塞原因；这里不展示冗余项目产物，只展示能辅助判断流程是否健康的信息。",
    claudeRepoDecision: "Claude 仓库决策",
    notSelected: "尚未选择",
    claudeNoStructuredDecision: "Environment 主控 Claude 尚未给出结构化仓库/主题适配判断；系统不会把当前仓库当作最终路线。",
    requiredModification: "需要改造",
    riskGap: "风险/缺口",
    evidence: "证据",
    repoAction: "仓库动作",
    repoActionReason: "仓库动作理由",
    envAction: "环境动作",
    envActionReason: "环境动作理由",
    dataAction: "数据动作",
    dataActionReason: "数据动作理由",
    recommendedEnv: "推荐环境",
    stewardshipMemory: "后续记忆",
    datasetLabel: "数据集",
    repoPathShort: "仓库路径",
    gateLabel: "门控",
    sessionLabel: "会话",
    repoLabel: "仓库",
    modeLabel: "模式",
    autonomyLabel: "自主模式",
    lastLabel: "最近状态",
    defaultOption: "默认",
    statusNotStarted: "尚未开始",
    statusClaimReady: "证据可用",
    statusNotClaimReady: "证据未就绪",
    statusAuto: "自动",
    candidateRepos: "候选仓库",
    noRepoCandidates: "暂无仓库候选。",
    noRepoAudit: "暂无仓库审计说明",
    nextAction: "下一步",
    datasets: "数据集",
    noDatasetRegistry: "暂无数据集登记。",
    noExtraInfo: "无额外说明",
    missingFiles: "缺少文件",
    usableData: "可用数据",
    noClaimReadyData: "暂无可用于结论的数据；实验结果不能包装成正式论文证据。",
    threshold: "门槛",
    claimReadyThreshold: "只有真实数据同时满足 claim_ready=true 且 loader_probe_success=true，才会进入这里。",
    pendingEvidence: "待补证据",
    noPendingEvidence: "暂无待补证据数据。",
    pendingEvidenceFallback: "有线索，但还没有达到可用于真实实验的证据门槛。",
    blockedData: "阻塞数据",
    noBlockedData: "暂无阻塞数据。",
    missingBlockReason: "缺少阻塞原因，请检查数据集登记表",
    envStatus: "环境状态",
    notLockedFirstOnly: "尚未锁定；只允许首次创建。",
    experimentHelp: "监督实验主线：先复现参考工作，再循环 idea、改代码、跑实验、读日志/loss、坏例分析和下一步计划。",
    runExperimentLoop: "只跑实验子循环",
    runSettings: "运行设置",
    maxExperimentsPerRound: "每轮最多实验数",
    currentDefaultBackend: "当前执行后端",
    projectDefault: "项目默认",
    lastActualBackend: "最近一次执行后端",
    currentExperimentSummary: "当前实验摘要",
    noExperimentRun: "尚未运行实验",
    completedExperiments: "审计就绪记录",
    caution: "注意",
    syntheticSmokeWarning: "合成数据冒烟测试只能证明流程跑通，不能支撑论文结论。",
    experimentGateOverview: "实验与复现门控",
    experimentGateHelp: "这里先看主线能不能继续：参考工作是否按论文协议复现、算力是否可支撑、候选方法是否超过基线、实验循环是否完整。",
    referenceReproductionGate: "参考工作复现",
    computeFeasibility: "算力可行性",
    scientificProgressGate: "科学进展",
    iterationTrajectoryAudit: "实验循环",
    paperTarget: "论文目标",
    localReproduction: "本地复现",
    computeBudget: "计算预算",
    currentBestCandidate: "当前最佳候选",
    currentBaseline: "当前基线",
    loopCompleteness: "循环完整性",
    mustRepairBeforeNovel: "参考论文级复现未过门时，流程必须先修复复现协议/数据/评测或换基底，不能继续把新方法或论文写作当主线。",
    researchTrajectorySystem: "研究轨迹监督",
    researchTrajectoryHelp: "只显示科研流程是否被证据阻塞、下一步该做什么、记忆是否正常。",
    trajectoryPhase: "轨迹阶段",
    assuranceStatus: "证据保障",
    landscapeNodes: "研究版图节点",
    noveltyNodes: "新颖性节点",
    failedHypotheses: "失败假设",
    unexploredNiches: "未探索 niche",
    memoryEntries: "落盘记忆",
    nextObjectives: "下一步目标",
    agentRoles: "Agent 角色",
    assuranceIssues: "证据问题",
    trajectoryFiles: "轨迹文件",
    evoPhases: "可恢复周期阶段",
    recoverableExceptions: "可恢复异常",
    localSkills: "本地 skills",
    thirdPartyResearchStack: "内置方法契约",
    thirdPartySources: "方法来源",
    thirdPartyModules: "能力模块",
    thirdPartySkills: "方法适配器",
    thirdPartyStackHelp: "外部研究方法的来源与 commit 仅用于高级审计；实际流程统一表现为研究方向管理、进化记忆、证据保障、轨迹优化和论文生产，不再暴露为单独外部 agent。",
    sourceCommit: "commit",
    sourceLicense: "license",
    directionMemoryEntries: "方向记忆",
    evidenceIntegrity: "证据完整性",
    evidenceIntegrityIssues: "完整性问题",
    optimizationQueue: "优化队列",
    trajectoryCheckpoints: "轨迹检查点",
    trajectoryDelta: "轨迹变化",
    evolutionaryIndex: "进化索引",
    graphHistoryEntries: "图谱历史",
    evolutionaryLedgerEntries: "进化 ledger",
    evidenceManifestRefs: "证据引用",
    weakUnsupportedClaims: "弱/无支撑声明",
    longHorizonAssets: "长期轨迹资产",
    landscapeAssessment: "研究版图评估",
    trajectoryQueue: "轨迹优化队列",
    trajectorySupervisor: "轨迹主控",
    supervisorSummary: "监督摘要",
    methodContracts: "内置方法契约",
    advancedAudit: "高级审计",
    memoryHealth: "记忆健康",
    latestAutonomousRun: "最近自主运行",
    mainBlockers: "主要阻塞",
    trajectoryProtocol: "执行协议",
    capabilityAudit: "能力审计",
    capabilityStatus: "能力状态",
    capabilityModules: "能力模块",
    capabilityChecks: "检查项",
    endToEndVerification: "端到端验证",
    research_trajectory_end_to_end_verification: "研究轨迹端到端验证",
    verificationStatus: "验证状态",
    totalChecks: "检查总数",
    failedChecks: "失败检查",
    warningChecks: "警告检查",
    supervisorRounds: "主控轮次",
    supervisorLatest: "最近主控状态",
    noTrajectoryQueue: "暂无轨迹优化队列。",
    updatedAt: "更新时间",
    noTrajectorySystem: "尚未生成研究轨迹系统；下一次环境/实验迭代会自动刷新。",
    noNextObjectives: "暂无下一步目标。",
    noAssuranceIssues: "暂无证据问题。",
    ideationMemory: "想法记忆",
    experimentationMemory: "实验记忆",
    assuranceMemory: "保障记忆",
    trajectoryMemory: "轨迹记忆",
    experimentRecordTable: "实验迭代记录",
    experimentRecordHelp: "当前路线的实验记录；历史记录只保留在 CSV 审计文件中。",
    experimentRecordUpdated: "记录更新时间",
    downloadCsv: "下载 CSV",
    experimentGoal: "实验目的",
    variant: "方法/变体",
    repo: "仓库",
    dataset: "数据集",
    env: "运行环境",
    commandConfig: "关键配置/命令",
    badCases: "坏例/切片",
    reflection: "结论/反思",
    evidencePath: "证据路径",
    resultDetail: "运行结果",
    noCurve: "暂无曲线",
    noExperimentRecords: "还没有实验记录。",
    paperHelp: "论文撰写只在参考复现、实验和投稿证据门控满足后启动；当前页展示真实门控和已有预览状态。",
    runPaperWriting: "生成与修订论文",
    paperSettingsAndGate: "论文设置与门控",
    currentGate: "当前门控",
    unknown: "未知",
    paperStatus: "论文状态",
    template: "模板",
    fetched: "已获取",
    notFetched: "未获取/不需要",
    pdfPreviewBlocked: "已有论文预览 PDF；证据门控未通过，不能视为最终投稿稿",
    pdfReadyBelow: "已生成并可在下方预览",
    pdfNotGenerated: "尚未生成当前论文预览",
    blockedPdfPreviewTitle: "论文预览 PDF",
    runningPdfPreviewTitle: "生成中 PDF 预览",
    runningPdfPreviewHelp: "论文阶段正在重新生成当前论文预览；这里显示当前可检查的 PDF/TeX 产物，可能会在编译和审计后更新，不能视为最终投稿稿。",
    blockedPdfPreviewHelp: "这份 PDF 是当前 TASTE 生成的论文预览，可用于查看论文结构、排版和内容；质量/证据门控仍按真实状态显示，不能视为最终投稿稿。",
    paperOrchestraStatus: "论文阶段状态",
    paperNormalityStatus: "正常论文形态审计",
    venueTemplateStatus: "目标模板格式",
    paperQualityGates: "论文质量门控",
    paperCitationRenderStatus: "引用渲染审计",
    paperCitationRenderBlockers: "引用渲染阻塞",
    paperSelfReviewStatus: "论文自审",
    paperSelfReviewBlockers: "论文自审待处理项",
    paperGateSeparator: "；",
    paperAdvancedDetails: "论文阶段高级详情",
    normalPreviewReady: "论文预览状态",
    rawPaperOrchestraOutput: "论文写作原始产物",
    hiddenPdfReason: "当前 PDF 是论文预览；质量、证据或投稿门控未通过时，不能视为最终投稿稿。",
    runningPdfReason: "论文刷新正在运行；下方 PDF/TeX 是当前可检查产物，系统仍会继续编译、审计和替换它。",
    skippedPdfReason: "证据门控未通过；论文撰写保持等待，直到参考复现、实验和投稿证据刷新通过。",
    blockedPdfReason: "当前 PDF 是论文预览稿；正常论文形态、模板、图表或投稿证据门控仍需继续迭代。",
    unrestrictedLimit: "不限/未限制",
    evidenceGateNotPassed: "证据门控未通过",
    evidenceGateWarning: "下方 PDF 仅用于查看排版和草稿结构；当前评审、结论和证据仍要求继续实验或修订，不能作为最终投稿版本。",
    pdfPreviewTitle: "PDF 论文预览",
    noPdf: "还没有可展示的 PDF。论文撰写会在参考复现、实验和投稿证据门控通过后生成当前 venue 预览。",
    time: "时间",
    method: "方法",
    status: "状态",
    metric: "指标",
    metrics: "指标",
    value: "数值",
    audit: "审计",
    curve: "曲线",
    ready: "就绪",
    accepted: "已接受",
    searching: "搜索中",
    executionReady: "可执行",
    needsCheck: "待检查",
    active: "当前",
    loaderOnly: "仅加载器通过",
    probeOnly: "仅探测通过",
    blocked: "阻塞",
  },
  en: {
    profile: "Profile",
    interest: "Research Interest",
    interestHelp: "Describe your current problems, methods, domains, or research intent. Find/Idea/Plan adapt matching from this text.",
    researcher: "Researcher Profile",
    researcherHelp: "Add your background, existing projects, preferred experimental constraints, and long-term directions.",
    llm: "LLM Settings",
    llmHelp: "This config is used only by Find for title/abstract scoring, inferred categories, and repair scoring. Read, Idea, Plan, and later stages do not use it.",
    provider: "Provider",
    providerHelp: "OpenAI-compatible service type, such as openai or siliconflow; mock disables remote LLM calls.",
    baseUrl: "Base URL",
    baseUrlHelp: "OpenAI-compatible API endpoint, e.g. https://api.openai.com/v1.",
    model: "Model",
    modelHelp: "Model name used for scoring and generation.",
    apiKey: "API Key",
    apiKeyHelp: "Stored only in the local config file for your LLM service.",
    temperature: "Temperature",
    temperatureHelp: "Controls generation randomness; 0.2-0.6 is usually better for reading and filtering.",
    validateLLM: "Validate LLM",
    validatingLLM: "Validating...",
    llmProbeHelp: "Uses the same JSON scoring probe as Find against the saved LLM config; API keys are never shown.",
    emailSettings: "Email Settings",
    emailHelp: "Optional notification/export settings for the bottom artifact panel and completion emails. They do not participate in the research workflow. The SMTP password is stored only in the local config file.",
    smtpServer: "SMTP Server",
    smtpPort: "SMTP Port",
    emailSender: "Sender email",
    emailReceivers: "Receiver emails",
    smtpPassword: "SMTP password / app password",
    autoEmail: "Auto-send after jobs complete",
    autoEmailStages: "Auto-send stages",
    sendEmail: "Send Email",
    sendingEmail: "Sending...",
    emailSubject: "Email subject",
    emailReceiversHelp: "Separate multiple recipients with commas or spaces. Manual send can override config recipients.",
    artifactPath: "File location",
    openPdf: "Open PDF",
    openTex: "Open TeX",
    workspaceLabel: "Workspace",
    conferencePreviewPages: "Manuscript preview pages",
    figureQualityStatus: "Figure quality audit",
    figureQualityBlocked: "Blocked figures/tables",
    figureRepairLoop: "Figure repair loop",
    previewRepairLoop: "Writing revision status",
    strictStrongOnlyNotice: "Recommended papers are selected by topic fit, abstract evidence, and source quality.",
    noRanking: "No paper is recommended yet. Check the source status or wait for Find to finish.",
    literatureCoverage: "Literature coverage",
    literatureCoverageHelp: "Detailed survey coverage statistics are kept in internal audit files and are not repeated on the main page.",
    strongRecommendations: "Recommended papers",
    studyCandidates: "Survey candidates",
    readCandidates: "Reading/boundary audit candidates",
    evaluatedCandidates: "Detail-scored",
    baseWorkCandidates: "Code/reproduction leads",
    critiqueCandidates: "Boundary/counterexample candidates",
    surveyFlowExplanation: "Find retrieval, title screening, detail scoring, and recommendation counts are shown only on the Find page.",
    sourceLimitations: "Source limitations",
    literatureGateNote: "Audit note",
    noStrongRecommendationButCandidates: "Survey succeeded and retained candidate papers; no paper is recommended yet, so this is not a crawl failure.",
    diversityScore: "Diversity",
    diversityHelp: "Diversity measures coverage across the configured research directions and contributes to the final global ranking; it is not a hard recommendation gate.",
    abstract: "Abstract",
    scoreDetail: "Score detail",
    sourceBonus: "Novelty/citation",
    qualityBonus: "Quality bonus",
    finalScore: "Final score",
    stableScore: "Stable source score",
    labels: "Labels",
    researchLiteratureSurvey: "Find Survey Gate",
    researchLiteratureSurveyHelp: "Shows retrieval status for every source and candidate counts entering title screening, title LLM, title+abstract LLM scoring, and final recommendations. Sources may enter at different steps.",
    venuePapersScanned: "Records retrieved",
    rawTitleIndexPapers: "Record total",
    titleScreenInputPapers: "Title-LLM input",
    categoryFilteredPapers: "Entering title screening",
    tfidfScreenedPapers: "Entering title LLM",
    titleScoredPapers: "Title-LLM scored",
    abstractScoredPapers: "Title+abstract LLM scored",
    titleCandidatePapers: "Candidates after title LLM",
    recentArxivCandidates: "Recent arXiv candidates",
    notEnabled: "not enabled",
    papersRead: "Papers read",
    topSurveyCandidates: "Recommended papers",
    noLiteratureSurvey: "No current Find audit result is visible yet. Once Find completes, this panel shows retrieval, screening, scoring, and recommended papers.",
    recommendationShortfall: "recommendation shortfall",
    findRunBudget: "Find Settings",
    findBudgetHelp: "Standard use only needs the minimum recommendation count and title-LLM prefilter concurrency. Final title+abstract scoring independently uses batches of 10 with 10 workers by default. Use advanced settings to adjust retrieval depth or scoring cost.",
    advancedFindSettings: "Advanced budgets",
    standardFindProfile: "standard profile",
    restoreStandardFindDefaults: "Restore standards",
    findStandardDefaultsApplied: "Standard Find settings filled in; save to apply.",
    ideaRunBudget: "Idea Budget",
    ideaBudgetHelp: "These settings only affect Idea generation; Read/Plan/Environment/Experiment/Paper do not read these limits.",
    projectRunHistoryHelp: "Only runs for the current project are shown; run ID is kept for artifact lookup.",
    llmConcurrency: "Title-LLM prefilter concurrency",
    llmConcurrencyHelp: "Controls only concurrent LLM requests for Find title prefiltering. Range: 1-32; default: 10. Final title+abstract scoring has separate concurrency, also defaulting to 10.",
    repairRounds: "Plan repair rounds",
    repairRoundsHelp: "After Claude writes the initial draft, run exactly this many repair rounds; 0 adds no repair round.",
    polishRounds: "Polish rounds",
    polishFurther: "Polish further",
    finishPlan: "Select for execution",
    planCompleted: "Selected",
    finishPlanConfirm: "Select this candidate as the sole execution plan? Claude Code will rewrite and validate the final plan.md.",
    nonvenueFetchLimit: "arXiv/bioRxiv fetch cap",
    nonvenueFetchLimitHelp: "Defaults to 5000. When a source has more matches, it retains the configured number of most recently published papers.",
    recommendLimit: "Minimum recommendation count",
    recommendLimitHelp: "Find targets at least the larger of this value and 5 × selected sources, then takes the global top N among candidates with real abstracts and completed LLM scoring.",
    ideaLimit: "Max ideas",
    ideaLimitHelp: "Maximum research ideas generated in the Idea stage.",
    titleScanLimit: "Publication-record full-scan safety cap",
    titleScanLimitHelp: "Conference or journal records are fully scanned by publication venue and year by default. 0 means no configured cap; use a positive value only for tests or abnormal-source protection.",
    titleScanFraction: "Record scan fraction",
    titleScanFractionHelp: "Fraction of the collected publication-record pool to scan. 1 means all; lower it only to reduce runtime.",
    titleAbstractScoringLimit: "Title+abstract LLM scoring cap",
    titleAbstractScoringLimitHelp: "After all title-LLM-scored candidates are globally deduplicated and ranked by title score, at most this many fetch abstracts/details and enter combined title+abstract LLM scoring. Default: 1000.",
    titleFilterTimeout: "Title filter timeout (sec)",
    titleFilterTimeoutHelp: "Maximum wait per LLM title-filter batch.",
    abstractWorkers: "Abstract scoring max workers",
    abstractWorkersHelp: "Maximum LLM concurrency for final title+abstract scoring. Default: 10; independent of title-prefilter concurrency.",
    abstractTimeout: "Abstract scoring timeout (sec)",
    abstractTimeoutHelp: "Maximum wait per final-scoring batch.",
    arxivMaxQueries: "arXiv fallback query cap",
    arxivMaxQueriesHelp: "Normal Find combines all keywords into one equal-status OR query. This only caps query groups on fallback paths.",
    arxivTimeout: "arXiv timeout (sec)",
    arxivTimeoutHelp: "Timeout per arXiv request; timeout/429 degrades to limited status.",
    saveConfig: "Save Config",
    saving: "Saving...",
    saved: "Config saved",
    checkVenue: "Check fetchability",
    checking: "Checking...",
    healthOk: "fetchable",
    healthFail: "not fetchable",
    noApprovedIdeas: "No approved ideas in this run. Approve ideas on the Ideas tab first.",
    selectAll: "Select All",
    clearAll: "Clear",
    rendered: "Rendered",
    raw: "Raw",
    stop: "Stop",
    deleteRun: "Delete",
    deleteRunConfirm: "Delete this run history? This removes the local run directory.",
    runs: "Runs",
    showAllRuns: "Show all history",
    showRecentRuns: "Collapse history",
    find: "Find",
    read: "Read",
    ideas: "Ideas",
    plan: "Plan",
    environment: "Environment",
    experiment: "Experiment Loop",
    fullCycle: "Full Research Workflow",
    paperWrite: "Paper Writing",
    runFind: "Run Find",
    venues: "Venues",
    venueHelp: "Select one or more conferences/journals. ICLR uses official categories; CCF/DBLP categories are LLM-inferred and labeled.",
    selectedVenuesTitle: "Selected Venues",
    availableVenuesTitle: "Available Venues",
    add: "Add",
    remove: "Remove",
    venueSearch: "Search venue, journal, field, or rank",
    years: "Years",
    yearsHelp: "The default pending year is the latest year; editing this field does not change selected venues until you click Add on a venue below.",
    selectedYear: "Selected year",
    addYears: "Pending years",
    availableYears: "Available years",
    notIndexed: "not indexed",
    selected: "selected",
    shown: "shown",
    sources: "Sources",
    sourcesHelp: "Choose whether to also collect arXiv, bioRxiv, Nature, Science, HuggingFace, and GitHub signals. Disabled sources are not used in this Find run.",
    arxivCategories: "arXiv categories",
    arxivHelp: "Leave blank to search without a category constraint, or enter explicit categories separated by commas or spaces, e.g. cs.AI, cs.CV.",
    arxivDateHelp: "Optional date range in YYYY-MM-DD or YYYY/MM/DD; shared by arXiv/HuggingFace/GitHub. For arXiv, leaving both empty defaults to the most recent 180 days.",
    sourceStatus: "Source Status",
    biorxivCategories: "bioRxiv categories",
    biorxivHelp: "Leave blank or use all to search without a category constraint, or enter official categories such as bioinformatics, neuroscience.",
    biorxivDateHelp: "Optional date range in YYYY-MM-DD or YYYY/MM/DD. Leave blank to fetch the latest 180 days.",
    naturePortfolio: "Nature Portfolio",
    natureHelp: "Fetch important Nature-branded journals through a separate journal stream and merge them into paper recommendations. Disabled by default; only checked sources enter this Find run.",
    naturePresets: "Nature presets",
    natureJournals: "Nature journal range",
    natureDateHelp: "Optional date range; when blank, Nature uses the latest available feed and does not inherit arXiv dates.",
    natureCandidateLimit: "Nature candidate limit",
    natureCandidateLimitTooltip: "Maximum Nature Portfolio candidates collected for scoring; this is not the final recommendation count.",
    natureArticleTypes: "Nature article types",
    natureArticleTypesTooltip: "Default article means research-article content only, avoiding noisier News, Editorial, Comment, and Career items. Keep the default unless you need broader content.",
    scienceFamily: "Science Family",
    scienceHelp: "Fetch AAAS Science-family journals through a separate journal stream and merge them into paper recommendations. Disabled by default; only checked sources enter this Find run.",
    sciencePresets: "Science presets",
    scienceJournals: "Science journal range",
    sciencePartnerJournals: "Science Partner Journals (Advanced)",
    sciencePartnerHelp: "Disabled by default. Only SPJs with verified RSS are selectable; Plant Phenomics is marked migrated and is not fetched.",
    scienceDateHelp: "Optional date range; when blank, Science uses the latest available feed and does not inherit arXiv dates.",
    scienceCandidateLimit: "Science candidate limit",
    scienceCandidateLimitTooltip: "Maximum Science-family candidates collected for scoring; this is not the final recommendation count.",
    scienceArticleTypes: "Science article types",
    scienceArticleTypesTooltip: "Default Research Article means research-article content only, avoiding noisier Books, Editorial, and News items. Keep the default unless you need broader content.",
    candidateLimit: "candidate limit",
    githubLanguages: "GitHub languages",
    githubLanguagesHelp: "GitHub Trending language filter, such as all, python, javascript.",
    startDate: "start date",
    endDate: "end date",
    runRead: "Run Read",
    runIdeas: "Generate Ideas",
    runPlan: "Generate Plan",
    selectExecutionPlan: "Ask main Claude Code to choose one execution plan",
    approve: "Approve",
    pending: "Pending",
    delete: "Delete",
    job: "Job",
    artifacts: "Artifacts",
    artifactHelp: "",
    noRunArtifacts: "The selected run has no readable Markdown artifacts yet. If Find is running, artifacts are still being generated; available JSON artifacts are listed below for audit.",
    loadingRunArtifacts: "Loading artifacts for the selected run...",
    idle: "idle",
    researchProject: "Research Project",
    researchProjectHelp: "Use the broader TASTE autonomous-research workflow from the same web UI: project status, autonomous iterations, paper stage, health checks, and the work-status log.",
    languageChinese: "Chinese",
    languageEnglish: "English",
    researchRunLoop: "Run Autonomous Research",
    runFullResearchCycle: "Run Full Research Workflow",
    fullResearchCycleHelp: "Runs research/idea, environment reproduction, experiment iteration, paper production, and audit repair as one route. Status is shown in the relevant pages, not as a separate pipeline.",
    fullCycleAlreadyRunning: "Full research workflow is running",
    fullCycleAlreadyRunningHelp: "A full research workflow process is already alive, so duplicate launch is disabled. Check PID, logs, and phase progress in the taskbar.",
    venueHardRules: "Venue hard requirements",
    bodyPages: "Body pages",
    referencePages: "Reference pages",
    totalPages: "Total pages",
    keyBlockers: "Key blockers",
    continueCycleHint: "Clicking Run Full Research Workflow again continues from these blockers instead of clearing the project.",
    researchInit: "Initialize / Log Request",
    researchHealth: "Health Check",
    researchStatus: "Generate Status",
    researchHandoff: "Refresh Work Status",
    researchPaper: "Run Paper Stage",
    researchRefresh: "Refresh Project",
    researchPrompt: "Natural-language request / prompt",
    researchTopic: "Research topic",
    researchVenue: "Target venue/journal",
    researchTitle: "Paper title",
    researchIterations: "Iterations",
    researchOptions: "Execution options",
    researchCodingBackend: "Module Claude Code",
    researchCodingBackendHelp: "Environment, Experimenting, and Writing each use their own module controller Claude Code; Find keeps LLM scoring.",
    researchExecutePlan: "Execute experiment plan",
    researchPrepareEnv: "Prepare env plan",
    researchRealBootstrapEnv: "Create/install conda env for real",
    researchSkipPaper: "Skip paper pipeline after autonomous run",
    researchForceTemplate: "The workflow generates the current venue paper preview",
    researchAutoInstallLatex: "Try auto-installing missing LaTeX deps",
    researchArtifacts: "Stage Summary",
    researchNoProject: "No research project found.",
    researchProjectLoading: "Loading research projects...",
    artifactAdvancedDetails: "Advanced artifact details",
    artifactLocalPathNote: "The local path is for auditing the current run artifact.",
    noData: "N/A",
    unnamed: "Unnamed",
    runtimeSaved: "runtime saved and diagnosed again.",
    runtimeDetected: "Claude Code and Node paths were auto-detected and saved.",
    envConfigSaved: "Experiment environment config saved; Conda/Python are configured only in the Environment step.",
    runtimeLockedReady: "Environment locked",
    runtimeLockedReadyDetail: "Using the locked remote configuration; no repeated creation or diagnosis is needed.",
    researchRunningTask: "running",
    researchResearchTopic: "TASTE Research Topic",
    currentProject: "Current Research Project",
    createProject: "Create Research Project",
    researchProjectId: "Project ID",
    researchProjectIdPlaceholder: "e.g. my_project_id",
    researchCreateProjectHelp: "Creates only the project directory and stable identity. It does not start Find, experiments, or paper generation; venue, interests, profile, and requests remain editable project-run preferences.",
    researchProjectCreated: "research project created and selected.",
    researchGlobalHelp: "This panel only stores the global research topic. Repo, data, environment status, and blockers appear in the Environment stage after research/planning.",
    researchRuntimeTitle: "Runtime",
    researchRuntimeHelp: "Configure only Claude Code, Node, and extra PATH here. Conda/Python experiment environments are configured only in the Environment step; the workflow uses these explicit paths for both interactive and non-interactive execution.",
    remoteToolPaths: "Remote Tool Paths",
    managementPythonExecutable: "management Python",
    experimentPythonExecutable: "Experiment Python",
    nodeBinDir: "Node bin directory",
    claudeExecutable: "Claude Code executable",
    extraPath: "Extra PATH",
    autoDetectClaude: "Auto-detect Claude Code",
    saveAndDiagnose: "Save and diagnose",
    missing: "missing",
    noDiagnostics: "No diagnostics yet",
    claudeWaiting: "The module Claude received the instruction and is waiting for output...",
    claudeSessionTitle: "Module Claude Code Chat",
    claudeSessionHelp: "Use this area only for limited human supervision; Environment, Experimenting, and Writing route to separate module Claude Code sessions.",
    notCreated: "Not created yet",
    claudeDone: "Latest module instruction completed",
    claudeFailed: "Latest module instruction failed",
    claudeWorking: "The module Claude is processing this instruction",
    events: "events",
    claudeSentWaiting: "Submitted to the matching module Claude Code.",
    claudeEnvPlaceholder: "Example: autonomously inspect the current repo and data gates, explain whether real experiments can start, and do not use my analysis as evidence.",
    claudeExperimentPlaceholder: "Example: based on the current plan and real loader-ready data, inspect how the next experiment should be implemented; read the code and evidence yourself.",
    claudePaperPlaceholder: "Example: keep revising the paper in the target venue format, checking citations, figures, and evidence gates; if it fails, keep iterating without hand-writing or inventing claims.",
    queueAgentGuidance: "Send to module Claude Code",
    interruptEnvironmentClaude: "Interrupt current task and send first",
    agentGuidanceQueued: "Guidance was submitted to the matching module Claude Code; module-owned checkpoints read it when queuing is needed.",
    queuedGuidance: "Waiting for module Claude Code",
    claudeTranscriptTitle: "Latest module-controller summary",
    noClaudeTranscript: "No module-controller summary yet; real run logs are shown in the bottom taskbar job entries.",
    arxivTopicQueries: "arXiv/bioRxiv manual keywords",
    arxivTopicQueriesHelp: "Optional. Each comma-separated item must contain 1-3 English words; manual items merge as equal-status keywords for both arXiv and bioRxiv.",
    arxivTopicQueriesPlaceholder: "leave empty to auto-generate from this research topic",
    retrievalPool: "Retrieval audit pool",
    retrievalPoolHelp: "Audit-only retrieval traces. The main UI shows coverage, recommendations, and deep-reading papers instead.",
    noRetrievalCandidates: "No retrieval candidates for this run yet.",
    environmentHelp: "Select the best repo to follow from Find/Plan results, check data availability, and prepare the conda experiment environment for this machine.",
    envLockedCreated: "Environment created and locked",
    firstCreateEnv: "Create environment once",
    currentEnvSummary: "Environment Summary",
    notRunEnvironment: "Environment has not run yet",
    activeRepoLabel: "Active repo",
    repoPathLabel: "Repo path",
    condaEnvLabel: "Conda env",
    envLockLabel: "Environment lock",
    envLockNotLocked: "Not locked yet; only first-time creation is allowed",
    claudeRepoJudgment: "Claude repo judgment",
    notCompleted: "Not completed",
    confidence: "confidence",
    selectionReason: "Selection rationale",
    currentBlockReason: "Current blocker",
    nonMainDataGap: "Non-main-route data gaps",
    oneShotEnvRule: "One-shot environment rule",
    oneShotEnvLocked: "This environment is created and locked; The workflow and web UI will not reinstall, modify, or recreate it.",
    oneShotEnvUnlocked: "Conda/bootstrap runs only on first creation. A successful creation automatically locks the environment.",
    experimentCondaPythonConfig: "Experiment Conda/Python Config",
    experimentCondaPythonHelp: "These fields belong only to the Environment step. The workflow uses them to create or reuse the experiment conda environment; set Experiment Python explicitly when training must not use the TASTE management environment.",
    condaEnvName: "Conda env name",
    condaBase: "Conda base",
    pythonExecutable: "management Python",
    saveExperimentEnv: "Save experiment environment config",
    firstEnvCreateControl: "First Environment Creation Control",
    firstEnvCreateHelp: "Environment setup only handles the first repo/data/conda bootstrap. After creation succeeds, the button is forcibly disabled and later experiments reuse the existing environment.",
    researchPromptPlaceholder: "New natural-language research request for initialization/logging",
    envLockedNoRepeat: "Environment locked; repeated creation/install from the web UI is disabled",
    realBootstrapConda: "Create/verify conda environment for real",
    envAssetsBlockDetails: "Environment Assets and Blockers",
    envAssetsBlockHelp: "Open each card to inspect repo, dataset, availability, and blockers. This panel hides redundant artifacts and shows only health-relevant details.",
    claudeRepoDecision: "Claude Repo Decision",
    notSelected: "Not selected yet",
    claudeNoStructuredDecision: "The Environment controller has not produced a structured repo/topic-fit judgment yet; the workflow will not treat the current repo as the final route.",
    requiredModification: "Required modification",
    riskGap: "Risk/gap",
    evidence: "Evidence",
    repoAction: "Repo action",
    repoActionReason: "Repo action reason",
    envAction: "Environment action",
    envActionReason: "Environment action reason",
    dataAction: "Data action",
    dataActionReason: "Data action reason",
    recommendedEnv: "Recommended env",
    stewardshipMemory: "Stewardship memory",
    datasetLabel: "Dataset",
    repoPathShort: "Repo path",
    gateLabel: "Gate",
    sessionLabel: "Session",
    repoLabel: "Repo",
    modeLabel: "Mode",
    autonomyLabel: "Autonomy",
    lastLabel: "Last status",
    defaultOption: "default",
    statusNotStarted: "not started",
    statusClaimReady: "auditable",
    statusNotClaimReady: "not auditable",
    statusAuto: "auto",
    candidateRepos: "Candidate repos",
    noRepoCandidates: "No repo candidates yet.",
    noRepoAudit: "No repo audit note yet",
    nextAction: "Next action",
    datasets: "Datasets",
    noDatasetRegistry: "No dataset registry entries yet.",
    noExtraInfo: "No extra details",
    missingFiles: "Missing files",
    usableData: "Usable data",
    noClaimReadyData: "No auditable data yet; experiment results cannot be packaged as formal paper evidence.",
    threshold: "Threshold",
    claimReadyThreshold: "Only real datasets with claim_ready=true and loader_probe_success=true enter this section.",
    pendingEvidence: "Evidence pending",
    noPendingEvidence: "No evidence-pending datasets.",
    pendingEvidenceFallback: "There are leads, but they have not reached the evidence threshold for real experiments.",
    blockedData: "Blocked data",
    noBlockedData: "No blocked data.",
    missingBlockReason: "Missing blocker reason; inspect the dataset registry",
    envStatus: "Environment status",
    notLockedFirstOnly: "Not locked yet; only first-time creation is allowed.",
    experimentHelp: "Supervise the experiment route: reproduce the reference work, then iterate idea, code change, experiment run, log/loss review, bad-case analysis, and next plan.",
    runExperimentLoop: "Run experiment sub-loop",
    runSettings: "Run settings",
    maxExperimentsPerRound: "Max experiments per round",
    currentDefaultBackend: "Current execution backend",
    projectDefault: "project default",
    lastActualBackend: "Last execution backend",
    currentExperimentSummary: "Current Experiment Summary",
    noExperimentRun: "Experiment has not run yet",
    completedExperiments: "Audit-ready records",
    caution: "Note",
    syntheticSmokeWarning: "Synthetic smoke tests only prove the pipeline runs; they cannot support paper conclusions.",
    experimentGateOverview: "Experiment and Reproduction Gates",
    experimentGateHelp: "This shows whether the main route can continue: paper-level reference reproduction, compute feasibility, candidate-vs-baseline progress, and loop completeness.",
    referenceReproductionGate: "Reference reproduction",
    computeFeasibility: "Compute feasibility",
    scientificProgressGate: "Scientific progress",
    iterationTrajectoryAudit: "Experiment loop",
    paperTarget: "Paper target",
    localReproduction: "Local reproduction",
    computeBudget: "Compute budget",
    currentBestCandidate: "Best candidate",
    currentBaseline: "Baseline",
    loopCompleteness: "Loop completeness",
    mustRepairBeforeNovel: "If paper-level reference reproduction is blocked, The workflow must repair the reproduction protocol/data/evaluation or switch base before treating novel methods or paper writing as the main route.",
    researchTrajectorySystem: "Research Trajectory Supervision",
    researchTrajectoryHelp: "Shows only whether evidence is blocking progress, what The workflow should do next, and whether memory is healthy.",
    trajectoryPhase: "Trajectory phase",
    assuranceStatus: "Assurance",
    landscapeNodes: "Landscape nodes",
    noveltyNodes: "Novelty nodes",
    failedHypotheses: "Failed hypotheses",
    unexploredNiches: "Unexplored niches",
    memoryEntries: "Persistent memory",
    nextObjectives: "Next objectives",
    agentRoles: "Agent roles",
    assuranceIssues: "Assurance issues",
    trajectoryFiles: "Trajectory files",
    evoPhases: "TASTE recoverable-cycle phases",
    recoverableExceptions: "Recoverable exceptions",
    localSkills: "Local skills",
    thirdPartyResearchStack: "Built-in method contracts",
    thirdPartySources: "Method sources",
    thirdPartyModules: "Capability modules",
    thirdPartySkills: "Method adapters",
    thirdPartyStackHelp: "External method sources and commits are retained only for advanced audit. TASTE's runtime flow is presented as native research-direction management, evolutionary memory, evidence assurance, trajectory optimization, and paper production, not as separate external agents.",
    sourceCommit: "commit",
    sourceLicense: "license",
    directionMemoryEntries: "Direction memory",
    evidenceIntegrity: "Evidence integrity",
    evidenceIntegrityIssues: "Integrity issues",
    optimizationQueue: "Optimization queue",
    trajectoryCheckpoints: "Trajectory checkpoints",
    trajectoryDelta: "Trajectory delta",
    evolutionaryIndex: "Evolutionary index",
    graphHistoryEntries: "Graph history",
    evolutionaryLedgerEntries: "Evolutionary ledger",
    evidenceManifestRefs: "Evidence refs",
    weakUnsupportedClaims: "Weak/unsupported claims",
    longHorizonAssets: "Long-horizon assets",
    landscapeAssessment: "Landscape assessment",
    trajectoryQueue: "Trajectory optimization queue",
    trajectorySupervisor: "Trajectory supervisor",
    supervisorSummary: "Supervisor summary",
    methodContracts: "Integrated method contracts",
    advancedAudit: "Advanced audit",
    memoryHealth: "Memory health",
    latestAutonomousRun: "Latest autonomous run",
    mainBlockers: "Main blockers",
    trajectoryProtocol: "Execution protocol",
    capabilityAudit: "Capability audit",
    capabilityStatus: "Capability status",
    capabilityModules: "Capability modules",
    capabilityChecks: "Checks",
    endToEndVerification: "End-to-end verification",
    research_trajectory_end_to_end_verification: "Research trajectory end-to-end verification",
    verificationStatus: "Verification status",
    totalChecks: "Total checks",
    failedChecks: "Failed checks",
    warningChecks: "Warning checks",
    supervisorRounds: "Supervisor rounds",
    supervisorLatest: "Latest supervisor status",
    noTrajectoryQueue: "No trajectory optimization queue yet.",
    updatedAt: "Updated at",
    noTrajectorySystem: "No research trajectory system yet; the next environment/experiment iteration will refresh it automatically.",
    noNextObjectives: "No next objectives yet.",
    noAssuranceIssues: "No assurance issues.",
    ideationMemory: "Ideation memory",
    experimentationMemory: "Experimentation memory",
    assuranceMemory: "Assurance memory",
    trajectoryMemory: "Trajectory memory",
    experimentRecordTable: "Experiment Iteration Record",
    experimentRecordHelp: "Experiment records for the current route; historical records remain in the CSV audit file.",
    experimentRecordUpdated: "Record updated",
    downloadCsv: "Download CSV",
    experimentGoal: "Experiment goal",
    variant: "Method / variant",
    repo: "Repo",
    dataset: "Dataset",
    env: "Runtime env",
    commandConfig: "Key config / command",
    badCases: "Bad cases / slices",
    reflection: "Conclusion / reflection",
    evidencePath: "Evidence paths",
    resultDetail: "Run result",
    noCurve: "No curve",
    noExperimentRecords: "No experiment records yet.",
    paperHelp: "Paper writing starts only after reference reproduction, experiment, and submission-evidence gates are ready. This page shows the truthful gates and any existing preview state.",
    runPaperWriting: "Generate and revise paper",
    paperSettingsAndGate: "Paper Settings and Gate",
    currentGate: "Current gate",
    unknown: "Unknown",
    paperStatus: "Paper status",
    template: "Template",
    fetched: "Fetched",
    notFetched: "Not fetched / not needed",
    pdfPreviewBlocked: "Preview PDF exists, but evidence gates have not cleared it as a final submission artifact",
    pdfReadyBelow: "Generated and available below",
    pdfNotGenerated: "No current paper preview yet",
    blockedPdfPreviewTitle: "Paper Preview PDF",
    runningPdfPreviewTitle: "In-progress PDF Preview",
    runningPdfPreviewHelp: "The paper stage is regenerating the current paper preview; this is the currently inspectable PDF/TeX artifact and may be replaced after compilation/audit. Do not treat it as the final submission.",
    blockedPdfPreviewHelp: "This PDF is the current generated paper preview for checking structure, layout, and content. Quality and evidence gates remain truthful, so do not treat it as a submission artifact.",
    paperOrchestraStatus: "writing status",
    paperNormalityStatus: "Paper normality audit",
    venueTemplateStatus: "Venue template format",
    paperQualityGates: "Paper quality gates",
    paperCitationRenderStatus: "Citation render audit",
    paperCitationRenderBlockers: "Citation render blockers",
    paperSelfReviewStatus: "Paper self-review",
    paperSelfReviewBlockers: "Paper self-review action items",
    paperGateSeparator: "; ",
    paperAdvancedDetails: "Advanced paper-stage details",
    normalPreviewReady: "Paper preview status",
    rawPaperOrchestraOutput: "paper writing artifact",
    hiddenPdfReason: "The current PDF is shown only as an paper preview. If quality, evidence, or submission gates have not cleared, it is not a submission artifact.",
    runningPdfReason: "Paper refresh is running; the PDF/TeX below is the currently inspectable artifact and The workflow will continue compiling, auditing, and replacing it.",
    skippedPdfReason: "Science or evidence gates have not cleared. Paper writing waits until reference reproduction, experiment, and submission-evidence gates refresh and pass.",
    blockedPdfReason: "The current PDF is a paper preview; paper normality, template, figure, or submission-evidence gates still need iteration.",
    unrestrictedLimit: "unrestricted / not enforced",
    evidenceGateNotPassed: "Evidence gate not passed",
    evidenceGateWarning: "The PDF below is for paper preview and layout checking. Review/claim/evidence gates still require more experiments or revision before final submission.",
    pdfPreviewTitle: "PDF Paper Preview",
    noPdf: "No PDF to display yet. Paper writing will generate the current venue preview after reference reproduction, experiment, and submission-evidence gates pass.",
    time: "Time",
    method: "Method",
    status: "Status",
    metric: "Metric",
    metrics: "Metrics",
    value: "Value",
    audit: "Audit",
    curve: "Curve",
    ready: "ready",
    accepted: "accepted",
    searching: "searching",
    executionReady: "execution-ready",
    needsCheck: "needs-check",
    active: "active",
    loaderOnly: "loader-only",
    probeOnly: "probe-only",
    blocked: "blocked",
  },
} satisfies Record<Lang, Record<string, string>>;

function splitList(value: string) {
  return value.split(/[,\s]+/).map((item) => item.trim()).filter(Boolean);
}

function splitCategoryList(value: string) {
  return value.split(/[,，;；\n]+/).map((item) => item.trim()).filter(Boolean);
}

function splitPhraseList(value: string) {
  return value.split(/[,，;；\n]+/).map((item) => item.trim()).filter(Boolean);
}

function normalizeSelectedYears(value: string | number[]) {
  const rawItems = Array.isArray(value) ? value : splitList(String(value || ""));
  const years = rawItems
    .map((item) => Number(item))
    .filter((year) => Number.isInteger(year) && year >= 2000 && year <= 2100);
  const uniqueYears = Array.from(new Set(years)).sort((a, b) => b - a);
  return uniqueYears.length ? uniqueYears : [DEFAULT_FIND_YEAR];
}

function isLegacyRecommendationCardStart(lines: string[], index: number) {
  const text = String(lines[index] || "").trim();
  if (!/^#\d+\b/.test(text)) return false;
  const windowText = lines.slice(index, Math.min(lines.length, index + 8)).join("\n");
  return /(?:Fit|Score)\s*=/i.test(windowText)
    || /^\s*(?:URL|PDF)\s*$/im.test(windowText)
    || /(?:全文状态|方法类型|core method reference|core reading)/i.test(windowText)
    || /\/\s*(?:推荐|recommended)\s*\//i.test(windowText);
}

function stripLegacyRecommendationCards(markdown: string) {
  const lines = String(markdown ?? "").split(/\r?\n/);
  const kept: string[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    if (isLegacyRecommendationCardStart(lines, index)) {
      index += 1;
      while (index < lines.length && lines[index].trim()) {
        if (/^#\d+\b/.test(lines[index].trim())) {
          index -= 1;
          break;
        }
        index += 1;
      }
      continue;
    }
    kept.push(lines[index]);
  }
  return kept.join("\n");
}

function stripLegacyArtifactPointerLines(markdown: string) {
  return stripLegacyRecommendationCards(markdown)
    .split(/\r?\n/)
    .filter((line) => {
      const text = line.trim();
      if (!text) return true;
      const lower = text.toLowerCase();
      const pointsToFindArtifact = lower.includes("find.md");
      const pointerVerb = /(?:见|查看|打开|see|open|refer)/i.test(text);
      const duplicatedArticleFields = /(?:摘要|推荐理由|完整|abstract|recommendation)/i.test(text);
      const legacyMetadataLine = /^(?:[-*]\s*)?(?:\*\*)?(?:id|url|pdf|fit(?:\s*分数)?|score|final\s*score|最终分数)(?:\*\*)?\s*[:：]\s*.*$/i.test(text)
        || /^(?:url|pdf)$/i.test(text);
      const legacyScoreLine = /[\/|]/.test(text) && /(?:Fit|Score)\s*=/i.test(text);
      return !(pointsToFindArtifact && pointerVerb && duplicatedArticleFields) && !legacyMetadataLine && !legacyScoreLine;
    })
    .join("\n")
    .replace(/\s*\/\s*(?:Fit|Score)\s*=\s*[^\n/]+/gi, "")
    .replace(/（\s*(?:Fit|Score)\s*=\s*[^）]+）/gi, "")
    .replace(/\(\s*(?:Fit|Score)\s*=\s*[^)]+\)/gi, "");
}

function normalizeMalformedLatexCommands(text: string) {
  const commandMap: Record<string, string> = { extit: "textit", extbf: "textbf", exttt: "texttt", extsc: "textsc", ext: "text" };
  return String(text ?? "").replace(/(^|[^A-Za-z\\])(extit|extbf|exttt|extsc|ext)\{/g, (_match, prefix, command) => String(prefix || "") + String.fromCharCode(92) + (commandMap[String(command)] || "text") + "{");
}

function normalizePublicLatexLinks(text: string) {
  const value = normalizeMalformedLatexCommands(text)
    .replace(/\\href\{(https?:\/\/[^{}\s]+)\}\{([^{}]+)\}/g, (_match, url, label) => {
      const href = String(url || "").trim();
      const textLabel = String(label || "").trim() || href;
      return `[${textLabel}](${href})`;
    })
    .replace(/\\url\{(https?:\/\/[^{}\s]+)\}/g, (_match, url) => {
      const href = String(url || "").trim();
      return `[${href}](${href})`;
    });
  return value.replace(/\\textemdash(?:\{\})?/g, " -- ");
}

function publicMarkdownArtifact(markdown: string) {
  return normalizePublicLatexLinks(stripLegacyArtifactPointerLines(markdown))
    .replace(/可作为重点精读候选/g, "可作为推荐精读候选")
    .replace(/中文摘要暂不可用/g, "中文摘要待补")
    .replace(/重新翻译/g, "后续补译")
    .replace(/\| # \| 论文 \| 方法类型 \| 主要优点 \| 主要局限 \|/g, "| # | 论文 | 机制类别 | 主要优点 | 主要局限 |")
    .replace(/方法类型：/g, "方法侧重：");
}

function markdownToHtml(markdown: string) {
  return markdownRenderer.render(publicMarkdownArtifact(markdown));
}


function badgeClass(status: string) {
  const value = String(status || "").toLowerCase();
  if (["ready", "completed", "pdf_ready", "running_or_ready", "done", "pass", "accepted"].includes(value)) return "ok";
  if (value.startsWith("blocked") || ["failed", "error", "fail"].includes(value)) return "fail";
  if (["drafting", "running", "queued", "cancelling", "in_progress"].includes(value)) return "warn";
  return "idle";
}

function jobStatusLabel(status: any, lang: Lang = "zh") {
  const value = String(status || "").trim();
  const normalized = value.toLowerCase().replace(/[\s-]+/g, "_");
  if (normalized.startsWith("stale")) return lang === "zh" ? "已停止" : "stopped";
  const labels: Record<string, { zh: string; en: string }> = {
    queued: { zh: "排队中", en: "queued" },
    running: { zh: "运行中", en: "running" },
    stale: { zh: "已停止", en: "stale" },
    done: { zh: "完成", en: "done" },
    preview_available: { zh: "预览可用", en: "preview available" },
    needs_writing: { zh: "待撰写", en: "needs writing" },
    preview_pdf_blocked: { zh: "预览受门控", en: "preview gated" },
    blocked: { zh: "阻塞", en: "blocked" },
    blocked_environment_base_selection_required: { zh: "等待环境阶段选择当前基底", en: "waiting for environment-stage base selection" },
    blocked_environment_bootstrap_failed: { zh: "Environment handoff 尚未就绪", en: "Environment handoff not ready" },
    blocked_environment_bootstrap_required: { zh: "Environment handoff 尚未就绪", en: "Environment handoff not ready" },
    environment_anchor_selection_required: { zh: "等待环境阶段选择当前基底", en: "waiting for environment-stage base selection" },
    error: { zh: "错误", en: "error" },
    cancelling: { zh: "停止中", en: "cancelling" },
    cancelled: { zh: "已取消", en: "cancelled" },
    interrupted: { zh: "已停止", en: "interrupted" },
  };
  if (labels[value]) return labels[value][lang === "zh" ? "zh" : "en"];
  if (labels[normalized]) return labels[normalized][lang === "zh" ? "zh" : "en"];
  if (normalized.startsWith("blocked_")) return lang === "zh" ? "阻塞" : "blocked";
  return value.replace(/_/g, " ") || (lang === "zh" ? "未知" : "unknown");
}

const PUBLIC_STAGES = ["find", "read", "idea", "plan", "environment", "experiment", "paper"];

function canonicalJobStage(job: any) {
  const explicitPanelStage = isClaudeGuidanceJob(job) ? jobPanelStage(job) : "";
  if (explicitPanelStage) return explicitPanelStage;
  const stageRaw = String(job?.stage || "").trim();
  const stage = stageRaw.toLowerCase().replace(/_/g, "-");
  const phase = String(job?.result?.phase || job?.progress?.phase || "").trim().toLowerCase().replace(/_/g, "-");
  const rawStage = String(job?.result?.raw_stage || "").trim().toLowerCase().replace(/_/g, "-");
  const haystack = `${stage} ${phase} ${rawStage}`;
  if (stage === "literature" || (stage === "find" && phase === "literature")) return "find";
  if (stage === "plan-polish") return "plan";
  if (stage === "email") return "paper";
  if (PUBLIC_STAGES.includes(stage)) return stage;
  if (haystack.includes("find") || haystack.includes("literature")) return "find";
  if (haystack.includes("read")) return "read";
  if (haystack.includes("idea") || haystack.includes("ideation")) return "idea";
  if (haystack.includes("plan")) return "plan";
  if (/(environment|loader|reference|fresh-base|base-selection|research-base-selection|safe-unblock)/.test(haystack)) return "environment";
  if (/(paper-pipeline|paper-preview|paper-figure|conference-preview|latex|email)/.test(haystack)) return "paper";
  if (/(experiment|autonomous|trajectory|evidence|blocker|research|guidance|full-cycle|full-research-cycle|paper-evidence-audit|paper-normality-audit|submission-readiness)/.test(haystack)) return "experiment";
  return "experiment";
}

function jobStageLabel(job: any, lang: Lang) {
  const label = jobDisplayTitle({ ...job, stage: canonicalJobStage(job), result: { ...(job?.result || {}), raw_stage: "" } }, lang);
  return lang === "zh" ? "阶段=" + label : "stage=" + label;
}

function jobProgressPhaseLabel(job: any, lang: Lang = "zh") {
  const phase = String(job?.progress?.phase || "").trim();
  if (!phase) return canonicalJobStage(job);
  const normalized = phase.toLowerCase().replace(/[\s-]+/g, "_");
  if (phase === "literature") return "find";
  if (normalized.startsWith("stale")) return lang === "zh" ? "已停止" : "stopped";
  if (phase === "complete") return jobStatusLabel("done", lang);
  if (["cancelled", "blocked", "error", "interrupted", "queued", "running", "cancelling"].includes(phase) || normalized.startsWith("blocked_")) return jobStatusLabel(phase, lang);
  if (phase === "started") return lang === "zh" ? "已启动" : "started";
  if (normalized === "current_find_read") return lang === "zh" ? "当前 Find 精读" : "current Find reading";
  if (normalized === "full_text") return lang === "zh" ? "爬文章" : "acquire papers";
  if (normalized === "deep_read") return lang === "zh" ? "读文章" : "read papers";
  return phase.replace(/_/g, " ");
}

function isFullCycleHeartbeatLine(line: string) {
  const text = String(line || "").trim().toLowerCase();
  return (text.startsWith("full-cycle:") && text.includes(" still running") && text.includes("lines=")) || (text.startsWith("[frontend] still running") && text.includes("elapsed_sec="));
}

function publicLogText(value: any, lang: Lang = "zh"): string {
  const agentName = lang === "zh" ? "模块主控 Claude" : "module controller";
  const researchAgentName = lang === "zh" ? "模块主控 Claude" : "module controller";
  let text = String(value ?? "")
    .replace(/主控\s*Claude Code/gi, "__MAIN_CLAUDE_CODE_ZH__")
    .replace(/main\s+Claude Code/gi, "__MAIN_CLAUDE_CODE_EN__")
    .replace(/当前状态[:：]\s*历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。/g, lang === "zh" ? "历史 full-cycle 启动器已停止；页面以项目摘要和实验模块为准。" : "Historical full-cycle launcher has stopped; current status comes from the project summary and Experiment module.")
    .replace(/历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。/g, lang === "zh" ? "历史 full-cycle 启动器已停止；页面以项目摘要和实验模块为准。" : "Historical full-cycle launcher has stopped; current status comes from the project summary and Experiment module.")
    .replace(/当前状态以项目摘要和实验模块为准/g, lang === "zh" ? "页面以项目摘要和实验模块为准" : "current status comes from the project summary and Experiment module")
    .replace(/stale[_ ]full[_ ]research[_ ]cycle[_ ]snapshot/gi, lang === "zh" ? "已停止" : "stopped")
    .replace(/deterministic\s+base[-_ ]switch\s+gate/gi, lang === "zh" ? "确定性 base-switch gate" : "deterministic base-switch gate")
    .replace(/base[-_ ]switch\s+gate/gi, "base-switch gate")
    .replace(/base_switch_gate/gi, "base-switch gate")
    .replace(/base_switch_execution/gi, lang === "zh" ? "base-switch 执行记录" : "base-switch execution receipt")
    .replace(/selected_base_viability_gate/gi, "experiment evidence audit")
    .replace(/selected_base_viability/gi, "experiment evidence audit")
    .replace(/selected[-_ ]base/gi, lang === "zh" ? "当前路线" : "selected repository")
    .replace(/current base full reference reproduction remains the comparison control/gi, lang === "zh" ? "当前参考复现仍是对照基线" : "current reference reproduction remains the comparison control")
    .replace(/current base_full_reference/gi, "current reference reproduction")
    .replace(/current base pretrain/gi, lang === "zh" ? "当前路线预训练" : "selected-route pretrain")
    .replace(/current base/g, lang === "zh" ? "当前路线" : "selected repository")
    .replace(/Current base/g, lang === "zh" ? "当前路线" : "Selected repository")
    .replace(/claim-ready/gi, lang === "zh" ? "审计就绪" : "auditable")
    .replace(/claim ready/gi, lang === "zh" ? "审计就绪" : "auditable")
    .replace(/\[TASTE\]\s*/g, "")
    .replace(/来源：确定性门控审计（状态和计数由项目 artifact 计算，不是项目代理自由文本）/g, "")
    .replace(/来源：确定性门控审计/g, "")
    .replace(/Source: deterministic gate audit \(status and counts are computed from project artifacts, not free-form project-agent text\)/gi, "")
    .replace(/Source: deterministic gate audit/gi, "")
    .replace(/项目代理最近一次处理已记录；阶段=[^，。]+，状态=blocked[_ ]tool[_ ]policy。详细审计保留在远端日志\/receipt 中。/g, lang === "zh" ? "模块主控 Claude 最近一次处理被安全策略拦截；详细审计保留在远端日志中。" : "The latest module-controller turn was safely blocked; detailed audit remains in remote logs.")
    .replace(/项目代理状态已记录；阶段=[^，。]+，状态=blocked[_ ]tool[_ ]policy。/g, lang === "zh" ? "模块主控 Claude 处理被安全策略拦截。" : "The module-controller turn was safely blocked.")
    .replace(/状态=blocked[_ ]tool[_ ]policy/gi, lang === "zh" ? "状态=安全策略拦截" : "status=safely blocked")
    .replace(/blocked[_ ]tool[_ ]policy/gi, lang === "zh" ? "安全策略拦截" : "safely blocked")
    .replace(/summary_source[:=]\s*deterministic_gate_audit/gi, "")
    .replace(/deterministic_gate_audit/gi, "")
    .replace(/当前\s+current_selected_plan_id/g, lang === "zh" ? "当前计划" : "selected execution plan")
    .replace(/当前\s+selected_plan_id/g, lang === "zh" ? "当前计划" : "selected execution plan")
    .replace(/current_selected_plan_id/g, lang === "zh" ? "当前计划" : "selected execution plan")
    .replace(/selected_plan_id/g, lang === "zh" ? "当前计划" : "selected execution plan")
    .replace(/当前\s+当前计划/g, "当前计划")
    .replace(/repo\/data\/protocol/g, lang === "zh" ? "仓库、数据、协议" : "repo/data/protocol")
    .replace(/idea-code-run-log\/loss-analysis-reflection-next plan/g, lang === "zh" ? "想法、代码、运行日志、loss 分析、反思和下一步计划" : "idea-code-run-log/loss-analysis-reflection-next plan")
    .replace(/实验循环：warn/g, lang === "zh" ? "实验循环：需继续检查" : "experiment loop: needs review")
    .replace(/确定性门控只确认当前状态；具体下一步应由项目代理读取证据后给出。/g, lang === "zh" ? "等待对应模块主控 Claude 读取证据并给出具体下一步。" : "Waiting for the relevant module controller to read the evidence and choose the concrete next step.")
    .replace(/确定性门控只确认当前缺口；具体实验或修复动作由项目代理读取证据后决定。/g, lang === "zh" ? "等待 Experimenting 主控 Claude 读取当前缺口证据，并给出下一轮实验或修复动作。" : "Waiting for the Experimenting controller to read the current evidence gap and choose the next experiment or repair action.")
    .replace(/确定性门控只确认当前主线缺少候选实验证据；具体下一步由项目代理读取证据后决定。/g, lang === "zh" ? "等待 Experimenting 主控 Claude 读取候选实验证据缺口，并给出下一步实验动作。" : "Waiting for the Experimenting controller to read the candidate-evidence gap and choose the next experiment action.")
    .replace(/Real-data comparison mixes metrics \(([^)]*)\); The workflow must compare on the same metric before paper promotion\./g, lang === "zh" ? "真实数据比较使用了不一致指标（$1）；需要先用同一指标重新比较，才能推进论文结论。" : "Real-data comparison uses inconsistent metrics ($1); compare on the same metric before paper promotion.")
    .replace(/native frontend skipped/g, "finding frontend skipped")
    .replace(/native frontend/g, "finding frontend")
    .replace(/当前阶段/g, "当前阶段")
    .replace(/阶段/g, "阶段")
    .replace(/TASTE\/计划/g, "Find/Plan")
    .replace(/TASTE\/Plan/g, "Find/Plan")
    .replace(/missing bib entries for cited keys=[^；。\n]+/gi, lang === "zh" ? "引用/参考文献仍需修复，具体修复清单已交由 Writing 主控 Claude 处理" : "Citation/references still need repair; detailed repair items are reserved for the Writing controller")
    .replace(/latex_undefined_citations[^；。\n]*/gi, lang === "zh" ? "引用/参考文献仍需修复，具体修复清单已交由 Writing 主控 Claude 处理" : "Citation/references still need repair; detailed repair items are reserved for the Writing controller")
    .replace(/natbib_author_undefined/gi, lang === "zh" ? "natbib 作者型引用未渲染" : "natbib author citation did not render")
    .replace(/pdf_unresolved_citation_markers/gi, lang === "zh" ? "PDF 未解析引用标记" : "PDF unresolved citation markers")
    .replace(/nature_numeric_style_textual_citations/gi, lang === "zh" ? "Nature 数字模板中的作者型引用命令" : "author-style citation commands in a Nature numeric template")
    .replace(/citation_render_clean/gi, lang === "zh" ? "引用渲染审计" : "citation render audit")
    .replace(/planning\/finding/g, "planning/finding")
    .replace(/state\/finding/g, "state/finding")
    .replace(/finding_frontend/g, "finding_frontend")
    .replace(/finding/g, "finding")
    .replace(/run_frontend/g, "run_finding")
    .replace(/run-finding/g, "run-finding")
    .replace(/PaperOrchestra/g, "writing")
    .replace(/Claude Code 原始回复/g, "模块主控处理摘要")
    .replace(/最近一次 Claude Code 原始回复/g, "最近一次模块主控处理摘要")
    .replace(/Raw Claude Code response/gi, "module-controller processing summary")
    .replace(/Latest raw Claude Code response/gi, "Latest module-controller processing summary")
    .replace(/TASTE\/Claude Code/gi, researchAgentName)
    .replace(/TASTE\/Claude/gi, researchAgentName)
    .replace(/Claude Code/gi, agentName)
    .replace(/Idea came from (?:项目代理|project agent) under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or (?:selected repository|selected base|当前路线|current route) before environment(?:-stage)? selection\.?/gi, lang === "zh" ? "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。" : "This idea was generated by the Ideation controller from the current Find/read evidence; before environment review it does not bind a repo, dataset, command, or base.")
    .replace(/Idea came from (?:项目代理|project agent) under TASTE control and was normalized by the current-Find evidence guard; paper conclusions still require repo\/data\/env\/experiment gates\.?/gi, lang === "zh" ? "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；论文结论仍需要仓库、数据、环境和实验门控通过。" : "This idea was generated by the Ideation controller from the current Find/read evidence; paper conclusions still require repository, data, environment, and experiment gates.")
    .replace(/原始回复/g, "处理摘要")
    .replace(/paper_orchestra/g, "paper writing")
    .replace(/paper-orchestra/g, "paper writing")
    .replace(/blocked[_ ]environment[_ ]bootstrap[_ ]failed/gi, lang === "zh" ? "Environment handoff 尚未就绪" : "Environment handoff not ready")
    .replace(/blocked[_ ]environment[_ ]bootstrap[_ ]required/gi, lang === "zh" ? "Environment handoff 尚未就绪" : "Environment handoff not ready")
    .replace(/environment[_ ]bootstrap[_ ]failed/gi, lang === "zh" ? "Environment handoff 尚未就绪" : "Environment handoff not ready")
    .replace(/environment_claude_code/g, "environment review")
    .replace(/environment-stage base selection/gi, "environment review")
    .replace(/environment-stage base selected/gi, "environment selected")
    .replace(/waiting_for_environment_base_selection/gi, "waiting_for_environment_review")
    .replace(/wait_for_environment_base_selection/gi, "waiting_for_environment_review")
    .replace(/当前状态[:：]\s*/g, lang === "zh" ? "状态：" : "status: ")
    .replace(/阶段状态[:：]\s*/g, lang === "zh" ? "阶段状态：" : "stage status: ")
    .replace(/当前阶段[:：]\s*/g, lang === "zh" ? "当前阶段：" : "current stage: ")
    .replace(/__MAIN_CLAUDE_CODE_ZH__/g, lang === "zh" ? "主控 Claude Code" : "main Claude Code")
    .replace(/__MAIN_CLAUDE_CODE_EN__/g, lang === "zh" ? "主控 Claude Code" : "main Claude Code")
    .trim();
  if (lang === "en") {
    const enReplacements: Array<[RegExp, string]> = [
      [/期刊稿预览已生成；正文页数\s*(\d+)；写作引用质量目标\s*([^；]+)；图表版面提示\s*(\d+)\s*项，优先处理图表占地；投稿\/证据门控仍按真实状态保留，不标记为投稿通过。/g, "Journal-style paper preview has been generated; body pages $1; citation target $2; figure-layout warnings $3; submission/evidence gates remain truthful and are not marked as passed."],
      [/参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果。/g, "Reference reproduction has passed; the current route still lacks auditable candidate-experiment results that can be written into the paper."],
      [/项目代理\s*需要补齐候选实验的来源、数据、协议、完整运行和本地产物审计；完成前不会更换当前路线或提升论文结论。/g, "The Experimenting controller must complete source, data, protocol, full-run, and artifact-local audit evidence for candidate experiments; before that it will keep the current route and avoid promoting paper conclusions."],
      [/需要补齐候选实验的来源、数据、协议、完整运行和本地产物审计；完成前不会更换当前路线或提升论文结论。/g, "must complete source, data, protocol, full-run, and artifact-local audit evidence for candidate experiments; before that it will keep the current route and avoid promoting paper conclusions."],
      [/继续补齐候选实验的来源、数据加载、协议、完整运行和本地产物审计；完成后刷新科学进展、论文证据和投稿准备度。/g, "Continue completing candidate-experiment source, data-loader, protocol, full-run, and artifact-local audit evidence; then refresh scientific-progress, paper-evidence, and submission-readiness gates."],
      [/参考工作复现已达到可继续作为基底的门槛。/g, "The reference reproduction has met the threshold for continuing with this base."],
      [/实验迭代轨迹完整。/g, "The experiment-iteration trajectory is complete."],
      [/这是当前主线下实验与参考复现记录的审计统计，不是完整科研流程完成进度；论文结论仍以科学进展、证据和投稿门控为准。/g, "This is an audit statistic for experiment and reference-reproduction records under the current route, not full research-cycle completion progress; paper conclusions still depend on scientific-progress, evidence, and submission gates."],
      [/论文结论s/g, "paper conclusions"],
      [/论文结论/g, "paper conclusions"],
      [/论文结论提升/g, "paper-conclusion gating"],
      [/论文主张/g, "paper conclusion"],
      [/投稿准备度/g, "submission readiness"],
      [/科学进展/g, "scientific progress"],
      [/论文证据/g, "paper evidence"],
      [/本地产物审计/g, "artifact-local audit"],
      [/完整运行/g, "full run"],
      [/数据加载/g, "data loader"],
      [/候选实验/g, "candidate experiment"],
      [/参考复现/g, "reference reproduction"],
      [/当前主线/g, "current route"],
      [/当前路线/g, "current route"],
      [/项目代理最近一次处理已记录；阶段=([^，]+)，状态=([^。]+)。详细审计保留在远端日志\/receipt 中。/g, "The latest module-controller turn has been recorded; stage=$1, status=$2. Detailed audit remains in the remote logs/receipt."],
      [/项目代理状态已记录；阶段=([^，]+)，状态=([^。]+)。/g, "Module-controller status has been recorded; stage=$1, status=$2."],
      [/项目代理正在处理\s*([^；]+)；详细审计保留在远端日志\/receipt 中，普通页面只展示处理摘要。/g, "The module controller is processing $1. Detailed audit remains in the remote logs/receipt; the page shows only the processing summary."],
      [/检验当前选中基底下的候选实验是否能超过当前参考复现。/g, "Test whether the candidate experiment under the current selected base can outperform the current reference reproduction."],
      [/候选实验观察记录/g, "candidate experiment observation record"],
      [/不得据此声称改进成立/g, "do not claim improvement from this record"],
      [/暂不能支撑paper conclusion，需要换思路或重新设计实验。/g, "does not yet support the paper conclusion; The workflow needs a different idea or redesigned experiment."],
      [/暂不能支撑论文主张，需要换思路或重新设计实验。/g, "does not yet support the paper conclusion; The workflow needs a different idea or redesigned experiment."],
      [/针对弱证据补做真实数据、坏例切片和反例压力测试/g, "For weak evidence, add real-data checks, bad-case slices, and counterexample stress tests"],
      [/实验产物目录/g, "artifact directory"],
      [/坏例切片/g, "bad-case slices"],
      [/审计文件/g, "audit file"],
      [/未记录坏例切片/g, "bad-case slices not recorded"],
      [/未记录/g, "not recorded"],
      [/通过：证据文件齐全/g, "pass: evidence files are complete"],
      [/命令：已记录，完整命令保留在后端任务审计。/g, "command recorded; full command remains in backend job audit."],
      [/历史 full-cycle 启动器已停止；当前状态以项目摘要和实验模块为准。/g, "Historical full-cycle launcher has stopped; current status comes from the project summary and Experiment module."],
      [/服务重启前的旧任务已停止；不是当前运行错误。/g, "Old task stopped before service reload; this is not a current runtime error."],
      [/实验\/复现审计记录/g, "experiment/reproduction audit records"],
      [/当前状态：/g, "current status: "],
      [/阶段状态：/g, "stage status: "],
      [/当前阶段：/g, "current stage: "],
      [/进程存活：/g, "process alive: "],
      [/项目：/g, "project: "],
      [/日志：/g, "log: "],
      [/观察记录/g, "observation record"],
      [/最好记录/g, "best record"],
    ];
    enReplacements.forEach(([pattern, replacement]) => {
      text = text.replace(pattern, replacement);
    });
  }
  if (lang === "en") {
    text = text
      .replace(/current base pretrain/gi, "selected-route pretrain")
      .replace(/current base/g, "current route")
      .replace(/当前基底/g, "current route")
      .replace(/当前路线/g, "current route")
      .replace(/候选实验/g, "candidate experiments")
      .replace(/来源、数据、协议、完整运行和本地产物审计/g, "source, data, protocol, full-run, and artifact-local audit")
      .replace(/需要补齐candidate experiment的来源、数据、协议、full run和artifact-local audit；完成前不会更换current route或提升paper conclusions。/g, "must complete candidate-experiment source, data, protocol, full-run, and artifact-local audit evidence; before that it will keep the current route and avoid promoting paper conclusions.")
      .replace(/research project agent\s*需要补齐candidate experiment的来源、数据、协议、full run和artifact-local audit；完成前不会更换current route或提升paper conclusions。/g, "The Experimenting controller must complete candidate-experiment source, data, protocol, full-run, and artifact-local audit evidence; before that it will keep the current route and avoid promoting paper conclusions.")
      .replace(/项目代理/g, "module controller")
      .replace(/论文结论提升/g, "paper-conclusion gating")
      .replace(/论文写作/g, "paper writing")
      .replace(/论文证据/g, "paper evidence")
      .replace(/投稿门控/g, "submission gate")
      .replace(/投稿审计/g, "submission audit")
      .replace(/claim-ready/gi, "auditable")
      .replace(/claim ready/gi, "auditable")
      .replace(/paper conclusions?/gi, "paper conclusions")
      .replace(/paper\.research project agent/gi, "paper. Writing controller")
      .replace(/research project agentmust/gi, "module controller must")
      .replace(/project agentmust/gi, "module controller must")
      .replace(/candidate experimentobservation/gi, "candidate experiment observation")
      .replace(/candidate experimentsobservation/gi, "candidate experiment observation")
      .replace(/candidate experiment的/g, "candidate-experiment ")
      .replace(/，/g, ", ")
      .replace(/最好记录/g, "best record");
  }
  if (!text) return "";
  const gateDump = /route authorization gate|route authorization status|experiment evidence audit|candidate_route|blocker_action_plan|current_route_viability/i.test(text);
  const fullCycleDump = /完整科研自循环|full cycle|full-cycle/i.test(text) && /route authorization|candidate|gate|阻塞|blocked/i.test(text);
  if (gateDump && (fullCycleDump || text.length > 220)) return "";
  return text;
}

function publicEnvironmentSelectionStatus(selection: any, lang: Lang = "zh") {
  const row = selection && typeof selection === "object" ? selection : {};
  const gate = String(row.selection_gate || row.raw_selection_gate || "").trim().toLowerCase();
  const stage = String(row.selection_stage || row.selected_by_stage || "").trim().toLowerCase();
  const zh = lang === "zh";
  if (gate.startsWith("accepted_by") || stage.includes("environment")) {
    return zh ? "已选择 / 主题适配已审计通过" : "selected / topic-fit review passed";
  }
  if (gate.includes("continued_search") || gate.includes("blocked")) {
    return zh ? "待继续选择 / 审计未通过" : "continue selection / review not passed";
  }
  if (stage || gate) {
    return publicStatusText(stage || gate, lang);
  }
  return zh ? "待选择" : "pending selection";
}

function publicStatusText(value: any, lang: Lang = "zh") {
  const raw = String(value ?? "").trim();
  const normalized = publicLogText(raw, lang).trim().toLowerCase().replace(/[\s-]+/g, "_");
  const labels: Record<string, { zh: string; en: string }> = {
    blocked_after_max_cycles: { zh: "已暂停，等待下一轮自动处理", en: "paused after configured cycles" },
    stale_full_research_cycle_snapshot: { zh: "已停止", en: "stopped" },
    experiment_evidence_audit: { zh: "实验证据审计", en: "experiment evidence audit" },
    semantic_data_provenance_required: { zh: "语义数据 provenance 门控", en: "semantic data provenance gate" },
    continue_experiment_evidence_repair: { zh: "继续补齐实验证据", en: "continue experiment evidence repair" },
    wait_for_environment_base_selection: { zh: "环境审查后执行", en: "run after environment review" },
    waiting_for_environment_base_selection: { zh: "环境审查后执行", en: "run after environment review" },
    waiting_for_environment_review: { zh: "环境审查后执行", en: "run after environment review" },
    current_find_packet_ready: { zh: "当前 Find 完成", en: "current Find complete" },
    current_environment_base_selected: { zh: "环境阶段已选定基底", en: "environment base selected" },
    accepted_by_claude_topic_fit: { zh: "已通过主题匹配审查", en: "accepted by topic-fit review" },
    claude_code_current_find_takeover: { zh: "当前 Find 精读产物", en: "current Find reading output" },
    real_data_loader_ready: { zh: "真实数据/loader 已就绪", en: "real data/loader ready" },
    approved: { zh: "通过", en: "approved" },
    pass: { zh: "通过", en: "pass" },
    completed: { zh: "完成", en: "completed" },
    selected: { zh: "已选择", en: "selected" },
    historical_evidence_retained: { zh: "历史证据保留", en: "historical evidence retained" },
    blocked: { zh: "阻塞", en: "blocked" },
    running: { zh: "运行中", en: "running" },
    preview_available: { zh: "预览可用", en: "preview available" },
    needs_writing: { zh: "待撰写", en: "needs writing" },
    preview_pdf_blocked: { zh: "预览受门控", en: "preview gated" },
    pdf_ready: { zh: "PDF 就绪", en: "PDF ready" },
    evidence_gated_preview: { zh: "证据门控未通过的论文预览", en: "paper preview with evidence gates uncleared" },
    normality_blocked: { zh: "论文预览需继续迭代", en: "paper preview needs iteration" },
    drafting: { zh: "撰写中", en: "drafting" },
    not_started: { zh: "尚未开始", en: "not started" },
    pending: { zh: "待定", en: "pending" },
  };
  if (labels[normalized]) return labels[normalized][lang === "zh" ? "zh" : "en"];
  return publicLogText(raw, lang).replace(/_/g, " ");
}

function publicLogLineText(line: string, lang: Lang, contextTab?: Tab) {
  const text = String(line || "").trim();
  if (!text) return "";
  const lowered = text.toLowerCase();
  const referenceReproductionLine = lowered.includes("selected-base full reference reproduction") || lowered.includes("selected-base full reproduction");
  if (referenceReproductionLine) {
    if (lowered.includes("running") || lowered.includes("process_alive=true")) {
      return lang === "zh" ? "参考复现正在运行；等待日志、指标和审计文件落盘。" : "Reference reproduction is running; waiting for logs, metrics, and audit artifacts.";
    }
    return lang === "zh" ? "参考复现状态已记录；完整证据保留在后端审计。" : "Reference reproduction status is recorded; full evidence remains in backend audit.";
  }
  const internalGate = lowered.includes("base_switch_gate")
    || lowered.includes("selected_base_viability")
    || lowered.includes("deterministic base-switch")
    || lowered.includes("candidate_route")
    || lowered.includes("blocker_action_plan");
  if (internalGate) {
    if (contextTab === "experiment") return lang === "zh" ? "实验门控：当前主线缺少审计就绪候选实验证据。" : "Experiment gate: current route lacks audit-ready candidate evidence.";
    if (contextTab === "environment") return lang === "zh" ? "环境门控：仓库、真实数据和参考复现状态见环境页主体。" : "Environment gate: repo, real data, and reference reproduction are shown above.";
    return lang === "zh" ? "TASTE 状态：存在科研门控，详细审计记录保留在后端日志。" : "TASTE status: a research gate is active; detailed audit logs remain in backend records.";
  }
  if ((text.startsWith("cmd=") || text.startsWith("command=")) && text.length > 180) {
    return lang === "zh" ? "命令：已记录，完整命令保留在后端任务审计。" : "command: recorded; full command remains in backend job audit.";
  }
  const cleaned = publicLogText(text, lang);
  if (!cleaned) return "";
  return cleaned.length > 260 ? `${cleaned.slice(0, 260)}...` : cleaned;
}

function originalFindLogLine(raw: string) {
  return publicLogText(String(raw || "").replace(/^find_activity=/, ""));
}

function publicJobMessageText(job: any, lang: Lang = "zh") {
  const message = String(job?.progress?.message || "").trim();
  if (!message) return "";
  const publicText = publicLogText(message, lang);
  if (canonicalJobStage(job) === "read") {
    return publicText.length > 300 ? `${publicText.slice(0, 300)}...` : publicText;
  }
  const lower = message.toLowerCase();
  const isGateDump = lower.includes("deterministic base-switch")
    || lower.includes("base_switch_gate")
    || lower.includes("selected_base_viability")
    || lower.includes("blocker_action_plan")
    || lower.includes("candidate_route")
    || publicText.length > 180;
  if (isGateDump) return stableJobProgressSummary(job, lang);
  return publicText;
}

function isTransientFindServiceLine(raw: string) {
  const text = String(raw || "").trim().toLowerCase();
  if (!text) return false;
  const markers = [
    "transient service error",
    "read operation timed out",
    "too many requests",
    "http 429",
    "queued for bounded single-item retry",
    "single-item retry disabled",
    "fallback-only marking",
    "unresolved-item audit marking",
    "marking unresolved items for audit",
    "latest released venue for freshness bonus",
    "abstract enrichment filled",
    "final scoring abstract enrichment",
    "abstract contract excluded",
    "title-filtered candidates before llm",
    "wrapper emitted structured evidence json",
    "wrapper structured evidence output suppressed",
    "full evidence is stored under",
  ];
  return markers.some((marker) => text.includes(marker));
}

function stableJobProgressSummary(job: any, lang: Lang = "zh") {
  const phase = jobProgressPhaseLabel(job, lang);
  const status = jobStatusLabel(job?.status, lang);
  const current = Number(job?.progress?.current || 0);
  const total = Number(job?.progress?.total || 0);
  if (Number.isFinite(total) && total > 0) return `${phase} ${status}; ${current || 0}/${total}`;
  if (Number.isFinite(current) && current > 0) return `${phase} ${status}; lines=${current}`;
  return `${phase} ${status}`;
}

function displayJobProgressMessage(job: any, lang: Lang = "zh") {
  if (isHistoricalStoppedResearchCycleJob(job)) return historicalResearchCycleSummary(job, lang)[0].replace(/^[^:：]+[:：]\s*/, "");
  const message = String(job?.progress?.message || "").trim();
  if (!message) return "";
  const containsTasteLog = message.includes("[TASTE]") || message.startsWith("find_activity=");
  if (!containsTasteLog && !isTransientFindServiceLine(message)) return publicJobMessageText(job, lang);
  const prefix = containsTasteLog ? message.slice(0, Math.max(0, message.indexOf("[TASTE]")).valueOf()).trim().replace(/[；;]\s*$/, "") : "";
  return publicLogText(prefix, lang) || stableJobProgressSummary(job, lang);
}

function humanFindLog(raw: string, _lang: Lang = "zh") {
  return originalFindLogLine(raw);
}

function summarizeJobLogLine(line: string, lang: Lang, contextTab?: Tab) {
  const text = String(line || "").trim();
  if (!text || isFullCycleHeartbeatLine(text)) return "";
  const findOnlyPrefixes = [
    "find_counts=",
    "literature_packet=",
    "strong_recommendations=",
    "claude_takeover=",
    "current_goal=current Find strict strong recommendations are below target",
  ];
  if (contextTab && contextTab !== "find" && findOnlyPrefixes.some((prefix) => text.startsWith(prefix))) return "";
  if (text.startsWith("project=")) return lang === "zh" ? `项目：${text.slice(8)}` : `project: ${text.slice(8)}`;
  if (text.startsWith("stage=")) return lang === "zh" ? `阶段：${publicLogText(text.slice(6), lang)}` : `stage: ${publicLogText(text.slice(6), lang)}`;
  if (text.startsWith("pid=")) return `PID：${text.slice(4)}`;
  if (text.startsWith("process_alive=")) {
    const alive = text.slice(14).trim();
    return lang === "zh" ? `进程存活：${alive}` : `process alive: ${alive}`;
  }
  if (text.startsWith("summary=")) return lang === "zh" ? `摘要：${publicLogText(text.slice(8), lang)}` : `summary: ${publicLogText(text.slice(8), lang)}`;
  if (text.startsWith("当前阶段：")) return publicLogText(text, lang);
  if (text.startsWith("当前阶段：")) return lang === "zh" ? `当前阶段：${publicLogText(text.slice(8), lang)}` : `current stage: ${publicLogText(text.slice(8), lang)}`;
  if (text.startsWith("当前状态：") || text.startsWith("当前状态:")) {
    const value = text.replace(/^当前状态[:：]\s*/, "");
    return lang === "zh" ? `状态：${publicLogText(value, lang)}` : `status: ${publicLogText(value, lang)}`;
  }
  if (text.startsWith("阶段状态：")) return lang === "zh" ? `阶段状态：${publicLogText(text.slice(5), lang)}` : `stage status: ${publicLogText(text.slice(5), lang)}`;
  if (text.startsWith("阶段进度：")) return lang === "zh" ? `阶段进度：${publicLogText(text.slice(5), lang)}` : `stage progress: ${publicLogText(text.slice(5), lang)}`;
  if (text.startsWith("当前动作：")) return lang === "zh" ? `当前动作：${publicLogText(text.slice(5), lang)}` : `current action: ${publicLogText(text.slice(5), lang)}`;
  if (text.startsWith("当前步骤：")) return text.replace("当前步骤：", lang === "zh" ? "当前动作：" : "current action: ");
  if (text.startsWith("细节：")) return lang === "zh" ? `细节：${publicLogText(text.slice(3), lang)}` : `detail: ${publicLogText(text.slice(3), lang)}`;
  if (text.startsWith("错误：")) return lang === "zh" ? `错误：${publicLogText(text.slice(3), lang)}` : `error: ${publicLogText(text.slice(3), lang)}`;
  if (text.startsWith("find_activity=") || text.startsWith("[TASTE]")) return originalFindLogLine(text);
  if (text.startsWith("find_live_progress=")) return originalFindLogLine(text.replace("find_live_progress=", ""));
  if (text.startsWith("find_source_status=")) return text.replace("find_source_status=", lang === "zh" ? "来源状态：" : "source status: ");
  if (text.startsWith("find_run_counts=")) return text.replace("find_run_counts=", lang === "zh" ? "Find 当前计数：" : "Find counts: ");
  if (text.startsWith("fresh_find_running=true")) {
    return lang === "zh"
      ? "新的 Find/文献调研正在运行；旧推荐统计仅作历史参考，等待本轮 Find 产物替换。"
      : "Fresh Find is running; previous recommendation counts are historical until this run replaces them.";
  }
  if (text.startsWith("find_counts=")) {
    const raw = text.match(/raw_title_index:(\d+)/)?.[1] || "";
    const titles = text.match(/title_candidates:(\d+)/)?.[1] || "";
    const details = text.match(/detail_fetched:(\d+)/)?.[1] || "";
    const scored = text.match(/evaluated_candidates:(\d+)/)?.[1] || "";
    return lang === "zh" ? `当前 Find run 计数：原始题录 ${raw || "?"}，标题候选 ${titles || "?"}，详情 ${details || "?"}，LLM 评分 ${scored || "?"}；这是底部全局 任务栏的 run/job 日志，不属于实验迭代主体。` : `Current Find run counts: raw ${raw || "?"}, title candidates ${titles || "?"}, details ${details || "?"}, LLM scored ${scored || "?"}; this is the bottom global taskbar log, not the Experiment page body.`;
  }
  if (text.startsWith("literature_packet=")) return lang === "zh" ? `文献包：${text.replace("literature_packet=", "")}` : `literature packet: ${text.replace("literature_packet=", "")}`;
  if (text.startsWith("recommendations=")) {
    const value = text.slice(16).trim();
    const count = value.match(/^(\d+\/?\d*)/)?.[1] || "";
    const shortfall = value.match(/shortfall=([^;]+)/)?.[1]?.trim() || "";
    const complete = /current Find title\+abstract LLM scoring complete/i.test(value);
    if (lang === "zh") {
      return [`Find 推荐门控：${count ? `推荐论文 ${count}` : value}`, shortfall ? `短缺 ${shortfall}` : "", complete ? "当前 Find 标题+摘要 LLM 评分已完成" : ""].filter(Boolean).join("；");
    }
    return [`Find recommendation gate: ${count || value}`, shortfall ? `shortfall ${shortfall}` : "", complete ? "current Find title+abstract LLM scoring complete" : ""].filter(Boolean).join("; ");
  }
  if (text.startsWith("claude_takeover=")) {
    const value = text.slice(16).trim();
    const label = value === "completed" ? (lang === "zh" ? "已完成" : "completed") : value.replace(/_/g, " ");
    return lang === "zh" ? `模块主控接管状态：${label}` : `module-controller takeover: ${label}`;
  }
  if (text.startsWith("latest=")) {
    const value = text.slice(7).trim();
    if (isFullCycleHeartbeatLine(value)) return "";
    const publicValue = value.startsWith("[TASTE]") ? originalFindLogLine(value) : publicLogText(value, lang);
    return lang === "zh" ? `实时日志：${publicValue}` : `live log: ${publicValue}`;
  }
  if (text.startsWith("last_log=")) {
    const value = text.slice(9).trim();
    if (isFullCycleHeartbeatLine(value)) return "";
    const publicValue = value.startsWith("[TASTE]") ? originalFindLogLine(value) : publicLogText(value, lang);
    return lang === "zh" ? `最后日志：${publicValue}` : `last log: ${publicValue}`;
  }
  if (text.startsWith("experiment_cmd=")) return lang === "zh" ? `实验命令：${text.slice(15)}` : `experiment cmd: ${text.slice(15)}`;
  if (text.startsWith("process=")) return lang === "zh" ? `进程：${text.slice(8)}` : `process: ${text.slice(8)}`;
  if (text.startsWith("experiment_artifact=")) return lang === "zh" ? `实验产物：${text.slice(20)}` : `experiment artifact: ${text.slice(20)}`;
  if (text.startsWith("experiment_log=")) {
    const raw = text.slice(15);
    const path = raw.split(";")[0].trim();
    if (raw.includes("empty_or_waiting_for_output=true")) {
      return lang === "zh" ? `实验日志：${path}（当前 run 日志仍为空或等待缓冲输出）` : `experiment log: ${path} (current run log is empty or waiting for buffered output)`;
    }
    if (raw.includes("stale_before_current_process=true")) {
      return lang === "zh" ? `实验日志：${path}（旧日志，早于当前训练进程启动；不作为当前 full-run 输出）` : `experiment log: ${path} (stale log from before the current training process; not current full-run output)`;
    }
    return lang === "zh" ? `实验日志：${raw}` : `experiment log: ${raw}`;
  }
  if (text.startsWith("experiment_output_status=")) return lang === "zh" ? `实验输出状态：${publicLogText(text.slice(25), lang)}` : `experiment output status: ${publicLogText(text.slice(25), lang)}`;
  if (text.startsWith("experiment_output_source=")) return lang === "zh" ? `实验输出来源：${publicLogText(text.slice(25), lang)}` : `experiment output source: ${publicLogText(text.slice(25), lang)}`;
  if (text.startsWith("experiment_output=")) return lang === "zh" ? `实验输出：${publicLogText(text.slice(18), lang)}` : `experiment output: ${publicLogText(text.slice(18), lang)}`;
  if (text.startsWith("stage_output=")) return lang === "zh" ? `当前阶段输出：${publicLogText(text.slice(13), lang)}` : `current stage output: ${publicLogText(text.slice(13), lang)}`;
  if (text.startsWith("full_cycle_output=")) return lang === "zh" ? `full-cycle 日志：${publicLogText(text.slice(18), lang)}` : `full-cycle log: ${publicLogText(text.slice(18), lang)}`;
  if (text.startsWith("artifact=")) return lang === "zh" ? `产物：${text.slice(9)}` : `artifact: ${text.slice(9)}`;
  if (text.startsWith("cmd=")) return lang === "zh" ? `命令：${publicLogText(text.slice(4), lang)}` : `cmd: ${publicLogText(text.slice(4), lang)}`;
  if (text.startsWith("log=")) return lang === "zh" ? `日志：${text.slice(4)}` : `log: ${text.slice(4)}`;
  return publicLogLineText(text, lang, contextTab);
}

function jobDisplayTitle(job: any, lang: Lang) {
  const normalized = canonicalJobStage(job);
  const labels: Record<string, { zh: string; en: string }> = {
    find: { zh: "find", en: "find" },
    read: { zh: "read", en: "read" },
    idea: { zh: "idea", en: "idea" },
    plan: { zh: "plan", en: "plan" },
    "plan-polish": { zh: "plan", en: "plan" },
    email: { zh: "paper", en: "paper" },
    literature: { zh: "find", en: "find" },
    environment: { zh: "environment", en: "environment" },
    experiment: { zh: "experiment", en: "experiment" },
    paper: { zh: "paper", en: "paper" },
    "literature-base-audit": { zh: "literature-base-audit", en: "literature-base-audit" },
    "full-cycle": { zh: "full-cycle", en: "full-cycle" },
    "current-find-selection": { zh: "当前 Find 选执行计划", en: "current Find execution selection" },
  };
  return labels[normalized]?.[lang === "zh" ? "zh" : "en"] || normalized || "job";
}

function visibleJobIdentifier(job: any) {
  const jobId = String(job?.job_id || "").trim();
  if (!jobId) return "";
  if (/^ar[-_]/.test(jobId)) return "";
  if (jobId.startsWith("current-find-")) return "";
  return jobId;
}

function jobMetaLine(job: any, lang: Lang = "zh") {
  const visibleId = visibleJobIdentifier(job);
  return [jobStageLabel(job, lang), visibleId, runIdFromJob(job) ? `run=${runIdFromJob(job)}` : ""].filter(Boolean).join(" / ");
}

function isFindRunJob(job: any) {
  const stage = canonicalJobStage(job);
  const jobId = String(job?.job_id || "").trim();
  return stage === "find" || jobId.startsWith("find-run-find") || jobId.startsWith("find_");
}

function isPrimaryFindTaskJob(job: any) {
  const jobId = String(job?.job_id || "").trim();
  return isFindRunJob(job) && (jobId.startsWith("find_") || String(job?.result?.action || "") === "find");
}

type FindTaskProgressView = {
  stagePercent: number;
  stageIndex: number;
  stageCount: number;
  stageLabel: string;
  stepLabel: string;
  action: string;
  logLines: string[];
};

function boundedPercent(value: any) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(100, Math.round(number)));
}

function findPhaseLabel(phase: string, lang: Lang) {
  const labels: Record<string, { zh: string; en: string }> = {
    initializing: { zh: "初始化 Find 任务", en: "initializing Find" },
    queued: { zh: "等待启动", en: "waiting to start" },
    running: { zh: "初始化 Find 任务", en: "initializing Find" },
    started: { zh: "初始化 Find 任务", en: "initializing Find" },
    venue_title_index: { zh: "抓取出版渠道题录", en: "fetching publication records" },
    venue_scan_complete: { zh: "出版渠道处理完成", en: "publication venues complete" },
    title_prefilter: { zh: "本地标题召回", en: "local title recall" },
    llm_title_filter: { zh: "标题 LLM 筛选", en: "LLM title screening" },
    detail_fetch: { zh: "抓取候选详情", en: "fetching candidate details" },
    detail_enrichment: { zh: "补全候选详情", en: "enriching candidate details" },
    abstract_enrichment: { zh: "补全真实摘要", en: "enriching real abstracts" },
    nature_detail_enrichment: { zh: "补全 Nature 文章详情", en: "enriching Nature article details" },
    science_detail_enrichment: { zh: "补全 Science 文章详情", en: "enriching Science article details" },
    nature: { zh: "检索 Nature 渠道", en: "searching Nature sources" },
    science: { zh: "检索 Science 渠道", en: "searching Science sources" },
    arxiv: { zh: "检索 arXiv 渠道", en: "searching arXiv" },
    biorxiv: { zh: "检索 bioRxiv 渠道", en: "searching bioRxiv" },
    huggingface: { zh: "检索 HuggingFace 渠道", en: "searching HuggingFace" },
    github: { zh: "检索 GitHub 渠道", en: "searching GitHub" },
    source_collection_complete: { zh: "候选池收集完成", en: "candidate collection complete" },
    abstract_contract: { zh: "校验真实摘要", en: "validating real abstracts" },
    abstract_scoring: { zh: "摘要 LLM 评估", en: "LLM abstract evaluation" },
    abstract_scoring_retry: { zh: "重试未完成的摘要评估", en: "retrying incomplete abstract evaluations" },
    final_ranking_prepare: { zh: "生成最终推荐排序", en: "building final recommendation ranking" },
    preliminary_artifacts_written: { zh: "整理推荐结果", en: "assembling recommendation results" },
    abstract_translation: { zh: "翻译推荐论文摘要", en: "translating recommended abstracts" },
    abstract_translation_retry: { zh: "重试摘要翻译", en: "retrying abstract translation" },
    abstract_translation_final: { zh: "完成摘要翻译", en: "finalizing abstract translations" },
    complete: { zh: "Find 产物写入完成", en: "Find artifacts complete" },
  };
  if (labels[phase]) return labels[phase][lang === "zh" ? "zh" : "en"];
  if (phase.endsWith("_llm_scoring_complete")) {
    const source = phase.replace(/_llm_scoring_complete$/, "").replace(/_/g, " ");
    return lang === "zh" ? `${source} 摘要评估完成` : `${source} abstract evaluation complete`;
  }
  return phase.replace(/_/g, " ") || (lang === "zh" ? "初始化 Find 任务" : "initializing Find");
}

function findStageProjectionLabel(value: any, lang: Lang) {
  const label = String(value || "").trim();
  const labels: Record<string, { zh: string; en: string }> = {
    "初始化研究画像": { zh: "初始化研究画像", en: "Initialize research profile" },
    "会议论文检索与标题筛选": { zh: "出版渠道题录检索与标题筛选", en: "Publication-record retrieval and title screening" },
    "扩展渠道论文检索": { zh: "扩展来源论文检索", en: "Extended-source retrieval" },
    "摘要校验与 LLM 综合评估": { zh: "摘要校验与 LLM 综合评估", en: "Abstract validation and LLM evaluation" },
    "推荐排序与产物生成": { zh: "推荐排序与产物生成", en: "Recommendation ranking and artifact generation" },
    "Find 完成": { zh: "Find 完成", en: "Find complete" },
  };
  return labels[label]?.[lang === "zh" ? "zh" : "en"] || label || "Find";
}

function findActionText(value: any, phase: string, lang: Lang) {
  const raw = publicLogText(String(value || "").trim(), lang);
  if (!raw) return findPhaseLabel(phase, lang);
  if (lang !== "zh") return raw;
  return raw
    .replace(/^Starting venue title index fetch$/i, "开始抓取所选出版渠道题录")
    .replace(/^Checking year availability:\s*/i, "检查出版渠道年份可用性：")
    .replace(/^Fetching title index:\s*/i, "抓取出版渠道题录：")
    .replace(/^Fetching selected paper details$/i, "抓取入选论文详情")
    .replace(/:\s*fetching selected paper details$/i, "：抓取入选论文详情")
    .replace(/:\s*metadata details ready$/i, "：候选详情已就绪")
    .replace(/:\s*detail fetch complete$/i, "：候选详情抓取完成")
    .replace(/:\s*starting LLM title filter, uncached batches\s*/i, "：开始标题 LLM 筛选；未缓存批次 ")
    .replace(/:\s*scoring title batch\s*/i, "：正在评估标题批次 ")
    .replace(/:\s*scored title batch\s*/i, "：已完成标题批次 ")
    .replace(/:\s*processed\s+(\d+)\s+title batches; scored\s+(\d+)/i, "：标题批次已处理 $1；已评分标题 $2")
    .replace(/:\s*scoring batch\s*/i, "：正在评估摘要批次 ")
    .replace(/:\s*scored batch\s*/i, "：已完成摘要批次 ")
    .replace(/^Fetching arXiv$/i, "开始检索 arXiv")
    .replace(/^Fetching bioRxiv$/i, "开始检索 bioRxiv")
    .replace(/^Fetching Nature Portfolio$/i, "开始检索 Nature Portfolio")
    .replace(/^Fetching Science Family$/i, "开始检索 Science Family")
    .replace(/^Fetching HuggingFace$/i, "开始检索 HuggingFace")
    .replace(/^Fetching GitHub$/i, "开始检索 GitHub")
    .replace(/^arXiv query\s+([^:]+):\s*([^,]+),\s*page start\s+(\d+)/i, "arXiv 查询 $1：$2，当前抓取页起点 $3")
    .replace(/with\s+(\d+)\s+workers?/i, "并发 $1")
    .replace(/cache_hits\s+(\d+)/i, "缓存命中 $1")
    .replace(/cache_hits=(\d+)/i, "缓存命中 $1");
}

function findCountLogLine(counts: any, lang: Lang) {
  if (!counts || typeof counts !== "object") return "";
  const number = (value: any) => Number(value || 0).toLocaleString(lang === "zh" ? "zh-CN" : "en-US");
  const titleInput = Number(counts.title_score_input_papers || counts.tfidf_screened_papers || counts.category_filtered_papers || 0);
  const titleScored = Number(counts.llm_title_scored_papers || 0);
  if (lang === "zh") {
    return `实时计数：题录 ${number(counts.raw_title_index || counts.raw_title_index_papers)}；标题 LLM ${number(titleScored)}/${number(titleInput)}；标题候选 ${number(counts.title_candidates)}；详情 ${number(counts.detail_fetched)}；摘要 LLM ${number(counts.abstract_scored_papers || counts.llm_scored_candidates)}`;
  }
  return `Live counts: titles ${number(counts.raw_title_index || counts.raw_title_index_papers)}; title LLM ${number(titleScored)}/${number(titleInput)}; title candidates ${number(counts.title_candidates)}; details ${number(counts.detail_fetched)}; abstract LLM ${number(counts.abstract_scored_papers || counts.llm_scored_candidates)}`;
}

function findTaskProgressView(job: any, lang: Lang): FindTaskProgressView | null {
  if (!isPrimaryFindTaskJob(job)) return null;
  const projection = job?.result?.find_progress;
  if (!projection || typeof projection !== "object") return null;
  const stageCount = Math.max(1, Number(projection.stage_total || 6));
  const stageIndex = Math.max(1, Math.min(stageCount, Number(projection.stage_index || 1)));
  const phase = String(projection.raw_phase || "initializing").trim().toLowerCase().replace(/[\s-]+/g, "_");
  const action = findActionText(projection.message, phase, lang);
  const stageLabel = findStageProjectionLabel(projection.stage_label, lang);
  const countLine = findCountLogLine(projection.counts, lang);
  const rawCurrent = Number(projection.raw_current || 0);
  const rawTotal = Number(projection.raw_total || 0);
  const messageBatch = String(projection.message || "").match(/batch\s+(\d+)\/(\d+)/i);
  const batchCurrent = Number(messageBatch?.[1] || rawCurrent);
  const batchTotal = Number(messageBatch?.[2] || rawTotal);
  const batchLine = batchTotal > 0
    ? `${lang === "zh" ? "当前步骤批次" : "Current step batches"}：${batchCurrent}/${batchTotal}`
    : "";
  const logLines = [
    `${lang === "zh" ? "当前具体任务" : "Current task"}：${action}`,
    `${lang === "zh" ? "流程位置" : "Workflow position"}：${stageLabel}；${lang === "zh" ? "具体步骤" : "step"}：${findPhaseLabel(phase, lang)}`,
    batchLine,
    countLine,
  ].filter(Boolean);
  return {
    stagePercent: boundedPercent(projection.stage_percent),
    stageIndex,
    stageCount,
    stageLabel,
    stepLabel: findPhaseLabel(phase, lang),
    action,
    logLines,
  };
}

function jobProcessAliveValue(job: any): boolean | null {
  if (job?.process_alive === true || job?.result?.process_alive === true || job?.alive === true || job?.result?.alive === true) return true;
  if (job?.process_alive === false || job?.result?.process_alive === false || job?.alive === false || job?.result?.alive === false) return false;
  const logValue = safeJobLogs(job)
    .map((line) => String(line || "").trim().toLowerCase())
    .find((line) => line.startsWith("process_alive=") || line.startsWith("alive="));
  if (logValue?.endsWith("true")) return true;
  if (logValue?.endsWith("false")) return false;
  return null;
}

function isHistoricalStoppedResearchCycleJob(job: any) {
  const status = String(job?.status || "").toLowerCase();
  if (["queued", "running", "cancelling"].includes(status)) return false;
  const jobId = String(job?.job_id || "").toLowerCase().replace(/_/g, "-");
  const rawStage = String(job?.result?.raw_stage || job?.progress?.phase || job?.stage || "").toLowerCase().replace(/_/g, "-");
  const command = String(job?.result?.command || "").toLowerCase();
  const looksLikeResearchCycle = jobId.includes("full-cycle") || rawStage.includes("full-cycle") || command.includes("run-full-research-cycle") || command.includes("run_full_research_cycle.py");
  if (!looksLikeResearchCycle) return false;
  const alive = jobProcessAliveValue(job);
  return alive === false || ["blocked", "done", "error", "cancelled"].includes(status);
}

function historicalResearchCycleSummary(job: any, lang: Lang = "zh") {
  const status = jobStatusLabel(job?.status, lang);
  const runId = runIdFromJob(job);
  const created = formatDateMinute(job?.created_at, lang);
  const summary = lang === "zh"
    ? "历史科研循环任务已停止；当前状态请以上方七个阶段卡片和实验记录为准。"
    : "Historical research-cycle task has stopped; use the seven stage cards and Experiment records above for current status.";
  return [
    `${lang === "zh" ? "状态" : "status"}: ${summary}`,
    [lang === "zh" ? "任务" : "job", status, created ? `${lang === "zh" ? "启动" : "created"}: ${created}` : "", runId ? `run=${runId}` : ""].filter(Boolean).join(" / "),
  ];
}

function nonFindTabFindJobSummary(job: any, lang: Lang = "zh") {
  const runId = runIdFromJob(job);
  const progressMessage = publicLogText(String(job?.progress?.message || "").trim(), lang);
  const parts = [
    `${lang === "zh" ? "状态" : "status"}=${jobStatusLabel(job?.status, lang)}`,
    jobStageLabel(job, lang),
    runId ? `run=${runId}` : "",
  ].filter(Boolean).join(" / ");
  return [
    lang === "zh" ? `历史 Find job 摘要：${parts}` : `Historical Find job summary: ${parts}`,
    progressMessage ? (lang === "zh" ? `最后进度：${progressMessage}` : `last progress: ${progressMessage}`) : "",
    lang === "zh"
      ? "该 Find job 已结束；抓取、标题筛选、详情评分、LLM 评分和 Find 产物请在“发现”页展开。正在运行的 Find 会在这里实时显示详细日志。"
      : "This Find job has finished; expand retrieval, title screening, detail scoring, LLM scoring, and Find artifacts on the Find page. Running Find jobs show detailed live logs here.",
  ].filter(Boolean);
}

function jobRecentLogs(job: any, lang: Lang = "zh", contextTab?: Tab) {
  if (isHistoricalStoppedResearchCycleJob(job)) return historicalResearchCycleSummary(job, lang);
  const status = String(job?.status || "").toLowerCase();
  const foldableFindHistory = ["done", "cancelled", "stale", "interrupted"].includes(status);
  if (contextTab && contextTab !== "find" && isFindRunJob(job) && foldableFindHistory) {
    return nonFindTabFindJobSummary(job, lang);
  }
  const rawLogs = safeJobLogs(job).filter((line) => {
    if (isTransientFindServiceLine(line)) return false;
    if (!(contextTab && contextTab !== "find" && isFindRunJob(job))) return true;
    const text = String(line || "").trim();
    return !text.startsWith("阶段数量：")
      && !text.startsWith("推荐质量：")
      && !text.startsWith("find_run_counts=")
      && !text.startsWith("recommendations=");
  });
  const scoringProgress = rawLogs.map((line) => String(line || "")).find((line) => line.startsWith("find_live_progress=") && /scored batch|scoring batch|abstract_scoring|LLM/i.test(line));
  const scoringBatch = scoringProgress?.match(/进度\s*([^；;]+)/)?.[1] || scoringProgress?.match(/batch\s+(\d+\/\d+)/i)?.[1] || "";
  return rawLogs
    .map((line) => {
      const summarized = publicLogLineText(summarizeJobLogLine(line, lang, contextTab), lang, contextTab);
      if (scoringBatch && summarized.includes("LLM 已评分 0")) {
        return summarized.replace("LLM 已评分 0", `LLM 评分批次 ${scoringBatch}`);
      }
      return summarized;
    })
    .filter((line) => String(line || "").trim());
}

function numberText(value: any) {
  const num = Number(value);
  if (Number.isFinite(num)) return Math.abs(num) >= 100 ? num.toFixed(1) : num.toFixed(4);
  return String(value ?? "");
}

function formatDateMinute(value: any, lang: Lang = "zh") {
  const text = String(value ?? "").trim();
  if (!text) return "";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return date.toLocaleString(lang === "zh" ? "zh-CN" : "en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function experimentMetricRows(row: any) {
  const explicit = asArray(row?.metric_rows)
    .map((metric: any) => ({
      key: String(metric?.key || metric?.metric || metric?.name || "").trim(),
      value: metric?.value ?? metric?.metric_value ?? metric?.result,
    }))
    .filter((metric) => metric.key || String(metric.value ?? "").trim());
  if (explicit.length) return explicit;
  const metrics = row?.metrics && typeof row.metrics === "object" && !Array.isArray(row.metrics) ? row.metrics : {};
  const fromDict = Object.entries(metrics)
    .filter(([, value]) => typeof value !== "object" || value === null)
    .map(([key, value]) => ({ key, value }));
  if (fromDict.length) return fromDict;
  const key = String(row?.metric || row?.metric_name || "").trim();
  const value = row?.metric_value ?? row?.result;
  return key || String(value ?? "").trim() ? [{ key: key || "metric", value }] : [];
}

function experimentMetricRowsFromRecord(text: any) {
  const raw = String(text ?? "").trim();
  if (!raw) return [];
  return raw.split(";")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const [key, ...valueParts] = item.split("=");
      return { key: key.trim(), value: valueParts.join("=").trim() };
    })
    .filter((metric) => metric.key || String(metric.value || "").trim());
}

function Sparkline({ values, emptyLabel = "No curve" }: { values: any[]; emptyLabel?: string }) {
  const nums = values.map((item) => Number(item)).filter((item) => Number.isFinite(item));
  if (nums.length < 2) return <div className="emptyCurve">{emptyLabel}</div>;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const points = nums.map((value, index) => {
    const x = (index / Math.max(1, nums.length - 1)) * 180;
    const y = 46 - ((value - min) / span) * 40;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg className="sparkline" viewBox="0 0 180 52" role="img" aria-label="loss curve">
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2.5" />
      <text x="0" y="10">{numberText(max)}</text>
      <text x="0" y="50">{numberText(min)}</text>
    </svg>
  );
}

function asArray(value: any): any[] {
  return Array.isArray(value) ? value : [];
}

function firstNonEmptyArray(...values: any[]): any[] {
  return values.find((value) => Array.isArray(value) && value.length > 0) || [];
}

function safeJobLogs(job: any): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  asArray(job?.logs).map((line) => String(line ?? "")).forEach((line) => {
    const text = line.trim();
    if (!text || isFullCycleHeartbeatLine(text) || seen.has(text)) return;
    seen.add(text);
    result.push(line);
  });
  return result;
}

function isLiveJob(job: any) {
  return ["queued", "running", "cancelling"].includes(String(job?.status || ""));
}

function JobDuration({ createdAt, finishedAt = "", live, lang }: { createdAt: string; finishedAt?: string; live: boolean; lang: Lang }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);

  const startedAt = Date.parse(createdAt);
  const endedAt = live ? now : Date.parse(finishedAt);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt)) return null;
  const totalSeconds = Math.max(0, Math.floor((endedAt - startedAt) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const elapsed = [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");

  const label = live ? (lang === "zh" ? "已运行" : "Running") : (lang === "zh" ? "耗时" : "Elapsed");
  return <time className="jobElapsed"> · {label} {elapsed}</time>;
}

function isStoppedWorkflowStatus(status: any) {
  const normalized = String(status || "").trim().toLowerCase();
  if (!normalized) return false;
  return (
    normalized === "stale" ||
    normalized.startsWith("stale_") ||
    normalized.startsWith("historical_") ||
    normalized.startsWith("blocked_") ||
    normalized.startsWith("cancelled") ||
    normalized.startsWith("interrupted") ||
    normalized.startsWith("done") ||
    normalized.startsWith("complete") ||
    normalized.startsWith("error") ||
    normalized.startsWith("failed")
  );
}

function runIdFromJob(job: any) {
  const explicitRunId = String(job?.run_id || "").trim();
  if (explicitRunId) return explicitRunId;
  const resultRunId = String(job?.result?.run_id || "").trim();
  if (resultRunId) return resultRunId;
  const createdRunLog = safeJobLogs(job)
    .map((line) => line.match(/Created run\s+(\S+)/)?.[1] || "")
    .find(Boolean);
  return createdRunLog || "";
}

function activeFindRunIdFromJobs(items: any) {
  const liveFind = asArray(items)
    .map(normalizeJobForState)
    .filter((item): item is Job => Boolean(item))
    .filter((item) => !isInternalJob(item) && !isSyntheticProjectJob(item))
    .filter((item) => String(item.stage || "") === "find" && isLiveJob(item))
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))[0];
  return runIdFromJob(liveFind);
}

function currentFindRunIdFromJobs(items: any, projectId = "") {
  const candidates = asArray(items)
    .map(normalizeJobForState)
    .filter((item): item is Job => Boolean(item))
    .map((item) => ({ item, runId: runIdFromJob(item) }))
    .filter(({ item, runId }) => {
      if (!String(runId || "").startsWith("find_")) return false;
      const resultProject = String(item.result?.project || "").trim();
      return !projectId || !resultProject || resultProject === projectId || String(item.job_id || "").includes(projectId);
    })
    .sort((a, b) => {
      const liveA = isLiveJob(a.item) ? 1 : 0;
      const liveB = isLiveJob(b.item) ? 1 : 0;
      return liveB - liveA || String(b.item.created_at || "").localeCompare(String(a.item.created_at || ""));
    });
  return String(candidates[0]?.runId || "").trim();
}

function artifactBelongsToCurrentFindRun(artifact: any, runId: string) {
  const path = String(artifact?.path || "");
  return Boolean(runId && (path.includes(`/runs/${runId}/`) || path.includes("/planning/finding/")));
}

function normalizeJobForState(job: any): Job | null {
  if (!job || typeof job !== "object") return null;
  const jobId = String(job.job_id || "").trim();
  if (!jobId) return null;
  const allowedStatuses = new Set<Job["status"]>(["queued", "running", "stale", "interrupted", "done", "blocked", "error", "cancelling", "cancelled", "preview_available", "needs_writing", "preview_pdf_blocked"]);
  const rawStatusText = String(job.status || "queued");
  const rawStatus = rawStatusText as Job["status"];
  const status = (allowedStatuses.has(rawStatus) || rawStatusText.startsWith("blocked") || rawStatusText.startsWith("stale")) ? rawStatus : "queued";
  const progress = job.progress && typeof job.progress === "object" ? {
    phase: String(job.progress.phase || ""),
    current: Number(job.progress.current || 0),
    total: Number(job.progress.total || 0),
    percent: Number(job.progress.percent || 0),
    message: String(job.progress.message || ""),
    read_progress: job.progress.read_progress && typeof job.progress.read_progress === "object" ? job.progress.read_progress : undefined,
  } : undefined;
  return {
    ...job,
    job_id: jobId,
    stage: String(job.stage || "unknown"),
    status,
    created_at: String(job.created_at || ""),
    logs: safeJobLogs(job),
    internal: Boolean(job.internal),
    display: String(job.display || ""),
    progress,
    run_id: String(job.run_id || ""),
  };
}

function mergeFindJobSnapshot(previous: Job | undefined, next: Job) {
  if (!previous || !isFindRunJob(next) || runIdFromJob(previous) !== runIdFromJob(next)) return next;
  const previousProgress = previous.result?.find_progress;
  return {
    ...next,
    result: {
      ...(next.result || {}),
      project: previous.result?.project || next.result?.project,
      find_progress: next.result?.find_progress || previousProgress,
    },
  };
}

function isConfirmedLiveProcess(row: any) {
  if (!row || typeof row !== "object") return false;
  const status = String(row.status || "").toLowerCase();
  const pid = row.pid !== undefined && row.pid !== null ? String(row.pid).trim() : "";
  if (row.process_alive === false || row.alive === false) return false;
  if (["queued", "cancelling"].includes(status)) return true;
  if (status === "running") return row.process_alive === true || row.alive === true || Boolean(pid && row.kind);
  return row.process_alive === true || row.alive === true;
}

function isInternalJob(job: any) {
  return Boolean(job?.internal)
    || job?.display === "hidden"
    || job?.stage === "safe-unblock"
    || String(job?.job_id || "").startsWith("safe-unblock_");
}

function isSyntheticProjectJob(job: any) {
  const jobId = String(job?.job_id || "");
  return jobId.startsWith("full-cycle-")
    || jobId.startsWith("agent-")
    || jobId === "controller-current-state"
    || jobId === "reference-reproduction-state";
}

function isWatchableWebJob(job: any) {
  if (!job || typeof job !== "object") return false;
  if (isInternalJob(job)) return false;
  const jobId = String(job.job_id || job.id || "").trim();
  if (!jobId || isSyntheticProjectJob(job)) return false;
  return ["queued", "running", "cancelling"].includes(String(job.status || ""));
}

function jobDedupeKey(job: Job) {
  const result = (job.result && typeof job.result === "object") ? job.result : {};
  const logPath = String(result.log_path || result.stdout_path || "").trim();
  const project = String(result.project || "").trim();
  const pid = String(result.pid || "").trim();
  const run = runIdFromJob(job);
  if (job.job_id) return `id:${job.job_id}`;
  return ["shape", job.stage, job.status, project, run, pid, logPath].join(":");
}

function dedupeJobsForState(items: Job[]) {
  const byKey = new Map<string, Job>();
  for (const item of items) {
    const key = jobDedupeKey(item);
    const previous = byKey.get(key);
    if (!previous || String(item.created_at || "") >= String(previous.created_at || "")) {
      byKey.set(key, item);
    }
  }
  return Array.from(byKey.values()).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

function visibleJobs(items: any) {
  return dedupeJobsForState(asArray(items)
    .map(normalizeJobForState)
    .filter((item): item is Job => Boolean(item))
    .filter((item) => !isInternalJob(item) && !isSyntheticProjectJob(item)));
}

function jobProjectId(job: any) {
  const result = job?.result && typeof job.result === "object" ? job.result : {};
  const paperStage = result.paper_stage && typeof result.paper_stage === "object" ? result.paper_stage : {};
  return String(job?.project || result.project || result.project_id || paperStage.project || "").trim();
}

function jobMatchesProject(job: any, projectId: string) {
  if (!projectId) return true;
  const project = jobProjectId(job);
  if (project) return project === projectId;
  const runId = runIdFromJob(job);
  const jobId = String(job?.job_id || "");
  return Boolean(runId && jobId.includes(projectId));
}

function jobsForProject(items: any, projectId: string) {
  return visibleJobs(items).filter((item) => jobMatchesProject(item, projectId));
}

function jobsForProjectResponse(items: any, projectId: string) {
  const scoped = visibleJobs(items).map((item) => {
    if (!projectId || jobProjectId(item)) return item;
    return { ...item, project: projectId };
  });
  return scoped.filter((item) => jobMatchesProject(item, projectId));
}

function runMatchesProject(run: RunInfo, projectId: string, pinnedRunIds: string[] = []) {
  if (!projectId) return true;
  const project = String(run.project || "").trim();
  if (project) return project === projectId;
  return pinnedRunIds.includes(run.run_id);
}

function syntheticJobsForProject(items: any, projectId: string) {
  const liveStatuses = new Set(["queued", "running", "cancelling", "blocked"]);
  return asArray(items)
    .map(normalizeJobForState)
    .filter((item): item is Job => Boolean(item))
    .filter((item) => {
      const resultProject = String(item.result?.project || "");
      return isSyntheticProjectJob(item)
        && liveStatuses.has(String(item.status || ""))
        && (!projectId || resultProject === projectId || item.job_id.includes(projectId));
    });
}


function joinText(value: any, fallback = "N/A") {
  const items = asArray(value).map((item) => String(item ?? "").trim()).filter(Boolean);
  return items.length ? items.join(", ") : fallback;
}

function displayName(row: any, fallback = "Unnamed") {
  return String(row?.name || row?.dataset || row?.repo || row?.id || row?.local_path || fallback);
}

const ARTIFACT_DISPLAY_NAMES: Record<string, { zh: string; en: string }> = {
  "find.md": { zh: "推荐文章", en: "Recommended papers" },
  "source_status.md": { zh: "来源状态", en: "Source status" },
  "find_results.json": { zh: "Find 结果审计", en: "Find result audit" },
  "read.md": { zh: "精读正文", en: "Reading brief" },
  "read_results.md": { zh: "精读结果", en: "Reading results" },
  "read_results.json": { zh: "精读结果审计", en: "Reading audit" },
  "idea.md": { zh: "想法正文", en: "Idea brief" },
  "ideas.json": { zh: "想法结果审计", en: "Idea audit" },
  "plan.md": { zh: "计划正文", en: "Plan brief" },
  "plans.md": { zh: "计划正文", en: "Plan brief" },
  "plans.json": { zh: "计划结果审计", en: "Plan audit" },
};

function artifactDisplayName(name: any, lang: Lang = "zh") {
  const raw = String(name || "").trim();
  const key = raw.toLowerCase();
  const mapped = ARTIFACT_DISPLAY_NAMES[key];
  return mapped ? mapped[lang] : raw;
}

function isPathLikeText(value: any): boolean {
  const text = String(value ?? "").trim();
  if (!text) return false;
  return /^\/[^\s]+/.test(text) || /^~\/[^\s]+/.test(text) || /^[A-Za-z]:[\\/][^\s]+/.test(text);
}

function publicArtifactPath(value: any, fallback = "", lang: Lang = "zh"): string {
  const text = String(value ?? "").trim();
  if (!text) return fallback;
  const runMatch = text.match(/(?:^|\/)runs\/(find_[^/]+)\/([^/]+)$/);
  if (runMatch) {
    const label = artifactDisplayName(runMatch[2], lang);
    return lang === "zh" ? `Find 产物：${runMatch[1]} / ${label}` : `Find artifact: ${runMatch[1]} / ${label}`;
  }
  if (text.includes("/web/backend/auto_research/") || text.includes("/framework/auto_research/") || text.includes("/modules/finding/") || text.includes("/modules/reading/")) return text.split("/").slice(-2).join("/") || fallback;
  if (isPathLikeText(text)) return text;
  return publicLogText(text, lang);
}

function displayMaybe(value: any, fallback = "N/A"): string {
  if (value && typeof value === "object") {
    if (Array.isArray(value)) return value.map((item) => displayMaybe(item, "")).filter(Boolean).join(", ") || fallback;
    const readable = value.label ?? value.name ?? value.title ?? value.status ?? value.issue ?? value.summary ?? value.path ?? value.url;
    return readable !== undefined ? displayMaybe(readable, fallback) : fallback;
  }
  if (typeof value === "string" && isPathLikeText(value)) return value.trim() || fallback;
  const text = publicLogText(value);
  return text || fallback;
}

function firstPresentValue(...values: any[]): any {
  for (const value of values) {
    if (value === undefined || value === null || value === "") continue;
    return value;
  }
  return undefined;
}

function firstNumericValue(...values: any[]): number {
  const value = firstPresentValue(...values);
  if (value === undefined) return 0;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function maxNumericValue(...values: any[]): number {
  const nums = values.map((value) => Number(value)).filter((value) => Number.isFinite(value));
  return nums.length ? Math.max(0, ...nums) : 0;
}

function normalizeText(value: any) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function statusBool(value: any) {
  return value ? "yes" : "no";
}

function runtimeDraftFromSummary(summary: ProjectSummary | null) {
  const runPreferences = (summary as any)?.run_preferences || {};
  const preferenceRuntime = runPreferences.runtime || {};
  const runtime = {
    ...(summary?.config?.runtime || {}),
    ...(summary?.state?.runtime?.runtime || {}),
    ...(summary?.runtime?.runtime || {}),
    ...preferenceRuntime,
  };
  const codingAgent = summary?.config?.coding_agent || {};
  return {
    source_bashrc: false,
    bashrc_path: "",
    node_bin: runtime.node_bin || "",
    claude_path: runtime.claude_path || codingAgent.claude_path_hint || "",
    management_python: runtime.management_python || runtime.python_executable || summary?.config?.python_executable || "",
    extra_path: Array.isArray(runtime.extra_path) ? runtime.extra_path.join(":") : String(runtime.extra_path || ""),
  };
}

function environmentDraftFromSummary(summary: ProjectSummary | null) {
  const runPreferences = (summary as any)?.run_preferences || {};
  const preferenceRuntime = runPreferences.runtime || {};
  const summaryRuntime = {
    ...(summary?.config?.runtime || {}),
    ...(summary?.state?.runtime?.runtime || {}),
    ...(summary?.runtime?.runtime || {}),
    ...preferenceRuntime,
  };
  const environment = summary?.config?.environment || runPreferences.environment || {};
  return {
    conda_env: summaryRuntime.conda_env || runPreferences.conda_env || summary?.config?.conda_env || "",
    conda_base: summaryRuntime.conda_base || environment.conda_base_hint || "",
    experiment_python: summaryRuntime.experiment_python || environment.experiment_python || "",
    python_executable: summaryRuntime.management_python || summaryRuntime.python_executable || summary?.config?.python_executable || "",
  };
}

function environmentDraftHasAnyValue(draft: Record<string, any>) {
  return Boolean(
    String(draft?.conda_env || "").trim()
    || String(draft?.conda_base || "").trim()
    || String(draft?.experiment_python || "").trim()
  );
}

function fillEmptyEnvironmentDraftFromSummary(current: Record<string, any>, summary: ProjectSummary | null) {
  const next = environmentDraftFromSummary(summary);
  if (!environmentDraftHasAnyValue(next)) return current;
  return {
    ...current,
    conda_env: next.conda_env || current?.conda_env || "",
    conda_base: next.conda_base || current?.conda_base || "",
    experiment_python: next.experiment_python || current?.experiment_python || "",
    python_executable: next.python_executable || current?.python_executable || "",
  };
}

function derivedCondaPython(condaBase: any, condaEnv: any) {
  const envName = String(condaEnv || "").trim();
  if (!envName) return "";
  const base = String(condaBase || "").trim().replace(/\/+$/, "");
  return base ? `${base}/envs/${envName}/bin/python` : `conda env: ${envName}`;
}

const HIDDEN_RUN_ARTIFACTS = new Set(["literature_tool_packet.md", "literature_tool_packet.json"]);
const FIND_ARTIFACT_NAMES = new Set(["find.md", "source_status.md", "find_results.json"]);
const READ_ARTIFACT_NAMES = new Set(["read.md", "read_results.md", "read_results.json"]);
const IDEA_ARTIFACT_NAMES = new Set(["idea.md", "ideas.json"]);
const PLAN_ARTIFACT_NAMES = new Set(["plans.md", "plan.md", "plans.json"]);
const EXPERIMENT_ARTIFACT_RE = /(experiment|reproduction|reference|trajectory|gate|audit|record|log|metrics|result)/i;
const PAPER_ARTIFACT_RE = /(paper|latex|tex|pdf|submission|camera|figure)/i;

function artifactVisibleForTab(artifact: any, tab: Tab) {
  const name = String(artifact?.name || "").toLowerCase();
  const path = String(artifact?.path || "").toLowerCase();
  const haystack = `${name} ${path}`;
  if (tab === "find") return FIND_ARTIFACT_NAMES.has(name);
  if (tab === "read") return READ_ARTIFACT_NAMES.has(name);
  if (tab === "ideas") return IDEA_ARTIFACT_NAMES.has(name) || name.includes("idea");
  if (tab === "plan") return PLAN_ARTIFACT_NAMES.has(name) || name.includes("plan");
  if (tab === "environment") return /(environment|repo|base|data|conda|runtime|reference|reproduction)/i.test(haystack);
  if (tab === "experiment") return EXPERIMENT_ARTIFACT_RE.test(haystack) && !FIND_ARTIFACT_NAMES.has(name);
  if (tab === "paperWrite") return PAPER_ARTIFACT_RE.test(haystack);
  return true;
}

function artifactListSignature(items: any[]) {
  return asArray(items).map((artifact: any) => {
    const content = artifact?.content;
    const contentSize = typeof content === "string"
      ? content.length
      : JSON.stringify(content ?? "").length;
    return [artifact?.name || "", artifact?.path || "", artifact?.kind || "", contentSize].join(":");
  }).join("|");
}

type ClaudePanelStage = "environment" | "experiment" | "paper";

function normalizeClaudePanelStage(value: any): ClaudePanelStage | "" {
  const raw = String(value || "").trim().toLowerCase().replace(/_/g, "-");
  if (!raw) return "";
  if (raw === "paperwrite" || raw === "paper-write" || raw === "paper-writing" || raw.startsWith("paper") || raw.includes("writing")) return "paper";
  if (raw === "environment" || raw === "env" || raw.startsWith("environment") || raw.includes("repo-env")) return "environment";
  if (raw === "experiment" || raw.startsWith("experiment") || raw.includes("trajectory") || raw.includes("autonomous")) return "experiment";
  return "";
}

function isClaudeGuidanceJob(job: any) {
  const result = job?.result && typeof job.result === "object" ? job.result : {};
  const haystack = [job?.stage, result.raw_stage, result.action, result.kind, job?.job_id].map((item) => String(item || "").toLowerCase()).join(" ");
  return haystack.includes("claude-message")
    || haystack.includes("agent-guidance")
    || haystack.includes("environment-chat")
    || haystack.includes("experimenting-chat")
    || haystack.includes("experiment-chat")
    || haystack.includes("writing-chat")
    || haystack.includes("paper-chat");
}

function jobPanelStage(job: any): ClaudePanelStage | "" {
  const result = job?.result && typeof job.result === "object" ? job.result : {};
  const progress = job?.progress && typeof job.progress === "object" ? job.progress : {};
  for (const candidate of [result.panel_stage, result.requested_stage, result.stage, progress.panel_stage, progress.requested_stage]) {
    const normalized = normalizeClaudePanelStage(candidate);
    if (normalized) return normalized;
  }
  return "";
}

function jobMatchesClaudePanelStage(job: any, stage: ClaudePanelStage) {
  if (!isClaudeGuidanceJob(job)) return false;
  if (!["queued", "running", "cancelling", "done", "blocked", "error"].includes(String(job?.status || ""))) return false;
  return jobPanelStage(job) === stage;
}

function preferredProjectId(projects: Project[], jobs: any[]) {
  const liveProject = projects.find((project) => syntheticJobsForProject(jobs, project.id).length > 0);
  if (liveProject) return liveProject.id;
  const storedProject = localStorage.getItem("selected_project") || "";
  if (storedProject && projects.some((project) => project.id === storedProject)) return storedProject;
  return projects[0]?.id || "";
}

function replaceUrlWithoutProject(params: URLSearchParams) {
  params.delete("project");
  const nextSearch = params.toString();
  const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash || ""}`;
  if (nextUrl !== `${window.location.pathname}${window.location.search}${window.location.hash || ""}`) {
    window.history.replaceState(null, "", nextUrl);
  }
}

function runHasReadableStages(run: RunInfo) {
  const stages = asArray(run.stages).map((stage) => String(stage || "").toLowerCase());
  return ["find", "read", "idea", "plan"].some((stage) => stages.includes(stage));
}

function currentFindRunIdFromSummary(summary: any) {
  return String(
    summary?.current_find_pipeline?.run_id
    || summary?.state?.current_find_pipeline?.run_id
    || summary?.literature_survey?.run_id
    || summary?.state?.literature_survey?.run_id
    || summary?.stages?.find?.run_id
    || summary?.state?.stages?.find?.run_id
    || summary?.config?.default_find_run_id
    || summary?.human_supervision?.main_route?.find_run_id
    || summary?.state?.human_supervision?.main_route?.find_run_id
    || summary?.main_route?.find_run_id
    || "",
  ).trim();
}

function projectReadPaperLimit(summary: any, fallback = DEFAULT_READ_PAPER_LIMIT) {
  const candidates = [
    summary?.run_preferences?.max_read_papers,
    summary?.config?.max_read_papers,
    summary?.state?.run_preferences?.max_read_papers,
    summary?.state?.config?.max_read_papers,
  ];
  for (const candidate of candidates) {
    const numeric = Math.trunc(Number(candidate));
    if (Number.isFinite(numeric) && numeric > 0) return numeric;
  }
  return fallback;
}

function defaultRunId(runData: RunInfo[], preferredFindRunId = "") {
  if (preferredFindRunId && runData.some((run) => !run.readonly && run.run_id === preferredFindRunId)) return preferredFindRunId;
  const readableFind = runData.find((run) => !run.readonly && run.run_id.startsWith("find_") && runHasReadableStages(run));
  const readableRun = runData.find((run) => !run.readonly && runHasReadableStages(run));
  const ordinaryFind = runData.find((run) => !run.readonly && run.run_id.startsWith("find_"));
  return readableFind?.run_id || readableRun?.run_id || ordinaryFind?.run_id || runData.find((run) => !run.readonly)?.run_id || runData[0]?.run_id || "";
}

function latestFindRunId(runData: RunInfo[]) {
  return runData.find((run) => !run.readonly && run.run_id.startsWith("find_"))?.run_id || "";
}

function hasLiveFindJob(jobData: any) {
  return asArray(jobData)
    .map(normalizeJobForState)
    .some((item) => Boolean(item) && String(item?.stage || "") === "find" && isLiveJob(item));
}

function defaultRunIdForJobs(runData: RunInfo[], jobData: any, preferredFindRunId = "") {
  const activeFindRunId = activeFindRunIdFromJobs(jobData);
  if (activeFindRunId && runData.some((run) => run.run_id === activeFindRunId)) return activeFindRunId;
  if (preferredFindRunId && runData.some((run) => !run.readonly && run.run_id === preferredFindRunId)) return preferredFindRunId;
  if (hasLiveFindJob(jobData)) return latestFindRunId(runData) || defaultRunId(runData, preferredFindRunId);
  return defaultRunId(runData, preferredFindRunId);
}

function runExists(runData: RunInfo[], id: string) {
  return Boolean(id && runData.some((run) => !run.readonly && run.run_id === id));
}

function hasUnsavedLLMConfigDraft(config: Config) {
  if (String(config.api_key || "").trim()) return true;
  return Object.values(config.llm_roles || {}).some((roleConfig: any) => String(roleConfig?.api_key || "").trim());
}

function initialTabFromLocation(): Tab {
  const allowed = new Set<Tab>(["find", "read", "ideas", "plan", "environment", "experiment", "paperWrite"]);
  const params = new URLSearchParams(window.location.search);
  const raw = params.get("tab") || params.get("stage") || localStorage.getItem("active_tab") || "find";
  const normalized = raw === "paper" || raw === "paper-write" || raw === "paper_write" ? "paperWrite" : raw === "idea" ? "ideas" : raw;
  return allowed.has(normalized as Tab) ? normalized as Tab : "find";
}

function tabUrlValue(tab: Tab) {
  return tab;
}

function TasteApp({ account, onLogout }: { account: AuthUser; onLogout: () => void }) {
  const [tab, setTabState] = useState<Tab>(() => initialTabFromLocation());
  const [lang, setLang] = useState<Lang>(() => (localStorage.getItem("ui_lang") as Lang) || (localStorage.getItem("auto_research_lang") as Lang) || "zh");
  const [config, setConfig] = useState<Config>(DEFAULT_CONFIG);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [showAllRuns, setShowAllRuns] = useState(false);
  const [researchProjects, setProjects] = useState<Project[]>([]);
  const [researchProjectsLoaded, setProjectsLoaded] = useState(false);
  const [researchProject, setProjectId] = useState(() => localStorage.getItem("selected_project") || "");
  const [researchSummary, setProjectSummary] = useState<ProjectSummary | null>(null);
  const [researchProjectLoading, setProjectLoading] = useState(false);
  const [newProjectId, setNewProjectId] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [researchProjectMessage, setProjectMessage] = useState("");
  const [researchPrompt, setPrompt] = useState("");
  const [researchTopic, setTopic] = useState("");
  const [researchVenue, setVenue] = useState("");
  const [researchTitle, setTitle] = useState("");
  const [researchResearchInterest, setResearchResearchInterest] = useState("");
  const [researchResearcherProfile, setResearchResearcherProfile] = useState("");
  const [researchIterations, setIterations] = useState(1);
  const [researchMaxLaunches, setMaxLaunches] = useState(1);
  const [researchExecutePlan, setExecutePlan] = useState(false);
  const [researchPrepareEnv, setPrepareEnv] = useState(false);
  const [researchRealBootstrapEnv, setRealBootstrapEnv] = useState(true);
  const [researchSkipPaper, setSkipPaper] = useState(false);
  const [researchAutoInstallLatex, setAutoInstallLatex] = useState(false);
  const [activeProjectArtifact, setActiveProjectArtifact] = useState("");
  const [agentGuidanceMessages, setAgentGuidanceMessages] = useState<Record<string, string>>({});
  const [agentGuidanceMessage, setAgentGuidanceMessage] = useState("");
  const [claudeFullResponses, setClaudeFullResponses] = useState<Record<string, { loading?: boolean; error?: string; data?: any }>>({});
  const [researchRuntimeDraft, setResearchRuntimeDraft] = useState<Record<string, any>>({});
  const [researchRuntimeSaving, setResearchRuntimeSaving] = useState(false);
  const [researchRuntimeMessage, setResearchRuntimeMessage] = useState("");
  const [researchEnvDraft, setResearchEnvDraft] = useState<Record<string, any>>({});
  const [researchEnvSaving, setResearchEnvSaving] = useState(false);
  const [researchEnvMessage, setResearchEnvMessage] = useState("");
  const [rawProjectArtifacts, setRawProjectArtifacts] = useState<Record<string, boolean>>({});
  const [runId, setRunId] = useState("");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [lastVisibleRunArtifactsByTab, setLastVisibleRunArtifactsByTab] = useState<Partial<Record<Tab, ArtifactPanelSnapshot>>>({});
  const [currentFindArtifacts, setCurrentFindArtifacts] = useState<Artifact[]>([]);
  const [activeFindArtifacts, setActiveFindArtifacts] = useState<Artifact[]>([]);
  const [currentFindArtifactsLoading, setCurrentFindArtifactsLoading] = useState(false);
  const [runArtifactsLoading, setRunArtifactsLoading] = useState(false);
  const [selectedVenues, setSelectedVenues] = useState<string[]>([]);
  const [selectedVenueYears, setSelectedVenueYears] = useState<Record<string, number[]>>(() => defaultVenueYearMap());
  const [years, setYears] = useState(String(DEFAULT_FIND_YEAR));
  const [venueQuery, setVenueQuery] = useState("");
  const [showAllAvailableVenues, setShowAllAvailableVenues] = useState(false);
  const [includeArxiv, setIncludeArxiv] = useState(false);
  const [includeBiorxiv, setIncludeBiorxiv] = useState(false);
  const [includeHf, setIncludeHf] = useState(false);
  const [includeGithub, setIncludeGithub] = useState(false);
  const [includeNature, setIncludeNature] = useState(false);
  const [includeScience, setIncludeScience] = useState(false);
  const [selectedPapers, setSelectedPapers] = useState<string[]>([]);
  const [readPaperLimit, setReadPaperLimit] = useState(DEFAULT_READ_PAPER_LIMIT);
  const [readPaperLimitDirty, setReadPaperLimitDirty] = useState(false);
  const [readPaperLimitMessage, setReadPaperLimitMessage] = useState("");
  const [planIdeaIds, setPlanIdeaIds] = useState<string[]>([]);
  const [planRepairRounds, setPlanRepairRounds] = useState(3);
  const [polishRounds, setPolishRounds] = useState<Record<string, number>>({});
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [planMarkdownDraft, setPlanMarkdownDraft] = useState("");
  const [planMarkdownDirty, setPlanMarkdownDirty] = useState(false);
  const [planMarkdownSaving, setPlanMarkdownSaving] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [findLaunchPending, setFindLaunchPending] = useState(false);
  const [ideaStatusSaving, setIdeaStatusSaving] = useState<Record<string, string>>({});
  const [ideaEditorDrafts, setIdeaEditorDrafts] = useState<Record<string, IdeaEditorDraft>>({});
  const [ideaEditorSaving, setIdeaEditorSaving] = useState<Record<string, boolean>>({});
  const [ideaMarkdownEditing, setIdeaMarkdownEditing] = useState(false);
  const [ideaMarkdownDraft, setIdeaMarkdownDraft] = useState("");
  const [ideaMarkdownSaving, setIdeaMarkdownSaving] = useState(false);
  const activeProjectRef = useRef("");
  const projectSummaryLoadedAtRef = useRef<Record<string, number>>({});
  const projectSummaryInFlightRef = useRef("");
  const watchedJobIdsRef = useRef<Set<string>>(new Set());
  const jobsRefreshInFlightRef = useRef(false);
  const jobsLoadedAtRef = useRef<Record<string, number>>({});
  const runLoadSeq = useRef(0);
  const userSelectedRunRef = useRef(false);
  const currentFindArtifactsInFlightRef = useRef("");
  const currentFindArtifactsRunRef = useRef("");
  const activeFindArtifactsInFlightRef = useRef("");
  const fallbackRunArtifactsInFlightRef = useRef("");
  const fallbackRunArtifactCacheRef = useRef<Record<string, Artifact[]>>({});
  const frontendVersionRef = useRef("");
  const [error, setError] = useState("");
  const [savingConfig, setSavingConfig] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [researchProjectConfigSaving, setProjectConfigSaving] = useState(false);
  const [researchProjectConfigMessage, setProjectConfigMessage] = useState("");
  const [llmProbeLoading, setLLMProbeLoading] = useState(false);
  const [llmProbeResult, setLLMProbeResult] = useState<{ ok: boolean; error: string; summary?: Record<string, any> } | null>(null);
  const [checkingVenues, setCheckingVenues] = useState(false);
  const [venueHealth, setVenueHealth] = useState<Record<string, { ok: boolean; message: string; source_adapter: string; sample_count: number }>>({});
  const [rawArtifacts, setRawArtifacts] = useState<Record<string, boolean>>({});
  const [activeArtifact, setActiveArtifact] = useState("");
  const [emailReceiversOverride, setEmailReceiversOverride] = useState("");
  const [emailSubject, setEmailSubject] = useState("");
  const t = TEXT[lang];
  const unsavedLLMConfigDraft = hasUnsavedLLMConfigDraft(config);

  function setTab(nextTab: Tab) {
    setTabState(nextTab);
    localStorage.setItem("active_tab", nextTab);
    const params = new URLSearchParams(window.location.search);
    params.set("tab", tabUrlValue(nextTab));
    replaceUrlWithoutProject(params);
  }

  async function refreshJobsForProject(projectId: string, options: { force?: boolean; isCurrent?: () => boolean } = {}) {
    const activeId = String(projectId || "");
    if (!activeId) {
      setJobsLoaded(true);
      return;
    }
    if (options.isCurrent && !options.isCurrent()) return;
    if (!options.force && Date.now() - (jobsLoadedAtRef.current[activeId] || 0) < 5000) {
      setJobsLoaded(true);
      return;
    }
    if (jobsRefreshInFlightRef.current) return;
    jobsRefreshInFlightRef.current = true;
    try {
      const jobData = await getJobs(activeId);
      if ((options.isCurrent && !options.isCurrent()) || activeProjectRef.current !== activeId) return;
      const visibleJobData = jobsForProjectResponse(jobData, activeId);
      setJobs((prev) => visibleJobData.map((item) => mergeFindJobSnapshot(prev.find((current) => current.job_id === item.job_id), item)));
      jobsLoadedAtRef.current[activeId] = Date.now();
      setJobsLoaded(true);
      visibleJobData.filter(isWatchableWebJob).forEach((item) => watchExistingJob(item.job_id));
    } catch {
      setJobsLoaded(true);
    } finally {
      jobsRefreshInFlightRef.current = false;
    }
  }

  useEffect(() => {
    void bootstrap();
  }, []);


  useEffect(() => {
    let cancelled = false;
    const refreshIfFrontendChanged = async () => {
      try {
        const info = await getFrontendVersion();
        const version = String(info.version || "");
        if (!version || cancelled) return;
        if (!frontendVersionRef.current) {
          frontendVersionRef.current = version;
          return;
        }
        if (frontendVersionRef.current !== version) {
          window.location.reload();
        }
      } catch {
        // Version checks are a cache-safety guard only; transient failures must not affect rendering.
      }
    };
    void refreshIfFrontendChanged();
    const timer = window.setInterval(refreshIfFrontendChanged, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("project")) {
      replaceUrlWithoutProject(params);
    }
  }, []);

  useEffect(() => {
    localStorage.setItem("ui_lang", lang);
  }, [lang]);

  useEffect(() => {
    setClaudeFullResponses({});
  }, [researchProject]);

  useEffect(() => {
    if (!researchProject) return;
    activeProjectRef.current = researchProject;
    let cancelled = false;
    const refreshProject = async () => {
      const projectId = researchProject;
      if (Date.now() - (projectSummaryLoadedAtRef.current[projectId] || 0) < 5000) return;
      if (projectSummaryInFlightRef.current === projectId) return;
      projectSummaryInFlightRef.current = projectId;
      try {
        const summary = await getProject(projectId, { compact: true });
        if (cancelled || activeProjectRef.current !== projectId) return;
        projectSummaryLoadedAtRef.current[projectId] = Date.now();
        setProjectSummary(summary);
        setResearchEnvDraft((prev) => fillEmptyEnvironmentDraftFromSummary(prev, summary));
        setError((prev) => String(prev || "").includes("Failed to fetch") ? "" : prev);
      } catch {
        // Keep the last visible state; transient refresh failures should not blank the page.
      } finally {
        if (projectSummaryInFlightRef.current === projectId) projectSummaryInFlightRef.current = "";
      }
    };
    const timer = window.setInterval(refreshProject, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [researchProject]);

  useEffect(() => {
    if (tab !== "environment" || !researchProject) return;
    const projectId = researchProject;
    let cancelled = false;
    activeProjectRef.current = projectId;
    const refreshEnvironmentSummary = async () => {
      try {
        const summary = await getProject(projectId, { compact: true });
        if (cancelled || activeProjectRef.current !== projectId) return;
        projectSummaryLoadedAtRef.current[projectId] = Date.now();
        setProjectSummary(summary);
        setResearchRuntimeDraft((prev) => ({ ...runtimeDraftFromSummary(summary), ...prev }));
        setResearchEnvDraft((prev) => fillEmptyEnvironmentDraftFromSummary(prev, summary));
      } catch {
        // Keep the last visible state; the normal project poller will retry.
      }
    };
    void refreshEnvironmentSummary();
    return () => {
      cancelled = true;
    };
  }, [tab, researchProject]);

  useEffect(() => {
    if (!researchSummary || readPaperLimitDirty) return;
    if (researchSummary.project && researchSummary.project !== researchProject) return;
    setReadPaperLimit(projectReadPaperLimit(researchSummary));
  }, [researchProject, researchSummary, readPaperLimitDirty]);

  useEffect(() => {
    let cancelled = false;
    const refreshJobs = async () => {
      const projectId = activeProjectRef.current || researchProject || "";
      await refreshJobsForProject(projectId, { isCurrent: () => !cancelled });
    };
    const firstTimer = window.setTimeout(refreshJobs, 2500);
    const timer = window.setInterval(refreshJobs, 15000);
    return () => {
      cancelled = true;
      window.clearTimeout(firstTimer);
      window.clearInterval(timer);
    };
  }, [researchProject]);

  function applyProjectDrafts(summary: ProjectSummary, fallbackProject?: Project) {
    const topic = summary.config?.topic || fallbackProject?.topic || "";
    const runPreferences = (summary as any).run_preferences || {};
    const prompt = runPreferences.user_prompt || "";
    const selection = runPreferences.default_find_selection || (summary as any).default_find_selection || summary.config?.default_find_selection || {};
    setTopic(topic);
    setPrompt(prompt);
    setTitle(runPreferences.paper?.title || "");
    setVenue(runPreferences.target_venue || runPreferences.venue || summary.human_supervision?.target_venue || summary.config?.target_venue || summary.config?.venue || "");
    setResearchResearchInterest(runPreferences.research_interest || "");
    setResearchResearcherProfile(runPreferences.researcher_profile || "");
    const nextSelectedVenues: string[] = Array.isArray(selection.venue_ids) && selection.venue_ids.length
      ? Array.from(new Set<string>(selection.venue_ids.map((item: any) => String(item || "").trim()).filter(Boolean)))
      : [];
    setSelectedVenues(nextSelectedVenues);
    setSelectedVenueYears(venueYearMapFromSelection(selection, nextSelectedVenues));
    if (Array.isArray(selection.years) && selection.years.length) {
      setYears(normalizeSelectedYears(selection.years).join(", "));
    } else {
      setYears(String(DEFAULT_FIND_YEAR));
    }
    setIncludeArxiv(Boolean(selection.include_arxiv));
    setIncludeBiorxiv(Boolean(selection.include_biorxiv));
    setIncludeHf(Boolean(selection.include_huggingface));
    setIncludeGithub(Boolean(selection.include_github));
    setIncludeNature(Boolean(selection.include_nature));
    setIncludeScience(Boolean(selection.include_science));
    setReadPaperLimit(projectReadPaperLimit(summary));
    setReadPaperLimitDirty(false);
    setReadPaperLimitMessage("");
    setResearchRuntimeDraft(runtimeDraftFromSummary(summary));
    setResearchEnvDraft(environmentDraftFromSummary(summary));
  }

  async function bootstrap() {
    try {
      const [cfg] = await Promise.all([getConfig(), getConfigMeta()]);
      setConfig(cfg);
      const [venueData, researchProjectData] = await Promise.all([getVenues(), getProjects()]);
      setError("");
      setVenues(venueData);
      setProjects(researchProjectData);
      setProjectsLoaded(true);
      const initialProjectId = preferredProjectId(researchProjectData, []);
      const initialProject = researchProjectData.find((project) => project.id === initialProjectId) || researchProjectData[0];
      if (initialProject) {
        activeProjectRef.current = initialProjectId;
        setProjectId(initialProjectId);
        localStorage.setItem("selected_project", initialProjectId);
        projectSummaryInFlightRef.current = initialProjectId;
        let summary: ProjectSummary;
        try {
          summary = await getProject(initialProjectId, { compact: true });
        } finally {
          if (projectSummaryInFlightRef.current === initialProjectId) projectSummaryInFlightRef.current = "";
        }
        if (activeProjectRef.current !== initialProjectId) return;
        projectSummaryLoadedAtRef.current[initialProjectId] = Date.now();
        setProjectSummary(summary);
        setError("");
        applyProjectDrafts(summary, initialProject);
        setActiveProjectArtifact(asArray(summary.artifacts)[0]?.name || "");
        const summaryFindRunId = currentFindRunIdFromSummary(summary);
        if (summaryFindRunId) {
          setRunId(summaryFindRunId);
        }
        void getRuns(initialProjectId).then((projectRunData) => {
          if (activeProjectRef.current !== initialProjectId) return;
          setRuns(projectRunData);
          if (!summaryFindRunId) {
            const initialRunId = defaultRunIdForJobs(projectRunData, jobs, "");
            if (initialRunId) void loadRun(initialRunId);
          }
        }).catch(() => {});
      } else {
        setJobsLoaded(true);
      }
    } catch (err) {
      setProjectsLoaded(true);
      setJobsLoaded(true);
      setError(String(err));
    }
  }

  async function loadRun(id: string, options: { clear?: boolean; loading?: boolean; userInitiated?: boolean } = {}) {
    const seq = ++runLoadSeq.current;
    if (options.userInitiated) userSelectedRunRef.current = true;
    const showLoading = options.loading !== false;
    if (showLoading) setRunArtifactsLoading(true);
    setRunId(id);
    setActiveArtifact("");
    setRawArtifacts({});
    if (options.clear !== false) setArtifacts([]);
    try {
      const data = await getArtifacts(id);
      if (seq !== runLoadSeq.current) return;
      setArtifacts(data.artifacts);
    } finally {
      if (seq === runLoadSeq.current && showLoading) setRunArtifactsLoading(false);
    }
  }

  async function loadCurrentFindArtifacts(id: string, options: { loading?: boolean; scope?: CurrentFindArtifactScope } = {}) {
    if (!id) {
      currentFindArtifactsInFlightRef.current = "";
      currentFindArtifactsRunRef.current = "";
      setCurrentFindArtifacts([]);
      setActiveArtifact("");
      setRawArtifacts({});
      return;
    }
    if (currentFindArtifactsRunRef.current !== id) {
      currentFindArtifactsRunRef.current = id;
      setCurrentFindArtifacts([]);
    }
    const requestKey = `${researchProject}:${id}:${options.scope || "all"}`;
    if (currentFindArtifactsInFlightRef.current === requestKey) return;
    setActiveArtifact("");
    setRawArtifacts({});
    currentFindArtifactsInFlightRef.current = requestKey;
    const showLoading = options.loading !== false;
    if (showLoading) setCurrentFindArtifactsLoading(true);
    try {
      const data = await getArtifacts(id, { light: true, scope: options.scope, project: researchProject || undefined });
      if (options.scope) {
        const scopeNames = new Set(CURRENT_FIND_SCOPE_ARTIFACT_NAMES[options.scope]);
        setCurrentFindArtifacts((prev) => [...prev.filter((artifact) => !scopeNames.has(artifact.name)), ...data.artifacts]);
      } else {
        setCurrentFindArtifacts(data.artifacts);
      }
    } catch {
      if (!options.scope) setCurrentFindArtifacts([]);
    } finally {
      if (currentFindArtifactsInFlightRef.current === requestKey) currentFindArtifactsInFlightRef.current = "";
      if (showLoading) setCurrentFindArtifactsLoading(false);
    }
  }

  async function loadActiveFindArtifacts(id: string) {
    if (!id) {
      activeFindArtifactsInFlightRef.current = "";
      setActiveFindArtifacts([]);
      return;
    }
    if (activeFindArtifactsInFlightRef.current === id) return;
    activeFindArtifactsInFlightRef.current = id;
    try {
      const project = activeProjectRef.current || researchProject || "";
      const data = await getArtifacts(id, { light: true, scope: "find", project: project || undefined });
      setActiveFindArtifacts(data.artifacts);
    } catch {
      setActiveFindArtifacts([]);
    } finally {
      if (activeFindArtifactsInFlightRef.current === id) activeFindArtifactsInFlightRef.current = "";
    }
  }
  async function loadProject(id: string, options: { resetDrafts?: boolean } = {}) {
    const resetDrafts = options.resetDrafts !== false;
    activeProjectRef.current = id;
    runLoadSeq.current += 1;
    setProjectId(id);
    localStorage.setItem("selected_project", id);
    setProjectLoading(true);
    setSelectedPapers([]);
    setPlanIdeaIds([]);
    setSelectedPlanId("");
    setPlanMarkdownDraft("");
    setPlanMarkdownDirty(false);
    setActiveProjectArtifact("");
    setLastVisibleRunArtifactsByTab({});
    fallbackRunArtifactsInFlightRef.current = "";
    fallbackRunArtifactCacheRef.current = {};
    try {
      projectSummaryInFlightRef.current = id;
      const summary = await getProject(id, { compact: true });
      if (activeProjectRef.current !== id) return;
      projectSummaryLoadedAtRef.current[id] = Date.now();
      setProjectSummary(summary);
      setError("");
      const projectMeta = researchProjects.find((project) => project.id === id);
      if (resetDrafts) {
        applyProjectDrafts(summary, projectMeta);
      }
      setActiveProjectArtifact(asArray(summary.artifacts)[0]?.name || "");
      const summaryFindRunId = currentFindRunIdFromSummary(summary);
      if (summaryFindRunId) {
        void refreshRuns(summaryFindRunId, id).catch(() => {});
      }
    } finally {
      if (projectSummaryInFlightRef.current === id) projectSummaryInFlightRef.current = "";
      if (activeProjectRef.current === id) {
        setProjectLoading(false);
      }
    }
  }


  async function refreshRuns(nextRunId?: string, projectId = activeProjectRef.current || researchProject) {
    const runData = await getRuns(projectId || undefined);
    setRuns(runData);
    if (nextRunId) {
      await loadRun(nextRunId);
    }
  }

  function currentFindSelection() {
    const pairs = venueYearPairs(selectedVenues, selectedVenueYears);
    return {
      venue_ids: selectedVenues,
      years: yearsFromVenueYearMap(selectedVenues, selectedVenueYears),
      venue_years: pairs,
      include_arxiv: includeArxiv,
      include_biorxiv: includeBiorxiv,
      include_huggingface: includeHf,
      include_github: includeGithub,
      include_nature: includeNature,
      include_science: includeScience,
    };
  }

  function configWithCurrentFindSelection(nextConfig = config): Config {
    return {
      ...nextConfig,
      research_interest: researchProject ? researchResearchInterest : nextConfig.research_interest,
      researcher_profile: researchProject ? researchResearcherProfile : nextConfig.researcher_profile,
      default_find_selection: currentFindSelection(),
    };
  }


  function savedSecretHint(saved?: boolean) {
    return saved ? (lang === "zh" ? "已保存，留空则继续使用；输入新值会替换。" : "Saved; leave blank to keep it, enter a new value to replace it.") : "";
  }

  function updateConfig<K extends keyof Config>(key: K, value: Config[K]) {
    setConfig((prev) => ({ ...prev, [key]: value }));
    setSaveMessage("");
  }

  function applyStandardFindDefaults() {
    setConfig((prev) => ({ ...prev, ...STANDARD_FIND_DEFAULTS }));
    setSaveMessage(t.findStandardDefaultsApplied);
  }

  function toggleNatureJournal(slug: string, checked: boolean) {
    const current = config.nature_journals || [];
    updateConfig("nature_journals", checked ? [...new Set([...current, slug])] : current.filter((item) => item !== slug));
  }

  function toggleNaturePreset(journals: string[], checked: boolean) {
    const current = config.nature_journals || [];
    if (checked) {
      updateConfig("nature_journals", [...new Set([...current, ...journals])]);
      return;
    }
    const remove = new Set(journals);
    updateConfig("nature_journals", current.filter((item) => !remove.has(item)));
  }

  function naturePresetState(journals: string[]) {
    const selected = new Set(config.nature_journals || []);
    const count = journals.filter((journal) => selected.has(journal)).length;
    return {
      checked: count === journals.length,
      partial: count > 0 && count < journals.length,
      count,
    };
  }

  function toggleScienceJournal(slug: string, checked: boolean) {
    const current = config.science_journals || [];
    updateConfig("science_journals", checked ? [...new Set([...current, slug])] : current.filter((item) => item !== slug));
  }

  function toggleSciencePreset(journals: string[], checked: boolean) {
    const current = config.science_journals || [];
    if (checked) {
      updateConfig("science_journals", [...new Set([...current, ...journals])]);
      return;
    }
    const remove = new Set(journals);
    updateConfig("science_journals", current.filter((item) => !remove.has(item)));
  }

  function sciencePresetState(journals: string[]) {
    const selected = new Set(config.science_journals || []);
    const count = journals.filter((journal) => selected.has(journal)).length;
    return {
      checked: count === journals.length,
      partial: count > 0 && count < journals.length,
      count,
    };
  }

  function updateEmailConfig(key: string, value: string | number | boolean | string[]) {
    setConfig((prev) => ({
      ...prev,
      email: {
        ...(prev.email || DEFAULT_CONFIG.email),
        [key]: value,
      },
    }));
    setSaveMessage("");
  }

  function updateRuntimeDraft(key: string, value: any) {
    setResearchRuntimeDraft((prev) => ({ ...prev, [key]: value }));
    setResearchRuntimeMessage("");
  }

  function updateEnvDraft(key: string, value: any) {
    setResearchEnvDraft((prev) => ({ ...prev, [key]: value }));
    setResearchEnvMessage("");
  }

  async function saveRuntimeConfig() {
    if (!researchProject) return;
    try {
      setResearchRuntimeSaving(true);
      setError("");
      const payload = {
        ...researchRuntimeDraft,
        extra_path: String(researchRuntimeDraft.extra_path || "").split(/[:,]/).map((item) => item.trim()).filter(Boolean),
      };
      await saveRuntime(researchProject, payload);
      const summary = await getProject(researchProject);
      setProjectSummary(summary);
      setResearchRuntimeDraft(runtimeDraftFromSummary(summary));
      setResearchRuntimeMessage(t.runtimeSaved);
    } catch (err) {
      setError(String(err));
    } finally {
      setResearchRuntimeSaving(false);
    }
  }

  async function loadClaudeFullResponse(receiptKey: string, stage = "") {
    if (!researchProject || !receiptKey) return;
    const projectId = researchProject;
    setClaudeFullResponses((prev) => ({ ...prev, [receiptKey]: { ...(prev[receiptKey] || {}), loading: true, error: "" } }));
    try {
      const data = await getClaudeLatestResponse(projectId, stage);
      if (activeProjectRef.current && activeProjectRef.current !== projectId) return;
      setClaudeFullResponses((prev) => ({ ...prev, [receiptKey]: { loading: false, error: "", data } }));
    } catch (err) {
      setClaudeFullResponses((prev) => ({
        ...prev,
        [receiptKey]: {
          ...(prev[receiptKey] || {}),
          loading: false,
          error: String(err),
        },
      }));
    }
  }

  async function detectRuntimeConfig() {
    if (!researchProject) return;
    try {
      setResearchRuntimeSaving(true);
      setError("");
      await detectRuntime(researchProject);
      const summary = await getProject(researchProject);
      setProjectSummary(summary);
      setResearchRuntimeDraft(runtimeDraftFromSummary(summary));
      setResearchEnvDraft(environmentDraftFromSummary(summary));
      setResearchRuntimeMessage(t.runtimeDetected);
    } catch (err) {
      setError(String(err));
    } finally {
      setResearchRuntimeSaving(false);
    }
  }

  function envConfigPatchFromDraft() {
    const fallback = environmentDraftFromSummary(researchSummary);
    return {
      conda_env: researchEnvDraft.conda_env || effectiveResearchEnvDraft.conda_env || fallback.conda_env || "",
      conda_base: researchEnvDraft.conda_base || effectiveResearchEnvDraft.conda_base || fallback.conda_base || "",
      experiment_python: researchEnvDraft.experiment_python || effectiveResearchEnvDraft.experiment_python || fallback.experiment_python || "",
    };
  }

  async function saveEnvConfig() {
    if (!researchProject) return;
    try {
      setResearchEnvSaving(true);
      setError("");
      await saveRuntime(researchProject, envConfigPatchFromDraft());
      const summary = await getProject(researchProject);
      setProjectSummary(summary);
      setResearchEnvDraft(environmentDraftFromSummary(summary));
      setResearchRuntimeDraft(runtimeDraftFromSummary(summary));
      setResearchEnvMessage(t.envConfigSaved);
    } catch (err) {
      setError(String(err));
    } finally {
      setResearchEnvSaving(false);
    }
  }

  async function persistEnvConfigForRun() {
    if (!researchProject || environmentLocked) return;
    await saveRuntime(researchProject, envConfigPatchFromDraft());
    const summary = await getProject(researchProject);
    setProjectSummary(summary);
    setResearchEnvDraft(environmentDraftFromSummary(summary));
    setResearchRuntimeDraft(runtimeDraftFromSummary(summary));
  }

  function updateJob(nextJob: Job) {
    const normalizedJob = normalizeJobForState(nextJob);
    if (!normalizedJob) return;
    setJobs((prev) => {
      const exists = prev.some((item) => item.job_id === normalizedJob.job_id);
      const merged = exists ? prev.map((item) => item.job_id === normalizedJob.job_id ? mergeFindJobSnapshot(item, normalizedJob) : item) : [normalizedJob, ...prev];
      return jobsForProject(merged, activeProjectRef.current || researchProject || "");
    });
  }

  function watchExistingJob(jobId: string, nextTab?: Tab) {
    const normalizedJobId = String(jobId || "").trim();
    if (!normalizedJobId || watchedJobIdsRef.current.has(normalizedJobId)) return;
    watchedJobIdsRef.current.add(normalizedJobId);
    const releaseWatcher = () => watchedJobIdsRef.current.delete(normalizedJobId);
    const socket = watchJob(normalizedJobId, (message) => {
      if (message.type === "log") {
        setJobs((prev) => prev.map((item) => item.job_id === normalizedJobId ? { ...item, logs: [...safeJobLogs(item), String(message.message ?? "")] } : item));
      }
      if (message.type === "progress") {
        setJobs((prev) => prev.map((item) => item.job_id === normalizedJobId ? { ...item, progress: message.progress } : item));
      }
      if (message.type === "snapshot") {
        updateJob(message.job);
      }
      if (message.type === "complete") {
        updateJob(message.job);
        const resultRunId = message.job?.result?.run_id || runId;
        if (resultRunId) void refreshRuns(resultRunId);
        if (message.job?.status === "done" && isFindRunJob(message.job) && resultRunId) {
          void loadRun(resultRunId, { clear: false, loading: false });
          if (researchProject) void refreshProject({ resetDrafts: false });
        }
        if (nextTab && message.job?.status === "done") setTab(nextTab);
        if (nextTab && ["environment", "experiment", "paperWrite"].includes(nextTab) && researchProject) void refreshProject({ resetDrafts: false });
        releaseWatcher();
        socket.close();
      }
      if (message.type === "error") {
        if (String(message.message || "").toLowerCase() === "job not found") {
          releaseWatcher();
          socket.close();
          return;
        }
        setError(message.message);
      }
    });
    socket.onclose = releaseWatcher;
    socket.onerror = releaseWatcher;
  }

  async function handleSaveConfig() {
    try {
      setSavingConfig(true);
      setError("");
      const nextConfig = configWithCurrentFindSelection();
      const savedConfig = await saveConfig(nextConfig);
      setConfig(savedConfig);
      setLLMProbeResult(null);
      if (researchProject) {
        await saveProjectConfigDraft({ silent: true, propagateError: true });
        void loadProject(researchProject, { resetDrafts: false }).catch(() => {});
      }
      setSaveMessage(t.saved);
    } catch (err) {
      setError(String(err));
    } finally {
      setSavingConfig(false);
    }
  }

  async function handleProbeLLMConfig() {
    try {
      setLLMProbeLoading(true);
      setError("");
      const nextConfig = configWithCurrentFindSelection();
      const savedConfig = await saveConfig(nextConfig);
      setConfig(savedConfig);
      const result = await probeLLMConfig();
      setLLMProbeResult(result);
      if (researchProject) {
        void loadProject(researchProject, { resetDrafts: false }).catch(() => {});
      }
      setSaveMessage(result.ok ? (lang === "zh" ? "LLM 验证通过" : "LLM probe passed") : (lang === "zh" ? "LLM 验证失败" : "LLM probe failed"));
    } catch (err) {
      setLLMProbeResult({ ok: false, error: String(err) });
      setError(String(err));
    } finally {
      setLLMProbeLoading(false);
    }
  }

  function attachJob(nextJob: Job, nextTab?: Tab) {
    updateJob(nextJob);
    setError("");
    const nextRunId = runIdFromJob(nextJob);
    if (nextRunId && !isFindRunJob(nextJob)) {
      setRunId(nextRunId);
      void loadRun(nextRunId, { clear: false, loading: false }).catch(() => {});
    }
    watchExistingJob(nextJob.job_id, nextTab);
  }

  async function runFind() {
    if (stageLaunchDisabledByFullCycle) {
      setError(stageLaunchLockedText);
      return;
    }
    if (findLaunchPending || freshFindRunning) {
      setError(lang === "zh" ? "Find 正在启动或运行；请等待当前任务完成。" : "Find is starting or running; wait for the current job to finish.");
      return;
    }
    setFindLaunchPending(true);
    try {
      setError("");
      setVenueHealth({});
      const nextConfig = configWithCurrentFindSelection();
      const savedConfig = await saveConfig(nextConfig);
      setConfig(savedConfig);
      if (researchProject) {
        await saveProjectConfigDraft({ silent: true, propagateError: true });
        void loadProject(researchProject, { resetDrafts: false }).catch(() => {});
      }
      const nextJob = await startFind(savedConfig, savedConfig.default_find_selection, {
        project: researchProject,
        human_approved_new_find: true,
        approval_reason: "user_explicit_find_run_from_web",
      });
      attachJob(nextJob, "read");
    } catch (err) {
      setError(String(err));
    } finally {
      setFindLaunchPending(false);
    }
  }

  async function persistReadPaperLimit(showMessage = false) {
    const limit = Math.max(1, Math.trunc(Number(readPaperLimit) || DEFAULT_READ_PAPER_LIMIT));
    if (!researchProject) return limit;
    const summary = await saveProjectConfig(researchProject, { max_read_papers: limit });
    const savedLimit = projectReadPaperLimit(summary, 0);
    if (savedLimit !== limit) throw new Error("Project did not persist max_read_papers");
    setProjectSummary(summary);
    setReadPaperLimit(savedLimit);
    setReadPaperLimitDirty(false);
    if (showMessage) setReadPaperLimitMessage(lang === "zh" ? "已保存到当前项目。" : "Saved for this project.");
    return savedLimit;
  }

  async function saveReadPaperLimit() {
    try {
      setReadPaperLimitMessage("");
      await persistReadPaperLimit(true);
    } catch {
      setReadPaperLimitMessage(lang === "zh" ? "保存失败，请重试。" : "Save failed; please retry.");
    }
  }

  async function runRead() {
    if (rejectHistoricalRunMutation()) return;
    const readRunId = currentProjectFindRunId || runId;
    if (!readRunId) return;
    if (stageLaunchDisabledByFullCycle) {
      setError(stageLaunchLockedText);
      return;
    }
    try {
      setError("");
      const maxPapers = readPaperLimitDirty
        ? await persistReadPaperLimit()
        : Math.max(1, Math.trunc(Number(readPaperLimit) || DEFAULT_READ_PAPER_LIMIT));
      attachJob(await startRead(readRunId, [], maxPapers), "read");
    } catch (err) {
      setError(String(err));
    }
  }

  async function runIdeas() {
    if (rejectHistoricalRunMutation()) return;
    const ideaRunId = currentProjectFindRunId || runId;
    if (!ideaRunId || !researchProject) return;
    if (stageLaunchDisabledByFullCycle) {
      setError(stageLaunchLockedText);
      return;
    }
    const maxIdeas = Math.min(50, Math.max(1, Number(config.max_ideas) || 1));
    try {
      setError("");
      attachJob(await startIdea(ideaRunId, maxIdeas, researchProject), "ideas");
    } catch (err) {
      setError(String(err));
    }
  }

  async function runPlan() {
    if (rejectHistoricalRunMutation()) return;
    const planRunId = currentProjectFindRunId || runId;
    if (!planRunId) return;
    if (stageLaunchDisabledByFullCycle) {
      setError(stageLaunchLockedText);
      return;
    }
    if (!planIdeaIds.length) {
      setError(lang === "zh" ? "请至少选择一个已批准的 Idea。" : "Select at least one approved Idea.");
      return;
    }
    try {
      setError("");
      attachJob(await startPlan(planRunId, planIdeaIds, planRepairRounds), "plan");
    } catch (err) {
      setError(String(err));
    }
  }

  async function runPlanPolish(planId: string, versionId: string) {
    if (rejectHistoricalRunMutation()) return;
    const planRunId = currentProjectFindRunId || runId;
    if (!planRunId) return;
    if (stageLaunchDisabledByFullCycle) {
      setError(stageLaunchLockedText);
      return;
    }
    try {
      setError("");
      attachJob(await startPlanPolish(planRunId, planId, versionId, polishRounds[planId] || 1), "plan");
    } catch (err) {
      setError(String(err));
    }
  }

  async function runPlanFinish(planId: string) {
    if (rejectHistoricalRunMutation()) return;
    const planRunId = currentProjectFindRunId || runId;
    if (!planRunId) return;
    if (stageLaunchDisabledByFullCycle) {
      setError(stageLaunchLockedText);
      return;
    }
    if (!window.confirm(t.finishPlanConfirm)) return;
    await finishPlan(planRunId, planId);
    await loadRun(planRunId);
  }

  const activeFindRunId = useMemo(() => {
    const explicit = activeFindRunIdFromJobs(jobs);
    if (explicit) return explicit;
    return hasLiveFindJob(jobs) ? latestFindRunId(runs) : "";
  }, [jobs, runs]);
  const activeProjectInfo = useMemo(() => researchProjects.find((project) => project.id === researchProject), [researchProject, researchProjects]);
  const researchStages = useMemo(() => researchSummary?.stages || researchSummary?.state?.stages || {}, [researchSummary]);
  const humanSupervision = useMemo(() => researchSummary?.human_supervision || researchSummary?.state?.human_supervision || {}, [researchSummary]);
  const currentFindRunIdFromVisibleJobs = useMemo(() => currentFindRunIdFromJobs(jobs, researchProject), [jobs, researchProject]);
  const currentProjectFindRunId = useMemo(() => {
    const fromSummary = currentFindRunIdFromSummary(researchSummary);
    if (fromSummary) return fromSummary;
    if (currentFindRunIdFromVisibleJobs) return currentFindRunIdFromVisibleJobs;
    return "";
  }, [researchSummary, currentFindRunIdFromVisibleJobs]);
  const researchLiteratureSurvey = useMemo(() => researchSummary?.literature_survey || researchSummary?.state?.literature_survey || activeProjectInfo?.literature_survey_preview || {}, [researchSummary, activeProjectInfo]);
  const researchLiteratureCounts = useMemo(() => researchLiteratureSurvey?.counts || {}, [researchLiteratureSurvey]);
  const researchSurveyCandidates = useMemo(() => recommendationLiteraturePapers(asArray(researchLiteratureSurvey?.survey_candidates)), [researchLiteratureSurvey]);
  const researchAuditCandidates = useMemo(() => auditLiteraturePapers(asArray(researchLiteratureSurvey?.audit_candidates)), [researchLiteratureSurvey]);
  const researchReadCandidates = useMemo(() => readableLiteraturePapers(asArray(researchLiteratureSurvey?.read_candidates)), [researchLiteratureSurvey]);
  const researchStrongRecommendations = useMemo(() => asArray(researchLiteratureSurvey?.strong_recommendations).filter((paper: any) => paper && typeof paper === "object" && Boolean(paper.title || paper.id)), [researchLiteratureSurvey]);
  const researchSourceStatus = useMemo(() => asArray(researchLiteratureSurvey?.source_status), [researchLiteratureSurvey]);
  const projectRunPinnedIds = useMemo(() => Array.from(new Set([currentProjectFindRunId, runId].filter(Boolean))), [currentProjectFindRunId, runId]);
  const projectRuns = useMemo(
    () => runs.filter((run) => runMatchesProject(run, researchProject, projectRunPinnedIds)),
    [runs, researchProject, projectRunPinnedIds],
  );
  const visibleRuns = useMemo(() => {
    const pinned = new Set(projectRunPinnedIds);
    const ordered = [
      ...projectRunPinnedIds.map((id) => projectRuns.find((run) => run.run_id === id)).filter(Boolean) as RunInfo[],
      ...projectRuns.filter((run) => !pinned.has(run.run_id)),
    ];
    if (showAllRuns) return ordered;
    return ordered.slice(0, Math.max(12, projectRunPinnedIds.length));
  }, [projectRunPinnedIds, projectRuns, showAllRuns]);
  const hiddenRunCount = Math.max(0, projectRuns.length - visibleRuns.length);
  const displayJobs = useMemo(() => jobsForProject(jobs, researchProject), [jobs, researchProject]);
  const researchSourceLimitations = useMemo(() => asArray(researchLiteratureSurvey?.source_limitations), [researchLiteratureSurvey]);
  const researchMissingVenueIndexes = useMemo(() => asArray(researchLiteratureSurvey?.missing_venue_indexes), [researchLiteratureSurvey]);
  const currentFindArtifactsRunId = useMemo(() => {
    const payload = currentFindArtifacts.find((a) => ["find_results.json", "read_results.json", "ideas.json", "plans.json", "find_progress.json"].includes(a.name))?.content;
    return String(payload?.run_id || payload?.source_run_id || payload?.find_run_id || "").trim();
  }, [currentFindArtifacts]);
  const selectedRunArtifactsRunId = useMemo(() => {
    const payload = artifacts.find((a) => ["find_results.json", "read_results.json", "ideas.json", "plans.json", "find_progress.json"].includes(a.name))?.content;
    return String(payload?.run_id || payload?.source_run_id || payload?.find_run_id || "").trim();
  }, [artifacts]);
  const activeFindArtifactsRunId = useMemo(() => {
    const payload = activeFindArtifacts.find((a) => ["find_results.json", "find_progress.json", "selection.json", "venue_health_report.json"].includes(a.name))?.content;
    return String(payload?.run_id || payload?.source_run_id || payload?.find_run_id || "").trim();
  }, [activeFindArtifacts]);
  const activeFindArtifactSource = useMemo(() => {
    if (!activeFindRunId || !activeFindArtifacts.length) return [];
    if (activeFindArtifactsRunId && activeFindArtifactsRunId !== activeFindRunId) return [];
    return activeFindArtifacts;
  }, [activeFindArtifacts, activeFindArtifactsRunId, activeFindRunId]);
  const currentFindArtifactRunId = currentProjectFindRunId || activeFindRunId;
  const currentFindArtifactsMatch = Boolean(currentFindArtifactRunId && currentFindArtifacts.length && (!currentFindArtifactsRunId || currentFindArtifactsRunId === currentFindArtifactRunId));
  const selectedRunArtifactsMatchCurrentFind = Boolean(currentFindArtifactRunId && runId === currentFindArtifactRunId && artifacts.length && (!selectedRunArtifactsRunId || selectedRunArtifactsRunId === currentFindArtifactRunId));
  const viewingCurrentProjectFindRun = Boolean(currentFindArtifactRunId && runId === currentFindArtifactRunId);
  const viewingSelectedHistoricalFindRun = Boolean(String(runId || "").startsWith("find_") && !viewingCurrentProjectFindRun);
  function rejectHistoricalRunMutation() {
    if (!viewingSelectedHistoricalFindRun) return false;
    setError(lang === "zh" ? "历史 run 仅供查看；请切回当前 Find 后再修改。" : "Historical runs are read-only. Switch back to the current Find before making changes.");
    return true;
  }
  const currentFindArtifactSource = useMemo(() => {
    if (viewingSelectedHistoricalFindRun) return artifacts;
    if (currentFindArtifactsMatch) return currentFindArtifacts;
    if (selectedRunArtifactsMatchCurrentFind) return artifacts;
    return [];
  }, [artifacts, currentFindArtifacts, currentFindArtifactsMatch, selectedRunArtifactsMatchCurrentFind, viewingSelectedHistoricalFindRun]);
  const findResults = useMemo(() => currentFindArtifactSource.find((a) => a.name === "find_results.json")?.content, [currentFindArtifactSource]);
  const findProgress = useMemo(() => currentFindArtifactSource.find((a) => a.name === "find_progress.json")?.content, [currentFindArtifactSource]);
  const activeFindResults = useMemo(() => activeFindArtifactSource.find((a) => a.name === "find_results.json")?.content, [activeFindArtifactSource]);
  const activeFindProgress = useMemo(() => activeFindArtifactSource.find((a) => a.name === "find_progress.json")?.content, [activeFindArtifactSource]);
  const hasCurrentFindResults = Boolean(findResults && currentProjectFindRunId && String(findResults.run_id || "") === currentProjectFindRunId);
  const hasActiveFindResults = Boolean(activeFindResults && activeFindRunId && String(activeFindResults.run_id || "") === activeFindRunId);
  const selectedFindJobForRun = useMemo(
    () => jobs.find((job) => isFindRunJob(job) && runIdFromJob(job) === (activeFindRunId || runId)),
    [activeFindRunId, jobs, runId],
  );
  const activeFindJobForRun = useMemo(
    () => selectedFindJobForRun && isLiveJob(selectedFindJobForRun) ? selectedFindJobForRun : undefined,
    [selectedFindJobForRun],
  );
  const viewingActiveIncompleteFindRun = Boolean(activeFindRunId && (!hasActiveFindResults || activeFindJobForRun));
  const hasLiveCurrentFindArtifactJob = useMemo(
    () => Boolean(currentFindArtifactRunId && displayJobs.some((job: any) => isFindRunJob(job) && isLiveJob(job) && runIdFromJob(job) === currentFindArtifactRunId)),
    [currentFindArtifactRunId, displayJobs],
  );
  const useCurrentFindPacket = Boolean(currentProjectFindRunId && !hasCurrentFindResults && !viewingActiveIncompleteFindRun);
  const activeRunFindState = hasActiveFindResults ? activeFindResults : activeFindProgress;
  const runFindState = viewingActiveIncompleteFindRun ? (activeRunFindState || {}) : (hasCurrentFindResults ? findResults : findProgress);
  const expectedCurrentFindDownstreamCount = Number(
    researchLiteratureCounts.readings
    || researchLiteratureCounts.read_candidates
    || researchLiteratureCounts.strong_recommendations
    || researchLiteratureCounts.ideas
    || researchLiteratureCounts.plans
    || researchLiteratureSurvey?.read_candidates_count
    || researchLiteratureSurvey?.strong_recommendations_count
    || 0,
  );
  const currentFindArtifactsPending = Boolean(currentProjectFindRunId && currentFindArtifactSource.length === 0 && !viewingActiveIncompleteFindRun && expectedCurrentFindDownstreamCount > 0);
  const findStageBootstrapping = Boolean(!researchSummary && researchProject);
  const currentFindArtifactLoading = Boolean(findStageBootstrapping || currentFindArtifactsPending || (currentFindArtifactSource.length === 0 && ((currentFindArtifactsLoading && !hasCurrentFindResults) || (runArtifactsLoading && currentProjectFindRunId && (!runId || runId === currentProjectFindRunId) && !hasCurrentFindResults))));
  const selectedRunSelection = useMemo(() => {
    const resultSelection = runFindState?.selection;
    if (resultSelection && typeof resultSelection === "object" && !Array.isArray(resultSelection)) return resultSelection;
    const artifactSelection = currentFindArtifactSource.find((a) => a.name === "selection.json")?.content;
    if (artifactSelection && typeof artifactSelection === "object" && !Array.isArray(artifactSelection)) return artifactSelection;
    return config.default_find_selection || {};
  }, [currentFindArtifactSource, config.default_find_selection, runFindState]);
  const findStrongLiteratureRows = useMemo(() => {
    const strongRows = recommendationLiteraturePapers(filterBySourceSelection(asArray(findResults?.strong_recommendations), selectedRunSelection));
    if (strongRows.length) return strongRows;
    const rawArticles = asArray(findResults?.articles);
    return recommendationLiteraturePapers(filterBySourceSelection(rawArticles, selectedRunSelection));
  }, [findResults, selectedRunSelection]);
  const activeStrongLiteratureRows = useMemo(() => {
    if (useCurrentFindPacket) return researchStrongRecommendations;
    if (viewingActiveIncompleteFindRun) return [];
    return hasCurrentFindResults ? findStrongLiteratureRows : researchStrongRecommendations;
  }, [researchStrongRecommendations, findStrongLiteratureRows, hasCurrentFindResults, useCurrentFindPacket, viewingActiveIncompleteFindRun]);
  const literatureCounts = useMemo(() => {
    const surveyStats = { ...(findResults?.survey_stats || {}), ...(findResults?.diagnostics?.survey_stats || {}) };
    const rawProgressCounts = runFindState?.counts || {};
    const progressCounts = viewingActiveIncompleteFindRun ? rawProgressCounts : {};
    const artifactSourceRowsForCounts = expandedSourceStatusRows(runFindState);
    const summarySourceRowsForCounts = filterBySourceSelection(firstNonEmptyArray(
      researchSourceStatus,
      expandedSourceStatusRows(researchSummary?.current_find_pipeline || {}),
      expandedSourceStatusRows(researchSummary?.state?.current_find_pipeline || {}),
      expandedSourceStatusRows(researchLiteratureSurvey?.current_find_pipeline || {}),
      expandedSourceStatusRows(researchStages?.find || researchSummary?.state?.stages?.find || {}),
    ), selectedRunSelection);
    const sourceRowsForCounts = artifactSourceRowsForCounts.length ? artifactSourceRowsForCounts : summarySourceRowsForCounts;
    const sourceRowTotal = sourceRowsForCounts.reduce((total: number, row: any) => total + Number(row?.raw_title_index_count || row?.corpus_count || row?.count || row?.sample_count || 0), 0);
    const sourceRowCategoryTotal = sourceRowsForCounts.reduce((total: number, row: any) => total + Number(row?.selected_category_count || row?.candidate_count || row?.count || row?.sample_count || 0), 0);
    const categoryRows = asArray(findResults?.category_scan_report);
    const titleRows = asArray(findResults?.title_filter_report);
    const arxivReport = findResults?.arxiv_prefilter_report || {};
    const sum = (rows: any[], key: string) => rows.reduce((total, row) => total + Number(row?.[key] || 0), 0);
    const screened = recommendationLiteraturePapers(filterBySourceSelection(asArray(findResults?.screened_ranking), selectedRunSelection));
    const rawTitleCount = asArray(findResults?.raw_title_index).length || asArray(findResults?.raw_candidates).length;
    const titleInputCount = surveyStats.venue_title_filter_input_papers ?? sum(titleRows, "title_filter_input_papers");
    const arxivEnabled = Boolean(selectedRunSelection.include_arxiv);
    const useArFallback = useCurrentFindPacket || !viewingActiveIncompleteFindRun;
    const runFunnelHasCounts = Boolean(
      surveyStats.raw_title_index_papers
      || surveyStats.category_filtered_papers
      || surveyStats.tfidf_screened_papers
      || surveyStats.venue_title_filter_input_papers
      || surveyStats.llm_title_scored_papers
      || surveyStats.venue_final_title_candidates
      || rawTitleCount
      || progressCounts.raw_title_index
      || progressCounts.raw_title_index_papers,
    );
    const summaryFunnelRaw = useArFallback ? (researchLiteratureCounts.raw_title_index_papers || researchLiteratureCounts.venue_corpus_audited_papers || researchLiteratureCounts.venue_total_papers_available) : "";
    const scannedTotal = surveyStats.raw_title_index_papers || surveyStats.venue_corpus_audited_papers || surveyStats.venue_total_papers_available || rawTitleCount || progressCounts.raw_title_index || progressCounts.raw_title_index_papers || titleInputCount || sum(categoryRows, "total_papers") || summaryFunnelRaw || (!runFunnelHasCounts ? sourceRowTotal : "") || "";
    const titleScreenInputTotal = surveyStats.category_filtered_papers || progressCounts.category_filtered_papers || (useArFallback ? Number(researchLiteratureCounts.category_filtered_papers || 0) : 0) || (!runFunnelHasCounts ? sourceRowCategoryTotal : "") || "";
    const selectedTotal = surveyStats.venue_category_selected_papers || surveyStats.category_selected_papers || sum(categoryRows, "selected_category_papers") || titleScreenInputTotal || (useArFallback ? researchLiteratureCounts.venue_category_selected_papers : "") || (!runFunnelHasCounts ? sourceRowCategoryTotal : "") || "";
    return {
      raw_title_index_papers: scannedTotal,
      venue_total_papers_available: scannedTotal,
      venue_corpus_audited_papers: scannedTotal,
      category_selected_papers: selectedTotal,
      venue_category_selected_papers: selectedTotal,
      scanned: scannedTotal,
      corpusAudited: scannedTotal,
      selected: selectedTotal,
      categoryFiltered: titleScreenInputTotal,
      titleInput: titleInputCount || (useArFallback ? researchLiteratureCounts.venue_title_filter_input_papers : "") || "",
      tfidfScreened: surveyStats.tfidf_screened_papers || progressCounts.tfidf_screened_papers || titleInputCount || (useArFallback ? Number(researchLiteratureCounts.tfidf_screened_papers || researchLiteratureCounts.venue_title_filter_input_papers || 0) : 0),
      titleScoreInput: surveyStats.title_score_input_papers || progressCounts.title_score_input_papers || titleInputCount || 0,
      llmTitleScored: surveyStats.llm_title_scored_papers || progressCounts.llm_title_scored_papers || (useArFallback ? Number(researchLiteratureCounts.llm_title_scored_papers || 0) : 0),
      titleCandidates: (surveyStats.venue_final_title_candidates ?? sum(titleRows, "final_title_candidates")) || progressCounts.title_candidates || asArray(findResults?.title_candidates).length || (useArFallback ? (researchLiteratureCounts.venue_final_title_candidates || researchLiteratureCounts.survey_candidates) : 0) || 0,
      detailFetched: surveyStats.venue_detail_fetched_candidates || progressCounts.detail_fetched || asArray(findResults?.evaluated_candidates).length || (useArFallback ? Number(researchLiteratureCounts.venue_detail_fetched_candidates || researchLiteratureCounts.evaluated_candidates || 0) : 0),
      llmScored: firstNumericValue(
        surveyStats.abstract_scored_papers,
        surveyStats.llm_scored_candidates,
        findResults?.diagnostics?.llm_scored_count,
        progressCounts.abstract_scored_papers,
        progressCounts.llm_scored_candidates,
        useArFallback ? researchLiteratureCounts.abstract_scored_papers : undefined,
        useArFallback ? researchLiteratureCounts.llm_scored_candidates : undefined,
      ),
      fullCorpusAudit: Boolean(surveyStats.full_venue_corpus_audit || (useArFallback && researchLiteratureCounts.full_venue_corpus_audit)),
      llmPolicy: surveyStats.llm_scoring_policy || findResults?.diagnostics?.survey_stats?.llm_scoring_policy || "category/title-screened candidates only",
      arxivRaw: arxivEnabled ? (surveyStats.arxiv_raw_count ?? asArray(findResults?.arxiv_raw).length ?? (useArFallback ? researchLiteratureCounts.arxiv_raw_count : 0)) : 0,
      arxivCandidates: arxivEnabled ? (surveyStats.arxiv_prefiltered_count ?? arxivReport.prefiltered_count ?? asArray(findResults?.arxiv_prefiltered).length ?? (useArFallback ? researchLiteratureCounts.arxiv_prefiltered_count : 0)) : 0,
      arxivEnabled,
      strong: activeStrongLiteratureRows.length || (useArFallback ? Number(researchLiteratureCounts.strong_recommendations || 0) : 0),
      strictStrongAnchors: useArFallback ? Number(researchLiteratureCounts.strict_strong_anchor_count || 0) : Number(findResults?.strict_strong_anchor_count || findResults?.counts?.strict_strong_anchor_count || 0),
      articleOutput: activeStrongLiteratureRows.length || (useArFallback ? Number(researchLiteratureCounts.article_output || 0) : 0),
      readCandidatesRaw: activeStrongLiteratureRows.length || (useArFallback ? Number(researchLiteratureCounts.read_candidates || researchLiteratureCounts.strong_recommendations || 0) : Number(asArray(findResults?.read_candidates).length || 0)),
      triageCandidates: useArFallback ? Number(researchLiteratureCounts.triage_candidates || researchLiteratureCounts.audit_candidates || 0) : Number(asArray(findResults?.triage_candidates).length || asArray(findResults?.audit_candidates).length || 0),
      screened: screened.length || activeStrongLiteratureRows.length || (useArFallback ? Number(researchLiteratureCounts.screened_ranking || 0) : 0),
      auditPool: screened.length || (useArFallback ? Number(researchLiteratureCounts.screened_ranking || 0) : 0),
      evaluated: asArray(findResults?.evaluated_candidates).length || progressCounts.evaluated_candidates || (useArFallback ? Number(researchLiteratureCounts.evaluated_candidates || 0) : 0),
    };
  }, [activeStrongLiteratureRows, researchLiteratureCounts, researchLiteratureSurvey, researchSourceStatus, researchStages, researchSummary, findResults, runFindState, selectedRunSelection, useCurrentFindPacket, viewingActiveIncompleteFindRun]);
  const retrievalPool = useMemo(() => {
    const source = firstNonEmptyArray(
      findResults?.retrieval_candidates,
      findResults?.title_candidates,
      findResults?.evaluated_candidates,
      findResults?.arxiv_prefiltered,
      findResults?.raw_title_index,
    );
    const runRows = filterBySourceSelection(Array.isArray(source) ? source : [], selectedRunSelection);
    if (useCurrentFindPacket) return researchSurveyCandidates;
    if (viewingActiveIncompleteFindRun) return runRows;
    return runRows.length ? runRows : researchSurveyCandidates;
  }, [researchSurveyCandidates, findResults, selectedRunSelection, useCurrentFindPacket, viewingActiveIncompleteFindRun]);
  const readCandidatePool = useMemo(() => {
    const explicitReadRows = asArray(findResults?.read_candidates);
    const runRows = explicitReadRows.length
      ? readableLiteraturePapers(filterBySourceSelection(explicitReadRows, selectedRunSelection))
      : readableLiteraturePapers(filterBySourceSelection(firstNonEmptyArray(findResults?.strong_recommendations, findResults?.articles), selectedRunSelection));
    if (useCurrentFindPacket) return firstNonEmptyArray(researchStrongRecommendations, researchReadCandidates);
    if (viewingActiveIncompleteFindRun) return runRows;
    return firstNonEmptyArray(runRows, researchStrongRecommendations, researchReadCandidates);
  }, [researchReadCandidates, researchStrongRecommendations, findResults, selectedRunSelection, useCurrentFindPacket, viewingActiveIncompleteFindRun]);
  const currentFindPipelineCounts = researchSummary?.current_find_pipeline || researchLiteratureSurvey?.current_find_pipeline || {};
  const readResultsArtifact = useMemo(() => currentFindArtifactSource.find((a) => a.name === "read_results.json"), [currentFindArtifactSource]);
  const readResults = useMemo(() => readResultsArtifact?.content || {}, [readResultsArtifact]);
  const auditReadings = useMemo(() => asArray(readResults?.readings).filter((row: any) => row && typeof row === "object" && Boolean(row.title || row.paper_id || row.id)), [readResults]);
  const publicReadings = useMemo(() => asArray(readResults?.public_readings).filter((row: any) => row && typeof row === "object" && Boolean(row.title || row.paper_id || row.id)), [readResults]);
  const currentReadings = useMemo(() => publicReadings.length ? publicReadings : auditReadings, [publicReadings, auditReadings]);
  const auditReadingCount = auditReadings.length || currentReadings.length;
  const readDisplayRows = useMemo(() => currentReadings.length ? currentReadings : readCandidatePool, [currentReadings, readCandidatePool]);
  const publishedReadCount = maxNumericValue(researchLiteratureCounts.readings, currentFindPipelineCounts?.reading_count, currentFindPipelineCounts?.readings, currentFindPipelineCounts?.read_count, currentFindPipelineCounts?.full_text_reading_count);
  const expectedReadCandidateCount = maxNumericValue(auditReadingCount, publishedReadCount, researchLiteratureCounts.read_candidates, researchLiteratureCounts.strong_recommendations, researchLiteratureSurvey?.read_candidates_count, researchLiteratureSurvey?.strong_recommendations_count);
  const readArtifactStale = Boolean(readResultsArtifact && publishedReadCount > auditReadingCount);
  const readCandidatesStillSyncing = Boolean((!currentReadings.length || readArtifactStale) && expectedReadCandidateCount > 0 && (!readResultsArtifact || currentFindArtifactLoading || (useCurrentFindPacket && readCandidatePool.length === 0)));
  const hasSurveyCandidates = retrievalPool.length > 0 || readCandidatePool.length > 0 || readCandidatesStillSyncing || expectedReadCandidateCount > 0 || Number(literatureCounts.evaluated || 0) > 0 || Number(literatureCounts.arxivCandidates || 0) > 0 || (!viewingActiveIncompleteFindRun && Number(researchLiteratureCounts.survey_candidates || 0) > 0);
  const hasCurrentFindSourceContext = useMemo(() => Boolean(
    currentProjectFindRunId
    || currentFindArtifactRunId
    || researchLiteratureSurvey?.run_id
    || researchLiteratureSurvey?.current_find_pipeline?.run_id
    || researchSummary?.current_find_pipeline?.run_id
    || researchSummary?.state?.current_find_pipeline?.run_id
  ), [currentFindArtifactRunId, currentProjectFindRunId, researchLiteratureSurvey, researchSummary]);

  const sourceStatus = useMemo(() => {
    const runRows = filterBySourceSelection(expandedSourceStatusRows(runFindState), selectedRunSelection);
    const surveyRows = filterBySourceSelection(researchSourceStatus, selectedRunSelection);
    const pipelineRows = filterBySourceSelection(firstNonEmptyArray(
      expandedSourceStatusRows(researchSummary?.current_find_pipeline || {}),
      expandedSourceStatusRows(researchSummary?.state?.current_find_pipeline || {}),
      expandedSourceStatusRows(researchLiteratureSurvey?.current_find_pipeline || {}),
    ), selectedRunSelection);
    const stageRows = filterBySourceSelection(
      expandedSourceStatusRows(researchStages?.find || researchSummary?.state?.stages?.find || {}),
      selectedRunSelection,
    );
    const hasLiveFindJob = displayJobs.some((job: any) => isFindRunJob(job) && isLiveJob(job));
    const usableRows = (rows: any[]) => rows.filter((row: any) => {
      const message = String(row?.message || row?.reason || "");
      if (message.includes("verified local venue metadata cache missing")) return false;
      if (String(row?.source_kind || "").trim().toLowerCase() !== "venue_health") return true;
      return Boolean(row?.raw_title_index_count || row?.corpus_count || row?.candidate_count || row?.count || row?.sample_count);
    });
    const runUsableRows = usableRows(runRows);
    const surveyUsableRows = usableRows(surveyRows);
    const pipelineUsableRows = usableRows(pipelineRows);
    const stageUsableRows = usableRows(stageRows);
    if (runUsableRows.length) return runUsableRows;
    if (hasLiveFindJob && !surveyUsableRows.length && !pipelineUsableRows.length && !stageUsableRows.length) return [];
    if (surveyUsableRows.length) return surveyUsableRows;
    if (pipelineUsableRows.length) return pipelineUsableRows;
    if (stageUsableRows.length) return stageUsableRows;
    if (viewingActiveIncompleteFindRun || activeFindJobForRun || hasLiveFindJob) return [];
    if (hasCurrentFindSourceContext) return [];
    return [];
  }, [activeFindJobForRun, displayJobs, hasCurrentFindSourceContext, researchLiteratureSurvey, researchSourceStatus, researchStages, researchSummary, runFindState, selectedRunSelection, viewingActiveIncompleteFindRun]);
  const ideaMarkdownArtifact = useMemo(() => currentFindArtifactSource.find((a) => a.name === "idea.md"), [currentFindArtifactSource]);
  const ideaMarkdownText = useMemo(() => String(ideaMarkdownArtifact?.content ?? ""), [ideaMarkdownArtifact]);
  const ideasArtifact = useMemo(() => currentFindArtifactSource.find((a) => a.name === "ideas.json"), [currentFindArtifactSource]);
  const plansArtifact = useMemo(() => currentFindArtifactSource.find((a) => a.name === "plans.json"), [currentFindArtifactSource]);
  const planMarkdownArtifact = useMemo(() => currentFindArtifactSource.find((a) => a.name === "plan.md"), [currentFindArtifactSource]);
  const planMarkdownText = useMemo(() => String(planMarkdownArtifact?.content ?? planMarkdownArtifact?.content_zh ?? planMarkdownArtifact?.content_en ?? ""), [planMarkdownArtifact]);
  const planMarkdownTitles = useMemo(() => planTitlesFromMarkdown(planMarkdownText), [planMarkdownText]);
  const ideas = useMemo(() => ideasArtifact?.content?.ideas ?? [], [ideasArtifact]);
  const plans = useMemo(() => plansArtifact?.content?.plans ?? [], [plansArtifact]);
  const selectedPlanFromArtifact = useMemo(() => plans.find((plan: any) => {
    const executionSelection = plan?.execution_selection && typeof plan.execution_selection === "object" ? plan.execution_selection : {};
    return plan?.selected_for_execution === true || plan?.execute_next === true || executionSelection?.selected === true;
  }) || null, [plans]);
  const mainRoute = humanSupervision?.main_route || {};
  const selectedExecution = useMemo(() => {
    const candidates = [
      currentFindPipelineCounts?.selected_execution,
      researchStages?.plan?.selected_execution,
      researchSummary?.state?.current_find_pipeline?.selected_execution,
      researchLiteratureSurvey?.current_find_pipeline?.selected_execution,
      plansArtifact?.content?.selected_execution,
    ];
    return candidates.find((item: any) => item && typeof item === "object" && !Array.isArray(item)) || {};
  }, [researchSummary, researchStages, researchLiteratureSurvey, currentFindPipelineCounts, plansArtifact]);
  const contractSelectedPlanId = useMemo(() => String(
    selectedExecution?.selected_plan_id
    || currentFindPipelineCounts?.selected_plan_id
    || researchStages?.plan?.selected_plan_id
    || plansArtifact?.content?.selected_plan_id
    || selectedPlanFromArtifact?.plan_id
    || "",
  ).trim(), [researchStages, currentFindPipelineCounts, plansArtifact, selectedExecution, selectedPlanFromArtifact]);
  const contractSelectedIdeaId = useMemo(() => String(
    selectedExecution?.selected_idea_id
    || currentFindPipelineCounts?.selected_idea_id
    || researchStages?.plan?.selected_idea_id
    || plansArtifact?.content?.selected_idea_id
    || selectedPlanFromArtifact?.idea_id
    || "",
  ).trim(), [researchStages, currentFindPipelineCounts, plansArtifact, selectedExecution, selectedPlanFromArtifact]);
  const selectedPlanForControls = useMemo(() => {
    const selected = plans.find((plan: any) => String(plan?.plan_id || "") === selectedPlanId);
    if (selected) return selected;
    if (contractSelectedPlanId) return plans.find((plan: any) => String(plan?.plan_id || "") === contractSelectedPlanId) || null;
    return null;
  }, [contractSelectedPlanId, plans, selectedPlanId]);
  const contractSelectedPlan = useMemo(() => {
    if (contractSelectedPlanId) {
      const selected = plans.find((plan: any) => String(plan?.plan_id || "") === contractSelectedPlanId);
      if (selected) return selected;
    }
    return selectedPlanFromArtifact || selectedPlanForControls || null;
  }, [contractSelectedPlanId, plans, selectedPlanForControls, selectedPlanFromArtifact]);
  const contractSelectedIdea = useMemo(() => {
    const selectedIdeaKey = String(contractSelectedIdeaId || contractSelectedPlan?.idea_id || "").trim();
    if (!selectedIdeaKey) return null;
    return ideas.find((idea: any, index: number) => {
      const keys = [ideaKey(idea, index), idea?.id, idea?.idea_id, idea?.title].map((value) => String(value || "").trim());
      return keys.includes(selectedIdeaKey);
    }) || null;
  }, [contractSelectedIdeaId, contractSelectedPlan, ideas]);
  const selectedPlanLatest = useMemo(() => selectedPlanForControls ? latestPlanVersion(selectedPlanForControls) : {}, [selectedPlanForControls]);
  const selectedExecutionStatus = useMemo(() => String(selectedExecution?.status || currentFindPipelineCounts?.selected_execution_status || researchStages?.plan?.selected_execution_status || (contractSelectedPlanId ? "selected_plan_ready" : "")).trim(), [researchStages, contractSelectedPlanId, currentFindPipelineCounts, selectedExecution]);
  const selectedExecutionMissing = Boolean(plans.length && !contractSelectedPlanId);
  const selectedExecutionText = useMemo(() => {
    if (!contractSelectedPlanId) {
      return lang === "zh"
        ? "候选计划已生成，但主控 Claude Code 或人类监督尚未选择唯一执行计划；环境、实验、论文和论文结论提升保持阻断。"
        : "Plan candidates exist, but the main Claude Code or human supervisor has not selected exactly one execution plan; environment, experiment, paper, and claim execution stay blocked.";
    }
    const planIndex = Math.max(0, plans.indexOf(contractSelectedPlan));
    const ideaIndex = Math.max(0, ideas.indexOf(contractSelectedIdea));
    const planLabel = contractSelectedPlan ? planTitleText(contractSelectedPlan, planIndex) : (lang === "zh" ? "已选择" : "selected");
    const ideaLabel = contractSelectedIdea ? ideaTitleText(contractSelectedIdea, ideaIndex) : "";
    const ideaSuffix = ideaLabel ? (lang === "zh" ? `；对应想法：${ideaLabel}` : `; idea: ${ideaLabel}`) : "";
    return lang === "zh" ? `唯一执行计划：${planLabel}${ideaSuffix}` : `selected execution plan: ${planLabel}${ideaSuffix}`;
  }, [contractSelectedIdea, contractSelectedPlan, contractSelectedPlanId, ideas, lang, plans]);
  const publishedIdeaCount = maxNumericValue(researchLiteratureCounts.ideas, currentFindPipelineCounts?.idea_count, currentFindPipelineCounts?.ideas, researchStages?.idea?.idea_count, mainRoute.ideas);
  const publishedPlanCount = maxNumericValue(researchLiteratureCounts.plans, currentFindPipelineCounts?.plan_count, currentFindPipelineCounts?.plans, researchStages?.plan?.plan_count, mainRoute.plans);
  const expectedIdeaCount = maxNumericValue(ideas.length, publishedIdeaCount);
  const expectedPlanCount = maxNumericValue(plans.length, publishedPlanCount);
  const ideasArtifactStale = Boolean(ideasArtifact && publishedIdeaCount > ideas.length);
  const plansArtifactStale = Boolean(plansArtifact && publishedPlanCount > plans.length);
  const ideasStillSyncing = Boolean((!ideas.length || ideasArtifactStale) && expectedIdeaCount > 0 && (!ideasArtifact || currentFindArtifactLoading || plans.length > 0));
  const plansStillSyncing = Boolean((!plans.length || plansArtifactStale) && expectedPlanCount > 0 && (!plansArtifact || currentFindArtifactLoading));
  useEffect(() => {
    if (!ideaMarkdownEditing) setIdeaMarkdownDraft(ideaMarkdownText);
  }, [ideaMarkdownEditing, ideaMarkdownText]);
  useEffect(() => {
    if (!planMarkdownDirty) setPlanMarkdownDraft(planMarkdownText);
  }, [planMarkdownDirty, planMarkdownText]);
  useEffect(() => {
    const next: Record<string, IdeaEditorDraft> = {};
    ideas.forEach((idea: any, index: number) => {
      const id = String(idea?.id || idea?.title || `idea-${index}`).trim();
      next[id] = {
        title: String(idea?.title || ""),
        new_method: String(idea?.new_method || ""),
        initial_experiment: String(idea?.initial_experiment || ""),
      };
    });
    setIdeaEditorDrafts(next);
  }, [ideas]);
  function ideaKey(idea: any, index?: number) {
    return String(idea?.id || idea?.idea_id || idea?.title || (index !== undefined ? `idea-${index}` : "")).trim();
  }
  const approvedIdeas = useMemo(() => ideas.filter((idea: any) => {
    const status = String(idea?.status || idea?.recommendation || "").toLowerCase();
    if (["deleted", "rejected", "reject", "archived", "pending"].includes(status)) return false;
    return idea?.approved === true
      || idea?.approved_for_planning === true
      || idea?.pursue === true
      || status === "approved"
      || status.includes("approved")
      || status.includes("pursue");
  }), [ideas]);
  useEffect(() => {
    const approvedIds = approvedIdeas.map((idea: any, index: number) => ideaKey(idea, index)).filter(Boolean);
    setPlanIdeaIds((previous) => {
      const retained = previous.filter((ideaId) => approvedIds.includes(ideaId));
      return retained.length ? retained : approvedIds;
    });
  }, [runId, approvedIdeas]);
  function ideaScoreText(idea: any) {
    const value = idea?.score;
    if (value === undefined || value === null || value === "") return "";
    return numberText(value);
  }
  function ideaEvidencePapers(idea: any) {
    return firstNonEmptyArray(idea?.inspired_by, idea?.supporting_papers, idea?.positive_anchor_papers, idea?.evidence_papers).slice(0, 8);
  }
  function stageArtifactText(value: any, fallback = "") {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const status = displayValue(value.status || value.decision || "");
      const repo = displayArtifactText(value.repo_name || value.repo_path || "", "");
      if (status || repo) {
        return lang === "zh"
          ? [`环境审查：${status || t.noData}`, repo ? `仓库：${repo}` : ""].filter(Boolean).join("；")
          : [`Environment review: ${status || t.noData}`, repo ? `repo: ${repo}` : ""].filter(Boolean).join("; ");
      }
    }
    let text = displayArtifactText(value, fallback).trim();
    if (!text) return fallback;
    const zhReplacements: Record<string, string> = {
      "After environment-stage base selection, run a minimal same-protocol baseline/candidate/ablation experiment with audited metrics and bad cases.": "idea.md 未提供具体初步实验；请重新生成或手动补齐。",
      "After environment review, run a minimal same-protocol baseline/candidate/ablation experiment with audited metrics and bad cases.": "idea.md 未提供具体初步实验；请重新生成或手动补齐。",
      "Idea came from Claude Code under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected repository before environment-stage selection.": "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。",
      "Idea came from Claude Code under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected base before environment-stage selection.": "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。",
      "Verify current Find run_id and guarded read/idea/plan outputs.": "核对当前 Find run_id 以及受门控保护的精读、想法和计划产物。",
      "Environment-stage Claude Code reads all current strong recommendations and audits candidate repos/data/protocols.": "环境审查阶段由 Environment 主控 Claude 精读全部推荐论文，并审计候选仓库、数据和协议。",
      "Accept a base only by writing state/evidence_ready_repo_selection.json with selection_stage=environment_claude_code and fresh_find_run_id matching the current run.": "只有写入可审计的仓库选择记录，并确认 Find run_id 与当前运行一致后，才能接受当前路线。",
      "Refresh reference/scientific/evidence/submission gates before paper writing or paper-conclusion gating.": "论文写作或论文结论提升前，必须刷新参考复现、科学进展、证据和投稿门控。",
      "environment-stage base selected": "环境审查已完成",
      "repo/data/env/protocol gate passed": "仓库、数据、环境和协议门控通过",
      "repo/data/protocol evidence ready": "仓库、数据和协议证据就绪",
      "metrics and bad cases written": "指标和坏例已写入",
      "metrics parsed": "指标已解析",
      "bad-case slice written": "坏例切片已写入",
      "audit JSON exists": "本地审计 JSON 已存在",
      "scientific gate refreshed": "科学进展门控已刷新",
      "evidence gates refreshed": "证据门控已刷新",
      "local evidence gates pass": "本地证据门控通过",
    };
    const enReplacements: Record<string, string> = {
      "After environment-stage base selection, run a minimal same-protocol baseline/candidate/ablation experiment with audited metrics and bad cases.": "idea.md did not provide a concrete initial experiment; regenerate or edit it.",
      "After environment review, run a minimal same-protocol baseline/candidate/ablation experiment with audited metrics and bad cases.": "idea.md did not provide a concrete initial experiment; regenerate or edit it.",
      "Idea came from Claude Code under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected repository before environment-stage selection.": "This idea was generated by the Ideation controller from the current Find/read evidence; before environment review it does not bind a repo, dataset, command, or base.",
      "Idea came from Claude Code under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected base before environment-stage selection.": "This idea was generated by the Ideation controller from the current Find/read evidence; before environment review it does not bind a repo, dataset, command, or base.",
      "Verify current Find run_id and guarded read/idea/plan outputs.": "Verify the current Find run ID and the guarded reading, idea, and plan outputs.",
      "Environment-stage Claude Code reads all current strong recommendations and audits candidate repos/data/protocols.": "During environment review, the Environment controller reads all recommended papers and audits candidate repositories, data, and protocols.",
      "Accept a base only by writing state/evidence_ready_repo_selection.json with selection_stage=environment_claude_code and fresh_find_run_id matching the current run.": "Accept the selected repository only after an auditable repository-selection record confirms the Find run ID matches the current run.",
      "Refresh reference/scientific/evidence/submission gates before paper writing or paper-conclusion gating.": "Refresh reference-reproduction, scientific-progress, evidence, and submission gates before paper writing or paper-conclusion gating.",
      "environment-stage base selected": "environment review completed",
      "repo/data/env/protocol gate passed": "repo, data, environment, and protocol checks passed",
      "repo/data/protocol evidence ready": "repo, data, and protocol evidence ready",
      "metrics and bad cases written": "metrics and bad cases written",
      "metrics parsed": "metrics parsed",
      "bad-case slice written": "bad-case slices written",
      "audit JSON exists": "local audit JSON exists",
      "scientific gate refreshed": "scientific-progress gate refreshed",
      "evidence gates refreshed": "evidence gates refreshed",
      "local evidence gates pass": "local evidence gates pass",
    };
    if (lang === "zh") {
      text = zhReplacements[text] || text
        .replace(/Idea came from 项目代理 under TASTE control.*?environment(?:-stage)? selection\.?/gi, "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。")
        .replace(/Idea came from project agent under TASTE control.*?environment(?:-stage)? selection\.?/gi, "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。")
        .replace(/environment-stage base selection/gi, "环境审查")
        .replace(/environment-stage base selected/gi, "环境审查已完成")
        .replace(/repo\/data\/env\/protocol gate passed/gi, "仓库、数据、环境和协议门控通过")
        .replace(/same-protocol baseline\/candidate\/ablation experiment/gi, "同协议基线/候选/消融实验")
        .replace(/audited metrics and bad cases/gi, "审计指标和坏例")
        .replace(/waiting for environment-stage base selection/gi, "环境审查后执行")
        .replace(/Idea came from 项目代理 under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected repository before environment-stage selection\.?/gi, "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。")
        .replace(/Idea came from 项目代理 under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected repository before environment review\.?/gi, "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。")
        .replace(/Idea came from project agent under TASTE control and was normalized by the current-Find evidence guard; it cannot bind a repo, dataset, command, or selected repository before environment-stage selection\.?/gi, "该想法由 Ideation 主控 Claude 基于当前 Find 精读证据形成；环境审查前不会绑定仓库、数据、命令或当前路线。")
        .replace(/Environment-stage 项目代理 reads all current strong recommendations and audits candidate repos\/data\/protocols\.?/gi, "环境审查阶段由 Environment 主控 Claude 精读全部推荐论文，并审计候选仓库、数据和协议。")
        .replace(/Accept a base only by writing state\/evidence_ready_repo_selection\.json with selection_stage=environment review and fresh_find_run_id matching the current run\.?/gi, "只有写入可审计的仓库选择记录，并确认 Find run_id 与当前运行一致后，才能接受当前路线。")
        .replace(/After current base repo\/data\/env\/protocol gates pass, run minimal baseline\/candidate\/ablation experiments with identical data, seed, metrics, logs, and bad-case extraction\.?/gi, "仓库、数据、环境和协议门控通过后，在相同数据、seed、指标、日志和坏例抽取设置下运行最小对比实验。")
        .replace(/After 当前路线 repo\/data\/env\/protocol gates pass, run minimal baseline\/candidate\/ablation experiments with identical data, seed, metrics, logs, and bad-case extraction\.?/gi, "仓库、数据、环境和协议门控通过后，在相同数据、seed、指标、日志和坏例抽取设置下运行最小对比实验。")
        .replace(/After selected repository repo\/data\/env\/protocol gates pass, run minimal baseline\/candidate\/ablation experiments with identical data, seed, metrics, logs, and bad-case extraction\.?/gi, "仓库、数据、环境和协议门控通过后，在相同数据、seed、指标、日志和坏例抽取设置下运行最小对比实验。")
        .replace(/Refresh reference\/scientific\/evidence\/submission gates before paper writing or paper-conclusion gating\.?/gi, "论文写作或论文结论提升前，必须刷新参考复现、科学进展、证据和投稿门控。")
        .replace(/Refresh reference\/scientific\/evidence\/submission gates before paper writing or 论文结论提升\.?/gi, "论文写作或论文结论提升前，必须刷新参考复现、科学进展、证据和投稿门控。")
        .replace(/Verify current Find run_id and guarded read\/idea\/plan outputs\.?/gi, "核对当前 Find run_id 以及受门控保护的精读、想法和计划产物。")
        .replace(/paper-conclusion gating/gi, "论文结论提升")
        .replace(/主控\s*Claude Code/gi, "主控 Claude Code")
        .replace(/main\s+Claude Code/gi, "主控 Claude Code")
        .replace(/Claude Code/gi, "模块主控 Claude")
        .replace(/project agent/gi, "模块主控 Claude")
        .replace(/selected repository/gi, "当前路线")
        .replace(/current route/gi, "当前路线")
        .replace(/current base/gi, "当前路线");
    } else {
      text = enReplacements[text] || text
        .replace(/environment-stage base selection/gi, "environment review")
        .replace(/environment-stage base selected/gi, "environment review completed")
        .replace(/waiting for environment-stage base selection/gi, "waiting for environment review")
        .replace(/same-protocol baseline\/candidate\/ablation experiment/gi, "same-protocol baseline/candidate/ablation experiment")
        .replace(/Environment-stage project agent reads all current strong recommendations and audits candidate repos\/data\/protocols\.?/gi, "During environment review, the Environment controller reads all recommended papers and audits candidate repositories, data, and protocols.")
        .replace(/Accept a base only by writing state\/evidence_ready_repo_selection\.json with selection_stage=environment review and fresh_find_run_id matching the current run\.?/gi, "Accept the selected repository only after an auditable repository-selection record confirms the Find run ID matches the current run.")
        .replace(/After current base repo\/data\/env\/protocol gates pass, run minimal baseline\/candidate\/ablation experiments with identical data, seed, metrics, logs, and bad-case extraction\.?/gi, "After repository, data, environment, and protocol gates pass, run the minimal comparison under identical data, seed, metrics, logs, and bad-case extraction settings.")
        .replace(/Verify current Find run_id and guarded read\/idea\/plan outputs\.?/gi, "Verify the current Find run ID and the guarded reading, idea, and plan outputs.")
        .replace(/当前路线/g, "current route")
        .replace(/当前基底/g, "current route")
        .replace(/论文结论提升/g, "paper-conclusion gating")
        .replace(/主控\s*Claude Code/gi, "main Claude Code")
        .replace(/main\s+Claude Code/gi, "main Claude Code")
        .replace(/Claude Code/gi, "module controller");
    }
    const publicText = publicLogText(text, lang);
    return englishArtifactFallback(publicText, fallback);
  }
  function compactTextList(...values: any[]) {
    const out: string[] = [];
    values.forEach((value) => {
      const rows = Array.isArray(value) ? value : (value === undefined || value === null || value === "" ? [] : [value]);
      rows.forEach((item) => {
        const text = stageArtifactText(item, "").trim();
        if (text && !out.some((old) => old.toLowerCase() === text.toLowerCase())) out.push(text);
      });
    });
    return out;
  }
  function ideaRiskItems(idea: any) {
    return compactTextList(idea?.risks, idea?.risk, idea?.limitations, idea?.success_gate).slice(0, 8);
  }
  function ideaWorkflowStatus(idea: any) {
    const status = String(idea?.status || idea?.recommendation || "").toLowerCase();
    if (["deleted", "rejected", "reject", "archived"].includes(status)) return "deleted";
    if (status === "pending" || status === "todo" || status === "draft") return "pending";
    if (status === "approved") return "approved";
    return status || "pending";
  }
  function ideaWorkflowStatusLabel(status: string) {
    if (status === "approved") return lang === "zh" ? "通过" : "approved";
    if (status === "deleted") return lang === "zh" ? "删除" : "deleted";
    return lang === "zh" ? "待定" : "pending";
  }
  function ideaStatusText(idea: any) {
    return ideaWorkflowStatusLabel(ideaWorkflowStatus(idea));
  }
  function ideaSourceText(idea: any) {
    const papers = ideaEvidencePapers(idea).length;
    const gates = ideaRiskItems(idea).length;
    const parts = [
      lang === "zh" ? "精读后形成" : "from current reading",
      papers ? (lang === "zh" ? "支撑论文 " + papers : papers + " supporting papers") : "",
      gates ? (lang === "zh" ? "验证项 " + gates : gates + " validation items") : "",
    ].filter(Boolean);
    return parts.join(" / ");
  }
  function localizedStageField(row: any, key: string, fallback = "") {
    if (!row || typeof row !== "object") return fallback;
    const i18n = row[`${key}_i18n`];
    const direct = i18n && typeof i18n === "object" ? String(i18n[lang] ?? "").trim() : "";
    const suffixed = String(row[`${key}_${lang}`] ?? "").trim();
    const raw = direct || suffixed || (lang === "zh" ? String(row[key] ?? "").trim() : "");
    if (!raw) return fallback;
    const text = displayArtifactText(raw, fallback);
    if (lang === "en" && containsCJKText(text)) return fallback;
    return text || fallback;
  }
  function ideaTitleText(idea: any, index: number) {
    const title = localizedStageField(idea, "title", "") || displayArtifactText(idea?.title || "", "");
    if (lang === "en" && (!title || containsCJKText(title))) return `Idea ${index + 1}: current-reading candidate`;
    return title || (lang === "zh" ? `想法 ${index + 1}` : `Idea ${index + 1}`);
  }
  function planTitleText(plan: any, index: number) {
    const title = localizedStageField(plan, "title", "") || displayArtifactText(plan?.title || planMarkdownTitles[String(plan?.plan_id || "")] || "", "");
    if (lang === "en" && (!title || containsCJKText(title))) return `Plan ${index + 1}: current-reading experiment plan`;
    return title || String(plan?.plan_id || "") || (lang === "zh" ? `计划 ${index + 1}` : `Plan ${index + 1}`);
  }
  function planHypothesisText(plan: any) {
    return localizedStageField(plan, "hypothesis", lang === "zh" ? "" : "Planning hypothesis from current reading; it needs environment, data, and experiment evidence before it can support claims.");
  }
  function planIdeaLabel(plan: any) {
    const ideaId = String(plan?.idea_id || "").trim();
    if (!ideaId) return "";
    const index = ideas.findIndex((idea: any, ideaIndex: number) => {
      const keys = [ideaKey(idea, ideaIndex), idea?.id, idea?.idea_id, idea?.title].map((value) => String(value || "").trim());
      return keys.includes(ideaId);
    });
    if (index < 0) return "";
    const label = ideaTitleText(ideas[index], index);
    return label ? (lang === "zh" ? `对应想法 ${label}` : `idea ${label}`) : "";
  }
  function cleanEnglishExperimentRecordText(value: any) {
    return publicLogText(value, "en")
      .replace(/，/g, ", ")
      .replace(/最好记录/g, "best record")
      .replace(/paper\.research project agent/gi, "paper. Writing controller")
      .replace(/research project agentmust/gi, "module controller must")
      .replace(/project agentmust/gi, "module controller must")
      .replace(/candidate experimentobservation/gi, "candidate experiment observation")
      .replace(/candidate experimentsobservation/gi, "candidate experiment observation")
      .replace(/\s+/g, " ")
      .trim();
  }
  function experimentRecordText(value: any, fallback = t.noData) {
    const text = displayMaybe(value, fallback);
    if (lang === "en") {
      const translated = cleanEnglishExperimentRecordText(text);
      if (translated && !containsCJKText(translated)) return translated;
      const noBeat = text.match(/^当前\s+(.+?)\s+在\s+(.+?)\s+上没有超过可比基线（([^）]+)）/);
      if (noBeat) return "This " + cleanEnglishExperimentRecordText(noBeat[1]) + " run on " + noBeat[2] + " did not beat the comparable reference (" + cleanEnglishExperimentRecordText(noBeat[3]) + "); it cannot support paper conclusions yet and needs a different idea or redesigned experiment.";
      if (/检验当前选中基底下的候选实验是否能超过当前参考复现/.test(text)) return "Test whether candidate experiments under the current route can outperform the reference reproduction.";
      if (/检验修复梯度流后的/.test(text)) return "Test whether the repaired candidate variant can outperform the reference reproduction.";
      if (/候选实验观察记录/.test(text)) return "Candidate experiment observation record; reference reproduction remains the comparison control; do not claim improvement from this record.";
      if (/未记录坏例切片/.test(text)) return "bad-case slices not recorded";
      if (/未通过\/未记录/.test(text)) return "failed or not recorded";
      if (containsCJKText(text)) return "Current-route experiment record; audit status, metrics, bad cases, and evidence paths remain visible here.";
    }
    return text;
  }
  function planStatusText(plan: any) {
    return displayValue(plan?.status || plan?.recommendation || "pending");
  }
  function planMetaText(plan: any, versions: any[], papers: any[], gates: any[]) {
    const parts = [
      planStatusText(plan),
      papers.length ? (lang === "zh" ? "支撑论文 " + papers.length : papers.length + " supporting papers") : "",
      gates.length ? (lang === "zh" ? "成功门槛 " + gates.length : gates.length + " success gates") : "",
      versions.length ? (lang === "zh" ? versions.length + " 轮修订" : versions.length + " revisions") : "",
    ].map((item) => String(item || "").trim()).filter((item) => item && item !== "/" && item !== "0");
    return parts.length ? parts.join(" / ") : (lang === "zh" ? "等待环境审查后执行" : "waiting for environment review");
  }
  function latestPlanVersion(plan: any) {
    const versions = asArray(plan?.versions);
    return versions[versions.length - 1] || {};
  }
  const selectedRunArtifacts = useMemo(() => artifacts.filter((a) => a.kind === "markdown" && !HIDDEN_RUN_ARTIFACTS.has(a.name)), [artifacts]);
  const currentFindMarkdownArtifacts = useMemo(() => currentFindArtifactSource.filter((a) => a.kind === "markdown" && !HIDDEN_RUN_ARTIFACTS.has(a.name)), [currentFindArtifactSource]);
  const currentProjectFindMarkdownArtifacts = useMemo(() => currentFindArtifacts.filter((a) => a.kind === "markdown" && !HIDDEN_RUN_ARTIFACTS.has(a.name)), [currentFindArtifacts]);
  const researchArtifacts = useMemo(() => asArray(researchSummary?.artifacts), [researchSummary]);
  const visibleRunArtifacts = useMemo(() => {
    const showStableCurrentFindArtifacts = Boolean(
      FIND_RUN_ARTIFACT_TABS.includes(tab)
      && currentProjectFindRunId
      && currentProjectFindMarkdownArtifacts.length > 0
      && (viewingActiveIncompleteFindRun || !viewingSelectedHistoricalFindRun)
    );
    const sourceArtifacts = showStableCurrentFindArtifacts ? currentProjectFindMarkdownArtifacts : selectedRunArtifacts;
    const sourceRunId = showStableCurrentFindArtifacts ? currentProjectFindRunId : runId;
    if (!FIND_RUN_ARTIFACT_TABS.includes(tab) && String(runId || "").startsWith("find_")) return [];
    return sourceArtifacts
      .filter((artifact) => artifactBelongsToCurrentFindRun(artifact, sourceRunId))
      .filter((artifact) => artifactVisibleForTab(artifact, tab));
  }, [currentProjectFindMarkdownArtifacts, currentProjectFindRunId, selectedRunArtifacts, runId, tab, viewingActiveIncompleteFindRun, viewingCurrentProjectFindRun, viewingSelectedHistoricalFindRun]);
  const visibleRunArtifactsRunId = useMemo(() => {
    if (FIND_RUN_ARTIFACT_TABS.includes(tab) && currentProjectFindRunId && currentProjectFindMarkdownArtifacts.length > 0 && (viewingActiveIncompleteFindRun || !viewingSelectedHistoricalFindRun)) return currentProjectFindRunId;
    return runId;
  }, [currentProjectFindMarkdownArtifacts.length, currentProjectFindRunId, runId, tab, viewingActiveIncompleteFindRun, viewingCurrentProjectFindRun, viewingSelectedHistoricalFindRun]);
  const claudeStatus = useMemo(() => researchSummary?.claude_status || researchSummary?.state?.claude_status || {}, [researchSummary]);
  const latestClaudeReceiptsByStage = useMemo(() => {
    const stages: ("environment" | "experiment" | "paper")[] = ["environment", "experiment", "paper"];
    const primary = (claudeStatus as any)?.latest_receipt_by_stage;
    const direct = (researchSummary as any)?.claude_status?.latest_receipt_by_stage;
    const nested = (researchSummary as any)?.state?.claude_status?.latest_receipt_by_stage;
    const receipts = primary || direct || nested || {};
    if (!receipts || typeof receipts !== "object") return {};
    return stages.reduce((out, stage) => {
      const receipt = (receipts as any)[stage];
      if (receipt && typeof receipt === "object") (out as any)[stage] = receipt;
      return out;
    }, {} as Record<string, any>);
  }, [researchSummary, claudeStatus]);
  function latestClaudeReceiptForStage(stage: "environment" | "experiment" | "paper") {
    const receipt = (latestClaudeReceiptsByStage as any)?.[stage] || {};
    return receipt && typeof receipt === "object" ? receipt : {};
  }
  function claudeFullResponseKeyForStage(stage: "environment" | "experiment" | "paper", receipt: any) {
    const available = Boolean(receipt?.full_response_available || receipt?.raw_response_hidden || receipt?.content_compacted);
    if (!researchProject || !available) return "";
    return `${researchProject || "project"}:${stage}:${receipt?.stage_session_key || receipt?.session_id || receipt?.finished_at || receipt?.stage || "latest"}`;
  }
  const latestClaudeFullResponseRequests = useMemo(() => {
    const stages: ("environment" | "experiment" | "paper")[] = ["environment", "experiment", "paper"];
    return stages.map((stage) => {
      const receipt = latestClaudeReceiptForStage(stage) as any;
      const available = Boolean(receipt?.full_response_available || receipt?.raw_response_hidden || receipt?.content_compacted);
      return { stage, key: claudeFullResponseKeyForStage(stage, receipt), available };
    }).filter((item) => item.key || item.available);
  }, [latestClaudeReceiptsByStage, researchProject]);
  const researchExperiments = useMemo(() => firstNonEmptyArray(researchSummary?.state?.recent_experiments, researchStages?.experiment?.recent_experiments, researchSummary?.state?.experiments, researchStages?.experiment?.experiments), [researchSummary, researchStages]);
  const researchExperimentTotalCount = useMemo(() => Number(researchStages?.experiment?.experiment_count ?? researchSummary?.state?.experiment_count ?? researchExperiments.length) || 0, [researchSummary, researchStages, researchExperiments]);
  const researchExperimentCompletedCount = useMemo(() => Number(researchStages?.experiment?.completed_experiment_count ?? researchSummary?.state?.completed_experiment_count ?? researchExperiments.filter((row: any) => String(row.status).toLowerCase() === "completed").length) || 0, [researchSummary, researchStages, researchExperiments]);
  const showExperimentSummaryCount = Boolean(researchStages?.experiment?.show_experiment_summary_count ?? researchSummary?.state?.show_experiment_summary_count ?? false);
  const experimentCountLabel = displayMaybe(researchStages?.experiment?.experiment_count_label ?? researchSummary?.state?.experiment_count_label, lang === "zh" ? "实验/复现审计记录" : "Experiment/reproduction audit records");
  const experimentCountHelp = displayMaybe(researchStages?.experiment?.experiment_count_help ?? researchSummary?.state?.experiment_count_help, "");
  const showSyntheticSmokeWarning = Boolean(researchStages?.experiment?.show_synthetic_smoke_warning ?? researchSummary?.state?.show_synthetic_smoke_warning ?? false);
  const experimentRecord = useMemo(() => researchStages?.experiment?.experiment_record || researchSummary?.state?.experiment_record || {}, [researchSummary, researchStages]);
  const fallbackExperimentRecordRows = useMemo(() => researchExperiments.map((row: any) => ({
    "时间": row.finished_at || row.timestamp || row.started_at || "",
    "实验ID": row.experiment_id || row.name || "",
    "实验目的": row.human_goal || row.goal || row.hypothesis || row.notes || "",
    "方法/变体": row.method || row.method_slug || "",
    "仓库": row.repo || row.repo_path || "",
    "数据集": row.dataset || row.benchmark || "",
    "运行环境": row.env_name || "",
    "关键配置/命令": row.command || row.command_display || row.config_summary || "",
    "指标": experimentMetricRows(row).map((metric) => `${metric.key}=${metric.value}`).join("; "),
    "坏例/切片": displayMaybe(row.bad_case_path || row.slice_report || row.counterexample_outcome, ""),
    "审计状态": row.audit_ready ? (lang === "zh" ? "通过：证据文件齐全" : "audit-ready") : String(row.status || "not_audited").replace(/_/g, " "),
    "结论/反思": row.reflection || row.claim_verdict || row.result || "",
    "下一步行动": row.next_action || "",
    "证据路径": row.artifact_path || row.audit_path || row.metrics_path || "",
  })), [researchExperiments, lang]);
  const experimentRecordRows = useMemo(() => {
    const rows = asArray(experimentRecord?.rows);
    return rows.length ? rows : fallbackExperimentRecordRows;
  }, [experimentRecord, fallbackExperimentRecordRows]);
  const experimentRecordTotalCount = useMemo(() => Number(experimentRecord?.row_count ?? experimentRecordRows.length) || 0, [experimentRecord, experimentRecordRows]);
  const experimentRowsNewest = useMemo(() => researchExperiments, [researchExperiments]);
  const experimentRowsByTime = useMemo(() => {
    const map = new Map<string, any>();
    researchExperiments.forEach((row: any) => {
      const key = String(row.finished_at || row.timestamp || row.started_at || "").trim();
      if (key && !map.has(key)) map.set(key, row);
    });
    return map;
  }, [researchExperiments]);
  const trajectorySystem = useMemo(() => researchSummary?.trajectory_system || researchSummary?.state?.trajectory_system || researchStages?.experiment?.trajectory_system || {}, [researchSummary, researchStages]);
  const referenceReproductionGate = useMemo(() => researchSummary?.state?.reference_reproduction_gate || researchStages?.experiment?.reference_reproduction_gate || {}, [researchSummary, researchStages]);
  const scientificProgressGate = useMemo(() => researchSummary?.state?.scientific_progress_gate || researchStages?.experiment?.scientific_progress_gate || {}, [researchSummary, researchStages]);
  const experimentIterationAudit = useMemo(() => researchSummary?.state?.experiment_iteration_audit || researchStages?.experiment?.experiment_iteration_audit || {}, [researchSummary, researchStages]);
  const humanGateSummary = useMemo(() => researchSummary?.human_gate_summary || researchSummary?.state?.human_gate_summary || researchStages?.experiment?.human_gate_summary || humanSupervision?.gate_summary || {}, [researchSummary, researchStages, humanSupervision]);
  const paperGlobalEvidenceGateBlocked = useMemo(() => {
    const blocker = humanSupervision?.blocker || researchSummary?.current_blocker || {};
    const category = String(blocker?.category || humanGateSummary?.category || "").toLowerCase();
    const statuses = [
      humanGateSummary?.status,
      humanGateSummary?.scientific_progress?.status,
      scientificProgressGate?.status,
      (researchSummary as any)?.status,
    ].map((value) => String(value || "").trim().toLowerCase()).filter(Boolean);
    const gateText = [
      category,
      blocker?.summary,
      blocker?.human_summary,
      blocker?.issue,
      humanGateSummary?.summary,
      humanGateSummary?.title,
      humanGateSummary?.scientific_progress?.summary,
    ].map((value) => String(value || "").toLowerCase()).join(" ");
    const evidenceCategory = category.includes("experiment_evidence")
      || category.includes("scientific_progress")
      || category.includes("paper_evidence")
      || category.includes("submission_readiness");
    const evidenceText = /evidence|scientific progress|experiment|submission|候选实验|科学进展|证据|投稿/.test(gateText);
    const blockedStatus = statuses.some((value) => value === "blocked" || value.startsWith("blocked"));
    return Boolean((evidenceCategory || evidenceText) && blockedStatus);
  }, [humanSupervision, researchSummary, humanGateSummary, scientificProgressGate]);
  const paperGlobalEvidenceGateText = useMemo(() => (
    humanReadableMaybe(
      humanGateSummary?.summary || humanSupervision?.blocker?.summary || researchSummary?.current_blocker?.summary,
      t.evidenceGateWarning,
    )
  ), [humanGateSummary, humanSupervision, researchSummary, t.evidenceGateWarning]);
  const researchRuntime = useMemo(() => researchSummary?.runtime || researchSummary?.state?.runtime || {}, [researchSummary]);
  const currentFindPipeline = useMemo(() => researchSummary?.current_find_pipeline || researchLiteratureSurvey?.current_find_pipeline || {}, [researchSummary, researchLiteratureSurvey]);
  const publicFindStage = useMemo(() => researchStages?.find || researchSummary?.state?.stages?.find || {}, [researchStages, researchSummary]);
  const publicReadStage = useMemo(() => researchStages?.read || researchSummary?.state?.stages?.read || {}, [researchStages, researchSummary]);
  const publicReadCounts = useMemo(() => {
    const positiveNumber = (...values: any[]) => {
      for (const value of values) {
        const numeric = Number(value);
        if (Number.isFinite(numeric) && numeric > 0) return numeric;
      }
      return 0;
    };
    const nonNegativeNumber = (...values: any[]) => {
      for (const value of values) {
        const numeric = Number(value);
        if (Number.isFinite(numeric) && numeric >= 0) return numeric;
      }
      return 0;
    };
    const recommended = positiveNumber(
      publicReadStage?.recommended_count,
      currentFindPipeline?.recommended_count,
      currentFindPipeline?.strong_count,
      currentFindPipeline?.strong_recommendations_count,
      researchLiteratureCounts.strong_recommendations,
      researchLiteratureCounts.recommended,
    );
    const displayed = positiveNumber(
      publicReadStage?.reading_count,
      currentFindPipeline?.displayed_count,
      currentFindPipeline?.reading_count,
      currentFindPipeline?.read_count,
      currentFindPipeline?.readings,
      researchLiteratureCounts.readings,
    );
    const fullText = positiveNumber(
      publicReadStage?.full_text_reading_count,
      currentFindPipeline?.full_text_reading_count,
      researchSummary?.full_text_reading_count,
      researchLiteratureCounts.full_text_reading_count,
      displayed,
    );
    const pending = nonNegativeNumber(
      publicReadStage?.pending_full_text_reading_count,
      currentFindPipeline?.pending_full_text_reading_count,
      researchSummary?.pending_full_text_reading_count,
      researchLiteratureCounts.pending_full_text_reading_count,
    );
    return { recommended, displayed, fullText, pending };
  }, [researchLiteratureCounts, researchSummary, currentFindPipeline, publicReadStage]);
  const publicReadSummaryLoaded = Boolean(researchSummary && (publicReadStage?.run_id || currentFindPipeline?.run_id || researchLiteratureSurvey?.run_id));
  const publicReadLoadingText = lang === "zh" ? "加载中" : "Loading";
  const publicIdeaStage = useMemo(() => researchStages?.idea || researchSummary?.state?.stages?.idea || {}, [researchStages, researchSummary]);
  const publicPlanStage = useMemo(() => researchStages?.plan || researchSummary?.state?.stages?.plan || {}, [researchStages, researchSummary]);
  const freshFindRunning = useMemo(() => {
    const statuses = [
      researchLiteratureSurvey?.status,
      researchLiteratureSurvey?.recommendation_gate_status,
      currentFindPipeline?.status,
      researchStages?.experiment?.status,
      researchStages?.paper?.status,
      humanSupervision?.blocker?.category,
    ];
    const hasLiveStandaloneFindJob = displayJobs.some((job: any) => isFindRunJob(job) && isLiveJob(job));
    return viewingActiveIncompleteFindRun || hasLiveStandaloneFindJob || statuses.some((value) => String(value ?? "").trim().toLowerCase() === "fresh_find_running");
  }, [researchLiteratureSurvey, currentFindPipeline, researchStages, humanSupervision, displayJobs, viewingActiveIncompleteFindRun]);
  const currentFindPipelineSummary = useMemo(() => {
    if (freshFindRunning) return lang === "zh" ? "新的 Find 正在运行；等待本轮评分、推荐精读、想法和计划产物落盘。" : "Fresh Find is running; waiting for this run's scoring, recommended reading, ideas, and plans.";
    const findCounts = publicFindStage?.counts || {};
    const recommended = currentFindPipeline?.recommended_count ?? findCounts.recommended ?? researchLiteratureCounts.strong_recommendations ?? researchStrongRecommendations.length ?? 0;
    const readings = currentFindPipeline?.reading_count ?? publicReadStage?.reading_count ?? currentFindPipeline?.readings ?? 0;
    const ideasCount = currentFindPipeline?.idea_count ?? publicIdeaStage?.idea_count ?? currentFindPipeline?.ideas ?? 0;
    const plansCount = currentFindPipeline?.plan_count ?? publicPlanStage?.plan_count ?? currentFindPipeline?.plans ?? 0;
    const status = displayValue(currentFindPipeline?.status || (currentFindPipeline?.takeover_ready ? "claude_takeover_ready" : "not_ready"));
    const countLine = lang === "zh"
      ? `推荐 ${displayMaybe(recommended, "0")} / 精读 ${displayMaybe(readings, "0")} / 想法 ${displayMaybe(ideasCount, "0")} / 计划 ${displayMaybe(plansCount, "0")}`
      : `recommendations ${displayMaybe(recommended, "0")} / readings ${displayMaybe(readings, "0")} / ideas ${displayMaybe(ideasCount, "0")} / plans ${displayMaybe(plansCount, "0")}`;
    const summary = localizedField(publicFindStage, "summary", "") || currentFindPipeline?.summary_zh || currentFindPipeline?.summary_en || "";
    return [status, countLine, displayMaybe(summary, "")].filter(Boolean).join("; ");
  }, [currentFindPipeline, publicFindStage, publicReadStage, publicIdeaStage, publicPlanStage, researchLiteratureCounts, researchStrongRecommendations, freshFindRunning, lang]);
  const researchFullCycle = useMemo(() => researchSummary?.full_research_cycle || researchSummary?.state?.full_research_cycle || researchStages?.experiment?.full_research_cycle || {}, [researchSummary, researchStages]);
  const researchFullCycleJob = useMemo(() => researchFullCycle?.full_cycle_job || {}, [researchFullCycle]);
  const liveFullCycleJobFromJobs = useMemo(() => jobs.find((job) => {
    const status = String(job?.status || "").trim().toLowerCase();
    if (!isLiveJob(job) || isStoppedWorkflowStatus(status) || jobProcessAliveValue(job) === false) return false;
    const stage = String(job.stage || "").toLowerCase();
    const jobId = String(job.job_id || "").toLowerCase();
    const command = String(job.result?.command || job.result?.cmd || "").toLowerCase();
    const project = String(job.result?.project || researchProject || "");
    const matchesProject = !researchProject || !project || project === researchProject;
    return matchesProject && (jobId.includes("full-cycle") || stage.includes("full-cycle") || command.includes("run_full_research_cycle.py"));
  }), [jobs, researchProject]);
  const fullCycleProcessAlive = useMemo(() => {
    if (liveFullCycleJobFromJobs) return true;
    const status = String(researchFullCycleJob?.status || "").trim().toLowerCase();
    const pid = String(researchFullCycleJob?.pid || "").trim();
    if (researchFullCycleJob?.process_alive === true || researchFullCycleJob?.alive === true) return true;
    if (isStoppedWorkflowStatus(status) || researchFullCycleJob?.process_alive === false || researchFullCycleJob?.alive === false) return false;
    return Boolean(pid && status === "running");
  }, [researchFullCycleJob, liveFullCycleJobFromJobs]);
  const fullCycleLaunchDisabled = Boolean(!researchProject || fullCycleProcessAlive);
  const stageLaunchDisabledByFullCycle = Boolean(fullCycleProcessAlive);
  const liveProjectStageJob = useMemo(() => {
    const exclusiveStages = new Set(["environment", "experiment", "paper"]);
    return jobs.find((job) => {
      const status = String(job?.status || "").trim().toLowerCase();
      if (!isLiveJob(job) || isStoppedWorkflowStatus(status) || jobProcessAliveValue(job) === false) return false;
      const stage = String(job.stage || "").toLowerCase();
      if (!exclusiveStages.has(stage)) return false;
      const result = (job.result && typeof job.result === "object") ? job.result : {};
      const project = String(result.project || "").trim();
      return !researchProject || !project || project === researchProject || String(job.job_id || "").includes(researchProject);
    }) || null;
  }, [jobs, researchProject]);
  const environmentStageRunning = String(liveProjectStageJob?.stage || "").toLowerCase() === "environment";
  const experimentStageRunning = String(liveProjectStageJob?.stage || "").toLowerCase() === "experiment";
  const paperStageRunning = String(liveProjectStageJob?.stage || "").toLowerCase() === "paper";
  const stageLaunchDisabledByProjectWorker = Boolean(liveProjectStageJob);
  const stageLaunchLockedText = useMemo(() => {
    if (!stageLaunchDisabledByFullCycle) return "";
    return lang === "zh"
      ? "完整科研流程正在运行；网页已锁定新的 Find/Read/Idea/Plan/环境/实验/论文启动按钮，避免并发重复任务。需要人工介入时，请在对应阶段的模块主控指令框提交。"
      : "The full research cycle is running; new Find/Read/Idea/Plan/environment/experiment/paper launches are locked to avoid duplicate concurrent jobs. Use the stage guidance box to queue intervention for the active workflow.";
  }, [stageLaunchDisabledByFullCycle, lang]);
  const fullCycleRunningText = useMemo(() => {
    if (!fullCycleProcessAlive) return "";
    const liveResult = liveFullCycleJobFromJobs?.result || {};
    const pid = String(researchFullCycleJob?.pid || liveResult?.pid || "").trim();
    const stage = displayValue(liveResult?.phase || liveResult?.raw_stage || researchFullCycleJob?.stage || researchFullCycle?.latest_step?.phase || researchFullCycle?.latest_step?.stage || "full-cycle");
    const logPath = String(researchFullCycleJob?.log_path || researchFullCycleJob?.stdout_path || liveResult?.log_path || "").trim();
    const parts = [pid ? `PID=${pid}` : "", stage ? `${lang === "zh" ? "阶段" : "phase"}: ${stage}` : "", logPath ? `${lang === "zh" ? "日志" : "log"}: ${logPath}` : ""].filter(Boolean);
    return parts.join("; ");
  }, [researchFullCycle, researchFullCycleJob, fullCycleProcessAlive, lang, liveFullCycleJobFromJobs]);
  const literatureGateBlocked = useMemo(() => {
    if (freshFindRunning) return true;
    const values = [
      (researchSummary as any)?.status,
      humanSupervision?.status,
      researchStages?.experiment?.status,
      researchFullCycle?.status,
      researchLiteratureSurvey?.recommendation_gate_status,
    ];
    return values.some((value) => {
      const text = String(value ?? "").trim().toLowerCase();
      return text === "blocked_literature_recommendation_gate" || text === "blocked_literature_llm_quota_exhausted" || text === "blocked_llm_quota_exhausted" || text === "shortfall" || text.includes("literature_recommendation_gate") || text.includes("llm_quota_exhausted");
    });
  }, [researchSummary, humanSupervision, researchStages, researchFullCycle, researchLiteratureSurvey, freshFindRunning]);
  const literatureGateShortfallText = useMemo(() => {
    if (freshFindRunning) return lang === "zh" ? "新的 Find 正在运行；等待本轮检索、详情抓取、LLM 评分和后续产物落盘。" : "Fresh Find is running; waiting for retrieval, details, LLM scoring, and downstream artifacts.";
    const llmBlocked = [(researchSummary as any)?.status, humanSupervision?.status, researchLiteratureSurvey?.status].some((value) => String(value || "").toLowerCase().includes("llm_quota_exhausted"));
    if (llmBlocked) return lang === "zh" ? "LLM API 额度/配置不可用；Find 不能进行必需的摘要打分。" : "LLM API quota/config is unavailable; Find cannot perform required abstract scoring.";
    const target = Number(researchLiteratureCounts.recommendation_target_count || researchLiteratureSurvey?.recommendation_target_count || 0);
    const strong = Number(researchLiteratureCounts.strong_recommendations || researchStrongRecommendations.length || 0);
    const shortfall = Number(researchLiteratureCounts.recommendation_shortfall || researchLiteratureSurvey?.recommendation_shortfall || (target ? Math.max(0, target - strong) : 0));
    if (!target) return lang === "zh" ? "当前 Find 推荐门控未通过。" : "The current Find recommendation gate has not passed.";
    return lang === "zh"
      ? `当前 Find 推荐文章 ${strong}/${target}${shortfall ? `，短缺 ${shortfall}` : ""}。`
      : `Current Find recommended papers: ${strong}/${target}${shortfall ? `, short by ${shortfall}` : ""}.`;
  }, [researchLiteratureCounts, researchLiteratureSurvey, researchStrongRecommendations, freshFindRunning, lang]);
  const newFindBlockedByLiteratureGate = Boolean(researchProject && literatureGateBlocked);
  const newFindBlockedReason = useMemo(() => (
    lang === "zh"
      ? `${literatureGateShortfallText}当前 系统会通过统一 literature tool 受控补检索/补评分；门控通过前不会推进实验、论文或 claim。`
      : `${literatureGateShortfallText} The workflow will use the unified literature tool for controlled targeted retrieval/scoring repair; experiments, paper, and claims stay blocked until the gate passes.`
  ), [lang, literatureGateShortfallText]);
  const globalLiteratureRepairStatus = useMemo(() => {
    const status = humanSupervision?.literature_repair?.targeted_search_tool_status;
    return status && typeof status === "object" ? status : {};
  }, [humanSupervision]);
  const globalLLMRepairBlockerText = useMemo(() => {
    const text = String(globalLiteratureRepairStatus?.failure_summary || globalLiteratureRepairStatus?.error || "").trim();
    const statusText = String(globalLiteratureRepairStatus?.status || "").trim();
    const haystack = `${statusText} ${text}`;
    if (!haystack.trim()) return "";
    if (/401|invalid api key/i.test(haystack)) {
      return lang === "zh"
        ? "LLM 401 Invalid API Key；请在上方 LLM 配置保存有效 key 并验证通过后，流程才能继续受控补检索/补评分。"
        : "LLM 401 Invalid API Key; save and validate a working key before TASTE can continue controlled retrieval/scoring repair.";
    }
    if (/429|quota|rate[- ]?limit|too many requests/i.test(haystack)) {
      return lang === "zh"
        ? "LLM API 额度/限流导致补检索或补评分暂停；请等待额度恢复，或在上方保存并验证可用配置。"
        : "LLM API quota/rate limit paused controlled retrieval or scoring repair; wait for quota recovery or save and validate a working config above.";
    }
    if (/blocked_llm|llm_quota/i.test(statusText)) {
      return lang === "zh"
        ? "LLM API 配置或额度不可用；Find 的受控补检索/补评分会保持阻塞。"
        : "LLM API config or quota is unavailable; controlled Find repair remains blocked.";
    }
    return "";
  }, [globalLiteratureRepairStatus, lang]);
  const literatureGateExperimentSummary = useMemo(() => (
    lang === "zh"
      ? freshFindRunning
        ? `实验阶段等待：${literatureGateShortfallText}本轮 Find 完成前不会启动新的复现、实验、论文写作或论文结论提升。`
        : `实验阶段已暂停：${literatureGateShortfallText}请在“发现”页查看本轮 Find 调研验收；通过前不会启动新的复现、实验、论文写作或论文结论提升。`
      : freshFindRunning
        ? `Experiment waits: ${literatureGateShortfallText} Reproduction, experiments, paper writing, and paper-conclusion gating will not start until this Find run finishes.`
        : `Experiment is paused: ${literatureGateShortfallText} Review the current Find audit on the Find page; reproduction, experiments, paper writing, and paper-conclusion gating stay blocked until it passes.`
  ), [lang, literatureGateShortfallText, freshFindRunning]);
  const experimentSummaryStatus = freshFindRunning ? "fresh_find_running" : literatureGateBlocked ? "blocked_literature_recommendation_gate" : (researchStages?.experiment?.status || "not_started");
  const experimentSummaryTitle = freshFindRunning ? (lang === "zh" ? "等待 Find 结果" : "Waiting For Find") : literatureGateBlocked ? (lang === "zh" ? "实验阶段暂停" : "Experiment Paused") : t.currentExperimentSummary;
  const experimentSummaryText = literatureGateBlocked ? literatureGateExperimentSummary : localizedField(researchStages?.experiment, "module_summary", localizedField(researchStages?.experiment, "summary", t.noExperimentRun));
  const experimentNextActionText = localizedField(researchStages?.experiment, "next_action", "");
  const experimentCompletedLabel = literatureGateBlocked ? (lang === "zh" ? "旧实验记录" : "Old experiment records") : (lang === "zh" ? "审计就绪记录" : "Audit-ready records");
  const researchCurrentBlockers = useMemo(() => {
    const rows = firstNonEmptyArray(researchSummary?.blockers, researchFullCycle?.latest_blockers, researchFullCycle?.blocker_action_plan?.actions);
    return rows.filter((row: any) => {
      const text = displayMaybe(row?.human_summary || row?.summary || row?.issue || row, "");
      return Boolean(humanReadableMaybe(text, ""));
    });
  }, [researchSummary, researchFullCycle, lang]);
  const researchNextActions = useMemo(() => firstNonEmptyArray(researchSummary?.next_actions, researchFullCycle?.blocker_action_plan?.actions), [researchSummary, researchFullCycle]);
  const runtimeChecks = useMemo(() => researchRuntime?.checks || {}, [researchRuntime]);
  const summaryEnvironmentDraft = useMemo(() => environmentDraftFromSummary(researchSummary), [researchSummary]);
  const effectiveResearchEnvDraft = useMemo(() => ({
    ...summaryEnvironmentDraft,
    ...researchEnvDraft,
    conda_env: researchEnvDraft.conda_env || summaryEnvironmentDraft.conda_env || "",
    conda_base: researchEnvDraft.conda_base || summaryEnvironmentDraft.conda_base || "",
    experiment_python: researchEnvDraft.experiment_python || summaryEnvironmentDraft.experiment_python || "",
    python_executable: researchEnvDraft.python_executable || summaryEnvironmentDraft.python_executable || "",
  }), [researchEnvDraft, summaryEnvironmentDraft]);
  const supervisionTick = useMemo(() => researchSummary?.supervision || humanSupervision?.supervision || {}, [researchSummary, humanSupervision]);
  const claudeCurrentFindState = useMemo(() => supervisionTick?.claude_current_find_state || humanSupervision?.supervision?.claude_current_find_state || {}, [supervisionTick, humanSupervision]);
  const claudeCurrentFindStale = Boolean(claudeCurrentFindState?.takeover_stale || claudeCurrentFindState?.reading_validation_stale);
  const mainRouteRepoName = useMemo(() => String(humanSupervision?.main_route?.repo_name || humanSupervision?.main_route?.base_title || "").trim(), [humanSupervision]);
  const currentMainExperimentRecordRows = useMemo(() => {
    const repoKey = mainRouteRepoName.toLowerCase();
    if (!repoKey) return experimentRecordRows;
    const repoTail = repoKey.split("/").pop() || repoKey;
    return experimentRecordRows.filter((row: any) => {
      const haystack = [
        row?.["仓库"],
        row?.["方法/变体"],
        row?.["实验ID"],
        row?.["实验目的"],
        row?.["关键配置/命令"],
        row?.["证据路径"],
      ].map((value) => String(value || "").toLowerCase()).join(" ");
      return haystack.includes(repoKey) || haystack.includes(repoTail);
    });
  }, [experimentRecordRows, mainRouteRepoName]);
  const visibleExperimentRecordRows = useMemo(() => currentMainExperimentRecordRows, [currentMainExperimentRecordRows]);
  const currentMainHasNoExperimentRows = Boolean(mainRouteRepoName && experimentRecordRows.length && currentMainExperimentRecordRows.length === 0);
  const referenceGateAlreadyPassed = useMemo(() => {
    const status = String(referenceReproductionGate?.status || "").toLowerCase();
    const decision = String(referenceReproductionGate?.decision || "").toLowerCase();
    return status === "pass" || decision === "continue_base";
  }, [referenceReproductionGate]);
  const currentMainNoExperimentRowsText = useMemo(() => {
    if (referenceGateAlreadyPassed) {
      return lang === "zh"
        ? "当前路线 reference reproduction gate 已通过；当前主线训练或候选方法实验尚未产出可展开记录，等待 Experimenting 主控 Claude 刷新实验审计。"
        : "The current-base reference reproduction gate has passed; the main training or candidate-method run has not produced an expandable record yet. Waiting for the Experimenting controller to refresh the experiment audit.";
    }
    return lang === "zh"
      ? "当前路线还没有实验/参考复现记录。流程必须先完成当前路线 reference reproduction gate，之后才会启动主线实验。"
      : "The current route has no experiment/reference-reproduction record yet. The workflow must pass the reference reproduction gate before starting main experiments.";
  }, [lang, referenceGateAlreadyPassed]);
  const envStage = useMemo(() => researchStages?.environment || {}, [researchStages]);
  const envReferenceGate = useMemo(() => envStage?.reference_reproduction_gate || {}, [envStage]);
  const envReferenceFullJob = useMemo(() => envStage?.reference_full_job || {}, [envStage]);
  const envChecks = useMemo(() => asArray(envStage?.checks), [envStage]);
  const repoDetails = useMemo(() => asArray(envStage?.repo_details), [envStage]);
  const datasetDetails = useMemo(() => asArray(envStage?.dataset_details), [envStage]);
  const readyDatasetDetails = useMemo(() => asArray(envStage?.ready_dataset_details), [envStage]);
  const pendingDatasetDetails = useMemo(() => asArray(envStage?.pending_dataset_details), [envStage]);
  const blockedDatasetDetails = useMemo(() => asArray(envStage?.blocked_dataset_details), [envStage]);
  const environmentSelectionValid = Boolean(envStage?.selection?.valid);
  const activeRepo = useMemo(() => environmentSelectionValid ? (envStage?.active_repo || repoDetails.find((row: any) => row.active) || {}) : {}, [envStage, repoDetails, environmentSelectionValid]);
  const pendingEnvironmentCandidate = useMemo(() => envStage?.pending_candidate || envStage?.selection?.pending_candidate || {}, [envStage]);
  const claudeTopicDecision = useMemo(() => envStage?.claude_topic_decision || {}, [envStage]);
  const selectedProject = useMemo(() => researchProjects.find((project) => project.id === researchProject), [researchProjects, researchProject]);
  const environmentLocked = useMemo(() => Boolean(envStage?.locked || envStage?.status === "ready"), [envStage]);
  const freshBaseBlockerCategory = useMemo(() => String(humanSupervision?.blocker?.category || researchSummary?.current_blocker?.category || ""), [humanSupervision, researchSummary]);
  const freshBaseDataBlocked = useMemo(() => freshBaseBlockerCategory === "fresh_base_data_required", [freshBaseBlockerCategory]);
  const freshBaseReferenceBlocked = useMemo(() => freshBaseBlockerCategory === "fresh_base_reference_probe_required", [freshBaseBlockerCategory]);
  const freshBaseSmokeBlocked = useMemo(() => freshBaseBlockerCategory === "fresh_base_reference_smoke_required", [freshBaseBlockerCategory]);
  const freshBaseReproductionBlocked = useMemo(() => freshBaseBlockerCategory === "fresh_base_reference_reproduction_required", [freshBaseBlockerCategory]);
  const freshBaseMainBlocked = useMemo(() => freshBaseDataBlocked || freshBaseReferenceBlocked || freshBaseSmokeBlocked || freshBaseReproductionBlocked || String(humanSupervision?.status || "").startsWith("blocked_fresh_base_"), [freshBaseDataBlocked, freshBaseReferenceBlocked, freshBaseSmokeBlocked, freshBaseReproductionBlocked, humanSupervision]);
  const paperLaunchGateBlocked = Boolean(freshBaseMainBlocked || literatureGateBlocked || paperGlobalEvidenceGateBlocked);
  const referenceFullJobRunning = useMemo(() => {
    const blockerJob = String(humanSupervision?.blocker?.reference_full_job_status || "");
    const tickJob = supervisionTick?.full_reference_job || {};
    return blockerJob === "running" || (String(tickJob.status || "") === "running" && tickJob.alive !== false);
  }, [humanSupervision, supervisionTick]);
  const projectSummaryLoadingForDisplay = Boolean(researchProject && (!researchSummary || researchProjectLoading));
  const projectStatusLoadingForLaunch = Boolean(projectSummaryLoadingForDisplay || (researchProject && !jobsLoaded));
  const environmentConfigLoading = Boolean(projectSummaryLoadingForDisplay && !environmentDraftHasAnyValue(effectiveResearchEnvDraft));
  const projectStageLaunchLockedText = useMemo(() => (
    lang === "zh"
      ? "当前项目已有环境/实验/论文阶段任务正在运行或状态仍在刷新；网页已阻止新的全流程/实验/论文启动，避免并发重复任务。需要人工介入时，请在对应阶段的模块主控指令框提交。"
      : "A project environment/experiment/paper stage job is running or the project state is still refreshing; new workflow, experiment, and paper launches are locked to avoid duplicate concurrent tasks. Use the stage guidance box for intervention."
  ), [lang]);
  const workflowLaunchDisabled = Boolean(fullCycleLaunchDisabled || stageLaunchDisabledByProjectWorker || projectStatusLoadingForLaunch);
  const experimentLoopLaunchDisabled = Boolean(
    !researchProject
    || projectStatusLoadingForLaunch
    || freshBaseMainBlocked
    || referenceFullJobRunning
    || literatureGateBlocked
    || stageLaunchDisabledByFullCycle
    || stageLaunchDisabledByProjectWorker
  );
  const environmentLaunchDisabled = Boolean(
    !researchProject
    || environmentLocked
    || projectStatusLoadingForLaunch
    || environmentStageRunning
    || referenceFullJobRunning
    || stageLaunchDisabledByFullCycle
    || stageLaunchDisabledByProjectWorker
  );
  const environmentConfigDisabled = Boolean(
    environmentConfigLoading
    || environmentLocked
    || environmentStageRunning
    || experimentStageRunning
    || paperStageRunning
    || referenceFullJobRunning
    || stageLaunchDisabledByFullCycle
    || stageLaunchDisabledByProjectWorker
  );
  const environmentAgentActionDisabled = Boolean(
    !researchProject
    || projectStatusLoadingForLaunch
    || environmentStageRunning
    || experimentStageRunning
    || paperStageRunning
    || referenceFullJobRunning
    || stageLaunchDisabledByFullCycle
    || stageLaunchDisabledByProjectWorker
  );
  const referenceFullJobStatus = String(humanSupervision?.blocker?.reference_full_job_status || "").trim();
  const referenceFullJobIsRunning = referenceFullJobStatus === "running" && referenceFullJobRunning;
  const referenceFullJobPidText = humanSupervision?.blocker?.reference_full_job_pid
    ? `${referenceFullJobIsRunning ? "PID" : (lang === "zh" ? "历史 PID" : "historical PID")}=${humanSupervision.blocker.reference_full_job_pid}`
    : "";
  const referenceFullJobDetailText = displayMaybe(
    referenceFullJobPidText || humanSupervision?.blocker?.reference_full_job_log,
    lang === "zh" ? "暂无" : "N/A",
  );
  const mainRouteHumanPanelActive = useMemo(() => (
    freshBaseMainBlocked
    || referenceFullJobRunning
    || [
      "fresh_base_reference_reproduction_running",
      "fresh_base_reference_reproduction_required",
      "selected_base_viability_gate",
      "experiment_evidence_audit",
      "submission_readiness",
    ].includes(freshBaseBlockerCategory)
  ), [freshBaseMainBlocked, referenceFullJobRunning, freshBaseBlockerCategory]);


  const currentProjectArtifact = useMemo(() => {
    if (!researchArtifacts.length) return undefined;
    return researchArtifacts.find((artifact) => artifact.name === activeProjectArtifact) || researchArtifacts[0];
  }, [researchArtifacts, activeProjectArtifact]);
  const paperPreviewArtifact = useMemo(() => {
    return researchArtifacts.find((artifact) => artifact.name === "paper_revision.md")
      || researchArtifacts.find((artifact) => artifact.name === "paper_draft.md")
      || researchArtifacts.find((artifact) => artifact.name === "aggregated_review.md");
  }, [researchArtifacts]);
  function isMachineOnlyText(value: any) {
    const text = String(value ?? "").trim();
    if (!text) return true;
    const lower = text.toLowerCase();
    if (text.length > 260 && (text.includes("/home/") || text.includes(".json") || text.includes("{\"") || text.includes("\": "))) return true;
    return Boolean(
      lower.includes("research_trajectory_end_to_end_verification")
      || lower.includes("reflect live ar process state")
      || lower.includes("subprocess pid")
      || /^subprocess pid\b/i.test(text)
      || /^[-\w]+:\s*pid\s+\d+/i.test(text)
      || (text.includes("/home/") && (text.includes(".json") || text.includes(".log") || text.includes(".py")))
      || (text.startsWith("{") && text.endsWith("}"))
      || /^\"[A-Za-z0-9_]+\"\s*:/.test(text)
    );
  }
  function readableLogLines(value: any, limit = 80) {
    const rawRows = Array.isArray(value) ? value : String(value ?? "").split(/\r?\n/);
    const rows = rawRows.map((item) => String(item ?? "").trim()).filter(Boolean).filter((line) => {
      if (/^[{}\[\],]$/.test(line)) return false;
      if (/^"[^"\n]+"\s*:\s*(?:"[^"]*"|[\d.]+|true|false|null|\{|\[)\s*,?$/.test(line)) return false;
      if (/^[-\w]+:\s*running\s+\/.*\/scripts\//i.test(line)) return false;
      if (/^\/.*\/scripts\/.*\.py\b/.test(line)) return false;
      if (isMachineOnlyText(line)) return false;
      return true;
    });
    return rows.slice(-limit);
  }

  function humanReadableMaybe(value: any, fallback = "") {
    const text = displayMaybe(value, "").trim();
    if (!text || isMachineOnlyText(text)) return fallback;
    const lower = text.toLowerCase();
    const looksLikeGateDump = lower.includes("no audit-ready promotable")
      || lower.includes("bounded audit passed")
      || lower.includes("paper-level full reference reproduction")
      || lower.includes("reference_reproduction_gate")
      || lower.includes("scientific_progress_gate")
      || lower.includes("selected_base_reference_full_")
      || lower.includes("best reference reproduction")
      || lower.includes("non-promotable candidates")
      || lower.includes("paper_evidence_audit recommends")
      || lower.includes("current best candidate")
      || lower.includes("current baseline")
      || lower.includes("best_candidate")
      || lower.includes("best_control")
      || lower.includes("当前最佳候选")
      || lower.includes("当前基线")
      || lower.includes("base_switch_execution")
      || lower.includes("base_switch_gate")
      || lower.includes("selected_base_viability")
      || lower.includes("environment_claude_code")
      || lower.includes("selected-base full");
    if (looksLikeGateDump) return fallback;
    return text;
  }
  function humanCycleActionText(value: any, fallback: string) {
    const text = humanReadableMaybe(value, "");
    if (!text) return fallback;
    const normalized = text.toLowerCase();
    if (normalized === "use validated fresh literature packet and continue to reference/experiment gates") return fallback;
    return text;
  }
  function supervisionFallbackNextAction() {
    return lang === "zh"
      ? "继续监督当前完整科研循环；paper-pipeline 正在运行，等待合格 PDF 预览门控通过。"
      : "Continue supervising the active full research cycle; paper-pipeline is running and waiting for the accepted PDF preview gate to pass.";
  }
  function localizedText(value: any, fallback = t.noData) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const direct = value[lang] ?? value[lang === "zh" ? "zh_CN" : "en_US"];
      if (typeof direct === "string" && direct.trim()) return direct.trim();
    }
    const text = String(value ?? "").trim();
    return text || fallback;
  }
  function localizedField(row: any, key: string, fallback = t.noData) {
    if (!row || typeof row !== "object") return fallback;
    const clean = (text: any) => {
      const value = publicLogText(String(text ?? "").trim(), lang);
      return value || fallback;
    };
    const i18n = row[`${key}_i18n`];
    if (i18n && typeof i18n === "object") {
      const text = String(i18n[lang] ?? "").trim();
      if (text) return clean(text);
    }
    const direct = String(row[`${key}_${lang}`] ?? "").trim();
    if (direct) return clean(direct);
    return clean(localizedText(row[key], fallback));
  }
  function paperStageSummaryText(paper: any) {
    if (!paper || typeof paper !== "object") return t.unknown;
    return localizedField(paper, "summary", t.unknown);
  }
  function localizedList(row: any, key: string) {
    if (!row || typeof row !== "object") return [];
    const i18n = row[`${key}_i18n`];
    let value = i18n && typeof i18n === "object" ? i18n[lang] : undefined;
    if (value === undefined) value = row[`${key}_${lang}`];
    if (value === undefined) value = row[key];
    return asArray(value).map((item) => localizedText(item, "")).filter(Boolean);
  }
  function displayName(row: any, fallback = t.unnamed) {
    return String(row?.name || row?.dataset || row?.repo || row?.id || row?.local_path || fallback);
  }
  function displayMaybe(value: any, fallback = t.noData): string {
    if (value && typeof value === "object") {
      if (Array.isArray(value)) return value.map((item) => displayMaybe(item, "")).filter(Boolean).join(", ") || fallback;
      const readable = value.label_i18n || value.summary_i18n || value.name_i18n || value.title_i18n || value.label || value.summary || value.name || value.title || value.status || value.issue || value.path || value.url;
      if (typeof readable === "string" && isPathLikeText(readable)) return readable.trim() || fallback;
      return readable !== undefined ? publicLogText(localizedText(readable, fallback), lang) : fallback;
    }
    if (typeof value === "string" && isPathLikeText(value)) return value.trim() || fallback;
    return publicLogText(localizedText(value, fallback), lang);
  }
  function containsCJK(value: any) {
    return /[一-鿿]/.test(String(value ?? ""));
  }
  function englishArtifactFallback(value: any, fallback = "") {
    const text = publicLogText(String(value ?? "").trim(), lang);
    if (lang !== "en" || !containsCJKText(text)) return text || fallback;
    const safeFallback = String(fallback || "").trim();
    if (safeFallback && !containsCJKText(safeFallback)) return safeFallback;
    return "";
  }
  function displayArtifactText(value: any, fallback = t.noData): string {
    const text = displayMaybe(value, fallback);
    return englishArtifactFallback(text, fallback);
  }
  function localizedArtifactText(row: any, key: string, fallback = "") {
    if (!row || typeof row !== "object") return fallback;
    const i18n = row[`${key}_i18n`];
    if (i18n && typeof i18n === "object") {
      const direct = String(i18n[lang] ?? "").trim();
      if (direct) return displayArtifactText(direct, fallback);
    }
    const direct = String(row[`${key}_${lang}`] ?? "").trim();
    if (direct) return displayArtifactText(direct, fallback);
    return displayArtifactText(row[key], fallback);
  }
  function gateMetricText(row: any) {
    if (!row || typeof row !== "object") return t.noData;
    const dataset = row.dataset ? ` / ${row.dataset}` : "";
    const metricName = row.metric_name || "NDCG@10";
    const value = row.metric_value ?? row.target_value;
    const metric = value !== undefined && value !== "" && value !== null ? `${metricName}=${numberText(value)}` : `${metricName}=${t.noData}`;
    const id = humanReadableMaybe(row.display_name || row.method || "", "");
    return `${id || (lang === "zh" ? "审计记录" : "audit record")}${dataset} / ${metric}`;
  }
  function gateStatusDetail(gate: any) {
    const fallback = displayValue(gate?.decision || gate?.status || "not_started");
    return humanReadableMaybe(localizedField(gate, "human_summary", localizedField(gate, "summary", fallback)), fallback);
  }
  function pageLimitText(value: any) {
    const text = String(value ?? "").trim();
    if (!text) return t.unrestrictedLimit;
    const num = Number(text);
    if (Number.isFinite(num) && num <= 0) return t.unrestrictedLimit;
    return text;
  }
  function rangeLimitText(minValue: any, maxValue: any) {
    const minText = String(minValue ?? "").trim();
    const maxText = pageLimitText(maxValue);
    const minNum = Number(minText);
    if (maxText === t.unrestrictedLimit) return minText && Number.isFinite(minNum) && minNum > 0 ? `${minText}+` : t.unrestrictedLimit;
    return minText && Number.isFinite(minNum) && minNum > 0 ? `${minText}-${maxText}` : `<=${maxText}`;
  }
  function paperHasBlockedPreview(paper: any) {
    return Boolean(paper?.blocked_pdf_url && !paper?.pdf_url);
  }
  function paperGatePassed(value: any) {
    return ["pass", "passed", "ready", "ok", "true"].includes(String(value ?? "").trim().toLowerCase());
  }
  function paperSelfReviewEvidenceRows(paper: any) {
    return asArray(paper?.paper_self_review_evidence_blockers);
  }
  function paperSelfReviewEvidenceBlockerCount(paper: any) {
    const explicit = Number(paper?.paper_self_review_evidence_blocker_count || 0);
    const rows = paperSelfReviewEvidenceRows(paper).length;
    return Number.isFinite(explicit) && explicit > 0 ? Math.max(explicit, rows) : rows;
  }
  function paperSubmissionEvidenceBlocked(paper: any) {
    if (!paper) return false;
    if (paperSelfReviewEvidenceBlockerCount(paper) > 0) return true;
    if (paper?.paper_self_review_preview_only_ready && paper?.paper_self_review_submission_evidence_ready === false) return true;
    return false;
  }
  function paperSelfReviewDisplayStatus(paper: any) {
    const status = displayMaybe(paper?.paper_self_review_status, t.noData);
    const count = paperSelfReviewEvidenceBlockerCount(paper);
    if (count > 0) return `${status} (${lang === "zh" ? "预览通过；投稿证据阻塞" : "preview passed; submission evidence blocked"}: ${count})`;
    return status;
  }
  function paperSubmissionGateText(paper: any) {
    const count = paperSelfReviewEvidenceBlockerCount(paper);
    if (count > 0) return lang === "zh" ? `论文自审发现 ${count} 个未解决科研证据问题；PDF 仅作预览，不是投稿就绪稿。` : `Paper self-review found ${count} unresolved scientific-evidence issues; the PDF is preview-only, not submission-ready.`;
    if (paper?.paper_self_review_preview_only_ready && paper?.paper_self_review_submission_evidence_ready === false) return lang === "zh" ? "论文自审允许预览展示，但投稿证据仍未通过。" : "Paper self-review allows preview display, but submission evidence is still blocked.";
    return paper?.submission_ready ? (lang === "zh" ? "投稿准备度已通过。" : "Submission readiness passed.") : (lang === "zh" ? "投稿准备度未通过。" : "Submission readiness has not passed.");
  }
  function paperAcceptedPreviewBlocked(paper: any) {
    if (!paperHasBlockedPreview(paper)) return false;
    const status = String(paper?.status || "").toLowerCase();
    return status.includes("blocked") || (
      paperGatePassed(paper?.paper_normality_status)
      && paperGatePassed(paper?.paper_venue_format_status)
      && paperGatePassed(paper?.paper_figure_quality_status)
    );
  }
  function paperHasUnclearedQualityGate(paper: any) {
    if (!paper) return false;
    const statuses = [
      paper?.paper_normality_status,
      paper?.paper_citation_render_status,
      paper?.paper_self_review_status,
      paper?.paper_figure_quality_status,
    ];
    return statuses.some((value) => {
      const text = String(value ?? "").trim();
      return text !== "" && !paperGatePassed(text);
    }) || Boolean(paper?.blocked_preview_available) || paper?.paper_self_review_submission_evidence_ready === false;
  }
  function paperHumanStatus(paper: any) {
    if (paperSubmissionEvidenceBlocked(paper) && (paper?.pdf_url || paper?.blocked_pdf_url)) return lang === "zh" ? "论文预览可看，投稿证据阻塞" : "paper preview available; submission evidence blocked";
    if ((paper?.pdf_url || paper?.blocked_pdf_url) && paperHasUnclearedQualityGate(paper)) return lang === "zh" ? "论文预览需继续迭代" : "paper preview needs iteration";
    if (paper?.pdf_url) return lang === "zh" ? "论文预览已通过格式门控" : "paper preview passed format gates";
    if (paper?.status === "running") return displayValue("running");
    if (paperAcceptedPreviewBlocked(paper)) return lang === "zh" ? "论文预览需继续迭代" : "paper preview needs iteration";
    if (paper?.paper_generation_skipped || paper?.status === "blocked_before_paper_generation" || paper?.status === "evidence_gated_preview") return lang === "zh" ? "证据门控未通过的论文预览" : "paper preview with evidence gates uncleared";
    return displayValue(paper?.status || "not_started");
  }
  function paperPdfLabel(paper: any) {
    if (paperSubmissionEvidenceBlocked(paper) && (paper?.pdf_url || paper?.blocked_pdf_url)) return lang === "zh" ? "预览 PDF 已生成；投稿证据仍阻塞" : "preview PDF generated; submission evidence remains blocked";
    if (paper?.pdf_url && paperHasUnclearedQualityGate(paper)) return lang === "zh" ? "已生成论文预览，仍需继续质量/证据迭代" : "paper preview generated; quality/evidence iteration continues";
    if (paper?.pdf_url) return paper.status === "preview_pdf_blocked" ? t.pdfPreviewBlocked : t.pdfReadyBelow;
    if (paper?.blocked_pdf_url) return paper.status === "running" ? t.runningPdfPreviewTitle : (lang === "zh" ? "已有论文预览 PDF，仍需继续质量/证据迭代" : "paper preview PDF exists; quality/evidence iteration continues");
    return t.pdfNotGenerated;
  }
  function paperPreviewTitle(paper: any) {
    if (paperSubmissionEvidenceBlocked(paper) && (paper?.pdf_url || paper?.blocked_pdf_url)) return lang === "zh" ? "投稿证据阻塞的论文预览" : "paper preview with submission evidence blocked";
    if (paper?.pdf_url) return t.pdfPreviewTitle;
    if (paper?.blocked_pdf_url) return paper.status === "running" ? t.runningPdfPreviewTitle : t.blockedPdfPreviewTitle;
    return t.pdfPreviewTitle;
  }
  function paperCitationRenderRows(paper: any) {
    const direct = asArray(paper?.paper_citation_render_blockers);
    if (direct.length) return direct;
    return asArray(paper?.conference_preview_blockers).filter((item: any) => {
      const text = `${item?.id || ""} ${item?.public_detail || item?.detail || item || ""}`.toLowerCase();
      return text.includes("citation") || text.includes("author?") || text.includes("citet") || text.includes("引用");
    });
  }
  function paperCitationRenderIssueText(item: any) {
    return displayMaybe(item?.public_detail || item?.detail || item?.summary || item?.id || item, "");
  }
  function paperCitationRenderSummary(paper: any) {
    const rows = paperCitationRenderRows(paper);
    if (rows.length) return rows.map((item: any) => paperCitationRenderIssueText(item)).filter(Boolean).join("；");
    return displayMaybe(paper?.paper_citation_render_status, t.noData);
  }
  function paperSelfReviewRows(paper: any) {
    return [...asArray(paper?.paper_self_review_blockers), ...asArray(paper?.paper_self_review_evidence_blockers)];
  }
  function paperSelfReviewEvidenceTitle(item: any) {
    const marker = `${item?.public_title || ""} ${item?.public_summary || ""} ${item?.category || ""} ${item?.id || ""} ${item?.public_detail || ""} ${item?.detail || ""}`.toLowerCase();
    if (marker.includes("missing_empirical_validation") || marker.includes("zero empirical") || marker.includes("untested architecture")) return lang === "zh" ? "缺少新方法实验验证" : "Missing proposed-method validation";
    if (marker.includes("results_contains_untested_design_space") || marker.includes("method design space") || marker.includes("untested architectural variants")) return lang === "zh" ? "Results 含未验证设计空间" : "Results include untested design space";
    if (marker.includes("evaluation_scope_mismatch") || (marker.includes("contribution") && marker.includes("backbone"))) return lang === "zh" ? "贡献表述范围不匹配" : "Contribution scope mismatch";
    if (marker.includes("data_code_availability") || marker.includes("data availability") || marker.includes("code availability")) return lang === "zh" ? "数据/代码可用性缺少明确链接" : "Data/code availability needs explicit links";
    if (marker.includes("citation") || marker.includes("author?")) return lang === "zh" ? "引用渲染或参考文献仍需修复" : "Citation/reference rendering needs repair";
    return displayMaybe(item?.public_title || item?.public_summary || item?.summary || item?.category || item?.id, lang === "zh" ? "科研证据待补齐" : "Scientific evidence needs follow-up");
  }
  function paperSelfReviewEvidenceText(item: any) {
    const raw = displayMaybe(item?.public_detail || item?.detail || item?.summary || item?.id || item, "");
    const title = paperSelfReviewEvidenceTitle(item);
    const next = displayMaybe(item?.public_next_action, "");
    const marker = `${item?.category || ""} ${item?.id || ""} ${raw}`.toLowerCase();
    const rawLooksInternal = raw.length > 180
      || marker.includes("paper proposes")
      || marker.includes("section 2")
      || marker.includes("contribution (3)")
      || marker.includes("data availability says")
      || marker.includes("zero empirical result")
      || marker.includes("untested architectural variants");
    const fallback = lang === "zh"
      ? "完整自审原文保留在审计 artifact；页面只显示面向监督者的可执行摘要。"
      : "The full self-review text stays in audit artifacts; this page shows only an actionable supervisor summary.";
    const detail = raw && !rawLooksInternal ? raw : fallback;
    const titlePrefix = detail.includes(title) ? detail : `${title}：${detail}`;
    return next ? `${titlePrefix} ${lang === "zh" ? "下一步" : "Next"}：${next}` : titlePrefix;
  }
  function paperSelfReviewIssueText(item: any) {
    const marker = `${item?.category || ""} ${item?.id || ""} ${item?.source || ""}`.toLowerCase();
    if (marker.includes("self_review_evidence") || item?.submission_blocker === true) return paperSelfReviewEvidenceText(item);
    return displayMaybe(item?.public_detail || item?.detail || item?.summary || item?.id || item, "");
  }
  function paperSelfReviewSummary(paper: any) {
    const rows = paperSelfReviewRows(paper);
    if (rows.length) return rows.map((item: any) => paperSelfReviewIssueText(item)).filter(Boolean).join("；");
    return displayMaybe(paper?.paper_self_review_status, t.noData);
  }
  function paperPreviewHelp(paper: any) {
    const evidenceRows = asArray(paper?.paper_self_review_evidence_blockers);
    if (evidenceRows.length) {
      return lang === "zh"
        ? "PDF 仅作预览；科研证据与投稿准备度仍需继续迭代，具体修复项已交由 Writing 主控 Claude 处理。"
        : "The PDF is preview-only; scientific evidence and submission readiness still need iteration, and detailed repair items are handled by the Writing controller.";
    }
    if (paperSelfReviewRows(paper).length || paperCitationRenderRows(paper).length) {
      return lang === "zh"
        ? "PDF 仅作预览；底层 LaTeX/BibTeX/自审诊断已保留给 Writing 主控 Claude 处理，不在这里展开。"
        : "The PDF is preview-only; low-level LaTeX/BibTeX/self-review diagnostics are reserved for the Writing controller and are not expanded here.";
    }
    if (paperAcceptedPreviewBlocked(paper)) return lang === "zh" ? "这份 PDF 是当前论文预览，可用于查看排版和内容；系统仍会根据质量、证据和投稿门控继续审计和修订。" : "This PDF is the current paper preview; The workflow will continue auditing and revising against quality, evidence, and submission gates.";
    return paper?.status === "running" ? t.runningPdfPreviewHelp : t.blockedPdfPreviewHelp;
  }
  function paperPdfReason(paper: any) {
    if (paper?.status === "running") return t.runningPdfReason;
    if (paperAcceptedPreviewBlocked(paper)) return lang === "zh" ? "PDF/TeX 已生成，正常论文形态、目标模板和图表审计显示为通过；full-cycle 仍需把它纳入当前合格预览门控，因此按论文预览展示，不能当投稿稿。" : "PDF/TeX have been generated and normality, venue-template, and figure audits show pass; full-cycle still needs to accept it as the current qualified preview, so treat it as preview-only.";
    if (paper?.paper_generation_skipped || paper?.status === "blocked_before_paper_generation") return paper?.science_gate_preflight_blockers?.slice?.(0, 3)?.join("；") || paper?.paper_generation_skipped_reason || t.skippedPdfReason;
    return t.blockedPdfReason;
  }
  function statusBool(value: any) {
    return value ? (lang === "zh" ? "是" : "yes") : (lang === "zh" ? "否" : "no");
  }
  function displayValueI18n(): Record<string, Record<Lang, string>> {
    return {
    accept: { zh: "接受", en: "accept" },
    "accept-with-modifications": { zh: "接受但需要改造", en: "accept with modifications" },
    "needs-more-search": { zh: "需要继续搜索", en: "needs more search" },
    keep_and_modify_current_repo: { zh: "保留并改造当前仓库", en: "keep and modify current repo" },
    switch_to_best_repo: { zh: "切换到最优仓库", en: "switch to best repo" },
    continue_search: { zh: "继续搜索", en: "continue searching" },
    reuse_existing_env: { zh: "复用现有环境", en: "reuse existing env" },
    repair_existing_env: { zh: "修补现有环境", en: "repair existing env" },
    create_new_project_env: { zh: "新建项目环境", en: "create new project env" },
    defer_until_repo_selected: { zh: "选定仓库后再配置", en: "defer until repo selected" },
    use_claim_ready_dataset: { zh: "使用已过门数据", en: "use auditable dataset" },
    download_or_place_required_data: { zh: "下载或放置所需数据", en: "download/place required data" },
    continue_data_search: { zh: "继续寻找数据", en: "continue data search" },
    bypassPermissions: { zh: "无人值守批准", en: "bypass permissions" },
    "yolo / unattended": { zh: "YOLO / 无人值守", en: "YOLO / unattended" },
    pass: { zh: "通过", en: "pass" },
    ready: { zh: "就绪", en: "ready" },
    completed: { zh: "已完成", en: "completed" },
    selected: { zh: "已选择", en: "selected" },
    historical_evidence_retained: { zh: "历史证据保留", en: "historical evidence retained" },
    running_or_ready: { zh: "运行中或就绪", en: "running or ready" },
    running: { zh: "运行中", en: "running" },
    running_full_research_cycle: { zh: "完整科研循环运行中", en: "full research cycle running" },
    normality_blocked: { zh: "论文预览需继续迭代", en: "paper preview needs iteration" },
    preview_available: { zh: "预览可用", en: "preview available" },
    needs_writing: { zh: "待撰写", en: "needs writing" },
    preview_pdf_blocked: { zh: "预览受门控", en: "preview gated" },
    blocked_before_paper_generation: { zh: "证据门控未通过的论文预览", en: "paper preview with evidence gates blocked" },
    evidence_gated_preview: { zh: "证据门控未通过的论文预览", en: "paper preview with evidence gates uncleared" },
    blocked_fresh_base_data_required: { zh: "主线数据/loader 门控阻塞", en: "main data/loader gate blocked" },
    blocked_fresh_base_reference_probe_required: { zh: "参考协议探针门控阻塞", en: "reference protocol gate blocked" },
    blocked_fresh_base_reference_smoke_required: { zh: "有界参考 smoke 门控阻塞", en: "bounded reference smoke gate blocked" },
    blocked_fresh_base_reference_reproduction_required: { zh: "论文级参考复现门控阻塞", en: "paper-level reference reproduction gate blocked" },
    blocked_literature_recommendation_gate: { zh: "Find 推荐门控阻塞", en: "Find recommendation gate blocked" },
    blocked_literature_llm_quota_exhausted: { zh: "LLM API 额度阻塞", en: "LLM API quota blocked" },
    blocked_environment_base_selection_required: { zh: "等待环境阶段选择当前基底", en: "waiting for environment-stage base selection" },
    environment_anchor_selection_required: { zh: "等待环境阶段选择当前基底", en: "waiting for environment-stage base selection" },
    blocked_llm_quota_exhausted: { zh: "LLM API 额度阻塞", en: "LLM API quota blocked" },
    literature_llm_quota_exhausted: { zh: "LLM API 额度阻塞", en: "LLM API quota blocked" },
    fresh_find_running: { zh: "Find 正在运行", en: "Find running" },
    current_find_packet_ready: { zh: "当前 Find 完成", en: "current Find complete" },
    blocked_missing_selected_plan: { zh: "等待唯一执行计划", en: "missing selected execution plan" },
    no_selected_plan: { zh: "未选择执行计划", en: "no selected execution plan" },
    selected_plan_ready: { zh: "唯一执行计划已选择", en: "selected execution plan ready" },
    "current-find-public-i18n": { zh: "当前 Find 公开展示同步", en: "current Find public-display sync" },
    claim_ready_anchor: { zh: "证据线索", en: "evidence anchor" },
    positive_anchor_for_planning: { zh: "计划线索", en: "positive planning anchor" },
    foundation_anchor: { zh: "方法线索", en: "method-borrowing anchor" },
    nethreshold_for_reading: { zh: "未入选线索", en: "boundary reading candidate" },
    critique_or_boundary_case: { zh: "反例/边界候选", en: "boundary/counterexample candidate" },
    strong_recommendation: { zh: "推荐", en: "recommended" },
    strong_recommendations_ready: { zh: "推荐论文评分完成", en: "strong recommendations scored" },
    waiting_for_current_find_results: { zh: "等待当前 Find 结果", en: "waiting for current Find results" },
    real_data_loader_ready: { zh: "真实数据/loader 已就绪", en: "real data/loader ready" },
    waiting_for_real_data_loader_evidence: { zh: "等待真实数据/loader 证据", en: "waiting for real data/loader evidence" },
    wait_for_environment_base_selection: { zh: "环境审查后执行", en: "run after environment review" },
    waiting_for_environment_base_selection: { zh: "环境审查后执行", en: "run after environment review" },
    waiting_for_environment_review: { zh: "环境审查后执行", en: "run after environment review" },
    route_authorization_gate: { zh: "路线授权门控", en: "route authorization gate" },
    current_base: { zh: "当前路线", en: "current route" },
    claude_code_current_find_takeover: { zh: "当前 Find 精读产物", en: "current-Find reading output" },
    queued: { zh: "排队中", en: "queued" },
    stale: { zh: "已停止", en: "stale" },
    warn: { zh: "需继续检查", en: "needs review" },
    warning: { zh: "需继续检查", en: "needs review" },
    blocked: { zh: "阻塞", en: "blocked" },
    recommendation_shortfall: { zh: t.recommendationShortfall, en: t.recommendationShortfall },
    failed: { zh: "失败", en: "failed" },
    error: { zh: "错误", en: "error" },
    interrupted: { zh: "中途停止", en: "interrupted" },
    missing: { zh: t.missing, en: t.missing },
    pending: { zh: t.pending, en: t.pending },
    not_started: { zh: t.statusNotStarted, en: t.statusNotStarted },
    claim_ready: { zh: t.statusClaimReady, en: t.statusClaimReady },
    "auditable": { zh: t.statusClaimReady, en: t.statusClaimReady },
    "not auditable": { zh: t.statusNotClaimReady, en: t.statusNotClaimReady },
    auto: { zh: t.statusAuto, en: t.statusAuto },
    default: { zh: t.defaultOption, en: t.defaultOption },
    };
  }
  function displayValue(value: any, fallback = t.noData) {
    const text = publicStatusText(value, lang);
    if (!text) return fallback;
    const dictionary = displayValueI18n();
    const lowerText = text.toLowerCase();
    const normalizedText = lowerText.replace(/[\s-]+/g, "_");
    return dictionary[text]?.[lang] || dictionary[lowerText]?.[lang] || dictionary[normalizedText]?.[lang] || text.replace(/_/g, " ");
  }
  function commandSummary(value: any) {
    const text = String(value ?? "").trim();
    if (!text) return t.noData;
    if (text.startsWith("{")) {
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object") {
          const parts = [
            parsed.mode ? `mode=${parsed.mode}` : "",
            parsed.method ? `method=${parsed.method}` : "",
            parsed.dataset ? `dataset=${parsed.dataset}` : "",
            parsed.epoch ? `epoch=${parsed.epoch}` : "",
            parsed.paper_level !== undefined ? `paper_level=${parsed.paper_level}` : "",
          ].filter(Boolean);
          return parts.length ? parts.join("; ") : (lang === "zh" ? "JSON 配置已记录，完整内容见 CSV" : "JSON config recorded; full content in CSV");
        }
      } catch {}
      return lang === "zh" ? "JSON 配置已记录，完整内容见 CSV" : "JSON config recorded; full content in CSV";
    }
    const publicText = publicLogText(text, lang);
    if (/^candidate_observation_only/i.test(publicText)) return lang === "zh" ? "候选实验观察记录；完整配置保留在 CSV。" : "candidate observation recorded; full config remains in CSV.";
    if (/selected[-_ ]base|base[_-]?switch|deterministic/i.test(publicText)) return lang === "zh" ? "基底/候选路线控制信息已记录，完整命令保留在后端审计。" : "base/candidate-route control metadata recorded; full command remains in backend audit.";
    return publicText.length > 220 ? `${publicText.slice(0, 220)}...` : publicText;
  }
  const evidenceList = (row: any) => localizedList(row, "evidence");
  const claudeDecisionList = (key: string) => localizedList(claudeTopicDecision, key);
  const filteredVenues = useMemo(() => {
    const query = venueQuery.trim().toLowerCase();
    if (!query) return venues;
    return venues.filter((venue) => {
      const aliasText = (venue.aliases || []).map((alias) => [alias.id, alias.name, alias.full_name, alias.source, alias.rank].filter(Boolean).join(" ")).join(" ");
      const haystack = [
        venue.name,
        venue.full_name,
        venue.field,
        venue.rank,
        venue.type,
        venue.source,
        venue.classification_source,
        aliasText,
      ].join(" ").toLowerCase();
      return haystack.includes(query);
    });
  }, [venues, venueQuery]);
  const visibleRunArtifactsSignature = useMemo(() => artifactListSignature(visibleRunArtifacts), [visibleRunArtifacts]);
  const stableRunArtifactsForTab = lastVisibleRunArtifactsByTab[tab];
  const suppressStableDownstreamArtifacts = useMemo(() => {
    if (!(tab === "read" || tab === "ideas" || tab === "plan")) return false;
    const status = String(currentFindPipeline?.status || publicReadStage?.status || "").toLowerCase();
    const currentRun = String(currentFindPipeline?.run_id || "");
    const artifactRun = String(visibleRunArtifactsRunId || stableRunArtifactsForTab?.runId || "");
    const blocked = status.startsWith("blocked") || Boolean(currentFindPipeline?.failure_type);
    const contentReady = Boolean(currentFindPipeline?.content_ready || currentFindPipeline?.read_idea_plan_ready || currentFindPipeline?.takeover_ready);
    return Boolean(blocked && !contentReady && currentRun && artifactRun && currentRun === artifactRun);
  }, [currentFindPipeline, publicReadStage, stableRunArtifactsForTab, tab, visibleRunArtifactsRunId]);
  const renderedRunArtifacts = useMemo(() => (
    visibleRunArtifacts.length ? visibleRunArtifacts : (suppressStableDownstreamArtifacts ? [] : (stableRunArtifactsForTab?.artifacts || []))
  ), [stableRunArtifactsForTab, suppressStableDownstreamArtifacts, visibleRunArtifacts]);
  const renderedRunArtifactsRunId = visibleRunArtifacts.length
    ? visibleRunArtifactsRunId
    : String(suppressStableDownstreamArtifacts ? visibleRunArtifactsRunId : (stableRunArtifactsForTab?.runId || visibleRunArtifactsRunId || ""));
  const renderedRunArtifactsSignature = useMemo(() => artifactListSignature(renderedRunArtifacts), [renderedRunArtifacts]);
  const artifactPanelLoading = useMemo(() => {
    return Boolean(runArtifactsLoading || (FIND_RUN_ARTIFACT_TABS.includes(tab) && currentFindArtifactLoading));
  }, [currentFindArtifactLoading, runArtifactsLoading, tab]);
  const showRunArtifactPanel = useMemo(() => {
    if (renderedRunArtifacts.length) return true;
    if (FIND_RUN_ARTIFACT_TABS.includes(tab)) return true;
    return Boolean(runId && !String(runId).startsWith("find_"));
  }, [renderedRunArtifacts.length, runId, tab]);
  const venueById = useMemo(() => venueMapWithAliases(venues), [venues]);
  const selectedVenueItems = useMemo(
    () => selectedVenues.map((id) => venueById.get(id) || CORE_VENUE_FALLBACKS[id]).filter((venue): venue is Venue => Boolean(venue)),
    [selectedVenues, venueById],
  );
  useEffect(() => {
    const deduped = dedupeVenueSelectionByIdentity(selectedVenues, selectedVenueYears, venueById);
    if (!sameStringArray(deduped.venueIds, selectedVenues)) setSelectedVenues(deduped.venueIds);
    if (!sameVenueYearMap(deduped.venueYears, selectedVenueYears)) setSelectedVenueYears(deduped.venueYears);
  }, [selectedVenueYears, selectedVenues, venueById]);
  const addCandidateYears = useMemo(() => normalizeSelectedYears(years), [years]);
  const availableVenues = useMemo(() => uniqueVenuesByIdentity(filteredVenues), [filteredVenues]);
  const availableVenueDisplayLimit = venueQuery.trim() || showAllAvailableVenues ? 300 : 24;
  const visibleAvailableVenues = availableVenues.slice(0, availableVenueDisplayLimit);
  const hiddenAvailableVenueCount = Math.max(0, Math.min(availableVenues.length, 300) - visibleAvailableVenues.length);
  const currentArtifact = useMemo(() => {
    if (!renderedRunArtifacts.length) return undefined;
    return renderedRunArtifacts.find((artifact) => artifact.name === activeArtifact) || renderedRunArtifacts[0];
  }, [renderedRunArtifacts, activeArtifact]);

  useEffect(() => {
    if (tab !== "read") return undefined;
    let frame = 0;
    const fit = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => fitRenderedMath());
    };
    fit();
    window.addEventListener("resize", fit);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", fit);
    };
  }, [currentArtifact?.name, rawArtifacts, renderedRunArtifactsSignature, tab]);

  useEffect(() => {
    if (!FIND_RUN_ARTIFACT_TABS.includes(tab)) return;
    if (!visibleRunArtifacts.length || !visibleRunArtifactsRunId) return;
    setLastVisibleRunArtifactsByTab((prev) => {
      const current = prev[tab];
      if (current?.runId === visibleRunArtifactsRunId && artifactListSignature(current.artifacts) === visibleRunArtifactsSignature) return prev;
      return { ...prev, [tab]: { runId: visibleRunArtifactsRunId, artifacts: visibleRunArtifacts } };
    });
  }, [tab, visibleRunArtifacts, visibleRunArtifactsRunId, visibleRunArtifactsSignature]);

  useEffect(() => {
    if (!FIND_RUN_ARTIFACT_TABS.includes(tab)) return;
    if (visibleRunArtifacts.length || lastVisibleRunArtifactsByTab[tab]?.artifacts?.length) return;
    const candidateRunIds = projectRuns.map((run) => String(run.run_id || "").trim()).filter(Boolean);
    if (!candidateRunIds.length) return;
    const requestKey = `${researchProject}:${tab}:${candidateRunIds.join("|")}`;
    if (fallbackRunArtifactsInFlightRef.current === requestKey) return;
    let cancelled = false;
    fallbackRunArtifactsInFlightRef.current = requestKey;
    const loadFallback = async () => {
      for (const candidateRunId of candidateRunIds) {
        let candidateArtifacts = fallbackRunArtifactCacheRef.current[candidateRunId];
        if (!candidateArtifacts) {
          const data = await getArtifacts(candidateRunId, { light: true });
          candidateArtifacts = data.artifacts;
          fallbackRunArtifactCacheRef.current = { ...fallbackRunArtifactCacheRef.current, [candidateRunId]: candidateArtifacts };
        }
        if (cancelled) return;
        const visible = asArray(candidateArtifacts)
          .filter((artifact: any) => artifact.kind === "markdown" && !HIDDEN_RUN_ARTIFACTS.has(artifact.name))
          .filter((artifact: any) => artifactBelongsToCurrentFindRun(artifact, candidateRunId))
          .filter((artifact: any) => artifactVisibleForTab(artifact, tab));
        if (visible.length) {
          setLastVisibleRunArtifactsByTab((prev) => ({ ...prev, [tab]: { runId: candidateRunId, artifacts: visible } }));
          return;
        }
      }
    };
    void loadFallback().finally(() => {
      if (fallbackRunArtifactsInFlightRef.current === requestKey) fallbackRunArtifactsInFlightRef.current = "";
    });
    return () => {
      cancelled = true;
    };
  }, [lastVisibleRunArtifactsByTab, projectRuns, researchProject, tab, visibleRunArtifacts.length]);

  useEffect(() => {
    setActiveArtifact("");
    setRawArtifacts({});
  }, [tab, renderedRunArtifactsRunId, renderedRunArtifactsSignature]);

  useEffect(() => {
    if (activeArtifact && !renderedRunArtifacts.some((artifact) => artifact.name === activeArtifact)) {
      setActiveArtifact("");
    }
  }, [renderedRunArtifacts, activeArtifact]);

  useEffect(() => {
    if (tab === "plan" && !activeArtifact && renderedRunArtifacts.some((artifact) => artifact.name === "plan.md")) {
      setActiveArtifact("plan.md");
    }
  }, [activeArtifact, renderedRunArtifacts, tab]);


  useEffect(() => {
    if (!currentFindArtifactRunId) {
      currentFindArtifactsRunRef.current = "";
      setCurrentFindArtifacts([]);
      return;
    }
    if (!FIND_RUN_ARTIFACT_TABS.includes(tab)) return;
    void loadCurrentFindArtifacts(currentFindArtifactRunId, { loading: true, scope: currentFindArtifactScope(tab) });
  }, [currentFindArtifactRunId, tab]);

  useEffect(() => {
    if (!currentFindArtifactRunId || !hasLiveCurrentFindArtifactJob) return;
    let cancelled = false;
    const refreshCurrentFindArtifacts = () => {
      if (!cancelled && FIND_RUN_ARTIFACT_TABS.includes(tab)) {
        void loadCurrentFindArtifacts(currentFindArtifactRunId, { loading: false, scope: currentFindArtifactScope(tab) });
      }
    };
    refreshCurrentFindArtifacts();
    const timer = window.setInterval(refreshCurrentFindArtifacts, 20000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [currentFindArtifactRunId, hasLiveCurrentFindArtifactJob, tab]);

  useEffect(() => {
    if (!activeFindRunId || !activeFindJobForRun) {
      if (!activeFindRunId) setActiveFindArtifacts([]);
      return;
    }
    let cancelled = false;
    const refreshActiveFindArtifacts = () => {
      if (!cancelled) void loadActiveFindArtifacts(activeFindRunId);
    };
    refreshActiveFindArtifacts();
    const timer = window.setInterval(refreshActiveFindArtifacts, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeFindRunId, activeFindJobForRun?.job_id]);

  useEffect(() => {
    if (!currentProjectFindRunId || activeFindRunId || userSelectedRunRef.current) return;
    if (runId === currentProjectFindRunId) return;
    if (!runExists(runs, currentProjectFindRunId)) return;
    setRunId(currentProjectFindRunId);
    setActiveArtifact("");
    setRawArtifacts({});
  }, [currentProjectFindRunId, activeFindRunId, runId, runs]);

  useEffect(() => {
    if (currentProjectArtifact && activeProjectArtifact !== currentProjectArtifact.name) {
      setActiveProjectArtifact(currentProjectArtifact.name);
    }
  }, [currentProjectArtifact, activeProjectArtifact]);

  useEffect(() => {
    setPlanMarkdownDraft("");
    setPlanMarkdownDirty(false);
  }, [researchProject, runId]);

  function ideaStatusPatch(status: "approved" | "deleted" | "pending") {
    return { status };
  }

  function patchIdeaArtifactRows(list: Artifact[], ideaId: string, updates: Record<string, any>) {
    return list.map((artifact) => {
      if (artifact.name !== "ideas.json" || !artifact.content || typeof artifact.content !== "object") return artifact;
      const rows = Array.isArray(artifact.content.ideas) ? artifact.content.ideas : [];
      let changed = false;
      const ideas = rows.map((row: any, index: number) => {
        const keys = [ideaKey(row, index), row?.id, row?.idea_id, row?.title].map((value) => String(value || "").trim());
        if (!keys.includes(String(ideaId || "").trim())) return row;
        changed = true;
        return { ...row, ...updates };
      });
      return changed ? { ...artifact, content: { ...artifact.content, ideas } } : artifact;
    });
  }

  function applyIdeaArtifactPatch(ideaId: string, updates: Record<string, any>) {
    setCurrentFindArtifacts((prev) => patchIdeaArtifactRows(prev, ideaId, updates));
    setArtifacts((prev) => patchIdeaArtifactRows(prev, ideaId, updates));
    setActiveFindArtifacts((prev) => patchIdeaArtifactRows(prev, ideaId, updates));
  }

  function updateIdeaEditorDraft(ideaId: string, idea: any, field: keyof IdeaEditorDraft, value: string) {
    setIdeaEditorDrafts((prev) => {
      const current = prev[ideaId] || {
        title: String(idea?.title || ""),
        new_method: String(idea?.new_method || ""),
        initial_experiment: String(idea?.initial_experiment || ""),
      };
      return { ...prev, [ideaId]: { ...current, [field]: value } };
    });
  }

  async function saveIdeaFields(ideaId: string) {
    if (rejectHistoricalRunMutation()) return;
    const ideaRunId = currentProjectFindRunId || runId;
    const draft = ideaEditorDrafts[ideaId];
    if (!ideaRunId || !draft || !researchProject) return;
    setIdeaEditorSaving((prev) => ({ ...prev, [ideaId]: true }));
    try {
      await patchIdea(ideaRunId, ideaId, draft, researchProject);
      applyIdeaArtifactPatch(ideaId, draft);
      currentFindArtifactsInFlightRef.current = "";
      await loadCurrentFindArtifacts(ideaRunId, { loading: false, scope: "ideas" });
    } catch (err) {
      setError(String(err));
    } finally {
      setIdeaEditorSaving((prev) => ({ ...prev, [ideaId]: false }));
    }
  }

  async function setIdeaStatus(ideaId: string, status: "approved" | "deleted" | "pending") {
    if (rejectHistoricalRunMutation()) return;
    const ideaRunId = currentProjectFindRunId || runId;
    if (!ideaRunId || !researchProject) return;
    const updates = ideaStatusPatch(status);
    setIdeaStatusSaving((prev) => ({ ...prev, [ideaId]: status }));
    applyIdeaArtifactPatch(ideaId, updates);
    setPlanIdeaIds((previous) => status === "approved"
      ? Array.from(new Set([...previous, ideaId]))
      : previous.filter((selectedId) => selectedId !== ideaId));
    try {
      await patchIdea(ideaRunId, ideaId, { status }, researchProject);
      currentFindArtifactsInFlightRef.current = "";
      await loadCurrentFindArtifacts(ideaRunId, { loading: false, scope: "ideas" });
    } catch (err) {
      setError(String(err));
      currentFindArtifactsInFlightRef.current = "";
      await loadCurrentFindArtifacts(ideaRunId, { loading: false, scope: "ideas" });
    } finally {
      setIdeaStatusSaving((prev) => {
        const next = { ...prev };
        delete next[ideaId];
        return next;
      });
    }
  }

  async function saveIdeaMarkdown() {
    if (rejectHistoricalRunMutation()) return;
    const ideaRunId = currentProjectFindRunId || runId;
    if (!ideaRunId || !researchProject) return;
    setIdeaMarkdownSaving(true);
    try {
      await updateIdeaMarkdown(ideaRunId, ideaMarkdownDraft, researchProject);
      currentFindArtifactsInFlightRef.current = "";
      await loadCurrentFindArtifacts(ideaRunId, { loading: false, scope: "ideas" });
      setIdeaMarkdownEditing(false);
    } catch (err) {
      setError(String(err));
    } finally {
      setIdeaMarkdownSaving(false);
    }
  }

  async function savePlanMarkdown() {
    if (rejectHistoricalRunMutation()) return;
    const planRunId = currentProjectFindRunId || runId;
    if (!planRunId || !planMarkdownDraft.trim()) return;
    setPlanMarkdownSaving(true);
    try {
      await updatePlanMarkdown(planRunId, planMarkdownDraft, researchProject || undefined);
      setPlanMarkdownDirty(false);
      currentFindArtifactsInFlightRef.current = "";
      await loadCurrentFindArtifacts(planRunId, { loading: false, scope: "plan" });
    } catch (err) {
      setError(String(err));
    } finally {
      setPlanMarkdownSaving(false);
    }
  }

  async function handleDeleteRun(id: string) {
    if (!window.confirm(t.deleteRunConfirm)) return;
    try {
      await deleteRun(id);
      const nextRuns = await getRuns(researchProject || undefined);
      setRuns(nextRuns);
      if (id === runId) {
        const next = nextRuns[0];
        if (next) {
          await loadRun(next.run_id);
        } else {
          setRunId("");
          setArtifacts([]);
          setSelectedPapers([]);
          setPlanIdeaIds([]);
          setSelectedPlanId("");
          setPlanMarkdownDraft("");
          setPlanMarkdownDirty(false);
        }
      }
    } catch (err) {
      setError(String(err));
    }
  }

  async function runVenueHealth() {
    if (!researchProjectsLoaded || researchProjectLoading || !researchSummary) return;
    try {
      setCheckingVenues(true);
      setError("");
      const highPriority = ["ICLR", "NeurIPS", "ICML", "CVPR", "ICCV", "ECCV", "ACL", "EMNLP", "NAACL", "AAAI", "IJCAI"];
      const ids = selectedVenues.length
        ? selectedVenues
        : venues.filter((venue) => highPriority.includes(venue.name)).map((venue) => venue.id);
      const pairs = selectedVenues.length
        ? venueYearPairs(selectedVenues, selectedVenueYears)
        : ids.flatMap((venueId) => addCandidateYears.map((year) => ({ venue_id: venueId, year })));
      const byId = venueMapWithAliases(venues);
      const response = await checkVenueHealth({
        project: researchProject,
        venue_ids: ids,
        years: selectedVenues.length ? yearsFromVenueYearMap(selectedVenues, selectedVenueYears) : addCandidateYears,
        venue_years: pairs,
        sample_limit: 2,
      });
      const next: Record<string, { ok: boolean; message: string; source_adapter: string; sample_count: number }> = {};
      for (const result of response.results) {
        const resultKeys = venueComparableKeys(result.venue_id, byId);
        const matchingSelectedIds = selectedVenues.filter((id) => {
          const sameVenue = Array.from(venueComparableKeys(id, byId)).some((key) => resultKeys.has(key));
          const sameYear = !result.year || yearsForVenue(selectedVenueYears, id).includes(Number(result.year));
          return sameVenue && sameYear;
        });
        const targetIds = Array.from(new Set([result.venue_id, ...matchingSelectedIds].filter(Boolean)));
        for (const targetId of targetIds) {
          const current = next[targetId];
          next[targetId] = {
            ok: Boolean(current?.ok || result.ok),
            message: result.message,
            source_adapter: result.source_adapter,
            sample_count: (current?.sample_count || 0) + result.sample_count,
          };
        }
      }
      setVenueHealth((prev) => ({ ...prev, ...next }));
    } catch (err) {
      setError(String(err));
    } finally {
      setCheckingVenues(false);
    }
  }

  function researchPayload(action: string, options: { venue?: string } = {}) {
    const paperAction = action === "paper";
    const venueAction = paperAction || action === "full-cycle" || action === "full_research_cycle";
    const summaryVenue = String(
      (researchSummary as any)?.run_preferences?.target_venue
      || (researchSummary as any)?.run_preferences?.venue
      || (researchSummary as any)?.human_supervision?.target_venue
      || researchSummary?.config?.target_venue
      || researchSummary?.config?.venue
      || "",
    ).trim();
    const payloadVenue = venueAction ? String(options.venue || researchVenue || summaryVenue || "").trim() : "";
    const payload: Record<string, any> = {
      action,
      project: researchProject,
      prompt: researchPrompt,
      topic: researchTopic,
      title: venueAction ? researchTitle : "",
      max_papers: config.max_recommended_papers,
      max_ideas: config.max_ideas,
      repair_rounds: planRepairRounds,
      iterations: researchIterations,
      iterations_per_cycle: researchIterations,
      max_cycles: action === "full-cycle" ? Math.max(3, researchIterations) : researchIterations,
      max_launches: researchMaxLaunches,
      execute_plan: researchExecutePlan,
      prepare_env: researchPrepareEnv,
      real_bootstrap_env: researchRealBootstrapEnv,
      conda_env: researchEnvDraft.conda_env || (researchSummary as any)?.run_preferences?.conda_env || researchSummary?.config?.conda_env || "",
      skip_paper: researchSkipPaper,
      refresh_current_paper: paperAction,
      refresh_current_venue: paperAction,
      auto_install_latex: researchAutoInstallLatex,
    };
    if (venueAction) {
      payload.venue = payloadVenue;
      payload.target_venue = payloadVenue;
    }
    return payload;
  }

  async function saveProjectConfigDraft(options: { silent?: boolean; propagateError?: boolean; includePaperSettings?: boolean } = {}) {
    if (!researchProject) return researchSummary;
    const includePaperSettings = Boolean(options.includePaperSettings);
    const patch: Record<string, any> = {
      topic: researchTopic,
      user_prompt: researchPrompt,
      research_interest: researchResearchInterest,
      researcher_profile: researchResearcherProfile,
      default_find_selection: currentFindSelection(),
      llm: {
        enabled: Boolean((config.api_key || config.api_key_saved) && config.model && config.provider && config.provider.toLowerCase() !== "mock"),
        provider: config.provider || "openai_compatible",
        api_base: config.base_url || "",
        model: config.model || "",
        api_key_env: "OPENAI_API_KEY",
        temperature: config.temperature,
        api_mode: "chat_completions",
      },
    };
    if (includePaperSettings) {
      const venueDraft = researchVenue.trim();
      if (!venueDraft) {
        if (!options.silent) setError(lang === "zh" ? "投稿会议/期刊不能为空。" : "Target venue cannot be empty.");
        return researchSummary;
      }
      patch.target_venue = venueDraft;
      patch.venue = venueDraft;
      patch.paper = { ...(patch.paper || {}), target_venue: venueDraft, title: researchTitle };
    }
    try {
      setProjectConfigSaving(true);
      setError("");
      const summary = await saveProjectConfig(researchProject, patch);
      setProjectSummary(summary);
      const runPreferences = (summary as any).run_preferences || {};
      if (includePaperSettings) {
        const savedVenue = runPreferences.target_venue || runPreferences.venue || summary.human_supervision?.target_venue || researchVenue.trim();
        setVenue(savedVenue || "");
        setProjectConfigMessage(options.silent ? "" : (lang === "zh" ? "投稿目标已保存。" : "Target venue saved."));
      } else if (!options.silent) {
        setProjectConfigMessage(lang === "zh" ? "项目配置已保存。" : "Project config saved.");
      }
      return summary;
    } catch (err) {
      setError(String(err));
      if (options.propagateError) throw err;
      return researchSummary;
    } finally {
      setProjectConfigSaving(false);
    }
  }

  async function persistProjectConfigForRun(action = "") {
    return await saveProjectConfigDraft({ silent: true, propagateError: true, includePaperSettings: action === "paper" });
  }

  async function runAR(action: string) {
    if (!researchProject) return;
    const exclusiveAction = ["full-cycle", "environment", "experiment", "paper", "current-find-selection"].includes(action);
    if (stageLaunchDisabledByFullCycle && exclusiveAction) {
      setError(stageLaunchLockedText);
      return;
    }
    if ((projectStatusLoadingForLaunch || stageLaunchDisabledByProjectWorker) && exclusiveAction) {
      setError(projectStageLaunchLockedText);
      return;
    }
    if ((action === "environment" && environmentStageRunning) || (action === "experiment" && (environmentStageRunning || experimentStageRunning || referenceFullJobRunning)) || (action === "paper" && paperStageRunning)) {
      setError(lang === "zh" ? "当前项目已有环境/实验/论文阶段任务正在运行；已阻止重复启动。" : "A project environment/experiment/paper stage job is already running; duplicate launch is blocked.");
      return;
    }
    if (literatureGateBlocked && action === "experiment") {
      setError(lang === "zh" ? "当前 Find 推荐门控未过；禁止直接启动实验子循环。完整流程仍可重新调研或自行决定复用当前 Find；论文页可生成目标 venue 论文预览但不会标记为投稿通过。" : "The current Find strong-recommendation gate has not passed; direct experiment actions are blocked. The paper page may still generate an paper preview, but it will not be marked submission-ready.");
      return;
    }
    try {
      setError("");
      const savedSummary = await persistProjectConfigForRun(action);
      if (action === "environment") {
        await persistEnvConfigForRun();
      }
      const savedRunPreferences = (savedSummary as any)?.run_preferences || {};
      const venueAction = action === "paper" || action === "full-cycle" || action === "full_research_cycle";
      const savedVenue = venueAction
        ? (savedRunPreferences.target_venue || savedRunPreferences.venue || (savedSummary as any)?.human_supervision?.target_venue || (savedSummary as any)?.config?.target_venue || (savedSummary as any)?.config?.venue || researchVenue)
        : "";
      const nextJob = await startProjectJob(researchPayload(action, { venue: savedVenue }));
      const nextTab: Tab = action === "environment" ? "environment" : action === "experiment" || action === "full-cycle" ? "experiment" : action === "paper" ? "paperWrite" : action === "current-find-selection" ? "plan" : tab;
      attachJob(nextJob, nextTab);
    } catch (err) {
      setError(String(err));
    }
  }

  async function queueAgentGuidance(stage: string, interruptCurrent = false) {
    const key = `${stage}:controller`;
    const text = String(agentGuidanceMessages[key] || agentGuidanceMessage || "").trim();
    if (!researchProject || !text) return;
    setError("");
    const action = stage === "paper" ? "writing-chat" : stage === "environment" ? "environment-chat" : "experimenting-chat";
    const nextJob = await startProjectJob({
      action,
      project: researchProject,
      stage,
      message: text,
      ...(stage === "environment" ? {} : { timeout_sec: 14400 }),
      queue_if_busy: true,
      interrupt_current: interruptCurrent,
    });
    setAgentGuidanceMessages((prev) => ({ ...prev, [key]: "" }));
    setAgentGuidanceMessage("");
    attachJob(nextJob, stage === "paper" ? "paperWrite" : stage === "experiment" ? "experiment" : "environment");
    await refreshProject({ resetDrafts: false });
  }

  async function handleCreateProject() {
    const id = newProjectId.trim();
    const topic = researchTopic.trim() || id;
    if (!id || !topic) return;
    try {
      setCreatingProject(true);
      setError("");
      setProjectMessage("");
      const summary = await createProject({
        id,
        name: id,
        topic,
      });
      const projectData = await getProjects();
      setProjects(projectData);
      setProjectsLoaded(true);
      setNewProjectId("");
      setProjectMessage(t.researchProjectCreated);
      await loadProject(summary.project || id);
    } catch (err) {
      setError(String(err));
    } finally {
      setCreatingProject(false);
    }
  }

  async function refreshProject(options?: { resetDrafts?: boolean }) {
    if (!researchProject) return;
    await loadProject(researchProject, { resetDrafts: options?.resetDrafts ?? true });
  }

  function artifactPanelContent(artifact: any, options: { raw?: boolean } = {}) {
    const rawContent = String(artifact?.content ?? "");
    if (artifact?.name === "idea.md") return rawContent;
    const localizedContent = lang === "zh"
      ? String(artifact?.content_zh ?? artifact?.content ?? "")
      : String(artifact?.content_en ?? artifact?.content ?? "");
    if (artifact?.name === "source_status.md") {
      const structuredRows = expandedSourceStatusRows(findResults || findProgress);
      const localizedSourceStatus = sourceStatusArtifactMarkdown(structuredRows, lang);
      if (localizedSourceStatus) return options.raw ? publicMarkdownArtifact(localizedSourceStatus) : localizedSourceStatus;
    }
    if (options.raw) return publicMarkdownArtifact(rawContent);
    if (artifact?.name !== "find.md" && lang === "en" && containsCJKText(localizedContent)) {
      return "This artifact is authored in Chinese by a module controller. The structured English projection for this step is shown above; use Raw to inspect the original artifact. No scientific status is changed by this display fallback.";
    }
    return publicLogText(localizedContent, lang);
  }


  function findSourceStatusRows() {
    const literature = researchLiteratureSurvey || {};
    const freshFindActive = freshFindRunning || String(literature.status || "").toLowerCase() === "fresh_find_running";
    const hasLiveFindJob = displayJobs.some((job: any) => isFindRunJob(job) && isLiveJob(job));
    if (sourceStatus.length) return sourceStatus;
    if (freshFindActive || hasLiveFindJob) return [];
    if (researchSourceStatus.length) return researchSourceStatus;
    const hasCurrentFindSource = Boolean(
      currentProjectFindRunId
      || currentFindArtifactRunId
      || literature.run_id
      || literature.current_find_pipeline?.run_id
    );
    return freshFindActive ? researchSourceStatus : (researchSourceStatus.length ? researchSourceStatus : sourceStatus);
  }

  function findSurveyVisibleStatus(status?: string) {
    const raw = String(status || "").trim();
    const normalized = raw.toLowerCase().replace(/[\s-]+/g, "_");
    if (!raw) return "";
    const internalReadyStates = new Set([
      "current_find_packet_ready",
      "current_find_public_i18n",
      "strong_recommendations_ready",
      "ready",
      "completed",
      "done",
      "pass",
    ]);
    if (internalReadyStates.has(normalized) || normalized.endsWith("_ready") || normalized.includes("packet_ready")) return "";
    if (raw.includes("已就绪") || raw.includes("产物已就绪")) return "";
    return /(running|fresh_find|blocked|refreshing|quota|shortfall|missing|error|fail|stale)/.test(normalized) ? raw : "";
  }

  function renderFindLiteratureSurveyPanel() {
    if (tab !== "find") return null;
    const renderSurveyShell = (content: any) => {
      return (
        <div className="findSurveyPanel embeddedFindSurveyPanel findSurveyConfigPanel" data-testid="find-literature-survey" data-layout-order="after-find-config-source-before-task-artifact">
          <div className="toolbar compactToolbar findSurveyHeader">
            <div>
              <h3 data-testid="find-literature-survey-heading">{t.researchLiteratureSurvey}</h3>
              <p className="help">{t.researchLiteratureSurveyHelp}</p>
            </div>
          </div>
          {content}
        </div>
      );
    };
    const pendingSurveyState = (
      <div className="emptyState compactFindSurveyEmpty" data-testid="find-survey-pending">
        <p>{lang === "zh" ? "等待当前 Find run 的渠道抓取、候选筛选和评分概览；完整来源状态会写入下方产物。" : "Waiting for the current Find run source fetching, candidate screening, and scoring overview; full source status is written to the artifacts below."}</p>
      </div>
    );
    const literature = researchLiteratureSurvey || {};
    const stageCounts = (publicFindStage?.counts || {}) as any;
    const activeRunCounts = (viewingActiveIncompleteFindRun && runFindState?.counts && typeof runFindState.counts === "object" ? runFindState.counts : {}) as any;
    const counts = viewingActiveIncompleteFindRun
      ? { ...activeRunCounts } as any
      : { ...stageCounts, ...(researchLiteratureCounts || {}), ...(literatureCounts || {}) } as any;
    const hasCompletedFindResultsForPanel = hasCurrentFindResults && !viewingActiveIncompleteFindRun;
    const literatureFreshFindRunning = String(literature.status || "").toLowerCase() === "fresh_find_running";
    const freshFindActive = !hasCompletedFindResultsForPanel && (freshFindRunning || viewingActiveIncompleteFindRun || Boolean(activeFindJobForRun) || literatureFreshFindRunning);
    const currentFindCounts: any = freshFindActive ? {} : literatureCounts || {};
    const sourceLimitations = freshFindActive ? [] : [...researchSourceLimitations, ...researchMissingVenueIndexes].slice(0, 4);
    const categoryFilteredCount = (freshFindActive ? 0 : (currentFindCounts as any).categoryFiltered) || counts.category_filtered_papers;
    const tfidfScreenedCount = (freshFindActive ? 0 : (currentFindCounts as any).tfidfScreened) || counts.tfidf_screened_papers || (freshFindActive ? 0 : (currentFindCounts as any).titleInput);
    const titleScoredCount = (freshFindActive ? 0 : (currentFindCounts as any).llmTitleScored) || counts.llm_title_scored_papers || (freshFindActive ? 0 : (currentFindCounts as any).titleCandidates);
    const detailFetched = counts.detail_fetched || counts.venue_detail_fetched_candidates || (freshFindActive ? 0 : (currentFindCounts as any).detailFetched);
    const evaluated = firstPresentValue(
      counts.abstract_scored_papers,
      counts.llm_scored_candidates,
      counts.llm_scoring_batches_total ? `${counts.llm_scoring_batches_current || 0}/${counts.llm_scoring_batches_total} 批` : "",
      freshFindActive ? 0 : (currentFindCounts as any).llmScored,
      0,
    );
    const candidatePoolCount = Number(
      counts.title_candidates
      || counts.venue_final_title_candidates
      || (freshFindActive ? 0 : (currentFindCounts as any).titleCandidates)
      || counts.traceable_candidates
      || counts.survey_candidates
      || (freshFindActive ? 0 : retrievalPool.length)
      || 0,
    );
    const recommendedCount = freshFindActive ? Number(counts.strong_recommendations || 0) : Number(counts.strong_recommendations || (currentFindCounts as any).strong || activeStrongLiteratureRows.length || researchStrongRecommendations.length || 0);
    const sourceRows = findSourceStatusRows();
    const selectedFindStatus = String(selectedFindJobForRun?.status || "").toLowerCase();
    const selectedFindStopped = Boolean(selectedFindJobForRun && ["cancelled", "error", "blocked"].includes(selectedFindStatus));
    const stoppedFindProgress = selectedFindStopped
      ? {
        phase: selectedFindStatus,
        current: selectedFindJobForRun?.progress?.current || 0,
        total: selectedFindJobForRun?.progress?.total || 1,
        percent: selectedFindJobForRun?.progress?.percent ?? 0,
        message: selectedFindJobForRun?.error || selectedFindJobForRun?.progress?.message || jobStatusLabel(selectedFindStatus, lang),
      }
      : null;
    const artifactLiveProgress = selectedFindStopped ? {} : (runFindState?.live_progress || (literature.current_find_pipeline && literature.current_find_pipeline.live_progress) || currentFindPipeline?.live_progress || {});
    const liveProgress = activeFindJobForRun?.progress || stoppedFindProgress || artifactLiveProgress || {};
    const livePhase = String(liveProgress.phase || "");
    const liveProgressText = liveProgress.message
      ? `${jobStatusLabel(livePhase || "fresh_find_running", lang)}：${displayMaybe(liveProgress.message)}${liveProgress.total ? ` (${liveProgress.current || 0}/${liveProgress.total}${liveProgress.percent !== undefined ? `, ${liveProgress.percent}%` : ""})` : ""}`
      : "";
    const hasSurveyState = Boolean(
      sourceRows.length
      || sourceLimitations.length
      || candidatePoolCount
      || detailFetched
      || evaluated
      || recommendedCount
      || liveProgressText
      || literature.status,
    );
    if (!hasSurveyState) {
      return renderSurveyShell(pendingSurveyState);
    }
    return renderSurveyShell((
      <>
        {sourceRows.length > 0 ? (
          <div className="sourceStatus compactSourceStatus">
            <h4 data-testid="find-source-status-heading">{t.sourceStatus}</h4>
            {sourceRows.map((item: any, index: number) => (
              <div className={String(item.status || "").toLowerCase() === "checking" ? "sourceRow" : item.ok ? "sourceRow ok" : "sourceRow fail"} key={`${item.source || item.venue || "source"}-${index}`}>
                <span>{sourceStatusLabel(item, venueById, lang)}</span>
                <small>{sourceStatusCompactDetail(item, lang)}</small>
              </div>
            ))}
          </div>
        ) : (
          <div className="emptyState compactFindSurveyEmpty" data-testid="find-source-status-empty">
            <p>{lang === "zh" ? "当前 Find run 尚未返回来源摘要；渠道抓取、候选筛选和评分概览会在本验收块内更新，完整来源状态写入产物。" : "The current Find run has not returned a source summary yet; source fetching, candidate screening, and scoring update here, while full source status is written to artifacts."}</p>
          </div>
        )}
        <div className="surveyFlowGrid compactSurveyFlow">
          <div><strong>{displayMaybe(counts.raw_title_index_papers || counts.title_total_papers || counts.venue_corpus_audited_papers || counts.venue_total_papers_available || currentFindCounts.scanned || currentFindCounts.corpusAudited)}</strong><span>{t.rawTitleIndexPapers}</span></div>
          <div><strong>{displayMaybe(categoryFilteredCount)}</strong><span>{t.categoryFilteredPapers}</span></div>
          <div><strong>{displayMaybe(tfidfScreenedCount)}</strong><span>{t.tfidfScreenedPapers}</span></div>
          <div><strong>{displayMaybe(titleScoredCount)}</strong><span>{t.titleScoredPapers}</span></div>
          <div><strong>{displayMaybe(evaluated)}</strong><span>{t.abstractScoredPapers}</span></div>
          <div><strong>{displayMaybe(recommendedCount)}</strong><span>{t.strongRecommendations}</span></div>
        </div>
        {sourceLimitations.length > 0 && (
          <details className="metricCard">
            <summary><strong>{sourceLimitations.length}</strong><span>{t.sourceLimitations}</span></summary>
            <div className="sourceStatus compactSourceStatus">
              {sourceLimitations.map((item: any, index: number) => (
                <div className={String(item.status || "").includes("ok") ? "sourceRow ok" : "sourceRow fail"} key={`${item.source || item.venue || index}-${index}`}>
                  <span>{sourceStatusLabel(item, venueById, lang)}</span>
                  <small>{displayMaybe(item.status)} / {displayMaybe(item.count, "")} / {displayMaybe(item.message || item.reason)}</small>
                </div>
              ))}
            </div>
          </details>
        )}
        {currentFindArtifactLoading && !freshFindActive && !sourceRows.length && !recommendedCount && !evaluated && (
          <div className="emptyState">{lang === "zh" ? "正在加载 Find 验收状态..." : "Loading Find review status..."}</div>
        )}
      </>
    ));
  }


  function renderExperimentGatePanel() {
    const referenceGate = referenceReproductionGate || {};
    const progressGate = scientificProgressGate || {};
    const iterationAudit = experimentIterationAudit || {};
    const fullCycleBlockers: any[] = [];
    const nextActions: any[] = [];
    const compute = referenceGate.compute_feasibility || {};
    const comparisons = asArray(referenceGate.comparisons);
    const paperComparison = comparisons.find((row: any) => row?.target?.paper_level) || comparisons[0] || {};
    const localComparison = comparisons.find((row: any) => row?.target && !row.target.paper_level) || comparisons[1] || {};
    const paperTarget = paperComparison.target || {};
    const localBest = paperComparison.best_reproduction || localComparison.best_reproduction || referenceGate.best_reproduction || {};
    const candidate = progressGate.best_candidate || {};
    const baseline = progressGate.best_control || progressGate.best_audit_ready_control || {};
    const loopChecks = asArray(iterationAudit.checks);
    const passedLoopChecks = loopChecks.filter((row: any) => String(row.status || "").toLowerCase() === "pass").length;
    const blockers: any[] = [];
    const blockerCategory = String(humanSupervision?.blocker?.category || "").toLowerCase();
    const referenceJob = researchFullCycle?.reference_full_job || supervisionTick?.full_reference_job || {};
    const liveReferenceRunning = referenceFullJobRunning
      || blockerCategory === "fresh_base_reference_reproduction_running"
      || (String(referenceJob?.status || "").toLowerCase() === "running" && referenceJob?.process_alive !== false);
    if (liveReferenceRunning && !mainRouteHumanPanelActive) {
      const baseTitle = displayMaybe(humanSupervision?.main_route?.base_title || (researchSummary as any)?.fresh_base?.title, lang === "zh" ? "当前参考工作" : "current reference work");
      const repoName = displayMaybe(humanSupervision?.main_route?.repo_name || (researchSummary as any)?.fresh_base?.repo_name, t.noData);
      const refPid = displayMaybe(referenceJob?.pid || humanSupervision?.blocker?.reference_full_job_pid, t.noData);
      const refLog = displayMaybe(referenceJob?.log_path || humanSupervision?.blocker?.reference_full_job_log, t.noData);
      return (
        <div className="panel experimentGatePanel compactHumanPanel">
          <div className="toolbar compactToolbar"><div><h3>{t.experimentGateOverview}</h3><p className="help">{lang === "zh" ? "当前参考复现进程来自真实 PID/日志；这里仅显示确定性运行状态，Experimenting 主控原文单独显示在模块回复中。" : "The current reproduction process comes from a real PID/log; this panel shows deterministic run status only, while Experimenting-controller text is shown separately."}</p></div></div>
          <div className="trajectorySupervisorGrid humanSummaryGrid">
            <article className="supervisorCard"><span>{lang === "zh" ? "主线基底" : "Main base"}</span><strong>{baseTitle}</strong><small>{repoName}</small></article>
            <article className="supervisorCard"><span>{lang === "zh" ? "当前任务" : "Current task"}</span><strong>{lang === "zh" ? "论文级参考复现" : "Full reference reproduction"}</strong><small>{lang === "zh" ? `运行中 / PID=${refPid}` : `running / PID=${refPid}`}</small></article>
            <article className="supervisorCard"><span>{lang === "zh" ? "下一步" : "Next"}</span><strong>{lang === "zh" ? "等待门控刷新" : "Wait for gate refresh"}</strong><small>{humanReadableMaybe(humanSupervision?.blocker?.next_action, lang === "zh" ? "完成后自动刷新参考复现、科学进展、论文证据和投稿 readiness 门控。" : "Refresh reproduction, scientific progress, paper evidence, and submission-readiness gates after completion.")}</small></article>
          </div>
          <div className="researchGateNote"><strong>{lang === "zh" ? "日志" : "Log"}:</strong> {refLog}</div>
        </div>
      );
    }
    if (humanGateSummary && Object.keys(humanGateSummary).length && !literatureGateBlocked && !liveReferenceRunning) {
      const ref = humanGateSummary.reference_reproduction || {};
      const science = humanGateSummary.scientific_progress || {};
      const loop = humanGateSummary.experiment_loop || {};
      const baseTitle = displayMaybe(humanGateSummary.main_route_title || humanSupervision?.main_route?.base_title, t.notSelected);
      const repoName = displayMaybe(humanGateSummary.main_route_repo || humanSupervision?.main_route?.repo_name, t.noData);
      return (
        <div className="panel experimentGatePanel compactHumanPanel">
          <div className="toolbar compactToolbar"><div><h3>{t.experimentGateOverview}</h3><p className="help">{lang === "zh" ? `当前主线：${baseTitle}` : `Current main route: ${baseTitle}`}</p></div></div>
          <div className="trajectorySupervisorGrid humanSummaryGrid">
            <article className="supervisorCard"><span>{lang === "zh" ? "主线基底" : "Main base"}</span><strong>{baseTitle}</strong><small>{repoName}</small></article>
            <article className="supervisorCard"><span>{t.referenceReproductionGate}</span><strong className={badgeClass(ref.status || referenceGate.status)}>{displayValue(ref.status || referenceGate.status || "not_started")}</strong><small>{humanReadableMaybe(ref.summary || referenceGate.human_summary, lang === "zh" ? "参考复现状态已由 审计；详见产物文件。" : "Reference reproduction status is audited by TASTE; see artifacts for evidence.")}</small></article>
            <article className="supervisorCard"><span>{t.scientificProgressGate}</span><strong className={badgeClass(science.status || progressGate.status)}>{displayValue(science.status || progressGate.status || "not_started")}</strong><small>{humanReadableMaybe(science.summary || progressGate.human_summary || progressGate.summary, lang === "zh" ? "当前还没有可写入论文的候选方法证据。" : "No promotable candidate-method evidence yet.")}</small></article>
            <article className="supervisorCard"><span>{t.iterationTrajectoryAudit}</span><strong className={badgeClass(loop.status || iterationAudit.status)}>{displayValue(loop.status || iterationAudit.status || "not_started")}</strong><small>{humanReadableMaybe(loop.summary || iterationAudit.human_summary || iterationAudit.summary, lang === "zh" ? "实验迭代状态等待 刷新。" : "Experiment-loop status is waiting for workflow refresh.")}</small></article>
          </div>
          <div className="researchGateNote warning"><strong>{lang === "zh" ? "当前阻塞" : "Current blocker"}:</strong> {humanReadableMaybe(humanGateSummary.summary || humanSupervision?.blocker?.summary, lang === "zh" ? "当前科研门控阻塞；完整证据见 state/report 文件。" : "A research gate is blocked; see state/report artifacts for evidence.")}</div>
          <div className="researchGateNote"><strong>{t.nextAction}:</strong> {humanCycleActionText(humanGateSummary.next_action || humanSupervision?.blocker?.next_action, supervisionFallbackNextAction())}</div>

        </div>
      );
    }
    if (literatureGateBlocked) {
      return (
        <div className="panel experimentGatePanel compactHumanPanel">
          <div className="toolbar compactToolbar">
            <div>
              <h3>{t.experimentGateOverview}</h3>
              <p className="help">{lang === "zh" ? "实验页只展示复现、训练、实验历史和实验门控。当前阻塞来自 Find 文献门控；本轮调研验收请回到“发现”页查看。" : "This page shows reproduction, training, experiment history, and experiment gates. The active blocker is the Find literature gate; review the current Find audit on the Find page."}</p>
            </div>
          </div>
          <div className="trajectorySupervisorGrid humanSummaryGrid">
            <article className="supervisorCard"><span>{freshFindRunning ? (lang === "zh" ? "当前阶段" : "Current stage") : (lang === "zh" ? "当前阻塞" : "Current blocker")}</span><strong className={freshFindRunning ? "" : "fail"}>{displayValue(freshFindRunning ? "fresh_find_running" : (String((researchSummary as any)?.status || "").includes("llm_quota_exhausted") ? "blocked_literature_llm_quota_exhausted" : "blocked_literature_recommendation_gate"))}</strong><small>{literatureGateShortfallText}</small></article>
            <article className="supervisorCard"><span>{lang === "zh" ? "实验执行" : "Experiment execution"}</span><strong>{freshFindRunning ? (lang === "zh" ? "等待" : "waiting") : (lang === "zh" ? "暂停" : "paused")}</strong><small>{lang === "zh" ? "不会启动新的复现、训练、实验子循环或论文写作。" : "No new reproduction, training, experiment loop, or paper-writing job will start."}</small></article>
            <article className="supervisorCard"><span>{lang === "zh" ? "下一步" : "Next"}</span><strong>{lang === "zh" ? "看发现页" : "Watch Find"}</strong><small>{freshFindRunning ? (lang === "zh" ? "跟随当前 Find 的检索、详情抓取和 LLM 评分进度。" : "Follow the current Find retrieval, detail fetch, and LLM scoring progress.") : (lang === "zh" ? "只允许修复当前 Find 的检索/评分包；不得用弱论文凑推荐。" : "Only repair the current Find retrieval/scoring packet; weak papers must not be padded into recommendations.")}</small></article>
          </div>
          <div className="researchGateNote warning"><strong>{lang === "zh" ? "分区说明" : "Display boundary"}:</strong> {lang === "zh" ? "Find 调研验收只在“发现”页展示；实验页只保留复现、训练、实验记录和实验门控。" : "Find audit details are shown only on the Find page; this page keeps reproduction, training, experiment records, and experiment gates."}</div>
          {supervisionTick?.generated_at && <div className="researchGateNote"><strong>{lang === "zh" ? "最近自动监督" : "Latest supervision"}:</strong> {displayMaybe(supervisionTick.action || supervisionTick.status, t.noData)} / {displayMaybe(supervisionTick.generated_at, t.noData)}</div>}
        </div>
      );
    }
    if (mainRouteHumanPanelActive) {
      const mainRouteNoteLabel = referenceFullJobIsRunning ? (lang === "zh" ? "当前任务" : "Current task") : t.currentBlockReason;
      const mainRouteNoteClass = referenceFullJobIsRunning ? "researchGateNote" : "researchGateNote warning";
      return (
        <div className="panel experimentGatePanel compactHumanPanel">
          <div className="toolbar compactToolbar"><div><h3>{t.experimentGateOverview}</h3><p className="help">{lang === "zh" ? `当前主线：${displayMaybe(humanSupervision?.main_route?.base_title, t.notSelected)}` : `Current main route: ${displayMaybe(humanSupervision?.main_route?.base_title, t.notSelected)}`}</p></div></div>
          <div className="trajectorySupervisorGrid humanSummaryGrid">
            <article className="supervisorCard"><span>{lang === "zh" ? "主线基底" : "Main base"}</span><strong>{displayMaybe(humanSupervision?.main_route?.base_title, t.notSelected)}</strong><small>{displayMaybe(humanSupervision?.main_route?.repo_name, t.noData)}</small></article>
            <article className="supervisorCard"><span>{lang === "zh" ? "当前状态" : "Status"}</span><strong className={badgeClass(humanSupervision?.status)}>{displayValue(humanSupervision?.status || "blocked")}</strong><small>{humanCycleActionText(humanSupervision?.blocker?.summary, localizedField(humanSupervision, "summary", t.noData))}</small></article>
            <article className="supervisorCard"><span>{lang === "zh" ? "已完成" : "TASTE completed"}</span><strong>{lang === "zh" ? "数据/loader/协议/smoke 已通过" : "data/loader/protocol/smoke passed"}</strong><small>{lang === "zh" ? "当前正在补齐论文级 full reference reproduction。" : "Now completing paper-level full reference reproduction."}</small></article>
            {referenceFullJobStatus && <article className="supervisorCard"><span>{referenceFullJobIsRunning ? (lang === "zh" ? "正在执行" : "running") : (lang === "zh" ? "已完成" : "TASTE completed")}</span><strong>{displayMaybe(referenceFullJobStatus, t.noData)}</strong><small>{referenceFullJobDetailText}</small></article>}
          </div>
          <div className={mainRouteNoteClass}><strong>{mainRouteNoteLabel}:</strong> {humanReadableMaybe(humanSupervision?.blocker?.title, t.noData)}。{humanCycleActionText(humanSupervision?.blocker?.next_action, t.noData)}</div>
          {supervisionTick?.generated_at && <div className="researchGateNote"><strong>{lang === "zh" ? "最近自动监督" : "Latest supervision"}:</strong> {displayMaybe(supervisionTick.action || supervisionTick.status, t.noData)} / {displayMaybe(supervisionTick.generated_at, t.noData)} / {lang === "zh" ? "问题" : "issues"}: {displayMaybe(supervisionTick.issue_count, "0")}</div>}
          {freshBaseDataBlocked && <div className="researchGateNote"><strong>{lang === "zh" ? "缺失文件" : "Missing files"}:</strong> {displayMaybe(humanSupervision?.blocker?.missing_files, t.noData)}</div>}
        </div>
      );
    }
    const referenceGateDetail = humanReadableMaybe(referenceGate.human_summary || referenceGate.summary || referenceGate.reason, lang === "zh" ? "参考复现状态等待 审计刷新；详见底部任务栏和产物文件。" : "Reference reproduction status is waiting for TASTE audit refresh; see the taskbar and artifacts.");
    const progressGateDetail = humanReadableMaybe(progressGate.human_summary || progressGate.summary || progressGate.reason, lang === "zh" ? "当前缺少可写入论文的候选方法证据；完整证据见 state/report 文件。" : "No promotable candidate-method evidence yet; see state/report artifacts for evidence.");
    const iterationGateDetail = humanReadableMaybe(iterationAudit.human_summary || iterationAudit.summary || iterationAudit.reason, lang === "zh" ? "实验迭代状态等待 刷新。" : "Experiment-loop status is waiting for workflow refresh.");
    const cards = [
      {
        label: t.referenceReproductionGate,
        value: displayValue(referenceGate.status || "not_started"),
        status: referenceGate.status || "not_started",
        detail: referenceGateDetail,
        small: referenceFullJobIsRunning ? referenceFullJobDetailText : "",
      },
      {
        label: t.scientificProgressGate,
        value: displayValue(progressGate.status || "not_started"),
        status: progressGate.status || "not_started",
        detail: progressGateDetail,
        small: "",
      },
      {
        label: t.iterationTrajectoryAudit,
        value: displayValue(iterationAudit.status || "not_started"),
        status: iterationAudit.status || "not_started",
        detail: iterationGateDetail,
        small: loopChecks.length ? `${t.loopCompleteness}: ${passedLoopChecks}/${loopChecks.length}` : "",
      },
    ];
    if (compute.status && String(compute.status).toLowerCase() !== "unknown") {
      cards.splice(1, 0, {
        label: t.computeFeasibility,
        value: displayValue(compute.status),
        status: compute.status,
        detail: lang === "zh" ? "算力信息已由参考复现任务记录；详细耗时和 GPU 见底部任务栏日志和产物。" : "Compute evidence is recorded by the reproduction job; see the taskbar logs and artifacts for runtime/GPU details.",
        small: "",
      });
    }
    return (
      <div className="panel experimentGatePanel compactHumanPanel">
        <div className="toolbar compactToolbar">
          <div>
            <h3>{t.experimentGateOverview}</h3>
            <p className="help">{t.experimentGateHelp}</p>
          </div>
        </div>
        <div className="trajectorySupervisorGrid humanSummaryGrid">
          {cards.map((row) => (
            <article className="supervisorCard" key={row.label}>
              <span>{row.label}</span>
              <strong className={badgeClass(row.status)}>{row.value}</strong>
              <small>{row.detail}</small>
              <small>{row.small}</small>
            </article>
          ))}
        </div>
        {fullCycleBlockers.length > 0 && (
          <div className="researchGateNote warning">
            <strong>{lang === "zh" ? "当前阻塞" : "Current blocker"}:</strong> {humanReadableMaybe(fullCycleBlockers[0]?.human_summary || fullCycleBlockers[0]?.summary || fullCycleBlockers[0]?.issue || fullCycleBlockers[0], lang === "zh" ? "当前存在科研门控阻塞；完整证据见 state/report 文件。" : "A research gate is blocked; see state/report artifacts for full evidence.")}
          </div>
        )}
        {nextActions.length > 0 && (
          <details className="metricCard" open>
            <summary><strong>{nextActions.length}</strong><span>{t.nextAction}</span></summary>
            <div className="detailList">
              {nextActions.map((item: any, index: number) => (
                <article className="detailItem" key={`next-action-${index}`}>
                  <div className="detailTitle"><strong>{displayValue(item.route || item.category || item.source_check_id || "action")}</strong><span className={`stageBadge ${badgeClass(item.severity || item.priority || "blocked")}`}>{displayValue(item.priority || item.severity || "blocked")}</span></div>
                  <p>{displayMaybe(item.issue || item.repair_strategy || item.next_action)}</p>
                  {item.repair_strategy && <small>{displayMaybe(item.repair_strategy)}</small>}
                </article>
              ))}
            </div>
          </details>
        )}
        {String(referenceGate.status || "") !== "pass" && <div className="researchGateNote warning">{t.mustRepairBeforeNovel}</div>}
        {blockers.length > 0 && (
          <div className="humanIssueList">
            <h4>{t.keyBlockers}</h4>
            {blockers.map((item: any, index: number) => <article className="detailCard" key={`experiment-gate-blocker-${index}`}><p>{humanReadableMaybe(item?.human_summary || item?.summary || item?.issue || item, lang === "zh" ? "当前存在科研门控阻塞；完整证据见 state/report 文件。" : "A research gate is blocked; see state/report artifacts for full evidence.")}</p></article>)}
          </div>
        )}
      </div>
    );
  }


  function renderTrajectorySystemPanel() {
    const trajectory = trajectorySystem || {};
    const hasTrajectory = Boolean(trajectory && Object.keys(trajectory).length);
    const objectives = asArray(trajectory.next_objectives);
    const queueRows = asArray(trajectory.optimization_queue || trajectory.optimization_plan?.queue);
    const issues = asArray(trajectory.assurance_issues);
    const memory = trajectory.memory || {};
    const blockerRows = (issues.length ? issues : asArray(trajectory.evidence_integrity?.issues)).slice(0, 1);
    if (!hasTrajectory) return null;
    const memoryTotal = Number(memory.ideation_entries || 0) + Number(memory.experimentation_entries || 0) + Number(memory.assurance_entries || 0) + Number(memory.trajectory_entries || 0);
    const nextObjective = objectives[0]
      ? localizedField(objectives[0], "text", displayMaybe(objectives[0].text))
      : queueRows[0]
        ? localizedField(queueRows[0], "objective", displayMaybe(queueRows[0].objective))
        : t.noNextObjectives;
    const phase = localizedText(trajectory.phase_i18n, displayValue(trajectory.phase || trajectory.assurance_status));
    const status = trajectory.assurance_status || trajectory.phase || "not_started";
    return (
      <div className="panel trajectoryPanel compactHumanPanel">
        <div className="sectionTitle compactSectionTitle">
          <h3>{t.researchTrajectorySystem}</h3>
          <span className={`stageBadge ${badgeClass(status)}`}>{displayValue(status)}</span>
        </div>
        <p className="help">{t.researchTrajectoryHelp}</p>
        <div className="trajectorySupervisorGrid humanSummaryGrid compactSupervisorGrid">
          <article className="supervisorCard">
            <span>{t.trajectoryPhase}</span>
            <strong>{phase}</strong>
            <small>{localizedText(trajectory.summary_i18n, t.noData)}</small>
          </article>
          <article className="supervisorCard">
            <span>{t.nextAction}</span>
            <strong>{nextObjective}</strong>
            <small>{t.optimizationQueue}: {trajectory.optimization_queue_size ?? queueRows.length}</small>
          </article>
          <article className="supervisorCard">
            <span>{t.memoryHealth}</span>
            <strong>{memoryTotal ? t.ready : t.noData}</strong>
            <small>{t.ideationMemory}: {memory.ideation_entries ?? 0}; {t.experimentationMemory}: {memory.experimentation_entries ?? 0}</small>
          </article>
        </div>
        {blockerRows.length > 0 && (
          <div className="researchGateNote warning">
            <strong>{t.keyBlockers}:</strong> {localizedField(blockerRows[0], "issue", displayMaybe(blockerRows[0].issue))}
          </div>
        )}
      </div>
    );
  }

  function renderClaudeSessionPanel(stage: "environment" | "experiment" | "paper") {
    const moduleClaudeLabel = stage === "paper"
      ? (lang === "zh" ? "Writing 主控 Claude" : "Writing controller")
      : stage === "environment"
        ? (lang === "zh" ? "Environment 主控 Claude" : "Environment controller")
        : (lang === "zh" ? "Experimenting 主控 Claude" : "Experimenting controller");
    const guidanceJob = jobs.find((item) => jobMatchesClaudePanelStage(item, stage));
    const guidanceKey = `${stage}:controller`;
    const guidanceDraft = agentGuidanceMessages[guidanceKey] ?? agentGuidanceMessage;
    const guidanceProgressText = guidanceJob?.progress
      ? String(guidanceJob.progress.message || `${displayMaybe(guidanceJob.progress.phase || guidanceJob.stage || stage)} / ${guidanceJob.progress.total ? `${guidanceJob.progress.current}/${guidanceJob.progress.total}` : `${guidanceJob.progress.current || 0} ${t.events}`}`)
      : "";
    const guidanceRows = Array.isArray((researchSummary as any)?.queued_guidance)
      ? (researchSummary as any).queued_guidance
      : [];
    const queued = guidanceRows
      .filter((item: any) => String(item?.status || "queued") === "queued")
      .filter((item: any) => normalizeClaudePanelStage(item?.stage) === stage)
      .map((item: any) => {
        const created = formatDateMinute(item?.created_at, lang) || displayMaybe(item?.created_at, "");
        const status = stage === "environment"
          ? (lang === "zh" ? "等待 Environment 主控 Claude" : "queued for Environment controller")
          : stage === "paper"
            ? (lang === "zh" ? "等待 Writing 主控 Claude" : "queued for Writing controller")
            : (lang === "zh" ? "等待 Experimenting 主控 Claude" : "queued for Experimenting controller");
        const itemStage = String(item?.stage || "project");
        const stageLabel = itemStage && itemStage !== stage ? `${lang === "zh" ? "阶段" : "stage"}=${displayValue(itemStage)}` : "";
        return [created, status, stageLabel, displayMaybe(item?.message, "")].filter(Boolean).join(" / ");
      })
      .filter(Boolean);
    const latestReceipt = latestClaudeReceiptForStage(stage) as any;
    const receiptStatusRaw = String(latestReceipt?.status || "completed");
    const receiptStatusKey = receiptStatusRaw.toLowerCase().replace(/[-\s]+/g, "_");
    const receiptStatusLabel = receiptStatusKey === "blocked_tool_policy"
      ? (lang === "zh" ? "安全策略已拦截" : "Safely blocked")
      : displayValue(receiptStatusRaw);
    const fullResponseRequest = latestClaudeFullResponseRequests.find((item) => item.stage === stage);
    const controllerConversation = (stage === "paper" || stage === "environment") && Array.isArray(latestReceipt?.conversation)
      ? latestReceipt.conversation
      : [];
    const receiptRows = controllerConversation.length > 0 ? controllerConversation.map((turn: any, index: number) => ({
      id: turn?.message_id || `${stage}-claude-${index}`,
      status: displayValue(turn?.status || "completed"),
      meta: [
        turn?.finished_at ? `${lang === "zh" ? "完成时间" : "finished"}=${formatDateMinute(turn.finished_at, lang) || turn.finished_at}` : "",
        turn?.controller_turn ? `${lang === "zh" ? "主控轮次" : "controller turn"}=${turn.controller_turn}` : "",
        turn?.target_venue ? `${lang === "zh" ? "投稿目标" : "venue"}=${turn.target_venue}` : "",
        turn?.session_id ? `session=${String(turn.session_id).slice(0, 8)}` : "",
      ].filter(Boolean).join(" / "),
      instruction: String(turn?.instruction || ""),
      response: publicLogText(String(turn?.response_markdown || ""), lang),
      fullResponseKey: index === controllerConversation.length - 1 ? (fullResponseRequest?.key || claudeFullResponseKeyForStage(stage, latestReceipt)) : "",
      fullResponseStage: stage,
      fullResponseAvailable: index === controllerConversation.length - 1 && Boolean(fullResponseRequest?.available || latestReceipt?.full_response_available),
      responseCharCount: index === controllerConversation.length - 1 ? Number(latestReceipt?.response_chcount || 0) : 0,
      source: String(turn?.response_source || ""),
    })) : latestReceipt?.response_markdown ? [{
      id: latestReceipt?.session_id || `${stage}-claude-latest`,
      status: receiptStatusLabel,
      meta: [
        latestReceipt?.finished_at ? `${lang === "zh" ? "完成时间" : "finished"}=${formatDateMinute(latestReceipt.finished_at, lang) || latestReceipt.finished_at}` : "",
        latestReceipt?.controller_turn ? `${lang === "zh" ? "主控轮次" : "controller turn"}=${latestReceipt.controller_turn}` : "",
        stage === "paper" && latestReceipt?.session_id ? `session=${String(latestReceipt.session_id).slice(0, 8)}` : "",
      ].filter(Boolean).join(" / "),
      instruction: String(latestReceipt?.instruction || ""),
      response: publicLogText(String(latestReceipt?.response_markdown || ""), lang),
      fullResponseKey: fullResponseRequest?.key || claudeFullResponseKeyForStage(stage, latestReceipt),
      fullResponseStage: fullResponseRequest?.stage || stage,
      fullResponseAvailable: Boolean(fullResponseRequest?.available || latestReceipt?.full_response_available || latestReceipt?.raw_response_hidden || latestReceipt?.content_compacted),
      responseCharCount: Number(latestReceipt?.response_chcount || 0),
      source: String(latestReceipt?.response_source || ""),
    }] : [];
    const statusHelp = stage === "paper"
      ? (lang === "zh"
        ? "输入的自然语言只进入本项目独立的 Writing 主控 Claude Code；每条消息续接同一个会话，回复会直接显示在这里。"
        : "Natural-language input goes only to this project's dedicated Writing controller; every message resumes the same session and its reply appears here.")
      : stage === "environment"
        ? (lang === "zh"
          ? "输入的自然语言只进入当前项目唯一的 Environment 主控 Claude 会话。"
          : "Natural-language input goes only to the project's unique Environment controller session.")
        : (lang === "zh"
          ? "输入的自然语言只进入当前项目唯一的 Experimenting 主控 Claude 会话。"
          : "Natural-language input goes only to the project's unique Experimenting controller session.");
    const logRedirectHelp = stage === "paper"
      ? (lang === "zh"
        ? "本面板只连接当前项目唯一的 Writing 主控 Claude；忙碌时消息进入模块队列，也可打断当前任务并优先处理。原任务随后由同一会话恢复。"
        : "This panel only targets the project's unique Writing controller; busy messages enter the module queue, or can interrupt the current task and run first. The same session then resumes the original work.")
      : stage === "environment"
        ? (lang === "zh"
          ? "本面板只连接 Environment 主控会话；环境部署和审计仍由 Environment 阶段按钮提交给同一模块。"
          : "This panel only connects to the Environment controller session; the Environment stage button submits deployment and audit work to that same module.")
        : (lang === "zh"
          ? "本面板只连接当前项目唯一的 Experimenting 主控 Claude；忙碌时消息进入模块队列，也可打断当前任务并优先处理。"
          : "This panel only targets the project's unique Experimenting controller; busy messages enter the module queue, or can interrupt the current task and run first.");
    return (
      <div className="panel claudeSessionPanel">
        <div className="toolbar compactToolbar">
          <div>
            <h3>{stage === "environment" ? (lang === "zh" ? "Environment 主控 Claude 对话" : "Environment Controller Chat") : t.claudeSessionTitle}</h3>
            <p className="help">{t.claudeSessionHelp}</p>
          </div>
        </div>
        <div className="agentPanel guidancePanel">
          <div className="agentHeader">
            <div>
              <h4>{lang === "zh" ? `发送给 ${moduleClaudeLabel}` : `Send to ${moduleClaudeLabel}`}</h4>
              <p className="help">{statusHelp}</p>
            </div>
          </div>
          {queued.length > 0 && <div className="researchGateNote"><strong>{t.queuedGuidance}:</strong> {queued.join(" / ")}</div>}
          <label>{lang === "zh" ? "指令内容" : "Instruction"}</label>
          <textarea
            value={guidanceDraft || ""}
            onChange={(e) => {
              const value = e.target.value;
              setAgentGuidanceMessages((prev) => ({ ...prev, [guidanceKey]: value }));
              setAgentGuidanceMessage(value);
            }}
            placeholder={stage === "paper" ? t.claudePaperPlaceholder : stage === "experiment" ? t.claudeExperimentPlaceholder : t.claudeEnvPlaceholder}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
          />
          <div className="actions">
            <button onClick={() => queueAgentGuidance(stage)} disabled={!researchProject || !String(guidanceDraft || "").trim()}>{lang === "zh" ? `发送给 ${moduleClaudeLabel}` : `Send to ${moduleClaudeLabel}`}</button>
            <button className="danger" onClick={() => queueAgentGuidance(stage, true)} disabled={!researchProject || !String(guidanceDraft || "").trim()}>{t.interruptEnvironmentClaude}</button>
          </div>
        </div>
        {guidanceJob && (
          <div className={guidanceJob.status === "error" ? "claudeLiveBox errorLiveBox" : "claudeLiveBox"}>
            <div className="jobHeader">
              <strong>{guidanceJob.status === "done" ? t.claudeDone : guidanceJob.status === "error" ? t.claudeFailed : guidanceJob.status === "blocked" ? t.blocked : t.queuedGuidance}</strong>
              <span>{displayValue(guidanceJob.status)}</span>
            </div>
            {guidanceProgressText && <small className="claudeProgress">{guidanceProgressText}</small>}
            {String(guidanceJob?.progress?.phase || "") === "queued" && (guidanceJob?.result?.instruction || guidanceJob?.progress?.message) && <p><strong>{lang === "zh" ? "正在排队的消息：" : "Queued message: "}</strong>{String(guidanceJob?.result?.instruction || guidanceJob?.progress?.message)}</p>}
            <p className="help">{logRedirectHelp}</p>
          </div>
        )}
        <details className="transcriptBox" open>
          <summary>{stage === "environment" ? (lang === "zh" ? "Environment 主控 Claude 会话记录" : "Environment controller conversation") : t.claudeTranscriptTitle}</summary>
          {receiptRows.length > 0 ? (
            <div className="guidanceReceiptList">
              {receiptRows.map((item: any, index: number) => {
                const fullState = claudeFullResponses[item.fullResponseKey] || {};
                const fullData = fullState.data || {};
                const fullText = String(fullData.response_markdown || "");
                const fullMeta = [
                  fullData.source ? `${lang === "zh" ? "来源" : "source"}=${fullData.source}` : "",
                  fullData.response_chcount ? `${lang === "zh" ? "字符" : "chars"}=${fullData.response_chcount}` : "",
                  fullData.truncated ? `${lang === "zh" ? "已截断前部" : "head truncated"}=${fullData.truncated_head_chars || 0}` : "",
                ].filter(Boolean).join(" / ");
                return <div className="guidanceReceipt" key={item.id || `${stage}-guidance-${index}`}>
                  <strong>{item.status}</strong>
                  {item.meta ? <small>{item.meta}</small> : null}
                  {item.instruction ? <><strong>{lang === "zh" ? "你的指令" : "Your instruction"}</strong><pre>{publicLogText(item.instruction, lang)}</pre></> : null}
                  <strong>{stage === "paper" ? (lang === "zh" ? "Writing 主控回复" : "Writing controller reply") : stage === "environment" ? (lang === "zh" ? "Environment 主控回复" : "Environment controller reply") : (lang === "zh" ? "处理摘要" : "Processing summary")}</strong>
                  {item.response ? <pre>{publicLogText(item.response, lang)}</pre> : <p>{stage === "environment" ? (lang === "zh" ? "Environment 主控 Claude 尚未返回处理结果。" : "The Environment controller has not returned a result yet.") : (lang === "zh" ? "对应模块 Claude 尚无处理摘要。" : "The module Claude turn has no processing summary yet.")}</p>}
                  {item.fullResponseAvailable && (
                    <div className="receiptActions">
                      <button type="button" onClick={() => loadClaudeFullResponse(item.fullResponseKey, item.fullResponseStage || stage)} disabled={Boolean(fullState.loading)}>{fullState.loading ? (lang === "zh" ? "正在加载完整回复..." : "Loading full response...") : fullText ? (lang === "zh" ? "刷新模块主控完整回复" : "Refresh full controller response") : (lang === "zh" ? "查看模块主控完整回复" : "Show full controller response")}</button>
                      {item.responseCharCount ? <small>{lang === "zh" ? `完整回复约 ${item.responseCharCount} 字符；点击按钮可查看或刷新。` : `Full response is about ${item.responseCharCount} chars; use the button to show or refresh it.`}</small> : null}
                    </div>
                  )}
                  {fullState.error && <p className="errorText">{fullState.error}</p>}
                  {fullText && <div className="fullClaudeResponse"><strong>{lang === "zh" ? "模块主控完整回复" : "Full module-controller response"}</strong>{fullMeta && <small>{fullMeta}</small>}<pre>{fullText}</pre></div>}
                </div>;
              })}
              <p className="help">{logRedirectHelp}</p>
            </div>
          ) : (
            <div className="emptyState"><p>{stage === "environment" ? (lang === "zh" ? "Environment 主控 Claude 尚无处理摘要；当前任务状态显示在底部任务栏。" : "The Environment controller has no processing summary yet; current task status appears in the bottom taskbar.") : t.noClaudeTranscript}</p><p className="help">{logRedirectHelp}</p></div>
          )}
        </details>
      </div>
    );
  }


  function renderARRuntimePanel() {
    if (!researchProject) return null;
    const hasRuntimeDiagnostics = Object.keys(runtimeChecks || {}).length > 0;
    return (
      <details className="panel compact runtimePanel sidebarDetails">
        <summary><span>{t.researchRuntimeTitle}</span><small>{hasRuntimeDiagnostics ? (lang === "zh" ? "已诊断" : "diagnosed") : (lang === "zh" ? "待诊断" : "pending")}</small></summary>
        <p className="help">{t.researchRuntimeHelp}</p>
        <details className="roleSettings">
          <summary>{t.remoteToolPaths}</summary>
          <label>{t.nodeBinDir}</label>
          <input value={researchRuntimeDraft.node_bin || ""} onChange={(e) => updateRuntimeDraft("node_bin", e.target.value)} placeholder="/path/to/node/bin" />
          <label>{t.claudeExecutable}</label>
          <input value={researchRuntimeDraft.claude_path || ""} onChange={(e) => updateRuntimeDraft("claude_path", e.target.value)} placeholder="claude" />
          <label>{t.managementPythonExecutable}</label>
          <input value={researchRuntimeDraft.management_python || ""} onChange={(e) => updateRuntimeDraft("management_python", e.target.value)} placeholder="python" />
          <label>{t.extraPath}</label>
          <input value={researchRuntimeDraft.extra_path || ""} onChange={(e) => updateRuntimeDraft("extra_path", e.target.value)} placeholder="/custom/bin:/another/bin" />
          <div className="saveBar">
            <button onClick={detectRuntimeConfig} disabled={researchRuntimeSaving}>{t.autoDetectClaude}</button>
            <button className="primary" onClick={saveRuntimeConfig} disabled={researchRuntimeSaving}>{researchRuntimeSaving ? t.saving : t.saveAndDiagnose}</button>
            {researchRuntimeMessage && <span>{researchRuntimeMessage}</span>}
          </div>
        </details>
        <div className="runtimeChecks">
          {["claude", "node", "npm", "management_python"].map((name) => {
            const check = runtimeChecks?.[name] || {};
            const lockedReady = environmentLocked && Object.keys(check).length === 0;
            const waitingForDiagnostics = !hasRuntimeDiagnostics && !lockedReady;
            const ok = Boolean(check.ok || lockedReady);
            const statusClass = waitingForDiagnostics ? "runtimeCheck idle" : ok ? "runtimeCheck ok" : "runtimeCheck fail";
            return (
              <div className={statusClass} key={name}>
                <strong>{name}</strong>
                <span>{waitingForDiagnostics ? (lang === "zh" ? "诊断加载中" : "loading") : ok ? (lockedReady ? t.runtimeLockedReady : "ok") : t.missing}</span>
                <small>{check.path || check.reason || (waitingForDiagnostics ? (lang === "zh" ? "等待远端项目摘要返回诊断结果" : "waiting for remote diagnostics") : lockedReady ? t.runtimeLockedReadyDetail : t.noDiagnostics)}</small>
                {check.version && <small>{check.version}</small>}
              </div>
            );
          })}
        </div>
      </details>
    );
  }

  async function runEmail() {
    const artifactRunId = renderedRunArtifactsRunId || runId;
    if (!artifactRunId) return;
    try {
      setError("");
      const nextConfig = configWithCurrentFindSelection();
      await saveConfig(nextConfig);
      setConfig(nextConfig);
      const receivers = emailReceiversOverride.trim() ? splitList(emailReceiversOverride) : [];
      const artifactNames = visibleRunArtifacts.map((artifact) => artifact.name);
      const nextJob = await startEmail({
        run_id: artifactRunId,
        artifact_names: artifactNames,
        receivers,
        subject: emailSubject,
        include_ranking: true,
      });
      attachJob(nextJob);
    } catch (err) {
      setError(String(err));
    }
  }

  async function stopJob(jobId: string) {
    if (jobId.startsWith("full-cycle-") || jobId.startsWith("agent-")) {
      setError(lang === "zh" ? "后台 主控进程不能从网页按钮队列停止；请使用统一监督入口处理恢复或中止。" : "Background controller processes cannot be stopped from the web button queue; use the unified supervision entrypoint for recovery or shutdown.");
      return;
    }
    updateJob(await cancelJob(jobId));
  }

  return (
    <main className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="mark" aria-label="TASTE">T</span>
          <div>
            <h1>TASTE</h1>
            <p>{window.location.host}</p>
          </div>
        </div>
        <div className="accountBar">
          <span title={account.username}>{account.username}</span>
          <button className="smallButton" onClick={onLogout}>{lang === "zh" ? "退出" : "Sign out"}</button>
        </div>
        <div className="langSwitch" aria-label="Language">
          <button className={lang === "zh" ? "active" : ""} onClick={() => setLang("zh")}>{t.languageChinese}</button>
          <button className={lang === "en" ? "active" : ""} onClick={() => setLang("en")}>{t.languageEnglish}</button>
        </div>

        <section className="panel researchGlobalCard">
          <h2>{t.researchResearchTopic}</h2>
          {!researchProjectsLoaded ? <div className="emptyState">{t.researchProjectLoading}</div> : researchProjects.length === 0 ? <div className="emptyState">{t.researchNoProject}</div> : null}
          {researchProjects.length > 0 && (
            <>
              <label>{t.currentProject}</label>
              <select value={researchProject} onChange={(e) => void loadProject(e.target.value)}>
                {researchProjects.map((project) => <option value={project.id} key={project.id}>{project.id} / {project.topic}</option>)}
              </select>
              <p className="help">{t.researchGlobalHelp}</p>
              <div className="researchContextBox">
                <strong>{t.researchTopic}</strong>
                <span>{displayMaybe(researchSummary?.config?.topic || selectedProject?.topic || researchTopic, t.notCompleted)}</span>
              </div>
            </>
          )}
          <details className="createProjectBox">
            <summary>{t.createProject}</summary>
            <p className="help">{t.researchCreateProjectHelp}</p>
            <label>{t.researchProjectId}</label>
            <input value={newProjectId} onChange={(e) => setNewProjectId(e.target.value)} placeholder={t.researchProjectIdPlaceholder} />
            <label>{t.researchTopic}</label>
            <input value={researchTopic} onChange={(e) => setTopic(e.target.value)} />
            <button className="primary" onClick={handleCreateProject} disabled={creatingProject || !newProjectId.trim()}>{creatingProject ? t.saving : t.createProject}</button>
            {researchProjectMessage && <span className="inlineSuccess">{researchProjectMessage}</span>}
          </details>
        </section>

        <details className="panel sidebarDetails">
          <summary><span>{t.profile}</span></summary>
          <p className="help">{t.interestHelp}</p>
          <label>{t.interest}</label>
          <textarea value={researchResearchInterest} onChange={(e) => { setResearchResearchInterest(e.target.value); setProjectConfigMessage(""); }} onBlur={() => { if (researchProject) void saveProjectConfigDraft({ silent: true }); }} autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false} />
          <label>{t.researcher}</label>
          <p className="help">{t.researcherHelp}</p>
          <textarea value={researchResearcherProfile} onChange={(e) => { setResearchResearcherProfile(e.target.value); setProjectConfigMessage(""); }} onBlur={() => { if (researchProject) void saveProjectConfigDraft({ silent: true }); }} autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false} />
        </details>

        <details className="panel compact sidebarDetails">
          <summary><span>{t.llm}</span><small>{config.provider} / {config.model}</small></summary>
          <p className="help">{t.llmHelp}</p>
          <label>{t.provider}</label>
          <p className="help">{t.providerHelp}</p>
          <input value={config.provider} onChange={(e) => updateConfig("provider", e.target.value)} placeholder="openai" />
          <label>{t.baseUrl}</label>
          <p className="help">{t.baseUrlHelp}</p>
          <input value={config.base_url} onChange={(e) => updateConfig("base_url", e.target.value)} placeholder="https://api.openai.com/v1" />
          <label>{t.model}</label>
          <p className="help">{t.modelHelp}</p>
          <input value={config.model} onChange={(e) => updateConfig("model", e.target.value)} placeholder="gpt-4o-mini" />
          <label>{t.apiKey}</label>
          <p className="help">{t.apiKeyHelp}</p>
          <input value={config.api_key || ""} onChange={(e) => { updateConfig("api_key", e.target.value); setLLMProbeResult(null); }} placeholder={config.api_key_saved ? (lang === "zh" ? "已保存，输入新 key 替换" : "Saved, enter a new key to replace") : "sk-..."} type="password" autoComplete="new-password" />
          {config.api_key_saved && !config.api_key && <p className="help">{savedSecretHint(config.api_key_saved)}{config.api_key_suffix ? ` · ${lang === "zh" ? "尾号" : "suffix"}: ${config.api_key_suffix}` : ""}</p>}
          {(config.config_saved_at || config.project_llm_synced !== undefined) && <p className="help">{lang === "zh" ? "当前服务已保存配置" : "Saved config on this service"}{config.config_saved_at ? `: ${formatDateMinute(config.config_saved_at, lang)}` : ""}{config.project_llm_synced !== undefined ? ` · ${lang === "zh" ? "项目同步" : "project sync"}: ${config.project_llm_synced ? (lang === "zh" ? "已同步" : "synced") : (lang === "zh" ? "未同步" : "not synced")}` : ""}</p>}
          {unsavedLLMConfigDraft && <p className="help llmDraftWarning">{lang === "zh" ? "检测到未保存的 LLM 密钥草稿；只有点击“保存配置”或“验证 LLM”后，Find/workflow 才会使用新配置。" : "Unsaved LLM key draft detected; Find/The workflow will use it only after Save config or Validate LLM."}</p>}
          <div className="saveBar">
            <button className="primary" onClick={handleSaveConfig} disabled={savingConfig}>{savingConfig ? t.saving : t.saveConfig}</button>
            <button onClick={handleProbeLLMConfig} disabled={llmProbeLoading || savingConfig}>{llmProbeLoading ? t.validatingLLM : t.validateLLM}</button>
            {llmProbeResult && <span className={llmProbeResult.ok ? "okText" : "errorText"}>{llmProbeResult.ok ? (lang === "zh" ? "可用" : "available") : (llmProbeResult.error || (lang === "zh" ? "不可用" : "unavailable"))}</span>}
            {saveMessage && <span>{saveMessage}</span>}
          </div>
          <p className="help">{t.llmProbeHelp}</p>
          {llmProbeResult?.summary && <p className="help">{[llmProbeResult.summary.provider, llmProbeResult.summary.model, llmProbeResult.summary.base_url].filter(Boolean).join(" · ")}</p>}
          <label>{t.temperature}</label>
          <p className="help">{t.temperatureHelp}</p>
          <div className="row">
            <input value={config.temperature} onChange={(e) => updateConfig("temperature", Number(e.target.value))} type="number" step="0.1" />
          </div>
          <details className="roleSettings">
            <summary>{t.emailSettings}</summary>
            <p className="help">{t.emailHelp}</p>
            <label>{t.smtpServer}</label>
            <input value={config.email.smtp_server} onChange={(e) => updateEmailConfig("smtp_server", e.target.value)} placeholder="smtp.example.com" />
            <label>{t.smtpPort}</label>
            <input value={config.email.smtp_port} onChange={(e) => updateEmailConfig("smtp_port", Number(e.target.value))} type="number" min="1" />
            <label>{t.emailSender}</label>
            <input value={config.email.sender} onChange={(e) => updateEmailConfig("sender", e.target.value)} placeholder="sender@example.com" />
            <label>{t.emailReceivers}</label>
            <p className="help">{t.emailReceiversHelp}</p>
            <input value={asArray(config.email?.receivers).join(", ")} onChange={(e) => updateEmailConfig("receivers", splitList(e.target.value))} placeholder="receiver@example.com" />
            <label>{t.smtpPassword}</label>
            <input value={config.email.smtp_password || ""} onChange={(e) => updateEmailConfig("smtp_password", e.target.value)} placeholder={config.email.smtp_password_saved ? (lang === "zh" ? "已保存，输入新密码替换" : "Saved, enter a new password to replace") : ""} type="password" autoComplete="new-password" />
            {config.email.smtp_password_saved && !config.email.smtp_password && <p className="help">{savedSecretHint(config.email.smtp_password_saved)}</p>}
            <label className="switch">
              <input type="checkbox" checked={config.email.manual_enabled} onChange={(e) => updateEmailConfig("manual_enabled", e.target.checked)} />
              {t.sendEmail}
            </label>
            <label className="switch">
              <input type="checkbox" checked={config.email.auto_send_enabled} onChange={(e) => updateEmailConfig("auto_send_enabled", e.target.checked)} />
              {t.autoEmail}
            </label>
            <label>{t.autoEmailStages}</label>
            <input value={asArray(config.email?.auto_send_stages).join(", ")} onChange={(e) => updateEmailConfig("auto_send_stages", splitList(e.target.value))} placeholder="find, read, idea, plan" />
          </details>
        </details>

        {renderARRuntimePanel()}

        <details className="panel runs sidebarDetails">
          <summary><span>{t.runs}</span><small>{runId || (lang === "zh" ? "未选择" : "none")}</small></summary>
          <div className="panelHeaderLine compactHeader">
            {projectRuns.length > 12 && <button className="smallButton" onClick={() => setShowAllRuns((value) => !value)}>{showAllRuns ? t.showRecentRuns : t.showAllRuns}</button>}
          </div>
          <p className="help">{t.projectRunHistoryHelp}</p>
          {hiddenRunCount > 0 && <p className="help">{lang === "zh" ? `默认只显示当前/最近 ${visibleRuns.length} 条，另有 ${hiddenRunCount} 条历史可展开。` : `Showing current/recent ${visibleRuns.length}; ${hiddenRunCount} older runs hidden.`}</p>}
          {visibleRuns.map((run) => (
            <div className={run.run_id === runId ? "runRow active" : "runRow"} key={run.run_id}>
              <button className="run" onClick={() => loadRun(run.run_id, { userInitiated: true })}>
                <span>{run.run_id}</span>
                <small>{asArray(run.stages).join(" / ")}</small>
              </button>
              <button className="danger smallButton" onClick={() => handleDeleteRun(run.run_id)}>{t.deleteRun}</button>
            </div>
          ))}
        </details>
      </aside>

      <section className="workspace">
        <nav className="tabs">
          {(["find", "read", "ideas", "plan", "environment", "experiment", "paperWrite"] as Tab[]).map((item) => (
            <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
              {t[item]}
            </button>
          ))}
        </nav>

        {error && <div className="error">{error}</div>}
        {stageLaunchLockedText && <div className="researchGateNote warning" data-testid="stage-launch-locked">{stageLaunchLockedText}</div>}

        {tab === "find" && (
          <>
          <section className="stage findConfigStage" data-testid="find-config-stage">
            <div className="toolbar">
              <h2>{t.find}</h2>
              <div className="toolbarActions">
                <button onClick={runVenueHealth} disabled={checkingVenues || researchProjectLoading || !researchProjectsLoaded || !researchSummary}>{checkingVenues ? t.checking : t.checkVenue}</button>
                <button className="primary" onClick={runFind} disabled={stageLaunchDisabledByFullCycle || freshFindRunning || findLaunchPending}>{t.runFind}</button>
              </div>
            </div>
            <div className="findConfigGrid" data-testid="find-config-grid">
              <div className="findConfigTopRow" data-testid="find-config-top-row">
              <div className="panel findConfigPanel findVenueConfigPanel" data-testid="find-venue-config">
                <h3>{t.venues}</h3>
                <p className="help">{t.venueHelp}</p>
                <label>{t.venueSearch}</label>
                <input value={venueQuery} onChange={(e) => { setVenueQuery(e.target.value); setShowAllAvailableVenues(false); }} placeholder={t.venueSearch} />
                <label>{t.years}</label>
                <p className="help">{t.yearsHelp}</p>
                <input value={years} onChange={(e) => setYears(e.target.value)} onBlur={(e) => setYears(normalizeSelectedYears(e.target.value).join(", "))} placeholder="2026" />
                <div className="countLine">{selectedVenues.length} {t.selected} / {visibleAvailableVenues.length} {t.shown} · {selectedYearLabel(addCandidateYears, t.addYears)}</div>
                <div className="venuePicker">
                  <div>
                    <h4>{t.selectedVenuesTitle}</h4>
                    <div className="venueList compactList">
                      {selectedVenueItems.map((venue) => {
                        const health = venueHealth[venue.id];
                        const venueSelectedYears = yearsForVenue(selectedVenueYears, venue.id);
                        return (
                          <div className="venueRow" key={venue.id}>
	                            <div>
	                              <strong>{venue.name}</strong>
	                              <small>{venue.full_name}</small>
	                              <small>{venueMetaLabel(venue, t, venueSelectedYears)}</small>
	                              {health && (
	                                <small className={health.ok ? "health ok" : "health fail"}>
	                                  {health.ok ? t.healthOk : t.healthFail} / {health.source_adapter} / {health.sample_count}
                                </small>
                              )}
                            </div>
                            <button className="smallButton" onClick={() => { setSelectedVenues((prev) => prev.filter((id) => id !== venue.id)); setSelectedVenueYears((prev) => { const next = { ...prev }; delete next[venue.id]; return next; }); }}>{t.remove}</button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                  <div>
                    <div className="panelHeaderLine compactHeader">
                      <h4>{t.availableVenuesTitle}</h4>
                      {hiddenAvailableVenueCount > 0 && <button className="smallButton" onClick={() => setShowAllAvailableVenues(true)}>{lang === "zh" ? `显示更多 ${hiddenAvailableVenueCount}` : `Show ${hiddenAvailableVenueCount} more`}</button>}
                    </div>
                    {!venueQuery.trim() && hiddenAvailableVenueCount > 0 && <p className="help">{lang === "zh" ? "默认只显示前 24 个出版渠道；用搜索框定位渠道，或展开更多。" : "Showing the first 24 venues by default; search to narrow the list or expand more."}</p>}
                    <div className="venueList">
                      {visibleAvailableVenues.map((venue) => {
                        const health = venueHealth[venue.id];
                        return (
                          <div className="venueRow" key={venue.id}>
	                            <div>
	                              <strong>{venue.name}</strong>
	                              <small>{venue.full_name}</small>
	                              <small>{venueMetaLabel(venue, t)}</small>
	                              {health && (
	                                <small className={health.ok ? "health ok" : "health fail"}>
	                                  {health.ok ? t.healthOk : t.healthFail} / {health.source_adapter} / {health.sample_count}
                                </small>
                              )}
                            </div>
                            <button className="smallButton" onClick={() => { const targetVenueId = selectedVenueIdForVenue(selectedVenues, venue, venueById) || venue.id; setSelectedVenues((prev) => selectedVenueIdForVenue(prev, venue, venueById) ? prev : [...prev, venue.id]); setSelectedVenueYears((prev) => addYearsForVenue(prev, targetVenueId, addCandidateYears)); }}>{t.add}</button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>
              <div className="panel findConfigPanel findSourceConfigPanel" data-testid="find-source-config">
                <h3>{t.sources}</h3>
                <p className="help">{t.sourcesHelp}</p>
                <label>{t.arxivCategories}</label>
                <p className="help">{t.arxivHelp}</p>
                <input value={config.arxiv_categories.join(", ")} onChange={(e) => updateConfig("arxiv_categories", splitList(e.target.value))} placeholder="cs.AI, cs.CV" />
                <label>{t.arxivTopicQueries}</label>
                <p className="help">{t.arxivTopicQueriesHelp}</p>
                <input value={(config.arxiv_queries || []).join(", ")} onChange={(e) => updateConfig("arxiv_queries", splitPhraseList(e.target.value))} placeholder={t.arxivTopicQueriesPlaceholder} />
                <p className="help">{t.arxivDateHelp}</p>
                <div className="row">
                  <input value={config.arxiv_start_date} onChange={(e) => updateConfig("arxiv_start_date", e.target.value)} placeholder={t.startDate} />
                  <input value={config.arxiv_end_date} onChange={(e) => updateConfig("arxiv_end_date", e.target.value)} placeholder={t.endDate} />
                </div>
                <label>{t.biorxivCategories}</label>
                <p className="help">{t.biorxivHelp}</p>
                <input value={(config.biorxiv_categories || []).join(", ")} onChange={(e) => updateConfig("biorxiv_categories", splitCategoryList(e.target.value))} placeholder="bioinformatics, molecular biology" />
                <p className="help">{t.biorxivDateHelp}</p>
                <div className="row">
                  <input value={config.biorxiv_start_date || ""} onChange={(e) => updateConfig("biorxiv_start_date", e.target.value)} placeholder={t.startDate} />
                  <input value={config.biorxiv_end_date || ""} onChange={(e) => updateConfig("biorxiv_end_date", e.target.value)} placeholder={t.endDate} />
                </div>
                <details className="subPanel collapsiblePanel">
                  <summary>
                    <span>{t.naturePortfolio}</span>
                    <small>{includeNature ? "on" : "off"} / {(config.nature_journals || []).length} {t.selected}</small>
                  </summary>
                  <label className="switch"><input type="checkbox" checked={includeNature} onChange={(e) => setIncludeNature(e.target.checked)} /> {t.naturePortfolio}</label>
                  <p className="help">{t.natureHelp}</p>
                  <label>{t.naturePresets}</label>
                  <div className="presetGrid">
                    {NATURE_PRESETS.map((preset) => {
                      const state = naturePresetState(preset.journals);
                      const title = preset.journals.map((slug) => NATURE_JOURNAL_NAMES[slug] || slug).join("\\n");
                      return (
                        <label className={state.partial ? "presetItem partial" : "presetItem"} key={preset.id} title={title}>
                          <input type="checkbox" checked={state.checked} onChange={(e) => toggleNaturePreset(preset.journals, e.target.checked)} />
                          <span>{preset.name}</span>
                          <small>{state.count}/{preset.journals.length}</small>
                        </label>
                      );
                    })}
                  </div>
                  <label>{t.natureJournals}</label>
                  <div className="checkGrid">
                    {NATURE_JOURNALS.map((journal) => (
                      <label className="checkItem" key={journal.slug}>
                        <input type="checkbox" checked={(config.nature_journals || []).includes(journal.slug)} onChange={(e) => toggleNatureJournal(journal.slug, e.target.checked)} />
                        <span>{journal.name}</span>
                        <small>{journal.tier}</small>
                      </label>
                    ))}
                  </div>
                  <label className="labelWithHelp"><span>{t.natureArticleTypes}</span><span className="helpDot" tabIndex={0} data-tooltip={t.natureArticleTypesTooltip}>?</span></label>
                  <input value={(config.nature_article_types || ["article"]).join(", ")} onChange={(e) => updateConfig("nature_article_types", splitList(e.target.value))} placeholder="article" />
                  <p className="help">{t.natureDateHelp}</p>
                  <div className="row">
                    <input value={config.nature_start_date || ""} onChange={(e) => updateConfig("nature_start_date", e.target.value)} placeholder={t.startDate} />
                    <input value={config.nature_end_date || ""} onChange={(e) => updateConfig("nature_end_date", e.target.value)} placeholder={t.endDate} />
                  </div>
                  <label className="labelWithHelp"><span>{t.natureCandidateLimit}</span><span className="helpDot" tabIndex={0} data-tooltip={t.natureCandidateLimitTooltip}>?</span></label>
                  <input type="number" min={1} max={1000} value={config.nature_candidate_limit || 200} onChange={(e) => updateConfig("nature_candidate_limit", Number(e.target.value))} />
                </details>
                <details className="subPanel collapsiblePanel">
                  <summary>
                    <span>{t.scienceFamily}</span>
                    <small>{includeScience ? "on" : "off"} / {(config.science_journals || []).length} {t.selected}</small>
                  </summary>
                  <label className="switch"><input type="checkbox" checked={includeScience} onChange={(e) => setIncludeScience(e.target.checked)} /> {t.scienceFamily}</label>
                  <p className="help">{t.scienceHelp}</p>
                  <label>{t.sciencePresets}</label>
                  <div className="presetGrid">
                    {SCIENCE_PRESETS.map((preset) => {
                      const state = sciencePresetState(preset.journals);
                      const title = preset.journals.map((slug) => SCIENCE_JOURNAL_NAMES[slug] || slug).join("\\n");
                      return (
                        <label className={state.partial ? "presetItem partial" : "presetItem"} key={preset.id} title={title}>
                          <input type="checkbox" checked={state.checked} onChange={(e) => toggleSciencePreset(preset.journals, e.target.checked)} />
                          <span>{preset.name}</span>
                          <small>{state.count}/{preset.journals.length}</small>
                        </label>
                      );
                    })}
                  </div>
                  <label>{t.scienceJournals}</label>
                  <div className="checkGrid">
                    {SCIENCE_JOURNALS.map((journal) => (
                      <label className="checkItem" key={journal.slug}>
                        <input type="checkbox" checked={(config.science_journals || []).includes(journal.slug)} onChange={(e) => toggleScienceJournal(journal.slug, e.target.checked)} />
                        <span>{journal.name}</span>
                        <small>{journal.tier}</small>
                      </label>
                    ))}
                  </div>
                  <details className="nestedDetails">
                    <summary>{t.sciencePartnerJournals}</summary>
                    <p className="help">{t.sciencePartnerHelp}</p>
                    <div className="checkGrid">
                      {SCIENCE_PARTNER_JOURNALS.map((journal) => (
                        <label className={journal.disabled ? "checkItem disabled" : "checkItem"} key={journal.slug}>
                          <input type="checkbox" disabled={Boolean(journal.disabled)} checked={(config.science_journals || []).includes(journal.slug)} onChange={(e) => toggleScienceJournal(journal.slug, e.target.checked)} />
                          <span>{journal.name}</span>
                          <small>{journal.tier}</small>
                        </label>
                      ))}
                    </div>
                  </details>
                  <label className="labelWithHelp"><span>{t.scienceArticleTypes}</span><span className="helpDot" tabIndex={0} data-tooltip={t.scienceArticleTypesTooltip}>?</span></label>
                  <input value={(config.science_article_types || ["Research Article"]).join(", ")} onChange={(e) => updateConfig("science_article_types", splitList(e.target.value))} placeholder="Research Article" />
                  <p className="help">{t.scienceDateHelp}</p>
                  <div className="row">
                    <input value={config.science_start_date || ""} onChange={(e) => updateConfig("science_start_date", e.target.value)} placeholder={t.startDate} />
                    <input value={config.science_end_date || ""} onChange={(e) => updateConfig("science_end_date", e.target.value)} placeholder={t.endDate} />
                  </div>
                  <label className="labelWithHelp"><span>{t.scienceCandidateLimit}</span><span className="helpDot" tabIndex={0} data-tooltip={t.scienceCandidateLimitTooltip}>?</span></label>
                  <input type="number" min={1} max={1000} value={config.science_candidate_limit || 200} onChange={(e) => updateConfig("science_candidate_limit", Number(e.target.value))} />
                </details>
                <label>{t.githubLanguages}</label>
                <p className="help">{t.githubLanguagesHelp}</p>
                <input value={config.github_languages.join(", ")} onChange={(e) => updateConfig("github_languages", splitList(e.target.value))} placeholder="all, python" />
                <label className="switch"><input type="checkbox" checked={includeArxiv} onChange={(e) => setIncludeArxiv(e.target.checked)} /> arXiv</label>
                <label className="switch"><input type="checkbox" checked={includeBiorxiv} onChange={(e) => setIncludeBiorxiv(e.target.checked)} /> bioRxiv</label>
                <label className="switch"><input type="checkbox" checked={includeNature} onChange={(e) => setIncludeNature(e.target.checked)} /> Nature</label>
                <label className="switch"><input type="checkbox" checked={includeScience} onChange={(e) => setIncludeScience(e.target.checked)} /> Science</label>
                <label className="switch"><input type="checkbox" checked={includeHf} onChange={(e) => setIncludeHf(e.target.checked)} /> HuggingFace</label>
                <label className="switch"><input type="checkbox" checked={includeGithub} onChange={(e) => setIncludeGithub(e.target.checked)} /> GitHub</label>
              </div>
              </div>
            </div>
            <div className="panel findBudgetConfigPanel" data-testid="find-budget-config">
              <h3>{t.findRunBudget}</h3>
              <p className="help">{t.findBudgetHelp}</p>
              <div className="row"><div><label>{t.recommendLimit}</label><p className="help">{t.recommendLimitHelp}</p><input value={config.max_recommended_papers} onChange={(e) => updateConfig("max_recommended_papers", Number(e.target.value))} type="number" min="1" /></div><div><label>{t.llmConcurrency}</label><p className="help">{t.llmConcurrencyHelp}</p><input value={config.llm_concurrency} onChange={(e) => updateConfig("llm_concurrency", Math.max(1, Math.min(32, Number(e.target.value))))} type="number" min="1" max="32" /></div></div>
              <details className="subPanel collapsiblePanel">
                <summary><span>{t.advancedFindSettings}</span><small>{t.standardFindProfile}</small></summary>
                <div className="row"><div><label>{t.nonvenueFetchLimit}</label><p className="help">{t.nonvenueFetchLimitHelp}</p><input value={config.nonvenue_fetch_limit} onChange={(e) => updateConfig("nonvenue_fetch_limit", Math.max(1, Number(e.target.value)))} type="number" min="1" /></div><div><label>{t.arxivMaxQueries}</label><p className="help">{t.arxivMaxQueriesHelp}</p><input value={config.arxiv_max_queries} onChange={(e) => updateConfig("arxiv_max_queries", Math.max(1, Number(e.target.value)))} type="number" min="1" /></div></div>
                <div className="row"><div><label>{t.titleAbstractScoringLimit}</label><p className="help">{t.titleAbstractScoringLimitHelp}</p><input value={config.title_abstract_scoring_limit} onChange={(e) => updateConfig("title_abstract_scoring_limit", Math.max(1, Number(e.target.value)))} type="number" min="1" /></div><div><label>{t.titleScanLimit}</label><p className="help">{t.titleScanLimitHelp}</p><input value={config.venue_title_scan_limit} onChange={(e) => updateConfig("venue_title_scan_limit", Math.max(0, Number(e.target.value)))} type="number" min="0" /></div></div>
              </details>
              <div className="saveBar"><button onClick={applyStandardFindDefaults} disabled={savingConfig}>{t.restoreStandardFindDefaults}</button><button className="primary" onClick={handleSaveConfig} disabled={savingConfig}>{savingConfig ? t.saving : t.saveConfig}</button>{saveMessage && <span>{saveMessage}</span>}</div>
            </div>
            <div className="findLayoutSentinel" data-testid="find-source-configs-complete" aria-hidden="true" />
            <div className="findSurveySlot findSurveyAfterConfig" data-testid="find-survey-slot">{renderFindLiteratureSurveyPanel()}</div>
          </section>
          </>
        )}

        {tab === "read" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.read}</h2>
              <button className="primary" data-testid="run-read-button" onClick={runRead} disabled={!(currentProjectFindRunId || runId) || researchProjectConfigSaving || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{t.runRead}</button>
            </div>
            <div className="panel readSettingsPanel">
              <h3>{lang === "zh" ? "精读设置" : "Read settings"}</h3>
              <p className="help">{lang === "zh"
                ? "默认 50 篇；排名论文不足时全部精读，超过时按 Find 最终排名取前 N 篇。Find 完成后，项目默认值会自动更新为推荐数量的 2 倍，你仍可单独修改。"
                : "The module default is 50. Read all ranked papers when fewer are available, otherwise read the top N from Find's final ranking. After Find completes, the project default updates to 2× the recommendation count and remains editable."}</p>
              <div className="row">
                <div>
                  <label htmlFor="read-paper-limit">{lang === "zh" ? "精读篇数" : "Papers to read"}</label>
                  <input
                    id="read-paper-limit"
                    data-testid="read-paper-limit"
                    value={readPaperLimit}
                    onChange={(event) => {
                      setReadPaperLimit(Math.max(1, Math.trunc(Number(event.target.value) || 1)));
                      setReadPaperLimitDirty(true);
                      setReadPaperLimitMessage("");
                    }}
                    type="number"
                    min="1"
                  />
                </div>
                <div>
                  <label>{lang === "zh" ? "本次精读范围" : "This read scope"}</label>
                  <p className="readScopeSummary">{lang === "zh" ? `Find 最终排名前 ${readPaperLimit} 篇` : `Top ${readPaperLimit} in Find's final ranking`}</p>
                  <p className="help">{lang === "zh" ? "不再仅精读 Find 推荐文章。" : "Read is no longer limited to Find recommendations."}</p>
                </div>
              </div>
              <div className="saveBar">
                <button className="primary" onClick={saveReadPaperLimit} disabled={!researchProject || researchProjectConfigSaving || !readPaperLimitDirty}>{researchProjectConfigSaving ? t.saving : (lang === "zh" ? "保存到当前项目" : "Save for this project")}</button>
                {readPaperLimitMessage && <span>{readPaperLimitMessage}</span>}
                {readPaperLimitDirty && <span className="readLimitDirty">{lang === "zh" ? "未保存；运行精读时会自动保存。" : "Unsaved; running Read will save it automatically."}</span>}
              </div>
            </div>
            <div className="panel readStatusPanel">
              <h3>{lang === "zh" ? "精读状态" : "Reading status"}</h3>
              <div className="surveyFlowGrid compactSurveyFlow">
                <div><strong>{publicReadSummaryLoaded ? displayMaybe(publicReadCounts.recommended || expectedReadCandidateCount || 0) : publicReadLoadingText}</strong><span>{lang === "zh" ? "推荐论文" : "Recommended"}</span></div>
                <div><strong>{publicReadSummaryLoaded ? displayMaybe(publicReadCounts.displayed || currentReadings.length || 0) : publicReadLoadingText}</strong><span>{lang === "zh" ? "当前展示" : "Displayed"}</span></div>
                <div><strong>{publicReadSummaryLoaded ? displayMaybe(publicReadCounts.fullText || 0) : publicReadLoadingText}</strong><span>{lang === "zh" ? "全文精读完成" : "Full-text complete"}</span></div>
                <div><strong>{publicReadSummaryLoaded ? displayMaybe(publicReadCounts.pending) : publicReadLoadingText}</strong><span>{lang === "zh" ? "待补" : "Pending"}</span></div>
              </div>
              <p className="help">{humanReadableMaybe(publicReadStage?.summary_zh || publicReadStage?.summary || currentFindPipeline?.summary_zh || currentFindPipeline?.summary, lang === "zh" ? "当前 Find 精读状态等待 刷新。" : "The current Find reading status is waiting for workflow refresh.")}</p>
            </div>
          </section>
        )}

        {tab === "ideas" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.ideas}</h2>
              <div className="actions">
                {ideaMarkdownText && (
                  <button disabled={viewingSelectedHistoricalFindRun} onClick={() => {
                    setIdeaMarkdownDraft(ideaMarkdownText);
                    setIdeaMarkdownEditing((editing) => !editing);
                  }}>
                    {ideaMarkdownEditing ? (lang === "zh" ? "返回字段编辑" : "Back to fields") : (lang === "zh" ? "编辑 Markdown 源文" : "Edit Markdown source")}
                  </button>
                )}
                <button className="primary" onClick={runIdeas} disabled={!(currentProjectFindRunId || runId) || !researchProject || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{t.runIdeas}</button>
              </div>
            </div>
            <div className="panel ideaRunBudgetPanel">
              <h3>{t.ideaRunBudget}</h3>
              <p className="help">{t.ideaBudgetHelp}</p>
              <div><label>{t.ideaLimit}</label><p className="help">{t.ideaLimitHelp}</p><input value={config.max_ideas} onChange={(e) => updateConfig("max_ideas", Number(e.target.value))} type="number" min="1" max="50" /></div>
              <div className="saveBar"><button className="primary" onClick={handleSaveConfig} disabled={savingConfig}>{savingConfig ? t.saving : t.saveConfig}</button>{saveMessage && <span>{saveMessage}</span>}</div>
            </div>
            {currentFindArtifactLoading || ideasStillSyncing ? (
              <div className="emptyState">{lang === "zh" ? "正在加载当前 Find 的想法产物..." : "Loading idea artifacts for the current Find run..."}</div>
            ) : ideas.length === 0 ? (
              <div className="emptyState">{lang === "zh" ? "当前 Find 尚未产出想法。" : "No ideas have been produced for the current Find run yet."}</div>
            ) : ideaMarkdownEditing ? (
              <div className="panel ideaMarkdownEditorPanel">
                <textarea
                  className="ideaMarkdownSourceEditor"
                  value={ideaMarkdownDraft}
                  onChange={(event) => setIdeaMarkdownDraft(event.target.value)}
                  readOnly={viewingSelectedHistoricalFindRun}
                  spellCheck={false}
                  aria-label="idea.md"
                />
                <div className="actions">
                  <button onClick={() => { setIdeaMarkdownDraft(ideaMarkdownText); setIdeaMarkdownEditing(false); }} disabled={ideaMarkdownSaving || viewingSelectedHistoricalFindRun}>{lang === "zh" ? "取消" : "Cancel"}</button>
                  <button className="primary" onClick={saveIdeaMarkdown} disabled={ideaMarkdownSaving || !ideaMarkdownDraft.trim() || viewingSelectedHistoricalFindRun}>{ideaMarkdownSaving ? t.saving : (lang === "zh" ? "保存 idea.md" : "Save idea.md")}</button>
                </div>
              </div>
            ) : (
              <div className="ideaGrid">
                {ideas.map((idea: any, index: number) => {
                  const ideaId = String(idea.id || idea.title || index);
                  const status = ideaWorkflowStatus(idea);
                  const savingStatus = ideaStatusSaving[ideaId];
                  const savingEdit = Boolean(ideaEditorSaving[ideaId]);
                  const draft = ideaEditorDrafts[ideaId] || {
                    title: String(idea.title || ""),
                    new_method: String(idea.new_method || ""),
                    initial_experiment: String(idea.initial_experiment || ""),
                  };
                  const dirty = draft.title !== String(idea.title || "")
                    || draft.new_method !== String(idea.new_method || "")
                    || draft.initial_experiment !== String(idea.initial_experiment || "");
                  const invalid = !draft.title.trim() || !draft.new_method.trim() || !draft.initial_experiment.trim();
                  return (
                    <article className={`idea ideaEditorCard ${status}`} key={ideaId}>
                      <input
                        className="ideaTitle"
                        value={draft.title}
                        disabled={viewingSelectedHistoricalFindRun}
                        onChange={(event) => updateIdeaEditorDraft(ideaId, idea, "title", event.target.value)}
                        aria-label={lang === "zh" ? "idea 标题" : "idea title"}
                      />
                      <div className="ideaMetaLine">
                        {ideaScoreText(idea) && <span>{lang === "zh" ? `评分 ${ideaScoreText(idea)}/10` : `score ${ideaScoreText(idea)}/10`}</span>}
                        <span className={`ideaStatusBadge ${status}`}>{ideaWorkflowStatusLabel(status)}</span>
                      </div>
                      <label className="ideaFieldLabel">{lang === "zh" ? "新方法" : "New method"}</label>
                      <textarea
                        className="ideaLargeTextarea"
                        value={draft.new_method}
                        disabled={viewingSelectedHistoricalFindRun}
                        onChange={(event) => updateIdeaEditorDraft(ideaId, idea, "new_method", event.target.value)}
                        aria-label={lang === "zh" ? "新方法" : "new method"}
                      />
                      <label className="ideaFieldLabel">{lang === "zh" ? "初步实验" : "Initial experiment"}</label>
                      <textarea
                        className="ideaLargeTextarea"
                        value={draft.initial_experiment}
                        disabled={viewingSelectedHistoricalFindRun}
                        onChange={(event) => updateIdeaEditorDraft(ideaId, idea, "initial_experiment", event.target.value)}
                        aria-label={lang === "zh" ? "初步实验" : "initial experiment"}
                      />
                      <div className="actions ideaStatusActions" aria-label={lang === "zh" ? "想法操作" : "idea actions"}>
                        <button className="primary" onClick={() => saveIdeaFields(ideaId)} disabled={!dirty || invalid || savingEdit || Boolean(savingStatus) || viewingSelectedHistoricalFindRun}>{savingEdit ? t.saving : (lang === "zh" ? "保存修改" : "Save changes")}</button>
                        <button className={status === "approved" ? "active" : ""} onClick={() => setIdeaStatus(ideaId, "approved")} disabled={dirty || savingEdit || Boolean(savingStatus) || viewingSelectedHistoricalFindRun}>{savingStatus === "approved" ? t.saving : t.approve}</button>
                        <button className={status === "pending" ? "active" : ""} onClick={() => setIdeaStatus(ideaId, "pending")} disabled={dirty || savingEdit || Boolean(savingStatus) || viewingSelectedHistoricalFindRun}>{savingStatus === "pending" ? t.saving : t.pending}</button>
                        <button className={status === "deleted" ? "danger active" : "danger"} onClick={() => setIdeaStatus(ideaId, "deleted")} disabled={dirty || savingEdit || Boolean(savingStatus) || viewingSelectedHistoricalFindRun}>{savingStatus === "deleted" ? t.saving : t.delete}</button>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        )}

        {tab === "plan" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.plan}</h2>
              <button className="primary" onClick={runPlan} disabled={!runId || !planIdeaIds.length || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{t.runPlan}</button>
            </div>
            <div className="planControlGrid planTopGrid">
              <div className="panel planControlPanel planIdeasPanel">
                <div className="planPanelHeader">
                  <h3>{lang === "zh" ? "计划生成输入" : "Plan generation input"}</h3>
                  <span>{planIdeaIds.length} / {approvedIdeas.length}</span>
                </div>
                {currentFindArtifactLoading || ideasStillSyncing ? (
                  <div className="emptyState">{lang === "zh" ? "正在加载当前 Find 的想法和计划产物..." : "Loading ideas and plans for the current Find run..."}</div>
                ) : approvedIdeas.length === 0 ? (
                  <div className="emptyState">{t.noApprovedIdeas}</div>
                ) : (
                  <>
                    <div className="actions compactActions">
                      <button disabled={viewingSelectedHistoricalFindRun} onClick={() => setPlanIdeaIds(approvedIdeas.map((idea: any, index: number) => ideaKey(idea, index)).filter(Boolean))}>{t.selectAll}</button>
                      <button disabled={viewingSelectedHistoricalFindRun} onClick={() => setPlanIdeaIds([])}>{t.clearAll}</button>
                    </div>
                    <div className="planPickList">
                      {approvedIdeas.map((idea: any, index: number) => {
                        const key = ideaKey(idea, index);
                        return (
                          <label className="check paper compactPlanPick" key={key}>
                            <input
                              type="checkbox"
                              disabled={viewingSelectedHistoricalFindRun}
                              checked={planIdeaIds.includes(key)}
                              onChange={(event) => setPlanIdeaIds((previous) => event.target.checked ? Array.from(new Set([...previous, key])) : previous.filter((ideaId) => ideaId !== key))}
                            />
                            <span>{ideaTitleText(idea, index)}</span>
                            <small>{[ideaScoreText(idea) ? (lang === "zh" ? "评分 " + ideaScoreText(idea) + "/10" : "score " + ideaScoreText(idea) + "/10") : "", ideaStatusText(idea)].filter(Boolean).join(" / ")}</small>
                          </label>
                        );
                      })}
                    </div>
                  </>
                )}
              </div>
              <div className="panel planControlPanel planSettingsPanel">
                <h3>{lang === "zh" ? "生成设置" : "Generation settings"}</h3>
                <label>{t.repairRounds}</label>
                <p className="help">{t.repairRoundsHelp}</p>
                <input value={planRepairRounds} onChange={(event) => setPlanRepairRounds(Math.max(0, Math.trunc(Number(event.target.value) || 0)))} type="number" min="0" disabled={viewingSelectedHistoricalFindRun} />
                <div className="actions compactActions">
                  <button className="primary" onClick={runPlan} disabled={!runId || !planIdeaIds.length || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{t.runPlan}</button>
                </div>
              </div>
              <div className="panel planControlPanel planActionsPanel" data-testid="plan-human-editor">
                <h3>{lang === "zh" ? "计划操作" : "Plan actions"}</h3>
                {currentFindArtifactLoading || plansStillSyncing ? (
                  <div className="emptyState">{lang === "zh" ? "正在加载当前 Find 的计划产物..." : "Loading plans for the current Find run..."}</div>
                ) : plans.length === 0 ? (
                  <div className="emptyState">{lang === "zh" ? "当前 Find 尚未产出计划。" : "No plans have been produced for the current Find run yet."}</div>
                ) : (
                  <>
                    <div className={contractSelectedPlanId ? "planExecutionContract ready" : "planExecutionContract blocked"}>
                      <strong>{lang === "zh" ? "执行合同" : "Execution contract"}</strong>
                      <span>{selectedExecutionText}</span>
                      {selectedExecutionStatus && <small>{displayValue(selectedExecutionStatus)}</small>}
                    </div>
                    <label>{lang === "zh" ? "候选计划操作对象" : "Candidate plan for editing"}</label>
                    <select className="planSelect" value={selectedPlanId || contractSelectedPlanId} onChange={(event) => setSelectedPlanId(event.target.value)} disabled={viewingSelectedHistoricalFindRun}>
                      <option value="">{lang === "zh" ? "请选择候选计划" : "Select a candidate plan"}</option>
                      {plans.map((plan: any, index: number) => (
                        <option value={String(plan.plan_id || "")} key={plan.plan_id || plan.idea_id || index}>{planTitleText(plan, index)}</option>
                      ))}
                    </select>
                    <small className="planControlMeta">{selectedPlanForControls ? [planMetaText(selectedPlanForControls, asArray(selectedPlanForControls.versions), [], []), planIdeaLabel(selectedPlanForControls)].filter(Boolean).join(" / ") : ""}</small>
                    {selectedExecutionMissing && (
                      <div className="actions compactActions selectionActions">
                        <button className="primary" onClick={() => runAR("current-find-selection")} disabled={!researchProject || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{t.selectExecutionPlan}</button>
                      </div>
                    )}
                    <label>{lang === "zh" ? "计划正文" : "Plan body"}</label>
                    <textarea
                      className="planTextEditor"
                      value={planMarkdownDraft}
                      onChange={(event) => {
                        setPlanMarkdownDraft(event.target.value);
                        setPlanMarkdownDirty(true);
                      }}
                      aria-label={lang === "zh" ? "计划正文" : "Plan body"}
                      readOnly={viewingSelectedHistoricalFindRun}
                      spellCheck={false}
                    />
                    <div className="actions compactActions planEditorActions">
                      <button onClick={() => { setPlanMarkdownDraft(planMarkdownText); setPlanMarkdownDirty(false); }} disabled={planMarkdownSaving || !planMarkdownDirty || viewingSelectedHistoricalFindRun}>{lang === "zh" ? "重置" : "Reset"}</button>
                      <button className="primary" onClick={savePlanMarkdown} disabled={planMarkdownSaving || !planMarkdownDirty || !planMarkdownDraft.trim() || viewingSelectedHistoricalFindRun}>{planMarkdownSaving ? t.saving : (lang === "zh" ? "保存修改" : "Save changes")}</button>
                    </div>
                    <div className="compactPlanControls">
                      <label>{t.polishRounds}</label>
                      <input
                        value={polishRounds[selectedPlanForControls?.plan_id] || 1}
                        onChange={(event) => selectedPlanForControls && setPolishRounds((previous) => ({ ...previous, [selectedPlanForControls.plan_id]: Math.max(1, Number(event.target.value)) }))}
                        type="number"
                        min="1"
                        disabled={viewingSelectedHistoricalFindRun}
                      />
                    </div>
                    <div className="actions compactActions">
                      <button onClick={() => selectedPlanForControls && runPlanPolish(selectedPlanForControls.plan_id, selectedPlanLatest.version_id)} disabled={!selectedPlanForControls?.plan_id || !selectedPlanLatest?.version_id || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{t.polishFurther}</button>
                      <button className={selectedPlanForControls?.completed ? "" : "primary"} onClick={() => selectedPlanForControls && runPlanFinish(selectedPlanForControls.plan_id)} disabled={!selectedPlanForControls?.plan_id || selectedPlanForControls?.completed || stageLaunchDisabledByFullCycle || viewingSelectedHistoricalFindRun}>{selectedPlanForControls?.completed ? t.planCompleted : t.finishPlan}</button>
                    </div>
                  </>
                )}
              </div>
            </div>
          </section>
        )}

        {tab === "environment" && (
          <section className="stage researchStage">
            <div className="toolbar">
              <div>
                <h2>{t.environment}</h2>
                <p className="help">{t.environmentHelp}</p>
              </div>
              <div className="toolbarActions">
                <button onClick={() => refreshProject()} disabled={!researchProject}>{t.researchRefresh}</button>
                <button onClick={() => runAR("healthcheck")} disabled={!researchProject}>{t.researchHealth}</button>
                <button className="primary" onClick={() => runAR("environment")} disabled={environmentLaunchDisabled}>{environmentLocked ? t.envLockedCreated : (projectStatusLoadingForLaunch || environmentStageRunning || referenceFullJobRunning || stageLaunchDisabledByProjectWorker) ? t.researchRunningTask : t.firstCreateEnv}</button>
              </div>
            </div>
            {!researchProjectsLoaded && !researchProject ? <div className="emptyState">{t.researchProjectLoading}</div> : researchProjectsLoaded && !researchProject ? <div className="emptyState">{t.researchNoProject}</div> : (
              <>
                {(!researchProjectsLoaded || projectSummaryLoadingForDisplay) && <div className="researchGateNote warning">{lang === "zh" ? "正在刷新当前项目状态；环境配置面板会先保留显示。" : "Refreshing current project state; the Environment panels stay visible."}</div>}
                <div className="grid two environmentGrid">
                  <div className="panel researchStageCard readableOnly envSummaryPanel">
                    <div className="envSummaryHeader">
                      <div>
                        <h3>{t.currentEnvSummary}</h3>
                        <p>{localizedField(envStage, "module_summary", localizedField(envStage, "summary", t.notRunEnvironment))}</p>
                      </div>
                      <span className={`stageBadge ${badgeClass(envStage?.status)}`}>{displayValue(envStage?.status || "not_started")}</span>
                    </div>
                    <div className="envSummaryList compactEnvSummaryList">
                      <div className="envSummaryItem">
                        <span>{lang === "zh" ? "当前基底" : "Current base"}</span>
                        <strong>{environmentSelectionValid ? displayMaybe(envStage?.selection?.selected_base?.title || activeRepo?.name, t.notSelected) : (lang === "zh" ? "未选择" : "not selected")}</strong>
                      </div>
                      {(pendingEnvironmentCandidate?.name || pendingEnvironmentCandidate?.title || pendingEnvironmentCandidate?.repo_path) && (
                        <div className="envSummaryItem">
                          <span>{lang === "zh" ? "候选" : "Candidate"}</span>
                          <strong>{displayMaybe(pendingEnvironmentCandidate?.name || pendingEnvironmentCandidate?.title || pendingEnvironmentCandidate?.repo_path, t.notSelected)}</strong>
                        </div>
                      )}
                      <div className="envSummaryItem">
                        <span>{lang === "zh" ? "门控" : "Gate"}</span>
                        <strong>{displayValue(envStage?.selection?.selection_gate || envStage?.selection?.raw_selection_gate || envStage?.status, t.noData)}</strong>
                      </div>
                      <div className="envSummaryItem">
                        <span>{lang === "zh" ? "参考复现" : "Reference"}</span>
                        <strong>{displayValue(envReferenceFullJob?.status === "running" ? envReferenceFullJob.status : (envReferenceGate?.status || "not_started"))}</strong>
                      </div>
                    </div>
                    <div className={`envSummaryStatus ${String(envStage?.status || "").includes("blocked") ? "warning" : ""}`}>
                      <span>{lang === "zh" ? "环境状态" : "Environment status"}</span>
                      <strong>{displayValue(envStage?.status || "not_started")}</strong>
                    </div>
                  </div>
                  <div className="panel">
                    <h3>{t.experimentCondaPythonConfig}</h3>
                    <p className="help">{t.experimentCondaPythonHelp}</p>
                    {environmentConfigLoading ? (
                      <div className="researchGateNote warning">{lang === "zh" ? "正在刷新当前项目环境配置；加载完成前不会显示空 Conda 配置。" : "Refreshing the current project environment configuration; empty Conda values are hidden until loading completes."}</div>
                    ) : (
                      <>
                        <label>{t.condaEnvName}</label>
                        <input value={effectiveResearchEnvDraft.conda_env || ""} onChange={(e) => updateEnvDraft("conda_env", e.target.value)} placeholder={researchProject || "project_env"} disabled={environmentConfigDisabled} />
                        <label>{t.condaBase}</label>
                        <input value={effectiveResearchEnvDraft.conda_base || ""} onChange={(e) => updateEnvDraft("conda_base", e.target.value)} placeholder="~/miniforge3" disabled={environmentConfigDisabled} />
                        <label>{t.experimentPythonExecutable}</label>
                        <input value={effectiveResearchEnvDraft.experiment_python || ""} onChange={(e) => updateEnvDraft("experiment_python", e.target.value)} placeholder={derivedCondaPython(effectiveResearchEnvDraft.conda_base, effectiveResearchEnvDraft.conda_env) || "python"} disabled={environmentConfigDisabled} />
                        <p className="help">{lang === "zh" ? `当前训练/实验 Python: ${effectiveResearchEnvDraft.experiment_python || derivedCondaPython(effectiveResearchEnvDraft.conda_base, effectiveResearchEnvDraft.conda_env) || "由 Conda 环境名称派生"}` : `Current training/experiment Python: ${effectiveResearchEnvDraft.experiment_python || derivedCondaPython(effectiveResearchEnvDraft.conda_base, effectiveResearchEnvDraft.conda_env) || "derived from the Conda environment name"}`}</p>
                        <div className="saveBar">
                          <button onClick={saveEnvConfig} disabled={!researchProject || researchEnvSaving || environmentConfigDisabled}>{researchEnvSaving ? t.saving : environmentLocked ? t.envLockedCreated : t.saveExperimentEnv}</button>
                          {researchEnvMessage && <span>{researchEnvMessage}</span>}
                        </div>
                        <div className="runtimeChecks envRuntimeChecks">
                          {["conda", "experiment_python", "conda_base"].map((name) => {
                            const check = runtimeChecks?.[name] || {};
                            const lockedReady = environmentLocked && Object.keys(check).length === 0;
                            const ok = Boolean(check.ok || lockedReady);
                            return (
                              <div className={ok ? "runtimeCheck ok" : "runtimeCheck fail"} key={"env-" + name}>
                                <strong>{name}</strong>
                                <span>{ok ? (lockedReady ? t.runtimeLockedReady : "ok") : t.missing}</span>
                                <small>{check.path || check.reason || (lockedReady ? t.runtimeLockedReadyDetail : t.noDiagnostics)}</small>
                                {check.version && <small>{check.version}</small>}
                              </div>
                            );
                          })}
                        </div>
                      </>
                    )}
                    <h3>{t.firstEnvCreateControl}</h3>
                    <p className="help">{environmentLocked ? (lang === "zh" ? "仓库、数据与 Conda handoff 已完成；环境配置页保留当前状态和刷新入口。" : "The repository, data, and Conda handoff is complete; the Environment page keeps its current status and refresh controls.") : t.firstEnvCreateHelp}</p>
                    {!environmentLocked && <>
                      <label>{t.researchPrompt}</label>
                      <textarea
                        value={researchPrompt}
                        onChange={(e) => setPrompt(e.target.value)}
                        placeholder={t.researchPromptPlaceholder}
                        autoComplete="off"
                        autoCorrect="off"
                        autoCapitalize="off"
                        spellCheck={false}
                      />
                      <label>{t.researchTopic}</label>
                      <input value={researchTopic} onChange={(e) => setTopic(e.target.value)} />
                      <label className="switch"><input type="checkbox" checked={researchRealBootstrapEnv} onChange={(e) => setRealBootstrapEnv(e.target.checked)} /> {t.realBootstrapConda}</label>
                    </>}
                    <div className="actions">{!environmentLocked && <button onClick={() => runAR("init")} disabled={environmentAgentActionDisabled}>{t.researchInit}</button>}<button onClick={() => runAR("status")} disabled={!researchProject}>{t.researchStatus}</button></div>
                  </div>
                </div>

                {renderClaudeSessionPanel("environment")}

                <div className="panel researchMetricPanel">
                  <h3>{lang === "zh" ? "主线门控详情" : "Main Gate"}</h3>
                  <p className="help">{lang === "zh" ? `这里只显示环境配置步骤的证据：仓库、真实数据/loader 和参考复现。` : `Only environment-step evidence is shown here: repository, real-data/loader, and reference reproduction.`}</p>
                  <div className="trajectorySupervisorGrid humanSummaryGrid">
                    {envChecks.length ? envChecks.map((check: any) => (
                      <article className="supervisorCard" key={check.id || check.label_zh || check.label_en}>
                        <span>{localizedField(check, "label", check.id || t.noData)}</span>
                        <strong className={badgeClass(check.status)}>{displayValue(check.status || "not_started")}</strong>
                        <small>{displayMaybe(check.summary, t.noData)}</small>
                      </article>
                    )) : (
                      <article className="supervisorCard">
                        <span>{lang === "zh" ? "环境状态" : "Environment status"}</span>
                        <strong className={badgeClass(envStage?.status || "not_started")}>{displayValue(envStage?.status || "not_started")}</strong>
                        <small>{localizedField(envStage, "module_summary", localizedField(envStage, "summary", t.noData))}</small>
                      </article>
                    )}
                    {envReferenceFullJob?.status && <article className="supervisorCard">
                      <span>{lang === "zh" ? "参考复现任务" : "Reference job"}</span>
                      <strong>{displayValue(envReferenceFullJob.status)}</strong>
                      <small>{displayMaybe(envReferenceFullJob.log_path || envReferenceFullJob.pid, t.noData)}</small>
                    </article>}
                  </div>
                  <details className="metricCard">
                    <summary><strong>{lang === "zh" ? "高级" : "Advanced"}</strong><span>{lang === "zh" ? "环境选择证据" : "Environment selection evidence"}</span></summary>
                    <div className="detailList">
                      <article className="detailItem"><p>{lang === "zh" ? "选择状态" : "Selection status"}</p><small>{publicEnvironmentSelectionStatus(envStage?.selection, lang)}</small></article>
                      <article className="detailItem"><p>{lang === "zh" ? "数据集" : "Dataset"}</p><small>{displayMaybe(envStage?.dataset || envStage?.ready_datasets, t.noData)}</small></article>
                      <article className="detailItem"><p>{t.repoPathLabel}</p><small>{displayMaybe(envStage?.repo_path || activeRepo?.local_path, t.noData)}</small></article>
                      {envStage?.historical_active_repo?.name && <article className="detailItem"><p>{lang === "zh" ? "历史仓库" : "Historical repo"}</p><small>{displayMaybe(envStage.historical_active_repo.name, t.noData)}</small></article>}
                      {envReferenceFullJob?.log_path && <article className="detailItem"><p>{lang === "zh" ? "参考复现日志" : "Reference reproduction log"}</p><small>{displayMaybe(envReferenceFullJob.log_path, t.noData)}</small></article>}
                    </div>
                  </details>
                </div>
              </>
            )}
          </section>
        )}

        {tab === "experiment" && (
          <section className="stage researchStage">
            <div className="toolbar">
              <div>
                <h2>{t.experiment}</h2>
                <p className="help">{t.experimentHelp}</p>
              </div>
              <div className="toolbarActions experimentMainActions">
                <button onClick={() => refreshProject()} disabled={!researchProject}>{t.researchRefresh}</button>
                <button className="primary" onClick={() => runAR("full-cycle")} disabled={workflowLaunchDisabled}>{t.runFullResearchCycle}</button>
                <button onClick={() => runAR("experiment")} disabled={experimentLoopLaunchDisabled}>{t.runExperimentLoop}</button>
              </div>
            </div>
            {fullCycleProcessAlive && (
              <div className="researchGateNote warning"><strong>{t.fullCycleAlreadyRunning}:</strong> {t.fullCycleAlreadyRunningHelp}{fullCycleRunningText ? ` ${fullCycleRunningText}` : ""}</div>
            )}
            {stageLaunchDisabledByProjectWorker && !fullCycleProcessAlive && (
              <div className="researchGateNote warning"><strong>{lang === "zh" ? "当前任务" : "Current task"}:</strong> {projectStageLaunchLockedText}</div>
            )}
            {!researchSummary && <div className="researchGateNote warning">{lang === "zh" ? "正在刷新当前项目状态；实验迭代面板会先保留显示。" : "Refreshing current project state; the Experiment panels stay visible."}</div>}
            <>
            <details className="panel runSettingsPanel">
              <summary>{t.runSettings}</summary>
              <p className="help">{t.fullResearchCycleHelp}</p>
              <div className="row"><div><label>{t.researchIterations}</label><input value={researchIterations} onChange={(e) => setIterations(Math.max(1, Number(e.target.value)))} type="number" min="1" /></div><div><label>{t.maxExperimentsPerRound}</label><input value={researchMaxLaunches} onChange={(e) => setMaxLaunches(Math.max(1, Number(e.target.value)))} type="number" min="1" /></div></div>
              <div className="row"><div><label>{t.researchCodingBackend}</label><input value="Claude Code" readOnly /></div></div>
              <label className="switch"><input type="checkbox" checked={researchExecutePlan} onChange={(e) => setExecutePlan(e.target.checked)} /> {t.researchExecutePlan}</label>
              <label className="switch"><input type="checkbox" checked={researchPrepareEnv} onChange={(e) => setPrepareEnv(e.target.checked)} /> {t.researchPrepareEnv}</label>
              <label className="switch"><input type="checkbox" checked={researchSkipPaper} onChange={(e) => setSkipPaper(e.target.checked)} /> {t.researchSkipPaper}</label>
              <p className="help">{t.researchCodingBackendHelp} {t.lastActualBackend}: {researchStages?.experiment?.last_backend || "claude"}.</p>
            </details>
            {renderExperimentGatePanel()}
            <div className="panel researchStageCard readableOnly"><div className="researchStageTop"><span className={`stageBadge ${badgeClass(experimentSummaryStatus)}`}>{displayValue(experimentSummaryStatus)}</span></div><h3>{experimentSummaryTitle}</h3><p>{experimentSummaryText}</p>{experimentNextActionText && <p><strong>{t.nextAction}:</strong> {experimentNextActionText}</p>}{showExperimentSummaryCount && <><p><strong>{experimentCountLabel}:</strong> {researchExperimentCompletedCount} / {researchExperimentTotalCount}</p>{experimentCountHelp && <p className="help">{experimentCountHelp}</p>}</>}{showSyntheticSmokeWarning && <p><strong>{t.caution}:</strong> {t.syntheticSmokeWarning}</p>}</div>
            {renderTrajectorySystemPanel()}
            {renderClaudeSessionPanel("experiment")}
            {literatureGateBlocked ? (
              <div className="panel"><h3>{t.experimentRecordTable}</h3><div className="emptyState">{lang === "zh" ? `当前 Find 推荐门控未过，实验阶段没有新的有效运行；旧实验记录暂不在主页面展开，避免把上一轮历史路线误认为当前科研循环。完整 CSV 仍保留在后端用于审计。` : `The current Find gate has not passed, so there is no new valid experiment run. Old experiment records are hidden here to avoid confusing previous historical routes with the current research cycle; the full CSV remains available for audit.`}</div>{experimentRecord?.csv_url && <a className="buttonLink" href={experimentRecord.csv_url} target="_blank" rel="noreferrer">{t.downloadCsv}</a>}</div>
            ) : (
            <div className="panel"><div className="panelHeaderLine"><div><h3>{t.experimentRecordTable}</h3><p className="help">{t.experimentRecordHelp} {mainRouteRepoName ? (currentMainExperimentRecordRows.length ? (lang === "zh" ? `当前路线 ${mainRouteRepoName} 已有 ${currentMainExperimentRecordRows.length} 条实验/复现记录；CSV 保留全部 ${experimentRecordTotalCount} 条审计历史。` : `${currentMainExperimentRecordRows.length} experiment/reproduction records match current route ${mainRouteRepoName}; CSV keeps all ${experimentRecordTotalCount} audit records.`) : (experimentRecordRows.length ? (lang === "zh" ? `当前路线 ${mainRouteRepoName} 尚未产生实验记录；不会在这里展开旧基底历史记录。` : `The current route ${mainRouteRepoName} has no experiment records yet; historical records are not expanded here.`) : "")) : ""}</p></div>{experimentRecord?.csv_url && <a className="buttonLink" href={experimentRecord.csv_url} target="_blank" rel="noreferrer">{t.downloadCsv}</a>}</div>{experimentRecord?.updated_at && <p className="artifactPath"><strong>{t.experimentRecordUpdated}:</strong> {formatDateMinute(experimentRecord.updated_at, lang)}</p>}{experimentRecord?.refresh_error && <div className="researchGateNote warning">{experimentRecord.refresh_error}</div>}{currentMainHasNoExperimentRows ? <div className="emptyState">{currentMainNoExperimentRowsText}</div> : visibleExperimentRecordRows.length === 0 ? <div className="emptyState">{t.noExperimentRecords}</div> : (<div className="experimentTableWrap fullExperimentRecordWrap"><table className="experimentTable experimentRecordTable"><thead><tr><th>{t.time}</th><th>{t.experimentGoal}</th><th>{t.repo} / {t.dataset}</th><th>{t.commandConfig}</th><th>{t.resultDetail}</th><th>{t.audit} / {t.reflection}</th><th>{t.nextAction} / {t.evidencePath}</th></tr></thead><tbody>{visibleExperimentRecordRows.slice(0, 50).map((row: any, index: number) => {
              const metricText = displayMaybe(row["指标"], "");
              const rawExperiment = experimentRowsByTime.get(String(row["时间"] || "").trim()) || experimentRowsNewest[index] || {};
              const registryMetrics = experimentMetricRows(rawExperiment);
              const recordMetrics = experimentMetricRowsFromRecord(metricText);
              const displayMetrics = recordMetrics.length ? recordMetrics : registryMetrics;
              const rowKey = `${row["实验ID"] || row["时间"] || "experiment"}-${index}`;
              return <tr key={rowKey}>
                <td className="experimentTime">{formatDateMinute(row["时间"], lang) || displayMaybe(row["时间"])}<small>{rawExperiment.duration_sec ? (lang === "zh" ? `耗时 ${numberText(rawExperiment.duration_sec)} 秒` : `${numberText(rawExperiment.duration_sec)} sec`) : ""}</small></td>
                <td className="experimentRecordGoal"><strong>{experimentRecordText(row["实验目的"])}</strong><small>{t.variant}: {experimentRecordText(row["方法/变体"], "")}</small><small>{lang === "zh" ? "实验ID" : "Run ID"}: {experimentRecordText(row["实验ID"], "")}</small></td>
                <td><strong>{displayMaybe(row["仓库"])}</strong><small>{t.dataset}: {displayMaybe(row["数据集"])}</small><small>{t.env}: {displayMaybe(row["运行环境"])}</small></td>
                <td className="experimentCommandCell">{commandSummary(row["关键配置/命令"])}</td>
                <td className="experimentResultCell"><div className="experimentResultTop"><span className={`stageBadge ${badgeClass(rawExperiment.status || row["审计状态"])}`}>{displayValue(rawExperiment.status || row["审计状态"])}</span><span className={rawExperiment.audit_ready ? "stageBadge ok" : "stageBadge idle"}>{rawExperiment.audit_ready ? t.ready : t.missing}</span></div>{row["结论/反思"] && <p className="experimentResultNote">{experimentRecordText(row["结论/反思"], "")}</p>}<div className="metricPills">{displayMetrics.length ? displayMetrics.slice(0, 8).map((metric: any, metricIndex: number) => <span className="metricPill" key={`${rowKey}-metric-${metricIndex}`}>{metric.key ? <strong>{metric.key}</strong> : null}{metric.value !== undefined && metric.value !== "" ? numberText(metric.value) : ""}</span>) : <span className="muted">{t.noData}</span>}</div>{displayMetrics.length > 8 && <small>{`+${displayMetrics.length - 8}`}</small>}<small>{t.badCases}: {displayMaybe(row["坏例/切片"])}</small><Sparkline values={rawExperiment.loss_curve || []} emptyLabel={t.noCurve} /></td>
                <td><strong>{displayMaybe(row["审计状态"])}</strong><small>{experimentRecordText(row["结论/反思"], "")}</small></td>
                <td className="experimentEvidenceCell"><strong>{experimentRecordText(row["下一步行动"], "")}</strong><small>{displayMaybe(row["证据路径"])}</small></td>
              </tr>;
            })}</tbody></table></div>)}</div>
            )}
            </>
          </section>
        )}

        {tab === "paperWrite" && (
          <section className="stage researchStage">
            <div className="toolbar">
              <div><h2>{t.paperWrite}</h2><p className="help">{t.paperHelp}</p></div>
              <div className="toolbarActions"><button onClick={() => refreshProject()} disabled={!researchProject}>{t.researchRefresh}</button><button className="primary" onClick={() => runAR("paper")} disabled={!researchProject || !researchVenue || stageLaunchDisabledByFullCycle || stageLaunchDisabledByProjectWorker || paperLaunchGateBlocked}>{t.runPaperWriting}</button></div>
            </div>
            {!researchSummary && <div className="researchGateNote warning">{lang === "zh" ? "正在刷新当前项目状态；论文撰写面板会先保留显示。" : "Refreshing current project state; the Paper panels stay visible."}</div>}
            <>
            {(freshBaseMainBlocked || literatureGateBlocked || paperGlobalEvidenceGateBlocked) && (
              <div className="researchGateNote warning"><strong>{t.evidenceGateNotPassed}:</strong> {paperGlobalEvidenceGateText} {lang === "zh" ? "论文撰写保持等待；参考复现、实验和投稿证据刷新通过后再启动。" : "Paper writing stays paused until reference reproduction, experiment, and submission-evidence gates refresh and pass."}</div>
            )}
            <div className="grid two">
              <div className="panel"><h3>{t.paperSettingsAndGate}</h3><div className="row"><div><label>{t.researchVenue}</label><div className="inlineInputAction"><input value={researchVenue} onChange={(e) => { setVenue(e.target.value); setProjectConfigMessage(""); }} onBlur={() => { if (researchVenue.trim()) void saveProjectConfigDraft({ silent: true, includePaperSettings: true }); }} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void saveProjectConfigDraft({ includePaperSettings: true }); } }} /><button className="smallButton" onClick={() => void saveProjectConfigDraft({ includePaperSettings: true })} disabled={!researchProject || !researchVenue.trim() || researchProjectConfigSaving}>{researchProjectConfigSaving ? t.saving : (lang === "zh" ? "保存投稿目标" : "Save venue")}</button></div>{researchProjectConfigMessage && <small className="statusHint">{researchProjectConfigMessage}</small>}</div><div><label>{t.researchTitle}</label><input value={researchTitle} onChange={(e) => { setTitle(e.target.value); setProjectConfigMessage(""); }} onBlur={() => { if (researchVenue.trim()) void saveProjectConfigDraft({ silent: true, includePaperSettings: true }); }} /></div></div><p className="help">{t.researchForceTemplate}</p><label className="switch"><input type="checkbox" checked={researchAutoInstallLatex} onChange={(e) => setAutoInstallLatex(e.target.checked)} /> {t.researchAutoInstallLatex}</label><div className="researchGateNote"><strong>{t.currentGate}:</strong> {paperStageSummaryText(researchStages?.paper)}</div></div>
              <div className="panel researchStageCard readableOnly">
                <div className="researchStageTop"><span className={`stageBadge ${badgeClass(researchStages?.paper?.status)}`}>{paperHumanStatus(researchStages?.paper)}</span></div>
                <h3>{t.paperStatus}</h3>
                <p><strong>{t.researchVenue}:</strong> {researchVenue || researchStages?.paper?.target_venue || researchStages?.paper?.venue || researchSummary?.config?.target_venue || researchSummary?.config?.venue || t.notCompleted}</p>
                <p><strong>{t.template}:</strong> {researchStages?.paper?.template_fetched ? t.fetched : t.notFetched}</p>
                <p><strong>{t.paperQualityGates}:</strong> {t.paperNormalityStatus}={displayMaybe(researchStages?.paper?.paper_normality_status)}{t.paperGateSeparator}{t.venueTemplateStatus}={displayMaybe(researchStages?.paper?.paper_venue_format_status)}{t.paperGateSeparator}{t.paperCitationRenderStatus}={displayMaybe(researchStages?.paper?.paper_citation_render_status)}{t.paperGateSeparator}{t.paperSelfReviewStatus}={paperSelfReviewDisplayStatus(researchStages?.paper)}{t.paperGateSeparator}{t.figureQualityStatus}={displayMaybe(researchStages?.paper?.paper_figure_quality_status)}</p>
                {researchStages?.paper?.conference_preview_pages && <p><strong>{t.conferencePreviewPages}:</strong> {researchStages.paper.conference_preview_pages}</p>}
                <p><strong>{t.venueHardRules}:</strong> {t.bodyPages}={displayMaybe(researchStages?.paper?.paper_normality_body_pages)} / {rangeLimitText(researchStages?.paper?.venue_submission_policy?.body_page_min, researchStages?.paper?.venue_submission_policy?.body_page_max)}; {t.referencePages}={displayMaybe(researchStages?.paper?.paper_normality_estimated_reference_pages)} / {pageLimitText(researchStages?.paper?.venue_submission_policy?.reference_page_max)}; {t.totalPages}={displayMaybe(researchStages?.paper?.paper_normality_pages)} / {pageLimitText(researchStages?.paper?.venue_submission_policy?.total_page_max)}</p>
                <p><strong>PDF:</strong> {paperPdfLabel(researchStages?.paper)}</p>
                {paperSubmissionEvidenceBlocked(researchStages?.paper) && <p><strong>{lang === "zh" ? "投稿证据门控" : "Submission evidence gate"}:</strong> {paperSubmissionGateText(researchStages?.paper)}</p>}
                <details className="nestedDetails">
                  <summary>{t.paperAdvancedDetails}</summary>
                  <p><strong>{t.paperOrchestraStatus}:</strong> {displayMaybe(researchStages?.paper?.writing_status || researchStages?.paper?.paper_stage_status)}</p>
                  <p><strong>{t.paperNormalityStatus}:</strong> {displayMaybe(researchStages?.paper?.paper_normality_status)} {researchStages?.paper?.paper_normality_citation_count ? `(${researchStages.paper.paper_normality_citation_count} refs)` : ""}</p>
                  <p><strong>{t.venueHardRules}:</strong> {displayMaybe(researchStages?.paper?.venue_submission_policy?.source_label || researchStages?.paper?.venue_submission_policy?.source_url || researchStages?.paper?.venue_submission_policy_status)}</p>
                  <p><strong>{t.venueTemplateStatus}:</strong> {displayMaybe(researchStages?.paper?.paper_venue_format_status)}</p>
                  <p><strong>{t.paperCitationRenderStatus}:</strong> {displayMaybe(researchStages?.paper?.paper_citation_render_status)} {paperCitationRenderRows(researchStages?.paper).length ? `(${paperCitationRenderRows(researchStages?.paper).length})` : ""}</p>
                  <p><strong>{t.paperSelfReviewStatus}:</strong> {paperSelfReviewDisplayStatus(researchStages?.paper)} {researchStages?.paper?.paper_self_review_independent_findings_count !== undefined || researchStages?.paper?.paper_self_review_repairs_count !== undefined ? `(${lang === "zh" ? "发现" : "findings"}: ${displayMaybe(researchStages?.paper?.paper_self_review_independent_findings_count)}; ${lang === "zh" ? "修复" : "repairs"}: ${displayMaybe(researchStages?.paper?.paper_self_review_repairs_count)}; ${lang === "zh" ? "证据阻塞" : "evidence blockers"}: ${displayMaybe(researchStages?.paper?.paper_self_review_evidence_blocker_count ?? asArray(researchStages?.paper?.paper_self_review_evidence_blockers).length)})` : ""}</p>
                  <p><strong>{t.figureQualityStatus}:</strong> {displayMaybe(researchStages?.paper?.paper_figure_quality_status)} {researchStages?.paper?.paper_figure_blocker_count !== undefined && researchStages?.paper?.paper_figure_blocker_count !== "" ? `(${t.figureQualityBlocked}: ${researchStages.paper.paper_figure_blocker_count})` : ""}</p>
                  <p><strong>{t.figureRepairLoop}:</strong> {displayMaybe(researchStages?.paper?.paper_figure_repair_loop_status)} {researchStages?.paper?.paper_figure_repair_rounds ? `(${researchStages.paper.paper_figure_repair_rounds})` : ""}</p>
                  <p><strong>{t.previewRepairLoop}:</strong> {displayMaybe(researchStages?.paper?.paper_preview_repair_loop_status)} {researchStages?.paper?.paper_preview_repair_rounds ? `(${researchStages.paper.paper_preview_repair_rounds})` : ""}</p>
                </details>
              </div>
            </div>
            {researchStages?.paper?.paper_generation_skipped && <div className="researchGateNote warning"><strong>{t.evidenceGateNotPassed}:</strong> {researchStages?.paper?.science_gate_preflight_blockers?.slice?.(0, 3)?.join("；") || researchStages?.paper?.paper_generation_skipped_reason || t.evidenceGateWarning}</div>}
            {researchStages?.paper?.status === "preview_pdf_blocked" && <div className="researchGateNote warning"><strong>{t.evidenceGateNotPassed}:</strong> {t.evidenceGateWarning}</div>}
            {renderClaudeSessionPanel("paper")}
            <div className="panel paperPreview"><h3>{paperPreviewTitle(researchStages?.paper)}</h3>{researchStages?.paper?.pdf_url ? (<><p className="artifactPath"><strong>PDF:</strong> {researchStages.paper.pdf_path || researchStages.paper.pdf_url}</p>{researchStages.paper.tex_path && <p className="artifactPath"><strong>TeX:</strong> {researchStages.paper.tex_path}</p>}<div className="paperArtifactActions"><a href={researchStages.paper.pdf_url} target="_blank" rel="noreferrer">{t.openPdf}</a>{researchStages.paper.tex_url && <a href={researchStages.paper.tex_url} target="_blank" rel="noreferrer">{t.openTex}</a>}</div><iframe className="pdfViewer" src={researchStages.paper.pdf_url} title="compiled paper pdf" /></>) : researchStages?.paper?.blocked_pdf_url ? (<><p className="help">{paperPreviewHelp(researchStages?.paper)}</p><p className="artifactPath"><strong>PDF:</strong> {researchStages.paper.blocked_pdf_path || researchStages.paper.blocked_pdf_url}</p>{researchStages.paper.blocked_tex_path && <p className="artifactPath"><strong>TeX:</strong> {researchStages.paper.blocked_tex_path}</p>}<div className="paperArtifactActions"><a href={researchStages.paper.blocked_pdf_url} target="_blank" rel="noreferrer">{t.openPdf}</a>{researchStages.paper.blocked_tex_url && <a href={researchStages.paper.blocked_tex_url} target="_blank" rel="noreferrer">{t.openTex}</a>}</div><iframe className="pdfViewer blockedPdfViewer" src={researchStages.paper.blocked_pdf_url} title="paper pdf preview" /></>) : (<div className="emptyState"><p>{t.noPdf}</p>{researchStages?.paper?.raw_pdf_path && <p className="artifactPath"><strong>{t.rawPaperOrchestraOutput}:</strong> {researchStages.paper.raw_pdf_path}</p>}{researchStages?.paper?.writing_workspace && <p className="artifactPath"><strong>{t.workspaceLabel}:</strong> {researchStages.paper.writing_workspace}</p>}</div>)}</div>
            </>
          </section>
        )}

        <section className="bottom" data-testid="global-task-artifact">
          <div className="panel logPanel">
            <h2 data-testid="global-task-heading">{t.job}</h2>
            <p className="help">{lang === "zh" ? (tab === "find" ? "全局 任务栏：展示当前和历史 run/job 的阶段、进度、日志和产物状态。" : "全局 任务栏：展示当前和历史 run/job；Find 文献计数和文献包只在“发现”页展开，避免混入当前阶段主体。") : (tab === "find" ? "Global taskbar: current and historical run/job stages, progress, logs, and artifact status." : "Global taskbar: current and historical run/jobs; Find literature counts and packets expand only on the Find page so they do not mix into this stage body.")}</p>
            {!jobsLoaded ? (
              <div className="status">{lang === "zh" ? "正在加载任务状态..." : "Loading jobs..."}</div>
            ) : displayJobs.length === 0 ? (
              <div className="status">{t.idle}</div>
            ) : (
              <div className="jobList">
                {displayJobs.map((item) => {
                  const detailedFindProgress = findTaskProgressView(item, lang);
                  const showDetailedFindProgress = Boolean(detailedFindProgress);
                  const taskLogLines = showDetailedFindProgress
                    ? (detailedFindProgress?.logLines || []).filter((line) => tab === "find" || !/^(?:实时计数[:：]|Live counts:)/.test(line))
                    : [];
                  const consoleLines = [...taskLogLines, ...jobRecentLogs(item, lang, tab)]
                    .filter((line, index, rows) => Boolean(String(line || "").trim()) && rows.indexOf(line) === index);
                  const readOverall = canonicalJobStage(item) === "read" && item.progress?.read_progress && typeof item.progress.read_progress === "object"
                    ? item.progress.read_progress
                    : null;
                  const progressTotal = Number(readOverall?.overall_total ?? item.progress?.total ?? 0);
                  const progressPercent = Number(readOverall?.overall_percent ?? item.progress?.percent ?? 0);
                  const progressView = detailedFindProgress ? {
                    message: `${lang === "zh" ? "当前阶段" : "Current stage"} ${detailedFindProgress.stageIndex}/${detailedFindProgress.stageCount} · ${detailedFindProgress.stageLabel}`,
                    percent: detailedFindProgress.stagePercent,
                    measured: true,
                    detail: `${lang === "zh" ? "具体步骤" : "Step"}：${detailedFindProgress.stepLabel} · ${lang === "zh" ? "正在进行" : "Now"}：${detailedFindProgress.action}`,
                    ariaLabel: lang === "zh" ? "Find 当前阶段进度" : "Current Find stage progress",
                  } : item.progress ? {
                    message: displayJobProgressMessage(item, lang),
                    percent: progressPercent,
                    measured: progressTotal > 0,
                    detail: progressTotal > 0
                      ? `${jobProgressPhaseLabel(item, lang)} / ${item.progress.current} / ${item.progress.total}`
                      : `${jobProgressPhaseLabel(item, lang)} ${jobStatusLabel(item.status, lang)}`,
                    ariaLabel: "",
                  } : null;
                  return (
                    <article className="jobCard" key={item.job_id}>
                      <div className="jobHeader">
                        <strong>{jobDisplayTitle(item, lang)}</strong>
                        <span>
                          {jobStatusLabel(item.status, lang)}
                          {(isLiveJob(item) || item.finished_at) && <JobDuration createdAt={item.created_at} finishedAt={item.finished_at} live={isLiveJob(item)} lang={lang} />}
                        </span>
                      </div>
                      <small>{jobMetaLine(item, lang)}</small>
                      {["queued", "running", "cancelling"].includes(item.status) && (
                        <button className="danger smallButton" onClick={() => stopJob(item.job_id)} disabled={item.status === "cancelling"}>
                          {t.stop}
                        </button>
                      )}
                      {progressView && (
                        <div className="progressBlock" data-testid={detailedFindProgress ? "find-task-progress" : undefined}>
                          <div className="progressMeta">
                            <span>{progressView.message}</span>
                            {progressView.measured && <strong>{progressView.percent}%</strong>}
                          </div>
                          {progressView.measured && <progress aria-label={progressView.ariaLabel || undefined} value={progressView.percent} max="100" />}
                          <small>{progressView.detail}</small>
                        </div>
                      )}
                      <pre>{consoleLines.join("\n")}</pre>
                    </article>
                  );
                })}
              </div>
            )}
          </div>
          {showRunArtifactPanel && <div className="panel artifactPanel" data-testid={tab === "plan" ? "plan-artifact-panel" : undefined}>
            <h2 data-testid="global-artifact-heading">{t.artifacts}</h2>
            {t.artifactHelp && <p className="help">{t.artifactHelp}</p>}
            {renderedRunArtifacts.length === 0 && (
              <div className="emptyState">
                <p>{artifactPanelLoading ? t.loadingRunArtifacts : t.noRunArtifacts}</p>
                {renderedRunArtifactsRunId && <p className="artifactPath"><strong>run:</strong> {renderedRunArtifactsRunId}</p>}
                {!runArtifactsLoading && !(["find", "read", "ideas", "plan"] as Tab[]).includes(tab) && String(runId || "").startsWith("find_") && <p className="artifactPath"><strong>{lang === "zh" ? "Find 产物边界" : "Find artifact boundary"}:</strong> {lang === "zh" ? "当前选中的是 Find run；Find/read/idea/plan 产物只在对应前四个页面展开，避免混入环境、实验或论文阶段。" : "The selected run is a Find run; Find/read/idea/plan artifacts expand only on their matching pages, not in environment, experiment, or paper stages."}</p>}
              </div>
            )}
            {renderedRunArtifacts.length > 0 && (
              <>
                {renderedRunArtifactsRunId && <p className="artifactPath"><strong>run:</strong> {renderedRunArtifactsRunId}</p>}
                <div className="emailBox">
                  <input value={emailSubject} onChange={(e) => setEmailSubject(e.target.value)} placeholder={t.emailSubject} />
                  <input value={emailReceiversOverride} onChange={(e) => setEmailReceiversOverride(e.target.value)} placeholder={t.emailReceivers} />
                  <button className="primary" onClick={runEmail} disabled={!renderedRunArtifactsRunId || !config.email.manual_enabled}>{t.sendEmail}</button>
                </div>
                <div className="artifactTabs">
                  {renderedRunArtifacts.map((artifact) => (
                    <button key={artifact.name} className={currentArtifact?.name === artifact.name ? "active" : ""} onClick={() => setActiveArtifact(artifact.name)}>
                      {artifactDisplayName(artifact.name, lang)}
                    </button>
                  ))}
                </div>
                {currentArtifact && (
                  <div className="artifactView">
                    {currentArtifact.path && (
                      <p className="artifactPath"><strong>{t.artifactPath}:</strong> {publicArtifactPath(currentArtifact.path, "", lang)}</p>
                    )}
                    <div className="artifactToggle">
                      <button className={!rawArtifacts[currentArtifact.name] ? "active" : ""} onClick={() => setRawArtifacts((prev) => ({ ...prev, [currentArtifact.name]: false }))}>
                        {t.rendered}
                      </button>
                      <button className={rawArtifacts[currentArtifact.name] ? "active" : ""} onClick={() => setRawArtifacts((prev) => ({ ...prev, [currentArtifact.name]: true }))}>
                        {t.raw}
                      </button>
                    </div>
                    {rawArtifacts[currentArtifact.name] ? (
                      <pre>{artifactPanelContent(currentArtifact, { raw: true })}</pre>
                    ) : (
                      <div
                        className="markdownBody"
                        data-testid={currentArtifact.name === "idea.md" ? "idea-artifact-markdown" : currentArtifact.name === "plan.md" ? "plan-artifact-markdown" : undefined}
                        dangerouslySetInnerHTML={{
                          __html: currentArtifact.name === "idea.md"
                            ? markdownRenderer.render(artifactPanelContent(currentArtifact))
                            : markdownToHtml(artifactPanelContent(currentArtifact)),
                        }}
                      />
                    )}
                  </div>
                )}
              </>
            )}
          </div>}
        </section>
      </section>
    </main>
  );
}

function authErrorMessage(error: unknown) {
  const raw = String(error || "");
  const jsonStart = raw.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const payload = JSON.parse(raw.slice(jsonStart));
      if (payload?.error) return String(payload.error);
    } catch {
      // Keep the original network error when the response is not JSON.
    }
  }
  return raw.replace(/^Error:\s*/, "") || "请求失败，请稍后重试。";
}

function App() {
  const [account, setAccount] = useState<AuthUser | null | undefined>(undefined);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [identifier, setIdentifier] = useState("");
  const [registerUsername, setRegisterUsername] = useState("");
  const [email, setEmail] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authNotice, setAuthNotice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sendingCode, setSendingCode] = useState(false);
  const [codeRetryAfter, setCodeRetryAfter] = useState(0);

  useEffect(() => {
    void getCurrentUser()
      .then(setAccount)
      .catch((error) => {
        setAuthError(authErrorMessage(error));
        setAccount(null);
      });
    const requireAuth = () => setAccount(null);
    window.addEventListener("taste:auth-required", requireAuth);
    return () => window.removeEventListener("taste:auth-required", requireAuth);
  }, []);

  useEffect(() => {
    if (codeRetryAfter <= 0) return;
    const timer = window.setInterval(() => {
      setCodeRetryAfter((value) => Math.max(0, value - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [codeRetryAfter]);

  function switchAuthMode(nextMode: "login" | "register") {
    setMode(nextMode);
    setAuthError("");
    setAuthNotice("");
  }

  async function sendVerificationCode() {
    const emailInput = document.getElementById("auth-email") as HTMLInputElement | null;
    if (!emailInput?.reportValidity()) return;
    setSendingCode(true);
    setAuthError("");
    setAuthNotice("");
    try {
      const response = await requestEmailVerification(email.trim());
      setCodeRetryAfter(Math.max(1, response.retry_after || 60));
      setAuthNotice("验证码已发送，请查看邮箱。");
    } catch (error) {
      setAuthError(authErrorMessage(error));
    } finally {
      setSendingCode(false);
    }
  }

  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mode === "register" && password !== confirmPassword) {
      setAuthError("两次输入的密码不一致。");
      return;
    }
    setSubmitting(true);
    setAuthError("");
    setAuthNotice("");
    try {
      const response = mode === "login"
        ? await login(identifier, password)
        : await register(registerUsername, email, password, verificationCode);
      localStorage.removeItem("selected_project");
      setAccount(response.user);
      setPassword("");
      setConfirmPassword("");
    } catch (error) {
      setAuthError(authErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function signOut() {
    try {
      await logout();
    } finally {
      localStorage.removeItem("selected_project");
      setAccount(null);
    }
  }

  if (account === undefined) {
    return <main className="authShell"><section className="authCard"><div className="authMark">T</div><p>正在连接 TASTE…</p></section></main>;
  }

  if (!account) {
    return (
      <main className="authShell">
        <section className="authCard">
          <div className="authHeading">
            <div className="authMark">T</div>
            <div><h1>TASTE</h1></div>
          </div>
          <div className="authTabs">
            <button type="button" className={mode === "login" ? "active" : ""} onClick={() => switchAuthMode("login")}>登录</button>
            <button type="button" className={mode === "register" ? "active" : ""} onClick={() => switchAuthMode("register")}>注册</button>
          </div>
          <form onSubmit={submitAuth}>
            {mode === "login" ? (
              <>
                <label htmlFor="auth-identifier">用户名或邮箱</label>
                <input id="auth-identifier" value={identifier} onChange={(event) => setIdentifier(event.target.value)} autoComplete="username" required maxLength={254} autoFocus />
              </>
            ) : (
              <>
                <label htmlFor="auth-username">用户名</label>
                <input id="auth-username" value={registerUsername} onChange={(event) => setRegisterUsername(event.target.value)} autoComplete="username" required minLength={3} maxLength={64} autoFocus />
                <label htmlFor="auth-email">邮箱</label>
                <input id="auth-email" type="email" value={email} onChange={(event) => {
                  setEmail(event.target.value);
                  setVerificationCode("");
                  setCodeRetryAfter(0);
                  setAuthNotice("");
                }} autoComplete="email" required maxLength={254} />
                <label htmlFor="auth-verification-code">邮箱验证码</label>
                <div className="authCodeRow">
                  <input id="auth-verification-code" value={verificationCode} onChange={(event) => setVerificationCode(event.target.value.replace(/\D/g, "").slice(0, 6))} autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" title="请输入 6 位验证码" required maxLength={6} />
                  <button type="button" onClick={() => void sendVerificationCode()} disabled={sendingCode || codeRetryAfter > 0}>
                    {sendingCode ? "发送中…" : codeRetryAfter > 0 ? `${codeRetryAfter} 秒` : "获取验证码"}
                  </button>
                </div>
              </>
            )}
            <label htmlFor="auth-password">密码</label>
            <input id="auth-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} required minLength={8} maxLength={128} />
            {mode === "register" && (
              <>
                <label htmlFor="auth-password-confirm">确认密码</label>
                <input id="auth-password-confirm" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" required minLength={8} maxLength={128} />
              </>
            )}
            {authError && <p className="authError" role="alert">{authError}</p>}
            {authNotice && <p className="authNotice" role="status">{authNotice}</p>}
            <button className="primary authSubmit" disabled={submitting}>{submitting ? "请稍候…" : mode === "login" ? "登录" : "创建账户"}</button>
          </form>
          {mode === "register" && <p className="authHelp">验证码 10 分钟内有效；密码至少 8 个字符。</p>}
        </section>
      </main>
    );
  }

  return <TasteApp key={account.id} account={account} onLogout={() => void signOut()} />;
}

export default App;
