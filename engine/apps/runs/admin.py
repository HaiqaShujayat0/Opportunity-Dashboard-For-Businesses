from django.contrib import admin, messages
from django.http import HttpResponse

from apps.runs.exporters import generate_csv, generate_excel
from apps.runs.tasks import run_pipeline_async

from .models import Run, RunStage


def _single_selected_run(modeladmin, request, queryset):
    if queryset.count() != 1:
        modeladmin.message_user(
            request,
            "Select exactly one Run to download an export.",
            level=messages.ERROR,
        )
        return None
    return queryset.select_related("client").first()


@admin.action(description="Download Opportunities as CSV")
def download_opportunities_csv(modeladmin, request, queryset):
    """Download Option B: only the main actionable Opportunities table."""
    run = _single_selected_run(modeladmin, request, queryset)
    if run is None:
        return None
    try:
        content = generate_csv(run)
    except RuntimeError as exc:
        modeladmin.message_user(request, str(exc), level=messages.ERROR)
        return None
    return HttpResponse(
        content,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="run_{run.pk}_opportunities.csv"'
            )
        },
    )


@admin.action(description="Download current Run as XLSX (6 tabs)")
def download_run_xlsx(modeladmin, request, queryset):
    run = _single_selected_run(modeladmin, request, queryset)
    if run is None:
        return None
    try:
        content = generate_excel(run)
    except RuntimeError as exc:
        modeladmin.message_user(request, str(exc), level=messages.ERROR)
        return None
    return HttpResponse(
        content,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="run_{run.pk}_export.xlsx"'
        },
    )


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
    actions = [
        download_opportunities_csv,
        download_run_xlsx,
        run_pipeline_in_background,
    ]
@admin.register(RunStage)
class RunStageAdmin(admin.ModelAdmin):
    list_display = ("run", "name", "status", "records_in", "records_out")
    list_filter = ("status", "name")
