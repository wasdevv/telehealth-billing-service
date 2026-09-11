from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    # The contract with telehealth-clinical-api lives under /api/v1/ and its paths are
    # fixed by that contract: /invoices/, /invoices/void/, /notifications/reminder/,
    # /webhooks/stripe/ and /health/.
    path("api/v1/", include("billing.urls")),
    path("api/v1/notifications/", include("notifications.urls")),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
