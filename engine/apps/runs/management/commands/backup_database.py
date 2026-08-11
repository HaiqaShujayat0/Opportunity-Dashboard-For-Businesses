"""Create a portable UTF-8 Django JSON backup of the configured database."""

from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Export the configured database as UTF-8 JSON with natural keys"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="backup.json",
            help="Destination JSON file (default: backup.json)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite an existing destination file",
        )

    def handle(self, *args, **options):
        output = Path(options["output"]).expanduser().resolve()
        if output.exists() and not options["force"]:
            raise CommandError(
                f"Backup already exists: {output}. Choose another path or pass --force."
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("w", encoding="utf-8", newline="") as stream:
                call_command(
                    "dumpdata",
                    "--natural-foreign",
                    "--natural-primary",
                    stdout=stream,
                    verbosity=0,
                )
        except Exception:
            output.unlink(missing_ok=True)
            raise
        self.stdout.write(self.style.SUCCESS(
            f"Database backup written to {output} ({output.stat().st_size} bytes)"
        ))
