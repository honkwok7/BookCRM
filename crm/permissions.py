"""CRM access rules that are more than one capability check.

Providers (staff role) have no ``customers.view`` but must see the customers they treat;
which customers exactly is decided by ``crm.selectors.customers_visible_to``.

The plain functions take a ``TenantContext`` and are shared by the API permission classes
below and the web pages (crm/web_views.py), so both apply the same rules.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from crm.models import CustomerNote
from organizations.models import OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant


def _is_provider(tenant) -> bool:
    return tenant.role == OrganizationRole.STAFF


def can_browse_customers(tenant) -> bool:
    """``customers.view``, or a provider (who then sees only their own customers)."""
    return tenant is not None and (tenant.has(Capability.CUSTOMERS_VIEW) or _is_provider(tenant))


def can_manage_customers(tenant) -> bool:
    return tenant is not None and tenant.has(Capability.CUSTOMERS_MANAGE)


def can_write_notes(tenant) -> bool:
    """``customers.manage``, or a provider (on customers they can see)."""
    return tenant is not None and (tenant.has(Capability.CUSTOMERS_MANAGE) or _is_provider(tenant))


def can_write_internal_notes(tenant) -> bool:
    """Capability holders, and providers for their own notes (which only they and capability
    holders can then read; see crm.selectors.notes_visible_to)."""
    return tenant is not None and (
        tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE) or _is_provider(tenant)
    )


def can_change_note(tenant, user, note) -> bool:
    """Edit, pin or delete a note: ``customers.notes.private``, or its author. An author may
    change their own *internal* note only while still allowed to write internal notes: a
    provider who became a receptionist, or lost the capability, keeps read access to what they
    wrote but can no longer change it."""
    if tenant is None:
        return False
    if tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE):
        return True
    if note.author_id != user.pk:
        return False
    return note.visibility != CustomerNote.Visibility.INTERNAL or can_write_internal_notes(tenant)


class CustomerAccess(BasePermission):
    """Read: ``customers.view`` or a provider. Write: ``customers.manage``."""

    def has_permission(self, request, view):
        tenant = resolve_tenant(request)
        if request.method in SAFE_METHODS:
            return can_browse_customers(tenant)
        return can_manage_customers(tenant)


class NoteAccess(BasePermission):
    """Read like customers. Write: ``customers.manage``, or a provider writing their own
    notes (on customers they can see; the serializer checks the customer)."""

    def has_permission(self, request, view):
        tenant = resolve_tenant(request)
        if request.method in SAFE_METHODS:
            return can_browse_customers(tenant)
        return can_write_notes(tenant)
