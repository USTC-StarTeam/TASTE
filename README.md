# TASTE: 自动科研工作流

TASTE 是一个自动科研系统。它把论文发现、精读、想法生成、实验计划、环境配置、实验迭代和论文撰写放进同一个网页工作台：Web 负责配置、命令传输和产物展示，Framework 是唯一编排与项目同步后端，各阶段模块负责本阶段能力，Claude Code 负责需要代理完成的内容生成、代码、实验和论文工作。

默认网页地址：

```text
http://127.0.0.1:8879
```

## 必要环境

| 环境 | 用途 | 建议 |
| --- | --- | --- |
| Conda / Mamba + Python | 运行 TASTE 后端、调度脚本、Find 和审计脚本 | 必须使用名为 `taste` 的环境，推荐 Python 3.11 |
| Node.js + npm | 构建 React/Vite 网页前端 | 按 Node.js 官网推荐版本安装 |
| Claude Code CLI (`claude`) | Read、Ideas、Plan 以及 Environment/Experiment/Paper 各模块的 Claude 能力 | 用户自己安装、登录和维护账号 |
| Find 阶段 LLM API | Find 的标题筛选、摘要评分和推荐排序 | 在网页里配置，密钥保存到本机私有 `modules/finding/config/llm.local.json` |

TASTE 区分两类 Python：

- `management_python`：运行 Web、调度、Find、审计等 TASTE 框架脚本；启动网页前就应该可用。
- `experiment_python`：运行具体科研项目的训练、评估和仓库脚本；在 Environment/环境配置阶段配置或由项目环境部署流程检测。

