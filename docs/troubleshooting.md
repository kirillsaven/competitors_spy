# Troubleshooting

This guide focuses on safe diagnostics. Do not paste real tokens, `.env` files, Telegram IDs, private hosts, provider payloads, screenshots with private data, or production logs into public issues.

## Safe First Checks

Run these locally before opening an issue:

```bash
python manage.py check
python manage.py check --deploy --fail-level WARNING
docker compose config --quiet
```

To verify live TikTok and Instagram report sections without sending Telegram messages:

```bash
docker compose exec web python manage.py verify_live_platform_report \
  --tiktok https://www.tiktok.com/@example \
  --instagram https://www.instagram.com/example/
```

Use placeholder handles in public examples. Use your own private test profiles only in private local runs.

## Missing Provider Credentials

Symptoms:

- report section is empty;
- setup cannot resolve a profile;
- command output says a provider access token or API key is not set.

Check:

- `YOUTUBE_API_KEY` or `YOUTUBE_API_KEYS` for YouTube;
- `TIKTOK_PROVIDER=apify` and `TIKTOK_PROVIDER_ACCESS_TOKEN` for TikTok;
- `INSTAGRAM_PROVIDER=apify` and `INSTAGRAM_PROVIDER_ACCESS_TOKEN` for Instagram.

Keep credentials in `.env` or your secret manager. Never commit them and never paste them into issues.

## Provider Rate Limits, Billing, or Actor Errors

TikTok and Instagram collection depends on the configured provider. If a provider actor changes, is rate-limited, has billing disabled, or returns an error, Competitor Spy should fail visibly instead of silently inventing report items.

Check:

- provider dashboard status;
- provider actor permissions and billing;
- local command output from `verify_live_platform_report`;
- `JobRun` records in Django Admin.

When opening an issue, redact provider payloads and include only the platform, command, high-level error class, and app version or commit SHA.

## No Public Metrics Returned

Some public posts do not expose usable view counts, like counts, comments, or share metrics through the selected provider. Those items may be collected but filtered out of the report.

Check whether the provider response includes public metric fields for the platform and content type. Do not upload raw provider JSON unless it is fully synthetic.

## No Baseline Snapshots Yet

The report score is baseline-relative. New competitors may need collection history before the app can identify outliers.

Symptoms:

- first report has few or no items;
- diagnostics mention missing baseline snapshots;
- newly added competitors have no historical metric snapshots.

Run collection again after the configured schedule has had time to collect enough recent items.

## Thresholds Too High

Reports filter out weak candidates using thresholds such as:

- `MIN_DELTA_VIEWS`
- `MIN_VIEWS_END`
- `BASELINE_N`
- `BASELINE_WINDOW_DAYS`

If every item is below threshold, the section can be empty even when collection works.

Adjust thresholds in `.env` for your own deployment. Keep defaults conservative for public examples.

## All Items Filtered Below Score

An empty section can mean collection succeeded but all items were filtered because they were too old, already reported, below delta threshold, below view threshold, or matched report stopwords.

Use Django Admin to inspect:

- competitors;
- content items;
- metric snapshots;
- reports;
- job runs.

## Fake Non-Empty YouTube Keys in Tests

Do not set `YOUTUBE_API_KEY=test-youtube-key` for offline test runs. A non-empty fake key can trigger real YouTube API calls and fail with `API_KEY_INVALID`.

Use empty YouTube keys for CI-style offline tests:

```bash
YOUTUBE_API_KEY="" YOUTUBE_API_KEYS="" pytest -q --durations=20
```

On Windows PowerShell:

```powershell
$env:YOUTUBE_API_KEY=''
$env:YOUTUBE_API_KEYS=''
pytest -q --durations=20
```

## Telegram Messages

Use `verify_live_platform_report` for safe diagnostics when you do not want to send Telegram messages.

Only use `send_test_platform_report` with your own dedicated test bot and test chat:

```bash
docker compose exec web python manage.py send_test_platform_report \
  --tg-user-id YOUR_TELEGRAM_USER_ID \
  --tg-chat-id YOUR_TELEGRAM_CHAT_ID \
  --tiktok https://www.tiktok.com/@example \
  --instagram https://www.instagram.com/example/
```

Never include real Telegram IDs or message IDs in public issues.
