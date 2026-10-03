"""The production build steps the deployment runs (entrypoint.sh, docker-compose.prod.yml)."""

import tempfile
from pathlib import Path

from django.apps import apps
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from core.apps import CoreConfig

MANIFEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


class CollectStaticTests(SimpleTestCase):
    def test_it_runs_and_leaves_out_the_tailwind_input(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(STATIC_ROOT=root, STORAGES=MANIFEST_STORAGES):
                call_command("collectstatic", interactive=False, verbosity=0)
            collected = Path(root)
            self.assertTrue((collected / "dist" / "app.css").exists())
            self.assertTrue((collected / "admin").is_dir())
            self.assertFalse((collected / "src").exists())

    def test_core_keeps_its_own_app_config(self):
        self.assertIsInstance(apps.get_app_config("core"), CoreConfig)
