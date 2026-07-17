# Ideation / Ideas 用户手册

Ideation 模块把 Find 和 Read 的论文证据转成可编辑的研究想法。`idea.md` 是给用户审阅的最终产物；`ideas.json` 只是网页卡片、状态同步和后续模块读取用的机器投影。模块只生成候选 idea，不选择最终执行路线，不配置环境，不运行实验，也不写论文。

## 运行环境

在 TASTE 仓库根目录使用 `taste` conda 环境运行：

```bash
cd <TASTE_ROOT>
conda activate taste
```

正式生成需要当前终端能调用 Claude Code。Claude Code 会直接生成 `idea.md`，模块再从 `idea.md` 解析出 `ideas.json`。开发自检可以使用 `--mock`，不会调用 Claude Code。

如果 Claude 首稿只因固定栏目、证据引用、占位文本、公式或链接检查失败，模块会把明确错误和原稿交给 Claude 做一次完整 Markdown 修复；修复稿仍不合格时，本次 run 失败且 Framework 不会替换项目当前产物。

## 网页使用

通常从 TASTE 网页点击 Ideas。网页只发送明确的项目、当前 Find run、idea 数量上限和任务指令；Framework 校验当前 Find/Read、构建规范化输入包，再通过 `python framework/scripts/main.py module ideation --action idea` 调用本模块。Framework 对同一项目的生成和编辑使用阻塞式文件锁，不同项目可以独立运行。Framework 随后把本次模块 run 复制到：

```text
projects/<project>/planning/finding/ideation_runs/<ideation_timestamp>/
```

这里的目录名是本次 Ideation run 的 UTC 微秒时间戳，不是 Find `run_id`。网页展示使用项目中的：

```text
projects/<project>/planning/finding/idea.md
projects/<project>/planning/finding/ideas.json
```

`ideation_runs/<ideation_timestamp>/` 是对应模块 run 的完整副本，便于追溯。项目状态文件只保存当前时间戳、idea 数量和门控/选择状态，不再复制完整 idea 内容。

## 命令行使用

独立使用也走与 Framework 相同的公开动作。调用方先准备一个规范化的 `ideation_input.json`，模块不扫描项目目录，也不寻找或校验 Find/Read 项目状态：

```bash
conda run -n taste python modules/ideation/main.py \
  --action idea \
  --run-id <find_run_id> \
  --input-json /path/to/ideation_input.json \
  --max-ideas 6
```

只做本地自检：

```bash
conda run -n taste python modules/ideation/main.py \
  --action idea \
  --run-id test-find-run \
  --input-json /path/to/ideation_input.json \
  --max-ideas 2 \
  --mock
```

## 输入

模块只接受一个 JSON 对象：

| 输入 | 说明 |
| --- | --- |
| `schema_version` | 固定为 `taste.ideation_input.v1`。 |
| `run_id` | 与命令行 `--run-id` 相同。 |
| `items` | 至少一条带 `title` 的规范化证据；可含 `url`、`summary`、`source` 和分数。 |
| `read_markdown` | 调用方提供的精读 Markdown 正文。 |

项目工作流中，Find/Read 完整性、同一 `run_id` 和 Read 门控都由 Framework 负责。Idea 模块只消费 Framework 生成的单个 `ideation_input.json`，并把它保存为本次 run 的 `input_bundle.json`；模块不寻找项目、不检查项目状态，也不构建 Find/Read 输入目录。

## 输出

每次运行都会在启动时创建一个固定 run 目录：

```text
modules/ideation/.runtime/output/YYYYMMDDTHHMMSSffffffZ/
```

同一进程只写入这个一开始创建好的目录。并发运行会得到不同的微秒级 UTC 时间戳目录。

常见输出：

| 文件 | 说明 |
| --- | --- |
| `input_bundle.json` | 调用方已经构建好的规范化输入包。 |
| `claude_prompt.md` | 发送给 Claude Code 的 prompt，要求其直接输出 `idea.md`。 |
| `claude_stdout.json` / `claude_stderr.log` | Claude Code 原始输出。 |
| `claude_repair_*.json` / `claude_repair_stderr.log` | 仅首稿未过门控时出现的一次修复调用回执。 |
| `idea.md` | 最终公开产物，Claude Code 直接生成，包含新方法、机制细节、初步实验、启发来源、风险与停止标准。 |
| `ideas.json` | 从 `idea.md` 解析出的机器投影，供状态控制、项目同步和 Planning 使用；网页正文不从它渲染。 |
| `manifest.json` | 本次 run 的产物清单。 |

`idea.md` 中关于已有工作的能力、局限、数据和数字事实必须来自输入证据。所有 Markdown 网页链接都必须使用输入中已有的精确证据标题和对应 URL。新方法、初步实验和停止标准属于研究提案，不代表已经实现或得到结果；没有直接风险证据时只写停止规则，不推测失败原因或失败样本。

运行结束后，模块取得 `.latest_run.lock` 的阻塞式文件锁，再把本次 run 完整复制到：

```text
modules/ideation/.runtime/latest_run/
```

`latest_run` 只给人审查，程序不会把它当输入或同步来源。

## 编辑 idea

网页上方是人工修改区：每个 idea 保留标题、新方法、初步实验以及通过/待定/删除控件，点击“保存修改”时才提交；也可以切换到完整 Markdown 源文编辑。Framework 定位当前项目对应的 timestamp Ideation run，通过 `modules/ideation/main.py --action patch` 或 `--action update_markdown` 原地更新该 run，再同步同一个项目 run 副本和项目当前 `idea.md`。

页面右下“产物”栏直接使用 Markdown 库和 KaTeX 渲染项目当前 `idea.md`；它只负责审阅最终产物，不替代上方人工修改区。

通过、待定和删除按钮同样原地修改 `idea.md` 中对应 idea 的 `status`，不会创建新的 Ideation run。`ideas.json` 随后从更新后的 Markdown 重新派生，供状态控制和 Planning 使用。

## 常见问题

### 为什么 `idea.md` 没有“自检”栏目？

Markdown 标题、数学公式定界符、网页链接和栏目完整性由模块在后台检查。Claude Code 也会在提交前自行检查，但检查过程和 pass/fail 清单不会写进用户产物。

| 问题 | 处理方式 |
| --- | --- |
| 没有生成 idea | 确认输入包版本、`run_id`、`items` 和 `read_markdown` 完整，并检查 `latest_run/claude_stderr.log`。 |
| 想只检查链路 | 使用 `--mock`。 |
| 网页没有显示最新 idea | 确认当前项目的 Find run 与 `ideas.json.run_id` 一致，再刷新网页。 |
| 想审查最近一次运行 | 打开 `modules/ideation/.runtime/latest_run/`。 |
