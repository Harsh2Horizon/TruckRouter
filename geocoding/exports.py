"""Flat source comparisons alongside the accepted combined decision."""

import csv
from collections import Counter
from pathlib import Path

from .matching import ALGORITHM_VERSION, business_name

PROVIDERS = ("alltheplaces", "overture", "osm")
SOURCE_FIELDS = (
    "score",
    "latitude",
    "longitude",
    "status",
    "source_id",
    "matched_name",
    "matched_address",
    "matched_city",
    "matched_state",
    "evidence_coverage",
    "margin",
    "reasons",
    "name_similarity",
    "city_similarity",
    "state_match",
    "country_match",
    "locality_all_match",
    "highway_similarity",
    "exit_similarity",
    "name_points",
    "locality_points",
    "highway_points",
    "exit_points",
    "applicable_weight",
    "gates",
    "matched_country",
    "city_method",
)
PREDICTION_FIELDS = (
    "predicted_latitude",
    "predicted_longitude",
    "prediction_method",
    "prediction_precision",
    "prediction_source",
    "prediction_score",
    "is_approximate",
    "fallback_status",
    "fallback_reason",
    "fallback_highway",
    "fallback_exit",
    "fallback_city_distance_m",
    "geographic_score",
    "atp_overture_distance_m",
    "atp_osm_distance_m",
    "overture_osm_distance_m",
    "max_source_deviation_m",
    "deviation_severity",
    "deviation_notes",
)
HIGHEST_FIELDS = (
    "highest_matching_score",
    "highest_matching_source",
    "highest_matching_source_id",
    "highest_matching_latitude",
    "highest_matching_longitude",
    "highest_matching_status",
    "highest_matching_tied_sources",
)
BASE_FIELDS = (
    "external_id",
    "name",
    "business_name",
    "address",
    "highways",
    "exits",
    "city",
    "state",
    "country",
    "status",
    "score",
    "latitude",
    "longitude",
    "provider",
    "source_id",
    "proposed_latitude",
    "proposed_longitude",
    "proposed_provider",
    "proposed_source_id",
    "reasons",
    "algorithm_version",
    "run_id",
)
CSV_FIELDS = [
    *BASE_FIELDS,
    *HIGHEST_FIELDS,
    *PREDICTION_FIELDS,
    *(f"{provider}_{field}" for provider in PROVIDERS for field in SOURCE_FIELDS),
]


