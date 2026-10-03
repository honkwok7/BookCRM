"""Web forms for the form builder. They only collect input: names, options, versioning and
auditing are handled by customer_forms.services, whose errors the views attach to the form."""

from __future__ import annotations

from django import forms

from customer_forms.models import FormQuestion, FormTemplate
from services.models import Service

FORM_FIELDS_BY_ERROR_CODE = {
    "name_required": "name",
    "duplicate": "name",
    "invalid_kind": "kind",
    "invalid_service": "services",
    "invalid_type": "type",
    "label_required": "label",
    "invalid_help": "help_text",
    "invalid_options": "options",
}


class ServiceErrorsMixin:
    def add_service_error(self, error) -> None:
        field = FORM_FIELDS_BY_ERROR_CODE.get(getattr(error, "code", ""))
        self.add_error(field if field in self.fields else None, error.message)


class TemplateForm(ServiceErrorsMixin, forms.Form):
    name = forms.CharField(label="Name", max_length=150)
    kind = forms.ChoiceField(label="Kind", choices=FormTemplate.Kind.choices)
    description = forms.CharField(
        label="Introduction",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown to customers above the questions.",
    )
    services = forms.ModelMultipleChoiceField(
        label="Give it to customers who book",
        queryset=Service.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Customers receive the form when they book one of these services.",
    )
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        initial=True,
        help_text="Inactive forms are never given to customers. Past answers stay.",
    )

    def __init__(self, *args, organization, include_active=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["services"].queryset = Service.objects.filter(
            organization=organization, is_archived=False
        ).order_by("name")
        if not include_active:
            del self.fields["is_active"]

    @classmethod
    def initial_for(cls, template: FormTemplate) -> dict:
        return {
            "name": template.name,
            "kind": template.kind,
            "description": template.description,
            "services": list(template.services.all()),
            "is_active": template.is_active,
        }

    def template_fields(self) -> tuple[dict, list]:
        data = dict(self.cleaned_data)
        services = list(data.pop("services"))
        return data, services


class OptionsField(forms.CharField):
    """One option per line."""

    widget = forms.Textarea(attrs={"rows": 4})

    def to_python(self, value):
        text = super().to_python(value) or ""
        return [line for line in text.splitlines() if line.strip()]

    def prepare_value(self, value):
        return "\n".join(value) if isinstance(value, list) else value


class QuestionForm(ServiceErrorsMixin, forms.Form):
    label = forms.CharField(label="Question", max_length=300)
    type = forms.ChoiceField(label="Answer type", choices=FormQuestion.Type.choices)
    help_text = forms.CharField(
        label="Help text", max_length=500, required=False, help_text="A hint shown under it."
    )
    options = OptionsField(
        label="Options",
        required=False,
        help_text="For “One choice” and “Several choices” only: one option per line.",
    )
    required = forms.BooleanField(label="An answer is required", required=False)

    @classmethod
    def initial_for(cls, question: FormQuestion) -> dict:
        return {
            "label": question.label,
            "type": question.type,
            "help_text": question.help_text,
            "options": list(question.options),
            "required": question.required,
        }
