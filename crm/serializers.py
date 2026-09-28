from rest_framework import serializers

from core.api import TenantScopedModelSerializer
from crm.models import Tag
from crm.services import create_tag, update_tag
from organizations.selectors import get_request_organization


class TagSerializer(TenantScopedModelSerializer):
    customer_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tag
        fields = ("id", "organization", "name", "slug", "color", "customer_count", "created_at")
        read_only_fields = ("id", "organization", "slug", "created_at")

    def create(self, validated_data):
        request = self.context["request"]
        return create_tag(
            organization=get_request_organization(request), actor=request.user, **validated_data
        )

    def update(self, instance, validated_data):
        return update_tag(tag=instance, actor=self.context["request"].user, **validated_data)


class TagSummarySerializer(serializers.ModelSerializer):
    """A tag as shown on a customer."""

    class Meta:
        model = Tag
        fields = ("id", "name", "slug", "color")
        read_only_fields = fields
