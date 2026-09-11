"""Invoices, payments and the record of every webhook this service has seen.

Idempotency here is a *database* property, not a code convention. Every "have we already
done this?" question is answered by a unique constraint, because a check-then-act in
Python reads a snapshot taken before the competing transaction committed — which is
exactly the race that double-charges a patient.
"""

from django.db import models
from django.utils import timezone


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        VOID = "void", "Void"
        REFUNDED = "refunded", "Refunded"

    # The idempotency key, handed to us by telehealth-clinical-api as "appointment:<id>".
    # Unique is what makes a retried create return the existing invoice instead of a second
    # one, no matter how many times the Rails side retries after a timeout.
    external_ref = models.CharField(max_length=255, unique=True)

    patient_email = models.EmailField()
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="usd")
    description = models.TextField(blank=True, default="")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    # Provider-side identifiers. Nullable because an invoice exists locally even when
    # Stripe is unreachable — losing the appointment because the PSP is down is worse.
    provider = models.CharField(max_length=32, default="stripe")
    # NULL rather than "" on purpose: the column is unique, and a second invoice without
    # a PaymentIntent yet would collide with the first if they both stored "".
    provider_payment_intent_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    provider_client_secret = models.CharField(max_length=255, blank=True, default="")

    amount_refunded_cents = models.PositiveIntegerField(default=0)
    disputed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_invoice"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["patient_email"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="billing_invoice_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(amount_refunded_cents__lte=models.F("amount_cents")),
                name="billing_invoice_refund_within_amount",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.external_ref} ({self.status})"

    @property
    def is_settled(self) -> bool:
        """States no webhook should move an invoice out of."""
        return self.status in {self.Status.PAID, self.Status.VOID, self.Status.REFUNDED}

    def mark_paid(self) -> None:
        self.status = self.Status.PAID
        self.paid_at = self.paid_at or timezone.now()
        self.save(update_fields=["status", "paid_at", "updated_at"])


class Payment(models.Model):
    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")

    # The provider's own id for the charge. Unique is the reason reprocessing the same
    # webhook cannot create a second Payment row: the second INSERT simply loses.
    provider_charge_id = models.CharField(max_length=255, unique=True)

    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="usd")
    status = models.CharField(max_length=16, choices=Status.choices)
    failure_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_payment"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["invoice", "status"])]

    def __str__(self) -> str:
        return f"{self.provider_charge_id} ({self.status})"


class WebhookEvent(models.Model):
    """Every event the provider has delivered, handled or not.

    Rows are written before any business logic runs, so an event that crashes the
    processor is still on record and still retryable. Unhandled types are recorded too:
    "Stripe has been sending us X for a month and nobody noticed" should be answerable.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    provider = models.CharField(max_length=32, default="stripe")
    # The provider's event id. Together with `provider` it is unique, which is what turns
    # a redelivery — Stripe retries for days — into a no-op instead of a double refund.
    event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100)

    payload = models.JSONField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "billing_webhook_event"
        ordering = ("-received_at",)
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="billing_webhook_event_unique")
        ]
        indexes = [
            models.Index(fields=["status", "received_at"]),
            models.Index(fields=["event_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id} ({self.event_type})"

    def mark(self, status: str, error: str = "") -> None:
        self.status = status
        self.last_error = error[:2000]
        self.processed_at = timezone.now()
        self.save(update_fields=["status", "last_error", "processed_at"])


class InternalRequestLog(models.Model):
    """Audit of what the clinical service asked us to do.

    Money moves because another service said so; being able to answer "who asked, and
    when" months later is worth one small append-only table.
    """

    path = models.CharField(max_length=255)
    external_ref = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    remote_addr = models.CharField(max_length=64, blank=True, default="")
    response_status = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_internal_request_log"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["external_ref", "created_at"])]

    def __str__(self) -> str:
        return f"{self.path} {self.external_ref} -> {self.response_status}"
