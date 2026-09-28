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
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path("resend-verification/", ResendVerificationView.as_view(), name="resend-verification"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot-password"),
    path("reset-password/", PasswordResetConfirmView.as_view(), name="reset-password"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
]
