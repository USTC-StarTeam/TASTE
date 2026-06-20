---
name: citation-integrity
description: 写作模块的真实引用、BibTeX、正文 citation 覆盖和查重规约。
---

# Citation Integrity Skill

论文中的每个 citation key 都必须能在 `refs.bib` 中找到，每个 BibTeX 条目都必须来自可核验来源。

## 验证规则

- 标题相似度不足、缺少作者/年份、缺少 DOI/arXiv/OpenAlex/Semantic Scholar ID 且无法人工核验的条目不能进入最终 bibliography。
- 正文引用必须覆盖核心方法、数据集、基线、指标、领域近邻和目标 venue 读者预期的经典工作。
- 如果官方没有最低引用数，按 `venue_requirements.json` 里的 `citation_policy.min_verified_references` 作为本模块质量目标。
- 不允许为了凑数量加入无关引用。
- 不允许正文引用不存在的 BibTeX key，也不允许保留完全未被正文使用的参考文献，除非 venue 明确允许且有合理说明。
