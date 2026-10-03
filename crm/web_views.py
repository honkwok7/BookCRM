"""CRM screens: the customer list and the customer profile.

Reads go through crm.selectors (``customers_visible_to``, ``notes_visible_to``,
``customer_timeline``) and writes through crm.services, exactly like the API, so the web app
applies the same access rules, validation and auditing. A customer the user may not see is a
404, whichever organization it belongs to.

htmx: forms post with ``hx-post`` and get back the fragment they replace (plus a toast);
without JavaScript the same URLs work as ordinary pages and redirects.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from bookings.models import Customer
from bookings.selectors import bookings_visible_to
from core.exceptions import DomainError
from core.search import MIN_QUERY_LENGTH, organization_search
from core.web import (
    NO_ACCESS_MESSAGE,
    HtmxPartialMixin,
    TenantPageMixin,
    htmx_trigger,
    is_htmx,
    toast,
)
from crm.forms import CustomerForm, CustomerTagsForm, NoteForm
from crm.models import CustomerActivity, CustomerNote
from crm.permissions import (
    can_browse_customers,
    can_change_note,
    can_manage_customers,
    can_write_internal_notes,
    can_write_notes,
)
from crm.selectors import (
    can_read_internal_notes,
    customer_stats,
    customer_timeline,
    customers_of_provider,
    customers_visible_to,
    last_visit_annotation,
    list_tags,
    notes_visible_to,
    search_filter,
)
from crm.services import (
    add_customer_tag,
    create_customer,
    create_note,
    delete_note,
    get_or_create_tag,
    set_customer_tags,
    update_customer,
    update_note,
)
from organizations.permissions import Capability
from staff.selectors import is_provider_here

# Profile tabs, in order: (slug, label). Transactions is a placeholder until payments.
TABS = (
    ("overview", "Overview"),
    ("appointments", "Appointments"),
    ("notes", "Notes"),
    ("communications", "Communications"),
    ("forms", "Forms"),
    ("transactions", "Transactions"),
    ("activity", "Activity"),
)
TAB_SLUGS = {slug for slug, _ in TABS}
COMMUNICATION_KINDS = (CustomerActivity.Kind.EMAIL_SENT, CustomerActivity.Kind.SMS_SENT)


class CustomerPageMixin(TenantPageMixin):
    """Customer pages: ``customers.view``, or a provider (who sees only their customers)."""

    def has_page_access(self, tenant):
        return can_browse_customers(tenant)

    def get_customer(self, pk) -> Customer:
        customer = (
            customers_visible_to(self.request)
            .select_related("assigned_staff__user", "preferred_staff__user", "organization")
            .filter(pk=pk)
            .first()
        )
        if customer is None:
            raise Http404("Customer not found")
        return customer


class CustomerManageMixin(CustomerPageMixin):
    def has_page_access(self, tenant):
        return can_manage_customers(tenant)


# -- List -----------------------------------------------------------------------------------


class CustomerListView(CustomerPageMixin, HtmxPartialMixin, TemplateView):
    template_name = "crm/customer_list.html"
    page_size = 25
    heading = "Customers"

    def visible_customers(self):
        return customers_visible_to(self.request)

    def can_add(self) -> bool:
        return can_manage_customers(self.tenant)

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        tags = list(list_tags(organization).order_by("name"))
        tag_id = self.request.GET.get("tag", "")
        tag = next((t for t in tags if str(t.pk) == tag_id), None)

        customers = self.visible_customers().prefetch_related("tag_set")
        if status in Customer.Status.values:
            customers = customers.filter(status=status)
        else:
            status = ""
            customers = customers.exclude(status=Customer.Status.ANONYMIZED)
        if tag is not None:
            customers = customers.filter(customer_tags__tag=tag)
        customers = (
            search_filter(customers, query)
            .annotate(last_visit=last_visit_annotation())
            .order_by("last_name", "first_name", "created_at")
        )
        page = Paginator(customers, self.page_size).get_page(self.request.GET.get("page"))
        return super().get_context_data(**kwargs) | {
            "page_obj": page,
            "query": query,
            "filtered": bool(query or status or tag),
            "can_manage": self.can_add(),
            "heading": self.heading,
            "filters": [
                {
                    "name": "status",
                    "label": "Status",
                    "value": status,
                    "options": Customer.Status.choices,
                    "all_label": "Active, inactive and archived",
                },
                {
                    "name": "tag",
                    "label": "Tag",
                    "value": str(tag.pk) if tag else "",
                    "options": [(str(t.pk), t.name) for t in tags],
                    "all_label": "Any tag",
                },
            ],
        }


class ProviderCustomerListView(CustomerListView):
    """``/staff/customers/`` (M5.3): only the customers assigned to the signed-in provider or
    seen by them, whatever else their role may see. Details open the usual customer page."""

    heading = "My customers"

    def has_page_access(self, tenant):
        return is_provider_here(tenant)

    def visible_customers(self):
        return customers_of_provider(self.tenant.organization, user=self.tenant.user)

    def can_add(self) -> bool:
        return False


# -- Create and edit ------------------------------------------------------------------------


class CustomerFormMixin(CustomerManageMixin):
    template_name = "crm/customer_form.html"

    def render_form(self, form, *, status=200, customer=None):
        context = {
            "form": form,
            "customer": customer,
            "organization": self.tenant.organization,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
        }
        name = f"{self.template_name}#form" if is_htmx(self.request) else self.template_name
        return render(self.request, name, context, status=status)

    def success(self, customer, message):
        messages.success(self.request, message)
        url = reverse("crm-customer-detail", args=[customer.pk])
        if is_htmx(self.request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


class CustomerCreateView(CustomerFormMixin, View):
    def form(self, data=None):
        return CustomerForm(
            data,
            organization=self.tenant.organization,
            include_tags=True,
            include_status=False,
            initial={"email_consent": False},
        )

    def get(self, request):
        return self.render_form(self.form())

    def post(self, request):
        form = self.form(request.POST)
        if form.is_valid():
            try:
                customer = create_customer(
                    organization=self.tenant.organization,
                    actor=request.user,
                    source=Customer.Source.RECEPTION,
                    **form.service_fields(),
                )
                if form.cleaned_data["tags"]:
                    set_customer_tags(
                        customer=customer, tags=form.cleaned_data["tags"], actor=request.user
                    )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.success(customer, f"{customer.display_name} was added.")
        return self.render_form(form, status=422)


class CustomerEditView(CustomerFormMixin, View):
    def get(self, request, pk):
        customer = self.get_customer(pk)
        form = CustomerForm(
            organization=self.tenant.organization, initial=CustomerForm.initial_for(customer)
        )
        return self.render_form(form, customer=customer)

    def post(self, request, pk):
        customer = self.get_customer(pk)
        form = CustomerForm(request.POST, organization=self.tenant.organization)
        if form.is_valid():
            try:
                update_customer(customer=customer, actor=request.user, **form.service_fields())
            except DomainError as error:
                form.add_service_error(error)
            else:
                customer.refresh_from_db()
                return self.success(customer, "Changes saved.")
        return self.render_form(form, status=422, customer=customer)


# -- Profile --------------------------------------------------------------------------------


class CustomerDetailView(CustomerPageMixin, TemplateView):
    template_name = "crm/customer_detail.html"

    def get_template_names(self):
        if is_htmx(self.request):
            return [f"{self.template_name}#tabs"]
        return [self.template_name]

    def get(self, request, *args, **kwargs):
        if kwargs.get("tab", "overview") not in TAB_SLUGS:
            raise Http404("No such tab")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, pk, tab="overview", **kwargs):
        customer = self.get_customer(pk)
        tabs = [
            {
                "slug": slug,
                "label": label,
                "url": customer_tab_url(customer, slug),
                "active": slug == tab,
            }
            for slug, label in TABS
        ]
        context = super().get_context_data(**kwargs) | {
            "customer": customer,
            "tab": tab,
            "tabs": tabs,
            "tab_template": f"crm/tabs/{tab if tab in TAB_TEMPLATES else 'placeholder'}.html",
            "can_manage": can_manage_customers(self.tenant)
            and customer.status != Customer.Status.ANONYMIZED,
        }
        builder = getattr(self, f"context_{tab}", None)
        if builder is not None:
            context |= builder(customer)
        return context

    def context_overview(self, customer):
        stats = customer_stats(customer)
        return {
            "stats": stats,
            "show_spend": self.tenant.has(Capability.CUSTOMERS_VIEW),
            **tags_context(self.request, self.tenant, customer),
        }

    def context_appointments(self, customer):
        appointments = (
            bookings_visible_to(self.request)
            .filter(customer=customer)
            .select_related("service", "staff__user")
            .order_by("-start_datetime")
        )
        page = Paginator(appointments, 25).get_page(self.request.GET.get("page"))
        return {"page_obj": page, "sees_all": self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL)}

    def context_notes(self, customer):
        return notes_context(self.request, self.tenant, customer)

    def context_activity(self, customer):
        entries = customer_timeline(
            customer, include_internal=can_read_internal_notes(self.request)
        )
        page = Paginator(entries, 50).get_page(self.request.GET.get("page"))
        return {"page_obj": page, "entries": describe_activity(page, customer)}

    def context_communications(self, customer):
        entries = customer_timeline(
            customer, include_internal=can_read_internal_notes(self.request)
        ).filter(kind__in=COMMUNICATION_KINDS)
        page = Paginator(entries, 50).get_page(self.request.GET.get("page"))
        return {"page_obj": page, "entries": describe_activity(page, customer)}

    def context_forms(self, customer):
        from customer_forms.selectors import customer_assignments

        return {
            "assignments": customer_assignments(customer),
            "can_read_answers": self.tenant.has(Capability.CUSTOMERS_VIEW),
            "can_send_forms": can_manage_customers(self.tenant)
            and customer.status != Customer.Status.ANONYMIZED,
        }


TAB_TEMPLATES = {"overview", "appointments", "notes", "activity", "communications", "forms"}


def customer_tab_url(customer, tab: str) -> str:
    if tab == "overview":
        return reverse("crm-customer-detail", args=[customer.pk])
    return reverse("crm-customer-tab", args=[customer.pk, tab])


def describe_activity(entries, customer) -> list[dict]:
    """Readable timeline rows. Entries hold ids and codes only (docs/CRM.md), so tag names and
    field labels are looked up here, at display time."""
    tag_names = {str(tag.pk): tag.name for tag in list_tags(customer.organization)}
    field_labels = {
        field.name: str(field.verbose_name).capitalize() for field in Customer._meta.fields
    }
    note_types = dict(CustomerNote.NoteType.choices)
    from customer_forms.models import FormTemplate

    form_names = {
        str(pk): name
        for pk, name in FormTemplate.objects.filter(organization=customer.organization).values_list(
            "pk", "name"
        )
    }
    kind = CustomerActivity.Kind
    rows = []
    for entry in entries:
        data = entry.metadata or {}
        detail = ""
        if entry.kind in (kind.TAG_ADDED, kind.TAG_REMOVED):
            detail = tag_names.get(entry.subject_id, "a deleted tag")
        elif entry.kind == kind.PROFILE_UPDATED:
            detail = ", ".join(field_labels.get(f, f) for f in data.get("fields", []))
        elif entry.kind == kind.CONSENT_CHANGED:
            detail = ", ".join(
                f"{field.replace('_consent', '').replace('_', ' ')} {'on' if on else 'off'}"
                for field, on in data.items()
            )
        elif entry.kind == kind.NOTE_CREATED:
            detail = note_types.get(data.get("note_type"), "")
        elif entry.kind == kind.CUSTOMER_MERGED:
            moved = data.get("appointments_moved", 0)
            detail = f"{moved} appointment{'s' if moved != 1 else ''} moved"
        elif data.get("form"):  # a completed form, or the email asking for it
            detail = form_names.get(data["form"], "a deleted form")
        elif data.get("reference"):
            detail = data["reference"]
        rows.append(
            {
                "entry": entry,
                "label": entry.get_kind_display(),
                "detail": detail,
                "actor": entry.actor.get_full_name() or entry.actor.email if entry.actor else "",
            }
        )
    return rows


# -- Tags -----------------------------------------------------------------------------------


def tags_context(request, tenant, customer, form=None) -> dict:
    can_edit = can_manage_customers(tenant) and customer.status != Customer.Status.ANONYMIZED
    if can_edit and form is None:
        form = CustomerTagsForm(
            organization=tenant.organization,
            initial={"tags": list(customer.tag_set.values_list("pk", flat=True))},
        )
    return {
        "customer": customer,
        "customer_tags": list(customer.tag_set.order_by("name")),
        "tags_form": form if can_edit else None,
    }


class CustomerTagsView(CustomerManageMixin, View):
    """POST the full set of ticked tags (``tags``), or one ``new_tag`` name to create (or
    reuse, if it already exists) and add."""

    def post(self, request, pk):
        customer = self.get_customer(pk)
        new_tag = request.POST.get("new_tag", "").strip()
        try:
            if new_tag:
                message = self.add_new_tag(customer, new_tag)
            else:
                form = CustomerTagsForm(request.POST, organization=self.tenant.organization)
                if not form.is_valid():
                    raise DomainError("Tag not found", code="not_found")
                set_customer_tags(
                    customer=customer, tags=form.cleaned_data["tags"], actor=request.user
                )
                message = "Tags updated."
        except DomainError as error:
            if not is_htmx(request):
                messages.error(request, error.message)
                return redirect("crm-customer-detail", pk=customer.pk)
            return toast(HttpResponse(status=204), error.message, "error")
        if not is_htmx(request):
            messages.success(request, message)
            return redirect("crm-customer-detail", pk=customer.pk)
        response = render(
            request,
            "crm/_tags.html",
            tags_context(request, self.tenant, customer) | {"organization": customer.organization},
        )
        return toast(response, message)

    def add_new_tag(self, customer, name) -> str:
        tag = get_or_create_tag(
            organization=self.tenant.organization, name=name, actor=self.request.user
        )
        add_customer_tag(customer=customer, tag=tag, actor=self.request.user)
        return f"Tagged “{tag.name}”."


# -- Notes ----------------------------------------------------------------------------------


def notes_context(request, tenant, customer, form=None) -> dict:
    notes = (
        notes_visible_to(request)
        .filter(customer=customer)
        .select_related("author")
        .order_by("-pinned", "-created_at")
    )
    can_write = can_write_notes(tenant) and customer.status != Customer.Status.ANONYMIZED
    if can_write and form is None:
        form = NoteForm(allow_internal=can_write_internal_notes(tenant))
    return {
        "customer": customer,
        "notes": [
            {"note": note, "can_change": can_change_note(tenant, request.user, note)}
            for note in notes
        ],
        "note_form": form if can_write else None,
    }


class NoteMixin(CustomerPageMixin):
    def get_note(self, customer, note_pk) -> CustomerNote:
        note = notes_visible_to(self.request).filter(customer=customer, pk=note_pk).first()
        if note is None:
            raise Http404("Note not found")
        if not can_change_note(self.tenant, self.request.user, note):
            raise PermissionDenied("Only the author can change this note.")
        return note

    def notes_response(self, customer, message=None, *, form=None, status=200):
        if not is_htmx(self.request):
            if message:
                messages.success(self.request, message)
            return redirect("crm-customer-tab", pk=customer.pk, tab="notes")
        response = render(
            self.request,
            "crm/_notes.html",
            notes_context(self.request, self.tenant, customer, form)
            | {"organization": customer.organization},
            status=status,
        )
        return toast(response, message) if message else response


class NoteCreateView(NoteMixin, View):
    def post(self, request, pk):
        if not can_write_notes(self.tenant):
            raise PermissionDenied(NO_ACCESS_MESSAGE)
        customer = self.get_customer(pk)
        form = NoteForm(request.POST, allow_internal=can_write_internal_notes(self.tenant))
        if form.is_valid():
            try:
                create_note(customer=customer, author=request.user, **form.cleaned_data)
            except DomainError as error:
                form.add_error(None, error.message)
            else:
                return self.notes_response(customer, "Note added.")
        if not is_htmx(request):
            messages.error(request, "The note wasn't saved: " + "; ".join(_errors(form)))
            return redirect("crm-customer-tab", pk=customer.pk, tab="notes")
        return self.notes_response(customer, form=form, status=422)


class NoteEditView(NoteMixin, View):
    template_name = "crm/note_form.html"

    def form_page(self, customer, note, form, status=200):
        name = f"{self.template_name}#form" if is_htmx(self.request) else self.template_name
        context = {"customer": customer, "note": note, "form": form}
        context["organization"] = customer.organization
        return render(self.request, name, context, status=status)

    def get(self, request, pk, note_pk):
        customer = self.get_customer(pk)
        note = self.get_note(customer, note_pk)
        form = NoteForm(
            allow_internal=can_write_internal_notes(self.tenant),
            initial={
                "content": note.content,
                "note_type": note.note_type,
                "visibility": note.visibility,
                "pinned": note.pinned,
            },
        )
        return self.form_page(customer, note, form)

    def post(self, request, pk, note_pk):
        customer = self.get_customer(pk)
        note = self.get_note(customer, note_pk)
        # can_change_note (in get_note) already refused internal notes to members who may not
        # write them, so the choice offered here is simply what the member may write.
        form = NoteForm(request.POST, allow_internal=can_write_internal_notes(self.tenant))
        if form.is_valid():
            try:
                update_note(note=note, actor=request.user, **form.cleaned_data)
            except DomainError as error:
                form.add_error(None, error.message)
            else:
                if not is_htmx(request):
                    messages.success(request, "Note saved.")
                    return redirect("crm-customer-tab", pk=customer.pk, tab="notes")
                response = self.notes_response(customer, "Note saved.")
                response["HX-Retarget"] = "#notes"
                response["HX-Reswap"] = "outerHTML"
                return htmx_trigger(response, "modal:close", after_swap=True)
        return self.form_page(customer, note, form, status=422)


class NoteDeleteView(NoteMixin, View):
    """htmx asks for confirmation in the browser (hx-confirm). Without JavaScript the button
    leads to a confirmation page first; only its "Delete" button (confirmed=yes) deletes."""

    template_name = "crm/note_confirm_delete.html"

    def confirm_page(self, customer, note):
        context = {"customer": customer, "note": note, "organization": customer.organization}
        return render(self.request, self.template_name, context)

    def get(self, request, pk, note_pk):
        customer = self.get_customer(pk)
        return self.confirm_page(customer, self.get_note(customer, note_pk))

    def post(self, request, pk, note_pk):
        customer = self.get_customer(pk)
        note = self.get_note(customer, note_pk)
        if not is_htmx(request) and request.POST.get("confirmed") != "yes":
            return self.confirm_page(customer, note)
        delete_note(note=note, actor=request.user)
        return self.notes_response(customer, "Note deleted.")


class NotePinView(NoteMixin, View):
    def post(self, request, pk, note_pk):
        customer = self.get_customer(pk)
        note = self.get_note(customer, note_pk)
        update_note(note=note, actor=request.user, pinned=not note.pinned)
        return self.notes_response(customer, "Note unpinned." if note.pinned else "Note pinned.")


def _errors(form) -> list[str]:
    return [str(error) for errors in form.errors.values() for error in errors]


# -- Header search --------------------------------------------------------------------------


class SearchView(TenantPageMixin, TemplateView):
    """The search box in the app header: a dropdown of matches (htmx) or a results page."""

    template_name = "crm/search.html"
    limit = 8

    def get_template_names(self):
        if is_htmx(self.request):
            return [f"{self.template_name}#results"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        query = self.request.GET.get("q", "").strip()
        results = None
        if len(query) >= MIN_QUERY_LENGTH:
            results = organization_search(self.request, query, limit=self.limit)
        return super().get_context_data(**kwargs) | {
            "query": query,
            "results": results,
            "has_results": bool(results and any(results.values())),
            "min_length": MIN_QUERY_LENGTH,
        }
