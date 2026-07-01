# Promotion kit

This file keeps public promotion copy in one place so the project can be shared consistently without overselling or spamming.

## Short description

Competitor Spy is a self-hosted Telegram bot plus Django Admin that tracks competitor content across YouTube, TikTok, and Instagram, scores new items against each competitor's own baseline, and sends scheduled Telegram reports.

## One-line pitch

Self-hosted competitor-content trend reports in Telegram for creators, agencies, and developers building content-intelligence workflows.

## Longer pitch

Competitor Spy helps creators and small teams stop manually checking competitor profiles. Users add public handles or URLs, the app collects public metrics through provider adapters, scores content against each competitor's recent baseline, and sends scheduled Telegram reports. The codebase is also a practical reference for Django, Celery, Redis, Postgres, aiogram, provider adapters, and admin observability.

## Social post

I opened Competitor Spy as OSS: a self-hosted Telegram bot + Django Admin for competitor-content trend reports across YouTube, TikTok, and Instagram.

It includes Docker Compose, Celery schedules, provider adapters, baseline-relative scoring, CI, release notes, and public-safe docs.

Repo: https://github.com/kirillsaven/competitors_spy

## Hacker News / Reddit style submission

Show HN: Competitor Spy - self-hosted Telegram reports for competitor content

Competitor Spy is a Django/Celery/aiogram app for tracking competitor content across YouTube, TikTok, and Instagram. It stores public metrics, compares new items against each competitor's baseline, and sends scheduled Telegram reports.

I built it as a practical self-hosted content-intelligence baseline and opened it with Docker setup, tests, CI, Django Admin observability, contribution docs, and a roadmap. Feedback on provider adapters, scoring, deployment hardening, and security review is welcome.

## GitHub topics

Suggested repository topics:

- telegram-bot
- django
- celery
- redis
- postgres
- youtube-api
- tiktok
- instagram
- apify
- content-intelligence
- competitor-analysis
- social-media-analytics
- self-hosted
- docker-compose
- open-source

## Ethical promotion rules

- Do not buy stars, trade stars, or use fake accounts.
- Do not post the same message repeatedly across communities.
- Disclose that you are the maintainer.
- Ask for feedback first; stars should be a secondary call to action.
- Share in communities where the project is genuinely relevant: Django, Telegram bot, Celery, self-hosted, creator tools, marketing automation, and indie hacker communities.

## Ethical visibility plan

1. Publish the release, sample report, troubleshooting docs, and visual preview first.
2. Record a 60-90 second demo clip. A longer 4-5 minute walkthrough can follow the script in `docs/demo-script.md`.
3. Post once from the maintainer's real account with a clear maintainer disclosure, a feedback request, and a secondary "star only if useful" call to action.
4. Ask 10-20 real developer friends, creators, or self-hosters for feedback. Do not ask for guaranteed stars.
5. Use Hacker News `Show HN` only when the maintainer has time to answer comments.
6. Post to Reddit, Discord, Telegram, or similar communities only where self-promotion is allowed and the project is genuinely relevant.
7. Rewrite the post for each community instead of reposting the same text everywhere.
8. Respond quickly to early issues and setup friction.

## Maintainer follow-up draft for OpenAI

```text
Hi OpenAI team - quick update on my OSS support application for kirillsaven/competitors_spy.

Since applying, I merged an OSS-readiness pass and prepared another review-polish PR: clearer README and quickstart, MIT/CONTRIBUTING/SECURITY/CODE_OF_CONDUCT, CI + Docker image workflow, public roadmap, starter issues, sanitized sample report output, troubleshooting docs, reviewer evidence, and a release/demo path. The project is a self-hosted Telegram + Django/Celery app for competitor-content trend reports across YouTube, TikTok, and Instagram, with optional maintainer AI tooling separate from core runtime.

Repo: https://github.com/kirillsaven/competitors_spy
Reviewer evidence: https://github.com/kirillsaven/competitors_spy/blob/main/docs/reviewer-evidence.md
CI: https://github.com/kirillsaven/competitors_spy/actions/workflows/ci.yml

Thanks for reviewing - feedback is welcome even if the application is not selected.
```

## Outreach checklist

- [ ] Add GitHub description and topics.
- [ ] Publish a short demo clip or screenshots.
- [ ] Share a concise post from the maintainer's real account.
- [ ] Ask 5-10 relevant developer friends or users for feedback, not just stars.
- [ ] Open beginner-friendly issues before posting.
- [ ] Respond to the first issues quickly.
- [ ] Keep the README current with any setup friction reported by users.
