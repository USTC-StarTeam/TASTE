---
name: venue-intelligence
description: 当前官方投稿要求、page policy、review policy、AI disclosure、citation policy 和官方模板解析规约。Use before writing or auditing any venue-formatted paper and whenever venue_requirements.json/template_source must be created or repaired.
---

# Venue Intelligence Skill

目标是为指定会议/期刊生成自包含的 `venue_requirements.json`，并下载最新官方模板。该 skill 只接受官方来源或官方明确链接的模板来源。

所有产物只写入当前项目 canonical `paper/writing/venue/` 目录。

## 官方来源优先级

1. 会议/期刊官网 author instructions、call for papers、submission guidelines。
2. 出版方官网模板页，例如 ACM、IEEE、Springer Nature、ACL、NeurIPS、ICML、OpenReview 官方链接。
3. 官方 GitHub/Overleaf/zip/arxiv style 链接，必须能从官方页面回链确认。
4. 第三方博客、旧年仓库、非官方镜像只作为线索；最终依据必须来自 1-3 类来源。

## 必须解析字段

- `official_sources`：URL、标签、短证据说明、访问时间。
- `page_policy`：body/page/reference/appendix/supplement 的规则及来源。
- `citation_policy`：官方要求与本模块质量目标分开记录。
- `review_policy`：匿名、自引、伦理、AI disclosure、reproducibility checklist。
- `template`：family、官方 URL、archive/repository、main tex、required files、required markers、bibliography style。
- `paper_shape`：目标 venue 推荐 section 形状。
- `layout_guidance`：超过页数时先检查 float/table/bibliography/template，再改正文。

## 阻塞条件

- 找不到当前官方要求。
- 模板不是官方来源，或无法验证必需文件/标记。
- 页数规则互相矛盾且无法从官方上下文消歧。
- Claude Code 没有网络或工具权限完成调研。
