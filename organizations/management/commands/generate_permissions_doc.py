from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from organizations.permissions import PERMISSIONS_DOC_PATH, render_permissions_markdown


class Command(BaseCommand):
    help = "Regenerate docs/PERMISSIONS.md from the capability registry."

    def handle(self, *args, **options):
        path = Path(settings.BASE_DIR) / PERMISSIONS_DOC_PATH
        path.write_text(render_permissions_markdown(), encoding="utf-8", newline="\n")
        self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
