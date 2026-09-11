"""The four guarantees of the webhook endpoint, each tested through the front door."""

import json

import pytest
from django.urls import reverse

from billing.models import WebhookEvent
from conftest import payment_intent_succeeded_event

pytestmark = pytest.mark.django_db


@pytest.fixture
def url() -> str:
    return reverse("billing:stripe-webhook")


def test_invalid_signature_is_rejected_and_nothing_is_persisted(api_client, url, signed_webhook):
    body, headers = signed_webhook(payment_intent_succeeded_event(), secret="whsec_the_wrong_secret")

    response = api_client.post(url, data=body, **headers)

    assert response.status_code == 400
    assert WebhookEvent.objects.count() == 0


def test_missing_signature_header_is_rejected(api_client, url):
    response = api_client.post(
        url, data=json.dumps(payment_intent_succeeded_event()), content_type="application/json"
    )

    assert response.status_code == 400
    assert WebhookEvent.objects.count() == 0


def test_forged_body_under_a_valid_signature_of_a_different_body_is_rejected(api_client, url, signed_webhook):
    """The signature covers the body. Swapping the body after signing must not pass."""
    _, headers = signed_webhook(payment_intent_succeeded_event())
    tampered = json.dumps(payment_intent_succeeded_event(amount=1)).encode()

    response = api_client.post(url, data=tampered, **headers)

    assert response.status_code == 400
    assert WebhookEvent.objects.count() == 0


def test_stale_timestamp_is_rejected(api_client, url, signed_webhook):
    """Stripe's tolerance window is what stops a captured event being replayed later."""
    body, headers = signed_webhook(payment_intent_succeeded_event(), timestamp=1_600_000_000)

    response = api_client.post(url, data=body, **headers)

    assert response.status_code == 400
    assert WebhookEvent.objects.count() == 0


def test_valid_signature_is_accepted_and_persisted(api_client, url, signed_webhook, invoice):
    body, headers = signed_webhook(payment_intent_succeeded_event())

    response = api_client.post(url, data=body, **headers)

    assert response.status_code == 200
    event = WebhookEvent.objects.get()
    assert event.event_id == "evt_test_1"
    assert event.event_type == "payment_intent.succeeded"
    assert event.provider == "stripe"


def test_redelivery_of_the_same_event_creates_no_second_row(api_client, url, signed_webhook, invoice):
    body, headers = signed_webhook(payment_intent_succeeded_event())

    first = api_client.post(url, data=body, **headers)
    second = api_client.post(url, data=body, **headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert WebhookEvent.objects.filter(event_id="evt_test_1").count() == 1


def test_unhandled_event_type_is_recorded_but_not_queued(api_client, url, signed_webhook):
    event = {
        "id": "evt_unhandled_1",
        "object": "event",
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_1"}},
    }
    body, headers = signed_webhook(event)

    response = api_client.post(url, data=body, **headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    recorded = WebhookEvent.objects.get(event_id="evt_unhandled_1")
    assert recorded.status == WebhookEvent.Status.IGNORED


def test_endpoint_refuses_when_no_webhook_secret_is_configured(api_client, url, signed_webhook, settings):
    body, headers = signed_webhook(payment_intent_succeeded_event())
    settings.STRIPE_WEBHOOK_SECRET = ""

    response = api_client.post(url, data=body, **headers)

    assert response.status_code == 400
    assert WebhookEvent.objects.count() == 0
