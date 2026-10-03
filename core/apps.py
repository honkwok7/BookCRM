from django.apps import AppConfig
from django.contrib.staticfiles.apps import StaticFilesConfig as StaticFilesConfig_


class CoreConfig(AppConfig):
    default = True
    name = "core"


class StaticFilesConfig(StaticFilesConfig_):
    """collectstatic skips static/src/: the Tailwind input is read by the build only
    (manage.py tailwind build), and its @import "tailwindcss" isn't a file to serve."""

    ignore_patterns = [*StaticFilesConfig_.ignore_patterns, "src/*"]
