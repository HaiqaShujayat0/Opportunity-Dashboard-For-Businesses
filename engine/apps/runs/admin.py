from django.contrib import admin
from .models import Run, RunStage

@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "run_type", "status", "started_at", "total_cost_usd")
    list_filter = ("status", "run_type", "client")
    search_fields = ("client__name",)
    readonly_fields = ("created_at", "total_cost_usd", "started_at", "finished_at")

@admin.register(RunStage)
class RunStageAdmin(admin.ModelAdmin):
    list_display = ("run", "name", "status", "records_in", "records_out")
    list_filter = ("status", "name")
