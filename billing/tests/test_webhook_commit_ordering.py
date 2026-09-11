"""The fourth guarantee: queue *after* the commit, never inside the transaction.

These are the only tests that can tell the difference between `process_webhook_event.delay(...)`
and `transaction.on_commit(lambda: process_webhook_event.delay(...))`. Everything else in
the suite passes either way.
"""

import pytest
from django.urls import reverse

from billing.models import Invoice, Payment, WebhookEvent
from conftest import payment_intent_succeeded_event

pytestmark = pytest.mark.django_db


@pytest.fixture
def url() -> str:
    return reverse("billing:stripe-webhook")


def test_processing_is_deferred_to_after_the_commit(
    api_client, url, signed_webhook, invoice, django_capture_on_commit_callbacks
):
    """Nothing has been processed while the transaction is still open.

    A worker is a different process against the same database. Queue inside the
    transaction and it can pick the task up before this INSERT is visible — the task then
    fails on a row that is about to exist — or, if the transaction rolls back, it runs for
    an event that never happened.
    """
    body, headers = signed_webhook(payment_intent_succeeded_event())

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        response = api_client.post(url, data=body, **headers)

        # The response is already out and the event is already recorded...
        assert response.status_code == 200
        assert WebhookEvent.objects.get().status == WebhookEvent.Status.PENDING
        # ...but the invoice has not moved, because nothing has run yet.
        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.OPEN
        assert Payment.objects.count() == 0

    # Exactly one deferred callback: the task hand-off.
    assert len(callbacks) == 1


def test_the_deferred_callback_actually_processes_the_event(
    api_client, url, signed_webhook, invoice, django_capture_on_commit_callbacks
):
    body, headers = signed_webhook(payment_intent_succeeded_event())

    with django_capture_on_commit_callbacks(execute=True):
        api_client.post(url, data=body, **headers)

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert Payment.objects.get().provider_charge_id == "ch_test_42"
    assert WebhookEvent.objects.get().status == WebhookEvent.Status.PROCESSED


def test_an_ignored_event_queues_nothing(api_client, url, signed_webhook, django_capture_on_commit_callbacks):
    event = {
        "id": "evt_ignored_2",
        "object": "event",
        "type": "invoice.upcoming",
        "data": {"object": {"id": "in_1"}},
    }
    body, headers = signed_webhook(event)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        api_client.post(url, data=body, **headers)

    assert callbacks == []


def test_a_duplicate_queues_nothing(
    api_client, url, signed_webhook, invoice, django_capture_on_commit_callbacks
):
    body, headers = signed_webhook(payment_intent_succeeded_event())
    with django_capture_on_commit_callbacks(execute=True):
        api_client.post(url, data=body, **headers)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        second = api_client.post(url, data=body, **headers)

    assert second.json()["status"] == "duplicate"
    assert callbacks == []
