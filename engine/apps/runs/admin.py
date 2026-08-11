from django.contrib import admin
import csv
from django.http import HttpResponse
from .models import Run, RunStage
from apps.exports.builder import EXPORT_COLUMNS, build_opportunity_rows
from apps.runs.tasks import run_pipeline_async

@admin.action(description="Export Opportunities to CSV")
def export_to_csv(modeladmin, request, queryset):
    # Process the first selected run
    run = queryset.first()
    if not run:
        return
        
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="run_{run.pk}_opportunities.csv"'
    
    writer = csv.DictWriter(response, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    
    rows = build_opportunity_rows(run)
    for row in rows:
        writer.writerow(row)
        
    return response


@admin.action(description="Run Pipeline in Background")
def run_pipeline_in_background(modeladmin, request, queryset):
    queued = 0
    for run in queryset:
        run_pipeline_async.delay(run.id)
        queued += 1
    modeladmin.message_user(
        request,
        f"Queued {queued} pipeline run{'s' if queued != 1 else ''} in the background.",
    )

@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "run_type", "status", "started_at", "total_cost_usd")
    list_filter = ("status", "run_type", "client")
    search_fields = ("client__name",)
    readonly_fields = ("created_at", "total_cost_usd", "started_at", "finished_at")
    actions = [export_to_csv, run_pipeline_in_background]
@admin.register(RunStage)
class RunStageAdmin(admin.ModelAdmin):
    list_display = ("run", "name", "status", "records_in", "records_out")
    list_filter = ("status", "name")
