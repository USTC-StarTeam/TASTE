# TASTE Research System Agent Guide

This workspace must be operated from the workspace root represented as `<workspace_root>`.

## Default Operating Mode

Use the project-based workflow under `projects/<project>/`. TASTE has one public runtime route:

- Find uses the configured LLM for title/abstract/detail scoring and recommendation ranking.
- Read, Idea, and Plan are handled by the project Claude Code session through `scripts/ensure_current_find_research_plan.py`.
- If the `claude` CLI is unavailable, Read/Idea/Plan may use the configured LLM only as a structured fallback; that fallback must not execute code, choose an environment, run experiments, write papers, or promote claims.
- Environment, Experiment, and Paper are Claude Code plus deterministic gate stages. They use the single project-agent route and deterministic gates, with no separate text-only engineering, repair, reviewer, or writing route.

## Mandatory Takeover Protocol

Every fresh project-agent session should do these before major work:

1. Read `<workspace_root>/AGENTS.md`.
2. Read `<workspace_root>/START_HERE.md`.
3. Read the target project's `AGENTS.md` and visible status artifacts if they exist.
4. Run:
   - `python3 scripts/research_healthcheck.py --project <project> [--venue "<venue>"]`
   - `python3 scripts/report_status.py --project <project> [--venue "<venue>"]`
   - `python3 scripts/detect_machine_profile.py --project <project>`
   - `python3 scripts/audit_pipeline_runnability.py --project <project> [--venue "<venue>"]`
5. Read the generated `healthcheck.md`, `status.md`, `machine_profile.md`, `planning/next_actions.md`, and `reports/iteration_reflection.md` when present.
6. Use `state/current_find_research_plan.json`, `state/blocker_action_plan.json`, `state/experiment_registry.json`, `paper/metadata/paper_pipeline.json`, and raw logs/artifacts as the source of truth.

## Script Trust Policy

Scripts are decision-support tools, not unquestionable authorities.

- Treat generated reports as summaries of state, not perfect truth.
- If a script output conflicts with logs, artifacts, code, or direct file inspection, inspect the underlying files and repair the state.
- If a script fails, continue from primary artifacts instead of treating the whole project as scientifically complete.
- Never deepen a method only because a summary sounds optimistic; verify claim evidence, bad-case evidence, and counterexample pressure.

## Runtime Rule

- Do not assume a fixed GPU model, GPU count, CUDA version, Conda base path, package manager, or local absolute workspace path.
- Do not write API keys, user account settings, downloaded repos, datasets, logs, generated papers, or private project artifacts into tracked files.
- Project-agent work should use `CLAUDE.md`, `.claude/agents`, `.claude/commands`, and `.claude/skills` as local templates only. User-specific `.claude/settings.json` is never tracked.
- Find LLM configuration belongs in local config or environment variables. Downstream Claude Code account/API configuration belongs to the user's own Claude Code setup and must not be overwritten by TASTE.

## TASTE Integration

- The web UI runs through `scripts/start_web.sh` and `modules/taste/auto_research/web/server.py`.
- Find outputs are synchronized into `projects/<project>/planning/finding/` and summarized in `planning/finding_frontend.md`.
- Current-Find Read/Idea/Plan must stay tied to the latest selected Find run. Do not let Environment, Experiment, or Paper consume stale or non-selected ideas.
- Environment, Experiment, and Paper may consume only the single selected plan/idea contract. Non-selected ideas and plans are backlog only.

## LLM Configuration

Configure the Find LLM through environment variables or local runtime config:

```bash
export LLM_PROVIDER=openai_compatible
export LLM_API_BASE="https://example-compatible-endpoint/v1"
export LLM_MODEL="model-name"
# Set OPENAI_API_KEY in your local shell or runtime config; do not commit it.
export LLM_TIMEOUT_SEC=60
export LLM_MAX_TOKENS=1200
```

LLM keys must never be committed. The public repository should contain only examples and templates.
