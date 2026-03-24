# Deploy

## Required production env vars
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DATABASE_URL`
- `REDIS_URL`
- `TELEGRAM_BOT_TOKEN`
- `YOUTUBE_API_KEY`

Recommended hardening vars:
- `DJANGO_TRUST_X_FORWARDED_PROTO=1`
- `DJANGO_USE_X_FORWARDED_HOST=1`
- `DJANGO_USE_X_FORWARDED_PORT=1`
- `DJANGO_SECURE_SSL_REDIRECT=1`
- `DJANGO_SESSION_COOKIE_SECURE=1`
- `DJANGO_CSRF_COOKIE_SECURE=1`
- `DJANGO_SECURE_HSTS_SECONDS=3600`
- `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=1`
- `DJANGO_SECURE_HSTS_PRELOAD=1`

## Deploy commands
```bash
cp .env.example .env
```

Fill `.env` with production values, then:

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --build
```

## Validation commands
```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 web worker beat bot
curl http://localhost:8000/healthz/
docker compose -f docker-compose.prod.yml exec web python manage.py check --deploy
```

## Rollback baseline
1. Keep the previous image set and `.env` file available before each deploy.
2. If the new release is unhealthy, redeploy the previous revision:

```bash
git checkout <previous-good-commit>
docker compose -f docker-compose.prod.yml up -d --build
```

3. Re-run the validation commands and confirm `/healthz/` is healthy.
