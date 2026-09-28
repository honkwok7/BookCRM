from rest_framework import serializers

from organizations.selectors import get_request_organization
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)


class WeeklyAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = WeeklyAvailability
        fields = "__all__"
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)


class AvailabilityExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityException
        fields = "__all__"
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)


class TimeOffSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimeOff
        fields = "__all__"
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)


class OrganizationHolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationHoliday
        fields = "__all__"
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)
