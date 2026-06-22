# Finding / Find 模块

TASTE 七阶段中 `Find` 阶段的**独立后端模块**。框架可通过网页/任务队列调用它，但模块自身只依赖
**显式输入**与**外部维护的产物**，不隐式读取其它阶段的历史状态来替代输入。

> 本 README 描述稳定的对外口径与用法。若本机存在被 `.gitignore` 忽略的 `工作状态.txt`，只能作为维护交接背景，
> 不能作为仓库契约、项目科研记忆或提交内容。

---

## 1. 职责边界

契约原文：*Collect, filter, score, and rank literature/tool candidates from the research topic and profile.
Full-text reading evidence is explicitly outside this module.*

从「研究主题 + 研究兴趣 + 研究者画像 + 来源选择」出发，抓取候选文献/代码线索，完成
标题池 → 分类 → TF-IDF/关键词初筛 → LLM 标题评分 → 抓摘要/详情做摘要评分 → 程序性加分 →
最终推荐排序。**Find 不做全文精读**，也不把全文可用性当作前置门槛（全文证据属于 `reading` 模块）。

---

## 2. 两条发现路径（维护第一要务：先分清楚）

`scripts/` 里有两条**互不调用**的发现路径，看错会得出错误结论：

- **路径 A —— 真正的 Find 流水线（主线）**
  `main.py --action find` → `find_pipeline.run_find(FindRequest(config, selection))`，
  用 `find_support.py` 的真实 `fetch_*` 适配器抓数，按 `VenueSelection.include_*` 开关取数。
  这是与网页 Find、与本模块"独立可用"对应的路径。

- **路径 B —— 项目耦合的 discover 脚本（旧/并行线）**
  `discover_arxiv.py` / `discover_semantic_scholar.py` / `discover_github_repos.py` / `ingest_discovery.py`，
  需要 `--project`，把结果写进 `projects/<id>/...`，只服务项目工作流（被 `run_project.py`、`environment` 调用），
  **`find_pipeline` 不调用它们**。
  ⚠️ 只看 `discover_*.py` 会误以为 bioRxiv/HuggingFace 没实现——它们在路径 A 的 `find_support.py` 里。

---

## 3. 输入口径

`main.py --action find` 接受两个 JSON 文件参数：

| 参数 | 对应类型 | 含义 |
| --- | --- | --- |
| `--config-json`    | `auto_research.models.AppConfig`     | LLM 配置 + 研究主题/兴趣/画像 + 各类策略（含 `VenueMetadataPolicy` 的起止日期等） |
| `--selection-json` | `auto_research.models.VenueSelection`| 来源选择：开哪些源、哪些会议、哪些年份 |
| `--output-dir`     | 路径（可选）                          | 额外把产物拷贝到这个目录（框架/项目调用时用） |

`VenueSelection` 关键字段（缺省全部为关 / 空，调用方必须显式打开要抓的源）：

```jsonc
{
  "venue_ids": ["iclr", "neurips"],          // 要抓的会议/期刊 id（对应 data/ccf_venues.json 等）
  "years": [2025, 2026],                      // 年份
  "venue_years": [{"venue_id":"iclr","year":2026}], // 或精确到 venue×year（给了它就覆盖上面两项的叉乘）
  "include_arxiv": true,
  "include_biorxiv": false,
  "include_huggingface": false,
  "include_github": false,
  "include_nature": false,
  "include_science": false
}
```

时间窗：会议按 `year` 整数（含"最近已发布年份"回填）；预印本/期刊按 `AppConfig` 里
`VenueMetadataPolicy` 的 `arxiv/biorxiv/nature/science_start_date/end_date`（arXiv 缺省近 180 天、
bioRxiv 缺省近 30 天）；GitHub 用 `github_since`（daily/weekly/monthly）。
已知弱点：HuggingFace papers 流不支持日期窗；medRxiv/PubMed 未实现。

---

## 4. 输出口径

