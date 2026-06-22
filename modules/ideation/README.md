# Ideation / Ideas 模块

本目录是 TASTE 的 `Ideation / Ideas` 后端模块边界。当前维护目标是：模块只依赖显式输入的论文精读产物、研究主题/兴趣/配置和 Claude Code CLI，就能独立生成创新、可行、可审计的新论文 idea。它不依赖网页前端，不要求前端拼接脚本路径，也不把中间产物写到其它模块。

## 职责边界

- 输入论文精读产物，生成可编辑、可筛选、可实验化的新论文 idea。
- 调用主控 Claude Code 进行正式 idea 生成，并用本模块内的质量门做结构化审计。
- 输出 `ideas.json`、`idea.md`、`hypothesis_arena.json/md` 和质量审计。
- 不选择最终执行路线，不创建实验环境，不运行实验，不写 web 前端产物。

## 独立运行方式

必须在远端 TASTE 工作目录运行，并显式激活 `ar_taste` 与 nvm/Claude Code 环境：

```bash
cd <TASTE_ROOT>
source /home/fmh/workspace/miniforge/etc/profile.d/conda.sh
conda activate ar_taste
. /home/fmh/workspace/.nvm/nvm.sh
```

正式调用 Claude Code 生成 idea：

```bash
python modules/ideation/main.py \
  --action generate \
  --input /path/to/read_results.json \
  --input /path/to/read.md \
  --research-topic "你的研究主题" \
  --research-interest "你的研究兴趣" \
  --researcher-profile "研究者画像或偏好" \
  --idea-constraints '{"max_risk":"medium"}' \
  --max-ideas 6 \
  --model sonnet \
  --effort high
```

开发自检可以使用 `--mock`，这不会调用 Claude Code，只验证输入读取、产物写入、schema、渲染和质量审计链路：

```bash
python modules/ideation/main.py \
  --action generate \
  --input modules/ideation/runs/self_check_input/read_results.json \
  --research-topic "推荐系统中的语义增强" \
  --research-interest "可审计、可消融的新方法" \
  --max-ideas 2 \
  --mock
```

如果 Claude Code 已经成功返回、但本地解析或后处理在旧代码中断，可以不重新调用 Claude，直接从已保存的 `claude_stdout.json` 续写最终产物：

```bash
python modules/ideation/main.py \
  --action finalize \
  --run-id <run_id>
```

`finalize` 会读取 `modules/ideation/runs/<run_id>/input_bundle.json`、`claude_command.json` 和 `claude_stdout.json`，重新生成 `claude_payload.json`、`ideas.json`、`idea.md`、`idea_quality_audit.json`、`hypothesis_arena.json/md` 与 `manifest.json`。它不会再次调用 Claude Code。

## 输入口径

`--input` 和 `--input-dir` 可以重复传入。模块只读取这些显式路径，不隐式扫描其它 TASTE 状态。

支持的输入包括：

- `read_results.json`：包含 `readings`、`papers`、`articles` 等列表的精读结果。
- `read.md` / Markdown 精读笔记：按标题和章节切分为证据。
- `current_find_deep_read_fragments/*.json`：逐篇深读分片目录。
- `find_results.json` 或其它含标题、摘要、方法、实验、局限字段的 JSON。

核心字段会被规范为：论文标题、paper_id、url、summary、method、experiments、limitations、novelty、key_findings 等。Claude prompt 中只会使用规范化后的证据包。

## 输出口径

所有运行产物默认写入：

```text
<TASTE_ROOT>/modules/ideation/runs/<run_id>/
```

`runs/` 已被本目录 `.gitignore` 忽略，避免中间产物进入 git。

每次独立生成会产出：

