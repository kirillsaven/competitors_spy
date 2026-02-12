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
1) user sends a profile link/handle (required)
2) system infers niche keywords:
   - prefer LLM (Gemini) when configured + rate-limited
   - fallback to heuristic (channel description + recent titles)
   - if weak/empty: ask user to input keywords manually
3) user can paste competitor list (optional). System still does its own discovery for YouTube.
   If user provided competitors, use them to improve niche inference and YouTube discovery.
4) user reviews final YouTube competitor list by removing irrelevant channels (default: apply all suggestions)
5) user configures timezone and schedule:
   - timezone via location (optional) or manual input
   - schedule: 1 or 2 times per day (prefer presets; manual time as fallback)

Reporting:
- Send a report for the period (since last run) with TOP 5 YouTube videos that went “viral” relative to each competitor baseline.
- Report message must include 3 sections: YouTube (filled), TikTok (stub), Instagram (stub).

## Defaults / limits (env-configurable)
- BASELINE_N = 30
- BASELINE_WINDOW_DAYS = 30
- YT_RECENT_N_FOR_METRICS = 15 (how many recent videos per competitor we refresh each run)
- MAX_COMPETITORS_YOUTUBE = 20 (raise carefully; quota risk)
- MIN_DELTA_VIEWS = 500
- MIN_VIEWS_END = 1000 (only for warm-up/fallback scoring when delta is unavailable)
- Avoid expensive YouTube API calls:
  - Prefer channels.list + channelSections.list + playlistItems.list + videos.list
  - Use search.list only for discovery / fallback resolving, strictly limited and cached

LLM (optional):
- GOOGLE_LLM_API_KEY (Gemini) is used only for internal tasks (no user chat)
- Rate-limit: GOOGLE_LLM_MAX_CALLS_PER_USER_PER_DAY (default 3)

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
