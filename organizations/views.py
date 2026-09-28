from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.permissions import HasCapability
from organizations.models import OrganizationInvitation, OrganizationMembership
from organizations.selectors import scope_queryset_by_organization
from organizations.serializers import (
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    OrganizationMembershipSerializer,
    OrganizationSerializer,
)
from organizations.services import accept_invitation, create_invitation
from organizations.tenancy import resolve_tenant

User = get_user_model()


class CurrentOrganizationView(generics.RetrieveUpdateAPIView):
    serializer_class = OrganizationSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="organization.view", write="organization.manage"),
    ]

    def get_object(self):
        # The permission class guarantees a membership-backed tenant context.
        return resolve_tenant(self.request).organization

    def perform_update(self, serializer):
        with transaction.atomic():
            before = snapshot(serializer.instance)
            organization = serializer.save()
            changes = diff_snapshots(before, snapshot(organization))
            if changes:
                record_audit(
                    AuditAction.ORGANIZATION_UPDATED,
                    organization=organization,
                    actor=self.request.user,
                    target=organization,
                    changes=changes,
                    request=self.request,
                )


class OrganizationMembershipListView(generics.ListAPIView):
    serializer_class = OrganizationMembershipSerializer
    permission_classes = [permissions.IsAuthenticated, HasCapability(read="members.view")]

    def get_queryset(self):
        return scope_queryset_by_organization(
            OrganizationMembership.objects.select_related("user", "organization"), self.request
        )


class InvitationCreateView(generics.CreateAPIView):
    serializer_class = InvitationCreateSerializer
    permission_classes = [permissions.IsAuthenticated, HasCapability(write="members.invite")]

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "tenant": resolve_tenant(self.request)}

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = create_invitation(
            organization=resolve_tenant(request).organization,
            inviter=request.user,
            email=serializer.validated_data["email"],
            role=serializer.validated_data["role"],
            expires_at=serializer.validated_data.get("expires_at"),
            accept_base_url=request.build_absolute_uri("/"),
        )
        return Response(self.get_serializer(invitation).data, status=status.HTTP_201_CREATED)


class InvitationAcceptView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invitation = get_object_or_404(
            OrganizationInvitation, token=serializer.validated_data["token"]
        )
        if invitation.email.lower() != request.user.email.lower():
            return Response(
                {"detail": "Invitation email does not match your account"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            membership = accept_invitation(invitation=invitation, user=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrganizationMembershipSerializer(membership).data)
