# Reading 论文精读模块

本模块负责获取经过验证的论文正文材料，并基于正文生成精读任务产物；当公开全文不可用时，同轮替换只发生在 Reading 内，不回写 Finding。

边界契约原句：`Acquire verified paper-body text for the selected Find packet and synthesize reading notes`。这句话与 `main.py` 中的 `RESPONSIBILITY` 保持一致，用于模块边界测试；实际使用说明仍以本文中文描述为准。

`modules/reading` 是 TASTE 中专门负责“论文精读”的后端模块。它必须能够脱离网页前端单独运行：给定一篇论文或当前 Find 产物，自己完成题录整理、PDF/全文资料获取、正文抽取、Claude Code 主控提示生成，并要求主控 Claude Code 调用 Task/subagent 做逐篇精读。

本模块只维护后端能力，和 web 前端没有直接关系。前端或 TASTE 框架如果要使用 Reading，只能调用 `modules/reading/main.py` 暴露的 action，不应该直接拼内部脚本路径。

## 运行环境

在远程工作区 `/home/fmh/workspace/TASTE` 中运行。不要使用系统默认 `python`，也不要依赖默认 PATH 中是否有 `rg`。

推荐环境：

```bash
cd /home/fmh/workspace/TASTE
PY=/home/fmh/workspace/miniforge/envs/ar_taste/bin/python3.11
RG=/home/fmh/workspace/miniforge/envs/ar_taste/bin/rg
NODE_BIN=/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin
PATH="$NODE_BIN:$PATH" "$PY" modules/reading/main.py --contract
```

`ar_taste` 环境负责 Python 依赖，例如 `requests`、`PyMuPDF/fitz`。Claude Code 命令来自项目配置中的 nvm Node 环境：`/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin/claude`。

可选增强环境变量：

| 变量 | 含义 |
| --- | --- |
| `SEMANTIC_SCHOLAR_API_KEY` / `S2_API_KEY` | 启用 Semantic Scholar Graph 增强，用于补 DOI/arXiv/PMCID、引用数、TLDR、领域标签和开放 PDF 候选。无 key 时默认不请求，避免共享限流拖慢主流程。 |
| `READING_ENABLE_SEMANTIC_SCHOLAR=1` | 无 key 时仍显式尝试 Semantic Scholar；若遇到 `429` 只记录证据，不阻塞全文获取。 |
| `OPENALEX_API_KEY` / `OPENALEX_MAILTO` | OpenAlex 增强参数；用于提高开放索引请求稳定性。 |
| `UNPAYWALL_EMAIL` | 启用 Unpaywall DOI 开放全文位置兜底。 |

## 独立精读入口

给定一篇论文，使用 `deep-read` action：

