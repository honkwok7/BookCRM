from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from accounts.models import EmailVerificationToken, LoginHistory
from accounts.serializers import (
    CustomTokenObtainPairSerializer,
    EmailVerificationSerializer,
    ForgotPasswordSerializer,
    LogoutSerializer,
    PasswordResetConfirmSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    UserProfileSerializer,
)
from accounts.services import (
    queue_password_reset_email,
    queue_verification_email,
    revoke_refresh_tokens,
)
from core.audit import AuditAction, client_ip, record_audit

User = get_user_model()

# Brute-force and email-flood protection: the scoped rate ("login" / "password_reset" in
# DEFAULT_THROTTLE_RATES) applies on top of the global anonymous/user rates.
THROTTLES = [AnonRateThrottle, UserRateThrottle, ScopedRateThrottle]


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = THROTTLES
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        email = request.data.get("email")
        if response.status_code == status.HTTP_200_OK and email:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                LoginHistory.objects.create(
                    user=user,
                    is_successful=True,
                    ip_address=client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                )
        return response


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        refresh_token = serializer.validated_data["refresh"]
        token = RefreshToken(refresh_token)
        token.blacklist()
        return Response(
            {"detail": "Logged out successfully."}, status=status.HTTP_205_RESET_CONTENT
        )


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class VerifyEmailView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = THROTTLES
    throttle_scope = "password_reset"

    def post(self, request, *args, **kwargs):
        serializer = EmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_value = serializer.validated_data["token"]
        token = get_object_or_404(EmailVerificationToken, token=token_value)
        if not token.is_valid:
            return Response(
                {"detail": "Token is invalid or expired"}, status=status.HTTP_400_BAD_REQUEST
            )

        token.used_at = timezone.now()
        token.save(update_fields=["used_at", "updated_at"])
        token.user.email_verified = True
        token.user.save(update_fields=["email_verified"])
        record_audit(AuditAction.ACCOUNT_EMAIL_VERIFIED, actor=token.user, target=token.user)
        return Response({"detail": "Email verified"})


class ResendVerificationView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = THROTTLES
    throttle_scope = "password_reset"

    def post(self, request, *args, **kwargs):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email__iexact=email).first()
        if not user or user.email_verified:
            return Response({"detail": "If account exists, verification email has been sent."})

        queue_verification_email(user)
        return Response({"detail": "If account exists, verification email has been sent."})


class ForgotPasswordView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = THROTTLES
    throttle_scope = "password_reset"

    def post(self, request, *args, **kwargs):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email__iexact=email).first()
        if user:
            queue_password_reset_email(user)
        return Response(
            {"detail": "If account exists, password reset instructions have been sent."}
        )


class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = THROTTLES
    throttle_scope = "password_reset"

    def post(self, request, *args, **kwargs):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        with transaction.atomic():
            user.set_password(serializer.validated_data["new_password"])
            user.save(update_fields=["password"])
            # Sign out everywhere: whoever held the old password may hold a refresh token.
            revoked = revoke_refresh_tokens(user)
            record_audit(
                AuditAction.ACCOUNT_PASSWORD_RESET,
                actor=user,
                target=user,
                metadata={"refresh_tokens_revoked": revoked},
            )
        return Response({"detail": "Password changed successfully"})
