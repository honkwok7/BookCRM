"""Web forms for the staff screens. They only collect input: membership, the plan's staff
limit, same-organization checks and auditing are applied by staff.services, whose errors the
views attach to the form."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from django import forms
from django.contrib.auth import get_user_model

from locations.models import Location, Weekday
from organizations.models import OrganizationMembership, OrganizationRole
from scheduling.models import WeeklyAvailability
from services.models import Service
from staff.models import StaffProfile
from staff.services import OfferingChoice

FORM_FIELDS_BY_ERROR_CODE = {
    "not_a_member": "user",
    "duplicate": "user",
    "invalid_location": "locations",
    "invalid_limit": "max_daily_appointments",
}


class ServiceErrorsMixin:
    def add_service_error(self, error) -> None:
        field = FORM_FIELDS_BY_ERROR_CODE.get(getattr(error, "code", ""))
        self.add_error(field if field in self.fields else None, error.message)


class MemberChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        name = user.get_full_name()
        return f"{name} ({user.email})" if name else user.email


class LocationsField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def label_from_instance(self, location):
        return location.name


def active_locations(organization):
    return Location.objects.filter(organization=organization, is_active=True).order_by(
        "-is_default", "name"
    )


class ProfileFieldsMixin(forms.Form):
    display_name = forms.CharField(
        label="Name shown to customers",
        max_length=120,
        required=False,
        help_text="Leave empty to use their full name. Email addresses are never shown.",
    )
    job_title = forms.CharField(label="Job title", max_length=120, required=False)
    provider_type = forms.CharField(
        label="Provider type",
        max_length=60,
        required=False,
        help_text="For example “Massage therapist”. Services can require a type later.",
    )


class StaffCreateForm(ServiceErrorsMixin, ProfileFieldsMixin):
    user = MemberChoiceField(label="Team member", queryset=None, empty_label="Choose a person")
    locations = LocationsField(label="Works at", queryset=None, required=False)

    field_order = ["user", "display_name", "job_title", "provider_type", "locations"]

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        taken = StaffProfile.objects.filter(organization=organization).values("user_id")
        member_ids = OrganizationMembership.objects.filter(
            organization=organization, is_active=True
        ).exclude(role=OrganizationRole.CUSTOMER)
        self.fields["user"].queryset = (
            get_user_model()
            .objects.filter(pk__in=member_ids.values("user_id"))
            .exclude(pk__in=taken)
            .order_by("first_name", "last_name", "email")
        )
        self.fields["locations"].queryset = active_locations(organization)
        self.fields["locations"].initial = [
            location.pk for location in active_locations(organization) if location.is_default
        ]

    def service_fields(self) -> dict:
        return dict(self.cleaned_data)


class StaffProfileForm(ServiceErrorsMixin, ProfileFieldsMixin):
    bio = forms.CharField(
        label="Bio", required=False, widget=forms.Textarea(attrs={"rows": 3}), max_length=2000
    )
    phone_number = forms.CharField(
        label="Phone",
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={"type": "tel"}),
        help_text="For the team only.",
    )
    max_daily_appointments = forms.IntegerField(
        label="Most appointments a day",
        min_value=1,
        required=False,
        help_text="Leave empty for no limit.",
    )
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Inactive staff can't be booked and don't count towards your plan's limit.",
    )
    is_accepting_bookings = forms.BooleanField(label="Accepting new appointments", required=False)
    online_booking_visible = forms.BooleanField(
        label="Customers can book them online",
        required=False,
        help_text="Off: only your team can book appointments with them.",
    )

    FIELDS = (
        "display_name",
        "job_title",
        "provider_type",
        "bio",
        "phone_number",
        "max_daily_appointments",
        "is_active",
        "is_accepting_bookings",
        "online_booking_visible",
    )

    @classmethod
    def initial_for(cls, staff: StaffProfile) -> dict:
        return {name: getattr(staff, name) for name in cls.FIELDS}

    def service_fields(self) -> dict:
        return dict(self.cleaned_data)

    def sections(self):
        return [
            ("Profile", [self[name] for name in ("display_name", "job_title", "provider_type")]),
            ("About", [self["bio"], self["phone_number"]]),
            (
                "Booking",
                [
                    self[name]
                    for name in (
                        "is_active",
                        "is_accepting_bookings",
                        "online_booking_visible",
                        "max_daily_appointments",
                    )
                ],
            ),
        ]


class StaffLocationsForm(ServiceErrorsMixin, forms.Form):
    locations = LocationsField(
        label="Works at",
        queryset=None,
        required=False,
        help_text="Removing a location also removes the services they offer only there.",
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["locations"].queryset = active_locations(organization)


class StaffServicesForm(ServiceErrorsMixin, forms.Form):
    """Per service: where the person offers it (one box per location they work at) and
    optional custom duration and price. Ticking every location stores "all their locations",
    which also covers locations they are added to later."""

    def __init__(self, *args, staff: StaffProfile, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff = staff
        # Every location they work at, inactive ones too: saving must not drop offerings at a
        # location that is only closed for now.
        self.locations = list(staff.locations.order_by("-is_default", "name"))
        self.services = list(
            Service.objects.filter(organization=staff.organization, is_archived=False)
            .select_related("category")
            .order_by("category__name", "name")
        )
        for n, service in enumerate(self.services):
            for m, location in enumerate(self.locations):
                label = (
                    f"Offers {service.name}"
                    if len(self.locations) == 1
                    else f"Offers {service.name} at {location.name}"
                )
                self.fields[f"s{n}_l{m}"] = forms.BooleanField(label=label, required=False)
            self.fields[f"s{n}_duration"] = forms.IntegerField(
                label=f"Their duration for {service.name} (minutes)",
                min_value=1,
                required=False,
                widget=forms.NumberInput(attrs={"placeholder": str(service.duration_minutes)}),
            )
            self.fields[f"s{n}_price"] = forms.DecimalField(
                label=f"Their price for {service.name}",
                min_value=0,
                max_digits=12,
                decimal_places=2,
                required=False,
                widget=forms.NumberInput(attrs={"placeholder": f"{service.price}", "step": "0.01"}),
            )

    @classmethod
    def initial_for(cls, staff: StaffProfile) -> dict:
        form = cls(staff=staff)
        location_index = {location.pk: m for m, location in enumerate(form.locations)}
        by_service: dict = {}
        for offering in staff.offerings.filter(is_active=True):
            by_service.setdefault(offering.service_id, []).append(offering)
        initial = {}
        for n, service in enumerate(form.services):
            offerings = by_service.get(service.pk, [])
            for offering in offerings:
                if offering.location_id is None:
                    for m in range(len(form.locations)):
                        initial[f"s{n}_l{m}"] = True
                elif offering.location_id in location_index:
                    initial[f"s{n}_l{location_index[offering.location_id]}"] = True
            if offerings:
                initial[f"s{n}_duration"] = offerings[0].custom_duration_minutes
                initial[f"s{n}_price"] = offerings[0].custom_price
        return initial

    def choices(self) -> list[OfferingChoice]:
        data, choices = self.cleaned_data, []
        for n, service in enumerate(self.services):
            ticked = [
                location for m, location in enumerate(self.locations) if data.get(f"s{n}_l{m}")
            ]
            if not ticked:
                continue
            everywhere = len(ticked) == len(self.locations)
            choices.append(
                OfferingChoice(
                    service=service,
                    locations=None if everywhere else tuple(ticked),
                    custom_duration_minutes=data.get(f"s{n}_duration"),
                    custom_price=data.get(f"s{n}_price"),
                )
            )
        return choices

    def rows(self):
        return [
            {
                "service": service,
                "boxes": [
                    {"field": self[f"s{n}_l{m}"], "location": location}
                    for m, location in enumerate(self.locations)
                ],
                "duration": self[f"s{n}_duration"],
                "price": self[f"s{n}_price"],
            }
            for n, service in enumerate(self.services)
        ]


# -- Provider area (M5.3): time off and blocked time ----------------------------------------


def wall_time(day, at, zone) -> datetime:
    """``day`` at ``at`` in ``zone``, in UTC. A time the clocks skip (spring forward) is an
    error; a time that happens twice (fall back) is the first of the two."""
    local = datetime.combine(day, at, tzinfo=zone)
    utc = local.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise forms.ValidationError(
            f"{at:%H:%M} doesn't exist on {day.day} {day:%b}: the clocks change then."
        )
    return utc


class BlockTimeForm(forms.Form):
    """Part of one day, in the organization's time zone."""

    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.TimeField(label="From", widget=forms.TimeInput(attrs={"type": "time"}))
    end = forms.TimeField(label="Until", widget=forms.TimeInput(attrs={"type": "time"}))
    reason = forms.CharField(required=False, max_length=255, help_text="For example: lunch.")

    def __init__(self, *args, zone, **kwargs):
        super().__init__(*args, prefix="block", **kwargs)
        self.zone = zone

    def clean(self):
        data = super().clean()
        if all(data.get(name) for name in ("date", "start", "end")):
            try:
                self._period = (
                    wall_time(data["date"], data["start"], self.zone),
                    wall_time(data["date"], data["end"], self.zone),
                )
            except forms.ValidationError as error:
                self.add_error(None, error)
        return data

    def period(self):
        return self._period


