# Reading 论文精读模块

Reading 根据论文标题或已知链接获取同篇论文的全文或 PDF，并生成中文精读结果。只提供标题即可独立运行；URL、PDF、DOI、arXiv ID、OpenReview ID、作者和来源信息可以提高全文定位速度与准确性。最终面向用户的产物是 Markdown 文件 `read.md`。

## 功能概览

Reading 可以完成这些工作：

- 接收论文标题、arXiv、bioRxiv、OpenReview、期刊页面、PDF 链接、DOI 等论文输入。
- 获取同篇论文的全文材料或 PDF，并保存每篇论文的获取记录。
- 为每篇论文生成中文精读。
- 默认按输入最终排名精读前 50 篇；输入不足 50 篇时全部精读，也可用 `--max-papers` 覆盖。
- 基于全部已完成精读统一评出 0-10 的匹配度与可借鉴性，并按两项均分重排。
- 在会议明确提供展示类型时，将 `Oral`、`Spotlight` 或 `Poster` 写入来源。
- 汇总多篇论文，生成一个最终 `read.md`。
- 复用同篇论文的历史全文、PDF 和单篇精读结果。
- 通过网页 Read 页或命令行运行。

Reading 专注于同篇论文的全文获取和精读，不扫描会议论文列表。TASTE 的 Find 最终排名可以通过 Framework 作为普通 Reading 输入传入；Reading 取排名前 N 篇，而不是只读取 Find 推荐论文。已有的作者、URL、PDF、DOI、OpenReview ID 等字段会直接用于全文定位。

## 运行环境

在 TASTE 仓库根目录运行命令，并使用 `taste` conda 环境：

```bash
conda activate taste
cd <TASTE_ROOT>
```

检查 Reading 是否可用：

```bash
python modules/reading/main.py --contract
```

生成精读需要 Claude Code。确认当前 shell 可以找到 `claude`：

```bash
claude --version
```

如果 Claude Code 在固定路径，可以这样指定：

```bash
export CLAUDE_PATH=/path/to/claude
```

## 配置

公开配置文件：

```text
modules/reading/config/reading.json
```

本机私有配置文件：

```text
modules/reading/config/read.env
```

该文件统一保存 Reading 实际使用的本机环境变量，并对本服务器上的所有用户生效。例如：

```bash
OPENREVIEW_USERNAME=your_email@example.com
OPENREVIEW_PASSWORD=your_password
```

常用环境变量：

| 变量 | 用途 |
| --- | --- |
| `OPENREVIEW_USERNAME` / `OPENREVIEW_PASSWORD` | 访问需要登录的 OpenReview 页面或附件。 |
| `READING_OPENREVIEW_ALLOW_ANONYMOUS_OFFICIAL_CLIENT` | 控制未配置账号时是否允许 OpenReview 官方客户端匿名访问。 |
| `READING_OPENREVIEW_COOKIE` / `OPENREVIEW_COOKIE` | 使用已有 OpenReview Cookie。 |
| `SEMANTIC_SCHOLAR_API_KEY` / `S2_API_KEY` | 查找同篇论文的开放 PDF 候选。 |
| `UNPAYWALL_EMAIL` | 通过 Unpaywall 查找 DOI 对应开放全文。 |
| `OPENALEX_API_KEY` / `OPENALEX_MAILTO` | 提高 OpenAlex 查询稳定性；`OPENALEX_MAILTO` 只用于 OpenAlex。 |
| `SPRINGER_API_KEY` / `SPRINGER_NATURE_API_KEY` | 启用 Springer Nature Open Access API。 |
| `CROSSREF_MAILTO` | 进入 Crossref polite pool，只用于 Crossref。 |
| `READING_CONTACT_EMAIL` | Reading 通用联系邮箱；专用服务未配置邮箱时作为回退。 |
| `JINA_API_KEY` | 启用 Jina Search，并认证 Jina Reader PDF/网页文本后备。 |
| `GITHUB_TOKEN` / `GH_TOKEN` | 提高作者项目页 GitHub API 的请求额度。 |
| `READING_READ_WORKERS` | 设置多篇论文并发处理数量。 |
| `READING_DISABLE_ARTICLE_CACHE=1` | 本次运行跳过文章级缓存。 |
| `READING_DISABLE_RUNTIME_CACHE=1` | 本次运行重新获取全文材料。 |

所有外部论文渠道都在共享的按已知服务或未知主机请求层排队；同一服务或主机跨 Find、Read worker 和跨进程并发上限为 `1`，不同渠道可以并行。arXiv API 保持至少 3 秒间隔；OpenReview 为 1 秒，ICLR/ICML 为 3 秒。收到 `429` 时优先严格使用服务端 `Retry-After`/额度重置时间，没有响应头时才使用分渠道短冷却；`403` 与 Cloudflare challenge 使用独立的分渠道冷却。批次补抓同时受对应渠道等待上限和全阶段 30 秒总预算约束，超过任一上限会留给后续任务重试，不会长期阻塞当前 Read。`read-workers` 也用于并行处理后续单篇精读。

