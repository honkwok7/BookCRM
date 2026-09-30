"""Walk the public booking wizard (/book/<slug>/) the way a visitor would, for tests."""

from __future__ import annotations

from datetime import timedelta

from scheduling.availability import AvailabilityService

DETAILS = {"name": "Ada Lovelace", "email": "ada@example.test", "phone": "", "notes": ""}


def first_free_time(organization, service, *, staff=None, location=None):
    engine = AvailabilityService(organization, service, location=location, public=True)
    today = engine.now.astimezone(engine.zone).date()
    slots = engine.get_available_slots(today, today + timedelta(days=13), staff=staff)
    assert slots, "no free time in the next two weeks"
    return slots[0].start


def book_through_wizard(
    client, organization, service, *, staff=None, location=None, start=None, details=None
):
    """Answer every step and confirm. Returns the confirmation response (a redirect).

    ``staff``: a provider, "any", or None when the provider step is skipped (one provider).
    """
    base = f"/book/{organization.slug}/"
    if location is not None:
        client.post(base, {"location": str(location.pk)})
    client.post(f"{base}service/", {"service": str(service.pk)})
    if staff is not None:
        client.post(f"{base}provider/", {"staff": "any" if staff == "any" else str(staff.pk)})
    if start is None:
        start = first_free_time(
            organization,
            service,
            staff=None if staff in (None, "any") else staff,
            location=location,
        )
    client.post(f"{base}time/", {"start": start.isoformat()})
    client.post(f"{base}details/", details or DETAILS)
    return client.post(f"{base}review/")
