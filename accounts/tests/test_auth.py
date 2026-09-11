"""Knox tokens, and the second factor living inside the login serializer."""

import pyotp
import pytest
from django.urls import reverse
from knox.models import AuthToken

from accounts.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def login(api_client, **extra):
    return api_client.post(
        reverse("accounts:login"),
        {"email": "patient@example.com", "password": PASSWORD, **extra},
        format="json",
    )


def test_login_returns_a_knox_token(api_client, user):
    response = login(api_client)

    assert response.status_code == 200
    assert response.json()["token"]
    assert AuthToken.objects.filter(user=user).count() == 1


def test_a_wrong_password_and_an_unknown_email_answer_identically(api_client, user):
    wrong_password = api_client.post(
        reverse("accounts:login"),
        {"email": "patient@example.com", "password": "nope"},
        format="json",
    )
    unknown_email = api_client.post(
        reverse("accounts:login"), {"email": "nobody@example.com", "password": PASSWORD}, format="json"
    )

    assert wrong_password.status_code == unknown_email.status_code == 400
    assert wrong_password.json() == unknown_email.json()


def test_me_requires_a_token(api_client):
    assert api_client.get(reverse("accounts:me")).status_code == 401


def test_me_works_with_the_token(api_client, user):
    token = login(api_client).json()["token"]

    response = api_client.get(reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 200
    assert response.json()["email"] == "patient@example.com"


def test_logout_revokes_only_that_token(api_client, user):
    first = login(api_client).json()["token"]
    second = login(api_client).json()["token"]

    api_client.post(reverse("accounts:logout"), HTTP_AUTHORIZATION=f"Bearer {first}")

    assert api_client.get(reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {first}").status_code == 401
    assert api_client.get(reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {second}").status_code == 200


def test_logoutall_revokes_every_token(api_client, user):
    first = login(api_client).json()["token"]
    second = login(api_client).json()["token"]

    api_client.post(reverse("accounts:logoutall"), HTTP_AUTHORIZATION=f"Bearer {first}")

    assert api_client.get(reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {second}").status_code == 401
    assert AuthToken.objects.filter(user=user).count() == 0


def test_token_limit_per_user_is_enforced(api_client, user, settings):
    for _ in range(settings.REST_KNOX["TOKEN_LIMIT_PER_USER"] + 3):
        login(api_client)

    assert AuthToken.objects.filter(user=user).count() <= settings.REST_KNOX["TOKEN_LIMIT_PER_USER"]


class TestTwoFactor:
    def enrol(self, api_client, user) -> str:
        token = login(api_client).json()["token"]
        response = api_client.post(reverse("accounts:otp-setup"), HTTP_AUTHORIZATION=f"Bearer {token}")
        assert response.status_code == 200
        assert response.json()["otpauth_uri"].startswith("otpauth://totp/")
        assert response.json()["qr_code_png_base64"]
        user.refresh_from_db()
        return token

    def test_setup_does_not_enable_anything(self, api_client, user):
        self.enrol(api_client, user)

        assert user.otp_enabled is False

    def test_enable_rejects_a_wrong_code(self, api_client, user):
        token = self.enrol(api_client, user)

        response = api_client.post(
            reverse("accounts:otp-enable"),
            {"code": "000000"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        user.refresh_from_db()
        assert response.status_code == 400
        assert user.otp_enabled is False

    def test_enable_returns_recovery_codes_exactly_once_and_stores_only_hashes(self, api_client, user):
        token = self.enrol(api_client, user)
        code = pyotp.TOTP(user.otp_secret).now()

        response = api_client.post(
            reverse("accounts:otp-enable"),
            {"code": code},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        codes = response.json()["recovery_codes"]
        user.refresh_from_db()
        assert response.status_code == 200
        assert len(codes) == User.RECOVERY_CODE_COUNT
        assert user.otp_enabled is True
        assert user.otp_recovery_hashes == [User.hash_recovery_code(c) for c in codes]
        for plain in codes:
            assert plain not in user.otp_recovery_hashes

    def test_login_without_the_code_is_refused_once_2fa_is_on(self, api_client, user):
        user.otp_secret = pyotp.random_base32()
        user.otp_enabled = True
        user.save()

        response = login(api_client)

        assert response.status_code == 400
        assert "otp_code" in response.json()
        assert AuthToken.objects.count() == 0

    def test_login_with_a_wrong_code_is_refused(self, api_client, user):
        user.otp_secret = pyotp.random_base32()
        user.otp_enabled = True
        user.save()

        response = login(api_client, otp_code="000000")

        assert response.status_code == 400
        assert AuthToken.objects.count() == 0

    def test_login_with_a_valid_code_returns_a_token(self, api_client, user):
        user.otp_secret = pyotp.random_base32()
        user.otp_enabled = True
        user.save()

        response = login(api_client, otp_code=pyotp.TOTP(user.otp_secret).now())

        assert response.status_code == 200
        assert response.json()["token"]

    def test_a_recovery_code_works_once_and_never_again(self, api_client, user):
        user.otp_secret = pyotp.random_base32()
        user.otp_enabled = True
        user.otp_recovery_hashes = [User.hash_recovery_code("rescue-me")]
        user.save()

        first = login(api_client, recovery_code="rescue-me")
        second = login(api_client, recovery_code="rescue-me")

        user.refresh_from_db()
        assert first.status_code == 200
        assert second.status_code == 400
        assert user.otp_recovery_hashes == []

    def test_clock_drift_of_one_step_is_tolerated(self, user):
        """valid_window=1 is the documented remedy for a phone whose clock has drifted."""
        import time

        user.otp_secret = pyotp.random_base32()
        totp = pyotp.TOTP(user.otp_secret)
        one_step_ago = totp.at(time.time() - 30)

        assert user.verify_otp(one_step_ago) is True

    def test_a_code_from_five_minutes_ago_is_not_accepted(self, user):
        import time

        user.otp_secret = pyotp.random_base32()
        stale = pyotp.TOTP(user.otp_secret).at(time.time() - 300)

        assert user.verify_otp(stale) is False
