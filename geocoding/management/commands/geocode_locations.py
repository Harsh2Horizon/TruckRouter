import json

import httpx
from django.core.management.base import BaseCommand, CommandError

from geocoding.live import NominatimClient
from geocoding.models import FuelLocation
from geocoding.schemas import CanonicalAddress
from geocoding.services import resolve


class Command(BaseCommand):
    help = "Resolve imported input locations against local source places"

    def add_arguments(self, parser):
        parser.add_argument("--external-id")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--fetch-nominatim", action="store_true")

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive")
        locations = FuelLocation.objects.order_by("pk")
        if options["external_id"]:
            locations = locations.filter(external_id=options["external_id"])
        client = None
        try:
            if options["fetch_nominatim"]:
                client = NominatimClient()
            counts = {}
            for location in locations[: options["limit"]]:
                query = CanonicalAddress.model_validate(location.address)
                if client:
                    client.import_search(query)
                run = resolve(query, location)
                counts[run.status] = counts.get(run.status, 0) + 1
                self.stdout.write(
                    json.dumps(
                        {
                            "external_id": location.external_id,
                            "run_id": str(run.pk),
                            "status": run.status,
                            "score": run.result["score"],
                        }
                    )
                )
            self.stdout.write(json.dumps({"counts": counts}))
        except (ValueError, httpx.HTTPError) as exc:
            raise CommandError(str(exc)) from exc
        finally:
            if client:
                client.close()
