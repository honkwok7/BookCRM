from django.contrib import admin

from appointments.models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "specialist", "date", "time", "status", "created_at")
    list_filter = ("status", "date", "specialist")
    search_fields = ("user__username", "specialist__name", "specialist__profession")
