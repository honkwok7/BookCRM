"""Location screens: the list, a location's page (details, opening hours, closures) and the
forms that change them.

Reads go through locations.selectors and writes through locations.services, exactly like the
API. Viewing needs ``locations.view``; every change needs ``locations.manage``. A location of
another organization is a 404.

htmx: forms open in the page dialog and post with ``hx-post``; on success the server sends
the browser to the location's page (HX-Redirect). Without JavaScript the same URLs work as
ordinary pages and redirects.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core.exceptions import DomainError
from core.web import TenantPageMixin, is_htmx
from locations.forms import ClosureForm, HoursForm, LocationForm
from locations.models import Location, LocationClosure
from locations.selectors import (
    local_today,
    locations_for,
    past_closures,
    upcoming_closures,
    weekly_hours,
)
from locations.services import (
    create_closure,
    create_location,
    delete_closure,
    set_default_location,
    set_location_hours,
    update_location,
)
from organizations.permissions import Capability


def location_limit(organization) -> int | None:
    subscription = getattr(organization, "subscription", None)
    return subscription.plan.maximum_locations if subscription is not None else None


class LocationPageMixin(TenantPageMixin):
    required_capabilities = (Capability.LOCATIONS_VIEW,)

    def get_location(self, pk) -> Location:
        location = locations_for(self.tenant.organization).filter(pk=pk).first()
        if location is None:
            raise Http404("Location not found")
        return location

    def can_manage(self) -> bool:
        return self.tenant.has(Capability.LOCATIONS_MANAGE)


class LocationManageMixin(LocationPageMixin):
    required_capabilities = (Capability.LOCATIONS_VIEW, Capability.LOCATIONS_MANAGE)

    form_template = ""

    def render_form(self, form, *, status=200, location=None, **extra):
        context = {
            "form": form,
            "location": location,
            "organization": self.tenant.organization,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
            **extra,
        }
        name = f"{self.form_template}#form" if is_htmx(self.request) else self.form_template
        return render(self.request, name, context, status=status)

    def success(self, location, message):
        messages.success(self.request, message)
        url = reverse("app-location-detail", args=[location.pk])
        if is_htmx(self.request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


# -- List and page --------------------------------------------------------------------------


class LocationListView(LocationPageMixin, TemplateView):
    template_name = "locations/location_list.html"

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        locations = list(locations_for(organization).order_by("-is_default", "-is_active", "name"))
        rows = []
        for location in locations:
            week = weekly_hours(location)
            weekday = local_today(location).weekday()
            rows.append(
                {
                    "location": location,
                    "today": week[weekday],
                    "has_hours": any(row["periods"] for row in week),
                }
            )
        active = sum(1 for location in locations if location.is_active)
        limit = location_limit(organization)
        return super().get_context_data(**kwargs) | {
            "rows": rows,
            "active_count": active,
            "limit": limit,
            "at_limit": limit is not None and active >= limit,
            "can_manage": self.can_manage(),
        }


class LocationDetailView(LocationPageMixin, TemplateView):
    template_name = "locations/location_detail.html"

    def get_context_data(self, pk, **kwargs):
        location = self.get_location(pk)
        today = local_today(location)
        week = weekly_hours(location)
        return super().get_context_data(**kwargs) | {
            "location": location,
            "week": week,
            "today_weekday": today.weekday(),
            "has_hours": any(row["periods"] for row in week),
            "upcoming_closures": upcoming_closures(location, today=today),
            "past_closures": past_closures(location, today=today),
            "can_manage": self.can_manage(),
        }


# -- Create and edit ------------------------------------------------------------------------


class LocationCreateView(LocationManageMixin, View):
    form_template = "locations/location_form.html"

    def form(self, data=None):
        organization = self.tenant.organization
        return LocationForm(
            data,
            include_status=False,
            initial={"timezone": organization.timezone, "booking_enabled": True},
        )

    def get(self, request):
        return self.render_form(self.form())

    def post(self, request):
        form = self.form(request.POST)
        if form.is_valid():
            try:
                location = create_location(
                    organization=self.tenant.organization,
                    actor=request.user,
                    **form.service_fields(),
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(location, f"{location.name} was added.")
        return self.render_form(form, status=422)


class LocationEditView(LocationManageMixin, View):
    form_template = "locations/location_form.html"

    def get(self, request, pk):
        location = self.get_location(pk)
        return self.render_form(
            LocationForm(initial=LocationForm.initial_for(location)), location=location
        )

    def post(self, request, pk):
        location = self.get_location(pk)
        form = LocationForm(request.POST)
        if form.is_valid():
            try:
                location = update_location(
                    location=location, actor=request.user, **form.service_fields()
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(location, "Changes saved.")
        return self.render_form(form, status=422, location=location)


class LocationHoursView(LocationManageMixin, View):
    form_template = "locations/hours_form.html"

    def get(self, request, pk):
        location = self.get_location(pk)
        return self.render_form(
            HoursForm(initial=HoursForm.initial_for(location)), location=location
        )

    def post(self, request, pk):
        location = self.get_location(pk)
        form = HoursForm(request.POST)
        if form.is_valid():
            try:
                set_location_hours(location=location, periods=form.periods(), actor=request.user)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(location, "Opening hours saved.")
        return self.render_form(form, status=422, location=location)


class LocationMakeDefaultView(LocationManageMixin, View):
    def post(self, request, pk):
        location = self.get_location(pk)
        try:
            location = set_default_location(location=location, actor=request.user)
        except DomainError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, f"{location.name} is now the default location.")
        url = reverse("app-location-detail", args=[location.pk])
        if is_htmx(request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


# -- Closures -------------------------------------------------------------------------------


class ClosureCreateView(LocationManageMixin, View):
    form_template = "locations/closure_form.html"

    def get(self, request, pk):
        location = self.get_location(pk)
        return self.render_form(ClosureForm(), location=location)

    def post(self, request, pk):
        location = self.get_location(pk)
        form = ClosureForm(request.POST)
        if form.is_valid():
            try:
                create_closure(location=location, actor=request.user, **form.service_fields())
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(location, "Closure added.")
        return self.render_form(form, status=422, location=location)


class ClosureDeleteView(LocationManageMixin, View):
    def post(self, request, pk, closure_pk):
        location = self.get_location(pk)
        closure = LocationClosure.objects.filter(pk=closure_pk, location=location).first()
        if closure is None:
            raise Http404("Closure not found")
        delete_closure(closure=closure, actor=request.user)
        return self.success(location, "Closure removed.")
