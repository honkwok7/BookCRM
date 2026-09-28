import django_filters
from django.db.models import Count, Q
from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from bookings.models import Customer
from core.api import AuditedModelViewSetMixin
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from crm.models import CustomerNote, Tag
from crm.selectors import team_notes
from crm.serializers import CustomerNoteSerializer, TagSerializer
from crm.services import delete_note, delete_tag
from organizations.permissions import Capability
from organizations.selectors import scope_queryset_by_organization
from organizations.tenancy import resolve_tenant


class TagViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Organization-defined customer tags. Writes go through crm.services (audited there)."""

    audited_by_service = ("create", "update", "delete")
    serializer_class = TagSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="customers.view", write="customers.manage"),
    ]
    search_fields = ("name",)
    ordering_fields = ("name", "created_at")

    def get_queryset(self):
        queryset = Tag.objects.select_related("organization").annotate(
            customer_count=Count(
                "customer_tags", filter=~Q(customer_tags__customer__status="anonymized")
            )
        )
        return scope_queryset_by_organization(queryset, self.request)

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        delete_tag(tag=instance, actor=self.request.user)


def _include_internal(request) -> bool:
    tenant = resolve_tenant(request)
    return tenant is not None and tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE)


class CustomerNoteFilter(django_filters.FilterSet):
    customer = TenantModelChoiceFilter(Customer)

    class Meta:
        model = CustomerNote
        fields = ("customer", "visibility", "note_type", "pinned")


class CustomerNoteViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Team notes on customers.

    Internal notes are invisible (404) without ``customers.notes.private``. A note can be
    edited or deleted by its author, or by anyone holding ``customers.notes.private``.
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = CustomerNoteSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="customers.view", write="customers.manage"),
    ]
    filterset_class = CustomerNoteFilter
    search_fields = ("content",)
    ordering_fields = ("created_at", "pinned")

    def get_queryset(self):
        tenant = resolve_tenant(self.request)
        if tenant is None:
            return CustomerNote.objects.none()
        return team_notes(tenant.organization, include_internal=_include_internal(self.request))

    def _check_can_change(self, note):
        if note.author_id != self.request.user.pk and not _include_internal(self.request):
            raise PermissionDenied("Only the author can change this note.")

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        self._check_can_change(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._check_can_change(instance)
        delete_note(note=instance, actor=self.request.user)
