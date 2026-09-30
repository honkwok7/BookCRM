"""Web forms for the staff screens. They only collect input: membership, the plan's staff
limit, same-organization checks and auditing are applied by staff.services, whose errors the
views attach to the form."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django import forms
from django.contrib.auth import get_user_model

from locations.models import Location
from organizations.models import OrganizationMembership, OrganizationRole
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


class BlockTimeForm(forms.Form):
    """Part of one day, in the organization's time zone."""

    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.TimeField(label="From", widget=forms.TimeInput(attrs={"type": "time"}))
    end = forms.TimeField(label="Until", widget=forms.TimeInput(attrs={"type": "time"}))
    reason = forms.CharField(required=False, max_length=255, help_text="For example: lunch.")

    def __init__(self, *args, zone, **kwargs):
        super().__init__(*args, prefix="block", **kwargs)
        self.zone = zone

    def period(self):
        data = self.cleaned_data
        return (
            datetime.combine(data["date"], data["start"], tzinfo=self.zone),
            datetime.combine(data["date"], data["end"], tzinfo=self.zone),
        )


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
            datetime.combine(data["first_day"], time.min, tzinfo=self.zone),
            datetime.combine(data["last_day"] + timedelta(days=1), time.min, tzinfo=self.zone),
        )
