from core.api import TenantScopedModelSerializer
from services.models import Service, ServiceCategory
from staff.services import set_service_providers


class ServiceCategorySerializer(TenantScopedModelSerializer):
    class Meta:
        model = ServiceCategory
        fields = ("id", "organization", "name", "slug", "created_at", "updated_at")
        read_only_fields = ("id", "organization", "created_at", "updated_at")


class ServiceSerializer(TenantScopedModelSerializer):
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

    # ``assigned_staff_members`` is kept for older clients: writing it sets who offers the
    # service (at all their locations) through staff.services. /staff-offerings/ is the full
    # model, with per-location offerings and custom durations and prices.

    def create(self, validated_data):
        providers = validated_data.pop("assigned_staff_members", None)
        service = super().create(validated_data)
        if providers is not None:
            set_service_providers(
                service=service, staff_members=providers, actor=self.context["request"].user
            )
        return service

    def update(self, instance, validated_data):
        providers = validated_data.pop("assigned_staff_members", None)
        service = super().update(instance, validated_data)
        if providers is not None:
            set_service_providers(
                service=service, staff_members=providers, actor=self.context["request"].user
            )
        return service
