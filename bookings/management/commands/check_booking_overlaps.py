"""List active appointments that overlap another one of the same staff member.

Run before migration ``bookings.0010`` (which adds the PostgreSQL no-overlap constraint and
refuses to run while overlaps exist). Exits with status 1 when any are found.
"""

from django.core.management.base import BaseCommand, CommandError

from bookings.selectors import overlapping_bookings


class Command(BaseCommand):
    help = "List overlapping active appointments (the same staff member at the same time)."

    def handle(self, *args, **options):
        offenders = list(overlapping_bookings())
        if not offenders:
            self.stdout.write(self.style.SUCCESS("No overlapping appointments."))
            return
        for booking in offenders:
            self.stdout.write(
                f"{booking.organization.name}: {booking.reference} "
                f"{booking.staff.full_name} {booking.start_datetime:%Y-%m-%d %H:%M}"
                f"-{booking.end_datetime:%H:%M} UTC ({booking.status})"
            )
        raise CommandError(
            f"{len(offenders)} overlapping appointments. Cancel or move them, then migrate."
        )
