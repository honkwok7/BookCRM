"""Web forms for the CRM screens. They only collect input: every rule (duplicate email,
email-or-phone, staff of the same organization, consent stamping, auditing) is applied by
crm.services, whose errors the views attach to the form."""

from __future__ import annotations

from django import forms

from bookings.models import Customer
from crm.models import CustomerNote
from crm.selectors import list_tags
from staff.models import StaffProfile

EDITABLE_STATUSES = [
    (value, label)
    for value, label in Customer.Status.choices
    if value != Customer.Status.ANONYMIZED
]
FORM_FIELDS_BY_ERROR_CODE = {
    "duplicate_email": "email",
    "contact_required": "email",
    "name_required": "first_name",
    "invalid_staff": "assigned_staff",
}


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, staff):
        return staff.user.get_full_name() or staff.user.email


class CustomerForm(forms.Form):
    """Create (``include_tags``) or edit a customer of ``organization``."""

    first_name = forms.CharField(
        label="First name",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    last_name = forms.CharField(
        label="Last name",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    preferred_name = forms.CharField(label="Preferred name", max_length=150, required=False)
    email = forms.EmailField(label="Email", required=False)
    phone = forms.CharField(
        label="Phone", max_length=30, required=False, widget=forms.TextInput(attrs={"type": "tel"})
    )
    secondary_phone = forms.CharField(
        label="Other phone",
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={"type": "tel"}),
    )
    birthday = forms.DateField(
        label="Birthday", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    pronouns = forms.CharField(label="Pronouns", max_length=50, required=False)
    alerts = forms.CharField(
        label="Front-desk alert",
        max_length=255,
        required=False,
        help_text="Shown to everyone who can see the customer, e.g. “Uses a wheelchair”. "
        "Put private details in an internal note instead.",
    )
    address_line1 = forms.CharField(label="Address", max_length=255, required=False)
    address_line2 = forms.CharField(label="Address line 2", max_length=255, required=False)
    city = forms.CharField(label="City", max_length=120, required=False)
    region = forms.CharField(label="Province or state", max_length=120, required=False)
    postal_code = forms.CharField(label="Postal code", max_length=20, required=False)
    country = forms.CharField(
        label="Country code",
        max_length=2,
        required=False,
        help_text="Two letters, e.g. CA.",
    )
    preferred_contact_method = forms.ChoiceField(
        label="Preferred contact",
        choices=[("", "No preference"), *Customer.ContactMethod.choices],
        required=False,
    )
    status = forms.ChoiceField(label="Status", choices=EDITABLE_STATUSES)
    assigned_staff = StaffChoiceField(
        label="Assigned provider", queryset=StaffProfile.objects.none(), required=False
    )
    preferred_staff = StaffChoiceField(
        label="Preferred provider", queryset=StaffProfile.objects.none(), required=False
    )
    email_consent = forms.BooleanField(label="Agrees to appointment emails", required=False)
    sms_consent = forms.BooleanField(label="Agrees to text messages", required=False)
    marketing_consent = forms.BooleanField(label="Agrees to marketing messages", required=False)
    tags = forms.ModelMultipleChoiceField(
        label="Tags",
        queryset=None,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    CONTACT_FIELDS = ("email", "phone", "secondary_phone")
    PROFILE_FIELDS = ("preferred_name", "birthday", "pronouns", "alerts")
    ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "region", "postal_code", "country")
    PREFERENCE_FIELDS = ("preferred_contact_method", "assigned_staff", "preferred_staff")
    CONSENT_FIELDS = ("email_consent", "sms_consent", "marketing_consent")

    def __init__(self, *args, organization, include_tags=False, include_status=True, **kwargs):
        super().__init__(*args, **kwargs)
        staff = (
            StaffProfile.objects.filter(organization=organization, is_active=True)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )
        self.fields["assigned_staff"].queryset = staff
        self.fields["preferred_staff"].queryset = staff
        if include_tags:
            self.fields["tags"].queryset = list_tags(organization).order_by("name")
        else:
            del self.fields["tags"]
        if not include_status:
            del self.fields["status"]

    @classmethod
    def initial_for(cls, customer: Customer) -> dict:
        names = [
            "first_name",
            "last_name",
            "status",
            *cls.CONTACT_FIELDS,
            *cls.PROFILE_FIELDS,
            *cls.ADDRESS_FIELDS,
            *cls.PREFERENCE_FIELDS,
            *cls.CONSENT_FIELDS,
        ]
        return {name: getattr(customer, name) for name in names}

    def clean_country(self):
        return self.cleaned_data["country"].strip().upper()

    def service_fields(self) -> dict:
        """cleaned_data as keyword arguments for create_customer / update_customer."""
        data = dict(self.cleaned_data)
        data.pop("tags", None)
        for name, value in list(data.items()):
            if value is None and name not in ("birthday", "assigned_staff", "preferred_staff"):
                data[name] = ""
        return data

    def add_service_error(self, error) -> None:
        """Show a DomainError/ConflictError from crm.services next to the field it concerns."""
        field = FORM_FIELDS_BY_ERROR_CODE.get(getattr(error, "code", ""))
        self.add_error(field if field in self.fields else None, error.message)

    def sections(self):
        """Field groups for the template, in display order."""
        return [
            ("Name", [self["first_name"], self["last_name"]]),
            ("Contact", [self[name] for name in self.CONTACT_FIELDS]),
            ("Profile", [self[name] for name in self.PROFILE_FIELDS]),
            ("Address", [self[name] for name in self.ADDRESS_FIELDS]),
            (
                "Preferences",
                [self[name] for name in self.PREFERENCE_FIELDS]
                + ([self["status"]] if "status" in self.fields else []),
            ),
            ("Consent", [self[name] for name in self.CONSENT_FIELDS]),
        ]


class CustomerTagsForm(forms.Form):
    tags = forms.ModelMultipleChoiceField(
        label="Tags", queryset=None, required=False, widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tags"].queryset = list_tags(organization).order_by("name")


class NoteForm(forms.Form):
    content = forms.CharField(
        label="Note",
        max_length=10000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    note_type = forms.ChoiceField(label="Type", choices=CustomerNote.NoteType.choices)
    visibility = forms.ChoiceField(label="Who can read it", choices=())
    pinned = forms.BooleanField(label="Pin to the top", required=False)

    VISIBILITY_LABELS = {
        CustomerNote.Visibility.INTERNAL: "Internal: team members allowed to read private notes",
        CustomerNote.Visibility.CUSTOMER_VISIBLE: "Everyone who can see the customer, "
        "and the customer",
    }

    def __init__(self, *args, allow_internal: bool, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [
            (CustomerNote.Visibility.CUSTOMER_VISIBLE, self.VISIBILITY_LABELS["customer_visible"])
        ]
        if allow_internal:
            choices.insert(
                0, (CustomerNote.Visibility.INTERNAL, self.VISIBILITY_LABELS["internal"])
            )
        self.fields["visibility"].choices = choices
        self.fields["visibility"].initial = choices[0][0]
