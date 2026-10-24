"""Map published provider formats into one contract without inventing store identifiers."""

from datetime import date

from .normalization import brand_key, country_code, subdivision
from .parsing import parse_address
from .schemas import Coordinate, PlaceCandidate


def point(geometry: dict | None) -> Coordinate | None:
    if not geometry:
        return None
    if geometry.get("type") != "Point":
        raise ValueError("Expected Point geometry; derive polygon centers explicitly upstream")
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        raise ValueError("Point requires [longitude, latitude]")
    return Coordinate(longitude=coordinates[0], latitude=coordinates[1])


def tagged_address(props: dict, *, ref_is_store_id: bool = False):
    street = props.get("addr:street_address") or props.get("addr:street", "")
    if props.get("addr:housenumber") and not props.get("addr:street_address"):
        street = f"{props['addr:housenumber']} {street}"
    address = parse_address(
        {
            "name": props.get("name") or props.get("brand") or props.get("operator") or "UNKNOWN",
            "address": street or props.get("addr:full", ""),
            "city": props.get("addr:city", ""),
            "state": props.get("addr:state", ""),
            "country": country_code(props.get("addr:country", "")),
            "postcode": props.get("addr:postcode", ""),
        }
    )
    if props.get("brand"):
        address.brand = brand_key(props["brand"])
    address.brand_wikidata = props.get("brand:wikidata", "")
    address.store_id = str(
        props.get("ref:store")
        or props.get("store_number")
        or address.store_id
        or (props.get("ref", "") if ref_is_store_id else "")
    )
    if props.get("addr:housenumber"):
        address.house_number = str(props["addr:housenumber"])
        address.street = props.get("addr:street") or address.street
    address.unit = props.get("addr:unit", "")
    return address


def closure_status(value) -> str:
    if value == "yes":
        return "permanently_closed"
    if value:
        try:
            return "permanently_closed" if date.fromisoformat(value) <= date.today() else "open"
        except (ValueError, TypeError):
            return "unknown"
    return "unknown"


def alltheplaces(feature: dict, release: str = "", *, ref_is_store_id=False) -> PlaceCandidate:
    props = feature["properties"]
    spider = props.get("@spider")
    if not spider or feature.get("id") is None:
        raise ValueError("AllThePlaces requires a feature id and @spider namespace")
    coordinate = point(feature.get("geometry"))
    return PlaceCandidate(
        provider="alltheplaces",
        namespace=spider,
        source_id=str(feature["id"]),
        release=release,
        address=tagged_address(props, ref_is_store_id=ref_is_store_id),
        coordinate=coordinate,
        precision="poi" if coordinate else "unknown",
        operating_status=closure_status(props.get("end_date")),
        provenance=[
            {
                "dataset": "alltheplaces",
                "spider": spider,
                "record_id": str(feature["id"]),
                "ref": props.get("ref"),
                "source_uri": props.get("@source_uri", ""),
                "ref_is_store_id": ref_is_store_id,
            }
        ],
        license="CC0-1.0",
        raw=feature,
    )


def overture(feature: dict, release: str = "") -> PlaceCandidate:
    props = feature.get("properties", feature)
    source_id = props.get("id") or feature.get("id")
    if not source_id:
        raise ValueError("Overture requires its place/GERS id")
    addresses = props.get("addresses") or [{}]
    preferred = addresses[0]
    names = props.get("names") or {}
    brand = props.get("brand") or {}
    brand_names = brand.get("names") or {}
    address = parse_address(
        {
            "name": names.get("primary") or brand_names.get("primary") or "UNKNOWN",
            "address": preferred.get("freeform") or "",
            "city": preferred.get("locality") or "",
            "state": preferred.get("region") or "",
            "country": preferred.get("country") or "",
            "postcode": preferred.get("postcode") or "",
        }
    )
    if brand_names.get("primary"):
        address.brand = brand_key(brand_names["primary"])
    address.brand_wikidata = brand.get("wikidata") or ""
    coordinate = point(feature.get("geometry"))
    provenance = props.get("sources") or []
    licenses = sorted({str(source["license"]) for source in provenance if source.get("license")})
    return PlaceCandidate(
        provider="overture",
        namespace="places",
        source_id=str(source_id),
        release=release,
        address=address,
        coordinate=coordinate,
        precision="poi" if coordinate else "unknown",
        operating_status=props.get("operating_status") or "unknown",
        existence_confidence=props.get("confidence"),
        provenance=provenance,
        license="; ".join(licenses) or "source-dependent; see Overture attribution",
        raw=feature,
    )


