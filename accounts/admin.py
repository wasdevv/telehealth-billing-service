from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("email", "username", "otp_enabled", "is_staff", "date_joined")
    search_fields = ("email", "username")
    ordering = ("email",)
    # Credential material is never editable or visible through the admin.
    exclude = ("otp_secret", "otp_recovery_hashes")
