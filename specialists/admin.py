from django.contrib import admin

from specialists.models import Specialist, WorkingHour


@admin.register(Specialist)
class SpecialistAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "profession", "created_at")
    list_filter = ("profession", "created_at")
    search_fields = ("name", "profession", "description")


@admin.register(WorkingHour)
class WorkingHourAdmin(admin.ModelAdmin):
    list_display = ("id", "specialist", "day", "start_time", "end_time")
    list_filter = ("day", "specialist")
    search_fields = ("specialist__name", "specialist__profession")
