import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("telehealth_billing")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# acks_late plus a prefetch of 1: a task is acknowledged only after it finishes, so a
# worker killed mid-run hands its job back to the queue instead of losing it. That is
# only safe because every task in this service is idempotent — the whole point of the
# unique constraints in billing/models.py.
app.conf.task_acks_late = True
app.conf.worker_prefetch_multiplier = 1
