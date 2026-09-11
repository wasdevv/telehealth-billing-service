"""Outbound patient messaging.

Both channels degrade gracefully: with no credentials configured the task logs what it
would have sent and returns. A developer running this service locally should not have to
own a Twilio account, and a production deployment that loses one channel should not fail
the other.
"""

import logging

import requests
from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10

RETRY_KWARGS = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 5,
    "acks_late": True,
}


@shared_task(name="notifications.send_sms", **RETRY_KWARGS)
def send_sms(phone: str, body: str) -> str:
    if not phone:
        return "no-phone"

    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER):
        # Deliberately not an exception: an unconfigured channel is a deployment choice,
        # not a failure, and raising here would retry five times and then dead-letter.
        logger.info("twilio not configured; would have sent SMS to %s", _mask(phone))
        return "skipped-unconfigured"

    from twilio.rest import Client

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    message = client.messages.create(to=phone, from_=settings.TWILIO_FROM_NUMBER, body=body)
    logger.info("sent SMS %s to %s", message.sid, _mask(phone))
    return message.sid


@shared_task(name="notifications.send_email", **RETRY_KWARGS)
def send_email(to: str, subject: str, text: str) -> str:
    if not (settings.MAILGUN_API_KEY and settings.MAILGUN_DOMAIN):
        logger.info("mailgun not configured; would have emailed %s", _mask(to))
        return "skipped-unconfigured"

    response = requests.post(
        f"https://api.mailgun.net/v3/{settings.MAILGUN_DOMAIN}/messages",
        auth=("api", settings.MAILGUN_API_KEY),
        data={"from": settings.MAILGUN_FROM, "to": to, "subject": subject, "text": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()  # a 5xx here is exactly what the retry policy is for
    logger.info("sent email to %s", _mask(to))
    return "sent"


@shared_task(name="notifications.send_appointment_reminder", **RETRY_KWARGS)
def send_appointment_reminder(email: str, phone: str, starts_at: str, doctor: str) -> dict:
    """Fan one reminder out to both channels.

    Each channel is its own task so one failing provider cannot stop the other, and so a
    retry re-sends only the channel that actually failed.
    """
    body = f"Reminder: your telehealth appointment with {doctor} starts at {starts_at}."

    send_email.delay(to=email, subject="Your upcoming telehealth appointment", text=body)
    if phone:
        send_sms.delay(phone=phone, body=body)

    return {"email": bool(email), "sms": bool(phone)}


def _mask(value: str) -> str:
    """Contact details are PII; logs get the shape, not the value."""
    if not value or len(value) < 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"
