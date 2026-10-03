"""A published form version as a Django form the customer fills in, and answers for display.

``AnswerForm(version, data)`` has one field per question (``q_<question id>``) with the
question's type, options and "required". ``to_values`` turns cleaned data into what is stored
(``FormAnswer.value``); ``display_value`` turns a stored value back into text.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django import forms
from django.utils.formats import date_format

from customer_forms.models import FormQuestion

TEXT_LIMIT = 1000
PARAGRAPH_LIMIT = 5000
Type = FormQuestion.Type


def _field(question: FormQuestion) -> forms.Field:
    common = {
        "label": question.label,
        "help_text": question.help_text,
        "required": question.required,
    }
    choices = [(option, option) for option in question.options]
    match question.type:
        case Type.TEXT:
            return forms.CharField(max_length=TEXT_LIMIT, **common)
        case Type.TEXTAREA:
            return forms.CharField(
                max_length=PARAGRAPH_LIMIT, widget=forms.Textarea(attrs={"rows": 4}), **common
            )
        case Type.NUMBER:
            return forms.DecimalField(max_digits=15, decimal_places=4, **common)
        case Type.DATE:
            return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **common)
        case Type.YES_NO:
            return forms.TypedChoiceField(
                choices=[("yes", "Yes"), ("no", "No")],
                coerce=lambda value: value == "yes",
                empty_value=None,
                widget=forms.RadioSelect,
                **common,
            )
        case Type.SELECT:
            return forms.ChoiceField(choices=choices, widget=forms.RadioSelect, **common)
        case Type.MULTI_SELECT:
            return forms.MultipleChoiceField(
                choices=choices, widget=forms.CheckboxSelectMultiple, **common
            )
        case Type.SIGNATURE:
            common["help_text"] = question.help_text or "Type your full name to sign."
            return forms.CharField(max_length=200, **common)
    raise ValueError(f"Unknown question type {question.type}")


class AnswerForm(forms.Form):
    def __init__(self, version, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.questions = list(version.questions.order_by("position"))
        for question in self.questions:
            self.fields[f"q_{question.pk}"] = _field(question)

    def rows(self):
        """(question, bound field) in order, for the template."""
        return [(question, self[f"q_{question.pk}"]) for question in self.questions]

    def to_values(self) -> dict:
        """{question: stored value} from cleaned data; unanswered optional questions are None."""
        values = {}
        for question in self.questions:
            value = self.cleaned_data.get(f"q_{question.pk}")
            if value in ("", [], None):
                value = None
            elif isinstance(value, Decimal):
                value = format(value.normalize(), "f")
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            values[question] = value
        return values


def display_value(question: FormQuestion, value) -> str:
    if value is None or value == [] or value == "":
        return ""
    if question.type == Type.YES_NO:
        return "Yes" if value else "No"
    if question.type == Type.DATE:
        try:
            return date_format(date.fromisoformat(value), "j M Y")
        except TypeError, ValueError:
            return str(value)
    if isinstance(value, list):
        return ", ".join(value)
    return str(value)
