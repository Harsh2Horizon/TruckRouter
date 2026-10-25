"""Normalize and profile the supplied feed without a Django database or network access."""

import csv
import json
from collections import Counter
from pathlib import Path

from .parsing import parse_address


def profile_csv(filename: str, output_dir: str) -> dict:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    identities = set()
    with (destination / "normalized-addresses.jsonl").open("w") as target:
        with Path(filename).open(newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                address = parse_address(
                    {
                        "name": row["Truckstop Name"],
                        "address": row["Address"],
                        "city": row["City"],
                        "state": row["State"],
                    }
                )
                identities.add(row["OPIS Truckstop ID"])
                target.write(
                    json.dumps(
                        {
                            "external_id": row["OPIS Truckstop ID"],
                            "address": address.model_dump(mode="json"),
                        }
                    )
                    + "\n"
                )
                counts["rows"] += 1
                counts["with_store_id"] += bool(address.store_id)
                counts["with_highways"] += bool(address.highways)
                counts["with_exits"] += bool(address.exits)
                counts["with_unparsed_context"] += bool(address.unparsed_address)
                counts["country_" + address.country] += 1
    report = {**dict(counts), "unique_ids": len(identities), "geocodes_verified": 0}
    (destination / "parser-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
