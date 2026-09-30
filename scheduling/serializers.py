from django.core.exceptions import ValidationError as ModelValidationError
from rest_framework import serializers

from core.api import TenantScopedModelSerializer
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)

READ_ONLY = ("id", "organization", "created_at", "updated_at")


class ModelCleanMixin:
    """Run the model's ``clean()`` (its interval rules) on the merged data: DRF doesn't."""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        model = self.Meta.model
        values = {}
        for field in self.clean_fields:
            if field in attrs:
                values[field] = attrs[field]
            elif self.instance is not None:
                values[field] = getattr(self.instance, field)
            else:  # creating without it: the model's default applies
                values[field] = model._meta.get_field(field).get_default()
        instance = model(**values)
        try:
            instance.clean()
        except ModelValidationError as error:
            raise serializers.ValidationError({"non_field_errors": error.messages}) from error
        return attrs


class WeeklyAvailabilitySerializer(TenantScopedModelSerializer):
    """Weekly hours, Monday = 0, in the location's time zone. ``location`` empty: at any
    location the person works at."""

    class Meta:
        model = WeeklyAvailability
        fields = (
            *READ_ONLY,
            "staff",
            "location",
            "day_of_week",
            "start_time",
            "end_time",
            "is_active",
        )
        read_only_fields = READ_ONLY

    def validate(self, attrs):
        staff = attrs.get("staff", getattr(self.instance, "staff", None))
        location = attrs.get("location", getattr(self.instance, "location", None))
        start = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if start is not None and end is not None and start >= end:
            raise serializers.ValidationError({"end_time": "Must be after start_time."})
        if location is not None and not staff.locations.filter(pk=location.pk).exists():
            raise serializers.ValidationError(
                {"location": "This person doesn't work at that location."}
            )
        return attrs


class AvailabilityExceptionSerializer(ModelCleanMixin, TenantScopedModelSerializer):
    clean_fields = ("unavailable_all_day", "start_time", "end_time")

    class Meta:
        model = AvailabilityException
        fields = (
            *READ_ONLY,
            "staff",
            "date",
            "unavailable_all_day",
            "start_time",
            "end_time",
            "reason",
        )
        read_only_fields = READ_ONLY


class TimeOffSerializer(ModelCleanMixin, TenantScopedModelSerializer):
    clean_fields = ("start_datetime", "end_datetime")

    class Meta:
        model = TimeOff
        fields = (
            *READ_ONLY,
            "staff",
            "start_datetime",
            "end_datetime",
            "reason",
            "approval_status",
        )
        read_only_fields = READ_ONLY


class OrganizationHolidaySerializer(ModelCleanMixin, TenantScopedModelSerializer):
    clean_fields = ("full_day_closure", "start_time", "end_time")

    class Meta:
        model = OrganizationHoliday
        fields = (*READ_ONLY, "date", "name", "full_day_closure", "start_time", "end_time")
        read_only_fields = READ_ONLY
