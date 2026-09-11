"""Shared fixtures.

The only thing stubbed anywhere in this suite is the Stripe *network call*. Signature
verification, the database, the serializers and the processor all run for real, so a
passing test has exercised the code that runs in production.
"""

import hashlib
import hmac
import json
import time

import pytest
from django.conf import settings
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import Invoice


@pytest.fixture(autouse=True)
def reset_throttles():
    """DRF keeps throttle counters in the Django cache, which no test teardown clears.

    Without this, the eleventh login in a run — whichever test happens to make it — gets a
    429 and the suite becomes order-dependent. Clearing here keeps the throttles switched
    on in test settings, so the ones that matter stay covered by
    accounts/tests/test_throttling.py rather than being disabled and forgotten.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def internal_headers() -> dict:
    return {"HTTP_AUTHORIZATION": f"Token {settings.INTERNAL_SERVICE_TOKEN}"}


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(
        username="patient", email="patient@example.com", password="correct-horse-battery"
    )


@pytest.fixture
def invoice(db) -> Invoice:
    return Invoice.objects.create(
        external_ref="appointment:42",
        patient_email="patient@example.com",
        amount_cents=15000,
        currency="usd",
        description="Telehealth consultation",
        status=Invoice.Status.OPEN,
        provider_payment_intent_id="pi_test_42",
    )


def stripe_signature(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a real `Stripe-Signature` header for a test payload.

    Stripe signs the string ``{timestamp}.{raw body}`` with HMAC-SHA256 over the endpoint
    secret. Reproducing that here — rather than monkeypatching ``construct_event`` — means
    the tests go in through the same door an attacker would, so a test that passes proves
    the verification works, not that it was stubbed out.
    """
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


@pytest.fixture
def signed_webhook():
    """Return (body, headers) for a Stripe event, correctly signed."""

    def _build(event: dict, secret: str | None = None, timestamp: int | None = None):
        body = json.dumps(event).encode()
        header = stripe_signature(body, secret or settings.STRIPE_WEBHOOK_SECRET, timestamp)
        return body, {"HTTP_STRIPE_SIGNATURE": header, "content_type": "application/json"}

    return _build


def payment_intent_succeeded_event(
    *, event_id: str = "evt_test_1", external_ref: str = "appointment:42", amount: int = 15000
) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test_42",
                "object": "payment_intent",
                "amount": amount,
                "amount_received": amount,
                "currency": "usd",
                "latest_charge": "ch_test_42",
                "metadata": {"external_ref": external_ref},
            }
        },
    }


def payment_intent_failed_event(
    *, event_id: str = "evt_test_fail", external_ref: str = "appointment:42"
) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_test_42",
                "object": "payment_intent",
                "amount": 15000,
                "currency": "usd",
                "latest_charge": "ch_test_failed",
                "last_payment_error": {"message": "Your card was declined."},
                "metadata": {"external_ref": external_ref},
            }
        },
    }


def charge_refunded_event(
    *, event_id: str = "evt_test_refund", external_ref: str = "appointment:42", refunded: int = 15000
) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_test_42",
                "object": "charge",
                "payment_intent": "pi_test_42",
                "amount": 15000,
                "amount_refunded": refunded,
                "currency": "usd",
                "metadata": {"external_ref": external_ref},
            }
        },
    }


def dispute_created_event(
    *, event_id: str = "evt_test_dispute", external_ref: str = "appointment:42"
) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "dp_test_1",
                "object": "dispute",
                "charge": "ch_test_42",
                "payment_intent": "pi_test_42",
                "metadata": {"external_ref": external_ref},
            }
        },
    }
