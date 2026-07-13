# Experiment Recording Prompt

Use this prompt when Experimenting asks Claude Code to maintain metrics, registry rows, and experiment record tables.

You must first wait for the experiment and final validation commands to finish. Then read only their local launcher contracts, stdout/stderr logs, `experiment_iteration_summary.json`, `metrics.json`, `bad_cases.json`, current selected plan state, and existing registry/table files.

Required writes:

- artifact-local `experiment_record.json`
- `state/experiment_registry.json` as a JSON list
- `experiments/experiment_records.csv`
- `experiments/实验记录.md`

Required `experiment_record.json` keys:

- `timestamp`
- `validation_finished_at`
- `validation_return_code`
- `recorded_at`
- `run_id`
- `experiment_id`
- `iteration`
- `status`
- `method`
- `dataset`
- `repo_path`
- `artifact_path`
- `commands`
- `metrics`
- `acceptance_status`
- `acceptance_blockers`
- `evidence_paths`
- `bad_case_paths`
- `reflection`
- `next_action`

Rules:

- Write or update records only after final validation has completed and its return code and output paths are known.
- Set `validation_finished_at` from the completed validation receipt, then set `recorded_at` when the registry row is written.
- Metrics must come from `metrics.json` or parseable local logs.
- Project CSV and Markdown rows must match the JSON registry row count.
- Unsupported, synthetic-only, incomplete, or contaminated evidence must be recorded as blocked/skipped/failed, not accepted.
- Importing launcher artifacts means reading the artifact evidence and writing the same registry/table contract above.
- Record maintenance must preserve older rows unless a row has the same `run_id` and `iteration` or the same `artifact_path`.