独立运行 `modules/finding/main.py --action find` 时，主线产物默认写到模块私有运行目录
`modules/finding/.runtime/runs/<run_id>/`（被 `modules/finding/.gitignore` 忽略），`--output-dir` 会再额外拷一份。
如果外部框架/网页调用需要共享运行根，调用方应显式设置 `WORKFLOW_RUNTIME_DIR`；Finding 入口不会覆盖这个显式设置。

| 产物 | 作用 |
| --- | --- |
| `find_results.json` | 结构化推荐、来源状态、阶段计数、分数与调试信息（核心产物） |
| `article.md` | 用户可读的 Find 产物 |
| `source_status.md` | 各来源健康/分类统计 |
| `find_progress.json` / `*_report.json` | 分类扫描、标题筛选、评分等阶段报告 |

> **中间产物落点（现状）**：路径 A standalone run、stage latest、state/cache/local_database 默认都在
> `modules/finding/.runtime/` 下；这一路径被模块级 `.gitignore` 忽略。路径 B 的 `discover_*`/`ingest_discovery`
> 仍是项目耦合旧线，会按其 `--project` 语义写 `projects/<id>/...`，不属于主线 `find_pipeline`。

---

## 5. 运行逻辑（路径 A，run_find 的主流程）

1. 读取/规范化来源选择与画像；会议优先用带官方分类/track/类型信息的渠道（OpenReview 等），缺官方分类才退到标题池。
2. 聚合各源候选，构建标题总池与分类后池；TF-IDF/关键词召回控制候选规模。
3. 对更多标题做 LLM 标题评分，再抓摘要/详情做摘要评分。
4. 把 LLM 分、venue/track 类型、稳定性、多样性等程序性信号合成最终推荐分并排序。
5. 只发布"有真实标题 + 真实摘要证据"的用户可见推荐；内部 reader 线索保留为结构化字段，不混进面向用户文案。

---

## 6. 独立（单机）使用方法

```bash
# 全部在远程：ssh hidimension_5090_1，工作根 <TASTE_ROOT>
PY=/home/fmh/workspace/miniforge/envs/ar_taste/bin/python3.11   # 正确 python（系统 python3 缺依赖）

# 零依赖冒烟：打印契约
$PY modules/finding/main.py --contract

# 跑一次 Find（默认产物：modules/finding/.runtime/runs/<run_id>/；--output-dir 只是额外拷贝）
WORKSPACE_ROOT=<TASTE_ROOT> \
$PY modules/finding/main.py --action find \
    --config-json modules/finding/.tmp/config.json \
    --selection-json modules/finding/.tmp/selection.json \
    --output-dir modules/finding/.tmp/finding_out

# 外部框架若确实要共享运行根，可显式覆盖；standalone 不建议这么做
WORKFLOW_RUNTIME_DIR=<TASTE_ROOT>/runtime \
$PY modules/finding/main.py --action find --config-json ... --selection-json ...
```

LLM 配置走环境变量或 `AppConfig` 字段（key 绝不入库）：
`LLM_PROVIDER` / `LLM_API_BASE` / `LLM_MODEL` / `OPENAI_API_KEY` / `LLM_TIMEOUT_SEC` / `LLM_MAX_TOKENS`。

> 运营约定（见根 README/AGENTS）：端到端验证走网页 UI，CLI 只用于调试/测试/查状态；
> 验证改动用 `pytest tests/`（ar_taste python）。

---

## 7. 统一入口与 action 路由

`main.py` 是本模块**唯一公开后端入口**，`--contract` 输出输入/产物/职责边界。`scripts/` 是私有后端，
需要暴露的能力先在 `main.py` 注册 action。当前 action（节选）：

- `find`（缺省）→ 路径 A 主流水线
- `plan_literature` / `paper_quality` / `literature_base_candidates` → `finding_quality_tools.py`
- `discover_arxiv` / `discover_semantic_scholar` / `discover_github` / `ingest_discovery` → 路径 B
- `venue_metadata_cache` / `openreview_cache` / `local_database` → 缓存构建
- `literature_tool` / `tool_packet` / `literature_base_audit` → 工具包与基底审计

