"""Brand colours for an organization's public booking pages.

A fixed set of palettes rather than any colour: each one's button shade keeps white text at
WCAG AA contrast (4.5:1 or more). ``theme_css`` overrides the ``--color-brand-*`` variables
the stylesheet's ``brand`` utilities use, so buttons, links and focus rings follow the brand.
"""

from django.db import models


class BrandColor(models.TextChoices):
    DEFAULT = "", "Default (indigo)"
    BLUE = "blue", "Blue"
    TEAL = "teal", "Teal"
    GREEN = "green", "Green"
    ROSE = "rose", "Rose"
    VIOLET = "violet", "Violet"
    ORANGE = "orange", "Orange"
    SLATE = "slate", "Slate"


SHADES = ("50", "100", "200", "500", "600", "700", "800")
PALETTES = {
    "blue": ("#eff6ff", "#dbeafe", "#bfdbfe", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af"),
    "teal": ("#f0fdfa", "#ccfbf1", "#99f6e4", "#14b8a6", "#0f766e", "#115e59", "#134e4a"),
    "green": ("#ecfdf5", "#d1fae5", "#a7f3d0", "#10b981", "#047857", "#065f46", "#064e3b"),
    "rose": ("#fff1f2", "#ffe4e6", "#fecdd3", "#f43f5e", "#e11d48", "#be123c", "#9f1239"),
    "violet": ("#f5f3ff", "#ede9fe", "#ddd6fe", "#8b5cf6", "#7c3aed", "#6d28d9", "#5b21b6"),
    "orange": ("#fff7ed", "#ffedd5", "#fed7aa", "#f97316", "#c2410c", "#9a3412", "#7c2d12"),
    "slate": ("#f8fafc", "#f1f5f9", "#e2e8f0", "#64748b", "#475569", "#334155", "#1e293b"),
}


def theme_css(color: str) -> str:
    """CSS for the palette ``color`` (empty for the default palette or an unknown name)."""
    palette = PALETTES.get(color)
    if palette is None:
        return ""
    variables = "".join(
        f"--color-brand-{shade}:{value};" for shade, value in zip(SHADES, palette, strict=True)
    )
    return f":root{{{variables}}}\n"
