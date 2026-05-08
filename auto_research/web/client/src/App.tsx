import { useEffect, useMemo, useState } from "react";
import {
  Artifact,
  Config,
  Job,
  RunInfo,
  Venue,
  cancelJob,
  checkVenueHealth,
  deleteRun,
  finishPlan,
  getArtifacts,
  getConfig,
  getConfigMeta,
  getJobs,
  getRuns,
  getVenues,
  patchIdea,
  saveConfig,
  startEmail,
  startFind,
  startIdea,
  startPlan,
  startPlanPolish,
  startRead,
  watchJob,
} from "./api";

const DEFAULT_CONFIG: Config = {
  research_interest: "",
  researcher_profile: "",
  provider: "openai",
  base_url: "https://api.openai.com/v1",
  api_key: "",
  model: "gpt-4o-mini",
  temperature: 0.4,
  llm_roles: {},
  llm_concurrency: 16,
  idea_parallel_workers: 1,
  max_fetch_papers: 40,
  max_recommended_papers: 20,
  max_ideas: 6,
  venue_title_scan_limit: 200,
  venue_title_scan_fraction: 1.0,
  arxiv_categories: ["cs.AI"],
  arxiv_start_date: "",
  arxiv_end_date: "",
  github_languages: ["all"],
  github_since: "daily",
  hf_include_papers: true,
  hf_include_models: true,
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

type Tab = "find" | "read" | "ideas" | "plan";
type Lang = "zh" | "en";

const LLM_ROLES = [
  ["find", "Find"],
  ["read", "Read"],
  ["idea_generator", "Idea Generator"],
  ["idea_judge", "Idea Judge"],
  ["plan_generator", "Plan Generator"],
  ["plan_evaluator", "Plan Evaluator"],
] as const;

const TEXT = {
  zh: {
    profile: "研究画像",
    interest: "研究兴趣",
    interestHelp: "描述你当前关注的问题、方法、应用场景或关键词。Find/Idea/Plan 都会使用这段信息做匹配。",
    researcher: "研究者画像",
    researcherHelp: "填写你的背景、已有项目、偏好的实验条件、长期研究方向等。",
    llm: "LLM 配置",
    llmHelp: "用于论文相关性评分、分类推断、精读、idea 和 plan 生成。不填 API key 时会使用本地 fallback 跑通流程。",
    provider: "Provider",
    providerHelp: "OpenAI-compatible 服务类型，例如 openai、siliconflow；mock 表示不调用远程 LLM。",
    baseUrl: "Base URL",
    baseUrlHelp: "OpenAI-compatible API 地址，例如 https://api.openai.com/v1。",
    model: "Model",
    modelHelp: "用于评分和生成的模型名称。",
    apiKey: "API Key",
    apiKeyHelp: "仅保存在本地配置文件，用于调用你的 LLM 服务。",
    temperature: "Temperature",
    temperatureHelp: "控制生成随机性；精读和筛选建议 0.2-0.6。",
    roleConfig: "角色 LLM 配置",
    roleConfigHelp: "留空则继承上方全局 LLM。每个阶段可以单独覆盖 provider、base URL、model、API key 和 temperature。",
    emailSettings: "邮件配置",
    emailHelp: "用于把当前 run 的渲染后 HTML 报告发送到邮箱。SMTP 密码只保存在本地配置文件。",
    smtpServer: "SMTP Server",
    smtpPort: "SMTP Port",
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
    screenedRanking: "筛选后完整排名",
    screenedRankingHelp: "只展示 fit_score > 6 的候选论文，并按最终 score 降序排列。",
    noRanking: "当前 run 还没有 fit_score > 6 的筛选后排名。",
    limits: "运行数量",
    llmConcurrency: "LLM 评估并发数",
    llmConcurrencyHelp: "Find 等评估任务使用，范围 1-32，默认 16。",
    ideaWorkers: "Idea 生成并发数",
    ideaWorkersHelp: "范围 1-8，默认 1。每个 worker 使用不同论文窗口，不重复喂同一批论文。",
    repairRounds: "Plan 修复轮数",
    repairRoundsHelp: "每个 plan 先生成初版，再执行 evaluate -> repair 的轮数。",
    polishRounds: "继续优化轮数",
    polishFurther: "继续优化",
    finishPlan: "完成",
    planCompleted: "已完成",
    finishPlanConfirm: "完成后页面和 plan.md 将只保留正文，评估/修复过程仍保存在 plans.json 中。确认完成？",
    fetchLimit: "抓取论文数量",
    fetchLimitHelp: "每个来源或 venue 尝试抓取的候选数量上限。",
    recommendLimit: "推荐论文最大数量",
    recommendLimitHelp: "Find 阶段最终写入 article.md 的论文数量上限。",
    ideaLimit: "Idea 最大数量",
    ideaLimitHelp: "Idea 阶段生成的研究想法数量上限。",
    titleScanLimit: "会议标题扫描数量",
    titleScanLimitHelp: "没有官方分类的会议会先抓论文标题池，再由 LLM/关键词筛选值得抓详情的论文。",
    titleScanFraction: "标题扫描比例",
    titleScanFractionHelp: "对已抓到的标题池取多少比例做预筛选，1 表示全部，0.25 表示前 25%。",
    saveConfig: "保存配置",
    saving: "保存中...",
    saved: "配置已保存",
    configPath: "配置文件",
    checkVenue: "检查可抓取性",
    checking: "检查中...",
    healthOk: "可抓取",
    healthFail: "不可抓取",
    noApprovedIdeas: "当前 run 还没有通过的 idea。请先在 Ideas 页点击“通过”。",
    selectAll: "全选",
    clearAll: "清空",
    rendered: "渲染",
    raw: "源码",
    stop: "停止",
    deleteRun: "删除",
    deleteRunConfirm: "确定删除这条历史运行记录？该操作会删除本地 run 目录。",
    runs: "历史运行",
    find: "发现",
    read: "精读",
    ideas: "想法",
    plan: "计划",
    runFind: "运行 Find",
    venues: "会议 / 期刊",
    venueHelp: "选择一个或多个会议/期刊。ICLR 使用官方分类；CCF/DBLP 分类由 LLM 推断并标注。",
    selectedVenuesTitle: "已选会议",
    availableVenuesTitle: "未选会议",
    add: "添加",
    remove: "移除",
    venueSearch: "搜索会议、期刊、领域或 rank",
    years: "年份",
    yearsHelp: "可输入多个年份，用逗号或空格隔开，例如 2025, 2026。",
    selected: "已选",
    shown: "显示",
    sources: "Sources",
    sourcesHelp: "控制是否额外收集 arXiv、HuggingFace 和 GitHub 热门内容。",
    arxivCategories: "arXiv categories",
    arxivHelp: "可输入多个分类，用逗号或空格隔开，例如 cs.AI, cs.CV。",
    arxivDateHelp: "可选日期范围，格式 YYYY-MM-DD 或 YYYY/MM/DD；arXiv/HuggingFace/GitHub 共用，两个日期都留空则默认全时间段或最新可用 feed。",
    sourceStatus: "Source 状态",
    githubLanguages: "GitHub languages",
    githubLanguagesHelp: "GitHub Trending 语言过滤，可输入 all 或 python, javascript 等。",
    startDate: "开始日期",
    endDate: "结束日期",
    runRead: "运行 Read",
    runIdeas: "生成 Idea",
    runPlan: "生成 Plan",
    approve: "通过",
    pending: "待定",
    delete: "删除",
    job: "任务",
    artifacts: "产物",
    artifactHelp: "选择一个产物查看，避免一次展开过多内容。",
    idle: "空闲",
  },
  en: {
    profile: "Profile",
    interest: "Research Interest",
    interestHelp: "Describe your current problems, methods, domains, or keywords. Find/Idea/Plan use this for matching.",
    researcher: "Researcher Profile",
    researcherHelp: "Add your background, existing projects, preferred experimental constraints, and long-term directions.",
    llm: "LLM Settings",
    llmHelp: "Used for relevance scoring, inferred categories, paper reading, idea generation, and planning. Without an API key, local fallback keeps the workflow runnable.",
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
    roleConfig: "Role LLM Settings",
    roleConfigHelp: "Leave blank to inherit the global LLM above. Each stage can override provider, base URL, model, API key, and temperature.",
    emailSettings: "Email Settings",
    emailHelp: "Send the current run as a rendered HTML report. The SMTP password is stored only in the local config file.",
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
    screenedRanking: "Full screened ranking",
    screenedRankingHelp: "Only shows candidates with fit_score > 6, sorted by final score.",
    noRanking: "No screened ranking with fit_score > 6 for this run yet.",
    limits: "Run Limits",
    llmConcurrency: "LLM evaluation concurrency",
    llmConcurrencyHelp: "Used by Find-style evaluation tasks. Range 1-32, default 16.",
    ideaWorkers: "Idea generation workers",
    ideaWorkersHelp: "Range 1-8, default 1. Each worker receives a distinct paper window.",
    repairRounds: "Plan repair rounds",
    repairRoundsHelp: "Each plan gets an initial draft, then evaluate -> repair for this many rounds.",
    polishRounds: "Polish rounds",
    polishFurther: "Polish further",
    finishPlan: "Finish",
    planCompleted: "Completed",
    finishPlanConfirm: "After finishing, the page and plan.md will keep only the final body. Evaluation/repair history remains in plans.json. Continue?",
    fetchLimit: "Fetch paper count",
    fetchLimitHelp: "Maximum candidate items fetched from each source or venue.",
    recommendLimit: "Max recommended papers",
    recommendLimitHelp: "Maximum papers written to article.md by the Find stage.",
    ideaLimit: "Max ideas",
    ideaLimitHelp: "Maximum research ideas generated in the Idea stage.",
    titleScanLimit: "Venue title scan count",
    titleScanLimitHelp: "For venues without official categories, the app first collects a title pool and filters titles before fetching details.",
    titleScanFraction: "Title scan fraction",
    titleScanFractionHelp: "Fraction of the collected title pool to prefilter. 1 means all, 0.25 means the first 25%.",
    saveConfig: "Save Config",
    saving: "Saving...",
    saved: "Config saved",
    configPath: "Config file",
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
    find: "Find",
    read: "Read",
    ideas: "Ideas",
    plan: "Plan",
    runFind: "Run Find",
    venues: "Venues",
    venueHelp: "Select one or more conferences/journals. ICLR uses official categories; CCF/DBLP categories are LLM-inferred and labeled.",
    selectedVenuesTitle: "Selected Venues",
    availableVenuesTitle: "Available Venues",
    add: "Add",
    remove: "Remove",
    venueSearch: "Search venue, journal, field, or rank",
    years: "Years",
    yearsHelp: "Enter multiple years separated by commas or spaces, e.g. 2025, 2026.",
    selected: "selected",
    shown: "shown",
    sources: "Sources",
    sourcesHelp: "Choose whether to also collect arXiv, HuggingFace, and GitHub trending signals.",
    arxivCategories: "arXiv categories",
    arxivHelp: "Enter multiple categories separated by commas or spaces, e.g. cs.AI, cs.CV.",
    arxivDateHelp: "Optional date range in YYYY-MM-DD or YYYY/MM/DD; shared by arXiv/HuggingFace/GitHub. Leave both empty for all-time or the latest available feed.",
    sourceStatus: "Source Status",
    githubLanguages: "GitHub languages",
    githubLanguagesHelp: "GitHub Trending language filter, such as all, python, javascript.",
    startDate: "start date",
    endDate: "end date",
    runRead: "Run Read",
    runIdeas: "Generate Ideas",
    runPlan: "Generate Plan",
    approve: "Approve",
    pending: "Pending",
    delete: "Delete",
    job: "Job",
    artifacts: "Artifacts",
    artifactHelp: "Choose one artifact to view so the panel stays readable.",
    idle: "idle",
  },
} satisfies Record<Lang, Record<string, string>>;

function splitList(value: string) {
  return value.split(/[,\s]+/).map((item) => item.trim()).filter(Boolean);
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function markdownToHtml(markdown: string) {
  const lines = markdown.split(/\r?\n/);
  const html: string[] = [];
  let inList = false;
  let listTag = "ul";
  let inCode = false;
  const codeLines: string[] = [];
  const closeList = () => {
    if (inList) {
      html.push(`</${listTag}>`);
      inList = false;
    }
  };
  const flushCode = () => {
    if (inCode) {
      html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      codeLines.length = 0;
      inCode = false;
    }
  };
  const inline = (text: string) => escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      if (inCode) {
        flushCode();
      } else {
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    if (!line.trim()) {
      closeList();
      continue;
    }
    if (line.trim().startsWith("|") && lines[index + 1]?.match(/^\s*\|?[\s:-]+\|[\s|:-]*$/)) {
      closeList();
      const rows: string[][] = [];
      rows.push(line.split("|").map((cell) => cell.trim()).filter(Boolean));
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(lines[index].split("|").map((cell) => cell.trim()).filter(Boolean));
        index += 1;
      }
      index -= 1;
      const [head, ...body] = rows;
      html.push("<table><thead><tr>");
      head.forEach((cell) => html.push(`<th>${inline(cell)}</th>`));
      html.push("</tr></thead><tbody>");
      body.forEach((row) => {
        html.push("<tr>");
        row.forEach((cell) => html.push(`<td>${inline(cell)}</td>`));
        html.push("</tr>");
      });
      html.push("</tbody></table>");
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!inList || listTag !== "ul") {
        closeList();
        listTag = "ul";
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    const numbered = line.match(/^\d+\.\s+(.+)$/);
    if (numbered) {
      if (!inList || listTag !== "ol") {
        closeList();
        listTag = "ol";
        html.push("<ol>");
        inList = true;
      }
      html.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }
    closeList();
    html.push(`<p>${inline(line)}</p>`);
  }
  flushCode();
  closeList();
  return html.join("\n");
}

function App() {
  const [tab, setTab] = useState<Tab>("find");
  const [lang, setLang] = useState<Lang>(() => (localStorage.getItem("taste_lang") as Lang) || (localStorage.getItem("auto_research_lang") as Lang) || "zh");
  const [config, setConfig] = useState<Config>(DEFAULT_CONFIG);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [runId, setRunId] = useState("");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedVenues, setSelectedVenues] = useState<string[]>([]);
  const [years, setYears] = useState("2026");
  const [venueQuery, setVenueQuery] = useState("");
  const [includeArxiv, setIncludeArxiv] = useState(true);
  const [includeHf, setIncludeHf] = useState(true);
  const [includeGithub, setIncludeGithub] = useState(true);
  const [selectedPapers, setSelectedPapers] = useState<string[]>([]);
  const [planIdeaIds, setPlanIdeaIds] = useState<string[]>([]);
  const [planRepairRounds, setPlanRepairRounds] = useState(3);
  const [polishRounds, setPolishRounds] = useState<Record<string, number>>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const [savingConfig, setSavingConfig] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [configPath, setConfigPath] = useState("");
  const [checkingVenues, setCheckingVenues] = useState(false);
  const [venueHealth, setVenueHealth] = useState<Record<string, { ok: boolean; message: string; source_adapter: string; sample_count: number }>>({});
  const [rawArtifacts, setRawArtifacts] = useState<Record<string, boolean>>({});
  const [activeArtifact, setActiveArtifact] = useState("");
  const [emailReceiversOverride, setEmailReceiversOverride] = useState("");
  const [emailSubject, setEmailSubject] = useState("");

  useEffect(() => {
    void bootstrap();
  }, []);

  useEffect(() => {
    localStorage.setItem("taste_lang", lang);
  }, [lang]);

  async function bootstrap() {
    try {
      const [cfg, meta, venueData, runData, jobData] = await Promise.all([getConfig(), getConfigMeta(), getVenues(), getRuns(), getJobs()]);
      setConfig(cfg);
      setConfigPath(meta.path);
      setVenues(venueData);
      setRuns(runData);
      setJobs(jobData);
      jobData.filter((item) => ["queued", "running", "cancelling"].includes(item.status)).forEach((item) => watchExistingJob(item.job_id));
      if (runData[0]) {
        await loadRun(runData[0].run_id);
      }
    } catch (err) {
      setError(String(err));
    }
  }

  async function loadRun(id: string) {
    setRunId(id);
    const data = await getArtifacts(id);
    setArtifacts(data.artifacts);
  }

  async function refreshRuns(nextRunId?: string) {
    const runData = await getRuns();
    setRuns(runData);
    if (nextRunId) {
      await loadRun(nextRunId);
    }
  }

  function updateConfig<K extends keyof Config>(key: K, value: Config[K]) {
    setConfig((prev) => ({ ...prev, [key]: value }));
    setSaveMessage("");
  }

  function updateRoleConfig(role: string, key: string, value: string | number | null) {
    setConfig((prev) => ({
      ...prev,
      llm_roles: {
        ...(prev.llm_roles || {}),
        [role]: {
          ...((prev.llm_roles || {})[role] || {}),
          [key]: value,
        },
      },
    }));
    setSaveMessage("");
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

  function updateJob(nextJob: Job) {
    setJobs((prev) => {
      const exists = prev.some((item) => item.job_id === nextJob.job_id);
      const merged = exists ? prev.map((item) => item.job_id === nextJob.job_id ? nextJob : item) : [nextJob, ...prev];
      return merged.sort((a, b) => b.created_at.localeCompare(a.created_at));
    });
  }

  function watchExistingJob(jobId: string, nextTab?: Tab) {
    const socket = watchJob(jobId, (message) => {
      if (message.type === "log") {
        setJobs((prev) => prev.map((item) => item.job_id === jobId ? { ...item, logs: [...item.logs, message.message] } : item));
      }
      if (message.type === "progress") {
        setJobs((prev) => prev.map((item) => item.job_id === jobId ? { ...item, progress: message.progress } : item));
      }
      if (message.type === "complete") {
        updateJob(message.job);
        const resultRunId = message.job?.result?.run_id || runId;
        if (resultRunId) void refreshRuns(resultRunId);
        if (nextTab && message.job.status === "done") setTab(nextTab);
        socket.close();
      }
      if (message.type === "error") {
        setError(message.message);
      }
    });
  }

  async function handleSaveConfig() {
    try {
      setSavingConfig(true);
      setError("");
      await saveConfig(config);
      setSaveMessage(t.saved);
    } catch (err) {
      setError(String(err));
    } finally {
      setSavingConfig(false);
    }
  }

  function attachJob(nextJob: Job, nextTab?: Tab) {
    updateJob(nextJob);
    setError("");
    const socket = watchJob(nextJob.job_id, (message) => {
      if (message.type === "log") {
        setJobs((prev) => prev.map((item) => item.job_id === nextJob.job_id ? { ...item, logs: [...item.logs, message.message] } : item));
      }
      if (message.type === "progress") {
        setJobs((prev) => prev.map((item) => item.job_id === nextJob.job_id ? { ...item, progress: message.progress } : item));
      }
      if (message.type === "complete") {
        updateJob(message.job);
        const resultRunId = message.job?.result?.run_id || runId;
        void refreshRuns(resultRunId);
        if (nextTab && message.job.status === "done") setTab(nextTab);
        socket.close();
      }
      if (message.type === "error") {
        setError(message.message);
      }
    });
  }

  async function runFind() {
    await saveConfig(config);
    const parsedYears = splitList(years).map((x) => Number(x)).filter(Boolean);
    const nextJob = await startFind(config, {
      venue_ids: selectedVenues,
      years: parsedYears.length ? parsedYears : [2026],
      include_arxiv: includeArxiv,
      include_huggingface: includeHf,
      include_github: includeGithub,
    });
    attachJob(nextJob, "read");
  }

  async function runRead() {
    if (!runId) return;
    attachJob(await startRead(runId, selectedPapers), "ideas");
  }

  async function runIdeas() {
    if (!runId) return;
    attachJob(await startIdea(runId, config.max_ideas, config.idea_parallel_workers), "ideas");
  }

  async function runPlan() {
    if (!runId) return;
    attachJob(await startPlan(runId, planIdeaIds, planRepairRounds), "plan");
  }

  async function runPlanPolish(planId: string, versionId: string) {
    if (!runId) return;
    attachJob(await startPlanPolish(runId, planId, versionId, polishRounds[planId] || 1), "plan");
  }

  async function runPlanFinish(planId: string) {
    if (!runId || !window.confirm(t.finishPlanConfirm)) return;
    await finishPlan(runId, planId);
    await loadRun(runId);
  }

  const findResults = useMemo(() => artifacts.find((a) => a.name === "find_results.json")?.content, [artifacts]);
  const screenedRanking = useMemo(() => {
    const ranking = findResults?.screened_ranking;
    const source = Array.isArray(ranking) ? ranking : (findResults?.evaluated_candidates ?? []);
    return [...source]
      .filter((item: any) => Number(item.fit_score || 0) > 6)
      .sort((a: any, b: any) => Number(b.score || 0) - Number(a.score || 0));
  }, [findResults]);
  const sourceStatus = useMemo(() => findResults?.source_status ?? [], [findResults]);
  const ideas = useMemo(() => artifacts.find((a) => a.name === "ideas.json")?.content?.ideas ?? [], [artifacts]);
  const plans = useMemo(() => artifacts.find((a) => a.name === "plans.json")?.content?.plans ?? [], [artifacts]);
  const approvedIdeas = useMemo(() => ideas.filter((idea: any) => idea.status === "approved"), [ideas]);
  const selectedRunArtifacts = useMemo(() => artifacts.filter((a) => a.kind === "markdown"), [artifacts]);
  const t = TEXT[lang];
  const filteredVenues = useMemo(() => {
    const query = venueQuery.trim().toLowerCase();
    if (!query) return venues;
    return venues.filter((venue) => {
      const haystack = [
        venue.name,
        venue.full_name,
        venue.field,
        venue.rank,
        venue.type,
        venue.source,
        venue.classification_source,
      ].join(" ").toLowerCase();
      return haystack.includes(query);
    });
  }, [venues, venueQuery]);
  const selectedVenueItems = useMemo(() => venues.filter((venue) => selectedVenues.includes(venue.id)), [venues, selectedVenues]);
  const availableVenues = useMemo(() => filteredVenues.filter((venue) => !selectedVenues.includes(venue.id)), [filteredVenues, selectedVenues]);
  const currentArtifact = useMemo(() => {
    if (!selectedRunArtifacts.length) return undefined;
    return selectedRunArtifacts.find((artifact) => artifact.name === activeArtifact) || selectedRunArtifacts[0];
  }, [selectedRunArtifacts, activeArtifact]);

  useEffect(() => {
    if (currentArtifact && activeArtifact !== currentArtifact.name) {
      setActiveArtifact(currentArtifact.name);
    }
  }, [currentArtifact, activeArtifact]);

  useEffect(() => {
    const approvedIds = approvedIdeas.map((idea: any) => idea.id);
    setPlanIdeaIds((prev) => {
      const kept = prev.filter((id) => approvedIds.includes(id));
      return kept.length ? kept : approvedIds;
    });
  }, [runId, approvedIdeas]);

  async function setIdeaStatus(ideaId: string, status: "approved" | "deleted" | "pending") {
    if (!runId) return;
    await patchIdea(runId, ideaId, { status });
    await loadRun(runId);
    setPlanIdeaIds((prev) => status === "approved" ? Array.from(new Set([...prev, ideaId])) : prev.filter((id) => id !== ideaId));
  }

  async function editIdea(ideaId: string, field: string, value: string) {
    if (!runId) return;
    await patchIdea(runId, ideaId, { [field]: value });
    await loadRun(runId);
  }

  async function handleDeleteRun(id: string) {
    if (!window.confirm(t.deleteRunConfirm)) return;
    try {
      await deleteRun(id);
      const nextRuns = await getRuns();
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
        }
      }
    } catch (err) {
      setError(String(err));
    }
  }

  async function runVenueHealth() {
    try {
      setCheckingVenues(true);
      setError("");
      const parsedYears = splitList(years).map((x) => Number(x)).filter(Boolean);
      const highPriority = ["ICLR", "NeurIPS", "ICML", "CVPR", "ICCV", "ECCV", "ACL", "EMNLP", "NAACL", "AAAI", "IJCAI"];
      const ids = selectedVenues.length
        ? selectedVenues
        : venues.filter((venue) => highPriority.includes(venue.name)).map((venue) => venue.id);
      const response = await checkVenueHealth({ venue_ids: ids, years: selectedVenues.length && parsedYears.length ? parsedYears : [2023, 2024, 2025], sample_limit: 2 });
      const next: Record<string, { ok: boolean; message: string; source_adapter: string; sample_count: number }> = {};
      for (const result of response.results) {
        const current = next[result.venue_id];
        next[result.venue_id] = {
          ok: Boolean(current?.ok || result.ok),
          message: result.message,
          source_adapter: result.source_adapter,
          sample_count: (current?.sample_count || 0) + result.sample_count,
        };
      }
      setVenueHealth((prev) => ({ ...prev, ...next }));
    } catch (err) {
      setError(String(err));
    } finally {
      setCheckingVenues(false);
    }
  }

  async function runEmail() {
    if (!runId) return;
    try {
      setError("");
      await saveConfig(config);
      const receivers = emailReceiversOverride.trim() ? splitList(emailReceiversOverride) : [];
      const artifactNames = selectedRunArtifacts.map((artifact) => artifact.name);
      const nextJob = await startEmail({
        run_id: runId,
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
    updateJob(await cancelJob(jobId));
  }

  return (
    <main className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="mark">AR</span>
          <div>
            <h1>TASTE</h1>
            <p>127.0.0.1:8765</p>
          </div>
        </div>
        <div className="langSwitch" aria-label="Language">
          <button className={lang === "zh" ? "active" : ""} onClick={() => setLang("zh")}>中文</button>
          <button className={lang === "en" ? "active" : ""} onClick={() => setLang("en")}>EN</button>
        </div>

        <section className="panel">
          <h2>{t.profile}</h2>
          <p className="help">{t.interestHelp}</p>
          <label>{t.interest}</label>
          <textarea value={config.research_interest} onChange={(e) => updateConfig("research_interest", e.target.value)} />
          <label>{t.researcher}</label>
          <p className="help">{t.researcherHelp}</p>
          <textarea value={config.researcher_profile} onChange={(e) => updateConfig("researcher_profile", e.target.value)} />
        </section>

        <section className="panel compact">
          <h2>{t.llm}</h2>
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
          <input value={config.api_key} onChange={(e) => updateConfig("api_key", e.target.value)} placeholder="sk-..." type="password" />
          <label>{t.temperature}</label>
          <p className="help">{t.temperatureHelp}</p>
          <div className="row">
            <input value={config.temperature} onChange={(e) => updateConfig("temperature", Number(e.target.value))} type="number" step="0.1" />
          </div>
          <details className="roleSettings">
            <summary>{t.roleConfig}</summary>
            <p className="help">{t.roleConfigHelp}</p>
            {LLM_ROLES.map(([role, label]) => {
              const roleConfig = (config.llm_roles || {})[role] || {};
              return (
                <div className="roleBox" key={role}>
                  <h4>{label}</h4>
                  <input value={roleConfig.provider || ""} onChange={(e) => updateRoleConfig(role, "provider", e.target.value)} placeholder={t.provider} />
                  <input value={roleConfig.base_url || ""} onChange={(e) => updateRoleConfig(role, "base_url", e.target.value)} placeholder={t.baseUrl} />
                  <input value={roleConfig.model || ""} onChange={(e) => updateRoleConfig(role, "model", e.target.value)} placeholder={t.model} />
                  <input value={roleConfig.api_key || ""} onChange={(e) => updateRoleConfig(role, "api_key", e.target.value)} placeholder={t.apiKey} type="password" />
                  <input value={roleConfig.temperature ?? ""} onChange={(e) => updateRoleConfig(role, "temperature", e.target.value ? Number(e.target.value) : null)} placeholder={t.temperature} type="number" step="0.1" />
                </div>
              );
            })}
          </details>
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
            <input value={config.email.receivers.join(", ")} onChange={(e) => updateEmailConfig("receivers", splitList(e.target.value))} placeholder="receiver@example.com" />
            <label>{t.smtpPassword}</label>
            <input value={config.email.smtp_password} onChange={(e) => updateEmailConfig("smtp_password", e.target.value)} type="password" />
            <label className="switch">
              <input type="checkbox" checked={config.email.manual_enabled} onChange={(e) => updateEmailConfig("manual_enabled", e.target.checked)} />
              {t.sendEmail}
            </label>
            <label className="switch">
              <input type="checkbox" checked={config.email.auto_send_enabled} onChange={(e) => updateEmailConfig("auto_send_enabled", e.target.checked)} />
              {t.autoEmail}
            </label>
            <label>{t.autoEmailStages}</label>
            <input value={config.email.auto_send_stages.join(", ")} onChange={(e) => updateEmailConfig("auto_send_stages", splitList(e.target.value))} placeholder="find, read, idea, plan" />
          </details>
        </section>

        <section className="panel compact">
          <h2>{t.limits}</h2>
          <label>{t.fetchLimit}</label>
          <p className="help">{t.fetchLimitHelp}</p>
          <input value={config.max_fetch_papers} onChange={(e) => updateConfig("max_fetch_papers", Number(e.target.value))} type="number" min="1" />
          <label>{t.recommendLimit}</label>
          <p className="help">{t.recommendLimitHelp}</p>
          <input value={config.max_recommended_papers} onChange={(e) => updateConfig("max_recommended_papers", Number(e.target.value))} type="number" min="1" />
          <label>{t.ideaLimit}</label>
          <p className="help">{t.ideaLimitHelp}</p>
          <input value={config.max_ideas} onChange={(e) => updateConfig("max_ideas", Number(e.target.value))} type="number" min="1" />
          <label>{t.llmConcurrency}</label>
          <p className="help">{t.llmConcurrencyHelp}</p>
          <input value={config.llm_concurrency} onChange={(e) => updateConfig("llm_concurrency", Math.max(1, Math.min(32, Number(e.target.value))))} type="number" min="1" max="32" />
          <label>{t.ideaWorkers}</label>
          <p className="help">{t.ideaWorkersHelp}</p>
          <input value={config.idea_parallel_workers} onChange={(e) => updateConfig("idea_parallel_workers", Math.max(1, Math.min(8, Number(e.target.value))))} type="number" min="1" max="8" />
          <label>{t.titleScanLimit}</label>
          <p className="help">{t.titleScanLimitHelp}</p>
          <input value={config.venue_title_scan_limit} onChange={(e) => updateConfig("venue_title_scan_limit", Number(e.target.value))} type="number" min="1" />
          <label>{t.titleScanFraction}</label>
          <p className="help">{t.titleScanFractionHelp}</p>
          <input value={config.venue_title_scan_fraction} onChange={(e) => updateConfig("venue_title_scan_fraction", Number(e.target.value))} type="number" min="0.01" max="1" step="0.05" />
          <div className="saveBar">
            <button className="primary" onClick={handleSaveConfig} disabled={savingConfig}>
              {savingConfig ? t.saving : t.saveConfig}
            </button>
            {saveMessage && <span>{saveMessage}</span>}
          </div>
          {configPath && <p className="configPath"><strong>{t.configPath}:</strong> {configPath}</p>}
        </section>

        <section className="panel runs">
          <h2>{t.runs}</h2>
          {runs.map((run) => (
            <div className={run.run_id === runId ? "runRow active" : "runRow"} key={run.run_id}>
              <button className="run" onClick={() => loadRun(run.run_id)}>
                <span>{run.run_id}</span>
                <small>{run.stages.join(" / ")}</small>
              </button>
              <button className="danger smallButton" onClick={() => handleDeleteRun(run.run_id)}>{t.deleteRun}</button>
            </div>
          ))}
        </section>
      </aside>

      <section className="workspace">
        <nav className="tabs">
          {(["find", "read", "ideas", "plan"] as Tab[]).map((item) => (
            <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
              {t[item]}
            </button>
          ))}
        </nav>

        {error && <div className="error">{error}</div>}

        {tab === "find" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.find}</h2>
              <div className="toolbarActions">
                <button onClick={runVenueHealth} disabled={checkingVenues}>{checkingVenues ? t.checking : t.checkVenue}</button>
                <button className="primary" onClick={runFind}>{t.runFind}</button>
              </div>
            </div>
            <div className="grid two">
              <div className="panel">
                <h3>{t.venues}</h3>
                <p className="help">{t.venueHelp}</p>
                <label>{t.venueSearch}</label>
                <input value={venueQuery} onChange={(e) => setVenueQuery(e.target.value)} placeholder={t.venueSearch} />
                <label>{t.years}</label>
                <p className="help">{t.yearsHelp}</p>
                <input value={years} onChange={(e) => setYears(e.target.value)} placeholder="2025, 2026" />
                <div className="countLine">{selectedVenues.length} {t.selected} / {Math.min(availableVenues.length, 300)} {t.shown}</div>
                <div className="venuePicker">
                  <div>
                    <h4>{t.selectedVenuesTitle}</h4>
                    <div className="venueList compactList">
                      {selectedVenueItems.map((venue) => {
                        const health = venueHealth[venue.id];
                        return (
                          <div className="venueRow" key={venue.id}>
                            <div>
                              <strong>{venue.name}</strong>
                              <small>{venue.field} / {venue.rank} / {venue.classification_source}</small>
                              {health && (
                                <small className={health.ok ? "health ok" : "health fail"}>
                                  {health.ok ? t.healthOk : t.healthFail} / {health.source_adapter} / {health.sample_count}
                                </small>
                              )}
                            </div>
                            <button className="smallButton" onClick={() => setSelectedVenues((prev) => prev.filter((id) => id !== venue.id))}>{t.remove}</button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                  <div>
                    <h4>{t.availableVenuesTitle}</h4>
                    <div className="venueList">
                      {availableVenues.slice(0, 300).map((venue) => {
                        const health = venueHealth[venue.id];
                        return (
                          <div className="venueRow" key={venue.id}>
                            <div>
                              <strong>{venue.name}</strong>
                              <small>{venue.field} / {venue.rank} / {venue.classification_source}</small>
                              {health && (
                                <small className={health.ok ? "health ok" : "health fail"}>
                                  {health.ok ? t.healthOk : t.healthFail} / {health.source_adapter} / {health.sample_count}
                                </small>
                              )}
                            </div>
                            <button className="smallButton" onClick={() => setSelectedVenues((prev) => prev.includes(venue.id) ? prev : [...prev, venue.id])}>{t.add}</button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>
              <div className="panel">
                <h3>{t.sources}</h3>
                <p className="help">{t.sourcesHelp}</p>
                <label>{t.arxivCategories}</label>
                <p className="help">{t.arxivHelp}</p>
                <input value={config.arxiv_categories.join(", ")} onChange={(e) => updateConfig("arxiv_categories", splitList(e.target.value))} placeholder="cs.AI, cs.CV" />
                <p className="help">{t.arxivDateHelp}</p>
                <div className="row">
                  <input value={config.arxiv_start_date} onChange={(e) => updateConfig("arxiv_start_date", e.target.value)} placeholder={t.startDate} />
                  <input value={config.arxiv_end_date} onChange={(e) => updateConfig("arxiv_end_date", e.target.value)} placeholder={t.endDate} />
                </div>
                <label>{t.githubLanguages}</label>
                <p className="help">{t.githubLanguagesHelp}</p>
                <input value={config.github_languages.join(", ")} onChange={(e) => updateConfig("github_languages", splitList(e.target.value))} placeholder="all, python" />
                <label className="switch"><input type="checkbox" checked={includeArxiv} onChange={(e) => setIncludeArxiv(e.target.checked)} /> arXiv</label>
                <label className="switch"><input type="checkbox" checked={includeHf} onChange={(e) => setIncludeHf(e.target.checked)} /> HuggingFace</label>
                <label className="switch"><input type="checkbox" checked={includeGithub} onChange={(e) => setIncludeGithub(e.target.checked)} /> GitHub</label>
                {sourceStatus.length > 0 && (
                  <div className="sourceStatus">
                    <h4>{t.sourceStatus}</h4>
                    {sourceStatus.map((item: any) => (
                      <div className={item.ok ? "sourceRow ok" : "sourceRow fail"} key={item.source}>
                        <span>{item.source}</span>
                        <small>{item.limited ? "limited" : item.ok ? "ok" : "failed"} / {item.count} / {item.message}</small>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <details className="rankingPanel">
              <summary>{t.screenedRanking} ({screenedRanking.length})</summary>
              <p className="help">{t.screenedRankingHelp}</p>
              {screenedRanking.length === 0 ? (
                <div className="emptyState">{t.noRanking}</div>
              ) : (
                <div className="rankingList">
                  {screenedRanking.map((paper: any, index: number) => (
                    <article className="rankingItem" key={`${paper.id || paper.title}-${index}`}>
                      <div className="rankingHeader">
                        <strong>#{index + 1} {paper.title || "Untitled"}</strong>
                        <span>{paper.score}</span>
                      </div>
                      <div className="scoreGrid">
                        <span>Fit: {paper.fit_score}</span>
                        <span>Diversity: {paper.diversity_score}</span>
                        <span>{paper.venue} {paper.year}</span>
                      </div>
                      <p><strong>Hit:</strong> {Array.isArray(paper.hit_directions) ? paper.hit_directions.join(", ") : paper.hit_directions}</p>
                      <p><strong>Fit:</strong> {paper.fit_explanation || ""}</p>
                      <p>{paper.reason || ""}</p>
                      <div className="actions">
                        {paper.url && <a href={paper.url} target="_blank" rel="noreferrer">URL</a>}
                        {paper.pdf_url && <a href={paper.pdf_url} target="_blank" rel="noreferrer">PDF</a>}
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </details>
          </section>
        )}

        {tab === "read" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.read}</h2>
              <button className="primary" onClick={runRead} disabled={!runId}>{t.runRead}</button>
            </div>
            <div className="panel">
              {(findResults?.articles ?? []).map((paper: any) => (
                <label className="check paper" key={paper.id}>
                  <input
                    type="checkbox"
                    checked={selectedPapers.includes(paper.id)}
                    onChange={(e) => setSelectedPapers((prev) => e.target.checked ? [...prev, paper.id] : prev.filter((id) => id !== paper.id))}
                  />
                  <span>{paper.title}</span>
                  <small>{paper.venue} / {paper.category} / {paper.classification_source}</small>
                </label>
              ))}
            </div>
          </section>
        )}

        {tab === "ideas" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.ideas}</h2>
              <button className="primary" onClick={runIdeas} disabled={!runId}>{t.runIdeas}</button>
            </div>
            <div className="ideaGrid">
              {ideas.map((idea: any) => (
                <article className={`idea ${idea.status}`} key={idea.id}>
                  <input className="ideaTitle" value={idea.title} onChange={(e) => editIdea(idea.id, "title", e.target.value)} />
                  <textarea value={idea.hypothesis} onChange={(e) => editIdea(idea.id, "hypothesis", e.target.value)} />
                  <textarea value={idea.min_experiment} onChange={(e) => editIdea(idea.id, "min_experiment", e.target.value)} />
                  <div className="ideaMeta">{idea.novelty} / {idea.feasibility} / {idea.score}</div>
                  <div className="actions">
                    <button onClick={() => setIdeaStatus(idea.id, "approved")}>{t.approve}</button>
                    <button onClick={() => setIdeaStatus(idea.id, "pending")}>{t.pending}</button>
                    <button onClick={() => setIdeaStatus(idea.id, "deleted")}>{t.delete}</button>
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        {tab === "plan" && (
          <section className="stage">
            <div className="toolbar">
              <h2>{t.plan}</h2>
              <button className="primary" onClick={runPlan} disabled={!runId || !planIdeaIds.length}>{t.runPlan}</button>
            </div>
            <div className="panel">
              <label>{t.repairRounds}</label>
              <p className="help">{t.repairRoundsHelp}</p>
              <input value={planRepairRounds} onChange={(e) => setPlanRepairRounds(Math.max(1, Number(e.target.value)))} type="number" min="1" />
              {approvedIdeas.length === 0 ? (
                <div className="emptyState">{t.noApprovedIdeas}</div>
              ) : (
                <>
                  <div className="actions">
                    <button onClick={() => setPlanIdeaIds(approvedIdeas.map((idea: any) => idea.id))}>{t.selectAll}</button>
                    <button onClick={() => setPlanIdeaIds([])}>{t.clearAll}</button>
                  </div>
                  <div className="ideaGrid">
                    {approvedIdeas.map((idea: any) => (
                      <label className="check paper" key={idea.id}>
                        <input
                          type="checkbox"
                          checked={planIdeaIds.includes(idea.id)}
                          onChange={(e) => setPlanIdeaIds((prev) => e.target.checked ? [...prev, idea.id] : prev.filter((id) => id !== idea.id))}
                        />
                        <span>{idea.title}</span>
                        <small>{idea.hypothesis}</small>
                      </label>
                    ))}
                  </div>
                </>
              )}
            </div>
            {plans.length > 0 && (
              <div className="ideaGrid">
                {plans.map((plan: any) => {
                  const versions = plan.versions || [];
                  const latest = versions[versions.length - 1] || {};
                  return (
                    <article className="idea" key={plan.plan_id || plan.idea_id}>
                      <h3>{plan.title}</h3>
                      <div className="ideaMeta">
                        {plan.plan_id} / {latest.version_id} / {versions.length} versions
                        {plan.completed ? ` / ${t.planCompleted}` : ""}
                      </div>
                      <label>{t.polishRounds}</label>
                      <input
                        value={polishRounds[plan.plan_id] || 1}
                        onChange={(e) => setPolishRounds((prev) => ({ ...prev, [plan.plan_id]: Math.max(1, Number(e.target.value)) }))}
                        type="number"
                        min="1"
                      />
                      <div className="actions">
                        <button onClick={() => runPlanPolish(plan.plan_id, latest.version_id)} disabled={!plan.plan_id || !latest.version_id}>
                          {t.polishFurther}
                        </button>
                        <button className={plan.completed ? "" : "primary"} onClick={() => runPlanFinish(plan.plan_id)} disabled={!plan.plan_id || plan.completed}>
                          {plan.completed ? t.planCompleted : t.finishPlan}
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        )}

        <section className="bottom">
          <div className="panel logPanel">
            <h2>{t.job}</h2>
            {jobs.length === 0 ? (
              <div className="status">{t.idle}</div>
            ) : (
              <div className="jobList">
                {jobs.map((item) => (
                  <article className="jobCard" key={item.job_id}>
                    <div className="jobHeader">
                      <strong>{item.stage}</strong>
                      <span>{item.status}</span>
                    </div>
                    <small>{item.job_id}</small>
                    {["queued", "running", "cancelling"].includes(item.status) && (
                      <button className="danger smallButton" onClick={() => stopJob(item.job_id)} disabled={item.status === "cancelling"}>
                        {t.stop}
                      </button>
                    )}
                    {item.progress && (
                      <div className="progressBlock">
                        <div className="progressMeta">
                          <span>{item.progress.message}</span>
                          <strong>{item.progress.percent}%</strong>
                        </div>
                        <progress value={item.progress.percent} max="100" />
                        <small>{item.progress.phase} / {item.progress.current} / {item.progress.total}</small>
                      </div>
                    )}
                    <pre>{item.logs.join("\n")}</pre>
                  </article>
                ))}
              </div>
            )}
          </div>
          <div className="panel artifactPanel">
            <h2>{t.artifacts}</h2>
            <p className="help">{t.artifactHelp}</p>
            {selectedRunArtifacts.length > 0 && (
              <>
                <div className="emailBox">
                  <input value={emailSubject} onChange={(e) => setEmailSubject(e.target.value)} placeholder={t.emailSubject} />
                  <input value={emailReceiversOverride} onChange={(e) => setEmailReceiversOverride(e.target.value)} placeholder={t.emailReceivers} />
                  <button className="primary" onClick={runEmail} disabled={!runId || !config.email.manual_enabled}>{t.sendEmail}</button>
                </div>
                <div className="artifactTabs">
                  {selectedRunArtifacts.map((artifact) => (
                    <button key={artifact.name} className={currentArtifact?.name === artifact.name ? "active" : ""} onClick={() => setActiveArtifact(artifact.name)}>
                      {artifact.name}
                    </button>
                  ))}
                </div>
                {currentArtifact && (
                  <div className="artifactView">
                    {currentArtifact.path && (
                      <p className="artifactPath"><strong>{t.artifactPath}:</strong> {currentArtifact.path}</p>
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
                      <pre>{currentArtifact.content}</pre>
                    ) : (
                      <div className="markdownBody" dangerouslySetInnerHTML={{ __html: markdownToHtml(String(currentArtifact.content || "")) }} />
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </section>
    </main>
  );
}

export default App;
