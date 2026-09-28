from datetime import timedelta

from django.utils.dateparse import parse_datetime
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from bookings.models import Booking, Customer, WaitlistEntry
from bookings.serializers import (
	BookingCancelSerializer,
	BookingCreateSerializer,
	BookingSerializer,
	CustomerSerializer,
	WaitlistEntrySerializer,
)
from bookings.services import create_booking
from core.permissions import IsOrganizationManagerOrOwner
from organizations.selectors import get_request_organization, scope_queryset_by_organization, user_has_org_role
from services.models import Service
from staff.models import StaffProfile


class BookingViewSet(viewsets.ModelViewSet):
	serializer_class = BookingSerializer
	permission_classes = [permissions.IsAuthenticatedOrReadOnly]
	filterset_fields = ("status", "service", "staff")
	ordering_fields = ("start_datetime", "created_at")

	def get_queryset(self):
		queryset = Booking.objects.select_related("organization", "service", "staff", "customer")
		queryset = scope_queryset_by_organization(queryset, self.request)
		if self.request.user.is_superuser:
			return queryset
		organization = get_request_organization(self.request)
		if user_has_org_role(self.request.user, organization, ["owner", "manager", "staff"]):
			return queryset
		return queryset.filter(customer_email=self.request.user.email)

	def create(self, request, *args, **kwargs):
		serializer = BookingCreateSerializer(data=request.data, context={"request": request})
		serializer.is_valid(raise_exception=True)
		booking = serializer.save()
		return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)

	@action(detail=True, methods=["post"])
	def cancel(self, request, pk=None):
		booking = self.get_object()
		serializer = BookingCancelSerializer(data=request.data, context={"booking": booking, "request": request})
		serializer.is_valid(raise_exception=True)
		serializer.save()
		return Response(BookingSerializer(booking).data)

	@action(detail=True, methods=["post"])
	def reschedule(self, request, pk=None):
		booking = self.get_object()
		new_start_raw = request.data.get("start_datetime")
		new_start = parse_datetime(new_start_raw) if isinstance(new_start_raw, str) else new_start_raw
		if not new_start:
			return Response({"detail": "start_datetime is required"}, status=400)
		service = booking.service
		old_booking = booking
		old_booking.status = Booking.Status.CANCELLED
		old_booking.save(update_fields=["status", "updated_at"])

		new_booking = create_booking(
			organization=booking.organization,
			service=service,
			staff_profile=booking.staff,
			customer_name=booking.customer_name,
			customer_email=booking.customer_email,
			customer_phone=booking.customer_phone,
			start_datetime=new_start,
			customer_timezone=booking.customer_timezone,
			customer_notes=booking.customer_notes,
			actor=request.user if request.user.is_authenticated else None,
			customer_user=request.user if request.user.is_authenticated else None,
		)
		new_booking.rescheduled_from = old_booking
		new_booking.save(update_fields=["rescheduled_from", "updated_at"])
		return Response(BookingSerializer(new_booking).data)

	@action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated, IsOrganizationManagerOrOwner])
	def update_status(self, request, pk=None):
		booking = self.get_object()
		status_value = request.data.get("status")
		if status_value not in Booking.Status.values:
			return Response({"detail": "Invalid status"}, status=400)
		booking.status = status_value
		booking.save(update_fields=["status", "updated_at"])
		return Response(BookingSerializer(booking).data)


class CustomerViewSet(viewsets.ModelViewSet):
	serializer_class = CustomerSerializer
	permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]
	search_fields = ("name", "email", "phone")

	def get_queryset(self):
		queryset = Customer.objects.select_related("organization", "user")
		return scope_queryset_by_organization(queryset, self.request)


class WaitlistViewSet(viewsets.ModelViewSet):
	serializer_class = WaitlistEntrySerializer
	# Waitlist entries hold customer PII. Public/portal joining arrives with the waitlist service (M4.7).
	permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

	def get_queryset(self):
		queryset = WaitlistEntry.objects.select_related("organization", "service", "preferred_staff")
		return scope_queryset_by_organization(queryset, self.request)
