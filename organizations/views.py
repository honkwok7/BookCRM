from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsOrganizationManagerOrOwner
from core.services import write_audit_log
from organizations.models import OrganizationInvitation, OrganizationMembership
from organizations.selectors import get_request_organization, scope_queryset_by_organization
from organizations.serializers import (
	InvitationAcceptSerializer,
	InvitationCreateSerializer,
	OrganizationMembershipSerializer,
	OrganizationSerializer,
)
from organizations.services import accept_invitation


User = get_user_model()


class CurrentOrganizationView(generics.RetrieveUpdateAPIView):
	serializer_class = OrganizationSerializer
	permission_classes = [permissions.IsAuthenticated]

	def get_permissions(self):
		if self.request.method in permissions.SAFE_METHODS:
			return super().get_permissions()
		return [permissions.IsAuthenticated(), IsOrganizationManagerOrOwner()]

	def get_object(self):
		organization = get_request_organization(self.request)
		if organization is None:
			raise permissions.PermissionDenied("Organization not found in request context")
		if self.request.user.is_superuser:
			return organization
		if not OrganizationMembership.objects.filter(user=self.request.user, organization=organization, is_active=True).exists():
			raise permissions.PermissionDenied("You do not belong to this organization")
		return organization


class OrganizationMembershipListView(generics.ListAPIView):
	serializer_class = OrganizationMembershipSerializer
	permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

	def get_queryset(self):
		return scope_queryset_by_organization(OrganizationMembership.objects.select_related("user", "organization"), self.request)


class InvitationCreateView(generics.CreateAPIView):
	serializer_class = InvitationCreateSerializer
	permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

	def perform_create(self, serializer):
		organization = get_request_organization(self.request)
		invitation = serializer.save(
			organization=organization,
			inviter=self.request.user,
			token=OrganizationInvitation.generate_token(),
		)
		if not invitation.expires_at:
			invitation.expires_at = OrganizationInvitation.default_expiry()
			invitation.save(update_fields=["expires_at", "updated_at"])

		invite_url = f"{self.request.build_absolute_uri('/')}accept-invitation/?token={invitation.token}"
		send_mail(
			subject=f"Invitation to join {organization.name}",
			message=f"You were invited to join {organization.name}. Accept invitation: {invite_url}",
			from_email=None,
			recipient_list=[invitation.email],
		)
		write_audit_log(
			action="organization.invitation.created",
			organization=organization,
			user=self.request.user,
			object_type="OrganizationInvitation",
			object_identifier=str(invitation.id),
			metadata={"email": invitation.email, "role": invitation.role},
		)


class InvitationAcceptView(APIView):
	permission_classes = [permissions.IsAuthenticated]

	def post(self, request, *args, **kwargs):
		serializer = InvitationAcceptSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)

		invitation = get_object_or_404(OrganizationInvitation, token=serializer.validated_data["token"])
		if invitation.email.lower() != request.user.email.lower():
			return Response({"detail": "Invitation email does not match your account"}, status=status.HTTP_403_FORBIDDEN)
		try:
			membership = accept_invitation(invitation=invitation, user=request.user)
		except ValueError as exc:
			return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

		return Response(OrganizationMembershipSerializer(membership).data)
