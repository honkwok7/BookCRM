"""Form reads, always scoped to one organization."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Max, Prefetch, Q, QuerySet

from customer_forms.models import FormQuestion, FormTemplate, FormVersion


def forms_for(organization) -> QuerySet[FormTemplate]:
    """Each form with ``published_number`` (latest published version, or None) and
    ``draft_count`` (1 while a draft exists) worked out in the query, for lists."""
    return (
        FormTemplate.objects.filter(organization=organization)
        .annotate(
            published_number=Max(
                "versions__number", filter=Q(versions__published_at__isnull=False)
            ),
            draft_count=Count("versions", filter=Q(versions__published_at__isnull=True)),
        )
        .prefetch_related("services")
    )


@dataclass
class FormState:
    """What the builder shows: the draft when there is one, else the latest published version."""

    template: FormTemplate
    published: FormVersion | None
    draft: FormVersion | None

    @property
    def shown(self) -> FormVersion | None:
        return self.draft or self.published

    @property
    def questions(self) -> list[FormQuestion]:
        return list(self.shown.questions.all()) if self.shown else []

    @property
    def has_changes(self) -> bool:
        """The draft differs from the latest published version (or nothing is published)."""
        if self.draft is None:
            return False
        if self.published is None:
            return True
        return _signature(self.draft) != _signature(self.published)


def _signature(version: FormVersion):
    return [
        (q.key, q.type, q.label, q.help_text, q.required, list(q.options))
        for q in version.questions.all()
    ]


def form_state(template: FormTemplate) -> FormState:
    questions = Prefetch("questions", queryset=FormQuestion.objects.order_by("position"))
    versions = list(template.versions.prefetch_related(questions).order_by("-number"))
    draft = next((v for v in versions if v.published_at is None), None)
    published = next((v for v in versions if v.published_at is not None), None)
    return FormState(template=template, published=published, draft=draft)


def customer_assignments(customer) -> QuerySet:
    """A customer's forms, newest first, with what the CRM tab shows."""
    from customer_forms.models import FormAssignment

    return (
        FormAssignment.objects.filter(customer=customer)
        .select_related("template", "version", "booking", "assigned_by", "submission")
        .order_by("-created_at")
    )


def answer_rows(assignment) -> list[dict]:
    """The questions asked, in order, with the customer's answers as text."""
    from customer_forms.answers import display_value

    submission = getattr(assignment, "submission", None)
    answers = {}
    if submission is not None:
        answers = {answer.question_id: answer.value for answer in submission.answers.all()}
    return [
        {"question": question, "answer": display_value(question, answers.get(question.pk))}
        for question in assignment.version.questions.order_by("position")
    ]
