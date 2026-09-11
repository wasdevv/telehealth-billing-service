"""The only module that talks to Stripe.

Keeping the SDK behind one seam means the rest of the service never has to care whether
Stripe is configured, and the tests never have to reach the network.
"""

import logging

import stripe
from django.conf import settings

logger = logging.getLogger(__name__)


class StripeUnavailable(Exception):
    """Stripe is not configured, or refused the call. Never fatal to an invoice."""


def _client() -> None:
    if not settings.STRIPE_SECRET_KEY:
        raise StripeUnavailable("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.STRIPE_SECRET_KEY


def create_payment_intent(invoice) -> dict | None:
    """Create the PaymentIntent for an invoice, or return None if Stripe is unavailable.

    The ``idempotency_key`` is derived from the invoice's ``external_ref``, which the
    clinical service derives from the appointment id. If this very request is retried —
    by us on a timeout, by a proxy, by a redeployed pod replaying a queue — Stripe
    recognises the key and returns the *existing* intent instead of charging twice.
    Without it, our own retry is the double-charge.
    """
    try:
        _client()
    except StripeUnavailable as exc:
        # A missing PSP must not cost the patient their appointment. The invoice stays
        # local and `reconcile_with_stripe` picks it up once Stripe is configured again.
        logger.warning("skipping PaymentIntent for %s: %s", invoice.external_ref, exc)
        return None

    try:
        intent = stripe.PaymentIntent.create(
            amount=invoice.amount_cents,
            currency=invoice.currency,
            description=invoice.description or f"Telehealth invoice {invoice.external_ref}",
            receipt_email=invoice.patient_email,
            metadata={"external_ref": invoice.external_ref},
            automatic_payment_methods={"enabled": True},
            idempotency_key=f"invoice:{invoice.external_ref}",
        )
    except stripe.StripeError as exc:
        logger.warning("stripe rejected PaymentIntent for %s: %s", invoice.external_ref, type(exc).__name__)
        raise StripeUnavailable(str(exc)) from exc

    return {"id": intent["id"], "client_secret": intent.get("client_secret")}


def cancel_payment_intent(payment_intent_id: str) -> bool:
    try:
        _client()
        stripe.PaymentIntent.cancel(payment_intent_id)
    except StripeUnavailable as exc:
        logger.warning("cannot cancel %s: %s", payment_intent_id, exc)
        return False
    except stripe.StripeError as exc:
        # Already cancelled or already captured: both mean "there is nothing left to
        # cancel", which is the state the caller wanted.
        logger.info("stripe declined cancel for %s: %s", payment_intent_id, type(exc).__name__)
        return False
    return True


def retrieve_payment_intent(payment_intent_id: str) -> dict | None:
    try:
        _client()
        return dict(stripe.PaymentIntent.retrieve(payment_intent_id))
    except (StripeUnavailable, stripe.StripeError) as exc:
        logger.warning("cannot retrieve %s: %s", payment_intent_id, type(exc).__name__)
        return None
