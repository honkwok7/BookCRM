"""M1.6 acceptance: the legacy booking domain and global user role are gone."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase


class LegacyRemovedTests(TestCase):
    def test_legacy_endpoints_are_gone(self):
        for url in ("/api/specialists/", "/api/appointments/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_legacy_apps_are_not_installed(self):
        for label in ("specialists", "appointments"):
            self.assertFalse(apps.is_installed(label))

    def test_no_legacy_tables_after_migrate(self):
        tables = connection.introspection.table_names()
        self.assertEqual([t for t in tables if t.startswith(("specialists_", "appointments_"))], [])

    def test_user_has_no_global_role(self):
        field_names = {field.name for field in get_user_model()._meta.get_fields()}
        self.assertNotIn("role", field_names)
