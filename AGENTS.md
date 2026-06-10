# TASTE Research System Agent Guide

This workspace must be operated only from the workspace root represented as `<workspace_root>`.

## Default operating mode

Use the project-based workflow under `projects/<project>/`.
The target architecture is generic-LLM-first and backend-neutral: any role that behaves like an agent should be implementable through generic LLM calls plus scripts, without hard dependence on a product-specific built-in coding agent.

## Mandatory takeover protocol

Every fresh session should do these before major work:

1. Read `<workspace_root>/AGENTS.md`.
2. Read `<workspace_root>/START_HERE.md`.
3. Read the target project's `AGENTS.md` and the workspace `工作状态.txt`.
4. Run:
   - `python3 scripts/research_healthcheck.py --project <project> [--venue "<venue>"]`
   - `python3 scripts/report_status.py --project <project> [--venue "<venue>"]`
   - `python3 scripts/detect_machine_profile.py --project <project>`
   - `python3 scripts/run_frontend.py --project <project> --fast-mode --timeout-sec ${TIMEOUT_SEC:-300}`
   - `python3 scripts/run_llm_research_team.py --project <project> [--prompt "<user goal>"] [--venue "<venue>"]`
   - `python3 scripts/audit_pipeline_runnability.py --project <project> [--venue "<venue>"] --run-fallback-team`
5. Read the generated `healthcheck.md`, `status.md`, `machine_profile.md`, `planning/next_actions.md`, and `reports/iteration_reflection.md`.
6. Read `planning/llm_research_team.md` and use it as the cross-role decision board for planning, research, code, debug, analysis, writing, and criticism.

## Script trust policy

Scripts are decision-support tools, not unquestionable authorities.

- Treat generated reports as summaries of state, not perfect truth.
- If a script output conflicts with logs, artifacts, code, or direct file inspection, inspect the underlying files and repair the state.
- If a script fails, continue the research loop manually from primary artifacts instead of stopping the whole project.
- Primary sources of truth are usually:
  - `project.json`
  - `state/experiment_registry.json`
  - `state/parallel_plan.json`
  - `reports/*.md`
  - workspace `工作状态.txt`
  - `paper/metadata/paper_pipeline.json`
  - experiment artifact folders and raw logs
- Never deepen a method only because a script summary sounds optimistic; verify claim evidence, bad-case evidence, and counterexample pressure.

## Runtime rule

- The default architecture is generic-LLM-first and backend-neutral by design.
- If no LLM backend is configured, the system should degrade gracefully instead of blocking the whole pipeline.
- Never assume a fixed GPU model, GPU count, CUDA version, conda base path, or package manager.

## LLM-only agent architecture

- Agent-like work must be possible through generic LLM API calls plus scripts; Codex is optional support, not the core dependency.
- The staged team is implemented by `scripts/run_llm_research_team.py` and follows an source-style division of labor: planner, researcher, coder, debugger, analyst, writer, critic.
- The team state must feed the next loop through `state/llm_research_team_state.json` and `planning/llm_research_team.md`.
- Failed experiments should flow through debugger and analyst reasoning before the planner allocates more budget.
- Writer and critic must not promote a paper unless novelty, claim strength, counterexamples, bad-case slicing, and prune/deepen gates are addressed.


## TASTE integration

- The workflow is integrated as an module at `modules/taste` and is called through `scripts/run_frontend.py`. Use `scripts/start_web.sh` to run its local web UI.
- Outputs are synchronized into `projects/<project>/planning/finding/` and summarized in `planning/finding_frontend.md`.
- The wrapper feeds workflow feedback back through the researcher profile: `planning/next_actions.md`, `reports/evolution_memory.md`, `reports/iteration_reflection.md`, and previous workflow summaries.
- The workflow is mandatory during research initialization, after a new user topic/venue, when `idea_candidates` is empty, when strict literature ingestion finds no qualified papers, when the repo/data route stagnates, and before selecting or revising a scientific plan.
- For initialization, prefer `--fast-mode --timeout-sec 300`; for deeper literature refresh use a longer timeout and omit `--fast-mode` only after the fast path has produced usable outputs.
- A workflow run is scientifically usable only if it produces non-fallback papers or repos plus LLM/API status evidence. `recoverable_fallback` keeps the pipeline connected but does not count as literature evidence, novelty evidence, or paper support.
- If the workflow times out or LLM live check fails, the research loop must record the blocker, continue ordinary discovery, and schedule another workflow retry after network/API/source repair instead of silently treating fallback as success.
- The workflow and all LLM agents currently use Chat Completions by default (`LLM_API_MODE=chat_completions`) for the configured OpenAI-compatible endpoint. Do not switch protocols without an explicit user request and a live check.
- Do not write API keys into project files. Configure LLMs through environment variables only.

## OpenAI-Compatible LLM Configuration

Use environment variables for generic LLM-only mode:

```bash
export LLM_PROVIDER=openai_compatible
export LLM_API_BASE="https://example-compatible-endpoint/v1"
export LLM_MODEL="model-name"
export OPENAI_API_KEY="..."
export LLM_TIMEOUT_SEC=60
export LLM_MAX_TOKENS=1200
```

For slower reasoning models, run narrow role subsets first, for example:

```bash
python3 scripts/run_llm_research_team.py --project <project> --roles planner,researcher,critic --context-limit 24000
```

The LLM team has three robustness layers: strict JSON parsing, LLM-based JSON repair, and heuristic recovery from non-JSON role drafts. Recovered outputs are useful but should be checked against raw artifacts.


## Claude Code Agent Pack

When Claude Code is available, The workflow loads project memory from `CLAUDE.md` and `.claude/`. The pack contains research subagents, skills, and slash commands for ARIS-style experiment loops and PaperOrchestra-style paper writing. The generic LLM/Codex paths remain supported, but Claude-backed runs should use these resources instead of a single unstructured prompt.
