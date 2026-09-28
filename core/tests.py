from django.contrib.auth import get_user_model
from django.test import TestCase

from core.audit import AuditAction, record_audit
from core.middleware import get_current_request
from core.models import AuditLog


class RequestAuditMiddlewareTests(TestCase):
    def test_request_is_cleared_after_response(self):
        user = get_user_model().objects.create_user(
            email="audit@example.test", password="Audit12345!"
        )
        self.client.force_login(user)
        self.client.get("/health/")
        self.assertIsNone(get_current_request())

    def test_audit_outside_request_is_not_attributed_to_previous_requester(self):
        user = get_user_model().objects.create_user(
            email="audit@example.test", password="Audit12345!"
        )
        self.client.force_login(user)
        self.client.get("/health/")

        log = record_audit(AuditAction.SYSTEM_TEST)
        self.assertIsNone(log.user)
        self.assertIsNone(log.ip_address)
        self.assertEqual(AuditLog.objects.count(), 1)
