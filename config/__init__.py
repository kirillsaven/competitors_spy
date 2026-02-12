try:
    from .celery import app as celery_app
except ModuleNotFoundError:
    # Allows running manage.py commands in minimal local environments where Celery isn't installed.
    celery_app = None  # type: ignore[assignment]

__all__ = ("celery_app",)
