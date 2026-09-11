"""Idempotency of each handler, which is a separate guarantee from event de-duplication."""

import pytest

from billing.models import Invoice, Payment, WebhookEvent
from billing.processor import UnknownInvoice, WebhookProcessor
from conftest import (
    charge_refunded_event,
    dispute_created_event,
    payment_intent_failed_event,
    payment_intent_succeeded_event,
)

pytestmark = pytest.mark.django_db


def record(event: dict) -> WebhookEvent:
    return WebhookEvent.objects.create(
        provider="stripe", event_id=event["id"], event_type=event["type"], payload=event
    )


def test_success_marks_the_invoice_paid_and_records_the_payment(invoice):
    WebhookProcessor(record(payment_intent_succeeded_event())).process()

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert invoice.paid_at is not None
    payment = Payment.objects.get()
    assert payment.provider_charge_id == "ch_test_42"
    assert payment.amount_cents == 15000
    assert payment.status == Payment.Status.SUCCEEDED


def test_reprocessing_the_same_success_creates_no_second_payment(invoice):
    event = record(payment_intent_succeeded_event())

    WebhookProcessor(event).process()
    invoice.refresh_from_db()
    first_paid_at = invoice.paid_at

    WebhookProcessor(event).process()

    invoice.refresh_from_db()
    assert Payment.objects.count() == 1
    assert invoice.status == Invoice.Status.PAID
    # The timestamp must not move either: a replay is not a second payment.
    assert invoice.paid_at == first_paid_at


def test_a_second_event_id_for_the_same_charge_still_creates_no_duplicate(invoice):
    """De-duplication by event id would not catch this; the charge unique constraint does."""
    WebhookProcessor(record(payment_intent_succeeded_event(event_id="evt_a"))).process()
    WebhookProcessor(record(payment_intent_succeeded_event(event_id="evt_b"))).process()

    assert Payment.objects.count() == 1


def test_failure_records_the_reason_and_marks_the_invoice_failed(invoice):
    WebhookProcessor(record(payment_intent_failed_event())).process()

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.FAILED
    assert Payment.objects.get(status=Payment.Status.FAILED).failure_message == "Your card was declined."


def test_a_late_failure_cannot_unpay_a_paid_invoice(invoice):
    """Stripe can deliver out of order. A paid invoice must stay paid."""
    WebhookProcessor(record(payment_intent_succeeded_event())).process()
    WebhookProcessor(record(payment_intent_failed_event())).process()

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID


def test_full_refund_moves_the_invoice_to_refunded(invoice):
    WebhookProcessor(record(payment_intent_succeeded_event())).process()
    WebhookProcessor(record(charge_refunded_event())).process()

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.REFUNDED
    assert invoice.amount_refunded_cents == 15000
    assert Payment.objects.get().status == Payment.Status.REFUNDED


def test_redelivered_refund_does_not_accumulate(invoice):
    """The absolute total from the provider, never a running sum of our own."""
    WebhookProcessor(record(payment_intent_succeeded_event())).process()
    event = record(charge_refunded_event(refunded=5000))

    WebhookProcessor(event).process()
    WebhookProcessor(event).process()
    WebhookProcessor(event).process()

    invoice.refresh_from_db()
    assert invoice.amount_refunded_cents == 5000
    assert invoice.status == Invoice.Status.PAID  # partial refund is not a full one


def test_partial_then_full_refund_reaches_refunded(invoice):
    WebhookProcessor(record(payment_intent_succeeded_event())).process()
    WebhookProcessor(record(charge_refunded_event(event_id="evt_r1", refunded=5000))).process()
    WebhookProcessor(record(charge_refunded_event(event_id="evt_r2", refunded=15000))).process()

    invoice.refresh_from_db()
    assert invoice.amount_refunded_cents == 15000
    assert invoice.status == Invoice.Status.REFUNDED


def test_dispute_records_the_first_timestamp_only(invoice):
    event = record(dispute_created_event())

    WebhookProcessor(event).process()
    invoice.refresh_from_db()
    first = invoice.disputed_at

    WebhookProcessor(event).process()
    invoice.refresh_from_db()

    assert invoice.disputed_at == first


def test_an_event_for_an_unknown_invoice_raises_rather_than_guessing(db):
    event = record(payment_intent_succeeded_event(external_ref="appointment:does-not-exist"))

    with pytest.raises(UnknownInvoice):
        WebhookProcessor(event).process()


def test_invoice_is_found_by_payment_intent_when_metadata_is_absent(invoice):
    event = payment_intent_succeeded_event()
    del event["data"]["object"]["metadata"]

    WebhookProcessor(record(event)).process()

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
