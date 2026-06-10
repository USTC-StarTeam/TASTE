Coordinator startup for a new session in `<workspace_root>`:

1. Read `START_HERE.md`.
2. Read the root `AGENTS.md`.
3. Read `automation/three_agent_protocol.md`.
4. Read `projects/<project>/AGENTS.md`.
5. Read the workspace `工作状态.txt`; temporary handoff files may exist under root `handoff/` during a transfer, but they are private, ignored, and not a formal project startup source.
6. If subagents are available in the current backend or orchestrator, instantiate the three research roles under `prompts/subagents/`; otherwise emulate the same roles sequentially in one runner.
7. Keep orchestration local in the coordinator.
