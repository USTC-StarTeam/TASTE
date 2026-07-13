# Experimenting Script And Function Audit

Date: 2026-07-11 UTC

Public rule: Framework and command-line users invoke Experimenting only through `modules/experimenting/main.py`. Web invokes Framework and never invokes this module. Every file under `scripts/` is private.

## Classification

| Class | Keep only when |
| --- | --- |
| A | It is required to manage the project-unique Claude session, queue, interruption, process, or module I/O boundary. |
| B | It enforces a deterministic runtime/process/file fact that prompt-only Claude work cannot reproduce reliably. |
| C | It is judgment, diagnosis, experiment design, record interpretation, or prose generation; it must be a skill/prompt instead of a script flow. |

## Every Remaining Script

| Script | Class | Why it still exists |
| --- | --- | --- |
| `scripts/orchestration/controller_session.py` | A/B | Maintains the module-owned `project -> session UUID` table, fixed per-project controller directory, one execution lock per project, FIFO Web queue, priority interruption, automatic resumption, Claude process PID, project cwd, and validation-before-recording hard Gate. Controller messages create no run. Prompt text cannot guarantee mutual exclusion, signal a live process, or preserve one exact session under concurrent Web requests. |
| `scripts/audits/run_claude_audit.py` | A/B | Creates a fresh audit Claude process, packs bounded local evidence, captures its prompt/log/result, and checks that the required JSON exists. The audit verdict itself remains Claude-owned. |
| `scripts/common/entrypoint_guard.py` | B | Rejects direct private-script execution and rejects management Python outside Conda `taste`. |
| `scripts/common/runtime_environment.py` | B | Resolves actual Conda/Claude/Node/PATH facts and writes a reproducible runtime lock. Environment discovery and executable identity are machine facts. |
| `scripts/common/experiment_plan.py` | B | Parses JSON/YAML/text plan inputs used by deterministic tools into one stable schema. It does not choose an experiment. |
| `scripts/common/experiment_contracts.py` | B | Supplies deterministic row, metric, role, evidence-path, and claim-promotion checks reused by gates. It does not make scientific judgments. |
| `scripts/common/file_utils.py` | B | Provides atomic JSON/text writes, UTC timestamps, safe slugs, and compact text used by concurrent runtime code. |
| `scripts/execution/launch_experiment_run.py` | B | Starts one experiment with one fresh artifact directory, run contract, PID sidecar, stdout/stderr log, and project experiment Python. These process guarantees require code. |
| `scripts/execution/experiment_run_watchdog.py` | B | Reads OS PIDs, process groups, file descriptors, interpreter paths, run contracts, duplicate writers, and contaminated logs. These observations cannot be replaced by a prompt. |

No remaining script designs an experiment, diagnoses a scientific failure, selects a method, interprets novelty, writes the experiment table, or decides whether a paper claim is good.

## Deleted Or Converted

| Deleted area | Replacement | Reason |
| --- | --- | --- |
| `scripts/orchestration/run_autonomous_experiment.py` and public `run` aliases | `controller_session.py`, `skills/experiment-iteration/SKILL.md`, precise controller prompt | It created a second fresh-Claude loop, worked from the repo instead of the project, and validated after Claude had already written records. Work and chat now use one module-owned session. |
| `scripts/agent/run_coding_agent.py` | Project-unique Experimenting controller | It was a second Claude route. |
| `scripts/orchestration/run_loop.py` | Framework full-cycle stage sequence | Whole-pipeline orchestration is not Experimenting work. |
| Failure-analysis scripts | `prompts/failure-analysis.md` | Failure diagnosis requires reading method, logs, metrics, and counterexamples together. |
| Repo/domain smoke scripts, including `scripts/execution/proteinshake_realdata_probe.py`, `run_active_repo_smoke.py`, and `run_real_repo_smoke.py` | `prompts/real-data-smoke.md` | The correct command and data check are repository-specific. |
| Scripted iteration/runtime/reference verdicts | Fresh audit Claude prompts plus watchdog facts | Scripts can collect machine facts but cannot make complete scientific adjudications. |
| Experiment registry/import/table scripts and `import_artifacts`/`record_table` actions | `prompts/experiment-recording.md` used by the main controller | The controller must interpret final validation output before writing registry, CSV, and Markdown records. The deterministic ordering Gate checks timestamps and return codes afterward. |
| Empty `__init__.py` files | None | They provided no runtime behavior. |

ProteinShake is a domain-specific probe; its repository-specific commands and data checks belong in `prompts/real-data-smoke.md`, not a permanent Experimenting script.

## Skills And Prompts

| File | Claude-owned work |
| --- | --- |
| `skills/experiment-iteration/SKILL.md` | Current-contract intake, adaptive experiment design, implementation, execution, validation, recording, and next decision. |
| `skills/experiment-runtime-tools/SKILL.md` | Correct use of public launcher, watchdog, runtime, and audit actions. |
| `skills/experiment-audit-adjudication/SKILL.md` | Independent evidence-grounded audit procedure. |
| `prompts/adaptive-experiment-planning.md` | Find/Read-aware execution plan inside the selected route. |
| `prompts/experiment-recording.md` | Validation-first registry, CSV, Markdown, and artifact record format. |
| `prompts/failure-analysis.md` | Evidence-grounded failure diagnosis and repair choice. |
| `prompts/real-data-smoke.md` | Repository-specific real-data and loader smoke work. |
| `prompts/experiment-iteration-audit.md` | Independent iteration evidence audit. |
| `prompts/runtime-integrity-audit.md` | Independent runtime integrity audit. |
| `prompts/reference-reproduction-audit.md` | Independent reference reproduction audit. |
| `prompts/claim-progress-audit.md` | Claim-support pressure test. |
| `prompts/audit-adjudication.md` | Shared structured audit output contract. |
