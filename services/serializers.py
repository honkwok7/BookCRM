from rest_framework import serializers

from organizations.selectors import get_request_organization
from services.models import Service, ServiceCategory


class ServiceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceCategory
        fields = ("id", "organization", "name", "slug", "created_at", "updated_at")
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = (
            "id",
            "organization",
            "name",
            "slug",
            "description",
            "category",
            "price",
            "currency",
            "duration_minutes",
            "buffer_before_minutes",
            "buffer_after_minutes",
            "is_active",
            "is_public",
            "is_archived",
            "image",
            "color",
            "max_advance_days",
            "min_notice_minutes",
            "cancellation_deadline_hours",
            "rescheduling_deadline_hours",
            "capacity",
            "assigned_staff_members",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        organization = get_request_organization(self.context["request"])
        validated_data["organization"] = organization
        return super().create(validated_data)
