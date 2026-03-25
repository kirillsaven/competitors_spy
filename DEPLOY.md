# Deploy

Target server for the first real deployment: `YOUR_SERVER_IP`

## Bootstrap checklist
1. Confirm SSH access to `YOUR_SERVER_IP` with a user that can run Docker commands.
2. Install these server prerequisites:
   - Docker Engine
   - Docker Compose plugin (`docker compose version` must work)
   - Git
   - curl
3. Create the app directory, for example:

```bash
sudo mkdir -p YOUR_APP_DIR
sudo chown "$USER":"$USER" YOUR_APP_DIR
```

4. Point a DNS name at `YOUR_SERVER_IP`.
   A public certificate for HTTPS requires a real hostname. If DNS is not pointing at the VPS yet, HTTPS cannot be completed.
5. Configure GitHub read access for the private repo on the server.
   Without a deploy key, machine user, or PAT-backed clone, server-side `git clone`, update, and rollback commands will fail.
6. Clone the repo on the server:

```bash
git clone git@github.com:kirillsaven/competitors_spy.git YOUR_APP_DIR
cd YOUR_APP_DIR
```

7. Copy the production env template and fill all required values:

```bash
cp deploy/env.production.example .env
```

8. Obtain a certificate and private key for the chosen hostname on the server.
   The simplest baseline is host-level `certbot` with manually managed renewals:

```bash
sudo apt-get update
sudo apt-get install -y certbot
sudo certbot certonly --standalone -d app.example.com
```

9. Decide how traffic will reach the app.
   This repo now includes an internal production reverse proxy container with HTTPS termination.
   Open only `80/tcp` and `443/tcp` publicly.
   Do not expose `8000/tcp` publicly.

## Required production env vars
Minimum required values in `.env`:
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `PROXY_SERVER_NAME`
- `PROXY_TLS_CERT_PATH`
- `PROXY_TLS_KEY_PATH`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `REDIS_URL`
- `TELEGRAM_BOT_TOKEN`
- `YOUTUBE_API_KEY`

Optional provider envs:
- `TIKTOK_PROVIDER=apify` plus `TIKTOK_PROVIDER_ACCESS_TOKEN` when TikTok is enabled
- `INSTAGRAM_PROVIDER=apify` plus `INSTAGRAM_PROVIDER_ACCESS_TOKEN` when Instagram is enabled

Required hardening vars for the HTTPS production baseline:
- `DJANGO_TRUST_X_FORWARDED_PROTO=1`
- `DJANGO_USE_X_FORWARDED_HOST=1`
- `DJANGO_USE_X_FORWARDED_PORT=1`
- `DJANGO_SECURE_SSL_REDIRECT=1`
- `DJANGO_SESSION_COOKIE_SECURE=1`
- `DJANGO_CSRF_COOKIE_SECURE=1`
- `DJANGO_SECURE_HSTS_SECONDS=31536000`
- `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=1`
- `DJANGO_SECURE_HSTS_PRELOAD=1`

Generate a production-only Django secret before the first HTTPS deploy:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

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
- `deploy/nginx/default.conf.template` is missing
- `PROXY_SERVER_NAME`, `PROXY_TLS_CERT_PATH`, or `PROXY_TLS_KEY_PATH` are missing
- the configured TLS certificate or key file does not exist on the server
- the server checkout is dirty
- the requested git ref does not resolve

## Health verification commands
After each deploy:

```bash
cd YOUR_APP_DIR
bash scripts/prod-health.sh
```

`scripts/prod-health.sh` verifies the real HTTPS entrypoint through the reverse proxy at `https://$PROXY_SERVER_NAME/healthz/` using `--resolve` against `127.0.0.1`, then runs `python manage.py check --deploy --fail-level WARNING`.

If health checks fail or you need more context:

```bash
cd YOUR_APP_DIR
bash scripts/prod-logs.sh 200
```

This includes `proxy` logs as well as `web`, `bot`, `worker`, and `beat`.

## TLS renewal baseline
Manual renewal path:

```bash
cd YOUR_APP_DIR
docker compose -f docker-compose.prod.yml stop proxy
sudo certbot renew
docker compose -f docker-compose.prod.yml up -d proxy
```

Dry-run the renewal path before relying on it:

```bash
sudo certbot renew --dry-run
```

## Rollback baseline
1. Identify the previous good commit or tag.
2. Roll back by redeploying that exact ref:

```bash
cd YOUR_APP_DIR
bash scripts/prod-update.sh <previous-good-ref>
```

3. Re-run:

```bash
bash scripts/prod-health.sh
```

## Reverse proxy baseline
- Public entrypoints: `proxy` on ports `80` and `443`
- Port `80` redirects to HTTPS
- TLS terminates inside the `proxy` container using certificate files from the host
- Internal app port: `web:8000` on the Docker network only
- Current operational prerequisite: DNS and certificate issuance must exist before the secure baseline can pass

## First deployment record
Date: `2026-03-25`

Target host: `YOUR_SERVER_IP`

Deployed ref:
- `origin/main`
- resolved on server to commit `8d5c1c79b6c7f06842c87e361896a3e53cf92b8c`

Exact commands run:

```bash
ssh deploy@YOUR_SERVER_IP "docker stop PRIVATE_CONTAINER_PLACEHOLDER"
ssh deploy@YOUR_SERVER_IP "cd YOUR_APP_DIR && bash scripts/prod-update.sh origin/main"
ssh deploy@YOUR_SERVER_IP "cd YOUR_APP_DIR && bash scripts/prod-health.sh"
ssh deploy@YOUR_SERVER_IP "cd YOUR_APP_DIR && docker compose -f docker-compose.prod.yml exec web python manage.py migrate --check"
ssh deploy@YOUR_SERVER_IP "cd YOUR_APP_DIR && docker compose -f docker-compose.prod.yml exec web python manage.py send_test_platform_report --tg-user-id YOUR_TELEGRAM_USER_ID --tg-chat-id YOUR_TELEGRAM_USER_ID --tiktok nba --instagram nasa"
```

Observed results:
- Containers started successfully for `db`, `redis`, `web`, `proxy`, `bot`, `worker`, and `beat`
- Proxy health endpoint returned `{"status": "ok"}` from `http://YOUR_SERVER_IP/healthz/`
- `python manage.py migrate --check` succeeded, so no unapplied migrations remained after deploy
- Live Telegram smoke send succeeded with real provider data:
  - TikTok section count: `5`
  - Instagram section count: `3`
  - Telegram `message_id`: `251`

Final container status:
- `competitors_spy-db-1`: healthy
- `competitors_spy-redis-1`: healthy
- `competitors_spy-web-1`: healthy
- `competitors_spy-proxy-1`: healthy, bound to `0.0.0.0:80->80/tcp`
- `competitors_spy-bot-1`: running
- `competitors_spy-worker-1`: running
- `competitors_spy-beat-1`: running

Remaining operational gaps:
- Port `80` was occupied by unrelated container `PRIVATE_CONTAINER_PLACEHOLDER` before the first deploy. That conflict must stay resolved for future deploys.
- `bash scripts/prod-health.sh` still exits non-zero on this plain-HTTP baseline because `python manage.py check --deploy --fail-level WARNING` reports hardening warnings for missing TLS and secure-cookie settings.
- The current deployment is HTTP-only. HTTPS termination and the related secure Django settings are still pending.
- `DJANGO_SECRET_KEY` should be rotated to a strong production-only value before broader public exposure.