---

## 8. 脚本清单（与 `script_manifest.json` 保持一致）

### 核心流程
| 脚本 | 真实作用 |
| --- | --- |
| `scripts/find_pipeline.py` | Find 主流水线（唯一对外符号 `run_find`）：来源聚合、阶段计数、标题/摘要评分、推荐排序、产物写出。 |
| `scripts/find_support.py` | 兼容门面 + sources 主体：保留会议/期刊/预印本适配器、摘要/链接抓取、来源状态构造，以及对已拆分实现的 re-export；`requests`/`fetch_*` monkeypatch 面保持不变。 |
| `scripts/find_local_rank.py` | 兼容 wrapper：对外导出 `rank_papers_tfidf`，实际实现位于 `scripts/ranking/local_rank.py`，供主流水线和测试仍通过 `find_support.rank_papers_tfidf` 使用。 |
| `scripts/catalog/venue_catalog.py` | Venue catalog 实现：加载 packaged/default/custom/OpenReview/CCF catalog，合并别名与年份，输出 `load_catalog`/`catalog_by_id`。 |
| `scripts/selection/category_select.py` | Category selection 实现：基于研究兴趣/画像、LLM 或 deterministic fallback 从本地 category summary 中挑相关类别，并维护项目级选择缓存。 |
| `scripts/research_profile/normalize.py` | Stage 0 研究画像规范化：约束 LLM 输出 schema，生成安全扩展词、硬排除/条件排除、检索文本和 fallback profile。 |
| `scripts/quality/metadata.py` | 会议/期刊质量元数据查表与附加：匹配 venue、track、presentation label，给候选补充 quality tier 与可用 bonus。 |
| `scripts/ranking/local_rank.py` | 研究画像驱动的本地 TF-IDF/关键词召回排名实现：从研究主题/兴趣/画像中抽取信号，按全局预算和类别平衡生成召回候选。 |
| `scripts/sources/common.py` | Sources 纯工具层：稳定 id、文本清理、presentation metadata、标题 key、日期规范化、venue family/OpenReview 支持判断；不做网络请求。 |
| `scripts/sources/audit.py` | Sources 元数据审计层：venue metadata audit 结构、OpenReview/venue 审计附加与合并、metadata timeout；不做网络请求，供 venue/source 适配器与 pipeline 复用。 |
| `scripts/sources/metadata.py` | Sources 元数据解析层：Semantic Scholar cache 判定、DOI/ACM URL、DBLP record metadata、OpenAlex 候选匹配/摘要/PDF helper；不做网络请求、不读写 cache。 |
| `scripts/sources/source_choice.py` | Venue source 选择层：统计官方分类覆盖、构造 source audit、在 OpenReview/ICML/PMLR/DBLP 等候选之间选择优先源；不做网络请求。 |
| `scripts/sources/parsing.py` | Sources HTML/JSON-LD 解析层：标题过滤、NeurIPS virtual 详情/列表解析、conference virtual 摘要/作者解析、detail target/defer helper；不做网络请求。 |
| `scripts/sources/journals.py` | Nature/Science 期刊族纯实现层：期刊清单、feed/listing/Crossref URL 构造、RSS/HTML/Crossref 解析、DOI/摘要 helper；不做网络请求。 |
| `scripts/sources/preprints.py` | arXiv/bioRxiv 预印本纯实现层：日期窗、查询构造、title-match 查询、entry 作者/id 提取、paper 组装和 bioRxiv URL/category helper；不做网络请求。 |
| `scripts/sources/conferences.py` | 会议来源纯实现层：内置 ICLR 参考读取、DBLP URL/stream/authors/payload helper、OpenReview content helper、ICML/PMLR/ACL 标题/URL/摘要 helper、DBLP/adapter enrichment merge；不做网络请求。 |
| `scripts/sources/venue_cache.py` | Venue/year 可信缓存读取层：从 Finding runtime/latest/显式项目 run 中寻找已验证 venue-year 结果，给 ICML/DBLP fallback 复用；只读缓存，不做网络请求、不写产物。 |
| `scripts/literature_policy.py` | 路径 B 与质量工具共享的来源/筛选策略常量与轻量打分规则。 |

