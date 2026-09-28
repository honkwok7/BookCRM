from rest_framework.permissions import BasePermission

from accounts.permissions import get_specialist_profile, is_application_admin


class IsAdminOrLinkedSpecialist(BasePermission):
    """
    View-level permission for specialist-scoped URLs (e.g. /specialists/<pk>/appointments/).
    Passes for owner/admin role, or specialist role with a profile matching the URL's pk.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if is_application_admin(request.user):
            return True
        specialist_profile = get_specialist_profile(request.user)
        if specialist_profile is None:
            return False
        return specialist_profile.id == view.kwargs.get("pk")
