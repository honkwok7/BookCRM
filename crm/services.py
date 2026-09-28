"""CRM customer writes: the one place that creates, changes, merges or anonymizes customers.

The API, the web app, the booking engine and future AI agents all call these functions, so
duplicate detection, consent stamping and auditing are applied the same way everywhere.
Errors are ``DomainError`` (400) or ``ConflictError`` (409) with a stable ``code``.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from bookings.models import (
    Booking,
    BookingActivityLog,
    BookingStatusHistory,
    Customer,
    WaitlistEntry,
)
from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.exceptions import ConflictError, DomainError
from notifications.models import NotificationLog

CONSENT_FIELDS = ("marketing_consent", "email_consent", "sms_consent")

# Personal data: recorded in audit diffs as "changed" without values, and cleared on
# anonymization.
PII_FIELDS = (
    "first_name",
    "last_name",
    "preferred_name",
    "name",
    "birthday",
    "gender",
    "pronouns",
    "email",
    "phone",
    "secondary_phone",
    "address_line1",
    "address_line2",
    "city",
    "region",
    "postal_code",
    "country",
    "alerts",
    "notes",
    "tags",
)

# Fields callers may set through create_customer/update_customer.
EDITABLE_FIELDS = frozenset(
    {
        *(field for field in PII_FIELDS if field != "name"),
        *CONSENT_FIELDS,
        "status",
        "preferred_language",
        "preferred_contact_method",
        "assigned_staff",
        "preferred_staff",
        "source",
    }
)

ANONYMIZED_NAME = "Anonymized"


def _normalize(fields: dict) -> dict:
    for key in ("email", "phone", "secondary_phone"):
        if key in fields and fields[key] is not None:
            fields[key] = fields[key].strip()
    return fields


def _check_fields(fields: dict) -> None:
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise DomainError(f"Unknown customer fields: {', '.join(sorted(unknown))}", code="invalid")
    if fields.get("status") == Customer.Status.ANONYMIZED:
        raise DomainError("Use anonymization to anonymize a customer", code="invalid_status")


def _check_contact(customer: Customer) -> None:
    if not (customer.email or customer.phone):
        raise DomainError("An email address or a phone number is required", code="contact_required")


def _check_staff(customer: Customer) -> None:
    for field in ("assigned_staff", "preferred_staff"):
        staff = getattr(customer, field)
        if staff is not None and staff.organization_id != customer.organization_id:
            raise DomainError("Staff member not found", code="invalid_staff")


def _check_duplicate_email(customer: Customer) -> None:
    if not customer.email:
        return
    duplicates = Customer.objects.filter(
        organization_id=customer.organization_id, email__iexact=customer.email
    ).exclude(pk=customer.pk)
    if duplicates.exists():
        raise ConflictError("A customer with this email already exists", code="duplicate_email")


def _audit(action, customer, actor, **kwargs):
    record_audit(action, organization=customer.organization, actor=actor, target=customer, **kwargs)


@transaction.atomic
def create_customer(*, organization, actor=None, **fields) -> Customer:
    _check_fields(fields)
    customer = Customer(organization=organization, created_by=actor, **_normalize(fields))
    customer.sync_name()
    if not customer.name:
        raise DomainError("A first or last name is required", code="name_required")
    _check_contact(customer)
    _check_staff(customer)
    _check_duplicate_email(customer)
    if any(getattr(customer, field) for field in CONSENT_FIELDS):
        customer.consent_updated_at = timezone.now()
    customer.save()
    _audit(AuditAction.CUSTOMER_CREATED, customer, actor, metadata={"source": customer.source})
    return customer


@transaction.atomic
def update_customer(*, customer: Customer, actor=None, **changes) -> Customer:
    _check_fields(changes)
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    if customer.status == Customer.Status.ANONYMIZED:
        raise ConflictError("An anonymized customer cannot be changed", code="anonymized")

    before = snapshot(customer)
    for field, value in _normalize(changes).items():
        setattr(customer, field, value)
    if {"first_name", "last_name"} & set(changes):
        customer.name = ""  # recomputed from first/last name by sync_name
    customer.sync_name()
    if not customer.name:
        raise DomainError("A first or last name is required", code="name_required")
    _check_contact(customer)
    _check_staff(customer)
    _check_duplicate_email(customer)

    consent_changes = {
        field: getattr(customer, field)
        for field in CONSENT_FIELDS
        if getattr(customer, field) != before[field]
    }
    if consent_changes:
        customer.consent_updated_at = timezone.now()
    customer.save()

    changes = diff_snapshots(before, snapshot(customer), redact_fields=PII_FIELDS)
    if changes:
        _audit(AuditAction.CUSTOMER_UPDATED, customer, actor, changes=changes)
    if consent_changes:
        _audit(AuditAction.CUSTOMER_CONSENT_CHANGED, customer, actor, metadata=consent_changes)
    return customer


def find_or_create_customer(
    *, organization, name, email="", phone="", user=None, source="", actor=None
) -> Customer:
    """The booking engine's customer lookup: match by email, else by phone, else create.

    A match never links ``user`` to an existing record: knowing someone's email must not
    attach their history to your account.
    """
    email, phone = email.strip(), phone.strip()
    matches = Customer.objects.filter(organization=organization).exclude(
        status=Customer.Status.ANONYMIZED
    )
    if email:
        existing = matches.filter(email__iexact=email).first()
    else:
        existing = matches.filter(phone=phone).order_by("created_at").first() if phone else None
    if existing is not None:
        return existing

    first_name, last_name = Customer.split_name(name)
    try:
        with transaction.atomic():  # savepoint: a concurrent create of the same email wins
            customer = create_customer(
                organization=organization,
                actor=actor,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                source=source,
            )
    except IntegrityError, ConflictError:
        return matches.get(email__iexact=email)
    if user is not None:
        customer.user = user
        customer.save(update_fields=["user", "updated_at"])
    return customer


@transaction.atomic
def merge_customers(*, target: Customer, duplicate: Customer, actor=None) -> Customer:
    """Fold ``duplicate`` into ``target``: move its appointments, fill target's blank fields.

    Consent is never copied: a consent given on one record is not assumed for the other.
    """
    if target.pk == duplicate.pk:
        raise DomainError("A customer cannot be merged into itself", code="invalid_merge")
    if target.organization_id != duplicate.organization_id:
        raise DomainError("Customer not found", code="not_found")
    # Lock both rows in a fixed order so concurrent merges cannot deadlock.
    locked = {
        c.pk: c
        for c in Customer.objects.select_for_update().filter(pk__in=[target.pk, duplicate.pk])
    }
    target, duplicate = locked[target.pk], locked[duplicate.pk]
    if Customer.Status.ANONYMIZED in (target.status, duplicate.status):
        raise ConflictError("An anonymized customer cannot be merged", code="anonymized")
    if target.user_id and duplicate.user_id and target.user_id != duplicate.user_id:
        raise ConflictError(
            "Both customers are linked to different user accounts", code="linked_to_other_user"
        )

    before = snapshot(target)
    for field in EDITABLE_FIELDS - set(CONSENT_FIELDS) - {"status", "notes", "tags"}:
        if not getattr(target, field) and getattr(duplicate, field):
            setattr(target, field, getattr(duplicate, field))
    target.user_id = target.user_id or duplicate.user_id
    if duplicate.notes:
        target.notes = "\n\n".join(filter(None, [target.notes, duplicate.notes]))
    target.tags = list(dict.fromkeys([*target.tags, *duplicate.tags]))

    moved = Booking.objects.filter(customer=duplicate).update(customer=target)
    duplicate_id = str(duplicate.pk)
    duplicate.delete()  # before saving target: target may take over the duplicate's email
    target.save()

    _audit(
        AuditAction.CUSTOMER_MERGED,
        target,
        actor,
        metadata={"merged_customer": duplicate_id, "appointments_moved": moved},
        changes=diff_snapshots(before, snapshot(target), redact_fields=PII_FIELDS),
    )
    return target


@transaction.atomic
def anonymize_customer(*, customer: Customer, actor=None) -> Customer:
    """Replace a customer's personal data, keeping appointments for aggregate reporting.

    Clears the customer record and the copies of their details on their appointments
    (name, email, phone, notes, cancellation reasons, status-history notes), notification
    recipients and waitlist entries under the same email. Audit history already stores
    personal fields as "changed" without values. Irreversible.
    """
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    if customer.status == Customer.Status.ANONYMIZED:
        return customer
    old_email = customer.email

    for field in PII_FIELDS:
        default = Customer._meta.get_field(field).get_default()
        setattr(customer, field, default)
    customer.first_name = ANONYMIZED_NAME
    customer.user = None
    customer.assigned_staff = customer.preferred_staff = None
    customer.preferred_language = customer.preferred_contact_method = ""
    for field in CONSENT_FIELDS:
        setattr(customer, field, False)
    customer.status = Customer.Status.ANONYMIZED
    customer.anonymized_at = customer.consent_updated_at = timezone.now()
    customer.save()

    bookings = Booking.objects.filter(customer=customer)
    scrubbed = bookings.update(
        customer_name=ANONYMIZED_NAME,
        customer_email="",
        customer_phone="",
        customer_notes="",
        internal_notes="",
        cancellation_reason="",
    )
    BookingStatusHistory.objects.filter(booking__in=bookings).update(note="")
    for activity in BookingActivityLog.objects.filter(
        booking__in=bookings, metadata__has_key="reason"
    ):
        activity.metadata.pop("reason")
        activity.save(update_fields=["metadata"])
    NotificationLog.objects.filter(related_booking__in=bookings).update(
        recipient_email="", recipient=None
    )
    if old_email:
        WaitlistEntry.objects.filter(
            organization=customer.organization, customer_email__iexact=old_email
        ).update(customer_name=ANONYMIZED_NAME, customer_email="", customer_phone="")

    _audit(AuditAction.CUSTOMER_ANONYMIZED, customer, actor, metadata={"appointments": scrubbed})
    return customer
