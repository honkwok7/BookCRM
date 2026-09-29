from django import forms

from bookings.selectors import bookable_services, bookable_staff
from core.exceptions import ConflictError, DomainError
from scheduling.availability import validate_slot
from staff.selectors import provides


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, staff):
        # Public page: show a name, never the staff member's email address.
        return staff.public_name


class PublicBookingForm(forms.Form):
    """Guest booking on an organization's public page.

    A time without a UTC offset (what ``<input type="datetime-local">`` sends) is read in the
    organization's time zone: the caller validates inside ``timezone.override``.
    """

    service = forms.ModelChoiceField(queryset=None, empty_label=None)
    staff = StaffChoiceField(queryset=None, empty_label=None)
    start_datetime = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    customer_name = forms.CharField(max_length=255)
    customer_email = forms.EmailField()
    customer_phone = forms.CharField(max_length=30, required=False)
    customer_notes = forms.CharField(max_length=2000, required=False, widget=forms.Textarea)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.conflict = False  # the time was taken (409) rather than invalid (400)
        self.fields["service"].queryset = bookable_services(organization, public=True)
        self.fields["staff"].queryset = bookable_staff(organization)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "border rounded p-2")

    def clean(self):
        data = super().clean()
        service, staff = data.get("service"), data.get("staff")
        if service is not None and staff is not None and not provides(staff, service, public=True):
            self.add_error(
                "staff", f"{staff.public_name} doesn't offer {service.name}. Choose someone else."
            )
        elif service is not None and staff is not None and data.get("start_datetime"):
            try:
                validate_slot(
                    self.organization, service, staff, data["start_datetime"], public=True
                )
            except DomainError as error:
                self.conflict = isinstance(error, ConflictError)
                self.add_error("start_datetime", error.message)
        return data
