"""Cached, versioned acquisition of published place data. No public Nominatim batching."""

import hashlib
import json
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from .normalization import brand_key, subdivision

COUNTRIES = ("US", "CA")
FUEL_CATEGORIES = ("gas_station", "truck_stop", "fuel_station", "petrol_station")
# The project feed contains continental US/Canada locations. This broad envelope
# also includes Alaska and Hawaii; callers can provide an explicit alternative.
NORTH_AMERICA_BBOX = (-180.0, 15.0, -50.0, 85.0)


def json_value(value):
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported exported value: {type(value).__name__}")


class DownloadClient:
    def __init__(self, directory: str | Path, user_agent="truckroute/0.1 local research pipeline"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent

    def json(self, url: str) -> dict:
        request = Request(url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    def fetch(self, url: str, *, data: bytes | None = None, timeout=240) -> Path:
        key = hashlib.sha256(url.encode() + (data or b"")).hexdigest()
        destination = self.directory / key
        if destination.exists():
            return destination
        headers = {"User-Agent": self.user_agent}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers)
        temporary = destination.with_suffix(".part")
        try:
            with urlopen(request, timeout=timeout) as response, temporary.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
                resolved_url = response.url
            temporary.replace(destination)
            with destination.open("rb") as completed:
                digest = hashlib.file_digest(completed, "sha256").hexdigest()
            destination.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "url": url,
                        "resolved_url": resolved_url,
                        "sha256": digest,
                    },
                    indent=2,
                )
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination


def atp_feature_relevant(feature: dict, brands: set[str]) -> bool:
    props = feature.get("properties") or {}
    _, country = subdivision(props.get("addr:state") or "", props.get("addr:country") or "")
    if country and country not in COUNTRIES:
        return False
    name = (props.get("brand") or props.get("name") or "").partition("#")[0]
    return props.get("amenity") == "fuel" or brand_key(name) in brands


def fetch_alltheplaces(
    client: DownloadClient, brands: set[str], progress=print, *, spiders: list[str] | None = None
) -> dict:
    import ijson

    metadata = client.json("https://data.alltheplaces.xyz/runs/latest.json")
    run_id, url = str(metadata["run_id"]), metadata["output_url"]
    if urlparse(url).scheme != "https":
        raise ValueError("ATP output URL must use HTTPS")
    if spiders is not None:
        spiders = sorted(set(spiders))
        if not spiders or any(not re.fullmatch(r"[A-Za-z0-9_]+", spider) for spider in spiders):
            raise ValueError("Provide a nonempty list of valid ATP spider names")
    token = hashlib.sha256(json.dumps([run_id, sorted(brands), spiders]).encode()).hexdigest()[:20]
    target = client.directory / f"atp-{token}.ndjson"
    empty_members = 0
    progress(f"AllThePlaces run {run_id}; archive bytes: {metadata.get('size_bytes', 'unknown')}")
    if not target.exists():
        temporary = target.with_suffix(".part")
        try:
            with temporary.open("w") as output:
                if spiders is not None:
                    # Pin every file to the concrete run, rather than mixing the latest
                    # successful run for each spider into a falsely uniform release.
                    for index, spider in enumerate(spiders, start=1):
                        source_url = urljoin(url, f"output/{spider}.geojson")
                        progress(f"AllThePlaces spider {index}/{len(spiders)}: {spider}")
                        filename = client.fetch(source_url)
                        if filename.stat().st_size == 0:
                            empty_members += 1
                            continue
                        with filename.open("rb") as stream:
                            for feature in ijson.items(stream, "features.item", use_float=True):
                                if atp_feature_relevant(feature, brands):
                                    output.write(json.dumps(feature) + "\n")
                else:
                    archive_path = client.fetch(url, timeout=600)
                    with zipfile.ZipFile(archive_path) as archive:
                        for member in archive.infolist():
                            if not member.filename.endswith(".geojson"):
                                continue
                            if member.file_size == 0:
                                empty_members += 1
                                continue
                            # Read directly; never extract archive paths into the filesystem.
                            with archive.open(member) as stream:
                                for feature in ijson.items(stream, "features.item", use_float=True):
                                    if atp_feature_relevant(feature, brands):
                                        output.write(json.dumps(feature) + "\n")
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return {
        "provider": "alltheplaces",
        "release": run_id,
        "filename": str(target),
        "format": "ndjson",
        "metadata": metadata,
        "filter_brands": sorted(brands),
        "spiders": spiders,
        "empty_members_skipped": empty_members,
    }


