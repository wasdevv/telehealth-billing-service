import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


class IsInternalService(BasePermission):
    """Authenticates telehealth-clinical-api by a shared token.

    The comparison uses ``hmac.compare_digest``, not ``==``. Python's ``==`` on strings
    returns as soon as it finds a differing byte, so the time it takes leaks how many
    leading characters were right. An attacker who can time responses recovers the token
    one character at a time — a few thousand requests instead of brute-forcing 2^256.
    ``compare_digest`` takes the same time whatever the inputs are.

    The token is also read from settings at call time rather than module import, so a
    deployment that rotates it does not need the module reloaded.
    """

    message = "Invalid or missing internal service token."

    def has_permission(self, request, view) -> bool:
        expected = settings.INTERNAL_SERVICE_TOKEN
        if not expected:
            # An unset token must close the door, not open it. Falling through to "no
            # token configured, so allow everything" is how an internal API ends up public.
            return False

        header = request.headers.get("Authorization", "")
        prefix, _, presented = header.partition(" ")
        if prefix != "Token" or not presented:
            return False

        return hmac.compare_digest(presented.strip(), expected)
