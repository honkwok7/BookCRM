from core.api import TenantScopedModelSerializer
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)

READ_ONLY = ("id", "organization", "created_at", "updated_at")


class WeeklyAvailabilitySerializer(TenantScopedModelSerializer):
    class Meta:
        model = WeeklyAvailability
        fields = (*READ_ONLY, "staff", "day_of_week", "start_time", "end_time", "is_active")
        read_only_fields = READ_ONLY


class AvailabilityExceptionSerializer(TenantScopedModelSerializer):
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


class TimeOffSerializer(TenantScopedModelSerializer):
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


class OrganizationHolidaySerializer(TenantScopedModelSerializer):
    class Meta:
        model = OrganizationHoliday
        fields = (*READ_ONLY, "date", "name", "full_day_closure", "start_time", "end_time")
        read_only_fields = READ_ONLY
