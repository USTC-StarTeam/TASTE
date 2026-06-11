# Start Here

This is the short takeover path for a new TASTE workspace.

## 1. Prepare The Runtime

```bash
python -m pip install -r modules/taste/requirements.txt
mkdir -p runtime
cp config.example.json runtime/.config.json
```

The frontend is built automatically by `scripts/start_web.sh` on first launch.

Set LLM credentials through the web UI or `runtime/.config.json`; environment variables are only startup fallbacks for empty saved fields. Keep credentials out of Git. `MANAGEMENT_PYTHON` belongs to the Web/runtime launch environment; configure the experiment/training Python later in the Environment stage for each project.

If you will use the paper-writing module, restore the runtime-only writing references:

```bash
python scripts/sync_writing_vendor.py --venue <venue>
```

## 2. Start The Web/API

```bash
scripts/start_web.sh
```

Open `http://127.0.0.1:8765` and create or select a project in the web UI. Project directories under `projects/` are private runtime state and are intentionally ignored by Git.

## 3. Run Through Web Controls

Use the web/API job controls for Find, Read, Idea, Plan, experiments, and writing. Do not start duplicate full-cycle, Find, Read, or training processes manually. Experiments should go through launch wrappers so logs, PIDs, locks, metrics, and audits are traceable.

## 4. Verify After Changes

Run focused tests and rebuild the frontend after frontend edits:

```bash
PYTHONPATH="$PWD/modules/taste" python -m pytest modules/taste/tests -q
npm --prefix modules/taste/auto_research/web/client run build
```

Before publishing, check that only framework files, examples, templates, and docs are tracked.
