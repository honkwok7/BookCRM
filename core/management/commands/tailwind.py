"""Build the stylesheet with the Tailwind standalone CLI (no Node.js required).

    python manage.py tailwind build      # static/src/app.css -> static/dist/app.css (minified)
    python manage.py tailwind watch      # rebuild on template changes while developing

The CLI binary is downloaded once into ``.tailwind/`` (git-ignored) for the pinned version and
verified against the SHA-256 checksums published with that release, so an upgrade is a
deliberate change to ``VERSION`` and ``CHECKSUMS``. The built CSS is committed, so running the
app or the tests never needs the CLI; CI rebuilds it and fails when the committed file is stale.
"""

from __future__ import annotations

import hashlib
import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

VERSION = "v4.3.3"
RELEASE_URL = "https://github.com/tailwindlabs/tailwindcss/releases/download/{version}/{asset}"
# From the release's sha256sums.txt.
CHECKSUMS = {
    "tailwindcss-linux-arm64": "55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195",
    "tailwindcss-linux-x64": "dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a",
    "tailwindcss-macos-arm64": "cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d",
    "tailwindcss-macos-x64": "7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1",
    "tailwindcss-windows-x64.exe": (
        "e0e260ce048014e9268f6237ff18f8ccf02cef521cbd0ae04e82c2cdf7aa3955"
    ),
}
INPUT = Path("static/src/app.css")
OUTPUT = Path("static/dist/app.css")


def asset_name() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if sys.platform.startswith("win"):
        return f"tailwindcss-windows-{arch}.exe"
    if sys.platform == "darwin":
        return f"tailwindcss-macos-{arch}"
    return f"tailwindcss-linux-{arch}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Build static/dist/app.css with the pinned Tailwind standalone CLI."

    def add_arguments(self, parser):
        parser.add_argument("mode", choices=["build", "watch"], nargs="?", default="build")

    def handle(self, *args, mode, **options):
        base = Path(settings.BASE_DIR)
        binary = self.ensure_binary(base / ".tailwind")
        command = [str(binary), "-i", str(base / INPUT), "-o", str(base / OUTPUT)]
        command.append("--watch" if mode == "watch" else "--minify")
        self.stdout.write(" ".join(command))
        result = subprocess.run(command, cwd=base, check=False)
        if result.returncode:
            raise CommandError(f"Tailwind exited with status {result.returncode}")

    def ensure_binary(self, directory: Path) -> Path:
        asset = asset_name()
        expected = CHECKSUMS.get(asset)
        if expected is None:
            raise CommandError(f"No pinned Tailwind CLI for this platform ({asset}).")
        binary = directory / f"{VERSION}-{asset}"
        if binary.exists() and sha256(binary) == expected:
            return binary

        directory.mkdir(exist_ok=True)
        url = RELEASE_URL.format(version=VERSION, asset=asset)
        self.stdout.write(f"Downloading {url}")
        partial = binary.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        actual = sha256(partial)
        if actual != expected:
            partial.unlink()
            raise CommandError(f"Checksum mismatch for {asset}: expected {expected}, got {actual}")
        partial.replace(binary)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return binary
