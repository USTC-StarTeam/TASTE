---
name: nature-family-writing
description: Nature / Springer Nature 期刊文章形状、语气、section、availability statement 和 reference policy 规约。Use when venue_requirements.json identifies Nature-family, Springer Nature, Nature Machine Intelligence, or similar journal submission mode.
---

# Nature-Family Writing Skill

当 `venue_requirements.json` 解析出 Springer Nature 或 Nature-family 期刊时，TASTE Writing 必须切换到期刊文章模式。

## 写作形状

- 常见结构是 Abstract、Introduction、Results、Discussion、Methods、Data availability、Code availability、References。
- 不要默认使用会议论文的 Related Work / Experiments / Conclusion 顶层结构，除非官方模板或 journal guide 明确要求。
- 初投稿如果官方允许 flexible format，应诚实记录；本模块生成的 LaTeX/PDF 是 workflow preview，不得声称期刊强制 LaTeX。
- 参考文献数量遵守官方上限；没有官方下限时采用本模块质量目标。
- 语气要克制、证据密集、面向广泛科学读者，避免会议式 leaderboard 口吻。
