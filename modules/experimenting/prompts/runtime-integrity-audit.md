# Runtime Integrity Audit Prompt

Use this prompt for `audit_kind=runtime_integrity`.

You must inspect watchdog output, experiment run manifest, launcher contracts, PID/log evidence, and imported registry rows.

Required checks:

- active experiment workers use the project experiment Python
- one artifact directory has one launcher contract and one stdout/stderr stream
- duplicate writers, wrong interpreters, NUL logs, contaminated artifacts, and reused artifact dirs are blockers
- launcher return code is treated as process-start evidence only
- import readiness requires process exit plus clean watchdog/artifact evidence
- metrics and logs must point to local files

Output must be `audit_adjudication.json`. Process contamination must set `status=blocked` and `decision=repair_runtime`. Active clean workers must set `status=running` and `decision=wait_running`.
