"""Optional geographic diagnostics; these checks do not change matching scores."""

import argparse
import csv
import json
from pathlib import Path

import duckdb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="output/geocoding-v3")
    args = parser.parse_args()
    output = Path(args.run)
    metadata = json.loads(Path("data/reference/usa-states-manifest.json").read_text())
    states = json.loads(Path(metadata["filename"]).read_text())
    with duckdb.connect(
        config={
            "extension_directory": str(Path("data/sources/duckdb-extensions").resolve()),
            "memory_limit": "1GB",
            "threads": "2",
        }
    ) as connection:
        connection.execute("LOAD spatial")
        connection.execute("CREATE TABLE states (state VARCHAR, geometry GEOMETRY)")
        connection.executemany(
            "INSERT INTO states VALUES (?,ST_GeomFromGeoJSON(?))",
            [
                (f["properties"]["shapeISO"].rsplit("-", 1)[-1], json.dumps(f["geometry"]))
                for f in states["features"]
            ],
        )
        cursor = connection.execute(
            """
            SELECT p.external_id,p.provider,p.source_id,p.source_name,p.source_city,
                   p.source_state,p.source_country,p.predicted_latitude,p.predicted_longitude,
                   s.state AS coordinate_state,
                   coalesce(s.state=p.source_state,false) AS coordinate_in_reported_state
            FROM read_csv(?,all_varchar=true) p
            LEFT JOIN states s ON ST_Covers(s.geometry,
                ST_Point(try_cast(p.predicted_longitude AS DOUBLE),
                         try_cast(p.predicted_latitude AS DOUBLE)))
            WHERE p.eligible_point='True' AND p.source_country='US'
            ORDER BY p.external_id,p.provider
        """,
            [str(output / "source-comparison.csv")],
        )
        rows = [
            dict(zip([c[0] for c in cursor.description], row, strict=True))
            for row in cursor.fetchall()
        ]
    with (output / "coordinate-state-diagnostics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    mismatches = [r for r in rows if not r["coordinate_in_reported_state"]]
    (output / "coordinate-state-summary.json").write_text(
        json.dumps(
            {
                "checked_us_source_points": len(rows),
                "outside_reported_state": len(mismatches),
                "outside_reported_state_rows": mismatches,
                "boundary_source": metadata["metadata"],
                "diagnostic_only": True,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"checked_us_source_points": len(rows), "outside_reported_state": len(mismatches)}
        )
    )


if __name__ == "__main__":
    main()
