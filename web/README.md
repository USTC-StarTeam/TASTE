# Web / 网页层

`web/` 是 TASTE 的人机交互层。它只负责展示信息、修改配置、发送任务指令、查看 job 和渲染各模块产物；真正的科研后端能力属于 `modules/` 七个模块和 `framework/` 编排层。网页不能实现 Find/Read/Ideas/Plan/Environment/Experiment/Writing 的核心算法，也不能为了显示方便直接修改科研产物语义。

## 输入

- 浏览器中的用户操作：项目选择、配置编辑、模块按钮、产物展开、人工编辑 idea/plan。
- `framework/.runtime/.config.json`：Framework 持有的本机网页配置、非敏感偏好和运行参数。
- `modules/finding/config/llm.local.json`：本机私有 Find LLM 配置，保存 provider/base URL/model/API key/temperature；也可用 `FINDING_LLM_CONFIG` 指向其它本机路径。
- `projects/<project>/project.json` 与各阶段产物：网页只读取展示；保存配置或编辑产物时把指令交给 Framework。
- `projects/<project>/project.json` 与 `state/environment_handoff.json`：Web 已保存的 `conda_env` 名称直接使用；未填写时显示 Environment 主控选择并由 Framework 固定到项目配置的名称。handoff 通过后提供 run-local prefix、实验 Python、repo、数据入口和待实验指标。
- FastAPI 后端返回的 job、artifact、status 和 Markdown/JSON 片段。

## 输出

- API 请求：创建项目、保存配置、启动/停止模块任务、选择 run、编辑 idea 内容与状态。
- 用户可见页面：七个模块页、任务栏、运行历史、状态面板、产物预览。
- 不直接生成科研结论；结论必须来自后端模块产物。
- `web/.runtime/state/web_jobs.json`：本机任务栏状态；不属于科研项目证据。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `backend/auto_research/web/server.py` | FastAPI 服务入口，负责配置、项目、job、artifact 和静态前端服务。 |
| `backend/auto_research/web/auth.py` | 账户、邮箱验证码、密码哈希和会话存储。 |
| `backend/auto_research/web/verification_email.py` | 注册验证码的系统 SMTP 发送组件。 |
| `framework/scripts/bridges/project_bridge.py` | Framework 负责项目命令、运行编排和网页所需的项目状态投影；Web 只调用其公开函数。 |
| `backend/auto_research/web/script_manifest.json` | 后端网页脚本清单。 |
| `frontend/client/src/App.tsx` | React 单页应用主界面，包含七阶段页面和任务栏。 |
| `frontend/client/src/api.ts` | 前端 API 客户端。 |
| `frontend/client/src/styles.css` | 页面样式。样式只改善展示，不承载流程逻辑。 |
| `frontend/client/package.json` | 前端依赖和 build/dev 脚本。 |
| `frontend/client/dist/` | 构建产物，运行时可存在但不提交。 |
| `frontend/client/node_modules/` | 本机 npm 依赖，不提交，不参与源码扫描。 |

## 前后端分工

1. 前端只维护 UI 状态、表单状态和用户操作，不在浏览器里计算科研推荐或实验结论。
2. Web 后端只做 API、任务传输、job 连接、静态文件和展示安全过滤；项目命令、状态投影和模块调用统一由 Framework 负责，Web 不直接调用模块。
3. Environment 页面直接向该项目唯一的 Environment 主控 Claude 会话发送指令并显示会话历史。主控忙碌时消息持续排队且没有等待超时；页面提供优先打断和取消入口，Web 不维护会话或执行 Claude。
4. Environment handoff 可以显示 `ready_for_experimenting`，但不能把它解释为论文级 full reproduction 或指标已通过。`conda_env` 显示名称，`conda_env_prefix` 显示路径。
5. 用户可见 Markdown/HTML 要经过统一渲染和清洗，避免把内部 marker、paper id、JSON 字段、Claude scratchpad 或 reader 指令直接展示出来。
6. 产物默认展示上一个已完成结果；新任务未完成前不能用半成品替换当前用户可见产物。
7. Find 来源状态只能展示真实 Find run 的 `source_status` 或项目摘要；`检查可抓取性` 的 venue health sample 只能显示在出版渠道列表中，不能当作“题录总数/渠道候选”回退来源。
8. 当前 Find 产物按页面作用域传输：Ideas 请求显式携带项目和当前 Find run，只读取 `idea.md` 和 `ideas.json`，不会为了渲染想法同时读取超大的 Find/Read 产物。Ideas 页面上方保留人工修改卡片，最终 `idea.md` 只在右下产物栏渲染。
9. Plan 页面显示 Framework 确认的已批准 Ideas，并允许勾选一个或多个作为本次输入；没有选中项时禁用生成。页面保持三栏布局，右上“计划操作”直接修改 `plan.md`，右下产物栏渲染同一个项目文件；页面主体不得再复制一份正文。
10. Plan 生成、润色、Claude 选择、人类选择和 Markdown 保存都调用 `python framework/scripts/main.py module planning`。Web 不读取模块 `.runtime`、不复制 run，也不自行判断 Read/Ideas 是否就绪。
11. Plan 修复轮数是精确次数，默认 3，允许 0。切换到历史 Find run 后，Read/Idea/Plan 的生成、编辑、批准、保存和选择控件必须只读，不能借历史页面状态修改当前项目。
12. Find 页面必须区分“来源覆盖总量”和“本 run 漏斗计数”：`source_status_totals` 用于说明渠道覆盖，`counts/survey_stats` 用于显示本次实际处理的标题入口、标题筛选、LLM 打分和推荐结果，二者不能互相覆盖。
13. 测试网页时必须实际打开 `http://127.0.0.1:8879`，在相应 tab 视觉检查所有关键文本、按钮、计数和产物，不只看命令行输出。

