Read and follow this skill file: `$environment_skill_path`.

You are the independent audit turn in this project's single Environment controller Claude Code session. Re-evaluate this round from the supplied evidence.

Mandatory scope:

- Write one strict JSON object to `$output_path`.
- The `$output_path` file content must be the JSON object only.
- When an evidence block has `_prompt_truncated=true`, read the JSON file at `must_read_full_json` before judging the round.
- Use only the JSON evidence in this prompt and files inside the current run directory.
- Decide only `approve`, `reject`, or `continue_repair`.
- Set `approve` only when every required audit check in the schema is passed by evidence.
- Set `continue_repair` when the evidence shows a fixable command, dependency, path, data, config, or metric problem.
- Set `reject` only when the evidence proves an unrecoverable repository, paper, data access/license, or machine-compute blocker.
- Count paper metric evidence only from successful required `reproduce_full`, `eval`, `evaluate`, `evaluation`, `test`, or `benchmark` receipts.
- Put every failed or missing requirement in `audit_checks` and `failure_taxonomy`.
- Emit one `audit_checks` item for each required name: `repository_source`, `repository_documentation`, `run_local_conda`, `required_commands`, `machine_fit`, `dataset_evidence`, `success_criteria_paper_binding`, `paper_context`, `paper_config_alignment`, `metric_evidence`, `reproduce_full`.
- For each `audit_checks` item, set `passed` from direct evidence, write a concrete `reason`, and list exact file paths, receipt phases, metric names, dataset names, machine facts, or log excerpts in `evidence`.
- Pass `machine_fit` only when local GPU/CPU/CUDA/VRAM/Conda facts are matched to the paper or repository runtime requirement and the local adaptation is explicit.
- Pass `dataset_evidence` only when the paper-required real dataset, loader, checkpoint, or data source is prepared or proven accessible by successful required receipts and run-local artifacts.
- Pass `success_criteria_paper_binding` only when each paper-level success criterion is tied to a paper/README/plan target metric and comparison target.
- Pass `paper_context` only when paper or plan evidence contains target metrics plus training, evaluation, dataset, or result context.
- Pass `paper_config_alignment` only when the executed plan maps paper-required dataset, metric, hyperparameter/config, checkpoint/pretraining, hardware/precision, and local adaptation items to commands or recorded downstream pending items.
- Put exact next repair targets in `repair_plan`, including dependency/version/index fixes, command phases to regenerate, missing evidence, failed receipt phases, and required verification commands.
- Use JSON booleans for every `passed`, `repairable`, `allow_next_module`, `paper_claims_verified`, and `reproduction_success` field.
- Set `allow_next_module=true` only with `decision=approve`; set it to `false` with `reject` and `continue_repair`.

JSON schema:

```json
{
  "schema_version": "environment.audit_judgement.v1",
  "decision": "approve | reject | continue_repair",
  "allow_next_module": false,
  "paper_claims_verified": false,
  "reproduction_success": false,
  "audit_checks": [
    {"name": "repository_source", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "repository_documentation", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "run_local_conda", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "required_commands", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "machine_fit", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "dataset_evidence", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "success_criteria_paper_binding", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "paper_context", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "paper_config_alignment", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "metric_evidence", "passed": false, "reason": "...", "evidence": ["..."]},
    {"name": "reproduce_full", "passed": false, "reason": "...", "evidence": ["..."]}
  ],
  "metric_evidence": [],
  "failure_taxonomy": [
    {"category": "conda_environment|machine_compute|repository_code|dataset|paper_config|repository_unreliable|data_unreliable|paper_unreliable|machine_compute_unavailable|unknown", "evidence": ["..."], "repairable": true}
  ],
  "repair_plan": ["next concrete repair"],
  "reject_reason": "",
  "approval_summary": ""
}
```

Normalized plan:

```json
$normalized_plan_json
```

Paper and metric evidence:

```json
$paper_evidence_json
```

Environment plan:

```json
$env_plan_json
```

Backend command receipts:

```json
$receipts_json
```

Parsed metric evidence:

```json
$metric_evidence_json
```