def overpass_query(country: str) -> str:
    if country not in COUNTRIES:
        raise ValueError("Only US and CA country extracts are supported")
    return (
        f'[out:json][timeout:180];area["ISO3166-1"="{country}"]'
        '["admin_level"="2"]->.scope;nwr["amenity"="fuel"](area.scope);out body center;'
    )


def fetch_osm(client: DownloadClient, endpoint: str, progress=print) -> dict:
    import ijson

    if urlparse(endpoint).scheme not in {"http", "https"}:
        raise ValueError("Invalid Overpass endpoint")
    files, timestamps = [], []
    for index, country in enumerate(COUNTRIES):
        if index:
            time.sleep(5)
        progress(f"OpenStreetMap: requesting cached {country} fuel extract")
        filename = client.fetch(
            endpoint, data=urlencode({"data": overpass_query(country)}).encode()
        )
        with filename.open("rb") as stream:
            remark = next(ijson.items(stream, "remark"), "")
        if remark:
            raise ValueError(f"Overpass did not complete the {country} extract: {remark}")
        with filename.open("rb") as stream:
            timestamp = next(ijson.items(stream, "osm3s.timestamp_osm_base"), "unknown")
        timestamps.append(str(timestamp))
        files.append({"filename": str(filename), "country": country, "timestamp": str(timestamp)})
    return {
        "provider": "osm",
        "release": ";".join(timestamps),
        "format": "overpass",
        "files": files,
        "endpoint": endpoint,
    }


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    from math import isfinite

    try:
        west, south, east, north = [float(part) for part in value.split(",")]
    except ValueError as exc:
        raise ValueError("Bounding box must be west,south,east,north") from exc
    if not (
        all(isfinite(v) for v in (west, south, east, north))
        and -180 <= west < east <= 180
        and -90 <= south < north <= 90
    ):
        raise ValueError("Invalid WGS84 bounding box")
    return west, south, east, north


def bbox_intersects(left, right) -> bool:
    return (
        left[0] <= right[2] and left[2] >= right[0] and left[1] <= right[3] and left[3] >= right[1]
    )


def overture_plan(collection: dict, items: list[dict], bbox, *, provider="aws") -> dict:
    parts = []
    for item in items:
        if item.get("bbox") and not bbox_intersects(item["bbox"], bbox):
            continue
        asset = item.get("assets", {}).get(provider)
        if not asset or urlparse(asset.get("href", "")).scheme != "https":
            raise ValueError(f"STAC item {item.get('id')} has no {provider} HTTPS asset")
        parts.append(
            {
                "id": item["id"],
                "url": asset["href"],
                "bbox": item.get("bbox"),
                "bytes": asset.get("file:size"),
                "rows": item.get("properties", {}).get("num_rows"),
            }
        )
    if not parts:
        raise ValueError("No Overture Parquet files intersect the requested bounding box")
    return {
        "bbox": list(bbox),
        "global_rows": collection.get("table:row_count") or collection.get("features"),
        "global_file_count": len(items),
        "global_parquet_bytes": sum(
            item.get("assets", {}).get(provider, {}).get("file:size", 0) for item in items
        ),
        "selected_file_count": len(parts),
        "selected_parquet_bytes": sum(part["bytes"] or 0 for part in parts),
        "parts": parts,
    }


