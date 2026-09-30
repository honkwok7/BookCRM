"""The appointment calendar (M4.4): which appointments a member may see in a range, and how to
lay them out on a day (staff columns), week (day columns) or month grid.

Layout is computed here, not in the browser: the grid is plain HTML and CSS (Tailwind grid
utilities, no inline styles because of the Content Security Policy), so it works without
JavaScript. A row is ``SLOT_MINUTES`` long; events snap outwards to whole rows.
"""

from __future__ import annotations

import calendar as calendar_module
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import QuerySet

from bookings.models import Booking
from organizations.permissions import Capability

SLOT_MINUTES = 15
DEFAULT_FIRST_HOUR = 8
DEFAULT_LAST_HOUR = 20  # exclusive: the grid ends at 20:00
MAX_RANGE_DAYS = 62
COLOR_COUNT = 8  # .cal-color-0 ... .cal-color-7 in static/src/app.css
MAX_LANES = 24  # grid-cols-* and col-start-* up to 24 in static/src/app.css
HIDDEN_BY_DEFAULT = (Booking.Status.CANCELLED, Booking.Status.REJECTED)


def can_use_calendar(tenant) -> bool:
    """The calendar (and the sidebar link to it) is for members who see or manage
    appointments."""
    return tenant.has(Capability.APPOINTMENTS_VIEW_ALL) or tenant.has(
        Capability.APPOINTMENTS_MANAGE
    )


def calendar_bookings(
    tenant,
    start: datetime,
    end: datetime,
    *,
    location=None,
    staff=None,
    service=None,
    statuses=None,
) -> QuerySet[Booking]:
    """Appointments overlapping ``[start, end)`` that ``tenant`` may see.

    Members with ``appointments.view_all`` see the whole organization; others (providers) see
    only appointments assigned to them. Cancelled and rejected ones are hidden unless
    ``statuses`` asks for them.
    """
    bookings = Booking.objects.filter(
        organization=tenant.organization, start_datetime__lt=end, end_datetime__gt=start
    ).select_related("service", "staff__user", "customer", "location")
    if not tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
        bookings = bookings.filter(staff__user=tenant.user)
    if location is not None:
        bookings = bookings.filter(location=location)
    if staff is not None:
        bookings = bookings.filter(staff=staff)
    if service is not None:
        bookings = bookings.filter(service=service)
    if statuses:
        bookings = bookings.filter(status__in=statuses)
    else:
        bookings = bookings.exclude(status__in=HIDDEN_BY_DEFAULT)
    return bookings.order_by("start_datetime", "staff_id")


# -- layout ---------------------------------------------------------------------------------


@dataclass
class PlacedEvent:
    booking: Booking
    start: datetime  # local
    end: datetime  # local
    row: int  # 1-based grid row
    span: int
    lane: int = 1  # 1-based column inside its day or staff column
    color: int = 0


@dataclass
class Column:
    key: object  # a date (week view) or a StaffProfile (day view)
    label: str
    sublabel: str = ""
    events: list[PlacedEvent] = field(default_factory=list)
    lanes: int = 1
    is_today: bool = False


@dataclass
class TimeGrid:
    columns: list[Column]
    first_hour: int
    last_hour: int

    @property
    def rows(self) -> int:
        return (self.last_hour - self.first_hour) * 60 // SLOT_MINUTES

    @property
    def hours(self) -> list[dict]:
        """One label per hour, with the grid row it starts on."""
        per_hour = 60 // SLOT_MINUTES
        return [
            {"time": time(hour), "row": (hour - self.first_hour) * per_hour + 1}
            for hour in range(self.first_hour, self.last_hour)
        ]


def color_for(staff_id, palette: dict) -> int:
    """A stable colour per provider within one page."""
    if staff_id not in palette:
        palette[staff_id] = len(palette) % COLOR_COUNT
    return palette[staff_id]


def hour_bounds(events: list[tuple[datetime, datetime]]) -> tuple[int, int]:
    """The default working day, widened to show every event."""
    first, last = DEFAULT_FIRST_HOUR, DEFAULT_LAST_HOUR
    for start, end in events:
        first = min(first, start.hour)
        end_hour = end.hour + (1 if end.minute or end.second else 0)
        if end.date() > start.date():
            end_hour = 24
        last = max(last, end_hour)
    return first, last


