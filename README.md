# TASTE: 自动科研工作流

TASTE 是一个自动科研系统。它把论文发现、精读、想法生成、实验计划、环境配置、实验迭代和论文撰写放进同一个网页工作台：Web 负责配置、命令传输和产物展示，Framework 是唯一编排与项目同步后端，各阶段模块负责本阶段能力，Claude Code 负责需要代理完成的内容生成、代码、实验和论文工作。

默认网页地址：

```text
http://127.0.0.1:8879
```

## 必要环境

| 环境 | 用途 | 建议 |
| --- | --- | --- |
| Linux/macOS + Git + `curl` 或 `wget` | 克隆仓库、下载论文/代码并运行 TASTE 脚本 | Windows 请使用 WSL |
| Conda / Mamba + Python | 运行 TASTE 后端、调度脚本、Find 和审计脚本 | Python 3.10+；完整七阶段工作流要求管理 Conda 环境名为 `taste` |
| Node.js + npm | 构建 React/Vite 网页前端 | Node.js 20+ |
| Claude Code CLI (`claude`) | Read、Ideas、Plan 以及 Environment/Experiment/Paper 各模块的 Claude 能力 | 用户自己安装、登录和维护账号 |
| Find 阶段 LLM API | Find 的标题筛选、摘要评分和推荐排序 | 在网页里配置，密钥保存到本机私有 `modules/finding/config/llm.local.json` |
| LaTeX + `latexmk`（可选） | 编译 Paper 阶段的 TeX/PDF | 需要生成 PDF 时安装 |

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
conda activate taste
python framework/scripts/main.py
```

启动脚本使用默认地址 `127.0.0.1:8879`。缺少前端 `dist/` 时会执行构建；如果 `node_modules/` 也不存在，会先运行 `npm install`。需要改端口时只设置端口变量：

```bash
WEB_PORT=<port> python framework/scripts/main.py
```

Windows 请在 WSL 中克隆并运行仓库：

```bash
conda activate taste
python framework/scripts/main.py
```

如果 TASTE 部署在服务器，远端仍然执行 `python framework/scripts/main.py`，本地浏览器通过 SSH tunnel 访问：

```bash
ssh -L 127.0.0.1:8879:127.0.0.1:8879 <user>@<server>
```

随后在浏览器打开：

```text
http://127.0.0.1:8879
```

## 配置教程

第一次打开网页后，先在左侧栏创建或选择项目，再认真填写研究画像、Find 阶段 LLM 和运行环境。研究画像建议写得尽量详细，它会直接影响论文匹配、标题筛选、摘要评分、Idea 生成和后续 Claude Code 判断；移开画像输入框时会自动保存到当前项目。配置 Find LLM 后先点击“保存配置”，再点击“验证 LLM”确认可调用。投稿目标、实验 Python、实验轮数等配置分别在对应模块里设置，不放在全局侧栏。

配置保存位置：

| 类别 | 保存位置 | 说明 |
| --- | --- | --- |
| 公开模板 | `framework/resources/templates/project.json` | 给新项目提供默认结构，不含真实密钥和真实项目内容。 |
| 本机兼容配置 | `framework/.runtime/.config.json` | 保存邮件配置和兼容副本；Find 参数以当前项目配置为准。 |
| 本机 LLM 私有配置 | `modules/finding/config/llm.local.json` 或 `FINDING_LLM_CONFIG` 指定路径 | 保存 Find LLM 配置；默认文件被忽略。API key 不写入项目文件，自定义路径也应放在 Git 忽略范围内。 |
| Read 本机私有配置 | `modules/reading/config/read.env` | 保存 Reading 实际使用的 OpenReview 登录信息和全文索引联系邮箱；默认文件被忽略。 |
| 具体科研项目 | `projects/<project>/` | 保存 `project.json`、`config/finding.json`、状态、运行记录、产物、仓库、数据和论文草稿。 |
| 前端静态产物 | `web/frontend/client/dist/` | 由启动脚本或 `npm run build` 生成，网页服务读取这里的文件。 |

### 左侧栏配置

| 区域 | 配置项 | 作用 | 建议 |
| --- | --- | --- | --- |
| 项目 | 当前项目 | 选择正在操作的项目，网页状态、运行历史和产物都会跟随项目切换。 | 有运行中任务时优先显示对应项目，否则恢复最近使用的项目。 |
| 项目 | 创建项目 / 项目 ID | 创建 `projects/<project>/`，建立稳定项目身份。 | 使用小写字母、数字、下划线或连字符，例如 `my_retrieval_study`。 |
| 项目 | 研究主题 | 当前项目的核心研究问题。 | 写成一句清楚的问题或方向；Find 会直接读取，并通过当前 Find 产物约束 Read、Ideas 和 Plan。 |
| 画像 | 研究兴趣 | 描述当前真正想匹配的论文范围。 | 尽量详细写研究问题、方法关键词、应用领域、正例/反例、偏好的 venue/年份、数据和代码要求；移开输入框后自动保存。 |
| 画像 | 研究者画像 | 描述已有项目、技术背景、实验条件、长期方向和评价标准。 | 写清楚可用算力、熟悉框架、数据访问限制、希望避免的方向和评价标准；移开输入框后自动保存。 |
| LLM | provider | Find 阶段 LLM 供应商类型。 | OpenAI 兼容服务通常填 `openai_compatible`；也可按服务端支持填写。 |
| LLM | base URL | LLM API 地址。 | 填供应商提供的 `/v1` 兼容地址。 |
| LLM | model | Find 使用的模型名。 | 选择稳定、便宜、支持 JSON 输出的模型。 |
| LLM | API key | Find 阶段调用 LLM 的密钥。 | 输入后点击“保存配置”，再用“验证 LLM”测试；页面不会回显完整 key。 |
| LLM | temperature | Find LLM 的兼容配置项。 | 默认 0.4；当前关键词抽取、分类和评分流程使用各自固定的低温参数，普通使用保持默认。 |
| LLM | 邮件配置 | 手动或自动发送当前 run 的 Markdown 产物。 | 不需要邮件通知时保持默认即可。 |
| 运行环境 | node_bin | Node/npm 所在目录。 | 自动检测优先；网页找不到 node/npm 时再手动填写。 |
| 运行环境 | claude_path | Claude Code 可执行文件路径。 | 普通终端能执行 `claude` 时通常无需填写；检测失败时填绝对路径。 |
| 运行环境 | management_python | TASTE 管理 Python。 | 默认采用启动服务的 Python；需要写入项目时点击自动检测或保存。 |
| 运行环境 | extra_path | 额外 PATH 目录。 | 只在工具安装在非标准目录时填写，多个目录用 `:` 分隔。 |
| 历史运行 | run 列表 | 查看或删除当前项目的历史 run。 | 历史 run 内容为只读；切换后不能生成、编辑、批准或选择其中的产物。 |

### 七个模块配置

| 模块 | 配置项 | 作用 | 默认 / 建议 |
| --- | --- | --- | --- |
| Find | 出版渠道搜索 | 从目录里搜索会议/期刊。 | 默认只显示部分渠道；用搜索框定位后点击添加。 |
| Find | 选择年份 | 设置“待添加年份”。 | 当前版本初始显示 2026；修改年份不会改变已选出版渠道，只有点击“添加”才写入该渠道。 |
| Find | 已选出版渠道 | 决定会议/期刊题录池来源。 | 选择按项目保存；同一渠道可添加多个年份，并按年份独立抓取。 |
| Find | 检查可抓取 | 有选择时抽样检查所选出版渠道/年份；未选择时检查内置高优先渠道。 | 正式运行前建议点一次；此按钮不检查扩展来源。 |
| Find | arXiv / bioRxiv / Nature / Science / HuggingFace / GitHub | 扩展来源开关。 | 选择按项目保存；只勾选本轮确实需要的来源。 |
| Find | arXiv 分类、检索词、日期 | 控制 arXiv 的显式分类约束、主题 query 和时间窗；该日期也用于 HuggingFace/GitHub。 | 分类留空表示不限制；日期留空时 arXiv 默认最近 180 天；检索词留空时由 LLM 根据主题、研究兴趣和研究者画像抽取。 |
| Find | bioRxiv 分类、日期 | 控制 bioRxiv 的显式学科分类约束和时间窗。 | 分类留空或填 `all` 表示不过滤；不会由 LLM 自动猜测分类；日期留空时默认最近 180 天。 |
| Find | Nature / Science 预设、期刊、文章类型、日期和候选上限 | 控制期刊流来源。 | 打开后选择具体期刊和文章类型；日期留空时默认最近 365 天，候选上限默认各 200。 |
| Find | GitHub 语言 | 控制 GitHub 趋势榜语言过滤。 | 可填 `all`、`python`、`javascript` 等。 |
| Find | 最低推荐数量 | Find 最终展示的推荐论文最低目标。 | 默认 20，配置值有效范围为 1-200；实际目标取该值与（出版渠道-年份组合数 + 已启用扩展来源数）× 5 的较大者；有效候选不足时会报告缺口，不会用未完成综合评分的条目补足。 |
| Find | 标题 LLM 预筛并发数 | 控制 Find 标题 LLM 批次并发。 | 默认 10；标题+摘要综合评分使用独立的每批 10 篇、默认 10 并发。 |
| Find | 高级预算 | arXiv/bioRxiv 抓取上限、arXiv query 数、出版渠道题录扫描保护上限和标题+摘要综合评分上限。 | 预印本抓取上限默认 5000，超过时按发表时间保留每个来源最近的论文；综合评分上限默认 1000，arXiv query 数默认 3；`venue_title_scan_limit=0` 表示出版渠道题录不设数量上限。 |
| Read | 运行精读 | 按项目配置，对当前 Find 最终排名前 N 篇执行精读、评分和边界审计。 | 需要先有当前 Find run；模块默认 N=50。 |
| Read | 精读状态 | 分别展示 Find 推荐数、本次选中精读数、全文精读完成数和待补项。 | 主要用于判断 Read 是否拿到足够证据。 |
| Ideas | 想法最大数量 | 控制生成研究想法数量上限。 | 默认 6。 |
| Ideas | 人工修改与 `idea.md` 产物 | 页面上方用字段卡片修改标题、新方法和初步实验，也可切换到 Markdown 源文；页面右下产物栏直接渲染最终 `idea.md`。 | 保存时原地更新同一个 Idea run；不会为每次编辑新建 run。 |
| Ideas | 想法状态 | 将 Markdown 中的候选想法标记为通过、待定或删除。 | 只有通过的想法进入 Plan 候选。 |
| Plan | 已批准 Ideas | 从当前 Find 的已批准 Ideas 中选择本次 Planning 输入。 | 至少选择一个；支持单选和多选，默认全选。 |
| Plan | 修复轮数 | Claude 生成初版后精确执行的修复次数。 | 默认 3；设为 0 时只保留初版。 |
| Plan | 计划操作 | 页面右上选择候选、继续优化、选为执行计划，并直接编辑最终 `plan.md`。 | 保存命令交给 Framework 校验和同步；右下产物栏直接渲染同一文件。 |
| Plan | 候选计划操作 | 继续润色候选，或由人选定唯一执行计划。 | 不会默认把第一个候选当执行计划。 |
| Plan | 让主控 Claude Code 选择唯一执行计划 | 将候选计划交给主控 Claude Code，形成执行合同。 | 进入实验前建议完成。 |
| Environment | conda 环境名称 | 具体科研项目使用的实验环境名。 | 在环境配置阶段填写；不是左侧栏全局配置。 |
| Environment | conda base | Conda/Mamba 安装根目录。 | 自动检测优先；检测失败时手动填写。 |
| Environment | 实验 Python | 训练和评估命令使用的 Python。 | 可由 conda base + 环境名派生，也可显式填写绝对路径。 |
| Environment | 真实创建/安装 Conda 环境 | 网页会保存该开关，但当前 Environment 运行入口不读取它。 | 保持默认；实际创建与检查以 Environment 主控和 handoff 状态为准。 |
| Environment | 自然语言请求 | 给 Environment 主控 Claude 的环境部署说明。 | 说明仓库、数据、复现目标和限制。 |
| Experiment | 科研迭代轮数 | 控制实验子循环轮数。 | 从小轮数开始，确认日志和证据正常后再扩大。 |
| Experiment | 运行实验迭代 | 向当前项目唯一的 Experimenting 主控 Claude 发送继续工作命令。 | 先完成 Environment handoff。 |
| Experiment | 主控对话 | 向同一个 Experimenting 会话发送人工指令。 | 忙碌时可排队，也可打断当前任务并优先处理。 |
| Paper | 投稿会议/期刊 | 论文模板、格式门控和页面限制目标。 | 只在论文撰写页配置；不要写在左侧栏主题里。 |
| Paper | 论文标题 | 论文草稿标题。 | 可先留空或写工作标题，后续由 Writing 主控 Claude 修订。 |
| Paper | 缺 LaTeX 依赖时尝试自动安装 | Full-cycle 会传递该选项；单独运行 Paper 时不读取。 | 生成 PDF 前自行准备 LaTeX 和 `latexmk`。 |
| Paper | 生成与修订论文 | 启动论文代理，生成/修复 TeX、PDF、引用、图表和证据门控。 | 预览 PDF 不等于投稿通过；投稿状态以门控为准。 |

## 使用说明

### 左侧栏

左侧栏负责“项目级上下文”和“运行工具”。研究主题、研究兴趣和研究者画像会直接参与 Find、Read 最终评分、Ideas、Plan 和后续模块主控 Claude 判断；Framework 按项目配置从 Find 最终排名截取前 N 篇交给 Read，其中研究画像越详细，TASTE 越能区分“真正匹配的论文”和“只是泛泛相关的论文”。网页中的 LLM 配置只供 Find 做关键词抽取、分类排序、标题筛选和标题+摘要评分；后续模块使用 Claude Code。运行环境用于让网页后端找到 `node`、`npm`、`claude` 和管理 Python。

TASTE 不管理 Claude Code 账号，也不会写入用户的 Claude Code API。只要当前用户终端能正常运行 `claude`，网页通常可以通过自动检测找到它；检测失败时再填写 `claude_path` 或 `extra_path`。

### Find：发现候选论文和代码线索

Find 的输入来自研究主题、研究兴趣、研究者画像、已选出版渠道/年份和勾选的扩展来源。会议/期刊来源先扫描所选渠道与年份的题录池；其他来源按各自 API 或 RSS 获取候选。各来源完成标题 LLM 评分后，系统全局去重并按标题分排序，最多选择配置数量抓取摘要/详情并执行标题+摘要 LLM 综合评分。拥有真实摘要且完成有效综合评分的候选直接按分数排序取前列；主题证据字段和绝对分数不构成额外硬门控。

Find 页面同时展示来源状态、调研验收计数和当前 run 产物。出版渠道默认不设题录数量上限，`venue_title_scan_limit=0` 表示全扫；测试或异常源保护时才需要设正数。有可信主题分类的渠道会先让 LLM 根据研究主题、研究兴趣和研究者画像排序分类，完整保留“相关/有用”前缀；该前缀不足 1000 篇时再沿排序补足。没有可信主题分类的渠道直接使用完整题录池。arXiv/bioRxiv 分类留空时不加分类约束；arXiv 只有勾选后才抓取，日期留空时默认最近 180 天。

### Read：精读最终排名前 N 篇

网页中的 Read 由 Framework 按项目配置把当前 Find 最终排名前 N 篇转换成通用论文输入，再调用 Reading 获取同篇全文并生成精读；模块默认 N=50，Find 完成后 Framework 默认更新为推荐数的两倍，用户可按项目修改。全部逐篇产物完成后，Reading 使用 Claude Code 给出匹配度和可借鉴性并按两项均分重排。Find 已提供的 URL、PDF、DOI、OpenReview ID、作者和来源会用于加快全文定位；缺少这些字段时仍可从标题开始查找。需要配置 OpenReview 登录或开放全文索引时，使用 `modules/reading/config/read.env`，具体变量见 [Reading 使用说明](modules/reading/README.md)。Reading 命令行也可独立接收标题或论文列表，不读取项目或 Find 状态。页面只展示当前 Find 对应的精读状态，避免把历史 run 的内容混入当前项目判断。

### Ideas：生成和筛选研究想法

Ideas 基于当前 Find/Read 产物生成可实验化的研究想法。页面上方保留人工修改卡片，可编辑标题、新方法和初步实验，也可切换到完整 Markdown 源文；页面右下产物栏使用 Markdown 解析库和 KaTeX 直接渲染最终 `idea.md`。通过、待定和删除状态仍在修改卡片内操作；只有通过的想法进入 Plan。

Ideas 正式生成使用 Claude Code。Framework 负责校验当前 Find/Read、构建输入包和调用模块；网页只传递命令与配置。想法数量只影响 Ideas，不影响 Read、Plan、Environment、Experiment 或 Paper。

当前 Find 的推荐结果可能很大。Framework 会先提取 Ideas 所需证据并生成与当前 Find 绑定的规范化输入包。`idea.md` 是用户可读的 Idea 正文，`ideas.json` 是由它派生的状态投影；项目 state 只记录数量、门控和选择状态。

### Plan：形成可执行实验计划

Framework 校验当前 Find 中明确批准的 Ideas，并只把 Web 本次勾选的非空子集交给 Planning；可以选择一个或多个，不传选择时默认使用全部已批准项。Planning 为每个选中 Idea 生成候选并执行评估/修复。正式模式默认使用 Claude Code，最终由 Claude 直接写 `plan.md`，回读检查栏目、公式、网页引用和重复内容，再通过发布校验。

当前 Plan 已存在时，后续 Idea 修改由 Framework 识别是否影响已规划 Idea；受影响时旧的当前 Plan 会失效，并按原已规划且仍批准的 Idea 自动重生成；如果没有仍批准的已规划 Idea，则保持待重新选择状态。历史 Planning run 只供查看，不参与当前项目判断。

Plan 页面保持三栏布局：左侧选择 Ideas，中间设置精确修复轮数，右侧“计划操作”选择候选、继续优化、选为执行计划，并直接修改 `plan.md`。页面下方右侧产物栏渲染 Framework 同步后的同一个 `plan.md`；页面主体不再复制一份正文。`plans.json` 只保留 ID、顺序、版本、选择状态和 Markdown 审计，不复制标题或正文。

进入 Environment、Experiment 或 Paper 前，必须有且只能有一个当前执行合同。完成或选择计划后，TASTE 会把 `selected_idea_id`、`selected_plan_id` 和 `selected_plan_only` 执行策略写入项目状态；未选中的想法和计划只作为备选，不会驱动下游阶段。

### Environment：选择基底、检查数据并锁定实验环境

Environment 根据 Find/Plan 的证据选择最适合跟进的仓库或基底，检查真实数据/loader 是否可用，并准备具体科研项目使用的 Conda/Python 环境。这个阶段配置的是 `experiment_python`，它只服务训练、评估和外部仓库脚本，不应混同于 TASTE 管理 Python。

环境配置是一次性创建逻辑：成功创建或确认后会锁定，网页不会反复安装、修改或重建环境。之后 Experiment 和 Paper 复用已经锁定的环境状态与证据。

每个项目只有一个 Environment 主控 Claude 会话。网页显示同一会话的历史指令与回复；忙碌时消息在 Environment 模块中持续排队，不设置等待超时，也可选择打断当前 turn 并优先处理。Web 已保存的 Conda 名称会原样使用；未填写时由 Environment 主控选择合法名称，Framework 将名称写入项目配置，并在 handoff 通过后固定 prefix 和实验 Python。

### Experiment：真实代码和实验迭代

Experiment 由 Experimenting 模块自己的主控 Claude 执行。它会围绕当前执行计划检查代码、修改实现、启动实验、读取日志/loss、分析坏例、记录指标和下一步行动。页面展示实验与复现门控、当前主线摘要、实验记录表和证据路径；旧历史记录不会被当成当前路线的新证据。

同一项目只有一个 Experimenting 主控会话，实验按钮和网页对话都使用它。主控忙碌时网页消息显示为排队；选择打断后，新消息优先执行，随后同一会话恢复原实验工作。

实验必须先完成最终验证，再解析指标并写 registry/CSV/Markdown 记录。完成态记录需要验证结束时间、验证返回码和记录时间，确定性 Gate 会阻止顺序错误的记录进入完成态。实验结果只有在真实数据、loader、复现和审计证据满足门控时才会进入可写论文的候选证据；失败、阻塞和负结果也会保留。

### Paper：生成论文预览并执行门控修复

Paper 根据投稿会议/期刊、当前计划、实验记录和审计证据生成或修订论文。Writing 模块自己的主控 Claude 会检查官方模板、页面限制、引用渲染、图表质量、自审发现和证据门控。页面可以展示 PDF/TeX 预览，但预览稿不代表投稿通过；是否可投稿以门控状态为准。

如果实验或证据门控未通过，Paper 可能保留或生成不可投稿的目标 venue 预览，方便查看结构和格式，但不会把缺失证据包装成正式结论。

### Full-cycle：顺序执行七个模块

启动前需先在 Paper 页面保存投稿会议/期刊。完整科研循环不拥有额外主控 Claude，也不包含阶段专用科研判断。它按 Find、Read、Idea、Plan、Environment、Experimenting、Writing 的顺序触发与网页按钮相同的 Framework action；Plan 生成后会自动调用 Claude 选择唯一执行计划，再进入 Environment。任一动作返回阻塞或失败时立即停止。

### 任务栏和产物

页面底部任务栏展示当前项目的 job/run 状态、阶段进度、最近日志、命令和产物路径。Find/Read/Ideas/Plan 的 Markdown 产物只在对应页面展开；Environment、Experiment、Paper 展示各自阶段的真实状态与证据，避免把文献调研日志误看成实验或论文结论。

任务栏默认显示面向用户的阶段摘要，不直接刷长 JSON、完整 agent transcript 或大段 stdout。例如 Plan 运行中会显示正在为哪个 Idea 生成计划，完成后显示阶段和运行编号；需要细看结果时再打开对应产物。

## 重要目录

根目录只保留源码和私有项目工作区。框架模板与 Claude 资源位于 `framework/resources/`；临时输入、锁、任务状态和缓存由对应组件保存在各自忽略的 `.runtime/`。

```text
.
├── CLAUDE.md                      # 维护或运行 TASTE 时供 Claude Code 读取的仓库级说明
├── README.md                      # 用户手册
├── LICENSE
├── framework/                     # TASTE 框架层、按功能分类的脚本和公共资源
│   ├── .runtime/                 # Framework 本机配置、临时输入、锁和兼容运行态，整体忽略
│   ├── scripts/                  # 根目录仅放 main.py；其余脚本按功能在平级目录中分类
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
└── third_party/（可选）           # 外部参考 checkout，整体忽略
```

项目产物进入 `projects/<project>/`；可丢弃的组件运行态只进入该组件的 `.runtime/`，仓库根目录不会生成统一的 `runtime/`。

命令行入口（可选；日常使用直接操作网页即可）：

| 路径 | 作用 |
| --- | --- |
| `python framework/scripts/main.py` | 启动 Web/API，默认 `127.0.0.1:8879`。 |
| `modules/finding/main.py --action find` | Find 模块公开入口；内部流水线和 runtime 均在模块私有 `scripts/` 中，不依赖框架脚本或其它模块。 |
| `python framework/scripts/main.py module reading --action current_find_research_plan --project <project>` | 当前 Find 对应的 Read 路由。 |
| `python framework/scripts/main.py module ideation --action idea --project <project>` | 校验 Read 后生成并同步当前 Find 的 `idea.md`。 |
| `python framework/scripts/main.py module planning --action plan --project <project> [--idea-id <id> ...]` | 校验同一 Find，并从一个或多个选中的已批准 Ideas 生成 Plan。 |
| `python framework/scripts/main.py module planning --action select --project <project>` | 让 Claude Code 从当前候选中选择唯一执行计划。 |
| `python framework/scripts/main.py module environment --action deploy_from_plan --project <project>` | 校验当前执行合同并调用 Environment。 |
| `python framework/scripts/main.py module experimenting --action work --project <project>` | Framework 调用项目唯一 Experimenting 主控的入口。 |
| `python framework/scripts/main.py module experimenting --action chat --project <project> --message <text>` | Framework 转发 Experimenting 网页对话、排队和中断选项。 |
| `python framework/scripts/main.py module writing --action work --project <project> --venue <venue>` | 将论文生成、修复和审计任务交给项目唯一的 Writing 主控 Claude。 |

运行态项目目录：

```text
projects/<project>/
├── project.json
├── AGENTS.md                      # 项目 Claude Code 可读取的项目级规则
├── activate_env.sh                # 通过项目实验环境执行命令
├── config/finding.json            # Find 来源、预算和运行参数
├── state/
├── planning/finding/              # find/read/idea/plan Markdown 与状态投影
├── reports/
├── logs/
├── artifacts/
├── experiments/
├── repos/                         # 候选仓库与选定仓库
└── paper/
```

用户最常查看的正文是 `planning/finding/find.md`、`read.md`、`idea.md`、`plan.md` 和 `paper/writing/`；执行合同与阶段状态位于 `state/`，实验记录位于 `experiments/`。这些目录通常包含私人路径、下载仓库、数据、日志、论文草稿和未公开结论，默认不提交。

## 运行边界

正常项目流程请使用网页或 `python framework/scripts/main.py module`；Framework 负责校验输入、调用七个阶段模块并同步 `projects/<project>/`，Web 只传递配置和命令、展示任务状态与产物。需要独立使用某个模块时，从对应 `modules/<stage>/main.py` 进入，并按该模块 `README.md` 提供显式输入；模块不会替代 Framework 发现或同步项目状态。

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
