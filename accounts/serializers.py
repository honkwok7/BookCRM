from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from accounts.services import queue_verification_email

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    accept_terms = serializers.BooleanField(write_only=True)
    accept_privacy = serializers.BooleanField(write_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "phone_number",
            "first_name",
            "last_name",
            "password",
            "accept_terms",
            "accept_privacy",
        )

    def validate_email(self, email):
        # Unique regardless of case: "Sam@x.test" and "sam@x.test" are one mailbox.
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def validate(self, attrs):
        if not attrs.get("accept_terms") or not attrs.get("accept_privacy"):
            raise serializers.ValidationError("Terms and privacy policy acceptance are required")
        candidate = User(
            email=attrs.get("email", ""),
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
        )
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": list(error.messages)}) from error
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("accept_terms")
        validated_data.pop("accept_privacy")
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        now = timezone.now()
        user.terms_accepted_at = now
        user.privacy_accepted_at = now
        user.save(update_fields=["terms_accepted_at", "privacy_accepted_at"])
        queue_verification_email(user)
        return user


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = User.EMAIL_FIELD

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["email"] = user.email
        return token

    def validate(self, attrs):
        if "email" in attrs and "password" in attrs:
            attrs[self.username_field] = attrs.pop("email")
        data = super().validate(attrs)
        return data


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "phone_number",
            "first_name",
            "last_name",
            "profile_image",
            "email_verified",
            "account_status",
            "last_login",
        )
        read_only_fields = ("id", "email", "email_verified", "account_status", "last_login")


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class EmailVerificationSerializer(serializers.Serializer):
    token = serializers.CharField()


class ResendVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.IntegerField()
    token = serializers.CharField()
    new_password = serializers.CharField()

    def validate(self, attrs):
        user = User.objects.filter(pk=attrs["uid"]).first()
        if user is None or not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError("Invalid or expired reset token")
        try:
            validate_password(attrs["new_password"], user=user)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"new_password": list(error.messages)}) from error
        attrs["user"] = user
        return attrs
