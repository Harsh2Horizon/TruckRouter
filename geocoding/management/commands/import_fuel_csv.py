import json

from django.core.management.base import BaseCommand, CommandError

from geocoding.ingestion import import_fuel_csv


class Command(BaseCommand):
    help = "Import the fuel CSV idempotently, preserving raw rows and prices"

    def add_arguments(self, parser):
        parser.add_argument("filename")

    def handle(self, *args, **options):
        try:
            report = import_fuel_csv(options["filename"])
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report))
