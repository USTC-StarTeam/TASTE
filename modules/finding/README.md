# Finding 用户手册

Finding 是 TASTE 的论文发现模块。它根据研究主题、研究兴趣、研究者画像和来源选择，抓取论文元数据，筛选候选，调用 LLM 评分，输出一份可读的推荐清单。

请通过 `modules/finding/main.py` 使用本模块。所有 Python 命令都使用 conda `taste` 环境运行。

## 快速开始

### 网页使用

从 TASTE 仓库根目录启动网页：

```bash
CONDA_ENV_NAME=taste framework/scripts/start_web.sh
```

浏览器打开：

```text
http://127.0.0.1:8879
```

常用流程：

1. 选择项目。
2. 填写研究主题、研究兴趣和研究者画像。
3. 保存 LLM 配置。
4. 在 Finding 页面选择会议、年份、补充来源和最低推荐数量。
5. 点击 Find。
6. 在结果区域打开 `find.md`。

`find.md` 是用户阅读的最终推荐列表。网页会直接渲染这个 Markdown 文件。

网页中的 Find 配置和命令行配置使用同一套字段。模块默认配置在：

```text
modules/finding/config/find.config.json
```

使用 TASTE 项目运行时，网页会为当前项目保存一份配置副本：

```text
projects/<project>/config/finding.json
```

LLM API 配置只保存到：

```text
modules/finding/config/llm.local.json
```

### 命令行使用

从 TASTE 仓库根目录运行：

```bash
conda run -n taste python modules/finding/main.py --action find \
  --config-json modules/finding/config/find.config.json \
  --input-json path/to/input.json
```

也可以进入模块目录运行：

```bash
cd modules/finding
conda run -n taste python main.py --action find \
  --config-json config/find.config.json \
  --input-json path/to/input.json
```

查看公开入口合同：

```bash
conda run -n taste python modules/finding/main.py --contract
```

查看可选会议目录：

```bash
conda run -n taste python modules/finding/main.py --action catalog
```

## Finding 能做什么

Finding 提供这些功能：

- 按研究主题查找相关论文。
- 从会议、arXiv、bioRxiv、Nature、Science、HuggingFace、GitHub 等来源收集候选。
- 优先使用会议官方论文列表、官方详情页和可验证摘要来源。
- 记录会议展示类型，例如 Oral、Spotlight、Poster。
- 用 LLM 对标题、摘要和研究匹配度评分。
- 按相关性、多样性和来源质量生成推荐排序。
- 输出 `find.md`、`find_results.json`、`source_status.md`、`find_progress.json`。

Finding 负责发现和推荐。全文精读、想法生成、计划选择、实验和论文写作由 TASTE 后续阶段处理。

## 配置和输入

Finding 使用三类文件：

- `config/find.config.json`：推荐数量、来源参数、会议、年份和渠道开关。
- `input.json`：本次运行的研究主题、研究兴趣、研究者画像和检索查询。
- `config/llm.local.json`：本机 LLM API 配置。

### find.config.json 示例

```json
{
  "schema_version": 1,
  "config": {
    "max_recommended_papers": 10,
    "nonvenue_fetch_limit": 5000,
    "title_abstract_scoring_limit": 1000,
    "llm_concurrency": 8,
    "arxiv_categories": [],
    "biorxiv_categories": [],
    "github_languages": ["python", "all"],
    "github_since": "monthly"
  },
  "selection": {
    "venue_ids": ["openreview_iclr_2026", "openreview_neurips", "dblp_icml"],
    "years": [2026],
    "venue_years": [
      {"venue_id": "openreview_iclr_2026", "year": 2026},
      {"venue_id": "openreview_neurips", "year": 2026},
      {"venue_id": "dblp_icml", "year": 2026}
    ],
    "include_arxiv": true,
    "include_biorxiv": false,
    "include_huggingface": false,
    "include_github": false,
    "include_nature": false,
    "include_science": false
  }
}
```

