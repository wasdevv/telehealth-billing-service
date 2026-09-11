from django.urls import path

from . import views
from .webhooks import StripeWebhookView

app_name = "billing"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("invoices/", views.InvoiceCreateView.as_view(), name="invoice-create"),
    path("invoices/void/", views.InvoiceVoidView.as_view(), name="invoice-void"),
    path("invoices/<str:external_ref>/", views.InvoiceDetailView.as_view(), name="invoice-detail"),
    path("webhooks/stripe/", StripeWebhookView.as_view(), name="stripe-webhook"),
]
