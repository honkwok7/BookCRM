from core.api import TenantPrimaryKeyRelatedField, TenantScopedModelSerializer
from locations.models import Location
from organizations.selectors import get_request_organization
from services.models import Service, ServiceCategory
from services.services import create_category, create_service, update_category, update_service
from staff.services import set_service_providers


class ServiceCategorySerializer(TenantScopedModelSerializer):
    class Meta:
        model = ServiceCategory
        fields = (
            "id",
            "organization",
            "name",
            "slug",
            "color",
            "sort_order",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "slug", "created_at", "updated_at")

    def create(self, validated_data):
        request = self.context["request"]
        return create_category(
            organization=get_request_organization(request), actor=request.user, **validated_data
        )

    def update(self, instance, validated_data):
        return update_category(
            category=instance, actor=self.context["request"].user, **validated_data
        )


class ServiceSerializer(TenantScopedModelSerializer):
    locations = TenantPrimaryKeyRelatedField(
        queryset=Location.objects.all(),
        many=True,
        required=False,
        help_text="Where it is offered; empty means every location.",
    )

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
            "locations",
            "required_provider_type",
            "tax_rate",
            "cancellation_policy",
            "assigned_staff_members",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "slug", "image", "created_at", "updated_at")
        extra_kwargs = {
            "is_public": {"help_text": "Bookable online."},
            "currency": {"required": False},
        }

    # ``assigned_staff_members`` is kept for older clients: writing it sets who offers the
    # service (at all their locations) through staff.services. /staff-offerings/ is the full
    # model, with per-location offerings and custom durations and prices.

    def create(self, validated_data):
        request = self.context["request"]
        providers = validated_data.pop("assigned_staff_members", None)
        service = create_service(
            organization=get_request_organization(request), actor=request.user, **validated_data
        )
        if providers is not None:
            set_service_providers(service=service, staff_members=providers, actor=request.user)
        return service

    def update(self, instance, validated_data):
        actor = self.context["request"].user
        providers = validated_data.pop("assigned_staff_members", None)
        service = update_service(service=instance, actor=actor, **validated_data)
        if providers is not None:
            set_service_providers(service=service, staff_members=providers, actor=actor)
        return service
