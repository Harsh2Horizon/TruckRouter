"""Prepare coordinate-to-administrative-name indexes, independently of input brands."""

import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand

from geocoding.acquisition import FastDownloadClient, duckdb_connection, write_json
from geocoding.normalization import SUBDIVISIONS, locality_codes


def extract_shape(archive, directory):
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        shapes = [m for m in package.namelist() if m.endswith(".shp")]
        if len(shapes) != 1:
            raise ValueError("Expected exactly one shapefile")
        stem = Path(shapes[0]).stem
        for member in package.namelist():
            name = Path(member).name
            if Path(name).stem == stem and Path(name).suffix in {
                ".shp",
                ".shx",
                ".dbf",
                ".prj",
                ".cpg",
            }:
                (directory / name).write_bytes(package.read(member))
    return directory / f"{stem}.shp"


class Command(BaseCommand):
    help = "Prepare US Census place/state and geoBoundaries Canadian province boundaries"

    def add_arguments(self, parser):
        parser.add_argument("--directory", default="data/reference/localities")

    def handle(self, *args, **options):
        directory = Path(options["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        manifest_file = directory / "manifest.json"
        if manifest_file.exists():
            self.stdout.write(manifest_file.read_text())
            return
        client = FastDownloadClient(directory / "downloads")
        urls = {
            "us_states": "https://www2.census.gov/geo/tiger/TIGER2026/STATE/tl_2026_us_state.zip",
        }

        def fetch(item):
            label, url = item
            self.stdout.write(f"Fetching/reusing {label}")
            self.stdout.flush()
            path = client.fetch(url, timeout=240)
            metadata = json.loads(path.with_suffix(".json").read_text())
            shape = extract_shape(path, directory / label)
            return label, {**metadata, "url": url, "shape": str(shape)}

        with ThreadPoolExecutor(max_workers=3) as pool:
            files = dict(pool.map(fetch, urls.items()))
        with duckdb_connection(FastDownloadClient("data/sources")) as connection:
            connection.execute("SET memory_limit='4GB'")
            connection.execute("SET threads=2")
            connection.execute(
                "CREATE TABLE us_states_raw AS SELECT * FROM ST_Read(?)",
                [files["us_states"]["shape"]],
            )
            states = connection.execute("SELECT STATEFP,STUSPS FROM us_states_raw").fetchall()
            state_codes = {code: state for code, state in states}
            place_urls = {
                f"us_place_{code}": f"https://www2.census.gov/geo/tiger/TIGER2026/PLACE/tl_2026_{code}_place.zip"
                for code, state in states
                if state in SUBDIVISIONS["US"]
            }
            with ThreadPoolExecutor(max_workers=6) as pool:
                files.update(dict(pool.map(fetch, place_urls.items())))
            connection.execute(
                "CREATE TABLE regions (country VARCHAR,state VARCHAR,name VARCHAR,"
                "geometry GEOMETRY,source VARCHAR)"
            )
            connection.execute(
                "INSERT INTO regions SELECT 'US',STUSPS,NAME,"
                "ST_Transform(geom,'EPSG:4269','EPSG:4326',always_xy:=true),"
                "'US Census TIGER/Line 2026' FROM us_states_raw"
            )
            connection.execute(
                "CREATE TABLE cities (country VARCHAR,state VARCHAR,name VARCHAR,"
                "reference_id VARCHAR,kind VARCHAR,geometry GEOMETRY,source VARCHAR)"
            )
            for label, metadata in files.items():
                if label.startswith("us_place_"):
                    code = label.removeprefix("us_place_")
                    connection.execute(
                        """
                        INSERT INTO cities SELECT 'US',?,NAME,GEOID,'census_place',
                            ST_Transform(geom,'EPSG:4269','EPSG:4326',always_xy:=true),
                            'US Census TIGER/Line 2026' FROM ST_Read(?)
                    """,
                        [state_codes[code], metadata["shape"]],
                    )
            ca_metadata = client.json("https://www.geoboundaries.org/api/current/gbOpen/CAN/ADM1/")
            ca_file = client.fetch(ca_metadata["gjDownloadURL"], timeout=180)
            files["ca_regions"] = {
                "metadata": ca_metadata,
                "filename": str(ca_file),
                **json.loads(ca_file.with_suffix(".json").read_text()),
            }
            ca_features = json.loads(ca_file.read_text())["features"]
            connection.executemany(
                "INSERT INTO regions VALUES ('CA',?,?,ST_GeomFromGeoJSON(?),?)",
                [
                    (
                        locality_codes(f["properties"]["shapeName"], "CA")[0],
                        f["properties"]["shapeName"],
                        json.dumps(f["geometry"]),
                        "geoBoundaries CAN ADM1",
                    )
                    for f in ca_features
                ],
            )
            regions_file = directory / "regions.parquet"
            cities_file = directory / "cities.parquet"
            region_count = connection.execute(
                "COPY regions TO ? (FORMAT PARQUET)", [str(regions_file)]
            ).fetchone()[0]
            city_count = connection.execute(
                "COPY cities TO ? (FORMAT PARQUET)", [str(cities_file)]
            ).fetchone()[0]
        manifest = {
            "prepared_at": datetime.now(UTC).isoformat(),
            "version": "locality-enrichment-1.0",
            "regions_file": str(regions_file),
            "cities_file": str(cities_file),
            "region_count": region_count,
            "city_count": city_count,
            "files": files,
            "ca_note": "Province polygons; Canadian city fills use bounded locality estimates",
        }
        write_json(manifest_file, manifest)
        self.stdout.write(json.dumps({"regions": region_count, "cities": city_count}))
