"""The services screen: services grouped by category, and the forms that change services and
categories.

Reads go through services.selectors and writes through services.services, exactly like the
API. Viewing needs ``services.view``; every change needs ``services.manage``. A service or
category of another organization is a 404.

htmx: forms open in the page dialog and post with ``hx-post``; on success the server sends the
browser back to the list (HX-Redirect). Without JavaScript the same URLs work as ordinary pages
and redirects.
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core.exceptions import DomainError
from core.web import HtmxPartialMixin, TenantPageMixin, is_htmx
from locations.models import Location
from organizations.permissions import Capability
from services.forms import CategoryForm, ServiceForm
from services.models import ServiceCategory
from services.selectors import categories_for, offered_at_filter, services_for
from services.services import (
    create_category,
    create_service,
    delete_category,
    update_category,
    update_service,
)

STATUSES = [("archived", "Archived"), ("inactive", "Inactive"), ("all", "All, including archived")]


class ServicePageMixin(TenantPageMixin):
    required_capabilities = (Capability.SERVICES_VIEW,)

    def can_manage(self) -> bool:
        return self.tenant.has(Capability.SERVICES_MANAGE)


class ServiceManageMixin(ServicePageMixin):
    required_capabilities = (Capability.SERVICES_VIEW, Capability.SERVICES_MANAGE)
    form_template = ""

    def get_service(self, pk):
        service = services_for(self.tenant.organization).filter(pk=pk).first()
        if service is None:
            raise Http404("Service not found")
        return service

    def get_category(self, pk):
        category = ServiceCategory.objects.filter(
            organization=self.tenant.organization, pk=pk
        ).first()
        if category is None:
            raise Http404("Category not found")
        return category

    def render_form(self, form, *, status=200, **extra):
        context = {
            "form": form,
            "organization": self.tenant.organization,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
            **extra,
        }
        name = f"{self.form_template}#form" if is_htmx(self.request) else self.form_template
        return render(self.request, name, context, status=status)

    def success(self, message):
        messages.success(self.request, message)
        url = reverse("app-service-list")
        if is_htmx(self.request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


class ServiceListView(ServicePageMixin, HtmxPartialMixin, TemplateView):
    template_name = "services/service_list.html"

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        locations = list(Location.objects.filter(organization=organization).order_by("name"))
        location_id = self.request.GET.get("location", "")
        location = next((loc for loc in locations if str(loc.pk) == location_id), None)

        services = services_for(organization).annotate(
            provider_count=Count(
                "offerings__staff",
                filter=Q(offerings__is_active=True, offerings__staff__is_active=True),
                distinct=True,
            )
        )
        if status == "archived":
            services = services.filter(is_archived=True)
        elif status == "inactive":
            services = services.filter(is_archived=False, is_active=False)
        elif status != "all":
            status = ""
            services = services.filter(is_archived=False)
        if location is not None:
            services = services.filter(offered_at_filter(location))
        if query:
            services = services.filter(Q(name__icontains=query) | Q(description__icontains=query))
        services = list(services.distinct().order_by("name"))

        categories = list(categories_for(organization).order_by("sort_order", "name"))
        groups = [
            {
                "category": category,
                "services": [s for s in services if s.category_id == category.pk],
            }
            for category in categories
        ]
        uncategorized = [s for s in services if s.category_id is None]
        if uncategorized:
            groups.append({"category": None, "services": uncategorized})
        filtered = bool(query or status or location)
        if filtered:
            groups = [group for group in groups if group["services"]]

        subscription = getattr(organization, "subscription", None)
        return super().get_context_data(**kwargs) | {
            "groups": groups,
            "has_services": bool(services),
            "query": query,
            "filtered": filtered,
            "can_manage": self.can_manage(),
            "limit": subscription.plan.maximum_services if subscription else None,
            "active_count": services_for(organization).filter(is_archived=False).count(),
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
                    "options": STATUSES,
                    "all_label": "Not archived",
                },
            ],
        }


class ServiceCreateView(ServiceManageMixin, View):
    form_template = "services/service_form.html"

    def form(self, data=None):
        organization = self.tenant.organization
        initial = ServiceForm.initial_for_new(organization)
        category_id = self.request.GET.get("category")
        if category_id:
            category = ServiceCategory.objects.filter(
                organization=organization, pk=category_id
            ).first()
            initial["category"] = category.pk if category else None
        return ServiceForm(data, organization=organization, include_archive=False, initial=initial)

    def get(self, request):
        return self.render_form(self.form())

    def post(self, request):
        form = self.form(request.POST)
        if form.is_valid():
            fields, locations = form.service_fields()
            try:
                service = create_service(
                    organization=self.tenant.organization,
                    actor=request.user,
                    locations=locations,
                    **fields,
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(f"{service.name} was added.")
        return self.render_form(form, status=422)


class ServiceEditView(ServiceManageMixin, View):
    form_template = "services/service_form.html"

    def get(self, request, pk):
        service = self.get_service(pk)
        form = ServiceForm(
            organization=self.tenant.organization, initial=ServiceForm.initial_for(service)
        )
        return self.render_form(form, service=service)

    def post(self, request, pk):
        service = self.get_service(pk)
        form = ServiceForm(request.POST, organization=self.tenant.organization)
        if form.is_valid():
            fields, locations = form.service_fields()
            try:
                update_service(service=service, actor=request.user, locations=locations, **fields)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success("Changes saved.")
        return self.render_form(form, status=422, service=service)


class CategoryCreateView(ServiceManageMixin, View):
    form_template = "services/category_form.html"

    def get(self, request):
        return self.render_form(CategoryForm())

    def post(self, request):
        form = CategoryForm(request.POST)
        if form.is_valid():
            try:
                category = create_category(
                    organization=self.tenant.organization,
                    actor=request.user,
                    **form.cleaned_data,
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(f"{category.name} was added.")
        return self.render_form(form, status=422)


class CategoryEditView(ServiceManageMixin, View):
    form_template = "services/category_form.html"

    def get(self, request, pk):
        category = self.get_category(pk)
        form = CategoryForm(
            initial={
                "name": category.name,
                "color": category.color,
                "sort_order": category.sort_order,
            }
        )
        return self.render_form(form, category=category)

    def post(self, request, pk):
        category = self.get_category(pk)
        form = CategoryForm(request.POST)
        if form.is_valid():
            try:
                update_category(category=category, actor=request.user, **form.cleaned_data)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success("Changes saved.")
        return self.render_form(form, status=422, category=category)


class CategoryDeleteView(ServiceManageMixin, View):
    def post(self, request, pk):
        category = self.get_category(pk)
        delete_category(category=category, actor=request.user)
        return self.success(f"{category.name} was removed. Its services have no category now.")
