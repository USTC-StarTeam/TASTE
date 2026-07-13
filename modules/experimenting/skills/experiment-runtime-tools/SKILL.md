---
name: experiment-runtime-tools
description: "Use Experimenting deterministic runtime tools through main.py only: launcher, watchdog, runtime locks, and fresh-Claude audits."
allowed-tools: Bash Read Grep Glob
---

# Experiment Runtime Tools Skill

Use this skill when Claude Code needs deterministic Experimenting tools. The public route is the only callable route.

## Public Route Only

All module tools must be called through:

```bash
conda run -n taste python <workspace_root>/modules/experimenting/main.py --action <action> ...
```

Private files under `modules/experimenting/scripts/` are implementation details.

## Tools

- `--action work --project <project>`: send the module duty to that project's unique Experimenting controller session.
- `--action chat --project <project> --message <text>`: send a Web instruction to the same controller session; busy instructions queue and `--interrupt-current` runs the new instruction first.
- `--action runtime_env`: write an environment lock for a configured conda env.
- `--action launch`: start one project experiment with one artifact dir, one PID sidecar, one stdout/stderr log, and project-env Python enforcement.
- `--action watchdog`: inspect active experiment processes and contaminated artifact dirs.
- `--action audit_iteration --project <project>`: launch a fresh Claude audit of iteration evidence with the project as cwd.
- `--action runtime_integrity --project <project>`: launch a fresh Claude audit of runtime-integrity evidence with the project as cwd.
- `--action reference_reproduction --project <project>`: launch a fresh Claude audit of reference-reproduction evidence with the project as cwd.
- `--action audit_adjudication --project <project> --audit-kind <kind>`: launch a fresh Claude adjudication over the specified evidence pack.

## Runtime Rules

- Management Python must be conda env `taste`.
- The Experimenting controller Claude process must use `projects/<project>/` as its working directory.
- Controller `work`, `chat`, and `controller_status` use the fixed `.runtime/controllers/<project>/` directory and create no run.
- Deterministic tool and independent-audit invocations create one fixed folder under `modules/experimenting/.runtime/runs/`.
- `modules/experimenting/.runtime/latest_run/` is only a human review copy of a deterministic tool invocation.
- Program state must use the project controller state, explicit tool paths from stdout, `run_meta.json`, registry rows, or project state.
- New training launches must use the project experiment Python after `--`.
- Backgrounding, redirection, and process ownership must be delegated to `--action launch`.

## Evidence Rules

- A launcher return code of zero only means the process started. Record evidence only after the process exits and final validation plus watchdog/audit checks finish.
- Contaminated artifacts, wrong interpreter runs, reused artifact dirs, missing logs, or NUL logs are blockers.
- Metrics must point to local files and exact commands. Unsupported claims stay blocked.
