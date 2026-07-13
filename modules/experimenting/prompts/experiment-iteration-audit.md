# Experiment Iteration Audit Prompt

Use this prompt for `audit_kind=experiment_iteration`.

You must inspect the selected project evidence pack and determine whether the experiment loop has usable evidence for the current selected plan.

Required checks:

- selected Find/Plan identity is visible in current state files
- at least one current experiment attempt has command, log path, artifact path, status, and metric evidence
- running jobs have PID/log/artifact evidence and produce `status=running`
- completed jobs have stdout/stderr or equivalent local log evidence
- metrics come from local metric files or parseable logs
- bad-case, failure, counterexample, or claim-pressure evidence exists before paper promotion
- reflection and next action exist after completed attempts

Output must be `audit_adjudication.json`. Missing or stale selected-plan evidence must set `status=blocked` and `decision=refresh_plan`. Running experiment evidence must set `status=running` and `decision=wait_running`.
