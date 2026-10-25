"""Auditable locality inference; original provider payloads are immutable."""

from copy import deepcopy

from django.db import connection

from .normalization import BOUNDARY_CITY_WARNING, NEAREST_CITY_WARNING, locality_codes, text_key

VERSION = "locality-enrichment-1.0"
NEAREST_WARNING = NEAREST_CITY_WARNING


def audit_sources():
    with connection.cursor() as cursor:
        cursor.execute("""
            WITH fields AS (
              SELECT *,
                CASE provider WHEN 'osm' THEN raw #>> '{tags,addr:city}'
                  WHEN 'alltheplaces' THEN raw #>> '{properties,addr:city}'
                  ELSE coalesce(raw #>> '{properties,addresses,0,locality}',
                                raw #>> '{addresses,0,locality}') END AS native_city,
                CASE provider WHEN 'osm' THEN raw #>> '{tags,addr:state}'
                  WHEN 'alltheplaces' THEN raw #>> '{properties,addr:state}'
                  ELSE coalesce(raw #>> '{properties,addresses,0,region}',
                                raw #>> '{addresses,0,region}') END AS native_state
              FROM geocoding_sourceplace
            ) SELECT provider,COUNT(*) AS total,
              COUNT(*) FILTER (WHERE btrim(coalesce(native_city,''))='') AS native_city_missing,
              COUNT(*) FILTER (WHERE btrim(coalesce(native_state,''))='') AS native_state_missing,
              COUNT(*) FILTER (WHERE city_key='') AS indexed_city_missing,
              COUNT(*) FILTER (WHERE state='') AS indexed_state_missing,
              COUNT(*) FILTER (WHERE city_key='' AND state='') AS indexed_both_missing,
              COUNT(*) FILTER (WHERE latitude IS NULL) AS coordinates_missing,
              COUNT(*) FILTER (WHERE (city_key='' OR state='') AND latitude IS NULL)
                AS missing_locality_without_coordinates
            FROM fields GROUP BY provider ORDER BY provider
        """)
        return {
            row[0]: dict(zip([column[0] for column in cursor.description], row, strict=True))
            for row in cursor.fetchall()
        }


def enrich_record(record, mapping, performed_at):
    """Fill only empty canonical/index fields; never replace known provider values."""
    if (
        record.get("latitude") is None
        or record.get("longitude") is None
        or not mapping
        or not mapping.get("country")
        or not mapping.get("state")
    ):
        return None
    known_state, known_country = locality_codes(record["state"], record["country"])
    if known_country and known_country != mapping["country"]:
        return None
    if known_state and known_state != mapping["state"]:
        return None
    updated = deepcopy(record)
    address = updated["address"]
    filled = {}
    for key in ("country", "state", "city"):
        index_key = "city_key" if key == "city" else key
        value = mapping.get(key)
        if value and not record[index_key] and not address.get(key):
            value = text_key(value) if key == "city" else value
            if not value:
                continue
            address[key] = value
            updated[index_key] = value
            method = mapping["city_method"] if key == "city" else "point_in_polygon"
            filled[key] = {
                "value": value,
                "method": method,
                "reference_id": mapping.get("city_reference_id")
                if key == "city"
                else f"{mapping['country']}-{mapping['state']}",
                "source": mapping.get("city_source") if key == "city" else mapping["region_source"],
            }
            if key == "city" and method == "nearest_settlement":
                filled[key]["distance_m"] = mapping["city_distance_m"]
                filled[key]["reference_latitude"] = mapping["reference_latitude"]
                filled[key]["reference_longitude"] = mapping["reference_longitude"]
                if NEAREST_WARNING not in address["warnings"]:
                    address["warnings"].append(NEAREST_WARNING)
            elif key == "city" and BOUNDARY_CITY_WARNING not in address["warnings"]:
                address["warnings"].append(BOUNDARY_CITY_WARNING)
    if not filled:
        return None
    updated["provenance"].append(
        {
            "dataset": "locality_enrichment",
            "version": VERSION,
            "performed_at": performed_at,
            "original": {
                "city": record["city_key"],
                "state": record["state"],
                "country": record["country"],
            },
            "filled_fields": filled,
        }
    )
    updated["filled_fields"] = filled
    return updated


def original_locality_address(address, provenance):
    """Recover the pre-enrichment address when auditing an older saved run."""
    restored = deepcopy(address)
    original = next(
        (p.get("original") for p in provenance if p.get("dataset") == "locality_enrichment"), None
    )
    if original:
        for key in ("city", "state", "country"):
            restored[key] = original[key]
        restored["warnings"] = [
            w
            for w in restored.get("warnings", [])
            if w not in {NEAREST_CITY_WARNING, BOUNDARY_CITY_WARNING}
        ]
    return restored