def comparison_row(external_id, query, result, run_id):
    coordinate = result.get("coordinate") or {}
    selected = result.get("selected_source") or {}
    proposal = result.get("proposed_source") or {}
    proposed_coordinate = (
        next((row["coordinate"] for row in result["candidates"] if row["source"] == proposal), None)
        or {}
    )
    highest = result.get("highest_matching_coordinate") or {}
    fallback = result.get("geographic_fallback") or {}
    predicted = coordinate or highest or result.get("approximate_coordinate") or {}
    if coordinate or highest:
        method = "fuel_poi"
        prediction_source = (
            selected.get("provider") if coordinate else result.get("highest_matching_source")
        )
        prediction_score = result.get("highest_matching_score", 0)
        if coordinate:
            chosen = next((c for c in result["candidates"] if c["source"] == selected), None)
            if chosen:
                prediction_score = chosen["evidence"]["score"]
    elif predicted:
        method, prediction_source = fallback.get("method", "highway_exit"), fallback.get("source")
        prediction_score = fallback.get("station_match_score", 0)
    else:
        method, prediction_source, prediction_score = "unresolved", "", 0
    disagreement = result.get("coordinate_disagreement") or {}
    pairs = disagreement.get("pairwise_meters") or {}
    row = {
        "external_id": external_id,
        "name": query.raw_name,
        "business_name": business_name(query),
        "address": query.raw_address,
        "highways": "; ".join(dict.fromkeys(road.raw for road in query.highways)),
        "exits": "; ".join(exit_ref.raw for exit_ref in query.exits),
        "city": query.city,
        "state": query.state,
        "country": query.country,
        "status": result["status"],
        "score": result["score"],
        "latitude": coordinate.get("latitude", ""),
        "longitude": coordinate.get("longitude", ""),
        "provider": selected.get("provider", ""),
        "source_id": selected.get("source_id", ""),
        "proposed_latitude": proposed_coordinate.get("latitude", ""),
        "proposed_longitude": proposed_coordinate.get("longitude", ""),
        "proposed_provider": proposal.get("provider", ""),
        "proposed_source_id": proposal.get("source_id", ""),
        "reasons": ";".join(result["reasons"]),
        "algorithm_version": result.get("algorithm_version", ALGORITHM_VERSION),
        "run_id": str(run_id),
        "highest_matching_score": result.get("highest_matching_score", 0),
        "highest_matching_source": result.get("highest_matching_source") or "",
        "highest_matching_source_id": result.get("highest_matching_source_id") or "",
        "highest_matching_latitude": highest.get("latitude", ""),
        "highest_matching_longitude": highest.get("longitude", ""),
        "highest_matching_status": result.get("highest_matching_status", "unmatched"),
        "highest_matching_tied_sources": ";".join(result.get("highest_matching_tied_sources", [])),
        "predicted_latitude": predicted.get("latitude", ""),
        "predicted_longitude": predicted.get("longitude", ""),
        "prediction_method": method,
        "prediction_precision": "poi"
        if method == "fuel_poi"
        else fallback.get("precision", "unknown"),
        "prediction_source": prediction_source or "",
        "prediction_score": prediction_score,
        "is_approximate": bool(predicted) and method != "fuel_poi",
        "fallback_status": fallback.get("status", ""),
        "fallback_reason": fallback.get("reason", ""),
        "fallback_highway": fallback.get("highway", ""),
        "fallback_exit": fallback.get("exit_number", ""),
        "fallback_city_distance_m": fallback.get("city_distance_m", ""),
        "geographic_score": fallback.get("geographic_score", ""),
        "atp_overture_distance_m": pairs.get("alltheplaces__overture", ""),
        "atp_osm_distance_m": pairs.get("alltheplaces__osm", ""),
        "overture_osm_distance_m": pairs.get("osm__overture", ""),
        "max_source_deviation_m": disagreement.get("max_distance_m", 0),
        "deviation_severity": disagreement.get("severity", "no_coordinates"),
        "deviation_notes": disagreement.get("possible_cause", ""),
    }
    for provider in PROVIDERS:
        match = result.get("per_source", {}).get(provider)
        if match is None:
            match = {"status": "unavailable", "reasons": ["source_not_acquired"]}
        point = match.get("matched_coordinate") or {}
        identity = match.get("matched_source") or {}
        criteria = match.get("criteria") or {}
        components = criteria.get("components") or {}
        locality = criteria.get("locality") or {}
        contributions = criteria.get("contributions") or {}
        breakdown = {
            "name_similarity": components.get("name", ""),
            "city_similarity": locality.get("city", ""),
            "state_match": locality.get("state", ""),
            "country_match": locality.get("country", ""),
            "locality_all_match": locality.get("complete", False),
            "highway_similarity": components.get("highways", ""),
            "exit_similarity": components.get("exits", ""),
            "name_points": contributions.get("name", ""),
            "locality_points": contributions.get("locality", ""),
            "highway_points": contributions.get("highways", ""),
            "exit_points": contributions.get("exits", ""),
            "applicable_weight": criteria.get("applicable_weight", ""),
            "gates": ";".join(criteria.get("conflicts", [])),
            "city_method": locality.get("candidate_city_method", ""),
        }
        for key in SOURCE_FIELDS:
            if key in breakdown:
                value = breakdown[key]
            elif key in {"latitude", "longitude"}:
                value = point.get(key, "")
            elif key == "source_id":
                value = identity.get("source_id", "")
            elif key == "reasons":
                value = ";".join(match.get("reasons", []))
            else:
                value = match.get(key, "")
            row[f"{provider}_{key}"] = value
    return row


SOURCE_COMPARISON_FIELDS = [
    "external_id",
    "input_name",
    "business_name",
    "input_address",
    "input_city",
    "input_state",
    "input_country",
    "input_highways",
    "input_exits",
    "provider",
    "source_id",
    "source_name",
    "source_brand",
    "source_city",
    "source_state",
    "source_country",
    "source_address",
    "source_highways",
    "source_exits",
    "compared_input_name",
    "compared_source_name",
    "name_similarity",
    "city_similarity",
    "state_match",
    "country_match",
    "locality_all_match",
    "highway_similarity",
    "exit_similarity",
    "name_points",
    "locality_points",
    "highway_points",
    "exit_points",
    "applicable_weight",
    "ungated_score",
    "score",
    "eligible_point",
    "predicted_latitude",
    "predicted_longitude",
    "status",
    "margin",
    "gates",
    "reasons",
    "precision",
    "release",
    "diagnostic_latitude",
    "diagnostic_longitude",
    "algorithm_version",
    "city_method",
]
FINAL_FIELDS = [
    "external_id",
    "name",
    "business_name",
    "address",
    "city",
    "state",
    "country",
    "highways",
    "exits",
    "status",
    *PREDICTION_FIELDS,
    *HIGHEST_FIELDS,
    *(
        f"{provider}_{field}"
        for provider in PROVIDERS
        for field in ("score", "latitude", "longitude", "status", "source_id", "matched_name")
    ),
    "latitude",
    "longitude",
    "reasons",
    "algorithm_version",
    "run_id",
]


