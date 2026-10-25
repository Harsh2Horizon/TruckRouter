"""Fill final geographic coordinates without rewriting independent source matches."""

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from geocoding.acquisition import write_json
from geocoding.census import fetch_addresses
from geocoding.coverage import VERSION, CoverageResolver, parse_raw
from geocoding.matching import input_addresses_disagree

EXTRA_FIELDS = [
    "Input Row Number",
    "Latitude",
    "Longitude",
    "Location Method",
    "Coordinate Precision",
    "Coordinate Source",
    "Is Approximate",
    "Match Score",
    "Match Status",
    "Needs Review",
    "Matched Reference",
    "Reference ID",
    "City Anchor",
    "City Anchor Distance (m)",
    "Exit Matched",
    "Exit Association",
    "Approximation Reason",
    "Alternative Anchor Spread (m)",
    "Parsing Corrections",
    "Input Conflict",
    "Previous Latitude",
    "Previous Longitude",
    "Maximum Source Deviation (m)",
    "Source Deviation Severity",
    "Coverage Version",
    "Geographic Validation",
]


def read_csv(filename):
    with Path(filename).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def raw_query(raw):
    return parse_raw(
        {
            "name": raw["Truckstop Name"],
            "address": raw["Address"],
            "city": raw["City"],
            "state": raw["State"],
        }
    )


