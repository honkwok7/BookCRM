"""State of the public booking wizard (/book/<slug>/), kept on the server in the session.

Steps: location → service → provider (or anyone) → time → details → review → confirmation.
Each request rebuilds the choices from the session and re-checks them against the current
data (the location is still bookable, the service still public there, the provider still
offers it). A choice that is no longer valid is dropped, and the wizard sends the visitor back
to the first step that needs an answer. The time is checked against availability when it is
chosen and again on Confirm (by the booking service, under the lock), not on every request in
between: if it was taken meanwhile, Confirm says so and sends the visitor back to pick another
time. So a stale or tampered session can never book something the page wouldn't offer.

The state is keyed by organization, so wizards for two organizations in one browser don't mix.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from django.utils.dateparse import parse_datetime

from locations.models import Location
from services.selectors import services_bookable_at
from staff.selectors import list_providers_for, with_providers

ANY_PROVIDER = "any"
STEPS = ("location", "service", "provider", "time", "details", "review")
# What each step decides; choosing again clears everything after it.
DECIDES = {
    "location": "location",
    "service": "service",
    "provider": "staff",
    "time": "start",
    "details": "customer",
}


def _session_key(organization) -> str:
    return f"booking_wizard:{organization.pk}"


@dataclass
class Wizard:
    organization: object
    session: object
    data: dict = field(default_factory=dict)

    # Resolved (validated) choices; None when not chosen yet or no longer valid.
    location: Location | None = None
    service: object = None
    staff: object = None  # a StaffProfile, or None with ``any_provider``
    any_provider: bool = False
    start: datetime | None = None

    @classmethod
    def load(cls, organization, session) -> Wizard:
        wizard = cls(organization, session, dict(session.get(_session_key(organization), {})))
        wizard._resolve()
        return wizard

    # -- choices on offer ---------------------------------------------------------------------

    def locations(self) -> list[Location]:
        return list(
            Location.objects.filter(
                organization=self.organization, is_active=True, booking_enabled=True
            ).order_by("-is_default", "name")
        )

    def services(self) -> list:
        """Public services at the location that at least one visible provider offers there."""
        if self.location is None:
            return []
        services = with_providers(
            services_bookable_at(self.organization, self.location, public=True),
            self.location,
            public=True,
        )
        return list(
            services.select_related("category").order_by(
                "category__sort_order", "category__name", "name"
            )
        )

    def providers(self) -> list:
        if self.location is None or self.service is None:
            return []
        return list(
            list_providers_for(self.service, self.location, public=True).order_by(
                "display_name", "user__first_name", "user__last_name"
            )
        )

    # -- changing the state -------------------------------------------------------------------

    def choose(self, step: str, value) -> None:
        """Record the answer to ``step`` and forget every later answer."""
        order = list(DECIDES)
        for later in order[order.index(step) + 1 :]:
            self.data.pop(DECIDES[later], None)
        self.data[DECIDES[step]] = value
        self._save()
        self._resolve()

    def idempotency_key(self) -> str:
        """One key per attempt: a double-clicked Confirm books once."""
        if "key" not in self.data:
            self.data["key"] = uuid.uuid4().hex
            self._save()
        return self.data["key"]

    def forget_time(self) -> None:
        self.data.pop("start", None)
        self.data.pop("key", None)
        self._save()
        self.start = None

    def clear(self) -> None:
        self.session.pop(_session_key(self.organization), None)
        self.data = {}

    def _save(self) -> None:
        self.session[_session_key(self.organization)] = self.data

    # -- validation -----------------------------------------------------------------------------

    def _resolve(self) -> None:
        """Turn the stored ids into objects, dropping anything that is no longer bookable."""
        locations = self.locations()
        self.location = next(
            (item for item in locations if str(item.pk) == self.data.get("location")), None
        )
        if self.location is None and len(locations) == 1:
            self.location = locations[0]  # only one place to book: no need to ask
            self.data["location"] = str(self.location.pk)
            self._save()
        self.service = self.staff = self.start = None
        self.any_provider = False
        if self.location is None:
            return

        self.service = next(
            (item for item in self.services() if str(item.pk) == self.data.get("service")), None
        )
        if self.service is None:
            return

        providers = self.providers()
        chosen = self.data.get("staff")
        if chosen == ANY_PROVIDER and len(providers) > 1:
            self.any_provider = True
        else:
            self.staff = next((item for item in providers if str(item.pk) == chosen), None)
            if self.staff is None and len(providers) == 1:
                self.staff = providers[0]  # only one provider: no need to ask
                self.data["staff"] = str(self.staff.pk)
                self._save()
            if self.staff is None:
                return

        start = parse_datetime(self.data.get("start") or "")
        self.start = start if start is not None and start.tzinfo is not None else None

    @property
    def has_provider(self) -> bool:
        return self.staff is not None or self.any_provider

    @property
    def customer(self) -> dict:
        return self.data.get("customer") or {}

    def first_open_step(self) -> str:
        """The first step without a valid answer ("review" when everything is answered)."""
        if self.location is None:
            return "location"
        if self.service is None:
            return "service"
        if not self.has_provider:
            return "provider"
        if self.start is None:
            return "time"
        if not self.customer:
            return "details"
        return "review"

    def reachable(self, step: str) -> bool:
        return STEPS.index(step) <= STEPS.index(self.first_open_step())

    def skipped(self, step: str) -> bool:
        """Steps with a single possible answer are answered for the visitor and not shown."""
        if step == "location":
            return len(self.locations()) == 1
        if step == "provider":
            return len(self.providers()) == 1
        return False
