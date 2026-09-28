"""Tenant-safe DRF building blocks.

Every writable relation in a tenant API must use ``TenantPrimaryKeyRelatedField`` (or
``TenantMemberUserField`` for users), so a request can only reference objects that belong to
its own organization. An id from another organization is rejected exactly like an id that
does not exist, which also avoids confirming that it exists. ``test_api_contract`` enforces
this for every serializer reachable from the API router.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from organizations.selectors import get_request_organization


def _request_organization(field):
    request = field.context.get("request")
    return get_request_organization(request) if request is not None else None


class TenantPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """Primary-key relation limited to rows of the request's organization."""

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = _request_organization(self)
        if organization is None:
            return queryset.none()
        return queryset.filter(organization=organization)


class TenantMemberUserField(serializers.PrimaryKeyRelatedField):
    """A user who holds an active membership in the request's organization."""

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", get_user_model().objects.all())
        super().__init__(**kwargs)

    def get_queryset(self):
        organization = _request_organization(self)
        if organization is None:
            return get_user_model().objects.none()
        return (
            super()
            .get_queryset()
            .filter(
                organization_memberships__organization=organization,
                organization_memberships__is_active=True,
            )
            .distinct()
        )


class TenantScopedModelSerializer(serializers.ModelSerializer):
    """Sets ``organization`` from the tenant on create; the field is always read-only."""

    serializer_related_field = TenantPrimaryKeyRelatedField

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)