两者可以相同，但真实实验建议分离，避免训练依赖污染 TASTE 管理环境。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/USTC-StarTeam/TASTE.git TASTE
cd TASTE
```

### 2. 创建 Python 管理环境

Conda/Mamba：

```bash
conda create -n taste python=3.11 -y
conda activate taste
python -m pip install --upgrade pip
python -m pip install -r framework/requirements.txt
```

### 3. 安装 Node.js

Node.js、npm 和版本管理方式直接按 Node.js 官网下载页操作：<https://nodejs.org/zh-cn/download/>。

安装完成后确认：

```bash
node --version
npm --version
```

### 4. 安装并登录 Claude Code

TASTE 不配置 Claude Code 账号/API，也不会覆盖用户已有 Claude Code 设置。按 Claude Code 官方文档安装并登录即可：<https://docs.anthropic.com/en/docs/claude-code/setup>。

常用 npm 安装方式：

```bash
npm install -g @anthropic-ai/claude-code
```

确认当前终端可找到 Claude Code：

```bash
claude --version
```

### 5. 启动网页

```bash
cd TASTE
conda activate taste
framework/scripts/start_web.sh
```

启动脚本会自动补齐前端依赖、构建前端，并使用默认地址 `127.0.0.1:8879`。需要改端口时只设置端口变量：

```bash
WEB_PORT=<port> framework/scripts/start_web.sh
```

Windows 可以在 Git Bash、PowerShell 或 WSL 中运行。推荐安装 Git for Windows 后直接在 PowerShell 中调用同一个启动脚本：

```powershell
bash framework/scripts/start_web.sh
```

如果 TASTE 部署在服务器，远端仍然执行 `framework/scripts/start_web.sh`，本地浏览器通过 SSH tunnel 访问：

```bash
ssh -L 127.0.0.1:8879:127.0.0.1:8879 <user>@<server>
```

随后在浏览器打开：

```text
http://127.0.0.1:8879
```

## 配置教程

第一次打开网页后，先在左侧栏创建或选择项目，再认真填写研究画像、Find 阶段 LLM 和运行环境。研究画像建议写得尽量详细，它会直接影响论文匹配、标题筛选、摘要评分、idea 生成和后续 Claude Code 判断。投稿目标、实验 Python、实验轮数等配置分别在对应模块里设置，不放在全局侧栏。

配置保存原则：

| 类别 | 保存位置 | 说明 |
| --- | --- | --- |
| 公开模板 | `config.example.json`、`framework/resources/templates/project.json` | 给新用户和新项目提供默认结构，不含真实密钥和真实项目内容。 |
| 本机网页配置 | `framework/.runtime/.config.json` | 保存非敏感网页偏好、预算和运行参数；LLM API key 不放这里。 |
| 本机 LLM 私有配置 | `modules/finding/config/llm.local.json` 或 `FINDING_LLM_CONFIG` 指定路径 | 保存 Find LLM 的 provider/base URL/model/API key/temperature；被 `.gitignore` 忽略，不提交，不写入项目状态。 |
| 具体科研项目 | `projects/<project>/` | 保存项目配置、状态、运行记录、产物、仓库、数据和论文草稿。 |
| 前端静态产物 | 网页客户端构建目录 | 由启动脚本或 npm build 生成，网页服务读取这里的文件。 |

### 左侧栏配置

| 区域 | 配置项 | 作用 | 建议 |
| --- | --- | --- | --- |
| 项目 | 当前项目 | 选择正在操作的项目，网页状态、运行历史和产物都会跟随项目切换。 | 默认会加载最近使用的项目；需要新课题时再创建新项目。 |
| 项目 | 创建项目 / 项目 ID | 创建 `projects/<project>/`，建立稳定项目身份。 | 使用小写字母、数字、下划线或连字符，例如 `my_retrieval_study`。 |
| 项目 | 研究主题 | 当前项目的核心研究问题。 | 写成一句清楚的问题或方向；后续 Find、Read、Ideas、Plan 都会读取。 |
| 画像 | 研究兴趣 | 描述当前真正想匹配的论文范围。 | 尽量详细写研究问题、方法关键词、应用领域、正例/反例、偏好的 venue/年份、数据和代码要求；不要只写一个宽泛主题。 |
| 画像 | 研究者画像 | 描述已有项目、技术背景、实验条件、长期方向和评价标准。 | 写清楚可用算力、熟悉框架、数据访问限制、希望避免的方向和什么算“值得推进”；越具体，论文匹配和后续计划越稳定。 |
| LLM | provider | Find 阶段 LLM 供应商类型。 | OpenAI 兼容服务通常填 `openai_compatible`；也可按服务端支持填写。 |
| LLM | base URL | LLM API 地址。 | 填供应商提供的 `/v1` 兼容地址。 |
| LLM | model | Find 使用的模型名。 | 选择稳定、便宜、支持 JSON 输出的模型。 |
| LLM | API key | Find 阶段调用 LLM 的密钥。 | 在网页输入后保存到本机私有 `modules/finding/config/llm.local.json`；页面不会回显完整 key。 |
| LLM | temperature | Find 评分和少量兜底生成的随机性。 | 过滤/评分建议 0.2-0.6，默认 0.4。 |
| LLM | 角色 LLM 配置 | 高级兼容项；当前公开工作流中 Find 使用配置的 LLM，Read/Ideas/Plan 使用各自 Claude Code 路由。 | 普通使用留空，继承全局 Find LLM。 |
| LLM | 邮件配置 | 手动或自动发送当前 run 的 Markdown 产物。 | 不需要邮件通知时保持默认即可。 |
| 运行环境 | node_bin | Node/npm 所在目录。 | 自动检测优先；网页找不到 node/npm 时再手动填写。 |
| 运行环境 | claude_path | Claude Code 可执行文件路径。 | 普通终端能执行 `claude` 时通常无需填写；检测失败时填绝对路径。 |
| 运行环境 | management_python | TASTE 管理 Python。 | 启动网页的 Python 通常会自动写入；需要固定环境时手动填写。 |
| 运行环境 | extra_path | 额外 PATH 目录。 | 只在工具安装在非标准目录时填写，多个目录用 `:` 分隔。 |
| 历史运行 | run 列表 | 查看当前项目的历史 run、阶段和产物。 | 历史 run 为只读；切换后不会生成、编辑、批准或选择当前项目产物。 |

### 七个模块配置

| 模块 | 配置项 | 作用 | 默认 / 建议 |
| --- | --- | --- | --- |
| Find | 会议搜索 | 从会议库里搜索会议/期刊。 | 默认只显示部分会议；用搜索框定位后点击添加。 |
| Find | 选择年份 | 设置“待添加年份”。 | 默认最新一年；修改年份不会改变已选会议，只有点击“添加”才写入该会议。 |
| Find | 已选会议 | 决定会议标题池来源。 | 默认全不选；同一会议可添加多个年份，并按年份独立抓取。 |
| Find | 检查可抓取 | 检查所选来源是否可访问并显示来源状态。 | 正式运行前建议点一次。 |
| Find | arXiv / bioRxiv / Nature / Science / HuggingFace / GitHub | 非会议来源开关。 | 默认全不选；只勾选本轮确实需要的来源。 |
| Find | arXiv 分类、检索词、日期 | 控制 arXiv 的分类、主题 query 和时间窗。 | 日期留空时默认最近 180 天；检索词留空时由主题自动生成。 |
| Find | bioRxiv 分类、日期 | 控制 bioRxiv 学科分类和时间窗。 | `all` 表示不过滤分类。 |
| Find | Nature / Science 预设、期刊、文章类型、日期 | 控制期刊流来源。 | 默认关闭；打开后再选择具体期刊和文章类型。 |
| Find | GitHub 语言 | 控制 GitHub 趋势榜语言过滤。 | 可填 `all`、`python`、`javascript` 等。 |
| Find | 推荐文章数量 | Find 最终展示的推荐论文上限。 | 默认 20；这是最终展示数量，不是抓取上限。 |
| Find | LLM 评估并发数 | 控制 Find 评分请求并发。 | 默认 8；慢速或限流 API 建议 4-8。 |
| Find | 高级预算 | 非会议抓取、arXiv query 数、会议标题扫描上限、召回池和详情池。 | 标准使用保持默认；`venue_title_scan_limit=0` 表示会议标题不设数量上限。 |
| Read | 运行精读 | 对当前 Find 推荐论文执行精读和边界审计。 | 需要先有当前 Find run。 |
| Read | 精读状态 | 展示推荐论文数、当前展示数、全文精读完成数和待补项。 | 主要用于判断 Read 是否拿到足够证据。 |
| Ideas | 想法最大数量 | 控制生成研究想法数量上限。 | 默认 6。 |
| Ideas | 人工修改与 `idea.md` 产物 | 页面上方用字段卡片修改标题、新方法和初步实验，也可切换到 Markdown 源文；页面右下产物栏直接渲染最终 `idea.md`。 | 保存时原地更新同一个 Idea run；不会为每次编辑新建 run。 |
| Ideas | 想法状态 | 将 Markdown 中的候选想法标记为通过、待定或删除。 | 只有通过的想法进入 Plan 候选。 |
| Plan | 已批准 Ideas | 从当前 Find 的已批准 Ideas 中选择本次 Planning 输入。 | 至少选择一个；支持单选和多选，默认全选。 |
| Plan | 修复轮数 | Claude 生成初版后精确执行的修复次数。 | 默认 3；设为 0 时只保留初版。 |
| Plan | 计划操作 | 页面右上选择候选、继续优化、完成，并直接编辑最终 `plan.md`。 | 保存命令交给 Framework 校验和同步；右下产物栏直接渲染同一文件。 |
| Plan | 候选计划操作 | 继续润色候选，或由人选定唯一执行计划。 | 不会默认把第一个候选当执行计划。 |
| Plan | 让主控 Claude Code 选择唯一执行计划 | 将候选计划交给主控 Claude Code，形成执行合同。 | 进入实验前建议完成。 |
| Environment | conda 环境名称 | 具体科研项目使用的实验环境名。 | 在环境配置阶段填写；不是左侧栏全局配置。 |
| Environment | conda base | Conda/Mamba 安装根目录。 | 自动检测优先；检测失败时手动填写。 |
| Environment | 实验 Python | 训练和评估命令使用的 Python。 | 可由 conda base + 环境名派生，也可显式填写绝对路径。 |
| Environment | 真实创建 Conda 环境 | 是否让环境步骤实际创建/检查实验环境。 | 首次环境部署时开启；环境锁定后网页不再重复创建。 |
| Environment | 自然语言请求 | 给 Environment 主控 Claude 的环境部署说明。 | 说明仓库、数据、复现目标和限制。 |
| Experiment | 科研迭代轮数 | 控制实验子循环轮数。 | 从小轮数开始，确认日志和证据正常后再扩大。 |
| Experiment | 运行实验迭代 | 向当前项目唯一的 Experimenting 主控 Claude 发送继续工作命令。 | 先完成 Environment handoff。 |
| Experiment | 主控对话 | 向同一个 Experimenting 会话发送人工指令。 | 忙碌时可排队，也可打断当前任务并优先处理。 |
| Paper | 投稿会议/期刊 | 论文模板、格式门控和页面限制目标。 | 只在论文撰写页配置；不要写在左侧栏主题里。 |
| Paper | 论文标题 | 论文草稿标题。 | 可先留空或写工作标题，后续由 Writing 主控 Claude 修订。 |
| Paper | 自动安装 LaTeX | 允许论文阶段尝试安装或补齐 LaTeX 工具。 | 服务器可安装依赖时开启；受限环境中关闭并手动准备。 |
| Paper | 生成与修订论文 | 启动论文代理，生成/修复 TeX、PDF、引用、图表和证据门控。 | 预览 PDF 不等于投稿通过；投稿状态以门控为准。 |

## 使用说明

### 左侧栏

左侧栏负责“项目级上下文”和“运行工具”。项目、研究兴趣、研究者画像会参与 Find、Read、Ideas、Plan 和后续模块主控 Claude 判断；其中研究画像越详细，TASTE 越能区分“真正匹配的论文”和“只是泛泛相关的论文”。LLM 只负责 Find 的标题/摘要评分和少量兼容兜底；运行环境只负责让网页后端找到 `node`、`npm`、`claude` 和管理 Python。

TASTE 不管理 Claude Code 账号，也不会写入用户的 Claude Code API。只要当前用户终端能正常运行 `claude`，网页通常可以通过自动检测找到它；检测失败时再填写 `claude_path` 或 `extra_path`。

### Find：发现候选论文和代码线索

Find 的输入来自研究主题、研究兴趣、研究者画像、已选会议/年份和勾选的非会议来源。会议来源会先扫描所选会议/年份的标题池，再按主题相关性保留候选进入详情抓取；非会议来源按各自 API 或 RSS 获取候选。之后系统会抓取摘要/详情，用 Find LLM 做评分和排序，最终只展示通过真实摘要和评分门控的推荐论文。

Find 页面同时展示来源状态、调研验收计数和当前 run 产物。会议默认不设标题数量上限，`venue_title_scan_limit=0` 表示全扫；测试或异常源保护时才需要设正数。arXiv 只有勾选后才抓取，日期留空时默认最近 180 天。

### Read：精读推荐论文

网页中的 Read 由 Framework 把当前 Find 推荐项转换成通用论文输入，再调用 Reading 获取同篇全文并生成精读。Find 已提供的 URL、PDF、DOI、OpenReview ID、作者和来源会用于加快全文定位；缺少这些字段时仍可从标题开始查找。Reading 命令行也可独立接收标题或论文列表，不读取项目或 Find 状态。页面只展示当前 Find 对应的精读状态，避免把历史 run 的内容混入当前项目判断。

### Ideas：生成和筛选研究想法

Ideas 基于当前 Find/Read 产物生成可实验化的研究想法。页面上方保留人工修改卡片，可编辑标题、新方法和初步实验，也可切换到完整 Markdown 源文；页面右下产物栏使用 Markdown 解析库和 KaTeX 直接渲染最终 `idea.md`。通过、待定和删除状态仍在修改卡片内操作；只有通过的想法进入 Plan。

Ideas 正式生成使用 Claude Code。Framework 负责校验当前 Find/Read、构建输入包和调用模块；网页只传递命令与配置。想法数量只影响 Ideas，不影响 Read、Plan、Environment、Experiment 或 Paper。

当前 Find 的推荐结果可能很大。Framework 会先提取 Ideas 所需证据，生成一个规范化输入包；Idea 模块只保存这一份 `input_bundle.json`，不再同时复制 Find/Read 文件到 run 顶层和 `input/` 子目录。idea 明细只保存在 `idea.md` 和由它派生的 `ideas.json` 中，项目 state 只记录数量、门控和选择状态。

### Plan：形成可执行实验计划

Framework 校验当前 Find 中明确批准的 Ideas，并只把 Web 本次勾选的非空子集交给 Planning；可以选择一个或多个，不传选择时默认使用全部已批准项。Planning 为每个选中 Idea 生成候选并执行评估/修复。正式模式默认使用 Claude Code，最终由 Claude 直接写 `plan.md`，回读检查栏目、公式、网页引用和重复内容，再通过发布校验。

当前 Plan 已存在时，后续 Idea 修改由 Framework 识别是否影响已规划 Idea；受影响时旧的当前 Plan 会失效，并按原已规划且仍批准的 Idea 自动重生成。历史 Planning run 只供查看，不参与当前项目判断。

Plan 页面保持三栏布局：左侧选择 Ideas，中间设置精确修复轮数，右侧“计划操作”选择候选、继续优化、完成，并直接修改 `plan.md`。页面下方右侧产物栏渲染 Framework 同步后的同一个 `plan.md`；页面主体不再复制一份正文。`plans.json` 只保留 ID、顺序、版本、选择状态和 Markdown 审计，不复制标题或正文。

进入 Environment、Experiment 或 Paper 前，必须有且只能有一个当前执行合同。完成或选择计划后，TASTE 会把 `selected_idea_id`、`selected_plan_id` 和 `selected_plan_only` 执行策略写入项目状态；未选中的想法和计划只作为备选，不会驱动下游阶段。

### Environment：选择基底、检查数据并锁定实验环境

Environment 根据 Find/Plan 的证据选择最适合跟进的仓库或基底，检查真实数据/loader 是否可用，并准备具体科研项目使用的 Conda/Python 环境。这个阶段配置的是 `experiment_python`，它只服务训练、评估和外部仓库脚本，不应混同于 TASTE 管理 Python。

环境配置是一次性创建逻辑：成功创建或确认后会锁定，网页不会反复安装、修改或重建环境。之后 Experiment 和 Paper 复用已经锁定的环境状态与证据。

每个项目只有一个 Environment 主控 Claude 会话。网页显示同一会话的历史指令与回复；忙碌时消息在 Environment 模块中持续排队，不设置等待超时，也可选择打断当前 turn 并优先处理。Web 已保存的 Conda 名称会原样使用；未填写时由 Environment 主控选择合法名称，Framework 将名称写入项目配置，并在 handoff 通过后固定 prefix 和实验 Python。Environment run 不复制。

### Experiment：真实代码和实验迭代

Experiment 由 Experimenting 模块自己的主控 Claude 执行。它会围绕当前执行计划检查代码、修改实现、启动实验、读取日志/loss、分析坏例、记录指标和下一步行动。页面展示实验与复现门控、当前主线摘要、实验记录表和证据路径；旧历史记录不会被当成当前路线的新证据。

同一项目只有一个 Experimenting 主控会话，实验按钮和网页对话都使用它。主控忙碌时网页消息显示为排队；选择打断后，新消息优先执行，随后同一会话恢复原实验工作。

实验必须先完成最终验证，再解析指标并写 registry/CSV/Markdown 记录。完成态记录需要验证结束时间、验证返回码和记录时间，确定性 Gate 会阻止顺序错误的记录进入完成态。实验结果只有在真实数据、loader、复现和审计证据满足门控时才会进入可写论文的候选证据；失败、阻塞和负结果也会保留。

### Paper：生成论文预览并执行门控修复

Paper 根据投稿会议/期刊、当前计划、实验记录和审计证据生成或修订论文。Writing 模块自己的主控 Claude 会检查官方模板、页面限制、引用渲染、图表质量、自审发现和证据门控。页面可以展示 PDF/TeX 预览，但预览稿不代表投稿通过；是否可投稿以门控状态为准。

如果实验或证据门控未通过，Paper 仍可生成目标 venue 预览，方便查看结构和格式，但不会把缺失证据包装成正式结论。

### Full-cycle：顺序执行七个模块

完整科研循环不拥有额外主控 Claude，也不包含阶段专用科研判断。它按 Find、Read、Idea、Plan、Environment、Experimenting、Writing 的顺序触发与网页按钮相同的 Framework action；Plan 生成后会自动调用 Claude 选择唯一执行计划，再进入 Environment。任一动作返回阻塞或失败时立即停止。

### 任务栏和产物

页面底部任务栏展示当前项目的 job/run 状态、阶段进度、最近日志、命令和产物路径。Find/Read/Ideas/Plan 的 Markdown 产物只在对应页面展开；Environment、Experiment、Paper 展示各自阶段的真实状态与证据，避免把文献调研日志误看成实验或论文结论。

任务栏默认显示面向用户的阶段摘要，不直接刷长 JSON、完整 agent transcript 或大段 stdout。例如 Plan 运行中会显示正在为哪个 Idea 生成计划，完成后显示阶段完成、运行编号和保留的原始日志行数；需要细看产物时再打开对应 artifact。

## 重要目录

根目录只保留源码、配置样例和私有项目工作区。过去散落在根目录的 `prompts/`、`templates/`、`automation/`、`.claude/` 已迁入 `framework/resources/`；临时输入、锁、任务状态和缓存由对应组件保存在各自忽略的 `.runtime/`。

```text
.
├── AGENTS.md                      # 维护 TASTE 仓库的 agent 规则，不是项目 Claude 研究交接
├── CLAUDE.md                      # 维护/运行 TASTE 时给 Claude Code 的仓库级说明
├── README.md                      # 用户说明与根目录规划
├── SECURITY.md
├── LICENSE
├── config.example.json            # 公开配置样例，不含密钥
├── framework/                     # TASTE 框架层、编排脚本、共享包、公共资源
│   ├── .runtime/                 # Framework 本机配置、临时输入、锁和兼容运行态，整体忽略
│   ├── scripts/                  # 框架入口脚本与 auto_research 共享包
│   │   └── auto_research/
│   └── resources/
│       ├── templates/             # 项目/论文模板
│       ├── prompts/               # 框架 prompt 与 subagent 启动提示
│       ├── automation/            # subagent 协议和角色资源
│       └── claude/                # TASTE 提供的 Claude agent/command/skill 模板
├── modules/                       # 七个独立科研模块
│   ├── finding/
│   ├── reading/
│   ├── ideation/
│   ├── planning/
│   ├── environment/
│   ├── experimenting/
│   └── writing/
├── web/                           # FastAPI bridge 与 React/Vite 前端；.runtime 保存本机 Web job 状态
├── tests/                         # 回归测试
├── projects/                      # 本机私有项目工作区，git 只保留 .gitkeep
└── third_party/                   # 外部参考 checkout，整体忽略
```

根目录不规划 `runtime/`、`handoff/`、`logs/`、`reports/`、`state/`、`status/`、`discover/`、`raw/`、`obsidian/`、`tmp/`、`.probe/`、`.runtime/` 或 `.agents/`。项目产物进入 `projects/<project>/`；可丢弃的组件运行态只进入该组件的 `.runtime/`。

常用入口：

| 路径 | 作用 |
| --- | --- |
| `framework/scripts/create_project.py` | 根据 `framework/resources/templates/project.json` 创建项目。 |
| `framework/scripts/start_web.sh` | 启动 Web/API，默认 `127.0.0.1:8879`。 |
| `framework/scripts/auto_research/` | 框架层共享 Python 包：配置、模型、输入桥、项目命令、运行编排和 Web 状态投影。Find 后端不导入它，Find 的模型、LLM、路径和存储实现位于 `modules/finding/scripts/finding_runtime/`。 |
| `framework/scripts/project_paths.py` | 统一项目路径、资源路径、PythonPath 和 runtime 配置。 |
| `framework/scripts/runtime_env.py` | 检测和构造 Python、Node、Claude Code 运行环境。 |
| `modules/finding/main.py --action find` | Find 模块公开入口；内部流水线和 runtime 均在模块私有 `scripts/` 中，不依赖框架脚本或其它模块。 |
| `framework/scripts/run_module.py reading --action current_find_research_plan --project <project>` | 当前 Find 对应的 Read 路由。 |
| `framework/scripts/run_module.py ideation --action idea --project <project>` | 校验 Read 后生成并同步当前 Find 的 `idea.md`。 |
| `framework/scripts/run_module.py planning --action plan --project <project> [--idea-id <id> ...]` | 校验同一 Find，并从一个或多个选中的已批准 Ideas 生成 Plan。 |
| `framework/scripts/run_module.py planning --action select --project <project>` | 让 Claude Code 从当前候选中选择唯一执行计划。 |
| `modules/environment/main.py --action deploy_from_plan --project <project> --plan <plan.json>` | 环境配置阶段。 |
| `framework/scripts/run_module.py experimenting --action work --project <project>` | Framework 调用项目唯一 Experimenting 主控的入口。 |
| `framework/scripts/run_module.py experimenting --action chat --project <project> --message <text>` | Framework 转发 Experimenting 网页对话、排队和中断选项。 |
| `framework/scripts/run_module.py writing --action work` | 通过 Framework 将论文生成、修复和审计任务交给项目唯一的 Writing 主控 Claude。 |
| `web/backend/auto_research/web/server.py` | FastAPI 后端。 |
| `web/frontend/client/src/App.tsx` | React 前端主界面。 |

运行态项目目录：

```text
projects/<project>/
├── project.json
├── AGENTS.md / CLAUDE.md          # 项目 Claude Code 可读取的项目级规则
├── state/
├── planning/
├── reports/
├── logs/
├── runs/
├── artifacts/
├── repos/ 或 third_party/
└── paper/
```

这些目录通常包含私人路径、下载仓库、数据、日志、论文草稿和未公开结论，默认不提交。各模块主控 Claude 的研究交接、工作记忆和科学状态只能留在项目目录，不能写回仓库根部。

## 模块边界

TASTE 的科研能力按七个阶段拆成 `modules/finding/`、`modules/reading/`、`modules/ideation/`、`modules/planning/`、`modules/environment/`、`modules/experimenting/`、`modules/writing/`。每个阶段目录都包含：

- `README.md`：中文说明模块输入、输出、运行逻辑、脚本清单和冗余控制原则。
- `main.py`：该阶段唯一公开后端入口，负责 action 路由，并通过 `--contract` 输出外部输入、输入产物、输出产物和职责边界。
- `script_manifest.json`：当前 `scripts/` 顶层脚本归属、函数、imports 和归属理由。

`modules/` 只放七个科研阶段模块。`framework/` 负责运行时、任务队列、输入门控、项目状态、共享模型、公共资源、模块调用和跨阶段编排；`web/` 的 FastAPI/React 层只做人类交互、配置与指令传输、任务状态和产物展示，不直接调用模块或同步模块文件。Find 不负责全文抓取；全文证据和阅读包属于 Read。Ideas/Plan 不应提前依赖 Environment 的确认结果；Environment/Experiment/Writing 只能消费显式产物和选择合同，不能用历史状态替代当前输入。

## 维护原则

- 优先让 TASTE 主流程一次跑对；fallback 只作为最后兼容路线，不能用来掩盖主流程质量问题，更不能把失败直接甩给用户网页。
- 修改前必须读清相关前后端和模块逻辑，找到根因后再改；禁止只按某篇论文、某个研究主题、某台机器路径写特例。
- 所有代码脚本不能包含当前研究项目、工作环境、API key 或本机绝对路径的硬编码。
- Framework/Web 临时输入、任务状态、日志和缓存只进各自 `.runtime/`；模块运行态只进对应模块 `.runtime/`；项目产物进 `projects/<project>/`；源码根目录不新增运行态目录。
- 测试运行 TASTE 功能时，除 debug/单元测试外应通过网页操作，并亲自视觉检查对应页面的所有关键元素和产物文本。
- 每次较大修改要维护 `工作状态.txt`，并在工作树稳定后及时提交 git；该文件是本机维护状态，不提交。

## 参考与致谢

本项目在设计和实现过程中参考了若干项目的思路与部分实现方式。相关第三方项目各自遵循其原始许可证，TASTE 在本仓库中保留可审计的来源说明。

- **iDeer**：研究助手流程、信息源聚合、报告生成和邮件报告设计。
- **openccf**：CCF 目录结构、会议/期刊元数据组织和 DBLP 抓取策略；本仓库内置的 `modules/finding/data/ccf_venues.json` 是基于 openccf 公开 CCF 数据整理出的归一化 venue catalog。
- **[ICLR2026-Guide-CN](https://github.com/JenniferZhao0531/ICLR2026-Guide-CN)**：OpenReview/ICLR 论文收集、组织和展示方式。
- **[ccf-deadlines](https://github.com/ccfddl/ccf-deadlines)**：会议信息组织、截止日期元数据和用户侧 venue 工作流设计。
- **[academic-research-skills](https://github.com/Imbad0202/academic-research-skills)**：Claude Code 学术研究技能组织方式，以及 research -> write -> review -> revise -> finalize 的阶段化写作/审稿思路。
- **[PaperOrchestra](https://github.com/Ar9av/PaperOrchestra)**：多代理论文写作、outline/plot/literature/section/refinement 分工、引用核验、图表检查和论文质量自评思路。
- **[nature-skills](https://github.com/Yuan1z0825/nature-skills)**：面向 Nature 风格论文的学术表达、图表规范和写作检查技能设计。

TASTE 也感谢 FastAPI、React、Vite、Claude Code 以及 arXiv、bioRxiv、Nature、Science、HuggingFace、GitHub、DBLP/CCF/会议索引等公开工具与数据源；这些基础设施让本地可审计的科研自动化成为可能。

## 许可证

TASTE 使用 GNU Affero General Public License v3.0。详见 [LICENSE](LICENSE)。
