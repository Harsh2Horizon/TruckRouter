"""CLI for the supported import/matching workflow."""

import json

from django.core.management.base import BaseCommand, CommandError

from geocoding.pipeline import run_pipeline


class Command(BaseCommand):
    help = "Import or reuse the three acquired sources, then match and export the input stops"

    def add_arguments(self, parser):
        parser.add_argument("--csv", default="fuel-prices-for-be-assessment.csv")
        parser.add_argument("--output-dir", default="output/geocoding")
        parser.add_argument("--acquisition-report", default="output/acquisition/report.json")
        parser.add_argument("--reuse-import-report")
        parser.add_argument("--enrichment-report")
        parser.add_argument("--import-only", action="store_true")

    def handle(self, *args, **options):
        try:
            report = run_pipeline(
                csv_file=options["csv"],
                output_dir=options["output_dir"],
                acquisition_report=options["acquisition_report"],
                reuse_import_report=options["reuse_import_report"],
                enrichment_report=options["enrichment_report"],
                import_only=options["import_only"],
                progress=self.stdout.write,
            )
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({"status": report["status"], "geocodes": report["geocodes"]}))