## 运行与测试

```bash
python framework/scripts/main.py
npm --prefix web/frontend/client run build
```

启动脚本默认监听 `0.0.0.0:8879`，可直接通过 `http://服务器地址:8879` 检查，也可由 Nginx/Caddy 反向代理到 HTTPS 域名；仍可通过 `WEB_HOST=127.0.0.1` 限制为仅本机访问。网页提供注册、登录和退出功能，会话保存在 HttpOnly Cookie 中。

新账户使用邮箱验证码注册，验证码只以加盐哈希保存；登录框同时接受用户名和邮箱。发送注册邮件前，在服务环境中配置系统 SMTP（这些是服务器级注册配置，不会读取或覆盖任何用户自己的项目邮件配置）：

```bash
TASTE_AUTH_SMTP_HOST=smtp.example.com
TASTE_AUTH_SMTP_PORT=465
TASTE_AUTH_SMTP_SECURITY=ssl
TASTE_AUTH_SMTP_USERNAME=taste@example.com
TASTE_AUTH_SMTP_PASSWORD=your-smtp-authorization-code
TASTE_AUTH_SMTP_FROM=taste@example.com
```

`TASTE_AUTH_SMTP_SECURITY` 可取 `ssl`、`starttls` 或 `plain`；修改后需重启 Web 服务。旧账户无需补填邮箱，仍可继续使用用户名登录。

推荐由反向代理终止 HTTPS，再转发到 `http://127.0.0.1:8879`。代理必须传递协议和 WebSocket 头，TASTE 才会设置 Secure Cookie，并让任务连接自动使用 WSS：

```nginx
location / {
    proxy_pass http://127.0.0.1:8879;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

默认只信任本机代理头；若代理来自容器或另一台可信主机，用 `WEB_FORWARDED_ALLOW_IPS` 设置其来源 IP。若需要让 TASTE 自己直接在 8879 提供 TLS，可同时指定证书和私钥（此模式下 8879 将只接受 HTTPS）：

```bash
WEB_SSL_CERTFILE=/path/fullchain.pem WEB_SSL_KEYFILE=/path/privkey.pem python framework/scripts/main.py web
```

账户数据采用 Web 到 Framework 的账户项目映射隔离：项目产物与任务访问按账户校验，账户的 LLM/邮件配置保存在 `web/.runtime/accounts/<account-id>/`。Find/Read 的公共下载、题录和全文缓存继续由所有账户共享，科研模块本身不为账户复制；共享缓存不包含其他账户项目产物的访问权限。

维护者测试 TASTE 功能时，除 debug/单元测试外，应尽量通过网页按钮触发模块任务，模拟真实用户路径。

## 冗余控制原则

- 不要把业务规则写进 `App.tsx`；前端只展示后端给出的结构化状态。
- 不要在 `server.py`/`project_bridge.py` 中堆某个论文、某个研究主题、某个项目路径的特例。
- 新 API 必须有清晰的后端归属：配置/项目/job 属于 web，跨模块编排属于 framework，阶段功能属于 modules。
- 修改展示后必须浏览器视觉检查，而不是只跑 pytest 或 curl。
