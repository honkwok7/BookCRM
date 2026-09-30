"""Forms of the team's appointment screens (M4.5): new appointment, walk-in and reschedule.

The choices depend on each other (location → services offered there → providers who offer the
service → free times), so a form is rebuilt from what has been chosen so far. The screens
re-render the form after every change (htmx posts it with ``refresh``; without JavaScript the
"Update" button does the same) and only book when the form is submitted for real. Free times
come from the availability engine with the team's rules (no online notice or window). The
booking service checks everything again when booking.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from django import forms
from django.utils import timezone
from django.utils.dateparse import parse_date

from bookings.models import Customer
from crm.selectors import search_filter
from locations.models import Location
from scheduling.availability import AvailabilityService, zone_of
from services.selectors import services_bookable_at
from staff.selectors import list_providers_for

ANYONE = "any"


class SlotChoicesMixin:
    """Free times as ``(iso start, label)`` choices, with who is free at each."""

    def slot_choices(self, engine, day: date, staff=None, ignore_booking=None):
        self.slots = {
            slot.start.isoformat(): slot
            for slot in engine.get_available_slots(
                day, day, staff=staff, ignore_booking=ignore_booking
            )
        }
        choices = []
        for key, slot in self.slots.items():
            label = f"{slot.start.astimezone(engine.zone):%I:%M %p}".lstrip("0")
            if staff is None:
                names = ", ".join(candidate.staff.public_name for candidate in slot.candidates)
                label = f"{label} ({names})"
            choices.append((key, label))
        return choices


class AppointmentForm(SlotChoicesMixin, forms.Form):
    """Book for an existing customer (search) or a new one (name plus email or phone)."""

    q = forms.CharField(required=False, label="Find a customer", max_length=100)
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.none(),
        required=False,
        widget=forms.RadioSelect,
        empty_label=None,
        label="Customer",
    )
    name = forms.CharField(required=False, max_length=255, label="New customer's name")
    email = forms.EmailField(required=False)
    phone = forms.CharField(required=False, max_length=30)
    location = forms.ModelChoiceField(queryset=Location.objects.none(), empty_label=None)
    service = forms.ModelChoiceField(queryset=None, empty_label="Choose a service")
    staff = forms.ChoiceField(label="Provider")
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.ChoiceField(label="Time")
    customer_notes = forms.CharField(
        required=False,
        max_length=2000,
        label="Notes",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, tenant, customers, own_staff=None, walk_in=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.walk_in = walk_in
        self.own_staff = own_staff  # a provider without view_all books only for themselves
        organization = tenant.organization
        data = self.data if self.is_bound else self.initial
        self.slots = {}

        # Customer: search the customers this member may read.
        self.can_search = customers is not None
        if self.can_search:
            query = (data.get("q") or "").strip()
            chosen = data.get("customer") or ""
            matches = search_filter(customers, query).order_by("last_name", "first_name")[:8]
            ids = [customer.pk for customer in matches] if query else []
            if is_uuid(chosen):
                ids.append(chosen)
            self.fields["customer"].queryset = customers.filter(pk__in=ids)
        else:
            del self.fields["q"]
            del self.fields["customer"]

        # Location, service, provider.
        locations = Location.objects.filter(organization=organization, is_active=True).order_by(
            "-is_default", "name"
        )
        self.fields["location"].queryset = locations
        location = self._pick(locations, data.get("location")) or locations.first()
        self.location = location
        if self.is_bound and location is not None and not data.get("location"):
            # Not posted (one location, or a walk-in): the default location.
            self.data = self.data.copy()
            self.data["location"] = str(location.pk)
        if location is not None:
            self.fields["location"].initial = location.pk
        if locations.count() < 2:
            self.fields["location"].widget = forms.HiddenInput()

        services = services_bookable_at(organization, location, public=False).order_by("name")
        self.fields["service"].queryset = services
        self.service = self._pick(services, data.get("service"))

        providers = []
        if self.service is not None and location is not None:
            providers = list_providers_for(self.service, location)
            if own_staff is not None:
                providers = providers.filter(pk=own_staff.pk)
            providers = list(providers)
        self.providers = {str(provider.pk): provider for provider in providers}
        choices = [(str(provider.pk), provider.public_name) for provider in providers]
        if own_staff is None and len(providers) > 1:
            choices.insert(0, (ANYONE, "First available"))
        self.fields["staff"].choices = choices
        chosen_staff = str(data.get("staff") or "")
        if chosen_staff not in dict(choices):
            chosen_staff = choices[0][0] if choices else ""
        self.staff_choice = chosen_staff
        self.fields["staff"].initial = chosen_staff

        # Date and free times.
        zone = zone_of(location) if location is not None else timezone.get_current_timezone()
        self.zone = zone
        today = timezone.now().astimezone(zone).date()
        if walk_in:
            del self.fields["date"]
            del self.fields["start"]
            return
        day = data.get("date")
        day = day if isinstance(day, date) else parse_date(str(day or "")) or today
        self.day = max(day, today)
        self.fields["date"].initial = self.day
        self.fields["date"].widget.attrs["min"] = today.isoformat()
        self.fields["start"].choices = []
        if self.service is not None and chosen_staff:
            engine = AvailabilityService(organization, self.service, location=location)
            staff = None if chosen_staff == ANYONE else self.providers.get(chosen_staff)
            self.fields["start"].choices = self.slot_choices(engine, self.day, staff)

    @staticmethod
    def _pick(queryset, value):
        return queryset.filter(pk=value).first() if is_uuid(value) else None

    def clean(self):
        data = super().clean()
        customer = data.get("customer")
        if customer is None:
            if not data.get("name"):
                self.add_error("name", "Choose a customer or enter a new customer's name.")
            elif not (data.get("email") or data.get("phone")):
                self.add_error("phone", "Enter an email address or a phone number.")
        return data

    def staff_candidates(self, start=None) -> list:
        """Who to book: the chosen provider, or everyone free at ``start`` (first available)."""
        if self.staff_choice != ANYONE:
            provider = self.providers.get(self.staff_choice)
            return [provider] if provider else []
        if start is None:
            return list(self.providers.values())
        slot = self.slots.get(start.isoformat())
        return slot.staff if slot else []

    def customer_details(self) -> dict:
        data = self.cleaned_data
        customer = data.get("customer")
        if customer is not None:
            return {
                "customer": customer,
                "customer_name": customer.name,
                "customer_email": customer.email,
                "customer_phone": customer.phone,
            }
        return {
            "customer": None,
            "customer_name": data["name"].strip(),
            "customer_email": data["email"].strip(),
            "customer_phone": data["phone"].strip(),
        }


class RescheduleForm(SlotChoicesMixin, forms.Form):
    """A new date and time with the same provider (their free times, ignoring this
    appointment's own time)."""

    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.ChoiceField(label="New time")

    def __init__(self, *args, booking, **kwargs):
        super().__init__(*args, **kwargs)
        self.booking = booking
        engine = AvailabilityService(
            booking.organization, booking.service, location=booking.location
        )
        today = timezone.now().astimezone(engine.zone).date()
        data = self.data if self.is_bound else self.initial
        day = (
            parse_date(str(data.get("date") or ""))
            or booking.start_datetime.astimezone(engine.zone).date()
        )
        self.day = max(day, today)
        self.fields["date"].initial = self.day
        self.fields["date"].widget.attrs["min"] = today.isoformat()
        self.slots = {}
        choices = self.slot_choices(engine, self.day, booking.staff, ignore_booking=booking)
        # Its own time isn't a move.
        current = booking.start_datetime
        self.slots = {key: slot for key, slot in self.slots.items() if slot.start != current}
        self.fields["start"].choices = [item for item in choices if item[0] in self.slots]


def is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def next_minute(moment):
    """Walk-ins start now: the next whole minute (a start must be in the future)."""
    return moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
