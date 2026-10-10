from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import BaseUserCreationForm
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm

from nexa_core.identity.models import User


class UserCreationForm(BaseUserCreationForm[User]):
    class Meta:
        model = User
        fields = ("email",)


class UserChangeForm(DjangoUserChangeForm[User]):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin[User]):
    form = UserChangeForm
    add_form = UserCreationForm
    ordering = ("email",)
    list_display = ("email", "full_name", "is_staff", "is_active", "created_at")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("email", "full_name")
    readonly_fields = ("created_at", "updated_at", "last_login")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "email_verified_at")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Timestamps", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)
