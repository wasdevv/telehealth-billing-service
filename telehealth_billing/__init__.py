# Importing the Celery app here means `celery -A telehealth_billing` and Django both see
# the same configured instance, and @shared_task registers against it.
from .celery import app as celery_app

__all__ = ("celery_app",)
