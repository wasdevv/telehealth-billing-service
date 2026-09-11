"""Graceful degradation, and the fan-out."""

from unittest import mock

import pytest

from notifications.tasks import _mask, send_appointment_reminder, send_email, send_sms

pytestmark = pytest.mark.django_db


def test_sms_without_credentials_logs_instead_of_failing(settings):
    settings.TWILIO_ACCOUNT_SID = ""

    assert send_sms.run(phone="+15551234567", body="hi") == "skipped-unconfigured"


def test_sms_without_a_phone_number_is_a_no_op():
    assert send_sms.run(phone="", body="hi") == "no-phone"


def test_email_without_credentials_logs_instead_of_failing(settings):
    settings.MAILGUN_API_KEY = ""

    assert send_email.run(to="a@b.com", subject="s", text="t") == "skipped-unconfigured"


def test_email_posts_to_mailgun_when_configured(settings):
    settings.MAILGUN_API_KEY = "key-test"
    settings.MAILGUN_DOMAIN = "mg.example.com"

    with mock.patch("notifications.tasks.requests.post") as post:
        post.return_value.raise_for_status.return_value = None
        assert send_email.run(to="a@b.com", subject="s", text="t") == "sent"

    url = post.call_args.args[0]
    assert url == "https://api.mailgun.net/v3/mg.example.com/messages"
    assert post.call_args.kwargs["timeout"] == 10


def test_reminder_fans_out_to_both_channels():
    with (
        mock.patch("notifications.tasks.send_email.delay") as email,
        mock.patch("notifications.tasks.send_sms.delay") as sms,
    ):
        result = send_appointment_reminder.run(
            email="a@b.com", phone="+15551234567", starts_at="2026-09-23T10:00:00Z", doctor="Dr. X"
        )

    assert result == {"email": True, "sms": True}
    assert email.called and sms.called


def test_reminder_skips_sms_when_there_is_no_phone():
    with (
        mock.patch("notifications.tasks.send_email.delay") as email,
        mock.patch("notifications.tasks.send_sms.delay") as sms,
    ):
        result = send_appointment_reminder.run(
            email="a@b.com", phone="", starts_at="2026-09-23T10:00:00Z", doctor="Dr. X"
        )

    assert result == {"email": True, "sms": False}
    assert email.called
    assert not sms.called


def test_contact_details_are_masked_in_logs():
    assert _mask("+15551234567") == "+1***67"
    assert _mask("patient@example.com") == "pa***om"
    assert _mask("ab") == "***"
