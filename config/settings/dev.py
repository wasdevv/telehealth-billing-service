from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Tasks run in the calling process, so `uv run manage.py runserver` alone is enough to
# try the service end to end without a worker.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True
