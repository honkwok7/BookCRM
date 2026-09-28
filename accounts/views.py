from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
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
from core.services import write_audit_log

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        email = request.data.get("email")
        if response.status_code == status.HTTP_200_OK and email:
            user = User.objects.filter(email=email).first()
            if user:
                LoginHistory.objects.create(
                    user=user,
                    is_successful=True,
                    ip_address=request.META.get("REMOTE_ADDR"),
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
        write_audit_log(
            action="account.email_verified",
            user=token.user,
            object_type="User",
            object_identifier=str(token.user.id),
        )
        return Response({"detail": "Email verified"})


class ResendVerificationView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email=email).first()
        if not user or user.email_verified:
            return Response({"detail": "If account exists, verification email has been sent."})

        token = EmailVerificationToken.objects.create(
            user=user,
            token=EmailVerificationToken.generate_token(),
            expires_at=EmailVerificationToken.default_expires_at(),
        )
        verification_url = f"{request.build_absolute_uri('/')}verify-email/?token={token.token}"
        send_mail(
            subject="Verify your Schedula account",
            message=f"Use this link to verify your email: {verification_url}",
            from_email=None,
            recipient_list=[user.email],
        )
        return Response({"detail": "If account exists, verification email has been sent."})


class ForgotPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email=email).first()
        if user:
            token = default_token_generator.make_token(user)
            reset_url = (
                f"{request.build_absolute_uri('/')}reset-password/?uid={user.pk}&token={token}"
            )
            send_mail(
                subject="Reset your Schedula password",
                message=f"Use this link to reset your password: {reset_url}",
                from_email=None,
                recipient_list=[user.email],
            )
        return Response(
            {"detail": "If account exists, password reset instructions have been sent."}
        )


class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password changed successfully"})