Jina、网页搜索和 GitHub 等可选后备源的进程内熔断按服务和响应类型计算：`429` 优先使用服务端等待时间；没有响应头时分别使用 10、30、60 秒。普通网络异常不会触发进程熔断。

## 网页使用

启动网页：

```bash
python framework/scripts/main.py
```

默认地址：

```text
http://127.0.0.1:8879
```

使用步骤：

1. 打开网页并选择项目。
2. 确认当前 Find 已完成；Framework 会把完整最终排名转换成 Reading 输入。
3. 进入 Read 页。
4. 点击 Read 运行精读。
5. 在任务日志中查看“爬文章”和“读文章”两个阶段进度。
6. 任务完成后，在 Read 页查看 `read.md`。

需要重新生成单篇精读时，使用网页中的强制重读选项。

## 命令行使用

### 单篇论文

```bash
python modules/reading/main.py deep-read \
  --article "https://arxiv.org/abs/1706.03762" \
  --title "Attention Is All You Need" \
  --claude-mode run
```

只有标题时也可以独立查找同篇全文：

```bash
python modules/reading/main.py deep-read \
  --title "Attention Is All You Need" \
  --claude-mode run
```

常用参数：

| 参数 | 用途 |
| --- | --- |
| `--article` | 可选论文页面、PDF 链接、arXiv 编号或 DOI。 |
| `--title` | 论文标题。 |
| `--pdf-url` | 已知 PDF 地址。 |
| `--abstract` | 已知摘要。 |
| `--claude-mode run` | 获取全文并生成精读。 |
| `--claude-mode prepare` | 只获取和整理全文材料。 |
| `--force` | 重新生成单篇精读结果。 |
| `--timeout-sec` | 单次运行超时时间，单位为秒。 |

### 多篇论文

多篇精读使用 JSON 文件输入：

```bash
python modules/reading/main.py read \
  --input-json modules/reading/.runtime/output/<run-id>/input/source_input.json \
  --max-papers 50 \
  --claude-mode run \
  --read-workers 2 \
  --timeout-sec 1800
```

只检查全文获取情况：

```bash
python modules/reading/main.py read \
  --input-json modules/reading/.runtime/output/<run-id>/input/source_input.json \
  --claude-mode prepare \
  --read-workers 4
```

`--max-papers` 表示按输入顺序取前 N 篇；不传时使用模块默认值 50。`--read-workers` 可以提高多篇论文处理速度。建议从 `2` 或 `4` 开始，根据机器负载和 Claude Code 稳定性调整。

### 使用当前 Find 结果

网页会通过 Framework 完成输入转换、Reading 调用和项目同步。命令行使用：

```bash
python framework/scripts/main.py module reading \
  --action current_find_research_plan \
  --project <project>
```

Framework 会调用 Reading 的通用 `read` action。Reading 模块自身不读取项目目录或 Find 状态。

## 输入 JSON

最小示例：

```json
{
  "research_topic": "研究主题",
  "researcher_profile": "研究画像",
  "articles": [
    {
      "title": "Attention Is All You Need"
    }
  ]
}
```

带可选定位信息的示例：

```json
{
  "articles": [
    {
      "source": "arxiv",
      "paper_id": "1706.03762",
      "title": "Attention Is All You Need",
      "authors": ["Ashish Vaswani"],
      "url": "https://arxiv.org/abs/1706.03762",
      "pdf_url": "https://arxiv.org/pdf/1706.03762"
    }
  ]
}
```

常见字段：

| 字段 | 说明 |
| --- | --- |
| `source` | 来源，如 `arxiv`、`biorxiv`、`openreview`、`nature`。 |
| `paper_id` / `id` | 论文 ID。 |
| `title` | 论文标题。 |
| `authors` | 作者列表或字符串。 |
| `abstract` | 摘要。 |
| `url` / `abs_url` | 论文页面。 |
| `pdf_url` | PDF 地址。 |
| `doi` | DOI。 |
| `venue` / `year` | 会议、期刊或年份。 |
| `presentation_type` | 可选的会议展示类型：`oral`、`spotlight` 或 `poster`。 |

`title` 是最小输入。已知的 `url`、`pdf_url`、`doi`、arXiv ID、OpenReview ID 和作者信息会优先用于同篇全文定位。输入中的 `abstract` 会作为精读上下文使用。

批量运行会保持 `articles` 的输入排名，截取前 N 篇精读。统一评分需要顶层的 `research_topic`、`research_interest`、`researcher_profile`（或等价的 `research_context`）作为匹配依据。最终 `read_results.json` 的 item/reading 会包含 `match_score`、`transferability_score`、`average_score` 和 `final_read_rank`，`read.md` 会在每篇标题附近显示两项分数。

