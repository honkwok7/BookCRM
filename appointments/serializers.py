from django.utils import timezone
from rest_framework import serializers

from appointments import transactions as appointment_transactions
from appointments.models import ALLOWED_TRANSITIONS, Appointment
from appointments.scheduling import scheduling_context, scheduling_error


class AppointmentSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    specialist_name = serializers.CharField(source="specialist.name", read_only=True)

    class Meta:
        model = Appointment
        fields = (
            "id",
            "user",
            "specialist",
            "specialist_name",
            "date",
            "time",
            "status",
            "notes",
            "duration",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "user", "status", "created_at", "updated_at")

    def validate(self, attrs):
        specialist = attrs.get("specialist")
        date = attrs.get("date")
        time = attrs.get("time")

        if date < timezone.localdate():
            raise serializers.ValidationError("Cannot book an appointment in the past.")

        if date == timezone.localdate() and time <= timezone.localtime().time():
            raise serializers.ValidationError("Cannot book an appointment at a past time today.")

        working, occupied = scheduling_context(specialist, date)
        error = scheduling_error(
            date,
            time,
            attrs.get("duration", specialist.slot_duration),
            specialist.slot_duration,
            working,
            occupied,
        )
        if error:
            raise serializers.ValidationError(error)

        return attrs

    def create(self, validated_data):
        # The creation view owns the transaction, including initial validation.
        validated_data["specialist"] = appointment_transactions.lock_specialist(
            validated_data["specialist"].pk,
        )
        # Initial DRF validation is advisory: occupancy and specialist data
        # must be checked again after acquiring the scheduling lock.
        self.validate(validated_data)
        validated_data["user"] = self.context["request"].user
        if "duration" not in validated_data:
            validated_data["duration"] = validated_data["specialist"].slot_duration
        return super().create(validated_data)


class AppointmentStatusSerializer(serializers.ModelSerializer):
    """Used exclusively for status transitions. Validates lifecycle rules."""

    class Meta:
        model = Appointment
        fields = ("status",)

    def validate_status(self, new_status):
        current = self.instance.status
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise serializers.ValidationError(
                f"Cannot transition appointment from '{current}' to '{new_status}'."
            )
        return new_status

    def update(self, instance, validated_data):
        instance.status = validated_data["status"]
        instance.save(update_fields=["status", "updated_at"])
        return instance


class AppointmentRescheduleSerializer(serializers.Serializer):
    """Validates the new date/time for a reschedule request."""

    date = serializers.DateField()
    time = serializers.TimeField()

    def validate(self, attrs):
        appointment = self.context["appointment"]
        new_date = attrs["date"]
        new_time = attrs["time"]
        specialist = appointment.specialist

        if appointment.date == new_date and appointment.time == new_time:
            raise serializers.ValidationError("New slot is identical to the current slot.")

        if new_date < timezone.localdate():
            raise serializers.ValidationError("Cannot reschedule to a past date.")

        if new_date == timezone.localdate() and new_time <= timezone.localtime().time():
            raise serializers.ValidationError("Cannot reschedule to a past time today.")

        working, occupied = scheduling_context(
            specialist,
            new_date,
            exclude_pk=appointment.pk,
        )
        error = scheduling_error(
            new_date,
            new_time,
            appointment.duration,
            specialist.slot_duration,
            working,
            occupied,
        )
        if error:
            raise serializers.ValidationError(error)

        return attrs
