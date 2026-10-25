"""Prepare typed workbook inputs from a completed, auditable geocoding run."""

import argparse
import csv
import json
from pathlib import Path


def read_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="output/geocoding-v3")
    parser.add_argument("--output", default="outputs/truckroute-brand-locality-v3/.build/data.json")
    args = parser.parse_args()
    run = Path(args.run)
    report = json.loads((run / "report.json").read_text())
    if report["status"] != "completed":
        raise ValueError("Workbooks require a completed run")
    payload = {
        "report": {
            key: report[key]
            for key in (
                "algorithm_version",
                "started_at",
                "finished_at",
                "geocodes",
                "per_source",
                "source_candidate_coordinates",
                "prediction_methods",
                "coordinate_deviation",
                "locations_processed",
                "enriched_input_rows",
                "scoring",
                "fallback_reasons",
            )
        },
        "sources": read_rows(run / "source-comparison.csv"),
        "stops": read_rows(run / "final-stops.csv"),
        "prices": read_rows(run / "fuel-prices-geocoded.csv"),
        "disagreements": [
            r
            for r in read_rows(run / "coordinate-disagreements.csv")
            if float(r["max_source_deviation_m"]) >= 1000
        ],
    }
    diagnostics = run / "coordinate-state-diagnostics.csv"
    checks = read_rows(diagnostics) if diagnostics.exists() else []
    by_source = {(r["external_id"], r["provider"]): r for r in checks}
    warnings = {}
    for row in checks:
        if row["coordinate_in_reported_state"] == "False":
            actual_state = row["coordinate_state"] or "no covered US state"
            warning = (
                f"{row['provider']}: coordinate in {actual_state}; "
                f"address reports {row['source_state']}"
            )
            warnings.setdefault(row["external_id"], []).append(warning)
    payload["report"]["coordinate_metadata_conflicts"] = len(warnings)
    for row in payload["sources"]:
        check = by_source.get((row["external_id"], row["provider"]))
        row["coordinate_state"] = check["coordinate_state"] if check else ""
        row["coordinate_state_check"] = (
            "in reported state"
            if check and check["coordinate_in_reported_state"] == "True"
            else "outside reported state"
            if check
            else "not checked"
        )
    for key in ("stops", "prices", "disagreements"):
        for row in payload[key]:
            stop_id = row.get("external_id") or row["OPIS Truckstop ID"].strip()
            row["coordinate_metadata_warning"] = "; ".join(warnings.get(stop_id, []))
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload))
    print(
        json.dumps(
            {key: len(payload[key]) for key in ("sources", "stops", "prices", "disagreements")}
        )
    )


if __name__ == "__main__":
    main()
