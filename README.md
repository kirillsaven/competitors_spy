# Competitor Content Tracker Bot (MVP: YouTube)

## Local run (Docker)
Все команды ниже выполняй из папки, где лежит `docker-compose.yml`.

1) Create `.env` from `.env.example` (skip if you already have `.env`) and fill:
- `TELEGRAM_BOT_TOKEN`
- `YOUTUBE_API_KEY`
- (optional) `GOOGLE_LLM_API_KEY` (Gemini, used only for internal niche inference during setup)

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
curl http://localhost:8000/healthz/
```

The production override switches Django to Gunicorn, collects static files, runs migrations on web startup, and enables restart/healthcheck defaults for all services.

## CI
GitHub Actions runs `python manage.py check`, `python manage.py check --deploy --fail-level WARNING`, and `pytest -q` on pushes to `main` and on pull requests targeting `main`.