class Command(BaseCommand):
    help = "Export every original row with the best POI or explicitly approximate geographic point"

    def add_arguments(self, parser):
        parser.add_argument("--csv", default="fuel-prices-for-be-assessment.csv")
        parser.add_argument("--base-run", default="output/geocoding-v3")
        parser.add_argument("--output-dir", default="output/geocoding-v4")
        parser.add_argument("--reference-dir", default="data/reference/coverage")
        parser.add_argument("--fetch-census", action="store_true")

    def handle(self, *args, **options):
        output, base = Path(options["output_dir"]), Path(options["base_run"])
        output.mkdir(parents=True, exist_ok=True)
        base_report = json.loads((base / "report.json").read_text())
        if base_report["status"] != "completed":
            raise CommandError("The base POI run must be completed")
        fields, raw_rows = read_csv(options["csv"])
        _, base_rows = read_csv(base / "geocodes.csv")
        base_index = {row["external_id"]: row for row in base_rows}
        if {r["OPIS Truckstop ID"].strip() for r in raw_rows} != set(base_index):
            raise CommandError("Input stop identities differ from the base run")
        parsed = [raw_query(raw) for raw in raw_rows]
        report = {
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "coverage_version": VERSION,
            "base_run": str(base),
            "base_algorithm": base_report["algorithm_version"],
            "input_rows": len(raw_rows),
            "original_fields": fields,
        }
        if options["fetch_census"]:
            queries = [
                query
                for raw, (query, _) in zip(raw_rows, parsed, strict=True)
                if not base_index[raw["OPIS Truckstop ID"].strip()]["predicted_latitude"]
            ]
            self.stdout.write("Checking Census address geocoding for eligible structured addresses")
            self.stdout.flush()
            report["census_queries"] = fetch_addresses(queries, options["reference_dir"])
            self.stdout.write(json.dumps(report["census_queries"]))
        self.stdout.write("Loading versioned locality, exit and road references")
        self.stdout.flush()
        resolver = CoverageResolver(options["reference_dir"])
        method_counts, unique_methods = Counter(), Counter()
        results, stops = [], {}
        fallback_cache = {}
        for index, (raw, (query, corrections)) in enumerate(
            zip(raw_rows, parsed, strict=True), start=2
        ):
            identifier = raw["OPIS Truckstop ID"].strip()
            previous = base_index[identifier]
            canonical, _ = parse_raw(
                {
                    "name": previous["name"],
                    "address": previous["address"],
                    "city": previous["city"],
                    "state": previous["state"],
                    "country": previous["country"],
                }
            )
            conflict = "input_records_disagree" in previous["reasons"] or input_addresses_disagree(
                query, canonical
            )
            if previous["predicted_latitude"] and not conflict:
                point = {
                    "latitude": float(previous["predicted_latitude"]),
                    "longitude": float(previous["predicted_longitude"]),
                }
                poi = previous["prediction_method"] == "fuel_poi"
                method = previous["prediction_method"]
                exit_matched = method == "highway_exit"
                coordinate_source = previous["prediction_source"]
                coordinate_score = float(previous["prediction_score"]) if poi else 0.0
                coordinate_reference = (
                    previous["source_id"] or previous["highest_matching_source_id"]
                )
                geographic_warning = ""
                if query.country == "US" and not resolver.roads.point_in_state(query, point):
                    alternatives = []
                    for provider in ("alltheplaces", "overture", "osm"):
                        if not previous[f"{provider}_latitude"]:
                            continue
                        alternative = {
                            "latitude": float(previous[f"{provider}_latitude"]),
                            "longitude": float(previous[f"{provider}_longitude"]),
                        }
                        if resolver.roads.point_in_state(query, alternative):
                            alternatives.append(
                                (
                                    float(previous[f"{provider}_score"]),
                                    provider,
                                    alternative,
                                    previous[f"{provider}_source_id"],
                                )
                            )
                    geographic_warning = "excluded original point outside input state"
                    if alternatives:
                        alternatives.sort(key=lambda item: (-item[0], item[1]))
                        coordinate_score, coordinate_source, point, coordinate_reference = (
                            alternatives[0]
                        )
                    else:
                        point = None
                result = {
                    "coordinate": point,
                    "method": method,
                    "precision": previous.get("prediction_precision")
                    or ("poi" if poi else "motorway_exit" if exit_matched else "unknown"),
                    "source": coordinate_source,
                    "is_approximate": not poi,
                    "match_score": coordinate_score,
                    "match_status": previous["status"],
                    "needs_review": previous["status"] != "accepted",
                    "reference_id": coordinate_reference if poi else "",
                    "reference_name": (
                        previous.get(f"{coordinate_source}_matched_name") or previous["name"]
                    )
                    if poi
                    else f"{previous['fallback_highway']} Exit {previous['fallback_exit']}"
                    if exit_matched
                    else query.city,
                    "city_anchor": query.city,
                    "city_distance_m": previous["fallback_city_distance_m"],
                    "exit_matched": exit_matched,
                    "exit_association": "input_scoped" if exit_matched else "",
                    "reason": "source POI candidate; retain original review status"
                    if poi
                    else "verified highway exit; fuel station not verified"
                    if exit_matched
                    else previous.get("fallback_reason")
                    or "geographic approximation; station not verified",
                }
                if geographic_warning:
                    result["reason"] += "; " + geographic_warning
                    result["needs_review"] = True
                    result["match_status"] = "needs_review"
                if point is None:
                    result = resolver.resolve(query)
            else:
                key = (query.country, query.state, query.city, query.raw_address)
                if key not in fallback_cache:
                    fallback_cache[key] = resolver.resolve(query)
                result = fallback_cache[key]
            point = result.get("coordinate") or {}
            row = {
                **raw,
                "Input Row Number": index,
                "Latitude": point.get("latitude", ""),
                "Longitude": point.get("longitude", ""),
                "Location Method": result["method"],
                "Coordinate Precision": result["precision"],
                "Coordinate Source": result["source"],
                "Is Approximate": result["is_approximate"],
                "Match Score": result["match_score"],
                "Match Status": result.get(
                    "match_status", "approximate" if point else "unresolved"
                ),
                "Needs Review": result["needs_review"],
                "Matched Reference": result.get("reference_name", ""),
                "Reference ID": result.get("reference_id", ""),
                "City Anchor": result.get("city_anchor", ""),
                "City Anchor Distance (m)": result.get("city_distance_m", ""),
                "Exit Matched": result.get("exit_matched", False),
                "Exit Association": result.get("exit_association", ""),
                "Approximation Reason": result.get("reason", ""),
                "Alternative Anchor Spread (m)": result.get("alternative_anchor_spread_m", ""),
                "Parsing Corrections": ";".join(corrections),
                "Input Conflict": conflict,
                "Previous Latitude": previous["predicted_latitude"],
                "Previous Longitude": previous["predicted_longitude"],
                "Maximum Source Deviation (m)": previous["max_source_deviation_m"],
                "Source Deviation Severity": previous["deviation_severity"],
                "Coverage Version": VERSION,
                "Geographic Validation": "US state polygon"
                if query.country == "US"
                else "country/province metadata; polygon not checked",
            }
            results.append(row)
            method_counts[row["Location Method"]] += 1
            if identifier not in stops:
                stops[identifier] = row
                unique_methods[row["Location Method"]] += 1
            if (index - 1) % 500 == 0:
                self.stdout.write(f"Processed {index - 1:,} rows: {dict(method_counts)}")
                self.stdout.flush()
        resolver.roads.close()
        for name, rows in (
            ("all-records-geocoded.csv", results),
            ("final-stops.csv", stops.values()),
            ("recovered-records.csv", [r for r in results if not r["Previous Latitude"]]),
        ):
            with (output / name).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=[*fields, *EXTRA_FIELDS])
                writer.writeheader()
                writer.writerows(rows)
        missing = sum(r["Latitude"] == "" or r["Longitude"] == "" for r in results)
        report.update(
            status="completed" if not missing else "incomplete",
            finished_at=datetime.now(UTC).isoformat(),
            rows_with_coordinates=len(results) - missing,
            rows_without_coordinates=missing,
            method_counts=dict(method_counts),
            unique_stops=len(stops),
            unique_stop_methods=dict(unique_methods),
            newly_filled_rows=sum(
                not r["Previous Latitude"] and r["Latitude"] != "" for r in results
            ),
            reference_manifest=resolver.manifest,
        )
        write_json(output / "report.json", report)
        self.stdout.write(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "status",
                        "rows_with_coordinates",
                        "rows_without_coordinates",
                        "newly_filled_rows",
                        "unique_stop_methods",
                    )
                },
                indent=2,
            )
        )
        if missing:
            raise CommandError(f"{missing} rows have insufficient geographic input; see report")
