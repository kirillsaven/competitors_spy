# Competitor Content Tracker Bot (MVP: YouTube)

## Local run (Docker)
Все команды ниже выполняй из папки, где лежит `docker-compose.yml`.

1) Create `.env` from `.env.example` (skip if you already have `.env`) and fill:
- `TELEGRAM_BOT_TOKEN`
- `YOUTUBE_API_KEY`
- (optional) TikTok via Apify: `TIKTOK_PROVIDER=apify`, `TIKTOK_PROVIDER_ACCESS_TOKEN`, `TIKTOK_PROVIDER_BASE_URL`
- (optional) Instagram via Apify: `INSTAGRAM_PROVIDER=apify`, `INSTAGRAM_PROVIDER_ACCESS_TOKEN`, `INSTAGRAM_PROVIDER_BASE_URL`

2) Start services:
```bash
docker compose up -d
```

3) Run migrations:
```bash
docker compose exec web python manage.py migrate
```

4) Create admin user:
```bash
docker compose exec web python manage.py createsuperuser
```
`createsuperuser` creates a login/password for Django Admin (`/admin/`). It is not related to Telegram.
You can choose any username/password you want.

5) Open:
- Django Admin: `http://localhost:8000/admin/`

## Useful commands
Run report immediately for a user (by tg_user_id):
```bash
docker compose exec web python manage.py run_user_report <tg_user_id>
```

Verify live TikTok and Instagram report sections locally with real provider data:
```bash
docker compose exec web python manage.py verify_live_platform_report \
  --tiktok https://www.tiktok.com/@example \
  --instagram https://www.instagram.com/example/
```
The command resolves the supplied profiles through the real providers, refreshes snapshots, applies the current scoring pipeline, and fails if either requested platform still renders an empty report section.

Export top recent Instagram Reels from competitor profiles as producer-ready markdown:
```bash
docker compose exec web python manage.py export_instagram_competitor_spy \
  --instagram anyagal \
  --instagram englex_school \
  --format markdown \
  --output /tmp/instagram_competitor_spy.md
```
Use `--input-file /path/to/handles.txt` for a reusable competitor list. The export is intended for content planning: it ranks recent Reels with public view counts and adds a mechanism/adaptation prompt. It should be used to adapt structures, not copy wording.

Send a real live TikTok/Instagram report to Telegram and print the exact sent text plus Telegram `message_id`:
```bash
docker compose exec web python manage.py send_test_platform_report \
  --tg-user-id <your_telegram_user_id> \
  --tiktok nba \
  --instagram nasa
```
Use `--tg-chat-id` as well if the target chat id differs from the Telegram user id.

Run tests:
```bash
docker compose exec web pytest
```

## Production baseline
1) Prepare `.env` with production values:
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DATABASE_URL`
- `REDIS_URL`
- `TELEGRAM_BOT_TOKEN`
- `YOUTUBE_API_KEY`

2) Start the production stack:
```bash
docker compose -f docker-compose.prod.yml up -d --build
```

3) Check the app health endpoint:
```bash
curl http://localhost/healthz/
```

The production override switches Django to Gunicorn behind an Nginx reverse proxy, keeps `web` internal on the Docker network, publishes only port `80`, collects static files, runs migrations on web startup, and enables restart/healthcheck defaults for all services.

For the first real VPS deployment baseline, see `DEPLOY.md`.

Post-merge completion standard:

- merge is not enough
- done = merge + deploy + verify
- use `pwsh ./scripts/post_merge_deploy_verify.ps1 -SmokeUserId <user_id>` after every merged production PR
- truth report must include live SHA before, live SHA after, restarted services, one smoke check, and whether the key symptom is fixed

## CI
GitHub Actions runs `python manage.py check`, `python manage.py check --deploy --fail-level WARNING`, and `pytest -q` on pushes to `main` and on pull requests targeting `main`.

## Provider env vars
TikTok and Instagram now support Apify as MVP providers.

- `TIKTOK_PROVIDER`
- `TIKTOK_PROVIDER_BASE_URL`
- `TIKTOK_PROVIDER_API_KEY`
- `TIKTOK_PROVIDER_API_SECRET`
- `TIKTOK_PROVIDER_ACCESS_TOKEN`
- `TIKTOK_APIFY_PROFILE_ACTOR_ID`
- `TIKTOK_APIFY_RESULTS_PER_PROFILE`
- `INSTAGRAM_PROVIDER`
- `INSTAGRAM_PROVIDER_BASE_URL`
- `INSTAGRAM_PROVIDER_API_KEY`
- `INSTAGRAM_PROVIDER_API_SECRET`
- `INSTAGRAM_PROVIDER_ACCESS_TOKEN`
- `INSTAGRAM_APIFY_PROFILE_ACTOR_ID`

TikTok Apify setup:

- Set `TIKTOK_PROVIDER=apify`
- Set `TIKTOK_PROVIDER_ACCESS_TOKEN` to your Apify API token
- Optional: set `TIKTOK_PROVIDER_BASE_URL` (defaults to `https://api.apify.com/v2`)
- Optional: override `TIKTOK_APIFY_PROFILE_ACTOR_ID` (default `clockworks/tiktok-profile-scraper`)
- Optional: cap per-profile fetch size with `TIKTOK_APIFY_RESULTS_PER_PROFILE` (default `10`)

Cross-platform competitor cap:

- Set `MAX_COMPETITORS_PER_PLATFORM` to keep the same per-user cap on YouTube, TikTok, and Instagram
- Legacy `MAX_COMPETITORS_YOUTUBE` is still honored as a compatibility alias if the new setting is not present

Instagram Apify setup:

- Set `INSTAGRAM_PROVIDER=apify`
- Set `INSTAGRAM_PROVIDER_ACCESS_TOKEN` to your Apify API token
- Optional: set `INSTAGRAM_PROVIDER_BASE_URL` (defaults to `https://api.apify.com/v2`)
- Optional: override `INSTAGRAM_APIFY_PROFILE_ACTOR_ID` (default `apify/instagram-profile-scraper`)

Current Instagram MVP tracks only recent items that include public view counts from Apify output, because the existing scoring/report pipeline is view-based and this PR does not add a separate image-post scoring path.
