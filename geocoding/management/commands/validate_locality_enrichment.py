"""Independently verify applied locality inference against its snapshot and geometry."""

import csv
import json
from collections import Counter, defaultdict
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from geocoding.acquisition import DownloadClient, duckdb_connection, write_json
from geocoding.locality_enrichment import audit_sources
from geocoding.models import SourcePlace
from geocoding.normalization import text_key


class Command(BaseCommand):
    help = "Validate locality-only changes and export remaining missing city/state records"

    def add_arguments(self, parser):
        parser.add_argument("--run", default="output/locality-enrichment")

    def handle(self, *args, **options):
        output = Path(options["run"])
        report = json.loads((output / "report.json").read_text())
        if report["status"] != "completed":
            raise CommandError("An applied enrichment run is required")
        originals = {
            row["id"]: row for row in map(json.loads, (output / "original-records.jsonl").open())
        }
        audit = audit_sources()
        assert audit == report["after"], "Database changed since enrichment"
        counts = defaultdict(Counter)
        changed, nearest, polygon_points, state_points = 0, 0, [], []
        fields = list(next(iter(originals.values())))
        current = SourcePlace.objects.filter(id__in=originals).values(*fields)
        seen = set()
        for row in current.iterator(chunk_size=2000):
            old = originals[row["id"]]
            seen.add(row["id"])
            for key in set(fields) - {"address", "provenance", "city_key", "state", "country"}:
                assert row[key] == old[key], (row["id"], key)
            for key in ("city_key", "state", "country"):
                if old[key]:
                    assert old[key] == row[key], (row["id"], "overwritten", key)
            for key in set(old["address"]) | set(row["address"]):
                if key not in {"city", "state", "country", "warnings"}:
                    assert old["address"].get(key) == row["address"].get(key), (row["id"], key)
            assert row["provenance"][: len(old["provenance"])] == old["provenance"]
            assert set(old["address"]["warnings"]) <= set(row["address"]["warnings"])
            additions = row["provenance"][len(old["provenance"]) :]
            if not additions:
                assert row == old, (row["id"], "untracked change")
                continue
            assert len(additions) == 1 and additions[0]["dataset"] == "locality_enrichment"
            changed += 1
            details = additions[0]["filled_fields"]
            for key in ("city", "state", "country"):
                index = "city_key" if key == "city" else key
                assert row[index] == row["address"][key]
                if row[index] != old[index]:
                    assert key in details and not old["address"][key]
                    assert row[index] == details[key]["value"] and row[index]
                    counts[row["provider"]][f"{key}_filled"] += 1
            city = details.get("city")
            if city:
                counts[row["provider"]][f"city_{city['method']}"] += 1
                if city["method"] == "point_in_polygon":
                    polygon_points.append(
                        (
                            row["id"],
                            row["country"],
                            row["state"],
                            row["city_key"],
                            str(city["reference_id"]),
                            row["longitude"],
                            row["latitude"],
                        )
                    )
                else:
                    assert city["method"] == "nearest_settlement"
                    lat, lon = radians(row["latitude"]), radians(row["longitude"])
                    rlat, rlon = (
                        radians(city["reference_latitude"]),
                        radians(city["reference_longitude"]),
                    )
                    distance = (
                        2
                        * 6371008.8
                        * asin(
                            sqrt(
                                min(
                                    1,
                                    sin((lat - rlat) / 2) ** 2
                                    + cos(lat) * cos(rlat) * sin((lon - rlon) / 2) ** 2,
                                )
                            )
                        )
                    )
                    assert distance <= report["max_nearest_distance_m"]
                    assert abs(distance - city["distance_m"]) < 0.0011
                    assert (
                        "city_from_nearest_settlement_requires_review" in row["address"]["warnings"]
                    )
                    nearest += 1
            if "state" in details:
                state_points.append(
                    (row["id"], row["country"], row["state"], row["longitude"], row["latitude"])
                )
        assert seen == set(originals)
        assert changed == report["updated_records"]
        for provider, expected in report["changes"].items():
            for key, count in counts[provider].items():
                assert count == expected[key], (provider, key)
        self.stdout.write(f"Snapshot checks passed for {len(seen):,} records; verifying polygons")
        self.stdout.flush()
        remaining = list(
            SourcePlace.objects.filter(Q(city_key="") | Q(state="")).values(
                "id",
                "provider",
                "namespace",
                "source_id",
                "city_key",
                "state",
                "country",
                "latitude",
                "longitude",
            )
        )
        manifest = report["reference_manifest"]
        with duckdb_connection(DownloadClient("data/sources")) as db:
            db.execute("SET memory_limit='4GB'")
            db.execute("SET threads=2")
            db.execute(
                "CREATE TABLE cities AS SELECT * FROM read_parquet(?)", [manifest["cities_file"]]
            )
            db.execute(
                "CREATE TABLE regions AS SELECT * FROM read_parquet(?)", [manifest["regions_file"]]
            )
            db.execute(
                "CREATE TABLE checks (id BIGINT,country VARCHAR,state VARCHAR,city VARCHAR,"
                "reference_id VARCHAR,lon DOUBLE,lat DOUBLE)"
            )
            # CSV loading avoids a slow per-row SQL insert for the national batch.
            checks_file = output / "polygon-validation-points.csv"
            with checks_file.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["id", "country", "state", "city", "reference_id", "lon", "lat"])
                writer.writerows(polygon_points)
            db.execute(
                "INSERT INTO checks SELECT id::BIGINT,country,state,city,reference_id,"
                "lon::DOUBLE,lat::DOUBLE FROM read_csv(?,all_varchar=true)",
                [str(checks_file)],
            )
            verified = db.execute(
                "SELECT p.id,c.name FROM checks p JOIN cities c "
                "ON p.reference_id=c.reference_id AND p.country=c.country AND p.state=c.state "
                "WHERE ST_Covers(c.geometry,ST_Point(p.lon,p.lat))"
            ).fetchall()
            verified_names = dict(verified)
            assert len(verified_names) == len(polygon_points)
            assert all(text_key(verified_names[p[0]]) == p[3] for p in polygon_points)
            db.execute(
                "CREATE TABLE state_checks (id BIGINT,country VARCHAR,state VARCHAR,"
                "lon DOUBLE,lat DOUBLE)"
            )
            if state_points:
                db.executemany("INSERT INTO state_checks VALUES (?,?,?,?,?)", state_points)
            assert db.execute(
                "SELECT count(DISTINCT p.id) FROM state_checks p JOIN regions r "
                "ON p.country=r.country AND p.state=r.state "
                "WHERE ST_Covers(r.geometry,ST_Point(p.lon,p.lat))"
            ).fetchone()[0] == len(state_points)
            db.execute("DELETE FROM state_checks")
            with_coords = [
                r for r in remaining if r["latitude"] is not None and r["longitude"] is not None
            ]
            if with_coords:
                db.executemany(
                    "INSERT INTO state_checks VALUES (?,?,?,?,?)",
                    [
                        (r["id"], r["country"], r["state"], r["longitude"], r["latitude"])
                        for r in with_coords
                    ],
                )
            contained = {
                r[0]
                for r in db.execute(
                    "SELECT p.id FROM state_checks p JOIN regions r "
                    "ON p.country=r.country AND p.state=r.state "
                    "WHERE ST_Covers(r.geometry,ST_Point(p.lon,p.lat))"
                ).fetchall()
            }
        reason_counts = Counter()
        for row in remaining:
            row["missing_fields"] = ";".join(
                k for k in ("city", "state") if not row["city_key" if k == "city" else k]
            )
            row["reason"] = (
                "no_coordinates"
                if row["latitude"] is None or row["longitude"] is None
                else "no_unique_locality_within_10km"
                if row["id"] in contained
                else "outside_claimed_region_or_unsupported_boundary"
            )
            reason_counts[row["reason"]] += 1
        with (output / "remaining-missing-localities.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(remaining[0]) if remaining else ["id"])
            writer.writeheader()
            writer.writerows(remaining)
        result = {
            "status": "passed",
            "snapshot_records_checked": len(seen),
            "changed_records_checked": changed,
            "city_polygons_verified": len(polygon_points),
            "state_polygons_verified": len(state_points),
            "bounded_nearest_distances_verified": nearest,
            "coordinate_changes": 0,
            "known_locality_overwrites": 0,
            "remaining_records": len(remaining),
            "remaining_reasons": dict(reason_counts),
            "filled_fields": {p: dict(c) for p, c in counts.items()},
        }
        write_json(output / "validation.json", result)
        self.stdout.write(json.dumps(result, indent=2))
