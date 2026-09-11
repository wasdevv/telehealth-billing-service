"""Accounts and their second factor.

The TOTP secret is the only credential material stored here that is not a hash, because
TOTP verification needs the shared secret itself. Recovery codes are hashes.
"""

import hashlib
import secrets

import pyotp
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction


class User(AbstractUser):
    """Email is the identifier; `username` stays only because AbstractUser requires it."""

    email = models.EmailField(unique=True)

    otp_secret = models.CharField(max_length=64, blank=True, default="")
    otp_enabled = models.BooleanField(default=False)
    # SHA-256 digests only. A database dump must not be replayable against an account.
    otp_recovery_hashes = models.JSONField(default=list, blank=True)

    google_sub = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        help_text="Google's stable subject identifier; never the email, which can change.",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    RECOVERY_CODE_COUNT = 10

    class Meta:
        db_table = "accounts_user"

    def __str__(self) -> str:
        return self.email

    # --- TOTP -------------------------------------------------------------------------
    def start_otp_enrollment(self) -> str:
        """Issue a fresh secret without enabling anything.

        Enrolment replaces any half-finished attempt and never touches ``otp_enabled``: a
        secret becomes the account's second factor only once a code proves the phone has it.
        """
        self.otp_secret = pyotp.random_base32()
        self.save(update_fields=["otp_secret"])
        return self.otp_secret

    def provisioning_uri(self) -> str:
        return pyotp.TOTP(self.otp_secret).provisioning_uri(name=self.email, issuer_name=settings.OTP_ISSUER)

    def verify_otp(self, code: str | None) -> bool:
        """Check a TOTP code, tolerating one 30-second step of clock skew either way.

        ``valid_window=1`` is the documented remedy for phones whose clock has drifted. It
        widens the window to 90 seconds, which is a deliberate trade: without it, a user
        with a slightly wrong clock can never log in.
        """
        if not self.otp_secret or not code:
            return False
        return pyotp.TOTP(self.otp_secret).verify(str(code).strip(), valid_window=1)

    @staticmethod
    def hash_recovery_code(code: str) -> str:
        return hashlib.sha256(code.strip().lower().encode()).hexdigest()

    def enable_otp(self, code: str) -> list[str] | None:
        """Turn enrolment into an enabled factor and return the plaintext recovery codes.

        This is the only moment those codes exist outside the user's own records.
        """
        if not self.verify_otp(code):
            return None

        plain = [secrets.token_hex(6) for _ in range(self.RECOVERY_CODE_COUNT)]
        self.otp_enabled = True
        self.otp_recovery_hashes = [self.hash_recovery_code(c) for c in plain]
        self.save(update_fields=["otp_enabled", "otp_recovery_hashes"])
        return plain

    def consume_recovery_code(self, code: str | None) -> bool:
        """Spend one recovery code, under a row lock so two requests cannot spend the same one."""
        if not code:
            return False

        digest = self.hash_recovery_code(code)
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=self.pk)
            remaining = list(locked.otp_recovery_hashes or [])
            if digest not in remaining:
                return False
            remaining.remove(digest)
            locked.otp_recovery_hashes = remaining
            locked.save(update_fields=["otp_recovery_hashes"])
        self.refresh_from_db(fields=["otp_recovery_hashes"])
        return True
