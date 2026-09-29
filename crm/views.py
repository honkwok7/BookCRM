import django_filters
from django.db.models import Count, Q
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from bookings.models import Customer
from bookings.selectors import bookings_visible_to
from bookings.serializers import BookingSerializer, CustomerDetailSerializer, CustomerSerializer
from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from crm.models import CustomerNote, Tag
from crm.permissions import CustomerAccess, NoteAccess
from crm.selectors import (
    can_read_internal_notes,
    customer_timeline,
    customers_visible_to,
    last_visit_annotation,
    notes_visible_to,
    search_filter,
)
from crm.serializers import (
    CustomerActivitySerializer,
    CustomerAnonymizeSerializer,
    CustomerMergeSerializer,
    CustomerNoteSerializer,
    CustomerSearchResultSerializer,
    TagSerializer,
)
from crm.services import anonymize_customer, delete_note, delete_tag, merge_customers
from organizations.selectors import scope_queryset_by_organization
from staff.models import StaffProfile


class CustomerFilter(django_filters.FilterSet):
    # Every relation filter is tenant-safe: an id from another organization answers like an
    # id that doesn't exist.
    search = django_filters.CharFilter(method="filter_search")
    tag = TenantModelChoiceFilter(Tag, field_name="customer_tags__tag")
    assigned_staff = TenantModelChoiceFilter(StaffProfile)
    preferred_staff = TenantModelChoiceFilter(StaffProfile)
    staff = TenantModelChoiceFilter(StaffProfile, method="filter_staff")
    last_visit_before = django_filters.DateFilter(field_name="last_visit", lookup_expr="date__lt")
    last_visit_after = django_filters.DateFilter(field_name="last_visit", lookup_expr="date__gte")
    never_visited = django_filters.BooleanFilter(field_name="last_visit", lookup_expr="isnull")

    class Meta:
        model = Customer
        fields = ("status", "tag", "assigned_staff", "preferred_staff")

    def filter_search(self, queryset, name, value):
        return search_filter(queryset, value)

    def filter_staff(self, queryset, name, staff):
        # Assigned to, preferring, or ever booked with this staff member.
        booked = staff.bookings.values("customer_id")
        return queryset.filter(
            Q(assigned_staff=staff) | Q(preferred_staff=staff) | Q(pk__in=booked)
        )


class CustomerViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """CRM customers.

    Read: ``customers.view``, or providers for their own customers (read-only). Write:
    ``customers.manage``. Delete and anonymize: ``customers.erase`` (irreversible).
    """

    audit_actions = {"delete": AuditAction.CUSTOMER_DELETED}
    # crm.services audits create/update (field-level, personal values redacted).
    audited_by_service = ("create", "update")
    serializer_class = CustomerSerializer
    permission_classes = [permissions.IsAuthenticated, CustomerAccess]
    filterset_class = CustomerFilter
    ordering_fields = ("last_name", "first_name", "created_at", "last_visit")
    ordering = ("last_name", "first_name", "created_at")

    def get_permissions(self):
        if self.action in ("destroy", "anonymize"):
            return [permissions.IsAuthenticated(), HasCapability(write="customers.erase")()]
        return super().get_permissions()

    def get_queryset(self):
        return (
            customers_visible_to(self.request)
            .select_related("organization", "user")
            .prefetch_related("tag_set")
            .annotate(last_visit=last_visit_annotation())
        )

    def get_serializer_class(self):
        return {
            "retrieve": CustomerDetailSerializer,
            "timeline": CustomerActivitySerializer,
            "notes": CustomerNoteSerializer,
            "appointments": BookingSerializer,
            "search": CustomerSearchResultSerializer,
        }.get(self.action, CustomerSerializer)

    def _paginated(self, queryset):
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=False, methods=["get"])
    def search(self, request):
        """Quick lookup (``?q=``, at least 2 characters): at most 20 matches."""
        query = request.query_params.get("q", "").strip()
        if len(query) < 2:
            raise ValidationError({"q": "Enter at least 2 characters."})
        matches = search_filter(
            customers_visible_to(request).exclude(status=Customer.Status.ANONYMIZED), query
        ).order_by("last_name", "first_name")[:20]
        return Response(self.get_serializer(matches, many=True).data)

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """The customer's history, newest first (paginated). Internal entries need
        customers.notes.private."""
        activities = customer_timeline(
            self.get_object(), include_internal=can_read_internal_notes(request)
        )
        return self._paginated(activities)

    @action(detail=True, methods=["get"])
    def notes(self, request, pk=None):
        """The customer's notes the caller may read, pinned first. Write via /customer-notes/."""
        return self._paginated(notes_visible_to(request).filter(customer=self.get_object()))

    @action(detail=True, methods=["get"])
    def appointments(self, request, pk=None):
        """The customer's appointments the caller may see, newest first."""
        bookings = bookings_visible_to(request).filter(customer=self.get_object())
        return self._paginated(bookings.order_by("-start_datetime"))

    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        """Fold ``duplicate`` into this customer (appointments, notes, history, tags)."""
        target = self.get_object()
        serializer = CustomerMergeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        merged = merge_customers(
            target=target, duplicate=serializer.validated_data["duplicate"], actor=request.user
        )
        return Response(CustomerDetailSerializer(merged, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def anonymize(self, request, pk=None):
        """Irreversibly replace the customer's personal data (needs ``confirm: true``)."""
        customer = self.get_object()
        CustomerAnonymizeSerializer(data=request.data).is_valid(raise_exception=True)
        customer = anonymize_customer(customer=customer, actor=request.user)
        return Response(CustomerDetailSerializer(customer, context={"request": request}).data)

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()


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


class CustomerNoteFilter(django_filters.FilterSet):
    customer = TenantModelChoiceFilter(Customer)

    class Meta:
        model = CustomerNote
        fields = ("customer", "visibility", "note_type", "pinned")


class CustomerNoteViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Team notes on customers.

    Internal notes are invisible (404) without ``customers.notes.private``, except to their
    author. Providers read and write notes on their own customers only. A note can be edited
    or deleted by its author, or by anyone holding ``customers.notes.private``.
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = CustomerNoteSerializer
    permission_classes = [permissions.IsAuthenticated, NoteAccess]
    filterset_class = CustomerNoteFilter
    search_fields = ("content",)
    ordering_fields = ("created_at", "pinned")

    def get_queryset(self):
        return notes_visible_to(self.request)

    def _check_can_change(self, note):
        if note.author_id != self.request.user.pk and not can_read_internal_notes(self.request):
            raise PermissionDenied("Only the author can change this note.")

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        self._check_can_change(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._check_can_change(instance)
        delete_note(note=instance, actor=self.request.user)
