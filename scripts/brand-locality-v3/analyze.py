"""Validate complete output coverage and report cross-source coordinate differences."""

# ruff: noqa: E501 -- Markdown prose and table rows are kept as complete strings.

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path

PROVIDERS = ("alltheplaces", "overture", "osm")
PAIR_FIELDS = ("atp_overture_distance_m", "atp_osm_distance_m", "overture_osm_distance_m")


def read(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def haversine(left, right):
    lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(a)))


def quantile(values, p):
    values = sorted(values)
    return round(values[round((len(values) - 1) * p)], 3) if values else None


def safe(value):
    return str(value).replace("|", "/").replace("\n", " ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="output/geocoding-v3")
    parser.add_argument("--input", default="fuel-prices-for-be-assessment.csv")
    args = parser.parse_args()
    output = Path(args.run)
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "completed"
    original, enriched = read(Path(args.input)), read(output / "fuel-prices-geocoded.csv")
    stops, sources = read(output / "final-stops.csv"), read(output / "source-comparison.csv")
    assert len(original) == len(enriched) == report["enriched_input_rows"]
    assert all(
        all(row[key] == result[key] for key in row)
        for row, result in zip(original, enriched, strict=True)
    )
    ids = {row["OPIS Truckstop ID"].strip() for row in original}
    assert len(stops) == len(ids) == report["locations_processed"]
    assert {row["external_id"] for row in stops} == ids
    assert len(sources) == len(stops) * 3
    assert len({(row["external_id"], row["provider"]) for row in sources}) == len(sources)
    assert all(row["provider"] in PROVIDERS for row in sources)
    for row in sources:
        predicted = row["predicted_latitude"] != ""
        assert predicted == (row["eligible_point"] == "True")
        assert predicted == (row["predicted_longitude"] != "")
        if predicted:
            assert row["locality_all_match"] == "True"
            assert float(row["name_similarity"]) >= 0.70
            assert float(row["state_match"]) == float(row["country_match"]) == 1
            assert float(row["city_similarity"]) >= 0.90
            assert not row["gates"]
            assert (
                abs(
                    sum(
                        float(row[k] or 0)
                        for k in ("name_points", "locality_points", "highway_points", "exit_points")
                    )
                    - float(row["score"])
                )
                <= 0.00051
            )
        else:
            assert float(row["score"]) == 0
    for row in stops:
        point = row["predicted_latitude"] != ""
        assert point == (row["predicted_longitude"] != "")
        if point:
            assert -90 <= float(row["predicted_latitude"]) <= 90
            assert -180 <= float(row["predicted_longitude"]) <= 180
        if row["status"] == "accepted":
            assert row["latitude"] and row["longitude"]
        else:
            assert not row["latitude"] and not row["longitude"]
        eligible = [p for p in PROVIDERS if row[f"{p}_latitude"]]
        if eligible:
            highest = max(float(row[f"{p}_score"]) for p in eligible)
            provider = row["highest_matching_source"]
            assert (
                highest == float(row["highest_matching_score"]) == float(row[f"{provider}_score"])
            )
            assert row["highest_matching_latitude"] == row[f"{provider}_latitude"]
            assert row["highest_matching_longitude"] == row[f"{provider}_longitude"]
        distances = []
        for (left, right), field in zip(
            (("alltheplaces", "overture"), ("alltheplaces", "osm"), ("overture", "osm")),
            PAIR_FIELDS,
            strict=True,
        ):
            if row[f"{left}_latitude"] and row[f"{right}_latitude"]:
                distance = haversine(
                    tuple(float(row[f"{left}_{c}"]) for c in ("latitude", "longitude")),
                    tuple(float(row[f"{right}_{c}"]) for c in ("latitude", "longitude")),
                )
                assert abs(distance - float(row[field])) <= 0.00051
                distances.append(distance)
            else:
                assert not row[field]
        assert abs(max(distances, default=0) - float(row["max_source_deviation_m"])) <= 0.00051
        if row["is_approximate"] == "True":
            assert row["status"] == "approximate" and not eligible
            assert row["prediction_method"] == "highway_exit"
            assert row["fallback_highway"] and row["fallback_exit"]
            assert float(row["prediction_score"]) <= 40
            assert float(row["fallback_city_distance_m"]) <= 20000
        if row["prediction_method"] == "unresolved":
            assert not point
    pair_stats = {}
    for field in PAIR_FIELDS:
        values = [float(row[field]) for row in stops if row[field]]
        pair_stats[field] = {
            "pairs": len(values),
            "median_m": quantile(values, 0.5),
            "p90_m": quantile(values, 0.9),
            "p95_m": quantile(values, 0.95),
            "max_m": max(values, default=0),
            "at_least_1km": sum(v >= 1000 for v in values),
            "at_least_5km": sum(v >= 5000 for v in values),
        }
    validation = {
        "passed": True,
        "distinct_stops": len(stops),
        "source_rows": len(sources),
        "original_price_rows_preserved": len(original),
        "pairwise_statistics": pair_stats,
        "status_counts": dict(Counter(row["status"] for row in stops)),
    }
    (output / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
    flagged = sorted(
        (r for r in stops if float(r["max_source_deviation_m"]) >= 1000),
        key=lambda r: -float(r["max_source_deviation_m"]),
    )
    lines = [
        "# Coordinate disagreement analysis — scoring v3",
        "",
        f"Completed {report['finished_at']}. Algorithm: `{report['algorithm_version']}`.",
        "",
        f"The run covers **{len(stops):,} stops** and preserves **{len(original):,} original price rows**. "
        f"Results: {', '.join(f'{count:,} {status}' for status, count in validation['status_counts'].items())}.",
        "",
        "## What is compared",
        "",
        "Distances are WGS84 great-circle distances between each source's best eligible fuel POI candidate. "
        "A point can still require branch review. Missing coordinates are excluded; diagnostic rejected records "
        "and approximate highway exits are excluded. No coordinates are averaged.",
        "",
        f"**{report['coordinate_deviation']['compared_stops']:,} stops** have at least two source points. "
        f"**{len(flagged):,}** have a maximum separation of at least 1 km; "
        f"**{report['coordinate_deviation']['severe_at_5km']:,}** reach 5 km. "
        f"**{report['coordinate_deviation']['high_score_review_stops']:,}** have a separation over 1 km "
        "between two candidates scoring at least 85, which prevents combined automatic acceptance.",
        "",
        "| Source pair | Compared stops | Median m | P90 m | P95 m | Maximum km | ≥1 km | ≥5 km |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for field, stats in pair_stats.items():
        lines.append(
            f"| {field.removesuffix('_distance_m')} | {stats['pairs']:,} | {stats['median_m']} | {stats['p90_m']} | {stats['p95_m']} | {stats['max_m'] / 1000:.2f} | {stats['at_least_1km']:,} | {stats['at_least_5km']:,} |"
        )
    lines += [
        "",
        "## Largest differences",
        "",
        "| OPIS ID | Input name | City / state | Maximum km | ATP score | Overture score | OSM score |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in flagged[:15]:
        lines.append(
            f"| {safe(row['external_id'])} | {safe(row['name'])} | {safe(row['city'])}, {row['state']} | {float(row['max_source_deviation_m']) / 1000:.2f} | {row['alltheplaces_score']} | {row['overture_score']} | {row['osm_score']} |"
        )
    lines += [
        "",
        "## How to interpret the flagged cases",
        "",
        "A large distance does not establish which source is wrong. Brand and city can identify several "
        "branches, especially when provider records omit highway and exit clues. The new policy deliberately "
        "removes store IDs and street/postal evidence, so those fields cannot break such ties. "
        "The likely branch ambiguity below is an inference from the matching evidence, not a verified error attribution.",
        "",
    ]
    coordinate_checks = {}
    diagnostics = output / "coordinate-state-diagnostics.csv"
    if diagnostics.exists():
        checks = read(diagnostics)
        coordinate_checks = {(r["external_id"], r["provider"]): r for r in checks}
        mismatches = [r for r in checks if r["coordinate_in_reported_state"] == "False"]
        lines += [
            "An additional geometric check compares US source points against the cached "
            "geoBoundaries state polygons. This is a diagnostic, not another score component. "
            f"It checked {len(checks):,} points and flagged {len(mismatches):,} outside their reported state. "
            "Small border differences can reflect boundary precision; the extreme cases below provide "
            "stronger evidence of coordinate/metadata inconsistency. This does not verify a fuel-station entrance.",
            "",
        ]
    index = {(r["external_id"], r["provider"]): r for r in sources}
    for row in flagged[:5]:
        lines += [
            f"### {safe(row['name'])} — {safe(row['city'])}, {row['state']} (OPIS {row['external_id']})",
            "",
            f"Input road/exit clues: `{row['address']}`. Maximum separation: **{float(row['max_source_deviation_m']) / 1000:.2f} km**. Combined status: `{row['status']}`.",
            "",
        ]
        for provider in PROVIDERS:
            s = index[(row["external_id"], provider)]
            if s["predicted_latitude"]:
                lines.append(
                    f"- {provider}: {safe(s['source_name'])}; ({s['predicted_latitude']}, {s['predicted_longitude']}); score {s['score']}; name {float(s['name_similarity']):.1%}; city {float(s['city_similarity']):.1%}; highway {s['highway_similarity'] or 'not applicable'}; exit {s['exit_similarity'] or 'not applicable'}; source address `{s['source_address']}`; record `{s['source_id']}`."
                )
                check = coordinate_checks.get((row["external_id"], provider))
                if check and check["coordinate_in_reported_state"] == "False":
                    lines.append(
                        f"  - Geometric diagnostic: this point lies in **{check['coordinate_state'] or 'no covered US state'}**, "
                        f"while its source address reports **{check['source_state']}**. "
                        "The coordinate contradicts the source locality metadata in this reference."
                    )
        lines += [
            "",
            "Review the source records and road/exit access before assigning a unique branch. The highest-score point remains a candidate with its review status.",
            "",
        ]
    lines += [
        "## Approximate fallback and remaining coverage",
        "",
        f"Prediction methods: `{json.dumps(report['prediction_methods'], sort_keys=True)}`.",
        "",
        "Fallback uses real OpenInterstate/OSM interstate-exit points with route membership, state polygon "
        "containment and a named city anchor within 20 km. It marks the station location approximate. "
        "It currently covers US signed interstates; Canadian, state-route and other unsupported exits "
        "remain unresolved when no eligible named POI exists. Ambiguous interchanges are also left unresolved.",
        "",
        "Fallback reason counts:",
        "",
        "| Reason | Stops |",
        "|---|---:|",
    ]
    lines += [
        f"| {reason} | {count:,} |" for reason, count in sorted(report["fallback_reasons"].items())
    ]
    lines += [
        "",
        "## Files and validation",
        "",
        "- `output/geocoding-v3/source-comparison.csv`: all three sources, all criterion scores, accepted and rejected-candidate diagnostics.",
        "- `output/geocoding-v3/final-stops.csv`: one final prediction per stop, statuses and source coordinates.",
        "- `output/geocoding-v3/coordinate-disagreements.csv`: all stops with at least two source points, largest differences first.",
        "- `output/geocoding-v3/validation.json`: coverage, preserved prices, coordinate bounds, score contributions and independently recomputed distances.",
        "- `outputs/truckroute-brand-locality-v3/source-matching-analysis.xlsx`: matching criteria and flagged coordinate differences.",
        "- `outputs/truckroute-brand-locality-v3/final-geocoded-locations.xlsx`: distinct stops and original price observations.",
        "",
        "The validation script passed for every output row. Exact coordinates still require external ground truth; "
        "agreement between sources is not independent corroboration when datasets share upstream lineage.",
        "",
        "References: [OpenInterstate](https://github.com/tldev/openinterstate), "
        "[OSM mirror](https://github.com/gsueur/osm-geoparquet), "
        "[Overture Places](https://docs.overturemaps.org/guides/places/), "
        "[AllThePlaces](https://www.alltheplaces.xyz/).",
        "",
    ]
    workbook_validation = output / "workbook-validation.json"
    if workbook_validation.exists():
        verified = json.loads(workbook_validation.read_text())
        if verified.get("passed"):
            lines += [
                "Both exported XLSX workbooks also passed read-only verification against the CSVs: "
                f"{verified['source_rows']:,} source rows, {verified['final_stops']:,} final stops and "
                f"{verified['original_price_rows']:,} price observations. Native ZIP integrity, formulas, "
                "cached criterion scores, source coordinates, summaries and absence of spreadsheet error cells were checked. "
                "Every worksheet was rendered and visually inspected.", "",
            ]
    Path("docs/coordinate-disagreement-analysis-v3.md").write_text("\n".join(lines))
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
