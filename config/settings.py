"""
Django settings for Competitor Content Tracker (MVP).

Keep this file boring and env-driven: Docker Compose is the default runtime.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return int(v)


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return float(v)


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    v = os.environ.get(name)
    if v is None:
        return list(default or [])
    return [item.strip() for item in v.split(",") if item.strip()]


SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-insecure-secret-key") or "dev-insecure-secret-key"
DEBUG = _env_bool("DJANGO_DEBUG", True)

if DEBUG:
    ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
else:
    ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
USE_X_FORWARDED_HOST = _env_bool("DJANGO_USE_X_FORWARDED_HOST", not DEBUG)
USE_X_FORWARDED_PORT = _env_bool("DJANGO_USE_X_FORWARDED_PORT", not DEBUG)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if _env_bool("DJANGO_TRUST_X_FORWARDED_PROTO", not DEBUG) else None


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'tracking',
    'botapp',
    'common',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# Docker uses DATABASE_URL; local dev without it falls back to sqlite.
DATABASE_URL = _env("DATABASE_URL")
if DATABASE_URL:
    try:
        import dj_database_url  # type: ignore

        DATABASES = {
            "default": dj_database_url.config(
                default=DATABASE_URL,
                conn_max_age=60,
                conn_health_checks=True,
            )
        }
    except Exception:
        # Last-resort fallback. Makes `manage.py` usable even if deps aren't installed locally.
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'ru'
TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# App config (env-configurable)
DEFAULT_TIMEZONE = _env("DEFAULT_TIMEZONE", "Europe/Moscow") or "Europe/Moscow"
BASELINE_N = _env_int("BASELINE_N", 30)
BASELINE_WINDOW_DAYS = _env_int("BASELINE_WINDOW_DAYS", 30)
YT_RECENT_N_FOR_METRICS = _env_int("YT_RECENT_N_FOR_METRICS", 50)
YT_UPLOADS_MAX_PAGES_SCAN = _env_int("YT_UPLOADS_MAX_PAGES_SCAN", 4)
YT_UPLOADS_MAX_UPLOADS_INSPECTED = _env_int("YT_UPLOADS_MAX_UPLOADS_INSPECTED", 200)
DISCOVERY_TARGET_COMPETITORS_PER_PLATFORM = _env_int(
    "DISCOVERY_TARGET_COMPETITORS_PER_PLATFORM",
    _env_int("MAX_COMPETITORS_PER_PLATFORM", _env_int("MAX_COMPETITORS_YOUTUBE", 20)),
)
MAX_COMPETITORS_PER_PLATFORM = DISCOVERY_TARGET_COMPETITORS_PER_PLATFORM
MAX_COMPETITORS_YOUTUBE = DISCOVERY_TARGET_COMPETITORS_PER_PLATFORM
MIN_DELTA_VIEWS = _env_int("MIN_DELTA_VIEWS", 500)
MIN_VIEWS_END = _env_int("MIN_VIEWS_END", 1000)
REPORT_MAX_ITEM_AGE_DAYS = _env_int("REPORT_MAX_ITEM_AGE_DAYS", 60)
REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM = _env_int("REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM", 2)
REPORT_FALLBACK_MAX_ITEMS_PER_PLATFORM = _env_int("REPORT_FALLBACK_MAX_ITEMS_PER_PLATFORM", 2)
REPORT_FALLBACK_MIN_ADAPTATION_SCORE = _env_float("REPORT_FALLBACK_MIN_ADAPTATION_SCORE", 0.45)
REPORT_FALLBACK_DELTA_RATIO = _env_float("REPORT_FALLBACK_DELTA_RATIO", 0.5)
ADAPTATION_RELEVANCE_WEIGHT = _env_float("ADAPTATION_RELEVANCE_WEIGHT", 2.0)
REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MAX_ITEMS = _env_int("REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MAX_ITEMS", 3)
REPORT_YOUTUBE_SUGGESTED_COMPETITORS_HISTORY_RUNS = _env_int("REPORT_YOUTUBE_SUGGESTED_COMPETITORS_HISTORY_RUNS", 5)
REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_APPEARANCES = _env_int(
    "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_APPEARANCES",
    2,
)
REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_AVERAGE_SCORE = _env_float(
    "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_AVERAGE_SCORE",
    0.45,
)
YT_MAX_SEARCH_CALLS_PER_SETUP = _env_int("YT_MAX_SEARCH_CALLS_PER_SETUP", 5)
ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION = _env_bool("ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION", True)
YT_SUPPLEMENTAL_MAX_QUERIES = _env_int("YT_SUPPLEMENTAL_MAX_QUERIES", 6)
YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY = _env_int("YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY", 5)
YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY = _env_int("YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY", 1)
YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS = _env_int("YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS", 20)
YT_SUPPLEMENTAL_MAX_AGE_DAYS = _env_int("YT_SUPPLEMENTAL_MAX_AGE_DAYS", 30)
YT_SUPPLEMENTAL_SHORTS_ONLY = _env_bool("YT_SUPPLEMENTAL_SHORTS_ONLY", True)
YT_SUPPLEMENTAL_MIN_VIEWS_BASE = _env_int("YT_SUPPLEMENTAL_MIN_VIEWS_BASE", 200)
YT_SUPPLEMENTAL_MIN_VIEWS_PER_DAY = _env_int("YT_SUPPLEMENTAL_MIN_VIEWS_PER_DAY", 120)
YT_SUPPLEMENTAL_MIN_VIEWS_CAP = _env_int("YT_SUPPLEMENTAL_MIN_VIEWS_CAP", 1200)
SETUP_RETRY_CACHE_TTL_SECONDS = _env_int("SETUP_RETRY_CACHE_TTL_SECONDS", 300)
COMPETITOR_SUGGEST_CACHE_TTL_SECONDS = _env_int("COMPETITOR_SUGGEST_CACHE_TTL_SECONDS", 900)
PICKER_SNAPSHOT_CACHE_TTL_SECONDS = _env_int("PICKER_SNAPSHOT_CACHE_TTL_SECONDS", 900)
YOUTUBE_SEARCH_CACHE_TTL_SECONDS = _env_int("YOUTUBE_SEARCH_CACHE_TTL_SECONDS", 21600)
YOUTUBE_SEED_SEARCH_CACHE_TTL_SECONDS = _env_int("YOUTUBE_SEED_SEARCH_CACHE_TTL_SECONDS", 21600)
YOUTUBE_RECENT_CACHE_TTL_SECONDS = _env_int("YOUTUBE_RECENT_CACHE_TTL_SECONDS", 3600)
SCHEDULE_RUNNING_STALE_MINUTES = _env_int("SCHEDULE_RUNNING_STALE_MINUTES", 60)
SCHEDULE_DUE_TOLERANCE_MINUTES = _env_int("SCHEDULE_DUE_TOLERANCE_MINUTES", 10)
SCHEDULE_RETRY_WINDOW_MINUTES = _env_int("SCHEDULE_RETRY_WINDOW_MINUTES", 10)
SCHEDULE_TASK_MAX_RETRIES = _env_int("SCHEDULE_TASK_MAX_RETRIES", 2)
SCHEDULE_TASK_RETRY_DELAY_SECONDS = _env_int("SCHEDULE_TASK_RETRY_DELAY_SECONDS", 90)

# Integrations
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "") or ""
YOUTUBE_API_KEY = _env("YOUTUBE_API_KEY", "") or ""
YOUTUBE_API_KEYS = _env_list("YOUTUBE_API_KEYS", [YOUTUBE_API_KEY] if YOUTUBE_API_KEY else [])
TIKTOK_PROVIDER = _env("TIKTOK_PROVIDER", "stub") or "stub"
TIKTOK_PROVIDER_BASE_URL = _env("TIKTOK_PROVIDER_BASE_URL", "") or ""
TIKTOK_PROVIDER_API_KEY = _env("TIKTOK_PROVIDER_API_KEY", "") or ""
TIKTOK_PROVIDER_API_SECRET = _env("TIKTOK_PROVIDER_API_SECRET", "") or ""
TIKTOK_PROVIDER_ACCESS_TOKEN = _env("TIKTOK_PROVIDER_ACCESS_TOKEN", "") or ""
TIKTOK_APIFY_PROFILE_ACTOR_ID = (
    _env("TIKTOK_APIFY_PROFILE_ACTOR_ID", "clockworks/tiktok-profile-scraper")
    or "clockworks/tiktok-profile-scraper"
)
TIKTOK_APIFY_SEARCH_ACTOR_ID = (
    _env("TIKTOK_APIFY_SEARCH_ACTOR_ID", "clockworks/tiktok-user-search-scraper")
    or "clockworks/tiktok-user-search-scraper"
)
TIKTOK_APIFY_RESULTS_PER_PROFILE = _env_int("TIKTOK_APIFY_RESULTS_PER_PROFILE", 10)
INSTAGRAM_PROVIDER = _env("INSTAGRAM_PROVIDER", "stub") or "stub"
INSTAGRAM_PROVIDER_BASE_URL = _env("INSTAGRAM_PROVIDER_BASE_URL", "") or ""
INSTAGRAM_PROVIDER_API_KEY = _env("INSTAGRAM_PROVIDER_API_KEY", "") or ""
INSTAGRAM_PROVIDER_API_SECRET = _env("INSTAGRAM_PROVIDER_API_SECRET", "") or ""
INSTAGRAM_PROVIDER_ACCESS_TOKEN = _env("INSTAGRAM_PROVIDER_ACCESS_TOKEN", "") or ""
INSTAGRAM_APIFY_PROFILE_ACTOR_ID = (
    _env("INSTAGRAM_APIFY_PROFILE_ACTOR_ID", "apify/instagram-profile-scraper")
    or "apify/instagram-profile-scraper"
)
INSTAGRAM_APIFY_SEARCH_ACTOR_ID = (
    _env("INSTAGRAM_APIFY_SEARCH_ACTOR_ID", "iron-crawler/instagram-search-users")
    or "iron-crawler/instagram-search-users"
)

# Redis / Celery
REDIS_URL = _env("REDIS_URL", "redis://redis:6379/0") or "redis://redis:6379/0"
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_TIME_LIMIT = _env_int("CELERY_TASK_TIME_LIMIT", 60 * 10)  # seconds
CELERY_TASK_SOFT_TIME_LIMIT = _env_int("CELERY_TASK_SOFT_TIME_LIMIT", 60 * 9)
CELERY_BEAT_SCHEDULE = {
    "tick_due_schedules": {
        "task": "tracking.tasks.tick_due_schedules",
        "schedule": timedelta(minutes=1),
    }
}

SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SECURE_HSTS_SECONDS = _env_int("DJANGO_SECURE_HSTS_SECONDS", 3600 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = _env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = _env_bool("DJANGO_SECURE_CONTENT_TYPE_NOSNIFF", True)
X_FRAME_OPTIONS = _env("DJANGO_X_FRAME_OPTIONS", "DENY") or "DENY"
SILENCED_SYSTEM_CHECKS = _env_list("DJANGO_SILENCED_SYSTEM_CHECKS")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "basic": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "basic",
        },
    },
    "loggers": {
        # Avoid leaking API keys/tokens in INFO-level request logs.
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "httpcore": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}
