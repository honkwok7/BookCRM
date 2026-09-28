from django.contrib.auth import get_user_model
from rest_framework import serializers

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership

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
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class InvitationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationInvitation
        fields = ("id", "email", "role", "expires_at")
        read_only_fields = ("id",)


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField()
