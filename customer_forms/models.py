"""Forms a business asks its customers to fill in: intake, consent and questionnaires (M6.1).

A ``FormTemplate`` has numbered ``FormVersion``s. The builder edits the one *draft* version;
publishing freezes it, and editing a published form starts a new draft copied from the latest
published version. Customers are always given a published version (M6.2), so their answers
point at exactly the questions they saw.

Write through ``customer_forms.services``: validation, versioning, locking and auditing.
"""

import uuid

from django.db import models
from django.db.models import Q

from core.models import BaseUUIDModel


class FormTemplate(BaseUUIDModel):
    class Kind(models.TextChoices):
        INTAKE = "intake", "Intake"
        CONSENT = "consent", "Consent"
        QUESTIONNAIRE = "questionnaire", "Questionnaire"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="form_templates"
    )
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.INTAKE)
    description = models.TextField(blank=True, help_text="Shown to customers above the form.")
    # Inactive forms are never given to customers; their answers stay.
    is_active = models.BooleanField(default=True)
    # Booking one of these services gives the customer this form (M6.2).
    services = models.ManyToManyField("services.Service", blank=True, related_name="forms")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "organization", models.functions.Lower("name"), name="form_name_unique_per_org"
            )
        ]

    def __str__(self) -> str:
        return self.name


class FormVersion(BaseUUIDModel):
    template = models.ForeignKey(FormTemplate, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    # Empty: the draft, still being edited. Set: frozen, can be given to customers.
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["template", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["template", "number"], name="form_version_number"),
            # At most one draft per form.
            models.UniqueConstraint(
                fields=["template"],
                condition=Q(published_at__isnull=True),
                name="form_one_draft",
            ),
        ]

    @property
    def is_draft(self) -> bool:
        return self.published_at is None

    def __str__(self) -> str:
        return f"{self.template} v{self.number}"


class FormQuestion(BaseUUIDModel):
    class Type(models.TextChoices):
        TEXT = "text", "Short answer"
        TEXTAREA = "textarea", "Paragraph"
        NUMBER = "number", "Number"
        DATE = "date", "Date"
        YES_NO = "yes_no", "Yes or no"
        SELECT = "select", "One choice"
        MULTI_SELECT = "multi_select", "Several choices"
        # The customer types their full name; a drawn signature may replace it later.
        SIGNATURE = "signature_placeholder", "Signature (typed name)"

    CHOICE_TYPES = frozenset({Type.SELECT, Type.MULTI_SELECT})

    version = models.ForeignKey(FormVersion, on_delete=models.CASCADE, related_name="questions")
    # The same question across versions: copied into each new draft, so a question can be
    # followed from version to version (and answers compared).
    key = models.UUIDField(default=uuid.uuid4, editable=False)
    position = models.PositiveIntegerField()
    type = models.CharField(max_length=30, choices=Type.choices)
    label = models.CharField(max_length=300)
    help_text = models.CharField(max_length=500, blank=True)
    required = models.BooleanField(default=False)
    # Choice questions: the options, in order (a list of strings). Empty for other types.
    options = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["version", "position"]
        constraints = [
            models.UniqueConstraint(fields=["version", "key"], name="form_question_key"),
            models.UniqueConstraint(
                fields=["version", "position"],
                name="form_question_position",
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]

    def __str__(self) -> str:
        return self.label


class FormAssignment(BaseUUIDModel):
    """A published form version given to one customer (M6.2): when they book a linked service,
    or by hand from their CRM record. Completed once, through the portal or an emailed link."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        BOOKING = "booking", "Booking"
        MANUAL = "manual", "Sent by the team"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    template = models.ForeignKey(FormTemplate, on_delete=models.PROTECT, related_name="assignments")
    # The exact questions asked. Protected: a form customers were given can't be deleted.
    version = models.ForeignKey(FormVersion, on_delete=models.PROTECT, related_name="assignments")
    customer = models.ForeignKey(
        "bookings.Customer", on_delete=models.CASCADE, related_name="form_assignments"
    )
    booking = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="form_assignments",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=20, choices=Source.choices)
    assigned_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "customer", "status"])]
        constraints = [
            # One waiting copy of a form per customer.
            models.UniqueConstraint(
                fields=["template", "customer"],
                condition=Q(status="pending"),
                name="form_assignment_one_pending",
            )
        ]

    def __str__(self) -> str:
        return f"{self.template} for {self.customer_id}"


class FormSubmission(BaseUUIDModel):
    assignment = models.OneToOneField(
        FormAssignment, on_delete=models.CASCADE, related_name="submission"
    )
    submitted_at = models.DateTimeField()
    # Who sent it: their account in the portal; empty through an emailed link.
    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)


class FormAnswer(BaseUUIDModel):
    submission = models.ForeignKey(FormSubmission, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(FormQuestion, on_delete=models.CASCADE, related_name="answers")
    # text/textarea/signature: str · number: str (as typed, decimal) · date: "YYYY-MM-DD"
    # yes_no: bool · select: str · multi_select: list of str. Null: not answered.
    value = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "question"], name="form_answer_once")
        ]
