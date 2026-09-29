"""The availability engine: which times can a service be booked, with whom, at a location.

    service = AvailabilityService(organization, service, location=location, public=True)
    slots = service.get_available_slots(start_date, end_date)            # any provider
    slots = service.get_available_slots(start_date, end_date, staff=maya)
    service.validate_slot(maya, start)                                   # or raises

One engine answers both "what's free?" and "is this time free?", so a slot that is offered can
be booked and a time that isn't offered is refused for the same reason.

How a provider's free time on a date is worked out, in the location's time zone:

1. their weekly hours for that weekday (rows for this location, or for any location);
2. limited by availability exceptions for that date (off all day, or only certain times);
3. limited by the location's opening hours, when it has any;
4. minus location closures and organization holidays (all day, or certain times);
5. minus approved time off and existing appointments. An appointment blocks its own time
   plus a gap on each side: the larger of its service's buffer and the new service's buffer
   (a buffer is only needed between appointments, not at the edges of working hours);
6. nothing at all on a day they already have ``max_daily_appointments`` appointments.

A slot starts on the step grid (every 15 minutes by default, on the location's wall clock) and
must fit entirely in free time, using the provider's own duration for the service. Public
callers are also held to the service's minimum notice and how far ahead it can be booked; the
team is only kept out of the past. Everything is loaded up front in a fixed number of queries
(``QUERY_BUDGET``), however many days and providers are asked for.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone

from bookings.models import Booking
from core.exceptions import ConflictError, DomainError
from locations.models import Location, LocationClosure, LocationHours
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)
from staff.models import StaffServiceOffering
from staff.selectors import list_providers_for

ACTIVE_STATUSES = (
    Booking.Status.PENDING,
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
    Booking.Status.IN_PROGRESS,
)
DEFAULT_STEP_MINUTES = 15
# Upper bound on queries per calculation, whatever the date range and number of providers.
QUERY_BUDGET = 10

Interval = tuple[datetime, datetime]


# -- Interval arithmetic (half-open [start, end)) -------------------------------------------


def merge(intervals) -> list[Interval]:
    """Sorted, with overlapping or touching intervals joined; empty ones dropped."""
    merged: list[list[datetime]] = []
    for start, end in sorted(item for item in intervals if item[0] < item[1]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def intersect(first, second) -> list[Interval]:
    first, second = merge(first), merge(second)
    result, i, j = [], 0, 0
    while i < len(first) and j < len(second):
        start = max(first[i][0], second[j][0])
        end = min(first[i][1], second[j][1])
        if start < end:
            result.append((start, end))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract(windows, blocks) -> list[Interval]:
    blocks = merge(blocks)
    result = []
    for start, end in merge(windows):
        cursor = start
        for block_start, block_end in blocks:
            if block_end <= cursor or block_start >= end:
                continue
            if block_start > cursor:
                result.append((cursor, block_start))
            cursor = max(cursor, block_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


# -- Results --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    staff: object  # StaffProfile
    end: datetime


@dataclass(frozen=True)
class Slot:
    """A start time and the providers free then (with when each would finish)."""

    start: datetime
    candidates: tuple[Candidate, ...]

    @property
    def staff(self) -> list:
        return [candidate.staff for candidate in self.candidates]


def zone_of(location: Location) -> ZoneInfo:
    try:
        return ZoneInfo(location.timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


# -- The engine -----------------------------------------------------------------------------


class AvailabilityService:
    def __init__(
        self,
        organization,
        service,
        *,
        location: Location | None = None,
        public: bool = False,
        now: datetime | None = None,
        step_minutes: int = DEFAULT_STEP_MINUTES,
    ):
        if service.organization_id != organization.pk:
            raise DomainError("Service not found", code="not_found")
        if location is None:
            location = Location.objects.filter(organization=organization, is_default=True).first()
        if location is None or location.organization_id != organization.pk:
            raise DomainError("Location not found", code="not_found")
        self.organization = organization
        self.service = service
        self.location = location
        self.public = public
        self.now = now or timezone.now()
        self.step = timedelta(minutes=step_minutes)
        self.zone = zone_of(location)

    # -- public API ---------------------------------------------------------------------------

    def providers(self, staff=None) -> list:
        providers = list_providers_for(self.service, self.location, public=self.public)
        if staff is not None:
            providers = providers.filter(pk=staff.pk)
        return list(providers)

    def get_available_slots(self, start_date: date, end_date: date, staff=None) -> list[Slot]:
        """Slots from ``start_date`` to ``end_date`` (inclusive, the location's dates), for
        ``staff`` or, when None, for any provider (each slot lists who is free)."""
        if end_date < start_date:
            return []
        providers = self.providers(staff)
        if not providers:
            return []
        data = self._load(providers, start_date, end_date)
        earliest, latest = self._bounds()
        by_start: dict[datetime, list[Candidate]] = defaultdict(list)
        day = start_date
        while day <= end_date:
            for provider in providers:
                duration = data.durations[provider.pk]
                for window_start, window_end in self._free(data, provider, day):
                    start = self._first_on_grid(max(window_start, earliest))
                    while start + duration <= window_end:
                        if latest is not None and start > latest:
                            break
                        by_start[start].append(Candidate(provider, start + duration))
                        start += self.step
            day += timedelta(days=1)
        return [Slot(start, tuple(by_start[start])) for start in sorted(by_start)]

    def validate_slot(self, staff, start: datetime, *, ignore_booking=None) -> Candidate:
        """Check that ``staff`` can be booked for the service at ``start``; return who and until
        when, or raise ``DomainError``/``ConflictError`` with a stable code.

        The start doesn't have to be on the step grid: the team may book any free time.
        ``ignore_booking`` leaves one appointment out (rescheduling it).
        """
        if timezone.is_naive(start):
            raise DomainError("The start time needs a time zone", code="invalid_time")
        if start <= self.now:
            raise DomainError("Cannot book in the past", code="in_past")
        if self.public:
            earliest, latest = self._bounds()
            if start < earliest:
                raise DomainError(
                    "That time is too soon to book online. Choose a later time.", code="too_soon"
                )
            if latest is not None and start > latest:
                raise DomainError("That time is too far ahead to book online.", code="too_far")
        providers = self.providers(staff)
        if not providers:
            raise DomainError(
                "This staff member doesn't offer the selected service here.", code="not_offered"
            )
        provider = providers[0]
        day = start.astimezone(self.zone).date()
        data = self._load(providers, day, day, ignore_booking=ignore_booking)
        end = start + data.durations[provider.pk]
        if not any(
            window_start <= start and end <= window_end
            for window_start, window_end in self._free(data, provider, day)
        ):
            raise ConflictError("Selected slot is no longer available", code="slot_unavailable")
        return Candidate(provider, end)

    # -- loading ------------------------------------------------------------------------------

    def _bounds(self) -> tuple[datetime, datetime | None]:
        if not self.public:
            return self.now + timedelta(microseconds=1), None
        earliest = self.now + timedelta(minutes=self.service.min_notice_minutes)
        return earliest, self.now + timedelta(days=self.service.max_advance_days)

    def _local(self, day: date, value: time) -> datetime:
        return datetime.combine(day, value, tzinfo=self.zone).astimezone(UTC)

    def _load(self, providers, start_date, end_date, *, ignore_booking=None) -> _Data:
        staff_ids = [provider.pk for provider in providers]
        # A little margin either side: appointments and buffers from the neighbouring days.
        range_start = self._local(start_date, time.min) - timedelta(days=1)
        range_end = self._local(end_date + timedelta(days=1), time.min) + timedelta(days=1)
        data = _Data()

        duration = timedelta(minutes=self.service.duration_minutes)
        data.durations = {pk: duration for pk in staff_ids}
        offerings = StaffServiceOffering.objects.filter(
            service=self.service, staff_id__in=staff_ids, is_active=True
        ).filter(Q(location=self.location) | Q(location__isnull=True))
        # A location-specific offering wins over an "all locations" one.
        for offering in sorted(offerings, key=lambda row: row.location_id is not None):
            if offering.custom_duration_minutes:
                data.durations[offering.staff_id] = timedelta(
                    minutes=offering.custom_duration_minutes
                )

        for row in LocationHours.objects.filter(location=self.location):
            data.location_hours[row.weekday].append((row.opens_at, row.closes_at))
        data.has_location_hours = bool(data.location_hours)

        for closure in LocationClosure.objects.filter(
            location=self.location, start_date__lte=end_date, end_date__gte=start_date
        ):
            data.closures.append(closure)
        for holiday in OrganizationHoliday.objects.filter(
            organization=self.organization, date__gte=start_date, date__lte=end_date
        ):
            data.holidays[holiday.date].append(holiday)

        for row in WeeklyAvailability.objects.filter(staff_id__in=staff_ids, is_active=True).filter(
            Q(location=self.location) | Q(location__isnull=True)
        ):
            data.weekly[(row.staff_id, row.day_of_week)].append((row.start_time, row.end_time))
        for row in AvailabilityException.objects.filter(
            staff_id__in=staff_ids, date__gte=start_date, date__lte=end_date
        ):
            data.exceptions[(row.staff_id, row.date)].append(row)
        for row in TimeOff.objects.filter(
            staff_id__in=staff_ids,
            approval_status="approved",
            start_datetime__lt=range_end,
            end_datetime__gt=range_start,
        ):
            data.busy[row.staff_id].append((row.start_datetime, row.end_datetime))

        bookings = Booking.objects.filter(
            staff_id__in=staff_ids,
            status__in=ACTIVE_STATUSES,
            start_datetime__lt=range_end,
            end_datetime__gt=range_start,
        ).select_related("service")
        if ignore_booking is not None:
            bookings = bookings.exclude(pk=ignore_booking.pk)
        before_new = timedelta(minutes=self.service.buffer_before_minutes)
        after_new = timedelta(minutes=self.service.buffer_after_minutes)
        for booking in bookings:
            before = max(timedelta(minutes=booking.service.buffer_before_minutes), after_new)
            after = max(timedelta(minutes=booking.service.buffer_after_minutes), before_new)
            data.busy[booking.staff_id].append(
                (booking.start_datetime - before, booking.end_datetime + after)
            )
            local_day = booking.start_datetime.astimezone(self.zone).date()
            data.daily_count[(booking.staff_id, local_day)] += 1
        return data

    # -- one provider, one day ----------------------------------------------------------------

    def _free(self, data: _Data, provider, day: date) -> list[Interval]:
        limit = provider.max_daily_appointments
        if limit and data.daily_count[(provider.pk, day)] >= limit:
            return []
        windows = [
            (self._local(day, start), self._local(day, end))
            for start, end in data.weekly[(provider.pk, day.weekday())]
        ]
        if not windows:
            return []

        exceptions = data.exceptions[(provider.pk, day)]
        if any(row.unavailable_all_day for row in exceptions):
            return []
        limited = [
            (self._local(day, row.start_time), self._local(day, row.end_time))
            for row in exceptions
            if row.start_time and row.end_time
        ]
        if exceptions:
            windows = intersect(windows, limited)

        if data.has_location_hours:
            opening = [
                (self._local(day, opens), self._local(day, closes))
                for opens, closes in data.location_hours[day.weekday()]
            ]
            windows = intersect(windows, opening)

        closed = []
        for closure in data.closures:
            if closure.start_date <= day <= closure.end_date:
                if closure.all_day:
                    return []
                closed.append(
                    (self._local(day, closure.start_time), self._local(day, closure.end_time))
                )
        for holiday in data.holidays[day]:
            if holiday.full_day_closure or not (holiday.start_time and holiday.end_time):
                return []
            closed.append(
                (self._local(day, holiday.start_time), self._local(day, holiday.end_time))
            )
        return subtract(windows, closed + data.busy[provider.pk])

    def _first_on_grid(self, moment: datetime) -> datetime:
        """The first step-grid time (on the location's wall clock) at or after ``moment``."""
        local = moment.astimezone(self.zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        # Same tzinfo on both sides: Python subtracts wall-clock times, which is the grid.
        elapsed = local - midnight
        step = self.step.total_seconds()
        remainder = elapsed.total_seconds() % step
        if remainder:
            local = local + timedelta(seconds=step - remainder)
        return local.astimezone(UTC)


class _Data:
    """Everything one calculation needs, loaded once."""

    def __init__(self):
        self.durations: dict = {}
        self.location_hours: dict[int, list] = defaultdict(list)
        self.has_location_hours = False
        self.closures: list = []
        self.holidays: dict[date, list] = defaultdict(list)
        self.weekly: dict[tuple, list] = defaultdict(list)
        self.exceptions: dict[tuple, list] = defaultdict(list)
        self.busy: dict = defaultdict(list)
        self.daily_count: dict[tuple, int] = defaultdict(int)


def get_available_slots(
    organization, service, start_date, end_date, *, location=None, staff=None, public=False
) -> list[Slot]:
    return AvailabilityService(
        organization, service, location=location, public=public
    ).get_available_slots(start_date, end_date, staff=staff)


def validate_slot(
    organization, service, staff, start, *, location=None, public=False, ignore_booking=None
) -> Candidate:
    return AvailabilityService(
        organization, service, location=location, public=public
    ).validate_slot(staff, start, ignore_booking=ignore_booking)
