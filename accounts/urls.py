from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from accounts.views import (
    ForgotPasswordView,
    LoginView,
    LogoutView,
    PasswordResetConfirmView,
    ProfileView,
    RegisterView,
    ResendVerificationView,
    VerifyEmailView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="api-register"),
    path("login/", LoginView.as_view(), name="api-login"),
    path("logout/", LogoutView.as_view(), name="api-logout"),
    path("verify-email/", VerifyEmailView.as_view(), name="api-verify-email"),
    path("resend-verification/", ResendVerificationView.as_view(), name="api-resend-verification"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="api-forgot-password"),
    path("reset-password/", PasswordResetConfirmView.as_view(), name="api-reset-password"),
    path("profile/", ProfileView.as_view(), name="api-profile"),
    path("token/refresh/", TokenRefreshView.as_view(), name="api-token-refresh"),
]
