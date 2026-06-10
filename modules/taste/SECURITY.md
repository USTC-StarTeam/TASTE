# Security Policy

The workflow is designed as a local-first research assistant. Please keep private credentials and research context out of the repository.

## Do Not Commit

- `runtime/.config.json`
- LLM API keys
- SMTP passwords or app passwords
- private researcher profiles
- generated run artifacts in `runtime/runs/`
- downloaded PDFs or paper caches
- private email reports

Use the repository-root `config.example.json` as the safe template for public examples.

## Reporting Issues

If you find a security issue, open a private disclosure channel with the maintainer rather than posting secrets or exploit details in a public issue.

