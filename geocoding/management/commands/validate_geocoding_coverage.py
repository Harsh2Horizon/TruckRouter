"""Validate raw-row preservation, geographic evidence and final coverage."""

import csv
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand

from geocoding.acquisition import DownloadClient, duckdb_connection, write_json
from geocoding.geography import boundary_state_code
from geocoding.models import SourcePlace
from geocoding.normalization import locality_codes


def read(filename):
    with Path(filename).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


class Command(BaseCommand):
    help = "Check every coverage row and independently check US state containment"

    def add_arguments(self, parser):
        parser.add_argument("--run", default="output/coverage")
        parser.add_argument("--base-run", default="output/geocoding")
        parser.add_argument("--csv", default="fuel-prices-for-be-assessment.csv")

    def handle(self, *args, **options):
        output, base = Path(options["run"]), Path(options["base_run"])
        raw = read(options["csv"])
        rows = read(output / "all-records-geocoded.csv")
        stops = read(output / "final-stops.csv")
        previous = {r["external_id"]: r for r in read(base / "geocodes.csv")}
        assert len(raw) == len(rows)
        assert len(stops) == len({r["OPIS Truckstop ID"].strip() for r in raw})
        points = []
        published_points = set()
        for provider in ("alltheplaces", "overture", "osm"):
            identities = {
                r["Reference ID"]
                for r in rows
                if r["Location Method"] == "fuel_poi" and r["Coordinate Source"] == provider
            }
            published_points.update(
                (provider, *point)
                for point in SourcePlace.objects.filter(provider=provider, source_id__in=identities)
                .values_list("source_id", "latitude", "longitude")
                .iterator()
            )
        for number, (original, row) in enumerate(zip(raw, rows, strict=True), start=2):
            assert all(row[k] == v for k, v in original.items()), number
            assert int(row["Input Row Number"]) == number
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
            assert -90 <= lat <= 90 and -180 <= lon <= 180
            assert (lat, lon) != (0, 0)
            state, country = locality_codes(row["State"], "")
            points.append((number, country, state, lat, lon, row["Location Method"]))
            if row["Location Method"] != "fuel_poi":
                assert row["Is Approximate"] == "True" and row["Needs Review"] == "True"
                assert float(row["Match Score"]) == 0
                assert row["Coordinate Precision"] != "poi"
            else:
                old = previous[row["OPIS Truckstop ID"].strip()]
                provider = row["Coordinate Source"]
                assert provider in {"alltheplaces", "overture", "osm"}
                assert (provider, row["Reference ID"], lat, lon) in published_points
                retained = (
                    old["prediction_source"] == provider
                    and float(old["predicted_latitude"]) == lat
                    and float(old["predicted_longitude"]) == lon
                    and float(old["prediction_score"]) == float(row["Match Score"])
                )
                alternative = (
                    old[f"{provider}_latitude"]
                    and float(old[f"{provider}_latitude"]) == lat
                    and float(old[f"{provider}_longitude"]) == lon
                    and float(old[f"{provider}_score"]) == float(row["Match Score"])
                )
                assert retained or alternative
            if row["Location Method"] in {"highway_exit", "highway_near_city"}:
                assert float(row["City Anchor Distance (m)"]) <= 20000
            if row["Location Method"] == "highway_exit":
                assert row["Exit Matched"] == "True" and "Exit" in row["Matched Reference"]
            if row["Location Method"] in {"named_locality", "highway_near_city", "census_address"}:
                assert row["Exit Matched"] == "False"
        metadata = json.loads(Path("data/reference/usa-states-manifest.json").read_text())
        geometry = json.loads(Path(metadata["filename"]).read_text())
        with duckdb_connection(DownloadClient("data/sources")) as connection:
            connection.execute(
                "CREATE TABLE points (row_number INTEGER,country VARCHAR,state VARCHAR,"
                "latitude DOUBLE,longitude DOUBLE,method VARCHAR)"
            )
            connection.executemany("INSERT INTO points VALUES (?,?,?,?,?,?)", points)
            connection.execute("CREATE TABLE states (state VARCHAR,geometry GEOMETRY)")
            connection.executemany(
                "INSERT INTO states VALUES (?,ST_GeomFromGeoJSON(?))",
                [
                    (boundary_state_code(f["properties"]), json.dumps(f["geometry"]))
                    for f in geometry["features"]
                ],
            )
            inconsistent = connection.execute("""
                SELECT p.row_number,p.state,p.method FROM points p
                LEFT JOIN states s ON p.state=s.state
                WHERE p.country='US' AND NOT coalesce(ST_Covers(s.geometry,
                      ST_Point(p.longitude,p.latitude)),false)
            """).fetchall()
        assert not inconsistent, inconsistent[:10]
        result = {
            "passed": True,
            "all_raw_fields_and_prices_preserved": True,
            "original_rows": len(raw),
            "unique_stops": len(stops),
            "rows_without_coordinates": 0,
            "method_counts": dict(Counter(r["Location Method"] for r in rows)),
            "us_points_geometrically_checked": sum(p[1] == "US" for p in points),
            "us_points_outside_input_state": 0,
            "canadian_validation": "country/province metadata; no polygon test",
            "city_level_ambiguous_rows": sum(
                "ambiguous same-name" in r["Approximation Reason"] for r in rows
            ),
            "provider_points_and_scores_verified": True,
            "limits": "Complete geographic coverage is not complete exact-station identification",
        }
        write_json(output / "validation.json", result)
        self.stdout.write(json.dumps(result, indent=2))
