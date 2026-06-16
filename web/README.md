# Web / 网页层

`web/` 是 TASTE 的人机交互层。它只负责展示信息、修改配置、发送任务指令、查看 job 和渲染各模块产物；真正的科研后端能力属于 `modules/` 七个模块和 `framework/` 编排层。网页不能实现 Find/Read/Ideas/Plan/Environment/Experiment/Writing 的核心算法，也不能为了显示方便直接修改科研产物语义。

## 输入

- 浏览器中的用户操作：项目选择、配置编辑、模块按钮、产物展开、人工编辑 idea/plan。
- `runtime/.config.json`：本机网页配置和密钥运行态。
- `projects/<project>/project.json` 与各阶段产物：网页只读取和 patch 明确字段。
- FastAPI 后端返回的 job、artifact、status 和 Markdown/JSON 片段。

## 输出

- API 请求：创建项目、保存配置、启动/停止模块任务、选择 run、编辑 idea/plan 状态。
- 用户可见页面：七个模块页、任务栏、运行历史、状态面板、产物预览。
- 不直接生成科研结论；结论必须来自后端模块产物。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `backend/auto_research/web/server.py` | FastAPI 服务入口，负责配置、项目、job、artifact 和静态前端服务。 |
| `backend/auto_research/web/project_bridge.py` | 把项目文件系统状态整理成网页可读模型；必须过滤内部字段、陈旧状态和用户不可读内容。 |
| `backend/auto_research/web/script_manifest.json` | 后端网页脚本清单。 |
| `frontend/client/src/App.tsx` | React 单页应用主界面，包含七阶段页面和任务栏。 |
| `frontend/client/src/api.ts` | 前端 API 客户端。 |
| `frontend/client/src/styles.css` | 页面样式。样式只改善展示，不承载流程逻辑。 |
| `frontend/client/package.json` | 前端依赖和 build/dev 脚本。 |
| `frontend/client/dist/` | 构建产物，运行时可存在但不提交。 |
| `frontend/client/node_modules/` | 本机 npm 依赖，不提交，不参与源码扫描。 |

## 前后端分工

1. 前端只维护 UI 状态、表单状态和用户操作，不在浏览器里计算科研推荐或实验结论。
2. 后端 web bridge 只做 API、任务派发、状态整形和安全过滤；核心逻辑必须调用 `framework/scripts` 或 `modules/*/scripts`。
3. 用户可见 Markdown/HTML 要经过统一渲染和清洗，避免把内部 marker、paper id、JSON 字段、Claude scratchpad 或 reader 指令直接展示出来。
4. 产物默认展示上一个已完成结果；新任务未完成前不能用半成品替换当前用户可见产物。
5. 测试网页时必须实际打开 `http://127.0.0.1:8765`，在相应 tab 视觉检查所有关键文本、按钮、计数和产物，不只看命令行输出。

## 运行与测试

```bash
framework/scripts/start_web.sh
npm --prefix web/frontend/client run build
```

远端服务器使用 SSH tunnel 后在本地浏览器访问 `http://127.0.0.1:8765`。维护者测试 TASTE 功能时，除 debug/单元测试外，应尽量通过网页按钮触发模块任务，模拟真实用户路径。

## 冗余控制原则

- 不要把业务规则写进 `App.tsx`；前端只展示后端给出的结构化状态。
- 不要在 `server.py`/`project_bridge.py` 中堆某个论文、某个研究主题、某个项目路径的特例。
- 新 API 必须有清晰的后端归属：配置/项目/job 属于 web，跨模块编排属于 framework，阶段功能属于 modules。
- 修改展示后必须浏览器视觉检查，而不是只跑 pytest 或 curl。
