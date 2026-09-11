import base64
import io

import qrcode
from django.contrib.auth import login
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from knox.models import AuthToken
from knox.views import LoginView as KnoxLoginView
from knox.views import LogoutAllView as KnoxLogoutAllView
from knox.views import LogoutView as KnoxLogoutView
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User
from .serializers import (
    GoogleLoginSerializer,
    LoginSerializer,
    OTPEnableResponseSerializer,
    OTPEnableSerializer,
    OTPSetupResponseSerializer,
    TokenResponseSerializer,
    UserSerializer,
)
from .services import GoogleAuthError, verify_google_id_token


def _qr_png_base64(uri: str) -> str:
    buffer = io.BytesIO()
    qrcode.make(uri).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class LoginView(KnoxLoginView):
    """Exchange credentials — and a second factor when the account has one — for a token."""

    permission_classes = (AllowAny,)
    authentication_classes = ()
    throttle_scope = "auth"

    @extend_schema(
        request=LoginSerializer,
        responses={200: TokenResponseSerializer, 400: OpenApiResponse(description="Rejected")},
        auth=[],
        summary="Log in",
    )
    def post(self, request, format=None):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        # Knox reads request.user; the session backend is named explicitly because
        # authenticate() above already chose one and login() would otherwise guess.
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return super().post(request, format=format)

    def get_post_response_data(self, request, token, instance):
        data = super().get_post_response_data(request, token, instance)
        data["user"] = UserSerializer(request.user).data
        return data


# extend_schema on the class does nothing for a plain APIView — the decorator has to
# reach the handler. extend_schema_view is how you target it by name.
@extend_schema_view(
    post=extend_schema(summary="Revoke the token used for this request", request=None, responses={204: None})
)
class LogoutView(KnoxLogoutView):
    pass


@extend_schema_view(
    post=extend_schema(summary="Revoke every token this user holds", request=None, responses={204: None})
)
class LogoutAllView(KnoxLogoutAllView):
    pass


class GoogleLoginView(APIView):
    """Trade a verified Google ID token for a Knox token."""

    permission_classes = (AllowAny,)
    authentication_classes = ()
    throttle_scope = "auth"

    @extend_schema(
        request=GoogleLoginSerializer,
        responses={200: TokenResponseSerializer, 400: OpenApiResponse(description="Rejected")},
        auth=[],
        summary="Log in with Google",
    )
    def post(self, request):
        serializer = GoogleLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            claims = verify_google_id_token(serializer.validated_data["id_token"])
        except GoogleAuthError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Matched on `sub`, not on email: Google's subject is stable, an email address is
        # not, and matching on a mutable field is how accounts get merged by accident.
        user = User.objects.filter(google_sub=claims["sub"]).first()
        if user is None:
            email = claims["email"].strip().lower()
            user, _ = User.objects.get_or_create(email=email, defaults={"username": email, "is_active": True})
            user.google_sub = claims["sub"]
            user.save(update_fields=["google_sub"])

        if user.otp_enabled:
            # OAuth proves one factor exactly like a password does. Letting it skip the
            # second one would make Google a way around 2FA for anyone who enabled it.
            return Response(
                {"detail": "This account requires a second factor; use the password login flow."},
                status=status.HTTP_403_FORBIDDEN,
            )

        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        instance, token = AuthToken.objects.create(user)
        return Response(
            {
                "expiry": instance.expiry,
                "token": token,
                "user": UserSerializer(user).data,
            }
        )


class OTPSetupView(APIView):
    """Start enrolment. Returns the provisioning URI and a QR code; enables nothing."""

    permission_classes = (IsAuthenticated,)

    @extend_schema(request=None, responses={200: OTPSetupResponseSerializer}, summary="Begin 2FA setup")
    def post(self, request):
        request.user.start_otp_enrollment()
        uri = request.user.provisioning_uri()
        return Response({"otpauth_uri": uri, "qr_code_png_base64": _qr_png_base64(uri)})


class OTPEnableView(APIView):
    """Confirm the phone holds the secret, then switch the factor on."""

    permission_classes = (IsAuthenticated,)

    @extend_schema(
        request=OTPEnableSerializer,
        responses={200: OTPEnableResponseSerializer, 400: OpenApiResponse(description="Wrong code")},
        summary="Enable 2FA",
    )
    def post(self, request):
        serializer = OTPEnableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        codes = request.user.enable_otp(serializer.validated_data["code"])
        if codes is None:
            return Response({"detail": "That code is not valid."}, status=status.HTTP_400_BAD_REQUEST)

        # The one and only time the plaintext codes leave the server.
        return Response({"otp_enabled": True, "recovery_codes": codes})


class MeView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(responses={200: UserSerializer}, summary="The authenticated account")
    def get(self, request):
        return Response(UserSerializer(request.user).data)