def osm(element: dict, release: str = "") -> PlaceCandidate:
    kind, source_id = element.get("type"), element.get("id")
    if kind not in {"node", "way", "relation"} or source_id is None:
        raise ValueError("OSM identity requires element type and id")
    tags = element.get("tags") or {}
    coords = (
        element
        if element.get("lat") is not None and element.get("lon") is not None
        else element.get("center") or {}
    )
    coordinate = (
        Coordinate(latitude=coords["lat"], longitude=coords["lon"])
        if "lat" in coords and "lon" in coords
        else None
    )
    is_fuel = tags.get("amenity") == "fuel"
    status = "unknown"
    if tags.get("disused:amenity") == "fuel" or tags.get("abandoned:amenity") == "fuel":
        status = "permanently_closed"
    address = tagged_address(tags)
    context = element.get("query_context") or {}
    if not address.country and context.get("country") in {"US", "CA"}:
        address.country = context["country"]
        address.warnings = [warning for warning in address.warnings if warning != "country_unknown"]
        address.warnings.append(
            "country_from_source_region"
            if context.get("mirror")
            else "country_from_overpass_query_area"
        )
    if not address.state and context.get("mirror") and context.get("state"):
        state, country = subdivision(context["state"], context.get("country", ""))
        if country == address.country:
            address.state = state
            address.warnings.append("state_from_source_region")
    return PlaceCandidate(
        provider="osm",
        namespace=kind,
        source_id=str(source_id),
        release=release,
        address=address,
        coordinate=coordinate,
        precision=("poi" if kind == "node" else "poi_center") if is_fuel else "unknown",
        operating_status=status,
        provenance=[
            {
                "dataset": "openstreetmap",
                "type": kind,
                "record_id": str(source_id),
                "query_context": context,
            }
        ],
        license="ODbL-1.0; © OpenStreetMap contributors",
        raw=element,
    )


def nominatim(record: dict, release: str = "") -> PlaceCandidate:
    names = record.get("namedetails") or {}
    extra = record.get("extratags") or {}
    address = record.get("address") or {}
    iso_state = next((v for k, v in address.items() if k.startswith("ISO3166-2-lvl")), "")
    tags = {
        **extra,
        "name": record.get("name") or names.get("name") or address.get("amenity") or "UNKNOWN",
        "brand": extra.get("brand") or names.get("brand") or "",
        "ref:store": extra.get("ref:store") or "",  # generic ref remains opaque
        "addr:housenumber": address.get("house_number", ""),
        "addr:street": address.get("road", ""),
        "addr:city": address.get("city") or address.get("town") or address.get("village") or "",
        "addr:state": iso_state or address.get("state", ""),
        "addr:country": address.get("country_code", ""),
        "addr:postcode": address.get("postcode", ""),
    }
    if record.get("type") == "fuel":
        tags["amenity"] = "fuel"
    candidate = osm(
        {
            "type": record.get("osm_type"),
            "id": record.get("osm_id"),
            "lat": record["lat"],
            "lon": record["lon"],
            "tags": tags,
        },
        release,
    )
    candidate.raw = record
    # Nominatim place_id is installation-specific: use osm_type/osm_id instead.
    return candidate


ADAPTERS = {"alltheplaces": alltheplaces, "overture": overture, "osm": osm, "nominatim": nominatim}
