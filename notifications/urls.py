from django.urls import path

from billing.views import ReminderView

app_name = "notifications"

urlpatterns = [
    path("reminder/", ReminderView.as_view(), name="reminder"),
]
