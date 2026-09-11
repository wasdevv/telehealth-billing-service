"""Turns a recorded webhook event into a change in our own ledger.

Every handler is independently idempotent. That is not belt-and-braces on top of the
unique constraint on ``(provider, event_id)`` — it is a different guarantee. The
constraint stops the same *event* being processed twice; these handlers stop the same
*outcome* being applied twice when two different events describe it (a redelivered
charge under a new event id, a reconciliation sweep, a manual replay of the dead-letter
queue). Reprocessing anything here must leave the database exactly as it was.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Invoice, Payment, WebhookEvent

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "charge.refunded",
        "charge.dispute.created",
    }
)


class UnknownInvoice(Exception):
    """The event refers to an invoice this service has never heard of."""


class WebhookProcessor:
    def __init__(self, event: WebhookEvent):
        self.event = event
        self.payload = event.payload or {}

    @property
    def data_object(self) -> dict:
        obj = self.payload.get("data", {}).get("object", {})
        return obj if isinstance(obj, dict) else {}

    def process(self) -> str:
        handler = {
            "payment_intent.succeeded": self.handle_payment_succeeded,
            "payment_intent.payment_failed": self.handle_payment_failed,
            "charge.refunded": self.handle_charge_refunded,
            "charge.dispute.created": self.handle_dispute_created,
        }.get(self.event.event_type)

        if handler is None:
            return WebhookEvent.Status.IGNORED

        handler()
        return WebhookEvent.Status.PROCESSED

    # --- locating the invoice -----------------------------------------------------
    def _locate_invoice(self, *, for_update: bool = True) -> Invoice:
        """Find the invoice this event is about, by metadata first and id second.

        ``metadata.external_ref`` is what we set when creating the PaymentIntent, so it is
        the authoritative link. The PaymentIntent id is the fallback for events that
        travelled a path where metadata was not copied.
        """
        obj = self.data_object
        external_ref = (obj.get("metadata") or {}).get("external_ref")

        queryset = Invoice.objects.select_for_update() if for_update else Invoice.objects
        if external_ref:
            invoice = queryset.filter(external_ref=external_ref).first()
            if invoice:
                return invoice

        intent_id = obj.get("payment_intent")
        if not intent_id and obj.get("object") == "payment_intent":
            intent_id = obj.get("id")
        if intent_id:
            invoice = queryset.filter(provider_payment_intent_id=intent_id).first()
            if invoice:
                return invoice

        raise UnknownInvoice(f"no invoice for event {self.event.event_id}")

    @staticmethod
    def _charge_id(obj: dict) -> str:
        """The provider's charge identifier, whichever shape the event arrived in."""
        if obj.get("object") == "charge":
            return obj.get("id", "")
        latest = obj.get("latest_charge")
        if isinstance(latest, dict):
            return latest.get("id", "")
        return latest or obj.get("id", "")

    # --- handlers -----------------------------------------------------------------
    def handle_payment_succeeded(self) -> None:
        obj = self.data_object
        charge_id = self._charge_id(obj)

        with transaction.atomic():
            # select_for_update: two events for the same invoice (a success and a refund
            # arriving together) would otherwise read the same row and one would overwrite
            # the other's status with stale data.
            invoice = self._locate_invoice()

            amount = int(obj.get("amount_received") or obj.get("amount") or invoice.amount_cents)

            if charge_id:
                try:
                    with transaction.atomic():
                        Payment.objects.create(
                            invoice=invoice,
                            provider_charge_id=charge_id,
                            amount_cents=amount,
                            currency=(obj.get("currency") or invoice.currency).lower(),
                            status=Payment.Status.SUCCEEDED,
                        )
                except IntegrityError:
                    # The unique constraint did its job: this charge is already recorded.
                    # Reprocessing must not create a second Payment, and here it does not.
                    logger.info("charge %s already recorded; not duplicating", charge_id)

            # Assigning the same terminal state again is a no-op by construction, so a
            # replay cannot move paid_at or flip a refunded invoice back to paid.
            if invoice.status != Invoice.Status.REFUNDED:
                invoice.status = Invoice.Status.PAID
                invoice.paid_at = invoice.paid_at or timezone.now()
                if not invoice.provider_payment_intent_id and obj.get("object") == "payment_intent":
                    invoice.provider_payment_intent_id = obj.get("id")
                invoice.save(update_fields=["status", "paid_at", "provider_payment_intent_id", "updated_at"])

    def handle_payment_failed(self) -> None:
        obj = self.data_object
        charge_id = self._charge_id(obj)
        error = (obj.get("last_payment_error") or {}).get("message", "")

        with transaction.atomic():
            invoice = self._locate_invoice()

            if charge_id:
                try:
                    with transaction.atomic():
                        Payment.objects.create(
                            invoice=invoice,
                            provider_charge_id=charge_id,
                            amount_cents=int(obj.get("amount") or invoice.amount_cents),
                            currency=(obj.get("currency") or invoice.currency).lower(),
                            status=Payment.Status.FAILED,
                            failure_message=error[:1000],
                        )
                except IntegrityError:
                    logger.info("charge %s already recorded; not duplicating", charge_id)

            # A failure arriving after a success — Stripe can deliver out of order — must
            # not un-pay an invoice. Only a non-settled invoice moves to failed.
            if not invoice.is_settled:
                invoice.status = Invoice.Status.FAILED
                invoice.save(update_fields=["status", "updated_at"])

    def handle_charge_refunded(self) -> None:
        obj = self.data_object
        charge_id = self._charge_id(obj)
        refunded = int(obj.get("amount_refunded") or 0)

        with transaction.atomic():
            invoice = self._locate_invoice()

            # The absolute refunded total from the provider, not a running sum of our own:
            # adding on every delivery is precisely what makes a redelivery over-refund.
            invoice.amount_refunded_cents = min(refunded, invoice.amount_cents)
            invoice.status = (
                Invoice.Status.REFUNDED
                if invoice.amount_refunded_cents >= invoice.amount_cents
                else invoice.status
            )
            invoice.save(update_fields=["amount_refunded_cents", "status", "updated_at"])

            if charge_id:
                Payment.objects.filter(invoice=invoice, provider_charge_id=charge_id).update(
                    status=Payment.Status.REFUNDED
                )

    def handle_dispute_created(self) -> None:
        with transaction.atomic():
            invoice = self._locate_invoice()
            # Recording the first dispute timestamp, not the latest: a redelivery must not
            # move the clock on something a human may be reading as "when did this start".
            if invoice.disputed_at is None:
                invoice.disputed_at = timezone.now()
                invoice.save(update_fields=["disputed_at", "updated_at"])