def overture_sql(columns: set[str], brands: set[str], *, bbox=None) -> tuple[str, list]:
    required = {"id", "geometry", "names", "addresses", "bbox"}
    if not required <= columns:
        raise ValueError(f"Unsupported Overture schema, missing: {sorted(required - columns)}")
    categories = []
    for parent in ("taxonomy", "categories"):
        if parent in columns:
            categories.append(f"list_contains(?, {parent}.primary)")
    if "basic_category" in columns:
        categories.append("list_contains(?, basic_category)")
    params = [list(FUEL_CATEGORIES) for _ in categories]
    if "brand" in columns and brands:
        categories.append("list_contains(?, upper(brand.names.primary))")
        params.append(sorted(brands))
    if not categories:
        raise ValueError("Overture has no recognized fuel category or brand fields")
    fields = [
        field
        for field in (
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
        if field in columns
    ]
    spatial_predicate = ""
    spatial_params = []
    if bbox is not None:
        west, south, east, north = bbox
        # Direct comparisons on covering columns allow Parquet row-group pruning.
        spatial_predicate = "bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ? AND "
        spatial_params = [west, east, south, north]
    query = (
        f"SELECT {', '.join(fields)}, ST_AsGeoJSON(geometry) AS geometry_json "
        "FROM read_parquet(?) WHERE list_contains(?, addresses[1].country) AND "
        + spatial_predicate
        + "("
        + " OR ".join(categories)
        + ")"
    )
    # The path and countries occur before category placeholders in the SQL statement.
    return query, [list(COUNTRIES), *spatial_params, *params]


def fetch_overture(
    client: DownloadClient, brands: set[str], progress=print, *, bbox=NORTH_AMERICA_BBOX
) -> dict:
    import duckdb

    catalog = client.json("https://stac.overturemaps.org/catalog.json")
    release = catalog["latest"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release):
        raise ValueError("Invalid release identifier in Overture STAC catalog")
    bbox = parse_bbox(",".join(str(part) for part in bbox))
    query_hash = hashlib.sha256(json.dumps([sorted(brands), bbox, "stac-v2"]).encode()).hexdigest()[
        :12
    ]
    target = client.directory / f"overture-{release}-{query_hash}.ndjson"
    checkpoints = client.directory / f"overture-{release}-{query_hash}-checkpoints"
    checkpoints.mkdir(exist_ok=True)
    plan_file = checkpoints / "plan.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
    else:
        collection_url = f"https://stac.overturemaps.org/{release}/places/place/collection.json"
        collection = json.loads(client.fetch(collection_url).read_text())
        links = [
            urljoin(collection_url, link["href"])
            for link in collection["links"]
            if link["rel"] == "item"
        ]

        def read_item(url):
            return json.loads(client.fetch(url).read_text())

        with ThreadPoolExecutor(max_workers=6) as executor:
            items = list(executor.map(read_item, links))
        plan = overture_plan(collection, items, bbox)
        plan_file.write_text(json.dumps(plan, indent=2) + "\n")
    if not target.exists():
        progress(
            f"Overture {release}: selected {plan['selected_file_count']}/"
            f"{plan['global_file_count']} spatially relevant files; "
            f"{plan['selected_parquet_bytes']:,} Parquet container bytes"
        )
        extensions = client.directory / "duckdb-extensions"
        extensions.mkdir(exist_ok=True)
        with duckdb.connect(
            config={"extension_directory": str(extensions.resolve())}
        ) as connection:
            connection.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
            connection.execute("SET s3_region='us-west-2'")
            connection.execute("SET threads=8")
            connection.execute("SET memory_limit='2GB'")
            connection.execute("SET enable_http_metadata_cache=true")
            connection.execute("SET http_timeout=30")
            remote = plan["parts"][0]["url"]
            schema = connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [remote]
            ).fetchall()
            sql, params = overture_sql({row[0] for row in schema}, brands, bbox=bbox)
            total = 0
            for index, part in enumerate(plan["parts"], start=1):
                # Hash the ID instead of trusting remote metadata as a filesystem path.
                token = hashlib.sha256(str(part["id"]).encode()).hexdigest()[:16]
                chunk = checkpoints / f"{token}.ndjson"
                if chunk.exists():
                    progress(f"Overture file {index}/{len(plan['parts'])}: reusing {part['id']}")
                    continue
                started = time.monotonic()
                progress(f"Overture file {index}/{len(plan['parts'])}: querying {part['id']}")
                cursor = connection.execute(sql, [part["url"], *params])
                names = [field[0] for field in cursor.description]
                partial = chunk.with_suffix(".part")
                count = 0
                try:
                    with partial.open("w") as stream:
                        while rows := cursor.fetchmany(2000):
                            for row in rows:
                                props = dict(zip(names, row, strict=True))
                                geometry = json.loads(props.pop("geometry_json"))
                                feature = {
                                    "type": "Feature",
                                    "id": props["id"],
                                    "geometry": geometry,
                                    "properties": props,
                                }
                                stream.write(json.dumps(feature, default=json_value) + "\n")
                                count += 1
                            progress(
                                f"Overture file {index}/{len(plan['parts'])}: "
                                f"{count:,} candidates; total new {total + count:,}"
                            )
                    partial.replace(chunk)
                    chunk.with_suffix(".json").write_text(
                        json.dumps(
                            {
                                "file_id": part["id"],
                                "records": count,
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    total += count
                except Exception:
                    partial.unlink(missing_ok=True)
                    raise
            temporary = target.with_suffix(".part")
            try:
                with temporary.open("wb") as output:
                    for part in plan["parts"]:
                        token = hashlib.sha256(str(part["id"]).encode()).hexdigest()[:16]
                        with (checkpoints / f"{token}.ndjson").open("rb") as stream:
                            shutil.copyfileobj(stream, output)
                temporary.replace(target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
    return {
        "provider": "overture",
        "release": release,
        "filename": str(target),
        "format": "ndjson",
        "catalog": catalog,
        "plan": plan,
        "checkpoints": str(checkpoints),
    }
