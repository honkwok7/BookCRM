"""The form builder: the list of forms, and one form's questions, settings and publishing.

Reads go through customer_forms.selectors and writes through customer_forms.services, exactly
like the API. Every page needs ``forms.manage``. A form or question of another organization
is a 404.

htmx: dialogs (new form, settings, a question) post with ``hx-post`` and send the browser to
the builder on success (HX-Redirect). Moving or removing a question, publishing and
discarding re-render the builder's ``#builder`` section in place. Without JavaScript the same
URLs work as ordinary pages and redirects.
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
from customer_forms.forms import QuestionForm, TemplateForm
from customer_forms.models import FormQuestion, FormTemplate
from customer_forms.selectors import form_state, forms_for
from customer_forms.services import (
    add_question,
    create_template,
    delete_question,
    delete_template,
    discard_draft,
    move_question,
    publish,
    update_question,
    update_template,
)
from organizations.permissions import Capability


class FormPageMixin(TenantPageMixin):
    required_capabilities = (Capability.FORMS_MANAGE,)
    form_template = ""

    def get_form_template(self, pk) -> FormTemplate:
        template = FormTemplate.objects.filter(organization=self.tenant.organization, pk=pk).first()
        if template is None:
            raise Http404("Form not found")
        return template

    def get_question(self, pk) -> FormQuestion:
        question = (
            FormQuestion.objects.select_related("version__template")
            .filter(version__template__organization=self.tenant.organization, pk=pk)
            .first()
        )
        if question is None:
            raise Http404("Question not found")
        return question

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

    def go(self, url, message=""):
        if message:
            messages.success(self.request, message)
        if is_htmx(self.request):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    def builder_url(self, template) -> str:
        return reverse("app-form-builder", args=[template.pk])


def builder_context(template) -> dict:
    state = form_state(template)
    return {"template": template, "state": state, "questions": state.questions}


class BuilderSectionMixin(FormPageMixin):
    """Actions that change the questions or the version: re-render ``#builder`` for htmx."""

    def done(self, template, message="", *, error=""):
        if not is_htmx(self.request):
            if error:
                messages.error(self.request, error)
            return self.go(self.builder_url(template), message)
        context = builder_context(template) | {
            "organization": self.tenant.organization,
            "section_message": message,
            "section_error": error,
        }
        return render(self.request, "customer_forms/form_builder.html#builder", context)


class FormListView(FormPageMixin, TemplateView):
    template_name = "customer_forms/form_list.html"

    def get_context_data(self, **kwargs):
        forms = list(forms_for(self.tenant.organization).order_by("name"))
        return super().get_context_data(**kwargs) | {"forms": forms}


class FormCreateView(FormPageMixin, View):
    form_template = "customer_forms/form_settings.html"

    def form(self, data=None):
        return TemplateForm(data, organization=self.tenant.organization, include_active=False)

    def get(self, request):
        return self.render_form(self.form())

    def post(self, request):
        form = self.form(request.POST)
        if form.is_valid():
            fields, services = form.template_fields()
            try:
                template = create_template(
                    organization=self.tenant.organization,
                    actor=request.user,
                    services=services,
                    **fields,
                )
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.go(
                    self.builder_url(template), f"{template.name} was created. Add its questions."
                )
        return self.render_form(form, status=422)


class FormBuilderView(FormPageMixin, TemplateView):
    template_name = "customer_forms/form_builder.html"

    def get_context_data(self, **kwargs):
        template = self.get_form_template(self.kwargs["pk"])
        return super().get_context_data(**kwargs) | builder_context(template)


class FormSettingsView(FormPageMixin, View):
    form_template = "customer_forms/form_settings.html"

    def get(self, request, pk):
        template = self.get_form_template(pk)
        form = TemplateForm(
            organization=self.tenant.organization, initial=TemplateForm.initial_for(template)
        )
        return self.render_form(form, template=template)

    def post(self, request, pk):
        template = self.get_form_template(pk)
        form = TemplateForm(request.POST, organization=self.tenant.organization)
        if form.is_valid():
            fields, services = form.template_fields()
            try:
                update_template(template=template, actor=request.user, services=services, **fields)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.go(self.builder_url(template), "Changes saved.")
        return self.render_form(form, status=422, template=template)


class FormDeleteView(FormPageMixin, View):
    def post(self, request, pk):
        template = self.get_form_template(pk)
        try:
            delete_template(template=template, actor=request.user)
        except DomainError as error:
            messages.error(request, error.message)
            return self.go(self.builder_url(template))
        return self.go(reverse("app-form-list"), f"{template.name} was deleted.")


class FormPublishView(BuilderSectionMixin, View):
    def post(self, request, pk):
        template = self.get_form_template(pk)
        try:
            version = publish(template=template, actor=request.user)
        except DomainError as error:
            return self.done(template, error=error.message)
        return self.done(template, f"Version {version.number} is published.")


class FormDiscardView(BuilderSectionMixin, View):
    def post(self, request, pk):
        template = self.get_form_template(pk)
        try:
            discard_draft(template=template, actor=request.user)
        except DomainError as error:
            return self.done(template, error=error.message)
        return self.done(template, "Unpublished changes were discarded.")


class QuestionCreateView(FormPageMixin, View):
    form_template = "customer_forms/question_form.html"

    def get(self, request, pk):
        template = self.get_form_template(pk)
        return self.render_form(QuestionForm(initial={"type": "text"}), template=template)

    def post(self, request, pk):
        template = self.get_form_template(pk)
        form = QuestionForm(request.POST)
        if form.is_valid():
            try:
                add_question(template=template, actor=request.user, **form.cleaned_data)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.go(self.builder_url(template), "Question added.")
        return self.render_form(form, status=422, template=template)


class QuestionEditView(FormPageMixin, View):
    form_template = "customer_forms/question_form.html"

    def get(self, request, pk):
        question = self.get_question(pk)
        form = QuestionForm(initial=QuestionForm.initial_for(question))
        return self.render_form(form, template=question.version.template, question=question)

    def post(self, request, pk):
        question = self.get_question(pk)
        template = question.version.template
        form = QuestionForm(request.POST)
        if form.is_valid():
            try:
                update_question(question=question, actor=request.user, **form.cleaned_data)
            except DomainError as error:
                form.add_service_error(error)
            else:
                return self.go(self.builder_url(template), "Question saved.")
        return self.render_form(form, status=422, template=template, question=question)


class QuestionDeleteView(BuilderSectionMixin, View):
    def post(self, request, pk):
        question = self.get_question(pk)
        template = question.version.template
        try:
            delete_question(question=question, actor=request.user)
        except DomainError as error:
            return self.done(template, error=error.message)
        return self.done(template, "Question removed.")


class QuestionMoveView(BuilderSectionMixin, View):
    def post(self, request, pk):
        question = self.get_question(pk)
        template = question.version.template
        try:
            move_question(question=question, actor=request.user, direction=request.POST.get("to"))
        except DomainError as error:
            return self.done(template, error=error.message)
        return self.done(template)
