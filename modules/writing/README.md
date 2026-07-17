# Writing / 论文撰写模块

Writing 为每个 TASTE 项目维护一个独立的主控 Claude Code 会话。它读取项目已经选定的 Idea、Plan、实验记录和原始证据，在项目目录中生成目标 venue 的论文，并执行“独立审计、主控返修、再次独立审计”闭环。

Writing 只负责论文撰写、修订、引用、图表、模板和论文质量。实验结果、指标和坏例必须来自项目已有证据。

## 使用条件

所有调用必须使用 conda `taste` 环境。正式写作还需要当前环境能找到 `claude` CLI；生成 PDF 需要可用的 LaTeX 工具。

```bash
cd <TASTE_ROOT>
conda run -n taste python framework/scripts/main.py module writing --contract
```

Writing 没有文本模型降级写作路径。`claude` 不可用时会明确阻塞。

## 开始写作

项目需要已经存在目标 venue、唯一 selected Idea/Plan，以及可供论文使用的实验记录。通过 Framework 启动：

```bash
conda run -n taste python framework/scripts/main.py module writing \
  --action work \
  --project <project_id> \
  --venue "ICLR 2027"
```

可选参数：

- `--title`：论文工作标题。
- `--max-audit-repair-rounds`：独立审计阻塞后的最大返修轮数，默认 2。
- `--timeout-sec`：单次 Claude 调用超时。
- `--dry-run`：验证会话和队列接口，不调用 Claude。

`work` 不创建 run。Writing 主控的工作目录固定为：

```text
projects/<project_id>/
```

论文产物固定写入：

```text
projects/<project_id>/paper/writing/
```

## 网页对话

Paper 页的 Writing 主控对话框直接向该项目唯一的 Writing 主控 Claude 发送命令。Web 只把消息交给 Framework；Framework 调用 Writing 公共入口，Web 不调用模块，也不复制模块文件。

命令行等价调用：

```bash
conda run -n taste python framework/scripts/main.py module writing \
  --action chat \
  --project <project_id> \
  --message "检查引用真实性并修复发现的问题"
```

主控忙碌时：

- 普通发送进入 Writing 模块队列，网页显示排队消息正文。
- “打断当前任务并优先发送”会终止当前 Claude 进程，先处理网页指令。
- 网页指令完成后，同一个 Writing 会话恢复被打断的正式写作或返修任务。
- 同一项目的所有 Writing 工作只能续接同一个模块会话。

网页回复来自：

```text
projects/<project_id>/state/writing_controller_last_result.json
projects/<project_id>/state/writing_controller_history.json
```

## 完整工作流程

1. Framework 把明确的项目、venue 和用户指令传给 `modules/writing/main.py`。
2. Writing 从模块自己的项目到会话对照表取得该项目唯一的 Claude UUID。
3. Writing 以 `projects/<project>/` 为 Claude 工作目录，要求主控先读 Writing 总规约和 skill-router。
4. skill-router 为正式写作加载 venue、引用、质量和 PaperOrchestra 全链路 skills；条件型任务只加载匹配的 skill。
5. 主控读取 selected Idea/Plan、Find/Read、实验 registry、records、报告和原始日志。
6. 主控直接更新 canonical `paper/writing/` 中的 venue contract、模板、TeX、BibTeX、图表、审计支持文件和 PDF。
7. 主入口启动一个不带主控 session ID 的全新 Claude 进程执行独立审计。
8. 审计返回 `blocked` 时，必须给出具体原因、文件、证据或计数，以及可复查的修复指导；主入口把这些意见交回同一个 Writing 主控会话返修。
9. 每轮返修后重新启动新的独立审计，直到 `pass` 或达到返修上限。

## 会话与队列状态

模块自己的唯一会话对照表：

```text
modules/writing/.runtime/controller_sessions.json
modules/writing/.runtime/controllers/<project>/controller.json
```

项目中的公开状态：

```text
projects/<project>/state/writing_controller.json
projects/<project>/state/writing_controller_last_result.json
projects/<project>/state/writing_controller_history.json
```

`.runtime` 只保存 Writing 自己的会话映射、队列和进程回执，不保存论文 run 或项目科学产物。

查看状态：

```bash
conda run -n taste python framework/scripts/main.py module writing \
  --action controller_status \
  --project <project_id>
```

## 论文产物

| 路径 | 内容 |
| --- | --- |
| `paper/writing/venue/venue_requirements.json` | 当前官方 venue、year、track 要求和来源。 |
| `paper/writing/venue/template_source.json` | 官方模板来源。 |
| `paper/writing/workspace/inputs/template.tex` | 当前论文使用的模板入口。 |
| `paper/writing/workspace/final/paper.tex` | canonical 论文源码。 |
| `paper/writing/workspace/final/paper.pdf` | 编译成功后的论文 PDF。 |
| `paper/writing/workspace/refs.bib` | 真实且与正文 key 一致的引用库。 |
| `paper/writing/workspace/audits/` | claim、页数和每轮独立质量审计。 |
| `paper/writing/workspace/repair_rounds/` | 主控根据审计意见执行的返修回执。 |
| `paper/writing/workspace/provenance.json` | 输入、证据、venue、模板、引用和编译来源。 |
| `paper/writing/audit_repair_loop.json` | 审计与返修轮次摘要。 |
| `paper/metadata/paper_pipeline.json` | Framework/Web 使用的论文公开状态。 |

## Skills

`modules/writing/SKILL.md` 是 Writing 的总工作边界；`modules/writing/skills/skill-router/SKILL.md` 是每次 Claude turn 的 skill 选择入口。主控 prompt 强制先读取这两个文件，再读取 router 选中的具体 skill。

检查所有 skill 是否存在且都有路由：

```bash
conda run -n taste python framework/scripts/main.py module writing --action assets
```

当前 `modules/writing/scripts/` 不保留运行脚本。`skills/*/scripts/` 中仍存在的 Python 文件是 skill 明确调用的确定性环境工具，例如模板初始化、BibTeX/引用一致性、LaTeX 检查和证据门控；它们不管理会话、不决定论文结论，也不能作为 Writing 公共入口。

## 结果判断

只有独立审计为 `pass`，且 canonical TeX/PDF、真实引用、venue contract、页数审计和 claim-evidence 审计齐全时，Framework 才会显示会议论文预览就绪。PDF 存在但审计阻塞时，只能作为检查预览。