def place(booking, zone, day: date, first_hour: int, last_hour: int) -> PlacedEvent | None:
    """Position ``booking`` on ``day``'s column (clipped to the day and the visible hours).

    Clipping and the length use real (UTC) time; the row comes from the wall clock, which is
    what the hour labels show. So on a daylight-saving day an appointment keeps its real
    length: one in the repeated hour still shows, one across the missing hour isn't doubled.
    """
    start = booking.start_datetime.astimezone(zone)
    end = booking.end_datetime.astimezone(zone)
    wall_day_start = datetime.combine(day, time(first_hour))
    day_start = wall_day_start.replace(tzinfo=zone).astimezone(UTC)
    day_end = (datetime.combine(day, time.min) + timedelta(hours=last_hour)).replace(tzinfo=zone)
    visible_start = max(booking.start_datetime.astimezone(UTC), day_start)
    visible_end = min(booking.end_datetime.astimezone(UTC), day_end.astimezone(UTC))
    if visible_start >= visible_end:
        return None
    wall_start = visible_start.astimezone(zone).replace(tzinfo=None)
    offset = (wall_start - wall_day_start).total_seconds() / 60
    length = (visible_end - visible_start).total_seconds() / 60
    rows = (last_hour - first_hour) * 60 // SLOT_MINUTES
    row = min(int(offset // SLOT_MINUTES) + 1, rows)
    last_row = min(int(-(-(offset + length) // SLOT_MINUTES)), rows)  # ceiling
    return PlacedEvent(booking, start, end, row, max(1, last_row - row + 1))


def assign_lanes(events: list[PlacedEvent]) -> int:
    """Side-by-side lanes for overlapping events in one column; returns the lane count.

    At most ``MAX_LANES`` (the grid classes safelisted in static/src/app.css): with more
    overlapping at once, the extra events share the least busy lanes and are drawn on top of
    each other rather than breaking the grid."""
    lanes_end: list[int] = []  # the first free row in each lane
    for event in sorted(events, key=lambda item: (item.row, -item.span)):
        for index, free_from in enumerate(lanes_end):
            if free_from <= event.row:
                event.lane = index + 1
                lanes_end[index] = event.row + event.span
                break
        else:
            if len(lanes_end) < MAX_LANES:
                lanes_end.append(event.row + event.span)
                event.lane = len(lanes_end)
            else:
                index = min(range(MAX_LANES), key=lambda i: lanes_end[i])
                event.lane = index + 1
                lanes_end[index] = max(lanes_end[index], event.row + event.span)
    return max(1, len(lanes_end))


def local_bounds(zone: ZoneInfo, first_day: date, days: int) -> tuple[datetime, datetime]:
    start = datetime.combine(first_day, time.min, tzinfo=zone)
    end = datetime.combine(first_day + timedelta(days=days), time.min, tzinfo=zone)
    return start, end


def week_grid(bookings, zone: ZoneInfo, first_day: date, today: date) -> TimeGrid:
    days = [first_day + timedelta(days=offset) for offset in range(7)]
    bookings = list(bookings)
    first, last = hour_bounds(
        [(b.start_datetime.astimezone(zone), b.end_datetime.astimezone(zone)) for b in bookings]
    )
    palette: dict = {}
    columns = []
    for day in days:
        column = Column(key=day, label=f"{day:%a}", sublabel=f"{day:%b} {day.day}")
        column.is_today = day == today
        for booking in bookings:
            event = place(booking, zone, day, first, last)
            if event is not None:
                event.color = color_for(booking.staff_id, palette)
                column.events.append(event)
        column.lanes = assign_lanes(column.events)
        columns.append(column)
    return TimeGrid(columns, first, last)


def day_grid(bookings, zone: ZoneInfo, day: date, providers) -> TimeGrid:
    """One column per provider (the "resource" view)."""
    bookings = list(bookings)
    first, last = hour_bounds(
        [(b.start_datetime.astimezone(zone), b.end_datetime.astimezone(zone)) for b in bookings]
    )
    palette: dict = {}
    columns = {
        provider.pk: Column(key=provider, label=provider.public_name) for provider in providers
    }
    for booking in bookings:
        column = columns.get(booking.staff_id)
        if column is None:  # a provider outside the list (e.g. no longer at this location)
            column = columns[booking.staff_id] = Column(
                key=booking.staff, label=booking.staff.public_name
            )
        event = place(booking, zone, day, first, last)
        if event is not None:
            event.color = color_for(booking.staff_id, palette)
            column.events.append(event)
    for column in columns.values():
        column.lanes = assign_lanes(column.events)
    return TimeGrid(list(columns.values()), first, last)


@dataclass
class MonthDay:
    date: date
    in_month: bool
    is_today: bool
    events: list[PlacedEvent] = field(default_factory=list)

    @property
    def shown(self):
        return self.events[:3]

    @property
    def more(self) -> int:
        return max(0, len(self.events) - 3)


def month_weeks(year: int, month: int) -> list[list[date]]:
    return calendar_module.Calendar(firstweekday=0).monthdatescalendar(year, month)


def month_grid(bookings, zone: ZoneInfo, year: int, month: int, today: date) -> list[list]:
    weeks = month_weeks(year, month)
    by_day: dict[date, MonthDay] = {
        day: MonthDay(day, day.month == month, day == today) for week in weeks for day in week
    }
    palette: dict = {}
    for booking in bookings:
        start = booking.start_datetime.astimezone(zone)
        cell = by_day.get(start.date())
        if cell is not None:
            end = booking.end_datetime.astimezone(zone)
            cell.events.append(
                PlacedEvent(booking, start, end, 0, 0, color=color_for(booking.staff_id, palette))
            )
    return [[by_day[day] for day in week] for week in weeks]


def month_range(year: int, month: int) -> tuple[date, date]:
    weeks = month_weeks(year, month)
    return weeks[0][0], weeks[-1][-1] + timedelta(days=1)
