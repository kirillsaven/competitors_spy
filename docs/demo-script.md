# Demo script

Use this script for a five-minute maintainer or contributor demo. It avoids real tokens and private account identifiers.

## Audience

- OSS reviewers evaluating whether the project is active and usable.
- Developers deciding whether to contribute.
- Self-hosters checking whether the architecture fits their workflow.

## Setup before recording

1. Start the local stack with placeholder-safe configuration:

   ```bash
   cp .env.example .env
   docker compose up -d --build
   docker compose exec web python manage.py migrate
   docker compose exec web python manage.py createsuperuser
   ```

2. Open Django Admin:

   ```text
   http://localhost:8000/admin/
   ```

3. Keep a terminal ready for checks:

   ```bash
   docker compose exec web python manage.py check
   docker compose exec web pytest -q
   ```

## Demo flow

### 1. Explain the product

Competitor Spy is a self-hosted Telegram bot plus Django Admin for scheduled competitor-content reports. Users provide public profile URLs or handles. The app does not ask users to log into social networks.

### 2. Show the architecture

Open the README architecture diagram and point out:

- Telegram bot: onboarding and commands.
- Django app: models, admin, management commands.
- Celery worker and beat: scheduled collection/reporting.
- Postgres and Redis: persistent state and queueing.
- Provider adapters: YouTube, TikTok, Instagram.

### 3. Show admin observability

In Django Admin, show the model groups a maintainer can inspect:

- Telegram users
- competitors
- content items
- metric snapshots
- schedules
- reports
- job runs

The JobRun table is important because scheduled collection must fail visibly instead of silently returning empty reports.

### 4. Show provider configuration

Open `.env.example` and show that provider credentials are environment-driven:

- YouTube API key or key pool
- TikTok Apify provider settings
- Instagram Apify provider settings
- scoring thresholds and competitor caps

Call out that users provide public handles only; platform credentials belong to the self-hoster.

### 5. Show report commands

Use these commands as the safe demo surface:

```bash
docker compose exec web python manage.py run_user_report <tg_user_id>
docker compose exec web python manage.py verify_live_platform_report --tiktok https://www.tiktok.com/@example --instagram https://www.instagram.com/example/
docker compose exec web python manage.py send_test_platform_report --tg-user-id <id> --tg-chat-id <id> --tiktok https://www.tiktok.com/@example --instagram https://www.instagram.com/example/
```

Use placeholders unless the demo environment has a dedicated test bot and test chat.

### 6. Show tests and maintainer automation

Run:

```bash
python manage.py check
pytest -q
python tools/maintainer_ai.py release-notes --changelog CHANGELOG.md
```

The maintainer AI tool is optional. Without `OPENAI_API_KEY`, it uses deterministic local fallback heuristics so the core project remains usable without OpenAI credentials.

## Closing line

Competitor Spy is a practical baseline for self-hosted content-intelligence bots: Telegram onboarding, provider adapters, Celery schedules, baseline-relative scoring, and admin-friendly operations in one repo.
