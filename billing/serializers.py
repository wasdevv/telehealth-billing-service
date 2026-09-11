from rest_framework import serializers

from .models import Invoice, Payment, WebhookEvent


class InvoiceCreateSerializer(serializers.Serializer):
    """The body telehealth-clinical-api sends. Field names are fixed by that contract."""

    external_ref = serializers.CharField(max_length=255)
    patient_email = serializers.EmailField()
    amount_cents = serializers.IntegerField(min_value=1)
    currency = serializers.CharField(max_length=3, default="usd")
    description = serializers.CharField(allow_blank=True, required=False, default="")

    def validate_currency(self, value: str) -> str:
        return value.lower()


class InvoiceVoidSerializer(serializers.Serializer):
    external_ref = serializers.CharField(max_length=255)


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ("id", "provider_charge_id", "amount_cents", "currency", "status", "created_at")
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = (
            "id",
            "external_ref",
            "patient_email",
            "amount_cents",
            "amount_refunded_cents",
            "currency",
            "description",
            "status",
            "provider",
            "provider_payment_intent_id",
            "provider_client_secret",
            "paid_at",
            "voided_at",
            "created_at",
            "payments",
        )
        read_only_fields = fields


class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = ("id", "provider", "event_id", "event_type", "status", "attempts", "received_at")
        read_only_fields = fields


class ReminderSerializer(serializers.Serializer):
    """Fixed by the contract. `phone` is an empty string, never null, when unknown."""

    email = serializers.EmailField()
    phone = serializers.CharField(allow_blank=True, required=False, default="")
    starts_at = serializers.DateTimeField()
    doctor = serializers.CharField(max_length=255)
