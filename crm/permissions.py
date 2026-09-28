"""CRM access rules that are more than one capability check.

Providers (staff role) have no ``customers.view`` but must see the customers they treat;
which customers exactly is decided by ``crm.selectors.customers_visible_to``.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from organizations.models import OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant


def _is_provider(tenant) -> bool:
    return tenant.role == OrganizationRole.STAFF


class CustomerAccess(BasePermission):
    """Read: ``customers.view`` or a provider. Write: ``customers.manage``."""

    def has_permission(self, request, view):
        tenant = resolve_tenant(request)
        if tenant is None:
            return False
        if request.method in SAFE_METHODS:
            return tenant.has(Capability.CUSTOMERS_VIEW) or _is_provider(tenant)
        return tenant.has(Capability.CUSTOMERS_MANAGE)


class NoteAccess(BasePermission):
    """Read like customers. Write: ``customers.manage``, or a provider writing their own
    notes (on customers they can see; the serializer checks the customer)."""

    def has_permission(self, request, view):
        tenant = resolve_tenant(request)
        if tenant is None:
            return False
        if request.method in SAFE_METHODS:
            return tenant.has(Capability.CUSTOMERS_VIEW) or _is_provider(tenant)
        return tenant.has(Capability.CUSTOMERS_MANAGE) or _is_provider(tenant)
