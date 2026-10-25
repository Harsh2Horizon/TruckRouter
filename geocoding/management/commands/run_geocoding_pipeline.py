import csv
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from geocoding.downloads import (
    NORTH_AMERICA_BBOX,
    DownloadClient,
    fetch_alltheplaces,
    fetch_osm,
    fetch_overture,
    parse_bbox,
)
from geocoding.exports import (
    CSV_FIELDS,
    SOURCE_COMPARISON_FIELDS,
    comparison_row,
    enrich_input_csv,
    source_comparison_rows,
    write_analysis_exports,
)
from geocoding.ingestion import import_fuel_csv, records_from_file
from geocoding.matching import ALGORITHM_VERSION
from geocoding.models import FuelLocation, SourcePlace
from geocoding.profile_csv import profile_csv
from geocoding.providers import ADAPTERS
from geocoding.schemas import CanonicalAddress
from geocoding.services import bulk_save_places, resolve


class Command(BaseCommand):
    help = (
        "Download all target sources, import candidates, resolve input locations and export results"
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", default="fuel-prices-for-be-assessment.csv")
        parser.add_argument("--output-dir", default="output/geocoding")
        parser.add_argument("--data-dir", default="data/sources")
        parser.add_argument(
            "--acquisition-report", help="Use the completed download_geocoding_sources report"
        )
        parser.add_argument(
            "--reuse-import-report",
            help="Reuse database imports verified by a previous pipeline report",
        )
        parser.add_argument(
            "--enrichment-report",
            help="Completed locality-enrichment audit for the indexed records",
        )
        parser.add_argument(
            "--atp-spiders-file", help="JSON array of explicitly selected ATP spider names"
        )
        parser.add_argument(
            "--overture-bbox",
            type=parse_bbox,
            default=NORTH_AMERICA_BBOX,
            metavar="W,S,E,N",
            help="Spatial scope for Parquet file and row-group pruning",
        )
        parser.add_argument(
            "--sources",
            nargs="+",
            choices=["alltheplaces", "overture", "osm"],
            default=["alltheplaces", "overture", "osm"],
        )

    def handle(self, *args, **options):
        output = Path(options["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        report = {
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "sources": {},
            "geocodes": {},
            "algorithm_version": ALGORITHM_VERSION,
        }
        prior_import = None
        enrichment = None
        if options["enrichment_report"]:
            enrichment = json.loads(Path(options["enrichment_report"]).read_text())
            if enrichment.get("status") != "completed":
                raise CommandError("The enrichment report must describe applied updates")
            report["locality_enrichment"] = {
                "version": enrichment["version"],
                "report": options["enrichment_report"],
                "changes": enrichment["changes"],
            }
        if options["reuse_import_report"]:
            if not options["acquisition_report"]:
                raise CommandError("--reuse-import-report requires --acquisition-report")
            prior_import = json.loads(Path(options["reuse_import_report"]).read_text())
            if prior_import.get("status") != "completed":
                raise CommandError("The import report must describe a completed run")
            report["reused_import_report"] = options["reuse_import_report"]

        def checkpoint():
            temporary = output / "report.json.part"
            temporary.write_text(json.dumps(report, indent=2) + "\n")
            temporary.replace(output / "report.json")

        report["parser"] = profile_csv(options["csv"], str(output))
        if prior_import is not None:
            digest = hashlib.sha256(Path(options["csv"]).read_bytes()).hexdigest()
            if prior_import.get("input_import", {}).get("dataset_sha256") != digest:
                raise CommandError("The previous import used a different input CSV")
            report["input_import"] = {
                **prior_import["input_import"],
                "locations_created": 0,
                "observations_created": 0,
                "import_reused": True,
            }
        else:
            report["input_import"] = import_fuel_csv(options["csv"])
        checkpoint()
        locations = (
            FuelLocation.objects.filter(
                observations__dataset_sha256=report["input_import"]["dataset_sha256"]
            )
            .distinct()
            .order_by("pk")
        )
        if prior_import is not None and locations.count() != report["parser"]["unique_ids"]:
            raise CommandError("The input location index does not match the reused dataset")
        brands = {location.address.get("brand", "") for location in locations}
        brands.discard("")
        spiders = None
        if options["atp_spiders_file"]:
            spiders = json.loads(Path(options["atp_spiders_file"]).read_text())
            if not isinstance(spiders, list) or not all(isinstance(s, str) for s in spiders):
                raise CommandError("ATP spiders file must contain a JSON array of names")
        client = DownloadClient(
            options["data_dir"], os.getenv("GEOCODER_USER_AGENT", "truckroute/0.1 local research")
        )
        loaders = {
            "alltheplaces": lambda: fetch_alltheplaces(
                client, brands, self.stdout.write, spiders=spiders
            ),
            "overture": lambda: fetch_overture(
                client, brands, self.stdout.write, bbox=options["overture_bbox"]
            ),
            "osm": lambda: fetch_osm(
                client,
                os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
                self.stdout.write,
            ),
        }
        if options["acquisition_report"]:
            acquisition = json.loads(Path(options["acquisition_report"]).read_text())
            expected = acquisition.get("dataset_sha256")
            if (
                expected
                and hashlib.sha256(Path(options["csv"]).read_bytes()).hexdigest() != expected
            ):
                raise CommandError("The downloaded source scope belongs to a different input CSV")
            for source in options["sources"]:
                entry = acquisition.get("sources", {}).get(source, {})
                if entry.get("status") != "downloaded":
                    raise CommandError(f"Acquisition is not complete for {source}")
                if not Path(entry["filename"]).is_file():
                    raise CommandError(f"Downloaded file is missing for {source}")
                loaders[source] = lambda entry=entry: entry
            report["acquisition_report"] = options["acquisition_report"]
        for source in options["sources"]:
            report["sources"][source] = {"status": "fetching"}
            checkpoint()
            try:
                manifest = loaders[source]()
                if prior_import is not None:
                    previous = prior_import.get("sources", {}).get(source, {})
                    if (
                        previous.get("status") != "imported"
                        or previous.get("release") != manifest["release"]
                        or previous.get("filename") != manifest["filename"]
                    ):
                        raise CommandError(
                            f"Previous {source} import does not match this acquisition"
                        )
                    unique_count = SourcePlace.objects.filter(
                        provider=source, release=manifest["release"]
                    ).count()
                    if not unique_count or unique_count != previous.get("unique_stored_records"):
                        raise CommandError(
                            f"Database record count does not match the {source} import"
                        )
                    if enrichment:
                        records = SourcePlace.objects.filter(
                            provider=source, release=manifest["release"]
                        )
                        expected = enrichment["after"][source]
                        if (
                            records.filter(city_key="").count() != expected["indexed_city_missing"]
                            or records.filter(state="").count() != expected["indexed_state_missing"]
                        ):
                            raise CommandError(
                                "The database does not match the locality enrichment audit"
                            )
                    report["sources"][source] = {
                        **manifest,
                        "status": "imported",
                        "import_reused": True,
                        "imported": previous["imported"],
                        "rejected": previous["rejected"],
                        "unique_stored_records": unique_count,
                    }
                    self.stdout.write(f"Reusing {source}: {unique_count:,} database records")
                    checkpoint()
                    continue
                files = manifest.get("files") or [{"filename": manifest["filename"]}]
                imported, rejected = 0, 0
                batch = []
                with (
                    transaction.atomic(),
                    (output / f"{source}-rejections.jsonl").open("w") as rejects,
                ):
                    for export in files:
                        for record in records_from_file(export["filename"], manifest["format"]):
                            if source == "osm" and export.get("country"):
                                record = {
                                    **record,
                                    "query_context": {
                                        **record.get("query_context", {}),
                                        "country": export["country"],
                                    },
                                }
                            try:
                                batch.append(ADAPTERS[source](record, manifest["release"]))
                            except (ValueError, KeyError, TypeError) as exc:
                                rejected += 1
                                rejects.write(
                                    json.dumps({"record_id": record.get("id"), "error": str(exc)})
                                    + "\n"
                                )
                            if len(batch) >= 500:
                                imported += bulk_save_places(batch)
                                batch.clear()
                                if imported % 10000 == 0:
                                    self.stdout.write(f"Imported {source}: {imported:,} records")
                                    self.stdout.flush()
                    imported += bulk_save_places(batch)
                    if imported == 0:
                        raise ValueError("Source returned zero usable records")
                report["sources"][source] = {
                    **manifest,
                    "status": "imported",
                    "imported": imported,
                    "rejected": rejected,
                    "unique_stored_records": SourcePlace.objects.filter(
                        provider=source, release=manifest["release"]
                    ).count(),
                }
            except Exception as exc:
                report["sources"][source].update(status="failed", error=str(exc))
                self.stderr.write(f"{source} failed: {exc}")
            checkpoint()

        scope = {
            source: entry["release"]
            for source, entry in report["sources"].items()
            if entry["status"] == "imported"
        }
        if not scope:
            report.update(status="failed", finished_at=datetime.now(UTC).isoformat())
            checkpoint()
            raise CommandError(f"No source acquisition succeeded; see {output}/report.json")
        acquisition_incomplete = len(scope) != len(options["sources"])
        report["phase"] = "matching"
        checkpoint()
        counts, per_source = Counter(), {source: Counter() for source in options["sources"]}
        source_coordinates = Counter()
        highest_sources = Counter()
        comparison_rows = {}
        candidate_key_cache = {}
        with (
            (output / "geocodes.csv").open("w", newline="") as csv_stream,
            (output / "geocodes.jsonl").open("w") as json_stream,
            (output / "per-source.jsonl").open("w") as source_stream,
            (output / "source-comparison.csv").open("w", newline="") as comparison_stream,
        ):
            writer = csv.DictWriter(
                csv_stream,
                fieldnames=CSV_FIELDS,
            )
            writer.writeheader()
            source_writer = csv.DictWriter(comparison_stream, fieldnames=SOURCE_COMPARISON_FIELDS)
            source_writer.writeheader()
            for index, location in enumerate(locations.iterator(), start=1):
                query = CanonicalAddress.model_validate(location.address)
                run = resolve(
                    query,
                    location,
                    source_scope=scope,
                    acquisition_incomplete=acquisition_incomplete,
                    candidate_key_cache=candidate_key_cache,
                )
                counts[run.status] += 1
                report["scoring"] = run.result["scoring"]
                row = comparison_row(location.external_id, query, run.result, run.pk)
                writer.writerow(row)
                comparison_rows[location.external_id] = row
                source_writer.writerows(
                    source_comparison_rows(location.external_id, query, run.result)
                )
                if run.result["highest_matching_source"]:
                    highest_sources[run.result["highest_matching_source"]] += 1
                json_stream.write(
                    json.dumps(
                        {"external_id": location.external_id, "run_id": str(run.pk), **run.result}
                    )
                    + "\n"
                )
                for source in options["sources"]:
                    if source not in scope:
                        per_source[source]["unavailable"] += 1
                        source_stream.write(
                            json.dumps(
                                {
                                    "external_id": location.external_id,
                                    "provider": source,
                                    "status": "unavailable",
                                    "error": report["sources"][source]["error"],
                                }
                            )
                            + "\n"
                        )
                        continue
                    result = run.result["per_source"][source]
                    per_source[source][result["status"]] += 1
                    if result["matched_coordinate"] is not None:
                        source_coordinates[source] += 1
                    source_stream.write(
                        json.dumps(
                            {
                                "external_id": location.external_id,
                                "provider": source,
                                "algorithm_version": ALGORITHM_VERSION,
                                **result,
                            }
                        )
                        + "\n"
                    )
                if index % 100 == 0:
                    report["geocodes"] = dict(counts)
                    report["locations_processed"] = index
                    checkpoint()
                    self.stdout.write(f"Resolved {index} input locations: {dict(counts)}")
                    self.stdout.flush()
        report["geocodes"] = dict(counts)
        report["per_source"] = {source: dict(values) for source, values in per_source.items()}
        report["source_candidate_coordinates"] = dict(source_coordinates)
        report["highest_matching_sources"] = dict(highest_sources)
        report.update(write_analysis_exports(output, comparison_rows))
        report["enriched_input_rows"] = enrich_input_csv(
            options["csv"], output / "fuel-prices-geocoded.csv", comparison_rows
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        failed = [s for s, result in report["sources"].items() if result["status"] == "failed"]
        report["status"] = "incomplete" if failed else "completed"
        report["phase"] = "finished"
        report["locations_processed"] = sum(counts.values())
        checkpoint()
        self.stdout.write(json.dumps({"status": report["status"], "geocodes": dict(counts)}))
        if failed:
            raise CommandError(
                f"Failed source acquisition: {', '.join(failed)}; see {output}/report.json"
            )
