"""The recurring safety nets."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from billing.models import Invoice, Payment, WebhookEvent
from billing.tasks import (
    expire_stale_invoices,
    process_webhook_event,
    reconcile_with_stripe,
    retry_failed_webhook_events,
)
from conftest import payment_intent_succeeded_event

pytestmark = pytest.mark.django_db


def test_process_webhook_event_is_a_no_op_for_a_missing_row():
    assert process_webhook_event.run(999_999) == "missing"


def test_process_webhook_event_skips_an_already_processed_event(invoice):
    event = WebhookEvent.objects.create(
        provider="stripe",
        event_id="evt_done",
        event_type="payment_intent.succeeded",
        payload=payment_intent_succeeded_event(event_id="evt_done"),
        status=WebhookEvent.Status.PROCESSED,
    )

    assert process_webhook_event.run(event.id) == "already-processed"
    assert Payment.objects.count() == 0


def test_sweeper_requeues_stalled_events(invoice):
    WebhookEvent.objects.create(
        provider="stripe",
        event_id="evt_stalled",
        event_type="payment_intent.succeeded",
        payload=payment_intent_succeeded_event(event_id="evt_stalled"),
        status=WebhookEvent.Status.FAILED,
        attempts=1,
    )

    result = retry_failed_webhook_events.run()

    assert result == {"requeued": 1}
    # Eager mode ran it through for real, so the invoice is now correct.
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID


def test_sweeper_gives_up_on_an_event_past_its_attempt_ceiling(invoice):
    WebhookEvent.objects.create(
        provider="stripe",
        event_id="evt_hopeless",
        event_type="payment_intent.succeeded",
        payload={},
        status=WebhookEvent.Status.FAILED,
        attempts=99,
    )

    assert retry_failed_webhook_events.run() == {"requeued": 0}


def test_reconciliation_corrects_an_invoice_stripe_considers_paid(invoice):
    """The lost-webhook case: Stripe says succeeded, we still say open."""
    with mock.patch("billing.stripe_gateway.retrieve_payment_intent", return_value={"status": "succeeded"}):
        result = reconcile_with_stripe.run()

    invoice.refresh_from_db()
    assert result == {"checked": 1, "corrected": 1}
    assert invoice.status == Invoice.Status.PAID


def test_reconciliation_leaves_an_unpaid_invoice_alone(invoice):
    with mock.patch(
        "billing.stripe_gateway.retrieve_payment_intent", return_value={"status": "requires_payment_method"}
    ):
        result = reconcile_with_stripe.run()

    invoice.refresh_from_db()
    assert result == {"checked": 1, "corrected": 0}
    assert invoice.status == Invoice.Status.OPEN


def test_reconciliation_is_idempotent(invoice):
    with mock.patch("billing.stripe_gateway.retrieve_payment_intent", return_value={"status": "succeeded"}):
        reconcile_with_stripe.run()
        second = reconcile_with_stripe.run()

    # The invoice is settled now, so the second pass has nothing to look at.
    assert second["corrected"] == 0


def test_reconciliation_survives_an_unreachable_stripe(invoice):
    with mock.patch("billing.stripe_gateway.retrieve_payment_intent", return_value=None):
        result = reconcile_with_stripe.run()

    invoice.refresh_from_db()
    assert result == {"checked": 1, "corrected": 0}
    assert invoice.status == Invoice.Status.OPEN


def test_expire_stale_invoices_voids_old_untouched_ones(invoice):
    Invoice.objects.filter(pk=invoice.pk).update(created_at=timezone.now() - timedelta(days=45))

    assert expire_stale_invoices.run() == {"expired": 1}
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.VOID


def test_expire_stale_invoices_leaves_recent_ones_alone(invoice):
    assert expire_stale_invoices.run() == {"expired": 0}


def test_expire_stale_invoices_leaves_anything_with_a_payment_attempt(invoice):
    """An invoice somebody tried to pay is a person's problem, not a sweep target."""
    Invoice.objects.filter(pk=invoice.pk).update(created_at=timezone.now() - timedelta(days=45))
    Payment.objects.create(
        invoice=invoice,
        provider_charge_id="ch_attempted",
        amount_cents=15000,
        status=Payment.Status.FAILED,
    )

    assert expire_stale_invoices.run() == {"expired": 0}
