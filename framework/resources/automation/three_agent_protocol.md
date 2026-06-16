# Three-Agent Research Protocol

This system can be operated by one coordinating runner plus three optional helper agents, but it must also remain runnable by a single standalone process without any subagent capability.

## Roles

1. Literature Mapper
2. Repo and Dataset Scout
3. Skeptic and Executor

## Coordination rules

- The coordinator owns overall loop control, but not a specific executor product.
- The three helper agents should operate on disjoint output surfaces as much as possible.
- `raw/` is append-only and should not be edited by any helper agent.
- The coordinator merges outcomes into `reports/shared_research.md`, `planning/init_brief.md`, `experiments/experiment_log.md`, and loop state.
- When external discovery fails, the workflow should continue from existing project state.
- Every loop should force five quality gates before spending more budget: novelty, claim strength, counterexample pressure, bad-case coverage, and prune readiness.
- Helper agents and scripts are decision-support tools, not unquestionable authorities. If summaries conflict with artifacts or logs, flag that explicitly.
- If no helper agents exist, the standalone runner must still execute the same state transitions and produce the same artifacts.

## Recommended task split

- Literature Mapper: wiki, concept synthesis, gap extraction, novelty delta mapping, contradiction tracking.
- Repo and Dataset Scout: candidate repo ranking, dataset registry, initialization notes, reproducibility prerequisites.
- Skeptic and Executor: experiment design, claim tests, counterexample tests, bad-case slicing, failure analysis, prune recommendations.

## Required handoff signals

- Literature Mapper should hand off: the nearest-neighbor papers, the exact novelty delta, unresolved contradictions, and what evidence would falsify each promising hypothesis.
- Repo and Dataset Scout should hand off: the best runnable repo, the cleanest benchmark path, environment blockers, and what instrumentation is needed for metrics plus bad cases.
- Skeptic and Executor should hand off: per-method claim verdicts, counterexample risks, bad-case slices, resource-aware continue or prune decisions, and what to test next.
