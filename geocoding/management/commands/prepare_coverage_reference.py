"""Download compact additional references for geographic coverage fallbacks."""

import csv
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand

from geocoding.acquisition import FastDownloadClient, duckdb_connection, write_json
from geocoding.normalization import SUBDIVISIONS, locality_codes
from geocoding.parsing import parse_address


class Command(BaseCommand):
    help = "Prepare GeoNames US/Canada localities and Census TIGER primary-road geometry"

    def add_arguments(self, parser):
        parser.add_argument("--directory", default="data/reference/coverage")

    def handle(self, *args, **options):
        directory = Path(options["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        client = FastDownloadClient(directory / "downloads")
        urls = {
            "admin1": "https://download.geonames.org/export/dump/admin1CodesASCII.txt",
            "US": "https://download.geonames.org/export/dump/US.zip",
            "CA": "https://download.geonames.org/export/dump/CA.zip",
            "roads": (
                "https://www2.census.gov/geo/tiger/TIGER2025/PRIMARYROADS/"
                "tl_2025_us_primaryroads.zip"
            ),
        }

        def fetch(item):
            name, url = item
            self.stdout.write(f"Fetching/reusing {name}: {url}")
            filename = client.fetch(url, timeout=180)
            metadata = json.loads(filename.with_suffix(".json").read_text())
            return name, {**metadata, "filename": str(filename), "url": url}

        with ThreadPoolExecutor(max_workers=4) as pool:
            files = dict(pool.map(fetch, urls.items()))
        admin = {}
        for line in Path(files["admin1"]["filename"]).read_text().splitlines():
            parts = line.split("\t")
            country = parts[0].split(".")[0]
            if country in {"US", "CA"}:
                state, _ = locality_codes(parts[1], country)
                if state in SUBDIVISIONS[country]:
                    admin[parts[0]] = state
        settlements = directory / "geonames-settlements.ndjson"
        counts = {}
        with settlements.open("w") as stream:
            for country in ("US", "CA"):
                count = 0
                with zipfile.ZipFile(files[country]["filename"]) as package:
                    with package.open(f"{country}.txt") as zipped:
                        for row in csv.reader(
                            io.TextIOWrapper(zipped, encoding="utf-8"), delimiter="\t"
                        ):
                            if row[6] != "P" and row[7] != "ADM1":
                                continue
                            state = admin.get(f"{country}.{row[10]}")
                            if not state:
                                continue
                            payload = {
                                "id": row[0],
                                "name": row[1],
                                "ascii_name": row[2],
                                "aliases": row[3].split(",") if row[3] else [],
                                "latitude": float(row[4]),
                                "longitude": float(row[5]),
                                "country": country,
                                "state": state,
                                "feature_code": row[7],
                                "population": int(row[14] or 0),
                                "source": "GeoNames",
                                "license": "CC-BY-4.0",
                                "modified_at": row[18],
                            }
                            stream.write(json.dumps(payload) + "\n")
                            count += 1
                counts[country] = count
        roads_dir = directory / "tiger-primaryroads"
        roads_dir.mkdir(exist_ok=True)
        basename = "tl_2025_us_primaryroads"
        with zipfile.ZipFile(files["roads"]["filename"]) as package:
            for extension in ("shp", "shx", "dbf", "prj", "cpg"):
                name = f"{basename}.{extension}"
                if name in package.namelist():
                    (roads_dir / name).write_bytes(package.read(name))
        road_file = directory / "tiger-primaryroads.parquet"
        with duckdb_connection(FastDownloadClient("data/sources")) as connection:
            connection.execute(
                "CREATE TABLE raw_roads AS SELECT * FROM ST_Read(?)",
                [str(roads_dir / f"{basename}.shp")],
            )
            names = connection.execute("SELECT DISTINCT FULLNAME FROM raw_roads").fetchall()
            parsed = []
            for (name,) in names:
                if name:
                    address = parse_address({"name": "ROAD", "address": name, "country": "US"})
                    for road in address.highways:
                        number = str(int(road.number)) if road.number.isdigit() else road.number
                        parsed.append((name, str(road.network), number, str(road.variant)))
            connection.execute(
                "CREATE TABLE route_names (name VARCHAR,network VARCHAR,"
                "number VARCHAR,variant VARCHAR)"
            )
            connection.executemany("INSERT INTO route_names VALUES (?,?,?,?)", parsed)
            road_count = connection.execute(
                """
                COPY (SELECT LINEARID AS id,FULLNAME AS name,MTFCC,RTTYP,
                             n.network,n.number,n.variant,
                             ST_Transform(geom,'EPSG:4269','EPSG:4326',always_xy:=true) AS geometry
                      FROM raw_roads r JOIN route_names n ON n.name=r.FULLNAME)
                TO ? (FORMAT PARQUET)
            """,
                [str(road_file)],
            ).fetchone()[0]
        manifest = {
            "prepared_at": datetime.now(UTC).isoformat(),
            "files": files,
            "settlements_file": str(settlements),
            "settlement_records": counts,
            "roads_file": str(road_file),
            "road_records": road_count,
            "roads_source": "US Census TIGER/Line 2025 Primary Roads",
            "roads_country": "US",
            "roads_limitations": (
                "Limited-access primary roads; not all state/US/local roads or exits"
            ),
            "geonames_attribution": "GeoNames, CC BY 4.0, https://www.geonames.org/",
        }
        write_json(directory / "manifest.json", manifest)
        self.stdout.write(
            json.dumps(
                {
                    k: manifest[k]
                    for k in ("settlement_records", "road_records", "roads_limitations")
                },
                indent=2,
            )
        )
