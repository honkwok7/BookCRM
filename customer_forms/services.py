"""Form writes: the one place that creates, changes, versions, publishes or removes forms.

The API, the web builder and ``seed_demo`` call these functions, so validation, versioning,
locking and auditing apply the same way everywhere. Errors are ``DomainError`` (400) or
``ConflictError`` (409) with a stable ``code``.

Versioning: question changes always land on the form's draft. ``draft_for`` returns it, or
starts one copied from the latest published version, so a published version never changes.
Every write locks the template row, so two editors can't create two drafts or clash on
question positions.

Auditing: creating, changing, publishing, discarding and deleting a form are audited; single
question edits are not (they are drafts until published, and publishing records the count).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Max, ProtectedError
from django.utils import timezone

from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.exceptions import ConflictError, DomainError
from customer_forms.models import FormQuestion, FormTemplate, FormVersion
from services.models import Service

TEMPLATE_FIELDS = frozenset({"name", "kind", "description", "is_active"})
QUESTION_FIELDS = frozenset({"type", "label", "help_text", "required", "options"})
MAX_QUESTIONS = 100
MAX_OPTIONS = 50
MAX_OPTION_LENGTH = 200


def _check_fields(fields: dict, allowed) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise DomainError(f"Unknown fields: {', '.join(sorted(unknown))}", code="invalid")


def _audit(action, template, actor, **kwargs):
    record_audit(action, organization=template.organization, actor=actor, target=template, **kwargs)


def _lock(template: FormTemplate) -> FormTemplate:
    return (
        FormTemplate.objects.select_for_update().select_related("organization").get(pk=template.pk)
    )


# -- Templates ------------------------------------------------------------------------------


def _check_template(template: FormTemplate) -> None:
    template.name = (template.name or "").strip()
    if not template.name:
        raise DomainError("A form name is required", code="name_required")
    if template.kind not in FormTemplate.Kind.values:
        raise DomainError("Unknown form kind", code="invalid_kind")
    template.description = (template.description or "").strip()
    duplicate = FormTemplate.objects.filter(
        organization_id=template.organization_id, name__iexact=template.name
    ).exclude(pk=template.pk)
    if duplicate.exists():
        raise ConflictError("A form with this name already exists", code="duplicate")


def _save_template(template: FormTemplate) -> None:
    """Save; a unique-name race is a 409, never a 500."""
    try:
        with transaction.atomic():
            template.save()
    except IntegrityError as error:
        raise ConflictError("A form with this name already exists", code="duplicate") from error


def _check_services(organization, services) -> list[Service]:
    services = list(services)
    ids = {service.pk for service in services}
    if Service.objects.filter(organization=organization, pk__in=ids).count() != len(ids):
        raise DomainError("Service not found", code="invalid_service")
    return services


@transaction.atomic
def create_template(*, organization, actor=None, services=None, **fields) -> FormTemplate:
    """A new form with an empty draft (version 1)."""
    _check_fields(fields, TEMPLATE_FIELDS)
    template = FormTemplate(organization=organization, **fields)
    _check_template(template)
    services = _check_services(organization, services or [])
    _save_template(template)
    template.services.set(services)
    FormVersion.objects.create(template=template, number=1)
    _audit(AuditAction.FORM_CREATED, template, actor, metadata={"name": template.name})
    return template


@transaction.atomic
def update_template(*, template: FormTemplate, actor=None, services=None, **changes):
    """``services=None`` leaves the linked services unchanged; ``[]`` unlinks them all."""
    _check_fields(changes, TEMPLATE_FIELDS)
    template = _lock(template)
    before = snapshot(template)
    for name, value in changes.items():
        setattr(template, name, value)
    _check_template(template)
    _save_template(template)
    diff = diff_snapshots(before, snapshot(template))
    if services is not None:
        services = _check_services(template.organization, services)
        old = set(template.services.values_list("pk", flat=True))
        new = {service.pk for service in services}
        if old != new:
            template.services.set(services)
            diff["services"] = [sorted(map(str, old)), sorted(map(str, new))]
    if diff:
        _audit(AuditAction.FORM_UPDATED, template, actor, changes=diff)
    return template


@transaction.atomic
def delete_template(*, template: FormTemplate, actor=None) -> None:
    """Delete a form that was never given to anyone; otherwise deactivate it instead (409)."""
    template = _lock(template)
    _audit(AuditAction.FORM_DELETED, template, actor, metadata={"name": template.name})
    try:
        with transaction.atomic():
            template.delete()
    except ProtectedError as error:
        raise ConflictError(
            "Customers have been given this form. Make it inactive instead.", code="in_use"
        ) from error


# -- Versions -------------------------------------------------------------------------------


def latest_published(template: FormTemplate) -> FormVersion | None:
    return template.versions.filter(published_at__isnull=False).order_by("-number").first()


def current_draft(template: FormTemplate) -> FormVersion | None:
    return template.versions.filter(published_at__isnull=True).first()


def draft_for(template: FormTemplate) -> FormVersion:
    """The form's draft, started from the latest published version when there is none.

    Call with the template locked (every write below does)."""
    draft = current_draft(template)
    if draft is not None:
        return draft
    published = latest_published(template)
    number = (template.versions.aggregate(top=Max("number"))["top"] or 0) + 1
    draft = FormVersion.objects.create(template=template, number=number)
    if published is not None:
        FormQuestion.objects.bulk_create(
            FormQuestion(
                version=draft,
                key=question.key,
                position=question.position,
                type=question.type,
                label=question.label,
                help_text=question.help_text,
                required=question.required,
                options=list(question.options),
            )
            for question in published.questions.all()
        )
    return draft


@transaction.atomic
def publish(*, template: FormTemplate, actor=None) -> FormVersion:
    """Freeze the draft so it can be given to customers."""
    template = _lock(template)
    draft = current_draft(template)
    if draft is None:
        raise DomainError("There are no changes to publish", code="nothing_to_publish")
    count = draft.questions.count()
    if not count:
        raise DomainError("Add at least one question before publishing", code="no_questions")
    draft.published_at = timezone.now()
    draft.published_by = actor
    draft.save(update_fields=["published_at", "published_by", "updated_at"])
    _audit(
        AuditAction.FORM_PUBLISHED,
        template,
        actor,
        metadata={"version": draft.number, "questions": count},
    )
    return draft


@transaction.atomic
def discard_draft(*, template: FormTemplate, actor=None) -> None:
    """Throw away unpublished changes. A form that was never published keeps an empty draft."""
    template = _lock(template)
    draft = current_draft(template)
    if draft is None:
        raise DomainError("There are no changes to discard", code="nothing_to_discard")
    if latest_published(template) is None:
        draft.questions.all().delete()
    else:
        draft.delete()
    _audit(AuditAction.FORM_DRAFT_DISCARDED, template, actor, metadata={"version": draft.number})


# -- Questions ------------------------------------------------------------------------------


def clean_options(options) -> list[str]:
    """Strip, drop empty lines and check the list (used by the forms and the API too)."""
    if options is None:
        return []
    if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
        raise DomainError("Options must be a list of text", code="invalid_options")
    cleaned = [item.strip() for item in options if item.strip()]
    if len(cleaned) > MAX_OPTIONS:
        raise DomainError(f"At most {MAX_OPTIONS} options", code="invalid_options")
    if any(len(item) > MAX_OPTION_LENGTH for item in cleaned):
        raise DomainError(
            f"Each option can be at most {MAX_OPTION_LENGTH} characters", code="invalid_options"
        )
    if len({item.casefold() for item in cleaned}) != len(cleaned):
        raise DomainError("Each option must be different", code="invalid_options")
    return cleaned


def _check_question(question: FormQuestion) -> None:
    if question.type not in FormQuestion.Type.values:
        raise DomainError("Unknown question type", code="invalid_type")
    question.label = (question.label or "").strip()
    if not question.label:
        raise DomainError("The question needs a label", code="label_required")
    if len(question.label) > 300:
        raise DomainError("The question can be at most 300 characters", code="label_required")
    question.help_text = (question.help_text or "").strip()
    if len(question.help_text) > 500:
        raise DomainError("Help text can be at most 500 characters", code="invalid_help")
    question.options = clean_options(question.options)
    if question.type in FormQuestion.CHOICE_TYPES:
        if len(question.options) < 2:
            raise DomainError("Give at least two options", code="invalid_options")
    elif question.options:
        raise DomainError("Only choice questions have options", code="invalid_options")


def _draft_question(question: FormQuestion) -> tuple[FormTemplate, FormVersion, FormQuestion]:
    """Lock the form and find this question in its draft (by its key).

    The builder and the API show the draft when there is one, else the latest published
    version; a change to a published question lands on its copy in a new draft."""
    template = _lock(question.version.template)
    draft = draft_for(template)
    copy = draft.questions.filter(key=question.key).first()
    if copy is None:
        raise DomainError("This question has changed since; reload the form", code="stale")
    return template, draft, copy


@transaction.atomic
def add_question(*, template: FormTemplate, actor=None, **fields) -> FormQuestion:
    _check_fields(fields, QUESTION_FIELDS)
    template = _lock(template)
    draft = draft_for(template)
    count = draft.questions.count()
    if count >= MAX_QUESTIONS:
        raise DomainError(f"A form can have at most {MAX_QUESTIONS} questions", code="too_many")
    question = FormQuestion(version=draft, position=count + 1, **fields)
    _check_question(question)
    question.save()
    return question


@transaction.atomic
def update_question(*, question: FormQuestion, actor=None, **changes) -> FormQuestion:
    _check_fields(changes, QUESTION_FIELDS)
    _template, _draft, question = _draft_question(question)
    for name, value in changes.items():
        setattr(question, name, value)
    _check_question(question)
    question.save()
    return question


@transaction.atomic
def delete_question(*, question: FormQuestion, actor=None) -> None:
    _template, draft, question = _draft_question(question)
    question.delete()
    _renumber(draft)


@transaction.atomic
def move_question(*, question: FormQuestion, actor=None, direction: str) -> FormQuestion:
    """``direction``: "up" or "down". At the top or bottom it stays where it is."""
    if direction not in ("up", "down"):
        raise DomainError("Move up or down", code="invalid_direction")
    _template, draft, question = _draft_question(question)
    target = question.position - 1 if direction == "up" else question.position + 1
    neighbour = draft.questions.filter(position=target).first()
    if neighbour is not None:
        # The position constraint is deferred, so the swap is checked at commit.
        neighbour.position, question.position = question.position, neighbour.position
        neighbour.save(update_fields=["position", "updated_at"])
        question.save(update_fields=["position", "updated_at"])
    return question


def _renumber(version: FormVersion) -> None:
    for number, question in enumerate(version.questions.order_by("position"), start=1):
        if question.position != number:
            question.position = number
            question.save(update_fields=["position", "updated_at"])
