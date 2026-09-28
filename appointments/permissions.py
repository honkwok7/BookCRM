from rest_framework.permissions import BasePermission

from accounts.models import UserRole
from accounts.permissions import get_specialist_profile, has_role, is_application_admin


class IsAdminOrAssignedSpecialist(BasePermission):
    """Owner/admin role, or specialist role with the assigned profile."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if is_application_admin(request.user):
            return True
        specialist_profile = get_specialist_profile(request.user)
        return specialist_profile is not None and obj.specialist_id == specialist_profile.id


class IsAppointmentCanceller(BasePermission):
    """Owner/admin, owning customer, or assigned specialist; also used for reschedule."""

    def has_object_permission(self, request, view, obj):
        if is_application_admin(request.user):
            return True
        if has_role(request.user, UserRole.CUSTOMER) and obj.user_id == request.user.id:
            return True
        specialist_profile = get_specialist_profile(request.user)
        return specialist_profile is not None and obj.specialist_id == specialist_profile.id