Framework 从 Find 启动 Reading 时会把 Find 已确认的会议展示类型写入 Reading 输入。独立运行时可直接提供 `presentation_type`；输入未提供时，来源保持会议与年份。

## 输出位置

单次运行结果保存在：

```text
modules/reading/.runtime/output/<run-id>/
```

最近一次运行会复制到：

```text
modules/reading/.runtime/latest_run/
```

`latest_run` 只供人工审查；程序始终使用明确的时间戳 run 目录。

常看文件：

| 文件 | 内容 |
| --- | --- |
| `read.md` | 最终中文精读结果。 |
| `read_results.json` | 运行状态、完成数量、warning、error 和结果路径。 |
| `full_text_reading/full_text_packet.json` | 本次运行的全文获取汇总。 |
| `papers/*/read.md` | 单篇论文精读结果。 |
| `papers/*/full_text_packet.json` | 单篇论文全文获取记录。 |
| `papers/*/paper.json` | 单篇论文题录信息。 |

通过网页运行时，项目中也会同步一份结果：

```text
projects/<project>/planning/finding/read.md
projects/<project>/planning/finding/read_results.json
projects/<project>/planning/finding/reading_runs/<run-id>/
```

`read.md` 放精读正文。JSON 文件用于查看状态、路径和错误信息。

## read.md 内容格式

最终 `read.md` 的结构：

```markdown
# 论文精读

## 001. Paper Title

- **匹配度：** 9/10
- **可借鉴性：** 8/10
- **来源：** ICLR 2026 Oral
- **论文链接：** URL：[论文页面](<https://example.org/paper>)；PDF：[PDF](<https://example.org/paper.pdf>)

### 摘要

...

### 动机与核心创新

...

### 方法

...

### 实验结果

...

### 优缺点总结

...
```

每篇论文标题下只展示两项元数据：

- `来源`：简洁写法，如 `ICLR 2026 Oral`、`NeurIPS 2025 Poster`、`arXiv 2026-06-01`、`Nature 2026-06-24`。会议有明确展示类型时附加 `Oral`、`Spotlight` 或 `Poster`。
- `论文链接`：Markdown 链接格式，如 `URL：[论文页面](<...>)；PDF：[PDF](<...>)`。PDF 缺失时显示 `未提供`。

正文栏目说明：

| 栏目 | 内容 |
| --- | --- |
| `摘要` | 输入提供 `abstract_zh` 时逐字使用该中文摘要；缺失时由单篇阅读 subagent 翻译原文摘要。 |
| `动机与核心创新` | 两段内容，分别说明研究动机和核心创新。 |
| `方法` | 论文自己的创新方法，结合数学公式做通俗解释。 |
| `实验结果` | 概括实验类型和总体效果。 |
| `优缺点总结` | 简短总结主要优点和风险边界。 |

数学变量使用 `$...$` 行内公式，关键方程使用独立的 `$$...$$` 公式块。网页会直接渲染 LaTeX；范数、条件概率、上下标和数学算子按标准 LaTeX 写法展示。

## 缓存与重读

Reading 会按文章复用历史结果。同篇论文已经有全文、PDF 或单篇 `read.md` 时，后续项目和新 run 会优先使用已有结果。

全文获取会优先使用会议、期刊或 OpenReview 的官方 PDF。若同篇论文的 PDF 或全文内容发生更新，对应的旧单篇精读会自动失效，并基于新内容重新生成；内容一致时继续复用。

会议输入即使只带标题或失效的元数据链接，Reading 也会先按会议限定检索对应官方全文域名，再依次尝试开放索引、预印本和作者/项目页；所有标题检索结果仍须通过同篇正文校验。

需要重新生成时：

- 网页：使用强制重读选项。
- 命令行：添加 `--force`。

跳过文章缓存：

```bash
READING_DISABLE_ARTICLE_CACHE=1 python modules/reading/main.py read \
  --input-json modules/reading/.runtime/output/<run-id>/input/source_input.json \
  --claude-mode run
```

## 工作流程

Reading 只有两个阶段：

1. `爬文章`：根据标题和可选定位信息获取或复用同篇全文。
2. `读文章`：生成或复用单篇精读，并汇总为最终 `read.md`。

## 常见问题

| 现象 | 查看位置 |
| --- | --- |
| 没有最终 `read.md` | `read_results.json` 里的运行状态、warning 和 error。 |
| 某篇论文没有精读 | 对应 `papers/<paper>/read_results.json`。 |
| 全文获取失败 | 对应 `papers/<paper>/full_text_packet.json`。 |
| OpenReview 访问失败 | 确认 `config/read.env` 或 Cookie 配置，并查看全文获取记录。 |
| 网页进度停在爬取全文 | 查看缺全文论文列表和任务日志。 |
| 网页显示 warning | 查看 Web 任务日志和 `read_results.json.warning_items`。 |

## 快速检查

```bash
python modules/reading/main.py --contract
```
