"""Celery tasks: webhook processing plus the three recurring safety nets."""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Invoice, WebhookEvent
from .processor import UnknownInvoice, WebhookProcessor

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    # Retries are automatic and spaced out, because the usual reason this fails is a
    # dependency having a bad minute, and hammering it makes that minute longer.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
    # acks_late: the message is acknowledged after the task finishes, so a worker killed
    # mid-run returns the job to the queue instead of losing a payment. Only safe because
    # the handlers are idempotent — running one twice reaches the same end state.
    acks_late=True,
    name="billing.process_webhook_event",
)
def process_webhook_event(self, webhook_event_id: int) -> str:
    event = WebhookEvent.objects.filter(pk=webhook_event_id).first()
    if event is None:
        # Nothing to retry towards: the row is gone, so a retry would fail identically
        # forever. This is why it returns rather than raising.
        logger.warning("webhook event %s no longer exists", webhook_event_id)
        return "missing"

    if event.status == WebhookEvent.Status.PROCESSED:
        # Reached by a redelivered Celery message rather than a redelivered Stripe event.
        return "already-processed"

    WebhookEvent.objects.filter(pk=event.pk).update(
        status=WebhookEvent.Status.PROCESSING, attempts=event.attempts + 1
    )

    try:
        result = WebhookProcessor(event).process()
    except UnknownInvoice as exc:
        # A real possibility, not a bug: Stripe can deliver before the clinical service's
        # invoice call lands. Marking it failed leaves it for the sweeper, which retries
        # it later when the invoice exists.
        logger.warning("%s", exc)
        event.mark(WebhookEvent.Status.FAILED, str(exc))
        raise self.retry(exc=exc) from exc
    except Exception as exc:
        event.mark(WebhookEvent.Status.FAILED, repr(exc))
        raise

    event.mark(result)
    return result


@shared_task(name="billing.retry_failed_webhook_events")
def retry_failed_webhook_events(max_attempts: int = 10, batch_size: int = 100) -> dict:
    """Dead-letter sweeper.

    Celery's own retries eventually run out. Without this, an event that failed while a
    dependency was down stays failed forever and the invoice it described stays wrong —
    silently, which is the worst way for a billing system to be wrong.
    """
    stale = WebhookEvent.objects.filter(
        status__in=[WebhookEvent.Status.FAILED, WebhookEvent.Status.PROCESSING],
        attempts__lt=max_attempts,
    ).order_by("received_at")[:batch_size]

    requeued = 0
    for event in stale:
        process_webhook_event.delay(event.id)
        requeued += 1

    if requeued:
        logger.info("requeued %s stalled webhook events", requeued)
    return {"requeued": requeued}


@shared_task(name="billing.reconcile_with_stripe")
def reconcile_with_stripe(lookback_hours: int = 48, batch_size: int = 200) -> dict:
    """Compare local state against the provider, which is the source of truth for money.

    Webhooks get lost. An endpoint can be down for an hour, a delivery can exhaust its
    retries, a deploy can drop a request mid-flight. Every one of those leaves an invoice
    that Stripe considers paid and we consider open — a patient charged and not credited.
    No amount of webhook robustness removes the need to ask the provider directly.
    """
    from .stripe_gateway import retrieve_payment_intent

    since = timezone.now() - timedelta(hours=lookback_hours)
    candidates = Invoice.objects.filter(
        status__in=[Invoice.Status.OPEN, Invoice.Status.FAILED],
        provider_payment_intent_id__isnull=False,
        created_at__gte=since,
    )[:batch_size]

    checked = corrected = 0
    for invoice in candidates:
        checked += 1
        intent = retrieve_payment_intent(invoice.provider_payment_intent_id)
        if not intent:
            continue

        if intent.get("status") == "succeeded":
            with transaction.atomic():
                locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
                if not locked.is_settled:
                    locked.mark_paid()
                    corrected += 1
                    logger.warning(
                        "reconciliation corrected %s: Stripe says paid, we said %s",
                        locked.external_ref,
                        invoice.status,
                    )

    return {"checked": checked, "corrected": corrected}


@shared_task(name="billing.expire_stale_invoices")
def expire_stale_invoices(older_than_days: int = 30, batch_size: int = 500) -> dict:
    """Close out invoices nobody ever paid, so `open` means something.

    Only untouched ones: an invoice with any payment attempt against it is somebody's
    problem to look at, not something to sweep away.
    """
    cutoff = timezone.now() - timedelta(days=older_than_days)
    stale = Invoice.objects.filter(
        status=Invoice.Status.OPEN, created_at__lt=cutoff, payments__isnull=True
    ).values_list("pk", flat=True)[:batch_size]

    expired = Invoice.objects.filter(pk__in=list(stale)).update(
        status=Invoice.Status.VOID, voided_at=timezone.now()
    )
    if expired:
        logger.info("expired %s stale invoices", expired)
    return {"expired": expired}
