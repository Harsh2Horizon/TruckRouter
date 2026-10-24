from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from ijson.common import JSONError

from geocoding.ingestion import records_from_file
from geocoding.providers import ADAPTERS
from geocoding.services import save_place


class Command(BaseCommand):
    help = "Import a provider export (strict transaction; malformed records roll back the file)"

    def add_arguments(self, parser):
        parser.add_argument("provider", choices=ADAPTERS)
        parser.add_argument("filename")
        parser.add_argument(
            "--format", choices=["geojson", "overpass", "json", "ndjson"], default="geojson"
        )
        parser.add_argument("--release", required=True)
        parser.add_argument(
            "--ref-is-store-id",
            action="store_true",
            help="ATP only: use only when this spider's ref is verified as a store ID",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["ref_is_store_id"] and options["provider"] != "alltheplaces":
            raise CommandError("--ref-is-store-id only applies to AllThePlaces")
        count = 0
        try:
            for record in records_from_file(options["filename"], options["format"]):
                extra = {"ref_is_store_id": True} if options["ref_is_store_id"] else {}
                save_place(ADAPTERS[options["provider"]](record, options["release"], **extra))
                count += 1
        except (ValueError, KeyError, TypeError, OSError, JSONError) as exc:
            raise CommandError(f"Record {count + 1}: {exc}") from exc
        if count == 0:
            raise CommandError("No records found; check --format and the export structure")
        self.stdout.write(f"Imported {count} {options['provider']} records")
