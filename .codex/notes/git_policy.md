# TASTE Git Policy

Track framework source, prompts, templates, docs, project configs, and lightweight planning files. Do not track research-object repositories, downloaded datasets, model checkpoints, generated PDFs, logs, or runtime state.

Before committing, run:

```bash
git status --short --ignored
```

Expected ignored examples:

- `projects/*/repos/`
- `projects/*/state/`
- `projects/*/reports/`
- `modules/taste/auto_research/runs/`
- `modules/taste/auto_research/web/client/dist/`
