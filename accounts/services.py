"""Google identity verification.

Verification goes to Google rather than trusting the client's decoded claims: an ID token
is only evidence if somebody checks its signature, and the tokeninfo endpoint is the
dependency-free way to do that.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
TIMEOUT_SECONDS = 5


class GoogleAuthError(Exception):
    pass


def verify_google_id_token(id_token: str) -> dict:
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise GoogleAuthError("Google sign-in is not configured on this deployment.")

    try:
        response = requests.get(TOKENINFO_URL, params={"id_token": id_token}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("google tokeninfo unreachable: %s", type(exc).__name__)
        raise GoogleAuthError("Could not reach Google to verify the token.") from exc

    if response.status_code != 200:
        raise GoogleAuthError("Google rejected that token.")

    claims = response.json()
    if not isinstance(claims, dict):
        raise GoogleAuthError("Google returned an unexpected response.")

    # A token issued for a different client is a valid Google token that says nothing
    # about *our* user. Checking the audience is what stops it being replayed here.
    if claims.get("aud") != settings.GOOGLE_OAUTH_CLIENT_ID:
        raise GoogleAuthError("That token was issued for a different application.")

    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("That Google account has no verified email.")

    if not claims.get("sub") or not claims.get("email"):
        raise GoogleAuthError("Google returned an incomplete profile.")

    return claims
