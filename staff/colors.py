"""Providers' appointment colours on the calendar.

``StaffProfile.appointment_color`` holds one of the names below, or "" for automatic. Each
name's position is its colour class: ``.cal-color-<n>`` (appointments) and ``.cal-swatch-<n>``
(the picker and legend) in static/src/app.css. Automatic colours follow the order staff were
added, skipping colours someone has chosen, so a provider keeps the same colour on every page.
"""

from __future__ import annotations

APPOINTMENT_COLORS = (
    ("sky", "Sky blue"),
    ("emerald", "Green"),
    ("violet", "Violet"),
    ("amber", "Amber"),
    ("rose", "Rose"),
    ("teal", "Teal"),
    ("indigo", "Indigo"),
    ("orange", "Orange"),
    ("lime", "Lime"),
    ("fuchsia", "Pink"),
    ("cyan", "Cyan"),
    ("stone", "Grey"),
)
COLOR_COUNT = len(APPOINTMENT_COLORS)
COLOR_INDEX = {name: index for index, (name, _label) in enumerate(APPOINTMENT_COLORS)}
COLOR_CHOICES = (("", "Automatic"), *APPOINTMENT_COLORS)


def provider_colors(organization) -> dict:
    """``{staff_pk: colour index}`` for every provider in the organization."""
    from staff.models import StaffProfile

    staff = list(
        StaffProfile.objects.filter(organization=organization)
        .order_by("created_at", "pk")
        .values_list("pk", "appointment_color")
    )
    colors = {pk: COLOR_INDEX[name] for pk, name in staff if name in COLOR_INDEX}
    taken = set(colors.values())
    free = [index for index in range(COLOR_COUNT) if index not in taken] or list(range(COLOR_COUNT))
    automatic = [pk for pk, name in staff if pk not in colors]
    for position, pk in enumerate(automatic):
        colors[pk] = free[position % len(free)]
    return colors
