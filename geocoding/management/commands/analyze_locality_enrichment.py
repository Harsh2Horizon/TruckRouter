"""Compare saved matching runs and package locality enrichment audit datasets."""

import csv
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from geocoding.acquisition import write_json

PROVIDERS = ("alltheplaces", "overture", "osm")


def rows(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def matched(row, provider=None):
    return (
        bool(row[f"{provider}_latitude"])
        if provider
        else any(row[f"{p}_latitude"] for p in PROVIDERS)
    )


class Command(BaseCommand):
    help = "Compare pre/post locality enrichment POI coverage and export review datasets"

    def add_arguments(self, parser):
        parser.add_argument("--baseline", default="output/geocoding-v3")
        parser.add_argument("--run", default="output/geocoding-v5")
        parser.add_argument("--completed-run", default="output/geocoding-v5-complete")
        parser.add_argument("--enrichment", default="output/locality-enrichment")
        parser.add_argument("--output-dir", default="outputs/truckroute-locality-enriched")

    def handle(self, *args, **options):
        baseline, run, completed, enrichment, output = (
            Path(options[k])
            for k in ("baseline", "run", "completed_run", "enrichment", "output_dir")
        )
        reports = {
            str(p): json.loads((p / "report.json").read_text())
            for p in (baseline, run, completed, enrichment)
        }
        if any(r["status"] != "completed" for r in reports.values()):
            raise CommandError("All matching, coverage and enrichment runs must be complete")
        validation = json.loads((enrichment / "validation.json").read_text())
        coverage_validation = json.loads((completed / "validation.json").read_text())
        assert validation["status"] == "passed" and coverage_validation["passed"]
        old = {r["external_id"]: r for r in rows(baseline / "geocodes.csv")}
        new = {r["external_id"]: r for r in rows(run / "geocodes.csv")}
        assert old.keys() == new.keys()
        same_sources = all(
            reports[str(baseline)]["sources"][p][field] == reports[str(run)]["sources"][p][field]
            for p in PROVIDERS
            for field in ("release", "unique_stored_records")
        )
        assert same_sources, "This comparison requires unchanged source extracts"
        report = {
            "baseline": str(baseline),
            "run": str(run),
            "unique_input_stops": len(new),
            "same_source_releases_and_record_counts": same_sources,
            "locality_before": reports[str(enrichment)]["before"],
            "locality_after": reports[str(enrichment)]["after"],
            "locality_changes": reports[str(enrichment)]["changes"],
            "providers": {},
            "candidate_retrieval": {},
        }
        for provider in (*PROVIDERS, None):
            counts = {
                "before": sum(matched(r, provider) for r in old.values()),
                "after": sum(matched(r, provider) for r in new.values()),
                "gained": sum(
                    not matched(old[k], provider) and matched(new[k], provider) for k in old
                ),
                "lost": sum(
                    matched(old[k], provider) and not matched(new[k], provider) for k in old
                ),
            }
            counts["net_gain"] = counts["after"] - counts["before"]
            if provider:
                counts["city_methods"] = dict(
                    Counter(
                        r[f"{provider}_city_method"] for r in new.values() if matched(r, provider)
                    )
                )
                report["providers"][provider] = counts
            else:
                report["any_source_poi"] = counts
        distinct_matches = defaultdict(set)
        for line in (run / "per-source.jsonl").open():
            source = json.loads(line)
            if source.get("matched_coordinate"):
                identity = source["matched_source"]
                distinct_matches[source["provider"]].add(
                    (identity["namespace"], identity["source_id"])
                )
        for provider in PROVIDERS:
            report["providers"][provider]["distinct_matched_source_records"] = len(
                distinct_matches[provider]
            )
        for label, directory in (("before", baseline), ("after", run)):
            requests, totals, truncated = Counter(), Counter(), Counter()
            identities = defaultdict(set)
            for line in (directory / "geocodes.jsonl").open():
                result = json.loads(line)
                retrieval = result["candidate_retrieval"]
                for provider, count in retrieval["counts"].items():
                    totals[provider] += count
                    requests[provider] += count > 0
                truncated.update(retrieval["truncated_sources"])
                for candidate in result["candidates"]:
                    identity = candidate["source"]
                    identities[identity["provider"]].add(
                        (identity["namespace"], identity["source_id"])
                    )
            report["candidate_retrieval"][label] = {
                p: {
                    "returned_rows_across_queries": totals[p],
                    "queries_with_candidates": requests[p],
                    "distinct_returned_source_records": len(identities[p]),
                    "truncated_queries": truncated[p],
                }
                for p in PROVIDERS
            }
            self.stdout.write(f"Counted candidate retrievals: {label}")
            self.stdout.flush()
        report["matching_statuses"] = reports[str(run)]["geocodes"]
        report["coverage"] = {
            k: reports[str(completed)][k]
            for k in (
                "input_rows",
                "rows_with_coordinates",
                "rows_without_coordinates",
                "unique_stop_methods",
                "method_counts",
            )
        }
        report["validation"] = validation
        output.mkdir(parents=True, exist_ok=True)
        transition_rows = []
        for key, row in new.items():
            transition = {k: row[k] for k in ("external_id", "name", "city", "state", "country")}
            for provider in PROVIDERS:
                for version, records in (("before", old), ("after", new)):
                    for field in ("latitude", "longitude", "score", "source_id", "status"):
                        transition[f"{provider}_{version}_{field}"] = records[key][
                            f"{provider}_{field}"
                        ]
                transition[f"{provider}_city_method"] = row[f"{provider}_city_method"]
            transition["any_source_before"] = matched(old[key])
            transition["any_source_after"] = matched(row)
            transition_rows.append(transition)
        with (output / "poi-coverage-before-after.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(transition_rows[0]))
            writer.writeheader()
            writer.writerows(transition_rows)
        for source, filename in (
            (run / "source-comparison.csv", "source-poi-comparison.csv"),
            (completed / "all-records-geocoded.csv", "all-records-geocoded.csv"),
            (completed / "final-stops.csv", "final-stops.csv"),
            (enrichment / "changes.csv", "source-locality-changes.csv"),
            (enrichment / "remaining-missing-localities.csv", "remaining-missing-localities.csv"),
            (enrichment / "validation.json", "locality-validation.json"),
            (completed / "validation.json", "coverage-validation.json"),
        ):
            shutil.copyfile(source, output / filename)
        write_json(output / "comparison-report.json", report)
        self.stdout.write(
            json.dumps(
                {
                    k: report[k]
                    for k in ("providers", "any_source_poi", "candidate_retrieval", "coverage")
                },
                indent=2,
            )
        )
