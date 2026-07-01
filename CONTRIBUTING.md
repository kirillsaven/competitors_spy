# Contributing

Thanks for considering a contribution to Competitor Spy.

## Local setup

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py check
pytest -q
```

The project targets Python 3.12.

Optional shortcuts:

```bash
make check
make test
make docker-build
```

## Docker setup

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web pytest
```

Equivalent shortcuts:

```bash
make up
make migrate
make demo-check
```

## Running migrations

Use Django migrations only:

```bash
python manage.py makemigrations
python manage.py migrate
```

Do not edit the database manually as a substitute for migrations.

## Running tests

```bash
python manage.py check
python manage.py check --deploy --fail-level WARNING
pytest -q
```

Tests must not require real Telegram, YouTube, Apify, TikTok, Instagram, or OpenAI credentials.

## Code style expectations

- Keep code modular: adapters, services, Celery tasks, and bot handlers should stay separated.
- Prefer fail-fast behavior with explicit errors.
- Do not silently substitute stub, default, or empty results unless the caller explicitly requested fallback behavior.
- Keep Telegram UI messages in Russian unless the product requirement changes.
- Add focused tests for parsing, time handling, scoring, report generation, and Telegram flows when behavior changes.

## Adding a provider

1. Add or extend an adapter under `tracking/adapters/`.
2. Keep provider credentials env-driven.
3. Add provider config parsing in `tracking/services/provider_config.py` when needed.
4. Route collection through service-layer code instead of calling providers from bot handlers.
5. Persist shared snapshots through existing models.
6. Add tests for successful collection, missing credentials, provider errors, and empty provider responses.
7. Document required env vars in `README.md` and `.env.example`.

## Testing Telegram flows

- Prefer handler-level tests with mocked Telegram API objects.
- Do not send real Telegram messages from tests.
- Assert user-visible Russian text and callback state transitions.
- Cover duplicate clicks, stale callbacks, and setup retry behavior when relevant.

## Testing report generation

- Use deterministic model objects and metric snapshots.
- Test strict-path scoring and fallback-path scoring separately.
- Assert payload diagnostics when report sections are empty.
- Avoid using live provider data in unit tests.

## Opening a PR

1. Keep PR scope narrow.
2. Include a short summary and test results.
3. Call out any intentional fallback behavior.
4. Call out any security or deployment implications.
5. Do not include secrets, private server details, raw Telegram IDs, or private smoke-test records.
6. Use [SUPPORT.md](./SUPPORT.md) for public support boundaries and safe troubleshooting guidance.

## PR checklist

- [ ] Tests pass locally or failures are explained.
- [ ] `python manage.py check` passes.
- [ ] New behavior has focused tests.
- [ ] Documentation and `.env.example` are updated when configuration changes.
- [ ] No `.env`, logs, dumps, archives, credentials, or private operational details are committed.
- [ ] Provider changes do not require social-network logins from users.
