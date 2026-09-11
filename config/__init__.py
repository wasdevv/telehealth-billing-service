# Django imports this package at startup, which is the hook that makes the Celery app
# exist before any @shared_task is registered. Without it, shared_task binds to Celery's
# unconfigured default app and every .delay() goes to amqp://localhost instead of Redis —
# a failure that only shows up at runtime, in production, as a connection refused.
from telehealth_billing.celery import app as celery_app

__all__ = ("celery_app",)
