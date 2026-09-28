from core.api import TenantScopedModelSerializer
from services.models import Service, ServiceCategory


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
