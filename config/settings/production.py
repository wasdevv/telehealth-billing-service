from .base import *

DEBUG = False

# Nothing here has a safe default. A deployment missing one of these should fail at boot,
# loudly, rather than run with a well-known signing key or an unauthenticated internal API.
_required = {
    "DJANGO_SECRET_KEY": SECRET_KEY,
    "INTERNAL_SERVICE_TOKEN": INTERNAL_SERVICE_TOKEN,
    "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
    "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
}
_missing = [name for name, value in _required.items() if not value or "insecure" in str(value)]
if _missing:
    raise RuntimeError(f"Missing required production settings: {', '.join(_missing)}")

# --- transport security -------------------------------------------------------------
# Everything below is off in development on purpose: SECURE_SSL_REDIRECT on a plain-HTTP
# laptop turns every request into a redirect loop.
# On by default, but overridable, because "is this request already on TLS?" depends on
# what sits in front of the process. Behind a terminating proxy that sets
# X-Forwarded-Proto, leave it on. On a private network with no terminator — the Compose
# stack, a service mesh doing mTLS itself — the redirect answers the clinical service's
# POST with a 301 it will not follow, and the integration silently stops working.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# Health probes arrive over plain HTTP from inside the network, and a load balancer reads
# a 301 as "not healthy". SECURE_REDIRECT_EXEMPT is Django's own name for this; patterns
# are matched against the path with the leading slash already stripped.
SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/$"]
SECURE_HSTS_SECONDS = 31_536_000  # one year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True

X_FRAME_OPTIONS = "DENY"

ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()]
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in production")

CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()
]
