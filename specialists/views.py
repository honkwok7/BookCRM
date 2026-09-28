from datetime import date, timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import generics, permissions, status
from rest_framework import serializers as rf_serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrAdmin
from appointments.models import Appointment
from appointments.scheduling import scheduling_context, scheduling_error
from appointments.serializers import AppointmentSerializer
from specialists.models import Specialist, WorkingHour
from specialists.permissions import IsAdminOrLinkedSpecialist
from specialists.serializers import (
    SpecialistSerializer,
    WorkingHourSerializer,
    WorkingHourWriteSerializer,
)


class SpecialistListView(generics.ListCreateAPIView):
    queryset = Specialist.objects.all()
    serializer_class = SpecialistSerializer
    search_fields = ["name", "profession"]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsOwnerOrAdmin()]
        return [permissions.AllowAny()]


class SpecialistDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Specialist.objects.all()
    serializer_class = SpecialistSerializer

    def get_permissions(self):
        if self.request.method in ["PUT", "PATCH", "DELETE"]:
            return [IsOwnerOrAdmin()]
        return [permissions.AllowAny()]


class SpecialistWorkingHoursView(generics.ListCreateAPIView):
    """GET: public working hours. POST: owner/admin creation."""

    def get_serializer_class(self):
        if self.request.method == "POST":
            return WorkingHourWriteSerializer
        return WorkingHourSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsOwnerOrAdmin()]
        return [permissions.AllowAny()]

    def get_queryset(self):
        return WorkingHour.objects.filter(specialist_id=self.kwargs["pk"])

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.method == "POST":
            ctx["specialist"] = get_object_or_404(Specialist, pk=self.kwargs["pk"])
        return ctx

    def perform_create(self, serializer):
        specialist = get_object_or_404(Specialist, pk=self.kwargs["pk"])
        serializer.save(specialist=specialist)


class SpecialistWorkingHourDeleteView(generics.DestroyAPIView):
    permission_classes = [IsOwnerOrAdmin]

    def get_object(self):
        # Ensures the working hour belongs to the specialist in the URL.
        return get_object_or_404(
            WorkingHour,
            pk=self.kwargs["working_hour_id"],
            specialist_id=self.kwargs["pk"],
        )


@extend_schema(
    parameters=[
        OpenApiParameter(
            name="date",
            description="Date to check availability for (YYYY-MM-DD).",
            required=True,
            type=OpenApiTypes.DATE,
            location=OpenApiParameter.QUERY,
        ),
    ],
    responses={
        200: inline_serializer(
            name="AvailableSlotsResponse",
            fields={
                "specialist": rf_serializers.IntegerField(),
                "date": rf_serializers.DateField(),
                "slot_duration": rf_serializers.IntegerField(),
                "slots": rf_serializers.ListField(child=rf_serializers.CharField()),
            },
        ),
        400: OpenApiResponse(description="Missing or invalid date, or date is in the past."),
        404: OpenApiResponse(description="Specialist not found."),
    },
)
class SpecialistAvailableSlotsView(APIView):
    """
    Returns available booking slots for a specialist on the given date.
    Slots are generated from the specialist's working hours minus existing active
    appointments, using the specialist's slot_duration (in minutes).
    Past dates return 400 (consistent with appointment booking rejecting past dates).
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        specialist = get_object_or_404(Specialist, pk=pk)

        date_str = request.query_params.get("date")
        if not date_str:
            return Response(
                {"detail": "date query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            requested_date = date.fromisoformat(date_str)
        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if requested_date < timezone.localdate():
            return Response(
                {"detail": "Cannot retrieve available slots for a past date."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        working, occupied = scheduling_context(specialist, requested_date)
        slot_duration = specialist.slot_duration
        delta = timedelta(minutes=slot_duration)
        all_slots = set()

        for start, end in working:
            current = start
            while slot_duration > 0 and delta <= end - current:
                all_slots.add(current.time())
                current += delta

        available = sorted(
            time
            for time in all_slots
            if scheduling_error(
                requested_date,
                time,
                slot_duration,
                slot_duration,
                working,
                occupied,
            )
            is None
        )

        return Response(
            {
                "specialist": specialist.id,
                "date": str(requested_date),
                "slot_duration": slot_duration,
                "slots": [t.strftime("%H:%M") for t in available],
            }
        )


class SpecialistAppointmentsView(generics.ListAPIView):
    """
    GET /api/specialists/<pk>/appointments/
    Owner/admin roles can view any specialist's appointments.
    Specialist role requires a matching specialist_profile.
    """

    serializer_class = AppointmentSerializer
    permission_classes = [IsAdminOrLinkedSpecialist]

    def get_queryset(self):
        specialist = get_object_or_404(Specialist, pk=self.kwargs["pk"])
        return Appointment.objects.filter(specialist=specialist).select_related(
            "user", "specialist"
        )
