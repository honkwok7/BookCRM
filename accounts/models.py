import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import BaseUUIDModel


class CustomUserManager(UserManager):
    def get_by_natural_key(self, username):
        # Login by email ignores case ("Sam@X.test" signs in the account "sam@x.test").
        return self.get(email__iexact=username)

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email must be provided")
        email = self.normalize_email(email)
        extra_fields.setdefault("username", self.model.generate_username(email))
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, blank=True)
    profile_image = models.ImageField(upload_to="users/profiles/", blank=True, null=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    privacy_accepted_at = models.DateTimeField(null=True, blank=True)
    email_verified = models.BooleanField(default=False)
    account_status = models.CharField(max_length=20, default="active")
    # Platform operators (the /saas/ pages), separate from is_superuser (Django admin, and the
    # only ones who can grant this). Neither gives access to an organization's data.
    is_platform_staff = models.BooleanField(default=False)

    @property
    def is_platform_user(self) -> bool:
        return self.is_active and (self.is_superuser or self.is_platform_staff)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(Lower("email"), name="user_email_unique_ignoring_case")
        ]

    objects = CustomUserManager()

    @staticmethod
    def generate_username(email: str) -> str:
        # ``username`` is unique but unused for login; derive a unique value from the email.
        return f"{(email or 'user').split('@')[0][:120]}-{secrets.token_hex(3)}"

    def save(self, *args, **kwargs):
        # One mailbox, one account: emails are stored lower-case (see Meta.constraints).
        self.email = (self.email or "").strip().lower()
        if not self.username:
            self.username = self.generate_username(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class EmailVerificationToken(BaseUUIDModel):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="email_verification_tokens"
    )
    token = models.CharField(max_length=128, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(48)

    @classmethod
    def default_expires_at(cls):
        return timezone.now() + timedelta(hours=24)

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and timezone.now() < self.expires_at


class LoginHistory(BaseUUIDModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="login_history")
    is_successful = models.BooleanField(default=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