### 来源抓取与缓存
| 脚本 | 真实作用 |
| --- | --- |
| `scripts/build_venue_metadata_cache.py` | 为指定 venue/year 构建本地会议元数据缓存（优先保留官方分类/track/presentation 类型）。 |
| `scripts/local_store/local_index.py` | 本地 venue/year 索引读取：生成 venue cache key、兼容 OpenReview/年份后缀别名，从本地数据库加载 papers/summary/manifest。 |
| `scripts/local_store/local_cache.py` | 本地 venue/year 缓存写入与读取：规范化 paper、生成 category summary/source report，只写入调用者给定的本地数据库根。 |
| `scripts/build_openreview_cache.py` | 构建 OpenReview（ICLR/NeurIPS）论文索引缓存。 |
| `scripts/build_category_summary.py` | 为本地 venue/year JSON 生成中性分类摘要，供分类选择/状态展示。 |
| `scripts/update_local_database.py` | 刷新 TASTE 本地 venue 索引与集成数据库。 |
| `scripts/discover_arxiv.py` | 路径 B：arXiv 候选抓取（写 `projects/<id>/discover`）。 |
| `scripts/discover_semantic_scholar.py` | 路径 B：Semantic Scholar 候选抓取。 |
| `scripts/discover_github_repos.py` | 路径 B：GitHub 仓库候选抓取。 |
| `scripts/ingest_discovery.py` | 路径 B：把 discover 结果归一化、打分、入库到项目候选池/raw 论文。 |

### 计划、工具包与基底审计
| 脚本 | 真实作用 |
| --- | --- |
| `scripts/finding_quality_tools.py` | 合并后的文献计划/论文质量审计/文献基底候选审计；用 `--tool-action plan_literature/paper_quality/literature_base_candidates` 选子功能。 |
| `scripts/build_literature_tool_packet.py` | 把 Find 结果压缩成 Claude Code 可调用的 literature packet。 |
| `scripts/run_literature_tool.py` | 给项目代理用的 literature tool 包装入口（内部调用 Find 并刷新 packet）。 |
| `scripts/run_literature_base_audit.py` | 把 Find 候选送入 repo/data/env 前置审计，防止不可执行基底进入后续阶段。 |

---

## 9. 冗余控制原则（防屎山）

- `find_pipeline.py` / `find_support.py` 是核心大块；拆/合只围绕「来源适配器、评分器、产物渲染器」三类边界，
  且必须保住对外公共面（`run_find`、`find_support` 的被消费符号 + tests 的 `requests` monkeypatch）。
  `find_support.py` 的干净参考版在 `third_party/reference_TASTE_latest/auto_research/auto_find/`，拆分=反拼接；当前已抽出 `catalog/`、`local_store/`、`quality/`、`selection/`、`research_profile/`、`ranking/`、`sources/` 实现目录，并用 `find_support.py`（以及 `find_local_rank.py` 兼容 wrapper）保持旧公共面；sources 抓取段仍留在门面内，避免破坏 tests 对 `requests`/`fetch_*` 的 monkeypatch。
- `discover_*` 是不同外部 API 的薄适配器；共同逻辑应进 `find_support`/统一 source adapter 层，不再新增零散 helper。
- 修改本模块必须先读相关脚本、`script_manifest.json` 与本 README，找到根因再改；本机忽略的维护记录只作辅助背景；
  禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
- **任何结构改动（脚本分子目录、巨型文件拆分、产物落点迁移）都受框架"模块边界契约"约束**
  （`tests/test_module_boundaries.py` 强制扁平脚本 + 文件名归属注册表 + manifest 精确匹配）。
  动结构前务必重新核对框架模块边界契约、manifest 与测试，并让全部测试保持绿。
