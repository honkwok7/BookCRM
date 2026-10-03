"""CRM customer writes: the one place that creates, changes, merges or anonymizes customers.

The API, the web app, the booking engine and future AI agents all call these functions, so
duplicate detection, consent stamping and auditing are applied the same way everywhere.
Errors are ``DomainError`` (400) or ``ConflictError`` (409) with a stable ``code``.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from bookings.models import (
    Booking,
    BookingActivityLog,
    BookingStatusHistory,
    Customer,
    WaitlistEntry,
)
from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.exceptions import ConflictError, DomainError
from crm.activity import Kind, record_activity
from crm.models import HEX_COLOR, CustomerActivity, CustomerNote, CustomerTag, Tag
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

# Fields callers may set through create_customer/update_customer. Tags are assigned with the
# tag functions below; the legacy ``tags`` JSON and ``notes`` text are read-only (notes live in
# CustomerNote, where customers.notes.private applies).
EDITABLE_FIELDS = frozenset(
    {
        *(field for field in PII_FIELDS if field not in ("name", "tags", "notes")),
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
    record_activity(
        Kind.CUSTOMER_CREATED, customer=customer, actor=actor, metadata={"source": customer.source}
    )
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
        profile_fields = sorted(set(changes) - set(CONSENT_FIELDS) - {"consent_updated_at"})
        if profile_fields:
            record_activity(
                Kind.PROFILE_UPDATED,
                customer=customer,
                actor=actor,
                metadata={"fields": profile_fields},  # names only, never values
            )
    if consent_changes:
        _audit(AuditAction.CUSTOMER_CONSENT_CHANGED, customer, actor, metadata=consent_changes)
        record_activity(
            Kind.CONSENT_CHANGED, customer=customer, actor=actor, metadata=consent_changes
        )
    return customer


def _may_link(user, email: str) -> bool:
    """May ``user`` be linked to a customer with ``email``? Only as themselves: a verified
    account whose email is that email. Otherwise anyone signed in could book under somebody
    else's address and see (and change) that person's later appointments."""
    return (
        user is not None
        and bool(email)
        and user.email_verified
        and user.email.strip().lower() == email.lower()
    )


def _lock_phone(organization, phone: str) -> None:
    """Serialize phone-only lookups for one number (PostgreSQL advisory lock, released at
    commit). Phones aren't unique (a family can share one), so a constraint can't do this."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [f"customer-phone:{organization.pk}:{phone}"],
            )


@transaction.atomic
def find_or_create_customer(
    *, organization, name, email="", phone="", user=None, source="", actor=None
) -> Customer:
    """The booking engine's customer lookup: match by email, else by phone, else create.

    A match never links ``user`` to an existing record: knowing someone's email must not
    attach their history to your account. A new record is linked to ``user`` only when it is
    their own verified email (``_may_link``).
    """
    email, phone = email.strip(), phone.strip()
    if not email and phone:
        _lock_phone(organization, phone)
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
    if _may_link(user, email):
        customer.user = user
        customer.save(update_fields=["user", "updated_at"])
    return customer


