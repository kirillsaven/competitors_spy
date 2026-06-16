# Deploy

This document is a generic deployment template for self-hosters. It is not a hosted service guarantee and should be adapted to your own infrastructure.

Use placeholders consistently:

- `YOUR_SERVER_IP`
- `YOUR_DOMAIN`
- `YOUR_TELEGRAM_USER_ID`
- `YOUR_TELEGRAM_CHAT_ID`
- `YOUR_APP_DIR`
- `YOUR_REPO_URL`

## Bootstrap checklist

1. Provision a Linux server that can run Docker.
2. Install prerequisites:
   - Docker Engine
   - Docker Compose plugin (`docker compose version` must work)
   - Git
   - curl
3. Create the app directory:

```bash
sudo mkdir -p YOUR_APP_DIR
sudo chown "$USER":"$USER" YOUR_APP_DIR
```

4. Configure repository read access on the server.
   Use a deploy key, machine user, or another secure Git access path.
5. Clone the repo:

```bash
git clone YOUR_REPO_URL YOUR_APP_DIR
cd YOUR_APP_DIR
```

6. Copy the production env template and fill required values:

```bash
cp deploy/env.production.example .env
```

7. Decide how traffic reaches the app.
   The production Compose file includes an internal Nginx reverse proxy. Open only the intended public reverse-proxy port. Do not expose Django's development server publicly.

## Required production env vars

Minimum required values in `.env`:

- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `REDIS_URL`
- `TELEGRAM_BOT_TOKEN`
- `YOUTUBE_API_KEY` or `YOUTUBE_API_KEYS`

Optional provider envs:

- `TIKTOK_PROVIDER=apify` plus `TIKTOK_PROVIDER_ACCESS_TOKEN`
- `INSTAGRAM_PROVIDER=apify` plus `INSTAGRAM_PROVIDER_ACCESS_TOKEN`

Recommended hardening vars for HTTPS behind a reverse proxy:

- `DJANGO_TRUST_X_FORWARDED_PROTO=1`
- `DJANGO_USE_X_FORWARDED_HOST=1`
- `DJANGO_USE_X_FORWARDED_PORT=1`
- `DJANGO_SECURE_SSL_REDIRECT=1`
- `DJANGO_SESSION_COOKIE_SECURE=1`
- `DJANGO_CSRF_COOKIE_SECURE=1`
- `DJANGO_SECURE_HSTS_SECONDS=3600`
- `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=1`
- `DJANGO_SECURE_HSTS_PRELOAD=1`

Do not silence Django security warnings unless you understand the exact risk. Use a strong production-only `DJANGO_SECRET_KEY`.

## HTTPS recommendation

Use HTTPS for production. Common options include:

- host-level Nginx or Caddy with managed certificates
- a managed load balancer or reverse proxy
- Cloudflare Tunnel or another explicitly configured edge proxy

Set `DJANGO_CSRF_TRUSTED_ORIGINS` to your public HTTPS origin, for example:

```env
DJANGO_ALLOWED_HOSTS=YOUR_DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=https://YOUR_DOMAIN
```

Never expose `python manage.py runserver` to the public internet.

## Production start and update commands

First start from the server checkout:

```bash
cd YOUR_APP_DIR
bash scripts/prod-update.sh origin/main
```

Routine update:

```bash
cd YOUR_APP_DIR
bash scripts/prod-update.sh origin/main
```

Deploy a specific commit or tag:

```bash
cd YOUR_APP_DIR
bash scripts/prod-update.sh <git-ref>
```

`scripts/prod-update.sh` fails fast if:

- required commands are missing
- `.env` is missing
- `deploy/nginx/default.conf` is missing
- the server checkout is dirty
- the requested git ref does not resolve

## Health check checklist

After each deploy:

```bash
cd YOUR_APP_DIR
bash scripts/prod-health.sh
```

If health checks fail or you need more context:

```bash
cd YOUR_APP_DIR
bash scripts/prod-logs.sh 200
```

Check:

- containers are running and healthy
- `http://127.0.0.1/healthz/` works on the server
- `python manage.py check --deploy --fail-level WARNING` passes or produces only reviewed warnings
- migrations are applied
- the bot, worker, and beat services are running

## Smoke test template

Use your own Telegram IDs and test profiles only:

```bash
cd YOUR_APP_DIR
docker compose -f docker-compose.prod.yml exec web python manage.py send_test_platform_report \
  --tg-user-id YOUR_TELEGRAM_USER_ID \
  --tg-chat-id YOUR_TELEGRAM_CHAT_ID \
  --tiktok https://www.tiktok.com/@example \
  --instagram https://www.instagram.com/example/
```

Do not publish real Telegram IDs or message IDs in issues, docs, or screenshots.

## Production checklist

- `.env` exists on the server and is not committed.
- Tokens and API keys were generated specifically for this deployment.
- `DJANGO_DEBUG=0`.
- `DJANGO_ALLOWED_HOSTS` contains only your public hostnames/IPs.
- HTTPS is configured before real users rely on the deployment.
- Secure cookie and HSTS settings match your HTTPS setup.
- Database and Redis volumes are backed up.
- Logs do not expose tokens or private user data.
- Admin access is restricted to trusted operators.
- Provider API quotas and billing limits are understood.

## Rollback checklist

1. Identify the previous good commit or tag.
2. Redeploy that exact ref:

```bash
cd YOUR_APP_DIR
bash scripts/prod-update.sh <previous-good-ref>
```

3. Re-run health checks:

```bash
bash scripts/prod-health.sh
```

4. Confirm worker, beat, bot, and web services are healthy.
5. Run a smoke check if the change affected report generation or Telegram delivery.

## Reverse proxy baseline

- Public entrypoint: `proxy` on port `80` in `docker-compose.prod.yml`
- Internal app port: `web:8000` on the Docker network only
- Recommended production path: put HTTPS termination in front of the proxy or adapt the Nginx config for TLS

Update `deploy/nginx/default.conf` with `YOUR_DOMAIN` or your intended server name before production use.

## Token rotation

Rotate credentials before public launch if they were ever stored in a private repo, shared in chat, used in local experiments, or pasted into logs. This includes:

- `DJANGO_SECRET_KEY`
- `TELEGRAM_BOT_TOKEN`
- YouTube API keys
- Apify tokens
- database passwords
- Redis passwords if configured
