# Project: Competitor Content Tracker Bot (MVP: YouTube only)

## Goal
Telegram bot + Django Admin to track competitors' content performance.
Users do NOT log into social networks. Users only provide a profile URL/handle.

MVP scope:
- YouTube end-to-end (setup -> discovery -> collection -> scoring -> scheduled reporting).
- YouTube coverage includes both long-form videos and Shorts (anything in channel uploads feed).
- TikTok + Instagram adapters exist as stubs only (same interface, return empty).
- Report format always supports 3 networks (Top 5 per network), but only YouTube is populated in MVP.
- Bot UI/messages: Russian.

## Tech stack
- Python 3.12
- Django + Django Admin
- Postgres
- Redis + Celery + Celery Beat
- aiogram (Telegram bot, long polling for dev)
- Docker Compose for local dev

## Product requirements (MVP)
Onboarding in Telegram:
1) user sends a profile link/handle (any platform) or a short niche description
2) system infers niche keywords if seed is YouTube (channel description + recent titles); otherwise ask user to input keywords
3) user can paste competitor list (optional). System still does its own discovery for YouTube
4) user reviews/edits final YouTube competitor list
5) user configures schedule: 1 or 2 times per day + preferred time(s); timezone via location (optional) or manual input

Reporting:
- Send a report for the period (since last run) with TOP 5 YouTube videos that went “viral” relative to each competitor baseline.
- Report message must include 3 sections: YouTube (filled), TikTok (stub), Instagram (stub).

## Defaults / limits (env-configurable)
- BASELINE_N = 30
- BASELINE_WINDOW_DAYS = 30
- YT_RECENT_N_FOR_METRICS = 15 (how many recent videos per competitor we refresh each run)
- MAX_COMPETITORS_YOUTUBE = 20 (raise carefully; quota risk)
- MIN_DELTA_VIEWS = 500
- Avoid expensive YouTube API calls:
  - Prefer channels.list + channelSections.list + playlistItems.list + videos.list
  - Use search.list only for discovery / fallback resolving, strictly limited and cached

## Engineering rules
- Modular code: adapters + services + celery tasks.
- Use Django migrations only.
- Minimal tests: YouTube URL parsing, time parsing, scoring.
- Structured logging + JobRun table to track task failures.
- Docker Compose must include: web, bot, worker, beat, db, redis.
- Never prompt users for social logins.

## Commands & docs
Provide README with exact commands to run:
- docker compose up -d
- docker compose exec web python manage.py migrate
- docker compose exec web python manage.py createsuperuser

## Acceptance criteria
- A user can complete setup and receive a scheduled YouTube report in Telegram.
- Admin can inspect users, competitors, reports, job runs in Django Admin.
