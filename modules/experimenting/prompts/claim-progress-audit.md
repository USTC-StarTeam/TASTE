# Claim Progress Audit Prompt

Use this prompt for `audit_kind=claim_progress`.

You must inspect reference reproduction evidence, runtime integrity evidence, experiment iteration evidence, scientific progress gates, paper evidence gates, metrics, logs, bad cases, and counterexamples.

Required checks:

- reference reproduction is current and passing
- runtime integrity is clean or only has non-blocking warnings
- candidate experiment evidence is current, audit-ready, and comparable to the reference/control
- metrics come from local commands/logs/artifacts
- bad-case or counterexample pressure is recorded
- unsupported paper claims remain blocked

Output must be `audit_adjudication.json`. `claim_promotion_allowed` may be true only when every required check has local evidence paths.
