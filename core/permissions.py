from rest_framework.permissions import SAFE_METHODS, BasePermission

from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant


class IsPlatformAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class IsOrganizationMember(BasePermission):
    """Any active membership in the request's (non-suspended) organization."""

    def has_permission(self, request, view):
        return resolve_tenant(request) is not None


def HasCapability(read=None, write=None):  # noqa: N802 - reads like a permission class
    """Permission class requiring tenant capabilities.

    ``read`` applies to safe methods (GET/HEAD/OPTIONS), ``write`` to everything else.
    Each may be a Capability, a code string, or a tuple of them (all required).
    ``write`` defaults to ``read``; a side left as None is denied.

        permission_classes = [IsAuthenticated, HasCapability(read="services.view",
                                                             write="services.manage")]
    """

    def _normalize(value):
        if value is None:
            return None
        values = value if isinstance(value, tuple | list | set | frozenset) else (value,)
        return tuple(Capability(v) for v in values)

    read_caps = _normalize(read)
    write_caps = _normalize(write) if write is not None else read_caps

    class _HasCapability(BasePermission):
        required_read = read_caps
        required_write = write_caps

        def has_permission(self, request, view):
            context = resolve_tenant(request)
            if context is None:
                return False
            required = self.required_read if request.method in SAFE_METHODS else self.required_write
            return required is not None and context.has(*required)

    _HasCapability.__name__ = f"HasCapability({read_caps}, {write_caps})"
    return _HasCapability
