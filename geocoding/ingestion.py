import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ijson
from django.db import transaction

from .matching import input_addresses_disagree
from .models import FuelLocation, FuelObservation
from .parsing import parse_address
from .schemas import CanonicalAddress


def records_from_file(filename: str, file_format: str):
    """Stream per-provider exports. GeoParquet must be exported to GeoJSON upstream."""
    with Path(filename).open("rb") as stream:
        if file_format == "ndjson":
            for line in stream:
                if line.strip():
                    yield json.loads(line)
        else:
            prefixes = {"geojson": "features.item", "overpass": "elements.item", "json": "item"}
            yield from ijson.items(stream, prefixes[file_format], use_float=True)


@transaction.atomic
def import_fuel_csv(filename: str, source="opis") -> dict:
    filename = Path(filename)
    with filename.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "dataset_sha256": digest,
        "rows": 0,
        "locations_created": 0,
        "observations_created": 0,
        "conflicting_locations": 0,
    }
    with filename.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {
            "OPIS Truckstop ID",
            "Truckstop Name",
            "Address",
            "City",
            "State",
            "Retail Price",
        }
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(
                f"Missing required CSV columns: {sorted(required - set(reader.fieldnames or []))}"
            )
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(key) is None for key in required):
                raise ValueError(f"Row {row_number}: malformed CSV column count")
            if not row["OPIS Truckstop ID"].strip():
                raise ValueError(f"Row {row_number}: missing truckstop identity")
            canonical = parse_address(
                {
                    "name": row["Truckstop Name"],
                    "address": row["Address"],
                    "city": row["City"],
                    "state": row["State"],
                }
            )
            try:
                price = Decimal(row["Retail Price"]) if row["Retail Price"].strip() else None
                if price is not None and (not price.is_finite() or price < 0):
                    raise ValueError("Price must be finite and non-negative")
            except (ValueError, InvalidOperation) as exc:
                raise ValueError(f"Row {row_number}: invalid retail price") from exc
            location, created = FuelLocation.objects.get_or_create(
                source=source,
                external_id=row["OPIS Truckstop ID"].strip(),
                defaults={
                    "address": canonical.model_dump(mode="json"),
                    "aliases": [canonical.raw_name],
                },
            )
            report["locations_created"] += created
            if not created:
                old = location.address
                disagreement = input_addresses_disagree(
                    CanonicalAddress.model_validate(old), canonical
                )
                if disagreement and "conflicting_input_rows" not in location.warnings:
                    location.warnings.append("conflicting_input_rows")
                    location.resolution_status = "needs_review"
                    location.resolved_coordinate = None
                    report["conflicting_locations"] += 1
                if canonical.raw_name not in location.aliases:
                    location.aliases.append(canonical.raw_name)
                location.save()
            _, observation_created = FuelObservation.objects.get_or_create(
                dataset_sha256=digest,
                row_number=row_number,
                defaults={"location": location, "raw": row, "retail_price": price},
            )
            report["observations_created"] += observation_created
            report["rows"] += 1
    return report
