"""Resumable acquisition with bounded concurrency and native Parquet exports."""

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

from .downloads import (
    NORTH_AMERICA_BBOX,
    DownloadClient,
    atp_feature_relevant,
    overture_plan,
    overture_sql,
)
from .normalization import brand_key


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


class FastDownloadClient(DownloadClient):
    """curl supplies bounded connect time, retries, compression and transfer resumption."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._transfers = {}
        self._transfer_lock = threading.Lock()

    def transfer_progress(self):
        with self._transfer_lock:
            active = list(self._transfers.values())
        result = []
        for url, path, size in active:
            try:
                received = path.stat().st_size
            except FileNotFoundError:
                received = 0
            result.append({"url": url, "received_bytes": received, "expected_bytes": size})
        return result

    def json(self, url):
        result = subprocess.run(
            [
                "curl",
                "-fLsS",
                "--compressed",
                "--connect-timeout",
                "8",
                "--max-time",
                "60",
                "--retry",
                "2",
                "-A",
                self.user_agent,
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def fetch(self, url, *, data=None, timeout=300, expected_bytes=None, sha256=None):
        key = hashlib.sha256(url.encode() + (data or b"")).hexdigest()
        destination = self.directory / key
        if destination.exists():
            if expected_bytes is None or destination.stat().st_size == expected_bytes:
                metadata_path = destination.with_suffix(".json")
                metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
                if sha256 is None or metadata.get("sha256") == sha256:
                    return destination
            raise ValueError(f"Cached file failed integrity check: {destination}")
        partial = destination.with_suffix(".part")
        command = [
            "curl",
            "-fLsS",
            "--connect-timeout",
            "8",
            "--max-time",
            str(timeout),
            "--retry",
            "2",
            "--retry-delay",
            "2",
            "-A",
            self.user_agent,
            "-o",
            str(partial),
            "-w",
            "%{url_effective}",
        ]
        if data is None:
            command += ["-C", "-"]
        else:
            command += [
                "--data-binary",
                "@-",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
            ]
        already_transferred = (
            expected_bytes is not None
            and partial.exists()
            and partial.stat().st_size == expected_bytes
        )
        if already_transferred:
            resolved_url = url
        else:
            with self._transfer_lock:
                self._transfers[url] = (url, partial, expected_bytes)
            try:
                result = subprocess.run(command + [url], input=data, capture_output=True)
            finally:
                with self._transfer_lock:
                    self._transfers.pop(url, None)
            if result.returncode:
                raise OSError(result.stderr.decode(errors="replace").strip())
            resolved_url = result.stdout.decode().strip()
        if expected_bytes is not None and partial.stat().st_size != expected_bytes:
            raise ValueError(f"Truncated download: {url}")
        with partial.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if sha256 is not None and digest != sha256:
            raise ValueError(f"SHA256 mismatch: {url}")
        write_json(
            destination.with_suffix(".json"),
            {
                "url": url,
                "resolved_url": resolved_url,
                "sha256": digest,
                "bytes": partial.stat().st_size,
            },
        )
        partial.replace(destination)
        return destination


def native_export(connection, sql, params, target):
    """Named binds avoid COPY destination's positional parameter ordering."""
    values = {f"p{i}": value for i, value in enumerate(params)}
    placeholders = iter(values)
    sql = re.sub(r"\?", lambda _: "$" + next(placeholders), sql)
    values["export_path"] = str(target)
    return connection.execute(f"COPY ({sql}) TO $export_path (FORMAT JSON)", values).fetchone()[0]


def duckdb_connection(client, *, remote=False):
    import duckdb

    extensions = (client.directory / "duckdb-extensions").resolve()
    extensions.mkdir(exist_ok=True)
    connection = duckdb.connect(config={"extension_directory": str(extensions)})
    connection.execute("LOAD spatial")
    if remote:
        connection.execute("LOAD httpfs")
        connection.execute("SET s3_region='us-west-2'")
        connection.execute("SET http_timeout=40")
        connection.execute("SET enable_http_metadata_cache=true")
    connection.execute("SET threads=4")
    connection.execute("SET memory_limit='1GB'")
    return connection


def combine_chunks(target, chunks):
    temporary = target.with_suffix(".part")
    with temporary.open("wb") as output:
        for chunk in chunks:
            with Path(chunk).open("rb") as stream:
                shutil.copyfileobj(stream, output)
    temporary.replace(target)


