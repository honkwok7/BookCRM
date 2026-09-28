"""Account flow hardening (codebase review, 2026-09-28).

- Password rules (AUTH_PASSWORD_VALIDATORS) apply to registration and password reset.
- Emails are queued to Celery after commit, carry only ids, and build links from SITE_URL,
  never from the request's Host header.
- A password reset signs the user out everywhere (refresh tokens are blacklisted).
- Login and password-reset endpoints have their own rate limits.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from accounts.models import EmailVerificationToken
from accounts.tasks import send_password_reset_email, send_verification_email
from tests import factories as f

User = get_user_model()
STRONG = "Correct-Horse-Battery-9"


def register_payload(**overrides):
    return {
        "email": "new@example.test",
        "password": STRONG,
        "accept_terms": True,
        "accept_privacy": True,
        **overrides,
    }


class RegistrationTests(APITestCase):
    def test_password_validators_apply(self):
        for weak in ("short1", "password123", "1234567890", "new@example.test"):
            with self.subTest(password=weak):
                response = self.client.post(
                    "/api/register/", register_payload(password=weak), format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("password", response.data)
        self.assertFalse(User.objects.exists())

    def test_email_is_unique_ignoring_case(self):
        f.UserFactory(email="sam@example.test")
        response = self.client.post(
            "/api/register/", register_payload(email="SAM@example.test"), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_terms_are_required(self):
        response = self.client.post(
            "/api/register/", register_payload(accept_terms=False), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.exists())

    @override_settings(SITE_URL="https://app.example")
    @mock.patch("accounts.services.send_verification_email.delay")
    def test_registration_queues_a_verification_email_after_commit(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/register/", register_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        token = EmailVerificationToken.objects.get(user__email="new@example.test")
        delay.assert_called_once_with(token_id=str(token.pk))  # only an id crosses the broker

        send_verification_email.apply(kwargs=delay.call_args.kwargs).get()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"https://app.example/verify-email/?token={token.token}", mail.outbox[0].body)

        response = self.client.post("/api/verify-email/", {"token": token.token}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.get(email="new@example.test").email_verified)

    @mock.patch(
        "accounts.services.send_verification_email.delay", side_effect=ConnectionError("down")
    )
    def test_broker_outage_does_not_fail_registration(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/register/", register_payload(), format="json")
        self.assertEqual(response.status_code, 201)


class PasswordResetTests(APITestCase):
    def setUp(self):
        self.user = f.UserFactory(email="ada@example.test")

    def reset(self, password, token=None):
        if token is None:
            self.user.refresh_from_db()  # the token covers last_login, which login updates
            token = default_token_generator.make_token(self.user)
        return self.client.post(
            "/api/reset-password/",
            {
                "uid": self.user.pk,
                "token": token,
                "new_password": password,
            },
            format="json",
        )

    @override_settings(SITE_URL="https://app.example")
    @mock.patch("accounts.services.send_password_reset_email.delay")
    def test_forgot_password_emails_a_link_on_the_configured_site(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/forgot-password/", {"email": "ADA@example.test"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        delay.assert_called_once_with(user_id=self.user.pk)
        send_password_reset_email.apply(kwargs=delay.call_args.kwargs).get()
        self.assertIn(
            f"https://app.example/reset-password/?uid={self.user.pk}&token=", mail.outbox[0].body
        )

    @mock.patch("accounts.services.send_password_reset_email.delay")
    def test_unknown_email_gets_the_same_answer_and_no_email(self, delay):
        known = self.client.post("/api/forgot-password/", {"email": "ada@example.test"})
        unknown = self.client.post("/api/forgot-password/", {"email": "nobody@example.test"})
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.data, unknown.data)

    def test_reset_applies_password_validators(self):
        response = self.reset("password123")
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.data)

    def test_bad_token_or_unknown_user_look_the_same(self):
        bad_token = self.reset(STRONG, token="nope")
        self.user.pk = 999999
        unknown_user = self.reset(STRONG, token="nope")
        self.assertEqual(bad_token.status_code, 400)
        self.assertEqual(bad_token.data, unknown_user.data)

    def test_reset_signs_the_user_out_everywhere(self):
        login = self.client.post(
            "/api/login/",
            {"email": self.user.email, "password": f.DEFAULT_PASSWORD},
            format="json",
        )
        refresh = login.data["refresh"]
        self.assertEqual(self.reset(STRONG).status_code, 200)

        response = self.client.post("/api/token/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 401)
        login = self.client.post(
            "/api/login/", {"email": self.user.email, "password": STRONG}, format="json"
        )
        self.assertEqual(login.status_code, 200)


class RateLimitTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_login_has_its_own_rate_limit(self):
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"login": "2/min"}):
            codes = [
                self.client.post(
                    "/api/login/", {"email": "x@example.test", "password": "wrong"}
                ).status_code
                for _ in range(3)
            ]
        self.assertEqual(codes, [401, 401, 429])

    def test_password_reset_has_its_own_rate_limit(self):
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"password_reset": "1/min"}):
            codes = [
                self.client.post("/api/forgot-password/", {"email": "x@example.test"}).status_code
                for _ in range(2)
            ]
        self.assertEqual(codes, [200, 429])
