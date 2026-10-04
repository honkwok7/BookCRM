"""Forms from the customer's side and the CRM's (M6.2).

- ``/forms/<signed link>/``: fill in a form without signing in (the emailed link). Anyone with
  the link can open it while it is valid and the form is still waiting; nothing else about the
  customer is shown.
- CRM: send a form to a customer (``customers.manage``), read their answers (``customers.view``;
  providers without it see that a form exists, not the answers), cancel a waiting form.

The portal's form pages live in ``portal.views`` and share ``customer_forms/_answer_form.html``.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from core.audit import client_ip
from core.exceptions import DomainError
from core.web import is_htmx
from crm.web_views import CustomerManageMixin, CustomerPageMixin
from customer_forms.answers import AnswerForm
from customer_forms.assignments import (
    InvalidAnswers,
    assign_form,
    assignable_forms,
    cancel_assignment,
    submit_answers,
)
from customer_forms.links import read_token
from customer_forms.models import FormAssignment, FormSubmission
from customer_forms.selectors import answer_rows
from organizations.permissions import Capability

FILL_TEMPLATE = "customer_forms/fill.html"


def fill_page(
    request,
    assignment,
    *,
    form=None,
    action,
    status=200,
    done=False,
    template=FILL_TEMPLATE,
    **extra,
):
    """The customer's form page (public link, or the portal with its own template)."""
    context = {
        "assignment": assignment,
        "organization": assignment.organization,
        "form": form or AnswerForm(assignment.version),
        "action": action,
        "done": done,
        **extra,
    }
    return render(request, template, context, status=status)


def take_answers(request, assignment, *, action, **extra):
    """Validate and store a posted form; re-render it with errors, or say thank you."""
    try:
        submit_answers(
            assignment=assignment,
            data=request.POST,
            user=request.user,
            ip_address=client_ip(request),
        )
    except InvalidAnswers as error:
        return fill_page(request, assignment, form=error.form, action=action, status=422, **extra)
    except DomainError:
        assignment.refresh_from_db()
        return fill_page(request, assignment, action=action, status=409, **extra)
    assignment.refresh_from_db()
    return fill_page(request, assignment, action=action, done=True, **extra)


class LinkFillView(View):
    """``/forms/<token>/``: the emailed link."""

    def assignment(self, token):
        assignment_id, expired = read_token(token)
        if expired:
            return None, True
        assignment = (
            FormAssignment.objects.select_related("organization", "template", "version", "customer")
            .filter(
                pk=assignment_id,
                organization__is_active=True,
                organization__is_suspended=False,
            )
            .first()
            if assignment_id
            else None
        )
        if assignment is None:
            raise Http404("Form not found")
        return assignment, False

    def expired(self, request):
        return render(request, "customer_forms/link_expired.html", status=410)

    def get(self, request, token):
        assignment, expired = self.assignment(token)
        if expired:
            return self.expired(request)
        return fill_page(request, assignment, action=request.path)

    def post(self, request, token):
        assignment, expired = self.assignment(token)
        if expired:
            return self.expired(request)
        return take_answers(request, assignment, action=request.path)


# -- CRM ------------------------------------------------------------------------------------


class SendForm(forms.Form):
    template = forms.ModelChoiceField(label="Form", queryset=None, empty_label=None)
    notify = forms.BooleanField(
        label="Email them a link to fill it in", required=False, initial=True
    )

    def __init__(self, *args, organization, can_email, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = assignable_forms(organization).order_by("name")
        if not can_email:
            self.fields["notify"].disabled = True
            self.fields["notify"].initial = False
            self.fields["notify"].help_text = "They have no email address on file."


class CustomerFormMixin:
    def get_assignment(self, customer, pk) -> FormAssignment:
        assignment = (
            FormAssignment.objects.select_related("template", "version", "booking", "customer")
            .filter(customer=customer, pk=pk)
            .first()
        )
        if assignment is None:
            raise Http404("Form not found")
        return assignment

    def back(self, customer, message=""):
        if message:
            messages.success(self.request, message)
        url = reverse("crm-customer-tab", args=[customer.pk, "forms"])
        if is_htmx(self.request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


class SendFormView(CustomerFormMixin, CustomerManageMixin, View):
    template_name = "customer_forms/send_form.html"

    def form(self, customer, data=None):
        return SendForm(data, organization=self.tenant.organization, can_email=bool(customer.email))

    def render_form(self, customer, form, status=200):
        context = {
            "customer": customer,
            "form": form,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
        }
        name = f"{self.template_name}#form" if is_htmx(self.request) else self.template_name
        return render(self.request, name, context, status=status)

    def get(self, request, pk):
        customer = self.get_customer(pk)
        return self.render_form(customer, self.form(customer))

    def post(self, request, pk):
        customer = self.get_customer(pk)
        form = self.form(customer, request.POST)
        if form.is_valid():
            try:
                assignment = assign_form(
                    template=form.cleaned_data["template"],
                    customer=customer,
                    actor=request.user,
                    notify=form.cleaned_data["notify"],
                )
            except DomainError as error:
                form.add_error("template", error.message)
            else:
                sent = " and emailed them a link" if form.cleaned_data["notify"] else ""
                return self.back(customer, f"Gave them “{assignment.template.name}”{sent}.")
        return self.render_form(customer, form, status=422)


class AnswersView(CustomerFormMixin, CustomerPageMixin, View):
    def get(self, request, pk, assignment_pk):
        if not self.tenant.has(Capability.CUSTOMERS_VIEW):
            raise PermissionDenied("You can't read customers' form answers.")
        customer = self.get_customer(pk)
        assignment = self.get_assignment(customer, assignment_pk)
        return render(
            request,
            "customer_forms/answers.html",
            {
                "customer": customer,
                "assignment": assignment,
                "rows": answer_rows(assignment),
                "has_submission": FormSubmission.objects.filter(assignment=assignment).exists(),
                "can_manage": self.tenant.has(Capability.CUSTOMERS_MANAGE),
            },
        )


class CancelAssignmentView(CustomerFormMixin, CustomerManageMixin, View):
    def post(self, request, pk, assignment_pk):
        customer = self.get_customer(pk)
        assignment = self.get_assignment(customer, assignment_pk)
        try:
            cancel_assignment(assignment=assignment, actor=request.user)
        except DomainError as error:
            messages.error(request, error.message)
            return self.back(customer)
        return self.back(customer, f"“{assignment.template.name}” was cancelled.")