def acquire_overture(client, brands, progress, *, bbox=NORTH_AMERICA_BBOX, workers=2, mode="bulk"):
    catalog = client.json("https://stac.overturemaps.org/catalog.json")
    release = catalog["latest"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release):
        raise ValueError("Invalid Overture release")
    token = hashlib.sha256(json.dumps([release, sorted(brands), bbox, "native-v1"]).encode())
    directory = client.directory / f"overture-{release}-{token.hexdigest()[:12]}-native"
    directory.mkdir(exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    url = f"https://stac.overturemaps.org/{release}/places/place/collection.json"
    collection = json.loads(client.fetch(url).read_text())
    links = [urljoin(url, link["href"]) for link in collection["links"] if link["rel"] == "item"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        items = list(pool.map(lambda u: json.loads(client.fetch(u).read_text()), links))
    plan = overture_plan(collection, items, bbox)
    write_json(directory / "plan.json", plan)
    progress(f"{len(plan['parts'])}/{len(items)} partitions; {mode}; {workers} query workers")
    local_files = {}
    if mode == "bulk":

        def download(part):
            started = time.monotonic()
            progress(f"Downloading partition {part['id']}: {part['bytes'] / 1e6:.1f} MB")
            filename = client.fetch(part["url"], expected_bytes=part["bytes"], timeout=600)
            progress(f"Cached partition {part['id']} in {time.monotonic() - started:.1f}s")
            return part["id"], str(filename)

        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in as_completed([pool.submit(download, part) for part in plan["parts"]]):
                part_id, filename = future.result()
                local_files[part_id] = filename

    def export(part):
        chunk = directory / f"{part['id']}.ndjson"
        stats = chunk.with_suffix(".json")
        if chunk.exists() and stats.exists():
            return json.loads(stats.read_text())
        started = time.monotonic()
        progress(f"Querying partition {part['id']}")
        with duckdb_connection(client, remote=mode == "remote") as connection:
            parquet = local_files.get(part["id"], part["url"])
            schema = connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [parquet]
            ).fetchall()
            columns = {row[0] for row in schema}
            sql, params = overture_sql(columns, brands, bbox=bbox)
            fields = [
                name
                for name in (
                    "id",
                    "names",
                    "brand",
                    "addresses",
                    "sources",
                    "confidence",
                    "operating_status",
                    "basic_category",
                    "taxonomy",
                    "categories",
                )
                if name in columns
            ]
            properties = ", ".join(f"{name}:=q.{name}" for name in fields)
            sql = (
                "SELECT 'Feature' AS type, q.id, q.geometry_json::JSON AS geometry, "
                f"struct_pack({properties}) AS properties FROM ({sql}) q"
            )
            partial = chunk.with_suffix(".part")
            count = native_export(connection, sql, [parquet, *params], partial)
            partial.replace(chunk)
        result = {
            "id": part["id"],
            "filename": str(chunk),
            "records": count,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        write_json(stats, result)
        progress(f"Partition {part['id']} complete: {count:,} rows in {result['elapsed_seconds']}s")
        return result

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(export, part) for part in plan["parts"]]):
            results.append(future.result())
    results.sort(key=lambda row: row["id"])
    target = directory / "places.ndjson"
    combine_chunks(target, [row["filename"] for row in results])
    manifest = {
        "provider": "overture",
        "release": release,
        "filename": str(target),
        "format": "ndjson",
        "plan": plan,
        "partitions": results,
        "download_mode": mode,
        "records": sum(row["records"] for row in results),
    }
    write_json(manifest_path, manifest)
    return manifest


ATP_ALIASES = {
    "pilot_flying_j": {"PILOT", "FLYING J", "FLYING J TRAVEL PLAZA", "ONE9"},
    "travelcenters_of_america_us": {"TA", "PETRO", "TA EXPRESS"},
    "kwik_trip": {"KWIK TRIP", "KWIK STAR"},
    "caseys_general_store_us": {"CASEYS"},
    "seven_eleven_ca_us": {"7 ELEVEN"},
    "allsups_yesway_us": {"ALLSUPS", "ALLSUPS CONVENIENCE STORE", "YESWAY"},
    "phillips_66_conoco_76": {"PHILLIPS 66", "CONOCO", "76"},
    "race_trac_us": {"RACETRAC", "RACEWAY"},
}


