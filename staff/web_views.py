"""Staff screens: the staff list, a person's page (Profile, Services, Locations, Availability,
Time off) and the forms that change them.

Reads go through staff.selectors and writes through staff.services, exactly like the API.
Viewing needs ``staff.view``; every change needs ``staff.manage``. A staff member of another
organization is a 404.

htmx: forms open in the page dialog and post with ``hx-post``; on success the server sends the
browser to the person's page (HX-Redirect). Without JavaScript the same URLs work as ordinary
pages and redirects. Each tab is its own URL; htmx swaps only the tab area.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.exceptions import DomainError
from core.web import HtmxPartialMixin, TenantPageMixin, is_htmx
from locations.models import Location
from organizations.permissions import Capability
from scheduling.models import TimeOff
from scheduling.selectors import weekly_hours
from scheduling.services import appointments_during, decide_time_off
from staff.forms import StaffCreateForm, StaffLocationsForm, StaffProfileForm, StaffServicesForm
from staff.models import StaffProfile
from staff.selectors import offerings_by_service, staff_for
from staff.services import (
    create_staff_profile,
    set_staff_locations,
    set_staff_offerings,
    update_staff_profile,
)

TABS = (
    ("profile", "Profile"),
    ("services", "Services"),
    ("locations", "Locations"),
    ("availability", "Availability"),
    ("time-off", "Time off"),
)
TAB_SLUGS = {slug for slug, _ in TABS}


def staff_tab_url(staff, tab: str) -> str:
    if tab == "profile":
        return reverse("app-staff-detail", args=[staff.pk])
    return reverse("app-staff-tab", args=[staff.pk, tab])


class StaffPageMixin(TenantPageMixin):
    required_capabilities = (Capability.STAFF_VIEW,)

    def get_staff(self, pk) -> StaffProfile:
        staff = staff_for(self.tenant.organization).filter(pk=pk).first()
        if staff is None:
            raise Http404("Staff member not found")
        return staff

    def can_manage(self) -> bool:
        return self.tenant.has(Capability.STAFF_MANAGE)


class StaffManageMixin(StaffPageMixin):
    required_capabilities = (Capability.STAFF_VIEW, Capability.STAFF_MANAGE)
    form_template = ""

    def render_form(self, form, *, status=200, staff=None, **extra):
        context = {
            "form": form,
            "staff": staff,
            "organization": self.tenant.organization,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
            **extra,
        }
        name = f"{self.form_template}#form" if is_htmx(self.request) else self.form_template
        return render(self.request, name, context, status=status)

    def success(self, staff, message, tab="profile"):
        messages.success(self.request, message)
        url = staff_tab_url(staff, tab)
        if is_htmx(self.request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


# -- List -----------------------------------------------------------------------------------


class StaffListView(StaffPageMixin, HtmxPartialMixin, TemplateView):
    template_name = "staff/staff_list.html"
    page_size = 25

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        locations = list(Location.objects.filter(organization=organization).order_by("name"))
        location_id = self.request.GET.get("location", "")
        location = next((loc for loc in locations if str(loc.pk) == location_id), None)

        staff = staff_for(organization).annotate(
            service_count=Count(
                "offerings__service", filter=Q(offerings__is_active=True), distinct=True
            )
        )
        if status == "inactive":
            staff = staff.filter(is_active=False)
        elif status == "all":
            pass
        else:
            status = ""
            staff = staff.filter(is_active=True)
        if location is not None:
            staff = staff.filter(locations=location)
        if query:
            staff = staff.filter(
                Q(display_name__icontains=query)
                | Q(user__first_name__icontains=query)
                | Q(user__last_name__icontains=query)
                | Q(user__email__icontains=query)
                | Q(job_title__icontains=query)
            )
        staff = staff.order_by("user__first_name", "user__last_name", "created_at")
        page = Paginator(staff, self.page_size).get_page(self.request.GET.get("page"))
        subscription = getattr(organization, "subscription", None)
        return super().get_context_data(**kwargs) | {
            "page_obj": page,
            "query": query,
            "filtered": bool(query or status or location),
            "can_manage": self.can_manage(),
            "limit": subscription.plan.maximum_staff if subscription else None,
            "active_count": StaffProfile.objects.filter(
                organization=organization, is_active=True
            ).count(),
            "filters": [
                {
                    "name": "location",
                    "label": "Location",
                    "value": str(location.pk) if location else "",
                    "options": [(str(loc.pk), loc.name) for loc in locations],
                    "all_label": "Every location",
                },
                {
                    "name": "status",
                    "label": "Status",
                    "value": status,
                    "options": [("inactive", "Inactive"), ("all", "Active and inactive")],
                    "all_label": "Active",
                },
            ],
        }


# -- Person page ----------------------------------------------------------------------------


class StaffDetailView(StaffPageMixin, TemplateView):
    template_name = "staff/staff_detail.html"

    def get_template_names(self):
        if is_htmx(self.request):
            return [f"{self.template_name}#tabs"]
        return [self.template_name]

    def get(self, request, *args, **kwargs):
        if kwargs.get("tab", "profile") not in TAB_SLUGS:
            raise Http404("No such tab")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, pk, tab="profile", **kwargs):
        staff = self.get_staff(pk)
        context = super().get_context_data(**kwargs) | {
            "staff": staff,
            "tab": tab,
            "tabs": [
                {
                    "slug": slug,
                    "label": label,
                    "url": staff_tab_url(staff, slug),
                    "active": slug == tab,
                }
                for slug, label in TABS
            ],
            "tab_template": f"staff/tabs/{tab}.html",
            "can_manage": self.can_manage(),
        }
        builder = getattr(self, f"context_{tab.replace('-', '_')}", None)
        if builder is not None:
            context |= builder(staff)
        return context

    def context_services(self, staff):
        grouped = offerings_by_service(staff)
        location_count = staff.locations.count()
        rows = []
        for offerings in grouped.values():
            first = offerings[0]
            everywhere = any(offering.location_id is None for offering in offerings)
            rows.append(
                {
                    "service": first.service,
                    "where": (
                        None
                        if everywhere
                        else sorted(offering.location.name for offering in offerings)
                    ),
                    "duration": first.duration_minutes,
                    "price": first.price,
                    "custom": first.custom_duration_minutes is not None
                    or first.custom_price is not None,
                    "active": any(offering.is_active for offering in offerings),
                }
            )
        rows.sort(key=lambda row: row["service"].name)
        return {"rows": rows, "location_count": location_count}

    def context_availability(self, staff):
        return weekly_hours(staff)

    def context_time_off(self, staff):
        now = timezone.now()
        entries = list(
            TimeOff.objects.filter(
                staff=staff,
                end_datetime__gte=now,
                approval_status__in=(
                    TimeOff.ApprovalStatus.PENDING,
                    TimeOff.ApprovalStatus.APPROVED,
                ),
            ).order_by("start_datetime")
        )
        for entry in entries:
            if entry.approval_status == TimeOff.ApprovalStatus.PENDING:
                entry.clashes = appointments_during(
                    staff, entry.start_datetime, entry.end_datetime
                ).count()
        return {"time_off": entries, "can_decide": self.can_manage()}


class StaffTimeOffDecideView(StaffManageMixin, View):
    """Approve or reject a pending time-off request (``staff.manage``)."""

    def post(self, request, pk, entry_pk):
        staff = self.get_staff(pk)
        entry = TimeOff.objects.filter(pk=entry_pk, staff=staff).first()
        if entry is None:
            raise Http404("Time off not found")
        approve = request.POST.get("decision") == "approve"
        try:
            decide_time_off(entry=entry, approve=approve, actor=request.user)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
            return redirect(staff_tab_url(staff, "time-off"))
        return self.success(
            staff, "Time off approved." if approve else "Time off rejected.", tab="time-off"
        )


# -- Forms ----------------------------------------------------------------------------------


class StaffCreateView(StaffManageMixin, View):
    form_template = "staff/staff_create_form.html"

    def get(self, request):
        return self.render_form(StaffCreateForm(organization=self.tenant.organization))

    def post(self, request):
        form = StaffCreateForm(request.POST, organization=self.tenant.organization)
        if form.is_valid():
            fields = form.service_fields()
            try:
                staff = create_staff_profile(
                    organization=self.tenant.organization,
                    user=fields.pop("user"),
                    locations=fields.pop("locations"),
                    actor=request.user,
                    **fields,
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(
                    staff,
                    f"{staff.public_name} was added. Choose the services they offer.",
                    "services",
                )
        return self.render_form(form, status=422)


class StaffEditView(StaffManageMixin, View):
    form_template = "staff/staff_form.html"

    def get(self, request, pk):
        staff = self.get_staff(pk)
        return self.render_form(
            StaffProfileForm(initial=StaffProfileForm.initial_for(staff)), staff=staff
        )

    def post(self, request, pk):
        staff = self.get_staff(pk)
        form = StaffProfileForm(request.POST)
        if form.is_valid():
            try:
                staff = update_staff_profile(
                    staff=staff, actor=request.user, **form.service_fields()
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(staff, "Changes saved.")
        return self.render_form(form, status=422, staff=staff)


class StaffLocationsView(StaffManageMixin, View):
    form_template = "staff/staff_locations_form.html"

    def get(self, request, pk):
        staff = self.get_staff(pk)
        form = StaffLocationsForm(
            organization=self.tenant.organization,
            initial={"locations": [location.pk for location in staff.locations.all()]},
        )
        return self.render_form(form, staff=staff)

    def post(self, request, pk):
        staff = self.get_staff(pk)
        form = StaffLocationsForm(request.POST, organization=self.tenant.organization)
        if form.is_valid():
            try:
                set_staff_locations(
                    staff=staff, locations=form.cleaned_data["locations"], actor=request.user
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(staff, "Locations saved.", "locations")
        return self.render_form(form, status=422, staff=staff)


class StaffServicesView(StaffManageMixin, View):
    form_template = "staff/staff_services_form.html"

    def get(self, request, pk):
        staff = self.get_staff(pk)
        form = StaffServicesForm(staff=staff, initial=StaffServicesForm.initial_for(staff))
        return self.render_form(form, staff=staff)

    def post(self, request, pk):
        staff = self.get_staff(pk)
        form = StaffServicesForm(request.POST, staff=staff)
        if form.is_valid():
            try:
                set_staff_offerings(staff=staff, choices=form.choices(), actor=request.user)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(staff, "Services saved.", "services")
        return self.render_form(form, status=422, staff=staff)
