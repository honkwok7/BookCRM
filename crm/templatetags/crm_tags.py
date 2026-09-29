"""Template helpers for the CRM screens."""

from django import template

register = template.Library()

# Tag colours are free hex values chosen by users. Inline styles are blocked by the Content
# Security Policy, so a tag is shown with the nearest colour of this fixed palette. The classes
# are listed for Tailwind in static/src/app.css (@source inline).
PALETTE = {
    "red": "ef4444",
    "orange": "f97316",
    "amber": "f59e0b",
    "yellow": "eab308",
    "lime": "84cc16",
    "green": "22c55e",
    "emerald": "10b981",
    "teal": "14b8a6",
    "cyan": "06b6d4",
    "sky": "0ea5e9",
    "blue": "3b82f6",
    "indigo": "6366f1",
    "violet": "8b5cf6",
    "purple": "a855f7",
    "fuchsia": "d946ef",
    "pink": "ec4899",
    "rose": "f43f5e",
    "slate": "64748b",
}


def _rgb(hex_value: str) -> tuple[int, int, int]:
    return tuple(int(hex_value[i : i + 2], 16) for i in (0, 2, 4))


PALETTE_RGB = {name: _rgb(value) for name, value in PALETTE.items()}


@register.filter
def tag_dot_class(tag) -> str:
    color = (getattr(tag, "color", "") or "").lstrip("#").lower()
    if len(color) != 6 or any(c not in "0123456789abcdef" for c in color):
        return "bg-slate-500"
    rgb = _rgb(color)
    name = min(
        PALETTE_RGB,
        key=lambda n: sum((a - b) ** 2 for a, b in zip(PALETTE_RGB[n], rgb, strict=True)),
    )
    return f"bg-{name}-500"
