"""The Stripe webhook endpoint.

Four things happen here, in this order, and the order is the design:

1. verify the signature before anything else
2. record the event even when we do not handle its type
3. persist it under a unique constraint so a redelivery is a no-op
4. answer 200 immediately and hand the work to Celery after the commit

Each step exists because of a specific way this endpoint gets people's money wrong.
"""

import logging

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import WebhookEvent
from .processor import HANDLED_EVENT_TYPES
from .tasks import process_webhook_event

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
@extend_schema(
    tags=["webhooks"],
    summary="Stripe webhook receiver",
    description=(
        "Verifies the Stripe signature, records the event, and queues processing. "
        "Always answers quickly: the body is processed asynchronously."
    ),
    auth=[],
    request=None,
    responses={
        200: OpenApiResponse(description="Accepted (including duplicates and ignored types)"),
        400: OpenApiResponse(description="Missing, malformed or unverifiable signature"),
    },
)
class StripeWebhookView(APIView):
    """Authenticated by Stripe's signature, not by a token — so no auth classes here."""

    permission_classes = (AllowAny,)
    authentication_classes = ()
    throttle_scope = "webhook"

    def post(self, request):
        # --- 1. signature first ----------------------------------------------------
        # Nothing is read out of the body, logged, or written to the database before
        # this passes. Without it the endpoint is "anyone on the internet can declare an
        # invoice paid": the payload is just JSON, and it claims whatever it likes.
        # construct_event also rejects stale timestamps, which is what stops a genuine
        # past event being captured and replayed later.
        signature = request.headers.get("Stripe-Signature", "")
        secret = settings.STRIPE_WEBHOOK_SECRET

        if not secret:
            # No secret means no way to tell a real event from a forged one. Refusing is
            # the only safe answer; accepting "because it is not configured yet" would
            # leave the door open exactly where money is decided.
            logger.error("STRIPE_WEBHOOK_SECRET is not configured; refusing webhook")
            return Response(
                {"detail": "Webhook processing is not configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            event = stripe.Webhook.construct_event(payload=request.body, sig_header=signature, secret=secret)
        except ValueError:
            logger.warning("stripe webhook: body is not valid JSON")
            return Response({"detail": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.SignatureVerificationError:
            # Deliberately the same shape of answer as a malformed body: a forged request
            # learns nothing about why it failed.
            logger.warning("stripe webhook: signature verification failed")
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)

        event_id = event["id"]
        event_type = event["type"]

        # --- 2. record even what we do not handle -----------------------------------
        # An unhandled type is still evidence. Dropping it silently means "Stripe has been
        # sending us charge.dispute.created for a month and nobody noticed" is a question
        # nobody can answer. It is stored as IGNORED and never queued.
        handled = event_type in HANDLED_EVENT_TYPES

        # --- 3. uniqueness in the database, not in an `if` --------------------------
        # Stripe redelivers for days after a non-2xx, and delivers twice on its own more
        # often than anyone expects. The unique constraint on (provider, event_id) is what
        # makes the second delivery lose the INSERT. An `exists()` check here would read a
        # snapshot from before the competing transaction committed — the exact race that
        # refunds a patient twice.
        try:
            with transaction.atomic():
                webhook_event = WebhookEvent.objects.create(
                    provider="stripe",
                    event_id=event_id,
                    event_type=event_type,
                    payload=event.to_dict() if hasattr(event, "to_dict") else dict(event),
                    status=WebhookEvent.Status.PENDING if handled else WebhookEvent.Status.IGNORED,
                )
        except IntegrityError:
            logger.info("stripe webhook %s already received; ignoring redelivery", event_id)
            return Response({"status": "duplicate"}, status=status.HTTP_200_OK)

        if not handled:
            return Response({"status": "ignored", "event_type": event_type}, status=status.HTTP_200_OK)

        # --- 4. commit, then queue, then answer -------------------------------------
        # on_commit, not delay() inline: Celery workers are a different process against
        # the same database. Queue the task inside the transaction and a worker can pick
        # it up microseconds later, before this INSERT is visible — the task then fails
        # with DoesNotExist on a row that is about to exist. Worse, if the transaction
        # rolls back, the task runs for an event that never happened.
        transaction.on_commit(lambda: process_webhook_event.delay(webhook_event.id))

        # 200 now, work later. Stripe times out at 20 seconds and retries on anything
        # that is not a 2xx, so doing the work inline turns one slow refund into a
        # redelivery storm — and a queue of duplicate events to de-duplicate.
        return Response({"status": "queued", "event_id": event_id}, status=status.HTTP_200_OK)