class TimeOffRequestForm(forms.Form):
    """Whole days, first to last, in the organization's time zone."""

    first_day = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    last_day = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(required=False, max_length=255)

    def __init__(self, *args, zone, **kwargs):
        super().__init__(*args, prefix="request", **kwargs)
        self.zone = zone

    def clean(self):
        data = super().clean()
        if data.get("first_day") and data.get("last_day") and data["last_day"] < data["first_day"]:
            self.add_error("last_day", "The last day can't be before the first.")
        return data

    def period(self):
        data = self.cleaned_data
        return (
            wall_time(data["first_day"], time.min, self.zone),
            wall_time(data["last_day"] + timedelta(days=1), time.min, self.zone),
        )


# -- Working hours ----------------------------------------------------------------------------

ANY_LOCATION = ""


class WeeklyHoursForm(forms.Form):
    """A provider's weekly hours: per weekday a "working" box, where, the hours and an optional
    second period (e.g. after a lunch break).

    Field names: ``on_<d>``, ``where_<d>``, ``start_<d>``, ``end_<d>``, ``start2_<d>``,
    ``end2_<d>`` with ``d`` = 0 (Monday) … 6 (Sunday). ``where`` is a location id, or empty for
    any of their locations.
    """

    def __init__(self, *args, staff, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff = staff
        self.locations = {str(location.pk): location for location in staff.locations.all()}
        where_choices = [(ANY_LOCATION, "Any of their locations")] + [
            (pk, location.name) for pk, location in self.locations.items()
        ]
        self.days = []
        for day, label in Weekday.choices:
            self.fields[f"on_{day}"] = forms.BooleanField(
                label=f"Working on {label}", required=False
            )
            self.fields[f"where_{day}"] = forms.ChoiceField(
                label=f"Where on {label}", choices=where_choices, required=False
            )
            for prefix, text in (
                ("start", "Starts"),
                ("end", "Ends"),
                ("start2", "Starts again"),
                ("end2", "Ends again"),
            ):
                self.fields[f"{prefix}_{day}"] = forms.TimeField(
                    label=f"{text} on {label}",
                    required=False,
                    widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                )
            self.days.append(
                {
                    "label": label,
                    "on": self[f"on_{day}"],
                    "where": self[f"where_{day}"],
                    "times": [self[f"{p}_{day}"] for p in ("start", "end", "start2", "end2")],
                }
            )
        # Weekly hours this form can't show exactly (more than two periods on a day, or
        # different places on one day): saving replaces them, so the page says so.
        self.reshaped_days = []

    @classmethod
    def initial_for(cls, staff) -> tuple[dict, list[str]]:
        initial, reshaped = {}, []
        rows = WeeklyAvailability.objects.filter(staff=staff, is_active=True).order_by(
            "day_of_week", "start_time"
        )
        by_day: dict[int, list] = {}
        for row in rows:
            by_day.setdefault(row.day_of_week, []).append(row)
        for day, day_rows in by_day.items():
            initial[f"on_{day}"] = True
            initial[f"where_{day}"] = str(day_rows[0].location_id or ANY_LOCATION)
            for index, row in enumerate(day_rows[:2]):
                suffix = "2" if index else ""
                initial[f"start{suffix}_{day}"] = row.start_time
                initial[f"end{suffix}_{day}"] = row.end_time
            if len(day_rows) > 2 or len({row.location_id for row in day_rows}) > 1:
                reshaped.append(dict(Weekday.choices)[day])
        return initial, reshaped

    def copied_from_monday(self) -> dict:
        """The submitted values with Monday's copied to Tuesday-Friday (not saved)."""
        data = self.data.copy()
        for day in range(1, 5):
            for prefix in ("on", "where", "start", "end", "start2", "end2"):
                value = self.data.get(f"{prefix}_0")
                if value is None:
                    data.pop(f"{prefix}_{day}", None)
                else:
                    data[f"{prefix}_{day}"] = value
        return data

    def clean(self):
        data = super().clean()
        for day, label in Weekday.choices:
            if not data.get(f"on_{day}"):
                continue
            start, end = data.get(f"start_{day}"), data.get(f"end_{day}")
            if start is None or end is None:
                self.add_error(f"start_{day}", f"Enter the start and end times for {label}.")
            elif start >= end:
                self.add_error(f"end_{day}", "The end must be after the start.")
            start2, end2 = data.get(f"start2_{day}"), data.get(f"end2_{day}")
            if (start2 is None) != (end2 is None):
                self.add_error(
                    f"start2_{day}", "Enter both times of the second period, or neither."
                )
            elif start2 is not None:
                if start2 >= end2:
                    self.add_error(f"end2_{day}", "The end must be after the start.")
                elif end is not None and start2 < end:
                    self.add_error(
                        f"start2_{day}", "The second period must start after the first ends."
                    )
        return data

    def periods(self) -> list[tuple]:
        data, periods = self.cleaned_data, []
        for day in Weekday.values:
            if not data.get(f"on_{day}"):
                continue
            where = self.locations.get(data.get(f"where_{day}") or "")
            periods.append((day, data[f"start_{day}"], data[f"end_{day}"], where))
            if data.get(f"start2_{day}") is not None:
                periods.append((day, data[f"start2_{day}"], data[f"end2_{day}"], where))
        return periods


class SpecialDayForm(forms.Form):
    """One date: off all day, or working only certain hours."""

    OFF, HOURS = "off", "hours"

    day = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    kind = forms.ChoiceField(
        label="That day",
        choices=((OFF, "Not working"), (HOURS, "Working only these hours")),
        widget=forms.RadioSelect,
        initial=OFF,
    )
    start = forms.TimeField(
        label="From",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    end = forms.TimeField(
        label="To",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    reason = forms.CharField(label="Reason", required=False, max_length=255)

    def clean(self):
        data = super().clean()
        if data.get("kind") == self.HOURS:
            start, end = data.get("start"), data.get("end")
            if start is None or end is None:
                self.add_error("start", "Enter the hours they work that day.")
            elif start >= end:
                self.add_error("end", "The end must be after the start.")
        return data

    def service_fields(self) -> dict:
        data = self.cleaned_data
        return {
            "day": data["day"],
            "off_all_day": data["kind"] == self.OFF,
            "start": data.get("start"),
            "end": data.get("end"),
            "reason": data.get("reason", ""),
        }