| 文件 | 作用 |
| --- | --- |
| `input_bundle.json` | 本次显式输入和规范化证据包。 |
| `claude_prompt.md` | 发送给主控 Claude Code 的完整 prompt。 |
| `claude_command.json` | Claude Code 调用命令和运行元信息。 |
| `claude_stdout.json` / `claude_stderr.log` | Claude Code 原始输出和错误日志。 |
| `claude_payload.json` | 从 Claude Code 输出解析出的 JSON payload。 |
| `ideas.json` | 最终结构化 idea、配置、质量审计和 Claude 调用元信息。 |
| `idea.md` | 人类可读的新论文 idea 卡片。 |
| `idea_quality_audit.json` | 字段完整性、主题贴合、实验协议和启发来源审计。 |
| `hypothesis_arena.json` | 将 idea 转成可对比假设面板。 |
| `hypothesis_arena.md` | 人类可读假设面板。 |
| `manifest.json` | 本次运行产物清单。 |

## 运行流程逻辑

1. `main.py --action generate` 解析输入路径、配置和运行参数。
2. `artifact_io/artifacts.py` 只读加载论文精读产物，统一成证据列表。
3. `core/prompting.py` 将研究主题、研究兴趣、研究者画像、配置约束和证据包组装为 Claude Code prompt。
4. `claude/runner.py` 调用 `claude -p --output-format json --json-schema ...`，并限制工作目录/允许目录为 `modules/ideation`。
5. `ideation_quality/schema.py` 规范化 Claude 输出，固定 `new_method`、`method_details`、`initial_experiment`、`bad_case_slice`、`inspired_by` 等字段。
6. `ideation_quality/schema.py` 执行质量审计：字段长度、初步实验协议、主题贴合、启发来源是否来自输入证据；若 idea 已正确锚定输入精读证据，也允许英文 idea 通过中文主题场景。
7. `ideation_quality/render.py` 渲染 `idea.md` 和 `hypothesis_arena`。
8. `core/standalone_pipeline.py` 将所有产物写入 `modules/ideation/runs/<run_id>/`。
9. `main.py --action finalize` 可从已保存的 Claude stdout 续写完整产物，用于恢复已成功调用但后处理失败的运行。

## 脚本结构

| 路径 | 类别 | 作用 |
| --- | --- | --- |
| `main.py` | 公开入口 | 本模块唯一公开后端入口；注册 `generate`、`finalize`、旧 `idea` 兼容入口和少量 legacy tool action。 |
| `scripts/core/prompting.py` | 核心流程 | 构造 Claude Code prompt 和证据包。 |
| `scripts/core/standalone_pipeline.py` | 核心流程 | 独立 idea 生成总控流程；负责运行目录、调用、审计、产物写入和 stdout 恢复。 |
| `scripts/artifact_io/workspace.py` | 输入输出 | 定义 ideation 模块根目录、安全写入、run 目录和 JSON/文本写入。 |
| `scripts/artifact_io/artifacts.py` | 输入输出 | 读取并规范化论文精读 JSON/Markdown/分片。 |
| `scripts/claude/runner.py` | Claude 调用 | 查找 nvm 中的 Claude Code CLI，执行 JSON schema 约束调用并解析输出。 |
| `scripts/ideation_quality/schema.py` | 质量门 | 定义 Claude 输出 schema、idea 规范化和质量审计。 |
| `scripts/ideation_quality/render.py` | 展示产物 | 渲染人类可读 `idea.md` 和假设面板。 |
| `scripts/idea_pipeline.py` | 兼容入口 | 保留 TASTE 既有 `run_idea/patch_idea` 兼容函数。新增独立能力不依赖它。 |
| `scripts/ideation_tools.py` | 兼容工具 | 保留旧 `assess/arena/initialization` 工具入口；新独立生成请优先使用 `--action generate`。 |

## 维护原则

- 新功能优先放进 `scripts/core`、`scripts/artifact_io`、`scripts/claude`、`scripts/ideation_quality` 这些分类目录，不再随意新增扁平脚本。
- 所有运行中间产物必须写到 `modules/ideation/runs/` 或显式传入的 ideation 内部输出目录。
- `runs/`、`tmp/`、`__pycache__/` 不进 git。
- 正式 idea 生成必须调用 Claude Code；`--mock` 只用于开发自检。
- 不为特定论文、特定项目、特定本机路径写硬编码规则。
- 代码应保持薄入口、清晰分层、函数复用，避免把读取、调用、审计、渲染混在一个大函数里。
