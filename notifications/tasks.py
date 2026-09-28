from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from config.celery import app
from notifications.models import NotificationLog


@app.task(bind=True, max_retries=3)
def send_templated_email(self, *, notification_log_id, organization_id, subject, template_base):
    # Tenant context is explicit: the log must belong to the organization the caller named.
    log = NotificationLog.objects.select_related("related_booking__service").get(
        id=notification_log_id,
        organization_id=organization_id,
    )
    if log.status == NotificationLog.Status.SENT:
        return
    context = {"booking": log.related_booking}
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
