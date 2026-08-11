# Find 批量修补与全局进度设计

## 目标

修复 Find 评分合同与网页进度口径，同时保留 LLM 修补能力：

- 标题主评分和标题修补均以最多 100 篇为一个 LLM 请求；禁止逐篇修补。
- 最终标题+摘要主评分和修补均以最多 10 篇为一个 LLM 请求；禁止逐篇修补。
- 各渠道先完成候选收集和详情补全，最终标题+摘要评分在全局候选池中统一分批，避免渠道尾批把 1000 篇拆成多于 100 个主评分请求。
- 网页上的评分进度、已评分数量和批次数必须是全局且单调递增的，不能在切换渠道或进入修补时回退。
- LLM 返回字段名发生可识别的语义别名变化时，本地确定性归一化，不额外消耗 LLM 请求。

## 已确认的故障链

当前 `_select_title_abstract_scoring_groups` 虽然全局选择候选，却保留来源分组；`run_find` 随后逐来源调用 `_evaluate_items`。`_evaluate_items` 发出的 `abstract_scoring` 进度和 `count_updates` 都是来源局部值。`_persist_find_progress` 直接用这些局部值覆盖全局统计，而网页优先显示活动任务的实时进度，所以每个新来源都会从 `0/N` 重新开始，并可能把已评分数从前一来源的累计值改小。

最近一次 Find 的 98 个成功最终评分响应使用了 `recommendation_reason_chinese`、`recommendation_reason_english` 等字段，而解析器只接受 `reason_zh`、`reason_en`。因此返回行虽有合法 ID 和分数，仍全部被归为 `omitted-item`。提示词没有明确列出稳定属性名，测试又只构造理想旧字段，导致这一问题未被覆盖。

## 数据流

1. 各来源仍独立完成抓取、标题筛选和来源专用详情补全。
2. 全局选择完成后，保留来源/输出槽位信息，把所有入选项合并为一个最终评分列表。
3. `_evaluate_items` 只调用一次，主评分严格按 10 篇分批。
4. 每个主响应先做确定性字段归一化，再分别校验：
   - ID 与数值分数合法时，该篇计入“摘要 LLM 已评分”。
   - 推荐理由不合格时仍保留真实 LLM 分数，但进入批量修补队列，不能进入推荐列表。
   - ID 缺失、重复或分数非法时进入批量修补队列，且不计为已评分。
5. 每一轮把待修补项重新按 10 篇分组，每个修补批次只有一次严格网络请求。达到既有修补轮数后仍失败的条目保留可审计失败原因。
6. 评分结束后按原来源槽位恢复 HuggingFace/GitHub 的展示上限，不改变其他推荐门禁。

标题阶段沿用相同原则：每个主批次最多 100 篇；未解决项以一个批量修补请求继续评价，既有修补轮数保留，但删除逐篇请求路径。

## 响应合同

最终评分提示词必须显式要求以下规范字段，而不是只用自然语言描述：

`id`、`category`、`fit_score`、`diversity_score`、`recommend_for_deep_reading`、`topic_evidence`、`topic_evidence_supported`、`matched_topic_route`、`topic_evidence_basis`、`missing_topic_evidence`、`hit_directions_zh`、`hit_directions_en`、`fit_explanation_zh`、`fit_explanation_en`、`reason_zh`、`reason_en`。

允许对已经在线上观察到的等价字段做确定性归一化，包括：

- `recommendation_reason_zh` / `recommendation_reason_chinese` → `reason_zh`
- `recommendation_reason_en` / `recommendation_reason_english` → `reason_en`
- `fit_explanation_chinese` / `fit_explanation_english` → 对应规范字段
- `hit_direction_chinese` / `hit_direction_english` 及其复数形式 → 对应规范字段

规范字段优先；不从无关文本猜测或编造内容。

## 进度合同

- 最终主评分的分母来自全局评分批次数，不再来自单一来源。
- 修补请求预留有界请求槽位，主评分和修补共用一个单调递增的全局进度分母；没有使用的修补槽位在确认无需继续修补时一次性完成。
- `abstract_scored_papers` 与 `llm_scored_candidates` 只统计已经获得合法 LLM 数值评分的唯一论文，并且只能递增。
- `evaluated_candidates` 表示全局进入最终评价流程的唯一候选数。
- 服务端投影把“全部来源”评分识别为全局评分阶段，不再按来源数量二次缩放。

## 错误与质量处理

- 每个主批次或修补批次仍严格执行一次 HTTP 请求；传输层、响应格式降级和 JSON 解析不会在同一次批次调用内暗中重发。
- 请求失败、缺失 ID、重复 ID、非法分数、占位文本和理由质量失败分别记录原因，不能再全部折叠成不可诊断的 `omitted-item`。
- 合法分数与推荐文案质量分离。理由最终仍不合格时，论文保留 LLM 评分审计证据，但通过 `reason_quality_invalid` 被推荐门禁排除。
- 内部标记 `Find` 只按大小写明确的阶段名识别；英文自然语句中的 `find` 或 `findings` 不应被误判为内部文本。

## 验收标准

- 102 篇标题的主请求大小为 `[100, 2]`；缺失条目按批修补，任何请求都不是单篇（除非全局实际只剩一篇）。
- 21 篇标题+摘要的主请求大小为 `[10, 10, 1]`；跨来源的 6+4 篇合并成一个 10 篇请求。
- 字段别名响应能得到合法评分；一语句理由会触发批量修补，修补失败时仍计入 LLM 已评分但不进入推荐。
- 多来源最终评分的实时 `current/total` 与已评分计数不回退。
- 服务端进度投影对全局主评分和修补保持阶段百分比单调。
- 框架汇总中的零个 LLM 评分保持为零，不再被 `len(evaluated)` 替代。

