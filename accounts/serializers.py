"""Auth serializers.

The second factor is enforced inside ``LoginSerializer`` rather than in a separate view.
Putting it anywhere else leaves a code path where credentials validate and a token is
issued without the factor ever being asked for.
"""

from django.contrib.auth import authenticate
from rest_framework import serializers

from .models import User


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    otp_code = serializers.CharField(required=False, allow_blank=True, write_only=True)
    recovery_code = serializers.CharField(required=False, allow_blank=True, write_only=True)

    default_error_messages = {
        "invalid": "Invalid email or password.",
        "otp_required": "This account requires a second factor.",
        "otp_invalid": "That code is not valid.",
    }

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["email"],
            password=attrs["password"],
        )

        # One message for "no such account" and "wrong password". Telling them apart hands
        # an attacker a way to enumerate which emails exist.
        if user is None or not user.is_active:
            raise serializers.ValidationError(self.error_messages["invalid"], code="invalid")

        if user.otp_enabled:
            otp_code = (attrs.get("otp_code") or "").strip()
            recovery = (attrs.get("recovery_code") or "").strip()

            if not otp_code and not recovery:
                # 'otp_required' is distinguishable on purpose: the client has to know to
                # ask for the code. It is only reachable *after* the password was correct.
                raise serializers.ValidationError(
                    {"otp_code": self.error_messages["otp_required"]}, code="otp_required"
                )

            if not (user.verify_otp(otp_code) or user.consume_recovery_code(recovery)):
                raise serializers.ValidationError(
                    {"otp_code": self.error_messages["otp_invalid"]}, code="otp_invalid"
                )

        attrs["user"] = user
        return attrs


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "username", "otp_enabled", "date_joined")
        read_only_fields = fields


class OTPSetupResponseSerializer(serializers.Serializer):
    otpauth_uri = serializers.CharField()
    qr_code_png_base64 = serializers.CharField(help_text="base64-encoded PNG, ready for a data: URI")


class OTPEnableSerializer(serializers.Serializer):
    code = serializers.CharField()


class OTPEnableResponseSerializer(serializers.Serializer):
    otp_enabled = serializers.BooleanField()
    recovery_codes = serializers.ListField(
        child=serializers.CharField(),
        help_text="Shown exactly once. Only SHA-256 digests are stored.",
    )


class GoogleLoginSerializer(serializers.Serializer):
    id_token = serializers.CharField(help_text="The Google-issued ID token from the client.")


class TokenResponseSerializer(serializers.Serializer):
    expiry = serializers.DateTimeField()
    token = serializers.CharField()
    user = UserSerializer()