```bash
cd /home/fmh/workspace/TASTE
PY=/home/fmh/workspace/miniforge/envs/ar_taste/bin/python3.11
PATH=/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin:$PATH \
  "$PY" modules/reading/main.py \
  --action deep-read \
  --article "https://arxiv.org/abs/1706.03762" \
  --claude-mode auto

# 也支持位置式 action，等价于 --action deep-read：
PATH=/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin:$PATH \
  "$PY" modules/reading/main.py deep-read \
  --article "https://arxiv.org/abs/1706.03762" \
  --claude-mode prepare
```

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--article` | 论文 URL、arXiv 编号/链接、PDF URL 或 DOI。 |
| `--title` | 可选标题；非 arXiv/非 PDF 输入时建议提供。 |
| `--authors` | 可选作者，逗号分隔。 |
| `--abstract` | 可选摘要。 |
| `--pdf-url` | 已知 PDF 地址。 |
| `--input-json` | 可选输入 JSON；模块只读取它，不在它旁边写产物。 |
| `--run-id` | 可选运行 ID；默认由时间戳和标题生成。 |
| `--claude-mode auto` | 默认模式：正文足够且 Claude 可用时调用 Claude Code。 |
| `--claude-mode prepare` | 只下载/抽取/生成 Claude prompt，不实际调用 Claude，适合调试和离线验收。 |
| `--claude-mode run` | 强制尝试调用 Claude Code。 |
| `--timeout-sec` | Claude Code 调用超时时间，默认 1800 秒。 |

## 输入口径

独立精读最小输入是一条 `--article`。如果输入是 arXiv 链接或编号，模块会查询 arXiv 元数据并获取 PDF。如果输入是 PDF URL，模块会直接下载并验证 PDF。如果输入是 DOI 或普通页面 URL，建议同时给出 `--title`、`--authors`、`--abstract` 或 `--pdf-url`，便于题录和 PDF 获取。

`--input-json` 可以提供同样的信息，例如：

```json
{
  "article": "https://arxiv.org/abs/1706.03762",
  "title": "Attention Is All You Need",
  "authors": ["Ashish Vaswani", "Noam Shazeer"],
  "abstract": "..."
}
```

## 输出口径

所有独立精读运行产物都写在：

```text
modules/reading/workspace/runs/<run-id>/
```

该目录已由 `modules/reading/.gitignore` 排除，PDF、正文、Claude 输出等中间产物不会进入 git。

主要输出：

| 文件 | 内容 |
| --- | --- |
| `input.json` | 本次 CLI 和输入 JSON 的留痕。 |
| `paper.json` | 归一化后的论文题录；启用 Semantic Scholar 时会包含 `semantic_scholar_context`、引用数量、TLDR、外部 ID 和开放 PDF 候选。 |
| `downloads/*.pdf` | 下载到的论文 PDF。 |
| `extracted/full_text.txt` | 从 PDF 抽取的正文文本。 |
| `extracted/full_text_xml.txt` | 从 EuropePMC/PMC XML 兜底抽取的论文正文文本。 |
| `full_text_packet.json` | Reading 内部全文证据包。 |
| `prompts/deep_read_prompt.md` | 交给主控 Claude Code 的中文任务提示，明确要求调用 Task/subagent。 |
| `outputs/reading_result.json` | Claude/subagent 预期写入的精读 JSON。 |
| `claude/claude_receipt.json` | Claude Code 调用状态、stdout/stderr 路径和解析结果。 |
| `read_results.json` | 本次运行的结构化总结果。 |
| `read.md` | 人类可读中文精读摘要或阻塞说明。 |

`modules/reading/workspace/latest_run.json` 会指向最近一次独立精读运行。

## 运行流程逻辑

1. `main.py` 解析 action，只把公开 action 分发到模块内部脚本。
2. `standalone_deep_read.py` 创建 `modules/reading/workspace/runs/<run-id>/`，确保所有运行产物留在 Reading 模块内。
3. `paper_sources.py` 归一化论文输入，必要时查询 arXiv 元数据；如果配置了 Semantic Scholar，则只把它作为题录、引用图谱和开放 PDF 候选增强源，不把它的摘要或题录直接当全文。
4. 资料获取层复用 `read_pipeline.py` 中已有 PDF 候选、下载和 PDF 文本抽取函数；所有候选 PDF 都必须通过文件类型和正文抽取校验。
5. 模块写出 `full_text_packet.json`，记录 PDF、正文路径、字数和获取证据。
6. `claude_subagent.py` 生成主控 Claude Code prompt。prompt 强制要求主控调用 Task/subagent 做全文精读；如果没有 Task/subagent，必须写 blocked JSON，不能由主控短写替代。
7. `--claude-mode auto/run` 会调用项目 nvm 环境中的 Claude Code；`prepare` 只生成 prompt 和证据包。
8. 模块汇总 Claude 结果、全文证据和运行状态到 `read_results.json` 与 `read.md`。
9. 在当前 Find/旧 Read 流水线中，模块会基于全部 reading 产物重新打分排序，写入 `reading_ranking`、`reading_ranking_order`，并按读后分数输出最终阅读顺序。


## 多渠道批量验收入口

用于测试多个论文来源渠道的下载、正文抽取和阅读任务产物生成：

```bash
cd /home/fmh/workspace/TASTE
PY=/home/fmh/workspace/miniforge/envs/ar_taste/bin/python3.11
PATH=/home/fmh/workspace/.nvm/versions/node/v22.21.0/bin:$PATH \
  "$PY" modules/reading/main.py \
  --action channel-batch-test \
  --run-id channel_batch_示例 \
  --per-channel 10 \
  --candidate-limit 100 \
  --claude-mode prepare \
  --workers 2
```

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--channels` | 逗号分隔渠道，默认测试 `nips2025,iclr2026,icml2026,sigkdd2026,arxiv,biorxiv,nature,science_family`。 |
| `--per-channel` | 每个渠道要求生成的合格阅读任务产物数量，默认 10。 |
| `--candidate-limit` | 每个渠道最多尝试多少个候选，默认 100。 |
| `--claude-mode prepare` | 只生成全文证据包和主控 Claude/subagent prompt，不实际调用 Claude。 |
| `--claude-mode run` | 对合格全文候选实际调用 Claude Code。该模式会消耗时间和模型额度。 |
| `--workers` | 并发处理渠道数，默认 2；Science 等站点对并发敏感，过高可能触发 403。 |

批量验收产物写入 `modules/reading/workspace/batch_tests/<run-id>/`，包括每个渠道的 `crawl_receipt.json`、`channel_summary.json`、每篇论文的 `paper.json`、`full_text_packet.json`、`prompts/deep_read_prompt.md`、`read_results.json`、`read.md`，以及总报告 `batch_report.json` / `batch_report.md`。

批量产物审计使用 `audit-channel-batch` action。它会逐渠道核对指定数量的合格产物，检查 `read_results.json`、`full_text_packet.json`、正文路径是否仍在 Reading 内、`full_text_available`、正文长度、`Task/subagent` 和 `subagent_deep_read` prompt 标记，并写出中文审计报告：

```bash
cd /home/fmh/workspace/TASTE
PY=/home/fmh/workspace/miniforge/envs/ar_taste/bin/python3.11
"$PY" modules/reading/main.py audit-channel-batch \
  --run-id channel_batch_示例 \
  --per-channel 10
```

默认审计报告写到 `modules/reading/workspace/batch_tests/<run-id>/manual_audit_zh.md`，结构化结果写到同名 `.json`。

多渠道候选来源会优先使用对应渠道的官方或开放索引：NeurIPS proceedings、ICLR/ICML virtual 页面与 OpenAlex/arXiv 公开 PDF、SIGKDD ACM proceedings DOI 前缀、arXiv API/OpenAlex 兜底、bioRxiv 官方 published mapping、Nature 搜索页与 Nature PDF、Science 系列 DOI/OpenAlex/Crossref。Semantic Scholar 只在配置 API key 或显式开启时作为增强源补题录、引用图谱、外部 ID 和开放 PDF 候选；它返回的题录、TLDR、摘要或 `openAccessPdf` 不能绕过 PDF/HTML/XML 正文校验。对 Science/PMC 这类 PDF 或 science.org 页面在当前环境被 403/PoW 拦截的情况，模块会从 OpenAlex 候选中识别 PMCID，并通过 EuropePMC `fullTextXML` 抽取正文。OpenReview、bioRxiv、ACM DL、science.org 等页面如果在当前环境返回 403，模块会记录证据并改用公开 PDF/HTML/XML 兜底；不会把 403 页、poster、RSS、题录或摘要算作全文。

合格标准不是“有摘要就算读过”：必须有可访问 PDF 正文或具备论文正文结构的 HTML 正文，`full_text_available=true`，并且 prompt 中包含 `Task/subagent`、`subagent_deep_read` 和实际正文路径。会议 poster 摘要页、RSS、Crossref 摘要、题录页、Cloudflare/403 页面都不能冒充全文精读。

最近一次严格验收：`channel_batch_20260618_semantic_v1_all_channels`，`nips2025`、`iclr2026`、`icml2026`、`sigkdd2026`、`arxiv`、`biorxiv`、`nature`、`science_family` 均达到 10/10；审计报告在该运行目录的 `manual_audit_zh.md`，结构化结果为 `manual_audit_zh.json`，`problem_count=0`，其中 XML 全文兜底条目 9 篇。上一轮稳定基线为 `channel_batch_20260618_rerank_v1_all_channels`。

## 与 TASTE 当前 Find 的兼容入口

旧有 TASTE 流程仍可通过这些 action 调用，根目录脚本只是兼容薄壳，真实实现已分到子目录：

| action | 作用 |
| --- | --- |
| `audit-channel-batch` | 审计多渠道批量验收产物，核对全文证据、prompt 标记和路径边界。 |
| `read` / `pipeline` | 读取 Find run 的推荐论文并生成 Read 结果；最终 `read_results.json` 包含基于精读产物的 `reading_ranking` 和 `reading_ranking_order`。 |
| `repair-full-text` | 修复当前 Find 的全文证据包，不回写 Finding 产物。 |
| `current-find-research-plan` | 编排当前 Find 的 Read/Idea/Plan，驱动 Claude Code 项目会话和 subagent 精读审计。 |
| `import` | 向项目原始论文区导入单篇外部论文题录，兼容旧 TASTE 项目数据结构。 |

## 脚本结构

| 路径 | 分类 | 作用 |
| --- | --- | --- |
| `main.py` | 公开入口 | Reading 唯一公开后端入口，负责 action 路由和 `--contract`。 |
| `scripts/read_pipeline.py` | 兼容薄壳 | 保持旧导入 `from read_pipeline import run_read` 可用。 |
| `scripts/repair_current_find_full_text_evidence.py` | 兼容薄壳 | 保持旧修复脚本路径可执行。 |
| `scripts/ensure_current_find_research_plan.py` | 兼容薄壳 | 保持旧编排脚本路径可执行。 |
| `scripts/common/` | 通用工具 | Reading 内部路径、JSON、文本、slug 和读后重评分排序工具。 |
| `scripts/acquisition/` | 资料获取 | 论文题录归一化、arXiv 元数据、可选 Semantic Scholar 增强、PDF 获取和正文抽取。 |
| `scripts/pipeline/` | 流水线 | 独立单篇精读流水线、多渠道批量验收/审计流水线，以及旧 Find run Read 流水线真实实现。 |
| `scripts/orchestration/` | Claude 编排 | Claude/subagent prompt 与当前 Find Read/Idea/Plan 编排。 |
| `scripts/repair/` | 证据修复 | 当前 Find 全文证据修复真实实现。 |
| `script_manifest.json` | 脚本清单 | 由当前文件结构生成的人类可读清单。 |
| `工作状态.txt` | 工作记录 | 记录 Reading 模块较大修改、状态和后续注意事项。 |

## 边界和约束

- Reading 的独立运行产物只能写入 `modules/reading/workspace/`。
- 不在 `web/`、其它 `modules/` 或 TASTE 框架目录产生 Reading 中间产物。
- `scripts/` 新增脚本必须放入功能子目录；根目录只保留兼容薄壳。
- 不新增只服务某一篇论文的特例脚本；论文差异通过输入和通用获取逻辑处理。
- Claude 精读结果必须有 `subagent_deep_read=true` 和 `deep_read_audit`，否则不能视为完成精读。
- 主控 Claude Code 完成逐篇精读后应写入 `read_score`、`read_score_breakdown`、`read_score_audit`；模块会据此形成 `reading_ranking` 最终排序，缺失时仅使用确定性保底排序并记录来源。
- Semantic Scholar、Crossref、OpenAlex 等元数据源只能补候选和证据说明；最终精读必须依赖通过校验的 PDF/HTML/XML 正文。
- 无全文证据时状态应为 blocked 或 prepared，不能用摘要/推荐理由冒充全文精读。
