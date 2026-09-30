from django.contrib.auth import get_user_model
from rest_framework import serializers

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.permissions import can_grant_role

User = get_user_model()


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = (
            "id",
            "public_uuid",
            "name",
            "slug",
            "logo",
            "description",
            "email",
            "phone",
            "website",
            "address",
            "timezone",
            "currency",
            "booking_page_enabled",
            "booking_page_theme",
            "default_appointment_rules",
            "allow_guest_booking",
            "booking_instructions",
            "brand_color",
            "reminder_hours_before",
            "second_reminder_hours_before",
            "is_active",
            "is_suspended",
            "created_at",
            "updated_at",
        )
        # Activation and suspension are platform-level decisions, never tenant-editable.
        read_only_fields = (
            "id",
            "public_uuid",
            "is_active",
            "is_suspended",
            "created_at",
            "updated_at",
        )


class OrganizationMembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = (
            "id",
            "organization",
            "user",
            "user_email",
            "role",
            "title",
            "capabilities",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    capabilities = serializers.SerializerMethodField()

    def get_capabilities(self, obj) -> list[str]:
        return sorted(obj.capabilities)


class InvitationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationInvitation
        fields = ("id", "email", "role", "expires_at")
        read_only_fields = ("id",)
        extra_kwargs = {"expires_at": {"required": False}}

    def validate_role(self, role):
        tenant = self.context.get("tenant")
        if tenant is None or not can_grant_role(tenant.role, role):
            raise serializers.ValidationError(
                "You cannot invite someone with a role above your own."
            )
        return role


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField()
