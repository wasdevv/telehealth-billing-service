"""Throttling is a requirement, so it gets a test rather than being switched off."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_login_is_throttled_after_the_scoped_rate(api_client, settings):
    rate = int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth"].split("/")[0])
    url = reverse("accounts:login")
    body = {"email": "nobody@example.com", "password": "wrong-on-purpose"}

    statuses = [api_client.post(url, body, format="json").status_code for _ in range(rate + 2)]

    # Brute-forcing a password is exactly what this endpoint is for, from an attacker's
    # point of view. Everything past the ceiling must be refused outright.
    assert 429 in statuses
    assert statuses[:rate] == [400] * rate


def test_the_webhook_scope_is_far_more_generous_than_the_auth_one(settings):
    """Stripe retries hard during an incident; a tight limit there drops real events."""
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

    webhook = int(rates["webhook"].split("/")[0])
    auth = int(rates["auth"].split("/")[0])
    assert webhook > auth * 10
