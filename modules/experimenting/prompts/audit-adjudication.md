# Audit Adjudication Prompt

Use this prompt when Claude Code adjudicates Experimenting audit evidence.

You must inspect the provided audit pack, deterministic gate JSON, project reports, logs, metrics, bad cases, and current selected Find/Plan state. Produce only an evidence-grounded adjudication with:

- `status`: `pass`, `warn`, `blocked`, or `running`
- `decision`: `continue_experiment`, `repair_runtime`, `refresh_plan`, `wait_running`, `maintain_records`, `block_paper`, or `ready_for_next_stage`
- `claim_promotion_allowed`: boolean
- `findings`: each with severity, claim, local evidence paths, and one required next action
- `gate_alignment`: experiment iteration, runtime integrity, and reference reproduction status
- `next_action`: one concrete action

Write `audit_adjudication.json` and print the same JSON object to stdout. Missing evidence must become `blocked` or `running`.
