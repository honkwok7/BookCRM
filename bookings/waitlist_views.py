"""The waitlist screen (M4.7): /app/waitlist/, adding someone and closing an entry.

Needs ``waitlist.manage``. Entries are written by ``bookings.waitlist``.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from bookings.models import Booking, WaitlistEntry
from bookings.waitlist import close_entry, join_waitlist
from core.exceptions import DomainError
from core.web import HtmxPartialMixin, TenantPageMixin, is_htmx
from locations.models import Location
from organizations.permissions import Capability
from services.selectors import services_for
from staff.selectors import staff_for

STATUS_LEVEL = {
    WaitlistEntry.Status.WAITING: "brand",
    WaitlistEntry.Status.NOTIFIED: "success",
    WaitlistEntry.Status.CLOSED: "neutral",
}


class WaitlistEntryForm(forms.Form):
    name = forms.CharField(max_length=255)
    email = forms.EmailField(help_text="We email them when a matching time frees up.")
    phone = forms.CharField(max_length=30, required=False)
    service = forms.ModelChoiceField(queryset=None, empty_label="Choose a service")
    staff = forms.ModelChoiceField(
        queryset=None, required=False, empty_label="Any provider", label="Provider"
    )
    location = forms.ModelChoiceField(queryset=None, required=False, empty_label="Any location")
    time_of_day = forms.ChoiceField(choices=WaitlistEntry.TimeOfDay.choices)
    preferred_start_date = forms.DateField(
        required=False, label="From", widget=forms.DateInput(attrs={"type": "date"})
    )
    preferred_end_date = forms.DateField(
        required=False, label="Until", widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = services_for(organization).filter(
            is_active=True, is_archived=False
        )
        self.fields["staff"].queryset = staff_for(organization).filter(is_active=True)
        self.fields["staff"].label_from_instance = lambda staff: staff.public_name
        self.fields["location"].queryset = Location.objects.filter(
            organization=organization, is_active=True
        )


class WaitlistPageMixin(TenantPageMixin):
    required_capabilities = (Capability.WAITLIST_MANAGE,)


class WaitlistListView(WaitlistPageMixin, HtmxPartialMixin, TemplateView):
    template_name = "waitlist/list.html"

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        status = self.request.GET.get("status", "")
        service_id = self.request.GET.get("service", "")
        services = list(services_for(organization).order_by("name"))
        entries = WaitlistEntry.objects.filter(organization=organization).select_related(
            "service", "preferred_staff__user", "location", "customer"
        )
        if status in WaitlistEntry.Status.values:
            entries = entries.filter(status=status)
        else:
            status = ""
            entries = entries.exclude(status=WaitlistEntry.Status.CLOSED)
        service = next((item for item in services if str(item.pk) == service_id), None)
        if service is not None:
            entries = entries.filter(service=service)
        page = Paginator(entries.order_by("created_at"), 25).get_page(self.request.GET.get("page"))
        for entry in page:
            entry.status_level = STATUS_LEVEL.get(entry.status, "neutral")
        return super().get_context_data(**kwargs) | {
            "organization": organization,
            "page_obj": page,
            "filtered": bool(status or service),
            "filters": [
                {
                    "name": "status",
                    "label": "Status",
                    "value": status,
                    "options": WaitlistEntry.Status.choices,
                    "all_label": "Waiting and notified",
                },
                {
                    "name": "service",
                    "label": "Service",
                    "value": str(service.pk) if service else "",
                    "options": [(str(item.pk), item.name) for item in services],
                    "all_label": "Every service",
                },
            ],
        }


class WaitlistAddView(WaitlistPageMixin, View):
    template_name = "waitlist/form.html"

    def render_form(self, form, status=200, error=""):
        context = {
            "organization": self.tenant.organization,
            "form": form,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
            "error": error,
        }
        name = f"{self.template_name}#form" if is_htmx(self.request) else self.template_name
        return render(self.request, name, context, status=status)

    def get(self, request):
        return self.render_form(WaitlistEntryForm(organization=self.tenant.organization))

    def post(self, request):
        form = WaitlistEntryForm(request.POST, organization=self.tenant.organization)
        if not form.is_valid():
            return self.render_form(form, status=422)
        data = form.cleaned_data
        try:
            join_waitlist(
                organization=self.tenant.organization,
                service=data["service"],
                preferred_staff=data["staff"],
                location=data["location"],
                time_of_day=data["time_of_day"],
                preferred_start_date=data["preferred_start_date"],
                preferred_end_date=data["preferred_end_date"],
                customer_name=data["name"],
                customer_email=data["email"],
                customer_phone=data["phone"],
                source=Booking.Source.RECEPTION,
                actor=request.user,
            )
        except DomainError as error:
            return self.render_form(form, status=422, error=error.message)
        messages.success(request, "Added to the waitlist.")
        url = reverse("app-waitlist")
        if is_htmx(request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


class WaitlistCloseView(WaitlistPageMixin, View):
    def post(self, request, pk):
        entry = WaitlistEntry.objects.filter(organization=self.tenant.organization, pk=pk).first()
        if entry is None:
            raise Http404("Waitlist entry not found")
        close_entry(entry=entry, actor=request.user)
        messages.success(request, "Waitlist entry closed.")
        return redirect("app-waitlist")
