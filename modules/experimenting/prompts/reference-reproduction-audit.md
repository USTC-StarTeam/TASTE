# Reference Reproduction Audit Prompt

Use this prompt for `audit_kind=reference_reproduction`.

You must inspect current Find/Plan state, environment repo selection, active repo evidence, reference reproduction artifacts, target metric configuration, registry rows, and paper/claim gates.

Required checks:

- the active repo/base belongs to the current selected Find/Plan route or has an explicit current-route full reference anchor
- loader/data/protocol evidence exists before bounded or full reference reproduction is accepted
- full reference reproduction evidence includes command, log, artifact, metric, dataset, repo path, and target/tolerance comparison when a target is configured
- a running reference reproduction job produces `status=running`
- stale historical-route reference evidence cannot satisfy the current selected route
- paper/claim promotion requires a passing reference anchor plus later candidate experiment evidence

Output must be `audit_adjudication.json`. Missing current-route base evidence must set `status=blocked` and `decision=refresh_plan` or `continue_experiment` according to the required next action. Running reference reproduction must set `status=running` and `decision=wait_running`.
