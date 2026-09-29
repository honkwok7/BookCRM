"""Web forms for the services screen. They only collect input: unique names and slugs, the plan's
service limit, value checks and auditing are applied by services.services, whose errors the
views attach to the form."""

from __future__ import annotations

from django import forms

from locations.models import Location
from services.models import Service, ServiceCategory
from staff.models import StaffProfile

FORM_FIELDS_BY_ERROR_CODE = {
    "name_required": "name",
    "duplicate": "name",
    "invalid_duration": "duration_minutes",
    "invalid_price": "price",
    "invalid_tax_rate": "tax_rate",
    "invalid_category": "category",
    "invalid_location": "locations",
    "invalid_color": "color",
}


class ServiceErrorsMixin:
    def add_service_error(self, error) -> None:
        field = FORM_FIELDS_BY_ERROR_CODE.get(getattr(error, "code", ""))
        self.add_error(field if field in self.fields else None, error.message)


class LocationsField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def label_from_instance(self, location):
        return location.name if location.is_active else f"{location.name} (inactive)"


class ServiceForm(ServiceErrorsMixin, forms.Form):
    name = forms.CharField(label="Name", max_length=255)
    category = forms.ModelChoiceField(
        label="Category", queryset=None, required=False, empty_label="No category"
    )
    description = forms.CharField(
        label="Description",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown to customers when they book.",
    )
    duration_minutes = forms.IntegerField(label="Duration (minutes)", min_value=1)
    price = forms.DecimalField(label="Price", min_value=0, max_digits=12, decimal_places=2)
    tax_rate = forms.DecimalField(
        label="Tax rate (%)",
        min_value=0,
        max_value=100,
        max_digits=5,
        decimal_places=2,
        required=False,
        help_text="Recorded for now; invoices and receipts come later.",
    )
    buffer_before_minutes = forms.IntegerField(
        label="Buffer before (minutes)",
        min_value=0,
        required=False,
        help_text="Free time kept before each appointment, e.g. to prepare the room.",
    )
    buffer_after_minutes = forms.IntegerField(
        label="Buffer after (minutes)", min_value=0, required=False
    )
    locations = LocationsField(
        label="Offered at",
        queryset=None,
        required=False,
        help_text="Leave every box empty to offer it at all locations, including new ones.",
    )
    required_provider_type = forms.CharField(
        label="Only providers of type",
        max_length=60,
        required=False,
        help_text="For example “Massage therapist”. Leave empty to let anyone offer it.",
    )
    is_public = forms.BooleanField(label="Customers can book it online", required=False)
    is_active = forms.BooleanField(
        label="Active", required=False, help_text="Inactive services can't be booked."
    )
    min_notice_minutes = forms.IntegerField(
        label="Minimum notice (minutes)",
        min_value=0,
        help_text="How long before the start an appointment can still be booked online.",
    )
    max_advance_days = forms.IntegerField(
        label="Book up to (days ahead)",
        min_value=1,
        help_text="How far ahead online booking is open.",
    )
    cancellation_deadline_hours = forms.IntegerField(
        label="Cancel up to (hours before)", min_value=0
    )
    rescheduling_deadline_hours = forms.IntegerField(
        label="Reschedule up to (hours before)", min_value=0
    )
    cancellation_policy = forms.CharField(
        label="Cancellation policy",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown to customers, e.g. “Cancellations within 24 hours are charged 50%.”",
    )
    is_archived = forms.BooleanField(
        label="Archived",
        required=False,
        help_text="Hidden everywhere and not counted towards your plan; appointment history "
        "is kept.",
    )

    SECTIONS = (
        ("Service", ("name", "category", "description")),
        (
            "Time and price",
            (
                "duration_minutes",
                "price",
                "tax_rate",
                "buffer_before_minutes",
                "buffer_after_minutes",
            ),
        ),
        ("Where and who", ("locations", "required_provider_type")),
        ("Online booking", ("is_public", "is_active", "min_notice_minutes", "max_advance_days")),
        (
            "Changes and cancellations",
            ("cancellation_deadline_hours", "rescheduling_deadline_hours", "cancellation_policy"),
        ),
        ("Archive", ("is_archived",)),
    )
    NUMBER_DEFAULTS = {"tax_rate": 0, "buffer_before_minutes": 0, "buffer_after_minutes": 0}

    def __init__(self, *args, organization, include_archive=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ServiceCategory.objects.filter(
            organization=organization
        ).order_by("sort_order", "name")
        self.fields["locations"].queryset = Location.objects.filter(
            organization=organization
        ).order_by("-is_default", "name")
        if not include_archive:
            del self.fields["is_archived"]
        types = sorted(
            {
                value.strip()
                for value in StaffProfile.objects.filter(organization=organization)
                .exclude(provider_type="")
                .values_list("provider_type", flat=True)
            }
        )
        if types:
            self.fields["required_provider_type"].help_text += (
                " Your team's types: " + ", ".join(types) + "."
            )

    @classmethod
    def initial_for(cls, service: Service) -> dict:
        names = [name for _, fields in cls.SECTIONS for name in fields if name != "locations"]
        initial = {name: getattr(service, name) for name in names}
        initial["locations"] = [location.pk for location in service.locations.all()]
        return initial

    @classmethod
    def initial_for_new(cls, organization) -> dict:
        defaults = {field.name: field.default for field in Service._meta.fields}
        return {
            "is_public": True,
            "is_active": True,
            "min_notice_minutes": defaults["min_notice_minutes"],
            "max_advance_days": defaults["max_advance_days"],
            "cancellation_deadline_hours": defaults["cancellation_deadline_hours"],
            "rescheduling_deadline_hours": defaults["rescheduling_deadline_hours"],
        }

    def service_fields(self) -> tuple[dict, list]:
        """(keyword arguments for create_service/update_service, locations)."""
        data = dict(self.cleaned_data)
        locations = list(data.pop("locations"))
        for name, default in self.NUMBER_DEFAULTS.items():
            if data.get(name) is None:
                data[name] = default
        return data, locations

    def sections(self):
        return [
            (title, [self[name] for name in names if name in self.fields])
            for title, names in self.SECTIONS
            if any(name in self.fields for name in names)
        ]


class CategoryForm(ServiceErrorsMixin, forms.Form):
    name = forms.CharField(label="Name", max_length=120)
    color = forms.CharField(
        label="Colour",
        required=False,
        initial="#64748b",
        widget=forms.TextInput(attrs={"type": "color"}),
        help_text="Used to tell categories apart in lists and the calendar.",
    )
    sort_order = forms.IntegerField(
        label="Position",
        min_value=0,
        required=False,
        help_text="Lower numbers come first; ties are sorted by name.",
    )

    def clean_sort_order(self):
        return self.cleaned_data["sort_order"] or 0