`arxiv_categories` 和 `biorxiv_categories` 仅在用户显式配置时作为分类约束，留空就是不按分类过滤。Find LLM 直接根据研究主题、研究兴趣和研究者画像抽取 1-3 个单词组成的平级英文关键词，并且必须包含领域、对象、方法和训练方式等基础概念本身；arXiv 与 bioRxiv 都在标题和摘要中按 OR 语义检索，并把最终关键词及显式分类写入当前 run 的 `intermediate/search_terms.json`。对有真实主题分类的每个出版渠道年份，LLM 使用分类名、样例标题和关键词一次性返回全部分类的严格排序，以及相关/有用分类前缀的唯一截止位。代码完整保留该前缀；只有前缀内论文不足 1000 篇时，才沿同一排序继续加入后续分类，累计首次达到至少 1000 篇后停止。相关前缀本身超过 1000 篇时不裁掉其中任何分类，全部分类总量不足 1000 篇时则全部保留。LLM 输出不完整或包含额外字段会明确报错，不切换本地排序。Poster/Oral、Main/Datasets、Full/Short/Demo、议程 session 和编号卷期只作为 track 审计信息，不作为主题分类裁剪论文。没有真实主题分类的出版渠道直接使用完整标题池。`nonvenue_fetch_limit` 是每个 arXiv/bioRxiv 来源的抓取上限，默认 5000；命中更多时按发表时间保留最近论文。bioRxiv 的 OpenAlex 后备检索可从本机环境变量 `OPENALEX_API_KEY` 读取免费 API key，密钥不会写入 run 产物。

`title_abstract_scoring_limit` 是全局最终评分上限。所有完成标题 LLM 评分的候选去重并按标题分排序后，最多取该数量抓取摘要/详情并进入最终标题+摘要 LLM 评分；默认值为 1000。

`max_recommended_papers` 是兼容保留的字段名，当前语义是最低推荐数量。最终目标 N 取该配置值与“每个已选来源 5 篇”的较大者；Find 对有真实摘要、已完成最终 LLM 评分且去重后的候选做全局排序，直接展示前 N，不使用主题证据或固定分数阈值减少结果数。

### input.json 示例

```json
{
  "research_topic": "protein generation with diffusion models",
  "research_interest": "methods that improve controllability, diversity, and experimental feasibility for protein design",
  "researcher_profile": "Machine learning researcher working on generative models for protein design.",
  "arxiv_queries": [
    "protein generation diffusion model controllability",
    "protein design generative model benchmark"
  ]
}
```

### LLM 配置

LLM key 放在本机私有文件：

```text
modules/finding/config/llm.local.json
```

示例：

```json
{
  "provider": "openai_compatible",
  "base_url": "https://example-compatible-endpoint/v1",
  "model": "model-name",
  "api_key": "",
  "temperature": 0.2
}
```

网页保存或验证 LLM 时会创建或更新这个文件。`config/llm.local.json` 已被 git 忽略。

## 支持的来源

### 重点会议

Finding 对这些会议提供稳定支持：

| 会议 | 常用 venue id | 说明 |
| --- | --- | --- |
| NeurIPS / NIPS | `openreview_neurips` | 使用 NeurIPS 官方论文索引和 virtual 页面；展示类型会合并 Oral、Spotlight、Poster。 |
| ICLR | `openreview_iclr` | 使用 OpenReview 官方元数据。 |
| ICML | `dblp_icml` | 优先使用官方 virtual / proceedings 元数据。 |
| SIGKDD / KDD | `dblp_kdd` | 使用官方来源和可验证摘要补全来源。 |
| WWW | `dblp_www` | 使用官方 accepted/proceedings 线索和可验证摘要补全来源。 |
| SIGIR | `dblp_sigir` | 使用官方 accepted/proceedings 线索和可验证摘要补全来源。 |
| CIKM | `dblp_cikm` | 使用官方 proceedings 页面。 |
| AAAI | `dblp_aaai` | 使用 AAAI 官方 OJS 页面。 |
| ICCV | `dblp_iccv` | 使用 CVF Open Access。 |
| CVPR | `dblp_cvpr` | 使用 CVF Open Access。 |
| ACL | `dblp_acl` | 使用 ACL Anthology。 |
| IJCAI | `dblp_ijcai` | 使用 IJCAI 官方 proceedings。 |
| ECCV | `dblp_eccv` | 使用 ECVA / ECCV official 页面。 |
| EMNLP | `dblp_emnlp` | 使用 ACL Anthology。 |

也可以通过 `--action catalog` 查看更多 CCF、DBLP 和自定义会议条目。

### 补充来源

可按需要打开：

- `include_arxiv`
- `include_biorxiv`
- `include_huggingface`
- `include_github`
- `include_nature`
- `include_science`

