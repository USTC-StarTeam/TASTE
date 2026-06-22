# Writing / 论文写作模块

`modules/writing` 是 TASTE 七阶段中的正式论文写作后端模块。它基于 idea、plan、实验记录、证据审计和目标会议/期刊要求，调研官方投稿规则、下载/验证官方模板，并让 Claude Code 生成证据受控的 venue-formatted 论文。

本模块不负责做实验，也不负责伪造证据。实验产物来自 `experimenting` 或项目目录中已审计的记录；证据不足时只能生成预览/诊断稿，不能标记为投稿通过。

## 运行环境

```bash
ssh hidimension_5090_1
cd <TASTE_ROOT>
source /home/fmh/workspace/miniforge/etc/profile.d/conda.sh
conda activate ar_taste
source /home/fmh/workspace/.nvm/nvm.sh
```

需要 `python`、`rg`、Claude Code；PDF 编译可用 `latexmk` 或 `pdflatex`，不可用时模块会留下编译阻塞报告。

## 两种使用方式

### 1. 独立输入包写作

先从项目复制写作输入：

```bash
python modules/writing/main.py \
  --action project_input_pack \
  --project <project_id> \
  --pack-id <pack_id>
```

该动作只读项目目录，只写：

```text
modules/writing/runs/source_packs/<pack_id>/
```

再独立运行写作：

```bash
python modules/writing/main.py \
  --action standalone \
  --venue "ICLR 2026" \
  --idea modules/writing/runs/source_packs/<pack_id>/idea.md \
  --plan modules/writing/runs/source_packs/<pack_id>/plan.md \
  --experimental-log modules/writing/runs/source_packs/<pack_id>/experimental_log.md \
  --records modules/writing/runs/source_packs/<pack_id>/records \
  --title "可选标题"
```

如果只准备 prompt 和目录，不调用 Claude：

```bash
python modules/writing/main.py --action standalone --venue "ICLR 2026" --idea ... --plan ... --experimental-log ... --records ... --prepare-only
```

### 2. TASTE 项目态论文预览

```bash
python modules/writing/main.py \
  --action pipeline \
  --project <project_id> \
  --venue "ICLR" \
  --generate-paper-preview \
  --refresh-current-venue
```

项目态入口会读取 `projects/<project>` 里的当前 Find/Read/Idea/Plan/Experiment/Paper 状态，并把项目论文预览写回项目目录。Web 的 paper 按钮使用这条兼容路径。

## 输入口径

独立写作需要：

- idea：研究问题、核心假设、贡献和方法直觉。
- plan：实验路线、数据、指标、基线、对照和成功/失败判据。
- experimental-log：真实实验、环境、命令、指标、失败和坏例记录。
- records：CSV/JSON/Markdown/图片/PDF/日志等证据目录。
- venue：目标会议/期刊，例如 `ICLR 2026`、`Nature Machine Intelligence`、`ACL 2026`。

项目态写作需要：

- `--project`：已有 TASTE 项目。
- `--venue`：目标会议/期刊。
- 当前项目中已有足够实验/引用/投稿审计证据；不足时生成预览但保持门控阻塞。

## 输出口径

独立写作输出：

```text
modules/writing/runs/<run_id>/
```

常见文件：

| 文件 | 作用 |
| --- | --- |
| `venue/venue_requirements.json` | 官方投稿要求解析。 |
| `venue/template_source/` | 官方模板或官方允许模板来源。 |
| `workspace/inputs/` | idea、plan、实验日志和 records 快照。 |
| `workspace/final/paper.tex` / `paper.pdf` | 最终 TeX/PDF。 |
| `workspace/refs.bib` | 引用库。 |
| `workspace/audits/*.json|md` | 引用、claim、页数、normality、submission readiness 审计。 |
| `workspace/provenance.json` | 证据和生成来源。 |

项目态写作输出在 `projects/<project>/paper/...` 和 `projects/<project>/state/...`，供 Web 展示。

## 运行流程

1. 解析目标 venue，抓取/核对官方 author guidelines、模板、页数、匿名、引用和 AI disclosure 要求。
2. 准备写作工作区和输入证据快照。
3. 让 Claude Code 生成或修订论文，但所有 claim 必须绑定输入证据。
4. 渲染 TeX、尝试编译 PDF。
5. 执行 citation、claim ledger、figure、normality、page/submission readiness 等审计。
6. 证据不足或格式不合规时保持 blocked，并输出可读阻塞原因。

## 脚本结构

| 目录 | 作用 |
| --- | --- |
| `scripts/core/` | 路径、JSON/text、venue policy、LaTeX 和审计公共函数。 |
| `scripts/pipeline/` | 主流水线、独立运行、项目输入包、Claude 写作桥。 |
| `scripts/venue/` | 官方 venue 要求解析和模板下载。 |
| `scripts/rendering/` | Markdown/结构化稿转 TeX、PDF 编译。 |
| `scripts/audit/` | 证据、引用、图表、normality、submission readiness、claim ledger 审计。 |
| `scripts/repair/` | 图表、引用、预览稿、Markdown 修复循环。 |
| `scripts/review/` | 内部评审、作者回应、再评审。 |
| `scripts/maintenance/` | 内部 skill/helper 完整性检查。 |

## 内部 skills

`skills/` 中保存写作规约，如 `taste-paper-writing`、`venue-intelligence`、`citation-integrity`、`nature-family-writing`、`writing-quality` 和内化的 PaperOrchestra 工作流。运行时不再依赖 `vendor/`。

## 维护原则

- 不恢复 `vendor/`。需要的外部知识必须整理进 `skills/` 或干净脚本。
- 不把运行产物、PDF、LaTeX 临时文件、缓存提交进 git。
- 不把 web 前端逻辑写进本模块。
- 新增脚本放入对应功能子目录，优先复用 `core/`。
- 没有真实证据就保持门控阻塞，不能把预览稿当作投稿通过。
