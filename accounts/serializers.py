from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from accounts.models import EmailVerificationToken

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
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

    def create(self, validated_data):
        accept_terms = validated_data.pop("accept_terms")
        accept_privacy = validated_data.pop("accept_privacy")
        if not accept_terms or not accept_privacy:
            raise serializers.ValidationError("Terms and privacy policy acceptance are required")
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        now = timezone.now()
        user.terms_accepted_at = now
        user.privacy_accepted_at = now
        user.save(update_fields=["terms_accepted_at", "privacy_accepted_at"])

        EmailVerificationToken.objects.create(
            user=user,
            token=EmailVerificationToken.generate_token(),
            expires_at=EmailVerificationToken.default_expires_at(),
        )
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
    new_password = serializers.CharField(min_length=10)

    def validate(self, attrs):
        try:
            user = User.objects.get(pk=attrs["uid"])
        except User.DoesNotExist as exc:
            raise serializers.ValidationError("Invalid reset payload") from exc
        if not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError("Invalid or expired reset token")
        attrs["user"] = user
        return attrs