建议先选择核心会议和年份，再按研究主题补充 arXiv 或 bioRxiv。

## Finding 如何生成推荐

Finding 的推荐过程分为六步：

1. 读取研究主题、研究兴趣和来源选择。
2. 抓取所选出版渠道的论文列表、摘要、作者、年份、链接和展示类型。
3. 合并可验证补全来源，例如 DOI、OpenAlex、Semantic Scholar、官方 PDF 或官方详情页。
4. 用本地召回先筛出主题相关候选。
5. 用 LLM 对标题和摘要做相关性评分。
6. 综合相关性、多样性、会议质量和展示类型，生成推荐列表。

展示类型在元数据阶段进入论文记录。推荐结果中会看到类似：

```text
ICML 2026 / Spotlight
NeurIPS 2025 / Poster
ICLR 2026 / Oral
```

展示类型加分规则：

| 类型 | 加分 |
| --- | ---: |
| Best Paper / Award | +0.50 |
| Oral | +0.45 |
| Spotlight / Highlight / Notable / Top-5% | +0.20 |
| Poster | +0.00 |

展示类型只在论文已经和研究主题有明确关系时影响排序。

## 输出文件怎么看

Finding run 默认写入：

```text
modules/finding/.runtime/runs/<run_id>/
```

最常用文件：

| 文件 | 用途 |
| --- | --- |
| `find.md` | 用户阅读的最终推荐列表。 |
| `find_results.json` | 机器可读结果，推荐池字段为 `strong_recommendations`。 |
| `source_status.md` | 来源状态摘要，显示每个来源的抓取情况。 |
| `find_progress.json` | 运行进度和计数。 |

`find.md` 是人工查看结果的首选文件。其它文件主要用于网页、项目同步和后续阶段。

`find.md` 中每篇论文通常包含：

- 标题
- 会议/年份/展示类型
- 分数
- 推荐理由
- 摘要
- 链接

## 常用命令

运行 Find：

```bash
conda run -n taste python modules/finding/main.py --action find \
  --config-json modules/finding/config/find.config.json \
  --input-json path/to/input.json
```

查看可用会议：

```bash
conda run -n taste python modules/finding/main.py --action catalog
```

刷新某个 run 的来源状态：

```bash
conda run -n taste python modules/finding/main.py --action refresh_source_health \
  --run-dir modules/finding/.runtime/runs/<run_id>
```

检查重点会议元数据覆盖：

```bash
conda run -n taste python modules/finding/main.py --action priority_venue_metadata_audit \
  --year 2026 \
  --max-backfill-years 3
```

在确认结果可复用后写入本地缓存：

```bash
conda run -n taste python modules/finding/main.py --action priority_venue_metadata_audit \
  --year 2026 \
  --max-backfill-years 3 \
  --write-cache
```

## 常见问题

### 没有推荐结果

优先检查：

- LLM provider、base URL、model、API key 是否可用。
- `find.config.json` 是否选了会议和年份。
- `include_arxiv` 等补充来源是否按需要开启。
- `source_status.md` 是否显示来源受限、403、超时或没有摘要。

### NeurIPS / ICLR / ICML 没有 Oral 或 Spotlight

检查来源年份是否已经公开展示类型页面。Finding 会在抓元数据时合并官方展示类型；官方页面还未发布时，结果中只显示可验证字段。

### `find.md` 和 JSON 信息有差异

人工阅读以 `find.md` 为准。`find_results.json` 提供结构化字段给网页和后续阶段使用。

### 会议年份还没公开

Finding 会优先使用请求年份。请求年份没有完整公开元数据时，会使用最近可验证年份作为可用来源，并在来源状态里说明。

### 输出目录在哪里

命令行结束时会打印 run id 和 run 目录。也可以查看：

```text
modules/finding/.runtime/runs/
```

最新一次正常 run 会复制到：

```text
modules/finding/.runtime/latest_run/
```

## 使用建议

- 先用 3 到 5 个核心会议和最近 1 到 2 年运行。
- 初次探索可打开 arXiv，正式筛选可同时使用所选会议、期刊和预印本来源。
- 推荐数量先设为 10 到 20。
- 每次改研究主题后重新运行 Find。
- 查看 `source_status.md` 判断来源覆盖，再阅读 `find.md`。
