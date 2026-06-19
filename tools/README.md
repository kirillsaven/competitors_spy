# Maintainer AI tools

This directory contains optional maintainer automation for Competitor Spy. It is not required for the application runtime, Telegram bot, scheduled reports, or tests.

Commands:

```bash
python tools/maintainer_ai.py pr-summary --diff-file path/to.diff
python tools/maintainer_ai.py issue-triage --issue-file path/to_issue.md
python tools/maintainer_ai.py release-notes --changelog CHANGELOG.md
```

Behavior:

- If `OPENAI_API_KEY` is not set, the tool uses deterministic local fallback heuristics.
- If `OPENAI_API_KEY` is set, the tool lazy-imports the optional `openai` SDK and calls the Responses API.
- If the optional SDK is missing while `OPENAI_API_KEY` is set, the tool fails with an explicit error.
- The core app and default test suite do not require OpenAI credentials.

Safety:

- Likely secrets are redacted before any model call.
- Raw `.env` files are refused as input.
- Diffs containing `.env` changes are not sent to OpenAI.
- Do not paste real bot tokens, API keys, database URLs, private keys, Telegram IDs, or private server details into input files.

Workflow example:

- A documentation-only GitHub Actions example lives at `docs/examples/maintainer-ai-example.workflow.yml`.
- It is intentionally outside `.github/workflows/` so it cannot run unless a maintainer copies it into the active workflow directory.
- Review generated output manually before sharing it publicly.

Optional install:

```bash
python -m pip install openai
```
