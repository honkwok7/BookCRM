from django.conf import settings


def pytest_configure(config):
    # Production serves hashed file names from the collectstatic manifest (WhiteNoise). Tests
    # don't run collectstatic, so templates resolve {% static %} to the plain file names.
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
