# TASTE Research System Agent Guide

This workspace must be operated from the workspace root represented as `<workspace_root>`.

## Default Operating Mode

Use the project-based workflow under `projects/<project>/`. Every stage has one public module entrypoint, and Framework is the only component allowed to invoke project-stage modules:

- Find uses the configured LLM for title/abstract/detail scoring and recommendation ranking.
- Read runs through `framework/scripts/run_module.py reading --action current_find_research_plan`, which prepares and validates the current-Find reading input before invoking `modules/reading/main.py`.
- Idea runs through `framework/scripts/run_module.py ideation --action idea`, which validates current Find/Read artifacts, prepares a normalized input bundle, invokes `modules/ideation/main.py`, and synchronizes the explicit timestamped run into the project.
- Plan runs through the Planning public entrypoint and may consume only explicitly approved ideas from the same current Find run.
- Web never invokes module entrypoints or synchronizes module artifacts directly. It displays project artifacts, transports configuration/commands to Framework, and reports Framework job state.
- If the `claude` CLI is unavailable, downstream structured fallbacks must not execute code, choose an environment, run experiments, write papers, or promote claims.
- Environment, Experiment, and Paper each own a separate module-controller Claude Code session plus deterministic gates. Framework supplies the project and invokes the module; Web talks to that module controller through Framework.

## Takeover Boundaries

There are two different handoff scopes; do not mix them.

Framework-maintainer agents that are repairing or refactoring TASTE itself should read root `AGENTS.md`, root `README.md`, and root `工作状态.txt` on the active machine when present. Human-readable maintainer handoff belongs in root `工作状态.txt`; disposable diagnostics belong only in the owning component's ignored `.runtime/` and are not takeover state.

Module-controller Claude Code sessions must keep their scientific state inside `projects/<project>/`. Their project handoff, if any, belongs under that project directory and must not depend on root `工作状态.txt` or component-local maintainer diagnostics.
TASTE-launched module controllers must run with `projects/<project>/` as their working directory. Each module owns exactly one controller session per project and must not reuse another module's session.

For a project-stage takeover before major work:

1. Read `<workspace_root>/AGENTS.md` for stable framework boundaries.
2. Read the target project's `AGENTS.md` and visible project status artifacts if they exist.
3. Run:
   - `$MANAGEMENT_PYTHON framework/scripts/research_healthcheck.py --project <project> [--venue "<venue>"]`
   - `$MANAGEMENT_PYTHON framework/scripts/report_status.py --project <project> [--venue "<venue>"]`
   - `$MANAGEMENT_PYTHON framework/scripts/detect_machine_profile.py --project <project>`
   - `$MANAGEMENT_PYTHON framework/scripts/audit_pipeline_runnability.py --project <project> [--venue "<venue>"]`
4. Read project-generated `healthcheck.md`, `status.md`, `machine_profile.md`, `planning/next_actions.md`, and `reports/iteration_reflection.md` when present.
5. Use project-local `state/current_find_research_plan.json`, `state/blocker_action_plan.json`, `state/experiment_registry.json`, `paper/metadata/paper_pipeline.json`, and raw logs/artifacts as the source of truth.

## Script Trust Policy

Scripts are decision-support tools, not unquestionable authorities.

- Treat generated reports as summaries of state, not perfect truth.
- If a script output conflicts with logs, artifacts, code, or direct file inspection, inspect the underlying files and repair the state.
- If a script fails, continue from primary artifacts instead of treating the whole project as scientifically complete.
- Never deepen a method only because a summary sounds optimistic; verify claim evidence, bad-case evidence, and counterexample pressure.

## Runtime Rule

- Do not assume a fixed GPU model, GPU count, CUDA version, Conda base path, package manager, or local absolute workspace path.
- Do not write API keys, user account settings, downloaded repos, datasets, logs, generated papers, or private project artifacts into tracked files.
- Module-controller work may use root `CLAUDE.md` plus `framework/resources/claude/agents`, `framework/resources/claude/commands`, and `framework/resources/claude/skills` as local framework templates only. User-specific `.claude/settings.json` is never tracked.
- Find LLM configuration belongs in local config or environment variables. Downstream Claude Code account/API configuration belongs to the user's own Claude Code setup and must not be overwritten by TASTE.

## TASTE Integration

- The web UI runs through `framework/scripts/start_web.sh` and `web/backend/auto_research/web/server.py`.
- Find outputs are synchronized into `projects/<project>/planning/finding/` and summarized in `planning/finding_frontend.md`.
- Current-Find Read, Idea, and Plan must each stay tied to the latest selected Find run. Framework owns these run-id and readiness checks; modules consume only the explicit inputs Framework passes them.
- Environment, Experiment, and Paper may consume only the single selected plan/idea contract. Non-selected ideas and plans are backlog only.
- `idea.md` is the user-facing Ideation source of truth. `ideas.json` is a derived machine projection for status and Planning interoperability; Web must render `idea.md` directly.

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
