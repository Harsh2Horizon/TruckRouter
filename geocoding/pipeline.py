"""Import validation, batched source storage and deterministic matching orchestration."""

import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from django.db import transaction

from .acquisition import write_json
from .exports import (
    CSV_FIELDS,
    SOURCE_COMPARISON_FIELDS,
    comparison_row,
    enrich_input_csv,
    source_comparison_rows,
    write_analysis_exports,
)
from .ingestion import import_fuel_csv, records_from_file
from .models import FuelLocation, SourcePlace
from .profile_csv import profile_csv
from .providers import ADAPTERS
from .schemas import CanonicalAddress
from .scoring import ALGORITHM_VERSION
from .services import bulk_save_places, resolve

PROVIDERS = tuple(ADAPTERS)


def completed_report(filename):
    report = json.loads(Path(filename).read_text())
    if report.get("status") != "completed":
        raise ValueError(f"A completed report is required: {filename}")
    return report


def import_source(provider, manifest, output_dir, progress=print):
    """Upsert one NDJSON source in a transaction; audit rejected provider records."""
    if manifest.get("format") != "ndjson":
        raise ValueError("Acquisition must produce normalized NDJSON exports")
    imported, rejected, batch = 0, 0, []
    with (
        transaction.atomic(),
        (Path(output_dir) / f"{provider}-rejections.jsonl").open("w") as rejects,
    ):
        for record in records_from_file(manifest["filename"]):
            try:
                batch.append(ADAPTERS[provider](record, manifest["release"]))
            except (ValueError, KeyError, TypeError) as exc:
                rejected += 1
                rejects.write(json.dumps({"record_id": record.get("id"), "error": str(exc)}) + "\n")
            if len(batch) >= 500:
                imported += bulk_save_places(batch)
                batch.clear()
                if imported % 10000 == 0:
                    progress(f"Imported {provider}: {imported:,} records")
        imported += bulk_save_places(batch)
        if not imported:
            raise ValueError(f"{provider} returned zero usable records")
    return {
        **manifest,
        "status": "imported",
        "imported": imported,
        "rejected": rejected,
        "unique_stored_records": SourcePlace.objects.filter(
            provider=provider, release=manifest["release"]
        ).count(),
    }


def reuse_source(provider, manifest, previous, enrichment=None):
    if previous.get("status") != "imported" or any(
        previous.get(field) != manifest[field] for field in ("release", "filename")
    ):
        raise ValueError(f"Previous {provider} import does not match this acquisition")
    records = SourcePlace.objects.filter(provider=provider, release=manifest["release"])
    count = records.count()
    if not count or count != previous.get("unique_stored_records"):
        raise ValueError(f"Database record count does not match the {provider} import")
    if enrichment:
        expected = enrichment["after"][provider]
        if (
            records.filter(city_key="").count() != expected["indexed_city_missing"]
            or records.filter(state="").count() != expected["indexed_state_missing"]
        ):
            raise ValueError("The database does not match the locality enrichment audit")
    return {**previous, "import_reused": True}


def match_locations(locations, scope, report, output, checkpoint, progress):
    counts, coordinates, highest = Counter(), Counter(), Counter()
    statuses = {provider: Counter() for provider in PROVIDERS}
    comparison_rows, candidate_cache = {}, {}
    with (
        (output / "geocodes.csv").open("w", newline="") as csv_stream,
        (output / "geocodes.jsonl").open("w") as json_stream,
        (output / "per-source.jsonl").open("w") as source_stream,
        (output / "source-comparison.csv").open("w", newline="") as comparison_stream,
    ):
        writer = csv.DictWriter(csv_stream, fieldnames=CSV_FIELDS)
        source_writer = csv.DictWriter(comparison_stream, fieldnames=SOURCE_COMPARISON_FIELDS)
        writer.writeheader()
        source_writer.writeheader()
        for index, location in enumerate(locations.iterator(), start=1):
            query = CanonicalAddress.model_validate(location.address)
            run = resolve(query, location, source_scope=scope, candidate_key_cache=candidate_cache)
            result = run.result
            counts[run.status] += 1
            report["scoring"] = result["scoring"]
            row = comparison_row(location.external_id, query, result, run.pk)
            comparison_rows[location.external_id] = row
            writer.writerow(row)
            source_writer.writerows(source_comparison_rows(location.external_id, query, result))
            if result["highest_matching_source"]:
                highest[result["highest_matching_source"]] += 1
            json_stream.write(
                json.dumps({"external_id": location.external_id, "run_id": str(run.pk), **result})
                + "\n"
            )
            for provider, source in result["per_source"].items():
                statuses[provider][source["status"]] += 1
                coordinates[provider] += source["matched_coordinate"] is not None
                source_stream.write(
                    json.dumps(
                        {
                            "external_id": location.external_id,
                            "provider": provider,
                            "algorithm_version": ALGORITHM_VERSION,
                            **source,
                        }
                    )
                    + "\n"
                )
            if index % 100 == 0:
                report.update(geocodes=dict(counts), locations_processed=index)
                checkpoint()
                progress(f"Resolved {index:,} input locations: {dict(counts)}")
    report.update(
        geocodes=dict(counts),
        per_source={provider: dict(values) for provider, values in statuses.items()},
        source_candidate_coordinates=dict(coordinates),
        highest_matching_sources=dict(highest),
        locations_processed=sum(counts.values()),
        **write_analysis_exports(output, comparison_rows),
    )
    return comparison_rows


