"""Celery entry points for the existing synchronous pipeline orchestrator."""

from celery.exceptions import Ignore
from django.core.management import call_command
from django.utils import timezone

from config.celery import app

from apps.runs.models import Run


def _normalise_stage(stages):
    """Map the task API onto the command's existing single-stage-or-all API."""
    if stages is None:
        return "all"
    if isinstance(stages, str):
        return stages
    selected = list(stages)
    if len(selected) != 1:
        raise ValueError(
            "The current pipeline orchestrator accepts one stage or all stages; "
            "pass stages=None, a stage name, or a one-item stage list."
        )
    return selected[0]


@app.task
def run_pipeline_async(run_id, stages=None):
    """Execute the unchanged management-command pipeline in a Celery worker."""
    try:
        run = Run.objects.select_related("client").get(pk=run_id)
    except Run.DoesNotExist as exc:
        # There is no database row whose status can be updated in this case.
        raise Ignore(f"Run #{run_id} does not exist.") from exc

    try:
        stage = _normalise_stage(stages)
        call_command(
            "run_pipeline",
            run_id=run.pk,
            stage=stage,
            verbosity=0,
        )
        run.refresh_from_db()

        # The orchestrator normally sets complete/partial. This fallback keeps
        # the task contract correct if a future command path returns cleanly
        # without finalising the Run, while never erasing partial semantics.
        if run.status not in {"complete", "partial"}:
            run.status = "complete"
            run.finished_at = timezone.now()
            run.error = ""
            run.save(update_fields=["status", "finished_at", "error"])

        return {
            "run_id": run.pk,
            "status": run.status,
            "stage": stage,
        }
    except Exception as exc:
        run.status = "failed"
        run.finished_at = timezone.now()
        run.error = str(exc)
        run.save(update_fields=["status", "finished_at", "error"])
        raise
