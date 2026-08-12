"""Stage 9: export a run to Google Sheets without losing human edits."""

from __future__ import annotations

import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.clients.models import Client
from apps.connectors.sheets import DriveSpreadsheetProvisioner, SheetsConnector
from apps.runs.exporters import OPPORTUNITY_COLUMNS, TAB_NAMES, build_export_tabs
from apps.runs.models import RunStage


def _spreadsheet_id(run, explicit_id=None, provisioner=None):
    """Resolve or provision the destination for every Sheets export entry point."""
    if explicit_id:
        return str(explicit_id).strip()
    if run.sheet_url:
        match = re.search(r"/spreadsheets/d/([^/]+)", run.sheet_url)
        if match:
            return match.group(1)
    configured = str(run.client.google_sheets_spreadsheet_id or "").strip()
    if configured:
        return configured
    if getattr(settings, "USE_DUMMY_SHEETS", True):
        return f"dummy-{run.client.slug}"

    provisioner = provisioner or DriveSpreadsheetProvisioner()
    # Serialize first-time provisioning per Client. This prevents two Celery
    # Runs for the same new client from creating two destination spreadsheets.
    with transaction.atomic():
        client = Client.objects.select_for_update().get(pk=run.client_id)
        configured = str(client.google_sheets_spreadsheet_id or "").strip()
        if not configured:
            configured = provisioner.provision(client.name)
            client.google_sheets_spreadsheet_id = configured
            client.save(update_fields=["google_sheets_spreadsheet_id"])

        snapshot = dict(run.settings_snapshot or {})
        if snapshot.get("google_sheets_spreadsheet_id") != configured:
            snapshot["google_sheets_spreadsheet_id"] = configured
            run.settings_snapshot = snapshot
            run.save(update_fields=["settings_snapshot"])
        run.client = client
        return configured


def run_stage_export(
    run,
    spreadsheet_id=None,
    connector=None,
    provisioner=None,
):
    """Export all opportunities for ``run`` and return tab/merge counts."""
    stage, _ = RunStage.objects.update_or_create(
        run=run,
        name="export",
        defaults={
            "status": "running",
            "records_in": 0,
            "records_out": 0,
            "started_at": timezone.now(),
            "finished_at": None,
            "error": "",
        },
    )
    try:
        connector = connector or SheetsConnector()
        sheet_id = _spreadsheet_id(run, spreadsheet_id, provisioner=provisioner)
        existing = connector.read_existing_sheet(sheet_id)
        existing_headers = connector.existing_headers or OPPORTUNITY_COLUMNS
        existing_archive = connector.read_tab(sheet_id, "Archived")
        existing_run_log = connector.read_tab(sheet_id, "Run log")
        connector.create_tabs(sheet_id, TAB_NAMES)

        tabs, summary = build_export_tabs(
            run,
            existing_rows=existing,
            existing_headers=existing_headers,
            existing_archive=existing_archive,
            existing_run_log=existing_run_log,
        )
        connector.write_batch(sheet_id, tabs)

        if not getattr(settings, "USE_DUMMY_SHEETS", True):
            run.sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            run.save(update_fields=["sheet_url"])

        summary = {"spreadsheet_id": sheet_id, **summary}
        stage.status = "complete"
        stage.records_in = summary["total_opportunities"]
        stage.records_out = (
            summary["opportunities"]
            + summary["ignored"]
            + summary["cannibalisation_rows"]
        )
        stage.finished_at = timezone.now()
        stage.error = ""
        stage.save(
            update_fields=[
                "status",
                "records_in",
                "records_out",
                "finished_at",
                "error",
            ]
        )
        return summary
    except Exception as exc:
        stage.status = "failed"
        stage.finished_at = timezone.now()
        stage.error = str(exc)
        stage.save(update_fields=["status", "finished_at", "error"])
        raise
