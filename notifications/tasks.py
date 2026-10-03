from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from config.celery import app
from crm.activity import Kind, record_activity
from notifications.models import NotificationLog


@app.task(bind=True, max_retries=3)
def send_templated_email(self, *, notification_log_id, organization_id, subject, template_base):
    # Tenant context is explicit: the log must belong to the organization the caller named.
    log = NotificationLog.objects.select_related(
        "related_booking__service",
        "related_booking__customer",
        "related_booking__location",
        "related_booking__organization",
        "related_waitlist_entry__customer",
        "related_form_assignment__customer",
        "related_form_assignment__template",
        "related_form_assignment__organization",
    ).get(
        id=notification_log_id,
        organization_id=organization_id,
    )
    # Claim the log atomically (PENDING/FAILED -> SENDING): a redelivered or concurrent copy
    # of this task finds nothing to claim and sends nothing. A worker crash mid-send leaves
    # the row SENDING; stale-row recovery belongs to the notification pipeline (M7.1).
    claimed = NotificationLog.objects.filter(
        pk=log.pk,
        status__in=[NotificationLog.Status.PENDING, NotificationLog.Status.FAILED],
    ).update(status=NotificationLog.Status.SENDING)
    if not claimed:
        return
    booking, entry = log.related_booking, log.related_waitlist_entry
    assignment = log.related_form_assignment
    context = {"booking": booking, "entry": entry, "assignment": assignment}
    if entry is not None and booking is not None:
        context["booking_url"] = f"{settings.SITE_URL}/book/{booking.organization.slug}/"
    if assignment is not None:
        from customer_forms.links import link_for

        context["form_url"] = link_for(assignment)
        context["link_days"] = settings.FORM_LINK_DAYS
    try:
        text_body = render_to_string(f"emails/{template_base}.txt", context)
        html_body = render_to_string(f"emails/{template_base}.html", context)
        message = EmailMultiAlternatives(subject=subject, body=text_body, to=[log.recipient_email])
        message.attach_alternative(html_body, "text/html")
        message.send()
        log.status = NotificationLog.Status.SENT
        log.sent_at = timezone.now()
        log.failure_reason = ""
        log.save(update_fields=["status", "sent_at", "failure_reason", "updated_at"])
    except Exception as exc:  # pragma: no cover
        log.status = NotificationLog.Status.FAILED
        log.failure_reason = str(exc)
        log.retry_count += 1
        log.save(update_fields=["status", "failure_reason", "retry_count", "updated_at"])
        raise self.retry(exc=exc, countdown=30) from exc

    # Outside the try: a timeline failure must never mark a delivered email as failed (and
    # trigger a resend). A waitlist email belongs to the waiting customer, not to the
    # customer whose cancellation freed the time.
    if entry is not None:
        customer = entry.customer
    elif assignment is not None:
        customer = assignment.customer
    else:
        customer = booking.customer if booking else None
    record_activity(
        Kind.EMAIL_SENT,
        customer=customer,
        subject=log,
        metadata={
            "notification_type": log.notification_type,
            # Not the freed booking's reference: it's another customer's appointment.
            "reference": booking.reference if booking and entry is None else "",
            **({"form": str(assignment.template_id)} if assignment is not None else {}),
        },
    )
