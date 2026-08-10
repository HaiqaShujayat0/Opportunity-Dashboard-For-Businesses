"""Export one pipeline run to a flat CSV file."""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.exports.builder import EXPORT_COLUMNS, build_opportunity_rows
from apps.runs.models import Run


class Command(BaseCommand):
    help = "Export a pipeline run's opportunities to CSV"

    def add_arguments(self, parser):
        parser.add_argument("--run-id", type=int, required=True)
        parser.add_argument(
            "--format",
            choices=["csv"],
            default="csv",
            help="Export format. CSV is currently supported.",
        )
        parser.add_argument(
            "--output",
            type=str,
            help="Output path. Defaults to run_<id>_opportunities.csv in the current directory.",
        )

    def handle(self, *args, **options):
        run_id = options["run_id"]
        try:
            run = Run.objects.select_related("client").get(pk=run_id)
        except Run.DoesNotExist as exc:
            raise CommandError(f"Run #{run_id} does not exist.") from exc

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
