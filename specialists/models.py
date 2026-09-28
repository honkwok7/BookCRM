from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class Specialist(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="specialist_profile",
    )
    name = models.CharField(max_length=120)
    profession = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="specialists/", blank=True, null=True)
    slot_duration = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.profession})"


class WorkingHour(models.Model):
    specialist = models.ForeignKey(
        Specialist,
        on_delete=models.CASCADE,
        related_name="working_hours",
    )
    day = models.IntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["specialist", "day", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["specialist", "day", "start_time", "end_time"],
                name="unique_working_interval_per_specialist",
            )
        ]

    def clean(self):
        if self.start_time >= self.end_time:
            raise ValidationError("Start time must be before end time.")

    def __str__(self):
        return (
            f"{self.specialist.name} - {self.get_day_display()} {self.start_time}-{self.end_time}"
        )


# Create your models here.
