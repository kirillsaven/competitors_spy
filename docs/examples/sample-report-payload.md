# Sample Report Payload

This is a deterministic, fully synthetic example. It is not live provider output and does not contain real Telegram IDs, real handles, private links, API responses, or provider payloads.

Use it to understand the report shape that a self-hosted instance can generate after public metrics are collected and baseline snapshots exist.

## Rendered Telegram Message Example

```text
Отчет за 2026-07-01 09:00-21:00 (Europe/Moscow)

YouTube
1. @example_channel - "Как собрать контент-план за 30 минут"
   Просмотры: 18 400 (+2 900 за 6 ч)
   Почему попало в отчет: выше обычного темпа канала в 3.1x
   https://www.youtube.com/watch?v=example001

2. @another_example - "Shorts: 5 ошибок в запуске рекламы"
   Просмотры: 9 800 (+1 250 за 4 ч)
   Почему попало в отчет: быстрый прирост относительно baseline
   https://www.youtube.com/shorts/example002

TikTok
1. @example_tiktok - "3 hooks for a product demo"
   Просмотры: 42 000 (+8 500 за 5 ч)
   Почему попало в отчет: выше обычного темпа профиля в 4.4x
   https://www.tiktok.com/@example_tiktok/video/7000000000000000001

Instagram
Нет подходящих постов за период.
Диагностика: provider вернул публичные посты, но все они ниже порогов MIN_DELTA_VIEWS или MIN_VIEWS_END.
```

## Structured Payload Example

```json
{
  "report_kind": "scheduled",
  "telegram": {
    "tg_user_id": "YOUR_TELEGRAM_USER_ID",
    "tg_chat_id": "YOUR_TELEGRAM_CHAT_ID"
  },
  "period": {
    "start": "2026-07-01T06:00:00Z",
    "end": "2026-07-01T18:00:00Z",
    "timezone": "Europe/Moscow"
  },
  "sections": [
    {
      "platform": "youtube",
      "items": [
        {
          "competitor_handle": "@example_channel",
          "title": "Как собрать контент-план за 30 минут",
          "url": "https://www.youtube.com/watch?v=example001",
          "published_at": "2026-07-01T10:15:00Z",
          "views_end": 18400,
          "delta_views": 2900,
          "delta_hours": 6.0,
          "score": 3.1,
          "reason": "above_baseline_velocity"
        },
        {
          "competitor_handle": "@another_example",
          "title": "Shorts: 5 ошибок в запуске рекламы",
          "url": "https://www.youtube.com/shorts/example002",
          "published_at": "2026-07-01T12:30:00Z",
          "views_end": 9800,
          "delta_views": 1250,
          "delta_hours": 4.0,
          "score": 2.4,
          "reason": "fast_delta_growth"
        }
      ],
      "diagnostics": {
        "active_competitors": 8,
        "refreshed_competitors": 8,
        "baseline_snapshots_available": true,
        "filtered_items": {
          "below_delta_threshold": 11,
          "already_reported": 2,
          "stopwords": 1
        }
      }
    },
    {
      "platform": "tiktok",
      "items": [
        {
          "competitor_handle": "@example_tiktok",
          "title": "3 hooks for a product demo",
          "url": "https://www.tiktok.com/@example_tiktok/video/7000000000000000001",
          "published_at": "2026-07-01T11:45:00Z",
          "views_end": 42000,
          "delta_views": 8500,
          "delta_hours": 5.0,
          "score": 4.4,
          "reason": "above_profile_baseline"
        }
      ],
      "diagnostics": {
        "active_competitors": 4,
        "provider": "apify",
        "provider_errors": []
      }
    },
    {
      "platform": "instagram",
      "items": [],
      "diagnostics": {
        "active_competitors": 5,
        "provider": "apify",
        "empty_reason": "all_filtered_by_scoring",
        "message": "Provider returned public posts, but all candidate items were below configured report thresholds."
      }
    }
  ]
}
```

## Empty Section Diagnostic Examples

```json
[
  {
    "platform": "youtube",
    "empty_reason": "no_baseline_snapshots",
    "message": "Collection ran, but the competitor does not have enough historical snapshots yet."
  },
  {
    "platform": "tiktok",
    "empty_reason": "provider_not_configured",
    "message": "TIKTOK_PROVIDER_ACCESS_TOKEN is not set."
  },
  {
    "platform": "instagram",
    "empty_reason": "provider_rate_limited",
    "message": "Provider returned a rate-limit or billing error. Check provider dashboard and retry later."
  }
]
```

Do not paste real report payloads, Telegram IDs, provider responses, private URLs, or `.env` values into public issues.
