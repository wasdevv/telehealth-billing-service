from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("logoutall/", views.LogoutAllView.as_view(), name="logoutall"),
    path("google/", views.GoogleLoginView.as_view(), name="google-login"),
    path("2fa/setup/", views.OTPSetupView.as_view(), name="otp-setup"),
    path("2fa/enable/", views.OTPEnableView.as_view(), name="otp-enable"),
    path("me/", views.MeView.as_view(), name="me"),
]
