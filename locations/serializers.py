from rest_framework import serializers

from core.api import TenantScopedModelSerializer
from locations.models import Location, LocationClosure, LocationHours, validate_timezone
from locations.services import (
    create_closure,
    create_location,
    update_closure,
    update_location,
)
from organizations.selectors import get_request_organization


class LocationHoursSerializer(serializers.ModelSerializer):
    class Meta:
        model = LocationHours
        fields = ("weekday", "opens_at", "closes_at")


class LocationHoursReplaceSerializer(serializers.Serializer):
    """``PUT /locations/{id}/hours/``: the full weekly hours (an empty list clears them)."""

    hours = LocationHoursSerializer(many=True)

    def periods(self):
        return [
            (row["weekday"], row["opens_at"], row["closes_at"])
            for row in self.validated_data["hours"]
        ]


class LocationSerializer(TenantScopedModelSerializer):
    hours = LocationHoursSerializer(many=True, read_only=True)

    class Meta:
        model = Location
        fields = (
            "id",
            "organization",
            "name",
            "slug",
            "is_default",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country",
            "timezone",
            "phone",
            "email",
            "is_active",
            "booking_enabled",
            "booking_settings",
            "hours",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "organization",
            "slug",
            "is_default",
            "booking_settings",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {"timezone": {"required": False}}

    def validate_timezone(self, value):
        validate_timezone(value)
        return value

    def create(self, validated_data):
        request = self.context["request"]
        return create_location(
            organization=get_request_organization(request), actor=request.user, **validated_data
        )

    def update(self, instance, validated_data):
        return update_location(
            location=instance, actor=self.context["request"].user, **validated_data
        )


class LocationClosureSerializer(TenantScopedModelSerializer):
    class Meta:
        model = LocationClosure
        fields = (
            "id",
            "organization",
            "location",
            "start_date",
            "end_date",
            "all_day",
            "start_time",
            "end_time",
            "reason",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "created_at", "updated_at")
        extra_kwargs = {"end_date": {"required": False}}

    def validate_location(self, location):
        if self.instance is not None and location != self.instance.location:
            raise serializers.ValidationError("A closure cannot be moved to another location.")
        return location

    def create(self, validated_data):
        return create_closure(actor=self.context["request"].user, **validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("location", None)
        return update_closure(
            closure=instance, actor=self.context["request"].user, **validated_data
        )
