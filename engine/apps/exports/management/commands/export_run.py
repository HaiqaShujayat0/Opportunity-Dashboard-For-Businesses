"""Export one pipeline run to CSV or Google Sheets."""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.exports.builder import EXPORT_COLUMNS, build_opportunity_rows
from apps.runs.models import Run
from apps.runs.exporters import generate_excel
from apps.runs.stages.stage_9_export import run_stage_export


class Command(BaseCommand):
    help = "Export a pipeline run's opportunities to CSV or Google Sheets"

    def add_arguments(self, parser):
        parser.add_argument("--run-id", type=int, required=True)
        parser.add_argument(
            "--format",
            choices=["csv", "xlsx", "sheets"],
            default="csv",
            help="Export format: csv, xlsx, or sheets.",
        )
        parser.add_argument(
            "--output",
            type=str,
            help="Output path. Defaults to run_<id>_opportunities.csv in the current directory.",
        )
        parser.add_argument(
            "--spreadsheet-id",
            type=str,
            help="Override the configured Google spreadsheet ID for a sheets export.",
        )

    def handle(self, *args, **options):
        run_id = options["run_id"]
        try:
            run = Run.objects.select_related("client").get(pk=run_id)
        except Run.DoesNotExist as exc:
            raise CommandError(f"Run #{run_id} does not exist.") from exc

        if options["format"] == "sheets":
            summary = run_stage_export(
                run, spreadsheet_id=options.get("spreadsheet_id")
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Exported Run #{run_id} to Google Sheets spreadsheet "
                    f"{summary['spreadsheet_id']} "
                    f"({summary['opportunities']} actionable rows, "
                    f"{summary['archived']} archived)."
                )
            )
            return

        if options["format"] == "xlsx":
            output_path = Path(
                options["output"] or f"run_{run_id}_export.xlsx"
            ).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                output_path.write_bytes(generate_excel(run))
            except RuntimeError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(
                self.style.SUCCESS(
                    f"Exported Run #{run_id} to XLSX at {output_path}."
                )
            )
            return

        rows = build_opportunity_rows(run)
        if not rows:
            raise CommandError(
                f"Run #{run_id} has no opportunities to export. Run DECIDE and SCORE first."
            )

        output_path = Path(
            options["output"] or f"run_{run_id}_opportunities.csv"
        ).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # utf-8-sig lets Excel open non-ASCII keywords without mojibake.
        with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {len(rows)} opportunities from Run #{run_id} to {output_path}"
            )
        )