def select_atp_members(archive, brands):
    import ijson

    selected, empty, errors = [], 0, []
    for member in archive.infolist():
        if not member.filename.endswith(".geojson"):
            continue
        if not member.file_size:
            empty += 1
            continue
        stem = Path(member.filename).stem
        exact = brand_key(re.sub(r"_(us|ca)$", "", stem)) in brands
        alias = bool(ATP_ALIASES.get(stem, set()) & brands)
        try:
            with archive.open(member) as stream:
                first = next(
                    ijson.items(stream, "features.item", use_float=True, buf_size=8192), None
                )
            if exact or alias or (first and atp_feature_relevant(first, brands)):
                selected.append(member.filename)
        except (ValueError, ijson.JSONError) as exc:
            errors.append({"member": member.filename, "error": str(exc)[:300]})
    return selected, empty, errors


def acquire_atp(client, brands, progress, *, spiders=None):
    import ijson

    metadata = client.json("https://data.alltheplaces.xyz/runs/latest.json")
    release = str(metadata["run_id"])
    archive_url = metadata["output_url"]
    cached = client.directory / hashlib.sha256(archive_url.encode()).hexdigest()
    # Reuse the exact published archive when already present. A fresh install can
    # explicitly select spider files without downloading the full archive.
    archive = zipfile.ZipFile(cached) if cached.exists() else None
    empty, discovery_errors = 0, []
    if spiders is None:
        if archive is not None:
            members, empty, discovery_errors = select_atp_members(archive, brands)
            spiders = [Path(name).stem for name in members]
        else:
            stats = client.json(metadata["stats_url"])
            spiders = [
                row["spider"]
                for row in stats["results"]
                if brand_key(re.sub(r"_(us|ca)$", "", row["spider"])) in brands
                or ATP_ALIASES.get(row["spider"], set()) & brands
            ]
    spiders = sorted(set(spiders))
    if not spiders or any(not re.fullmatch(r"[a-zA-Z0-9_]+", s) for s in spiders):
        raise ValueError("No valid ATP spiders selected")
    token = hashlib.sha256(json.dumps([release, sorted(brands), spiders, "v2"]).encode())
    directory = client.directory / f"atp-{release}-{token.hexdigest()[:12]}-selected"
    directory.mkdir(exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        if archive:
            archive.close()
        return json.loads(manifest_path.read_text())
    transport = "cached archive" if archive else "individual downloads"
    progress(f"{len(spiders)} selected spiders; {transport}")
    failures, chunks, count = [], [], 0
    try:
        for index, spider in enumerate(spiders, 1):
            chunk = directory / f"{spider}.ndjson"
            stats_file = chunk.with_suffix(".json")
            if not (chunk.exists() and stats_file.exists()):
                partial = chunk.with_suffix(".part")
                try:
                    if archive:
                        stream = archive.open(f"output/{spider}.geojson")
                    else:
                        filename = client.fetch(urljoin(archive_url, f"output/{spider}.geojson"))
                        stream = filename.open("rb")
                    records = 0
                    with stream, partial.open("w") as output:
                        for feature in ijson.items(stream, "features.item", use_float=True):
                            if atp_feature_relevant(feature, brands):
                                output.write(json.dumps(feature) + "\n")
                                records += 1
                    partial.replace(chunk)
                    write_json(stats_file, {"records": records})
                except Exception as exc:
                    partial.unlink(missing_ok=True)
                    failures.append({"spider": spider, "error": str(exc)[:400]})
                    continue
            chunks.append(chunk)
            count += json.loads(stats_file.read_text())["records"]
            if index % 10 == 0:
                progress(f"{index}/{len(spiders)} spiders; {count:,} candidate rows")
    finally:
        if archive:
            archive.close()
    target = directory / "places.ndjson"
    combine_chunks(target, chunks)
    if not count:
        raise ValueError("ATP returned zero candidates")
    manifest = {
        "provider": "alltheplaces",
        "release": release,
        "format": "ndjson",
        "filename": str(target),
        "records": count,
        "spiders": spiders,
        "metadata": metadata,
        "empty_members_skipped": empty,
        "selection_method": "exact names, explicit aliases and first-feature fuel/brand sample",
        "coverage_note": (
            "Selected spiders; discovery does not guarantee an exhaustive fuel inventory"
        ),
        "discovery_errors": discovery_errors,
        "failed_spiders": failures,
        "acquisition_complete": not failures,
    }
    write_json(manifest_path, manifest)
    progress(f"Complete: {count:,} candidates; {len(failures)} failed selected spiders")
    return manifest


def acquire_osm(client, regions, progress, *, workers=6):
    """OSM-only mirror; retain original IDs, complete tags and snapshot provenance."""
    base = "https://parquetry.geomermaids.com/osm/"
    snapshots = client.json(base + "snapshots.json")
    snapshot = max(row["date"] for row in snapshots["snapshots"])
    token = hashlib.sha256(json.dumps([snapshot, sorted(regions), "fuel-v1"]).encode())
    directory = client.directory / f"osm-{snapshot}-{token.hexdigest()[:12]}"
    directory.mkdir(exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    progress(f"OSM mirror snapshot {snapshot}; {len(regions)} input states/provinces")

    def get_region(region):
        country, state = region.split("-", 1)
        url = f"{base}{snapshot}/country={country}/state={region}/"
        metadata = json.loads(client.fetch(url + "_manifest.json").read_text())
        files = []
        for theme in ("pois", "amenities_polygons"):
            stats = metadata["theme_stats"][theme]
            filename = client.fetch(
                url + theme + ".parquet", expected_bytes=stats["size_bytes"], sha256=stats["sha256"]
            )
            files.append(str(filename))
        return {"region": region, "metadata": metadata, "files": files, "url": url}

    regions_data = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(get_region, region) for region in sorted(regions)]):
            row = future.result()
            regions_data.append(row)
            progress(f"Downloaded {row['region']}: {len(regions_data)}/{len(regions)} regions")
    files, timestamps = [], set()
    with duckdb_connection(client) as connection:
        for region in sorted(regions_data, key=lambda row: row["region"]):
            timestamp = region["metadata"]["source_timestamp"]
            timestamps.add(timestamp)
            target = directory / f"{region['region']}.ndjson"
            count = native_export(
                connection,
                """
                SELECT osm_id AS id, osm_type AS type, tags,
                       CASE WHEN osm_type = 'node' THEN ST_Y(ST_Centroid(geometry)) END AS lat,
                       CASE WHEN osm_type = 'node' THEN ST_X(ST_Centroid(geometry)) END AS lon,
                       CASE WHEN osm_type <> 'node' THEN
                         struct_pack(lat:=ST_Y(ST_Centroid(geometry)),
                                     lon:=ST_X(ST_Centroid(geometry))) END AS center,
                       struct_pack(country:=country, state:=state_iso,
                         snapshot:=?, mirror:=?, coordinate_method:='centroid') AS query_context
                FROM read_parquet(?, hive_partitioning=false)
                WHERE amenity = 'fuel'
            """,
                [timestamp, region["url"], region["files"]],
                target.with_suffix(".part"),
            )
            target.with_suffix(".part").replace(target)
            files.append(
                {
                    "filename": str(target),
                    "country": region["region"].split("-")[0],
                    "region": region["region"],
                    "timestamp": timestamp,
                    "records": count,
                }
            )
    # A border feature can be present in more than one region. Keep one identity.
    target, seen, records = directory / "places.ndjson", set(), 0
    with target.with_suffix(".part").open("w") as output:
        for file in files:
            with Path(file["filename"]).open() as stream:
                for line in stream:
                    row = json.loads(line)
                    identity = (row["type"], row["id"])
                    if identity not in seen:
                        seen.add(identity)
                        output.write(line)
                        records += 1
    target.with_suffix(".part").replace(target)
    if not records:
        raise ValueError("OSM mirror returned zero fuel records")
    manifest = {
        "provider": "osm",
        "release": f"{snapshot};{';'.join(sorted(timestamps))}",
        "format": "ndjson",
        "filename": str(target),
        "records": records,
        "snapshot": snapshot,
        "source_timestamps": sorted(timestamps),
        "transport": "OSM GeoParquet mirror from Geofabrik PBFs",
        "mirror": base,
        "attribution": "© OpenStreetMap contributors, ODbL-1.0",
        "regions": files,
        "region_manifests": regions_data,
        "coverage_note": "Fuel nodes and polygonal ways/relations in input regions",
    }
    write_json(manifest_path, manifest)
    progress(f"Complete: {records:,} distinct OSM fuel objects")
    return manifest
