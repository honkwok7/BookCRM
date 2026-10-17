"""CRM access rules that are more than one capability check.

Providers (staff role) have no ``customers.view`` but must see the customers they treat;
which customers exactly is decided by ``crm.selectors.customers_visible_to``.

The plain functions take a ``TenantContext`` and are shared by the API permission classes
below and the web pages (crm/web_views.py), so both apply the same rules.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from crm.models import CustomerNote
from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant


def _sees_own_customers(tenant) -> bool:
    return tenant.has(Capability.CUSTOMERS_VIEW_OWN)


def can_browse_customers(tenant) -> bool:
    """``customers.view``, or ``customers.view_own`` (providers: only their own customers)."""
    return tenant is not None and (
        tenant.has(Capability.CUSTOMERS_VIEW) or _sees_own_customers(tenant)
    )


def can_manage_customers(tenant) -> bool:
    return tenant is not None and tenant.has(Capability.CUSTOMERS_MANAGE)


def can_write_notes(tenant) -> bool:
    """``customers.manage``, or ``customers.view_own`` (on the customers they can see)."""
    return tenant is not None and (
        tenant.has(Capability.CUSTOMERS_MANAGE) or _sees_own_customers(tenant)
    )


def can_write_internal_notes(tenant) -> bool:
    """Internal notes need ``customers.notes.private``, to write as well as to read."""
    return tenant is not None and tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE)


def can_change_note(tenant, user, note) -> bool:
    """Edit, pin or delete a note: ``customers.notes.private``, or the author of a note that
    isn't internal. Without the capability nobody reads or changes internal notes, not even
    the ones they wrote (crm.selectors.notes_visible_to)."""
    if tenant is None:
        return False
    if tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE):
        return True
    return note.author_id == user.pk and note.visibility != CustomerNote.Visibility.INTERNAL


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
