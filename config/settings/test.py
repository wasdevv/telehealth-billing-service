from .base import *

DEBUG = False
SECRET_KEY = "test-only-secret-key-not-used-anywhere-else"  # noqa: S105

# Fixed, worthless values so the suite exercises the real code paths instead of the
# "credential missing" branches. The graceful-degradation branches get their own tests.
INTERNAL_SERVICE_TOKEN = "test-internal-token"  # noqa: S105
STRIPE_SECRET_KEY = "sk_test_placeholder"  # noqa: S105
STRIPE_WEBHOOK_SECRET = "whsec_test_placeholder"  # noqa: S105

DATABASES["default"]["HOST"] = env("POSTGRES_HOST", "localhost")
DATABASES["default"]["PORT"] = env("POSTGRES_PORT", "5432")

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast, test-only
