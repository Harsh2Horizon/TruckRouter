"""Pure source selection and Parquet query planning for the three active adapters."""

from urllib.parse import urlparse

from .normalization import brand_key, subdivision

COUNTRIES = ("US", "CA")
FUEL_CATEGORIES = ("gas_station", "truck_stop", "fuel_station", "petrol_station")
NORTH_AMERICA_BBOX = (-180.0, 15.0, -50.0, 85.0)


def atp_feature_relevant(feature: dict, brands: set[str]) -> bool:
    props = feature.get("properties") or {}
    _, country = subdivision(props.get("addr:state") or "", props.get("addr:country") or "")
    if country and country not in COUNTRIES:
        return False
    name = (props.get("brand") or props.get("name") or "").partition("#")[0]
    return props.get("amenity") == "fuel" or brand_key(name) in brands


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