def run_pipeline(
    *,
    csv_file,
    output_dir,
    acquisition_report,
    reuse_import_report=None,
    enrichment_report=None,
    import_only=False,
    progress=print,
):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    acquisition = completed_report(acquisition_report)
    previous = completed_report(reuse_import_report) if reuse_import_report else None
    enrichment = completed_report(enrichment_report) if enrichment_report else None
    if enrichment and not previous:
        raise ValueError(
            "--enrichment-report requires reused imports; a fresh import replaces enrichment"
        )
    with Path(csv_file).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if acquisition.get("dataset_sha256") != digest:
        raise ValueError("The downloaded source scope belongs to a different input CSV")
    if previous and previous.get("input_import", {}).get("dataset_sha256") != digest:
        raise ValueError("The previous import used a different input CSV")
    for provider in PROVIDERS:
        source = acquisition.get("sources", {}).get(provider, {})
        if source.get("status") != "downloaded" or not Path(source["filename"]).is_file():
            raise ValueError(f"Acquisition is not complete for {provider}")
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "phase": "importing",
        "algorithm_version": ALGORITHM_VERSION,
        "sources": {},
        "geocodes": {},
        "acquisition_report": str(acquisition_report),
    }

    def checkpoint():
        write_json(output / "report.json", report)

    try:
        report["parser"] = profile_csv(csv_file, str(output))
        report["input_import"] = (
            {
                **previous["input_import"],
                "locations_created": 0,
                "observations_created": 0,
                "import_reused": True,
            }
            if previous
            else import_fuel_csv(csv_file)
        )
        if previous:
            report["reused_import_report"] = str(reuse_import_report)
        if enrichment:
            report["locality_enrichment"] = {
                "version": enrichment["version"],
                "report": str(enrichment_report),
                "changes": enrichment["changes"],
            }
        locations = (
            FuelLocation.objects.filter(observations__dataset_sha256=digest)
            .distinct()
            .order_by("pk")
        )
        if locations.count() != report["parser"]["unique_ids"]:
            raise ValueError("The input location index does not match the dataset")
        checkpoint()
        for provider in PROVIDERS:
            manifest = acquisition["sources"][provider]
            report["sources"][provider] = (
                reuse_source(
                    provider, manifest, previous.get("sources", {}).get(provider, {}), enrichment
                )
                if previous
                else import_source(provider, manifest, output, progress)
            )
            progress(
                f"{'Reused' if previous else 'Stored'} {provider}: "
                f"{report['sources'][provider]['unique_stored_records']:,} records"
            )
            checkpoint()
        if not import_only:
            scope = {provider: report["sources"][provider]["release"] for provider in PROVIDERS}
            report["phase"] = "matching"
            checkpoint()
            comparisons = match_locations(locations, scope, report, output, checkpoint, progress)
            report["enriched_input_rows"] = enrich_input_csv(
                csv_file, output / "fuel-prices-geocoded.csv", comparisons
            )
        report.update(status="completed", phase="imported" if import_only else "finished")
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        checkpoint()
    return report