def source_comparison_rows(external_id, query, result):
    """Exactly one audit row per requested source, including failed match diagnostics."""
    base = comparison_row(external_id, query, result, "")
    for provider in PROVIDERS:
        match = result.get("per_source", {}).get(provider, {})
        diagnostic = match.get("diagnostic_candidate") or {}
        identity = match.get("matched_source") or diagnostic.get("source")
        candidate = next((c for c in result["candidates"] if c["source"] == identity), {})
        address = candidate.get("address") or {}
        criteria = match.get("criteria") or {}
        names = criteria.get("name_match") or {}
        point = match.get("matched_coordinate") or {}
        diagnostic_point = diagnostic.get("coordinate") or {}
        row = {
            "external_id": external_id,
            "input_name": query.raw_name,
            "business_name": base["business_name"],
            "input_address": query.raw_address,
            "input_city": query.city,
            "input_state": query.state,
            "input_country": query.country,
            "input_highways": base["highways"],
            "input_exits": base["exits"],
            "provider": provider,
            "source_id": (identity or {}).get("source_id", ""),
            "source_name": address.get("raw_name", ""),
            "source_brand": address.get("brand", ""),
            "source_city": address.get("city", ""),
            "source_state": address.get("state", ""),
            "source_country": address.get("country", ""),
            "source_address": address.get("raw_address", ""),
            "source_highways": "; ".join(
                dict.fromkeys(r["raw"] for r in address.get("highways", []))
            ),
            "source_exits": "; ".join(e["raw"] for e in address.get("exits", [])),
            "compared_input_name": names.get("input_name", ""),
            "compared_source_name": names.get("candidate_name", ""),
            "ungated_score": criteria.get("ungated_score", ""),
            "eligible_point": bool(point),
            "predicted_latitude": point.get("latitude", ""),
            "predicted_longitude": point.get("longitude", ""),
            "precision": candidate.get("precision", ""),
            "release": (identity or {}).get("release", ""),
            "diagnostic_latitude": diagnostic_point.get("latitude", ""),
            "diagnostic_longitude": diagnostic_point.get("longitude", ""),
            "algorithm_version": ALGORITHM_VERSION,
        }
        for key in SOURCE_FIELDS:
            if key in SOURCE_COMPARISON_FIELDS and key != "source_id":
                row[key] = base[f"{provider}_{key}"]
        yield row


def write_analysis_exports(output, comparison_rows):
    output = Path(output)
    rows = list(comparison_rows.values())
    with (output / "final-stops.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FINAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    compared = [
        r for r in rows if r["deviation_severity"] not in {"no_coordinates", "single_source"}
    ]
    compared.sort(key=lambda r: -r["max_source_deviation_m"])
    with (output / "coordinate-disagreements.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FINAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(compared)
    return {
        "prediction_methods": dict(Counter(r["prediction_method"] for r in rows)),
        "fallback_reasons": dict(
            Counter(r["fallback_reason"] for r in rows if r["fallback_reason"])
        ),
        "coordinate_deviation": {
            "severity_counts": dict(Counter(r["deviation_severity"] for r in rows)),
            "compared_stops": len(compared),
            "flagged_at_1km": sum(r["max_source_deviation_m"] >= 1000 for r in compared),
            "severe_at_5km": sum(r["max_source_deviation_m"] >= 5000 for r in compared),
            "high_score_review_stops": sum(
                "cross_source_coordinates_diverge" in r["reasons"] for r in rows
            ),
            "largest_cases": [
                {
                    key: r[key]
                    for key in (
                        "external_id",
                        "name",
                        "city",
                        "state",
                        "max_source_deviation_m",
                        "deviation_severity",
                        "status",
                        "deviation_notes",
                    )
                }
                for r in compared[:20]
            ],
        },
        "export_rows": {
            "source_comparison": len(rows) * len(PROVIDERS),
            "final_stops": len(rows),
            "coordinate_comparisons": len(compared),
        },
    }


def enrich_input_csv(input_file, output_file, comparison_rows):
    """Preserve every original price row and append its location's comparison."""
    extra_fields = [
        name
        for name in CSV_FIELDS
        if name not in {"external_id", "name", "city", "state", "country"}
    ]
    count = 0
    with (
        open(input_file, newline="", encoding="utf-8-sig") as source,
        open(output_file, "w", newline="") as target,
    ):
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=[*reader.fieldnames, *extra_fields])
        writer.writeheader()
        for original in reader:
            comparison = comparison_rows[original["OPIS Truckstop ID"].strip()]
            writer.writerow({**original, **{key: comparison[key] for key in extra_fields}})
            count += 1
    return count
