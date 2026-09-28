from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from core.api import TenantScopedModelSerializer
from crm.models import CustomerActivity, CustomerNote, Tag
from crm.services import create_note, create_tag, update_note, update_tag
from organizations.permissions import Capability
from organizations.selectors import get_request_organization
from organizations.tenancy import resolve_tenant


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


def _can_use_internal_notes(request) -> bool:
    tenant = resolve_tenant(request)
    return tenant is not None and tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE)


class CustomerNoteSerializer(TenantScopedModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = CustomerNote
        fields = (
            "id",
            "organization",
            "customer",
            "author",
            "author_name",
            "note_type",
            "visibility",
            "content",
            "pinned",
            "edited_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "author", "edited_at", "created_at", "updated_at")

    def get_author_name(self, note) -> str:
        author = note.author
        return (author.get_full_name() or author.email) if author else ""

    def validate_customer(self, customer):
        if self.instance is not None and customer != self.instance.customer:
            raise serializers.ValidationError("A note cannot be moved to another customer.")
        return customer

    def validate_visibility(self, visibility):
        if visibility == CustomerNote.Visibility.INTERNAL and not _can_use_internal_notes(
            self.context["request"]
        ):
            raise PermissionDenied("Internal notes need the customers.notes.private permission.")
        return visibility

    def validate(self, attrs):
        # The default visibility is internal: check it when the client didn't choose one.
        if self.instance is None and "visibility" not in attrs:
            attrs["visibility"] = self.validate_visibility(CustomerNote.Visibility.INTERNAL)
        return attrs

    def create(self, validated_data):
        customer = validated_data.pop("customer")
        return create_note(customer=customer, author=self.context["request"].user, **validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("customer", None)
        return update_note(note=instance, actor=self.context["request"].user, **validated_data)


class CustomerActivitySerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = CustomerActivity
        fields = (
            "id",
            "kind",
            "kind_display",
            "actor",
            "actor_name",
            "subject_type",
            "subject_id",
            "metadata",
            "occurred_at",
        )
        read_only_fields = fields

    def get_actor_name(self, activity) -> str:
        actor = activity.actor
        return (actor.get_full_name() or actor.email) if actor else ""
