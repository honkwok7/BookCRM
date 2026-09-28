from rest_framework.permissions import BasePermission

from organizations.selectors import get_request_organization, user_has_org_role


class IsPlatformAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class IsOrganizationMember(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        organization = get_request_organization(request)
        return organization is not None and user_has_org_role(request.user, organization)


class IsOrganizationManagerOrOwner(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        organization = get_request_organization(request)
        return organization is not None and user_has_org_role(
            request.user, organization, ["owner", "manager"]
        )
