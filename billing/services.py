"""Invoice use cases, kept out of the views.

Every function here is safe to call more than once with the same input — that is the
service's whole contract with telehealth-clinical-api, which retries on timeout.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Invoice
from .stripe_gateway import StripeUnavailable, cancel_payment_intent, create_payment_intent

logger = logging.getLogger(__name__)


def get_or_create_invoice(data: dict) -> tuple[Invoice, bool]:
    """Return (invoice, created).

    ``created`` drives the 201-versus-200 in the contract. The uniqueness of
    ``external_ref`` is enforced by the database, and the IntegrityError branch below is
    not defensive decoration: two concurrent retries from the clinical service both pass
    ``get_or_create``'s internal SELECT, and one of them loses the INSERT. Catching it and
    re-reading is what turns that race into a plain 200.
    """
    external_ref = data["external_ref"]

    try:
        with transaction.atomic():
            invoice, created = Invoice.objects.get_or_create(
                external_ref=external_ref,
                defaults={
                    "patient_email": data["patient_email"],
                    "amount_cents": data["amount_cents"],
                    "currency": data.get("currency", "usd"),
                    "description": data.get("description", ""),
                    "status": Invoice.Status.OPEN,
                },
            )
    except IntegrityError:
        return Invoice.objects.get(external_ref=external_ref), False

    if created:
        _attach_payment_intent(invoice)

    return invoice, created


def _attach_payment_intent(invoice: Invoice) -> None:
    """Best effort. An invoice without a PaymentIntent is recoverable; a lost one is not."""
    try:
        intent = create_payment_intent(invoice)
    except StripeUnavailable:
        return

    if not intent:
        return

    invoice.provider_payment_intent_id = intent["id"]
    invoice.provider_client_secret = intent.get("client_secret") or ""
    invoice.save(update_fields=["provider_payment_intent_id", "provider_client_secret", "updated_at"])


def void_invoice(external_ref: str) -> Invoice | None:
    """Void an invoice by its external reference, or return None if there is none.

    Returning None maps to the contract's 404, which the clinical service treats as
    success — nothing to void is the same end state as a successful void.
    """
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().filter(external_ref=external_ref).first()
        if invoice is None:
            return None

        if invoice.status == Invoice.Status.VOID:
            return invoice  # already done; saying so again costs nothing

        if invoice.status in {Invoice.Status.PAID, Invoice.Status.REFUNDED}:
            # A paid invoice is not voidable — money already moved. Refunding it is a
            # different decision with different accounting, and not one a cancellation
            # request from another service gets to make silently.
            logger.info("refusing to void %s in status %s", external_ref, invoice.status)
            return invoice

        invoice.status = Invoice.Status.VOID
        invoice.voided_at = timezone.now()
        invoice.save(update_fields=["status", "voided_at", "updated_at"])
        intent_id = invoice.provider_payment_intent_id

    if intent_id:
        # After the commit: cancelling at Stripe is an external effect that cannot be
        # rolled back, so it must not happen inside a transaction that might still abort.
        cancel_payment_intent(intent_id)

    return invoice
