# Real Data Smoke Prompt

Use this prompt instead of hard-coded dataset-specific smoke scripts.

Given a repo, conda env, dataset target, and artifact directory:

1. Find the repo's smallest real-data loader or evaluation entrypoint.
2. Prefer existing configs and documented commands; patch only a temporary low-resource config when needed.
3. Run one bounded real-data smoke that exercises loader, model path, and metric emission.
4. Restore temporary repo config changes unless they are part of the selected method implementation.
5. Write stdout/stderr, `metrics.json`, `bad_cases.json` when possible, and `experiment_iteration_summary.json`.

The summary must state one of: `plumbing_only`, `real_data_metric_ready`, or `blocked_missing_real_data_evidence`. Domain-specific probes must report their dataset, split, command, metric file, and evidence tier. Paper evidence requires the selected benchmark, evaluation protocol, command log, and metric file.
