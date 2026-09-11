"""The HTTP surface telehealth-clinical-api talks to, plus health.

Paths, payload keys and the `Authorization: Token ...` header are fixed by the shared
integration contract and must not be adjusted here.
"""

import logging

from django.db import connection
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.tasks import send_appointment_reminder

from .models import InternalRequestLog, Invoice
from .permissions import IsInternalService
from .serializers import (
    InvoiceCreateSerializer,
    InvoiceSerializer,
    InvoiceVoidSerializer,
    ReminderSerializer,
)
from .services import get_or_create_invoice, void_invoice

logger = logging.getLogger(__name__)


class InternalAPIView(APIView):
    permission_classes = (IsInternalService,)
    authentication_classes = ()
    throttle_scope = "internal"

    def audit(self, request, response_status: int, external_ref: str = "") -> None:
        InternalRequestLog.objects.create(
            path=request.path,
            external_ref=external_ref,
            idempotency_key=request.headers.get("Idempotency-Key", "")[:128],
            remote_addr=request.META.get("REMOTE_ADDR", "")[:64],
            response_status=response_status,
        )


@extend_schema(
    tags=["internal"],
    summary="Create an invoice (idempotent on external_ref)",
    request=InvoiceCreateSerializer,
    responses={
        201: OpenApiResponse(InvoiceSerializer, description="Created"),
        200: OpenApiResponse(
            InvoiceSerializer, description="An invoice for this external_ref already existed"
        ),
        403: OpenApiResponse(description="Missing or invalid internal service token"),
    },
    examples=[
        OpenApiExample(
            "From telehealth-clinical-api",
            value={
                "external_ref": "appointment:42",
                "patient_email": "patient@example.com",
                "amount_cents": 15000,
                "currency": "usd",
                "description": "Telehealth consultation with Dr. Ana Reyes on 2026-09-23",
            },
            request_only=True,
        )
    ],
)
class InvoiceCreateView(InternalAPIView):
    def post(self, request):
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invoice, created = get_or_create_invoice(serializer.validated_data)
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK

        self.audit(request, code, invoice.external_ref)
        return Response(InvoiceSerializer(invoice).data, status=code)


@extend_schema(
    tags=["internal"],
    summary="Void an invoice by external_ref",
    request=InvoiceVoidSerializer,
    responses={
        200: OpenApiResponse(InvoiceSerializer, description="Voided, or already void"),
        404: OpenApiResponse(description="No invoice for that external_ref"),
        403: OpenApiResponse(description="Missing or invalid internal service token"),
    },
)
class InvoiceVoidView(InternalAPIView):
    def post(self, request):
        serializer = InvoiceVoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        external_ref = serializer.validated_data["external_ref"]

        invoice = void_invoice(external_ref)
        if invoice is None:
            # The contract says 404 here, and the clinical service treats it as success:
            # nothing to void is the same end state as a void that worked.
            self.audit(request, status.HTTP_404_NOT_FOUND, external_ref)
            return Response({"detail": "No invoice for that external_ref."}, status=status.HTTP_404_NOT_FOUND)

        self.audit(request, status.HTTP_200_OK, external_ref)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["internal"],
    summary="Queue an appointment reminder",
    request=ReminderSerializer,
    responses={
        202: OpenApiResponse(description="Accepted and queued"),
        403: OpenApiResponse(description="Missing or invalid internal service token"),
    },
)
class ReminderView(InternalAPIView):
    def post(self, request):
        serializer = ReminderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 202, not 200: the reminder has been accepted, not delivered. Sending it inline
        # would make the clinical service's booking request wait on Twilio.
        send_appointment_reminder.delay(
            email=data["email"],
            phone=data.get("phone", ""),
            starts_at=data["starts_at"].isoformat(),
            doctor=data["doctor"],
        )

        self.audit(request, status.HTTP_202_ACCEPTED)
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)


@extend_schema(
    tags=["ops"],
    summary="Liveness and dependency check",
    auth=[],
    responses={
        200: OpenApiResponse(description="Healthy"),
        503: OpenApiResponse(description="A dependency is unreachable"),
    },
)
class HealthView(APIView):
    """Unauthenticated on purpose: a load balancer has no token.

    It reports nothing an anonymous caller could use — no versions, no hostnames, no
    counts — only whether this process can reach its database.
    """

    permission_classes = (AllowAny,)
    authentication_classes = ()

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            logger.exception("health check failed")
            return Response({"status": "unhealthy"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"status": "ok", "time": timezone.now().isoformat()})


@extend_schema(
    tags=["internal"],
    summary="Read one invoice by external_ref",
    responses={200: InvoiceSerializer, 404: OpenApiResponse(description="Not found")},
)
class InvoiceDetailView(InternalAPIView):
    def get(self, request, external_ref: str):
        invoice = Invoice.objects.filter(external_ref=external_ref).first()
        if invoice is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceSerializer(invoice).data)
