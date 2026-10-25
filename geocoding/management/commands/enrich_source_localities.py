"""Batch point-in-polygon and bounded nearest-locality enrichment of imported POIs."""

import csv
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from psycopg.types.json import Jsonb

from geocoding.acquisition import DownloadClient, duckdb_connection, write_json
from geocoding.locality_enrichment import VERSION, audit_sources, enrich_record
from geocoding.models import SourcePlace
from geocoding.normalization import city_key, text_key


class Command(BaseCommand):
    help = "Fill missing city/state from coordinate geometry, preserving raw data and provenance"

    def add_arguments(self, parser):
        parser.add_argument("--reference-dir", default="data/reference/localities")
        parser.add_argument("--output-dir", default="output/locality-enrichment")
        parser.add_argument("--max-nearest-distance-m", type=float, default=10000)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        output = Path(options["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        existing = output / "report.json"
        if existing.exists() and json.loads(existing.read_text()).get("status") == "completed":
            raise CommandError(
                "Completed audit exists; use a new --output-dir to preserve its backup"
            )
        manifest_file = Path(options["reference_dir"]) / "manifest.json"
        if not manifest_file.exists():
            raise CommandError("Run prepare_locality_reference first")
        manifest = json.loads(manifest_file.read_text())
        if options["max_nearest_distance_m"] < 0:
            raise CommandError("Distance must be nonnegative")
        before = audit_sources()
        write_json(output / "before-audit.json", before)
        performed_at = datetime.now(UTC).isoformat()
        pending = list(
            SourcePlace.objects.filter(Q(city_key="") | Q(state="") | Q(country="")).values(
                "id",
                "provider",
                "namespace",
                "source_id",
                "release",
                "city_key",
                "state",
                "country",
                "latitude",
                "longitude",
                "address",
                "provenance",
            )
        )
        with (output / "original-records.jsonl").open("w") as stream:
            for row in pending:
                stream.write(json.dumps(row) + "\n")
        points_file = output / "points.csv"
        with points_file.open("w", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["id", "country", "state", "city", "latitude", "longitude"]
            )
            writer.writeheader()
            for row in pending:
                if row["latitude"] is not None and row["longitude"] is not None:
                    writer.writerow(
                        {
                            "id": row["id"],
                            "country": row["country"],
                            "state": row["state"],
                            "city": row["city_key"],
                            "latitude": row["latitude"],
                            "longitude": row["longitude"],
                        }
                    )
        self.stdout.write(f"Mapping {len(pending):,} records with missing locality fields")
        self.stdout.flush()
        with duckdb_connection(DownloadClient("data/sources")) as db:
            db.execute("SET memory_limit='4GB'")
            db.execute("SET threads=2")
            db.execute(
                "CREATE TABLE points AS SELECT CAST(id AS BIGINT) AS id,country,state,city,"
                "CAST(latitude AS DOUBLE) AS lat,CAST(longitude AS DOUBLE) AS lon,"
                "ST_Point(CAST(longitude AS DOUBLE),CAST(latitude AS DOUBLE)) AS geometry "
                "FROM read_csv(?,all_varchar=true,nullstr='__NULL__')",
                [str(points_file)],
            )
            db.execute(
                "CREATE TABLE regions AS SELECT * FROM read_parquet(?)", [manifest["regions_file"]]
            )
            db.execute(
                "CREATE TABLE cities AS SELECT * FROM read_parquet(?)", [manifest["cities_file"]]
            )
            self.stdout.write("Joining POI coordinates to administrative polygons")
            self.stdout.flush()
            # Most records already have a state from the regional source extract.
            # Check that one region first; run a full reverse join only for missing states.
            region_rows = db.execute("""
                SELECT p.id,r.country,r.state,r.source FROM points p JOIN regions r
                  ON r.country=p.country AND r.state=p.state
                WHERE p.state<>'' AND ST_Covers(r.geometry,p.geometry)
                UNION ALL
                SELECT p.id,r.country,r.state,r.source
                FROM (SELECT * FROM points WHERE state='') p JOIN regions r
                  ON ST_Covers(r.geometry,p.geometry)
            """).fetchall()
            self.stdout.write(f"Administrative regions resolved for {len(region_rows):,} points")
            self.stdout.flush()
            region_hits = defaultdict(list)
            for identity, country, state, source in region_rows:
                region_hits[identity].append((country, state, source))
            mappings = {}
            for row in pending:
                hits = region_hits.get(row["id"], [])
                if len(hits) > 1:
                    hits = [
                        hit for hit in hits if hit[0] == row["country"] and hit[1] == row["state"]
                    ]
                if len(hits) == 1:
                    country, state, source = hits[0]
                    if (not row["country"] or country == row["country"]) and (
                        not row["state"] or state == row["state"]
                    ):
                        mappings[row["id"]] = {
                            "country": country,
                            "state": state,
                            "region_source": source,
                        }
            city_rows = db.execute(
                "SELECT p.id,c.country,c.state,c.name,c.reference_id,c.source "
                "FROM points p JOIN cities c ON ST_Covers(c.geometry,p.geometry) "
                "WHERE p.city=''"
            ).fetchall()
            city_hits = defaultdict(list)
            for identity, country, state, name, reference, source in city_rows:
                mapping = mappings.get(identity)
                if mapping and (country, state) == (mapping["country"], mapping["state"]):
                    city_hits[identity].append((name, reference, source))
            for identity, hits in city_hits.items():
                if len({city_key(h[0]) for h in hits}) == 1:
                    name, reference, source = hits[0]
                    mappings[identity].update(
                        city=name,
                        city_method="point_in_polygon",
                        city_reference_id=reference,
                        city_source=source,
                    )
            self.stdout.write("Indexing named settlements for the remaining rural POIs")
            self.stdout.flush()
            settlement_file = output / "settlements.csv"
            with settlement_file.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    ["country", "state", "name", "reference_id", "source", "lat", "lon"]
                )
                for line in Path("data/reference/coverage/geonames-settlements.ndjson").open():
                    r = json.loads(line)
                    if text_key(r["name"]) and r["feature_code"] in {
                        "PPL",
                        "PPLA",
                        "PPLA2",
                        "PPLA3",
                        "PPLA4",
                        "PPLC",
                        "PPLG",
                    }:
                        writer.writerow(
                            [
                                r["country"],
                                r["state"],
                                r["name"],
                                r["id"],
                                "GeoNames",
                                r["latitude"],
                                r["longitude"],
                            ]
                        )
                for line in Path("data/reference/cities.ndjson").open():
                    r = json.loads(line)
                    if r.get("place") in {"city", "town", "village", "hamlet", "municipality"}:
                        name = next(
                            (
                                n
                                for n in [r.get("tags", {}).get("name:en", ""), r["name"]]
                                if text_key(n)
                            ),
                            None,
                        )
                        if not name:
                            continue
                        writer.writerow(
                            [
                                r["country"],
                                r["state_iso"].split("-")[-1],
                                name,
                                str(r["osm_id"]),
                                "OpenStreetMap settlement",
                                r["latitude"],
                                r["longitude"],
                            ]
                        )
            db.execute(
                """CREATE TABLE settlements AS SELECT *,ST_Point(lon,lat) AS geometry,
                    floor(lon/.1)::INTEGER AS gx,floor(lat/.1)::INTEGER AS gy
                    FROM (SELECT country,state,name,reference_id,source,
                        CAST(lat AS DOUBLE) AS lat,CAST(lon AS DOUBLE) AS lon
                        FROM read_csv(?,all_varchar=true,quote='"',escape='"'))""",
                [str(settlement_file)],
            )
            unresolved = [
                (identity, m["country"], m["state"])
                for identity, m in mappings.items()
                if not m.get("city")
            ]
            db.execute("CREATE TABLE unresolved (id BIGINT,country VARCHAR,state VARCHAR)")
            if unresolved:
                db.executemany("INSERT INTO unresolved VALUES (?,?,?)", unresolved)
            radius = options["max_nearest_distance_m"]
            db.execute(
                """
                CREATE TABLE nearest AS WITH expanded AS (
                  SELECT p.id,u.country,u.state,p.lat,p.lon,x.gx,y.gy
                  FROM points p JOIN unresolved u USING(id),
                  range(floor((p.lon-$radius/(111320*cos(radians(p.lat))))/.1)::BIGINT,
                        floor((p.lon+$radius/(111320*cos(radians(p.lat))))/.1)::BIGINT+1) x(gx),
                  range(floor((p.lat-$radius/111320)/.1)::BIGINT,
                        floor((p.lat+$radius/111320)/.1)::BIGINT+1) y(gy)
                  WHERE p.city=''
                ), distances AS (
                  SELECT p.id,s.name,s.reference_id,s.source,s.lat AS ref_lat,s.lon AS ref_lon,
                    2*6371008.8*asin(sqrt(least(1.0,
                      pow(sin(radians(p.lat-s.lat)/2),2)+cos(radians(p.lat))*cos(radians(s.lat))*
                      pow(sin(radians(p.lon-s.lon)/2),2)))) AS distance_m
                  FROM expanded p JOIN settlements s ON s.country=p.country AND s.state=p.state
                      AND s.gx=p.gx AND s.gy=p.gy
                ) SELECT * FROM distances WHERE distance_m<=$radius
                  QUALIFY row_number() OVER (
                    PARTITION BY id ORDER BY distance_m,source,reference_id)=1
            """,
                {"radius": radius},
            )
            for identity, name, reference, source, lat, lon, distance in db.execute(
                "SELECT * FROM nearest"
            ).fetchall():
                mappings[identity].update(
                    city=name,
                    city_method="nearest_settlement",
                    city_reference_id=reference,
                    city_source=source,
                    city_distance_m=round(distance, 3),
                    reference_latitude=lat,
                    reference_longitude=lon,
                )
        updates = []
        counts = defaultdict(Counter)
        with (output / "changes.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "id",
                    "provider",
                    "namespace",
                    "source_id",
                    "latitude",
                    "longitude",
                    "original_city",
                    "original_state",
                    "city",
                    "state",
                    "city_method",
                    "city_distance_m",
                    "city_source",
                    "state_source",
                ],
            )
            writer.writeheader()
            for record in pending:
                mapping = mappings.get(record["id"])
                updated = enrich_record(record, mapping, performed_at)
                if not updated:
                    counts[record["provider"]]["unresolved_records"] += 1
                    continue
                updates.append(updated)
                for field, detail in updated["filled_fields"].items():
                    counts[record["provider"]][f"{field}_filled"] += 1
                    counts[record["provider"]][f"{field}_{detail['method']}"] += 1
                writer.writerow(
                    {
                        "id": record["id"],
                        "provider": record["provider"],
                        "namespace": record["namespace"],
                        "source_id": record["source_id"],
                        "latitude": record["latitude"],
                        "longitude": record["longitude"],
                        "original_city": record["city_key"],
                        "original_state": record["state"],
                        "city": updated["city_key"],
                        "state": updated["state"],
                        "city_method": updated["filled_fields"].get("city", {}).get("method", ""),
                        "city_distance_m": mapping.get("city_distance_m", ""),
                        "city_source": mapping.get("city_source", ""),
                        "state_source": mapping["region_source"],
                    }
                )
        if options["apply"]:
            self.stdout.write(f"Applying {len(updates):,} auditable canonical/index updates")
            self.stdout.flush()
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "CREATE TEMP TABLE locality_updates (id BIGINT,city TEXT,state TEXT,"
                    "country TEXT,address JSONB,provenance JSONB) ON COMMIT DROP"
                )
                with cursor.copy("COPY locality_updates FROM STDIN") as copier:
                    for row in updates:
                        copier.write_row(
                            (
                                row["id"],
                                row["city_key"],
                                row["state"],
                                row["country"],
                                Jsonb(row["address"]),
                                Jsonb(row["provenance"]),
                            )
                        )
                cursor.execute(
                    "UPDATE geocoding_sourceplace p SET city_key=u.city,state=u.state,"
                    "country=u.country,address=u.address,provenance=u.provenance "
                    "FROM locality_updates u WHERE p.id=u.id"
                )
                assert cursor.rowcount == len(updates)
        after = audit_sources()
        for provider in before:
            assert before[provider]["total"] == after[provider]["total"]
            for field in ("native_city_missing", "native_state_missing", "coordinates_missing"):
                assert before[provider][field] == after[provider][field]
            if options["apply"]:
                assert (
                    before[provider]["indexed_city_missing"]
                    - after[provider]["indexed_city_missing"]
                    == counts[provider]["city_filled"]
                )
                assert (
                    before[provider]["indexed_state_missing"]
                    - after[provider]["indexed_state_missing"]
                    == counts[provider]["state_filled"]
                )
        report = {
            "status": "completed" if options["apply"] else "planned",
            "version": VERSION,
            "performed_at": performed_at,
            "before": before,
            "after": after,
            "changes": {p: dict(c) for p, c in counts.items()},
            "updated_records": len(updates),
            "max_nearest_distance_m": radius,
            "reference_manifest": manifest,
            "raw_payloads_preserved": True,
            "coordinates_preserved": True,
            "backup": str(output / "original-records.jsonl"),
        }
        write_json(output / "report.json", report)
        self.stdout.write(
            json.dumps(
                {"status": report["status"], "changes": report["changes"], "after": after}, indent=2
            )
        )
