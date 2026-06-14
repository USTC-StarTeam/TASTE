Coordinator startup for a new project session in `<workspace_root>`:

1. Read the root `AGENTS.md` for stable TASTE boundaries.
2. Read `automation/three_agent_protocol.md`.
3. Read `projects/<project>/AGENTS.md`.
4. Read project-local status, reports, and state artifacts under `projects/<project>/`; project handoff files belong only inside the project directory.
5. Do not treat root `HANDOFF.md` or root `工作状态.txt` as project scientific memory; those are for framework-maintainer handoff only.
6. If subagents are available in the current backend or orchestrator, instantiate the three research roles under `prompts/subagents/`; otherwise emulate the same roles sequentially in one runner.
7. Keep orchestration local in the coordinator.
