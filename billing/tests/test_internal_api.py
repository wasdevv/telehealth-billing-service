"""The contract telehealth-clinical-api depends on. Status codes here are load-bearing."""

import pytest
from django.urls import reverse

from billing.models import InternalRequestLog, Invoice

pytestmark = pytest.mark.django_db

PAYLOAD = {
    "external_ref": "appointment:99",
    "patient_email": "patient@example.com",
    "amount_cents": 15000,
    "currency": "usd",
    "description": "Telehealth consultation with Dr. Ana Reyes on 2026-09-23",
}


def test_health_needs_no_authentication(api_client):
    response = api_client.get(reverse("billing:health"))

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_invoice_create_requires_the_internal_token(api_client):
    response = api_client.post(reverse("billing:invoice-create"), PAYLOAD, format="json")

    assert response.status_code == 403
    assert Invoice.objects.count() == 0


def test_a_wrong_token_is_refused(api_client):
    response = api_client.post(
        reverse("billing:invoice-create"),
        PAYLOAD,
        format="json",
        HTTP_AUTHORIZATION="Token definitely-not-the-token",
    )

    assert response.status_code == 403


def test_a_bearer_prefix_is_not_accepted_for_the_internal_contract(api_client, settings):
    response = api_client.post(
        reverse("billing:invoice-create"),
        PAYLOAD,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {settings.INTERNAL_SERVICE_TOKEN}",
    )

    assert response.status_code == 403


def test_an_unset_internal_token_closes_the_door(api_client, settings):
    settings.INTERNAL_SERVICE_TOKEN = ""

    response = api_client.post(
        reverse("billing:invoice-create"), PAYLOAD, format="json", HTTP_AUTHORIZATION="Token "
    )

    assert response.status_code == 403


def test_first_create_returns_201(api_client, internal_headers):
    response = api_client.post(reverse("billing:invoice-create"), PAYLOAD, format="json", **internal_headers)

    assert response.status_code == 201
    invoice = Invoice.objects.get()
    assert invoice.external_ref == "appointment:99"
    assert invoice.amount_cents == 15000
    assert invoice.status == Invoice.Status.OPEN


def test_repeating_the_same_create_returns_200_and_no_second_invoice(api_client, internal_headers):
    """The clinical service retries after a timeout. That retry must not bill twice."""
    first = api_client.post(reverse("billing:invoice-create"), PAYLOAD, format="json", **internal_headers)
    second = api_client.post(reverse("billing:invoice-create"), PAYLOAD, format="json", **internal_headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert Invoice.objects.filter(external_ref="appointment:99").count() == 1
    assert first.json()["id"] == second.json()["id"]


def test_the_idempotency_key_header_is_recorded(api_client, internal_headers):
    api_client.post(
        reverse("billing:invoice-create"),
        PAYLOAD,
        format="json",
        HTTP_IDEMPOTENCY_KEY="a" * 64,
        **internal_headers,
    )

    assert InternalRequestLog.objects.get().idempotency_key == "a" * 64


def test_void_returns_404_for_an_unknown_reference(api_client, internal_headers):
    response = api_client.post(
        reverse("billing:invoice-void"),
        {"external_ref": "appointment:nope"},
        format="json",
        **internal_headers,
    )

    assert response.status_code == 404


def test_void_marks_the_invoice_void(api_client, internal_headers, invoice):
    response = api_client.post(
        reverse("billing:invoice-void"),
        {"external_ref": invoice.external_ref},
        format="json",
        **internal_headers,
    )

    invoice.refresh_from_db()
    assert response.status_code == 200
    assert invoice.status == Invoice.Status.VOID
    assert invoice.voided_at is not None


def test_voiding_twice_is_a_no_op(api_client, internal_headers, invoice):
    api_client.post(
        reverse("billing:invoice-void"),
        {"external_ref": invoice.external_ref},
        format="json",
        **internal_headers,
    )
    invoice.refresh_from_db()
    first_voided_at = invoice.voided_at

    response = api_client.post(
        reverse("billing:invoice-void"),
        {"external_ref": invoice.external_ref},
        format="json",
        **internal_headers,
    )

    invoice.refresh_from_db()
    assert response.status_code == 200
    assert invoice.voided_at == first_voided_at


def test_a_paid_invoice_is_not_voided_by_a_cancellation(api_client, internal_headers, invoice):
    """Money already moved. Refunding is a different decision with different accounting."""
    invoice.mark_paid()

    response = api_client.post(
        reverse("billing:invoice-void"),
        {"external_ref": invoice.external_ref},
        format="json",
        **internal_headers,
    )

    invoice.refresh_from_db()
    assert response.status_code == 200
    assert invoice.status == Invoice.Status.PAID


def test_reminder_returns_202_and_queues(api_client, internal_headers):
    response = api_client.post(
        reverse("notifications:reminder"),
        {
            "email": "patient@example.com",
            "phone": "",
            "starts_at": "2026-09-23T10:00:00Z",
            "doctor": "Dr. Ana Reyes",
        },
        format="json",
        **internal_headers,
    )

    assert response.status_code == 202


def test_reminder_requires_the_internal_token(api_client):
    response = api_client.post(
        reverse("notifications:reminder"),
        {"email": "a@b.com", "phone": "", "starts_at": "2026-09-23T10:00:00Z", "doctor": "X"},
        format="json",
    )

    assert response.status_code == 403


def test_an_invalid_amount_is_rejected(api_client, internal_headers):
    response = api_client.post(
        reverse("billing:invoice-create"), {**PAYLOAD, "amount_cents": 0}, format="json", **internal_headers
    )

    assert response.status_code == 400
    assert Invoice.objects.count() == 0
