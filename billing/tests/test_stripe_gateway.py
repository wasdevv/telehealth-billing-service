"""Requirement 9: our own retry must not be the double-charge."""

from unittest import mock

import pytest
import stripe

from billing.models import Invoice
from billing.services import get_or_create_invoice
from billing.stripe_gateway import StripeUnavailable, cancel_payment_intent, create_payment_intent

pytestmark = pytest.mark.django_db


def test_payment_intent_carries_an_idempotency_key_derived_from_external_ref(invoice):
    with mock.patch("stripe.PaymentIntent.create") as create:
        create.return_value = {"id": "pi_new", "client_secret": "cs_new"}
        create_payment_intent(invoice)

    kwargs = create.call_args.kwargs
    # Derived, not random: the same invoice always produces the same key, so a retry of
    # this very call returns the existing intent instead of creating a second charge.
    assert kwargs["idempotency_key"] == f"invoice:{invoice.external_ref}"
    assert kwargs["amount"] == 15000
    assert kwargs["currency"] == "usd"
    assert kwargs["metadata"]["external_ref"] == invoice.external_ref


def test_the_key_is_stable_across_calls(invoice):
    keys = []
    with mock.patch("stripe.PaymentIntent.create") as create:
        create.return_value = {"id": "pi_new", "client_secret": "cs_new"}
        create_payment_intent(invoice)
        create_payment_intent(invoice)
        keys = [c.kwargs["idempotency_key"] for c in create.call_args_list]

    assert keys[0] == keys[1]


def test_an_unconfigured_stripe_skips_the_intent_instead_of_failing(invoice, settings):
    settings.STRIPE_SECRET_KEY = ""

    assert create_payment_intent(invoice) is None


def test_a_stripe_error_becomes_stripe_unavailable(invoice):
    with (
        mock.patch("stripe.PaymentIntent.create", side_effect=stripe.APIConnectionError("down")),
        pytest.raises(StripeUnavailable),
    ):
        create_payment_intent(invoice)


def test_invoice_creation_survives_stripe_being_down(db):
    """Losing the appointment because the PSP is down is worse than a missing intent."""
    with mock.patch("stripe.PaymentIntent.create", side_effect=stripe.APIConnectionError("down")):
        invoice, created = get_or_create_invoice(
            {
                "external_ref": "appointment:777",
                "patient_email": "patient@example.com",
                "amount_cents": 15000,
                "currency": "usd",
                "description": "",
            }
        )

    assert created is True
    assert invoice.status == Invoice.Status.OPEN
    assert invoice.provider_payment_intent_id is None


def test_cancel_returns_false_rather_than_raising_when_stripe_refuses():
    with mock.patch("stripe.PaymentIntent.cancel", side_effect=stripe.InvalidRequestError("gone", None)):
        assert cancel_payment_intent("pi_x") is False


def test_cancel_returns_false_when_stripe_is_unconfigured(settings):
    settings.STRIPE_SECRET_KEY = ""

    assert cancel_payment_intent("pi_x") is False