def _lock_bookings_of(customer_id) -> None:
    list(
        Booking.objects.select_for_update(of=("self",))
        .filter(customer_id=customer_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


@transaction.atomic
def merge_customers(*, target: Customer, duplicate: Customer, actor=None) -> Customer:
    """Fold ``duplicate`` into ``target``: move its appointments, fill target's blank fields.

    Consent is never copied: a consent given on one record is not assumed for the other.
    """
    if target.pk == duplicate.pk:
        raise DomainError("A customer cannot be merged into itself", code="invalid_merge")
    if target.organization_id != duplicate.organization_id:
        raise DomainError("Customer not found", code="not_found")
    # Lock order shared with the booking paths: appointments first, then customers. A
    # reschedule holds its appointment and then needs the customer (the new row's foreign
    # key); taking customers first here would deadlock with it.
    _lock_bookings_of(duplicate.pk)
    # Both customer rows in a fixed order (primary key) so concurrent merges cannot deadlock.
    locked = {
        c.pk: c
        for c in Customer.objects.select_for_update()
        .filter(pk__in=[target.pk, duplicate.pk])
        .order_by("pk")
    }
    if len(locked) != 2:  # a concurrent merge or deletion removed one of them meanwhile
        raise ConflictError("The customer no longer exists", code="customer_gone")
    target, duplicate = locked[target.pk], locked[duplicate.pk]
    if Customer.Status.ANONYMIZED in (target.status, duplicate.status):
        raise ConflictError("An anonymized customer cannot be merged", code="anonymized")
    if target.user_id and duplicate.user_id and target.user_id != duplicate.user_id:
        raise ConflictError(
            "Both customers are linked to different user accounts", code="linked_to_other_user"
        )

    before = snapshot(target)
    for field in EDITABLE_FIELDS - set(CONSENT_FIELDS) - {"status"}:
        if not getattr(target, field) and getattr(duplicate, field):
            setattr(target, field, getattr(duplicate, field))
    target.user_id = target.user_id or duplicate.user_id
    target_tags = set(target.customer_tags.values_list("tag_id", flat=True))
    duplicate.customer_tags.exclude(tag_id__in=target_tags).update(customer=target)

    moved = Booking.objects.filter(customer=duplicate).update(customer=target)
    duplicate_id = str(duplicate.pk)
    # The duplicate's history and notes become the target's.
    CustomerActivity.objects.filter(customer=duplicate).update(customer=target)
    CustomerNote.objects.filter(customer=duplicate).update(customer=target)
    # Waitlist entries too. One waiting entry per customer and service: where both wait for
    # the same service, the target's entry stays and the duplicate's is closed.
    waiting = WaitlistEntry.Status.WAITING
    target_services = WaitlistEntry.objects.filter(customer=target, status=waiting).values(
        "service_id"
    )
    WaitlistEntry.objects.filter(
        customer=duplicate, status=waiting, service_id__in=target_services
    ).update(status=WaitlistEntry.Status.CLOSED)
    WaitlistEntry.objects.filter(customer=duplicate).update(customer=target)
    # Forms too (M6.2). One waiting copy per form and customer: the target's stays.
    from customer_forms.models import FormAssignment

    pending = FormAssignment.Status.PENDING
    FormAssignment.objects.filter(
        customer=duplicate,
        status=pending,
        template_id__in=FormAssignment.objects.filter(customer=target, status=pending).values(
            "template_id"
        ),
    ).update(status=FormAssignment.Status.CANCELLED, cancelled_at=timezone.now())
    FormAssignment.objects.filter(customer=duplicate).update(customer=target)
    duplicate.delete()  # before saving target: target may take over the duplicate's email
    target.save()

    _audit(
        AuditAction.CUSTOMER_MERGED,
        target,
        actor,
        metadata={"merged_customer": duplicate_id, "appointments_moved": moved},
        changes=diff_snapshots(before, snapshot(target), redact_fields=PII_FIELDS),
    )
    record_activity(
        Kind.CUSTOMER_MERGED,
        customer=target,
        actor=actor,
        metadata={"merged_customer": duplicate_id, "appointments_moved": moved},
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
    _lock_bookings_of(customer.pk)  # appointments before the customer (see merge_customers)
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    if customer.status == Customer.Status.ANONYMIZED:
        return customer
    old_email = customer.email
    own_account = customer.user_id  # their sign-in account, if linked

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
    customer.customer_tags.all().delete()  # labels can be sensitive ("diabetic", ...)
    customer.customer_notes.all().delete()  # free text about the person

    bookings = Booking.objects.filter(customer=customer)
    scrubbed = bookings.update(
        customer_name=ANONYMIZED_NAME,
        customer_email="",
        customer_phone="",
        customer_notes="",
        internal_notes="",
        cancellation_reason="",
    )
    BookingStatusHistory.objects.filter(booking__in=bookings).update(note="", reason="")
    for activity in BookingActivityLog.objects.filter(
        booking__in=bookings, metadata__has_key="reason"
    ):
        activity.metadata.pop("reason")
        activity.save(update_fields=["metadata"])
    entries = WaitlistEntry.objects.filter(customer=customer)
    if old_email:
        entries = WaitlistEntry.objects.filter(
            Q(customer=customer)
            | Q(organization=customer.organization, customer_email__iexact=old_email)
        )
    entry_ids = list(entries.values_list("pk", flat=True))
    WaitlistEntry.objects.filter(pk__in=entry_ids).update(
        customer_name=ANONYMIZED_NAME, customer_email="", customer_phone=""
    )
    # Form answers are personal data: deleted. Waiting forms are cancelled (M6.2).
    from customer_forms.models import FormAssignment, FormSubmission

    FormSubmission.objects.filter(assignment__customer=customer).delete()
    FormAssignment.objects.filter(customer=customer, status=FormAssignment.Status.PENDING).update(
        status=FormAssignment.Status.CANCELLED, cancelled_at=timezone.now()
    )
    NotificationLog.objects.filter(
        Q(related_booking__in=bookings)
        | Q(related_waitlist_entry__in=entry_ids)
        | Q(related_form_assignment__customer=customer)
    ).update(recipient_email="", recipient=None, failure_reason="")
    if own_account:
        # What they did themselves (booked, cancelled, ...) no longer points to their account.
        # Staff actors stay. The audit log is append-only and keeps its entries (access is
        # limited to audit.view); see docs/CRM.md.
        Customer.objects.filter(pk=customer.pk, created_by_id=own_account).update(created_by=None)
        bookings.filter(created_by_id=own_account).update(created_by=None)
        bookings.filter(cancelled_by_id=own_account).update(cancelled_by=None)
        BookingStatusHistory.objects.filter(booking__in=bookings, changed_by_id=own_account).update(
            changed_by=None
        )
        BookingActivityLog.objects.filter(booking__in=bookings, actor_id=own_account).update(
            actor=None
        )
        CustomerActivity.objects.filter(customer=customer, actor_id=own_account).update(actor=None)

    _audit(AuditAction.CUSTOMER_ANONYMIZED, customer, actor, metadata={"appointments": scrubbed})
    return customer


@transaction.atomic
def delete_customer(*, customer: Customer, actor=None) -> None:
    """Delete a customer: anonymize first (so no copy of their details survives on
    appointments, waitlist entries or notifications), then remove the record. Appointments
    stay, unlinked, for aggregate reporting. Irreversible."""
    customer = anonymize_customer(customer=customer, actor=actor)
    _audit(AuditAction.CUSTOMER_DELETED, customer, actor)
    customer.delete()


# -- Tags ----------------------------------------------------------------------------------


def _tag_fields(name: str, color: str) -> tuple[str, str, str]:
    name, color = name.strip(), color.strip()
    slug = slugify(name)[:80]
    if not slug or len(name) > 60:
        raise DomainError("A tag needs a name of up to 60 characters", code="invalid_tag")
    if color:
        try:
            HEX_COLOR(color)
        except ValidationError as error:
            raise DomainError(error.messages[0], code="invalid_color") from error
    return name, slug, color


def _check_tag_unique(organization_id, slug, exclude_pk=None) -> None:
    clashes = Tag.objects.filter(organization_id=organization_id, slug=slug).exclude(pk=exclude_pk)
    if clashes.exists():
        raise ConflictError("A tag with this name already exists", code="duplicate_tag")


def _audit_tag(action, tag, actor, **kwargs):
    record_audit(action, organization=tag.organization, actor=actor, target=tag, **kwargs)


@transaction.atomic
def create_tag(*, organization, name: str, color: str = "", actor=None) -> Tag:
    name, slug, color = _tag_fields(name, color)
    _check_tag_unique(organization.pk, slug)
    tag = Tag.objects.create(organization=organization, name=name, slug=slug, color=color)
    _audit_tag(AuditAction.TAG_CREATED, tag, actor, metadata={"name": name})
    return tag


def get_or_create_tag(*, organization, name: str, actor=None) -> Tag:
    """The organization's tag with this name's slug, created if it doesn't exist.

    Safe under concurrency: if another request creates the same tag between the lookup and
    the insert, the unique constraint refuses the second insert and the existing tag is used.
    """
    _, slug, _ = _tag_fields(name, "")
    existing = _find_tag(organization, slug)
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            return create_tag(organization=organization, name=name, actor=actor)
    except IntegrityError, ConflictError:
        return Tag.objects.get(organization=organization, slug=slug)


def _find_tag(organization, slug) -> Tag | None:
    return Tag.objects.filter(organization=organization, slug=slug).first()


@transaction.atomic
def update_tag(*, tag: Tag, name: str | None = None, color: str | None = None, actor=None) -> Tag:
    before = snapshot(tag)
    tag.name, tag.slug, tag.color = _tag_fields(
        tag.name if name is None else name, tag.color if color is None else color
    )
    _check_tag_unique(tag.organization_id, tag.slug, exclude_pk=tag.pk)
    tag.save()
    changes = diff_snapshots(before, snapshot(tag))
    if changes:
        _audit_tag(AuditAction.TAG_UPDATED, tag, actor, changes=changes)
    return tag


@transaction.atomic
def delete_tag(*, tag: Tag, actor=None) -> None:
    customers = tag.customer_tags.count()
    _audit_tag(
        AuditAction.TAG_DELETED, tag, actor, metadata={"name": tag.name, "customers": customers}
    )
    tag.delete()


def _lock_taggable(customer: Customer, tag: Tag) -> Customer:
    """Re-read the customer under a row lock: a stale copy must not tag an anonymized row."""
    if tag.organization_id != customer.organization_id:
        raise DomainError("Tag not found", code="not_found")  # same answer as a missing id
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    if customer.status == Customer.Status.ANONYMIZED:
        raise ConflictError("An anonymized customer cannot be changed", code="anonymized")
    return customer


def _audit_tagging(action, customer, tag, actor):
    # Only the tag id: the link between a person and a label like "diabetic" is personal data
    # once the tag is renamed or deleted, and must not outlive anonymization in plain text.
    _audit(action, customer, actor, metadata={"tag": str(tag.pk)})
    kind = Kind.TAG_ADDED if action == AuditAction.CUSTOMER_TAG_ADDED else Kind.TAG_REMOVED
    record_activity(kind, customer=customer, actor=actor, subject=tag)


@transaction.atomic
def add_customer_tag(*, customer: Customer, tag: Tag, actor=None) -> bool:
    """Tag a customer. Returns False if the tag was already there."""
    customer = _lock_taggable(customer, tag)
    _, created = CustomerTag.objects.get_or_create(
        customer=customer,
        tag=tag,
        defaults={"organization_id": customer.organization_id, "tagged_by": actor},
    )
    if created:
        _audit_tagging(AuditAction.CUSTOMER_TAG_ADDED, customer, tag, actor)
    return created


@transaction.atomic
def remove_customer_tag(*, customer: Customer, tag: Tag, actor=None) -> bool:
    """Untag a customer. Returns False if the tag was not there."""
    customer = _lock_taggable(customer, tag)
    deleted, _ = CustomerTag.objects.filter(customer=customer, tag=tag).delete()
    if deleted:
        _audit_tagging(AuditAction.CUSTOMER_TAG_REMOVED, customer, tag, actor)
    return bool(deleted)


@transaction.atomic
def set_customer_tags(*, customer: Customer, tags, actor=None) -> None:
    """Make the customer's tags exactly ``tags`` (each change audited)."""
    wanted = {tag.pk: tag for tag in tags}
    current = {ct.tag_id: ct.tag for ct in customer.customer_tags.select_related("tag")}
    for tag_id in wanted.keys() - current.keys():
        add_customer_tag(customer=customer, tag=wanted[tag_id], actor=actor)
    for tag_id in current.keys() - wanted.keys():
        remove_customer_tag(customer=customer, tag=current[tag_id], actor=actor)


# -- Notes ---------------------------------------------------------------------------------
# Who may read or write which notes (customers.notes.private for internal ones) is decided by
# the caller's capabilities in the API/web layer; the selectors in crm.selectors enforce
# visibility on reads.

NOTE_FIELDS = frozenset({"content", "visibility", "note_type", "pinned"})


def _clean_note_fields(fields: dict) -> dict:
    unknown = set(fields) - NOTE_FIELDS
    if unknown:
        raise DomainError(f"Unknown note fields: {', '.join(sorted(unknown))}", code="invalid")
    if "content" in fields:
        fields["content"] = (fields["content"] or "").strip()
        if not fields["content"]:
            raise DomainError("A note cannot be empty", code="empty_note")
    return fields


def _audit_note(action, note, actor, **kwargs):
    record_audit(
        action,
        organization=note.customer.organization,
        actor=actor,
        target=note,
        metadata={"customer": str(note.customer_id), "visibility": note.visibility},
        **kwargs,
    )


@transaction.atomic
def create_note(*, customer: Customer, author=None, **fields) -> CustomerNote:
    fields = _clean_note_fields(fields)
    if "content" not in fields:
        raise DomainError("A note cannot be empty", code="empty_note")
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    if customer.status == Customer.Status.ANONYMIZED:
        raise ConflictError("An anonymized customer cannot be changed", code="anonymized")
    note = CustomerNote.objects.create(
        organization_id=customer.organization_id, customer=customer, author=author, **fields
    )
    _audit_note(AuditAction.NOTE_CREATED, note, author)
    record_activity(
        Kind.NOTE_CREATED,
        customer=customer,
        actor=author,
        subject=note,
        metadata={"note_type": note.note_type, "visibility": note.visibility},
        internal=note.visibility == CustomerNote.Visibility.INTERNAL,
    )
    return note


@transaction.atomic
def update_note(*, note: CustomerNote, actor=None, **fields) -> CustomerNote:
    fields = _clean_note_fields(fields)
    note = CustomerNote.objects.select_for_update().select_related("customer").get(pk=note.pk)
    before = snapshot(note)
    for field, value in fields.items():
        setattr(note, field, value)
    if note.content != before["content"]:
        note.edited_at = timezone.now()
    note.save()
    changes = diff_snapshots(before, snapshot(note), redact_fields=("content",))
    changes.pop("edited_at", None)
    if changes:
        _audit_note(AuditAction.NOTE_UPDATED, note, actor, changes=changes)
    if note.visibility != before["visibility"]:
        # The timeline entry must follow the note: an entry about a note that is now internal
        # must disappear for readers without customers.notes.private, and the reverse.
        CustomerActivity.objects.filter(
            customer_id=note.customer_id,
            kind=Kind.NOTE_CREATED,
            subject_type=CustomerNote._meta.label_lower,
            subject_id=str(note.pk),
        ).update(
            internal=note.visibility == CustomerNote.Visibility.INTERNAL,
            metadata={"note_type": note.note_type, "visibility": note.visibility},
        )
    return note


@transaction.atomic
def delete_note(*, note: CustomerNote, actor=None) -> None:
    _audit_note(AuditAction.NOTE_DELETED, note, actor)
    note.delete()
