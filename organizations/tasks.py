"""Invitation emails, sent by Celery after the invitation is committed.

Only the invitation id crosses the broker; the link is built from SITE_URL, never from the
request's Host header (which a client controls).
"""

from django.conf import settings
from django.core.mail import send_mail

from config.celery import app
from organizations.models import OrganizationInvitation


@app.task(bind=True, max_retries=3)
def send_invitation_email(self, *, invitation_id):
    invitation = OrganizationInvitation.objects.select_related("organization").get(pk=invitation_id)
    organization = invitation.organization
    if not invitation.is_usable or not organization.accepts_members:
        return
    url = f"{settings.SITE_URL}/accept-invitation/?token={invitation.token}"
    try:
        send_mail(
            subject=f"Invitation to join {organization.name}",
            message=f"You were invited to join {organization.name}. Accept invitation: {url}",
            from_email=None,
            recipient_list=[invitation.email],
        )
    except Exception as exc:  # pragma: no cover
        raise self.retry(exc=exc, countdown=30) from exc
