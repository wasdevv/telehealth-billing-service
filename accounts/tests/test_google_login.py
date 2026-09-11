from unittest import mock

import pytest
from django.urls import reverse

from accounts.models import User

pytestmark = pytest.mark.django_db

CLAIMS = {
    "sub": "google-subject-123",
    "email": "patient@example.com",
    "email_verified": True,
    "aud": "test-client-id",
}


@pytest.fixture(autouse=True)
def google_configured(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "test-client-id"


def test_a_verified_token_creates_the_account_and_returns_a_knox_token(api_client):
    with mock.patch("accounts.views.verify_google_id_token", return_value=CLAIMS):
        response = api_client.post(reverse("accounts:google-login"), {"id_token": "x"}, format="json")

    assert response.status_code == 200
    assert response.json()["token"]
    assert User.objects.get(email="patient@example.com").google_sub == "google-subject-123"


def test_the_same_subject_logs_into_the_same_account(api_client):
    with mock.patch("accounts.views.verify_google_id_token", return_value=CLAIMS):
        api_client.post(reverse("accounts:google-login"), {"id_token": "x"}, format="json")
        api_client.post(reverse("accounts:google-login"), {"id_token": "x"}, format="json")

    assert User.objects.count() == 1


def test_a_rejected_token_returns_400(api_client):
    from accounts.services import GoogleAuthError

    with mock.patch("accounts.views.verify_google_id_token", side_effect=GoogleAuthError("nope")):
        response = api_client.post(reverse("accounts:google-login"), {"id_token": "x"}, format="json")

    assert response.status_code == 400
    assert User.objects.count() == 0


def test_google_cannot_bypass_an_enabled_second_factor(api_client, user):
    import pyotp

    user.google_sub = "google-subject-123"
    user.otp_secret = pyotp.random_base32()
    user.otp_enabled = True
    user.save()

    with mock.patch("accounts.views.verify_google_id_token", return_value=CLAIMS):
        response = api_client.post(reverse("accounts:google-login"), {"id_token": "x"}, format="json")

    assert response.status_code == 403


def test_an_unverified_email_is_refused_by_the_verifier(settings):
    from accounts.services import GoogleAuthError, verify_google_id_token

    with mock.patch("accounts.services.requests.get") as get:
        get.return_value.status_code = 200
        get.return_value.json.return_value = {**CLAIMS, "email_verified": False}

        with pytest.raises(GoogleAuthError):
            verify_google_id_token("x")


def test_a_token_for_another_audience_is_refused(settings):
    from accounts.services import GoogleAuthError, verify_google_id_token

    with mock.patch("accounts.services.requests.get") as get:
        get.return_value.status_code = 200
        get.return_value.json.return_value = {**CLAIMS, "aud": "someone-elses-client-id"}

        with pytest.raises(GoogleAuthError):
            verify_google_id_token("x")
