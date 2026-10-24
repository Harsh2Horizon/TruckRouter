"""Flat source comparisons alongside the accepted combined decision."""

import csv

from .matching import ALGORITHM_VERSION

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
    row = {
        "external_id": external_id,
        "name": query.raw_name,
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
    }
    for provider in PROVIDERS:
        match = result.get("per_source", {}).get(provider)
        if match is None:
            match = {"status": "unavailable", "reasons": ["source_not_acquired"]}
        point = match.get("matched_coordinate") or {}
        identity = match.get("matched_source") or {}
        for key in SOURCE_FIELDS:
            if key in {"latitude", "longitude"}:
                value = point.get(key, "")
            elif key == "source_id":
                value = identity.get("source_id", "")
            elif key == "reasons":
                value = ";".join(match.get("reasons", []))
            else:
                value = match.get(key, "")
            row[f"{provider}_{key}"] = value
    return row


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
