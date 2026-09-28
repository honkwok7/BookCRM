from rest_framework import serializers

from core.api import TenantMemberUserField, TenantScopedModelSerializer
from staff.models import StaffProfile


class StaffProfileSerializer(TenantScopedModelSerializer):
    # A staff profile can only be created for someone who already belongs to the organization.
    user = TenantMemberUserField()
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = StaffProfile
        fields = (
            "id",
            "user",
            "user_email",
            "organization",
            "job_title",
            "bio",
            "profile_image",
            "phone_number",
            "is_active",
            "is_accepting_bookings",
            "appointment_color",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def validate_user(self, user):
        if self.instance is not None and user != self.instance.user:
            raise serializers.ValidationError("The user of a staff profile cannot be changed.")
        return user
