# SPEC - Competitor Content Tracker (MVP YouTube)

## Summary
Telegram bot for tracking competitors and sending scheduled reports (RU messages).
User supplies a profile URL/handle (no social logins).
MVP: YouTube full; TikTok/Instagram stubs.

Key decisions:
- Collection strategy: incremental (minimize YouTube quota)
- Report period: since last run (last_run_at -> now)
- Timezone: optional via location -> IANA timezone; fallback manual input; default Europe/Moscow

## Architecture
Django project with apps:
- tracking: models, adapters, services, celery tasks
- botapp: aiogram handlers + FSM
- common: utils, logging, time helpers

Data flow:
1) onboarding writes SeedProfile + niche keywords + user schedule/timezone
2) discovery suggests YouTube competitor candidates (seed featured channels + limited keyword search)
3) user edits competitor list
4) collector stores ContentItems + MetricSnapshots (incremental)
5) scorer computes CompetitorBaseline + viral scores
6) reporter renders Telegram message and stores Report, sends to chat

## Data model (Django models)
Note: keep default Django auth User for admin login; create separate TgUser model.

- TgUser(tg_user_id unique, tg_chat_id, timezone_str, tz_source, created_at, updated_at, limits_json)
- SeedProfile(user, raw_input, detected_platform, canonical_url, niche_keywords jsonb, niche_source, status, created_at)
- Competitor(user, platform, external_id, handle, url, display_name, added_by, is_active, meta jsonb, created_at)
- ContentItem(competitor, platform, external_id, url, title, description, published_at, duration_seconds, meta jsonb, created_at)
- MetricSnapshot(content_item, captured_at, views, likes nullable, comments nullable, shares nullable, extra jsonb)
- CompetitorBaseline(competitor, computed_at, window_days, n_items, metrics jsonb)
- Schedule(user, is_enabled, times jsonb ["09:00"], next_run_at, last_run_at, created_at, updated_at)
- Report(user, period_start, period_end, created_at, sent_at, status, payload jsonb)
- JobRun(job_type, user nullable, status, started_at, finished_at, attempts, error text, payload jsonb)

Constraints/indexes:
- Unique: (user, platform, external_id) for Competitor
- Unique: (competitor, platform, external_id) for ContentItem
- Index MetricSnapshot(content_item, captured_at)

## YouTube adapter (MVP)
API: YouTube Data API v3 via HTTP client (timeouts, retries, backoff).

Resolve input -> channelId:
Accept:
- @handle
- https://youtube.com/@handle
- https://youtube.com/channel/<id>
- video URL (watch?v= / shorts/) -> resolve videoId -> channelId via videos.list
Fallback (limited): search.list(type=channel) if handle/custom URL cannot be resolved.

Channel info:
- channels.list(part=snippet,contentDetails,statistics) -> uploads playlist id

Seed-based competitor hints (cheap):
- channelSections.list(part=contentDetails,snippet) -> extract featured channels (contentDetails.channels[])

Discovery by niche keywords (limited, cached):
- search.list limited calls (e.g., 2-3): type=video and/or type=channel, q="kw1 kw2 kw3"
- Extract channelIds from results, fetch details via channels.list
- De-dup, filter seed itself, cap to K candidates (e.g., 20)

Collect channel videos (incremental):
- Store uploads_playlist_id in Competitor.meta
- playlistItems.list(maxResults=YT_RECENT_N_FOR_METRICS or BASELINE_N on full refresh) -> recent videoIds
- videos.list(part=snippet,statistics,contentDetails, id=batched up to 50)
- Create ContentItem on first sight
- Create MetricSnapshot each refresh run for selected recent items

Notes:
- Shorts are included automatically because they are present in the channel uploads feed.
- When Instagram adapter is implemented later, it must cover both Posts and Reels (store as meta/content type).

## Niche keyword inference (MVP)
If seed is YouTube and channel resolves:
- source text: channel title + description + last ~10 video titles
- tokenize (RU+EN), remove stopwords, keep tokens len>=3
- pick top N keywords by frequency
If weak/empty -> ask user to input/edit keywords manually.

## Viral scoring (MVP)
Period-based:
- period_start = schedule.last_run_at (if null: now-24h)
- period_end = now
For each candidate video:
- snapshot_end = latest snapshot at/near period_end (collected in this run)
- snapshot_start = latest snapshot <= period_start (often previous run snapshot); if missing -> skip delta-based scoring
- delta_views = views_end - views_start
- delta_hours = hours(snapshot_end - snapshot_start)
- view_velocity = delta_views / max(delta_hours, eps)
- er_end = (likes_end+comments_end)/views_end when available

Baseline per competitor:
- Use latest snapshot per each of last BASELINE_N videos within BASELINE_WINDOW_DAYS
- baseline_views_per_hour: median + IQR of (views / age_hours_at_snapshot)
- baseline_er: median + IQR of er_end

Score:
- z_vel = (view_velocity - median_vph)/max(iqr_vph, eps)
- z_er  = (er_end - median_er)/max(iqr_er, eps) if er present
- score = 0.75*z_vel + 0.25*z_er (if er missing: score=z_vel)

Filters:
- delta_views >= MIN_DELTA_VIEWS
- published within last 30 days
Select:
- Top 5 by score for YouTube section.

## Report payload / format
payload.sections = [youtube, tiktok, instagram]
Each section has items (max 5).
TikTok/Instagram empty in MVP (explicit stub text).

Telegram message (RU) contains:
- Header: period + timezone
- YouTube section: list items with title, competitor, published date, delta views, optional ER, link
- TikTok/Instagram: “MVP: пока не поддерживается”

## Celery tasks
- tick_due_schedules (every 5 min): find due schedules, enqueue run_user_report
- run_user_report(user_id):
  - compute period
  - collect fresh metrics (incremental)
  - compute baseline + scores
  - store Report, send Telegram message
  - update schedule next_run_at/last_run_at
  - write JobRun for observability

## Telegram bot commands (MVP)
/start, /setup, /status, /competitors, /schedule, /report, /help

FSM (updated):
WAIT_SEED_INPUT
-> CONFIRM_OR_EDIT_NICHE (if auto inferred)
-> WAIT_MANUAL_NICHE (if needed)
-> ASK_UPLOAD_COMPETITORS
-> WAIT_COMPETITOR_LIST
-> SHOW_AUTO_CANDIDATES (choose by numbers)
-> REVIEW_FINAL_COMPETITORS (optional removal)
-> ASK_REPORTS_PER_DAY (1/2)
-> WAIT_TIME_1 (and WAIT_TIME_2 if needed)
-> ASK_TIMEZONE_METHOD (location vs manual)
-> WAIT_LOCATION / WAIT_TZ_MANUAL
-> DONE

## Docker Compose
Services:
- db (postgres)
- redis
- web (django)
- bot (aiogram)
- worker (celery)
- beat (celery beat)

.env.example includes:
TELEGRAM_BOT_TOKEN, YOUTUBE_API_KEY, DATABASE_URL, REDIS_URL,
BASELINE_N, BASELINE_WINDOW_DAYS, YT_RECENT_N_FOR_METRICS,
MAX_COMPETITORS_YOUTUBE, MIN_DELTA_VIEWS, DEFAULT_TIMEZONE
