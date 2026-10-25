"""CLI for per-observation geographic completion and export."""

import json

from django.core.management.base import BaseCommand, CommandError

from geocoding.completion import complete_coverage


class Command(BaseCommand):
    help = "Complete every original row with a POI or explicitly approximate geographic point"

    def add_arguments(self, parser):
        parser.add_argument("--csv", default="fuel-prices-for-be-assessment.csv")
        parser.add_argument("--base-run", default="output/geocoding")
        parser.add_argument("--output-dir", default="output/coverage")
        parser.add_argument("--reference-dir", default="data/reference/coverage")
        parser.add_argument("--fetch-census", action="store_true")

    def handle(self, *args, **options):
        try:
            report = complete_coverage(
                csv_file=options["csv"],
                base_run=options["base_run"],
                output_dir=options["output_dir"],
                reference_dir=options["reference_dir"],
                fetch_census=options["fetch_census"],
                progress=self.stdout.write,
            )
        except (ValueError, OSError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "status",
                        "rows_with_coordinates",
                        "rows_without_coordinates",
                        "unique_stop_methods",
                    )
                },
                indent=2,
            )
        )
