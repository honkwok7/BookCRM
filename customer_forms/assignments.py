"""Giving forms to customers and taking their answers (M6.2): the only writer of
``FormAssignment``, ``FormSubmission`` and ``FormAnswer``.

- ``assign_form``: by hand (CRM) or from a booking. Always the latest published version of an
  active form; one waiting copy per customer and form (a database constraint).
- ``assign_for_booking``: called by ``bookings.services.create_booking`` in its transaction.
  An *intake* form is asked for once per customer (skipped if one is waiting or completed);
  consent forms and questionnaires once per appointment (skipped while one is waiting).
  A rescheduled appointment asks for nothing new.
- ``submit_answers``: validates against the version's questions, stores the answers, completes
  the assignment and adds a ``form_completed`` timeline entry.

Answers are personal data: they are never written to audit or timeline metadata, and they are
deleted when the customer is anonymized (``crm.services.anonymize_customer``).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from bookings.models import Customer
from core.audit import AuditAction, record_audit
from core.exceptions import ConflictError, DomainError
from crm.activity import Kind, record_activity
from customer_forms.answers import AnswerForm
from customer_forms.models import (
    FormAnswer,
    FormAssignment,
    FormSubmission,
    FormTemplate,
)
from customer_forms.services import latest_published


class InvalidAnswers(DomainError):
    """The answers don't fit the questions; ``form`` holds the field errors."""

    def __init__(self, form: AnswerForm):
        super().__init__("Please check your answers", code="invalid_answers")
        self.form = form


def _audit(action, assignment, actor, **metadata):
    record_audit(
        action,
        organization=assignment.organization,
        actor=actor,
        target=assignment,
        metadata={
            "form": str(assignment.template_id),
            "version": assignment.version.number,
            "customer": str(assignment.customer_id),
            **metadata,
        },
    )


def assignable_forms(organization):
    """Active forms with a published version: what can be given to customers."""
    return FormTemplate.objects.filter(
        organization=organization, is_active=True, versions__published_at__isnull=False
    ).distinct()


@transaction.atomic
def assign_form(
    *,
    template: FormTemplate,
    customer: Customer,
    actor=None,
    booking=None,
    source: str = FormAssignment.Source.MANUAL,
    notify: bool = True,
) -> FormAssignment:
    """Give ``customer`` the latest published version of ``template``.

    ``notify`` emails them a link (when they have an email address). A form already waiting
    for them is a 409 ``already_waiting``."""
    if template.organization_id != customer.organization_id:
        raise DomainError("Form not found", code="invalid_form")
    if booking is not None and booking.organization_id != customer.organization_id:
        raise DomainError("Appointment not found", code="invalid_booking")
    if customer.status == Customer.Status.ANONYMIZED:
        raise DomainError("This customer was anonymized", code="anonymized")
    if not template.is_active:
        raise DomainError("This form is inactive", code="inactive")
    version = latest_published(template)
    if version is None:
        raise DomainError("Publish the form before giving it to customers", code="not_published")
    if FormAssignment.objects.filter(
        template=template, customer=customer, status=FormAssignment.Status.PENDING
    ).exists():
        raise ConflictError("This form is already waiting for them", code="already_waiting")
    try:
        with transaction.atomic():
            assignment = FormAssignment.objects.create(
                organization_id=customer.organization_id,
                template=template,
                version=version,
                customer=customer,
                booking=booking,
                source=source,
                assigned_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
    except IntegrityError as error:  # a concurrent assignment won
        raise ConflictError(
            "This form is already waiting for them", code="already_waiting"
        ) from error
    _audit(AuditAction.FORM_ASSIGNED, assignment, actor, source=source)
    if notify and customer.email:
        from notifications.services import queue_form_notification

        queue_form_notification(assignment=assignment)
    return assignment


def assign_for_booking(booking, *, actor=None, notify: bool = True) -> list[FormAssignment]:
    """The forms linked to the booked service, as described in the module docstring."""
    customer = booking.customer
    if customer is None or booking.rescheduled_from_id is not None:
        return []
    assigned = []
    forms = assignable_forms(booking.organization).filter(services=booking.service_id)
    for template in forms.order_by("name"):
        existing = FormAssignment.objects.filter(template=template, customer=customer)
        blocking = [FormAssignment.Status.PENDING]
        if template.kind == FormTemplate.Kind.INTAKE:
            blocking.append(FormAssignment.Status.COMPLETED)
        if existing.filter(status__in=blocking).exists():
            continue
        try:
            assigned.append(
                assign_form(
                    template=template,
                    customer=customer,
                    booking=booking,
                    actor=actor,
                    source=FormAssignment.Source.BOOKING,
                    notify=notify,
                )
            )
        except ConflictError:
            continue  # assigned concurrently: nothing more to do
    return assigned


@transaction.atomic
def cancel_assignment(*, assignment: FormAssignment, actor=None) -> FormAssignment:
    assignment = FormAssignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status != FormAssignment.Status.PENDING:
        raise ConflictError("Only a waiting form can be cancelled", code="not_waiting")
    assignment.status = FormAssignment.Status.CANCELLED
    assignment.cancelled_at = timezone.now()
    assignment.save(update_fields=["status", "cancelled_at", "updated_at"])
    _audit(AuditAction.FORM_ASSIGNMENT_CANCELLED, assignment, actor)
    return assignment


@transaction.atomic
def submit_answers(
    *, assignment: FormAssignment, data, user=None, ip_address: str | None = None
) -> FormSubmission:
    """Store the customer's answers. ``data`` is the posted form data (``q_<question id>``).

    Raises ``InvalidAnswers`` (with the bound form) when they don't fit, 409 ``not_waiting``
    when the form was already completed or cancelled."""
    user = user if getattr(user, "is_authenticated", False) else None  # a guest's link
    assignment = (
        FormAssignment.objects.select_for_update()
        .select_related("version", "customer", "organization")
        .get(pk=assignment.pk)
    )
    if assignment.status != FormAssignment.Status.PENDING:
        raise ConflictError("This form is no longer open", code="not_waiting")
    form = AnswerForm(assignment.version, data)
    if not form.is_valid():
        raise InvalidAnswers(form)
    now = timezone.now()
    submission = FormSubmission.objects.create(
        assignment=assignment,
        submitted_at=now,
        submitted_by=user,
        ip_address=ip_address,
    )
    FormAnswer.objects.bulk_create(
        FormAnswer(submission=submission, question=question, value=value)
        for question, value in form.to_values().items()
    )
    assignment.status = FormAssignment.Status.COMPLETED
    assignment.completed_at = now
    assignment.save(update_fields=["status", "completed_at", "updated_at"])
    _audit(AuditAction.FORM_COMPLETED, assignment, user)
    record_activity(
        Kind.FORM_COMPLETED,
        customer=assignment.customer,
        actor=user,
        subject=assignment,
        metadata={"form": str(assignment.template_id), "version": assignment.version.number},
    )
    return submission
