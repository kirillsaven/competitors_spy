# Reviewer Evidence

This page is a compact map for external reviewers. It describes the project as open source software, not as a request for sponsorship or approval.

## What It Does

Competitor Spy is a self-hosted Telegram bot plus Django Admin for competitor-content trend reports across YouTube, TikTok, and Instagram.

Users provide public profile URLs or handles. They do not log into social networks through the app.

## Why It Is Useful OSS

- It is a practical reference for Telegram bot onboarding with aiogram.
- It shows Django Admin as an operator surface for users, competitors, reports, schedules, and job runs.
- It uses Celery and Celery Beat for scheduled collection and reporting.
- It keeps provider integrations modular.
- It scores content against each competitor's own recent baseline.
- It includes public-safe maintainer automation that is optional and separate from core runtime.

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web pytest
```

Open Django Admin at:

```text
http://localhost:8000/admin/
```

## Review Links

- [README](../README.md)
- [Demo script](./demo-script.md)
- [Sample report payload](./examples/sample-report-payload.md)
- [Troubleshooting](./troubleshooting.md)
- [Roadmap](../ROADMAP.md)
- [Security policy](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Support](../SUPPORT.md)
- [Promotion kit](./promotion-kit.md)

## CI and Docker

- CI workflow: https://github.com/kirillsaven/competitors_spy/actions/workflows/ci.yml
- Docker image workflow: https://github.com/kirillsaven/competitors_spy/actions/workflows/docker-image.yml
- Current `main` image:

  ```bash
  docker pull ghcr.io/kirillsaven/competitors_spy:main
  ```

## Release State

Current release state:

- Latest GitHub Release: `v0.1.1`.
- Existing tag: `v0.1.0`, created before the GHCR workflow existed.
- GHCR `main` image: public and inspectable.
- GHCR release image: `ghcr.io/kirillsaven/competitors_spy:0.1.1`.

## Starter Issues

- #75: Add a sanitized sample Telegram report payload.
- #76: Document provider diagnostics for empty report sections.
- #77: Add synthetic Instagram provider parsing fixtures.
- #78: Add GHCR Docker image usage notes.
- #79: Improve Russian report copy for empty platform sections.

Some of these are addressed by the OSS approval polish PR. Keep at least a few beginner-friendly issues open after merge.

Replacement starter issues already opened:

- #81: Add YouTube sample fixture for missing engagement metrics.
- #82: Add provider diagnostics test for empty Instagram response.
- #83: Add docs asset accessibility pass.
- #84: Add maintainer demo recording checklist.

## OpenAI Runtime Note

OpenAI is not required for core runtime. Optional maintainer tooling under `tools/` can use OpenAI when configured, but the Telegram bot, Django Admin, providers, scoring, and scheduled reports are designed to run without OpenAI credentials.

## Adoption Claims

No adoption, star count, install count, or production usage claim should be inferred from this page. Use current GitHub metadata for live repository statistics.
