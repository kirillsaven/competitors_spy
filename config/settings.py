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


SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-insecure-secret-key") or "dev-insecure-secret-key"
DEBUG = _env_bool("DJANGO_DEBUG", True)

allowed_hosts_raw = _env("DJANGO_ALLOWED_HOSTS", "*") or "*"
if allowed_hosts_raw.strip() == "*":
    ALLOWED_HOSTS = ["*"]
else:
    ALLOWED_HOSTS = [h.strip() for h in allowed_hosts_raw.split(",") if h.strip()]


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

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# App config (env-configurable)
DEFAULT_TIMEZONE = _env("DEFAULT_TIMEZONE", "Europe/Moscow") or "Europe/Moscow"
BASELINE_N = _env_int("BASELINE_N", 30)
BASELINE_WINDOW_DAYS = _env_int("BASELINE_WINDOW_DAYS", 30)
YT_RECENT_N_FOR_METRICS = _env_int("YT_RECENT_N_FOR_METRICS", 50)
MAX_COMPETITORS_YOUTUBE = _env_int("MAX_COMPETITORS_YOUTUBE", 20)
MIN_DELTA_VIEWS = _env_int("MIN_DELTA_VIEWS", 500)
MIN_VIEWS_END = _env_int("MIN_VIEWS_END", 1000)
YT_MAX_SEARCH_CALLS_PER_SETUP = _env_int("YT_MAX_SEARCH_CALLS_PER_SETUP", 3)
SCHEDULE_RUNNING_STALE_MINUTES = _env_int("SCHEDULE_RUNNING_STALE_MINUTES", 60)

# Integrations
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "") or ""
YOUTUBE_API_KEY = _env("YOUTUBE_API_KEY", "") or ""
GOOGLE_LLM_API_KEY = _env("GOOGLE_LLM_API_KEY", "") or ""
GOOGLE_LLM_MODEL = _env("GOOGLE_LLM_MODEL", "gemini-2.0-flash-lite") or "gemini-2.0-flash-lite"
GOOGLE_LLM_MAX_CALLS_PER_USER_PER_DAY = _env_int("GOOGLE_LLM_MAX_CALLS_PER_USER_PER_DAY", 10)

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
