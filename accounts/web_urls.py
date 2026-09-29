from django.urls import path

from accounts import web_views

urlpatterns = [
    path("login/", web_views.LoginView.as_view(), name="login"),
    path("logout/", web_views.LogoutView.as_view(), name="logout"),
    path(
        "password-reset/",
        web_views.PasswordResetRequestView.as_view(),
        name="password-reset",
    ),
    path(
        "reset-password/",
        web_views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("verify-email/", web_views.VerifyEmailView.as_view(), name="verify-email"),
]
