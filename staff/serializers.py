from rest_framework import serializers

from core.api import (
    TenantMemberUserField,
    TenantPrimaryKeyRelatedField,
    TenantScopedModelSerializer,
)
from locations.models import Location
from organizations.selectors import get_request_organization
from staff.models import StaffProfile, StaffServiceOffering
from staff.services import (
    add_offering,
    create_staff_profile,
    set_staff_locations,
    update_offering,
    update_staff_profile,
)


class StaffProfileSerializer(TenantScopedModelSerializer):
    # A staff profile can only be created for someone who already belongs to the organization.
    user = TenantMemberUserField()
    user_email = serializers.EmailField(source="user.email", read_only=True)
    public_name = serializers.CharField(read_only=True)
    locations = TenantPrimaryKeyRelatedField(
        queryset=Location.objects.all(), many=True, required=False
    )

    class Meta:
        model = StaffProfile
        fields = (
            "id",
            "user",
            "user_email",
            "organization",
            "display_name",
            "public_name",
            "job_title",
            "provider_type",
            "bio",
            "profile_image",
            "phone_number",
            "is_active",
            "is_accepting_bookings",
            "online_booking_visible",
            "max_daily_appointments",
            "appointment_color",
            "locations",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "profile_image", "created_at", "updated_at")

    def validate_user(self, user):
        if self.instance is not None and user != self.instance.user:
            raise serializers.ValidationError("The user of a staff profile cannot be changed.")
        return user

    def create(self, validated_data):
        request = self.context["request"]
        return create_staff_profile(
            organization=get_request_organization(request), actor=request.user, **validated_data
        )

    def update(self, instance, validated_data):
        actor = self.context["request"].user
        validated_data.pop("user", None)
        locations = validated_data.pop("locations", None)
        staff = update_staff_profile(staff=instance, actor=actor, **validated_data)
        if locations is not None:
            staff = set_staff_locations(staff=staff, locations=locations, actor=actor)
        return staff


class StaffServiceOfferingSerializer(TenantScopedModelSerializer):
    duration_minutes = serializers.IntegerField(read_only=True)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = StaffServiceOffering
        fields = (
            "id",
            "organization",
            "staff",
            "service",
            "location",
            "custom_duration_minutes",
            "custom_price",
            "duration_minutes",
            "price",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def validate(self, attrs):
        if self.instance is not None:
            for name in ("staff", "service", "location"):
                if name in attrs and attrs[name] != getattr(self.instance, name):
                    raise serializers.ValidationError(
                        {name: "Remove the offering and add a new one instead."}
                    )
        return attrs

    def create(self, validated_data):
        return add_offering(actor=self.context["request"].user, **validated_data)

    def update(self, instance, validated_data):
        for name in ("staff", "service", "location"):
            validated_data.pop(name, None)
        return update_offering(
            offering=instance, actor=self.context["request"].user, **validated_data
        )
