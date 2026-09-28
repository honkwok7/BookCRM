from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from specialists.models import Specialist, WorkingHour


class AppointmentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    COMPLETED = "completed", "Completed"
    NO_SHOW = "no_show", "No Show"
    CANCELLED = "cancelled", "Cancelled"


# Maps each status to the set of statuses it may transition into.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    AppointmentStatus.PENDING: {AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED},
    AppointmentStatus.CONFIRMED: {
        AppointmentStatus.COMPLETED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.CANCELLED,
    },
    AppointmentStatus.COMPLETED: set(),
    AppointmentStatus.NO_SHOW: set(),
    AppointmentStatus.CANCELLED: set(),
}


class Appointment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="appointments",
    )
    specialist = models.ForeignKey(
        Specialist,
        on_delete=models.CASCADE,
        related_name="appointments",
    )
    date = models.DateField()
    time = models.TimeField()
    status = models.CharField(
        max_length=10,
        choices=AppointmentStatus.choices,
        default=AppointmentStatus.PENDING,
    )
    notes = models.TextField(blank=True)
    duration = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-time", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["specialist", "date", "time"],
                condition=models.Q(
                    status__in=[AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]
                ),
                name="unique_active_appointment_slot",
            )
        ]

    def clean(self):
        weekday = self.date.weekday()
        works = WorkingHour.objects.filter(
            specialist=self.specialist,
            day=weekday,
            start_time__lte=self.time,
            end_time__gt=self.time,
        ).exists()
        if not works:
            raise ValidationError("Selected time is outside specialist working hours.")

    def __str__(self):
        return f"{self.user} -> {self.specialist} ({self.date} {self.time})"
