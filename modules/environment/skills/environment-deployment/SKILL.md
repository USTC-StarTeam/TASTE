---
name: environment-deployment
description: Deploy and audit a research-paper reproduction environment from a selected TASTE plan using real repositories, real data, run-local Conda, executable commands, and auditable evidence.
allowed-tools: Bash Read Grep Glob
---

# Environment Deployment Skill

Use this skill for Environment repository review, repository discovery, environment planning, run audit, and reproduction judgement.

## Authorized Workspace

- Work inside the current Environment run directory and cloned repositories supplied by the prompt.
- Write only the requested JSON output path and command artifacts inside the current run directory.
- Write the requested JSON output file as a JSON object only.
- Use the user's existing Claude Code authentication.
- Use run-local Conda prefixes under `conda_envs/<env_name>`.

## Repository Work

- Rank provided candidates only from prompt evidence and repository identity evidence.
- Discover a repository only from prompt evidence that links the repository to the target paper, authors, organization, package, or reproduction instructions.
- Return `reject` when repository evidence is insufficient.

## Environment Plan

- Output command plans as strict JSON.
- Put every command in JSON array token form.
- Use `cwd` values of `repo`, `run`, or a path inside the run directory.
- Include Conda creation/install, import verification, real data/checkpoint preparation, loader/model smoke, and full reference reproduction phases.
- Use real datasets, loaders, checkpoints, repository scripts, and evaluation targets.
- Use `hf download` for HuggingFace Hub.
- Repair dependency, CUDA, PyTorch, PyG, ESM, Python-version, package-index, checkpoint, dataset, path, and command failures by emitting corrected JSON commands in the next plan.
- Add a verification command after each dependency, package-index, CUDA, loader, data, or checkpoint repair.
- Treat backend approval and handoff failures as the required repair target for the next plan.

## Evidence

- Bind `machine_assessment` to concrete local GPU/CPU/CUDA/Conda facts from `machine_profile`.
- Bind `paper_config_alignment` to dataset, metric, epoch/steps, batch size, learning rate, seed, checkpoint/pretraining, hardware/precision, and local adaptation evidence.
- Bind `success_criteria` to metric name, comparison operator, numeric or percentage target, and paper/README/plan source.
- Bind paper-level approval to successful required full reproduction or evaluation receipts and metric evidence.
- Bind environment handoff readiness to repository source, run-local Conda, real data/loader/model smoke, required command receipts, and workspace audit readiness.

## Audit And Judgement

- Decide only `approve`, `reject`, or `continue_repair`.
- Approve only when every required audit check has evidence.
- Emit exactly these required audit check names when judging a run: `repository_source`, `repository_documentation`, `run_local_conda`, `required_commands`, `machine_fit`, `dataset_evidence`, `success_criteria_paper_binding`, `paper_context`, `paper_config_alignment`, `metric_evidence`, `reproduce_full`.
- For every audit check, include `passed`, `reason`, and `evidence`.
- Pass semantic checks only from direct evidence in the prompt, receipts, logs, repository files, paper evidence, and current run artifacts.
- Continue repair for fixable dependency, path, dataset, command, configuration, or metric problems.
- Reject only for proven unrecoverable repository, paper, data access/license, or machine-compute blockers.
- Use JSON booleans for all `passed`, `repairable`, `allow_next_module`, `paper_claims_verified`, and `reproduction_success` fields.

## Portable Notes

- Claude selects and repairs PyTorch/PyG/CUDA, Python, package-index, and project dependency commands from run evidence.
- The backend enforces run-local Conda, command safety, path boundaries, receipts, and approval gates.
- `dm-tree` imports as `tree`.
- Project-specific fixes must come from files and logs in the current run.
