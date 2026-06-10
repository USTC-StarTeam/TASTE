# Start Here

This is the short takeover path for a new TASTE workspace.

## 1. Prepare The Runtime

```bash
python -m pip install -r modules/taste/requirements.txt
npm --prefix modules/taste/auto_research/web/client install
npm --prefix modules/taste/auto_research/web/client run build
mkdir -p runtime
cp config.example.json runtime/.config.json
```

Set LLM credentials through the web UI or `runtime/.config.json`; environment variables are only startup fallbacks for empty saved fields. Keep credentials out of Git. If your TASTE management environment and experiment/training environment are different, set `MANAGEMENT_PYTHON` and `EXPERIMENT_PYTHON`, or save those paths in the web runtime/environment panels.

If you will use the paper-writing module, restore the runtime-only writing references:

```bash
python scripts/sync_writing_vendor.py --venue <venue>
```

## 2. Create Or Pick A Project

```bash
python scripts/create_project.py --name <project> --topic "<topic>" --prompt "<goal>" --query "<query>"
```

Project directories under `projects/` are private runtime state. They are intentionally ignored by Git.

## 3. Start The Web/API

```bash
WORKSPACE_ROOT="$PWD" PROJECT_ID=<project> DEFAULT_PROJECT_ID=<project> PYTHONPATH="$PWD/modules/taste:$PWD" scripts/start_web.sh
```

Open `http://127.0.0.1:8765`.

## 4. Run Through Web Controls

Use the web/API job controls for Find, Read, Idea, Plan, experiments, and writing. Do not start duplicate full-cycle, Find, Read, or training processes manually. Experiments should go through launch wrappers so logs, PIDs, locks, metrics, and audits are traceable.

## 5. Verify After Changes

Run focused tests and rebuild the frontend after frontend edits:

```bash
PYTHONPATH="$PWD/modules/taste" python -m pytest modules/taste/tests -q
npm --prefix modules/taste/auto_research/web/client run build
```

Before publishing, check that only framework files, examples, templates, and docs are tracked.
