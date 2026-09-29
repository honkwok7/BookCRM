"""Web forms for the location screens. They only collect input: the plan limit, the default
location rules, hour overlaps and closure rules are applied by locations.services, whose errors
the views attach to the form."""

from __future__ import annotations

import zoneinfo

from django import forms

from locations.models import Location, Weekday
from locations.selectors import weekly_hours

FORM_FIELDS_BY_ERROR_CODE = {
    "name_required": "name",
    "invalid_timezone": "timezone",
    "default_location": "is_active",
    "invalid_dates": "end_date",
    "times_required": "start_time",
    "invalid_times": "end_time",
}


def timezone_choices():
    zones = sorted(
        zone for zone in zoneinfo.available_timezones() if "/" in zone and "Etc/" not in zone
    )
    return [("UTC", "UTC"), *((zone, zone.replace("_", " ")) for zone in zones)]


class ServiceErrorsMixin:
    def add_service_error(self, error) -> None:
        """Show a DomainError/ConflictError from locations.services next to its field."""
        field = FORM_FIELDS_BY_ERROR_CODE.get(getattr(error, "code", ""))
        self.add_error(field if field in self.fields else None, error.message)


class LocationForm(ServiceErrorsMixin, forms.Form):
    """Add (``include_status=False``) or edit a location."""

    name = forms.CharField(label="Name", max_length=120)
    address_line1 = forms.CharField(label="Address", max_length=255, required=False)
    address_line2 = forms.CharField(label="Address line 2", max_length=255, required=False)
    city = forms.CharField(label="City", max_length=120, required=False)
    region = forms.CharField(label="Province or state", max_length=120, required=False)
    postal_code = forms.CharField(label="Postal code", max_length=20, required=False)
    country = forms.CharField(
        label="Country code", max_length=2, required=False, help_text="Two letters, e.g. CA."
    )
    timezone = forms.ChoiceField(
        label="Time zone",
        choices=(),
        help_text="Opening hours and appointments at this location use this time zone.",
    )
    phone = forms.CharField(
        label="Phone", max_length=30, required=False, widget=forms.TextInput(attrs={"type": "tel"})
    )
    email = forms.EmailField(label="Email", required=False)
    booking_enabled = forms.BooleanField(
        label="Customers can book online at this location", required=False
    )
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Inactive locations are hidden from booking and don't count towards your "
        "plan's limit.",
    )

    ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "region", "postal_code", "country")
    CONTACT_FIELDS = ("phone", "email")

    def __init__(self, *args, include_status=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["timezone"].choices = timezone_choices()
        if not include_status:
            del self.fields["is_active"]

    @classmethod
    def initial_for(cls, location: Location) -> dict:
        names = ["name", "timezone", "booking_enabled", "is_active"]
        names += [*cls.ADDRESS_FIELDS, *cls.CONTACT_FIELDS]
        return {name: getattr(location, name) for name in names}

    def clean_country(self):
        return self.cleaned_data["country"].strip().upper()

    def service_fields(self) -> dict:
        return dict(self.cleaned_data)

    def sections(self):
        settings = [self["booking_enabled"]]
        if "is_active" in self.fields:
            settings.append(self["is_active"])
        return [
            ("Location", [self["name"], self["timezone"]]),
            ("Address", [self[name] for name in self.ADDRESS_FIELDS]),
            ("Contact", [self[name] for name in self.CONTACT_FIELDS]),
            ("Booking", settings),
        ]


class HoursForm(ServiceErrorsMixin, forms.Form):
    """The weekly opening hours: per weekday an "open" box and up to two periods.

    Field names: ``open_<d>``, ``opens_<d>``, ``closes_<d>``, ``opens2_<d>``, ``closes2_<d>``
    with ``d`` = 0 (Monday) … 6 (Sunday).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for day, label in Weekday.choices:
            self.fields[f"open_{day}"] = forms.BooleanField(
                label=f"Open on {label}", required=False
            )
            for prefix, text in (
                ("opens", "Opens"),
                ("closes", "Closes"),
                ("opens2", "Reopens"),
                ("closes2", "Closes again"),
            ):
                self.fields[f"{prefix}_{day}"] = forms.TimeField(
                    label=f"{text} on {label}",
                    required=False,
                    widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                )

    @classmethod
    def initial_for(cls, location: Location) -> dict:
        initial = {}
        for row in weekly_hours(location):
            day, periods = row["weekday"], row["periods"]
            initial[f"open_{day}"] = bool(periods)
            for index, (opens_at, closes_at) in enumerate(periods[:2]):
                suffix = "2" if index else ""
                initial[f"opens{suffix}_{day}"] = opens_at
                initial[f"closes{suffix}_{day}"] = closes_at
        return initial

    def clean(self):
        data = super().clean()
        for day, label in Weekday.choices:
            if not data.get(f"open_{day}"):
                continue
            opens, closes = data.get(f"opens_{day}"), data.get(f"closes_{day}")
            if opens is None or closes is None:
                self.add_error(f"opens_{day}", f"Enter the opening and closing times for {label}.")
            elif opens >= closes:
                self.add_error(f"closes_{day}", "Closing time must be after the opening time.")
            opens2, closes2 = data.get(f"opens2_{day}"), data.get(f"closes2_{day}")
            if (opens2 is None) != (closes2 is None):
                self.add_error(
                    f"opens2_{day}", "Enter both times of the second period, or neither."
                )
            elif opens2 is not None:
                if opens2 >= closes2:
                    self.add_error(f"closes2_{day}", "Closing time must be after reopening.")
                elif closes is not None and opens2 < closes:
                    self.add_error(
                        f"opens2_{day}", "The second period must start after the first ends."
                    )
        return data

    def periods(self) -> list[tuple]:
        data, periods = self.cleaned_data, []
        for day in Weekday.values:
            if not data.get(f"open_{day}"):
                continue
            periods.append((day, data[f"opens_{day}"], data[f"closes_{day}"]))
            if data.get(f"opens2_{day}") is not None:
                periods.append((day, data[f"opens2_{day}"], data[f"closes2_{day}"]))
        return periods

    def rows(self):
        """Per weekday, the bound fields in display order (for the template)."""
        return [
            {
                "label": label,
                "open": self[f"open_{day}"],
                "times": [
                    self[f"opens_{day}"],
                    self[f"closes_{day}"],
                    self[f"opens2_{day}"],
                    self[f"closes2_{day}"],
                ],
            }
            for day, label in Weekday.choices
        ]


class ClosureForm(ServiceErrorsMixin, forms.Form):
    start_date = forms.DateField(
        label="First day closed", widget=forms.DateInput(attrs={"type": "date"})
    )
    end_date = forms.DateField(
        label="Last day closed",
        required=False,
        help_text="Leave empty for a single day.",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    all_day = forms.BooleanField(label="Closed all day", required=False, initial=True)
    start_time = forms.TimeField(
        label="Closed from",
        required=False,
        help_text="Only for a partial closure.",
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    end_time = forms.TimeField(
        label="Reopens at",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    reason = forms.CharField(
        label="Reason",
        max_length=120,
        required=False,
        help_text="Shown to your team, e.g. “Renovation”.",
    )

    def service_fields(self) -> dict:
        return dict(self.cleaned_data)
