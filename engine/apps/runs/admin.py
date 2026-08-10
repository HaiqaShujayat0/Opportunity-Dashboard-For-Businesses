from django.contrib import admin
import csv
from django.http import HttpResponse
from .models import Run, RunStage
from apps.exports.builder import EXPORT_COLUMNS, build_opportunity_rows

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

@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "run_type", "status", "started_at", "total_cost_usd")
    list_filter = ("status", "run_type", "client")
    search_fields = ("client__name",)
    readonly_fields = ("created_at", "total_cost_usd", "started_at", "finished_at")
    actions = [export_to_csv]
@admin.register(RunStage)
class RunStageAdmin(admin.ModelAdmin):
    list_display = ("run", "name", "status", "records_in", "records_out")
    list_filter = ("status", "name")
